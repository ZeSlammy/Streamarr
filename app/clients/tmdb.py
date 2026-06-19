"""
TMDB API client.

Supports both auth styles — v3 API keys (32-char hex) use `?api_key=KEY`,
v4 Read Access Tokens (long JWT) use `Authorization: Bearer KEY`. The
client detects which by key length so users can paste either.

All functions return parsed dicts; failed lookups return `None` rather than
raising, so the enrichment loop can flag-and-continue.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("streamarr.tmdb")

BASE_URL = "https://api.themoviedb.org/3"
TIMEOUT = 15.0
V3_KEY_LENGTH = 32  # hex; v4 Bearer tokens are JWT-shaped and much longer


def _is_v3_key(key: str) -> bool:
    return len(key) <= V3_KEY_LENGTH and all(c in "0123456789abcdef" for c in key.lower())


def is_enabled() -> bool:
    """Truthy when TMDB_API_KEY is configured — gates the enrichment UI."""
    return bool(settings.tmdb_api_key)


class TmdbNotFound(Exception):
    """TMDB returned 404 — the id genuinely doesn't exist (vs. a transient error)."""


async def _get(
    path: str,
    params: Optional[dict] = None,
    *,
    raise_404: bool = False,
) -> Optional[dict]:
    if not is_enabled():
        return None
    url = f"{BASE_URL}{path}"
    headers = {"Accept": "application/json"}
    query = dict(params or {})
    key = settings.tmdb_api_key
    if _is_v3_key(key):
        query["api_key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(url, headers=headers, params=query)
            if r.status_code == 404:
                if raise_404:
                    raise TmdbNotFound(path)
                return None
            r.raise_for_status()
            return r.json()
    except (httpx.HTTPError, ValueError) as exc:
        # ValueError covers json.JSONDecodeError — TMDB occasionally serves a
        # non-JSON body on a 200 (edge-cache hiccup, rate-limit text page).
        # Treat it like a transient failure: no tombstone, retry next batch.
        logger.warning("TMDB %s failed: %s", path, exc)
        return None


async def get_movie(tmdb_id: int) -> Optional[dict]:
    """`/movie/{id}` — title, overview, runtime, genres, belongs_to_collection,
    backdrop/poster, release_date.

    Raises `TmdbNotFound` on 404 so callers can tombstone the id;
    returns None on transient failures (caller should retry later).
    """
    return await _get(f"/movie/{tmdb_id}", raise_404=True)


async def get_credits(tmdb_id: int) -> Optional[dict]:
    """`/movie/{id}/credits` — cast + crew lists."""
    return await _get(f"/movie/{tmdb_id}/credits")


async def get_collection(collection_id: int) -> Optional[dict]:
    """`/collection/{id}` — name, poster, parts list (other films in the set)."""
    return await _get(f"/collection/{collection_id}")
