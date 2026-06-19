"""
Nightly job: failover-check → catalog refresh → full .strm sync.

Runs in-process via APScheduler. Fires at SYNC_CRON_HOUR:SYNC_CRON_MINUTE
in SYNC_CRON_TIMEZONE (default 03:00 Europe/Paris). Disable by setting
SYNC_CRON_ENABLED=false.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import Session, select

from clients import xtream
from config import settings
from database import engine
from models import Category, Series
from services.epgenius import verify_m3u as verify_epgenius
from services.jellyfin import kill_stuck_sessions
from services.notify import send_discord
from services.providers import current_url, run_failover_check
from services.movies_strm import (
    audit_movie_strm_urls,
    refresh_vod_catalog,
    sync_all_in_library_movies,
)
from services.movies_tmdb import (
    count_tombstones,
    enrich_batch as enrich_movies_batch,
)
from services.strm import audit_strm_urls, run_full_sync

# Capped to keep the nightly job from blowing through TMDB rate limits.
# ~200 films × 0.1s sleep = ~20s of TMDB time; multi-night fills are fine.
NIGHTLY_TMDB_LIMIT = 200

logger = logging.getLogger("streamarr.scheduler")

_scheduler: AsyncIOScheduler | None = None


async def _refresh_catalog() -> tuple[int, int]:
    """Pull series + categories from XTREAM into SQLite. Returns (series, categories)."""
    categories = await xtream.get_series_categories()
    all_series = await xtream.get_series()

    with Session(engine) as session:
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
        total = len(session.exec(select(Series)).all())
    return total, len(categories)


async def nightly_job() -> dict:
    """Failover check → catalog refresh → .strm audit → full sync. Notify on changes."""
    started = datetime.utcnow().isoformat(timespec="seconds")
    logger.info("nightly job starting (%s)", started)

    failover = await run_failover_check()
    logger.info(
        "failover: current=%s swapped=%s reason=%s",
        failover.get("current"),
        failover.get("swapped"),
        failover.get("reason"),
    )

    if not failover.get("current"):
        logger.error("nightly job aborting — no working XTREAM URL")
        await send_discord(
            ":rotating_light: **Streamarr** — no working URL among candidates. Manual intervention needed.",
            title="Streamarr: all URLs dead",
        )
        return {"failover": failover, "aborted": "no working URL"}

    epgenius_verify: dict = {}
    active = current_url()
    if active and settings.epgenius_enabled and settings.epgenius_m3u_url:
        epgenius_verify = await verify_epgenius(active)
        logger.info("EPGenius verify: %s", epgenius_verify.get("detail"))

    try:
        series_count, cat_count = await _refresh_catalog()
        logger.info("catalog refresh: %d series, %d categories", series_count, cat_count)
    except Exception as exc:
        logger.exception("catalog refresh failed")
        await send_discord(
            f":warning: **Streamarr** — catalog refresh failed: `{exc}`",
            title="Streamarr: refresh error",
        )
        return {"failover": failover, "catalog_error": str(exc)}

    audit = audit_strm_urls()
    logger.info(
        ".strm audit: sampled=%d fresh=%d stale=%d missing=%d base=%s",
        audit["sampled"], audit["fresh"], audit["stale"], audit["missing"], audit["base"],
    )

    try:
        sync_log = await run_full_sync()
        logger.info(
            "sync: %d series, +%d files, -%d files, status=%s",
            sync_log.series_processed,
            sync_log.files_created,
            sync_log.files_removed,
            sync_log.status,
        )
    except Exception as exc:
        logger.exception("sync failed")
        await send_discord(
            f":warning: **Streamarr** — sync failed: `{exc}`",
            title="Streamarr: sync error",
        )
        return {"failover": failover, "sync_error": str(exc)}

    # Movies side — same pattern, appended after series. Errors here do not
    # abort the job: a bad VOD refresh shouldn't undo a good series sync.
    movie_stats: dict = {}
    movie_audit: dict = {}
    movie_errors: list[str] = []
    try:
        catalog_stats = await refresh_vod_catalog()
        logger.info(
            "VOD catalog refresh: total=%d new=%d updated=%d",
            catalog_stats.catalog_total,
            catalog_stats.catalog_new,
            catalog_stats.catalog_updated,
        )
        movie_audit = audit_movie_strm_urls()
        logger.info(
            "movie .strm audit: sampled=%d fresh=%d stale=%d missing=%d",
            movie_audit["sampled"],
            movie_audit["fresh"],
            movie_audit["stale"],
            movie_audit["missing"],
        )
        m_created, m_removed, m_errors = sync_all_in_library_movies()
        movie_errors = m_errors
        movie_stats = {
            "catalog_total": catalog_stats.catalog_total,
            "catalog_new": catalog_stats.catalog_new,
            "catalog_updated": catalog_stats.catalog_updated,
            "files_created": m_created,
            "files_removed": m_removed,
            "errors": len(m_errors),
        }
        logger.info(
            "movie sync: +%d files, -%d files, %d errors",
            m_created,
            m_removed,
            len(m_errors),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("movie sync failed")
        movie_stats = {"error": str(exc)}

    # TMDB enrichment — capped per-night so we don't hammer TMDB. Multi-night
    # backfills are expected for first-time setups. Skipped silently when
    # TMDB_API_KEY is empty (enrich_batch is a no-op then).
    try:
        tombstones_before = count_tombstones()
        tmdb_status = await enrich_movies_batch(limit=NIGHTLY_TMDB_LIMIT)
        tombstones_after = count_tombstones()
        tombstones_added = max(0, tombstones_after - tombstones_before)
        if tmdb_status.total_candidates:
            logger.info(
                "TMDB enrichment: %d/%d processed, %d failed, +%d tombstones",
                tmdb_status.processed,
                tmdb_status.total_candidates,
                tmdb_status.failed,
                tombstones_added,
            )
            movie_stats["tmdb_processed"] = tmdb_status.processed
            movie_stats["tmdb_failed"] = tmdb_status.failed
            movie_stats["tombstones_added"] = tombstones_added
            movie_stats["tombstones_total"] = tombstones_after
            if tombstones_added >= settings.tombstone_spike_threshold:
                movie_stats["tombstone_spike"] = True
                logger.warning(
                    "tombstone spike: +%d in one run (threshold=%d)",
                    tombstones_added,
                    settings.tombstone_spike_threshold,
                )
    except Exception as exc:  # noqa: BLE001
        logger.exception("TMDB enrichment failed")
        movie_stats["tmdb_error"] = str(exc)

    await send_discord(
        _format_notification(failover, audit, sync_log, movie_stats, movie_audit, epgenius_verify)
    )

    return {
        "failover": failover,
        "epgenius_verify": epgenius_verify,
        "series": series_count,
        "categories": cat_count,
        "audit": audit,
        "sync_status": sync_log.status,
        "files_created": sync_log.files_created,
        "files_removed": sync_log.files_removed,
        "movies": movie_stats,
        "movie_audit": movie_audit,
    }


def _format_notification(
    failover: dict,
    audit: dict,
    sync_log,
    movies: dict | None = None,
    movie_audit: dict | None = None,
    epgenius_verify: dict | None = None,
) -> str:
    lines = []
    if failover.get("swapped"):
        prev = failover.get("previous") or "—"
        new = failover.get("current") or "—"
        reason = failover.get("reason") or "unknown"
        lines.append(f":arrows_counterclockwise: **URL changed**: `{prev}` → `{new}`")
        lines.append(f"Reason: {reason}")
    else:
        lines.append(f":white_check_mark: URL alive: `{failover.get('current')}`")

    lines.append(
        f":mag: .strm audit: {audit['fresh']}/{audit['sampled']} fresh, "
        f"{audit['stale']} stale, {audit['missing']} missing"
    )
    if audit["examples"]:
        for ex in audit["examples"]:
            lines.append(f"  • {ex}")

    badge = ":white_check_mark:" if sync_log.status == "done" else ":x:"
    lines.append(
        f"{badge} Sync: {sync_log.series_processed} shows, "
        f"+{sync_log.files_created} / −{sync_log.files_removed} files "
        f"(status: `{sync_log.status}`)"
    )
    if sync_log.message:
        lines.append(f"Errors: {sync_log.message[:300]}")

    if movies:
        if "error" in movies:
            lines.append(f":x: Movies: catalog/sync error — {movies['error'][:200]}")
        else:
            lines.append(
                f":clapper: Movies: catalog {movies.get('catalog_total', 0)} "
                f"({movies.get('catalog_new', 0)} new, {movies.get('catalog_updated', 0)} upd) · "
                f"+{movies.get('files_created', 0)} / −{movies.get('files_removed', 0)} files"
                + (f" · {movies['errors']} errors" if movies.get('errors') else "")
            )
        added = movies.get("tombstones_added")
        if added:
            badge = ":rotating_light:" if movies.get("tombstone_spike") else ":headstone:"
            lines.append(
                f"{badge} TMDB tombstones: +{added} this run "
                f"(total {movies.get('tombstones_total', 0)}, "
                f"threshold {settings.tombstone_spike_threshold})"
            )
    if movie_audit and movie_audit.get("sampled"):
        lines.append(
            f":mag: Movie .strm audit: {movie_audit['fresh']}/{movie_audit['sampled']} fresh, "
            f"{movie_audit['stale']} stale, {movie_audit['missing']} missing"
        )

    if epgenius_verify:
        if epgenius_verify.get("ok"):
            lines.append(f":satellite: EPGenius M3U: `{epgenius_verify['m3u_base']}` matches active URL.")
        elif epgenius_verify.get("m3u_base"):
            lines.append(
                f":warning: EPGenius M3U mismatch: "
                f"`{epgenius_verify['m3u_base']}` ≠ `{epgenius_verify['active_base']}`"
            )
        else:
            lines.append(f":grey_question: EPGenius M3U check: {epgenius_verify.get('detail', 'unknown')}")

    if settings.mirror_public_url:
        lines.append(f":link: Mirror URL: `{settings.mirror_public_url}/xtream` (unchanged)")

    return "\n".join(lines)


def start_scheduler() -> AsyncIOScheduler | None:
    global _scheduler
    if not settings.sync_cron_enabled:
        logger.info("scheduler disabled via SYNC_CRON_ENABLED=false")
        return None
    if _scheduler is not None:
        return _scheduler

    try:
        tz = ZoneInfo(settings.sync_cron_timezone)
    except Exception:
        logger.warning("invalid timezone %r, falling back to UTC", settings.sync_cron_timezone)
        tz = ZoneInfo("UTC")

    sched = AsyncIOScheduler(timezone=tz)
    sched.add_job(
        nightly_job,
        trigger=CronTrigger(
            hour=settings.sync_cron_hour,
            minute=settings.sync_cron_minute,
            timezone=tz,
        ),
        id="nightly_sync",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    if settings.jellyfin_watchdog_enabled and settings.jellyfin_api_key:
        sched.add_job(
            kill_stuck_sessions,
            trigger=IntervalTrigger(minutes=settings.jellyfin_watchdog_interval_minutes),
            id="jellyfin_watchdog",
            replace_existing=True,
            misfire_grace_time=60,
            coalesce=True,
        )
        logger.info(
            "Jellyfin watchdog enabled — checking every %d min (threshold %ds)",
            settings.jellyfin_watchdog_interval_minutes,
            settings.jellyfin_stuck_threshold_seconds,
        )
    else:
        logger.info("Jellyfin watchdog disabled (set JELLYFIN_API_KEY to enable)")

    sched.start()
    _scheduler = sched
    logger.info(
        "scheduler started — next run %s",
        sched.get_job("nightly_sync").next_run_time,
    )
    return sched


def get_next_run() -> datetime | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("nightly_sync")
    return job.next_run_time if job else None
