"""Creator side of licensing: answer a brand's request from a link, claim a handle, an inbox, and automatic rules.

Every "License for ads" request (digest.license_view) gets a creator_token. The operator's message to the creator
links /offer/<token>: no account needed, the link is the key (like the brand's /license link). A creator who claims
their handle (TikTok: a code in the bio; Instagram: the operator approves) sees every request for it at
/creators/licenses, is emailed when one arrives, and can set rules: a minimum fee, brands to approve automatically,
and brands to decline. Nothing is charged here; payment is still brokered by hand (docs/CONCIERGE.md).
"""
import math
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import psycopg

import creator
import digest
import tiktok_public
from server import HANDLE_RE, ApiError, connect

SCHEMA = """
ALTER TABLE license_requests
  ADD COLUMN IF NOT EXISTS creator_token  text UNIQUE DEFAULT replace(gen_random_uuid()::text, '-', ''),  -- /offer/<token>
  ADD COLUMN IF NOT EXISTS responded_via  text,      -- app (the creator) | auto (their rules) | operator
  ADD COLUMN IF NOT EXISTS ad_code        text,      -- the Spark code, or the creator's note that the partnership ad is approved
  ADD COLUMN IF NOT EXISTS decline_reason text;
CREATE TABLE IF NOT EXISTS creator_handles (
  id          serial PRIMARY KEY,
  user_id     int  NOT NULL REFERENCES users ON DELETE CASCADE,
  platform    text NOT NULL,
  handle      text NOT NULL,                        -- lowercase, no @
  code        text NOT NULL,                        -- goes in the TikTok bio, or to the operator for Instagram
  verified_at timestamptz,
  verified_by text,                                 -- bio | operator
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (user_id, platform, handle)
);
CREATE UNIQUE INDEX IF NOT EXISTS creator_handles_verified ON creator_handles (platform, handle) WHERE verified_at IS NOT NULL;
CREATE TABLE IF NOT EXISTS creator_prefs (
  user_id        int PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  min_price_usd  int,                               -- the creator's own share; below it, requests are declined for them
  approve_brands text[] NOT NULL DEFAULT '{}',      -- requests from these brands are accepted for them
  block_brands   text[] NOT NULL DEFAULT '{}'       -- and from these, declined
);
ALTER TABLE creator_prefs ADD COLUMN IF NOT EXISTS weekly_summary boolean NOT NULL DEFAULT true;
CREATE TABLE IF NOT EXISTS creator_summaries (     -- when each creator last got the weekly summary email
  user_id int PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  sent_at timestamptz NOT NULL
);
"""

CREATOR_SHARE = 0.85                    # the pilot pays creators 85% of what the brand pays
PLATFORMS = ("tiktok", "instagram")
OFFER_SQL = """
SELECT r.*, d.brand, v.handle, v.platform, h.user_id AS owner_id
FROM license_requests r JOIN digests d ON d.id = r.digest_id JOIN videos v ON v.id = r.video_id
LEFT JOIN creator_handles h ON h.platform = v.platform AND h.handle = lower(v.handle) AND h.verified_at IS NOT NULL
"""


def offer_url(token):
    return "%s/offer/%s" % (digest.app_url(), token)


def brand_price(r):
    """What the brand pays: the agreed final price, else the creator's counter grossed up, else the price it was shown."""
    if r["final_price_usd"]:
        return r["final_price_usd"]
    if r["creator_price_usd"]:
        return math.ceil(r["creator_price_usd"] / CREATOR_SHARE)
    return r["price_usd"]


def creator_share(r):
    if r["creator_price_usd"] and not r["final_price_usd"]:
        return r["creator_price_usd"]
    return round(brand_price(r) * CREATOR_SHARE)


