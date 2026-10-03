"""Money and renewals for licences: brands pay through Stripe Checkout once the creator accepts, the ad goes live as
soon as it is paid for and the creator has sent the code, the creator's share goes to their Stripe Connect (Express)
account, a decline after payment is refunded, and licences ending within a week remind the brand to renew.

Stripe parts are off unless LICENSE_PAYMENTS=1 and STRIPE_SECRET_KEY + STRIPE_WEBHOOK_SECRET are set; without them the
manual flow in docs/CONCIERGE.md is unchanged (the operator takes payment and pays the creator). Use Stripe test keys
until a test licence has gone through end to end. Webhook events: checkout.session.completed and account.updated, on
the same endpoint as Pro billing (/api/creators/billing/webhook). Expiry, reminders and renewals work either way.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from urllib import error, request
from urllib.parse import urlencode

import creator
import digest
import licenses
from server import ApiError, connect

SCHEMA = """
ALTER TABLE license_requests
  ADD COLUMN IF NOT EXISTS renewal_of            int REFERENCES license_requests ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS renewal_reminded_at   timestamptz,
  ADD COLUMN IF NOT EXISTS stripe_session_id     text,
  ADD COLUMN IF NOT EXISTS stripe_payment_intent text,
  ADD COLUMN IF NOT EXISTS stripe_charge         text,
  ADD COLUMN IF NOT EXISTS stripe_transfer       text,      -- 'pending' while a transfer call is in flight
  ADD COLUMN IF NOT EXISTS refunded_at           timestamptz;
ALTER TABLE license_requests DROP CONSTRAINT IF EXISTS license_requests_digest_id_video_id_key;  -- renewals repeat a video
CREATE UNIQUE INDEX IF NOT EXISTS license_requests_first ON license_requests (digest_id, video_id) WHERE renewal_of IS NULL;
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS stripe_connect_account  text,
  ADD COLUMN IF NOT EXISTS connect_payouts_enabled boolean NOT NULL DEFAULT false;
ALTER TABLE creator_prefs ADD COLUMN IF NOT EXISTS rate_30d_usd int;  -- what the creator wants to receive per 30 days
"""

CONNECT_COUNTRIES = ("AE", "US", "GB", "CA", "AU", "FR", "DE", "IE", "NL", "ES", "IT", "SG", "IN")
RENEWAL_WINDOW_DAYS = 30                     # a licence can be renewed until this long after it ended


def enabled():
    return os.environ.get("LICENSE_PAYMENTS") == "1" and bool(os.environ.get("STRIPE_SECRET_KEY")) \
        and bool(os.environ.get("STRIPE_WEBHOOK_SECRET"))


def stripe_call(path, params=None, key=None):
    """POST (with params) or GET to Stripe. `key` becomes the Idempotency-Key, so a retried call never moves money twice."""
    headers = {"Authorization": "Bearer " + os.environ.get("STRIPE_SECRET_KEY", "")}
    if key:
        headers["Idempotency-Key"] = key
    data = urlencode(params).encode() if params is not None else None
    req = request.Request(creator.STRIPE_API + path, data=data, method="POST" if data is not None else "GET", headers=headers)
    try:
        with request.urlopen(req, timeout=30) as res:
            return json.load(res)
    except error.HTTPError as e:
        try:
            msg = json.loads(e.read())["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = e.reason
        raise ApiError(502, "Stripe returned %s: %s" % (e.code, msg))
    except OSError as e:
        raise ApiError(502, "Can't reach Stripe (%s)." % e)


def load(rid):
    with connect() as db:
        return db.execute(licenses.OFFER_SQL + " WHERE r.id = %s", (rid,)).fetchone()


# ---------------------------------------------------------------- the brand's side of a request (its /license page)

def latest(digest_id, vid):
    with connect() as db:
        return db.execute(licenses.OFFER_SQL + " WHERE r.digest_id = %s AND r.video_id = %s ORDER BY r.id DESC LIMIT 1",
                          (digest_id, vid)).fetchone()


def brand_view(r):
    """What the brand's licence page shows about its latest request for a video."""
    paid = r["brand_paid_at"] is not None
    left = r["expires_at"] - datetime.now(timezone.utc) if r["expires_at"] else None   # renew in the last week, or after
    renewable = left is not None and ((r["status"] == "live" and left <= timedelta(days=7))
                                      or (r["status"] == "ended" and left >= -timedelta(days=RENEWAL_WINDOW_DAYS)))
    return {"id": r["id"], "days": r["days"], "priceUsd": licenses.brand_price(r), "status": r["status"], "note": r["note"],
            "createdAt": r["created_at"], "startsAt": r["starts_at"], "expiresAt": r["expires_at"], "paid": paid,
            "refunded": r["refunded_at"] is not None, "adCode": r["ad_code"] if paid else None,
            "counterUsd": r["creator_price_usd"] if r["status"] == "contacted" and r["responded_via"] else None,
            "renewal": r["renewal_of"] is not None, "renewable": renewable, "payOnline": enabled()}


