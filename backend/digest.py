"""Weekly digest: save a brand search once (email only, no account) and get new mentions every week.

The push side of Unprompted, built on Oriane: each run is one Oriane content search for videos published since
the last run, stored like any dashboard search, diffed against what this subscriber has already been shown, and
mailed as a short email (quotes, who said it, spoken vs tagged vs disclosed) that links back to the dashboard.

Env: MAIL_RELAY_URL + MAIL_RELAY_SECRET or RESEND_API_KEY + DIGEST_FROM to send mail (otherwise emails are logged
and shown on the manage page), DIGEST_RUN_TOKEN to allow `POST /api/digests/run`, APP_URL for links.
"""
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import traceback
from pathlib import Path
from string import Template
from datetime import datetime, timedelta, timezone
from urllib import error, request
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

import creator
import demo
import server
from server import ApiError, connect, parse, profile_url

RUN_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS digests (
  id           serial PRIMARY KEY,
  email        text NOT NULL,
  brand        text NOT NULL,
  params       jsonb NOT NULL,                -- {variants, platform, lang}
  token        text UNIQUE NOT NULL,          -- confirm / manage / unsubscribe link
  confirmed_at timestamptz,
  paused_at    timestamptz,
  last_run_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (email, brand, params)
);
ALTER TABLE digests ADD COLUMN IF NOT EXISTS user_id int REFERENCES users ON DELETE SET NULL;  -- dashboard account that started it
CREATE TABLE IF NOT EXISTS digest_runs (
  id         serial PRIMARY KEY,
  digest_id  int NOT NULL REFERENCES digests ON DELETE CASCADE,
  search_id  int REFERENCES searches ON DELETE SET NULL,
  new_count  int NOT NULL,
  sent       boolean NOT NULL,
  subject    text,
  html       text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS license_requests (    -- a brand asking to run a creator's video as a paid ad; brokered by hand for now
  id         serial PRIMARY KEY,
  digest_id  int  NOT NULL REFERENCES digests ON DELETE CASCADE,
  video_id   text NOT NULL REFERENCES videos,
  days       int  NOT NULL,
  price_usd  int  NOT NULL,                     -- the indicative price shown when they asked
  note       text,
  status     text NOT NULL DEFAULT 'requested',  -- requested | contacted | accepted | declined | live | ended
  created_at timestamptz NOT NULL DEFAULT now()  -- one first request per video: payments.SCHEMA's partial unique index
);
ALTER TABLE license_requests                     -- brokering by hand: what the operator records as it happens
  ADD COLUMN IF NOT EXISTS contacted_at      timestamptz,
  ADD COLUMN IF NOT EXISTS responded_at      timestamptz,
  ADD COLUMN IF NOT EXISTS creator_price_usd int,
  ADD COLUMN IF NOT EXISTS final_price_usd   int,
  ADD COLUMN IF NOT EXISTS code_received_at  timestamptz,
  ADD COLUMN IF NOT EXISTS brand_paid_at     timestamptz,
  ADD COLUMN IF NOT EXISTS creator_paid_at   timestamptz,
  ADD COLUMN IF NOT EXISTS starts_at         timestamptz,
  ADD COLUMN IF NOT EXISTS expires_at        timestamptz,
  ADD COLUMN IF NOT EXISTS ops_note          text;
CREATE TABLE IF NOT EXISTS digest_seen (         -- videos already shown to this subscriber (or seen on the dashboard)
  digest_id int  NOT NULL REFERENCES digests ON DELETE CASCADE,
  video_id  text NOT NULL,
  PRIMARY KEY (digest_id, video_id)
);
"""

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EVERY_DAYS = 7
MAX_ITEMS = 8
MAX_PER_EMAIL = 2
KIND_LABEL = {"spoken": "said on camera only", "tagged": "tagged", "sponsored": "disclosed partnership"}
KIND_COLOR = {"spoken": "#2f6b4f", "tagged": "#2d6f86", "sponsored": "#9a5a1c"}
ORIANE_CREDIT = "Search and transcripts by Oriane"
LICENSE_DAYS = (30, 60, 90)
LICENSE_STATUS = ("requested", "contacted", "accepted", "declined", "live", "ended")
LICENSABLE = ("spoken", "tagged")                   # a disclosed partnership already has a deal behind it
LICENSE_BASIS = ("Indicative: paid usage is usually quoted at about 25% of a creator's fee per month; "
                 "the fee here is a $10-25 per 1,000 views rule of thumb on this video.")


def app_url():
    return os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")


def manage_url(token):
    return "%s/digest/%s" % (app_url(), token)


def collab_url(token, video_id):
    return "%s/collab/new/%s?v=%s" % (app_url(), token, video_id)


def license_url(token, video_id):
    return "%s/license/%s/%s" % (app_url(), token, video_id)


def quote(video, days):
    """The price a brand is shown: the creator's own rate when their verified handle has one, else license_price."""
    import licenses
    return licenses.brand_quote(video["platform"], video["handle"], video["views"], days)


def license_price(views, days=30):
    """Indicative fee to run one video as a paid ad for `days`: creator.rate_card's usage line (25% of the high end
    of a $10-25 CPM fee), at least $50 a month. The creator confirms the real price before anything is charged."""
    return max(50, creator.rate_card(views)["usage"]) * days // 30


def now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- mail

def mail_configured():
    relay = os.environ.get("MAIL_RELAY_URL") and os.environ.get("MAIL_RELAY_SECRET")
    resend = os.environ.get("RESEND_API_KEY") and os.environ.get("DIGEST_FROM")
    return bool(relay or resend)


def send(to, subject, body):
    """Send through the configured provider; without one, log it and report not sent."""
    relay_url = os.environ.get("MAIL_RELAY_URL")
    relay_secret = os.environ.get("MAIL_RELAY_SECRET")
    if relay_url and relay_secret:
        req = request.Request(relay_url, method="POST",
                              data=json.dumps({"secret": relay_secret, "to": to, "subject": subject, "html": body,
                                               "text": "%s\n\n%s/brands/" % (subject, app_url()), "name": "Unprompted"}).encode(),
                              headers={"Content-Type": "application/json", "User-Agent": "Unprompted digest (+%s)" % app_url()})
        try:
            with request.urlopen(req, timeout=30) as res:
                response = json.load(res)
            if isinstance(response, dict) and response.get("ok") is True:
                return True
            print("mail relay returned ok=false", file=sys.stderr, flush=True)
        except error.HTTPError as e:
            print("mail relay HTTP error (%s)" % e.code, file=sys.stderr, flush=True)
        except OSError as e:
            print("mail relay request failed (%s)" % type(e).__name__, file=sys.stderr, flush=True)
        except (ValueError, TypeError):
            print("mail relay returned invalid JSON", file=sys.stderr, flush=True)
        return False
    if not mail_configured():
        print("digest mail (not sent, no provider configured) to %s: %s" % (to, subject), file=sys.stderr, flush=True)
        return False
    req = request.Request("https://api.resend.com/emails", method="POST",
                          data=json.dumps({"from": os.environ["DIGEST_FROM"], "to": [to], "subject": subject, "html": body}).encode(),
                          headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["RESEND_API_KEY"],
                                   "User-Agent": "Unprompted digest (+%s)" % app_url()})
    try:
        with request.urlopen(req, timeout=20):
            return True
    except error.HTTPError as e:
        print("resend %s: %s" % (e.code, e.read()[:300]), file=sys.stderr, flush=True)
    except OSError as e:
        print("resend unreachable: %s" % e, file=sys.stderr, flush=True)
    return False


