"""Weekly digest: save a brand search once (email only, no account) and get new mentions every week.

The push side of Unprompted, built on Oriane: each run is one Oriane content search for videos published since
the last run, stored like any dashboard search, diffed against what this subscriber has already been shown, and
mailed as a short email (quotes, who said it, spoken vs tagged vs disclosed) that links back to the dashboard.

Env: RESEND_API_KEY + DIGEST_FROM to send mail (otherwise emails are logged and shown on the manage page),
DIGEST_RUN_TOKEN to allow `POST /api/digests/run`, APP_URL for links.
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

import demo
import server
from server import ApiError, connect, parse, profile_url

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
CREATE TABLE IF NOT EXISTS digest_seen (         -- videos already shown to this subscriber (or seen on the dashboard)
  digest_id int  NOT NULL REFERENCES digests ON DELETE CASCADE,
  video_id  text NOT NULL,
  PRIMARY KEY (digest_id, video_id)
);
"""

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EVERY_DAYS = 7
MAX_ITEMS = 8
KIND_LABEL = {"spoken": "said on camera only", "tagged": "tagged", "sponsored": "disclosed partnership"}
KIND_COLOR = {"spoken": "#2f6b4f", "tagged": "#2d6f86", "sponsored": "#9a5a1c"}
ORIANE_CREDIT = "Search and transcripts by Oriane"


def app_url():
    return os.environ.get("APP_URL", "http://127.0.0.1:8000").rstrip("/")


def manage_url(token):
    return "%s/digest/%s" % (app_url(), token)


def now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- mail

def mail_configured():
    return bool(os.environ.get("RESEND_API_KEY") and os.environ.get("DIGEST_FROM"))


def send(to, subject, body):
    """Send through Resend; without a key, log it and report not sent (the manage page still shows the digest)."""
    if not mail_configured():
        print("digest mail (not sent, RESEND_API_KEY unset) to %s: %s" % (to, subject), file=sys.stderr, flush=True)
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


def item_html(v):
    kind, q = v["kind"], v.get("quote")
    return TEMPLATES["item"].substitute(
        profile=esc(profile_url(v["platform"], v["handle"])), handle=esc(v["handle"]),
        platform="TikTok" if v["platform"] == "tiktok" else "Instagram", views=fmt(v["views"]),
        color=KIND_COLOR.get(kind, "#6b7a73"), label=esc(KIND_LABEL.get(kind, kind)),
        quote=quote_html(q, v.get("caption") or ""), url=esc(v["url"]),
        at=" at %d:%02d" % divmod(int(q["start"]), 60) if q and q.get("start") is not None else "")


