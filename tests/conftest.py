"""Test session factory. DB-backed tests need any reachable PostgreSQL:

    export TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/db
    pytest -q

All schema is created as session-scoped TEMP tables (mirroring the host
migrations), so the target database is never written to and needs no setup.
Without TEST_DATABASE_URL the db-backed tests skip; the pure tests
(extraction, velocity math, scoring, adapter politeness) always run.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DB_URL = os.environ.get("TEST_DATABASE_URL")

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(DB_URL, pool_pre_ping=True)
    return _engine


# TEMP-table mirror of the host schema (migrations 0001/0004), with the
# host's confidence_level enum relaxed to text so any plain Postgres works.
SCHEMA = """
CREATE TEMP TABLE config (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TEMP TABLE job_log (
  id bigserial, ts timestamptz NOT NULL DEFAULT now(),
  job text NOT NULL, input_id text, output_id text, code_version text,
  ok boolean NOT NULL, detail jsonb, PRIMARY KEY (id, ts)
);
CREATE TEMP TABLE alerts (
  id bigserial, ts timestamptz NOT NULL DEFAULT now(),
  kind text NOT NULL, payload jsonb NOT NULL,
  acked boolean NOT NULL DEFAULT false, PRIMARY KEY (id, ts)
);
CREATE TEMP TABLE momentum_scores (
  ts timestamptz NOT NULL, ticker text NOT NULL,
  catalyst double precision, news double precision,
  price double precision, social double precision,
  composite double precision NOT NULL, PRIMARY KEY (ticker, ts)
);
CREATE TEMP TABLE social_posts (
  id bigserial, ts timestamptz NOT NULL,
  fetched_at timestamptz NOT NULL DEFAULT now(),
  subreddit text NOT NULL, post_id text NOT NULL, author text,
  title text NOT NULL DEFAULT '', body text NOT NULL DEFAULT '',
  score integer NOT NULL DEFAULT 0, num_comments integer NOT NULL DEFAULT 0,
  tickers text[] NOT NULL DEFAULT '{}',
  PRIMARY KEY (id, ts), UNIQUE (post_id, ts)
);
CREATE TEMP TABLE social_agg (
  ts timestamptz NOT NULL, ticker text NOT NULL,
  mentions integer NOT NULL, unique_authors integer NOT NULL,
  velocity_z double precision,
  sent_good integer, sent_neutral integer, sent_bad integer,
  direction smallint CHECK (direction IN (-1, 0, 1)),
  confidence text, social_score double precision,
  PRIMARY KEY (ticker, ts)
);
"""

TABLES = ("social_agg", "social_posts", "momentum_scores",
          "alerts", "job_log", "config")


@pytest.fixture
def db():
    if not DB_URL:
        pytest.skip("TEST_DATABASE_URL not set - db-backed test skipped")
    Session = sessionmaker(bind=_get_engine(), autoflush=False,
                           expire_on_commit=False)
    s = Session()
    for t in TABLES:  # pooled connections may still hold previous temps
        s.execute(text(f"DROP TABLE IF EXISTS pg_temp.{t}"))
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            s.execute(text(stmt))
    yield s
    s.rollback()
    for t in TABLES:
        s.execute(text(f"DROP TABLE IF EXISTS pg_temp.{t}"))
    s.commit()
    s.close()
