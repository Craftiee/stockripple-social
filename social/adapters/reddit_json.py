"""DEFAULT adapter: Reddit's public read-only JSON endpoints, no credentials.

Reddit closed self-serve OAuth registration (Nov 2025 Responsible Builder
Policy), but the unauthenticated listing endpoints remain available for
polite, low-volume readers. The contract here is deliberately conservative:

  * descriptive User-Agent from REDDIT_USER_AGENT (a generic UA is the #1
    cause of 429s on these endpoints)
  * sequential requests, >= 2s apart (enforced in-process, never parallel)
  * HTTP 429/403 -> SocialBackoffError and an exponential in-process cool-off
    (2 min, 4 min, ... capped at 1h); while cooling off every call raises
    immediately so a cycle can skip cleanly. We NEVER hammer.
  * empty/missing listings return [] - a soft failure, not an exception
"""
from __future__ import annotations

import logging
import os
import time

from social.port import (
    LISTINGS, SocialBackoffError, SocialPost, SocialSourceError,
)

log = logging.getLogger("sigint.social.json")

BASE = "https://www.reddit.com"
MIN_INTERVAL = 2.0                  # seconds between requests, minimum
BACKOFF_BASE = 120.0                # first cool-off: 2 minutes
BACKOFF_CAP = 3600.0                # never longer than an hour
DEFAULT_UA = "linux:sigint-social:v0.1 (set REDDIT_USER_AGENT)"


class RedditJSONAdapter:
    # class-level so pacing/backoff hold across instances within the worker
    _last_request = 0.0
    _backoff_until = 0.0
    _backoff_count = 0

    def __init__(self, get=None, sleep=time.sleep, now=time.monotonic):
        # injectable transport/clock so tests run without network or waiting
        self._get = get
        self._sleep = sleep
        self._now = now
        self.user_agent = os.environ.get("REDDIT_USER_AGENT") or DEFAULT_UA
        if self.user_agent == DEFAULT_UA:
            log.warning("REDDIT_USER_AGENT unset - using a placeholder UA; "
                        "set a descriptive one to avoid rate limiting")

    @property
    def name(self) -> str:
        return "reddit_json"

    # ── politeness machinery ────────────────────────────────────────────────

    def _pace(self) -> None:
        cls = RedditJSONAdapter
        wait = MIN_INTERVAL - (self._now() - cls._last_request)
        if wait > 0:
            self._sleep(wait)
        cls._last_request = self._now()

    def _check_backoff(self) -> None:
        cls = RedditJSONAdapter
        if self._now() < cls._backoff_until:
            remaining = int(cls._backoff_until - self._now())
            raise SocialBackoffError(
                f"reddit_json cooling off for {remaining}s more "
                f"(strike {cls._backoff_count})")

    def _enter_backoff(self, status: int, url: str) -> None:
        cls = RedditJSONAdapter
        cls._backoff_count += 1
        cooloff = min(BACKOFF_BASE * (2 ** (cls._backoff_count - 1)),
                      BACKOFF_CAP)
        cls._backoff_until = self._now() + cooloff
        log.warning("reddit_json HTTP %d on %s - backing off %.0fs "
                    "(strike %d), skipping rest of cycle",
                    status, url, cooloff, cls._backoff_count)
        raise SocialBackoffError(
            f"HTTP {status} from reddit - backing off {cooloff:.0f}s")

    def _clear_backoff(self) -> None:
        RedditJSONAdapter._backoff_count = 0
        RedditJSONAdapter._backoff_until = 0.0

    # ── the port ────────────────────────────────────────────────────────────

    def fetch_posts(self, subreddit: str, listing: str) -> list[SocialPost]:
        if listing not in LISTINGS:
            raise SocialSourceError(f"unknown listing {listing!r}")
        self._check_backoff()
        self._pace()

        url = f"{BASE}/r/{subreddit}/{listing}.json"
        params = {"limit": LISTINGS[listing], "raw_json": 1}
        get = self._get
        if get is None:
            import httpx
            get = lambda u, **kw: httpx.get(u, **kw)  # noqa: E731
        try:
            r = get(url, params=params, timeout=15.0,
                    headers={"User-Agent": self.user_agent},
                    follow_redirects=True)
        except Exception as e:  # transport trouble = soft failure, no retry
            log.info("reddit_json fetch failed (%s): %s", url, e)
            return []

        if r.status_code in (429, 403):
            self._enter_backoff(r.status_code, url)
        if r.status_code != 200:
            log.info("reddit_json HTTP %d on %s - treating as empty",
                     r.status_code, url)
            return []
        self._clear_backoff()

        try:
            children = r.json()["data"]["children"]
        except (KeyError, TypeError, ValueError):
            log.info("reddit_json unexpected payload shape on %s", url)
            return []
        return [p for p in (_parse_child(c) for c in children) if p]


def _parse_child(child: dict):
    from datetime import datetime, timezone
    d = child.get("data") or {}
    if not d.get("id"):
        return None
    return SocialPost(
        post_id=str(d["id"]),
        ts=datetime.fromtimestamp(float(d.get("created_utc") or 0),
                                  tz=timezone.utc),
        author=str(d.get("author") or "[deleted]"),
        title=str(d.get("title") or ""),
        body=str(d.get("selftext") or ""),
        score=int(d.get("score") or 0),
        num_comments=int(d.get("num_comments") or 0),
    )
