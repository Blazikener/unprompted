"""Brand dashboard authentication, quotas, ownership, and provider-credit tests."""
import http.cookiejar
import io
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import psycopg
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"

import creator  # noqa: E402
import server  # noqa: E402


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


@pytest.fixture(autouse=True)
def clean():
    server.init_db()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("TRUNCATE users, sessions, scans, brand_activity, events, mentions, searches, videos, checks,"
                   " oriane_calls, digests RESTART IDENTITY CASCADE")


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=10) as res:
                return res.status, self.decode(res.headers, res.read())
        except urllib.error.HTTPError as err:
            return err.code, self.decode(err.headers, err.read())

    @staticmethod
    def decode(headers, raw):
        if "application/json" in headers.get("Content-Type", ""):
            return json.loads(raw)
        return raw


def signup(base, email="brand@example.com"):
    client = Client(base)
    status, result = client.request("POST", "/api/creators/signup", {"email": email, "password": "password-123"})
    assert status == 201
    return client, result["user"]


def empty_page():
    return {"data": {"results": [], "aggregations": {"totalViewsCount": 0}},
            "metadata": {"pagination": {"totalCount": 0}}}


def video(handle="fan"):
    return {
        "id": "video-" + handle, "platform": "tiktok", "platformId": "post-" + handle,
        "profileHandle": handle, "profileFollowersCount": 1000, "viewsCount": 1200,
        "publishedAt": "2026-01-01T00:00:00Z", "caption": "", "hashtags": [],
        "mentions": [], "coAuthors": [], "transcript": "", "transcriptChunks": [],
        "frames": [], "thumbnailMediaUrl": "thumb.jpg", "duration": 60,
    }


def test_brand_posts_require_auth_before_provider_call(base, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "oriane", lambda *args, **kwargs: calls.append((args, kwargs)))
    client = Client(base)
    for path, body in (
        ("/api/searches", {"brand": "Alpha Brand"}),
        ("/api/searches/stream", {"brand": "Alpha Brand"}),
        ("/api/checks", {"platform": "tiktok", "handle": "creator", "brand": "Alpha Brand"}),
    ):
        status, result = client.request("POST", path, body)
        assert status == 401 and result == {"error": "Sign in to search."}
    assert calls == []


def test_search_quotas_reuse_ownership_and_usage(base, monkeypatch):
    calls = []

    def oriane(filters, limit=100, sort="transcriptRelevance", offset=0, index="contents"):
        calls.append((filters, limit, sort, offset, index))
        return empty_page()

    monkeypatch.setattr(server, "oriane", oriane)
    client_a, _ = signup(base, "a@example.com")
    client_b, _ = signup(base, "b@example.com")
    assert creator.PLANS["free"]["brandSearchesPerWeek"] == 2
    assert creator.PLANS["free"]["checksPerWeek"] == 3
    assert creator.PLANS["pro"]["brandSearchesPerWeek"] == 30
    assert creator.PLANS["pro"]["checksPerWeek"] == 50

    for brand in ("Alpha Brand", "Beta Brand"):
        status, _ = client_a.request("POST", "/api/searches", {"brand": brand})
        assert status == 200
    status, repeated = client_a.request("POST", "/api/searches", {"brand": "Beta Brand"})
    assert status == 200 and repeated["search"]["brand"] == "Beta Brand"
    assert len(calls) == 2
    assert all(call[1] == 100 for call in calls)

    status, limited = client_a.request("POST", "/api/searches", {"brand": "Gamma Brand"})
    assert status == 402 and "2 free brand searches" in limited["error"]
    assert len(calls) == 2

    status, _ = client_b.request("POST", "/api/searches", {"brand": "Beta Brand"})
    assert status == 200 and len(calls) == 3
    status, usage_a = client_a.request("GET", "/api/creators/me")
    assert status == 200 and usage_a["brandUsage"] == {
        "searches": {"used": 2, "limit": 2}, "checks": {"used": 0, "limit": 3},
    }
    status, usage_b = client_b.request("GET", "/api/creators/me")
    assert status == 200 and usage_b["brandUsage"]["searches"] == {"used": 1, "limit": 2}

    status, searches_a = client_a.request("GET", "/api/searches")
    assert status == 200 and {row["brand"] for row in searches_a} == {"Alpha Brand", "Beta Brand"}
    status, searches_b = client_b.request("GET", "/api/searches")
    assert status == 200 and [row["brand"] for row in searches_b] == ["Beta Brand"]
    anon = Client(base)
    assert anon.request("GET", "/api/searches") == (200, [])
    public_id = searches_a[0]["id"]
    status, public = anon.request("GET", "/api/searches/%d" % public_id)
    assert status == 200 and public["search"]["id"] == public_id