# ---------------------------------------------------------------- rendering

def esc(s):
    return html.escape(str(s or ""), quote=True)


def fmt(n):
    n = n or 0
    for unit, div in (("M", 1e6), ("K", 1e3)):
        if n >= div:
            return ("%.1f" % (n / div)).rstrip("0").rstrip(".") + unit
    return str(int(n))


def quote_html(q, caption):
    if q:
        t, (a, b) = q["text"], q["hit"]
        return "&ldquo;%s<b>%s</b>%s&rdquo;" % (esc(t[:a]), esc(t[a:b]), esc(t[b:]))
    return esc(caption[:160])


TEMPLATES = {name: Template((Path(__file__).parent / "templates" / ("digest_%s.html" % name)).read_text())
             for name in ("item", "email", "confirm")}
FONT = "-apple-system,Segoe UI,Helvetica,Arial,sans-serif"


def item_html(v, token):
    kind, q = v["kind"], v.get("quote")
    # Organic mentions get an invite button; it opens a proposal form, so a mail scanner opening links sends nothing.
    lic = ('<a href="%s" style="display:inline-block;margin-left:10px;padding:4px 11px;border-radius:999px;background:#d0f854;'
           'color:#1b2a23;font:600 12px %s;text-decoration:none;">Invite to collab &rarr;</a>'
           % (esc(collab_url(token, v["id"])), FONT)) if kind in LICENSABLE else ""
    return TEMPLATES["item"].substitute(
        license=lic, profile=esc(profile_url(v["platform"], v["handle"])), handle=esc(v["handle"]),
        platform="TikTok" if v["platform"] == "tiktok" else "Instagram", views=fmt(v["views"]),
        color=KIND_COLOR.get(kind, "#6b7a73"), label=esc(KIND_LABEL.get(kind, kind)),
        quote=quote_html(q, v.get("caption") or ""), url=esc(v["url"]),
        at=" at %d:%02d" % divmod(int(q["start"]), 60) if q and q.get("start") is not None else "")


