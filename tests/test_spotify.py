"""Phase 2: OAuth, token refresh, rate limits, pagination, error handling."""
import os
import stat
from urllib.parse import parse_qs, urlparse

import pytest
import requests

from office_radio.spotify.auth import (AuthError, TokenManager, authorize_url,
                                       exchange_code, load_token_file)
from office_radio.spotify.client import SpotifyAPIError, SpotifyClient
from tests.fakes import FakeTransport, Resp


def tok(access="AT", expires=3600, refresh=None):
    b = {"access_token": access, "expires_in": expires, "token_type": "Bearer"}
    if refresh:
        b["refresh_token"] = refresh
    return Resp(200, b)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---- OAuth -------------------------------------------------------------------

def test_authorize_url_has_minimum_scopes_and_state():
    qs = parse_qs(urlparse(authorize_url("cid", "http://127.0.0.1:8888/callback", "st8")).query)
    assert qs["response_type"] == ["code"] and qs["state"] == ["st8"]
    assert set(qs["scope"][0].split()) == {
        "playlist-read-private", "playlist-read-collaborative",
        "playlist-modify-private", "playlist-modify-public"}


def test_exchange_code_uses_basic_auth_and_form():
    t = FakeTransport([Resp(200, {"access_token": "a", "refresh_token": "r", "expires_in": 3600})])
    assert exchange_code("CODE", "cid", "sec", "http://127.0.0.1:8888/callback", t)["refresh_token"] == "r"
    _, url, kw = t.calls[0]
    assert url.endswith("/api/token") and kw["headers"]["Authorization"].startswith("Basic ")
    assert kw["data"]["grant_type"] == "authorization_code"


def test_token_error_message_has_no_secrets():
    t = FakeTransport([Resp(400, {"error": "invalid_grant", "error_description": "Invalid refresh token"})])
    tm = TokenManager("cid", "sec", "REFRESH_SECRET", transport=t)
    with pytest.raises(AuthError) as e:
        tm.access_token()
    assert "invalid_grant" in str(e.value) and "REFRESH_SECRET" not in str(e.value)


def test_access_token_cached_until_near_expiry():
    clock = Clock()
    t = FakeTransport([tok("A1"), tok("A2")])
    tm = TokenManager("cid", "sec", "R", transport=t, clock=clock)
    assert tm.access_token() == "A1"
    clock.t += 3000
    assert tm.access_token() == "A1" and len(t.calls) == 1
    clock.t += 560  # within 60s of expiry
    assert tm.access_token() == "A2" and len(t.calls) == 2


def test_rotated_refresh_token_is_saved_and_preferred(tmp_path):
    path = str(tmp_path / "tok.json")
    tm = TokenManager("cid", "sec", "R_OLD", path, transport=FakeTransport([tok(refresh="R_NEW")]))
    tm.access_token()
    assert load_token_file(path) == "R_NEW"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert TokenManager("cid", "sec", "R_OLD", path, transport=None).refresh_token == "R_NEW"


def test_missing_credentials_raise():
    with pytest.raises(AuthError):
        TokenManager("", "sec", "R")
    with pytest.raises(AuthError):
        TokenManager("cid", "sec", None)


# ---- client --------------------------------------------------------------------

def client(responses, **kw):
    tm = TokenManager("cid", "sec", "R", transport=FakeTransport([tok()] * 5))
    t = FakeTransport(responses)
    sleeps = []
    errors = []
    c = SpotifyClient(tm, transport=t, sleep=sleeps.append,
                      on_error=lambda *a: errors.append(a), **kw)
    return c, t, sleeps, errors


def test_pagination_follows_next():
    c, t, _, _ = client([
        Resp(200, {"items": [{"id": 1}, {"id": 2}], "next": "https://api.spotify.com/v1/me/playlists?offset=2"}),
        Resp(200, {"items": [{"id": 3}], "next": None}),
    ])
    assert [p["id"] for p in c.list_my_playlists()] == [1, 2, 3]
    assert t.calls[0][2]["params"]["limit"] == 50


def test_playlist_items_uses_items_endpoint():
    c, t, _, _ = client([Resp(200, {"items": [], "next": None})])
    list(c.get_playlist_items("abc"))
    assert t.calls[0][1] == "https://api.spotify.com/v1/playlists/abc/items"


def test_rate_limit_honors_retry_after():
    c, _, sleeps, _ = client([Resp(429, {"error": {"message": "slow"}}, {"Retry-After": "7"}),
                              Resp(200, {"id": "me"})])
    assert c.current_user_id() == "me" and sleeps == [7.0]


def test_server_error_retries_with_backoff():
    c, _, sleeps, _ = client([Resp(503, None), Resp(502, None), Resp(200, {"id": "me"})])
    assert c.current_user_id() == "me" and sleeps == [2, 4]


def test_network_error_retries():
    c, _, sleeps, _ = client([requests.ConnectionError("down"), Resp(200, {"id": "me"})])
    assert c.current_user_id() == "me" and sleeps == [2]


def test_401_refreshes_token_once():
    c, _, _, _ = client([Resp(401, {"error": {"message": "expired"}}), Resp(200, {"id": "me"})])
    assert c.current_user_id() == "me"


def test_client_error_raises_and_is_recorded():
    c, _, _, errors = client([Resp(403, {"error": {"status": 403, "message": "Forbidden"}})])
    with pytest.raises(SpotifyAPIError) as e:
        list(c.get_playlist_items("x"))
    assert e.value.status == 403 and errors[0][2] == 403


def test_gives_up_after_max_retries():
    c, _, _, errors = client([Resp(500, None)] * 3, max_retries=2)
    with pytest.raises(SpotifyAPIError):
        c.current_user_id()
    assert errors


def test_add_items_batch_limit_and_body():
    c, t, _, _ = client([Resp(201, {"snapshot_id": "S1"})])
    assert c.add_items("pl", ["spotify:track:a"]) == "S1"
    assert t.calls[0][0] == "POST" and t.calls[0][2]["json"] == {"uris": ["spotify:track:a"]}
    with pytest.raises(ValueError):
        c.add_items("pl", ["u"] * 101)


def test_remove_items_uses_items_key_not_tracks():
    c, t, _, _ = client([Resp(200, {"snapshot_id": "S2"})])
    c.remove_items("pl", ["spotify:track:a"])
    body = t.calls[0][2]["json"]
    assert body == {"items": [{"uri": "spotify:track:a"}]} and "tracks" not in body


def test_create_playlist_body_and_collaborative_must_be_private():
    c, t, _, _ = client([Resp(201, {"id": "new"})])
    assert c.create_playlist("N", "D", public=False, collaborative=True)["id"] == "new"
    assert t.calls[0][1].endswith("/me/playlists")
    with pytest.raises(ValueError):
        c.create_playlist("N", "D", public=True, collaborative=True)
