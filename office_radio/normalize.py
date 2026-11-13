"""Pure helpers: turn Spotify payloads into catalog rows; match playlist names."""
from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_name(s: str) -> str:
    """'Office—Vibes!!' -> 'office vibes'"""
    return _NON_ALNUM.sub(" ", (s or "").casefold()).strip()


def name_matches(name: str, patterns: list[str]) -> bool:
    n = f" {normalize_name(name)} "
    return any(f" {normalize_name(p)} " in n for p in patterns if normalize_name(p))


def entry_track(entry: dict) -> dict | None:
    """Track object from a playlist-item entry, or None for unsupported items.

    Skips local files, episodes and other types, and removed/unavailable
    entries that come back with a null item. Reads "item" (Feb 2026 shape)
    and falls back to the legacy "track" key.
    """
    if not entry or entry.get("is_local"):
        return None
    obj = entry.get("item") or entry.get("track")
    if not obj or obj.get("type") != "track" or not obj.get("id"):
        return None
    return obj


def track_row(track: dict) -> dict:
    tid = track["id"]
    playable = track.get("is_playable")
    return {
        "track_id": tid,
        "track_uri": track.get("uri") or f"spotify:track:{tid}",
        "name": track.get("name"),
        "artist_names": [a.get("name", "") for a in track.get("artists") or []],
        "album_name": (track.get("album") or {}).get("name"),
        "explicit": int(bool(track.get("explicit"))),
        "spotify_url": (track.get("external_urls") or {}).get("spotify"),
        "is_playable": None if playable is None else int(bool(playable)),
    }


def playlist_row(pl: dict, me: str, inbox_name: str) -> dict:
    owner = pl.get("owner") or {}
    owner_id = owner.get("id")
    collaborative = bool(pl.get("collaborative"))
    return {
        "playlist_id": pl["id"],
        "name": pl.get("name") or "",
        "owner_id": owner_id,
        "owner_name": owner.get("display_name"),
        "url": (pl.get("external_urls") or {}).get("spotify"),
        "description": pl.get("description"),
        # Spotify omits `public` for playlists the caller doesn't own.
        "is_public": None if pl.get("public") is None else int(bool(pl["public"])),
        "collaborative": int(collaborative),
        # Since Feb 2026, items are returned only for owned/collaborative playlists.
        "items_accessible": int(owner_id == me or collaborative),
        "is_inbox": int(owner_id == me and normalize_name(pl.get("name", ""))
                        == normalize_name(inbox_name)),
        "snapshot_id": pl.get("snapshot_id"),
    }
