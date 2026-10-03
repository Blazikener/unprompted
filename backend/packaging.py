"""Receipts packaging test (Phase 7): which way of charging creators brings them back and gets them to pay?

Visitors to /creators/ are split by a cookie into three arms, each a different offer on the same product:
  a: today's offer. Free shows the top 3 brands; Pro, $29/month, unlocks every brand, the sponsor check and pitch drafts.
  b: free for good, paid by licences. Every brand and pitch draft is free (the "open" plan); creators earn when brands
     license their videos (licenses.py: they keep 85%) and Unprompted keeps the rest.
  c: free as in (a), and Weekly leads at $9/month: every Monday, the brands they mention that are paying creators that
     week, with the receipt and a pitch draft (run_feeds), plus everything Pro unlocks in the app.
An account keeps the arm it signed up under, on any device. /admin/ shows each arm's visitors, signups, week-4 active
rate and paid conversion. When one wins, set RECEIPTS_ARMS to it: everyone sees that offer from then on.
"""
import os
import secrets
import traceback
from http.cookies import SimpleCookie

import creator
import digest
import licenses
import rosters
from server import ApiError, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (                   -- the packaging test: which offer each browser was shown
  token      text PRIMARY KEY,
  arm        text NOT NULL,
  forced     boolean NOT NULL DEFAULT false,            -- a ?arm= preview: never counted
  user_id    int REFERENCES users ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS arm text;     -- the offer the account signed up under; NULL: before the test
ALTER TABLE users ADD COLUMN IF NOT EXISTS arm_forced boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS feed_sent_at timestamptz;   -- the last Weekly leads email
CREATE TABLE IF NOT EXISTS feed_brands (                 -- brands already sent in someone's Weekly leads: the rest are new
  user_id  int  NOT NULL REFERENCES users ON DELETE CASCADE,
  brand    text NOT NULL,
  first_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, brand)
);
"""
ARMS = ("a", "b", "c")
COOKIE = "receipts_visitor"
LEADS_PRICE_USD = 9
TARGET_VISITORS = 300                       # per arm, before reading the result
ACTIVE_EVENTS = ["visit", "scan", "activity", "pitch", "share", "report", "feed_click"]   # plus offer_* (licences.py)
BRANDS_PER_FEED = 8                         # unpaid brands checked per Weekly leads email
ACTIVITY_LOOKUPS = 3                        # fresh "is this brand paying creators?" lookups per email (~80 credits each)


def offers():
    """What each arm sells: (a) Pro, (b) nothing (licences pay), (c) Weekly leads."""
    return {"a": {"plan": "pro", "name": "Pro", "price": creator.PRO_PRICE_USD},
            "b": {"plan": None, "name": "Free", "price": 0},
            "c": {"plan": "leads", "name": "Weekly leads", "price": LEADS_PRICE_USD}}


def active_arms():
    arms = [a for a in (os.environ.get("RECEIPTS_ARMS") or "a,b,c").replace(" ", "").lower().split(",") if a in ARMS]
    return arms or ["a"]


def effective(arm):
    """The offer an arm sees now: its own while the test runs, the winner once RECEIPTS_ARMS drops it."""
    arms = active_arms()
    return arm if arm in arms else arms[0]


def user_arm(user):
    """The offer a creator sees: the arm they signed up under. Accounts from before the test get today's offer."""
    return effective(user.get("arm") or "a")


def plan_of(user):
    """The plan whose limits apply: in arm (b) the free plan is "open" (every brand and pitch draft)."""
    return "open" if user["plan"] == "free" and user_arm(user) == "b" else user["plan"]


def offer(arm):
    o = offers()[arm]
    price_env = creator.PLAN_PRICE_ENV.get(o["plan"])
    return {"arm": arm, **o, "checkout": bool(price_env and os.environ.get(price_env) and os.environ.get("STRIPE_SECRET_KEY"))}


def offer_plan(user):
    """The plan this creator can buy: Pro in (a), Weekly leads in (c); (b) has nothing to buy."""
    plan = offers()[user_arm(user)]["plan"]
    if not plan:
        raise ApiError(400, "Receipts is free on your account: there's nothing to buy.")
    return plan


def paid_name(user):
    return offers()[user_arm(user)]["name"] if offers()[user_arm(user)]["plan"] else None


def visitor_token(headers):
    cookie = SimpleCookie(headers.get("Cookie") or "")
    return cookie[COOKIE].value if COOKIE in cookie else None


