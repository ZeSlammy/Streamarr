"""
Discord webhook notifier. No-op when DISCORD_WEBHOOK_URL is unset.

We only fire on meaningful events (URL swap, audit found stale files, job error)
so a healthy night stays silent.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger("streamarr.notify")


async def send_discord(content: str, *, title: Optional[str] = None) -> bool:
    """POST `content` (and optional embed title) to DISCORD_WEBHOOK_URL.

    Returns True if delivered, False if disabled or the post failed.
    Discord allows up to 2000 chars in `content`; we truncate to be safe.
    """
    webhook = settings.discord_webhook_url
    if not webhook:
        return False

    payload: dict = {"content": content[:1900]}
    if title:
        payload["embeds"] = [{"title": title[:256], "description": content[:4000]}]
        payload["content"] = ""

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook, json=payload)
        if r.status_code >= 300:
            logger.warning("discord webhook returned %s: %s", r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as exc:
        logger.warning("discord webhook failed: %s", exc)
        return False
