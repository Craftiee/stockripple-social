"""Social fetch cycle (ingest worker): subreddits -> port -> social_posts.

Sequential by design — the reddit_json adapter enforces >=2s pacing, and a
SocialBackoffError aborts the remainder of the cycle (job_log ok=false);
whatever was fetched before the abort is kept. Posts upsert on (post_id, ts)
so the new/hot overlap and refetches just refresh score/num_comments.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import text

from social.host import SessionLocal
from social.extract import extract_tickers
from social.port import SocialBackoffError, get_source

log = logging.getLogger("sigint.social.fetch")

DEFAULT_SUBREDDITS = ["wallstreetbets", "stocks", "investing",
                      "StockMarket", "options"]
LISTING_ORDER = ("new", "hot")


def seed_subreddits(db) -> None:
    db.execute(text("""
        INSERT INTO config (key, value) VALUES ('social_subreddits', :v)
        ON CONFLICT (key) DO NOTHING
    """), {"v": json.dumps(DEFAULT_SUBREDDITS)})
    db.commit()


def load_subreddits(db) -> list[str]:
    v = db.execute(text(
        "SELECT value FROM config WHERE key='social_subreddits'")).scalar()
    return list(v) if v else list(DEFAULT_SUBREDDITS)


def load_universe_set(db) -> set[str]:
    from social.host import load_universe
    return set(load_universe(db))


def upsert_posts(db, subreddit: str, posts, universe: set[str]) -> int:
    """Insert/refresh posts; returns number of NEW rows."""
    new = 0
    for p in posts:
        tickers = extract_tickers(f"{p.title}\n{p.body}", universe)
        inserted = db.execute(text("""
            INSERT INTO social_posts
              (ts, subreddit, post_id, author, title, body,
               score, num_comments, tickers)
            VALUES (:ts, :sub, :pid, :author, :title, :body,
                    :score, :nc, :tickers)
            ON CONFLICT (post_id, ts) DO UPDATE SET
              score = EXCLUDED.score,
              num_comments = EXCLUDED.num_comments,
              fetched_at = now()
            RETURNING (xmax = 0) AS inserted
        """), {"ts": p.ts, "sub": subreddit, "pid": p.post_id,
               "author": p.author, "title": p.title[:1000],
               "body": p.body[:8000], "score": p.score,
               "nc": p.num_comments, "tickers": tickers}).scalar()
        new += bool(inserted)
    return new


def run_social_fetch_sync() -> dict:
    db = SessionLocal()
    stats = {"subreddits": 0, "posts": 0, "new": 0, "aborted": None}
    try:
        seed_subreddits(db)
        subs = load_subreddits(db)
        universe = load_universe_set(db)
        source = get_source(db)
        try:
            for sub in subs:
                for listing in LISTING_ORDER:
                    posts = source.fetch_posts(sub, listing)
                    stats["posts"] += len(posts)
                    stats["new"] += upsert_posts(db, sub, posts, universe)
                    db.commit()
                stats["subreddits"] += 1
        except SocialBackoffError as e:
            stats["aborted"] = str(e)
            db.execute(text("""
                INSERT INTO job_log (job, ok, detail)
                VALUES ('social_fetch', false, :d)
            """), {"d": json.dumps({"error": str(e), "source": source.name,
                                    **{k: stats[k] for k in
                                       ("subreddits", "posts", "new")}})})
            db.commit()
            log.warning("social fetch aborted (backoff): %s", e)
            return stats

        db.execute(text("""
            INSERT INTO job_log (job, ok, detail)
            VALUES ('social_fetch', true, :d)
        """), {"d": json.dumps({"source": source.name,
                                **{k: stats[k] for k in
                                   ("subreddits", "posts", "new")}})})
        db.commit()
        log.info("social fetch: %s", stats)
        return stats
    finally:
        db.close()


async def run_social_fetch(ctx) -> dict:
    import asyncio
    stats = await asyncio.to_thread(run_social_fetch_sync)
    if stats.get("posts") and not stats.get("aborted") \
            and ctx and ctx.get("redis"):
        await ctx["redis"].enqueue_job(
            "social_aggregate", _queue_name="arq:pipeline")
    return stats
