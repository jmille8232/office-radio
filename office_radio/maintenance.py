"""Phase 5 (Office Radio + Inbox playlists) and Phase 6 (maintenance engine).

Write safety:
  - Every Spotify write requires the admin setting `writes_enabled` = true.
  - Creating playlists also requires an explicit confirm flag.
  - Only the Office Radio playlist is ever modified; source playlists never are.
  - Nothing is deleted automatically. Removal happens only through an explicit,
    confirmed rollback of a change this system made.
"""
from __future__ import annotations

import logging

from .config import INBOX_DESCRIPTION, INBOX_NAME, MASTER_DESCRIPTION, MASTER_NAME
from .normalize import entry_track, normalize_name
from .spotify.client import MAX_BATCH
from .store import Store

log = logging.getLogger("office_radio.maintenance")

ALLOWED = {
    "pending": {"approved", "rejected"},
    "approved": {"applied", "rejected"},
    "applied": {"rolled_back"},
    "rejected": set(),
    "rolled_back": set(),
}


class WritesDisabled(RuntimeError):
    pass


def require_writes(store: Store) -> None:
    if not store.get_setting("writes_enabled", False):
        raise WritesDisabled(
            "Spotify writes are disabled. After reviewing a read-only sync and the "
            "proposed changes, run `cli settings enable-writes --confirm`."
        )


# ---- Phase 5: playlists ---------------------------------------------------------

def find_owned_playlist(spotify, name: str) -> str | None:
    me, target = spotify.current_user_id(), normalize_name(name)
    for pl in spotify.list_my_playlists():
        if pl and (pl.get("owner") or {}).get("id") == me and normalize_name(pl.get("name", "")) == target:
            return pl["id"]
    return None


def ensure_master(spotify, store: Store, actor: str, create: bool = False,
                  confirm: bool = False) -> str | None:
    pid = store.get_setting("office_radio_playlist_id")
    if pid:
        return pid
    pid = find_owned_playlist(spotify, MASTER_NAME)  # avoid creating a duplicate
    if not pid and create:
        if not confirm:
            raise ValueError("Pass --confirm to create the Office Radio playlist")
        require_writes(store)
        pid = spotify.create_playlist(MASTER_NAME, MASTER_DESCRIPTION,
                                      public=False, collaborative=False)["id"]
        log.info("Created Office Radio playlist %s", pid)
    if pid:
        store.set_setting("office_radio_playlist_id", pid, actor)
    return pid


def ensure_inbox(spotify, store: Store, actor: str, confirm: bool = False) -> str:
    existing = find_owned_playlist(spotify, INBOX_NAME)
    if existing:
        return existing
    if not confirm:
        raise ValueError("Pass --confirm to create the Inbox playlist")
    require_writes(store)
    # Spotify requires collaborative playlists to be private.
    pid = spotify.create_playlist(INBOX_NAME, INBOX_DESCRIPTION, public=False, collaborative=True)["id"]
    log.info("Created Inbox playlist %s", pid)
    return pid


def sync_active(spotify, store: Store) -> dict:
    """Mirror Office Radio's current contents into active_playlist (read-only)."""
    pid = store.get_setting("office_radio_playlist_id")
    if not pid:
        return {"master": None}
    rows, unavailable, unsupported = [], [], 0
    for pos, e in enumerate(spotify.get_playlist_items(pid)):
        t = entry_track(e)
        if t is None:
            unsupported += 1
            continue
        playable = t.get("is_playable")
        rows.append({"track_id": t["id"], "track_uri": t.get("uri") or f"spotify:track:{t['id']}",
                     "position": pos, "is_playable": None if playable is None else int(bool(playable))})
        if playable is False:
            unavailable.append(t["id"])
    store.replace_active(rows)
    return {"master": pid, "tracks": len(rows), "unavailable": unavailable,
            "unsupported_or_removed": unsupported}


# ---- Phase 6: proposals -----------------------------------------------------------

def generate_proposals(store: Store, actor: str = "reconciliation") -> dict:
    """Propose adding catalog tracks that aren't in Office Radio yet.

    A track is proposed once. Rejected, applied or rolled-back tracks are not
    re-proposed; an admin can re-add them manually in Spotify.
    """
    active = store.active_ids()
    new, in_active, known = [], 0, 0
    for c in store.candidates_with_sources():
        tid = c["track_id"]
        if tid in active:
            in_active += 1
        elif store.has_add_change(tid):
            known += 1
        else:
            source = "inbox" if c["from_inbox"] else "catalog"
            src_pl = c["inbox_playlist_id"] if c["from_inbox"] else c["any_playlist_id"]
            store.add_change("add", tid, c["track_uri"], source, "pending", actor, src_pl)
            new.append(tid)
    return {"new_proposals": len(new), "already_in_office_radio": in_active,
            "previously_proposed": known}


