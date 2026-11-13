"""Data layer: the Phase 8 datasets as SQLite tables.

All other modules talk to this class only, so it can be replaced with a
Chalk-backed implementation once the sandbox's Chalk conventions are confirmed.
Keep the database on a persistent path (e.g. a Chalk Volume mount).

Tables
  office_playlists              discovered source playlists
  candidate_tracks              normalized, deduplicated track catalog
  playlist_track_relationships  which source playlist holds which track
  active_playlist               current Office Radio contents
  playlist_sync_history         job runs and results
  playlist_change_log           proposed / approved / applied changes (+ audit events)
  admin_settings                configuration and operational controls
  api_errors                    failed Spotify API requests
  feedback_events               reserved for a future, separately approved feedback system
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS office_playlists (
    playlist_id      TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    owner_id         TEXT,
    owner_name       TEXT,
    url              TEXT,
    description      TEXT,
    is_public        INTEGER,
    collaborative    INTEGER NOT NULL DEFAULT 0,
    items_accessible INTEGER NOT NULL DEFAULT 0,
    is_inbox         INTEGER NOT NULL DEFAULT 0,
    snapshot_id      TEXT,
    still_matching   INTEGER NOT NULL DEFAULT 1,
    first_discovered_at TEXT NOT NULL,
    last_synced_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_tracks (
    track_id         TEXT PRIMARY KEY,
    track_uri        TEXT NOT NULL,
    name             TEXT,
    artist_names     TEXT,          -- JSON list
    album_name       TEXT,
    explicit         INTEGER NOT NULL DEFAULT 0,
    spotify_url      TEXT,
    is_playable      INTEGER,       -- NULL when Spotify did not say
    first_discovered_at TEXT NOT NULL,
    last_synced_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_track_relationships (
    playlist_id   TEXT NOT NULL REFERENCES office_playlists(playlist_id),
    track_id      TEXT NOT NULL REFERENCES candidate_tracks(track_id),
    added_at      TEXT,
    added_by      TEXT,
    present       INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (playlist_id, track_id)
);
CREATE TABLE IF NOT EXISTS active_playlist (
    track_id   TEXT PRIMARY KEY,
    track_uri  TEXT NOT NULL,
    position   INTEGER,
    is_playable INTEGER,
    synced_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_sync_history (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,     -- running | success | failed
    attempts    INTEGER NOT NULL DEFAULT 1,
    details     TEXT,              -- JSON
    error       TEXT
);
CREATE TABLE IF NOT EXISTS playlist_change_log (
    change_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    action       TEXT NOT NULL,    -- add | remove
    track_id     TEXT NOT NULL,
    track_uri    TEXT NOT NULL,
    source       TEXT NOT NULL,    -- inbox | catalog | rollback
    source_playlist_id TEXT,
    status       TEXT NOT NULL,    -- pending | approved | rejected | applied | rolled_back
    reverts_change_id INTEGER,
    snapshot_id  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    events       TEXT NOT NULL     -- JSON audit trail
);
CREATE INDEX IF NOT EXISTS idx_change_track ON playlist_change_log(action, track_id);
CREATE TABLE IF NOT EXISTS admin_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,      -- JSON
    updated_at TEXT NOT NULL,
    updated_by TEXT
);
CREATE TABLE IF NOT EXISTS api_errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at       TEXT NOT NULL,
    method   TEXT,
    path     TEXT,
    status   INTEGER,
    message  TEXT
);
CREATE TABLE IF NOT EXISTS feedback_events (
    event_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id             TEXT NOT NULL,
    feedback_source      TEXT NOT NULL,
    feedback_at          TEXT NOT NULL,
    feedback_type        TEXT NOT NULL,
    reviewer_authorization TEXT NOT NULL,
    review_status        TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:  # commits on success, rolls back on error
            yield self.conn

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, args).fetchone()

    # ---- admin settings ----------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM admin_settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any, by: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO admin_settings (key, value, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at, "
                "updated_by = excluded.updated_by",
                (key, json.dumps(value), utcnow(), by),
            )

    # ---- playlists -----------------------------------------------------------
    def upsert_playlist(self, p: dict) -> None:
        now = utcnow()
        with self.tx() as c:
            c.execute(
                """INSERT INTO office_playlists (playlist_id, name, owner_id, owner_name, url,
                   description, is_public, collaborative, items_accessible, is_inbox,
                   snapshot_id, still_matching, first_discovered_at, last_synced_at)
                   VALUES (:playlist_id, :name, :owner_id, :owner_name, :url, :description,
                   :is_public, :collaborative, :items_accessible, :is_inbox, :snapshot_id, 1,
                   :now, :now)
                   ON CONFLICT(playlist_id) DO UPDATE SET name = excluded.name,
                   owner_id = excluded.owner_id, owner_name = excluded.owner_name,
                   url = excluded.url, description = excluded.description,
                   is_public = excluded.is_public, collaborative = excluded.collaborative,
                   items_accessible = excluded.items_accessible, is_inbox = excluded.is_inbox,
                   snapshot_id = excluded.snapshot_id, still_matching = 1,
                   last_synced_at = excluded.last_synced_at""",
                {**p, "now": now},
            )

    def mark_unmatched_playlists(self, keep_ids: set[str]) -> None:
        """Playlists no longer returned/matching are flagged, never deleted."""
        with self.tx() as c:
            for row in c.execute("SELECT playlist_id FROM office_playlists").fetchall():
                if row["playlist_id"] not in keep_ids:
                    c.execute(
                        "UPDATE office_playlists SET still_matching = 0 WHERE playlist_id = ?",
                        (row["playlist_id"],),
                    )

    def set_playlist_accessible(self, playlist_id: str, accessible: bool) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE office_playlists SET items_accessible = ? WHERE playlist_id = ?",
                (int(accessible), playlist_id),
            )

    def source_playlists(self, accessible_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM office_playlists WHERE still_matching = 1"
        if accessible_only:
            sql += " AND items_accessible = 1"
        return self.q(sql + " ORDER BY name")

    # ---- catalog -------------------------------------------------------------
    def upsert_track(self, t: dict) -> None:
        now = utcnow()
        with self.tx() as c:
            c.execute(
                """INSERT INTO candidate_tracks (track_id, track_uri, name, artist_names,
                   album_name, explicit, spotify_url, is_playable, first_discovered_at,
                   last_synced_at) VALUES (:track_id, :track_uri, :name, :artist_names,
                   :album_name, :explicit, :spotify_url, :is_playable, :now, :now)
                   ON CONFLICT(track_id) DO UPDATE SET track_uri = excluded.track_uri,
                   name = excluded.name, artist_names = excluded.artist_names,
                   album_name = excluded.album_name, explicit = excluded.explicit,
                   spotify_url = excluded.spotify_url, is_playable = excluded.is_playable,
                   last_synced_at = excluded.last_synced_at""",
                {**t, "artist_names": json.dumps(t["artist_names"]), "now": now},
            )

    def replace_relationships(self, playlist_id: str, entries: list[dict]) -> None:
        """Upsert current entries; mark tracks that left the playlist as not present."""
        now = utcnow()
        seen = {e["track_id"] for e in entries}
        with self.tx() as c:
            for e in entries:
                c.execute(
                    """INSERT INTO playlist_track_relationships (playlist_id, track_id,
                       added_at, added_by, present, first_seen_at, last_seen_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(playlist_id, track_id) DO UPDATE SET present = 1,
                       added_at = excluded.added_at, added_by = excluded.added_by,
                       last_seen_at = excluded.last_seen_at""",
                    (playlist_id, e["track_id"], e.get("added_at"), e.get("added_by"), now, now),
                )
            for row in c.execute(
                "SELECT track_id FROM playlist_track_relationships "
                "WHERE playlist_id = ? AND present = 1", (playlist_id,)
            ).fetchall():
                if row["track_id"] not in seen:
                    c.execute(
                        "UPDATE playlist_track_relationships SET present = 0 "
                        "WHERE playlist_id = ? AND track_id = ?",
                        (playlist_id, row["track_id"]),
                    )

    def candidates_with_sources(self) -> list[sqlite3.Row]:
        """Tracks currently present in at least one active, accessible source."""
        return self.q(
            """SELECT t.*, MAX(p.is_inbox) AS from_inbox,
                      MIN(CASE WHEN p.is_inbox = 1 THEN r.playlist_id END) AS inbox_playlist_id,
                      MIN(r.playlist_id) AS any_playlist_id
               FROM candidate_tracks t
               JOIN playlist_track_relationships r ON r.track_id = t.track_id AND r.present = 1
               JOIN office_playlists p ON p.playlist_id = r.playlist_id
                    AND p.still_matching = 1 AND p.items_accessible = 1
               GROUP BY t.track_id ORDER BY MIN(r.first_seen_at), t.track_id"""
        )

    # ---- active playlist -------------------------------------------------------
    def replace_active(self, rows: list[dict]) -> None:
        now = utcnow()
        with self.tx() as c:
            c.execute("DELETE FROM active_playlist")
            for r in rows:
                c.execute(
                    "INSERT OR IGNORE INTO active_playlist VALUES (?, ?, ?, ?, ?)",
                    (r["track_id"], r["track_uri"], r["position"], r.get("is_playable"), now),
                )

    def active_ids(self) -> set[str]:
        return {r["track_id"] for r in self.q("SELECT track_id FROM active_playlist")}

    # ---- change log ------------------------------------------------------------
    def add_change(self, action: str, track_id: str, track_uri: str, source: str,
                   status: str, actor: str, source_playlist_id: str | None = None,
                   reverts_change_id: int | None = None, note: str | None = None) -> int:
        now = utcnow()
        events = [{"at": now, "from": None, "to": status, "by": actor, "note": note}]
        with self.tx() as c:
            cur = c.execute(
                """INSERT INTO playlist_change_log (action, track_id, track_uri, source,
                   source_playlist_id, status, reverts_change_id, created_at, updated_at, events)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (action, track_id, track_uri, source, source_playlist_id, status,
                 reverts_change_id, now, now, json.dumps(events)),
            )
            return int(cur.lastrowid)

    def change(self, change_id: int) -> sqlite3.Row | None:
        return self.one("SELECT * FROM playlist_change_log WHERE change_id = ?", (change_id,))

    def changes(self, status: str | None = None, action: str | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM playlist_change_log WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if action:
            sql += " AND action = ?"
            args.append(action)
        return self.q(sql + " ORDER BY change_id", tuple(args))

    def has_add_change(self, track_id: str) -> bool:
        return self.one(
            "SELECT 1 FROM playlist_change_log WHERE action = 'add' AND track_id = ?",
            (track_id,),
        ) is not None

    def transition(self, change_id: int, new_status: str, actor: str,
                   note: str | None = None, snapshot_id: str | None = None) -> None:
        row = self.change(change_id)
        if row is None:
            raise KeyError(f"No change {change_id}")
        events = json.loads(row["events"])
        now = utcnow()
        events.append({"at": now, "from": row["status"], "to": new_status, "by": actor, "note": note})
        with self.tx() as c:
            c.execute(
                "UPDATE playlist_change_log SET status = ?, updated_at = ?, events = ?, "
                "snapshot_id = COALESCE(?, snapshot_id) WHERE change_id = ?",
                (new_status, now, json.dumps(events), snapshot_id, change_id),
            )

    # ---- job history & errors -------------------------------------------------
    def start_run(self, job: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO playlist_sync_history (job, started_at, status) VALUES (?, ?, 'running')",
                (job, utcnow()),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, attempts: int,
                   details: dict | None = None, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE playlist_sync_history SET finished_at = ?, status = ?, attempts = ?, "
                "details = ?, error = ? WHERE run_id = ?",
                (utcnow(), status, attempts, json.dumps(details or {}), error, run_id),
            )

    def last_success(self, job: str) -> str | None:
        row = self.one(
            "SELECT MAX(finished_at) AS t FROM playlist_sync_history "
            "WHERE job = ? AND status = 'success'", (job,),
        )
        return row["t"] if row else None

    def log_api_error(self, method: str, path: str, status: int | None, message: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO api_errors (at, method, path, status, message) VALUES (?, ?, ?, ?, ?)",
                (utcnow(), method, path, status, message[:500]),
            )