def render(d, sid, new, since, found=()):
    """Subject and HTML for one digest email; `found` are gifted creators (seeding.py) found posting in this run."""
    import seeding
    brand, n = d["brand"], len(new)
    counts = {k: sum(1 for v in new if v["kind"] == k) for k in KIND_LABEL}
    parts = ["%d %s" % (counts[k], KIND_LABEL[k]) for k in KIND_LABEL if counts[k]]
    subject = ("%d new creator mention%s of %s this week" % (n, "" if n == 1 else "s", brand) if n
               else "%d gifted creator%s posted about %s" % (len(found), "" if len(found) == 1 else "s", brand))
    more = ('<tr><td style="font:14px %s;color:#48564f;">+ %d more on the dashboard.</td></tr>' % (FONT, n - MAX_ITEMS)) if n > MAX_ITEMS else ""
    body = TEMPLATES["email"].substitute(
        headline=("%d new video%s mention%s" % (n, "" if n == 1 else "s", "s" if n == 1 else "") if n
                  else "Gifted creators posted about"), brand=esc(brand),
        since="%d %s" % (since.day, since.strftime("%b")), filters=esc(filter_text(d["params"])), parts=esc(", ".join(parts) + "." if parts else ""),
        items="".join(item_html(v, d["token"]) for v in new[:MAX_ITEMS]), more=more, dash=esc("%s/brands/#search=%d" % (app_url(), sid)),
        seeding=seeding.email_section(d, found, lambda vid: collab_url(d["token"], vid)),
        credit=ORIANE_CREDIT, email=esc(d["email"]), manage=esc(manage_url(d["token"])))
    return subject, body


def filter_text(params):
    bits = []
    if params.get("platform") in ("instagram", "tiktok"):
        bits.append("on " + ("TikTok" if params["platform"] == "tiktok" else "Instagram"))
    if params.get("lang") in ("en", "ar"):
        bits.append("in " + ("English" if params["lang"] == "en" else "Arabic"))
    if params.get("variants"):
        bits.append("also listening for " + ", ".join(params["variants"]))
    return (", " + ", ".join(bits)) if bits else ""


def confirm_mail(d):
    link = manage_url(d["token"]) + "?confirm=1"
    return ("Confirm your weekly %s report" % d["brand"],
            TEMPLATES["confirm"].substitute(brand=esc(d["brand"]), filters=esc(filter_text(d["params"])), link=esc(link), credit=ORIANE_CREDIT))


# ---------------------------------------------------------------- subscriptions

def request_view(r):
    import licenses
    return {"days": r["days"], "priceUsd": licenses.brand_price(r), "status": r["status"], "note": r["note"],
            "createdAt": r["created_at"], "expiresAt": r["expires_at"]}


def row_view(d, runs=None, licenses=None):
    latest = next((r for r in runs or [] if r["html"]), None)
    return {"token": d["token"], "brand": d["brand"], "params": d["params"], "email": d["email"],
            "status": "unsubscribed" if d.get("gone") else "paused" if d["paused_at"] else "active" if d["confirmed_at"] else "pending",
            "createdAt": d["created_at"], "lastRunAt": d["last_run_at"], "manageUrl": manage_url(d["token"]),
            "mail": mail_configured(), "every": EVERY_DAYS,
            "runs": [{"id": r["id"], "createdAt": r["created_at"], "newCount": r["new_count"], "sent": r["sent"],
                      "searchId": r["search_id"], "subject": r["subject"]} for r in runs or []],
            "latestHtml": latest["html"] if latest else None,
            "licenses": [{**request_view(r), "handle": r["handle"], "url": license_url(d["token"], r["video_id"])} for r in licenses or []]}


def ensure_active_capacity(db):
    limit = int(os.environ.get("DIGEST_MAX_ACTIVE", "25"))
    active = db.execute("SELECT count(*) AS n FROM digests WHERE confirmed_at IS NOT NULL AND paused_at IS NULL").fetchone()["n"]
    if active >= limit:
        raise ApiError(503, "Weekly watches are full right now.")