def offer_view(r):
    video = digest.license_video({"id": r["digest_id"]}, r["video_id"])
    return {"token": r["creator_token"], "url": offer_url(r["creator_token"]), "brand": r["brand"], "video": video,
            "days": r["days"], "status": r["status"], "shareUsd": creator_share(r), "brandPriceUsd": brand_price(r),
            "counterUsd": r["creator_price_usd"], "adCode": r["ad_code"], "declineReason": r["decline_reason"],
            "respondedVia": r["responded_via"], "claimed": r["owner_id"] is not None, "createdAt": r["created_at"],
            "startsAt": r["starts_at"], "expiresAt": r["expires_at"], "creatorPaidAt": r["creator_paid_at"],
            "brandPaid": r["brand_paid_at"] is not None, "refunded": r["refunded_at"] is not None, "renewal": r["renewal_of"] is not None}


def brand_quote(platform, handle, views, days):
    """What a brand is shown for `days`: the creator's own 30-day rate grossed up by our cut when their verified handle
    has one, else digest.license_price's rule of thumb."""
    with connect() as db:
        rate = db.execute("SELECT p.rate_30d_usd FROM creator_handles h JOIN creator_prefs p ON p.user_id = h.user_id"
                          " WHERE h.platform = %s AND h.handle = lower(%s) AND h.verified_at IS NOT NULL", (platform, handle)).fetchone()
    if rate and rate["rate_30d_usd"]:
        return math.ceil(rate["rate_30d_usd"] * days / 30 / CREATOR_SHARE)
    return digest.license_price(views, days)


def load_offer(token):
    with connect() as db:
        r = db.execute(OFFER_SQL + " WHERE r.creator_token = %s", (token,)).fetchone()
    if not r:
        raise ApiError(404, "This offer link isn't valid.")
    return r


# ---------------------------------------------------------------- answering an offer (no account: the link is the key)

ACTIONS = {"accept": ("requested", "contacted"), "counter": ("requested", "contacted"),
           "decline": ("requested", "contacted", "accepted"), "code": ("accepted", "live")}
ANSWERED = "contacted_at = coalesce(contacted_at, now()), responded_at = coalesce(responded_at, now()), " \
           "responded_via = coalesce(responded_via, %s)"


def answer(token, body, via="app"):
    """accept | counter {price: what the creator wants to receive} | decline {reason} | code {code: Spark code or
    'approved'}. Each tells the operator, who still takes payment and passes the code on by hand."""
    r = load_offer(token)
    action = body.get("action")
    if action not in ACTIONS:
        raise ApiError(400, "Unknown action.")
    if r["status"] not in ACTIONS[action]:
        raise ApiError(409, "This offer is already %s." % r["status"])
    if action == "accept":
        sql, args = "status = 'accepted', " + ANSWERED, (via,)
    elif action == "counter":
        price = body.get("price")
        if not isinstance(price, int) or isinstance(price, bool) or not 1 <= price <= 1_000_000:
            raise ApiError(400, "Enter the amount you want to receive, in whole dollars.")
        sql, args = "status = 'contacted', creator_price_usd = %s, " + ANSWERED, (price, via)
    elif action == "decline":
        sql, args = "status = 'declined', decline_reason = %s, " + ANSWERED, (str(body.get("reason") or "").strip()[:300] or None, via)
    else:
        code = str(body.get("code") or "").strip()
        if not 1 <= len(code) <= 200:
            raise ApiError(400, "Paste the ad code (or write that you approved the partnership ad).")
        sql, args = "ad_code = %s, code_received_at = coalesce(code_received_at, now())", (code,)
    with connect() as db:
        db.execute("UPDATE license_requests SET " + sql + " WHERE id = %s", (*args, r["id"]))
    import payments                             # a code on a paid licence makes it live; a decline after payment refunds
    if action in ("accept", "counter", "decline"):
        payments.tell_brand(r["id"], {"accept": "accepted", "counter": "countered", "decline": "declined"}[action])
    payments.maybe_go_live(r["id"])
    payments.refund_if_paid(r["id"])
    r = load_offer(token)
    creator.track(r["owner_id"], "offer_" + action, {"request": r["id"], "via": via})
    tell_operator(r, action)
    return offer_view(r)


