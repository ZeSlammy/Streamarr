import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from config import settings
from services.epgenius import push_credentials as push_epgenius, verify_m3u as verify_epgenius
from services.providers import current_url, read_state, run_failover_check
from services.scheduler import get_next_run

router = APIRouter(prefix="/providers")
templates = Jinja2Templates(directory="templates")
logger = logging.getLogger("streamarr.providers")

_check_task: asyncio.Task | None = None
_last_summary: dict | None = None


@router.get("/status", response_class=HTMLResponse)
async def status(request: Request):
    state = read_state()
    running = _check_task is not None and not _check_task.done()
    next_run = get_next_run()
    return templates.TemplateResponse(
        "partials/providers_status.html",
        {
            "request": request,
            "state": state,
            "running": running,
            "summary": _last_summary,
            "next_run": next_run,
            "mirror_url": settings.mirror_public_url.rstrip("/") if settings.mirror_public_url else "",
            "mirror_user": settings.mirror_username,
            "mirror_pass": settings.mirror_password,
        },
    )


@router.post("/check", response_class=HTMLResponse)
async def check_now(request: Request):
    global _check_task

    if _check_task and not _check_task.done():
        return templates.TemplateResponse(
            "partials/toast.html",
            {"request": request, "message": "Provider check already running.", "status": "info"},
        )

    async def _run():
        global _last_summary
        try:
            _last_summary = await run_failover_check()
        except Exception as exc:
            logger.exception("provider check failed")
            _last_summary = {
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "error": str(exc),
            }

    _check_task = asyncio.create_task(_run())
    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": "Probing provider URLs…", "status": "success"},
    )


@router.post("/epgenius-sync", response_class=HTMLResponse)
async def epgenius_sync(request: Request):
    """Manually push the current active URL + creds to EPGenius. Useful for
    first-time setup and testing without waiting for a real failover."""
    url = current_url()
    if not url:
        return templates.TemplateResponse(
            "partials/toast.html",
            {"request": request, "message": "No active provider URL configured.", "status": "error"},
        )
    ok, detail = await push_epgenius(url)
    msg = f"EPGenius updated ({url})." if ok else f"EPGenius push failed: {detail}"
    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": msg, "status": "success" if ok else "error"},
    )


@router.get("/epgenius-verify", response_class=HTMLResponse)
async def epgenius_verify(request: Request):
    """Fetch the EPGenius M3U from Google Drive and verify its base URL matches
    the current active provider URL."""
    url = current_url()
    if not url:
        return templates.TemplateResponse(
            "partials/toast.html",
            {"request": request, "message": "No active provider URL configured.", "status": "error"},
        )
    result = await verify_epgenius(url)
    if result["ok"]:
        msg = f"M3U confirmed: {result['m3u_base']} matches active URL."
        status = "success"
    else:
        msg = f"Mismatch: {result['detail']}"
        status = "warning" if result["m3u_base"] else "error"
    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": msg, "status": status},
    )
