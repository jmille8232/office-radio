"""In-memory Spotify fakes. No network, no real accounts."""
from __future__ import annotations

ME = "svc_account"


def track(tid, name=None, explicit=False, playable=None):
    t = {"type": "track", "id": tid, "uri": f"spotify:track:{tid}", "name": name or tid,
         "artists": [{"name": "Artist"}], "album": {"name": "Album"}, "explicit": explicit,
         "external_urls": {"spotify": f"https://open.spotify.com/track/{tid}"}}
    if playable is not None:
        t["is_playable"] = playable
    return t


def entry(obj, added_by="coworker", is_local=False, key="item"):
    return {"added_at": "2026-11-13T12:00:00Z", "added_by": {"id": added_by},
            "is_local": is_local, key: obj}


def playlist(pid, name, owner=ME, collaborative=False):
    return {"id": pid, "name": name, "owner": {"id": owner, "display_name": owner},
            "collaborative": collaborative, "public": False if owner == ME else None,
            "external_urls": {"spotify": f"https://open.spotify.com/playlist/{pid}"},
            "description": "", "snapshot_id": "s0"}


class FakeSpotify:
    def __init__(self, playlists=None, items=None):
        self.playlists = list(playlists or [])
        self.items = {k: list(v) for k, v in (items or {}).items()}
        self.created, self.add_calls, self.remove_calls = [], [], []
        self.fail_add_on_call = None
        self.item_errors: dict[str, Exception] = {}

    def current_user_id(self):
        return ME

    def list_my_playlists(self):
        yield from self.playlists

    def get_playlist_items(self, pid):
        if pid in self.item_errors:
            raise self.item_errors[pid]
        yield from self.items.get(pid, [])

    def create_playlist(self, name, description, public, collaborative):
        pid = f"pl_created_{len(self.created)}"
        pl = {**playlist(pid, name, collaborative=collaborative), "public": public}
        self.created.append(pl)
        self.playlists.append(pl)
        return pl

    def add_items(self, pid, uris):
        assert 0 < len(uris) <= 100
        if self.fail_add_on_call is not None and len(self.add_calls) == self.fail_add_on_call:
            raise RuntimeError("spotify 500")
        self.add_calls.append((pid, list(uris)))
        self.items.setdefault(pid, []).extend(entry(track(u.rsplit(":", 1)[1]), ME) for u in uris)
        return f"snap{len(self.add_calls)}"

    def remove_items(self, pid, uris, snapshot_id=None):
        self.remove_calls.append((pid, list(uris)))
        gone = set(uris)
        self.items[pid] = [e for e in self.items.get(pid, [])
                           if (e.get("item") or {}).get("uri") not in gone]
        return f"rsnap{len(self.remove_calls)}"


class Resp:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = "" if body is None else "x"

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeTransport:
    """Scripted HTTP: pass a list of Resp objects returned in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
