"""
TMDB Collections page + Radarr request endpoint.

Each card shows availability/library counts and the per-film part list
(expanded inline). Films that exist in the XTREAM catalog get Add/Remove
buttons; films that don't get a Radarr Request button when Radarr is
configured.
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from clients import radarr
from database import get_session
from models import Collection, CollectionPart, Movie, MovieRequest, MovieTmdb
from services.movies_strm import sync_movie

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/collections", response_class=HTMLResponse)
async def collections_page(request: Request, session: Session = Depends(get_session)):
    # Sort: most-available first, ties broken by name.
    cols = session.exec(
        select(Collection).order_by(
            Collection.available_count.desc(), Collection.name
        )
    ).all()
    return templates.TemplateResponse(
        "collections.html",
        {
            "request": request,
            "collections": cols,
            "radarr_enabled": radarr.is_enabled(),
        },
    )


def _part_state(
    part: CollectionPart,
    movies_by_tmdb: dict[int, Movie],
    requested_by_tmdb: set[int],
) -> dict:
    movie = movies_by_tmdb.get(part.tmdb_id)
    return {
        "part": part,
        "movie": movie,
        "in_catalog": movie is not None,
        "in_library": bool(movie and movie.in_library),
        "requested": part.tmdb_id in requested_by_tmdb,
    }


@router.get("/collections/{collection_id}/parts", response_class=HTMLResponse)
async def collection_parts(
    collection_id: int, request: Request, session: Session = Depends(get_session)
):
    parts = session.exec(
        select(CollectionPart)
        .where(CollectionPart.collection_id == collection_id)
        .order_by(CollectionPart.release_date)
    ).all()
    if not parts:
        return HTMLResponse('<div class="text-muted small">No parts known yet.</div>')

    tmdb_ids = [p.tmdb_id for p in parts]
    movies = session.exec(select(Movie).where(Movie.tmdb_id.in_(tmdb_ids))).all()
    movies_by_tmdb = {m.tmdb_id: m for m in movies}
    requested = set(
        session.exec(
            select(MovieRequest.tmdb_id).where(MovieRequest.tmdb_id.in_(tmdb_ids))
        ).all()
    )
    rows = [_part_state(p, movies_by_tmdb, requested) for p in parts]
    return templates.TemplateResponse(
        "partials/collection_parts.html",
        {
            "request": request,
            "rows": rows,
            "radarr_enabled": radarr.is_enabled(),
        },
    )


@router.post("/collections/{collection_id}/add-all", response_class=HTMLResponse)
async def collection_add_all(
    collection_id: int, request: Request, session: Session = Depends(get_session)
):
    """Add every available film in this collection that isn't already in the library."""
    parts = session.exec(
        select(CollectionPart).where(CollectionPart.collection_id == collection_id)
    ).all()
    tmdb_ids = [p.tmdb_id for p in parts]
    candidates = session.exec(
        select(Movie)
        .where(Movie.tmdb_id.in_(tmdb_ids))
        .where(Movie.in_library == False)  # noqa: E712
    ).all()
    added = 0
    errors = 0
    for movie in candidates:
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
            added += 1
        except Exception:  # noqa: BLE001
            errors += 1

    # Bump cached counts on the parent collection so the badge updates without
    # waiting for the next nightly rebuild.
    col = session.get(Collection, collection_id)
    if col is not None:
        col.in_library_count += added
        session.add(col)
        session.commit()

    msg = f"Added {added} film(s) to library"
    if errors:
        msg += f" ({errors} .strm write errors — nightly job will retry)"
    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": msg, "status": "success" if added else "info"},
    )


@router.post("/movies/request/{tmdb_id}", response_class=HTMLResponse)
async def request_film(
    tmdb_id: int, request: Request, session: Session = Depends(get_session)
):
    """Send a film to Radarr. Used by Collections rows for films that aren't
    in the XTREAM catalog."""
    if not radarr.is_enabled():
        raise HTTPException(503, "Radarr is not configured")

    existing = session.get(MovieRequest, tmdb_id)
    if existing is not None:
        return _request_button_html(tmdb_id, state="already")

    tmdb_row = session.get(MovieTmdb, tmdb_id)
    # Fall back to the collection part data when we never enriched this film
    # (which is the typical case — collection parts often aren't in XTREAM and
    # so were never enriched via the per-movie path).
    title = (tmdb_row.title if tmdb_row else None) or ""
    year: Optional[str] = None
    if tmdb_row and tmdb_row.release_date:
        year = tmdb_row.release_date[:4] if len(tmdb_row.release_date) >= 4 else None
    if not title:
        part = session.exec(
            select(CollectionPart).where(CollectionPart.tmdb_id == tmdb_id)
        ).first()
        if part:
            title = part.title
            if part.release_date and len(part.release_date) >= 4:
                year = part.release_date[:4]
    if not title:
        raise HTTPException(404, "No metadata for this tmdb_id — enrich first.")

    result = await radarr.add_movie(tmdb_id, title, year)
    if result["status"] in {"created", "exists"}:
        session.add(
            MovieRequest(
                tmdb_id=tmdb_id,
                title=title,
                radarr_id=result.get("radarr_id"),
            )
        )
        session.commit()
        return _request_button_html(tmdb_id, state="requested" if result["status"] == "created" else "already")
    # Surface the error to the user inline — htmx will swap this back into the row.
    msg = result.get("message", "Radarr returned an error")
    return HTMLResponse(
        f'<span class="text-danger small" title="{msg}">Failed</span>',
        status_code=502,
    )


def _request_button_html(tmdb_id: int, state: str) -> HTMLResponse:
    if state == "requested":
        label = '<i class="bi bi-check2-circle me-1"></i>Requested'
    else:  # "already"
        label = '<i class="bi bi-check2 me-1"></i>Already in Radarr'
    return HTMLResponse(
        f'<button class="btn btn-success btn-sm w-100" disabled '
        f'id="request-{tmdb_id}">{label}</button>'
    )
