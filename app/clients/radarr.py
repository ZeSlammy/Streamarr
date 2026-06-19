"""
Radarr v3 API client. Used by the Collections UI Request button for films
that aren't in the XTREAM catalog. All public functions return a small
result dict with `status` (`"created"`, `"exists"`, `"error"`) and
optionally `radarr_id` / `message`.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("streamarr.radarr")

TIMEOUT = 20.0


def is_enabled() -> bool:
    return bool(settings.radarr_url and settings.radarr_api_key)


def _base() -> str:
    return settings.radarr_url.rstrip("/")


def _headers() -> dict:
    return {"X-Api-Key": settings.radarr_api_key, "Accept": "application/json"}


async def system_status() -> Optional[dict]:
    if not is_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(f"{_base()}/api/v3/system/status", headers=_headers())
            r.raise_for_status()
            return r.json()
    except httpx.HTTPError as exc:
        logger.warning("radarr system_status failed: %s", exc)
        return None


async def add_movie(tmdb_id: int, title: str, year: Optional[str]) -> dict:
    """POST /api/v3/movie. Returns:
        {"status": "created", "radarr_id": N}    on 201
        {"status": "exists"}                     when Radarr says it's already added
        {"status": "error", "message": "..."}    on other failures
    """
    if not is_enabled():
        return {"status": "error", "message": "Radarr not configured"}

    body = {
        "tmdbId": int(tmdb_id),
        "title": title,
        "qualityProfileId": settings.radarr_quality_profile_id,
        "rootFolderPath": settings.radarr_root_folder,
        "monitored": True,
        "addOptions": {"searchForMovie": True},
    }
    if year:
        try:
            body["year"] = int(year)
        except (TypeError, ValueError):
            pass

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{_base()}/api/v3/movie", headers=_headers(), json=body
            )
    except httpx.HTTPError as exc:
        return {"status": "error", "message": f"connection failed: {exc}"}

    if r.status_code == 201:
        data = r.json()
        return {"status": "created", "radarr_id": data.get("id")}

    # Radarr returns 400 with a `MovieExistsValidator` error code when the
    # film is already tracked. The free-text error varies across versions —
    # match on the error array content.
    if r.status_code == 400:
        try:
            errs = r.json()
        except ValueError:
            errs = []
        if isinstance(errs, list):
            for e in errs:
                if isinstance(e, dict):
                    msg = (e.get("errorMessage") or "").lower()
                    if "exist" in msg or "already" in msg:
                        return {"status": "exists"}
        return {"status": "error", "message": f"HTTP 400: {r.text[:200]}"}

    return {"status": "error", "message": f"HTTP {r.status_code}: {r.text[:200]}"}
