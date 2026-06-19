"""
Xtream-compatible mirror endpoint.

Clients (Dispatcharr, TiviMate, Jellyfin via .strm, Kodi, ...) point at
`<MIRROR_PUBLIC_URL>/xtream` with any username/password. The picker substitutes
the real XTREAM_USERNAME/PASSWORD when talking upstream, and rewrites m3u
playlists so the real credentials never leave the server.

Endpoints:

    GET /xtream/player_api.php?...   JSON API proxy (cred-scrubbed)
    GET /xtream/get.php?...          m3u / m3u_plus playlist (URLs rewritten)
    GET /xtream/xmltv.php?...        EPG XML passthrough
    GET /xtream/{series|movie|live}/{u}/{p}/{filename}
                                     302 redirect to active upstream stream URL
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Iterable, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlmodel import Session, select

from clients.xtream import active_base
from config import settings
from database import engine
from models import Category, Movie, MovieCategory, Series
from services.providers import run_failover_check

router = APIRouter(prefix="/xtream", tags=["mirror"])
logger = logging.getLogger("xtream-picker.proxy")

# Opportunistic failover state — single-flight + cooldown so concurrent failing
# requests don't each kick off their own ~10s probe of every candidate URL.
_failover_lock = asyncio.Lock()
_last_failover_at: float = 0.0
_FAILOVER_COOLDOWN_S = 60.0

STREAM_TYPES = {"series", "movie", "live"}

# Actions handled from local state (or short-circuited) without hitting upstream.
LOCAL_SERIES_ACTION = "get_series"
LOCAL_SERIES_CATEGORIES_ACTION = "get_series_categories"
LOCAL_VOD_ACTION = "get_vod_streams"
LOCAL_VOD_CATEGORIES_ACTION = "get_vod_categories"
LOCAL_VOD_INFO_ACTION = "get_vod_info"
# Live channels remain stubbed as empty — Streamarr is .strm-only, no live TV.
EMPTY_ACTIONS = {
    "get_live_streams",
    "get_live_categories",
}


def _real_creds_params(incoming: dict) -> dict:
    """Strip whatever creds the client sent and substitute the real ones."""
    forwarded = {k: v for k, v in incoming.items() if k not in {"username", "password"}}
    forwarded["username"] = settings.xtream_username
    forwarded["password"] = settings.xtream_password
    return forwarded


def _forward_headers(request: Request) -> dict:
    """Pass-through a minimal, safe subset of client headers."""
    out = {}
    for key in ("user-agent", "accept", "accept-language"):
        value = request.headers.get(key)
        if value:
            out[key] = value
    return out


def _mirror_base() -> str:
    return (settings.mirror_public_url or "").rstrip("/") + "/xtream"


def _build_rewrite_regexes() -> list[tuple[re.Pattern[str], str]]:
    real_u = re.escape(settings.xtream_username)
    real_p = re.escape(settings.xtream_password)
    mirror = _mirror_base()
    mu = settings.mirror_username
    mp = settings.mirror_password
    return [
        # http(s)://host[:port]/(series|movie|live)/<real_u>/<real_p>/...
        (
            re.compile(rf"https?://[^/\s]+/(series|movie|live)/{real_u}/{real_p}/"),
            lambda m: f"{mirror}/{m.group(1)}/{mu}/{mp}/",
        ),
        # Bare /username/password/ form some providers use for live streams.
        (
            re.compile(rf"https?://[^/\s]+/{real_u}/{real_p}/"),
            lambda m: f"{mirror}/live/{mu}/{mp}/",
        ),
    ]


def _rewrite_line(line: str, rules: Iterable[tuple[re.Pattern[str], str]]) -> str:
    for pattern, repl in rules:
        line = pattern.sub(repl, line)
    return line


def _looks_dead(status_code: int, content_type: Optional[str]) -> Optional[str]:
    """
    Heuristic: does this upstream response look like a dead/banned URL?
    Returns a short reason string if dead, None otherwise.

    Conservative on purpose — we want a clear signal, not every transient
    upstream hiccup, since a swap rewrites URL.md.
    """
    if status_code in (401, 403):
        return f"HTTP {status_code}"
    if 500 <= status_code < 600:
        return f"HTTP {status_code}"
    # Provider blocked → HTML "blocked" page where JSON/M3U was expected
    if status_code == 200 and content_type and "html" in content_type.lower():
        return "HTML response (expected JSON/M3U)"
    return None


async def _trigger_failover(reason: str) -> bool:
    """
    Single-flight, cooldown-gated failover. Returns True if the caller should
    retry the upstream request against (possibly new) active_base().

    - First caller through the lock with an expired cooldown runs the probe.
    - Concurrent callers queue on the lock; when they enter, the cooldown is
      fresh, so they just return True so they pick up whatever URL the
      predecessor swapped to.
    """
    global _last_failover_at
    async with _failover_lock:
        now = time.monotonic()
        if now - _last_failover_at < _FAILOVER_COOLDOWN_S:
            # Recent check by us or a predecessor — caller retries once
            # against whatever active_base() returns now.
            return True
        logger.warning("Opportunistic failover triggered: %s", reason)
        try:
            result = await run_failover_check()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Opportunistic failover failed")
            _last_failover_at = time.monotonic()  # avoid retry storm
            return False
        _last_failover_at = time.monotonic()
        if result.get("swapped"):
            logger.warning(
                "Failover swapped upstream %s -> %s (%s)",
                result.get("previous"),
                result.get("current"),
                result.get("reason"),
            )
            return True
        logger.warning(
            "Failover did not swap (reason: %s)", result.get("reason")
        )
        return False


def _series_to_dict(s: Series, num: int) -> dict:
    """Render a Series row in the shape XTREAM clients expect from get_series."""
    rating = s.rating if s.rating not in (None, "") else "0"
    rating_5 = s.rating_5based if s.rating_5based is not None else 0
    release = s.release_date or ""
    year = release[:4] if len(release) >= 4 and release[:4].isdigit() else ""
    category_id = s.category_id or ""
    try:
        category_ids = [int(category_id)] if category_id else []
    except (TypeError, ValueError):
        category_ids = []
    return {
        "num": num,
        "name": s.name,
        "title": s.name,
        "series_id": s.series_id,
        "stream_type": "series",
        "cover": s.cover or "",
        "plot": s.plot or "",
        "cast": "",
        "director": "",
        "genre": s.genre or "",
        "releaseDate": release,
        "release_date": release,
        "last_modified": "",
        "rating": str(rating),
        "rating_5based": rating_5,
        "backdrop_path": [],
        "youtube_trailer": "",
        "episode_run_time": "",
        "category_id": category_id,
        "category_ids": category_ids,
        "year": year,
    }


def _local_get_series() -> list[dict]:
    with Session(engine) as session:
        rows = session.exec(
            select(Series).where(Series.subscribed == True).order_by(Series.name)  # noqa: E712
        ).all()
    return [_series_to_dict(s, idx) for idx, s in enumerate(rows, start=1)]


def _local_get_series_categories() -> list[dict]:
    """Only categories that have ≥1 subscribed show, named from the category table."""
    with Session(engine) as session:
        subscribed_cat_ids = session.exec(
            select(Series.category_id)
            .where(Series.subscribed == True)  # noqa: E712
            .where(Series.category_id.is_not(None))
            .where(Series.category_id != "")
            .distinct()
        ).all()
        if not subscribed_cat_ids:
            return []
        cats = session.exec(
            select(Category).where(Category.category_id.in_(subscribed_cat_ids))
        ).all()
    cats_by_id = {c.category_id: c.category_name for c in cats}
    out = []
    for cid in subscribed_cat_ids:
        out.append(
            {
                "category_id": str(cid),
                "category_name": cats_by_id.get(cid, str(cid)),
                "parent_id": 0,
            }
        )
    out.sort(key=lambda c: c["category_name"].lower())
    return out


def _movie_to_dict(m: Movie, num: int) -> dict:
    """Render a Movie row in the shape XTREAM clients expect from get_vod_streams."""
    rating = m.rating if m.rating not in (None, "") else "0"
    rating_5 = m.rating_5based if m.rating_5based is not None else 0
    return {
        "num": num,
        "name": m.name,
        "title": m.name,
        "stream_type": "movie",
        "stream_id": m.vod_id,
        "stream_icon": m.cover or "",
        "rating": str(rating),
        "rating_5based": rating_5,
        "added": "",
        "category_id": m.category_id or "",
        "category_ids": [int(m.category_id)] if (m.category_id or "").isdigit() else [],
        "container_extension": m.container_extension or "mp4",
        "custom_sid": "",
        "direct_source": "",
        "plot": m.plot or "",
        "year": m.release_year or "",
    }


def _local_get_vod_streams() -> list[dict]:
    with Session(engine) as session:
        rows = session.exec(
            select(Movie).where(Movie.in_library == True).order_by(Movie.name)  # noqa: E712
        ).all()
    return [_movie_to_dict(m, idx) for idx, m in enumerate(rows, start=1)]


def _local_get_vod_categories() -> list[dict]:
    with Session(engine) as session:
        in_lib_cat_ids = session.exec(
            select(Movie.category_id)
            .where(Movie.in_library == True)  # noqa: E712
            .where(Movie.category_id.is_not(None))
            .where(Movie.category_id != "")
            .distinct()
        ).all()
        if not in_lib_cat_ids:
            return []
        cats = session.exec(
            select(MovieCategory).where(MovieCategory.category_id.in_(in_lib_cat_ids))
        ).all()
    cats_by_id = {c.category_id: c.category_name for c in cats}
    out = [
        {
            "category_id": str(cid),
            "category_name": cats_by_id.get(cid, str(cid)),
            "parent_id": 0,
        }
        for cid in in_lib_cat_ids
    ]
    out.sort(key=lambda c: c["category_name"].lower())
    return out


def _movie_is_in_library(vod_id: int) -> bool:
    with Session(engine) as session:
        m = session.get(Movie, vod_id)
        return bool(m and m.in_library)


@router.get("/player_api.php")
async def player_api(request: Request):
    incoming = dict(request.query_params)
    action = incoming.get("action", "")

    # Locally-served actions never touch upstream.
    if action == LOCAL_SERIES_ACTION:
        return JSONResponse(_local_get_series())
    if action == LOCAL_SERIES_CATEGORIES_ACTION:
        return JSONResponse(_local_get_series_categories())
    if action == LOCAL_VOD_ACTION:
        return JSONResponse(_local_get_vod_streams())
    if action == LOCAL_VOD_CATEGORIES_ACTION:
        return JSONResponse(_local_get_vod_categories())
    if action == LOCAL_VOD_INFO_ACTION:
        # Phase 2a: gate vod_info on library membership and proxy upstream when
        # in-library, 404 otherwise. Phase 2b will serve from movie_tmdb.
        vod_id_raw = incoming.get("vod_id", "")
        try:
            vod_id = int(vod_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid vod_id")
        if not _movie_is_in_library(vod_id):
            raise HTTPException(404, f"Movie {vod_id} is not in library")
        # Fall through to upstream proxy below.
    if action in EMPTY_ACTIONS:
        return JSONResponse([])

    params = _real_creds_params(incoming)
    headers = _forward_headers(request)

    r: Optional[httpx.Response] = None
    for attempt in range(2):
        upstream = active_base()
        if not upstream:
            raise HTTPException(503, "No active XTREAM upstream configured")
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(
                    f"{upstream}/player_api.php",
                    params=params,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            if attempt == 0 and await _trigger_failover(
                f"network: {exc.__class__.__name__}"
            ):
                continue
            logger.warning("player_api upstream error: %s", exc)
            raise HTTPException(502, f"Upstream error: {exc.__class__.__name__}")

        dead = _looks_dead(r.status_code, r.headers.get("content-type"))
        if dead and attempt == 0 and await _trigger_failover(dead):
            continue
        break

    assert r is not None  # one of the branches above must have set it

    ctype = r.headers.get("content-type", "")
    if "application/json" in ctype or ctype.startswith("text/json"):
        try:
            data = r.json()
        except ValueError:
            return Response(r.content, status_code=r.status_code, media_type=ctype or None)
        _scrub_json(data)
        return JSONResponse(data, status_code=r.status_code)

    return Response(r.content, status_code=r.status_code, media_type=ctype or None)


def _scrub_json(data) -> None:
    """Replace real creds + upstream URL inside player_api JSON responses in-place."""
    if not isinstance(data, dict):
        return
    user_info = data.get("user_info")
    if isinstance(user_info, dict):
        if "username" in user_info:
            user_info["username"] = settings.mirror_username
        if "password" in user_info:
            user_info["password"] = settings.mirror_password
    server_info = data.get("server_info")
    if isinstance(server_info, dict) and settings.mirror_public_url:
        mirror = settings.mirror_public_url.rstrip("/")
        # Drop scheme so clients honor server_info.url/port the way they normally would.
        host_only = re.sub(r"^https?://", "", mirror).split(":", 1)[0]
        port = "8011"
        if ":" in mirror.split("://", 1)[-1]:
            port = mirror.rsplit(":", 1)[-1]
        server_info["url"] = host_only
        server_info["port"] = port
        server_info["server_protocol"] = "https" if mirror.startswith("https://") else "http"


@router.get("/get.php")
async def get_php(request: Request):
    if not active_base():
        raise HTTPException(503, "No active XTREAM upstream configured")

    params = _real_creds_params(dict(request.query_params))
    headers = _forward_headers(request)
    rules = _build_rewrite_regexes()

    async def stream():
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            for attempt in range(2):
                upstream = active_base()
                if not upstream:
                    yield b"# no active XTREAM upstream configured\n"
                    return
                try:
                    async with client.stream(
                        "GET",
                        f"{upstream}/get.php",
                        params=params,
                        headers=headers,
                    ) as r:
                        dead = _looks_dead(r.status_code, r.headers.get("content-type"))
                        if dead and attempt == 0 and await _trigger_failover(dead):
                            continue
                        if r.status_code >= 400:
                            yield await r.aread()
                            return
                        async for line in r.aiter_lines():
                            yield (
                                _rewrite_line(line, rules) + "\n"
                            ).encode("utf-8", errors="replace")
                        return
                except httpx.HTTPError as exc:
                    if attempt == 0 and await _trigger_failover(
                        f"network: {exc.__class__.__name__}"
                    ):
                        continue
                    logger.warning("get.php upstream error: %s", exc)
                    yield f"# upstream error: {exc.__class__.__name__}\n".encode()
                    return

    return StreamingResponse(stream(), media_type="application/vnd.apple.mpegurl")


@router.get("/xmltv.php")
async def xmltv(request: Request):
    if not active_base():
        raise HTTPException(503, "No active XTREAM upstream configured")

    params = _real_creds_params(dict(request.query_params))
    headers = _forward_headers(request)

    async def stream():
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            for attempt in range(2):
                upstream = active_base()
                if not upstream:
                    return
                try:
                    async with client.stream(
                        "GET",
                        f"{upstream}/xmltv.php",
                        params=params,
                        headers=headers,
                    ) as r:
                        dead = _looks_dead(r.status_code, r.headers.get("content-type"))
                        if dead and attempt == 0 and await _trigger_failover(dead):
                            continue
                        async for chunk in r.aiter_bytes():
                            yield chunk
                        return
                except httpx.HTTPError as exc:
                    if attempt == 0 and await _trigger_failover(
                        f"network: {exc.__class__.__name__}"
                    ):
                        continue
                    logger.warning("xmltv upstream error: %s", exc)
                    return

    return StreamingResponse(stream(), media_type="application/xml")


async def _ffmpeg_proxy(upstream_url: str):
    """Async generator that remuxes an upstream stream through FFmpeg.

    Normalises timestamps with -avoid_negative_ts make_zero so Jellyfin's
    ffprobe sees a clean, zero-based MKV regardless of what the IPTV provider
    sends. Subprocess is killed on client disconnect via the finally block.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-probesize", "1M", "-analyzeduration", "1000000",
        "-i", upstream_url,
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-map", "0:v?",
        "-map", "0:a?",
        "-sn",
        "-f", "matroska",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    logger.debug("ffmpeg proxy pid=%d url=%s", proc.pid, upstream_url)
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            yield chunk
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        logger.debug("ffmpeg proxy pid=%d exited (rc=%d)", proc.pid, proc.returncode)


@router.get("/{stream_type}/{username}/{password}/{filename}")
async def stream_redirect(stream_type: str, username: str, password: str, filename: str):
    if stream_type not in STREAM_TYPES:
        raise HTTPException(404, f"Unknown stream type: {stream_type}")
    upstream = active_base()
    if not upstream:
        raise HTTPException(503, "No active XTREAM upstream configured")
    target = (
        f"{upstream}/{stream_type}/"
        f"{settings.xtream_username}/{settings.xtream_password}/{filename}"
    )
    if not settings.proxy_streams:
        return RedirectResponse(target, status_code=302)
    return StreamingResponse(
        _ffmpeg_proxy(target),
        media_type="video/x-matroska",
    )


@router.get("/")
async def mirror_root():
    return {
        "mirror": _mirror_base(),
        "active_upstream": active_base(),
        "endpoints": [
            "/xtream/player_api.php",
            "/xtream/get.php",
            "/xtream/xmltv.php",
            "/xtream/{series|movie|live}/{u}/{p}/{filename}",
        ],
        "mirror_credentials": {
            "username": settings.mirror_username,
            "password": settings.mirror_password,
            "note": "Picker ignores incoming creds and substitutes XTREAM_USERNAME/PASSWORD upstream.",
        },
    }
