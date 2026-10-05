"""Receipts packaging test (packaging.py): arms by cookie, the offer each account keeps, offer (b)'s open plan, offer (c)'s
Weekly leads (checkout, webhook, the Monday email) and the console's numbers. Demo creators, no Oriane or Stripe calls.

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_packaging.py
"""
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"
os.environ["APP_URL"] = "https://u.test"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

import creator  # noqa: E402
import digest  # noqa: E402
import packaging  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    server.init_db()
    for k in ("RECEIPTS_ARMS", "STRIPE_SECRET_KEY", "STRIPE_PRICE_PRO", "STRIPE_PRICE_LEADS", "STRIPE_WEBHOOK_SECRET", "DEV_PLAN_SWITCH"):
        monkeypatch.delenv(k, raising=False)
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM visitors")
        db.execute("DELETE FROM users WHERE email LIKE '%%@pack.test'")
        db.execute("DELETE FROM brand_activity")


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


class Browser:
    """Keeps cookies between calls, like the Receipts page would."""
    def __init__(self, base):
        self.base, self.cookies = base, {}

    def __call__(self, method, path, body=None, headers=None):
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if self.cookies:
            hdrs["Cookie"] = "; ".join("%s=%s" % kv for kv in self.cookies.items())
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req) as res:
                status, data, set_cookies = res.status, json.load(res), res.headers.get_all("Set-Cookie") or []
        except urllib.error.HTTPError as e:
            status, data, set_cookies = e.code, json.load(e), e.headers.get_all("Set-Cookie") or []
        for c in set_cookies:
            name, value = c.split(";")[0].split("=", 1)
            self.cookies[name] = value
        return status, data


def visitors():
    with psycopg.connect(server.DB_URL) as db:
        return db.execute("SELECT arm, forced FROM visitors ORDER BY created_at").fetchall()


def test_a_browser_keeps_its_arm_and_previews_are_not_counted(base):
    b = Browser(base)
    status, first = b("POST", "/api/creators/visit", {})
    assert status == 200 and first["arm"] in packaging.ARMS and packaging.COOKIE in b.cookies
    assert b("POST", "/api/creators/visit", {})[1]["arm"] == first["arm"] and len(visitors()) == 1   # same browser, same arm
    preview = Browser(base)
    assert preview("POST", "/api/creators/visit", {"arm": "c"})[1]["offer"] == {"arm": "c", "plan": "leads", "name": "Weekly leads",
                                                                                 "price": 9, "checkout": False}
    assert visitors()[-1] == (("c", True))
    assert sum(not forced for _, forced in visitors()) == 1
    status, inbox = Browser(base)("POST", "/api/creators/visit", {"page": "inbox"})        # the licence inbox never draws an arm
    assert status == 200 and inbox == {"arm": None} and len(visitors()) == 2


def test_the_account_keeps_its_offer_and_b_is_free_for_good(base, monkeypatch):
    monkeypatch.setenv("RECEIPTS_ARMS", "b")
    b = Browser(base)
    assert b("POST", "/api/creators/visit", {})[1]["arm"] == "b"
    status, out = b("POST", "/api/creators/signup", {"email": "bee@pack.test", "password": "longpassword"})
    assert status == 201 and out["user"]["arm"] == "b" and out["user"]["plan"] == "free"
    assert out["user"]["limits"]["brands"] is None and out["user"]["limits"]["pitch"] and not out["user"]["limits"]["activity"]
    status, scan = b("POST", "/api/creators/scans", {"platform": "tiktok", "handle": "maya.eats"})
    assert status == 200 and scan["plan"] == "open" and not any(x.get("locked") for x in scan["brands"])   # every brand, free
    brand = quote(scan["brands"][0]["brand"])
    assert b("POST", "/api/creators/scans/%d/brands/%s/pitch" % (scan["id"], brand), {})[0] == 200
    status, err = b("POST", "/api/creators/scans/%d/brands/%s/activity" % (scan["id"], brand), {})
    assert status == 402 and err["error"] == "Your plan doesn't include the sponsor check."
    assert b("POST", "/api/creators/billing/checkout", {})[1]["error"].startswith("Receipts is free on your account")
    status, err = b("POST", "/api/creators/scans", {"platform": "tiktok", "handle": "sami.lifts"})
    assert status == 402 and err["error"] == "You've used this week's free scan. The next one opens 7 days after the last."
    # The test ends with (a) winning: the same account now sees today's offer, top 3 brands on the free plan.
    monkeypatch.setenv("RECEIPTS_ARMS", "a")
    assert b("POST", "/api/creators/visit", {})[1]["arm"] == "a"
    status, again = b("GET", "/api/creators/scans/%d" % scan["id"])
    assert again["plan"] == "free" and sum(bool(x.get("locked")) for x in again["brands"]) == len(again["brands"]) - 3
    with psycopg.connect(server.DB_URL) as db:
        assert db.execute("SELECT arm FROM users WHERE email = 'bee@pack.test'").fetchone() == ("b",)   # history is kept