def tell_operator(r, action):
    what = {"accept": "accepted", "counter": "countered: wants $%s" % r["creator_price_usd"], "decline": "declined",
            "code": "sent the ad code"}[action]
    subject = "@%s %s (%s, %d days)" % (r["handle"], what, r["brand"], r["days"])
    lines = [("Brand", r["brand"]), ("Creator", "@%s on %s" % (r["handle"], r["platform"])), ("Status", r["status"]),
             ("Brand pays", "$%d" % brand_price(r)), ("Creator gets", "$%d" % creator_share(r)),
             ("Ad code", r["ad_code"] or "-"), ("Reason", r["decline_reason"] or "-"), ("Request id", r["id"])]
    notify(subject, "Next step in /admin/: take payment, pass the code on, or reply to the counter.", lines)


def notify(subject, lead, rows, to=None):
    """Email the operator (or `to`); logged when no address or no mail provider is set."""
    to = to or os.environ.get("LICENSE_NOTIFY_EMAIL")
    if not to:
        print("license note (no LICENSE_NOTIFY_EMAIL): %s" % subject, flush=True)
        return False
    body = ('<p style="font:15px %s;">%s</p><table style="font:14px %s;border-collapse:collapse;">%s</table>' % (
        digest.FONT, digest.esc(lead), digest.FONT, "".join('<tr><td style="padding:3px 14px 3px 0;color:#6b7a73;">%s</td><td>%s</td></tr>'
                                                            % (digest.esc(k), digest.esc(v)) for k, v in rows)))
    return digest.send(to, subject, body)


# ---------------------------------------------------------------- a new request: the creator's rules, then a heads-up

def on_request(request_id):
    """Apply the handle owner's rules to a new request and email them. Called once, right after the request is saved."""
    with connect() as db:
        r = db.execute(OFFER_SQL + " WHERE r.id = %s", (request_id,)).fetchone()
        if not r or r["owner_id"] is None:
            return r
        owner = db.execute("SELECT u.email, p.min_price_usd, p.approve_brands, p.block_brands FROM users u"
                           " LEFT JOIN creator_prefs p ON p.user_id = u.id WHERE u.id = %s", (r["owner_id"],)).fetchone()
    brand = r["brand"].lower()
    if brand in [b.lower() for b in owner["block_brands"] or []]:
        r = rule(r, "decline", "Declined by your rule: you block %s." % r["brand"])
    elif owner["min_price_usd"] and creator_share(r) < owner["min_price_usd"]:
        r = rule(r, "decline", "Declined by your rule: below your $%d minimum." % owner["min_price_usd"])
    elif brand in [b.lower() for b in owner["approve_brands"] or []]:
        r = rule(r, "accept", None)
    if r["responded_via"] == "auto":
        import payments
        payments.tell_brand(r["id"], r["status"])
    if r["status"] != "declined":
        accepted = r["status"] == "accepted"
        notify("%s %s your video as an ad: $%d to you" % (r["brand"], "will run" if accepted else "wants to run", creator_share(r)),
               ("Accepted by your rule. Next: send the ad code from the link below." if accepted
                else "Answer in one tap, no forms: accept, counter or decline."),
               [("Video", "@%s on %s" % (r["handle"], r["platform"])), ("Window", "%d days" % r["days"]),
                ("You get", "$%d" % creator_share(r)), ("Offer", offer_url(r["creator_token"]))], to=owner["email"])
    return r


def rule(r, action, reason):
    status = "accepted" if action == "accept" else "declined"
    with connect() as db:
        db.execute("UPDATE license_requests SET status = %s, decline_reason = %s, " + ANSWERED + " WHERE id = %s",
                   (status, reason, "auto", r["id"]))
        return db.execute(OFFER_SQL + " WHERE r.id = %s", (r["id"],)).fetchone()


# ---------------------------------------------------------------- handles, rules, inbox (Receipts account)

def norm(platform, handle):
    platform, handle = str(platform or "").lower(), str(handle or "").strip().lstrip("@").lower()
    if platform not in PLATFORMS or not HANDLE_RE.fullmatch(handle):
        raise ApiError(400, "Enter your TikTok or Instagram handle.")
    return platform, handle


def handle_view(h):
    return {"id": h["id"], "platform": h["platform"], "handle": h["handle"], "code": h["code"], "verified": bool(h["verified_at"]),
            "verifiedBy": h["verified_by"]}