def subscribe(body, user=None):
    """Watch a search: a weekly email of its new mentions. Double opt-in when mail is configured; otherwise active
    immediately. A signed-in user's watch is marked on their saved searches."""
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ApiError(400, "Enter a valid email address.")
    variants = body.get("variants", "")
    brand, variants, _, p = parse({**body, "variants": ", ".join(variants) if isinstance(variants, list) else variants, "days": 30})
    params = {"variants": variants, "platform": p["platform"], "lang": p["lang"]}
    if p.get("about"):
        params["about"] = p["about"]
    brand_user = user if user and user["role"] == "brand" else None
    search_id = body.get("searchId")
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(471478344)")
        d = db.execute("SELECT * FROM digests WHERE lower(email) = lower(%s) AND brand = %s AND params = %s",
                       (email, brand, Jsonb(params))).fetchone()
        created = d is None
        if created:
            count = db.execute("SELECT count(*) AS n FROM digests WHERE lower(email) = lower(%s)", (email,)).fetchone()["n"]
            if count >= MAX_PER_EMAIL:
                raise ApiError(429, "You already watch 2 searches; stop one first.")
            if not mail_configured():
                ensure_active_capacity(db)
            d = db.execute("INSERT INTO digests (email, brand, params, token, confirmed_at, user_id) VALUES (%s, %s, %s, %s, %s, %s)"
                           " RETURNING *", (email, brand, Jsonb(params), secrets.token_urlsafe(16),
                                            None if mail_configured() else now(), brand_user["id"] if brand_user else None)).fetchone()
        elif d["paused_at"]:
            if d["confirmed_at"]:
                ensure_active_capacity(db)
            d = db.execute("UPDATE digests SET paused_at = NULL WHERE id = %s RETURNING *", (d["id"],)).fetchone()
        if isinstance(search_id, int):  # what they just saw on the dashboard counts as seen
            db.execute("INSERT INTO digest_seen (digest_id, video_id) SELECT %s, video_id FROM mentions WHERE search_id = %s"
                       " ON CONFLICT DO NOTHING", (d["id"], search_id))
    mailed = send(email, *confirm_mail(d)) if not d["confirmed_at"] else False
    if brand_user:
        creator.track(brand_user["id"], "digest_subscribe", {"digest": d["id"], "brand": brand})
    return {**row_view(d), "created": created, "mailed": mailed}


def load_digest(token):
    with connect() as db:
        d = db.execute("SELECT * FROM digests WHERE token = %s", (token,)).fetchone()
        if not d:
            raise ApiError(404, "This digest link isn't valid any more.")
        runs = db.execute("SELECT * FROM digest_runs WHERE digest_id = %s ORDER BY id DESC LIMIT 12", (d["id"],)).fetchall()
        licenses = db.execute("SELECT r.*, v.handle FROM license_requests r JOIN videos v ON v.id = r.video_id"
                              " WHERE r.digest_id = %s ORDER BY r.id DESC", (d["id"],)).fetchall()
    return d, runs, licenses


# ---------------------------------------------------------------- license requests

def license_video(d, vid):
    """The video behind a license link: it must have been in one of this digest's runs and not be a disclosed ad."""
    with connect() as db:
        row = db.execute(
            "SELECT v.raw, m.kind, m.quote FROM mentions m JOIN searches s ON s.id = m.search_id JOIN videos v ON v.id = m.video_id"
            " WHERE m.video_id = %s AND s.params->>'digest' = %s ORDER BY s.id DESC LIMIT 1", (vid, str(d["id"]))).fetchone()
    if not row:
        raise ApiError(404, "This video isn't in your weekly reports.")
    if row["kind"] not in LICENSABLE:
        raise ApiError(400, "This video is a disclosed partnership, so it already has a deal behind it.")
    raw = row["raw"]
    return {"id": vid, "handle": raw["profileHandle"], "platform": raw["platform"], "url": server.post_url(raw),
            "profile": profile_url(raw["platform"], raw["profileHandle"]), "views": raw.get("viewsCount") or 0,
            "publishedAt": raw.get("publishedAt"), "kind": row["kind"], "quote": row["quote"],
            "caption": (raw.get("caption") or "")[:200]}


def license_view(token, vid, body=None):
    """GET (body None): the video, its prices and the latest request for it. POST: record the first request (and tell
    the operator), or with {"action": ...} pay, answer a counter-offer or renew (payments.brand_action)."""
    import payments
    d, _, _ = load_digest(token)
    video = license_video(d, vid)
    if body is not None and body.get("action"):
        out = payments.brand_action(d, vid, body)
        if out:
            return out
    elif body is not None:
        days = body.get("days", 30)
        if days not in LICENSE_DAYS:
            raise ApiError(400, "Choose 30, 60 or 90 days.")
        note = str(body.get("note") or "").strip()[:500] or None
        with connect() as db:
            req = db.execute("INSERT INTO license_requests (digest_id, video_id, days, price_usd, note) VALUES (%s, %s, %s, %s, %s)"
                             " ON CONFLICT (digest_id, video_id) WHERE renewal_of IS NULL DO NOTHING RETURNING *",
                             (d["id"], vid, days, quote(video, days), note)).fetchone()
        if req:
            import licenses  # the creator side; imported here because it imports this module
            after = licenses.on_request(req["id"])     # the handle owner's rules may answer it straight away
            creator.track(d["user_id"], "license_request", {"digest": d["id"], "video": vid, "days": days, "price": req["price_usd"]})
            notify_license(d, video, req, after)
    else:  # the page load; the email button is the only way here, so this is also the click-through
        creator.track(d["user_id"], "license_view", {"digest": d["id"], "video": vid, "price": quote(video, 30)})
    req = payments.latest(d["id"], vid)
    return {"brand": d["brand"], "email": d["email"], "manageUrl": manage_url(d["token"]), "video": video,
            "prices": [{"days": n, "usd": quote(video, n)} for n in LICENSE_DAYS], "basis": LICENSE_BASIS,
            "request": payments.brand_view(req) if req else None}


