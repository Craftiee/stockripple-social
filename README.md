# stockripple-social

This is the **complete Reddit-facing module of a larger private, personal,
non-commercial market-research system**, published in full so Reddit Data API
application reviewers can audit every line of code that touches Reddit.

> **Review copy, not a runnable service.** The host application provides the
> database session, config table, and observability tables; those integration
> points are stubbed in [`social/host.py`](social/host.py) with notes on what
> the host supplies. Everything Reddit-touching — transport, pacing, backoff,
> parsing, storage, aggregation — is in this repo, unmodified except for
> import paths. The test suite runs standalone (see **Tests**).

## Purpose

Studying the relationship between **finance-subreddit discussion activity and
ticker price behavior**. The research output is hourly per-ticker aggregates:
mention counts, distinct-participant counts, a mention-velocity z-score, and
a coarse sentiment grade (good/neutral/bad). Those aggregates feed a personal
momentum score and an attention-anomaly detector. Nothing else is derived
from Reddit data.

## Data handling

- **Read-only, public posts only** — listings from public subreddits
  (`/new`, `/hot`); no voting, posting, messaging, or any write operation.
- **No user profiling.** Author names are used solely to count *distinct
  participants per ticker per hour* ([`aggregate.py`](social/aggregate.py),
  `hour_mentions()` — `count(DISTINCT p.author)`); no per-user records,
  histories, or profiles are built.
- **No AI training, no redistribution.** Post text is not used to train
  models and is not republished anywhere; an LLM *labels* hourly sentiment
  from short excerpts ([`sentiment.py`](social/sentiment.py)) and only the
  aggregate counts are retained as the research output.
- Posts are stored privately and solely to compute the aggregates above.

## Politeness contract (with pointers to the exact code)

All in [`social/adapters/reddit_json.py`](social/adapters/reddit_json.py):

| guarantee | where |
|---|---|
| ≥ 2 seconds between requests, strictly sequential — never parallel | `MIN_INTERVAL`, `_pace()` |
| descriptive User-Agent from `REDDIT_USER_AGENT` env (warns if unset) | `__init__` |
| HTTP 429/403 → exponential cool-off, 2 min doubling, capped at 1 h | `_enter_backoff()` |
| during cool-off every call raises immediately — the fetch cycle skips its remainder and logs; **no retries inside a cycle, ever** | `_check_backoff()`, [`fetch.py`](social/fetch.py) `run_social_fetch_sync()` |
| missing/empty data is a soft failure (`[]`), not an error to retry | `fetch_posts()` |

The politeness behaviors are pinned by unit tests
([`tests/test_fetch_layer.py`](tests/test_fetch_layer.py)): pacing, backoff
entry, exponential doubling, in-cool-off short-circuit, and recovery.

## Volume

Default configuration: 5 subreddits × 2 listings = **10 requests per fetch
cycle** (~16–20 if the subreddit list grows), **2 cycles per hour** — well
under **2 requests/minute sustained**, with instantaneous spacing never
tighter than one request per 2 seconds.

## Two adapters, one port

The fetch layer is a port (`SocialSourcePort`,
[`social/port.py`](social/port.py)) with two interchangeable adapters:

- **`reddit_oauth`** ([code](social/adapters/reddit_oauth.py)) — the
  **intended production path**: the standard OAuth/praw client, fully built
  and tested, pending Reddit's approval of the Data API application. It is
  selected only when configured *and* credentials exist in the environment.
- **`reddit_json`** ([code](social/adapters/reddit_json.py)) — the public
  read-only JSON listings, carrying the same politeness guarantees, used in
  the interim. The swap to OAuth is a one-row configuration change; nothing
  downstream knows which adapter ran.

## Tests

Pure tests (extraction, velocity math, scoring, adapter politeness) run with
just `pip install -r requirements.txt && pip install pytest`:

```bash
pytest -q                 # db-backed tests skip without TEST_DATABASE_URL
```

The db-backed tests (aggregation, anomaly conjunction, idempotent upserts)
bootstrap their entire schema as TEMP tables, so any reachable PostgreSQL
works and is left untouched:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/postgres
pytest -q
```

## License

MIT — see [LICENSE](LICENSE).
