"""Spotify OAuth 2.0 Authorization Code flow.

  1. `authorize_url()`   -> user signs in and approves the scopes in a browser
  2. `exchange_code()`   -> code is traded for access + refresh tokens (once)
  3. `TokenManager`      -> keeps a valid access token, refreshing when needed

Spotify may return a new refresh token when refreshing; TokenManager saves it
to `token_path` (mode 600) so the newest one is always used. Never logs tokens.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import time
from typing import Callable
from urllib.parse import urlencode

import requests

from ..config import SCOPES

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
REFRESH_MARGIN_SECONDS = 60


class AuthError(RuntimeError):
    pass


def new_state() -> str:
    return secrets.token_urlsafe(24)


def authorize_url(client_id: str, redirect_uri: str, state: str,
                  scopes: list[str] = SCOPES) -> str:
    return AUTHORIZE_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    })


def _basic_auth(client_id: str, client_secret: str) -> str:
    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _token_request(transport, client_id, client_secret, form: dict) -> dict:
    resp = transport(
        "POST", TOKEN_URL,
        headers={"Authorization": _basic_auth(client_id, client_secret),
                 "Content-Type": "application/x-www-form-urlencoded"},
        data=form, timeout=30,
    )
    if resp.status_code != 200:
        # Spotify returns {"error": ..., "error_description": ...}; no tokens in it.
        try:
            body = resp.json()
            msg = f"{body.get('error')}: {body.get('error_description')}"
        except ValueError:
            msg = resp.text[:200]
        raise AuthError(f"Token request failed ({resp.status_code}) {msg}")
    return resp.json()


def exchange_code(code: str, client_id: str, client_secret: str, redirect_uri: str,
                  transport=requests.request) -> dict:
    return _token_request(transport, client_id, client_secret, {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
    })


def save_token_file(path: str, refresh_token: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": refresh_token, "saved_at": time.time()}, f)
    os.replace(tmp, path)


def load_token_file(path: str) -> str | None:
    try:
        with open(path) as f:
            return json.load(f).get("refresh_token")
    except (FileNotFoundError, ValueError):
        return None


class TokenManager:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str | None,
                 token_path: str | None = None, transport=requests.request,
                 clock: Callable[[], float] = time.time) -> None:
        if not client_id or not client_secret:
            raise AuthError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_path = token_path
        # A saved (possibly rotated) token is newer than the one in env/secrets.
        saved = load_token_file(token_path) if token_path else None
        self.refresh_token = saved or refresh_token
        if not self.refresh_token:
            raise AuthError("No refresh token. Run `python -m office_radio.cli login` first.")
        self.transport = transport
        self.clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0

    def access_token(self) -> str:
        if not self._access_token or self.clock() >= self._expires_at - REFRESH_MARGIN_SECONDS:
            self.refresh()
        return self._access_token  # type: ignore[return-value]

    def refresh(self) -> None:
        body = _token_request(self.transport, self.client_id, self.client_secret, {
            "grant_type": "refresh_token", "refresh_token": self.refresh_token,
        })
        self._access_token = body["access_token"]
        self._expires_at = self.clock() + int(body.get("expires_in", 3600))
        rotated = body.get("refresh_token")
        if rotated and rotated != self.refresh_token:
            self.refresh_token = rotated
            if self.token_path:
                save_token_file(self.token_path, rotated)

    def invalidate(self) -> None:
        self._access_token = None
