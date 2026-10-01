"""Weekly digest: subscribe, run (mocked Oriane: zero credits), render, manage.

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_digest.py
"""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["APP_URL"] = "https://u.test"
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"
os.environ.pop("RESEND_API_KEY", None)

import psycopg  # noqa: E402
import pytest  # noqa: E402

import digest  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def clean():
    server.init_db()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM digests")
        db.execute("DELETE FROM mentions WHERE search_id IN (SELECT id FROM searches WHERE brand = 'Tim Hortons')")
        db.execute("DELETE FROM searches WHERE brand = 'Tim Hortons'")


def video(vid, transcript, days_ago=1, caption="", handle="fan"):
    from datetime import date, timedelta
    return {"id": vid, "platform": "tiktok", "platformId": vid, "profileHandle": handle, "viewsCount": 1200, "caption": caption, "hashtags": [],
            "mentions": [], "coAuthors": [], "transcript": transcript,
            "publishedAt": (date.today() - timedelta(days=days_ago)).isoformat() + "T10:00:00Z",
            "transcriptChunks": [{"startSeconds": 65.0, "endSeconds": 70.0, "text": transcript}] if transcript else [], "frames": [],
            "thumbnailMediaUrl": "t.jpg"}


def page(*videos):
    return {"data": {"results": list(videos), "aggregations": {"totalViewsCount": 1200 * len(videos)}},
            "metadata": {"pagination": {"totalCount": len(videos)}}}


def test_subscribe_validates_and_is_active_without_mail(monkeypatch):
    sent = []
    monkeypatch.setattr(digest, "send", lambda *a: sent.append(a) or False)
    with pytest.raises(server.ApiError):
        digest.subscribe({"email": "nope", "brand": "Tim Hortons"})
    with pytest.raises(server.ApiError):
        digest.subscribe({"email": "a@b.co", "brand": "T"})
    d = digest.subscribe({"email": "Brand@Example.com ", "brand": "Tim Hortons", "variants": "Timmies, Tim Hortons",
                          "platform": "tiktok", "lang": "en"})
    assert d["status"] == "active" and d["created"] and d["email"] == "brand@example.com"
    assert d["params"] == {"variants": ["Timmies"], "platform": "tiktok", "lang": "en"}
    assert d["manageUrl"] == "https://u.test/digest/" + d["token"] and not sent
    again = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons", "variants": ["Timmies"], "platform": "tiktok", "lang": "en"})
    assert again["token"] == d["token"] and not again["created"]


