# mcp-spotify

A Spotify MCP server designed to run as a **remote service** over Streamable HTTP — not a local stdio subprocess.

Most Spotify MCP servers today ([varunneal/spotify-mcp](https://github.com/varunneal/spotify-mcp), [marcelmarais/spotify-mcp-server](https://github.com/marcelmarais/spotify-mcp-server), etc.) launch as a subprocess on the same machine as your AI client. If you have more than one machine — a homelab, a shared dev server, a laptop that moves around — you end up running N copies with N separate OAuth flows.

This server runs **once**, in a Docker container, exposes Spotify tools over Streamable HTTP on a port, and any MCP client on your LAN or Tailscale network connects to it with one URL. OAuth happens once in a bootstrap script; the refresh token lives on the server.

> Previously published as `spotify-mcp-sse`. Renamed to `mcp-spotify` on the migration from the deprecated HTTP+SSE transport to Streamable HTTP (MCP spec 2025-06-18). The old URL `https://github.com/pete-builds/spotify-mcp-sse` redirects to this repo.

## Features

Nine tools, oriented around creating and managing playlists:

| Tool | Purpose |
|---|---|
| `search_artist` | Resolve artist name → Spotify ID for disambiguation |
| `get_artist_top_tracks` | Artist's most popular tracks (works around the deprecated `/top-tracks` endpoint) |
| `create_playlist_from_artists` | Build a new playlist from a list of artist names; shuffle interleaves |
| `add_artists_to_playlist` | Extend an existing playlist; accepts URL, URI, ID, or exact name |
| `create_playlist_from_tracks` | Build a playlist from specific track URIs you already have |
| `list_my_playlists` | List playlists you own or follow |
| `update_playlist` | Rename, edit description, toggle public |
| `delete_playlist` | Remove from your library (Spotify's "unfollow") |
| `remove_tracks_from_playlist` | Remove specific tracks |

## Feb 2026 Spotify API migration — why this matters

Spotify shipped a breaking API migration on Feb 11, 2026. If you built a Spotify integration before then and haven't touched it since, it's broken. If you're starting a new one now, Spotify's dashboard quietly puts you in "Development Mode" with severe restrictions. This repo documents and works around all of it:

- `/artists/{id}/top-tracks` returns 403 for new apps → falls back to track search with `artist:` filter
- Artist search with `limit=1` returns the *wrong* artist (e.g. `Radiohead` → `Thom Yorke`) → always requests ≥2 and prefers exact name match
- Search `limit` caps at 10 (docs still say 50) → clamps internally
- `POST /users/{id}/playlists` removed → uses `POST /me/playlists`
- `POST /playlists/{id}/tracks` → renamed to `/items` (takes `{"uris": [...]}`)
- `DELETE /playlists/{id}/items` takes a *different* shape: `{"items": [{"uri": "..."}]}`
- Write operations require the authenticating user to be explicitly added to the app's User Management tab in the Spotify Developer dashboard (even the app creator)

## Quick start

### 1. Register a Spotify app

https://developer.spotify.com/dashboard → Create app.

- Redirect URI: `http://127.0.0.1:8765/callback` (exact — case-sensitive, no trailing slash)
- API: Web API
- Save **Client ID** and **Client Secret**

In the app's Settings → User Management, add yourself (name + the email on your Spotify account). This is required for any write operations.

### 2. One-time OAuth bootstrap (on your local machine)

```bash
git clone https://github.com/pete-builds/mcp-spotify
cd mcp-spotify
export SPOTIFY_CLIENT_ID=...
export SPOTIFY_CLIENT_SECRET=...
python3 bootstrap.py
```

Your browser opens, you authorize, the terminal prints a long-lived refresh token.

### 3. Deploy the server

Copy the code to whatever host will run the container (typically a homelab box, LAN-accessible). Write `.env`:

```
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REFRESH_TOKEN=...
```

Start it:

```bash
docker compose up -d --build
```

Server is now at `http://<host>:3703/mcp` (Streamable HTTP).

### 4. Register with your MCP client

Claude Code:

```bash
claude mcp add spotify http://<host>:3703/mcp --transport http --scope user
```

Any other MCP client that supports Streamable HTTP: point it at the same URL.

## Configuration

Environment variables (all optional except credentials):

| Var | Default | Purpose |
|---|---|---|
| `SPOTIFY_CLIENT_ID` | — | From Spotify Developer dashboard (required) |
| `SPOTIFY_CLIENT_SECRET` | — | From Spotify Developer dashboard (required) |
| `SPOTIFY_REFRESH_TOKEN` | — | From `bootstrap.py` (required) |
| `MCP_HOST` | `0.0.0.0` | Bind address |
| `MCP_PORT` | `3703` | Listening port |

The included `docker-compose.yml` uses `network_mode: host`. If you'd rather expose with a port mapping, replace that with:

```yaml
    ports:
      - "3703:3703"
```

## OAuth scopes

Bootstrap requests these:

- `playlist-modify-private`
- `playlist-modify-public`
- `playlist-read-private`

If you add tools that need more (e.g. `user-top-read` for "my listening history"), extend `SCOPES` in `bootstrap.py` and re-run it; you'll get a fresh refresh token with the new permissions.

## Architecture

- [FastMCP](https://github.com/jlowin/fastmcp) over Streamable HTTP transport (MCP spec 2025-06-18)
- [httpx](https://www.python-httpx.org/) async client with in-memory access-token cache and automatic refresh on 401 or expiry
- Stdlib-only bootstrap helper (`http.server`, `webbrowser`, `urllib`) — no extra deps for the one-time OAuth dance
- `python:3.13-slim` base image, `fastmcp==3.1.0`, `httpx==0.28.1`

## License

MIT. See [LICENSE](./LICENSE).

## Credits

Built by [Pete Stergion](https://brooksnewmedia.com) as part of Brooks New Media's homelab toolkit. The Feb 2026 API workarounds were hard-won; PRs welcome if you spot a cleaner approach.
