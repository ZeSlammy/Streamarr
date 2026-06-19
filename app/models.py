from datetime import datetime
from typing import Optional
from sqlmodel import Field, SQLModel


class Series(SQLModel, table=True):
    series_id: int = Field(primary_key=True)
    name: str
    cover: Optional[str] = None
    plot: Optional[str] = None
    genre: Optional[str] = None
    release_date: Optional[str] = None
    rating: Optional[str] = None
    rating_5based: Optional[float] = None
    category_id: Optional[str] = None
    subscribed: bool = False
    last_synced: Optional[datetime] = None
    trakt_id: Optional[int] = None
    tmdb_id: Optional[int] = None  # from provider `info.tmdb`; folder gets `[tmdbid-N]` so Jellyfin matches deterministically
    # Language metadata — populated lazily when a show's detail is first opened
    audio_languages: Optional[str] = None    # comma-separated, e.g. "English, French"
    subtitle_languages: Optional[str] = None
    video_info: Optional[str] = None         # e.g. "1080p / H.264"
    episode_count: Optional[int] = None
    season_count: Optional[int] = None
    detail_fetched: bool = False


class Category(SQLModel, table=True):
    category_id: str = Field(primary_key=True)
    category_name: str


class Movie(SQLModel, table=True):
    """XTREAM VOD entry. Catalog rows are upserted by the nightly refresh;
    `in_library` and `added_at` are preserved across refreshes."""

    vod_id: int = Field(primary_key=True)
    name: str
    tmdb_id: Optional[int] = None
    cover: Optional[str] = None
    plot: Optional[str] = None
    release_year: Optional[str] = None
    genre: Optional[str] = None
    rating: Optional[str] = None
    rating_5based: Optional[float] = None
    category_id: Optional[str] = None
    container_extension: Optional[str] = None
    stream_url: Optional[str] = None
    in_library: bool = False
    added_at: Optional[datetime] = None
    last_seen_in_catalog: Optional[datetime] = None
    last_synced: Optional[datetime] = None
    # Phase 3 — language filter. `lang` is lowercased ISO 639-1 ("fr", "en", …)
    # or "multi" for explicit multi-audio tags, or NULL when no signal could be
    # derived from category/title prefix. `subs_only` flags VOSTFR / MULTI-SUBS
    # entries (original audio, our language in subs only).
    lang: Optional[str] = None
    lang_source: Optional[str] = None  # "category" | "title" | NULL
    subs_only: bool = False


class MovieCategory(SQLModel, table=True):
    """Mirror of XTREAM `get_vod_categories`. Separate from `Category`
    (which is for series) so catalog refreshes don't collide on identical IDs."""

    category_id: str = Field(primary_key=True)
    category_name: str


class MovieTmdb(SQLModel, table=True):
    """TMDB enrichment cache. Rows are filled by the enrichment job and reused
    for 30 days before refreshing. `cast` and `director` are JSON-encoded
    lists so we can keep the schema flat."""

    tmdb_id: int = Field(primary_key=True)
    title: Optional[str] = None
    original_title: Optional[str] = None
    overview: Optional[str] = None
    release_date: Optional[str] = None
    runtime: Optional[int] = None
    genres: Optional[str] = None          # JSON list of genre names
    cast_field: Optional[str] = None      # JSON list [{name, character, order}]
    director: Optional[str] = None        # JSON list of names
    poster_path: Optional[str] = None
    backdrop_path: Optional[str] = None
    collection_id: Optional[int] = None
    collection_name: Optional[str] = None
    enriched_at: Optional[datetime] = None
    original_language: Optional[str] = None  # ISO 639-1 from TMDB; informational, not filter signal
    # Tombstone: tmdb_id 404'd on /movie/. Skipped from candidates() until last_attempt_at ages past MISS_TTL.
    not_found: bool = False
    last_attempt_at: Optional[datetime] = None  # updated on every attempt (success or 404)


class Collection(SQLModel, table=True):
    """TMDB collection — populated from `belongs_to_collection` during
    enrichment. `available_count` / `in_library_count` are recomputed after
    each enrichment batch."""

    collection_id: int = Field(primary_key=True)
    name: str
    poster_path: Optional[str] = None
    tmdb_total_parts: int = 0
    available_count: int = 0
    in_library_count: int = 0


class CollectionPart(SQLModel, table=True):
    """One film entry inside a TMDB collection. Used to drive the per-collection
    UI list, including films NOT present in the XTREAM catalog (the Request
    button targets these)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    collection_id: int
    tmdb_id: int
    title: str
    release_date: Optional[str] = None
    poster_path: Optional[str] = None


class MovieRequest(SQLModel, table=True):
    """Films sent to Radarr from the Collections UI. Used to flip the button
    from 'Request' to 'Requested' on subsequent renders."""

    tmdb_id: int = Field(primary_key=True)
    title: str
    requested_at: datetime = Field(default_factory=datetime.utcnow)
    radarr_id: Optional[int] = None


class SyncLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    finished_at: Optional[datetime] = None
    series_processed: int = 0
    files_created: int = 0
    files_removed: int = 0
    status: str = "running"  # running | done | error
    message: Optional[str] = None


class TraktToken(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    access_token: str
    refresh_token: str
    expires_at: datetime
