"""Phase 3 (playlist discovery) and Phase 4 (track catalog). Read-only on Spotify."""
from __future__ import annotations

import logging

from .config import DEFAULT_NAME_PATTERNS, INBOX_NAME
from .normalize import entry_track, name_matches, playlist_row, track_row
from .spotify.client import SpotifyAPIError
from .store import Store

log = logging.getLogger("office_radio.catalog")

SPOTIFY_EDITORIAL_OWNER = "spotify"  # not readable by new apps; never fetched


def discover_playlists(spotify, store: Store) -> dict:
    """Find office playlists among those the account can see (GET /me/playlists).

    This is not a company-wide search: it only sees playlists the authorized
    account owns or follows.
    """
    patterns = store.get_setting("name_patterns", DEFAULT_NAME_PATTERNS)
    master_id = store.get_setting("office_radio_playlist_id")
    me = spotify.current_user_id()
    kept: set[str] = set()
    stats = {"seen": 0, "matched": 0, "accessible": 0, "inbox_found": False,
             "skipped_spotify_owned": 0}

    for pl in spotify.list_my_playlists():
        stats["seen"] += 1
        if not pl or pl.get("id") == master_id:
            continue  # never treat Office Radio itself as a source
        row = playlist_row(pl, me, INBOX_NAME)
        if row["owner_id"] == SPOTIFY_EDITORIAL_OWNER:
            stats["skipped_spotify_owned"] += 1
            continue
        if not (row["is_inbox"] or name_matches(row["name"], patterns)):
            continue
        store.upsert_playlist(row)
        kept.add(row["playlist_id"])
        stats["matched"] += 1
        stats["accessible"] += row["items_accessible"]
        stats["inbox_found"] = stats["inbox_found"] or bool(row["is_inbox"])

    store.mark_unmatched_playlists(kept)
    log.info("Discovery: %s", stats)
    return stats


def sync_sources(spotify, store: Store) -> dict:
    """Refresh the catalog from every accessible source playlist. Idempotent."""
    stats = {"playlists": 0, "entries": 0, "tracks": 0, "skipped_unsupported": 0,
             "now_inaccessible": []}
    for pl in store.source_playlists(accessible_only=True):
        pid = pl["playlist_id"]
        try:
            entries = list(spotify.get_playlist_items(pid))  # fetch fully before writing
        except SpotifyAPIError as exc:
            if exc.status in (403, 404):
                store.set_playlist_accessible(pid, False)
                stats["now_inaccessible"].append(pid)
                continue
            raise
        rels = []
        for e in entries:
            stats["entries"] += 1
            t = entry_track(e)
            if t is None:
                stats["skipped_unsupported"] += 1
                continue
            row = track_row(t)
            store.upsert_track(row)
            rels.append({"track_id": row["track_id"], "added_at": e.get("added_at"),
                         "added_by": (e.get("added_by") or {}).get("id")})
        store.replace_relationships(pid, rels)
        stats["playlists"] += 1
        stats["tracks"] += len({r["track_id"] for r in rels})
    log.info("Source sync: %s", stats)
    return stats
