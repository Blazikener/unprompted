"""The console's setup and health panel (ops.py) and Stripe subscriptions that remember their plan.

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_ops.py
"""
import hashlib
import hmac
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"

import psycopg  # noqa: E402
import pytest  # noqa: E402

import creator  # noqa: E402
import ops  # noqa: E402
import server  # noqa: E402

SETTINGS = ("ORIANE_API_KEY", "MAIL_RELAY_URL", "MAIL_RELAY_SECRET", "RESEND_API_KEY", "DIGEST_FROM", "LICENSE_NOTIFY_EMAIL",
            "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_PRO", "STRIPE_PRICE_LEADS", "STRIPE_PRICE_ROSTER",
            "LICENSE_PAYMENTS", "DIGEST_SCHEDULER")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    server.init_db()
    for k in SETTINGS:
        monkeypatch.delenv(k, raising=False)
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM users WHERE email LIKE '%%@ops.test'")
        db.execute("DELETE FROM digests WHERE email LIKE '%%@ops.test'")


def user(email, plan="free"):
    with psycopg.connect(server.DB_URL) as db:
        return db.execute("INSERT INTO users (email, password, plan) VALUES (%s, 'x', %s) RETURNING id", (email, plan)).fetchone()[0]


def event(uid, name, data=None):
    with psycopg.connect(server.DB_URL) as db:
        db.execute("INSERT INTO events (user_id, name, data) VALUES (%s, %s, %s)", (uid, name, json.dumps(data or {})))


def test_setup_says_what_is_missing(monkeypatch):
    rows = {r["name"]: r["ok"] for r in ops.setup()}
    assert not any(rows.values())
    monkeypatch.setenv("RESEND_API_KEY", "re_x")
    assert not {r["name"]: r["ok"] for r in ops.setup()}["Email"]                      # Resend needs DIGEST_FROM too
    monkeypatch.setenv("DIGEST_FROM", "Unprompted <hi@u.test>")
    monkeypatch.setenv("LICENSE_PAYMENTS", "yes")                                      # flags are on only when "1"
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_x")
    rows = {r["name"]: r for r in ops.setup()}
    assert rows["Email"]["ok"] and not rows["Licence payments"]["ok"] and not rows["Stripe"]["ok"]
    assert rows["Stripe"]["settings"] == ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"]
    assert all(set(r) == {"name", "ok", "unlocks", "settings"} for r in ops.setup())   # never a value


def test_overdue_reports_and_people_waiting():
    with psycopg.connect(server.DB_URL) as db:
        for brand, last, paused in (("Late", "9 days", None), ("Fine", "2 days", None), ("Paused", "20 days", "now()")):
            db.execute("INSERT INTO digests (email, brand, params, token, confirmed_at, last_run_at, paused_at)"
                       " VALUES ('w@ops.test', %%s, '{}', %%s, now(), now() - interval '%s', %s)" % (last, paused or "NULL"),
                       (brand, "opstoken-" + brand.lower().ljust(12, "x")))
    before = ops.report()["weekly"]["overdueWatches"]
    assert before >= 1
    manager, other, reserver, payer, committed = (user("m@ops.test"), user("granted@ops.test"), user("r@ops.test"), user("p@ops.test", "pro"),
                                                  user("c@ops.test"))
    event(manager, "roster_pilot_request", {"note": "12 food creators"})
    event(other, "roster_pilot_request")
    with psycopg.connect(server.DB_URL) as db:
        db.execute("INSERT INTO rosters (user_id) VALUES (%s)", (other,))
    event(reserver, "checkout_intent", {"plan": "leads"})
    event(payer, "checkout_intent", {"plan": "pro"})                                  # bought it since: not waiting
    event(committed, "roster_commit")
    wait = ops.report()["waiting"]
    mine = lambda rows: [r for r in rows if r["email"].endswith("@ops.test")]   # noqa: E731
    assert [(r["email"], r["note"]) for r in mine(wait["pilots"])] == [("m@ops.test", "12 food creators")]
    assert [(r["email"], r["plan"]) for r in mine(wait["reservations"])] == [("r@ops.test", "leads")]
    assert [r["email"] for r in mine(wait["commitments"])] == ["c@ops.test"]


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


def test_health_route_is_operator_only(base):
    def get(token):
        req = urllib.request.Request(base + "/api/admin/health", headers={"Authorization": "Bearer " + token} if token else {})
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    assert get(None)[0] == 401 and get("wrong")[0] == 401
    status, out = get("run-secret")
    assert status == 200 and set(out) == {"setup", "weekly", "waiting"} and out["weekly"]["dailyBudget"] == 600


def signed(event_obj, secret="whsec_x"):
    payload, ts = json.dumps(event_obj).encode(), int(time.time())
    sig = hmac.new(secret.encode(), b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest()
    return payload, {"Stripe-Signature": "t=%d,v1=%s" % (ts, sig)}


def test_subscriptions_remember_their_plan(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_x")
    monkeypatch.setenv("STRIPE_PRICE_LEADS", "price_leads")
    sent = []
    monkeypatch.setattr(creator, "stripe", lambda path, params: sent.append(params) or {"url": "https://checkout.test/s"})
    uid = user("sub@ops.test")
    with psycopg.connect(server.DB_URL, row_factory=psycopg.rows.dict_row) as db:
        u = db.execute("SELECT * FROM users WHERE id = %s", (uid,)).fetchone()
    creator.checkout(u, "leads")
    assert sent[0]["subscription_data[metadata][plan]"] == "leads" and sent[0]["subscription_data[metadata][user_id]"] == uid
    with psycopg.connect(server.DB_URL) as db:
        db.execute("UPDATE users SET stripe_customer = 'cus_ops' WHERE id = %s", (uid,))
    # The subscription's own update can arrive before (or without) the checkout event: it still knows the plan.
    creator.webhook(*signed({"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_ops", "customer": "cus_ops", "status": "active", "metadata": {"plan": "leads"}}}}))
    plan = lambda: psycopg.connect(server.DB_URL).execute("SELECT plan FROM users WHERE id = %s", (uid,)).fetchone()[0]   # noqa: E731
    assert plan() == "leads"
    creator.webhook(*signed({"type": "customer.subscription.updated", "data": {"object": {
        "id": "sub_ops", "customer": "cus_ops", "status": "active", "metadata": {"plan": "free"}}}}))
    assert plan() == "leads"                                                          # nonsense metadata can't downgrade it
    creator.webhook(*signed({"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_ops", "customer": "cus_ops"}}}))
    assert plan() == "free"
    creator.webhook(*signed({"type": "customer.subscription.updated", "data": {"object": {   # an older subscription: Pro, as before
        "id": "sub_old", "customer": "cus_ops", "status": "active"}}}))
    assert plan() == "pro"
