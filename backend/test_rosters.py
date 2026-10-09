"""Roster seat for talent managers: pilot access, creators, the Monday report, deals and the roster plan (demo creators,
mocked mail and Stripe: zero Oriane credits).

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_rosters.py
"""
import hashlib
import hmac
import http.cookiejar
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["APP_URL"] = "https://u.test"
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"
os.environ["RESEND_API_KEY"] = ""
os.environ.pop("MAIL_RELAY_URL", None)
os.environ.pop("MAIL_RELAY_SECRET", None)

import psycopg  # noqa: E402
import pytest  # noqa: E402

import creator  # noqa: E402
import digest  # noqa: E402
import rosters  # noqa: E402
import server  # noqa: E402

MANAGER = "manager@agency.test"
OP = {"Authorization": "Bearer run-secret"}


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    server.init_db()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM users WHERE email = %s", (MANAGER,))
        db.execute("DELETE FROM brand_activity")
    monkeypatch.setenv("LICENSE_NOTIFY_EMAIL", "ops@u.test")
    for k in ("STRIPE_PRICE_ROSTER", "STRIPE_SECRET_KEY"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(digest, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __call__(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self.opener.open(req, timeout=30) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)


def manager(base):
    m = Client(base)
    assert m("POST", "/api/creators/signup",
             {"email": MANAGER, "password": "password-123", "role": "manager"})[0] == 201
    return m


def test_pilot_report_deal_and_commitment(base, mail):
    m = manager(base)
    view = m("GET", "/api/rosters/mine")[1]
    assert view["access"] is None and view["hasRoster"] is False and view["priceUsd"] == 99
    assert m("POST", "/api/rosters/creators", {"platform": "tiktok", "handle": "maya.eats"})[0] == 402   # no pilot yet
    assert m("POST", "/api/rosters/request", {"note": "12 creators, beauty"})[1] == {"requested": True}
    assert any(to == "ops@u.test" and s == "Roster pilot request: %s" % MANAGER for to, s, _ in mail)

    # The operator grants a 6-week pilot from /admin/; the manager is told.
    assert m("POST", "/api/admin/rosters", {"email": MANAGER})[0] == 401
    listing = m("POST", "/api/admin/rosters", {"email": MANAGER.upper()}, OP)[1]["rosters"]
    assert next(r for r in listing if r["email"] == MANAGER)["access"] == "pilot"
    assert any(to == MANAGER and "roster pilot is on (6 weeks)" in s for to, s, _ in mail)
    assert m("POST", "/api/admin/rosters", {"email": "nobody@x.test"}, OP)[0] == 404

    for platform, handle, name in (("tiktok", "@Maya.Eats", "Maya"), ("instagram", "sami.lifts", None), ("tiktok", "maya.eats", None)):
        view = m("POST", "/api/rosters/creators", {"platform": platform, "handle": handle, "name": name})[1]
    assert [(c["handle"], c["name"]) for c in view["creators"]] == [("maya.eats", "Maya"), ("sami.lifts", None)]
    assert view["canRunNow"] and view["report"] is None

    # First report: each creator's recent unpaid brands, with receipt, paying signal and pitch; emailed to the manager.
    view = m("POST", "/api/rosters/run", {})[1]
    items = {i["handle"]: i for i in view["report"]["items"]}
    maya, sami = items["maya.eats"], items["sami.lifts"]
    assert maya["first"] and [b["brand"] for b in maya["brands"]][:2] == ["Tim Hortons", "Vimto"] and len(maya["brands"]) == 5
    assert "Gymshark" not in [b["brand"] for b in sami["brands"]]                  # a past sponsor isn't "unpaid"
    top = maya["brands"][0]
    assert top["spoken"] >= 1 and top["url"] and top["quote"] and top["pitch"]["subject"].startswith("Already")
    assert sum(b["paying"] is not None for i in items.values() for b in i["brands"]) == rosters.ACTIVITY_LOOKUPS   # capped lookups
    subject, body = next((s, b) for to, s, b in mail if to == MANAGER and "unpaid brand mention" in s)
    assert subject == "10 unpaid brand mentions across 2 creators this week" and "@maya.eats" in body and "/creators/roster" in body
    assert m("POST", "/api/rosters/run", {})[0] == 429                            # once a day

    # Next week: nothing new in the fixtures, so a quiet report.
    r = rosters.load(_uid())
    rosters.run_roster(r)
    assert mail[-1][1] == "A quiet week: no new unpaid brand mentions on your roster"

    # A deal that came from a report, and committing to the paid plan (no Stripe price yet: recorded for the operator).
    view = m("POST", "/api/rosters/deal", {"reportId": view["report"]["id"], "handle": "maya.eats", "brand": "Tim Hortons", "amountUsd": 1500})[1]
    assert view["deals"][0]["brand"] == "Tim Hortons" and view["deals"][0]["amountUsd"] == 1500
    assert m("POST", "/api/rosters/subscribe", {})[1] == {"committed": True}
    row = next(r for r in m("GET", "/api/admin/rosters", None, OP)[1]["rosters"] if r["email"] == MANAGER)
    assert row["deals"] == 1 and row["committed"] is True and row["reports"] == 2 and row["creators"] == 2
    with psycopg.connect(server.DB_URL) as db:
        names = {n for (n,) in db.execute("SELECT name FROM events WHERE user_id = %s", (_uid(),))}
    assert {"roster_pilot_request", "roster_report", "roster_deal", "roster_commit"} <= names


def _uid():
    with psycopg.connect(server.DB_URL) as db:
        return db.execute("SELECT id FROM users WHERE email = %s", (MANAGER,)).fetchone()[0]


def test_roster_limit_and_monday_cron(base, mail):
    m = manager(base)
    m("POST", "/api/admin/rosters", {"email": MANAGER, "weeks": 2}, OP)
    m("POST", "/api/rosters/creators", {"platform": "tiktok", "handle": "maya.eats"})
    assert rosters.run_rosters(any_day=True) == 1 and rosters.run_rosters(any_day=True) == 0   # once a week
    status, out = m("POST", "/api/digests/run", {}, OP)
    assert status == 200 and "rosters" in out
    with psycopg.connect(server.DB_URL) as db:
        rid = db.execute("SELECT id FROM rosters WHERE user_id = %s", (_uid(),)).fetchone()[0]
        db.execute("INSERT INTO roster_creators (roster_id, platform, handle) SELECT %s, 'tiktok', 'c' || g FROM generate_series(1, 24) g",
                   (rid,))
    assert m("POST", "/api/rosters/creators", {"platform": "tiktok", "handle": "one.more"})[0] == 409
    with psycopg.connect(server.DB_URL) as db:                                    # pilot over: no more reports, no edits
        db.execute("UPDATE rosters SET pilot_until = now() - interval '1 day' WHERE id = %s", (rid,))
    assert m("GET", "/api/rosters/mine")[1]["access"] is None and rosters.run_rosters(any_day=True) == 0


def test_roster_plan_through_stripe(base, mail, monkeypatch):
    m = manager(base)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PRICE_ROSTER", "price_roster")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    calls = []
    monkeypatch.setattr(creator, "stripe", lambda path, params: calls.append((path, params)) or {"url": "https://checkout.stripe.test/roster"})
    assert m("POST", "/api/rosters/subscribe", {})[1] == {"url": "https://checkout.stripe.test/roster"}
    params = calls[-1][1]
    assert params["line_items[0][price]"] == "price_roster" and params["metadata[plan]"] == "roster"
    assert params["success_url"] == "https://u.test/creators/roster?subscribed=1"

    def hook(event):
        payload = json.dumps(event).encode()
        ts = int(time.time())
        sig = hmac.new(b"whsec_test", b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest()
        req = urllib.request.Request(base + "/api/creators/billing/webhook", data=payload, method="POST",
                                     headers={"Content-Type": "application/json", "Stripe-Signature": "t=%d,v1=%s" % (ts, sig)})
        urllib.request.urlopen(req, timeout=20).close()
    hook({"type": "checkout.session.completed", "data": {"object": {"mode": "subscription", "client_reference_id": str(_uid()),
          "customer": "cus_mgr", "subscription": "sub_mgr", "metadata": {"user_id": str(_uid()), "plan": "roster"}}}})
    assert m("GET", "/api/creators/me")[1]["user"]["plan"] == "roster"
    assert m("POST", "/api/rosters/creators", {"platform": "tiktok", "handle": "maya.eats"})[0] == 402   # paid, but no roster row yet
    with psycopg.connect(server.DB_URL) as db:
        db.execute("INSERT INTO rosters (user_id) VALUES (%s)", (_uid(),))
    assert m("GET", "/api/rosters/mine")[1]["access"] == "paid"
    hook({"type": "customer.subscription.updated", "data": {"object": {"customer": "cus_mgr", "id": "sub_mgr", "status": "active"}}})
    assert m("GET", "/api/creators/me")[1]["user"]["plan"] == "roster"            # still active: stays on roster, not Pro
    hook({"type": "customer.subscription.deleted", "data": {"object": {"customer": "cus_mgr", "id": "sub_mgr"}}})
    assert m("GET", "/api/creators/me")[1]["user"]["plan"] == "free"