def notify_license(d, video, req, after):
    """Email the operator (LICENSE_NOTIFY_EMAIL); without it the request is only logged and waits in license_requests.
    `after` is the request once the creator's rules ran: it carries the creator link and whether the handle is claimed."""
    import licenses
    subject = "License request: %s wants @%s's video for %d days (~$%d)" % (d["brand"], video["handle"], req["days"], req["price_usd"])
    to = os.environ.get("LICENSE_NOTIFY_EMAIL")
    if not to:
        print("license request (no LICENSE_NOTIFY_EMAIL): %s" % subject, file=sys.stderr, flush=True)
        return False
    q = video["quote"]
    rows = [("Brand", d["brand"]), ("Requested by", d["email"]), ("Creator", "@%s on %s" % (video["handle"], video["platform"])),
            ("Video", video["url"]), ("Views", fmt(video["views"])), ("Quote", q["text"] if q else video["caption"]),
            ("Window", "%d days" % req["days"]), ("Indicative price", "$%d" % req["price_usd"]), ("Note", req["note"] or "-"),
            ("Creator link", licenses.offer_url(after["creator_token"])),
            ("Handle claimed", "yes: they were emailed" if after["owner_id"] else "no: send them the creator link"),
            ("Status", after["status"] + (" (by the creator's own rule)" if after["responded_via"] == "auto" else "")),
            ("Request id", req["id"])]
    body = ('<p style="font:15px %s;">Broker this with the creator in /admin/ (request %d).</p>'
            '<table style="font:14px %s;border-collapse:collapse;">%s</table>' % (
                FONT, req["id"], FONT, "".join('<tr><td style="padding:3px 14px 3px 0;color:#6b7a73;">%s</td><td>%s</td></tr>'
                                               % (esc(k), esc(v)) for k, v in rows)))
    return send(to, subject, body)


def sample():
    with connect() as db:
        run = db.execute(
            "SELECT r.new_count, r.created_at, r.html, d.brand, d.email, d.token"
            " FROM digest_runs r JOIN digests d ON d.id = r.digest_id"
            " WHERE r.html IS NOT NULL ORDER BY r.id DESC LIMIT 1").fetchone()
    if not run:
        raise ApiError(404, "No sample digest yet.")
    sample_html = re.sub(
        r'href="[^"]*/(?:digest/%s(?:\?unsubscribe=1)?|license/%s/[^"]*|collab/new/%s[^"]*)"' % ((re.escape(run["token"]),) * 3),
        lambda _: 'href="%s"' % esc("%s/brands/" % app_url()),
        run["html"])
    sample_html = sample_html.replace(run["token"], "sample")
    sample_html = sample_html.replace(esc(run["email"]), "you@brand.com")
    return {"brand": run["brand"], "newCount": run["new_count"], "createdAt": run["created_at"], "html": sample_html}


def manage(token, action=None):
    d, runs, licenses = load_digest(token)
    if action:
        sql = {"confirm": "UPDATE digests SET confirmed_at = coalesce(confirmed_at, now()), paused_at = NULL WHERE id = %s RETURNING *",
               "pause": "UPDATE digests SET paused_at = coalesce(paused_at, now()) WHERE id = %s RETURNING *",
               "resume": "UPDATE digests SET paused_at = NULL WHERE id = %s RETURNING *",
               "unsubscribe": "DELETE FROM digests WHERE id = %s RETURNING *"}.get(action)
        if not sql:
            raise ApiError(404, "Not found.")
        with connect() as db:
            if action in ("confirm", "resume"):
                db.execute("SELECT pg_advisory_xact_lock(471478344)")
                current = db.execute("SELECT confirmed_at, paused_at FROM digests WHERE id = %s", (d["id"],)).fetchone()
                was_active = current["confirmed_at"] is not None and current["paused_at"] is None
                becomes_active = action == "confirm" or current["confirmed_at"] is not None
                if becomes_active and not was_active:
                    ensure_active_capacity(db)
            d = db.execute(sql, (d["id"],)).fetchone()
        if action == "unsubscribe":
            return row_view({**d, "gone": True})
        if action == "confirm" and d["user_id"]:
            creator.track(d["user_id"], "digest_confirm", {"digest": d["id"], "brand": d["brand"]})
    import seeding
    return {**row_view(d, runs, licenses), "seeding": seeding.summary(d)}


# ---------------------------------------------------------------- weekly run

