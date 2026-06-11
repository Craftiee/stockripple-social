"""Downstream tests: extraction, velocity math, aggregation, anomaly
conjunction, momentum write. No network, no LLM — sentiment is an injected
fake; db tests run on TEMP tables (see conftest.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from social import aggregate as agg
from social import fetch as sfetch
from social.extract import extract_tickers
from social.port import SocialPost
from social.sentiment import score_sentiment

UNIVERSE = {"SPY", "QQQ", "XLE", "JETS", "BZ=F"}


# ── extraction ───────────────────────────────────────────────────────────────

def test_cashtags_pass_even_outside_universe():
    assert extract_tickers("loading up on $NVDA and $smci", UNIVERSE) == \
        ["NVDA", "SMCI"]


def test_bare_symbols_require_universe_membership():
    out = extract_tickers("rotating SPY into NVDA and XLE", UNIVERSE)
    assert out == ["SPY", "XLE"]            # bare NVDA not in universe


def test_blacklist_guards_both_paths():
    assert extract_tickers("$DD on this YOLO play, ALL in", UNIVERSE) == []
    assert extract_tickers("did my DD: $JETS to the MOON", UNIVERSE) == ["JETS"]


def test_lowercase_and_word_boundaries():
    assert extract_tickers("buy $spy now", UNIVERSE) == ["SPY"]
    assert extract_tickers("DISPLAY this", UNIVERSE) == []   # SPY inside a word


# ── velocity z ───────────────────────────────────────────────────────────────

def test_velocity_z_needs_history():
    assert agg.velocity_z_score([1] * 10, 50) is None


def test_velocity_z_known_value():
    hist = [0, 2] * 24                       # mean 1
    z = agg.velocity_z_score(hist, 11)
    import statistics
    expected = (11 - 1) / statistics.stdev(hist)
    assert z == pytest.approx(expected, rel=1e-9)


def test_velocity_z_zero_variance_conventions():
    assert agg.velocity_z_score([1] * 48, 1) is None        # flat, no event
    assert agg.velocity_z_score([0] * 48, 20) == agg.VELOCITY_Z_CAP
    assert agg.velocity_z_score([0, 1000] * 24, 10 ** 6) == agg.VELOCITY_Z_CAP


# ── social score ─────────────────────────────────────────────────────────────

def test_social_score_neutral_and_bounds():
    assert agg.social_score(None, None, 0, 0, 0) == 50.0
    assert agg.social_score(10, 1, 6, 0, 0) == 100.0
    assert agg.social_score(10, -1, 0, 0, 6) == 0.0
    assert 0.0 <= agg.social_score(2.0, 0, 1, 3, 2) <= 100.0


def test_sentiment_skips_without_project(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    assert score_sentiment({"SPY": ["to the moon"]}) == {}
    assert score_sentiment({}) == {}


# ── db paths ─────────────────────────────────────────────────────────────────

HOUR = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(
    minute=0, second=0, microsecond=0)


def insert_post(db, ts, ticker_list, author="u1", pid=None):
    db.execute(text("""
        INSERT INTO social_posts (ts, subreddit, post_id, author, title,
                                  body, score, num_comments, tickers)
        VALUES (:ts, 'stocks', :pid, :a, 't', 'b', 1, 0, :tk)
    """), {"ts": ts, "pid": pid or f"p{ts.timestamp()}_{author}_{ticker_list}",
           "a": author, "tk": ticker_list})


def fake_sentiment(items, **kw):
    return {t: {"good": 4, "neutral": 1, "bad": 1, "direction": 1,
                "confidence": "high"} for t in items}


def test_upsert_posts_idempotent_and_refreshes(db):
    posts = [SocialPost("abc", HOUR, "u1", "title $SPY", "", 10, 2),
             SocialPost("def", HOUR, "u2", "title $QQQ", "", 5, 1)]
    assert sfetch.upsert_posts(db, "stocks", posts, set()) == 2
    bumped = [SocialPost("abc", HOUR, "u1", "title $SPY", "", 99, 7)]
    assert sfetch.upsert_posts(db, "stocks", bumped, set()) == 0   # no new rows
    row = db.execute(text(
        "SELECT score, num_comments, tickers FROM social_posts "
        "WHERE post_id='abc'")).first()
    assert row.score == 99 and row.num_comments == 7
    assert row.tickers == ["SPY"]            # extraction ran on the way in


def _seed_history(db, ticker, hours=30, per_hour=1):
    for i in range(1, hours + 1):
        for n in range(per_hour):
            insert_post(db, HOUR - timedelta(hours=i), [ticker],
                        author=f"hist{n}", pid=f"h_{ticker}_{i}_{n}")


def test_aggregate_alerts_on_two_signal_conjunction(db):
    _seed_history(db, "TST")
    for n in range(20):                      # spike: 20 mentions, 6 authors
        insert_post(db, HOUR + timedelta(minutes=n), ["TST"],
                    author=f"u{n % 6}", pid=f"spike{n}")
    stats = agg.aggregate_hour(db, HOUR, sentiment_fn=fake_sentiment,
                               universe={"TST"})
    assert stats["tickers"] == 1 and stats["alerts"] == 1
    row = db.execute(text(
        "SELECT mentions, unique_authors, velocity_z, sent_good, direction, "
        "social_score FROM social_agg WHERE ticker='TST'")).first()
    assert row.mentions == 20 and row.unique_authors == 6
    assert row.velocity_z is not None and row.velocity_z >= 3.0
    assert row.sent_good == 4 and row.direction == 1
    assert row.social_score > 50
    # momentum social sub-score written for universe ticker
    m = db.execute(text(
        "SELECT social, composite FROM momentum_scores WHERE ticker='TST'"
    )).first()
    assert m is not None and m.social == m.composite > 50


def test_single_author_spike_does_not_alert(db):
    _seed_history(db, "SOLO")
    for n in range(20):
        insert_post(db, HOUR + timedelta(minutes=n), ["SOLO"],
                    author="spammer", pid=f"solo{n}")
    stats = agg.aggregate_hour(db, HOUR, sentiment_fn=fake_sentiment,
                               universe=set())
    assert stats["alerts"] == 0              # velocity yes, authors no
    vz = db.execute(text(
        "SELECT velocity_z FROM social_agg WHERE ticker='SOLO'")).scalar()
    assert vz is not None and vz >= 3.0      # the other signal WAS firing


def test_alert_dedupes_per_ticker_hour(db):
    _seed_history(db, "TST")
    for n in range(20):
        insert_post(db, HOUR + timedelta(minutes=n), ["TST"],
                    author=f"u{n % 6}", pid=f"spike{n}")
    agg.aggregate_hour(db, HOUR, sentiment_fn=fake_sentiment, universe=set())
    agg.aggregate_hour(db, HOUR, sentiment_fn=fake_sentiment, universe=set())
    n = db.execute(text(
        "SELECT count(*) FROM alerts WHERE kind='social_anomaly'")).scalar()
    assert n == 1


def test_young_corpus_has_null_velocity_and_no_alert(db):
    for n in range(20):                      # spike with no baseline at all
        insert_post(db, HOUR + timedelta(minutes=n), ["NEW"],
                    author=f"u{n % 6}", pid=f"new{n}")
    stats = agg.aggregate_hour(db, HOUR, sentiment_fn=fake_sentiment,
                               universe=set())
    assert stats["alerts"] == 0
    vz = db.execute(text(
        "SELECT velocity_z FROM social_agg WHERE ticker='NEW'")).scalar()
    assert vz is None
