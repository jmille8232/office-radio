"""Configuration.

Credentials come only from environment variables (Chalk secrets are injected as
env vars). Operational settings (name patterns, schedules, write switch) live in
the admin_settings table so they can change without a code change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

SCOPES = [
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-private",
    "playlist-modify-public",
]

DEFAULT_NAME_PATTERNS = [
    "office playlist", "office music", "office vibes", "work playlist",
    "work music", "office radio", "office mix", "office favorites",
    "workday playlist", "background music",
]

# Seconds between runs of each scheduled job.
DEFAULT_SCHEDULES = {
    "discovery": 24 * 3600,
    "source_sync": 6 * 3600,
    "reconciliation": 24 * 3600,
}

MASTER_NAME = "Office Radio — AI Curated"
MASTER_DESCRIPTION = (
    "An evolving office music playlist curated from the office's existing music collections."
)
INBOX_NAME = "Office Radio — Inbox"
INBOX_DESCRIPTION = (
    "Drop songs here for Office Radio. An admin reviews every suggestion before it is added."
)


@dataclass
class Settings:
    client_id: str | None
    client_secret: str | None
    redirect_uri: str | None
    refresh_token: str | None
    data_dir: str
    admin: str
    alert_webhook: str | None = None
    page_limit: int = 50
    missing: list[str] = field(default_factory=list)

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "office_radio.db")

    @property
    def token_path(self) -> str:
        return os.path.join(self.data_dir, "spotify_token.json")

    @classmethod
    def from_env(cls) -> "Settings":
        g = os.getenv
        return cls(
            client_id=g("SPOTIFY_CLIENT_ID"),
            client_secret=g("SPOTIFY_CLIENT_SECRET"),
            redirect_uri=g("SPOTIFY_REDIRECT_URI"),
            refresh_token=g("SPOTIFY_REFRESH_TOKEN"),
            data_dir=g("OFFICE_RADIO_DATA_DIR", "data"),
            admin=g("OFFICE_RADIO_ADMIN") or g("USER") or "admin",
            alert_webhook=g("OFFICE_RADIO_ALERT_WEBHOOK"),
            page_limit=int(g("SPOTIFY_PAGE_LIMIT", "50")),
        )

    def validate(self, need_refresh_token: bool = True) -> list[str]:
        """Return names of required variables that are missing."""
        required = {
            "SPOTIFY_CLIENT_ID": self.client_id,
            "SPOTIFY_CLIENT_SECRET": self.client_secret,
            "SPOTIFY_REDIRECT_URI": self.redirect_uri,
        }
        missing = [k for k, v in required.items() if not v]
        has_token_file = os.path.exists(self.token_path)
        if need_refresh_token and not self.refresh_token and not has_token_file:
            missing.append("SPOTIFY_REFRESH_TOKEN (or a saved token file)")
        self.missing = missing
        return missing
