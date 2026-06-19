import logging

from fastapi import FastAPI, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)

from database import get_session, init_db
from models import Series, SyncLog
from routers import (
    backfill,
    browse,
    collections,
    movies,
    movies_backfill,
    providers,
    subscriptions,
    sync,
    trakt_router,
    xtream_proxy,
)
from services.scheduler import start_scheduler

app = FastAPI(title="Streamarr")

templates = Jinja2Templates(directory="templates")

app.include_router(browse.router)
app.include_router(subscriptions.router)
app.include_router(movies.router)
app.include_router(movies_backfill.router)
app.include_router(collections.router)
app.include_router(sync.router)
app.include_router(trakt_router.router)
app.include_router(backfill.router)
app.include_router(providers.router)
app.include_router(xtream_proxy.router)


@app.on_event("startup")
def on_startup():
    init_db()
    start_scheduler()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session: Session = Depends(get_session)):
    total = len(session.exec(select(Series)).all())
    subscribed_count = len(
        session.exec(select(Series).where(Series.subscribed == True)).all()
    )
    recent_synced = session.exec(
        select(Series)
        .where(Series.subscribed == True, Series.last_synced != None)
        .order_by(Series.last_synced.desc())
        .limit(6)
    ).all()
    last_log = session.exec(
        select(SyncLog).order_by(SyncLog.started_at.desc()).limit(1)
    ).first()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "total_series": total,
            "subscribed_count": subscribed_count,
            "recent_synced": recent_synced,
            "last_log": last_log,
        },
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    trakt: str = "",
    session: Session = Depends(get_session),
):
    from models import TraktToken
    from config import settings as cfg

    token = session.get(TraktToken, 1)
    trakt_connected = token is not None

    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "trakt_connected": trakt_connected,
            "trakt_flash": trakt,
            "xtream_url": cfg.xtream_url,
            "xtream_username": cfg.xtream_username,
            "library_path": cfg.library_path,
        },
    )
