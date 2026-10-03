"""Licence payments, payouts, refunds, renewals and creator rates (Stripe and Oriane mocked: nothing real is charged).

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_payments.py
"""
import hashlib
import hmac
import json
import os
import time
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["APP_URL"] = "https://u.test"
os.environ["RESEND_API_KEY"] = ""
os.environ.pop("MAIL_RELAY_URL", None)
os.environ.pop("MAIL_RELAY_SECRET", None)

import psycopg  # noqa: E402
import pytest  # noqa: E402

import creator  # noqa: E402
import digest  # noqa: E402
import licenses  # noqa: E402
import payments  # noqa: E402
import server  # noqa: E402

EMAILS = ("pay-ali@creator.test",)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    server.init_db()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM digests")
        db.execute("DELETE FROM mentions WHERE search_id IN (SELECT id FROM searches WHERE brand = 'Tim Hortons')")
        db.execute("DELETE FROM searches WHERE brand = 'Tim Hortons'")
        db.execute("DELETE FROM users WHERE email = ANY(%s)", (list(EMAILS),))
        db.execute("DELETE FROM creator_handles WHERE handle = 'ali'")   # verified in the other licence tests
    monkeypatch.setenv("LICENSE_NOTIFY_EMAIL", "ops@u.test")


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(digest, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


@pytest.fixture
def stripe(monkeypatch):
    """Payments on, with a fake Stripe that records every call."""
    monkeypatch.setenv("LICENSE_PAYMENTS", "1")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    calls = []

    def fake(path, params=None, key=None):
        calls.append((path, params, key))
        if path == "checkout/sessions":
            return {"id": "cs_%d" % len(calls), "url": "https://checkout.stripe.test/%d" % len(calls)}
        if path.startswith("payment_intents/"):
            return {"latest_charge": "ch_" + path.split("_")[-1]}
        if path == "transfers":
            return {"id": "tr_%s" % params["metadata[license_request]"]}
        if path == "refunds":
            return {"id": "re_1"}
        if path == "accounts":
            return {"id": "acct_1"}
        if path == "account_links":
            return {"url": "https://connect.stripe.test/onboard"}
        if path.startswith("accounts/"):
            return {"payouts_enabled": True}
        raise AssertionError(path)
    monkeypatch.setattr(payments, "stripe_call", fake)
    return calls


def video(vid, handle="ali", views=1200):
    text = "a Tim Hortons iced capp every single morning"
    return {"id": vid, "platform": "tiktok", "platformId": vid, "profileHandle": handle, "viewsCount": views, "caption": "", "hashtags": [],
            "mentions": [], "coAuthors": [], "transcript": text, "transcriptChunks": [{"startSeconds": 12.0, "endSeconds": 16.0, "text": text}],
            "publishedAt": (date.today() - timedelta(days=1)).isoformat() + "T10:00:00Z", "frames": [], "thumbnailMediaUrl": ""}


def setup(monkeypatch, vid, payouts=True):
    """A verified creator (Stripe payouts on by default), a watch whose report had `vid`, and a 30-day request for it."""
    with psycopg.connect(server.DB_URL, row_factory=psycopg.rows.dict_row) as db:
        ali = db.execute("INSERT INTO users (email, password, stripe_connect_account, connect_payouts_enabled) VALUES"
                         " (%s, 'x', %s, %s) RETURNING *", (EMAILS[0], "acct_ali" if payouts else None, payouts)).fetchone()
        db.execute("INSERT INTO creator_handles (user_id, platform, handle, code, verified_at) VALUES (%s, 'tiktok', 'ali', 'UNP-1', now())",
                   (ali["id"],))
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    monkeypatch.setattr(server, "oriane", lambda *a, **k: {"data": {"results": [video(vid)], "aggregations": {"totalViewsCount": 0}},
                                                           "metadata": {}})
    digest.run_due(force=True)
    digest.license_view(d["token"], vid, {"days": 30})
    with psycopg.connect(server.DB_URL, row_factory=psycopg.rows.dict_row) as db:
        r = db.execute("SELECT id, creator_token FROM license_requests WHERE video_id = %s ORDER BY id DESC LIMIT 1", (vid,)).fetchone()
    return ali, d, r["id"], r["creator_token"]


def signed(event):
    payload = json.dumps(event).encode()
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest()
    return payload, {"Stripe-Signature": "t=%d,v1=%s" % (ts, sig)}


def paid_event(rid, amount, pi="pi_%d"):
    return {"type": "checkout.session.completed", "data": {"object": {
        "mode": "payment", "payment_status": "paid", "amount_total": amount, "payment_intent": pi % rid,
        "metadata": {"license_request": str(rid)}}}}


def brand_request(d, vid):
    return digest.license_view(d["token"], vid)["request"]


def test_pay_then_code_goes_live_and_pays_the_creator_once(monkeypatch, mail, stripe):
    ali, d, rid, token = setup(monkeypatch, "pp1")
    licenses.answer(token, {"action": "accept"})
    req = brand_request(d, "pp1")
    assert req["status"] == "accepted" and req["payOnline"] and not req["paid"] and req["priceUsd"] == 50

    out = digest.license_view(d["token"], "pp1", {"action": "pay"})
    path, params, _ = stripe[-1]
    assert out == {"checkoutUrl": "https://checkout.stripe.test/%d" % len(stripe)} and path == "checkout/sessions"
    assert params["line_items[0][price_data][unit_amount]"] == 5000 and params["metadata[license_request]"] == rid
    assert params["success_url"] == "https://u.test/license/%s/pp1?paid=1" % d["token"] and params["mode"] == "payment"

    creator.webhook(*signed(paid_event(rid, 5000)))           # the real webhook path, signature and all
    req = brand_request(d, "pp1")
    assert req["paid"] and req["status"] == "accepted" and req["adCode"] is None   # paid, waiting for the code
    assert any(to == EMAILS[0] and "has paid" in s for to, s, _ in mail)

    licenses.answer(token, {"action": "code", "code": "SPARK-1"})                   # code arrives: live, creator paid
    req = brand_request(d, "pp1")
    assert req["status"] == "live" and req["adCode"] == "SPARK-1" and req["expiresAt"]
    transfers = [p for path, p, _ in stripe if path == "transfers"]
    assert transfers == [{"amount": 4200, "currency": "usd", "destination": "acct_ali", "source_transaction": "ch_%d" % rid,
                          "transfer_group": "license-%d" % rid, "metadata[license_request]": rid}]
    assert [k for path, _, k in stripe if path == "transfers"] == ["license-transfer-%d" % rid]
    assert any(to == "brand@example.com" and s.startswith("Live: @ali") and "SPARK-1" in b for to, s, b in mail)
    assert payments.pay_creator(rid) is None and payments.pay_owed(ali["id"]) == []              # never twice
    offer = licenses.offer_view(licenses.load_offer(token))
    assert offer["creatorPaidAt"] and offer["brandPaid"]


def test_decline_after_payment_is_refunded(monkeypatch, mail, stripe):
    _, d, rid, token = setup(monkeypatch, "pp2")
    licenses.answer(token, {"action": "accept"})
    payments.on_event(paid_event(rid, 5000))
    licenses.answer(token, {"action": "decline", "reason": "changed my mind"})
    assert [(p, k) for path, p, k in stripe if path == "refunds"] == [({"payment_intent": "pi_%d" % rid, "metadata[license_request]": rid},
                                                                       "license-refund-%d" % rid)]
    req = brand_request(d, "pp2")
    assert req["status"] == "declined" and req["refunded"] and any("Refunded" in s for _, s, _ in mail)


def test_brand_accepts_a_counter_and_pays_the_new_price(monkeypatch, mail, stripe):
    _, d, rid, token = setup(monkeypatch, "pp3")
    licenses.answer(token, {"action": "counter", "price": 60})
    req = brand_request(d, "pp3")
    assert req["counterUsd"] == 60 and req["priceUsd"] == 71
    digest.license_view(d["token"], "pp3", {"action": "accept_counter"})
    assert brand_request(d, "pp3")["status"] == "accepted"
    digest.license_view(d["token"], "pp3", {"action": "pay"})
    assert stripe[-1][1]["line_items[0][price_data][unit_amount]"] == 7100
    with pytest.raises(server.ApiError) as err:
        digest.license_view(d["token"], "pp3", {"action": "accept_counter"})
    assert err.value.status == 409


def test_reminder_renewal_and_end(monkeypatch, mail, stripe):
    _, d, rid, token = setup(monkeypatch, "pp4")
    with psycopg.connect(server.DB_URL) as db:
        db.execute("UPDATE license_requests SET status = 'live', starts_at = now() - interval '27 days',"
                   " expires_at = now() + interval '3 days' WHERE id = %s", (rid,))
    assert payments.run_reminders() == {"ended": 0, "reminded": 1}
    assert any(to == "brand@example.com" and s.startswith("Ends ") and digest.license_url(d["token"], "pp4") in b for to, s, b in mail)
    assert payments.run_reminders() == {"ended": 0, "reminded": 0}                   # once
    assert brand_request(d, "pp4")["renewable"]

    n = len(mail)
    digest.license_view(d["token"], "pp4", {"action": "renew"})
    req = brand_request(d, "pp4")
    assert req["renewal"] and req["status"] == "requested" and req["priceUsd"] == 50 and not req["renewable"]
    assert any(to == EMAILS[0] and "wants to run your video" in s for to, s, _ in mail[n:])  # the creator is asked again
    with psycopg.connect(server.DB_URL) as db:
        db.execute("UPDATE license_requests SET expires_at = now() - interval '1 hour' WHERE id = %s", (rid,))
    assert payments.run_reminders() == {"ended": 1, "reminded": 0}
    with psycopg.connect(server.DB_URL) as db:
        assert db.execute("SELECT status FROM license_requests WHERE id = %s", (rid,)).fetchone()[0] == "ended"


def test_creator_rate_sets_the_price_brands_see(monkeypatch, mail, stripe):
    ali, d, rid, token = setup(monkeypatch, "pp5")
    licenses.set_prefs(ali, {"rate30dUsd": 100})
    view = digest.license_view(d["token"], "pp5")
    assert view["prices"] == [{"days": 30, "usd": 118}, {"days": 60, "usd": 236}, {"days": 90, "usd": 353}]
    assert "~$118 / 30 days" in digest.item_html({**video("pp5"), "id": "pp5", "handle": "ali", "views": 1200, "kind": "spoken",
                                                  "url": "u", "quote": None}, d["token"])
    assert licenses.mine(ali)["prefs"]["rate30dUsd"] == 100


def test_payouts_onboarding_and_paying_what_is_owed(monkeypatch, mail, stripe):
    ali, d, rid, token = setup(monkeypatch, "pp6", payouts=False)
    assert licenses.mine(ali)["payouts"] == {"online": True, "connected": False, "payoutsEnabled": False,
                                             "countries": list(payments.CONNECT_COUNTRIES)}
    licenses.answer(token, {"action": "accept"})
    payments.on_event(paid_event(rid, 5000))
    licenses.answer(token, {"action": "code", "code": "SPARK-6"})
    assert not [1 for path, _, _ in stripe if path == "transfers"]                     # live, but nowhere to send it yet
    with pytest.raises(server.ApiError):
        payments.connect_onboard(ali, {"country": "ZZ"})
    assert payments.connect_onboard(ali, {"country": "AE"}) == {"url": "https://connect.stripe.test/onboard"}
    acct = next(p for path, p, _ in stripe if path == "accounts")
    assert acct["type"] == "express" and acct["country"] == "AE" and acct["capabilities[transfers][requested]"] == "true"
    payments.on_event({"type": "account.updated", "data": {"object": {"id": "acct_1", "payouts_enabled": True}}})
    assert [p["destination"] for path, p, _ in stripe if path == "transfers"] == ["acct_1"]   # the owed share went out
    assert licenses.mine(ali)["payouts"]["payoutsEnabled"] is True


def test_payments_off_keeps_the_manual_flow(monkeypatch, mail):
    monkeypatch.delenv("LICENSE_PAYMENTS", raising=False)
    _, d, rid, token = setup(monkeypatch, "pp7")
    licenses.answer(token, {"action": "accept"})
    assert brand_request(d, "pp7")["payOnline"] is False
    with pytest.raises(server.ApiError) as err:
        digest.license_view(d["token"], "pp7", {"action": "pay"})
    assert err.value.status == 503
    licenses.answer(token, {"action": "code", "code": "SPARK-7"})
    digest.admin_update(rid, {"brandPaid": True})          # the operator records a payment link that was paid
    req = brand_request(d, "pp7")
    assert req["status"] == "live" and req["adCode"] == "SPARK-7"                      # paid + code = live, no Stripe


def test_renewal_starts_when_the_licence_it_renews_ends(monkeypatch, mail, stripe):
    _, d, rid, token = setup(monkeypatch, "pp8")
    with psycopg.connect(server.DB_URL) as db:
        db.execute("UPDATE license_requests SET status = 'live', starts_at = now() - interval '24 days',"
                   " expires_at = now() + interval '6 days' WHERE id = %s", (rid,))
    digest.license_view(d["token"], "pp8", {"action": "renew"})
    with psycopg.connect(server.DB_URL) as db:
        new_id, new_token = db.execute("SELECT id, creator_token FROM license_requests WHERE renewal_of = %s", (rid,)).fetchone()
    n = len(mail)
    licenses.answer(new_token, {"action": "accept"})
    assert any(to == "brand@example.com" and "said yes: pay $50 to start" in s for to, s, _ in mail[n:])   # the brand is told
    payments.on_event(paid_event(new_id, 5000))
    licenses.answer(new_token, {"action": "code", "code": "SPARK-RENEW"})
    with psycopg.connect(server.DB_URL) as db:
        old_end, new_start, new_end = db.execute(
            "SELECT o.expires_at, n.starts_at, n.expires_at FROM license_requests o JOIN license_requests n ON n.renewal_of = o.id"
            " WHERE o.id = %s", (rid,)).fetchone()
    assert new_start == old_end and (new_end - new_start).days == 30           # no paid days lost to the overlap


def test_brand_hears_every_answer(monkeypatch, mail, stripe):
    ali, d, rid, token = setup(monkeypatch, "pp9")
    n = len(mail)
    digest.admin_update(rid, {"status": "declined"})                          # the operator records a no
    assert any(to == "brand@example.com" and s == "@ali passed on this one" for to, s, _ in mail[n:])
    licenses.set_prefs(ali, {"blockBrands": ["Tim Hortons"]})                 # a creator's rule says no on the spot
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM license_requests WHERE id = %s", (rid,))
    n = len(mail)
    digest.license_view(d["token"], "pp9", {"days": 30})
    licenses.set_prefs(ali, {"blockBrands": []})
    assert any(to == "brand@example.com" and s == "@ali passed on this one" for to, s, _ in mail[n:])
