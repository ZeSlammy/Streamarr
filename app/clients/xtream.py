from dataclasses import dataclass, field
from typing import Any, Optional
import httpx
from config import settings
from services.providers import current_url

# ISO 639-2 → readable name for the most common codes
_LANG_MAP = {
    "eng": "English", "fre": "French", "fra": "French",
    "spa": "Spanish", "ger": "German", "deu": "German",
    "ita": "Italian", "por": "Portuguese", "rus": "Russian",
    "jpn": "Japanese", "chi": "Chinese", "zho": "Chinese",
    "ara": "Arabic", "kor": "Korean", "dut": "Dutch",
    "nld": "Dutch", "pol": "Polish", "tur": "Turkish",
    "swe": "Swedish", "nor": "Norwegian", "dan": "Danish",
    "fin": "Finnish", "heb": "Hebrew", "hin": "Hindi",
    "cze": "Czech", "ces": "Czech", "hun": "Hungarian",
    "rum": "Romanian", "ron": "Romanian", "gre": "Greek",
    "ell": "Greek", "und": "Unknown",
}


def _normalize_lang(code: str) -> str:
    return _LANG_MAP.get(code.lower(), code.title())


def _normalize_episodes(raw: Any) -> dict[str, list]:
    """Some series come back with episodes as ``{"1": [...], "2": [...]}`` (normal),
    others as ``[[...], [...]]`` — a list of seasons. Coerce both into the dict shape,
    keyed by each episode's own ``season`` field (with positional fallback)."""
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, list):
        return {}
    out: dict[str, list] = {}
    for idx, group in enumerate(raw, start=1):
        if not isinstance(group, list):
            continue
        for ep in group:
            if not isinstance(ep, dict):
                continue
            season_key = str(ep.get("season", idx))
            out.setdefault(season_key, []).append(ep)
    return out


def _to_list(x: Any) -> list:
    """Providers send audio/subtitles/video as either a list, a single dict, or null."""
    if x is None:
        return []
    if isinstance(x, dict):
        return [x]
    if isinstance(x, list):
        return x
    return []


def _track_language(track: Any) -> Optional[str]:
    if not isinstance(track, dict):
        return None
    tags = track.get("tags") if isinstance(track.get("tags"), dict) else {}
    return (
        tags.get("language")
        or tags.get("LANGUAGE")
        or track.get("language")
        or tags.get("title")
        or None
    )


def _extract_track_languages(raw_episodes: dict) -> tuple[list[str], list[str], Optional[str]]:
    """
    Scan the first episode that has track info and return:
      (audio_language_list, subtitle_language_list, video_info_string)
    """
    audio: list[str] = []
    subs: list[str] = []
    video_info: Optional[str] = None

    for season_eps in raw_episodes.values():
        for ep in season_eps:
            info = ep.get("info") or {}
            if not info:
                continue

            for track in _to_list(info.get("audio")):
                lang = _track_language(track)
                if lang:
                    label = _normalize_lang(lang)
                    if label not in audio:
                        audio.append(label)

            for sub in _to_list(info.get("subtitles")):
                lang = _track_language(sub)
                if lang:
                    label = _normalize_lang(lang)
                    if label not in subs:
                        subs.append(label)

            for vid in _to_list(info.get("video")):
                h = vid.get("height")
                codec = vid.get("codec_name", "")
                parts = []
                if h:
                    parts.append(f"{h}p")
                if codec:
                    parts.append(codec.upper())
                if parts and not video_info:
                    video_info = " / ".join(parts)
                break

            if audio or subs or video_info:
                return audio, subs, video_info

    return audio, subs, video_info


@dataclass
class XtreamEpisode:
    id: int
    title: str
    season: int
    episode_num: int
    container_extension: str
    plot: str = ""


@dataclass
class XtreamSeriesInfo:
    series_id: int
    name: str
    cover: str = ""
    plot: str = ""
    genre: str = ""
    release_date: str = ""
    rating: str = ""
    rating_5based: float = 0.0
    category_id: str = ""
    episodes: dict[int, list[XtreamEpisode]] = field(default_factory=dict)
    audio_languages: list[str] = field(default_factory=list)
    subtitle_languages: list[str] = field(default_factory=list)
    video_info: Optional[str] = None
    episode_count: int = 0
    season_count: int = 0
    tmdb_id: Optional[int] = None


def _base_params() -> dict:
    return {
        "username": settings.xtream_username,
        "password": settings.xtream_password,
    }


def active_base() -> str:
    """Active XTREAM base URL — from URL.md if present, else .env."""
    return (current_url() or settings.xtream_url).rstrip("/")


# Back-compat alias for the older private name still imported by services.strm
_active_base = active_base


