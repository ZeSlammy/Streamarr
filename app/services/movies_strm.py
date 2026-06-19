"""
Movie catalog refresh + .strm generation.

Mirrors the series flow in `services/strm.py`, with two key differences:

- Catalog refresh upserts every VOD entry from XTREAM into the `movies`
  table, preserving `in_library`/`added_at` flags so user picks survive a
  full catalog rebuild.
- `.strm` files are written under `settings.library_path_movies` rather than
  `settings.library_path`. Folder layout follows Jellyfin/Kodi conventions:
  `{Name} ({Year}) [tmdbid-N]/{Name}.strm` when a tmdb_id is present, else
  `{Name} ({Year})/{Name}.strm`.
"""

from __future__ import annotations

import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from clients.xtream import get_vod_categories, get_vod_streams, movie_stream_url
from config import settings
from database import engine
from models import Movie, MovieCategory
from services.languages import derive_movie_language

logger = logging.getLogger("streamarr.movies_strm")


# Reuse the same name-cleaning regexes as the series side — providers tag
# movies with EN-/FR-/ES- prefixes and dupe `(YYYY)` segments too.
_LANG_PREFIX_RE = re.compile(r"^[A-Z]{2,3}\s*-\s*")
_COUNTRY_RE = re.compile(
    r"\s*\((?:US|UK|GB|FR|CA|AU|DE|ES|IT|JP|KR|MX|BR|IE|NZ|RU|SE|DK|NO|FI|NL|BE|PL|TR|IN|CN|HK|AR|CL|CO|CH|AT|PT|ZA)\)"
)
_YEAR_RE = re.compile(r"\s*\((\d{4})\)")
_QUALITY_RE = re.compile(r"\b(4K|UHD|HDR|HEVC|H\.?265|1080p|720p|480p|HD|FHD)\b", re.IGNORECASE)


@dataclass
class MovieSyncStats:
    catalog_total: int = 0
    catalog_new: int = 0
    catalog_updated: int = 0
    files_created: int = 0
    files_removed: int = 0
    errors: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []


def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "", name).strip()


def _clean_movie_name(name: str, release_year: str | None) -> tuple[str, str]:
    s = _LANG_PREFIX_RE.sub("", name).strip()
    s = _COUNTRY_RE.sub("", s).strip()
    s = _QUALITY_RE.sub("", s)
    years = _YEAR_RE.findall(s)
    s = _YEAR_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    year = years[0] if years else ""
    if not year and release_year:
        y = release_year[:4]
        if y.isdigit():
            year = y
    return s, year


def _movie_folder(movie: Movie) -> Path:
    title, year = _clean_movie_name(movie.name, movie.release_year)
    base = f"{title} ({year})" if year else title
    if movie.tmdb_id:
        base = f"{base} [tmdbid-{movie.tmdb_id}]"
    return Path(settings.library_path_movies) / _safe_name(base)


def _strm_path(folder: Path, movie: Movie) -> Path:
    title, _year = _clean_movie_name(movie.name, movie.release_year)
    filename = _safe_name(f"{title}.strm")
    return folder / filename


