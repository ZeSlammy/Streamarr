"""Movies browse + library management. Mirrors `routers/browse.py` and
`routers/subscriptions.py` for the VOD side."""

from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.sql import ColumnElement
from sqlmodel import Session, select

from config import settings
from database import get_session
from models import Movie, MovieCategory
from services.languages import derive_movie_language, movie_language_clause
from services.movies_strm import (
    refresh_vod_catalog,
    remove_movie_file,
    sync_movie,
)


def _language_filter_clause() -> ColumnElement | None:
    """The active language filter, or None when the filter is disabled.

    Disabled when `LANGUAGE_FILTER_ENABLED=false` or when no allow-list at all
    is configured (no audio codes + no unknown + no subs-only)."""
    if not settings.language_filter_enabled:
        return None
    allowed = settings.allowed_languages_set
    if not allowed and not settings.allow_language_unknown:
        return None
    return movie_language_clause(
        allowed,
        settings.allow_language_unknown,
        settings.allow_language_subs_only,
    )

router = APIRouter()
templates = Jinja2Templates(directory="templates")

PAGE_SIZE = 60


@router.get("/movies", response_class=HTMLResponse)
async def movies_page(request: Request, session: Session = Depends(get_session)):
    lang_clause = _language_filter_clause()

    catalog_query = select(Movie)
    if lang_clause is not None:
        catalog_query = catalog_query.where(lang_clause)
    catalog_total = len(session.exec(catalog_query).all())

    in_library_count = len(
        session.exec(select(Movie).where(Movie.in_library == True)).all()  # noqa: E712
    )

    # Categories: only those that have ≥1 movie matching the active filter.
    # When the filter is off, fall back to "categories that have ≥1 movie at all"
    # rather than the raw MovieCategory table — keeps the dropdown manageable
    # even with the full catalog.
    cat_id_query = select(Movie.category_id).where(
        Movie.category_id.is_not(None), Movie.category_id != ""
    )
    if lang_clause is not None:
        cat_id_query = cat_id_query.where(lang_clause)
    populated_cat_ids = {
        cid for cid in session.exec(cat_id_query.distinct()).all() if cid
    }
    if populated_cat_ids:
        categories = session.exec(
            select(MovieCategory)
            .where(MovieCategory.category_id.in_(populated_cat_ids))
            .order_by(MovieCategory.category_name)
        ).all()
    else:
        categories = []

    return templates.TemplateResponse(
        "movies.html",
        {
            "request": request,
            "categories": categories,
            "catalog_total": catalog_total,
            "in_library_count": in_library_count,
        },
    )


@router.get("/movies/results", response_class=HTMLResponse)
async def movies_results(
    request: Request,
    q: str = "",
    category_id: str = "",
    library_only: str = "",
    show_all: str = "",
    offset: int = 0,
    session: Session = Depends(get_session),
):
    query = select(Movie).order_by(Movie.name)
    if q:
        query = query.where(Movie.name.ilike(f"%{q}%"))
    if category_id:
        query = query.where(Movie.category_id == category_id)
    if library_only == "1":
        query = query.where(Movie.in_library == True)  # noqa: E712
    else:
        # The language filter bypasses for `library_only` (already-added wins)
        # and for `show_all=1` (debug escape hatch).
        if show_all != "1":
            lang_clause = _language_filter_clause()
            if lang_clause is not None:
                query = query.where(lang_clause)

    all_movies = session.exec(query).all()
    total = len(all_movies)
    page = all_movies[offset : offset + PAGE_SIZE]
    next_offset = offset + PAGE_SIZE
    has_more = next_offset < total

    template = "partials/movies_page.html" if offset > 0 else "partials/movies_grid.html"
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "movies": page,
            "total": total,
            "has_more": has_more,
            "next_offset": next_offset,
            "q": q,
            "category_id": category_id,
            "library_only": library_only,
        },
    )


@router.post("/movies/{vod_id}/add", response_class=HTMLResponse)
async def movies_add(
    vod_id: int, request: Request, session: Session = Depends(get_session)
):
    movie = session.get(Movie, vod_id)
    if movie:
        movie.in_library = True
        if movie.added_at is None:
            movie.added_at = datetime.utcnow()
        session.add(movie)
        session.commit()
        session.refresh(movie)
        try:
            sync_movie(movie)
            movie.last_synced = datetime.utcnow()
            session.add(movie)
            session.commit()
            session.refresh(movie)
        except Exception:
            # .strm write failure shouldn't block the DB add; nightly job will retry.
            pass
    return templates.TemplateResponse(
        "partials/movie_card.html", {"request": request, "m": movie}
    )


@router.post("/movies/{vod_id}/remove", response_class=HTMLResponse)
async def movies_remove(
    vod_id: int, request: Request, session: Session = Depends(get_session)
):
    movie = session.get(Movie, vod_id)
    if movie:
        movie.in_library = False
        movie.last_synced = None
        session.add(movie)
        session.commit()
        session.refresh(movie)
        try:
            remove_movie_file(movie)
        except Exception:
            pass
    return templates.TemplateResponse(
        "partials/movie_card.html", {"request": request, "m": movie}
    )


@router.post("/movies/language/recompute")
async def movies_language_recompute(session: Session = Depends(get_session)):
    """Walk every Movie row, derive lang/lang_source/subs_only from the
    current category name + title. Idempotent — safe to re-run after the
    parser is tweaked or after a fresh catalog import.

    Returns a histogram so the caller can sanity-check the distribution
    without round-tripping through the DB.
    """
    cats = session.exec(select(MovieCategory)).all()
    cats_by_id = {c.category_id: c.category_name for c in cats}

    movies = session.exec(select(Movie)).all()
    lang_counts: Counter[str] = Counter()
    subs_only_count = 0
    source_counts: Counter[str] = Counter()
    unknown_count = 0

    for m in movies:
        cat_name = cats_by_id.get(m.category_id or "", "")
        lang, subs_only, source = derive_movie_language(cat_name, m.name)
        m.lang = lang
        m.lang_source = source
        m.subs_only = subs_only
        session.add(m)
        if lang is None:
            unknown_count += 1
        else:
            lang_counts[lang] += 1
        if source:
            source_counts[source] += 1
        if subs_only:
            subs_only_count += 1
    session.commit()

    return JSONResponse(
        {
            "total": len(movies),
            "unknown": unknown_count,
            "subs_only": subs_only_count,
            "lang_histogram": dict(lang_counts.most_common()),
            "source_histogram": dict(source_counts),
        }
    )


@router.post("/movies/catalog/refresh", response_class=HTMLResponse)
async def movies_refresh_catalog(request: Request):
    """Kick off a VOD catalog refresh. Runs synchronously — the 40k catalog
    fetch is one HTTP call upstream and the upsert loop is quick (a few
    seconds). Returning a toast on completion is simpler than polling."""
    try:
        stats = await refresh_vod_catalog()
        msg = (
            f"VOD catalog refreshed: {stats.catalog_total} movies "
            f"({stats.catalog_new} new, {stats.catalog_updated} updated)."
        )
        status = "success"
    except Exception as exc:  # noqa: BLE001
        msg = f"VOD catalog refresh failed: {exc}"
        status = "error"
    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": msg, "status": status},
    )
