"""
Generates and removes .strm files on the shared library volume.

Folder layout (Jellyfin / Kodi compatible):
  {library_root}/
    {Show Name} ({Year})/
      Season 01/
        {Show Name} S01E01.strm
"""

import asyncio
import random
import re
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from clients.xtream import _active_base, get_series_info, stream_url
from config import settings
from database import engine
from models import Series, SyncLog


def _safe_name(name: str) -> str:
    """Strip characters that are illegal in file/folder names."""
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


# Provider names look like "EN - Will Trent (2023) (US) (2023)" — the language
# prefix, country tag, and doubled year all confuse Jellyfin's TVDb/TMDb matcher.
_LANG_PREFIX_RE = re.compile(r"^[A-Z]{2,3}\s*-\s*")
_COUNTRY_RE = re.compile(
    r"\s*\((?:US|UK|GB|FR|CA|AU|DE|ES|IT|JP|KR|MX|BR|IE|NZ|RU|SE|DK|NO|FI|NL|BE|PL|TR|IN|CN|HK|AR|CL|CO|CH|AT|PT|ZA)\)"
)
_YEAR_RE = re.compile(r"\s*\((\d{4})\)")


def _clean_show_name(name: str, release_date: str | None) -> tuple[str, str]:
    """Normalize an XTREAM-provided show name for Jellyfin/Kodi matchers.

    Returns (title, year). Strips leading language prefix, embedded country
    codes, and duplicated year tags. Year comes from the first `(YYYY)` found
    in the provider name, falling back to `release_date`.
    """
    s = _LANG_PREFIX_RE.sub("", name).strip()
    s = _COUNTRY_RE.sub("", s).strip()
    years = _YEAR_RE.findall(s)
    s = _YEAR_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    year = years[0] if years else ""
    if not year and release_date:
        y = release_date[:4]
        if y.isdigit():
            year = y
    return s, year


def _show_folder(series: Series) -> Path:
    title, year = _clean_show_name(series.name, series.release_date)
    base = f"{title} ({year})" if year else title
    # `[tmdbid-N]` is Jellyfin's match-override convention — bypasses fuzzy name lookup.
    if getattr(series, "tmdb_id", None):
        base = f"{base} [tmdbid-{series.tmdb_id}]"
    return Path(settings.library_path) / _safe_name(base)


def _season_folder(show_folder: Path, season: int) -> Path:
    return show_folder / f"Season {season:02d}"


def _strm_path(season_folder: Path, show_name: str, season: int, episode: int, release_date: str | None = None) -> Path:
    title, _year = _clean_show_name(show_name, release_date)
    filename = _safe_name(f"{title} S{season:02d}E{episode:02d}.strm")
    return season_folder / filename


async def sync_series(series: Series, log: SyncLog) -> tuple[int, int]:
    """Fetch episodes for one series, write .strm files. Returns (created, removed)."""
    info = await get_series_info(series.series_id)

    # Adopt the provider's tmdb_id so this run's folder name includes [tmdbid-N].
    # Persist immediately so audits and unsubscribe-cleanup see the same path.
    if info.tmdb_id and series.tmdb_id != info.tmdb_id:
        series.tmdb_id = info.tmdb_id
        with Session(engine) as session:
            s = session.get(Series, series.series_id)
            if s:
                s.tmdb_id = info.tmdb_id
                session.add(s)
                session.commit()

    show_folder = _show_folder(series)
    show_folder.mkdir(parents=True, exist_ok=True)

    expected_paths: set[Path] = set()
    created = 0

    for season, episodes in info.episodes.items():
        season_folder = _season_folder(show_folder, season)
        season_folder.mkdir(exist_ok=True)

        # Some providers list the same episode number twice (two qualities / dub variants).
        # Keeping only the last entry per number prevents the file from being rewritten
        # on every sync run as the loop alternates between the two IDs.
        deduped: dict[int, object] = {}
        for ep in episodes:
            deduped[ep.episode_num] = ep
        episodes = list(deduped.values())

        for ep in episodes:
            strm = _strm_path(season_folder, series.name, season, ep.episode_num, series.release_date)
            expected_paths.add(strm)

            url = stream_url(ep.id, ep.container_extension)
            # Rewrite when missing OR when the stored URL is stale (provider failover).
            if not strm.exists() or strm.read_text(encoding="utf-8").strip() != url:
                strm.write_text(url, encoding="utf-8")
                created += 1

    removed = _remove_stale(show_folder, expected_paths)
    return created, removed


