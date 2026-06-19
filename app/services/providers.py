"""
Provider URL list manager.

Reads / writes a plain-text file with three regions:

    CURRENT:
    http://<active-url>

    http://<candidate-1>
    http://<candidate-2>

    BURNED:
    http://<old-url>  # burned 2026-05-16T03:00:00Z (401)

Only the first non-blank URL under CURRENT: is the active one; remaining URLs
between it and BURNED: are alternates we'll try when the active one dies.

Credentials (username/password) are shared across all URLs and come from .env.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("streamarr.providers")

from config import settings
from services.epgenius import push_credentials as _push_epgenius
from services.notify import send_discord


@dataclass
class ProviderState:
    current: Optional[str] = None
    candidates: list[str] = field(default_factory=list)
    burned: list[str] = field(default_factory=list)  # raw lines, may contain comments


_URL_RE = re.compile(r"https?://\S+")


def _strip_comment(line: str) -> str:
    """Drop trailing `# comment` and surrounding whitespace."""
    idx = line.find("#")
    if idx >= 0:
        line = line[:idx]
    return line.strip()


def _extract_url(line: str) -> Optional[str]:
    cleaned = _strip_comment(line)
    if not cleaned:
        return None
    m = _URL_RE.match(cleaned)
    return m.group(0).rstrip("/") if m else None


def _providers_path() -> Path:
    return Path(settings.providers_file)


def read_state() -> ProviderState:
    """Parse URL.md into a ProviderState. Missing file → empty state."""
    path = _providers_path()
    if not path.exists():
        return ProviderState()

    state = ProviderState()
    section: Optional[str] = None
    raw_burned: list[str] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        # Section headers (case-insensitive, optional colon)
        head = stripped.rstrip(":").upper()
        if head in ("CURRENT", "BURNED", "CANDIDATES"):
            section = head
            continue

        url = _extract_url(raw_line)
        if section == "CURRENT":
            if url is None:
                continue
            if state.current is None:
                state.current = url
            else:
                state.candidates.append(url)
        elif section == "CANDIDATES":
            if url is not None:
                state.candidates.append(url)
        elif section == "BURNED":
            # keep the raw line so we preserve burn timestamps / reasons
            if url is not None:
                raw_burned.append(stripped)

    state.burned = raw_burned

    # Dedup candidates while preserving order; never list current among them.
    seen = {state.current} if state.current else set()
    deduped: list[str] = []
    for c in state.candidates:
        if c in seen:
            continue
        seen.add(c)
        deduped.append(c)
    state.candidates = deduped

    return state


def write_state(state: ProviderState) -> None:
    """Persist the file. In-place write — docker bind-mounts a single file
    by inode, which makes the tmp+rename trick break the mount."""
    path = _providers_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = ["CURRENT:"]
    if state.current:
        lines.append(state.current)
    lines.append("")
    for c in state.candidates:
        lines.append(c)
    lines.append("")
    lines.append("BURNED:")
    for b in state.burned:
        lines.append(b)
    lines.append("")  # trailing newline

    path.write_text("\n".join(lines), encoding="utf-8")


def current_url() -> Optional[str]:
    """Active XTREAM base URL according to URL.md; falls back to settings.xtream_url."""
    url = read_state().current
    if url:
        return url
    return settings.xtream_url.rstrip("/") if settings.xtream_url else None


async def probe(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """
    Try player_api.php?action=get_account_info on `url` with .env creds.

    Returns (alive, reason). Alive means the request succeeded AND the body parses
    as JSON with a non-empty user_info AND status is anything other than 'Banned'/'Expired'.
    """
    base = url.rstrip("/")
    params = {
        "username": settings.xtream_username,
        "password": settings.xtream_password,
        "action": "get_account_info",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.get(f"{base}/player_api.php", params=params)
    except httpx.HTTPError as exc:
        return False, f"network: {exc.__class__.__name__}"

    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"

    try:
        data = r.json()
    except ValueError:
        return False, "non-JSON response"

    # Some providers return {} when creds are wrong but URL is alive.
    # We need a positive signal: user_info present and not flagged.
    user_info = data.get("user_info") if isinstance(data, dict) else None
    if not isinstance(user_info, dict) or not user_info:
        return False, "empty user_info"

    status = str(user_info.get("status", "")).lower()
    if status in {"banned", "expired", "disabled"}:
        return False, f"account status: {status}"

    return True, "ok"


async def run_failover_check() -> dict:
    """
    Probe the current URL; if dead, walk candidates until one passes.
    Rewrites URL.md atomically. Returns a summary dict for logging / UI.
    """
    state = read_state()
    summary: dict = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "previous": state.current,
        "current": state.current,
        "swapped": False,
        "reason": None,
        "tried": [],
    }

    if not state.current and not state.candidates:
        summary["reason"] = "no URLs configured"
        return summary

    # 1. Probe current
    if state.current:
        alive, reason = await probe(state.current)
        summary["tried"].append({"url": state.current, "alive": alive, "reason": reason})
        if alive:
            summary["reason"] = "current still alive"
            return summary
        dead_url = state.current
        dead_reason = reason
    else:
        dead_url = None
        dead_reason = "no current"

    # 2. Walk candidates
    new_current: Optional[str] = None
    remaining: list[str] = []
    for cand in state.candidates:
        if new_current is not None:
            remaining.append(cand)
            continue
        alive, reason = await probe(cand)
        summary["tried"].append({"url": cand, "alive": alive, "reason": reason})
        if alive:
            new_current = cand
        else:
            # Demote dead candidates too — saves time on the next run.
            state.burned.append(_burn_line(cand, reason))

    if new_current is None:
        summary["reason"] = "no working URL among candidates"
        summary["current"] = state.current  # leave as-is
        # Still write back: we may have burned some candidates.
        state.candidates = []
        write_state(state)
        return summary

    # 3. Promote + demote
    if dead_url:
        state.burned.append(_burn_line(dead_url, dead_reason))
    state.current = new_current
    state.candidates = remaining
    write_state(state)

    summary.update(
        current=new_current,
        swapped=True,
        reason=f"promoted after {dead_reason}",
    )
    epg_ok, epg_detail = await _push_epgenius(new_current)
    if epg_ok:
        logger.info("EPGenius credentials updated for %s", new_current)
        epg_line = ":satellite: EPGenius updated."
    elif epg_detail in ("disabled", "not configured"):
        epg_line = ""
    else:
        logger.warning("EPGenius push failed: %s", epg_detail)
        epg_line = f":warning: EPGenius push failed: `{epg_detail}`"

    msg = (
        f":arrows_counterclockwise: **XTREAM URL swapped**\n"
        f"`{dead_url}` → `{new_current}`\n"
        f"Reason: {dead_reason}"
    )
    if epg_line:
        msg += f"\n{epg_line}"
    await send_discord(msg, title="Streamarr: URL failover")
    summary["epgenius_push"] = {"ok": epg_ok, "detail": epg_detail}
    return summary


def _burn_line(url: str, reason: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    safe_reason = reason.replace("\n", " ").strip() or "unspecified"
    return f"{url}  # burned {ts} ({safe_reason})"


if __name__ == "__main__":
    # Quick CLI for local debugging: `python -m services.providers`
    async def _main():
        result = await run_failover_check()
        import json
        print(json.dumps(result, indent=2))
    asyncio.run(_main())
