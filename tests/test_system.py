"""Phases 3-10 against an in-memory Spotify fake and a temp SQLite store."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from office_radio import catalog, jobs, maintenance
from office_radio.maintenance import WritesDisabled
from office_radio.normalize import entry_track, name_matches
from office_radio.ranking import RankingDisabled, RankingModule
from office_radio.spotify.client import SpotifyAPIError
from office_radio.store import Store
from tests.fakes import FakeSpotify, entry, playlist, track

MASTER = "pl_master"


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def setup(store, playlists, items, master=True, writes=False):
    sp = FakeSpotify(playlists, items)
    if master:
        store.set_setting("office_radio_playlist_id", MASTER, "test")
        sp.items.setdefault(MASTER, [])
    store.set_setting("writes_enabled", writes, "test")
    return sp


def full_sync(sp, store):
    catalog.discover_playlists(sp, store)
    catalog.sync_sources(sp, store)
    maintenance.sync_active(sp, store)
    return maintenance.generate_proposals(store)


# ---- matching & normalization ------------------------------------------------

@pytest.mark.parametrize("name,ok", [
    ("Office Vibes", True), ("OFFICE—vibes!!", True), ("Friday office mix", True),
    ("Background Music for focus", True), ("Workday Playlist 2026", True),
    ("Officer Vibes", False), ("Gym", False), ("Network music", False),
])
def test_name_matching(name, ok):
    assert name_matches(name, ["office vibes", "office mix", "background music",
                               "workday playlist", "work music"]) is ok


def test_entry_track_skips_unsupported():
    assert entry_track(entry({"type": "episode", "id": "e"})) is None
    assert entry_track(entry(track("a"), is_local=True)) is None
    assert entry_track(entry(None)) is None
    assert entry_track(entry(track("a"), key="track"))["id"] == "a"


# ---- discovery -----------------------------------------------------------------

def test_discovery_matches_and_classifies(store):
    sp = setup(store, [
        playlist("p1", "Office Vibes"),
        playlist("p2", "Office Music", owner="coworker"),               # followed only
        playlist("p3", "Office Mix", owner="coworker", collaborative=True),
        playlist("p4", "Office Radio — Inbox"),
        playlist("p5", "Today's Office Hits", owner="spotify"),
        playlist("p6", "Gym"),
        playlist(MASTER, "Office Radio — AI Curated"),
    ], {})
    stats = catalog.discover_playlists(sp, store)
    rows = {r["playlist_id"]: r for r in store.source_playlists(accessible_only=False)}
    assert set(rows) == {"p1", "p2", "p3", "p4"}
    assert rows["p2"]["items_accessible"] == 0 and rows["p3"]["items_accessible"] == 1
    assert rows["p4"]["is_inbox"] == 1
    assert stats["skipped_spotify_owned"] == 1


def test_configurable_patterns(store):
    sp = setup(store, [playlist("p1", "Desk Jams")], {})
    catalog.discover_playlists(sp, store)
    assert store.source_playlists() == []
    store.set_setting("name_patterns", ["desk jams"], "admin")
    catalog.discover_playlists(sp, store)
    assert len(store.source_playlists()) == 1


def test_playlist_no_longer_matching_is_flagged_not_deleted(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {})
    catalog.discover_playlists(sp, store)
    sp.playlists[0]["name"] = "Old stuff"
    catalog.discover_playlists(sp, store)
    row = store.one("SELECT still_matching FROM office_playlists WHERE playlist_id='p1'")
    assert row["still_matching"] == 0


# ---- catalog --------------------------------------------------------------------

def test_catalog_dedupes_and_keeps_relationships(store):
    sp = setup(store, [playlist("p1", "Office Vibes"), playlist("p2", "Work Music")], {
        "p1": [entry(track("a")), entry(track("b")), entry(track("a"))],
        "p2": [entry(track("a")), entry({"type": "episode", "id": "ep"})],
    })
    catalog.discover_playlists(sp, store)
    stats = catalog.sync_sources(sp, store)
    assert store.one("SELECT COUNT(*) FROM candidate_tracks")[0] == 2
    rels = store.q("SELECT playlist_id, track_id FROM playlist_track_relationships ORDER BY 1, 2")
    assert [tuple(r) for r in rels] == [("p1", "a"), ("p1", "b"), ("p2", "a")]
    assert stats["skipped_unsupported"] == 1


def test_sync_is_idempotent(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]})
    for _ in range(3):
        full_sync(sp, store)
    assert store.one("SELECT COUNT(*) FROM candidate_tracks")[0] == 1
    assert store.one("SELECT COUNT(*) FROM playlist_change_log")[0] == 1


def test_removed_track_marked_not_present(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a")), entry(track("b"))]})
    catalog.discover_playlists(sp, store)
    catalog.sync_sources(sp, store)
    sp.items["p1"] = [entry(track("a"))]
    catalog.sync_sources(sp, store)
    r = store.one("SELECT present FROM playlist_track_relationships WHERE track_id='b'")
    assert r["present"] == 0


def test_forbidden_playlist_marked_inaccessible(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {})
    sp.item_errors["p1"] = SpotifyAPIError(403, "Forbidden")
    catalog.discover_playlists(sp, store)
    stats = catalog.sync_sources(sp, store)
    assert stats["now_inaccessible"] == ["p1"] and store.source_playlists() == []


def test_failed_fetch_writes_nothing(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {})
    sp.item_errors["p1"] = SpotifyAPIError(500, "boom")
    catalog.discover_playlists(sp, store)
    with pytest.raises(SpotifyAPIError):
        catalog.sync_sources(sp, store)
    assert store.one("SELECT COUNT(*) FROM candidate_tracks")[0] == 0


# ---- proposals & approval --------------------------------------------------------

def test_inbox_and_catalog_proposals(store):
    sp = setup(store, [playlist("pi", "Office Radio — Inbox", collaborative=True),
                       playlist("p1", "Office Vibes")],
               {"pi": [entry(track("a"), added_by="maria")], "p1": [entry(track("b"))],
                MASTER: [entry(track("c"))]})
    sp.items["p1"].append(entry(track("c")))
    res = full_sync(sp, store)
    srcs = {r["track_id"]: r["source"] for r in store.changes()}
    assert srcs == {"a": "inbox", "b": "catalog"}
    assert res["already_in_office_radio"] == 1


def test_rejected_not_reproposed(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]})
    full_sync(sp, store)
    maintenance.decide(store, [1], approve=False, actor="john", note="no")
    full_sync(sp, store)
    assert [r["status"] for r in store.changes()] == ["rejected"]


def test_invalid_transition(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]})
    full_sync(sp, store)
    maintenance.decide(store, [1], False, "john")
    with pytest.raises(ValueError):
        maintenance.decide(store, [1], True, "john")
    with pytest.raises(KeyError):
        maintenance.decide(store, [99], True, "john")


# ---- write gating -----------------------------------------------------------------

def test_writes_disabled_blocks_everything(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]})
    full_sync(sp, store)
    maintenance.decide(store, [1], True, "john")
    with pytest.raises(WritesDisabled):
        maintenance.apply_approved(sp, store, "john")
    store.set_setting("office_radio_playlist_id", None, "t")
    with pytest.raises(WritesDisabled):
        maintenance.ensure_master(sp, store, "john", create=True, confirm=True)
    assert sp.add_calls == [] and sp.created == []


def test_create_master_requires_confirm_and_is_private(store):
    sp = setup(store, [], {}, master=False, writes=True)
    with pytest.raises(ValueError):
        maintenance.ensure_master(sp, store, "john", create=True)
    pid = maintenance.ensure_master(sp, store, "john", create=True, confirm=True)
    assert sp.created[0]["public"] is False and sp.created[0]["name"] == "Office Radio — AI Curated"
    assert store.get_setting("office_radio_playlist_id") == pid
    assert maintenance.ensure_master(sp, store, "john", create=True, confirm=True) == pid
    assert len(sp.created) == 1


def test_existing_master_found_instead_of_duplicated(store):
    sp = setup(store, [playlist("existing", "Office Radio — AI Curated")], {},
               master=False, writes=True)
    assert maintenance.ensure_master(sp, store, "john", create=True, confirm=True) == "existing"
    assert sp.created == []


def test_create_inbox_collaborative_private(store):
    sp = setup(store, [], {}, writes=True)
    maintenance.ensure_inbox(sp, store, "john", confirm=True)
    assert sp.created[0]["collaborative"] is True and sp.created[0]["public"] is False


# ---- apply & rollback ---------------------------------------------------------------

def test_apply_only_approved_and_never_removes(store):
    sp = setup(store, [playlist("p1", "Office Vibes")],
               {"p1": [entry(track(x)) for x in "abc"], MASTER: [entry(track("z"))]}, writes=True)
    full_sync(sp, store)
    maintenance.decide(store, [1], True, "john")
    maintenance.decide(store, [2], False, "john")
    res = maintenance.apply_approved(sp, store, "john")
    assert sp.add_calls == [(MASTER, ["spotify:track:a"])] and sp.remove_calls == []
    assert res["added"] == [1]
    assert store.active_ids() == {"z", "a"}
    assert store.change(3)["status"] == "pending"


def test_apply_skips_tracks_added_manually(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]}, writes=True)
    full_sync(sp, store)
    maintenance.decide(store, [1], True, "john")
    sp.items[MASTER] = [entry(track("a"))]
    res = maintenance.apply_approved(sp, store, "john")
    assert res["already_present"] == [1] and sp.add_calls == []


def test_apply_batches_and_recovers_from_failure(store):
    sp = setup(store, [playlist("p1", "Office Vibes")],
               {"p1": [entry(track(f"t{i}")) for i in range(150)]}, writes=True)
    full_sync(sp, store)
    maintenance.decide(store, [r["change_id"] for r in store.changes("pending")], True, "john")
    sp.fail_add_on_call = 1
    with pytest.raises(RuntimeError):
        maintenance.apply_approved(sp, store, "john")
    assert len(store.changes("applied")) == 100 and len(store.changes("approved")) == 50
    sp.fail_add_on_call = None
    assert len(maintenance.apply_approved(sp, store, "john")["added"]) == 50
    assert [len(c[1]) for c in sp.add_calls] == [100, 50]


def test_rollback_requires_confirm_and_logs(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]}, writes=True)
    full_sync(sp, store)
    maintenance.decide(store, [1], True, "john")
    maintenance.apply_approved(sp, store, "john")
    with pytest.raises(ValueError):
        maintenance.rollback(sp, store, [1], "john")
    maintenance.rollback(sp, store, [1], "john", confirm=True)
    assert sp.remove_calls == [(MASTER, ["spotify:track:a"])]
    assert store.change(1)["status"] == "rolled_back"
    rev = store.changes(action="remove")[0]
    assert rev["reverts_change_id"] == 1 and rev["source"] == "rollback"
    assert store.active_ids() == set()
    full_sync(sp, store)  # rolled-back track is not re-proposed
    assert store.changes("pending") == []


def test_rollback_rejects_non_applied(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]}, writes=True)
    full_sync(sp, store)
    with pytest.raises(ValueError):
        maintenance.rollback(sp, store, [1], "john", confirm=True)


def test_audit_trail(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]}, writes=True)
    full_sync(sp, store)
    maintenance.decide(store, [1], True, "john", "good")
    maintenance.apply_approved(sp, store, "john")
    ev = json.loads(store.change(1)["events"])
    assert [(e["to"], e["by"]) for e in ev] == [
        ("pending", "reconciliation"), ("approved", "john"), ("applied", "john")]


def test_unavailable_tracks_reported(store):
    sp = setup(store, [], {MASTER: [entry(track("a", playable=False)), entry(None), entry(track("b"))]})
    res = maintenance.sync_active(sp, store)
    assert res["unavailable"] == ["a"] and res["unsupported_or_removed"] == 1


# ---- jobs, scheduling, reporting -------------------------------------------------------

def test_run_job_success_recorded(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]})
    for j in ("discovery", "source_sync", "reconciliation"):
        assert jobs.run_job(j, sp, store, sleep=lambda s: None)["status"] == "success"
    rep = jobs.build_report(store)
    assert rep["office_playlists"] == 1 and rep["unique_candidate_tracks"] == 1
    assert rep["pending_approvals"] == 1 and rep["new_candidates_last_24h"] == 1
    assert rep["last_successful_sync"] is not None


def test_run_job_retries_then_notifies(store, monkeypatch):
    sp = setup(store, [playlist("p1", "Office Vibes")], {})
    catalog.discover_playlists(sp, store)
    sp.item_errors["p1"] = SpotifyAPIError(500, "boom")
    alerts, sleeps = [], []
    monkeypatch.setattr(jobs, "notify", lambda hook, text: alerts.append(text))
    res = jobs.run_job("source_sync", sp, store, sleep=sleeps.append)
    assert res["status"] == "failed" and len(sleeps) == 2 and "source_sync" in alerts[0]
    row = store.one("SELECT status, attempts FROM playlist_sync_history ORDER BY run_id DESC")
    assert (row["status"], row["attempts"]) == ("failed", 3)
    assert jobs.build_report(store)["failed_jobs_24h"] == 1


def test_due_jobs(store):
    assert jobs.due_jobs(store) == ["discovery", "source_sync", "reconciliation"]
    for j in jobs.JOBS:
        store.finish_run(store.start_run(j), "success", 1)
    now = datetime.now(timezone.utc)
    assert jobs.due_jobs(store, now) == []
    assert jobs.due_jobs(store, now + timedelta(hours=7)) == ["source_sync"]
    store.set_setting("schedules", {"discovery": 60}, "admin")
    assert "discovery" in jobs.due_jobs(store, now + timedelta(minutes=2))


def test_failed_job_waits_for_cooldown(store):
    store.finish_run(store.start_run("discovery"), "failed", 3, error="x")
    now = datetime.now(timezone.utc)
    assert "discovery" not in jobs.due_jobs(store, now)
    assert "discovery" in jobs.due_jobs(store, now + timedelta(minutes=16))


def test_jobs_never_write_to_spotify(store):
    sp = setup(store, [playlist("p1", "Office Vibes")], {"p1": [entry(track("a"))]}, writes=True)
    jobs.run_scheduler(sp, store, once=True)
    assert sp.add_calls == [] and sp.remove_calls == [] and sp.created == []


# ---- ranking ----------------------------------------------------------------------------

def test_ranking_disabled_by_default_and_without_approval(store):
    r = RankingModule(store)
    with pytest.raises(RankingDisabled):
        r.score(["a"])
    store.set_setting("ranking_enabled", True, "admin")
    with pytest.raises(RankingDisabled):  # still needs a compliance approval ID
        r.score(["a"])
