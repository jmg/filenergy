"""DB-backed rate limiter that doubles as an audit trail.

Counts events of a given type for a user in a sliding window. Cheap because
events are already logged for analytics; rate limiting is a side effect.
"""
from __future__ import annotations

from filenergy import settings
from filenergy.services import events


class RateLimited(Exception):
    def __init__(self, retry_after: int, limit: int, window: int):
        super().__init__(
            f"Rate limit exceeded: {limit} requests per {window}s"
        )
        self.retry_after = retry_after
        self.limit = limit
        self.window = window


def check_ask(user) -> None:
    """Raise RateLimited if `user` has exceeded the /ask quota."""
    if user is None or not getattr(user, "id", None):
        return
    used = events.count_recent(
        user, events.ASK_QUESTION, settings.ASK_RATE_WINDOW_SECONDS
    )
    if used >= settings.ASK_RATE_LIMIT:
        raise RateLimited(
            retry_after=settings.ASK_RATE_WINDOW_SECONDS,
            limit=settings.ASK_RATE_LIMIT,
            window=settings.ASK_RATE_WINDOW_SECONDS,
        )