def test_watch_marks_only_the_watchers_matching_searches(base, monkeypatch):
    monkeypatch.setattr(server, "oriane", lambda *a, **k: empty_page())
    for name in ("MAIL_RELAY_URL", "RESEND_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    client_a, _ = signup(base, "a@example.com")
    client_b, _ = signup(base, "b@example.com")
    _, year = client_a.request("POST", "/api/searches", {"brand": "Alpha Brand"})
    _, month = client_a.request("POST", "/api/searches", {"brand": "Alpha Brand", "days": 30})
    _, other = client_b.request("POST", "/api/searches", {"brand": "Alpha Brand"})
    status, watch = client_a.request("POST", "/api/digests", {"email": "team@alpha.test", "brand": "Alpha Brand",
                                                              "searchId": year["search"]["id"]})
    assert status == 200 and watch["status"] == "active"
    status, _ = client_b.request("POST", "/api/digests", {"email": "b@example.com", "brand": "Alpha Brand", "platform": "tiktok"})
    assert status == 200

    _, rows_a = client_a.request("GET", "/api/searches")
    marked = {"status": "active", "manageUrl": watch["manageUrl"]}
    assert {r["id"]: r["watch"] for r in rows_a} == {year["search"]["id"]: marked, month["search"]["id"]: marked}  # any period
    _, rows_b = client_b.request("GET", "/api/searches")
    assert [r["watch"] for r in rows_b] == [None] and rows_b[0]["id"] == other["search"]["id"]  # tiktok watch, all-platform search


def test_stream_uses_one_call_and_keeps_progress_then_done(base, monkeypatch):
    calls = []

    def oriane(filters, limit=100, sort="transcriptRelevance", offset=0, index="contents"):
        calls.append((filters, limit, sort, offset, index))
        raw = video()
        raw["transcript"] = "Alpha Brand is great"
        return {"data": {"results": [raw], "aggregations": {"totalViewsCount": raw["viewsCount"]}},
                "metadata": {"pagination": {"totalCount": 1}}}

    monkeypatch.setattr(server, "oriane", oriane)
    client, _ = signup(base)
    status, body = client.request("POST", "/api/searches/stream", {"brand": "Alpha Brand"})
    events = [json.loads(line) for line in body.splitlines()]
    assert status == 200 and [event["type"] for event in events] == ["progress", "done"]
    assert len(calls) == 1 and calls[0][1:] == (100, "transcriptRelevance", 0, "contents")


def test_check_quota_cache_and_pro_fresh(base, monkeypatch):
    calls = []

    def oriane(filters, limit=100, sort="transcriptRelevance", offset=0, index="contents"):
        calls.append((filters, limit, sort, offset, index))
        handle = filters["profileHandle"]["exactMatch"]["values"][0]
        raw = video(handle)
        return {"data": {"results": [raw], "aggregations": {"totalViewsCount": raw["viewsCount"]}},
                "metadata": {"pagination": {"totalCount": 1}}}

    monkeypatch.setattr(server, "oriane", oriane)
    monkeypatch.setattr(server, "check", lambda *args, **kwargs: {"verdict": "Review first", "total": 42})
    free, _ = signup(base, "free@example.com")
    for n in range(3):
        status, _ = free.request("POST", "/api/checks", {
            "platform": "tiktok", "handle": "creator_%d" % n, "brand": "Alpha Brand",
        })
        assert status == 200
    assert len(calls) == 3

    status, limited = free.request("POST", "/api/checks", {
        "platform": "tiktok", "handle": "creator_3", "brand": "Alpha Brand",
    })
    assert status == 402 and "3 free creator checks" in limited["error"] and len(calls) == 3

    status, cached = free.request("POST", "/api/checks", {
        "platform": "tiktok", "handle": "creator_0", "brand": "Alpha Brand", "fresh": True,
    })
    assert status == 200 and cached["verdict"] == "Review first" and len(calls) == 3

    pro, pro_user = signup(base, "pro@example.com")
    with psycopg.connect(server.DB_URL) as db:
        db.execute("UPDATE users SET plan = 'pro' WHERE id = %s", (pro_user["id"],))
    status, _ = pro.request("POST", "/api/checks", {
        "platform": "tiktok", "handle": "creator_0", "brand": "Alpha Brand", "fresh": True,
    })
    assert status == 200 and len(calls) == 4
    assert calls[-1][1] == 30

    status, usage = free.request("GET", "/api/creators/me")
    assert status == 200 and usage["brandUsage"]["checks"] == {"used": 3, "limit": 3}
    status, usage = pro.request("GET", "/api/creators/me")
    assert status == 200 and usage["brandUsage"]["checks"] == {"used": 1, "limit": 50}


def test_streaming_anonymous_and_logged_out_usage(base, monkeypatch):
    monkeypatch.setattr(server, "oriane", lambda *args, **kwargs: pytest.fail("anonymous request reached Oriane"))
    client = Client(base)
    status, me = client.request("GET", "/api/creators/me")
    assert status == 200 and me["brandUsage"] is None
    status, rows = client.request("GET", "/api/searches")
    assert status == 200 and rows == []


def test_global_budget_accounts_for_endpoint_costs(monkeypatch):
    monkeypatch.setenv("ORIANE_API_KEY", "test-only")
    monkeypatch.setenv("ORIANE_DAILY_BUDGET", "80")
    calls = []

    def urlopen(req, timeout=0):
        calls.append(req.full_url)
        return io.BytesIO(json.dumps(empty_page()).encode())

    monkeypatch.setattr(server.request, "urlopen", urlopen)
    server.oriane({})
    server.oriane({})
    with pytest.raises(server.ApiError) as err:
        server.oriane({})
    assert err.value.status == 503 and "Nothing was taken from your quota" in str(err.value)
    assert len(calls) == 2
    with psycopg.connect(server.DB_URL) as db:
        rows = db.execute("SELECT endpoint, credits FROM oriane_calls ORDER BY id").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("contents", 40), ("contents", 40)]


