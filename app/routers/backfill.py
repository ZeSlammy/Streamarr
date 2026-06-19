from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from services import backfill as backfill_service

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _render(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "partials/backfill_status.html",
        {"request": request, "state": backfill_service.state},
    )


@router.post("/catalog/backfill", response_class=HTMLResponse)
async def start_backfill(request: Request):
    backfill_service.start()
    return _render(request)


@router.get("/catalog/backfill/status", response_class=HTMLResponse)
async def backfill_status(request: Request):
    return _render(request)
