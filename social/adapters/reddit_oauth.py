"""OAuth/praw adapter — fully built, dormant until Reddit approves the app.

Reddit's Nov 2025 Responsible Builder Policy ended self-serve OAuth app
registration; approval is now an application process. This adapter is the
original implementation, kept production-ready so the day credentials arrive
the swap is: set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET (+ optional
REDDIT_USERNAME / REDDIT_PASSWORD for a script app) in .env and flip one
config row:

    UPDATE config SET value = to_jsonb(CAST('reddit_oauth' AS text))
    WHERE key = 'social_source';

get_source() refuses to select this adapter unless the env creds are
actually present, so a premature config flip degrades to reddit_json
instead of crash-looping.
"""
from __future__ import annotations

import logging
import os

from social.port import (
    LISTINGS, SocialBackoffError, SocialPost, SocialSourceError,
)

log = logging.getLogger("sigint.social.oauth")

ENV_ID = "REDDIT_CLIENT_ID"
ENV_SECRET = "REDDIT_CLIENT_SECRET"
ENV_UA = "REDDIT_USER_AGENT"
ENV_USER = "REDDIT_USERNAME"
ENV_PASS = "REDDIT_PASSWORD"


def creds_present() -> bool:
    return bool(os.environ.get(ENV_ID) and os.environ.get(ENV_SECRET))


class RedditOAuthAdapter:
    def __init__(self):
        if not creds_present():
            raise SocialSourceError(
                f"{ENV_ID}/{ENV_SECRET} not set - reddit_oauth unavailable")
        self._reddit = None

    @property
    def name(self) -> str:
        return "reddit_oauth"

    def _client(self):
        if self._reddit is None:
            import praw  # lazy: only the active adapter pays the import
            kwargs = dict(
                client_id=os.environ[ENV_ID],
                client_secret=os.environ[ENV_SECRET],
                user_agent=os.environ.get(ENV_UA)
                or "linux:sigint-social:v0.1 (oauth)",
            )
            if os.environ.get(ENV_USER) and os.environ.get(ENV_PASS):
                kwargs["username"] = os.environ[ENV_USER]
                kwargs["password"] = os.environ[ENV_PASS]
            self._reddit = praw.Reddit(**kwargs)
            self._reddit.read_only = True
        return self._reddit

    def fetch_posts(self, subreddit: str, listing: str) -> list[SocialPost]:
        if listing not in LISTINGS:
            raise SocialSourceError(f"unknown listing {listing!r}")
        try:
            sub = self._client().subreddit(subreddit)
            submissions = (sub.new(limit=LISTINGS[listing])
                           if listing == "new"
                           else sub.hot(limit=LISTINGS[listing]))
            return [_to_post(s) for s in submissions]
        except SocialSourceError:
            raise
        except Exception as e:
            # praw raises prawcore exceptions; map rate limiting to backoff
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (429, 403):
                raise SocialBackoffError(
                    f"reddit_oauth HTTP {status}: {e}") from e
            log.info("reddit_oauth fetch failed (r/%s/%s): %s",
                     subreddit, listing, e)
            return []


def _to_post(s) -> SocialPost:
    from datetime import datetime, timezone
    return SocialPost(
        post_id=str(s.id),
        ts=datetime.fromtimestamp(float(s.created_utc), tz=timezone.utc),
        author=str(getattr(s.author, "name", None) or "[deleted]"),
        title=str(s.title or ""),
        body=str(getattr(s, "selftext", "") or ""),
        score=int(s.score or 0),
        num_comments=int(s.num_comments or 0),
    )