def _api_url() -> str:
    return f"{active_base()}/player_api.php"


async def get_series_categories() -> list[dict]:
    params = {**_base_params(), "action": "get_series_categories"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(_api_url(), params=params)
        r.raise_for_status()
        return r.json()


async def get_series(category_id: str | None = None) -> list[dict]:
    params = {**_base_params(), "action": "get_series"}
    if category_id:
        params["category_id"] = category_id
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(_api_url(), params=params)
        r.raise_for_status()
        return r.json()


async def get_vod_categories() -> list[dict]:
    params = {**_base_params(), "action": "get_vod_categories"}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(_api_url(), params=params)
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


async def get_vod_streams(category_id: str | None = None) -> list[dict]:
    params = {**_base_params(), "action": "get_vod_streams"}
    if category_id:
        params["category_id"] = category_id
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(_api_url(), params=params)
        r.raise_for_status()
        data = r.json()
    return data if isinstance(data, list) else []


async def get_vod_info(vod_id: int) -> dict:
    """Single-film detail call. Used by enrichment in Phase 2b — for Phase 2a
    the proxy handler just forwards upstream when needed."""
    params = {**_base_params(), "action": "get_vod_info", "vod_id": vod_id}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(_api_url(), params=params)
        r.raise_for_status()
        return r.json()


def movie_stream_url(vod_id: int, extension: str) -> str:
    """URL embedded in movie .strm files. Points at the mirror when
    MIRROR_PUBLIC_URL is set so URL failover propagates without rewriting
    every .strm. Same pattern as `stream_url` for series."""
    mirror = (settings.mirror_public_url or "").rstrip("/")
    if mirror:
        u = settings.mirror_username
        p = settings.mirror_password
        return f"{mirror}/xtream/movie/{u}/{p}/{vod_id}.{extension}"
    base = active_base()
    u = settings.xtream_username
    p = settings.xtream_password
    return f"{base}/movie/{u}/{p}/{vod_id}.{extension}"


async def get_series_info(series_id: int) -> XtreamSeriesInfo:
    params = {**_base_params(), "action": "get_series_info", "series_id": series_id}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(_api_url(), params=params)
        r.raise_for_status()
        data = r.json()

    info: dict[str, Any] = data.get("info", {})
    raw_episodes = _normalize_episodes(data.get("episodes"))

    episodes: dict[int, list[XtreamEpisode]] = {}
    for season_str, ep_list in raw_episodes.items():
        season = int(season_str)
        episodes[season] = [
            XtreamEpisode(
                id=int(ep["id"]),
                title=ep.get("title", f"Episode {ep.get('episode_num', '?')}"),
                season=season,
                episode_num=int(ep.get("episode_num", 0)),
                container_extension=ep.get("container_extension", "mkv"),
                plot=ep.get("info", {}).get("plot", ""),
            )
            for ep in ep_list
        ]

    audio_langs, sub_langs, video_info = _extract_track_languages(raw_episodes)
    ep_count = sum(len(eps) for eps in episodes.values())

    tmdb_raw = info.get("tmdb") or info.get("tmdb_id")
    tmdb_id: Optional[int] = None
    try:
        if tmdb_raw not in (None, "", 0, "0"):
            tmdb_id = int(tmdb_raw)
    except (TypeError, ValueError):
        tmdb_id = None

    return XtreamSeriesInfo(
        series_id=series_id,
        name=info.get("name", ""),
        cover=info.get("cover", ""),
        plot=info.get("plot", ""),
        genre=info.get("genre", ""),
        release_date=info.get("releaseDate", "") or info.get("release_date", ""),
        rating=str(info.get("rating", "")),
        rating_5based=float(info.get("rating_5based", 0) or 0),
        category_id=str(info.get("category_id", "")),
        episodes=episodes,
        audio_languages=audio_langs,
        subtitle_languages=sub_langs,
        video_info=video_info,
        episode_count=ep_count,
        season_count=len(episodes),
        tmdb_id=tmdb_id,
    )


def stream_url(episode_id: int, extension: str) -> str:
    """URL embedded in .strm files. Points at the mirror when MIRROR_PUBLIC_URL
    is set so clients (Jellyfin/Kodi) follow our 302 to the active provider —
    that way URL rotations propagate without rewriting every .strm again."""
    mirror = (settings.mirror_public_url or "").rstrip("/")
    if mirror:
        u = settings.mirror_username
        p = settings.mirror_password
        return f"{mirror}/xtream/series/{u}/{p}/{episode_id}.{extension}"
    base = active_base()
    u = settings.xtream_username
    p = settings.xtream_password
    return f"{base}/series/{u}/{p}/{episode_id}.{extension}"
