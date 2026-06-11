# Module: Social / Reddit Sentiment — BUILT (fetch currently 403-blocked)
Top stock subreddits -> ticker extraction (cashtags + universe-validated symbols,
blacklist-guarded) -> hourly per-ticker aggregation: mention velocity z-score,
unique authors, batched-LLM sentiment (good/neutral/bad), direction, confidence.
Anomaly alert = two-signal conjunction (velocity_z AND unique authors). Feeds the
momentum social sub-score and the Sympathy Spillover detector's attention half.

## Fetch layer: port + two adapters (amended)

Reddit ended self-serve OAuth app registration in **Nov 2025 (Responsible
Builder Policy)** — API credentials now require an approved application. The
fetch layer is therefore a port (`SocialSourcePort` in
`app/workers/social/port.py`, mirroring the classifier's port pattern) with
two adapters; everything downstream is adapter-blind, so the swap is a
one-row config update.

| adapter | status | how it reads Reddit |
|---|---|---|
| `reddit_json` | **DEFAULT, in use now** | public read-only JSON listings (`/r/{sub}/new.json` limit 50, `/hot.json` limit 25), no credentials |
| `reddit_oauth` | built, dormant | original OAuth/praw implementation; activates only when `social_source='reddit_oauth'` **and** `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` are set (premature config flip degrades back to `reddit_json`) |

`reddit_json` manners (non-negotiable): descriptive User-Agent from
`REDDIT_USER_AGENT`; sequential requests >= 2s apart; on HTTP 429/403 it
raises `SocialBackoffError` and cools off exponentially (2 min doubling,
capped at 1h) so the cycle skips cleanly and logs to job_log — never hammer.
Empty/missing listings are a soft failure (`[]`), not an error.

Flip to OAuth on approval day: add the creds to `.env`, then
`UPDATE config SET value = to_jsonb(CAST('reddit_oauth' AS text)) WHERE key='social_source';`

Source mode is visible at `GET /v1/social/status` (`source`,
`configured_source`, `oauth_creds_present`, last fetch, 24h failures,
posts_24h, last aggregate, anomalies_24h). Trending: `GET /v1/social/trending`.

## Downstream pipeline (built 2026-06-11)

- **Fetch cycle** (ingest worker, :12/:42): subreddits from config
  `social_subreddits` (wallstreetbets, stocks, investing, StockMarket,
  options), new+hot per sub through the port, ticker extraction on the way
  in, upsert into `social_posts` (refetch refreshes score/num_comments).
- **Extraction**: cashtags pass even outside the universe (Sympathy Spillover
  hunts untracked tickers); bare uppercase symbols require universe
  membership; both blacklist-guarded (`workers/social/extract.py`).
- **Hourly aggregation** (pipeline worker, enqueued after each fetch):
  `social_agg` per ticker-hour — mentions, unique authors, velocity_z
  (baseline = trailing 168h with missing hours as ZERO; NULL under 24h of
  corpus; zero-variance spike maps to cap 10), batched-LLM sentiment
  (one gemini-2.5-flash call per cycle, top 15 tickers with >=3 mentions,
  skips without GOOGLE_CLOUD_PROJECT), social_score 0-100.
- **Anomaly** = strict conjunction: velocity_z >= `social_velocity_z_alert`
  (3.0) AND unique_authors >= `social_min_authors` (5) → `alerts` row
  (kind social_anomaly), deduped per ticker-hour.
- **Momentum**: social_score writes `momentum_scores.social` for universe
  tickers; composite = mean of available sub-scores.

## Current status (2026-06-11)

Reddit returns **403 on all unauthenticated JSON listings from this host** —
tested with descriptive AND browser User-Agents, www and old.reddit.com: the
block is at the IP/edge level, not our client etiquette. The adapter behaves
exactly per spec (exponential cool-off, cycle skip, job_log ok=false, never
hammers) and the cron self-heals if the block lifts. The durable path is the
OAuth application — when approved, flip per the runbook above.