def brand_action(d, vid, body):
    """pay | accept_counter | decline_counter | renew, from the brand's licence page."""
    r = latest(d["id"], vid)
    if not r:
        raise ApiError(404, "Request this licence first.")
    action = body.get("action")
    if action == "pay":
        return {"checkoutUrl": checkout(d, r)}
    if action in ("accept_counter", "decline_counter"):
        if r["status"] != "contacted" or not r["creator_price_usd"]:
            raise ApiError(409, "There's no counter-offer to answer.")
        with connect() as db:
            if action == "accept_counter":
                db.execute("UPDATE license_requests SET status = 'accepted', final_price_usd = %s WHERE id = %s",
                           (licenses.brand_price(r), r["id"]))
            else:
                db.execute("UPDATE license_requests SET status = 'declined', decline_reason = %s WHERE id = %s",
                           ("%s declined the counter-offer." % d["brand"], r["id"]))
        r = load(r["id"])
        tell(r, "%s %s the counter" % (d["brand"], "accepted" if action == "accept_counter" else "declined"), creator_too=True)
        return None
    if action == "renew":
        renew(d, r)
        return None
    raise ApiError(400, "Unknown action.")


def checkout(d, r):
    """A Stripe Checkout page for an accepted request; the webhook marks it paid."""
    if not enabled():
        raise ApiError(503, "Online payment isn't switched on yet: we'll email you a payment link.")
    if r["status"] != "accepted" or r["brand_paid_at"]:
        raise ApiError(409, "This licence isn't waiting for payment.")
    page = digest.license_url(d["token"], r["video_id"])
    session = stripe_call("checkout/sessions", {
        "mode": "payment", "customer_email": d["email"], "success_url": page + "?paid=1", "cancel_url": page,
        "line_items[0][quantity]": 1, "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": licenses.brand_price(r) * 100,
        "line_items[0][price_data][product_data][name]": "%d-day ad licence: @%s's video for %s" % (r["days"], r["handle"], r["brand"]),
        "metadata[license_request]": r["id"], "payment_intent_data[metadata][license_request]": r["id"],
        "payment_intent_data[transfer_group]": "license-%d" % r["id"]})
    with connect() as db:
        db.execute("UPDATE license_requests SET stripe_session_id = %s WHERE id = %s", (session["id"], r["id"]))
    creator.track(d["user_id"], "license_checkout", {"request": r["id"], "price": licenses.brand_price(r)})
    return session["url"]


