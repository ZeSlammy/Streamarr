"""
EPGenius integration — pushes the active XTREAM URL + credentials to the
EPGenius playlist API after a failover so the curated EPG playlist stays
pointed at the live provider.

No-op when EPGENIUS_ENABLED=false or EPGENIUS_API_KEY / EPGENIUS_PLAYLIST_ID
are unset.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple
from urllib.parse import urlparse

import httpx

from config import settings

logger = logging.getLogger("streamarr.epgenius")

_ENDPOINT = "https://epgenius.org/api/public/update_creds"
_STREAM_URL_RE = re.compile(r"^https?://\S+/(?:live|movie|series)/\S+")


def _base_url(url: str) -> str:
    """Return scheme://host[:port] from a full URL."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.hostname}"
    if p.port:
        base += f":{p.port}"
    return base


async def push_credentials(new_url: str) -> Tuple[bool, str]:
    """POST new XTREAM base URL + creds to EPGenius.

    Returns (success, detail).
    """
    if not settings.epgenius_enabled:
        return False, "disabled"

    if not settings.epgenius_api_key or not settings.epgenius_playlist_id:
        logger.warning("EPGenius push skipped: EPGENIUS_API_KEY or EPGENIUS_PLAYLIST_ID not set")
        return False, "not configured"

    try:
        playlist_id: int | str = int(settings.epgenius_playlist_id)
    except ValueError:
        playlist_id = settings.epgenius_playlist_id

    payload = {
        "playlist_id": playlist_id,
        "dns": new_url,
        "username": settings.xtream_username,
        "password": settings.xtream_password,
    }
    headers = {
        "Authorization": settings.epgenius_api_key,
        "X-Discord-ID": settings.epgenius_discord_id,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(_ENDPOINT, json=payload, headers=headers)
        if r.status_code >= 300:
            logger.warning("EPGenius API returned %s: %s", r.status_code, r.text[:200])
            return False, f"HTTP {r.status_code}"
        logger.info("EPGenius credentials pushed (dns=%s)", new_url)
        return True, "ok"
    except httpx.HTTPError as exc:
        logger.warning("EPGenius push failed: %s", exc)
        return False, str(exc)


async def verify_m3u(active_url: str) -> dict:
    """Fetch EPGENIUS_M3U_URL, extract the base URL from the first stream line,
    and compare it with `active_url`.

    Returns a dict with:
      - ok (bool): True when m3u_base matches active_base
      - active_base (str): scheme://host[:port] of the current active URL
      - m3u_base (str | None): base URL found in the playlist, or None on error
      - detail (str): human-readable status
    """
    m3u_url = settings.epgenius_m3u_url
    active_base = _base_url(active_url)

    if not m3u_url:
        return {"ok": False, "active_base": active_base, "m3u_base": None,
                "detail": "EPGENIUS_M3U_URL not configured"}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            r = await client.get(m3u_url)
        if r.status_code >= 300:
            return {"ok": False, "active_base": active_base, "m3u_base": None,
                    "detail": f"M3U fetch returned HTTP {r.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "active_base": active_base, "m3u_base": None,
                "detail": f"M3U fetch failed: {exc}"}

    m3u_base: Optional[str] = None
    for line in r.text.splitlines():
        line = line.strip()
        if _STREAM_URL_RE.match(line):
            m3u_base = _base_url(line)
            break

    if m3u_base is None:
        return {"ok": False, "active_base": active_base, "m3u_base": None,
                "detail": "No stream URL found in M3U"}

    match = m3u_base == active_base
    detail = "M3U matches active URL" if match else f"M3U still points at {m3u_base} (expected {active_base})"
    logger.info("EPGenius verify: %s", detail)
    return {"ok": match, "active_base": active_base, "m3u_base": m3u_base, "detail": detail}
