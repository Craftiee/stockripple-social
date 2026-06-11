"""Hourly per-ticker aggregation + anomaly conjunction (pipeline worker).

Each run aggregates the current hour AND re-finalizes the previous hour
(idempotent upserts), so an hour reaches its final state shortly after it
ends without a separate finalizer job.

velocity_z: this hour's mentions z-scored against the trailing window of
hourly mention counts WITH MISSING HOURS COUNTED AS ZERO — most ticker-hours
have no row, and skipping them would wildly understate the baseline. NULL
until the corpus is old enough (MIN_HISTORY_HOURS); a zero-variance baseline
with a spike maps to the cap rather than infinity.

Anomaly alert = strict two-signal conjunction: velocity_z >= social_velocity_z_alert
AND unique_authors >= social_min_authors. One author spamming can move
velocity but not the author count; either signal alone never alerts.
Alerts dedupe per (ticker, hour) into the alerts table.

social_score (0-100, 50 neutral) feeds momentum_scores.social for universe
tickers (daily row = the hour's date):

    balance = (good - bad) / labeled            # crowd lean, -1..1
    attention = clamp(velocity_z, 0, 3) / 3     # how unusual the volume is
    raw = 0.6*balance + 0.4*attention*lean      # attention amplifies the lean
    score = clamp(50 + 50*raw, 0, 100)          # lean = direction or sign(balance)

composite becomes the mean of the available sub-scores (price, social).
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import text

from social.host import SessionLocal

log = logging.getLogger("sigint.social.agg")

VELOCITY_WINDOW_HOURS = 168       # 7 days of baseline
MIN_HISTORY_HOURS = 24            # corpus younger than this -> velocity_z NULL
VELOCITY_Z_CAP = 10.0
SENTIMENT_MIN_MENTIONS = 3        # LLM only for tickers with real volume
SENTIMENT_MAX_TICKERS = 15        # per batched call


def cfg(db, key: str, default):
    v = db.execute(text("SELECT value FROM config WHERE key=:k"),
                   {"k": key}).scalar()
    return default if v is None else v


# ── pure pieces (unit-tested without a db) ───────────────────────────────────

def velocity_z_score(history: Sequence[int], current: int,
                     min_history: int = MIN_HISTORY_HOURS,
                     cap: float = VELOCITY_Z_CAP) -> Optional[float]:
    """history: hourly mention counts (zeros included) for the baseline
    window, oldest first, EXCLUDING the current hour."""
    if len(history) < min_history:
        return None
    mean = statistics.fmean(history)
    std = statistics.stdev(history)
    if std == 0:
        return None if current == mean else cap
    return min((current - mean) / std, cap)


def social_score(velocity_z: Optional[float], direction: Optional[int],
                 good: int, neutral: int, bad: int) -> float:
    labeled = good + neutral + bad
    balance = (good - bad) / labeled if labeled else 0.0
    lean = direction if direction in (-1, 0, 1) and direction is not None \
        else (1 if balance > 0 else -1 if balance < 0 else 0)
    attention = min(max(velocity_z or 0.0, 0.0), 3.0) / 3.0
    raw = 0.6 * balance + 0.4 * attention * lean
    return float(min(max(50.0 + 50.0 * raw, 0.0), 100.0))


# ── db pieces ────────────────────────────────────────────────────────────────

def hour_mentions(db, hour: datetime) -> list:
    """(ticker, mentions, unique_authors, sample_texts) for one hour bucket."""
    return db.execute(text("""
        SELECT t.ticker,
               count(*) AS mentions,
               count(DISTINCT p.author) AS unique_authors,
               (array_agg(p.title ORDER BY p.score DESC))[1:8] AS samples
        FROM social_posts p, unnest(p.tickers) AS t(ticker)
        WHERE p.ts >= :h AND p.ts < :h + INTERVAL '1 hour'
        GROUP BY t.ticker
    """), {"h": hour}).all()


def mention_history(db, ticker: str, hour: datetime,
                    window_hours: int) -> list[int]:
    """Hourly counts for the baseline window before `hour`, zero-filled, and
    clipped to corpus age (hours before the first post carry no signal)."""
    start = hour - timedelta(hours=window_hours)
    rows = dict(db.execute(text("""
        SELECT date_trunc('hour', p.ts) AS h, count(*)
        FROM social_posts p, unnest(p.tickers) AS t(ticker)
        WHERE t.ticker = :t AND p.ts >= :s AND p.ts < :h
        GROUP BY 1
    """), {"t": ticker, "s": start, "h": hour}).all())
    oldest = db.execute(text(
        "SELECT min(ts) FROM social_posts")).scalar()
    if oldest is None:
        return []
    corpus_start = max(start, oldest.replace(minute=0, second=0, microsecond=0))
    out, cur = [], corpus_start
    while cur < hour:
        out.append(int(rows.get(cur, 0)))
        cur += timedelta(hours=1)
    return out


def upsert_agg(db, hour: datetime, ticker: str, mentions: int,
               unique_authors: int, vz: Optional[float],
               sent: Optional[dict]) -> None:
    s = sent or {}
    score = social_score(vz, s.get("direction"), s.get("good", 0),
                         s.get("neutral", 0), s.get("bad", 0))
    db.execute(text("""
        INSERT INTO social_agg
          (ts, ticker, mentions, unique_authors, velocity_z,
           sent_good, sent_neutral, sent_bad, direction, confidence,
           social_score)
        VALUES (:ts, :t, :m, :ua, :vz, :g, :n, :b, :dir, :conf, :score)
        ON CONFLICT (ticker, ts) DO UPDATE SET
          mentions = EXCLUDED.mentions,
          unique_authors = EXCLUDED.unique_authors,
          velocity_z = EXCLUDED.velocity_z,
          sent_good = COALESCE(EXCLUDED.sent_good, social_agg.sent_good),
          sent_neutral = COALESCE(EXCLUDED.sent_neutral, social_agg.sent_neutral),
          sent_bad = COALESCE(EXCLUDED.sent_bad, social_agg.sent_bad),
          direction = COALESCE(EXCLUDED.direction, social_agg.direction),
          confidence = COALESCE(EXCLUDED.confidence, social_agg.confidence),
          social_score = EXCLUDED.social_score
    """), {"ts": hour, "t": ticker, "m": mentions, "ua": unique_authors,
           "vz": vz, "g": s.get("good"), "n": s.get("neutral"),
           "b": s.get("bad"), "dir": s.get("direction"),
           "conf": s.get("confidence"), "score": score})


def maybe_alert(db, hour: datetime, ticker: str, mentions: int,
                unique_authors: int, vz: Optional[float],
                z_alert: float, min_authors: int) -> bool:
    """Two-signal conjunction, deduped per (ticker, hour)."""
    if vz is None or vz < z_alert or unique_authors < min_authors:
        return False
    exists = db.execute(text("""
        SELECT 1 FROM alerts
        WHERE kind = 'social_anomaly'
          AND payload->>'ticker' = :t AND payload->>'hour' = :h
        LIMIT 1
    """), {"t": ticker, "h": hour.isoformat()}).scalar()
    if exists:
        return False
    db.execute(text("""
        INSERT INTO alerts (kind, payload) VALUES ('social_anomaly', :p)
    """), {"p": json.dumps({
        "ticker": ticker, "hour": hour.isoformat(), "mentions": mentions,
        "unique_authors": unique_authors, "velocity_z": round(vz, 2)})})
    log.warning("SOCIAL ANOMALY %s @ %s: z=%.1f authors=%d",
                ticker, hour.isoformat(), vz, unique_authors)
    return True


def write_momentum_social(db, hour: datetime, ticker: str,
                          universe: set[str], score: float) -> None:
    if ticker not in universe:
        return
    day = hour.replace(hour=0, minute=0, second=0, microsecond=0)
    db.execute(text("""
        INSERT INTO momentum_scores (ticker, ts, social, composite)
        VALUES (:t, :d, :s, :s)
        ON CONFLICT (ticker, ts) DO UPDATE SET
          social = EXCLUDED.social,
          composite = (COALESCE(momentum_scores.price, EXCLUDED.social)
                       + EXCLUDED.social) / 2.0
    """), {"t": ticker, "d": day, "s": score})


# ── the cycle ────────────────────────────────────────────────────────────────

def aggregate_hour(db, hour: datetime, *, sentiment_fn=None,
                   universe: Optional[set] = None) -> dict:
    from social.host import load_universe
    from social.sentiment import score_sentiment

    sentiment_fn = sentiment_fn or score_sentiment
    universe = universe if universe is not None else set(load_universe(db))
    z_alert = float(cfg(db, "social_velocity_z_alert", 3.0))
    min_authors = int(cfg(db, "social_min_authors", 5))
    window = int(cfg(db, "social_velocity_window_hours", VELOCITY_WINDOW_HOURS))
    model = cfg(db, "social_sentiment_model", None)

    rows = hour_mentions(db, hour)
    stats = {"hour": hour.isoformat(), "tickers": len(rows), "alerts": 0}
    if not rows:
        return stats

    eligible = sorted((r for r in rows if r.mentions >= SENTIMENT_MIN_MENTIONS),
                      key=lambda r: -r.mentions)[:SENTIMENT_MAX_TICKERS]
    kwargs = {"model": model} if model else {}
    sentiments = sentiment_fn(
        {r.ticker: list(r.samples or []) for r in eligible}, **kwargs)

    for r in rows:
        vz = velocity_z_score(mention_history(db, r.ticker, hour, window),
                              r.mentions)
        sent = sentiments.get(r.ticker)
        upsert_agg(db, hour, r.ticker, r.mentions, r.unique_authors, vz, sent)
        stats["alerts"] += maybe_alert(db, hour, r.ticker, r.mentions,
                                       r.unique_authors, vz,
                                       z_alert, min_authors)
        s = sent or {}
        write_momentum_social(
            db, hour, r.ticker, universe,
            social_score(vz, s.get("direction"), s.get("good", 0),
                         s.get("neutral", 0), s.get("bad", 0)))
    db.commit()
    return stats


def run_social_aggregate_sync() -> dict:
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        this_hour = now.replace(minute=0, second=0, microsecond=0)
        prev = aggregate_hour(db, this_hour - timedelta(hours=1))
        cur = aggregate_hour(db, this_hour)
        detail = {"previous": prev, "current": cur}
        db.execute(text("""
            INSERT INTO job_log (job, ok, detail)
            VALUES ('social_aggregate', true, :d)
        """), {"d": json.dumps(detail)})
        db.commit()
        log.info("social aggregate: %s", detail)
        return detail
    finally:
        db.close()
