from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from clients import xtream
from database import get_session
from models import Category, Series

router = APIRouter()
templates = Jinja2Templates(directory="templates")

PAGE_SIZE = 60


@router.get("/browse", response_class=HTMLResponse)
async def browse_page(request: Request, session: Session = Depends(get_session)):
    categories = session.exec(select(Category).order_by(Category.category_name)).all()
    return templates.TemplateResponse(
        "browse.html", {"request": request, "categories": categories}
    )


@router.get("/browse/results", response_class=HTMLResponse)
async def browse_results(
    request: Request,
    q: str = "",
    category_id: str = "",
    audio_lang: str = "",
    sub_lang: str = "",
    offset: int = 0,
    session: Session = Depends(get_session),
):
    query = select(Series).order_by(Series.name)
    if q:
        query = query.where(Series.name.ilike(f"%{q}%"))
    if category_id:
        query = query.where(Series.category_id == category_id)
    if audio_lang:
        query = query.where(Series.audio_languages.ilike(f"%{audio_lang}%"))
    if sub_lang:
        query = query.where(Series.subtitle_languages.ilike(f"%{sub_lang}%"))

    all_series = session.exec(query).all()
    total = len(all_series)
    page = all_series[offset : offset + PAGE_SIZE]
    next_offset = offset + PAGE_SIZE
    has_more = next_offset < total

    template = "partials/series_page.html" if offset > 0 else "partials/series_grid.html"
    return templates.TemplateResponse(
        template,
        {
            "request": request,
            "series_list": page,
            "total": total,
            "has_more": has_more,
            "next_offset": next_offset,
            "q": q,
            "category_id": category_id,
            "audio_lang": audio_lang,
            "sub_lang": sub_lang,
        },
    )


async def _ensure_detail_fetched(series: Series, session: Session) -> None:
    if series.detail_fetched:
        return
    try:
        info = await xtream.get_series_info(series.series_id)
        series.audio_languages = ", ".join(info.audio_languages) or None
        series.subtitle_languages = ", ".join(info.subtitle_languages) or None
        series.video_info = info.video_info
        series.episode_count = info.episode_count
        series.season_count = info.season_count
        series.plot = series.plot or info.plot
        series.genre = series.genre or info.genre
        series.detail_fetched = True
        session.add(series)
        session.commit()
        session.refresh(series)
    except Exception:
        pass  # leave detail_fetched=False so it retries next time


@router.get("/series/{series_id}/detail", response_class=HTMLResponse)
async def series_detail(
    series_id: int, request: Request, session: Session = Depends(get_session)
):
    series = session.get(Series, series_id)
    if not series:
        return HTMLResponse("<p class='text-danger'>Show not found.</p>")

    await _ensure_detail_fetched(series, session)

    return templates.TemplateResponse(
        "partials/series_detail_modal.html",
        {"request": request, "s": series},
    )


@router.post("/series/{series_id}/fetch-info", response_class=HTMLResponse)
async def series_fetch_info(
    series_id: int, request: Request, session: Session = Depends(get_session)
):
    series = session.get(Series, series_id)
    if not series:
        return HTMLResponse("<p class='text-danger'>Show not found.</p>")

    await _ensure_detail_fetched(series, session)

    return templates.TemplateResponse(
        "partials/series_card.html",
        {"request": request, "s": series},
    )


@router.post("/catalog/refresh", response_class=HTMLResponse)
async def refresh_catalog(request: Request, session: Session = Depends(get_session)):
    try:
        categories = await xtream.get_series_categories()
        for cat in categories:
            existing = session.get(Category, str(cat["category_id"]))
            if existing:
                existing.category_name = cat["category_name"]
            else:
                session.add(
                    Category(
                        category_id=str(cat["category_id"]),
                        category_name=cat["category_name"],
                    )
                )

        all_series = await xtream.get_series()
        for s in all_series:
            sid = int(s["series_id"])
            existing = session.get(Series, sid)
            if existing:
                existing.name = s.get("name", existing.name)
                existing.cover = s.get("cover", existing.cover)
                existing.category_id = str(s.get("category_id", existing.category_id))
                existing.rating = str(s.get("rating", existing.rating))
                existing.rating_5based = float(s.get("rating_5based") or 0)
            else:
                session.add(
                    Series(
                        series_id=sid,
                        name=s.get("name", ""),
                        cover=s.get("cover"),
                        genre=s.get("genre"),
                        release_date=s.get("releaseDate") or s.get("release_date"),
                        rating=str(s.get("rating", "")),
                        rating_5based=float(s.get("rating_5based") or 0),
                        category_id=str(s.get("category_id", "")),
                    )
                )

        session.commit()
        total = session.exec(select(Series)).all()
        msg = f"Catalog refreshed: {len(total)} series, {len(categories)} categories."
        status = "success"
    except Exception as exc:
        session.rollback()
        msg = f"Catalog refresh failed: {exc}"
        status = "error"

    return templates.TemplateResponse(
        "partials/toast.html",
        {"request": request, "message": msg, "status": status},
    )