def run_one(d, force=False):
    """One Oriane search for videos since the last run, diffed against what this subscriber has seen; mail if new."""
    # First digest covers the week before the search was saved; later ones overlap the previous run by a day.
    since = (d["last_run_at"] or d["created_at"] - timedelta(days=EVERY_DAYS)) - timedelta(days=1)
    since = max(since, now() - timedelta(days=30))
    brand, variants, filters, params = parse({"brand": d["brand"], **d["params"], "variants": ", ".join(d["params"]["variants"]), "days": 30})
    filters["publishedAt"] = {"after": since.date().isoformat()}
    data = server.oriane(filters, limit=100, sort="publishedAt")
    results = data["data"]["results"]
    sid = server.new_search(brand, {**params, "digest": d["id"]}, data)
    server.store_results(sid, results, brand, variants, params.get("about", ()))
    import seeding                                # gifted creators' posts join this run's results
    try:
        found = seeding.check(d, sid)
    except Exception:                             # never let it block the brand's weekly report
        traceback.print_exc()
        found = []
    with connect() as db:
        seen = {r["video_id"] for r in db.execute("SELECT video_id FROM digest_seen WHERE digest_id = %s", (d["id"],))}
        db.execute("UPDATE digests SET last_run_at = now() WHERE id = %s", (d["id"],))
    new = [v for v in server.load(sid)["videos"]
           if v["id"] not in seen and v["kind"] in KIND_LABEL and v.get("publishedAt") and v["publishedAt"][:10] >= since.date().isoformat()]
    subject = body = None
    sent = False
    if new or found:
        subject, body = render(d, sid, new, since, found)
        sent = send(d["email"], subject, body)
        creator.track(d["user_id"], "report_sent", {"digest": d["id"], "new": len(new), "sent": sent, "gifted_found": len(found),
                                                    "licensable": sum(v["kind"] in LICENSABLE for v in new[:MAX_ITEMS])})
    with connect() as db:
        db.execute("INSERT INTO digest_seen (digest_id, video_id) SELECT %s, video_id FROM mentions WHERE search_id = %s ON CONFLICT DO NOTHING",
                   (d["id"], sid))
        run = db.execute("INSERT INTO digest_runs (digest_id, search_id, new_count, sent, subject, html)"
                         " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", (d["id"], sid, len(new), sent, subject, body)).fetchone()
    return {"digest": d["id"], "run": run["id"], "searchId": sid, "new": len(new), "sent": sent, "matched": len(results),
            "giftedFound": len(found)}


def due(digest_id=None, force=False):
    with connect() as db:
        if digest_id:
            return db.execute("SELECT * FROM digests WHERE id = %s", (digest_id,)).fetchall()
        # A week after the last run. The hour of slack lets a daily cron that fires a little early still land on day 7.
        where = "" if force else " AND (last_run_at IS NULL OR last_run_at < now() - interval '%d days' + interval '1 hour')" % EVERY_DAYS
        return db.execute("SELECT * FROM digests WHERE confirmed_at IS NOT NULL AND paused_at IS NULL" + where + " ORDER BY id").fetchall()


def run_due(digest_id=None, force=False):
    """Run every digest that is due (or one by id). One Oriane search each; nothing when there are none."""
    with RUN_LOCK:
        out = []
        for d in due(digest_id, force):
            try:
                out.append(run_one(d))
            except Exception as e:  # one broken digest must not stop the others
                traceback.print_exc()
                out.append({"digest": d["id"], "error": str(e)})
        return {"ran": len(out), "results": out}


def require_operator(handler):
    """The operator's bearer token (DIGEST_RUN_TOKEN) guards runs and the licence console; without it they don't exist."""
    token = os.environ.get("DIGEST_RUN_TOKEN")
    if not token:
        raise ApiError(404, "Not found.")
    if not secrets.compare_digest((handler.headers.get("Authorization") or "").removeprefix("Bearer ").strip(), token):
        raise ApiError(401, "Bad run token.")


def run_route(handler, body):
    """POST /api/digests/run with the operator token; {"id": n} runs one digest now, {"force": true} runs all."""
    require_operator(handler)
    digest_id = body.get("id")
    if body.get("resend") and isinstance(digest_id, int):
        return resend_last(digest_id)
    out = run_due(digest_id if isinstance(digest_id, int) else None, bool(body.get("force")))
    if not isinstance(digest_id, int):             # the same cron runs the other weekly jobs, each on its own
        import licenses
        import packaging
        import payments
        import rosters
        import collabs
        for key, job in (("summaries", lambda: licenses.run_summaries(bool(body.get("forceSummaries")))),
                         ("collabs", lambda: collabs.run_weekly(bool(body.get("forceSummaries")))),
                         ("licenses", payments.run_reminders), ("rosters", rosters.run_rosters), ("feeds", packaging.run_feeds)):
            try:
                out[key] = job()
            except Exception as e:
                traceback.print_exc()
                out[key] = {"error": str(e)}
    return out


