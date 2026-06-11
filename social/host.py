"""Host-application integration points — STUBS for review purposes.

In the private host system these are real implementations; the social module
only consumes their narrow interfaces, so review-relevant behavior lives
entirely in this repo. What the host provides:

  SessionLocal()      SQLAlchemy session factory (PostgreSQL/TimescaleDB).
                      The module's SQL — social_posts/social_agg upserts,
                      config-table reads, job_log writes, alerts inserts —
                      is all visible in fetch.py / aggregate.py / port.py.
  load_universe(db)   The market-ticker universe (exchange symbols tracked
                      by the wider system). Used only to (a) gate bare-word
                      ticker extraction and (b) decide which tickers feed
                      the momentum score. No Reddit data flows into it.
  strip_code_fences() Tiny helper shared with another module; real
                      implementation inlined below since it is review-
                      relevant (it post-processes LLM JSON output).

The test suite (tests/) does not use these stubs: it builds its own
sessions from TEST_DATABASE_URL and creates the schema as TEMP tables.
"""
from __future__ import annotations


def SessionLocal():
    raise RuntimeError(
        "SessionLocal is provided by the host application; this public "
        "review copy is not meant to run standalone (see README.md)")


def load_universe(db) -> list[str]:
    raise RuntimeError(
        "load_universe is provided by the host application; tests inject "
        "an explicit universe instead")


def strip_code_fences(s: str) -> str:
    """Models sometimes wrap JSON in ```json fences despite instructions.
    (Verbatim copy of the host implementation.)"""
    s = s.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()
