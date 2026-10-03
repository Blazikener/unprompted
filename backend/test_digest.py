"""Weekly digest: subscribe, run (mocked Oriane: zero credits), render, manage.

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_digest.py
"""
import io
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["APP_URL"] = "https://u.test"
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"
os.environ["ORIANE_API_KEY"] = ""
os.environ["RESEND_API_KEY"] = ""
os.environ.pop("MAIL_RELAY_URL", None)
os.environ.pop("MAIL_RELAY_SECRET", None)

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
        db.execute("DELETE FROM creator_handles WHERE handle = 'ali'")   # verified (with rules) in the licence tests


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
    assert d["status"] == "active" and d["created"] and d["email"] == "brand@example.com" and d["mailed"] is False
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
    assert d["status"] == "pending" and d["mailed"] is True and sent[0][0] == "brand@example.com" and "Confirm" in sent[0][1]
    assert "https://u.test/digest/%s?confirm=1" % d["token"] in sent[0][2] and "Oriane" in sent[0][2]
    assert digest.manage(d["token"], "confirm")["status"] == "active"
    assert len(digest.due()) == 1


def test_pending_subscription_retries_confirmation_and_reports_mail_result(monkeypatch):
    monkeypatch.setenv("MAIL_RELAY_URL", "https://relay.test/mail")
    monkeypatch.setenv("MAIL_RELAY_SECRET", "relay-test-secret")
    attempts = []
    monkeypatch.setattr(digest, "send", lambda *args: attempts.append(args) or len(attempts) == 1)

    first = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    again = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})

    assert first["status"] == again["status"] == "pending"
    assert first["mailed"] is True and again["mailed"] is False
    assert first["token"] == again["token"] and len(attempts) == 2


def test_mail_relay_takes_priority_and_sends_expected_payload(monkeypatch):
    monkeypatch.setenv("MAIL_RELAY_URL", "https://relay.test/mail")
    monkeypatch.setenv("MAIL_RELAY_SECRET", "relay-test-secret")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("DIGEST_FROM", "Unprompted <digest@u.test>")

    def urlopen(req, timeout):
        assert req.full_url == "https://relay.test/mail" and req.get_method() == "POST"
        assert req.headers["Content-type"] == "application/json"
        assert req.headers["User-agent"] == "Unprompted digest (+https://u.test)"
        assert timeout == 30
        payload = json.loads(req.data)
        assert payload == {
            "secret": "relay-test-secret", "to": "brand@example.com", "subject": "Weekly update", "html": "<p>Update</p>",
            "text": "Weekly update\n\nhttps://u.test/brands/", "name": "Unprompted",
        }
        return io.BytesIO(b'{"ok":true,"remaining":99}')

    monkeypatch.setattr(digest.request, "urlopen", urlopen)
    assert digest.mail_configured()
    assert digest.send("brand@example.com", "Weekly update", "<p>Update</p>") is True


@pytest.mark.parametrize("failure", ["rejected", "http", "oserror", "json"])
def test_mail_relay_failures_are_safe_and_do_not_fallback(monkeypatch, capsys, failure):
    secret = "relay-test-secret"
    monkeypatch.setenv("MAIL_RELAY_URL", "https://relay.test/mail")
    monkeypatch.setenv("MAIL_RELAY_SECRET", secret)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("DIGEST_FROM", "Unprompted <digest@u.test>")

    def urlopen(req, timeout):
        assert req.full_url == "https://relay.test/mail"
        if failure == "rejected":
            return io.BytesIO(json.dumps({"ok": False, "error": secret}).encode())
        if failure == "http":
            raise urllib.error.HTTPError(req.full_url, 502, secret, {}, None)
        if failure == "oserror":
            raise OSError(secret)
        return io.BytesIO(b"not json")

    monkeypatch.setattr(digest.request, "urlopen", urlopen)
    assert digest.send("brand@example.com", "Weekly update", "<p>Update</p>") is False
    logged = capsys.readouterr().err
    assert secret not in logged and len(logged.strip().splitlines()) == 1


