"""Spotify Web API client with OAuth 2.0 refresh-token flow."""

import base64
import re
import time

import httpx


SPOTIFY_API_BASE = "https://api.spotify.com/v1"
SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"


class SpotifyError(Exception):
    """Raised when the Spotify API returns an error."""


class SpotifyClient:
    """Async Spotify client. Manages access-token refresh transparently."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._user_id: str | None = None
        self._client = httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            headers={"User-Agent": "mcp-spotify/1.0"},
        )

    async def close(self):
        await self._client.aclose()

    # ---- token management ----

    def _basic_auth(self) -> str:
        raw = f"{self._client_id}:{self._client_secret}".encode()
        return base64.b64encode(raw).decode()

    async def _refresh_access_token(self):
        resp = await self._client.post(
            SPOTIFY_TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._refresh_token},
            headers={"Authorization": f"Basic {self._basic_auth()}"},
        )
        if resp.status_code != 200:
            raise SpotifyError(f"Token refresh failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        # Spotify may rotate the refresh token; use the new one if returned
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]

    async def _ensure_token(self):
        if self._access_token and time.time() < self._expires_at - 60:
            return
        await self._refresh_access_token()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        await self._ensure_token()
        url = f"{SPOTIFY_API_BASE}{path}"
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._access_token}"}
            resp = await self._client.request(
                method, url, params=params, json=json_body, headers=headers
            )
            if resp.status_code == 401 and attempt == 0:
                # Token may have been revoked or expired early — force refresh and retry
                self._access_token = None
                await self._ensure_token()
                continue
            if resp.status_code >= 400:
                raise SpotifyError(
                    f"Spotify API error ({resp.status_code}) on {method} {path}: {resp.text}"
                )
            if not resp.content:
                return {}
            return resp.json()
        raise SpotifyError(f"Request failed after retry: {method} {path}")

    # ---- public API ----

    async def search_artists(self, query: str, limit: int = 5) -> list[dict]:
        """Return matching artists. Note: new-dev-mode apps cap search at 10
        results, and `popularity`/`genres`/`followers` may be absent on
        individual items."""
        # Always request >=2 — with limit=1 Spotify sometimes returns a related
        # artist (e.g. "Radiohead" → "Thom Yorke") instead of the exact match.
        requested = max(2, min(limit, 10))
        data = await self._request(
            "GET",
            "/search",
            params={"q": query, "type": "artist", "limit": requested},
        )
        items = data.get("artists", {}).get("items", [])
        results = [
            {
                "id": a["id"],
                "name": a["name"],
                "popularity": a.get("popularity"),
                "genres": a.get("genres", []),
                "followers": a.get("followers", {}).get("total") if isinstance(a.get("followers"), dict) else None,
                "url": a.get("external_urls", {}).get("spotify"),
            }
            for a in items
        ]
        # Prefer an exact (case-insensitive) name match if one exists
        q_lower = query.strip().lower()
        exact = [r for r in results if r["name"].lower() == q_lower]
        if exact:
            other = [r for r in results if r["name"].lower() != q_lower]
            results = exact + other
        return results[: max(1, min(limit, 10))]

    async def get_top_tracks_for_artist(
        self, artist_name: str, limit: int = 5, market: str = "US"
    ) -> list[dict]:
        """Return the artist's most popular tracks available in `market`.

        Works around two Spotify Development Mode restrictions:
          * `/artists/{id}/top-tracks` returns 403 for new dev apps.
          * `/search` caps limit at 10 and strips the `popularity` field.

        Strategy: resolve artist to a canonical ID, then run a track-search with
        `artist:"NAME"` filter, keep only tracks actually credited to that ID,
        and rely on Spotify's relevance ordering (≈ popularity for this query
        shape) because we can no longer see `popularity` directly.
        """
        matches = await self.search_artists(artist_name, limit=5)
        if not matches:
            return []
        artist_id = matches[0]["id"]
        canonical = matches[0]["name"]

        data = await self._request(
            "GET",
            "/search",
            params={
                "q": f'artist:"{canonical}"',
                "type": "track",
                "limit": 10,  # dev-mode cap
                "market": market,
            },
        )
        items = data.get("tracks", {}).get("items", [])
        filtered = [
            t for t in items
            if any(a.get("id") == artist_id for a in t.get("artists", []))
        ]
        # De-duplicate by (track name, primary artist) to suppress multiple
        # album / compilation copies of the same recording.
        seen: set[tuple[str, str]] = set()
        deduped: list[dict] = []
        for t in filtered:
            key = (t["name"].lower(), t["artists"][0]["id"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(t)
        return [
            {
                "id": t["id"],
                "name": t["name"],
                "uri": t["uri"],
                "album": t.get("album", {}).get("name"),
                "artists": [ar["name"] for ar in t.get("artists", [])],
                "url": t.get("external_urls", {}).get("spotify"),
            }
            for t in deduped[: max(1, min(limit, 10))]
        ]

    async def get_current_user_id(self) -> str:
        """Return the authenticated user's Spotify user ID (cached)."""
        if self._user_id:
            return self._user_id
        data = await self._request("GET", "/me")
        self._user_id = data["id"]
        return self._user_id

    async def create_playlist(
        self, name: str, public: bool = False, description: str = ""
    ) -> dict:
        """Create a new playlist on the authenticated user's account.

        Uses POST /me/playlists per the Feb 2026 API migration — the older
        POST /users/{user_id}/playlists was removed.
        """
        body: dict = {"name": name, "public": public}
        if description:
            body["description"] = description
        data = await self._request("POST", "/me/playlists", json_body=body)
        return {
            "id": data["id"],
            "url": data.get("external_urls", {}).get("spotify"),
            "name": data["name"],
        }

    async def add_tracks(self, playlist_id: str, uris: list[str]) -> int:
        """Add tracks to a playlist. Spotify caps at 100 URIs per request.

        Uses POST /playlists/{id}/items per the Feb 2026 migration (renamed
        from /tracks).
        """
        added = 0
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            await self._request(
                "POST", f"/playlists/{playlist_id}/items", json_body={"uris": chunk}
            )
            added += len(chunk)
        return added

    async def remove_tracks(self, playlist_id: str, uris: list[str]) -> int:
        """Remove tracks from a playlist. Chunks at 100 URIs (Spotify limit).

        Post-Feb-2026 DELETE body shape is `{"items": [{"uri": "..."}]}` — the
        old `tracks` key was renamed to `items`, but the object wrapper stays.
        Bare URI strings return 400 "Invalid base62 id"; bare `uris` key
        returns 400 "No uris provided".
        """
        removed = 0
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            await self._request(
                "DELETE",
                f"/playlists/{playlist_id}/items",
                json_body={"items": [{"uri": u} for u in chunk]},
            )
            removed += len(chunk)
        return removed

    async def update_playlist(
        self,
        playlist_id: str,
        name: str | None = None,
        description: str | None = None,
        public: bool | None = None,
    ) -> None:
        """Rename a playlist, change its description, or toggle visibility."""
        body: dict = {}
        if name is not None:
            body["name"] = name
        if description is not None:
            body["description"] = description
        if public is not None:
            body["public"] = public
        if not body:
            return
        await self._request("PUT", f"/playlists/{playlist_id}", json_body=body)

    async def unfollow_playlist(self, playlist_id: str) -> None:
        """Spotify's equivalent of "delete a playlist" — removes it from the
        user's library by unfollowing. The playlist object itself persists on
        Spotify's side but drops out of the user's view."""
        await self._request("DELETE", f"/playlists/{playlist_id}/followers")

    async def get_my_playlists(self, limit: int = 50) -> list[dict]:
        """Return the authenticated user's playlists (owned + followed).

        Requires `playlist-read-private` scope.
        """
        results: list[dict] = []
        offset = 0
        page_size = max(1, min(limit, 50))
        while len(results) < limit:
            data = await self._request(
                "GET",
                "/me/playlists",
                params={"limit": min(page_size, limit - len(results)), "offset": offset},
            )
            items = data.get("items", [])
            if not items:
                break
            for p in items:
                owner = p.get("owner", {}) or {}
                results.append(
                    {
                        "id": p["id"],
                        "name": p["name"],
                        "url": p.get("external_urls", {}).get("spotify"),
                        "track_count": p.get("tracks", {}).get("total"),
                        "public": p.get("public"),
                        "owner_id": owner.get("id"),
                        "owner_name": owner.get("display_name"),
                    }
                )
            if not data.get("next"):
                break
            offset += len(items)
        return results

    # ---- parsing helpers ----

    @staticmethod
    def parse_track_ref(ref: str) -> str:
        """Accept a Spotify track URI, open.spotify.com URL, or bare 22-char
        track ID; return a normalized `spotify:track:<id>` URI."""
        s = ref.strip()
        if s.startswith("spotify:track:"):
            return s
        m = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]+)", s)
        if m:
            return f"spotify:track:{m.group(1)}"
        if re.fullmatch(r"[A-Za-z0-9]{22}", s):
            return f"spotify:track:{s}"
        raise ValueError(f"Not a recognizable Spotify track reference: {ref!r}")

    @staticmethod
    def parse_playlist_id(ref: str) -> str | None:
        """Accept a Spotify playlist URI, URL, or 22-char ID; return the bare
        ID. Returns None if the input doesn't look like any of those (caller
        should then try resolving by name)."""
        s = ref.strip()
        if s.startswith("spotify:playlist:"):
            return s.split(":")[-1]
        m = re.search(r"open\.spotify\.com/playlist/([A-Za-z0-9]+)", s)
        if m:
            return m.group(1)
        if re.fullmatch(r"[A-Za-z0-9]{22}", s):
            return s
        return None

    async def resolve_playlist(self, ref: str) -> dict | None:
        """Resolve a user-supplied playlist reference (URL, URI, ID, or name)
        to a `{id, name, url}` dict. Name resolution does a case-insensitive
        match against the user's own playlists."""
        pid = self.parse_playlist_id(ref)
        if pid:
            data = await self._request("GET", f"/playlists/{pid}", params={"fields": "id,name,external_urls"})
            return {
                "id": data["id"],
                "name": data["name"],
                "url": data.get("external_urls", {}).get("spotify"),
            }
        # Fall back to name match
        lowered = ref.strip().lower()
        mine = await self.get_my_playlists(limit=50)
        for p in mine:
            if p["name"].lower() == lowered:
                return {"id": p["id"], "name": p["name"], "url": p["url"]}
        return None
