from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from clients import trakt as trakt_client
from clients.trakt import token_expires_at
from database import get_session
from models import Series, TraktToken

router = APIRouter(prefix="/trakt")
templates = Jinja2Templates(directory="templates")


def _get_token(session: Session) -> TraktToken | None:
    return session.get(TraktToken, 1)


async def _ensure_fresh_token(token: TraktToken, session: Session) -> TraktToken:
    if datetime.utcnow() >= token.expires_at:
        data = await trakt_client.refresh_token(token.refresh_token)
        token.access_token = data["access_token"]
        token.refresh_token = data["refresh_token"]
        token.expires_at = token_expires_at(data["expires_in"])
        session.add(token)
        session.commit()
        session.refresh(token)
    return token


@router.get("/connect")
async def connect():
    return RedirectResponse(trakt_client.auth_url())


@router.get("/callback", response_class=HTMLResponse)
async def callback(
    request: Request,
    code: str = "",
    error: str = "",
    session: Session = Depends(get_session),
):
    if error or not code:
        return templates.TemplateResponse(
            "partials/toast.html",
            {"request": request, "message": f"Trakt auth failed: {error}", "status": "error"},
        )

    data = await trakt_client.exchange_code(code)
    token = session.get(TraktToken, 1)
    if token:
        token.access_token = data["access_token"]
        token.refresh_token = data["refresh_token"]
        token.expires_at = token_expires_at(data["expires_in"])
    else:
        token = TraktToken(
            id=1,
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            expires_at=token_expires_at(data["expires_in"]),
        )
    session.add(token)
    session.commit()
    return RedirectResponse("/settings?trakt=connected")


@router.post("/disconnect", response_class=HTMLResponse)
async def disconnect(request: Request, session: Session = Depends(get_session)):
    token = session.get(TraktToken, 1)
    if token:
        session.delete(token)
        session.commit()
    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": "Trakt disconnected.", "status": "info"},
    )


def _match_series(session: Session, title: str, year: int | None) -> Series | None:
    """Find a catalog series for a Trakt show by title (substring) and optional year.

    Catalog names embed a provider-prefix and language tag, e.g.
    ``EN - S.W.A.T. (2017) (US)`` or ``SE - S.W.A.T.``. We use substring match,
    prefer year-matching entries, then prefer English-language prefixes
    (Trakt titles are in English so an EN-tagged catalog entry is the most
    likely intended match), then fall back to the shortest name.
    """
    if not title:
        return None
    candidates = session.exec(
        select(Series).where(Series.name.ilike(f"%{title}%"))
    ).all()
    if not candidates:
        return None
    if year:
        year_str = str(year)
        year_matches = [
            c for c in candidates if c.release_date and c.release_date.startswith(year_str)
        ]
        if year_matches:
            candidates = year_matches

    def score(s: Series) -> tuple[int, int]:
        upper = s.name.upper()
        is_en = upper.startswith("EN ") or upper.startswith("EN-") or upper.startswith("EN - ")
        return (0 if is_en else 1, len(s.name))

    return min(candidates, key=score)


async def _import_shows(
    request: Request,
    session: Session,
    items: list[dict],
    source_label: str,
) -> HTMLResponse:
    matched = 0
    already = 0
    not_found: list[str] = []
    for item in items:
        show = item.get("show", {})
        title = show.get("title", "")
        year = show.get("year")
        series = _match_series(session, title, year)
        if not series:
            not_found.append(title)
            continue
        if series.subscribed:
            already += 1
            continue
        series.subscribed = True
        series.trakt_id = show.get("ids", {}).get("trakt")
        session.add(series)
        matched += 1
    session.commit()

    msg = (
        f"Imported {matched} show(s) from Trakt {source_label}"
        f" ({already} already subscribed, {len(not_found)} not in catalog)."
    )
    if not_found:
        sample = ", ".join(not_found[:5])
        msg += f" Missing examples: {sample}"
        if len(not_found) > 5:
            msg += f" (+{len(not_found) - 5} more)"

    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": msg, "status": "success"},
    )


@router.post("/import-watchlist", response_class=HTMLResponse)
async def import_watchlist(request: Request, session: Session = Depends(get_session)):
    token = _get_token(session)
    if not token:
        return templates.TemplateResponse(
            "partials/toast.html",
            {"request": request, "message": "Connect Trakt first.", "status": "error"},
        )
    token = await _ensure_fresh_token(token, session)
    watchlist = await trakt_client.get_watchlist(token.access_token)
    return await _import_shows(request, session, watchlist, "watchlist")


@router.post("/import-watched", response_class=HTMLResponse)
async def import_watched(request: Request, session: Session = Depends(get_session)):
    token = _get_token(session)
    if not token:
        return templates.TemplateResponse(
            "partials/toast.html",
            {"request": request, "message": "Connect Trakt first.", "status": "error"},
        )
    token = await _ensure_fresh_token(token, session)
    watched = await trakt_client.get_watched_shows(token.access_token)
    return await _import_shows(request, session, watched, "watched history")