def test_resend_is_used_when_relay_is_not_configured(monkeypatch):
    monkeypatch.delenv("MAIL_RELAY_URL", raising=False)
    monkeypatch.delenv("MAIL_RELAY_SECRET", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("DIGEST_FROM", "Unprompted <digest@u.test>")

    def urlopen(req, timeout):
        assert req.full_url == "https://api.resend.com/emails" and timeout == 20
        assert req.headers["Authorization"] == "Bearer re_test"
        return io.BytesIO(b'{"id":"mail-id"}')

    monkeypatch.setattr(digest.request, "urlopen", urlopen)
    assert digest.send("brand@example.com", "Weekly update", "<p>Update</p>") is True


def test_mail_relay_follows_post_302_with_get_without_body(monkeypatch):
    class RedirectHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.post_method = self.command
            self.server.post_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(302)
            self.send_header("Location", "/script.googleusercontent.com/result")
            self.end_headers()

        def do_GET(self):
            self.server.redirect_method = self.command
            self.server.redirect_body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *_):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MAIL_RELAY_URL", "http://127.0.0.1:%d/mail" % srv.server_port)
    monkeypatch.setenv("MAIL_RELAY_SECRET", "redirect-test-secret")
    try:
        assert digest.send("brand@example.com", "Weekly update", "<p>Update</p>") is True
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)

    assert srv.post_method == "POST"
    assert json.loads(srv.post_body)["secret"] == "redirect-test-secret"
    assert srv.redirect_method == "GET" and srv.redirect_body == b""


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