def test_global_budget_costs_profiles_and_contents(monkeypatch):
    monkeypatch.setenv("ORIANE_API_KEY", "test-only")
    monkeypatch.setenv("ORIANE_DAILY_BUDGET", "70")
    calls = []

    def urlopen(req, timeout=0):
        calls.append(req.full_url)
        return io.BytesIO(json.dumps(empty_page()).encode())

    monkeypatch.setattr(server.request, "urlopen", urlopen)
    server.oriane({})
    server.oriane({}, index="profiles")
    with pytest.raises(server.ApiError) as err:
        server.oriane({})
    assert err.value.status == 503 and len(calls) == 2
    with psycopg.connect(server.DB_URL) as db:
        rows = db.execute("SELECT endpoint, credits FROM oriane_calls ORDER BY id").fetchall()
    assert [(row[0], row[1]) for row in rows] == [("contents", 40), ("profiles", 30)]


def test_budget_rejection_does_not_consume_brand_or_receipts_quotas(base, monkeypatch):
    monkeypatch.setenv("ORIANE_API_KEY", "test-only")
    monkeypatch.setenv("ORIANE_DAILY_BUDGET", "0")
    monkeypatch.setattr(server.request, "urlopen", lambda *args, **kwargs: pytest.fail("budget rejection reached urlopen"))
    client, user = signup(base, "budget@example.com")

    status, search_error = client.request("POST", "/api/searches", {"brand": "Budget Brand"})
    assert status == 503 and "Nothing was taken from your quota" in search_error["error"]
    status, check_error = client.request("POST", "/api/checks", {
        "platform": "tiktok", "handle": "budget.creator", "brand": "Budget Brand",
    })
    assert status == 503 and "Nothing was taken from your quota" in check_error["error"]

    monkeypatch.setattr(creator.demo, "active", lambda: False)
    monkeypatch.setattr(creator, "fetch_videos", lambda *args, **kwargs: server.oriane({})["data"]["results"])
    with pytest.raises(server.ApiError) as err:
        creator.run_scan(user, {"platform": "tiktok", "handle": "receipt.creator"})
    assert err.value.status == 503
    status, me = client.request("GET", "/api/creators/me")
    assert status == 200 and me["brandUsage"] == {
        "searches": {"used": 0, "limit": 2}, "checks": {"used": 0, "limit": 3},
    }
    with psycopg.connect(server.DB_URL) as db:
        assert db.execute("SELECT count(*) FROM scans WHERE user_id = %s", (user["id"],)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM oriane_calls").fetchone()[0] == 0