def jobs():
    """The background jobs the scheduler runs every pass: brand reports, creator summaries, licence reminders, rosters
    and the Weekly leads emails (packaging.py)."""
    import collabs
    import licenses
    import packaging
    import payments
    import rosters
    return (run_due, licenses.run_summaries, collabs.run_weekly, payments.run_reminders, rosters.run_rosters, packaging.run_feeds)


def resend_last(digest_id):
    """Mail the newest stored run of one digest again (no Oriane call) and record whether it went out."""
    with connect() as db:
        d = db.execute("SELECT * FROM digests WHERE id = %s", (digest_id,)).fetchone()
        run = db.execute("SELECT * FROM digest_runs WHERE digest_id = %s AND html IS NOT NULL ORDER BY id DESC LIMIT 1",
                         (digest_id,)).fetchone()
    if not d or not run:
        raise ApiError(404, "No digest with a rendered run.")
    sent = send(d["email"], run["subject"], run["html"])
    with connect() as db:
        db.execute("UPDATE digest_runs SET sent = sent OR %s WHERE id = %s", (sent, run["id"]))
    return {"digest": digest_id, "run": run["id"], "new": run["new_count"], "sent": sent, "resent": True}


# ---------------------------------------------------------------- operator console (/admin/)

ADMIN_SQL = """
SELECT r.*, d.brand, d.email, d.token, v.handle, v.platform, v.views, v.raw,
       round(extract(epoch FROM now() - r.created_at) / 3600) AS age_hours,
       EXISTS (SELECT 1 FROM creator_handles h WHERE h.platform = v.platform AND h.handle = lower(v.handle)
               AND h.verified_at IS NOT NULL) AS claimed
FROM license_requests r JOIN digests d ON d.id = r.digest_id JOIN videos v ON v.id = r.video_id
"""


def admin_row(r):
    import licenses
    return {"brandPriceUsd": licenses.brand_price(r), "creatorShareUsd": licenses.creator_share(r),
            "id": r["id"], "status": r["status"], "brand": r["brand"], "requestedBy": r["email"], "handle": r["handle"],
            "platform": r["platform"], "views": r["views"], "videoUrl": server.post_url(r["raw"]),
            "profileUrl": profile_url(r["platform"], r["handle"]), "page": license_url(r["token"], r["video_id"]),
            "days": r["days"], "priceUsd": r["price_usd"], "creatorPriceUsd": r["creator_price_usd"],
            "finalPriceUsd": r["final_price_usd"], "note": r["note"], "opsNote": r["ops_note"], "ageHours": int(r["age_hours"]),
            "offerUrl": "%s/offer/%s" % (app_url(), r["creator_token"]), "claimed": r["claimed"], "respondedVia": r["responded_via"],
            "adCode": r["ad_code"], "declineReason": r["decline_reason"], "renewalOf": r["renewal_of"],
            "paidOnline": bool(r["stripe_charge"]), "transfer": r["stripe_transfer"], "refundedAt": r["refunded_at"],
            **{k: r[c] for k, c in (("createdAt", "created_at"), ("contactedAt", "contacted_at"), ("respondedAt", "responded_at"),
                                    ("codeReceivedAt", "code_received_at"), ("brandPaidAt", "brand_paid_at"),
                                    ("creatorPaidAt", "creator_paid_at"), ("startsAt", "starts_at"), ("expiresAt", "expires_at"))}}


def admin_list():
    with connect() as db:
        rows = db.execute(ADMIN_SQL + " ORDER BY r.status IN ('declined', 'live'), r.id DESC LIMIT 200").fetchall()
    return {"requests": [admin_row(r) for r in rows]}


