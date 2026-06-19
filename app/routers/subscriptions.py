from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from database import get_session
from models import Series
from services.strm import remove_series_folder

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.post("/subscribe/{series_id}", response_class=HTMLResponse)
async def subscribe(
    series_id: int, request: Request, session: Session = Depends(get_session)
):
    series = session.get(Series, series_id)
    if series:
        series.subscribed = True
        session.add(series)
        session.commit()
        session.refresh(series)
    return templates.TemplateResponse(
        "partials/series_card.html", {"request": request, "s": series}
    )


@router.post("/unsubscribe/{series_id}", response_class=HTMLResponse)
async def unsubscribe(
    series_id: int, request: Request, session: Session = Depends(get_session)
):
    series = session.get(Series, series_id)
    if series:
        series.subscribed = False
        series.last_synced = None
        session.add(series)
        session.commit()
        session.refresh(series)
        remove_series_folder(series)
    return templates.TemplateResponse(
        "partials/series_card.html", {"request": request, "s": series}
    )


@router.get("/my-shows", response_class=HTMLResponse)
async def my_shows(request: Request, session: Session = Depends(get_session)):
    subscribed = session.exec(
        select(Series).where(Series.subscribed == True).order_by(Series.name)
    ).all()
    return templates.TemplateResponse(
        "my_shows.html", {"request": request, "series_list": subscribed}
    )
