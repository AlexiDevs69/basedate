"""
Spotify Web API helper: OAuth token exchange/refresh + "what's playing now".

This only ever talks to Spotify on behalf of the single connected admin
account (the one row in spotify_auth, id=1) -- there's no per-visitor
OAuth here, this exists purely to power one "currently listening" widget
on /profile.
"""
import base64
import time
from urllib.parse import urlencode

import httpx

import crud
from config import get_settings

settings = get_settings()

TOKEN_URL = "https://accounts.spotify.com/api/token"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
NOW_PLAYING_URL = "https://api.spotify.com/v1/me/player/currently-playing"
RECENTLY_PLAYED_URL = "https://api.spotify.com/v1/me/player/recently-played"

SCOPES = "user-read-currently-playing user-read-playback-state user-read-recently-played"

# In-memory cache only -- access tokens live ~1h, so there's no reason to
# touch the database for them at all. Cleared on process restart, which is
# harmless: the refresh_token stored in the DB rebuilds it on demand.
_access_token: str | None = None
_access_token_expires_at: float = 0.0


def build_authorize_url(state: str) -> str:
    """The URL /spotify/login redirects the admin's browser to."""
    params = {
        "client_id": settings.spotify_client_id,
        "response_type": "code",
        "redirect_uri": settings.spotify_redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _basic_auth_header() -> dict:
    raw = f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
    return {"Authorization": f"Basic {base64.b64encode(raw).decode()}"}


async def exchange_code_for_tokens(code: str) -> str:
    """
    One-time exchange right after the admin approves access on Spotify's
    consent screen. Returns the refresh_token -- the only thing we persist.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.spotify_redirect_uri,
            },
            headers=_basic_auth_header(),
        )
        resp.raise_for_status()
        payload = resp.json()

    global _access_token, _access_token_expires_at
    _access_token = payload["access_token"]
    _access_token_expires_at = time.time() + payload.get("expires_in", 3600) - 30

    return payload["refresh_token"]


async def _refresh_access_token(db) -> str | None:
    """Uses the stored refresh_token to mint a new access_token."""
    refresh_token = await crud.get_spotify_refresh_token(db)
    if not refresh_token:
        print("[spotify] no refresh_token stored in DB -- /spotify/login was never completed")
        return None

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers=_basic_auth_header(),
        )
        if resp.status_code != 200:
            # TEMPORARY: prints Spotify's exact error so we can see the real
            # cause in Render's logs. Remove once this is working.
            print(f"[spotify] refresh_token exchange failed: {resp.status_code} {resp.text}")
            return None
        payload = resp.json()

    global _access_token, _access_token_expires_at
    _access_token = payload["access_token"]
    _access_token_expires_at = time.time() + payload.get("expires_in", 3600) - 30

    # Spotify occasionally rotates the refresh_token when you use it -- if
    # it sends a new one back, it MUST be saved, or the old one stops
    # working the next time we need to refresh.
    if payload.get("refresh_token"):
        await crud.save_spotify_refresh_token(db, payload["refresh_token"])

    return _access_token


async def _get_valid_access_token(db) -> str | None:
    if _access_token and time.time() < _access_token_expires_at:
        return _access_token
    return await _refresh_access_token(db)


async def get_now_playing(db) -> dict | None:
    """
    Returns a small dict describing what's playing, or None if Spotify
    isn't connected at all. If nothing is playing right this second, falls
    back to the most recently played track (flagged live=False) so the
    widget shows *something* useful instead of just going blank.
    """
    token = await _get_valid_access_token(db)
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(NOW_PLAYING_URL, headers=headers)
        print(f"[spotify] currently-playing -> {resp.status_code}")

        if resp.status_code == 200 and resp.content:
            data = resp.json()
            item = data.get("item")
            if item and data.get("is_playing"):
                return _format_track(item, live=True, progress_ms=data.get("progress_ms", 0))
        elif resp.status_code not in (200, 204):
            print(f"[spotify] currently-playing error body: {resp.text}")

        # Nothing playing right now (204 No Content, or is_playing=False) --
        # show the last played track instead of an empty widget.
        resp = await client.get(RECENTLY_PLAYED_URL, headers=headers, params={"limit": 1})
        print(f"[spotify] recently-played -> {resp.status_code}")
        if resp.status_code == 200:
            items = resp.json().get("items", [])
            if items:
                return _format_track(items[0]["track"], live=False, progress_ms=0)
        else:
            print(f"[spotify] recently-played error body: {resp.text}")

    return None


def _format_track(item: dict, live: bool, progress_ms: int) -> dict:
    images = item.get("album", {}).get("images", [])
    artwork_url = images[0]["url"] if images else None

    return {
        "connected": True,
        "live": live,
        "title": item.get("name"),
        "artists": ", ".join(a["name"] for a in item.get("artists", [])),
        "album_art": artwork_url,
        "track_url": item.get("external_urls", {}).get("spotify"),
        "progress_ms": progress_ms,
        "duration_ms": item.get("duration_ms", 0),
    }