def admin_update(rid, body):
    """Record a brokering step. A status stamps the times it implies, once (going live also starts the window and sets
    the expiry from the requested days). Flags sent alongside override those stamps: true stamps now, false clears."""
    sets = {}                                     # column -> (SQL expression, params); later entries win
    status = body.get("status")
    if status is not None:
        if status not in LICENSE_STATUS:
            raise ApiError(400, "Unknown status.")
        sets["status"] = ("%s", (status,))
        stamp = {"contacted": ["contacted_at"], "accepted": ["contacted_at", "responded_at"], "declined": ["contacted_at", "responded_at"],
                 "live": ["contacted_at", "responded_at", "code_received_at", "starts_at"]}.get(status, [])
        sets.update({c: ("coalesce(%s, now())" % c, ()) for c in stamp})
        if "responded_at" in stamp:                  # answered through you, unless the creator already did in the app
            sets["responded_via"] = ("coalesce(responded_via, 'operator')", ())
        if status == "live":
            sets["expires_at"] = ("coalesce(expires_at, coalesce(starts_at, now()) + days * interval '1 day')", ())
    for key, col in (("creatorPriceUsd", "creator_price_usd"), ("finalPriceUsd", "final_price_usd")):
        if key in body:
            v = body[key]
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 1_000_000):
                raise ApiError(400, "Prices are whole dollars.")
            sets[col] = ("%s", (v,))
    for key, col in (("codeReceived", "code_received_at"), ("brandPaid", "brand_paid_at"), ("creatorPaid", "creator_paid_at")):
        if key in body:
            sets[col] = ("coalesce(%s, now())" % col if body[key] else "NULL", ())
    if "opsNote" in body:
        sets["ops_note"] = ("%s", (str(body["opsNote"] or "").strip()[:1000] or None,))
    if not sets:
        raise ApiError(400, "Nothing to update.")
    sql = "UPDATE license_requests SET %s WHERE id = %%s RETURNING id" % ", ".join("%s = %s" % (c, e) for c, (e, _) in sets.items())
    with connect() as db:
        before = db.execute("SELECT status FROM license_requests WHERE id = %s", (rid,)).fetchone()
        if not db.execute(sql, (*(p for _, ps in sets.values() for p in ps), rid)).fetchone():
            raise ApiError(404, "No such request.")
    import payments                             # paid + code now means live; a decline after a Stripe payment is refunded
    if status in ("accepted", "declined") and before["status"] != status:
        payments.tell_brand(rid, status)
    payments.maybe_go_live(rid)
    payments.pay_creator(rid)
    payments.refund_if_paid(rid)
    with connect() as db:
        return admin_row(db.execute(ADMIN_SQL + " WHERE r.id = %s", (rid,)).fetchone())


def admin(handler):
    """Route /api/admin/licenses (GET list) and /api/admin/licenses/<id> (POST update), operator token only."""
    require_operator(handler)
    path = urlparse(handler.path).path
    if handler.command == "GET" and path == "/api/admin/licenses":
        return admin_list()
    m = re.fullmatch(r"/api/admin/licenses/(\d+)", path)
    if handler.command == "POST" and m:
        return admin_update(int(m.group(1)), read_json(handler))
    raise ApiError(404, "Not found.")


def scheduler(every_s=900, first_s=60):
    """Background loop (DIGEST_SCHEDULER=1): check after first_s, then every every_s while the process is up.

    Off by default so a deployment spends no Oriane credits until the operator opts in; hosts that sleep when idle
    only run checks while awake; use an external cron for reliable wall-clock execution.
    """
    if os.environ.get("DIGEST_SCHEDULER") != "1" or demo.active():
        return

    def loop():
        wait_s = first_s
        while True:
            time.sleep(wait_s)
            for job in jobs():                      # each on its own: one failing doesn't stop the rest
                try:
                    job()
                except Exception:
                    traceback.print_exc()
            wait_s = every_s
    threading.Thread(target=loop, name="digest-scheduler", daemon=True).start()


# ---------------------------------------------------------------- dispatch

def dispatch(handler):
    """Route one /api/digests* request."""
    url = urlparse(handler.path)
    path, method = url.path.removeprefix("/api/digests"), handler.command
    m = re.fullmatch(r"/([A-Za-z0-9_-]{16,32})(?:/(confirm|pause|resume|unsubscribe))?", path)
    lic = re.fullmatch(r"/([A-Za-z0-9_-]{16,32})/license/([A-Za-z0-9_.-]{1,96})", path)   # demo ids carry the handle's dots
    gifts = re.fullmatch(r"/([A-Za-z0-9_-]{16,32})/gifts(/clear)?", path)
    if method == "GET":
        if path == "/sample":
            return sample()
        if lic:
            return license_view(lic.group(1), lic.group(2))
        if m and not m.group(2):
            return manage(m.group(1))
        raise ApiError(404, "Not found.")
    if method != "POST":
        raise ApiError(405, "Method not allowed.")
    if not (handler.headers.get("Content-Type") or "").startswith("application/json"):
        raise ApiError(415, "Send application/json.")
    body = read_json(handler)
    if path == "":
        return subscribe(body, creator.current_user(handler.headers))
    if path == "/run":
        return run_route(handler, body)
    if m and m.group(2):
        return manage(m.group(1), m.group(2))
    if lic:
        return license_view(lic.group(1), lic.group(2), body)
    if gifts:
        import seeding
        d, _, _ = load_digest(gifts.group(1))
        return seeding.clear(d) if gifts.group(2) else seeding.upload(d, body)
    raise ApiError(404, "Not found.")


def read_json(handler):
    try:
        body = json.loads(handler.rfile.read(min(int(handler.headers.get("Content-Length") or 0), 10_000)) or b"{}")
    except ValueError:
        raise ApiError(400, "Invalid JSON.")
    if not isinstance(body, dict):
        raise ApiError(400, "Invalid request.")
    return body
