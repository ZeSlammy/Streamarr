from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    xtream_url: str = ""
    xtream_username: str = ""
    xtream_password: str = ""

    trakt_client_id: str = ""
    trakt_client_secret: str = ""
    trakt_redirect_uri: str = "http://localhost:8000/trakt/callback"

    library_path: str = "/library"
    library_path_movies: str = "/library_movies"
    db_path: str = "/data/db.sqlite"
    providers_file: str = "/data/URL.md"

    # TMDB (Phase 2b). Empty disables enrichment + Radarr request UI.
    tmdb_api_key: str = ""

    # Radarr integration (Phase 2b). Empty `radarr_url` disables the Request button.
    radarr_url: str = ""
    radarr_api_key: str = ""
    radarr_root_folder: str = "/media/WD4_Films"
    radarr_quality_profile_id: int = 1

    # Nightly job: failover check + catalog refresh + .strm sync
    sync_cron_hour: int = 3
    sync_cron_minute: int = 0
    sync_cron_timezone: str = "Europe/Paris"
    sync_cron_enabled: bool = True

    # How many subscribed shows to spot-check during the nightly .strm audit
    strm_audit_sample: int = 10

    # Discord webhook for failover / audit / error notifications. No-op if empty.
    discord_webhook_url: str = ""

    # EPGenius — push new XTREAM URL to EPGenius after failover so the curated
    # EPG playlist stays in sync with the live provider.
    # Get your API key and Discord ID from the EPGenius bot (/info command).
    epgenius_enabled: bool = True
    epgenius_api_key: str = ""
    epgenius_discord_id: str = ""
    epgenius_playlist_id: str = ""  # Playlist Key shown by the bot (integer value)
    epgenius_m3u_url: str = ""      # Google Drive download URL from the bot (/info)

    # Tombstone-spike alarm: how many new TMDB 404s in a single nightly
    # enrichment run before Discord fires even if nothing else changed.
    # ≥25% of NIGHTLY_TMDB_LIMIT (200) is a strong signal that either upstream
    # catalog metadata regressed or our /movie/{id} → /tv/{id} routing is wrong.
    tombstone_spike_threshold: int = 50

    # Streaming proxy — when true, stream_redirect proxies bytes through a local
    # FFmpeg remux instead of returning a 302. Normalises timestamps across all
    # tracks (fixes subtitle lag) and hides the double-redirect chain from
    # Jellyfin's ffprobe (prevents stuck HLS sessions).
    proxy_streams: bool = False

    # Jellyfin API — used by the stuck-session watchdog.
    # Generate a key at: Dashboard → API Keys → +
    jellyfin_url: str = "http://localhost:8096"
    jellyfin_api_key: str = ""
    jellyfin_watchdog_enabled: bool = True
    jellyfin_watchdog_interval_minutes: int = 2
    # Seconds a session must be stuck at position 0 before the watchdog kills it.
    jellyfin_stuck_threshold_seconds: int = 45

    # Base URL clients (Dispatcharr, TiviMate, ...) use to reach the /xtream
    # mirror endpoints. Embedded in rewritten m3u playlists and in .strm files,
    # so it has to be reachable from wherever your clients live. Set to "" to
    # disable the mirror and embed direct upstream URLs in .strm files.
    mirror_public_url: str = "http://localhost:8011"

    # Placeholder creds the mirror advertises to clients. The picker ignores
    # whatever the client sends and substitutes the real XTREAM_USERNAME/PASSWORD
    # when forwarding upstream, so these can be anything.
    mirror_username: str = "mirror"
    mirror_password: str = "mirror"

    # Phase 3 — movie language filter (browse-side, not mirror — mirror already
    # gates on in_library).
    # Comma-separated ISO 639-1 codes (use `multi` for explicit multi-audio).
    # Empty string disables the filter even if `language_filter_enabled=true`.
    allowed_languages: str = "fr,en"
    # Show entries we couldn't tag (no recognisable XX- prefix).
    allow_language_unknown: bool = False
    # Show entries where the prefix encodes subtitles, not audio (VOSTFR / MULTI-SUBS).
    allow_language_subs_only: bool = False
    # Master switch — when false, the browse UI shows the full catalog.
    language_filter_enabled: bool = True

    class Config:
        env_file = ".env"

    @property
    def allowed_languages_set(self) -> set[str]:
        from services.languages import parse_allowed_languages
        return parse_allowed_languages(self.allowed_languages)


settings = Settings()
