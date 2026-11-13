"""Spotify Web API client (official endpoints only, Feb 2026 revision).

  GET    /me                      current_user_id
  GET    /me/playlists            list_my_playlists      (paginated)
  POST   /me/playlists            create_playlist
  GET    /playlists/{id}/items    get_playlist_items     (paginated)
  POST   /playlists/{id}/items    add_items              (<= 100 URIs)
  DELETE /playlists/{id}/items    remove_items           (<= 100 URIs, body key "items")

Handles: expired tokens (one refresh + retry on 401), rate limits (429 with
Retry-After), transient 5xx with exponential backoff, and records failures.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Iterator

import requests

from .auth import TokenManager

log = logging.getLogger("office_radio.spotify")

API_BASE = "https://api.spotify.com/v1"
MAX_BATCH = 100


class SpotifyAPIError(RuntimeError):
    def __init__(self, status: int | None, message: str, path: str = "") -> None:
        super().__init__(f"Spotify API {status} on {path}: {message}")
        self.status = status
        self.path = path


class SpotifyClient:
    def __init__(self, tokens: TokenManager, transport=requests.request,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 5,
                 page_limit: int = 50,
                 on_error: Callable[[str, str, int | None, str], None] | None = None,
                 max_retry_after: float = 120.0) -> None:
        self.tokens = tokens
        self.transport = transport
        self.sleep = sleep
        self.max_retries = max_retries
        self.page_limit = page_limit
        self.on_error = on_error
        self.max_retry_after = max_retry_after
        self._me: str | None = None

    # ---- core request loop ---------------------------------------------------
    def _request(self, method: str, path_or_url: str, params: dict | None = None,
                 json: dict | None = None) -> dict | None:
        url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
        path = url.replace(API_BASE, "")
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.transport(
                    method, url,
                    headers={"Authorization": f"Bearer {self.tokens.access_token()}"},
                    params=params, json=json, timeout=30,
                )
            except requests.RequestException as exc:  # network error
                if attempt > self.max_retries:
                    self._record(method, path, None, str(exc))
                    raise SpotifyAPIError(None, str(exc), path) from exc
                self.sleep(min(2 ** attempt, 60))
                continue

            status = resp.status_code
            if status in (200, 201):
                return resp.json() if resp.text else None
            if status == 204:
                return None
            if status == 401 and not refreshed:
                refreshed = True
                self.tokens.invalidate()
                continue
            if status == 429 and attempt <= self.max_retries:
                wait = float(resp.headers.get("Retry-After", "1") or 1)
                log.warning("Rate limited on %s; waiting %.0fs", path, wait)
                self.sleep(min(wait, self.max_retry_after))
                continue
            if status >= 500 and attempt <= self.max_retries:
                self.sleep(min(2 ** attempt, 60))
                continue

            message = _error_message(resp)
            self._record(method, path, status, message)
            raise SpotifyAPIError(status, message, path)

    def _record(self, method, path, status, message) -> None:
        log.error("Spotify %s %s failed (%s): %s", method, path, status, message)
        if self.on_error:
            self.on_error(method, path, status, message)

    def _paginate(self, path: str, params: dict | None = None) -> Iterator[dict]:
        page = self._request("GET", path, params={"limit": self.page_limit, **(params or {})})
        while page:
            yield from page.get("items") or []
            nxt = page.get("next")
            page = self._request("GET", nxt) if nxt else None

    # ---- endpoints ----------------------------------------------------------------
    def current_user_id(self) -> str:
        if self._me is None:
            self._me = self._request("GET", "/me")["id"]
        return self._me

    def list_my_playlists(self) -> Iterator[dict]:
        return self._paginate("/me/playlists")

    def get_playlist_items(self, playlist_id: str) -> Iterator[dict]:
        # market=from_token makes Spotify report is_playable for the account's market.
        return self._paginate(f"/playlists/{playlist_id}/items", {"market": "from_token"})

    def create_playlist(self, name: str, description: str, public: bool,
                        collaborative: bool) -> dict:
        if collaborative and public:
            raise ValueError("Spotify collaborative playlists must be private")
        return self._request("POST", "/me/playlists", json={
            "name": name, "description": description,
            "public": public, "collaborative": collaborative,
        })

    def add_items(self, playlist_id: str, uris: list[str]) -> str:
        if not 0 < len(uris) <= MAX_BATCH:
            raise ValueError(f"add_items takes 1..{MAX_BATCH} URIs")
        body = self._request("POST", f"/playlists/{playlist_id}/items", json={"uris": uris})
        return body["snapshot_id"]

    def remove_items(self, playlist_id: str, uris: list[str],
                     snapshot_id: str | None = None) -> str:
        if not 0 < len(uris) <= MAX_BATCH:
            raise ValueError(f"remove_items takes 1..{MAX_BATCH} URIs")
        # Since Feb 2026 the body key is "items"; the old "tracks" key silently no-ops.
        payload: dict = {"items": [{"uri": u} for u in uris]}
        if snapshot_id:
            payload["snapshot_id"] = snapshot_id
        body = self._request("DELETE", f"/playlists/{playlist_id}/items", json=payload)
        return body["snapshot_id"]


def _error_message(resp) -> str:
    try:
        err = resp.json().get("error")
        return err.get("message", str(err)) if isinstance(err, dict) else str(err)
    except ValueError:
        return (resp.text or "")[:200]