def _remove_stale(show_folder: Path, expected: set[Path]) -> int:
    removed = 0
    for existing in show_folder.rglob("*.strm"):
        if existing not in expected:
            existing.unlink()
            removed += 1
    for season_dir in show_folder.iterdir():
        if season_dir.is_dir() and not any(season_dir.iterdir()):
            season_dir.rmdir()
    return removed


def remove_series_folder(series: Series) -> int:
    """Delete all .strm files for an unsubscribed series."""
    show_folder = _show_folder(series)
    removed = 0
    if show_folder.exists():
        for f in show_folder.rglob("*.strm"):
            f.unlink()
            removed += 1
        for d in sorted(show_folder.rglob("*"), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
        try:
            show_folder.rmdir()
        except OSError:
            pass
    return removed


def _expected_strm_prefix() -> str:
    """Prefix every freshly-written .strm file is expected to start with."""
    mirror = (settings.mirror_public_url or "").rstrip("/")
    if mirror:
        return f"{mirror}/xtream/series/{settings.mirror_username}/{settings.mirror_password}/"
    return f"{active_base()}/series/{settings.xtream_username}/{settings.xtream_password}/"


def audit_strm_urls(sample_size: int | None = None) -> dict:
    """Spot-check .strm files against the URL prefix the sync would write today.

    Picks `sample_size` subscribed shows at random (default from settings), reads
    the first .strm file for each, and counts how many start with the expected
    prefix (mirror URL if enabled, otherwise direct upstream URL).
    """
    if sample_size is None:
        sample_size = settings.strm_audit_sample
    expected_prefix = _expected_strm_prefix()
    base = expected_prefix  # for backward-compat in returned dict

    with Session(engine) as session:
        subscribed = session.exec(select(Series).where(Series.subscribed == True)).all()

    if not subscribed:
        return {"sampled": 0, "stale": 0, "fresh": 0, "missing": 0, "examples": [], "base": base}

    sample = random.sample(subscribed, min(sample_size, len(subscribed)))
    stale = 0
    fresh = 0
    missing = 0
    examples: list[str] = []

    for series in sample:
        show_folder = _show_folder(series)
        first_strm = next(show_folder.rglob("*.strm"), None) if show_folder.exists() else None
        if first_strm is None:
            missing += 1
            continue
        try:
            content = first_strm.read_text(encoding="utf-8").strip()
        except OSError:
            missing += 1
            continue
        if content.startswith(expected_prefix):
            fresh += 1
        else:
            stale += 1
            if len(examples) < 3:
                # Show only the host portion to keep the message short.
                head = content.split("/series/")[0] if "/series/" in content else content[:60]
                examples.append(f"{series.name}: {head}")

    return {
        "sampled": len(sample),
        "stale": stale,
        "fresh": fresh,
        "missing": missing,
        "examples": examples,
        "base": base,
    }


async def run_full_sync() -> SyncLog:
    with Session(engine) as session:
        log = SyncLog()
        session.add(log)
        session.commit()
        session.refresh(log)
        log_id = log.id

    total_created = 0
    total_removed = 0
    errors = []

    with Session(engine) as session:
        subscribed = session.exec(select(Series).where(Series.subscribed == True)).all()

    for series in subscribed:
        try:
            created, removed = await sync_series(series, None)
            total_created += created
            total_removed += removed

            with Session(engine) as session:
                s = session.get(Series, series.series_id)
                if s:
                    s.last_synced = datetime.utcnow()
                    session.add(s)
                    session.commit()
        except Exception as exc:
            errors.append(f"{series.name}: {exc}")

    with Session(engine) as session:
        log = session.get(SyncLog, log_id)
        log.finished_at = datetime.utcnow()
        log.series_processed = len(subscribed)
        log.files_created = total_created
        log.files_removed = total_removed
        log.status = "error" if errors else "done"
        log.message = "; ".join(errors) if errors else None
        session.add(log)
        session.commit()
        session.refresh(log)
        return log
