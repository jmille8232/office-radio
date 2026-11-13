"""Phase 9 (scheduled jobs) and Phase 10 (reporting).

Jobs:
  discovery       find office playlists                    (default daily)
  source_sync     refresh catalog from source playlists    (default every 6h)
  reconciliation  mirror Office Radio + propose additions  (default daily)

Each run is recorded in playlist_sync_history. Failures are retried with
exponential backoff, then a notification is sent. A report is produced after
every run. Jobs only read from Spotify; writes happen via the admin CLI.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from . import catalog, maintenance
from .config import DEFAULT_SCHEDULES
from .store import Store

log = logging.getLogger("office_radio.jobs")

FAILED_RETRY_COOLDOWN = 15 * 60  # wait before re-running a job that failed
STALE_RUNNING = 2 * 3600          # a "running" row this old means the process died


def _reconcile(spotify, store: Store) -> dict:
    active = maintenance.sync_active(spotify, store)
    proposals = maintenance.generate_proposals(store)
    return {"active": active, "proposals": proposals}


JOBS: dict[str, Callable] = {
    "discovery": catalog.discover_playlists,
    "source_sync": catalog.sync_sources,
    "reconciliation": _reconcile,
}


def build_report(store: Store) -> dict:
    one = lambda sql, a=(): store.one(sql, a)[0]  # noqa: E731
    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    last_ok = store.one(
        "SELECT job, MAX(finished_at) AS t FROM playlist_sync_history WHERE status='success'"
    )
    return {
        "office_playlists": one("SELECT COUNT(*) FROM office_playlists WHERE still_matching=1"),
        "office_playlists_readable": one(
            "SELECT COUNT(*) FROM office_playlists WHERE still_matching=1 AND items_accessible=1"),
        "unique_candidate_tracks": one("SELECT COUNT(*) FROM candidate_tracks"),
        "tracks_in_office_radio": one("SELECT COUNT(*) FROM active_playlist"),
        "unavailable_in_office_radio": one(
            "SELECT COUNT(*) FROM active_playlist WHERE is_playable = 0"),
        "pending_approvals": one(
            "SELECT COUNT(*) FROM playlist_change_log WHERE status='pending'"),
        "approved_not_applied": one(
            "SELECT COUNT(*) FROM playlist_change_log WHERE status='approved'"),
        "new_candidates_last_24h": one(
            "SELECT COUNT(*) FROM playlist_change_log WHERE action='add' AND "
            "created_at >= ?", (day_ago,)),
        "last_successful_sync": last_ok["t"] if last_ok else None,
        "failed_api_requests_24h": one(
            "SELECT COUNT(*) FROM api_errors WHERE at >= ?", (day_ago,)),
        "failed_jobs_24h": one(
            "SELECT COUNT(*) FROM playlist_sync_history WHERE status='failed' AND "
            "started_at >= ?", (day_ago,)),
        "recent_changes": [
            {"change_id": r["change_id"], "action": r["action"], "track_id": r["track_id"],
             "status": r["status"], "updated_at": r["updated_at"]}
            for r in store.q("SELECT * FROM playlist_change_log ORDER BY updated_at DESC LIMIT 10")
        ],
        "writes_enabled": bool(store.get_setting("writes_enabled", False)),
    }


def notify(webhook: str | None, text: str) -> None:
    """Failure alert. Contains job name and error only, never track data."""
    log.error(text)
    if webhook:
        try:
            requests.post(webhook, json={"text": text}, timeout=10)
        except requests.RequestException as exc:
            log.error("Alert webhook failed: %s", exc)


def run_job(name: str, spotify, store: Store, webhook: str | None = None,
            max_attempts: int = 3, sleep: Callable[[float], None] = time.sleep) -> dict:
    fn = JOBS[name]
    run_id = store.start_run(name)
    for attempt in range(1, max_attempts + 1):
        try:
            details = fn(spotify, store)
            store.finish_run(run_id, "success", attempt, details)
            log.info("Job %s succeeded (attempt %d)", name, attempt)
            return {"status": "success", "details": details, "report": build_report(store)}
        except Exception as exc:  # noqa: BLE001 - recorded and re-tried
            log.warning("Job %s attempt %d failed: %s", name, attempt, exc)
            if attempt == max_attempts:
                store.finish_run(run_id, "failed", attempt, error=f"{type(exc).__name__}: {exc}")
                notify(webhook, f"Office Radio job '{name}' failed after {attempt} attempts: {exc}")
                return {"status": "failed", "error": str(exc), "report": build_report(store)}
            sleep(min(2 ** attempt * 5, 300))
    raise AssertionError("unreachable")


def _age_seconds(ts: str | None, now: datetime) -> float:
    if not ts:
        return float("inf")
    return (now - datetime.fromisoformat(ts)).total_seconds()


def due_jobs(store: Store, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    schedules = {**DEFAULT_SCHEDULES, **store.get_setting("schedules", {})}
    due = []
    for job in JOBS:
        last = store.one(
            "SELECT status, COALESCE(finished_at, started_at) AS t FROM playlist_sync_history "
            "WHERE job = ? ORDER BY run_id DESC LIMIT 1", (job,))
        if last is None:
            due.append(job)
        elif last["status"] == "failed":
            if _age_seconds(last["t"], now) >= FAILED_RETRY_COOLDOWN:
                due.append(job)
        elif last["status"] == "success" and _age_seconds(last["t"], now) >= schedules[job]:
            due.append(job)
        elif last["status"] == "running" and _age_seconds(last["t"], now) >= STALE_RUNNING:
            due.append(job)
    return due


def run_scheduler(spotify, store: Store, webhook: str | None = None, once: bool = False,
                  poll_seconds: int = 300) -> None:
    """Run due jobs in dependency order; repeat. Not continuous polling of Spotify:
    Spotify is only called when a job is due."""
    while True:
        for job in due_jobs(store):  # JOBS order = discovery, sync, reconciliation
            result = run_job(job, spotify, store, webhook)
            log.info("Report after %s: %s", job, json.dumps(result["report"], default=str))
        if once:
            return
        time.sleep(poll_seconds)
