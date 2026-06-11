"""Ticker extraction from post text (pure — no db, no network).

Two admission paths, per the module spec:
  * cashtags ($NVDA): admitted even when OUTSIDE the market universe — the
    Sympathy Spillover detector specifically hunts attention on tickers the
    graph does not track yet.
  * bare uppercase symbols (NVDA): admitted only when they exact-match the
    market universe (nodes.tickers ∪ watchlist) — bare matching without that
    gate drowns in ordinary capitalized words.
Both paths run through the blacklist of finance slang / common words that
collide with real symbols (DD, CEO, YOLO, ALL, BIG ...). Known tradeoff:
the blacklist also suppresses the real tickers ALL/BIG/ON/IT — acceptable;
false positives here poison every downstream aggregate.
"""
from __future__ import annotations

import re

CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})(?![A-Za-z0-9])")
BARE_RE = re.compile(r"\b([A-Z]{2,5})\b")

BLACKLIST = {
    # pronouns / common words that look like symbols
    "A", "I", "ALL", "ARE", "BE", "BIG", "GO", "IT", "NOW", "ON", "ONE",
    "OR", "OUT", "SO", "TOP", "UP", "ANY", "CAN", "FOR", "HAS", "NEW",
    "SEE", "VERY", "REAL", "FREE", "FULL", "NEXT", "LAST", "BEST", "GOOD",
    "BAD", "DAY", "WEEK", "YEAR", "RED", "EVER", "LIFE", "PLAN", "SAVE",
    # finance / reddit slang
    "DD", "CEO", "CFO", "CTO", "IMO", "IMHO", "YOLO", "FOMO", "ATH", "IPO",
    "ETF", "FED", "GDP", "CPI", "PPI", "FOMC", "AI", "US", "USA", "UK",
    "EU", "LOL", "WSB", "FYI", "PSA", "TLDR", "EDIT", "BUY", "SELL",
    "HOLD", "CALL", "PUT", "PUTS", "CALLS", "MOON", "APE", "APES", "HODL",
    "RIP", "USD", "EUR", "NYSE", "SEC", "IRS", "API", "EOD", "AH", "PM",
    "EPS", "PE", "ITM", "OTM", "ATM", "IV", "TA", "FA", "CASH", "DEBT",
    "GAIN", "LOSS", "RISK", "NEWS", "HUGE", "PUMP", "DUMP", "BULL", "BEAR",
    "LONG", "SHORT", "EDGE", "PLAY", "BETS", "WIN", "LOSE", "STOP",
}


def extract_tickers(text: str, universe: set[str]) -> list[str]:
    """Tickers mentioned in `text`, sorted. `universe` gates bare symbols
    only; cashtags pass on their own (minus blacklist)."""
    found: set[str] = set()
    for m in CASHTAG_RE.finditer(text):
        t = m.group(1).upper()
        if t not in BLACKLIST:
            found.add(t)
    for m in BARE_RE.finditer(text):
        t = m.group(1)
        if t in universe and t not in BLACKLIST:
            found.add(t)
    return sorted(found)
