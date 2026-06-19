"""
TMDB enrichment for movies. Populates `movie_tmdb`, `collections`, and
`collection_parts` from the TMDB API, and refreshes derived counts on
the collections table.

Triggers:
- `/movies/backfill` button (UI)
- Nightly scheduler (capped batch)

Cache policy: rows < 30 days old are not re-fetched.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from clients import tmdb
from clients.tmdb import TmdbNotFound
from database import engine
from models import Collection, CollectionPart, Movie, MovieTmdb

logger = logging.getLogger("streamarr.movies_tmdb")

CACHE_TTL = timedelta(days=30)
# Tombstoned 404s are re-attempted after this — TMDB does occasionally fix
# typos / restore deleted entries, so the miss isn't permanent. 30 days mirrors
# the success-cache TTL.
MISS_TTL = timedelta(days=30)
RATE_LIMIT_DELAY = 0.1  # seconds between TMDB calls — well under TMDB's 40 req/s


@dataclass
class EnrichStatus:
    total_candidates: int = 0
    processed: int = 0
    skipped_cached: int = 0
    failed: int = 0
    running: bool = False
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    last_error: Optional[str] = None
    errors: list[str] = field(default_factory=list)


# Module-level status. Single concurrent backfill — the UI polls this state.
_status = EnrichStatus()
_status_lock = asyncio.Lock()


def get_status() -> EnrichStatus:
    return _status


def count_tombstones() -> int:
    """Total tmdb_ids tombstoned as 404 (regardless of MISS_TTL freshness)."""
    with Session(engine) as session:
        return len(session.exec(
            select(MovieTmdb.tmdb_id).where(MovieTmdb.not_found == True)  # noqa: E712
        ).all())


def _is_cache_fresh(row: MovieTmdb) -> bool:
    if row.enriched_at is None:
        return False
    return datetime.utcnow() - row.enriched_at < CACHE_TTL


def _is_miss_fresh(row: MovieTmdb) -> bool:
    """True when this row is a still-valid tombstone (recent 404)."""
    if not row.not_found:
        return False
    if row.last_attempt_at is None:
        return False
    return datetime.utcnow() - row.last_attempt_at < MISS_TTL


def _candidate_movies(limit: Optional[int] = None) -> list[int]:
    """Return tmdb_ids of movies in the catalog that need enrichment.

    Candidate when the id has no row in `movie_tmdb`, OR the row is stale
    (> CACHE_TTL since success), OR the tombstone is stale (> MISS_TTL since
    the last 404). Recent successes and recent 404s are both skipped.
    """
    with Session(engine) as session:
        rows = session.exec(
            select(Movie.tmdb_id).where(Movie.tmdb_id.is_not(None)).distinct()
        ).all()
        if not rows:
            return []
        existing = {
            t.tmdb_id: t for t in session.exec(select(MovieTmdb)).all()
        }
    skip = {
        tid for tid, row in existing.items()
        if _is_cache_fresh(row) or _is_miss_fresh(row)
    }
    candidates = [tid for tid in rows if tid not in skip]
    if limit is not None:
        candidates = candidates[:limit]
    return candidates


def _extract_director(crew: list[dict]) -> list[str]:
    return [c.get("name", "") for c in crew if c.get("job") == "Director" and c.get("name")]


def _extract_cast(cast: list[dict], top: int = 20) -> list[dict]:
    return [
        {"name": c.get("name", ""), "character": c.get("character", ""), "order": c.get("order", 0)}
        for c in cast[:top]
        if c.get("name")
    ]


def _write_miss_tombstone(tmdb_id: int) -> None:
    """Persist a 404 result so future candidate scans skip this id for MISS_TTL."""
    now = datetime.utcnow()
    with Session(engine) as session:
        row = session.get(MovieTmdb, tmdb_id)
        if row is None:
            row = MovieTmdb(tmdb_id=tmdb_id)
        row.not_found = True
        row.last_attempt_at = now
        session.add(row)
        session.commit()


async def enrich_one(tmdb_id: int) -> bool:
    """Fetch + persist movie_tmdb + (optional) collection for one tmdb_id.

    Returns True on success, False on any TMDB miss/error. On a TMDB 404
    we tombstone the id (`not_found=True`) so it gets skipped for MISS_TTL;
    transient errors return False without tombstoning so they retry next batch.
    """
    try:
        movie = await tmdb.get_movie(tmdb_id)
    except TmdbNotFound:
        _write_miss_tombstone(tmdb_id)
        return False
    if not movie:
        return False  # transient — don't tombstone, retry next batch
    credits = await tmdb.get_credits(tmdb_id) or {}

    collection_obj = movie.get("belongs_to_collection") or None
    collection_id = (collection_obj or {}).get("id") if collection_obj else None
    collection_name = (collection_obj or {}).get("name") if collection_obj else None

    with Session(engine) as session:
        row = session.get(MovieTmdb, tmdb_id)
        if row is None:
            row = MovieTmdb(tmdb_id=tmdb_id)
        row.title = movie.get("title", "")
        row.original_title = movie.get("original_title", "")
        row.overview = movie.get("overview", "") or ""
        row.release_date = movie.get("release_date", "") or ""
        row.runtime = movie.get("runtime")
        row.genres = json.dumps([g["name"] for g in movie.get("genres", []) if g.get("name")])
        row.cast_field = json.dumps(_extract_cast(credits.get("cast", []) or []))
        row.director = json.dumps(_extract_director(credits.get("crew", []) or []))
        row.poster_path = movie.get("poster_path", "") or ""
        row.backdrop_path = movie.get("backdrop_path", "") or ""
        row.collection_id = collection_id
        row.collection_name = collection_name
        row.original_language = movie.get("original_language") or None
        now = datetime.utcnow()
        row.enriched_at = now
        row.last_attempt_at = now
        row.not_found = False  # clear any previous tombstone — id resolves now
        session.add(row)
        session.commit()

    if collection_id:
        await _ensure_collection(collection_id)

    return True


async def _ensure_collection(collection_id: int) -> None:
    """Fetch + persist a TMDB collection. Skip if already present and fresh."""
    with Session(engine) as session:
        existing = session.get(Collection, collection_id)
    if existing is not None:
        return

    data = await tmdb.get_collection(collection_id)
    if not data:
        return
    parts = data.get("parts") or []
    with Session(engine) as session:
        session.add(
            Collection(
                collection_id=collection_id,
                name=data.get("name", ""),
                poster_path=data.get("poster_path") or "",
                tmdb_total_parts=len(parts),
                available_count=0,
                in_library_count=0,
            )
        )
        # Replace parts to keep this idempotent if collection structure changes later.
        for old in session.exec(
            select(CollectionPart).where(CollectionPart.collection_id == collection_id)
        ).all():
            session.delete(old)
        for p in parts:
            pid = p.get("id")
            title = p.get("title") or p.get("original_title") or ""
            if not pid or not title:
                continue
            session.add(
                CollectionPart(
                    collection_id=collection_id,
                    tmdb_id=int(pid),
                    title=title,
                    release_date=p.get("release_date") or "",
                    poster_path=p.get("poster_path") or "",
                )
            )
        session.commit()


def rebuild_collection_counts() -> None:
    """Refresh `available_count` and `in_library_count` on every collection
    by joining `collection_parts` against the current `movies` table."""
    with Session(engine) as session:
        collections = session.exec(select(Collection)).all()
        # Build a tmdb_id → (in_catalog, in_library) map once.
        movies_by_tmdb: dict[int, bool] = {}
        in_lib_tmdb: set[int] = set()
        for m in session.exec(select(Movie).where(Movie.tmdb_id.is_not(None))).all():
            movies_by_tmdb[m.tmdb_id] = True
            if m.in_library:
                in_lib_tmdb.add(m.tmdb_id)
        for c in collections:
            parts = session.exec(
                select(CollectionPart).where(CollectionPart.collection_id == c.collection_id)
            ).all()
            c.available_count = sum(1 for p in parts if p.tmdb_id in movies_by_tmdb)
            c.in_library_count = sum(1 for p in parts if p.tmdb_id in in_lib_tmdb)
            session.add(c)
        session.commit()


async def enrich_batch(limit: Optional[int] = None) -> EnrichStatus:
    """Run enrichment for up to `limit` candidate movies. Single concurrent
    invocation — second caller exits immediately if one is already running."""
    global _status
    if _status.running:
        return _status
    if not tmdb.is_enabled():
        # Don't claim "running" if there's nothing we can do.
        _status.last_error = "TMDB_API_KEY not configured"
        return _status

    async with _status_lock:
        if _status.running:
            return _status
        candidates = _candidate_movies(limit=limit)
        _status = EnrichStatus(
            total_candidates=len(candidates),
            running=True,
            started_at=datetime.utcnow(),
        )

    try:
        for tmdb_id in candidates:
            try:
                ok = await enrich_one(tmdb_id)
                _status.processed += 1
                if not ok:
                    _status.failed += 1
            except Exception as exc:  # noqa: BLE001
                _status.failed += 1
                _status.last_error = str(exc)
                if len(_status.errors) < 10:
                    _status.errors.append(f"tmdb_id={tmdb_id}: {exc}")
                logger.exception("enrich failed for tmdb_id=%s", tmdb_id)
            await asyncio.sleep(RATE_LIMIT_DELAY)
        rebuild_collection_counts()
    finally:
        _status.running = False
        _status.finished_at = datetime.utcnow()
    return _status