def claim(user, body):
    platform, handle = norm(body.get("platform"), body.get("handle"))
    with connect() as db:
        h = db.execute("INSERT INTO creator_handles (user_id, platform, handle, code) VALUES (%s, %s, %s, %s)"
                       " ON CONFLICT (user_id, platform, handle) DO UPDATE SET code = creator_handles.code RETURNING *",
                       (user["id"], platform, handle, "UNP-" + secrets.token_hex(3).upper())).fetchone()
    if platform == "instagram" and not h["verified_at"]:
        notify("Instagram handle claim: @%s (code %s)" % (handle, h["code"]),
               "Verify in /admin/ once @%s sends you this code by DM, or from the email in its bio." % handle,
               [("Account", user["email"]), ("Handle", "@" + handle), ("Code", h["code"])])
    return handle_view(h)


def verify(user, body):
    """TikTok: the code must be in the public bio. Instagram has no public profile we can read: the operator verifies."""
    platform, handle = norm(body.get("platform"), body.get("handle"))
    with connect() as db:
        h = db.execute("SELECT * FROM creator_handles WHERE user_id = %s AND platform = %s AND handle = %s",
                       (user["id"], platform, handle)).fetchone()
    if not h:
        raise ApiError(404, "Claim the handle first.")
    if h["verified_at"]:
        return handle_view(h)
    if platform == "instagram":
        raise ApiError(409, "Instagram handles are checked by hand: DM %s from @%s to the account that contacted you, "
                            "or email it from the address in your bio." % (h["code"], handle))
    info, _ = tiktok_public.creator_page(handle)
    if info is None:
        raise ApiError(503, "TikTok didn't show @%s's profile just now. Try again in a minute." % handle)
    if h["code"].lower() not in (info.get("signature") or "").lower():
        raise ApiError(409, "%s isn't in @%s's bio yet. Bios can take a minute to update after you save." % (h["code"], handle))
    return mark_verified(h["id"], "bio")


def mark_verified(handle_id, by):
    try:
        with connect() as db:
            h = db.execute("UPDATE creator_handles SET verified_at = now(), verified_by = %s WHERE id = %s RETURNING *",
                           (by, handle_id)).fetchone()
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "Another account already verified this handle. Email us if it's yours.")
    if not h:
        raise ApiError(404, "No such handle.")
    return handle_view(h)


def remove(user, body):
    platform, handle = norm(body.get("platform"), body.get("handle"))
    with connect() as db:
        db.execute("DELETE FROM creator_handles WHERE user_id = %s AND platform = %s AND handle = %s", (user["id"], platform, handle))
    return mine(user)


def brand_list(v):
    if not isinstance(v, list):
        return []
    return list(dict.fromkeys(str(b).strip()[:60] for b in v if str(b).strip()))[:50]


def set_prefs(user, body):
    m, rate = body.get("minPriceUsd"), body.get("rate30dUsd")
    for v in (m, rate):
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 1_000_000):
            raise ApiError(400, "Amounts are whole dollars.")
    with connect() as db:
        db.execute("INSERT INTO creator_prefs (user_id, min_price_usd, approve_brands, block_brands, weekly_summary, rate_30d_usd)"
                   " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET min_price_usd = excluded.min_price_usd,"
                   " approve_brands = excluded.approve_brands, block_brands = excluded.block_brands,"
                   " weekly_summary = excluded.weekly_summary, rate_30d_usd = excluded.rate_30d_usd",
                   (user["id"], m or None, brand_list(body.get("approveBrands")), brand_list(body.get("blockBrands")),
                    body.get("weeklySummary") is not False, rate or None))
    return mine(user)


