"""Office Radio admin CLI.  python -m office_radio.cli <command>

Setup
  check                         validate env, data dir, Spotify login (read-only)
  login                         one-time browser sign-in (run on your laptop)
Read-only jobs
  discover | sync | reconcile   run one job now
  refresh                       run discover, sync and reconcile in order
  scheduler [--once]            run jobs on their schedules
  report                        show the monitoring report
Review
  list [--status S]             show proposals (default: pending)
  approve ID... | --all-pending
  reject ID... [--note TEXT]
Writes (need `settings enable-writes --confirm`)
  create-master --confirm       create "Office Radio — AI Curated" (private)
  create-inbox --confirm        create "Office Radio — Inbox" (private, collaborative)
  set-master PLAYLIST_ID        use an existing playlist as Office Radio
  apply                         add approved tracks to Office Radio
  rollback ID... --confirm      remove tracks this system added
Settings
  settings show
  settings patterns add|remove TEXT
  settings schedule JOB SECONDS
  settings enable-writes --confirm | disable-writes
  settings auto-approve-inbox on|off [--hold-explicit]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from . import jobs, maintenance, persist
from .maintenance import WritesDisabled
from .spotify.auth import AuthError
from .spotify.client import SpotifyAPIError
from .config import DEFAULT_NAME_PATTERNS, DEFAULT_SCHEDULES, Settings
from .spotify.auth import TokenManager
from .spotify.client import SpotifyClient
from .store import Store


def make_spotify(settings: Settings, store: Store) -> SpotifyClient:
    missing = settings.validate()
    if missing:
        sys.exit("Missing configuration: " + ", ".join(missing))
    tokens = TokenManager(settings.client_id, settings.client_secret,
                          settings.refresh_token, settings.token_path)
    return SpotifyClient(tokens, page_limit=settings.page_limit,
                         on_error=store.log_api_error)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="office_radio")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("check", "login", "discover", "sync", "reconcile", "refresh", "report", "apply"):
        sub.add_parser(name)
    s = sub.add_parser("scheduler")
    s.add_argument("--once", action="store_true")
    s = sub.add_parser("list")
    s.add_argument("--status", default="pending")
    s = sub.add_parser("approve")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--all-pending", action="store_true")
    s.add_argument("--note")
    s = sub.add_parser("reject")
    s.add_argument("ids", nargs="+", type=int)
    s.add_argument("--note")
    for name in ("create-master", "create-inbox"):
        sub.add_parser(name).add_argument("--confirm", action="store_true")
    sub.add_parser("set-master").add_argument("playlist_id")
    s = sub.add_parser("rollback")
    s.add_argument("ids", nargs="+", type=int)
    s.add_argument("--confirm", action="store_true")
    st = sub.add_parser("settings").add_subparsers(dest="action", required=True)
    st.add_parser("show")
    pat = st.add_parser("patterns")
    pat.add_argument("op", choices=["add", "remove"])
    pat.add_argument("text")
    sch = st.add_parser("schedule")
    sch.add_argument("job", choices=list(DEFAULT_SCHEDULES))
    sch.add_argument("seconds", type=int)
    st.add_parser("enable-writes").add_argument("--confirm", action="store_true")
    st.add_parser("disable-writes")
    aa = st.add_parser("auto-approve-inbox")
    aa.add_argument("state", choices=["on", "off"])
    aa.add_argument("--hold-explicit", action="store_true",
                    help="explicit songs wait for manual approval")
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (AuthError, SpotifyAPIError, WritesDisabled, ValueError, KeyError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        # Save any changes when data lives on a Chalk Volume (no-op otherwise).
        persist.commit(Settings.from_env().data_dir)


def _main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    me = settings.admin

    if args.cmd == "login":
        from .spotify.login import run_login
        missing = settings.validate(need_refresh_token=False)
        if missing:
            sys.exit("Missing configuration: " + ", ".join(missing))
        run_login(settings.client_id, settings.client_secret,
                  settings.redirect_uri, settings.token_path)
        return 0

    store = Store(settings.db_path)

    # Commands that never touch Spotify
    if args.cmd == "report":
        _print(jobs.build_report(store))
        return 0
    if args.cmd == "list":
        for r in store.changes(status=args.status):
            t = store.one("SELECT name, artist_names, explicit FROM candidate_tracks "
                          "WHERE track_id = ?", (r["track_id"],))
            label = f"{t['name']} — {', '.join(json.loads(t['artist_names']))}" if t else r["track_id"]
            flag = "  [explicit]" if t and t["explicit"] else ""
            print(f"{r['change_id']:>5}  {r['action']:<6} {r['source']:<8} {label}{flag}")
        return 0
    if args.cmd in ("approve", "reject"):
        ids = args.ids
        if args.cmd == "approve" and args.all_pending:
            ids = [r["change_id"] for r in store.changes(status="pending")]
        if not ids:
            sys.exit("Give change IDs (see `list`)")
        done = maintenance.decide(store, ids, args.cmd == "approve", me, args.note)
        print(f"{args.cmd}d {len(done)} change(s)")
        return 0
    if args.cmd == "set-master":
        store.set_setting("office_radio_playlist_id", args.playlist_id, me)
        print("Office Radio playlist set.")
        return 0
    if args.cmd == "settings":
        if args.action == "show":
            _print({r["key"]: json.loads(r["value"]) for r in store.q("SELECT * FROM admin_settings")})
        elif args.action == "patterns":
            pats = store.get_setting("name_patterns", DEFAULT_NAME_PATTERNS)
            pats = sorted(set(pats) | {args.text}) if args.op == "add" else [p for p in pats if p != args.text]
            store.set_setting("name_patterns", pats, me)
            _print(pats)
        elif args.action == "schedule":
            sched = store.get_setting("schedules", {})
            sched[args.job] = args.seconds
            store.set_setting("schedules", sched, me)
            _print(sched)
        elif args.action == "enable-writes":
            if not args.confirm:
                sys.exit("Review `report` and `list` first, then re-run with --confirm.")
            store.set_setting("writes_enabled", True, me)
            print("Spotify writes enabled.")
        elif args.action == "disable-writes":
            store.set_setting("writes_enabled", False, me)
            print("Spotify writes disabled.")
        elif args.action == "auto-approve-inbox":
            on = args.state == "on"
            store.set_setting("auto_approve_inbox", on, me)
            store.set_setting("auto_approve_hold_explicit", on and args.hold_explicit, me)
            print(f"Inbox auto-approve {'ON' if on else 'OFF'}"
                  + (" (explicit songs held for approval)" if on and args.hold_explicit else ""))
            if on and not store.get_setting("writes_enabled", False):
                print("Note: writes are disabled, so nothing will be added until you run "
                      "`settings enable-writes --confirm`.")
        return 0

    spotify = make_spotify(settings, store)
    if args.cmd == "check":
        print("Environment OK. Signed in to Spotify as:", spotify.current_user_id())
        print("Database:", settings.db_path)
        print("Writes enabled:", bool(store.get_setting("writes_enabled", False)))
    elif args.cmd in ("discover", "sync", "reconcile"):
        job = {"discover": "discovery", "sync": "source_sync", "reconcile": "reconciliation"}[args.cmd]
        res = jobs.run_job(job, spotify, store, settings.alert_webhook)
        _print(res)
        return 0 if res["status"] == "success" else 1
    elif args.cmd == "refresh":
        status = 0
        for job in ("discovery", "source_sync", "reconciliation"):
            res = jobs.run_job(job, spotify, store, settings.alert_webhook)
            print(f"{job}: {res['status']}" + (f" - {res.get('error')}" if res["status"] != "success" else ""))
            if res["status"] != "success":
                status = 1
                break
        else:
            details = store.one("SELECT details FROM playlist_sync_history WHERE job='reconciliation' "
                                "ORDER BY run_id DESC LIMIT 1")["details"]
            _print(json.loads(details).get("inbox_auto_approve"))
        return status
    elif args.cmd == "scheduler":
        jobs.run_scheduler(spotify, store, settings.alert_webhook, once=args.once,
                           poll_seconds=int(os.getenv("OFFICE_RADIO_POLL_SECONDS", "300")),
                           after_job=lambda: persist.commit(settings.data_dir))
    elif args.cmd == "create-master":
        pid = maintenance.ensure_master(spotify, store, me, create=True, confirm=args.confirm)
        print("Office Radio playlist:", pid)
    elif args.cmd == "create-inbox":
        pid = maintenance.ensure_inbox(spotify, store, me, confirm=args.confirm)
        print("Inbox playlist:", pid, "- share its invite link from the Spotify app.")
    elif args.cmd == "apply":
        _print(maintenance.apply_approved(spotify, store, me))
    elif args.cmd == "rollback":
        _print(maintenance.rollback(spotify, store, args.ids, me, confirm=args.confirm))
    return 0


if __name__ == "__main__":
    sys.exit(main())
