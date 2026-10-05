"""Public waitlist signup, referral queue, visit tracking, and operator stats."""
import html
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

from psycopg.types.json import Jsonb

import demo
import digest
from server import ApiError, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS waitlist (
  id          serial PRIMARY KEY,
  email       text NOT NULL UNIQUE,
  role        text NOT NULL,
  handle      text,
  platform    text,
  size        text,
  pay         text,
  pain        text,
  code        text NOT NULL UNIQUE,
  referred_by int REFERENCES waitlist ON DELETE SET NULL,
  source      text,
  utm         jsonb,
  welcomed_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS waitlist_referred ON waitlist (referred_by);
CREATE TABLE IF NOT EXISTS waitlist_visits (
  visitor    text PRIMARY KEY,
  source     text,
  ref        text,
  created_at timestamptz NOT NULL DEFAULT now()
);
"""

ROLES = ("creator", "brand", "manager")
SIZES = {role: ("s", "m", "l", "xl") for role in ROLES}
SIZE_LABELS = {
    "creator": {"s": "<10k", "m": "10k–100k", "l": "100k–1M", "xl": "1M+"},
    "brand": {"s": "none", "m": "<$1k", "l": "$1k–10k", "xl": "$10k+"},
    "manager": {"s": "1–5", "m": "6–20", "l": "21–50", "xl": "50+"},
}
PAY = ("0", "9", "29", "99")
PLATFORMS = ("tiktok", "instagram", "youtube", "other")
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "referrer")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VISITOR_RE = re.compile(r"^[A-Za-z0-9_-]{8,40}$")
REFERRAL_JUMP = 5
RATE_LOCK = threading.Lock()
RATE_LIMIT = {}
MAIL_LOCK = threading.Lock()


def _optional_text(value, field, limit):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "Enter a valid %s." % field)
    value = value.strip()
    return value[:limit] or None


def _enum(value, choices, field):
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in choices:
        raise ApiError(400, "Choose a valid %s." % field)
    return value


def _source(body, handler):
    source = _optional_text(body.get("source"), "source", 60)
    if source:
        return source
    utm = body.get("utm")
    if isinstance(utm, dict) and isinstance(utm.get("utm_source"), str) and utm["utm_source"].strip():
        return utm["utm_source"].strip()[:60]
    host = urlparse(handler.headers.get("Referer") or "").hostname
    return (host or "direct")[:60]


def _utm(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError(400, "Enter valid campaign details.")
    out = {}
    for key in UTM_KEYS:
        if key not in value:
            continue
        item = value[key]
        if not isinstance(item, str) or len(item) > 200:
            raise ApiError(400, "Campaign details must be short text.")
        out[key] = item
    return out or None


def _validated(body, handler):
    email = body.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        raise ApiError(400, "Enter a valid email address.")
    role = _enum(body.get("role"), ROLES, "role")
    if role is None:
        raise ApiError(400, "Choose a valid role.")
    platform = _enum(body.get("platform"), PLATFORMS, "platform")
    size = _enum(body.get("size"), SIZES[role], "size")
    pay = _enum(body.get("pay"), PAY, "pay")
    handle = _optional_text(body.get("handle"), "handle", 80)
    pain = _optional_text(body.get("pain"), "answer", 500)
    utm = _utm(body.get("utm"))
    source = _source(body, handler)
    ref = body.get("ref")
    if ref is not None and not isinstance(ref, str):
        ref = ""
    return {
        "email": email, "role": role, "handle": handle, "platform": platform,
        "size": size, "pay": pay, "pain": pain, "utm": utm, "source": source,
        "ref": ref or "",
    }


def _counts(db):
    return db.execute("SELECT count(*) AS total FROM waitlist").fetchone()["total"]


def _queue(db, waitlist_id):
    row = db.execute(
        "WITH referral_counts AS ("
        " SELECT referred_by AS id, count(*)::int AS referrals FROM waitlist WHERE referred_by IS NOT NULL GROUP BY referred_by"
        "), ranked AS ("
        " SELECT w.id, row_number() OVER (ORDER BY w.created_at, w.id)::bigint AS ordering,"
        "        coalesce(r.referrals, 0)::int AS referrals"
        " FROM waitlist w LEFT JOIN referral_counts r ON r.id = w.id"
        "), scored AS ("
        " SELECT id, referrals, ordering - %s * referrals AS score FROM ranked"
        "), target AS (SELECT score, id, referrals FROM scored WHERE id = %s)"
        " SELECT 1 + (SELECT count(*) FROM scored s, target t WHERE (s.score, -s.referrals, s.id) < (t.score, -t.referrals, t.id)) AS position,"
        "        (SELECT count(*) FROM waitlist) AS total, target.referrals"
        " FROM target",
        (REFERRAL_JUMP, waitlist_id),
    ).fetchone()
    return dict(row)


def _response(db, row, already_joined=False):
    return {
        "code": row["code"],
        **_queue(db, row["id"]),
        "role": row["role"],
        "alreadyJoined": already_joined,
        "jump": REFERRAL_JUMP,
    }


def _update_existing(db, row, data):
    updated = db.execute(
        "UPDATE waitlist SET role = %s, handle = coalesce(%s, handle), platform = coalesce(%s, platform),"
        " size = coalesce(%s, size), pay = coalesce(%s, pay), pain = coalesce(%s, pain)"
        " WHERE id = %s RETURNING *",
        (data["role"], data["handle"], data["platform"], data["size"], data["pay"], data["pain"], row["id"]),
    ).fetchone()
    return _response(db, updated, True)


def _client_ip(handler):
    forwarded = handler.headers.get("X-Forwarded-For", "")
    return forwarded.split(",", 1)[0].strip() or handler.client_address[0]


def _rate_allowed(ip, now):
    recent = [stamp for stamp in RATE_LIMIT.get(ip, []) if now - stamp < 3600]
    RATE_LIMIT[ip] = recent
    return len(recent) < 8


def join(body, handler):
    if body.get("website"):
        with connect() as db:
            total = _counts(db)
        role = body.get("role") if body.get("role") in ROLES else "creator"
        return {
            "code": "xxxxxxxx", "position": total + 1, "total": total, "referrals": 0,
            "role": role, "alreadyJoined": False, "jump": REFERRAL_JUMP,
        }
    data = _validated(body, handler)

    ip = _client_ip(handler)
    with RATE_LOCK:
        now = datetime.now(timezone.utc).timestamp()
        with connect() as db:
            existing = db.execute("SELECT * FROM waitlist WHERE email = %s", (data["email"],)).fetchone()
            if existing:
                return _update_existing(db, existing, data)
            if not _rate_allowed(ip, now):
                raise ApiError(429, "Too many sign-ups from this network, try again in an hour.")
            referrer = db.execute("SELECT id FROM waitlist WHERE code = %s", (data["ref"],)).fetchone() if data["ref"] else None
            referred_by = referrer["id"] if referrer else None
            while True:
                code = secrets.token_urlsafe(6)
                row = db.execute(
                    "INSERT INTO waitlist (email, role, handle, platform, size, pay, pain, code, referred_by, source, utm)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                    " ON CONFLICT DO NOTHING RETURNING *",
                    (data["email"], data["role"], data["handle"], data["platform"], data["size"], data["pay"], data["pain"],
                     code, referred_by, data["source"], Jsonb(data["utm"]) if data["utm"] else None),
                ).fetchone()
                if row:
                    break
                existing = db.execute("SELECT * FROM waitlist WHERE email = %s", (data["email"],)).fetchone()
                if existing:
                    return _update_existing(db, existing, data)
            RATE_LIMIT.setdefault(ip, []).append(now)
            response = _response(db, row)
    if digest.mail_configured() and not demo.active():
        threading.Thread(target=_welcome, args=(row["id"], data["email"], row["code"], response["position"]),
                         daemon=True).start()
    return response


def _welcome(waitlist_id, email, code, position):
    try:
        if demo.active() or not digest.mail_configured():
            return
        with MAIL_LOCK:
            cap = int(os.environ.get("WAITLIST_MAIL_DAILY", "80"))
            if cap <= 0:
                return
            with connect() as db:
                sent_today = db.execute(
                    "SELECT count(*) AS n FROM waitlist"
                    " WHERE welcomed_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
                ).fetchone()["n"]
            if sent_today >= cap:
                return
            share_link = "%s/waitlist?ref=%s" % (digest.app_url(), code)
            subject = "You're #%d on the Receipts waitlist" % position
            safe_link = html.escape(share_link, quote=True)
            body = (
                '<div style="max-width:560px;margin:32px auto;padding:24px;color:#17211b;'
                'font:16px/1.6 Arial,sans-serif;background:#f8faf8">'
                "<p>You're <strong>#%d</strong> on the Receipts waitlist.</p>"
                '<p><a style="color:#256348" href="%s">Share your referral link</a> — each friend who joins moves you up 5 spots.</p>'
                "<p>— Yasir, Unprompted</p></div>"
            ) % (position, safe_link)
            if digest.send(email, subject, body):
                with connect() as db:
                    db.execute("UPDATE waitlist SET welcomed_at = now() WHERE id = %s", (waitlist_id,))
    except Exception:
        return


def _stats():
    with connect() as db:
        total = _counts(db)
        by_role = {role: 0 for role in ROLES}
        by_role.update({row["role"]: row["n"] for row in db.execute(
            "SELECT role, count(*)::int AS n FROM waitlist GROUP BY role").fetchall()})
        today = db.execute(
            "SELECT count(*) AS n FROM waitlist WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
        ).fetchone()["n"]
    return {"total": total, "byRole": by_role, "today": today}


def _admin_report():
    with connect() as db:
        totals = db.execute(
            "SELECT (SELECT count(*) FROM waitlist) AS total,"
            " (SELECT count(*) FROM waitlist WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')) AS today,"
            " (SELECT count(*) FROM waitlist WHERE created_at >= now() - interval '7 days') AS week,"
            " (SELECT count(*) FROM waitlist_visits) AS visitors,"
            " (SELECT count(*) FROM waitlist WHERE pay IN ('9', '29', '99')) AS would_pay,"
            " (SELECT count(*) FROM waitlist WHERE referred_by IS NOT NULL) AS referred"
        ).fetchone()
        roles = {role: 0 for role in ROLES}
        roles.update({r["role"]: r["n"] for r in db.execute(
            "SELECT role, count(*)::int AS n FROM waitlist GROUP BY role").fetchall()})
        by_pay = {role: {pay: 0 for pay in PAY} for role in ROLES}
        for r in db.execute("SELECT role, pay, count(*)::int AS n FROM waitlist WHERE pay IS NOT NULL GROUP BY role, pay").fetchall():
            if r["role"] in by_pay and r["pay"] in by_pay[r["role"]]:
                by_pay[r["role"]][r["pay"]] = r["n"]
        by_size = {role: {size: 0 for size in SIZES[role]} for role in ROLES}
        for r in db.execute("SELECT role, size, count(*)::int AS n FROM waitlist WHERE size IS NOT NULL GROUP BY role, size").fetchall():
            if r["role"] in by_size and r["size"] in by_size[r["role"]]:
                by_size[r["role"]][r["size"]] = r["n"]
        by_source = db.execute(
            "WITH grouped AS ("
            " SELECT coalesce(nullif(source, ''), 'direct') AS source, count(*)::int AS signups FROM waitlist GROUP BY 1"
            "), visits AS ("
            " SELECT coalesce(nullif(source, ''), 'direct') AS source, count(*)::int AS visits"
            " FROM waitlist_visits GROUP BY 1"
            ") SELECT coalesce(g.source, v.source) AS source, coalesce(v.visits, 0)::int AS visits,"
            " coalesce(g.signups, 0)::int AS signups FROM grouped g FULL OUTER JOIN visits v USING (source)"
            " ORDER BY signups DESC, visits DESC, source LIMIT 20"
        ).fetchall()
        daily = db.execute(
            "WITH days AS (SELECT generate_series((now() AT TIME ZONE 'UTC')::date - 29,"
            " (now() AT TIME ZONE 'UTC')::date, interval '1 day')::date AS day),"
            " visits AS (SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*)::int AS n"
            " FROM waitlist_visits GROUP BY 1),"
            " signups AS (SELECT (created_at AT TIME ZONE 'UTC')::date AS day, count(*)::int AS n"
            " FROM waitlist GROUP BY 1)"
            " SELECT days.day, coalesce(visits.n, 0)::int AS visits, coalesce(signups.n, 0)::int AS signups"
            " FROM days LEFT JOIN visits USING (day) LEFT JOIN signups USING (day) ORDER BY days.day"
        ).fetchall()
        top_referrers = db.execute(
            "SELECT w.email, w.handle, w.code, count(r.id)::int AS n FROM waitlist w"
            " JOIN waitlist r ON r.referred_by = w.id GROUP BY w.id"
            " ORDER BY n DESC, w.created_at, w.id LIMIT 10"
        ).fetchall()
        entries = db.execute(
            "WITH referral_counts AS (SELECT referred_by AS id, count(*)::int AS referrals"
            " FROM waitlist WHERE referred_by IS NOT NULL GROUP BY referred_by),"
            " ranked AS (SELECT w.*, row_number() OVER (ORDER BY w.created_at, w.id)::bigint AS ordering,"
            " coalesce(rc.referrals, 0)::int AS referrals FROM waitlist w"
            " LEFT JOIN referral_counts rc ON rc.id = w.id),"
            " scored AS (SELECT *, ordering - %s * referrals AS score FROM ranked)"
            " SELECT s.id, s.email, s.role, s.handle, s.platform, s.size, s.pay, s.pain, s.source, s.code,"
            " s.referrals, (SELECT 1 + count(*) FROM scored other"
            " WHERE (other.score, -other.referrals, other.id) < (s.score, -s.referrals, s.id)) AS position,"
            " referrer.code AS \"referredBy\", s.created_at AS \"createdAt\""
            " FROM scored s LEFT JOIN waitlist referrer ON referrer.id = s.referred_by"
            " ORDER BY s.created_at DESC, s.id DESC LIMIT 1000",
            (REFERRAL_JUMP,),
        ).fetchall()
        by_role = roles
    return {
        "total": totals["total"], "today": totals["today"], "week": totals["week"], "visitors": totals["visitors"],
        "conversion": totals["total"] / totals["visitors"] if totals["visitors"] else None,
        "wouldPay": totals["would_pay"], "byRole": by_role, "byPay": by_pay, "bySize": by_size,
        "bySource": [dict(r) for r in by_source],
        "daily": [{"day": r["day"].isoformat(), "visits": r["visits"], "signups": r["signups"]} for r in daily],
        "referrals": {"total": totals["referred"], "topReferrers": [dict(r) for r in top_referrers]},
        "labels": SIZE_LABELS, "entries": [dict(r) for r in entries],
    }


def admin_route(handler):
    digest.require_operator(handler)
    if handler.command != "GET":
        raise ApiError(405, "Method not allowed.")
    return _admin_report()


def dispatch(handler):
    path = urlparse(handler.path).path
    if handler.command == "GET":
        if path == "/api/waitlist/stats":
            return _stats()
        match = re.fullmatch(r"/api/waitlist/([^/]+)", path)
        if match:
            with connect() as db:
                row = db.execute("SELECT * FROM waitlist WHERE code = %s", (match.group(1),)).fetchone()
                if not row:
                    raise ApiError(404, "Waitlist signup not found.")
                return _response(db, row)
        raise ApiError(404, "Not found.")
    if handler.command != "POST":
        raise ApiError(405, "Method not allowed.")
    if not (handler.headers.get("Content-Type") or "").startswith("application/json"):
        raise ApiError(415, "Send application/json.")
    body = digest.read_json(handler)
    if path == "/api/waitlist":
        return join(body, handler)
    if path == "/api/waitlist/visit":
        visitor = body.get("visitor")
        if not isinstance(visitor, str) or not VISITOR_RE.fullmatch(visitor):
            raise ApiError(400, "Enter a valid visitor ID.")
        source = _optional_text(body.get("source"), "source", 60)
        if not source:
            source = _source(body, handler)
        ref = _optional_text(body.get("ref"), "referral code", 80)
        with connect() as db:
            db.execute(
                "INSERT INTO waitlist_visits (visitor, source, ref) VALUES (%s, %s, %s) ON CONFLICT (visitor) DO NOTHING",
                (visitor, source, ref),
            )
        return {"ok": True}
    raise ApiError(404, "Not found.")