def render(d, sid, new, since):
    """Subject and HTML for one digest email."""
    brand, n = d["brand"], len(new)
    counts = {k: sum(1 for v in new if v["kind"] == k) for k in KIND_LABEL}
    parts = ["%d %s" % (counts[k], KIND_LABEL[k]) for k in KIND_LABEL if counts[k]]
    subject = "%d new creator mention%s of %s this week" % (n, "" if n == 1 else "s", brand)
    more = ('<tr><td style="font:14px %s;color:#48564f;">+ %d more on the dashboard.</td></tr>' % (FONT, n - MAX_ITEMS)) if n > MAX_ITEMS else ""
    body = TEMPLATES["email"].substitute(
        headline="%d new video%s mention%s" % (n, "" if n == 1 else "s", "s" if n == 1 else ""), brand=esc(brand),
        since=since.strftime("%-d %b"), filters=esc(filter_text(d["params"])), parts=esc(", ".join(parts) + "." if parts else ""),
        items="".join(item_html(v) for v in new[:MAX_ITEMS]), more=more, dash=esc("%s/brands/#search=%d" % (app_url(), sid)),
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
    return ("Confirm your weekly %s digest" % d["brand"],
            TEMPLATES["confirm"].substitute(brand=esc(d["brand"]), filters=esc(filter_text(d["params"])), link=esc(link), credit=ORIANE_CREDIT))


# ---------------------------------------------------------------- subscriptions

def row_view(d, runs=None):
    latest = next((r for r in runs or [] if r["html"]), None)
    return {"token": d["token"], "brand": d["brand"], "params": d["params"], "email": d["email"],
            "status": "unsubscribed" if d.get("gone") else "paused" if d["paused_at"] else "active" if d["confirmed_at"] else "pending",
            "createdAt": d["created_at"], "lastRunAt": d["last_run_at"], "manageUrl": manage_url(d["token"]),
            "mail": mail_configured(), "every": EVERY_DAYS,
            "runs": [{"id": r["id"], "createdAt": r["created_at"], "newCount": r["new_count"], "sent": r["sent"],
                      "searchId": r["search_id"], "subject": r["subject"]} for r in runs or []],
            "latestHtml": latest["html"] if latest else None}


def subscribe(body):
    """Save a search for a weekly email. Double opt-in when mail is configured; otherwise active immediately."""
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise ApiError(400, "Enter a valid email address.")
    variants = body.get("variants", "")
    brand, variants, _, p = parse({**body, "variants": ", ".join(variants) if isinstance(variants, list) else variants, "days": 30})
    params = {"variants": variants, "platform": p["platform"], "lang": p["lang"]}
    search_id = body.get("searchId")
    with connect() as db:
        d = db.execute("SELECT * FROM digests WHERE email = %s AND brand = %s AND params = %s", (email, brand, Jsonb(params))).fetchone()
        created = d is None
        if created:
            d = db.execute("INSERT INTO digests (email, brand, params, token, confirmed_at) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                           (email, brand, Jsonb(params), secrets.token_urlsafe(16), None if mail_configured() else now())).fetchone()
        elif d["paused_at"]:
            d = db.execute("UPDATE digests SET paused_at = NULL WHERE id = %s RETURNING *", (d["id"],)).fetchone()
        if isinstance(search_id, int):  # what they just saw on the dashboard counts as seen
            db.execute("INSERT INTO digest_seen (digest_id, video_id) SELECT %s, video_id FROM mentions WHERE search_id = %s"
                       " ON CONFLICT DO NOTHING", (d["id"], search_id))
    if not d["confirmed_at"]:
        send(email, *confirm_mail(d))
    return {**row_view(d), "created": created}


def load_digest(token):
    with connect() as db:
        d = db.execute("SELECT * FROM digests WHERE token = %s", (token,)).fetchone()
        if not d:
            raise ApiError(404, "This digest link isn't valid any more.")
        runs = db.execute("SELECT * FROM digest_runs WHERE digest_id = %s ORDER BY id DESC LIMIT 12", (d["id"],)).fetchall()
    return d, runs


def manage(token, action=None):
    d, runs = load_digest(token)
    if action:
        sql = {"confirm": "UPDATE digests SET confirmed_at = coalesce(confirmed_at, now()), paused_at = NULL WHERE id = %s RETURNING *",
               "pause": "UPDATE digests SET paused_at = coalesce(paused_at, now()) WHERE id = %s RETURNING *",
               "resume": "UPDATE digests SET paused_at = NULL WHERE id = %s RETURNING *",
               "unsubscribe": "DELETE FROM digests WHERE id = %s RETURNING *"}.get(action)
        if not sql:
            raise ApiError(404, "Not found.")
        with connect() as db:
            d = db.execute(sql, (d["id"],)).fetchone()
        if action == "unsubscribe":
            return row_view({**d, "gone": True})
    return row_view(d, runs)


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
    server.store_results(sid, results, brand, variants)
    with connect() as db:
        seen = {r["video_id"] for r in db.execute("SELECT video_id FROM digest_seen WHERE digest_id = %s", (d["id"],))}
        db.execute("UPDATE digests SET last_run_at = now() WHERE id = %s", (d["id"],))
    new = [v for v in server.load(sid)["videos"]
           if v["id"] not in seen and v["kind"] in KIND_LABEL and v.get("publishedAt") and v["publishedAt"][:10] >= since.date().isoformat()]
    subject = body = None
    sent = False
    if new:
        subject, body = render(d, sid, new, since)
        sent = send(d["email"], subject, body)
    with connect() as db:
        db.execute("INSERT INTO digest_seen (digest_id, video_id) SELECT %s, video_id FROM mentions WHERE search_id = %s ON CONFLICT DO NOTHING",
                   (d["id"], sid))
        run = db.execute("INSERT INTO digest_runs (digest_id, search_id, new_count, sent, subject, html)"
                         " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id", (d["id"], sid, len(new), sent, subject, body)).fetchone()
    return {"digest": d["id"], "run": run["id"], "searchId": sid, "new": len(new), "sent": sent, "matched": len(results)}


def due(digest_id=None, force=False):
    with connect() as db:
        if digest_id:
            return db.execute("SELECT * FROM digests WHERE id = %s", (digest_id,)).fetchall()
        where = "" if force else " AND (last_run_at IS NULL OR last_run_at < now() - interval '%d days')" % (EVERY_DAYS - 1)
        return db.execute("SELECT * FROM digests WHERE confirmed_at IS NOT NULL AND paused_at IS NULL" + where + " ORDER BY id").fetchall()


def run_due(digest_id=None, force=False):
    """Run every digest that is due (or one by id). One Oriane search each; nothing when there are none."""
    out = []
    for d in due(digest_id, force):
        try:
            out.append(run_one(d))
        except Exception as e:  # one broken digest must not stop the others
            traceback.print_exc()
            out.append({"digest": d["id"], "error": str(e)})
    return {"ran": len(out), "results": out}


def run_route(handler, body):
    """POST /api/digests/run with the operator token; {"id": n} runs one digest now, {"force": true} runs all."""
    token = os.environ.get("DIGEST_RUN_TOKEN")
    if not token:
        raise ApiError(404, "Not found.")
    if not secrets.compare_digest((handler.headers.get("Authorization") or "").removeprefix("Bearer ").strip(), token):
        raise ApiError(401, "Bad run token.")
    digest_id = body.get("id")
    if body.get("resend") and isinstance(digest_id, int):
        return resend_last(digest_id)
    return run_due(digest_id if isinstance(digest_id, int) else None, bool(body.get("force")))


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


def scheduler(every_s=900):
    """Background loop (DIGEST_SCHEDULER=1): run due digests while the process is up.

    Off by default so a deployment spends no Oriane credits until the operator opts in; hosts that sleep when idle
    should hit POST /api/digests/run from an external cron instead.
    """
    if os.environ.get("DIGEST_SCHEDULER") != "1" or demo.active():
        return

    def loop():
        while True:
            time.sleep(every_s)
            try:
                run_due()
            except Exception:
                traceback.print_exc()
    threading.Thread(target=loop, name="digest-scheduler", daemon=True).start()


# ---------------------------------------------------------------- dispatch

def dispatch(handler):
    """Route one /api/digests* request."""
    url = urlparse(handler.path)
    path, method = url.path.removeprefix("/api/digests"), handler.command
    m = re.fullmatch(r"/([A-Za-z0-9_-]{16,32})(?:/(confirm|pause|resume|unsubscribe))?", path)
    if method == "GET":
        if m and not m.group(2):
            return manage(m.group(1))
        raise ApiError(404, "Not found.")
    if method != "POST":
        raise ApiError(405, "Method not allowed.")
    if not (handler.headers.get("Content-Type") or "").startswith("application/json"):
        raise ApiError(415, "Send application/json.")
    body = read_json(handler)
    if path == "":
        return subscribe(body)
    if path == "/run":
        return run_route(handler, body)
    if m and m.group(2):
        return manage(m.group(1), m.group(2))
    raise ApiError(404, "Not found.")


def read_json(handler):
    try:
        body = json.loads(handler.rfile.read(min(int(handler.headers.get("Content-Length") or 0), 10_000)) or b"{}")
    except ValueError:
        raise ApiError(400, "Invalid JSON.")
    if not isinstance(body, dict):
        raise ApiError(400, "Invalid request.")
    return body