def test_license_button_records_one_request_and_tells_the_operator(monkeypatch):
    monkeypatch.setenv("LICENSE_NOTIFY_EMAIL", "ops@u.test")
    sent = []
    monkeypatch.setattr(digest, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    organic = video("lic1", "I always get a Tim Hortons iced capp on the way", handle="ali")
    paid = video("lic2", "Tim Hortons sent me this", caption="#ad", handle="ad")
    monkeypatch.setattr(server, "oriane", lambda *a, **k: page(organic, paid))
    digest.run_due()

    # Only the organic mention gets a button; 1,200 views price at the $50/month floor.
    body = sent[-1][2]
    assert "https://u.test/license/%s/lic1" % d["token"] in body and "/license/%s/lic2" % d["token"] not in body
    assert "License for ads &middot; ~$50 / 30 days" in body

    view = digest.license_view(d["token"], "lic1")
    assert view["request"] is None and view["video"]["handle"] == "ali" and view["video"]["kind"] == "spoken"
    assert view["prices"] == [{"days": 30, "usd": 50}, {"days": 60, "usd": 100}, {"days": 90, "usd": 150}]
    n = len(sent)
    made = digest.license_view(d["token"], "lic1", {"days": 60, "note": "UAE launch"})
    assert made["request"]["days"] == 60 and made["request"]["priceUsd"] == 100 and made["request"]["status"] == "requested"
    to, subject, mail = sent[-1]
    assert len(sent) == n + 1 and to == "ops@u.test" and "@ali" in subject and "60 days" in subject and "UAE launch" in mail
    # One request per video: asking again changes nothing and sends nothing.
    assert digest.license_view(d["token"], "lic1", {"days": 30})["request"]["days"] == 60 and len(sent) == n + 1
    assert digest.manage(d["token"])["licenses"][0]["handle"] == "ali"
    with psycopg.connect(server.DB_URL) as db:
        events = db.execute("SELECT name, count(*) FROM events WHERE data->>'digest' = (SELECT id::text FROM digests WHERE token = %s)"
                            " GROUP BY name", (d["token"],)).fetchall()
    assert dict(events) == {"report_sent": 1, "license_view": 1, "license_request": 1}

    for vid, body, status in (("lic2", {"days": 30}, 400), ("nope", None, 404), ("lic1", {"days": 45}, 400)):
        with pytest.raises(server.ApiError) as err:
            digest.license_view(d["token"], vid, body)
        assert err.value.status == status


def licence_request(monkeypatch, vid="op1"):
    """A watch with one emailed organic mention and a 30-day request for it; returns (digest, request id)."""
    monkeypatch.setattr(digest, "send", lambda *a: True)
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    monkeypatch.setattr(server, "oriane", lambda *a, **k: page(video(vid, "a Tim Hortons iced capp every morning", handle="ali")))
    digest.run_due()
    digest.license_view(d["token"], vid, {"days": 30})
    with psycopg.connect(server.DB_URL) as db:
        return d, db.execute("SELECT id FROM license_requests WHERE digest_id = (SELECT id FROM digests WHERE token = %s)",
                             (d["token"],)).fetchone()[0]


def test_operator_console_records_brokering(base, monkeypatch):
    d, rid = licence_request(monkeypatch)
    op = {"Authorization": "Bearer run-secret"}
    assert call(base, "GET", "/api/admin/licenses")[0] == 401
    status, listing = call(base, "GET", "/api/admin/licenses", headers=op)
    row = next(r for r in listing["requests"] if r["id"] == rid)
    assert status == 200 and row["status"] == "requested" and row["handle"] == "ali" and row["priceUsd"] == 50
    assert row["page"] == "https://u.test/license/%s/op1" % d["token"] and row["contactedAt"] is None

    status, row = call(base, "POST", "/api/admin/licenses/%d" % rid, {"status": "contacted"}, op)
    assert status == 200 and row["contactedAt"] and row["respondedAt"] is None
    status, row = call(base, "POST", "/api/admin/licenses/%d" % rid, {"status": "live", "finalPriceUsd": 80, "brandPaid": True,
                                                                     "codeReceived": True, "opsNote": "DM via bio email"}, op)
    assert status == 200 and row["respondedAt"] and row["codeReceivedAt"] and row["brandPaidAt"] and row["creatorPaidAt"] is None
    assert row["finalPriceUsd"] == 80 and row["opsNote"] == "DM via bio email"
    with psycopg.connect(server.DB_URL) as db:
        window = db.execute("SELECT expires_at - starts_at FROM license_requests WHERE id = %s", (rid,)).fetchone()[0]
    assert window.days == 30
    assert call(base, "POST", "/api/admin/licenses/%d" % rid, {"brandPaid": False}, op)[1]["brandPaidAt"] is None

    # The brand's page follows: live, at the final price, with an end date.
    req = digest.license_view(d["token"], "op1")["request"]
    assert req["status"] == "live" and req["priceUsd"] == 80 and req["expiresAt"]

    for body, code in (({"status": "maybe"}, 400), ({"finalPriceUsd": "80"}, 400), ({}, 400)):
        assert call(base, "POST", "/api/admin/licenses/%d" % rid, body, op)[0] == code
    assert call(base, "POST", "/api/admin/licenses/999999", {"status": "contacted"}, op)[0] == 404
    status, html = call(base, "GET", "/admin/")
    assert status == 200 and b"License requests" in html and b"/api/admin/licenses" in html


def test_run_due_serializes_concurrent_callers(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    calls = []

    def oriane(*args, **kwargs):
        calls.append((args, kwargs))
        threading.Event().wait(0.2)
        return page()

    monkeypatch.setattr(server, "oriane", oriane)
    start = threading.Barrier(3)
    results, errors = [], []

    def run():
        start.wait()
        try:
            results.append(digest.run_due())
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors and all(not thread.is_alive() for thread in threads)
    assert len(calls) == 1
    assert sum(result["ran"] for result in results) == 1


def test_scheduler_checks_first_after_first_s(monkeypatch):
    monkeypatch.setenv("DIGEST_SCHEDULER", "1")
    monkeypatch.setattr(digest.demo, "active", lambda: False)
    waits, checks = [], []

    class StopScheduler(BaseException):
        pass

    def fake_sleep(seconds):
        waits.append(seconds)

    def fake_run_due():
        checks.append(waits[-1])
        if len(checks) == 2:
            raise StopScheduler

    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            try:
                self.target()
            except StopScheduler:
                pass

    monkeypatch.setattr(digest.time, "sleep", fake_sleep)
    monkeypatch.setattr(digest, "run_due", fake_run_due)
    monkeypatch.setattr(digest.threading, "Thread", InlineThread)
    digest.scheduler(every_s=900, first_s=60)

    assert waits == [60, 900]
    assert checks == [60, 900]


def test_watch_is_due_every_seven_days():
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})

    def due_after(ago):
        with psycopg.connect(server.DB_URL) as db:
            db.execute("UPDATE digests SET last_run_at = now() - %s::interval WHERE token = %s", (ago, d["token"]))
        return len(digest.due())
    assert due_after("6 days 22 hours") == 0  # a 15-minute scheduler used to mail every 6 days
    assert due_after("6 days 23 hours 30 minutes") == 1  # a daily cron firing a bit early still lands on day 7
    assert due_after("8 days") == 1


def test_manage_actions():
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    assert digest.manage(d["token"], "pause")["status"] == "paused" and digest.due() == []
    assert digest.manage(d["token"], "resume")["status"] == "active" and len(digest.due()) == 1
    assert digest.manage(d["token"], "unsubscribe")["status"] == "unsubscribed"
    with pytest.raises(server.ApiError):
        digest.manage(d["token"])


