"""Fetch-layer tests: adapter politeness contract + source selection.
No network: the JSON adapter takes injectable transport/clock/sleep."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from social import port
from social.adapters import reddit_json, reddit_oauth
from social.port import SocialBackoffError


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"data": {"children": []}}

    def json(self):
        return self._payload


class FakeClock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def make_adapter(responses, clock=None):
    """Adapter with canned responses and a virtual clock; resets the
    class-level pacing/backoff state so tests are order-independent."""
    reddit_json.RedditJSONAdapter._last_request = 0.0
    reddit_json.RedditJSONAdapter._backoff_until = 0.0
    reddit_json.RedditJSONAdapter._backoff_count = 0
    clock = clock or FakeClock()
    it = iter(responses)

    def get(url, **kw):
        return next(it)

    a = reddit_json.RedditJSONAdapter(get=get, sleep=clock.sleep,
                                      now=clock.now)
    return a, clock


def listing_payload(*posts):
    return {"data": {"children": [
        {"data": {"id": pid, "created_utc": 1765000000 + i,
                  "author": f"u{i}", "title": f"t{i}", "selftext": "b",
                  "score": 10 + i, "num_comments": i}}
        for i, pid in enumerate(posts)]}}


# ── reddit_json adapter ──────────────────────────────────────────────────────

def test_json_adapter_parses_posts():
    a, _ = make_adapter([FakeResponse(200, listing_payload("aa", "bb"))])
    posts = a.fetch_posts("stocks", "new")
    assert [p.post_id for p in posts] == ["aa", "bb"]
    assert posts[0].author == "u0" and posts[1].score == 11
    assert posts[0].ts.tzinfo is not None


def test_json_adapter_paces_sequential_requests():
    a, clock = make_adapter([FakeResponse(200, listing_payload("a")),
                             FakeResponse(200, listing_payload("b"))])
    a.fetch_posts("stocks", "new")
    a.fetch_posts("stocks", "hot")
    # second call must have slept to honor the >=2s spacing
    assert clock.slept and clock.slept[-1] == pytest.approx(2.0, abs=0.01)


def test_json_adapter_429_backs_off_and_skips_cycle():
    a, clock = make_adapter([FakeResponse(429)])
    with pytest.raises(SocialBackoffError):
        a.fetch_posts("stocks", "new")
    # still cooling off: immediate raise, no further requests attempted
    with pytest.raises(SocialBackoffError):
        a.fetch_posts("stocks", "hot")
    # cool-off is exponential from 2 minutes
    assert reddit_json.RedditJSONAdapter._backoff_until \
        == pytest.approx(clock.now() + 120.0, abs=5)


def test_json_adapter_backoff_doubles_then_clears():
    a, clock = make_adapter([FakeResponse(403), FakeResponse(403),
                             FakeResponse(200, listing_payload("ok"))])
    with pytest.raises(SocialBackoffError):
        a.fetch_posts("stocks", "new")
    clock.t = reddit_json.RedditJSONAdapter._backoff_until + 1
    with pytest.raises(SocialBackoffError):
        a.fetch_posts("stocks", "new")
    assert reddit_json.RedditJSONAdapter._backoff_count == 2     # 4 min strike
    clock.t = reddit_json.RedditJSONAdapter._backoff_until + 1
    posts = a.fetch_posts("stocks", "new")                       # recovers
    assert [p.post_id for p in posts] == ["ok"]
    assert reddit_json.RedditJSONAdapter._backoff_count == 0     # cleared


def test_json_adapter_soft_failures_return_empty():
    a, _ = make_adapter([FakeResponse(404)])
    assert a.fetch_posts("stocks", "new") == []
    a, _ = make_adapter([FakeResponse(200, {"weird": "shape"})])
    assert a.fetch_posts("stocks", "new") == []


def test_json_adapter_user_agent_from_env(monkeypatch):
    monkeypatch.setenv("REDDIT_USER_AGENT", "linux:example-app:v1 (by u/example)")
    a, _ = make_adapter([])
    assert a.user_agent == "linux:example-app:v1 (by u/example)"


# ── source selection ─────────────────────────────────────────────────────────

def set_source(db, mode):
    db.execute(text("""
        INSERT INTO config (key, value)
        VALUES ('social_source', to_jsonb(CAST(:v AS text)))
        ON CONFLICT (key) DO UPDATE SET value = to_jsonb(CAST(:v AS text))
    """), {"v": mode})


def test_default_source_is_reddit_json(db, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    src = port.get_source(db)
    assert src.name == "reddit_json"
    assert port.resolved_source_name(db) == "reddit_json"


def test_oauth_config_without_creds_falls_back(db, monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    set_source(db, "reddit_oauth")
    assert port.get_source(db).name == "reddit_json"
    assert port.resolved_source_name(db) == "reddit_json"


def test_oauth_selected_with_config_and_creds(db, monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "test-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "test-secret")
    set_source(db, "reddit_oauth")
    src = port.get_source(db)
    assert isinstance(src, reddit_oauth.RedditOAuthAdapter)
    assert port.resolved_source_name(db) == "reddit_oauth"


def test_unknown_source_degrades_to_default(db):
    set_source(db, "mastodon")
    assert port.get_source(db).name == "reddit_json"