def mine(user):
    """The inbox: handles, every offer for the verified ones (newest first), rules and money."""
    with connect() as db:
        handles = db.execute("SELECT * FROM creator_handles WHERE user_id = %s ORDER BY id", (user["id"],)).fetchall()
        rows = db.execute(OFFER_SQL + " WHERE h.user_id = %s ORDER BY r.id DESC LIMIT 100", (user["id"],)).fetchall()
        prefs = db.execute("SELECT * FROM creator_prefs WHERE user_id = %s", (user["id"],)).fetchone()
    offers = [offer_view(r) for r in rows]
    paid = sum(creator_share(r) for r in rows if r["creator_paid_at"])
    owed = sum(creator_share(r) for r in rows if r["brand_paid_at"] and not r["creator_paid_at"])
    return {"handles": [handle_view(h) for h in handles], "offers": offers,
            "prefs": {"minPriceUsd": prefs["min_price_usd"] if prefs else None,
                      "approveBrands": prefs["approve_brands"] if prefs else [], "blockBrands": prefs["block_brands"] if prefs else [],
                      "weeklySummary": prefs["weekly_summary"] if prefs else True, "rate30dUsd": prefs["rate_30d_usd"] if prefs else None},
            "payouts": payments_view(user),
            "money": {"paidUsd": paid, "owedUsd": owed, "live": sum(r["status"] == "live" for r in rows)}}


# ---------------------------------------------------------------- weekly summary email

SUMMARY_EVERY = timedelta(days=7) - timedelta(hours=1)   # weekly, with an hour of slack for a daily cron


def summary(user):
    """(subject, html) of this week's summary, or None when there is nothing worth an email."""
    inbox = mine(user)
    now = datetime.now(timezone.utc)
    waiting = [o for o in inbox["offers"] if o["status"] in ("requested", "contacted") and not (o["counterUsd"] and o["respondedVia"])]
    ending = [o for o in inbox["offers"] if o["status"] == "live" and o["expiresAt"] and o["expiresAt"] - now <= timedelta(days=7)]
    paid_month = sum(o["shareUsd"] for o in inbox["offers"] if o["creatorPaidAt"] and o["creatorPaidAt"].strftime("%Y-%m") == now.strftime("%Y-%m"))
    owed = inbox["money"]["owedUsd"]
    if not (waiting or ending or owed or paid_month):
        return None
    bits = ["%d offer%s waiting" % (len(waiting), "" if len(waiting) == 1 else "s")] if waiting else []
    bits += ["%d ad%s ending soon" % (len(ending), "" if len(ending) == 1 else "s")] if ending else []
    bits += ["$%d on its way" % owed] if owed else []
    subject = "Your license offers this week: " + (", ".join(bits) or "$%d paid this month" % paid_month)
    e, font = digest.esc, digest.FONT

    def row(o, note, cta):
        return ('<tr><td style="padding:10px 0;border-top:1px solid #e6ebe8;font:14px %s;color:#1b2a23;"><b>%s</b> &middot; @%s &middot; %s'
                ' <a href="%s" style="margin-left:8px;color:#2f6b4f;font-weight:600;">%s &rarr;</a></td></tr>'
                % (font, e(o["brand"]), e(o["video"]["handle"]), e(note), e(o["url"]), cta))
    sections = []
    if waiting:
        sections.append(("Waiting for your answer",
                         "".join(row(o, "$%d to you for %d days" % (o["shareUsd"], o["days"]), "Answer") for o in waiting)))
    if ending:
        sections.append(("Ending in the next 7 days", "".join(
            row(o, "live until %d %s" % (o["expiresAt"].day, o["expiresAt"].strftime("%b")), "Open") for o in ending)))
    money = "$%d paid to you this month &middot; $%d on its way" % (paid_month, owed)
    body = ('<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f3f6f4;">'
            '<table role="presentation" width="100%%" style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;padding:24px 26px;">'
            '<tr><td style="font:500 12px %s;letter-spacing:.08em;text-transform:uppercase;color:#6b7a73;">Receipts &middot; weekly</td></tr>'
            '<tr><td style="padding:8px 0 4px;font:600 22px/1.25 %s;color:#1b2a23;">%s</td></tr>'
            '<tr><td style="padding-bottom:10px;font:14px %s;color:#48564f;">%s</td></tr>%s'
            '<tr><td style="padding-top:18px;font:12px/1.6 %s;color:#6b7a73;">All offers and your rules: <a href="%s" style="color:#2f6b4f;">%s</a>.'
            ' Turn this email off there.</td></tr></table></body></html>' % (
                font, font, e(subject.split(": ", 1)[1]), font, money,
                "".join('<tr><td style="padding-top:14px;font:600 13px %s;color:#1b2a23;">%s</td></tr>%s' % (font, e(t), rows)
                        for t, rows in sections),
                font, e(inbox_url()), e(inbox_url())))
    return subject, body