def test_digest_email_limit_is_case_insensitive(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    digest.subscribe({"email": "Brand@Example.com", "brand": "Tim Hortons"})
    digest.subscribe({"email": "brand@example.com", "brand": "Noon"})
    with pytest.raises(server.ApiError) as err:
        digest.subscribe({"email": "BRAND@example.com", "brand": "Talabat"})
    assert err.value.status == 429
    assert str(err.value) == "You already watch 2 searches; stop one first."


def test_digest_active_limit_on_subscribe_and_resume(monkeypatch):
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("DIGEST_MAX_ACTIVE", "1")
    digest.subscribe({"email": "one@example.com", "brand": "Tim Hortons"})
    with pytest.raises(server.ApiError) as err:
        digest.subscribe({"email": "two@example.com", "brand": "Noon"})
    assert err.value.status == 503 and str(err.value) == "Weekly watches are full right now."

    monkeypatch.setenv("DIGEST_MAX_ACTIVE", "2")
    paused = digest.subscribe({"email": "two@example.com", "brand": "Noon"})
    assert digest.manage(paused["token"], "pause")["status"] == "paused"
    monkeypatch.setenv("DIGEST_MAX_ACTIVE", "1")
    with pytest.raises(server.ApiError) as err:
        digest.manage(paused["token"], "resume")
    assert err.value.status == 503 and str(err.value) == "Weekly watches are full right now."


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
    assert status == 200 and b"Weekly watch" in html
    monkeypatch.delenv("DIGEST_RUN_TOKEN")
    assert call(base, "POST", "/api/digests/run", {}, {"Authorization": "Bearer run-secret"})[0] == 404


def test_sample_api_is_anonymized_and_does_not_call_oriane(base, monkeypatch):
    monkeypatch.setattr(server, "oriane", lambda *a, **k: pytest.fail("sample endpoint must not call Oriane"))
    status, missing = call(base, "GET", "/api/digests/sample")
    assert status == 404 and missing == {"error": "No sample digest yet."}

    subscriber = "private-sample@example.com"
    d = digest.subscribe({"email": subscriber, "brand": "Tim Hortons"})
    manage = digest.manage_url(d["token"])
    sample_html = (
        '<!doctype html><html><body><p>2 new Tim Hortons videos.</p><a href="%s">License</a>'
        '<a href="%s">Manage</a><a href="%s?unsubscribe=1">Unsubscribe</a>'
        '<p>You get this because %s saved a search.</p></body></html>' % (digest.license_url(d["token"], "v1"), manage, manage, subscriber)
    )
    with psycopg.connect(server.DB_URL) as db:
        row = db.execute("SELECT id FROM digests WHERE token = %s", (d["token"],)).fetchone()
        db.execute("INSERT INTO digest_runs (digest_id, new_count, sent, html) VALUES (%s, 2, true, %s)", (row[0], sample_html))

    status, result = call(base, "GET", "/api/digests/sample")
    assert status == 200 and set(result) == {"brand", "newCount", "createdAt", "html"}
    assert result["brand"] == "Tim Hortons" and result["newCount"] == 2 and result["createdAt"]
    assert d["token"] not in result["html"] and subscriber not in result["html"]
    assert "you@brand.com" in result["html"]
    assert result["html"].count('href="https://u.test/brands/"') == 3


def test_frontend_routes(base):
    status, html = call(base, "GET", "/")
    assert status == 200 and b"Receipts by Unprompted" in html
    status, html = call(base, "GET", "/brands/")
    assert status == 200 and b"<title>Unprompted</title>" in html and b"Watch this search, get a weekly report." in html
    assert b"Saved, but we couldn't send the confirmation email right now. Try again in a few minutes." in html
    assert b"Watch a brand: one email report a week." in html and "Watching · weekly".encode() in html
    assert b"See a sample report" in html and b"Start with a search" in html
    status, html = call(base, "GET", "/digest/x")
    assert status == 200 and b"href: '/brands/'" in html and b"/brands/#search=${r.searchId}" in html
    status, html = call(base, "GET", "/digest/sample")
    assert status == 200 and b"Sample weekly report" in html and b"/api/digests/sample" in html
    assert b"Get this for your brand" in html and b"sandbox" in html
    status, html = call(base, "GET", "/license/x/y")
    assert status == 200 and b"Request license" in html and b"/api/digests/${token}/license/${vid}" in html
    assert call(base, "GET", "/api/digests/%s/license/v1" % ("x" * 20))[0] == 404