def _coerce_year(raw) -> str:
    """Parse a `YYYY` from a `YYYY-MM-DD`/`YYYY` string. Returns "" on miss.

    Won't accept epoch timestamps masquerading as years — we constrain to a
    sane release-year window (1900–2099).
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if len(s) < 4 or not s[:4].isdigit():
        return ""
    y = s[:4]
    if 1900 <= int(y) <= 2099:
        return y
    return ""


def _year_from_name(name: str) -> str:
    """Most providers tag year in the title as `(YYYY)`. Extract the last such
    occurrence so `(2025)` wins over `(GB)`-style country tags before it."""
    if not name:
        return ""
    matches = _YEAR_RE.findall(name)
    if not matches:
        return ""
    for candidate in reversed(matches):
        if 1900 <= int(candidate) <= 2099:
            return candidate
    return ""


def _coerce_int(raw) -> int | None:
    if raw in (None, "", 0, "0"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _coerce_float(raw) -> float | None:
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def refresh_vod_catalog() -> MovieSyncStats:
    """Pull get_vod_categories + get_vod_streams, upsert into movies table.

    Preserves `in_library` / `added_at` for existing rows. Updates
    `last_seen_in_catalog` for every row touched.
    """
    stats = MovieSyncStats()
    cats = await get_vod_categories()
    streams = await get_vod_streams()
    stats.catalog_total = len(streams)

    cats_by_id: dict[str, str] = {}
    for c in cats:
        cid = str(c.get("category_id", ""))
        if cid:
            cats_by_id[cid] = c.get("category_name", "") or ""

    now = datetime.utcnow()
    with Session(engine) as session:
        for c in cats:
            cid = str(c.get("category_id", ""))
            if not cid:
                continue
            name = c.get("category_name", "") or cid
            existing = session.get(MovieCategory, cid)
            if existing:
                if existing.category_name != name:
                    existing.category_name = name
                    session.add(existing)
            else:
                session.add(MovieCategory(category_id=cid, category_name=name))
        session.commit()

        # Movies upsert
        for entry in streams:
            sid = entry.get("stream_id") or entry.get("vod_id") or entry.get("id")
            sid = _coerce_int(sid)
            if sid is None:
                continue

            cat_id = str(entry.get("category_id", "") or "")
            cat_name = cats_by_id.get(cat_id, "")
            genre = cat_name or entry.get("genre", "") or ""
            lang, subs_only, lang_source = derive_movie_language(
                cat_name, entry.get("name", "")
            )
            # Provider rarely populates structured year fields for VOD — pull
            # from the title (`Foo (2025)`) as the primary signal.
            year = (
                _coerce_year(
                    entry.get("releaseDate")
                    or entry.get("release_date")
                    or entry.get("year")
                )
                or _year_from_name(entry.get("name", ""))
            )

            existing = session.get(Movie, sid)
            if existing:
                # Catalog-sourced fields fully refresh each pass — keeping stale
                # values when the new entry has an empty field would let a one-time
                # bad upsert (e.g. wrong year fallback) outlive the bug. User-state
                # fields (`in_library`, `added_at`) are explicitly NOT touched.
                existing.name = entry.get("name", existing.name) or existing.name
                existing.cover = entry.get("stream_icon", "") or ""
                new_tmdb = _coerce_int(entry.get("tmdb_id") or entry.get("tmdb"))
                if new_tmdb is not None:
                    existing.tmdb_id = new_tmdb
                existing.release_year = year
                existing.genre = genre
                existing.rating = str(entry.get("rating", "") or "")
                existing.rating_5based = _coerce_float(entry.get("rating_5based"))
                existing.category_id = cat_id
                existing.container_extension = entry.get("container_extension") or "mp4"
                existing.last_seen_in_catalog = now
                existing.lang = lang
                existing.lang_source = lang_source
                existing.subs_only = subs_only
                session.add(existing)
                stats.catalog_updated += 1
            else:
                session.add(
                    Movie(
                        vod_id=sid,
                        name=entry.get("name", "") or f"VOD {sid}",
                        tmdb_id=_coerce_int(entry.get("tmdb_id") or entry.get("tmdb")),
                        cover=entry.get("stream_icon") or "",
                        plot=entry.get("plot", "") or "",
                        release_year=year,
                        genre=genre,
                        rating=str(entry.get("rating", "") or ""),
                        rating_5based=_coerce_float(entry.get("rating_5based")),
                        category_id=cat_id,
                        container_extension=entry.get("container_extension") or "mp4",
                        in_library=False,
                        last_seen_in_catalog=now,
                        lang=lang,
                        lang_source=lang_source,
                        subs_only=subs_only,
                    )
                )
                stats.catalog_new += 1

        session.commit()

    logger.info(
        "VOD catalog refresh: total=%d new=%d updated=%d",
        stats.catalog_total,
        stats.catalog_new,
        stats.catalog_updated,
    )
    return stats


def sync_movie(movie: Movie) -> int:
    """Write the .strm file for one in-library movie. Returns 1 if a file was
    (re)written, 0 if it was already up to date."""
    folder = _movie_folder(movie)
    folder.mkdir(parents=True, exist_ok=True)
    strm = _strm_path(folder, movie)
    url = movie_stream_url(movie.vod_id, movie.container_extension or "mp4")
    if not strm.exists() or strm.read_text(encoding="utf-8").strip() != url:
        strm.write_text(url, encoding="utf-8")
        return 1
    return 0


def remove_movie_file(movie: Movie) -> int:
    """Delete the .strm + parent folder for a movie that was removed from
    the library. Returns count of files removed (0 or 1)."""
    folder = _movie_folder(movie)
    removed = 0
    if folder.exists():
        for f in folder.rglob("*.strm"):
            f.unlink()
            removed += 1
        try:
            folder.rmdir()
        except OSError:
            pass
    return removed


def sync_all_in_library_movies() -> tuple[int, int, list[str]]:
    """Regenerate .strm for every in_library movie. Returns (created, removed, errors).

    `removed` counts orphan .strm files (movies no longer in library) cleaned
    up under `library_path_movies`.
    """
    created = 0
    errors: list[str] = []
    expected_folders: set[Path] = set()

    with Session(engine) as session:
        in_library = session.exec(
            select(Movie).where(Movie.in_library == True)  # noqa: E712
        ).all()

    for movie in in_library:
        try:
            created += sync_movie(movie)
            expected_folders.add(_movie_folder(movie))
            with Session(engine) as session:
                m = session.get(Movie, movie.vod_id)
                if m:
                    m.last_synced = datetime.utcnow()
                    session.add(m)
                    session.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{movie.name}: {exc}")

    root = Path(settings.library_path_movies)
    removed = 0
    if root.exists():
        for child in root.iterdir():
            if child.is_dir() and child not in expected_folders:
                # Orphan — strip its .strm files and try to drop the folder.
                for f in child.rglob("*.strm"):
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass
                try:
                    child.rmdir()
                except OSError:
                    pass

    return created, removed, errors


def _expected_movie_prefix() -> str:
    mirror = (settings.mirror_public_url or "").rstrip("/")
    if mirror:
        return f"{mirror}/xtream/movie/{settings.mirror_username}/{settings.mirror_password}/"
    from clients.xtream import active_base

    return f"{active_base()}/movie/{settings.xtream_username}/{settings.xtream_password}/"


def audit_movie_strm_urls(sample_size: int | None = None) -> dict:
    """Spot-check .strm files against the URL prefix the sync would write today.

    Same shape/output as the series audit so the scheduler can present it
    uniformly.
    """
    if sample_size is None:
        sample_size = settings.strm_audit_sample
    expected_prefix = _expected_movie_prefix()

    with Session(engine) as session:
        in_library = session.exec(
            select(Movie).where(Movie.in_library == True)  # noqa: E712
        ).all()

    if not in_library:
        return {
            "sampled": 0,
            "stale": 0,
            "fresh": 0,
            "missing": 0,
            "examples": [],
            "base": expected_prefix,
        }

    sample = random.sample(in_library, min(sample_size, len(in_library)))
    stale = 0
    fresh = 0
    missing = 0
    examples: list[str] = []

    for movie in sample:
        folder = _movie_folder(movie)
        first = next(folder.rglob("*.strm"), None) if folder.exists() else None
        if first is None:
            missing += 1
            continue
        try:
            content = first.read_text(encoding="utf-8").strip()
        except OSError:
            missing += 1
            continue
        if content.startswith(expected_prefix):
            fresh += 1
        else:
            stale += 1
            if len(examples) < 3:
                head = content.split("/movie/")[0] if "/movie/" in content else content[:60]
                examples.append(f"{movie.name}: {head}")

    return {
        "sampled": len(sample),
        "stale": stale,
        "fresh": fresh,
        "missing": missing,
        "examples": examples,
        "base": expected_prefix,
    }
