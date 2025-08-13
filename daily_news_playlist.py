"""
Script to update a Spotify playlist with the latest episodes from selected news and sports podcasts.

This script uses the Spotify Web API to search for podcast shows, fetch the latest fresh episodes,
and replace the contents of a target playlist. It is designed to run in an automated environment
like GitHub Actions. To execute it, export the following environment variables:

    SPOTIFY_CLIENT_ID     – Client ID of your Spotify developer app
    SPOTIFY_CLIENT_SECRET – Client secret of your Spotify developer app
    SPOTIFY_REFRESH_TOKEN – Refresh token obtained via the authorization code flow
    SPOTIFY_USER_ID       – Your Spotify user ID (the account that owns the playlist)

If the playlist does not exist, the script will create it. The playlist will be private by default.

Dependencies: requests

"""

import base64
import datetime as dt
import os
import time
from typing import List, Optional, Set

import requests

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"

# Playlist configuration
PLAYLIST_NAME = "Notícias do Dia (Auto)"
PLAYLIST_DESCRIPTION = (
    "Atualizada automaticamente com notícias gerais e de esportes (futebol) "
    "das últimas 24–36h."
)

# Shows to pull episodes from
NEWS_SHOW_QUERIES = [
    "the news ☕️",        # Resumo de notícias gerais
    "Resumão Diário",     # Notícias gerais
    "Café da Manhã",      # Folha de S.Paulo
    "Ao Ponto",           # O Globo
]

SPORTS_SHOW_QUERIES = [
    "Resumo do dIA ge",   # Resumo esportivo do Globo Esporte
    "CBN Esportes",       # CBN
]

# Time window for “fresh” episodes (in hours)
FRESH_HOURS = 36

# Market to search in
MARKET = "BR"


def env(name: str) -> str:
    """Retrieve environment variable or exit with an error."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable: {name}")
    return value


def get_access_token() -> str:
    """Obtain an access token using a refresh token."""
    client_id = env("SPOTIFY_CLIENT_ID")
    client_secret = env("SPOTIFY_CLIENT_SECRET")
    refresh_token = env("SPOTIFY_REFRESH_TOKEN")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {"Authorization": f"Basic {basic}"}
    resp = requests.post(SPOTIFY_TOKEN_URL, data=data, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.json()["access_token"]


def api_get(path: str, token: str, params: Optional[dict] = None) -> dict:
    resp = requests.get(
        f"{SPOTIFY_API}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def api_post(path: str, token: str, json_data: Optional[dict] = None) -> dict:
    resp = requests.post(
        f"{SPOTIFY_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=json_data,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json() if resp.text else {}


def api_put(path: str, token: str, json_data: Optional[dict] = None) -> dict:
    resp = requests.put(
        f"{SPOTIFY_API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=json_data,
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        resp.raise_for_status()
    return resp.json() if resp.text else {}


def get_or_create_playlist(token: str, user_id: str, name: str, description: str, public: bool = False) -> str:
    """
    Look for a playlist with the given name in the user's library and return its ID.
    If not found, create a new playlist and return its ID.
    """
    # Paginate through user's playlists (50 per page)
    limit, offset = 50, 0
    while True:
        page = api_get("/me/playlists", token, params={"limit": limit, "offset": offset})
        for playlist in page.get("items", []):
            if playlist.get("name") == name:
                return playlist["id"]
        if page.get("next"):
            offset += limit
        else:
            break

    # Not found; create a new playlist
    payload = {
        "name": name,
        "public": public,
        "description": description,
    }
    resp = api_post(f"/users/{user_id}/playlists", token, json_data=payload)
    return resp["id"]


def search_show_id(token: str, query: str) -> Optional[str]:
    """Search for a podcast show by name and return its ID."""
    params = {"q": query, "type": "show", "market": MARKET, "limit": 1}
    data = api_get("/search", token, params)
    items = data.get("shows", {}).get("items", [])
    return items[0]["id"] if items else None


def get_latest_fresh_episode_uri(token: str, show_id: str, fresh_hours: int) -> Optional[str]:
    """Return the URI of the freshest episode within the last `fresh_hours` hours."""
    params = {"market": MARKET, "limit": 3}
    data = api_get(f"/shows/{show_id}/episodes", token, params)
    items = data.get("items", [])
    if not items:
        return None

    now = dt.datetime.utcnow().replace(tzinfo=dt.timezone.utc)
    for episode in items:
        release_date = episode.get("release_date")
        if not release_date:
            continue
        try:
            release_dt = dt.datetime.strptime(release_date, "%Y-%m-%d").replace(
                tzinfo=dt.timezone.utc, hour=12
            )
        except ValueError:
            continue
        hours_since = (now - release_dt).total_seconds() / 3600
        if hours_since <= fresh_hours:
            return episode["uri"]
    # If none within the window, return the most recent
    return items[0]["uri"]


def build_episode_list(token: str) -> List[str]:
    """Build a list of episode URIs from the configured shows."""
    episode_uris: List[str] = []
    seen: Set[str] = set()

    def add_latest(show_query: str) -> None:
        show_id = search_show_id(token, show_query)
        if not show_id:
            return
        uri = get_latest_fresh_episode_uri(token, show_id, FRESH_HOURS)
        if uri and uri not in seen:
            seen.add(uri)
            episode_uris.append(uri)
            time.sleep(0.25)  # Avoid hitting API rate limits

    for query in NEWS_SHOW_QUERIES + SPORTS_SHOW_QUERIES:
        add_latest(query)

    return episode_uris[:20]


def replace_playlist_items(token: str, playlist_id: str, uris: List[str]) -> None:
    """Replace all items in the playlist with the given URIs."""
    api_put(f"/playlists/{playlist_id}/tracks", token, json_data={"uris": uris})


def main() -> None:
    token = get_access_token()
    user_id = env("SPOTIFY_USER_ID")
    playlist_id = get_or_create_playlist(
        token, user_id, PLAYLIST_NAME, PLAYLIST_DESCRIPTION, public=False
    )
    episode_uris = build_episode_list(token)
    if episode_uris:
        replace_playlist_items(token, playlist_id, episode_uris)
        print(f"Playlist atualizada com {len(episode_uris)} episódios.")
    else:
        print("Nenhum episódio fresco encontrado; playlist não atualizada.")


if __name__ == "__main__":
    main()
