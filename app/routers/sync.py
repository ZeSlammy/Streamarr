import asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from database import get_session
from models import SyncLog
from services.strm import run_full_sync

router = APIRouter()
templates = Jinja2Templates(directory="templates")

_sync_task: asyncio.Task | None = None


@router.post("/sync", response_class=HTMLResponse)
async def trigger_sync(request: Request, session: Session = Depends(get_session)):
    global _sync_task

    if _sync_task and not _sync_task.done():
        return templates.TemplateResponse(
            "partials/toast.html",
            {"request": request, "message": "Sync already running.", "status": "info"},
        )

    _sync_task = asyncio.create_task(run_full_sync())
    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": "Sync started in background.", "status": "success"},
    )


@router.get("/sync/status", response_class=HTMLResponse)
async def sync_status(request: Request, session: Session = Depends(get_session)):
    logs = session.exec(
        select(SyncLog).order_by(SyncLog.started_at.desc()).limit(5)
    ).all()
    running = _sync_task is not None and not _sync_task.done()
    return templates.TemplateResponse(
        "partials/sync_status.html",
        {"request": request, "logs": logs, "running": running},
    )