def decide(store: Store, change_ids: list[int], approve: bool, actor: str,
           note: str | None = None) -> list[int]:
    target = "approved" if approve else "rejected"
    done = []
    for cid in change_ids:
        row = store.change(cid)
        if row is None:
            raise KeyError(f"No change {cid}")
        if target not in ALLOWED[row["status"]]:
            raise ValueError(f"Change {cid} is {row['status']}; cannot mark {target}")
        store.transition(cid, target, actor, note)
        done.append(cid)
    return done


def apply_approved(spotify, store: Store, actor: str) -> dict:
    require_writes(store)
    pid = store.get_setting("office_radio_playlist_id")
    if not pid:
        raise RuntimeError("No Office Radio playlist yet. Run `cli create-master --confirm`.")
    approved = store.changes(status="approved", action="add")
    result = {"added": [], "already_present": [], "snapshot_id": None}
    if not approved:
        return result

    live = {t["id"] for t in (entry_track(e) for e in spotify.get_playlist_items(pid)) if t}
    to_add = []
    for ch in approved:
        if ch["track_id"] in live:
            store.transition(ch["change_id"], "applied", actor, "already in Office Radio")
            result["already_present"].append(ch["change_id"])
        else:
            live.add(ch["track_id"])  # guard against two approvals of one track
            to_add.append(ch)

    for i in range(0, len(to_add), MAX_BATCH):
        batch = to_add[i:i + MAX_BATCH]
        snap = spotify.add_items(pid, [c["track_uri"] for c in batch])
        # Marked applied only after Spotify accepts the batch; a failure leaves
        # the remainder approved and safe to retry.
        for c in batch:
            store.transition(c["change_id"], "applied", actor, f"added; snapshot {snap}", snap)
            result["added"].append(c["change_id"])
        result["snapshot_id"] = snap

    sync_active(spotify, store)
    log.info("Applied %d additions", len(result["added"]))
    return result


def rollback(spotify, store: Store, change_ids: list[int], actor: str,
             confirm: bool = False) -> dict:
    """Remove tracks that this system added. Explicit and confirmed only.

    Spotify's API can't restore an old snapshot, so rollback removes the added
    tracks. Note: removing by URI removes every copy of that track in the playlist.
    """
    if not confirm:
        raise ValueError("Rollback removes tracks from Office Radio. Pass --confirm.")
    require_writes(store)
    pid = store.get_setting("office_radio_playlist_id")
    rows = []
    for cid in change_ids:
        row = store.change(cid)
        if row is None or row["action"] != "add" or row["status"] != "applied":
            raise ValueError(f"Change {cid} is not an applied addition")
        rows.append(row)

    removed = []
    for i in range(0, len(rows), MAX_BATCH):
        batch = rows[i:i + MAX_BATCH]
        snap = spotify.remove_items(pid, [r["track_uri"] for r in batch])
        for r in batch:
            store.add_change("remove", r["track_id"], r["track_uri"], "rollback", "applied",
                             actor, reverts_change_id=r["change_id"], note=f"snapshot {snap}")
            store.transition(r["change_id"], "rolled_back", actor, f"removed; snapshot {snap}")
            removed.append(r["change_id"])
    sync_active(spotify, store)
    return {"rolled_back": removed}


def auto_apply_inbox(spotify, store: Store, actor: str = "auto-approve") -> dict:
    """If enabled, approve and add pending Inbox songs without an admin.

    Settings:
      auto_approve_inbox           true/false (default false)
      auto_approve_hold_explicit   true -> explicit songs wait for manual approval
    Songs from other source playlists still need manual approval.
    """
    if not store.get_setting("auto_approve_inbox", False):
        return {"enabled": False}
    if not store.get_setting("writes_enabled", False):
        log.warning("Inbox auto-approve is on but writes are disabled; skipping.")
        return {"enabled": True, "skipped": "writes disabled"}
    hold_explicit = bool(store.get_setting("auto_approve_hold_explicit", False))
    approved, held = [], []
    for ch in store.changes(status="pending", action="add"):
        if ch["source"] != "inbox":
            continue
        t = store.one("SELECT explicit FROM candidate_tracks WHERE track_id = ?", (ch["track_id"],))
        if hold_explicit and t and t["explicit"]:
            held.append(ch["change_id"])
            continue
        store.transition(ch["change_id"], "approved", actor, "auto-approved (inbox)")
        approved.append(ch["change_id"])
    result = {"enabled": True, "auto_approved": len(approved), "held_explicit": len(held)}
    if approved:
        result["apply"] = apply_approved(spotify, store, actor)
    return result
