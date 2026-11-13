# Office Radio

Builds and maintains an office playlist on Spotify from the office's existing
playlists plus a shared **Office Radio — Inbox**. Every addition is approved
by an admin first. Uses only Spotify's official Web API (Feb 2026 endpoints).

## Architecture

```
Spotify Web API
  └─ spotify/auth.py      OAuth code flow, token refresh + rotation
  └─ spotify/client.py    endpoints, pagination, 429/5xx retries, error log
catalog.py                discovery (Phase 3) + track catalog (Phase 4)    read-only
maintenance.py            Office Radio/Inbox (Phase 5), proposals, apply,
                          rollback (Phase 6)                               the only writer
ranking.py                Phase 7 interface, disabled, no Spotify data
store.py                  Phase 8 datasets (SQLite)
jobs.py                   Phase 9 schedules/retries/alerts, Phase 10 report
cli.py                    admin commands
```

Scheduled jobs only **read** from Spotify. Writes happen only through the
admin CLI, only after `settings enable-writes --confirm`, and only to the
Office Radio playlist. Nothing is deleted automatically.

**Storage:** all data goes through `store.py` (SQLite at
`$OFFICE_RADIO_DATA_DIR/office_radio.db`). Point `OFFICE_RADIO_DATA_DIR` at
persistent storage, such as a Chalk Volume mount; sandbox files are discarded
when it stops. To move the datasets into Chalk, replace `Store` with a
Chalk-backed class with the same methods.

## Environment

| Variable | Where it comes from |
|---|---|
| `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` | Spotify developer app → Chalk secrets |
| `SPOTIFY_REDIRECT_URI` | e.g. `http://127.0.0.1:8888/callback` (must match the app) |
| `SPOTIFY_REFRESH_TOKEN` | produced by `login` → Chalk secret |
| `OFFICE_RADIO_DATA_DIR` | persistent path (default `./data`) |
| `OFFICE_RADIO_ADMIN` | name in the audit log (default `$USER`) |
| `OFFICE_RADIO_ALERT_WEBHOOK` | optional; job-failure alerts (no track data) |

## Runbook (Phase 12 order)

1. **Install and test**
   `pip install -r requirements.txt && python -m pytest -q`
2. **Log in once, on your laptop** (needs a browser):
   set the three SPOTIFY_* vars, run `python -m office_radio.cli login`,
   copy the saved refresh token into the Chalk secret `SPOTIFY_REFRESH_TOKEN`,
   then delete the local token file.
3. **Verify in the sandbox:** `python -m office_radio.cli check`
4. **Read-only sync:**
   `discover`, then `sync`, then `report`. Playlists coworkers own but haven't
   shared as collaborative show as not readable (Spotify only returns items for
   playlists you own or collaborate on).
5. **Turn on writes** after reviewing the report:
   `settings enable-writes --confirm`
6. **Create playlists:** `create-master --confirm`, `create-inbox --confirm`.
   Share the Inbox invite link from the Spotify app.
7. **Propose → approve → apply:** `reconcile`, `list`, `approve 1 2 3`
   (or `approve --all-pending`), `reject 4 --note "..."`, `apply`.
8. **Run on a schedule:** `scheduler` (or `scheduler --once` from an external
   cron). Defaults: discovery daily, source sync every 6h, reconciliation daily;
   change with `settings schedule source_sync 21600`.

Undo: `rollback <change_id> --confirm` removes tracks this system added.
Spotify can't restore old snapshots, and removing a track removes every copy
of it from the playlist.

## Spotify policy guardrails

- No scraping, private APIs, or Spotify-owned/editorial playlists (blocked for
  new apps; discovery skips them).
- No listening, play or skip data is read. No Spotify data goes to an LLM.
- Ranking stays disabled until a separately approved, non-Spotify feedback
  source exists (needs `ranking_enabled` and `ranking_compliance_approval_id`).
- Development Mode: app owner needs Premium; up to 5 authorized users.

## Tests

54 tests cover OAuth, token refresh/rotation, pagination, rate limits, retries,
name matching, normalization, dedupe, missing/unsupported tracks, discovery,
playlist creation, insertion and batching, write gating, failure recovery,
rollback, audit trail, scheduling, job failures and reporting. All Spotify
calls are mocked.
