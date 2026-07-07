"""
Last.fm helper: shows what's currently playing (or last played).

Spotify feeds Last.fm automatically once "scrobbling" is turned on in the
Spotify app's own Settings -> Apps -> Last.fm. From there, Last.fm's public
`user.getrecenttracks` method tells us the same thing Spotify's own API
would have -- including a live "now playing" flag -- but with just one
API key and one username, no OAuth, no tokens, no database row at all.

This entirely replaces the earlier spotify.py attempt, whose
currently-playing/recently-played endpoints require the app owner to have
an active Spotify Premium subscription.
"""
import httpx

from config import get_settings

settings = get_settings()

RECENT_TRACKS_URL = "https://ws.audioscrobbler.com/2.0/"


async def get_now_playing() -> dict | None:
    """
    Returns a small dict describing what's playing/was last played, or
    None if Last.fm isn't configured (missing API key/username) or the
    request fails for any reason.
    """
    if not settings.lastfm_api_key or not settings.lastfm_username:
        return None

    params = {
        "method": "user.getrecenttracks",
        "user": settings.lastfm_username,
        "api_key": settings.lastfm_api_key,
        "format": "json",
        "limit": 1,
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(RECENT_TRACKS_URL, params=params)
    except httpx.HTTPError as exc:
        print(f"[lastfm] request error: {exc}")
        return None

    if resp.status_code != 200:
        print(f"[lastfm] request failed: {resp.status_code} {resp.text}")
        return None

    data = resp.json()
    tracks = data.get("recenttracks", {}).get("track", [])
    if not tracks:
        return None

    track = tracks[0]
    is_live = track.get("@attr", {}).get("nowplaying") == "true"

    # Last.fm returns up to 4 image sizes (small -> extralarge); take the
    # largest one that actually has a URL.
    album_art = None
    for img in track.get("image", []):
        if img.get("#text"):
            album_art = img["#text"]

    return {
        "connected": True,
        "live": is_live,
        "title": track.get("name"),
        "artists": track.get("artist", {}).get("#text", ""),
        "album_art": album_art,
        "track_url": track.get("url"),
        # Last.fm doesn't expose playback progress -- the widget just
        # hides the progress bar when duration is 0.
        "progress_ms": 0,
        "duration_ms": 0,
    }