def visit(headers, body):
    """POST /api/creators/visit from the Receipts page: the browser's arm (drawn on its first visit), the offer to show,
    and for a signed-in creator a "visit" event, which is what the week-4 active rate counts. {"arm": "c"} previews an
    arm without being counted; {"page": "inbox"} (the licence inbox) only records the visit."""
    user = creator.current_user(headers)
    page = body.get("page") if body.get("page") in ("receipts", "inbox") else "receipts"
    if body.get("from") == "feed" and user:
        creator.track(user["id"], "feed_click", {})
    if page == "inbox":
        if user:
            creator.track(user["id"], "visit", {"page": page, "arm": user_arm(user)})
        return {"arm": user_arm(user) if user else None}
    forced = body.get("arm") if body.get("arm") in ARMS else None
    token, cookies = visitor_token(headers), []
    with connect() as db:
        v = db.execute("SELECT * FROM visitors WHERE token = %s", (token,)).fetchone() if token else None
        if forced and not (v and v["forced"] and v["arm"] == forced):
            v = None
        if not v:
            v = db.execute("INSERT INTO visitors (token, arm, forced) VALUES (%s, %s, %s) RETURNING *",
                           (secrets.token_urlsafe(18), forced or secrets.choice(active_arms()), bool(forced))).fetchone()
            cookies.append(("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (COOKIE, v["token"], 365 * 86400)))
        if user and not v["user_id"]:
            db.execute("UPDATE visitors SET user_id = %s WHERE token = %s", (user["id"], v["token"]))
    arm = forced or (user_arm(user) if user else effective(v["arm"]))
    if user:
        creator.track(user["id"], "visit", {"page": page, "arm": arm})
    return creator.Reply({"arm": arm, "offer": offer(arm)}, 200, cookies)


def signup_arm(headers):
    """(arm, forced) for a new account: the arm its browser was shown on /creators/. None for a browser that never saw the
    Receipts page (a manager's roster, a licence offer link): those accounts aren't part of the test."""
    token = visitor_token(headers)
    if not token:
        return None, False
    with connect() as db:
        v = db.execute("SELECT arm, forced FROM visitors WHERE token = %s", (token,)).fetchone()
    return (v["arm"], v["forced"]) if v else (None, False)


def link_visitor(headers, user_id):
    token = visitor_token(headers)
    if token:
        with connect() as db:
            db.execute("UPDATE visitors SET user_id = %s WHERE token = %s AND user_id IS NULL", (user_id, token))


def quota_message(user, used_limit):
    plan, arm = user["plan"], user_arm(user)
    if plan not in ("free", "open"):
        return "Scan limit reached (%d per week)." % used_limit
    if arm == "c":
        return "You've used your %d free scan this week. Weekly leads re-reads your videos every Monday." % used_limit
    if arm == "b":
        return "You've used this week's free scan. The next one opens 7 days after the last."
    return "You've used your %d free scan this week. Pro gets %d." % (used_limit, creator.PLANS["pro"]["scansPerWeek"])


# ---------------------------------------------------------------- Weekly leads (arm c, $9/month)

def build_feed(user):
    """This week's leads for one creator: the brands they mention unpaid in their latest scanned handle's videos (read
    fresh), each with the paying signal; brands paying creators now are the leads, newest first."""
    with connect() as db:
        last = db.execute("SELECT platform, handle FROM scans WHERE user_id = %s ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
        known = {r["brand"] for r in db.execute("SELECT brand FROM feed_brands WHERE user_id = %s", (user["id"],))}
    if not last:
        return {"handle": None, "leads": [], "quiet": []}
    out = {"platform": last["platform"], "handle": last["handle"], "leads": [], "quiet": []}
    try:
        videos, _ = creator.fetch_videos(last["platform"], last["handle"])
    except ApiError as e:
        return {**out, "error": str(e)}
    prof = creator.profile(last["platform"], last["handle"], videos)
    rate = creator.rate_card(prof["medianViews"])
    lookups = [ACTIVITY_LOOKUPS]
    for row in [r for r in creator.aggregate(videos) if r["status"] == "unpaid"][:BRANDS_PER_FEED]:
        a = rosters.paying_signal(row["brand"], last["platform"], lookups)
        if not a:                           # out of fresh lookups this week: say nothing rather than guess
            continue
        if a["verdict"] == "quiet":
            out["quiet"].append(row["brand"])
            continue
        best = row["receipts"][0]
        q = best["quote"] or {}
        out["leads"].append({
            "brand": row["brand"], "spoken": row["spoken"], "tagged": row["tagged"], "views": row["organicViews"], "url": best["url"],
            "quote": (q.get("core") or q.get("text") or best["caption"] or "").strip()[:220], "at": creator.fmt_ts(q.get("start")),
            "paying": a["verdict"], "paidPosts": a["sponsored"], "lastPaid": a.get("lastPaid"), "new": row["brand"] not in known,
            "pitch": creator.draft_pitch(prof, row, a, rate)})
    out["leads"].sort(key=lambda x: (x["paying"] != "active", not x["new"], -x["views"]))
    return out


def render_feed(feed):
    """(subject, html) of the Monday Weekly leads email."""
    e, font = digest.esc, digest.FONT
    leads = feed["leads"]
    if not feed["handle"]:
        subject = "Scan your handle to start your weekly leads"
    elif feed.get("error"):
        subject = "We couldn't read @%s this week" % feed["handle"]
    elif leads:
        subject = "%d brand%s you mention %s paying creators this week" % (len(leads), "" if len(leads) == 1 else "s",
                                                                            "is" if len(leads) == 1 else "are")
    else:
        subject = "A quiet week: none of the brands you mention are paying creators right now"
    rows = "".join(
        '<tr><td style="padding:12px 0;border-top:1px solid #e6ebe8;font:14px %s;color:#1b2a23;"><b>%s</b>%s'
        ' <span style="color:#6b7a73;">&middot; %s &middot; %s paid creator post%s in 90 days%s</span>'
        '<div style="margin:4px 0;color:#48564f;">You: %s &middot; %s unpaid views</div>'
        '<div style="margin:4px 0;font:italic 15px Georgia,serif;">&ldquo;%s&rdquo;</div>'
        '<a href="%s" style="color:#2f6b4f;">Watch%s &rarr;</a></td></tr>' % (
            font, e(x["brand"]), ' <span style="color:#2f6b4f;font-weight:600;">new</span>' if x["new"] else "",
            e(rosters.PAYING[x["paying"]]), x["paidPosts"], "" if x["paidPosts"] == 1 else "s",
            " &middot; latest %s" % e(x["lastPaid"][:10]) if x.get("lastPaid") else "",
            e(("said it on camera ×%d" % x["spoken"]) if x["spoken"] else "tagged ×%d" % x["tagged"]), e(creator.fmt_num(x["views"])),
            e(x["quote"]), e(x["url"]), " at %s" % e(x["at"]) if x["at"] else "")
        for x in leads)
    if not feed["handle"]:
        lead = "Weekly leads reads the handle you last scanned. Scan it once and the first leads arrive next Monday."
    elif feed.get("error"):
        lead = "%s We'll try again next Monday." % feed["error"]
    elif leads:
        lead = ("From @%s's latest videos: brands you've mentioned without being paid that are paying other creators now."
                " Open the app for the pitch draft." % feed["handle"])
    else:
        lead = "We read @%s's latest videos%s. We'll check again next Monday." % (
            feed["handle"], ": %s %s quiet for now" % (", ".join(feed["quiet"][:5]), "is" if len(feed["quiet"]) == 1 else "are")
            if feed["quiet"] else " and found no unpaid brand mentions")
    url = creator.app_url() + "/creators/?from=feed"
    body = ('<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f3f6f4;">'
            '<table role="presentation" width="100%%" style="max-width:600px;margin:0 auto;background:#fff;border-radius:16px;padding:24px 26px;">'
            '<tr><td style="font:500 12px %s;letter-spacing:.08em;text-transform:uppercase;color:#6b7a73;">Receipts &middot; weekly leads</td></tr>'
            '<tr><td style="padding:8px 0 6px;font:600 22px/1.25 %s;color:#1b2a23;">%s</td></tr>'
            '<tr><td style="padding:0 0 8px;font:14px/1.5 %s;color:#48564f;">%s</td></tr>%s'
            '<tr><td style="padding-top:16px;"><a href="%s" style="display:inline-block;padding:10px 16px;border-radius:999px;background:#1b2a23;'
            'color:#fff;font:600 14px %s;text-decoration:none;">Pitch drafts and receipts &rarr;</a></td></tr>'
            '<tr><td style="padding-top:16px;font:12px %s;color:#6b7a73;">You get this because you subscribed to Weekly leads ($%d/month). '
            'Cancel any time from Billing in the app.</td></tr>'
            '</table></body></html>' % (font, font, e(subject), font, e(lead), rows, e(url), font, font, LEADS_PRICE_USD))
    return subject, body


def run_feed(user):
    feed = build_feed(user)
    subject, body = render_feed(feed)
    sent = digest.send(user["email"], subject, body)
    with connect() as db, db.cursor() as cur:
        cur.executemany("INSERT INTO feed_brands (user_id, brand) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        [(user["id"], x["brand"]) for x in feed["leads"]])
    creator.track(user["id"], "feed_sent", {"leads": len(feed["leads"]), "sent": sent, "error": feed.get("error")})
    return feed


def run_feeds(any_day=False):
    """Every Weekly leads subscriber, Mondays (UTC), once a week (scheduler and the digest cron). Each is claimed by
    stamping feed_sent_at first, so two runs can't double-send."""
    with connect() as db:
        due = db.execute("SELECT * FROM users WHERE plan = 'leads' AND (%s OR extract(isodow FROM now() AT TIME ZONE 'UTC') = 1)"
                         " AND (feed_sent_at IS NULL OR feed_sent_at < now() - interval '6 days') ORDER BY id", (any_day,)).fetchall()
    sent = 0
    for u in due:
        with connect() as db:
            if not db.execute("UPDATE users SET feed_sent_at = now() WHERE id = %s AND (feed_sent_at IS NULL OR"
                              " feed_sent_at < now() - interval '6 days') RETURNING id", (u["id"],)).fetchone():
                continue
        try:
            run_feed(u)
            sent += 1
        except Exception:                   # one creator's failure mustn't stop the others
            traceback.print_exc()
    return sent


# ---------------------------------------------------------------- the numbers (/admin/)

def report():
    """Per arm: visitors, signups, scanned, week-4 active (of accounts at least 28 days old: any visit, scan or offer
    answer in days 21-28), paid (a subscription, or a licence a brand paid for), and what Unprompted earned."""
    with connect() as db:
        visitors = {r["arm"]: r["n"] for r in db.execute("SELECT arm, count(*) AS n FROM visitors WHERE NOT forced GROUP BY arm")}
        users = db.execute(
            "SELECT u.id, u.arm, u.plan, u.created_at <= now() - interval '28 days' AS eligible,"
            " EXISTS (SELECT 1 FROM scans s WHERE s.user_id = u.id) AS scanned,"
            " EXISTS (SELECT 1 FROM events e WHERE e.user_id = u.id AND (e.name = ANY(%s) OR e.name LIKE 'offer\\_%%')"
            "   AND e.created_at >= u.created_at + interval '21 days' AND e.created_at < u.created_at + interval '28 days') AS week4,"
            " EXISTS (SELECT 1 FROM events e WHERE e.user_id = u.id AND e.name IN ('checkout_intent', 'checkout_started')) AS intent,"
            " (SELECT e.data->>'plan' FROM events e WHERE e.user_id = u.id AND e.name = 'subscribed' ORDER BY e.id LIMIT 1) AS subscribed"
            " FROM users u WHERE u.arm IS NOT NULL AND NOT u.arm_forced", (ACTIVE_EVENTS,)).fetchall()
        paid = db.execute("SELECT * FROM (%s) o WHERE o.owner_id IS NOT NULL AND o.brand_paid_at IS NOT NULL" % licenses.OFFER_SQL).fetchall()
    kept = {}
    for r in paid:
        kept[r["owner_id"]] = kept.get(r["owner_id"], 0) + licenses.brand_price(r) - licenses.creator_share(r)
    rows = []
    for arm in ARMS:
        us = [u for u in users if u["arm"] == arm]
        subs = [u for u in us if u["subscribed"] and u["plan"] not in ("free", "open")]
        earners = [u for u in us if kept.get(u["id"])]
        eligible = [u for u in us if u["eligible"]]
        rows.append({
            "arm": arm, "offer": offers()[arm]["name"] if arm != "b" else "Free, paid by licences", "price": offers()[arm]["price"],
            "live": arm in active_arms(), "visitors": visitors.get(arm, 0), "signups": len(us),
            "scanned": sum(u["scanned"] for u in us), "intent": sum(u["intent"] for u in us),
            "eligible": len(eligible), "week4": sum(u["week4"] for u in eligible),
            "subscribed": len(subs), "licenceEarners": len(earners), "paid": len({u["id"] for u in subs + earners}),
            "mrrUsd": sum(offers()[arm]["price"] for u in subs if u["plan"] == offers()[arm]["plan"]),
            "licenceKeptUsd": sum(kept.get(u["id"], 0) for u in us)})
    return {"arms": rows, "live": active_arms(), "targetVisitors": TARGET_VISITORS}


def admin_route(handler):
    digest.require_operator(handler)
    if handler.command == "GET":
        return report()
    raise ApiError(405, "Method not allowed.")
