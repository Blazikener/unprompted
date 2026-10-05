"""Waitlist signups, referral queue, visit tracking, and operator reporting."""
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")

import psycopg  # noqa: E402
import pytest  # noqa: E402

import digest  # noqa: E402
import server  # noqa: E402
import waitlist  # noqa: E402


def db_connect():
    return psycopg.connect(server.DB_URL, row_factory=psycopg.rows.dict_row)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    server.init_db()
    monkeypatch.setenv("DIGEST_RUN_TOKEN", "run-secret")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("ORIANE_API_KEY", raising=False)
    monkeypatch.delenv("WAITLIST_MAIL_DAILY", raising=False)
    for key in ("MAIL_RELAY_URL", "MAIL_RELAY_SECRET", "RESEND_API_KEY", "DIGEST_FROM"):
        monkeypatch.delenv(key, raising=False)
    with waitlist.RATE_LOCK:
        waitlist.RATE_LIMIT.clear()
    with db_connect() as db:
        db.execute("DELETE FROM waitlist WHERE email LIKE '%@wl.test'")
        db.execute("DELETE FROM waitlist_visits WHERE visitor LIKE 'wltest-%'")


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


def request(base, path, body=None, token=None, headers=None):
    headers = dict(headers or {})
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def join(base, email, **fields):
    body = {"email": email, "role": "creator", **fields}
    return request(base, "/api/waitlist", body)


def test_join_returns_code_and_queue_position(base):
    status, out = join(base, "one@wl.test", handle="one", platform="tiktok", size="m", pay="9")
    assert status == 200
    assert out == {
        "code": out["code"], "position": 1, "total": 1, "referrals": 0,
        "role": "creator", "alreadyJoined": False, "jump": 5,
    }
    assert len(out["code"]) == 8
    assert re_code(out["code"])


def re_code(value):
    import re
    return re.fullmatch(r"[A-Za-z0-9_-]{8}", value) is not None


def test_duplicate_email_keeps_code_and_updates_answers(base):
    _, first = join(base, "same@wl.test", handle="first", size="s", pay="0", pain="old")
    status, second = request(base, "/api/waitlist", {
        "email": " SAME@WL.TEST ", "role": "brand", "handle": "Agency", "platform": "youtube",
        "size": "xl", "pay": "29", "pain": "new answer",
    })
    assert status == 200 and second["alreadyJoined"] is True
    assert second["code"] == first["code"] and second["role"] == "brand"
    with db_connect() as db:
        row = db.execute("SELECT role, handle, platform, size, pay, pain, code FROM waitlist WHERE email = %s",
                         ("same@wl.test",)).fetchone()
    assert tuple(row.values()) == ("brand", "Agency", "youtube", "xl", "29", "new answer", first["code"])


@pytest.mark.parametrize("body", [
    {"email": "a@wl.test", "role": "invalid"},
    {"email": "b@wl.test", "role": "creator", "pay": "5"},
    {"email": "bad-email", "role": "creator"},
])
def test_invalid_role_pay_and_email_are_rejected(base, body):
    status, out = request(base, "/api/waitlist", body)
    assert status == 400 and "error" in out


def test_honeypot_returns_fake_success_without_inserting(base):
    status, out = request(base, "/api/waitlist", {
        "email": "invalid", "role": "not-a-role", "website": "https://spam.test",
    })
    assert status == 200 and out["code"] == "xxxxxxxx"
    with db_connect() as db:
        assert db.execute("SELECT count(*) AS n FROM waitlist").fetchone()["n"] == 0


def test_referral_moves_referrer_up_and_unknown_or_self_referral_is_ignored(base):
    _, a = join(base, "a@wl.test")
    for number in range(6):
        status, _ = join(base, "between%d@wl.test" % number)
        assert status == 200
    status, b = join(base, "b@wl.test", ref=a["code"])
    assert status == 200
    status, a_after = request(base, "/api/waitlist/" + a["code"])
    assert status == 200 and a_after["position"] == 1 and a_after["referrals"] == 1
    assert b["total"] == 8

    _, unknown = request(base, "/api/waitlist", {"email": "unknown@wl.test", "role": "creator", "ref": "missing-code"},
                         headers={"X-Forwarded-For": "198.51.100.10"})
    _, duplicate = request(base, "/api/waitlist", {"email": "a@wl.test", "role": "creator", "ref": a["code"]})
    assert unknown["referrals"] == 0 and duplicate["alreadyJoined"] is True
    with db_connect() as db:
        assert db.execute("SELECT referred_by FROM waitlist WHERE email = 'a@wl.test'").fetchone()["referred_by"] is None
        assert db.execute("SELECT referred_by FROM waitlist WHERE email = 'unknown@wl.test'").fetchone()["referred_by"] is None


def test_rate_limit_allows_eight_new_joins_per_ip(base):
    headers = {"X-Forwarded-For": "192.0.2.40, 10.0.0.1"}
    for number in range(8):
        status, _ = request(base, "/api/waitlist", {"email": "rate%d@wl.test" % number, "role": "creator"}, headers=headers)
        assert status == 200
    status, out = request(base, "/api/waitlist", {"email": "rate8@wl.test", "role": "creator"}, headers=headers)
    assert status == 429 and out["error"] == "Too many sign-ups from this network, try again in an hour."