def test_subscribe_is_double_opt_in_when_mail_is_configured(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    monkeypatch.setenv("DIGEST_FROM", "Unprompted <digest@u.test>")
    sent = []
    monkeypatch.setattr(digest, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    assert d["status"] == "pending" and sent[0][0] == "brand@example.com" and "Confirm" in sent[0][1]
    assert "https://u.test/digest/%s?confirm=1" % d["token"] in sent[0][2] and "Oriane" in sent[0][2]
    assert digest.manage(d["token"], "confirm")["status"] == "active"
    assert len(digest.due()) == 1


def test_run_mails_only_unseen_mentions_and_credits_oriane(monkeypatch):
    calls, sent = [], []
    seen_before = video("old1", "Tim Hortons iced capp", days_ago=3)
    sid0 = server.new_search("Tim Hortons", {"variants": [], "platform": "all", "lang": "any", "days": 30, "match": 2}, page(seen_before))
    server.store_results(sid0, [seen_before], "Tim Hortons", [])
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons", "searchId": sid0})
    fresh = video("new1", "I always get a Tim Hortons iced capp on the way", handle="ali")
    tagged = video("new2", "morning", caption="#timhortons run", handle="sara")
    sponsored = video("new3", "Tim Hortons sent me this", caption="#ad", handle="ad")
    loose = video("new4", "nothing relevant here")
    stale = video("stale", "Tim Hortons forever", days_ago=40)

    def oriane(filters, limit=100, sort="transcriptRelevance", offset=0, index="contents"):
        calls.append((filters, limit, sort))
        return page(seen_before, fresh, tagged, sponsored, loose, stale)
    monkeypatch.setattr(server, "oriane", oriane)
    monkeypatch.setattr(digest, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)

    out = digest.run_due()
    assert out["ran"] == 1 and out["results"][0]["new"] == 3 and out["results"][0]["sent"]
    assert len(calls) == 1 and calls[0][1] == 100 and "publishedAt" in calls[0][0]
    assert calls[0][0]["transcript"]["includesExactly"] == {"values": ["Tim Hortons"]}
    to, subject, body = sent[0]
    assert to == "brand@example.com" and subject == "3 new creator mentions of Tim Hortons this week"
    assert "@ali" in body and "@sara" in body and "<b>Tim Hortons</b>" in body and "at 1:05" in body and "@fan" not in body
    assert "1 said on camera only, 1 tagged, 1 disclosed partnership" in body
    assert "Search and transcripts by Oriane" in body and "/digest/%s?unsubscribe=1" % d["token"] in body
    assert "#search=%d" % out["results"][0]["searchId"] in body
    assert "/brands/#search=%d" % out["results"][0]["searchId"] in body
    # the same week again: everything already seen, nothing mailed, not due anyway
    assert digest.run_due() == {"ran": 0, "results": []}
    assert digest.run_due(force=True)["results"][0]["new"] == 0 and len(sent) == 1
    view = digest.manage(d["token"])
    assert view["lastRunAt"] and len(view["runs"]) == 2 and view["runs"][1]["newCount"] == 3 and view["latestHtml"] == body
    # mail failed on the real run: the operator can re-send the newest rendered run without touching Oriane
    did, n_calls = out["results"][0]["digest"], len(calls)
    again = digest.resend_last(did)
    assert again == {"digest": did, "run": view["runs"][1]["id"], "new": 3, "sent": True, "resent": True}
    assert len(calls) == n_calls and sent[1] == sent[0]


def test_manage_actions():
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    assert digest.manage(d["token"], "pause")["status"] == "paused" and digest.due() == []
    assert digest.manage(d["token"], "resume")["status"] == "active" and len(digest.due()) == 1
    assert digest.manage(d["token"], "unsubscribe")["status"] == "unsubscribed"
    with pytest.raises(server.ApiError):
        digest.manage(d["token"])


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


def call(base, method, path, body=None, headers=None):
    h = {"Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=h)
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.load(res) if res.headers.get("Content-Type", "").startswith("application/json") else res.read()
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_http_routes(base, monkeypatch):
    monkeypatch.setattr(server, "oriane", lambda *a, **k: page())
    status, d = call(base, "POST", "/api/digests", {"email": "brand@example.com", "brand": "Tim Hortons", "platform": "instagram"})
    assert status == 200 and d["status"] == "active"
    assert call(base, "GET", "/api/digests/" + d["token"])[1]["brand"] == "Tim Hortons"
    assert call(base, "GET", "/api/digests/nope")[0] == 404
    assert call(base, "POST", "/api/digests/run", {})[0] == 401
    assert call(base, "POST", "/api/digests/run", {"force": True}, {"Authorization": "Bearer run-secret"})[1]["ran"] == 1
    assert call(base, "POST", "/api/digests/%s/pause" % d["token"], {})[1]["status"] == "paused"
    status, html = call(base, "GET", "/digest/" + d["token"])
    assert status == 200 and b"Weekly digest" in html
    monkeypatch.delenv("DIGEST_RUN_TOKEN")
    assert call(base, "POST", "/api/digests/run", {}, {"Authorization": "Bearer run-secret"})[0] == 404


def test_frontend_routes(base):
    status, html = call(base, "GET", "/")
    assert status == 200 and b"Receipts by Unprompted" in html
    status, html = call(base, "GET", "/brands/")
    assert status == 200 and b"<title>Unprompted</title>" in html and b"Hear the next ones first" in html
    status, html = call(base, "GET", "/digest/x")
    assert status == 200 and b"href: '/brands/'" in html and b"/brands/#search=${r.searchId}" in html
