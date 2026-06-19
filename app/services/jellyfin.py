"""
Jellyfin API client + stuck-session watchdog.

Detects XTREAM HLS sessions that are actively transcoding but stuck at
position 0 (symptom of a failed ffprobe / null MediaStreams cache). Once a
session has been stuck for longer than JELLYFIN_STUCK_THRESHOLD_SECONDS, the
watchdog kills the transcode and triggers a FullRefresh on the item so the
next playback attempt probes cleanly.
"""

from __future__ import annotations

import logging
import time

import httpx

from config import settings
from services.notify import send_discord

logger = logging.getLogger("streamarr.jellyfin")

# session_id → monotonic timestamp of when we first observed it stuck
_stuck_since: dict[str, float] = {}


def _is_xtream_item(now_playing: dict) -> bool:
    path = now_playing.get("Path", "")
    return path.endswith(".strm") or "xtream_strm" in path or "xtream_movies" in path


def _is_stuck(session: dict) -> bool:
    now_playing = session.get("NowPlayingItem")
    if not now_playing:
        return False
    if not _is_xtream_item(now_playing):
        return False
    play_state = session.get("PlayState") or {}
    if play_state.get("IsPaused"):
        return False
    position = play_state.get("PositionTicks")
    if position is None or position > 0:
        return False
    # Only flag sessions that are actively transcoding — not direct-play clients
    # that happen to be at the start of a file.
    return bool(session.get("TranscodingInfo"))


async def kill_stuck_sessions() -> dict:
    """Watchdog sweep. Returns a summary dict logged by the scheduler."""
    if not settings.jellyfin_api_key:
        return {"skipped": "JELLYFIN_API_KEY not set"}

    headers = {"X-Emby-Token": settings.jellyfin_api_key}
    base = settings.jellyfin_url.rstrip("/")
    killed: list[dict] = []
    refreshed: list[str] = []

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            r = await client.get(f"{base}/Sessions", headers=headers)
            r.raise_for_status()
            sessions: list[dict] = r.json()
        except Exception as exc:
            logger.warning("watchdog: failed to fetch Jellyfin sessions: %s", exc)
            return {"error": str(exc)}

        now = time.monotonic()
        active_ids = {s["Id"] for s in sessions if s.get("Id")}

        for session in sessions:
            sid = session.get("Id")
            if not sid:
                continue

            if not _is_stuck(session):
                _stuck_since.pop(sid, None)
                continue

            if sid not in _stuck_since:
                _stuck_since[sid] = now
                logger.debug("watchdog: session %s stuck at 0 (first observation)", sid)
                continue

            if now - _stuck_since[sid] < settings.jellyfin_stuck_threshold_seconds:
                continue

            # Stuck long enough — act.
            item = session.get("NowPlayingItem") or {}
            item_id = item.get("Id")
            item_name = item.get("Name", "?")
            device = session.get("DeviceName", "?")
            logger.warning(
                "watchdog: killing stuck session %s — %r on %r (stuck %.0fs)",
                sid, item_name, device, now - _stuck_since[sid],
            )

            # 1. Kill the active transcode encoding.
            transcode = session.get("TranscodingInfo") or {}
            device_id = session.get("DeviceId")
            play_session_id = transcode.get("PlaySessionId")
            if device_id and play_session_id:
                try:
                    await client.delete(
                        f"{base}/Videos/ActiveEncodings",
                        headers=headers,
                        params={"deviceId": device_id, "playSessionId": play_session_id},
                    )
                except Exception as exc:
                    logger.debug("watchdog: could not delete encoding for %s: %s", sid, exc)

            # 2. Force a FullRefresh to clear the null MediaStreams cache in the DB.
            if item_id:
                try:
                    await client.post(
                        f"{base}/Items/{item_id}/Refresh",
                        headers=headers,
                        params={
                            "metadataRefreshMode": "FullRefresh",
                            "imageRefreshMode": "None",
                            "replaceAllImages": "false",
                            "replaceAllMetadata": "false",
                        },
                    )
                    refreshed.append(item_id)
                    logger.info("watchdog: refreshed item %s (%s)", item_id, item_name)
                except Exception as exc:
                    logger.warning("watchdog: refresh failed for %s: %s", item_id, exc)

            killed.append({"session": sid, "item": item_name, "device": device})
            del _stuck_since[sid]

        # Evict sessions that disappeared from the active list.
        for sid in set(_stuck_since) - active_ids:
            del _stuck_since[sid]

    if killed:
        lines = [":stethoscope: **Jellyfin watchdog** — auto-healed stuck session(s):"]
        for k in killed:
            lines.append(f"  • `{k['item']}` on `{k['device']}`")
        await send_discord("\n".join(lines), title="Streamarr: stuck session healed")

    logger.debug(
        "watchdog sweep done: killed=%d refreshed=%d tracking=%d",
        len(killed), len(refreshed), len(_stuck_since),
    )
    return {"killed": killed, "refreshed": refreshed, "tracking": len(_stuck_since)}
