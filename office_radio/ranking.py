"""Phase 7: ranking interface. DISABLED.

This module deliberately contains no scoring logic and reads no Spotify data.
Spotify's Developer Policy prohibits deriving listenership metrics from Spotify
content, so it must never:
  - poll playback to infer skips,
  - combine users' listening histories,
  - infer skips from missing recently-played records,
  - produce a Spotify-derived performance score,
  - send Spotify metadata or listening data to an LLM.

It defines the shape of a future, independently authorized human-curation
input (feedback_events table). Enabling it requires a recorded compliance
approval AND a real implementation; until then every entry point refuses.
"""
from __future__ import annotations

from dataclasses import dataclass

from .store import Store


class RankingDisabled(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedbackEvent:
    track_id: str
    feedback_source: str          # must be a non-Spotify, approved source
    feedback_at: str
    feedback_type: str
    reviewer_authorization: str   # ID of the approval covering this source
    review_status: str            # e.g. pending_review | approved


class RankingModule:
    def __init__(self, store: Store) -> None:
        self.store = store

    @property
    def enabled(self) -> bool:
        return bool(self.store.get_setting("ranking_enabled", False)) and bool(
            self.store.get_setting("ranking_compliance_approval_id")
        )

    def _guard(self) -> None:
        if not self.enabled:
            raise RankingDisabled(
                "Ranking is disabled pending a compliance review of its data source."
            )

    def record_feedback(self, event: FeedbackEvent) -> None:
        self._guard()
        raise NotImplementedError("Implement only after the data source is approved.")

    def score(self, track_ids: list[str]) -> dict[str, float]:
        self._guard()
        raise NotImplementedError("Implement only after the data source is approved.")