def webhook(payload, secret):
    ts = int(time.time())
    sig = hmac.new(secret.encode(), b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest()
    return creator.webhook(payload, {"Stripe-Signature": "t=%d,v1=%s" % (ts, sig)})


def test_c_sells_weekly_leads(base, monkeypatch):
    monkeypatch.setenv("RECEIPTS_ARMS", "c")
    b = Browser(base)
    b("POST", "/api/creators/visit", {})
    uid = b("POST", "/api/creators/signup", {"email": "sea@pack.test", "password": "longpassword"})[1]["user"]["id"]
    assert b("POST", "/api/creators/billing/interest", {"scan": None}) == (200, {"ok": True})          # checkout not live: a reservation
    assert b("POST", "/api/creators/billing/checkout", {})[0] == 503
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_LEADS", "price_leads")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    assert b("POST", "/api/creators/visit", {})[1]["offer"]["checkout"] is True
    sent = []
    monkeypatch.setattr(creator, "stripe", lambda path, params: sent.append(params) or {"url": "https://checkout.test/s"})
    assert b("POST", "/api/creators/billing/checkout", {})[1] == {"url": "https://checkout.test/s"}   # arm's Weekly leads offer
    assert sent[0]["line_items[0][price]"] == "price_leads" and sent[0]["metadata[plan]"] == "leads"
    monkeypatch.delenv("STRIPE_PRICE_PRO", raising=False)
    assert b("POST", "/api/creators/billing/checkout", {"plan": "pro"})[0] == 503
    payload = json.dumps({"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "metadata": {"user_id": str(uid), "plan": "leads"}, "customer": "cus_1", "subscription": "sub_1"}}}).encode()
    assert webhook(payload, "whsec_x") == {"received": True}
    status, me = b("GET", "/api/creators/me")
    assert me["user"]["plan"] == "leads" and me["user"]["limits"]["activity"] and me["user"]["billing"]
    with psycopg.connect(server.DB_URL) as db:
        events = db.execute("SELECT name, data FROM events WHERE user_id = %s ORDER BY id", (uid,)).fetchall()
    assert ("checkout_intent", {"plan": "leads", "price": 9, "scan": None}) in events
    assert ("subscribed", {"subscription": "sub_1", "plan": "leads"}) in events


def make_user(email, plan="free", arm=None, days_old=0):
    with psycopg.connect(server.DB_URL) as db:
        return db.execute("INSERT INTO users (email, password, plan, arm, created_at) VALUES (%s, 'x', %s, %s, now() - %s * interval '1 day')"
                          " RETURNING *", (email, plan, arm, days_old)).fetchone()


def test_weekly_leads_email(monkeypatch):
    mail = []
    monkeypatch.setattr(packaging, "ACTIVITY_LOOKUPS", 100)              # every brand checked, so both builds below agree
    monkeypatch.setattr(digest, "send", lambda to, subject, body: mail.append((to, subject, body)) or True)
    with psycopg.connect(server.DB_URL, row_factory=dict_row) as db:
        u = db.execute("INSERT INTO users (email, password, plan, arm) VALUES ('leads@pack.test', 'x', 'leads', 'c') RETURNING *").fetchone()
        db.execute("INSERT INTO users (email, password, plan, arm) VALUES ('noscan@pack.test', 'x', 'leads', 'c')")
    creator.run_scan(u, {"platform": "tiktok", "handle": "maya.eats"})
    feed = packaging.build_feed(u)
    assert feed["handle"] == "maya.eats" and all(x["paying"] in ("active", "some") and x["new"] for x in feed["leads"])
    assert packaging.run_feeds(any_day=True) == 2 and packaging.run_feeds(any_day=True) == 0            # once a week
    to = {m[0]: m for m in mail}
    _, subject, body = to["leads@pack.test"]
    n = len(feed["leads"])
    assert subject == ("%d brand%s you mention %s paying creators this week" % (n, "" if n == 1 else "s", "is" if n == 1 else "are") if n
                       else "A quiet week: none of the brands you mention are paying creators right now")
    assert "https://u.test/creators/?from=feed" in body and all(x["brand"] in body for x in feed["leads"])
    assert to["noscan@pack.test"][1] == "Scan your handle to start your weekly leads"
    assert all(not x["new"] for x in packaging.build_feed(u)["leads"])                                # sent once: no longer new


def test_report(base, monkeypatch):
    a1 = make_user("a1@pack.test", arm="a", days_old=30)
    make_user("a2@pack.test", plan="pro", arm="a", days_old=2)
    b1 = make_user("b1@pack.test", arm="b", days_old=40)
    make_user("c1@pack.test", arm="c", days_old=29)
    make_user("cf@pack.test", arm="c")
    with psycopg.connect(server.DB_URL) as db:
        db.execute("INSERT INTO users (email, password, arm, arm_forced) VALUES ('preview@pack.test', 'x', 'b', true)")
        db.execute("INSERT INTO visitors (token, arm) VALUES ('t1', 'a'), ('t2', 'a'), ('t3', 'b'), ('t4', 'c')")
        db.execute("INSERT INTO visitors (token, arm, forced) VALUES ('t5', 'b', true)")
        a2 = db.execute("SELECT id FROM users WHERE email = 'a2@pack.test'").fetchone()[0]
        db.execute("INSERT INTO events (user_id, name, data) VALUES (%s, 'subscribed', '{\"plan\": \"pro\"}')", (a2,))
        db.execute("INSERT INTO events (user_id, name, created_at) VALUES (%s, 'visit', now() - interval '8 days')", (a1[0],))   # day 22
        db.execute("INSERT INTO events (user_id, name, created_at) VALUES (%s, 'offer_accept', now() - interval '35 days')", (b1[0],))   # day 5
        # A licence a brand paid for, on b1's verified handle: Unprompted keeps 15%.
        d = db.execute("INSERT INTO digests (email, brand, params, token) VALUES ('brand@pack.test', 'Pack', '{}', 'packtoken0000000001')"
                       " RETURNING id").fetchone()[0]
        db.execute("INSERT INTO videos (id, platform, handle, views, raw) VALUES ('packvid', 'tiktok', 'b1', 10, '{}')"
                   " ON CONFLICT (id) DO NOTHING")
        db.execute("INSERT INTO license_requests (digest_id, video_id, days, price_usd, final_price_usd, brand_paid_at)"
                   " VALUES (%s, 'packvid', 30, 100, 100, now())", (d,))
        db.execute("INSERT INTO creator_handles (user_id, platform, handle, code, verified_at) VALUES (%s, 'tiktok', 'b1', 'UNP-PACK', now())",
                   (b1[0],))
    try:
        b = Browser(base)
        assert b("GET", "/api/admin/packaging")[0] == 401
        status, out = b("GET", "/api/admin/packaging", headers={"Authorization": "Bearer run-secret"})
    finally:
        with psycopg.connect(server.DB_URL) as db:
            db.execute("DELETE FROM digests WHERE token = 'packtoken0000000001'")
    assert status == 200 and out["live"] == ["a", "b", "c"] and out["targetVisitors"] == 300
    arms = {r["arm"]: r for r in out["arms"]}
    assert (arms["a"]["visitors"], arms["a"]["signups"], arms["a"]["eligible"], arms["a"]["week4"]) == (2, 2, 1, 1)
    assert (arms["a"]["subscribed"], arms["a"]["paid"], arms["a"]["mrrUsd"]) == (1, 1, 29)
    assert (arms["b"]["visitors"], arms["b"]["signups"], arms["b"]["week4"], arms["b"]["paid"], arms["b"]["licenceKeptUsd"]) == (1, 1, 0, 1, 15)
    assert (arms["c"]["visitors"], arms["c"]["signups"], arms["c"]["eligible"], arms["c"]["paid"]) == (1, 2, 1, 0)
