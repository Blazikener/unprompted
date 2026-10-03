"""Roster seat for talent managers (Phase 4): one Monday email per manager, covering every creator they represent.

For each creator on the roster: the brands they mentioned on camera or tagged, unpaid, in videos new since the last
report (the first report covers what's already there), each with its receipt; whether that brand is paying creators
right now (Receipts' brand_activity cache, plus at most ACTIVITY_LOOKUPS fresh lookups per run, since each costs Oriane
credits); and a pitch draft that leads with the mention. Managers add up to MAX_CREATORS creators at /creators/roster
with their Receipts account and mark deals that came from a report: that, and committing to the paid plan, is Gate 4.

Access: a pilot the operator grants in /admin/ (PILOT_WEEKS free weeks), or the "roster" plan (ROSTER_PRICE_USD a month
through Stripe when STRIPE_PRICE_ROSTER is set; without it the button records a commitment for the operator).
Credits: TikTok creators are read from the public page first (free); Instagram creators cost one Oriane search each.
"""
import os
import re
import traceback
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

import creator
import digest
import licenses
from server import ApiError, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS rosters (
  id           serial PRIMARY KEY,
  user_id      int UNIQUE NOT NULL REFERENCES users ON DELETE CASCADE,
  pilot_until  timestamptz,                   -- free access until then (granted by the operator)
  last_run_at  timestamptz,                   -- any report, including "run now"
  last_sent_at timestamptz,                   -- the last report emailed
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS roster_creators (
  id         serial PRIMARY KEY,
  roster_id  int  NOT NULL REFERENCES rosters ON DELETE CASCADE,
  platform   text NOT NULL,
  handle     text NOT NULL,                   -- lowercase, no @
  name       text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (roster_id, platform, handle)
);
CREATE TABLE IF NOT EXISTS roster_seen (     -- videos already covered by a report, per roster creator
  roster_creator_id int  NOT NULL REFERENCES roster_creators ON DELETE CASCADE,
  video_id          text NOT NULL,
  PRIMARY KEY (roster_creator_id, video_id)
);
CREATE TABLE IF NOT EXISTS roster_reports (
  id         serial PRIMARY KEY,
  roster_id  int     NOT NULL REFERENCES rosters ON DELETE CASCADE,
  items      jsonb   NOT NULL,                -- per creator: brands with receipts, paying signal and pitch
  sent       boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS roster_deals (    -- a deal the manager says came from a report (Gate 4)
  id         serial PRIMARY KEY,
  roster_id  int  NOT NULL REFERENCES rosters ON DELETE CASCADE,
  report_id  int  REFERENCES roster_reports ON DELETE SET NULL,
  handle     text NOT NULL,
  brand      text NOT NULL,
  amount_usd int,
  created_at timestamptz NOT NULL DEFAULT now()
);
"""

MAX_CREATORS = 25
PILOT_WEEKS = 6
ROSTER_PRICE_USD = 99
ACTIVITY_LOOKUPS = 3                        # fresh "is this brand paying creators?" lookups per run (each ~80 credits)
BRANDS_PER_CREATOR = 5
PAYING = {"active": "Paying creators now", "some": "Some paid creator posts", "quiet": "No paid posts seen"}


def dashboard_url():
    return "%s/creators/roster" % digest.app_url()


def now():
    return datetime.now(timezone.utc)


def load(user_id):
    with connect() as db:
        return db.execute("SELECT r.*, u.email, u.plan FROM rosters r JOIN users u ON u.id = r.user_id WHERE r.user_id = %s",
                          (user_id,)).fetchone()


def access(r):
    """'paid' on the roster plan, 'pilot' during a granted pilot, else None."""
    if not r:
        return None
    if r["plan"] == "roster":
        return "paid"
    return "pilot" if r["pilot_until"] and r["pilot_until"] > now() else None


def require_access(user):
    r = load(user["id"])
    if not access(r):
        raise ApiError(402, "Your roster pilot isn't active. Ask for one, or subscribe for $%d a month." % ROSTER_PRICE_USD)
    return r


# ---------------------------------------------------------------- the manager's dashboard (/creators/roster)

def mine(user):
    r = load(user["id"])
    view = {"access": access(r), "pilotUntil": r["pilot_until"] if r else None, "priceUsd": ROSTER_PRICE_USD,
            "maxCreators": MAX_CREATORS, "online": billing_on(), "creators": [], "report": None, "deals": [], "hasRoster": bool(r)}
    if not r:
        return view
    with connect() as db:
        view["creators"] = [{"id": c["id"], "platform": c["platform"], "handle": c["handle"], "name": c["name"]} for c in db.execute(
            "SELECT * FROM roster_creators WHERE roster_id = %s ORDER BY id", (r["id"],))]
        last = db.execute("SELECT * FROM roster_reports WHERE roster_id = %s ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        view["deals"] = [{"handle": d["handle"], "brand": d["brand"], "amountUsd": d["amount_usd"], "createdAt": d["created_at"]}
                         for d in db.execute("SELECT * FROM roster_deals WHERE roster_id = %s ORDER BY id DESC", (r["id"],))]
    if last:
        view["report"] = {"id": last["id"], "createdAt": last["created_at"], "items": last["items"], "sent": last["sent"]}
    view["canRunNow"] = bool(view["access"]) and (not r["last_run_at"] or r["last_run_at"] < now() - timedelta(hours=24))
    return view


def add_creator(user, body):
    r = require_access(user)
    platform, handle = licenses.norm(body.get("platform"), body.get("handle"))
    name = str(body.get("name") or "").strip()[:80] or None
    with connect() as db:
        n = db.execute("SELECT count(*) AS n FROM roster_creators WHERE roster_id = %s", (r["id"],)).fetchone()["n"]
        if n >= MAX_CREATORS:
            raise ApiError(409, "A roster holds up to %d creators." % MAX_CREATORS)
        db.execute("INSERT INTO roster_creators (roster_id, platform, handle, name) VALUES (%s, %s, %s, %s)"
                   " ON CONFLICT (roster_id, platform, handle) DO UPDATE SET name = coalesce(excluded.name, roster_creators.name)",
                   (r["id"], platform, handle, name))
    return mine(user)


def remove_creator(user, body):
    r = require_access(user)
    with connect() as db:
        db.execute("DELETE FROM roster_creators WHERE roster_id = %s AND id = %s", (r["id"], body.get("id")))
    return mine(user)


def run_now(user, body):
    """The manager's own trigger (e.g. the first report), at most once a day."""
    r = require_access(user)
    if r["last_run_at"] and r["last_run_at"] > now() - timedelta(hours=24):
        raise ApiError(429, "You've already run a report today. The next one comes on Monday.")
    run_roster(r)
    return mine(user)


def deal(user, body):
    """A deal the manager closed from a report: the Gate 4 signal."""
    r = require_access(user)
    handle, brand = str(body.get("handle") or "").strip().lstrip("@").lower(), str(body.get("brand") or "").strip()[:80]
    amount = body.get("amountUsd")
    if not handle or not brand:
        raise ApiError(400, "Which creator and brand?")
    if amount is not None and (not isinstance(amount, int) or isinstance(amount, bool) or not 0 <= amount <= 10_000_000):
        raise ApiError(400, "Amount is whole dollars.")
    with connect() as db:
        db.execute("INSERT INTO roster_deals (roster_id, report_id, handle, brand, amount_usd) VALUES (%s, %s, %s, %s, %s)",
                   (r["id"], body.get("reportId") if isinstance(body.get("reportId"), int) else None, handle, brand, amount))
    creator.track(user["id"], "roster_deal", {"handle": handle, "brand": brand, "amount": amount})
    licenses.notify("Roster deal: @%s × %s%s" % (handle, brand, " ($%d)" % amount if amount else ""),
                    "A manager says this deal came from a Monday report.", [("Manager", user["email"])])
    return mine(user)


def request_pilot(user, body):
    creator.track(user["id"], "roster_pilot_request", {})
    licenses.notify("Roster pilot request: %s" % user["email"], "Grant it in /admin/ (Roster pilots) if they manage creators.",
                    [("Account", user["email"]), ("Note", str(body.get("note") or "-")[:300])])
    return {"requested": True}


def billing_on():
    return bool(os.environ.get("STRIPE_SECRET_KEY") and os.environ.get("STRIPE_PRICE_ROSTER"))


def subscribe(user, body):
    """Stripe Checkout for the roster plan; without a Stripe price, record the commitment (Gate 4's other half)."""
    creator.track(user["id"], "roster_commit", {"price": ROSTER_PRICE_USD, "online": billing_on()})
    if billing_on():
        return creator.checkout(user, plan="roster")
    licenses.notify("Roster commitment: %s at $%d/month" % (user["email"], ROSTER_PRICE_USD),
                    "Billing for the roster plan isn't set up (STRIPE_PRICE_ROSTER): follow up by hand.", [("Account", user["email"])])
    return {"committed": True}


# ---------------------------------------------------------------- the weekly report

def paying_signal(brand, platform, lookups):
    """The brand's paid-creator activity from the cache; a fresh lookup only while `lookups` (a one-item list) lasts."""
    b = next((x for x in creator.CATALOG if x["name"] == brand), None)
    if not b:
        return None
    with connect() as db:
        cached = db.execute("SELECT result FROM brand_activity WHERE brand = %s AND platform = %s AND created_at > now() - interval '1 day'",
                            (brand, platform)).fetchone()
    if cached:
        return cached["result"]
    if lookups[0] <= 0:
        return None
    lookups[0] -= 1
    try:
        return creator.sponsor_activity(b, platform)
    except ApiError:
        return None


def scan(c, lookups):
    """One roster creator: new unpaid brand mentions since the last report, with receipts, paying signal and pitch."""
    item = {"id": c["id"], "platform": c["platform"], "handle": c["handle"], "name": c["name"], "brands": []}
    try:
        videos, source = creator.fetch_videos(c["platform"], c["handle"])
    except ApiError as e:
        return {**item, "error": str(e)}
    except Exception:                       # an unexpected failure on one creator mustn't sink the whole report
        traceback.print_exc()
        return {**item, "error": "Couldn't read this creator this week."}
    if not videos:
        return {**item, "error": "No public videos found."}
    with connect() as db:
        seen = {r["video_id"] for r in db.execute("SELECT video_id FROM roster_seen WHERE roster_creator_id = %s", (c["id"],))}
        with db.cursor() as cur:
            cur.executemany("INSERT INTO roster_seen (roster_creator_id, video_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            [(c["id"], v["id"]) for v in videos])
    fresh = [v for v in videos if v["id"] not in seen] if seen else videos
    prof = creator.profile(c["platform"], c["handle"], videos)
    rate = creator.rate_card(prof["medianViews"])
    item.update(first=not seen, videosRead=len(videos), newVideos=len(fresh), followers=prof["followers"],
                medianViews=prof["medianViews"], source=source)
    for row in [r for r in creator.aggregate(fresh) if r["status"] == "unpaid"][:BRANDS_PER_CREATOR]:
        best = row["receipts"][0]
        q = best["quote"] or {}
        activity = paying_signal(row["brand"], c["platform"], lookups)
        item["brands"].append({
            "brand": row["brand"], "spoken": row["spoken"], "tagged": row["tagged"], "views": row["organicViews"], "url": best["url"],
            "quote": (q.get("core") or q.get("text") or best["caption"] or "").strip()[:220], "at": creator.fmt_ts(q.get("start")),
            "paying": activity["verdict"] if activity else None, "paidPosts": activity["sponsored"] if activity else None,
            "pitch": creator.draft_pitch(prof, row, activity, rate)})
    return item


def run_roster(r, send=True):
    """Scan every creator on the roster, store the report and email it to the manager."""
    with connect() as db:
        creators = db.execute("SELECT * FROM roster_creators WHERE roster_id = %s ORDER BY id", (r["id"],)).fetchall()
        db.execute("UPDATE rosters SET last_run_at = now() WHERE id = %s", (r["id"],))
    lookups = [ACTIVITY_LOOKUPS]
    items = [scan(c, lookups) for c in creators]
    subject, body = render(items)
    sent = digest.send(r["email"], subject, body) if send else False
    with connect() as db:
        report = db.execute("INSERT INTO roster_reports (roster_id, items, sent) VALUES (%s, %s, %s) RETURNING id",
                            (r["id"], Jsonb(items), sent)).fetchone()
        db.execute("UPDATE rosters SET last_sent_at = now() WHERE id = %s", (r["id"],))
    creator.track(r["user_id"], "roster_report", {"report": report["id"], "creators": len(items),
                                                  "brands": sum(len(i["brands"]) for i in items), "sent": sent})
    return report["id"]


def render(items):
    """(subject, html) of the Monday email: per creator, the new unpaid brands with receipts and the paying signal."""
    e, font = digest.esc, digest.FONT
    hits = [i for i in items if i["brands"]]
    n = sum(len(i["brands"]) for i in hits)
    subject = ("%d unpaid brand mention%s across %d creator%s this week" % (n, "" if n == 1 else "s", len(hits), "" if len(hits) == 1 else "s")
               if n else "A quiet week: no new unpaid brand mentions on your roster")
    sections = []
    for i in hits:
        rows = "".join(
            '<tr><td style="padding:10px 0;border-top:1px solid #e6ebe8;font:14px %s;color:#1b2a23;"><b>%s</b>'
            ' <span style="color:#6b7a73;">&middot; %s &middot; %s views%s</span>'
            '<div style="margin:4px 0;font:italic 15px Georgia,serif;">&ldquo;%s&rdquo;</div>'
            '<a href="%s" style="color:#2f6b4f;">Watch%s &rarr;</a></td></tr>' % (
                font, e(b["brand"]), e(("said on camera ×%d" % b["spoken"]) if b["spoken"] else "tagged ×%d" % b["tagged"]),
                e(creator.fmt_num(b["views"])), (" &middot; " + e(PAYING[b["paying"]])) if b["paying"] else "",
                e(b["quote"]), e(b["url"]), " at %s" % e(b["at"]) if b["at"] else "")
            for b in i["brands"])
        sections.append('<tr><td style="padding-top:18px;font:600 15px %s;color:#1b2a23;">@%s <span style="font-weight:400;color:#6b7a73;">'
                        '&middot; %s%s</span></td></tr>%s' % (font, e(i["handle"]), e(creator.PLATFORM_NAMES[i["platform"]]),
                                                             " &middot; first report" if i.get("first") else "", rows))
    quiet = [i["handle"] for i in items if not i["brands"] and not i.get("error")]
    failed = [i["handle"] for i in items if i.get("error")]
    notes = ""
    if quiet:
        notes += '<p style="font:13px %s;color:#6b7a73;">Nothing new: %s.</p>' % (font, e(", ".join("@" + h for h in quiet)))
    if failed:
        notes += '<p style="font:13px %s;color:#9a5a1c;">Couldn\'t read this week: %s.</p>' % (font, e(", ".join("@" + h for h in failed)))
    body = ('<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f3f6f4;">'
            '<table role="presentation" width="100%%" style="max-width:600px;margin:0 auto;background:#fff;border-radius:16px;padding:24px 26px;">'
            '<tr><td style="font:500 12px %s;letter-spacing:.08em;text-transform:uppercase;color:#6b7a73;">'
            'Unprompted &middot; roster report</td></tr>'
            '<tr><td style="padding:8px 0 6px;font:600 22px/1.25 %s;color:#1b2a23;">%s</td></tr>%s'
            '<tr><td style="padding-top:14px;">%s<a href="%s" style="display:inline-block;margin-top:8px;padding:10px 16px;border-radius:999px;'
            'background:#1b2a23;color:#fff;font:600 14px %s;text-decoration:none;">Pitch drafts and receipts &rarr;</a></td></tr>'
            '</table></body></html>' % (font, font, e(subject), "".join(sections), notes, e(dashboard_url()), font))
    return subject, body


def due(any_day=False):
    """Rosters with access whose Monday (UTC) report hasn't gone out this week."""
    with connect() as db:
        rows = db.execute(
            "SELECT r.*, u.email, u.plan FROM rosters r JOIN users u ON u.id = r.user_id"
            " WHERE EXISTS (SELECT 1 FROM roster_creators c WHERE c.roster_id = r.id)"
            " AND (%s OR extract(isodow FROM now() AT TIME ZONE 'UTC') = 1)"
            " AND (r.last_sent_at IS NULL OR r.last_sent_at < now() - interval '6 days')", (any_day,)).fetchall()
    return [r for r in rows if access(r)]


def run_rosters(any_day=False):
    """Every due roster (scheduler and the digest cron). Claims each by stamping last_sent_at first: no double sends."""
    sent = 0
    for r in due(any_day):
        with connect() as db:
            if not db.execute("UPDATE rosters SET last_sent_at = now() WHERE id = %s AND (last_sent_at IS NULL OR"
                              " last_sent_at < now() - interval '6 days') RETURNING id", (r["id"],)).fetchone():
                continue
        run_roster(r)
        sent += 1
    return sent


# ---------------------------------------------------------------- operator (/admin/)

def admin_list():
    with connect() as db:
        rows = db.execute(
            "SELECT r.*, u.email, u.plan, (SELECT count(*) FROM roster_creators c WHERE c.roster_id = r.id) AS creators,"
            " (SELECT count(*) FROM roster_reports p WHERE p.roster_id = r.id) AS reports,"
            " (SELECT count(*) FROM roster_deals d WHERE d.roster_id = r.id) AS deals,"
            " EXISTS (SELECT 1 FROM events e WHERE e.user_id = r.user_id AND e.name = 'roster_commit') AS committed"
            " FROM rosters r JOIN users u ON u.id = r.user_id ORDER BY r.id DESC").fetchall()
    return {"rosters": [{"email": r["email"], "access": access(r), "pilotUntil": r["pilot_until"], "creators": r["creators"],
                         "reports": r["reports"], "deals": r["deals"], "committed": r["committed"], "lastSentAt": r["last_sent_at"]}
                        for r in rows]}


def admin_grant(body):
    """Give an existing account a free pilot (default PILOT_WEEKS weeks), and tell them."""
    email, weeks = str(body.get("email") or "").strip().lower(), body.get("weeks", PILOT_WEEKS)
    if not isinstance(weeks, int) or not 1 <= weeks <= 52:
        raise ApiError(400, "Weeks is 1-52.")
    with connect() as db:
        u = db.execute("SELECT * FROM users WHERE lower(email) = %s", (email,)).fetchone()
        if not u:
            raise ApiError(404, "No account with that email: ask them to sign up at /creators/roster first.")
        db.execute("INSERT INTO rosters (user_id, pilot_until) VALUES (%s, now() + %s * interval '1 week')"
                   " ON CONFLICT (user_id) DO UPDATE SET pilot_until = now() + %s * interval '1 week'", (u["id"], weeks, weeks))
    licenses.notify("Your Unprompted roster pilot is on (%d weeks)" % weeks,
                    "Add your creators, and every Monday you'll get the brands they mention unpaid, with receipts and pitch drafts.",
                    [("Start here", dashboard_url())], to=u["email"])
    return admin_list()


# ---------------------------------------------------------------- routes

def dispatch(handler):
    """/api/rosters/* with a Receipts account; /api/admin/rosters with the operator token."""
    path, method = urlparse(handler.path).path, handler.command
    if path.startswith("/api/admin/"):
        digest.require_operator(handler)
        if method == "GET" and path == "/api/admin/rosters":
            return admin_list()
        if method == "POST" and path == "/api/admin/rosters":
            return admin_grant(digest.read_json(handler))
        raise ApiError(404, "Not found.")
    path = path.removeprefix("/api/rosters")
    user = creator.require_user(handler.headers)
    if method == "GET" and path == "/mine":
        return mine(user)
    if method != "POST":
        raise ApiError(404 if method == "GET" else 405, "Not found." if method == "GET" else "Method not allowed.")
    if not (handler.headers.get("Content-Type") or "").startswith("application/json"):
        raise ApiError(415, "Send application/json.")
    route = {"/creators": add_creator, "/creators/remove": remove_creator, "/run": run_now, "/deal": deal,
             "/request": request_pilot, "/subscribe": subscribe}.get(path)
    if not route or not re.fullmatch(r"/[a-z/]+", path):
        raise ApiError(404, "Not found.")
    return route(user, digest.read_json(handler))
