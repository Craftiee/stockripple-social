"""Batched LLM sentiment for the hourly social aggregate.

One Vertex call per aggregation cycle covers every qualifying ticker (cheap
model, JSON-only) — never one call per post or per ticker. Skips gracefully
to {} when GOOGLE_CLOUD_PROJECT is unset or the call fails: aggregation then
writes counts/velocity without sentiment columns rather than failing the
cycle. Model from config social_sentiment_model (default gemini-2.5-flash,
same family as the classifier's secondary).
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("sigint.social.sentiment")

DEFAULT_MODEL = "gemini-2.5-flash"
MAX_TEXTS_PER_TICKER = 5
TEXT_CLIP = 280

PROMPT_HEAD = """You label retail-investor sentiment from Reddit posts.
For EACH ticker below, read its posts and respond with ONLY a JSON object:
{"<TICKER>": {"good": <posts bullish on the ticker>,
              "neutral": <posts neutral/unclear>,
              "bad": <posts bearish on the ticker>,
              "direction": -1 | 0 | 1  (net crowd lean),
              "confidence": "high" | "medium" | "low"},
 ...one entry per ticker...}

Counts must sum to the number of posts shown for that ticker.

POSTS BY TICKER:
"""


def score_sentiment(items: dict[str, list[str]],
                    model: str = DEFAULT_MODEL,
                    temperature: float = 0.2) -> dict[str, dict]:
    """items: ticker -> sample texts. Returns ticker -> {good, neutral, bad,
    direction, confidence}; {} on any failure (soft — sentiment is optional)."""
    if not items:
        return {}
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        log.warning("GOOGLE_CLOUD_PROJECT unset - skipping social sentiment")
        return {}

    parts = [PROMPT_HEAD]
    for ticker, texts in items.items():
        parts.append(f"\n## {ticker}")
        for t in texts[:MAX_TEXTS_PER_TICKER]:
            parts.append(f"- {t[:TEXT_CLIP]}")
    prompt = "\n".join(parts)

    try:
        from google import genai
        from google.genai import types
        try:
            client = genai.Client(
                enterprise=True, project=project,
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
        except TypeError:  # google-genai 1.x
            client = genai.Client(
                vertexai=True, project=project,
                location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
        resp = client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                response_mime_type="application/json"))
        from social.host import strip_code_fences
        data = json.loads(strip_code_fences(resp.text or ""))
    except Exception as e:  # noqa: BLE001 — sentiment is best-effort
        log.warning("social sentiment failed (%s) - continuing without", e)
        return {}

    out = {}
    for ticker in items:
        d = data.get(ticker)
        if not isinstance(d, dict):
            continue
        try:
            direction = int(d.get("direction", 0))
        except (TypeError, ValueError):
            direction = 0
        out[ticker] = {
            "good": max(0, int(d.get("good", 0) or 0)),
            "neutral": max(0, int(d.get("neutral", 0) or 0)),
            "bad": max(0, int(d.get("bad", 0) or 0)),
            "direction": direction if direction in (-1, 0, 1) else 0,
            "confidence": d.get("confidence")
            if d.get("confidence") in ("high", "medium", "low") else "low",
        }
    return out
