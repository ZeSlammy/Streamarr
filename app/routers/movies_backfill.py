"""
Movie TMDB enrichment endpoints + actor/director search.

The enrichment job runs as a single in-process task — the UI polls
`/movies/backfill/status` for progress (same pattern as the series
backfill router).
"""

import asyncio
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import Session, select

from clients import tmdb
from database import get_session
from models import Movie, MovieTmdb
from services.movies_tmdb import enrich_batch, get_status

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.post("/movies/backfill", response_class=HTMLResponse)
async def start_backfill(request: Request, background_tasks: BackgroundTasks):
    """Kick the enrichment loop. Returns the live status partial."""
    status = get_status()
    if not status.running:
        # Schedule the long-running coroutine — keeping it inside FastAPI's
        # background_tasks keeps it tied to the app's event loop.
        background_tasks.add_task(enrich_batch, None)
    return templates.TemplateResponse(
        "partials/movies_backfill_status.html",
        {"request": request, "status": get_status(), "tmdb_enabled": tmdb.is_enabled()},
    )


@router.get("/movies/backfill/status", response_class=HTMLResponse)
async def backfill_status(request: Request, session: Session = Depends(get_session)):
    # Bundle coverage stats for the panel so the UI shows a "10/233 enriched"
    # progress hint when no job is running.
    enriched_count = session.exec(
        select(func.count()).select_from(MovieTmdb)
    ).one()
    total_with_tmdb = session.exec(
        select(func.count()).select_from(Movie).where(Movie.tmdb_id.is_not(None))
    ).one()
    return templates.TemplateResponse(
        "partials/movies_backfill_status.html",
        {
            "request": request,
            "status": get_status(),
            "tmdb_enabled": tmdb.is_enabled(),
            "enriched_count": enriched_count,
            "total_with_tmdb": total_with_tmdb,
        },
    )


def _search_movie_tmdb(session: Session, field: str, q: str) -> list[Movie]:
    """Return Movie rows whose `movie_tmdb.<field>` JSON contains `q`.

    `field` is one of "cast_field" or "director" — the column holds a JSON
    list of objects (cast) or strings (director). A LIKE on the JSON string
    is good enough — the actor/director names are unique enough that false
    positives are rare in practice.
    """
    if field not in {"cast_field", "director"}:
        return []
    needle = f"%{q}%"
    column = getattr(MovieTmdb, field)
    enriched_ids = session.exec(
        select(MovieTmdb.tmdb_id).where(column.ilike(needle))
    ).all()
    if not enriched_ids:
        return []
    return session.exec(
        select(Movie).where(Movie.tmdb_id.in_(enriched_ids)).order_by(Movie.name)
    ).all()


@router.get("/movies/search/actor", response_class=HTMLResponse)
async def search_actor(
    request: Request, q: str = "", session: Session = Depends(get_session)
):
    movies = _search_movie_tmdb(session, "cast_field", q) if q else []
    return templates.TemplateResponse(
        "partials/movies_grid.html",
        {
            "request": request,
            "movies": movies,
            "total": len(movies),
            "has_more": False,
            "next_offset": 0,
            "q": q,
            "category_id": "",
            "library_only": "",
        },
    )


@router.get("/movies/search/director", response_class=HTMLResponse)
async def search_director(
    request: Request, q: str = "", session: Session = Depends(get_session)
):
    movies = _search_movie_tmdb(session, "director", q) if q else []
    return templates.TemplateResponse(
        "partials/movies_grid.html",
        {
            "request": request,
            "movies": movies,
            "total": len(movies),
            "has_more": False,
            "next_offset": 0,
            "q": q,
            "category_id": "",
            "library_only": "",
        },
    )
