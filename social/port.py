"""SocialSourcePort — the boundary every social-fetch backend implements.

The downstream pipeline (ticker extraction, hourly aggregation, anomaly
conjunction, sentiment) consumes only SocialPost lists from fetch_posts();
which Reddit door the posts came through is a config detail. Selection lives
in get_source(): config key `social_source` picks the backend, and the OAuth
adapter is only eligible when its env credentials actually exist — so the
day Reddit approves the application, the swap is one config row.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import text

log = logging.getLogger("sigint.social")

# fetch listings and their per-spec limits
LISTINGS = {"new": 50, "hot": 25}


class SocialSourceError(RuntimeError):
    """Adapter-level failure (transport, parse, auth)."""


class SocialBackoffError(SocialSourceError):
    """Rate-limited / blocked (HTTP 429/403) — the caller must skip the
    remainder of the cycle, never retry within it."""


@dataclass
class SocialPost:
    post_id: str
    ts: datetime
    author: str
    title: str
    body: str
    score: int
    num_comments: int

    def as_dict(self) -> dict:
        return {
            "post_id": self.post_id, "ts": self.ts, "author": self.author,
            "title": self.title, "body": self.body, "score": self.score,
            "num_comments": self.num_comments,
        }


class SocialSourcePort(Protocol):
    def fetch_posts(self, subreddit: str, listing: str) -> list[SocialPost]:
        """listing: 'new' | 'hot'. Raises SocialBackoffError on rate limiting;
        returns [] when the listing is empty/unavailable (soft failure)."""
        ...


def configured_source(db) -> str:
    v = db.execute(text(
        "SELECT value FROM config WHERE key = 'social_source'")).scalar()
    return v if v else "reddit_json"


def get_source(db):
    """Resolve the active adapter. reddit_oauth requires BOTH the config row
    and live env credentials; anything else degrades to the keyless default."""
    from social.adapters import reddit_json, reddit_oauth

    mode = configured_source(db)
    if mode == "reddit_oauth":
        if reddit_oauth.creds_present():
            return reddit_oauth.RedditOAuthAdapter()
        log.warning("social_source='reddit_oauth' but REDDIT_CLIENT_ID/"
                    "REDDIT_CLIENT_SECRET unset - falling back to reddit_json")
        return reddit_json.RedditJSONAdapter()
    if mode != "reddit_json":
        log.warning("unknown social_source %r - using reddit_json", mode)
    return reddit_json.RedditJSONAdapter()


def resolved_source_name(db) -> str:
    """The adapter name get_source() would actually return (for /status)."""
    from social.adapters import reddit_oauth
    mode = configured_source(db)
    if mode == "reddit_oauth" and reddit_oauth.creds_present():
        return "reddit_oauth"
    return "reddit_json"