def test_get_signup_and_not_found(base):
    _, result = join(base, "lookup@wl.test")
    status, out = request(base, "/api/waitlist/" + result["code"])
    assert status == 200 and out["code"] == result["code"] and out["position"] == 1
    status, out = request(base, "/api/waitlist/no-such-code")
    assert status == 404 and "error" in out


def test_public_stats(base):
    join(base, "creator@wl.test", role="creator")
    request(base, "/api/waitlist", {"email": "brand@wl.test", "role": "brand"})
    status, out = request(base, "/api/waitlist/stats")
    assert status == 200 and out["total"] == 2 and out["today"] == 2
    assert out["byRole"]["creator"] == 1 and out["byRole"]["brand"] == 1 and out["byRole"]["manager"] == 0


def test_visit_validation_insert_and_deduplication(base):
    status, out = request(base, "/api/waitlist/visit", {"visitor": "wltest-visitor-0001", "source": "newsletter", "ref": "abc"})
    assert status == 200 and out == {"ok": True}
    request(base, "/api/waitlist/visit", {"visitor": "wltest-visitor-0001", "source": "changed"})
    with db_connect() as db:
        row = db.execute("SELECT count(*) AS n, min(source) AS source FROM waitlist_visits"
                         " WHERE visitor = 'wltest-visitor-0001'").fetchone()
    assert row["n"] == 1 and row["source"] == "newsletter"
    status, out = request(base, "/api/waitlist/visit", {"visitor": "bad"})
    assert status == 400 and "error" in out


def test_admin_requires_operator_and_returns_counts(base):
    join(base, "admin@wl.test", role="brand", pay="9", size="xl", source="launch")
    request(base, "/api/waitlist/visit", {"visitor": "wltest-admin-01", "source": "launch"})
    assert request(base, "/api/admin/waitlist")[0] == 401
    status, out = request(base, "/api/admin/waitlist", token="run-secret")
    assert status == 200
    with db_connect() as db:
        total = db.execute("SELECT count(*) AS n FROM waitlist").fetchone()["n"]
        visitors = db.execute("SELECT count(*) AS n FROM waitlist_visits").fetchone()["n"]
    assert out["total"] == total and out["visitors"] == visitors
    assert out["conversion"] == total / visitors
    assert out["wouldPay"] >= 1 and out["byRole"]["brand"] >= 1
    assert out["byPay"]["brand"]["9"] >= 1 and out["bySize"]["brand"]["xl"] >= 1
    assert out["bySource"][0]["source"] == "launch"
    assert len(out["daily"]) == 30 and out["daily"][-1]["day"]
    assert out["labels"] == waitlist.SIZE_LABELS
    entry = next(row for row in out["entries"] if row["email"] == "admin@wl.test")
    assert entry["role"] == "brand" and entry["code"] and entry["position"] >= 1


def test_welcome_email_uses_share_link_and_marks_sent(base, monkeypatch):
    monkeypatch.setenv("APP_URL", "https://u.test")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("DIGEST_FROM", "Unprompted <hi@u.test>")
    monkeypatch.setattr(digest, "mail_configured", lambda: True)
    monkeypatch.setattr(waitlist.demo, "active", lambda: False)
    sent = []
    done = threading.Event()

    def send(to, subject, body):
        sent.append((to, subject, body))
        done.set()
        return True

    monkeypatch.setattr(digest, "send", send)
    _, out = join(base, "welcome@wl.test", handle="<script>alert(1)</script>")
    assert done.wait(3)
    for _ in range(100):
        with db_connect() as db:
            welcomed = db.execute("SELECT welcomed_at FROM waitlist WHERE email = 'welcome@wl.test'").fetchone()["welcomed_at"]
        if welcomed:
            break
        time.sleep(0.01)
    assert welcomed is not None and len(sent) == 1
    to, subject, body = sent[0]
    assert to == "welcome@wl.test" and subject == "You're #1 on the Receipts waitlist"
    assert "https://u.test/waitlist?ref=" + out["code"] in body and "moves you up 5 spots" in body
    assert "<script>" not in body


def test_welcome_email_daily_cap_zero_skips_sending(base, monkeypatch):
    monkeypatch.setenv("WAITLIST_MAIL_DAILY", "0")
    monkeypatch.setattr(digest, "mail_configured", lambda: True)
    monkeypatch.setattr(waitlist.demo, "active", lambda: False)
    sent = []
    monkeypatch.setattr(digest, "send", lambda *args: sent.append(args) or True)
    _, out = join(base, "cap@wl.test")
    with db_connect() as db:
        row = db.execute("SELECT id FROM waitlist WHERE email = 'cap@wl.test'").fetchone()
    waitlist._welcome(row["id"], "cap@wl.test", out["code"], out["position"])
    assert not sent


def test_waitlist_and_join_static_aliases(base):
    page = Path(server.ROOT / "frontend/waitlist/index.html")
    if not page.is_file():
        pytest.skip("frontend/waitlist/index.html is still being built")
    for path in ("/waitlist", "/waitlist?ref=abc", "/join"):
        with urllib.request.urlopen(base + path) as response:
            assert response.status == 200