def payments_view(user):
    import payments
    return payments.connect_view(user)


def inbox_url():
    return "%s/creators/licenses" % digest.app_url()


def run_summaries(force=False):
    """Email every creator with a verified handle whose summary is due and who has something to read. Each send is
    claimed in creator_summaries first, so overlapping runs (scheduler and cron) never send twice."""
    with connect() as db:
        users = db.execute(
            "SELECT DISTINCT u.* FROM users u JOIN creator_handles h ON h.user_id = u.id AND h.verified_at IS NOT NULL"
            " LEFT JOIN creator_prefs p ON p.user_id = u.id LEFT JOIN creator_summaries s ON s.user_id = u.id"
            " WHERE coalesce(p.weekly_summary, true) AND (%s OR s.sent_at IS NULL OR s.sent_at < now() - %s)",
            (force, SUMMARY_EVERY)).fetchall()
    sent = 0
    for u in users:
        mail = summary(u)
        if not mail:
            continue
        with connect() as db:
            claimed = db.execute("INSERT INTO creator_summaries (user_id, sent_at) VALUES (%s, now()) ON CONFLICT (user_id) DO UPDATE"
                                 " SET sent_at = now() WHERE %s OR creator_summaries.sent_at < now() - %s RETURNING user_id",
                                 (u["id"], force, SUMMARY_EVERY)).fetchone()
        if claimed:
            digest.send(u["email"], *mail)
            creator.track(u["id"], "creator_summary", {"subject": mail[0]})
            sent += 1
    return sent


# ---------------------------------------------------------------- operator: Instagram claims

def admin_handles():
    with connect() as db:
        rows = db.execute("SELECT h.*, u.email FROM creator_handles h JOIN users u ON u.id = h.user_id"
                          " ORDER BY h.verified_at IS NOT NULL, h.id DESC LIMIT 200").fetchall()
    return {"handles": [{**handle_view(h), "email": h["email"], "createdAt": h["created_at"]} for h in rows]}


# ---------------------------------------------------------------- routes

def dispatch(handler):
    """/api/licenses/offer/<token> (GET, POST) without an account; /api/licenses/mine|handles|verify|remove|prefs with one;
    /api/admin/handles[/<id>/verify] with the operator token."""
    path, method = urlparse(handler.path).path, handler.command
    if path.startswith("/api/admin/"):
        digest.require_operator(handler)
        if method == "GET" and path == "/api/admin/handles":
            return admin_handles()
        m = re.fullmatch(r"/api/admin/handles/(\d+)/verify", path)
        if method == "POST" and m:
            return mark_verified(int(m.group(1)), "operator")
        raise ApiError(404, "Not found.")
    path = path.removeprefix("/api/licenses")
    m = re.fullmatch(r"/offer/([0-9a-f]{32})", path)
    if method == "GET":
        if m:
            r = load_offer(m.group(1))
            creator.track(r["owner_id"], "offer_view", {"request": r["id"]})
            return offer_view(r)
        if path == "/mine":
            return mine(creator.require_role(handler.headers, "creator"))
        raise ApiError(404, "Not found.")
    if method != "POST":
        raise ApiError(405, "Method not allowed.")
    if not (handler.headers.get("Content-Type") or "").startswith("application/json"):
        raise ApiError(415, "Send application/json.")
    body = digest.read_json(handler)
    if m:
        return answer(m.group(1), body)
    user = creator.require_role(handler.headers, "creator")
    import payments
    route = {"/handles": claim, "/verify": verify, "/remove": remove, "/prefs": set_prefs, "/payouts/connect": payments.connect_onboard,
             "/payouts/refresh": lambda u, _: payments.connect_refresh(u)}.get(path)
    if not route:
        raise ApiError(404, "Not found.")
    return route(user, body)
