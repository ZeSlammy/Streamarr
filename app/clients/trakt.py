from datetime import datetime, timedelta
from typing import Optional
import httpx
from config import settings

TRAKT_API = "https://api.trakt.tv"


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "trakt-api-version": "2",
        "trakt-api-key": settings.trakt_client_id,
        "Content-Type": "application/json",
    }


def auth_url(state: str = "") -> str:
    return (
        f"https://trakt.tv/oauth/authorize"
        f"?response_type=code"
        f"&client_id={settings.trakt_client_id}"
        f"&redirect_uri={settings.trakt_redirect_uri}"
        f"&state={state}"
    )


async def exchange_code(code: str) -> dict:
    payload = {
        "code": code,
        "client_id": settings.trakt_client_id,
        "client_secret": settings.trakt_client_secret,
        "redirect_uri": settings.trakt_redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TRAKT_API}/oauth/token", json=payload)
        r.raise_for_status()
        return r.json()


async def refresh_token(refresh_tok: str) -> dict:
    payload = {
        "refresh_token": refresh_tok,
        "client_id": settings.trakt_client_id,
        "client_secret": settings.trakt_client_secret,
        "redirect_uri": settings.trakt_redirect_uri,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TRAKT_API}/oauth/token", json=payload)
        r.raise_for_status()
        return r.json()


async def get_watchlist(access_token: str) -> list[dict]:
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{TRAKT_API}/users/me/watchlist/shows",
            headers=_headers(access_token),
        )
        r.raise_for_status()
        return r.json()


async def get_watched_shows(access_token: str) -> list[dict]:
    """Returns one entry per unique show the user has scrobbled, with play counts."""
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(
            f"{TRAKT_API}/users/me/watched/shows",
            headers=_headers(access_token),
        )
        r.raise_for_status()
        return r.json()


async def get_show_ratings(access_token: str) -> list[dict]:
    """Returns shows the user has rated on Trakt."""
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{TRAKT_API}/users/me/ratings/shows",
            headers=_headers(access_token),
        )
        r.raise_for_status()
        return r.json()


async def search_show(title: str, year: Optional[int] = None) -> Optional[dict]:
    """Search Trakt for a show by title, return best match."""
    params: dict = {"query": title, "extended": "full"}
    if year:
        params["years"] = str(year)
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{TRAKT_API}/search/show",
            params=params,
            headers={
                "trakt-api-version": "2",
                "trakt-api-key": settings.trakt_client_id,
            },
        )
        if r.status_code != 200:
            return None
        results = r.json()
        return results[0] if results else None


def token_expires_at(expires_in_seconds: int) -> datetime:
    return datetime.utcnow() + timedelta(seconds=expires_in_seconds)
