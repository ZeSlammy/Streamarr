"""
Background backfill: walks every series that has detail_fetched=False and pulls its
detail info from XTREAM so Browse filters (audio/subtitle language) have data to work with.

State is in-memory only — survives within the running process, but resets on container restart.
"""
import asyncio
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from clients import xtream
from database import engine
from models import Series

_CONCURRENCY = 5

state: dict[str, Any] = {
    "running": False,
    "total": 0,
    "done": 0,
    "errors": 0,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
}

_task: asyncio.Task | None = None


def start() -> None:
    """Synchronously mark backfill as running and schedule the worker task."""
    global _task
    if state["running"]:
        return
    state.update(
        running=True,
        total=0,
        done=0,
        errors=0,
        started_at=datetime.now(timezone.utc),
        finished_at=None,
        last_error=None,
    )
    _task = asyncio.create_task(_run())


async def _process_one(series_id: int) -> None:
    info = await xtream.get_series_info(series_id)
    with Session(engine) as s:
        series = s.get(Series, series_id)
        if not series:
            return
        series.audio_languages = ", ".join(info.audio_languages) or None
        series.subtitle_languages = ", ".join(info.subtitle_languages) or None
        series.video_info = info.video_info
        series.episode_count = info.episode_count
        series.season_count = info.season_count
        if not series.plot:
            series.plot = info.plot
        if not series.genre:
            series.genre = info.genre
        series.detail_fetched = True
        s.add(series)
        s.commit()


async def _run() -> None:
    try:
        with Session(engine) as s:
            ids = list(
                s.exec(
                    select(Series.series_id).where(Series.detail_fetched == False)
                ).all()
            )
        state["total"] = len(ids)

        queue: asyncio.Queue[int] = asyncio.Queue()
        for sid in ids:
            queue.put_nowait(sid)

        async def worker() -> None:
            while True:
                try:
                    sid = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await _process_one(sid)
                except Exception as exc:
                    state["errors"] += 1
                    state["last_error"] = f"#{sid}: {exc!r}"
                state["done"] += 1

        await asyncio.gather(*(worker() for _ in range(_CONCURRENCY)))
    finally:
        state["running"] = False
        state["finished_at"] = datetime.now(timezone.utc)