def renew(d, r):
    """A new request for the same video, window and price; the creator answers it like any other."""
    if r["status"] not in ("live", "ended") or r["expires_at"] is None:
        raise ApiError(409, "Only a live or recently ended licence can be renewed.")
    with connect() as db:
        if db.execute("SELECT now() > %s + %s * interval '1 day' AS late", (r["expires_at"], RENEWAL_WINDOW_DAYS)).fetchone()["late"]:
            raise ApiError(409, "This licence ended more than %d days ago: request it again from a weekly report." % RENEWAL_WINDOW_DAYS)
        new = db.execute("INSERT INTO license_requests (digest_id, video_id, days, price_usd, note, renewal_of)"
                         " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                         (r["digest_id"], r["video_id"], r["days"], licenses.brand_price(r), "Renewal", r["id"])).fetchone()
    after = licenses.on_request(new["id"])
    creator.track(d["user_id"], "license_renewal", {"request": new["id"], "of": r["id"]})
    tell(after, "%s wants to renew (%d days, $%d)" % (d["brand"], r["days"], licenses.brand_price(r)))


# ---------------------------------------------------------------- going live, paying the creator, refunds

def maybe_go_live(rid):
    """Once an accepted request is both paid for and has its code, start the window, pay the creator and tell everyone."""
    with connect() as db:
        # A renewal picks up where the licence it renews ends, so the brand keeps the days it already paid for.
        row = db.execute("UPDATE license_requests r SET status = 'live', starts_at = coalesce(r.starts_at, greatest(now(),"
                         " (SELECT o.expires_at FROM license_requests o WHERE o.id = r.renewal_of))) WHERE r.id = %s"
                         " AND r.status = 'accepted' AND r.brand_paid_at IS NOT NULL AND r.code_received_at IS NOT NULL RETURNING r.id",
                         (rid,)).fetchone()
        if row:
            db.execute("UPDATE license_requests SET expires_at = coalesce(expires_at, starts_at + days * interval '1 day') WHERE id = %s",
                       (rid,))
    if not row:
        return False
    r = load(rid)
    pay_creator(rid)
    with connect() as db:
        brand = db.execute("SELECT email, token FROM digests WHERE id = %s", (r["digest_id"],)).fetchone()
    licenses.notify("Live: @%s's video for %s, until %s" % (r["handle"], r["brand"], day(r["expires_at"])),
                    "Use this code to run the ad. Your licence page has it too.",
                    [("Ad code", r["ad_code"]), ("Video", "@%s on %s" % (r["handle"], r["platform"])),
                     ("Runs until", day(r["expires_at"])), ("Licence page", digest.license_url(brand["token"], r["video_id"]))],
                    to=brand["email"])
    tell(load(rid), "is live until %s" % day(r["expires_at"]), creator_too=True)
    return True


def day(ts):
    return "%d %s" % (ts.day, ts.strftime("%b %Y")) if ts else "-"


def pay_creator(rid):
    """Transfer the creator's share for a live licence the brand paid through Stripe, if they've connected payouts.
    Claimed with stripe_transfer = 'pending' first, and sent with an idempotency key, so it can't go out twice."""
    if not enabled():
        return None
    r = load(rid)
    if r["status"] != "live" or not r["stripe_charge"] or r["creator_paid_at"] or r["owner_id"] is None:
        return None
    with connect() as db:
        acct = db.execute("SELECT stripe_connect_account, connect_payouts_enabled FROM users WHERE id = %s", (r["owner_id"],)).fetchone()
        if not acct or not acct["stripe_connect_account"] or not acct["connect_payouts_enabled"]:
            return None
        if not db.execute("UPDATE license_requests SET stripe_transfer = 'pending' WHERE id = %s AND stripe_transfer IS NULL"
                          " RETURNING id", (rid,)).fetchone():
            return None
    try:
        t = stripe_call("transfers", {"amount": licenses.creator_share(r) * 100, "currency": "usd",
                                      "destination": acct["stripe_connect_account"], "source_transaction": r["stripe_charge"],
                                      "transfer_group": "license-%d" % rid, "metadata[license_request]": rid},
                        key="license-transfer-%d" % rid)
    except ApiError as e:
        with connect() as db:
            db.execute("UPDATE license_requests SET stripe_transfer = NULL WHERE id = %s", (rid,))
        licenses.notify("Transfer failed for request %d" % rid, "Pay the creator by hand and tick Creator paid in /admin/.",
                        [("Creator", "@" + r["handle"]), ("Amount", "$%d" % licenses.creator_share(r)), ("Stripe", str(e))])
        return None
    with connect() as db:
        db.execute("UPDATE license_requests SET stripe_transfer = %s, creator_paid_at = now() WHERE id = %s", (t["id"], rid))
    creator.track(r["owner_id"], "creator_paid", {"request": rid, "amount": licenses.creator_share(r)})
    return t["id"]


def pay_owed(user_id):
    """Everything this creator is owed for Stripe-paid licences (e.g. they connected payouts after going live)."""
    with connect() as db:
        rows = db.execute(licenses.OFFER_SQL + " WHERE h.user_id = %s AND r.status = 'live' AND r.stripe_charge IS NOT NULL"
                          " AND r.creator_paid_at IS NULL", (user_id,)).fetchall()
    return [t for t in (pay_creator(r["id"]) for r in rows) if t]


def refund_if_paid(rid):
    """A licence declined after the brand paid through Stripe is refunded in full."""
    r = load(rid)
    if not enabled() or not r["stripe_payment_intent"] or r["refunded_at"] or r["status"] != "declined":
        return False
    stripe_call("refunds", {"payment_intent": r["stripe_payment_intent"], "metadata[license_request]": rid},
                key="license-refund-%d" % rid)
    with connect() as db:
        db.execute("UPDATE license_requests SET refunded_at = now() WHERE id = %s", (rid,))
        brand = db.execute("SELECT email FROM digests WHERE id = %s", (r["digest_id"],)).fetchone()
    licenses.notify("Refunded: @%s's video for %s" % (r["handle"], r["brand"]),
                    "The creator declined, so the full payment is on its way back to your card.",
                    [("Amount", "$%d" % licenses.brand_price(r))], to=brand["email"])
    return True


def tell_brand(rid, event):
    """Email the brand when the creator (or their rule, or the operator) answers: accepted means pay; countered means
    answer the new price; declined means nothing was charged (a paid one is refunded and emailed by refund_if_paid)."""
    r = load(rid)
    if not r or (event == "declined" and r["brand_paid_at"]):
        return False
    with connect() as db:
        brand = db.execute("SELECT email, token FROM digests WHERE id = %s", (r["digest_id"],)).fetchone()
    page = digest.license_url(brand["token"], r["video_id"])
    subject, lead = {
        "accepted": ("@%s said yes: %s the licence" % (r["handle"], "pay $%d to start" % licenses.brand_price(r) if enabled() else "next, payment"),
                     "Pay on the licence page and the ad code is released as soon as it's paid." if enabled()
                     else "We'll email you a payment link; the ad code follows once it's paid."),
        "countered": ("@%s asked for $%d instead (you'd pay $%d)" % (r["handle"], r["creator_price_usd"] or 0, licenses.brand_price(r)),
                      "Accept or decline the new price on the licence page."),
        "declined": ("@%s passed on this one" % r["handle"], "Nothing was charged. Your next weekly report may have another clip."),
    }[event]
    return licenses.notify(subject, lead, [("Video", "@%s on %s" % (r["handle"], r["platform"])), ("Window", "%d days" % r["days"]),
                                           ("Licence page", page)], to=brand["email"])


def tell(r, what, creator_too=False):
    """A one-line status email to the operator, and to the creator when they've claimed the handle."""
    rows = [("Brand", r["brand"]), ("Creator", "@%s on %s" % (r["handle"], r["platform"])), ("Status", r["status"]),
            ("Brand pays", "$%d" % licenses.brand_price(r)), ("Creator gets", "$%d" % licenses.creator_share(r)), ("Request id", r["id"])]
    licenses.notify("@%s: %s" % (r["handle"], what), "See /admin/ for the details.", rows)
    if creator_too and r["owner_id"] is not None:
        with connect() as db:
            email = db.execute("SELECT email FROM users WHERE id = %s", (r["owner_id"],)).fetchone()["email"]
        licenses.notify("%s: %s" % (r["brand"], what), "Your offer page has the details.",
                        [("Offer", licenses.offer_url(r["creator_token"]))], to=email)


# ---------------------------------------------------------------- Stripe webhook (called from creator.webhook)

def on_event(event):
    obj = event.get("data", {}).get("object", {})
    kind = event.get("type", "")
    rid = (obj.get("metadata") or {}).get("license_request")
    if kind == "checkout.session.completed" and obj.get("mode") == "payment" and rid:
        r = load(int(rid))
        if not r or obj.get("payment_status") != "paid":
            return
        expected = licenses.brand_price(r) * 100
        charge = None
        if obj.get("payment_intent"):
            charge = stripe_call("payment_intents/%s" % obj["payment_intent"]).get("latest_charge")
        with connect() as db:
            db.execute("UPDATE license_requests SET brand_paid_at = coalesce(brand_paid_at, now()), stripe_payment_intent = %s,"
                       " stripe_charge = %s WHERE id = %s", (obj.get("payment_intent"), charge, r["id"]))
        if obj.get("amount_total") != expected:
            licenses.notify("Check payment for request %d" % r["id"], "Stripe's amount doesn't match the agreed price.",
                            [("Paid (cents)", obj.get("amount_total")), ("Expected (cents)", expected)])
        creator.track(None, "license_paid", {"request": r["id"], "amount": obj.get("amount_total")})
        if not maybe_go_live(r["id"]):
            tell(load(r["id"]), "%s has paid: send the ad code to go live" % r["brand"], creator_too=True)
    elif kind == "account.updated":
        with connect() as db:
            u = db.execute("UPDATE users SET connect_payouts_enabled = %s WHERE stripe_connect_account = %s RETURNING id",
                           (bool(obj.get("payouts_enabled")), obj.get("id"))).fetchone()
        if u and obj.get("payouts_enabled"):
            pay_owed(u["id"])


# ---------------------------------------------------------------- creator payouts (Stripe Connect Express)

def connect_view(user):
    with connect() as db:
        u = db.execute("SELECT stripe_connect_account, connect_payouts_enabled FROM users WHERE id = %s", (user["id"],)).fetchone()
    return {"online": enabled(), "connected": bool(u["stripe_connect_account"]), "payoutsEnabled": u["connect_payouts_enabled"],
            "countries": list(CONNECT_COUNTRIES)}


def connect_onboard(user, body):
    """A Stripe-hosted onboarding link (creating the Express account the first time)."""
    if not enabled():
        raise ApiError(503, "Automatic payouts aren't switched on yet: we'll pay you by bank transfer or PayPal.")
    with connect() as db:
        acct = db.execute("SELECT stripe_connect_account FROM users WHERE id = %s", (user["id"],)).fetchone()["stripe_connect_account"]
    if not acct:
        country = str(body.get("country") or "").upper()
        if country not in CONNECT_COUNTRIES:
            raise ApiError(400, "Pick the country your bank account is in.")
        params = {"type": "express", "country": country, "email": user["email"], "capabilities[transfers][requested]": "true",
                  "metadata[user_id]": user["id"]}
        if country != os.environ.get("STRIPE_PLATFORM_COUNTRY", country):   # cross-border: payouts only
            params["tos_acceptance[service_agreement]"] = "recipient"
        acct = stripe_call("accounts", params, key="connect-account-%d" % user["id"])["id"]
        with connect() as db:
            db.execute("UPDATE users SET stripe_connect_account = %s WHERE id = %s", (acct, user["id"]))
    inbox = licenses.inbox_url()
    link = stripe_call("account_links", {"account": acct, "type": "account_onboarding",
                                         "refresh_url": inbox + "?connect=retry", "return_url": inbox + "?connect=done"})
    return {"url": link["url"]}


def connect_refresh(user):
    """After onboarding: read the account's payout state now instead of waiting for the webhook, and pay what's owed."""
    view = connect_view(user)
    if not enabled() or not view["connected"]:
        return view
    with connect() as db:
        acct = db.execute("SELECT stripe_connect_account FROM users WHERE id = %s", (user["id"],)).fetchone()["stripe_connect_account"]
    on = bool(stripe_call("accounts/%s" % acct).get("payouts_enabled"))
    with connect() as db:
        db.execute("UPDATE users SET connect_payouts_enabled = %s WHERE id = %s", (on, user["id"]))
    if on:
        pay_owed(user["id"])
    return connect_view(user)


# ---------------------------------------------------------------- expiry and renewal reminders (scheduler / cron)

def run_reminders():
    """End licences past their window, and remind brands a week before one ends (once, and not if already renewed)."""
    with connect() as db:
        ended = db.execute("UPDATE license_requests SET status = 'ended' WHERE status = 'live' AND expires_at < now() RETURNING id").fetchall()
        due = db.execute(
            licenses.OFFER_SQL + " WHERE r.status = 'live' AND r.expires_at < now() + interval '7 days' AND r.renewal_reminded_at IS NULL"
            " AND NOT EXISTS (SELECT 1 FROM license_requests n WHERE n.renewal_of = r.id)").fetchall()
        for r in due:
            db.execute("UPDATE license_requests SET renewal_reminded_at = now() WHERE id = %s", (r["id"],))
    for r in due:
        with connect() as db:
            brand = db.execute("SELECT email, token FROM digests WHERE id = %s", (r["digest_id"],)).fetchone()
        licenses.notify("Ends %s: @%s's video for %s. Renew?" % (day(r["expires_at"]), r["handle"], r["brand"]),
                        "One click renews it for another %d days at $%d; the creator confirms and sends a new code "
                        "(TikTok codes can't be extended)." % (r["days"], licenses.brand_price(r)),
                        [("Renew", digest.license_url(brand["token"], r["video_id"]))], to=brand["email"])
    return {"ended": len(ended), "reminded": len(due)}
