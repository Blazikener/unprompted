"""Receipts: the creator-side product on top of Unprompted's transcript index.

A creator pastes their handle. We find the brands they already say, tag or feature on camera, split unpaid
mentions from disclosed ads, and hand them evidence (quote, timestamp, frame, views, link) they can pitch
with. Pro unlocks every brand, a "who's paying creators for this right now" check, and a pitch draft.

Routes live under /api/creators/ and are dispatched from server.Handler. Accounts are email + password
(scrypt), sessions are an HttpOnly cookie, billing is Stripe Checkout + webhooks (optional: without keys
the app runs with billing disabled and, if DEV_PLAN_SWITCH=1, a local plan toggle).
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import statistics
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from http.cookies import SimpleCookie
from urllib import error, request
from urllib.parse import unquote, urlencode, urlparse

from psycopg.types.json import Jsonb

import demo
import server
from brands import catalog
from server import (
    DISCLOSURE_PHRASES, DISCLOSURE_TAGS, HANDLE_RE, VIDEO_UPSERT, ApiError, connect, find_quote, post_url,
    profile_url, text_quote, video_row,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id                  serial PRIMARY KEY,
  email               text UNIQUE NOT NULL,
  password            text NOT NULL,            -- scrypt$salt$hash
  plan                text NOT NULL DEFAULT 'free',
  stripe_customer     text,
  stripe_subscription text,
  created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS sessions (
  token      text PRIMARY KEY,                  -- sha256 of the cookie value
  user_id    int NOT NULL REFERENCES users ON DELETE CASCADE,
  expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
  id         serial PRIMARY KEY,
  user_id    int NOT NULL REFERENCES users ON DELETE CASCADE,
  platform   text NOT NULL,
  handle     text NOT NULL,
  source     text NOT NULL,                     -- live | demo
  profile    jsonb NOT NULL,
  brands     jsonb NOT NULL,                    -- aggregated brand rows with receipts
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS scans_user ON scans (user_id, created_at DESC);
CREATE TABLE IF NOT EXISTS brand_activity (
  brand      text NOT NULL,
  platform   text NOT NULL,
  source     text NOT NULL,
  result     jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (brand, platform, source)
);
CREATE TABLE IF NOT EXISTS events (                -- funnel: scan, paywall, checkout_started, subscribed, ...
  id         serial PRIMARY KEY,
  user_id    int,
  name       text NOT NULL,
  data       jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
"""

PLATFORM_NAMES = {"tiktok": "TikTok", "instagram": "Instagram"}
PRO_PRICE_USD = 29
PLANS = {
    "free": {"scansPerWeek": 1, "brands": 3, "activity": False, "pitch": False},
    "pro": {"scansPerWeek": 50, "brands": None, "activity": True, "pitch": True},
}
SESSION_DAYS = 30
COOKIE = "receipts_session"
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
ACTIVITY_DAYS = 90
STRIPE_API = "https://api.stripe.com/v1/"



def brand_re(terms):
    """Like server.mention_re, but the term must end at a word boundary (optionally plural): a 300-brand
    dictionary can't afford prefix matches ("du" in "Dubai")."""
    return re.compile("|".join("(?:%s)(?:['’]?s)?(?![^\\W_])" % server.term_pattern(t) for t in terms), re.IGNORECASE)


CATALOG = catalog()
for _b in CATALOG:
    _b["rx"] = brand_re(_b["terms"])


class Reply:
    """Route result with a status and extra headers (Set-Cookie), for server.Handler.api."""
    def __init__(self, body, status=200, headers=()):
        self.body, self.status, self.headers = body, status, list(headers)


# ---------------------------------------------------------------- detection

def brand_mentions(raw):
    """Every catalog brand this video mentions: (entry, kind, quote).

    kind: sponsored (disclosed video that tags the brand, or co-authored with it), tagged (caption, hashtag or
    @mention), spoken (only said on camera). Ambiguous names (Apple, Target) need a context word when spoken.
    """
    caption = " ".join([raw.get("caption") or "", *(raw.get("hashtags") or [])])
    tags = {t[1:] for t in re.findall(r"#[^\W_]+", caption.lower())}
    disclosed = bool(tags & DISCLOSURE_TAGS) or any(p in caption.lower() for p in DISCLOSURE_PHRASES)
    mentioned = {m["profileHandle"].lower() for m in raw.get("mentions") or []}
    co_authors = {c["profileHandle"].lower() for c in raw.get("coAuthors") or []}
    thumb = raw.get("thumbnailMediaUrl")
    for b in CATALOG:
        handles = set(b["handles"])
        in_handles = bool(handles & (mentioned | co_authors))
        in_caption = bool(b["rx"].search(caption))
        q = find_quote(raw, b["rx"])
        if not (in_handles or in_caption or q):
            continue
        if q and b["context"] and not (in_caption or in_handles) and not any(w in q["text"].lower() for w in b["context"]):
            continue
        if handles & co_authors or (disclosed and (in_caption or in_handles)):
            kind = "sponsored"
        elif in_caption or in_handles:
            kind = "tagged"
        else:
            kind = "spoken"
        if not q:
            q = text_quote(caption, b["rx"], thumb) if in_caption else {
                "start": None, "text": "@" + next(iter(handles & (mentioned | co_authors))), "hit": None, "core": None, "frame": thumb}
        yield b, kind, q


def aggregate(videos):
    """Brand rows for one creator, best pitch first."""
    rows = {}
    for raw in videos:
        views = raw.get("viewsCount") or 0
        for b, kind, q in brand_mentions(raw):
            row = rows.setdefault(b["name"], {"brand": b["name"], "category": b["category"], "spoken": 0, "tagged": 0, "sponsored": 0,
                                              "organicViews": 0, "receipts": []})
            row[kind] += 1
            if kind != "sponsored":
                row["organicViews"] += views
            row["receipts"].append({"videoId": raw["id"], "kind": kind, "views": views, "publishedAt": raw.get("publishedAt"),
                                    "url": post_url(raw), "quote": q, "caption": raw.get("caption") or ""})
    out = []
    for row in rows.values():
        row["organic"] = row["spoken"] + row["tagged"]
        row["receipts"].sort(key=lambda r: ({"spoken": 0, "tagged": 1, "sponsored": 2}[r["kind"]], -r["views"]))
        row["lastMention"] = max((r["publishedAt"] or "" for r in row["receipts"]), default=None)
        row["status"] = "unpaid" if row["organic"] and not row["sponsored"] else "past-sponsor" if row["organic"] else "sponsored-only"
        row["score"] = server.score(row["organic"], row["spoken"], row["organicViews"], None)["total"]
        out.append(row)
    out.sort(key=lambda r: (r["status"] == "sponsored-only", -(r["spoken"] * 2 + r["tagged"]), -r["organicViews"]))
    return out


def profile(platform, handle, videos):
    views = [v.get("viewsCount") or 0 for v in videos]
    ers = [v["engagementRatePerViews"] for v in videos if v.get("engagementRatePerViews") is not None]
    dates = sorted(v["publishedAt"] for v in videos if v.get("publishedAt"))
    latest = max(videos, key=lambda v: v.get("publishedAt") or "")
    return {"platform": platform, "handle": handle, "url": profile_url(platform, handle), "name": latest.get("profileDisplayName"),
            "pfp": latest.get("profilePictureUrl"), "followers": max(v.get("profileFollowersCount") or 0 for v in videos),
            "verified": any(v.get("profileVerified") for v in videos), "videos": len(videos),
            "medianViews": int(statistics.median(views)), "medianEr": round(statistics.median(ers), 2) if ers else None,
            "window": [dates[0], dates[-1]] if dates else None}


def rate_card(median_views):
    """Rule-of-thumb ask for one dedicated video, from median views. ponytail: replace with observed deal data.

    Creator communities quote roughly $10-$25 per 1,000 views for a dedicated TikTok/Reel; usage rights add ~25%.
    """
    low = max(150, round(median_views * 0.010 / 50) * 50)
    high = max(low + 100, round(median_views * 0.025 / 50) * 50)
    return {"currency": "USD", "low": low, "high": high, "usage": round(high * 0.25 / 50) * 50,
            "basis": "%s median views x $10-$25 CPM" % fmt_num(median_views)}


def fmt_num(n):
    n = n or 0
    if n >= 1_000_000:
        return ("%.1fM" % (n / 1_000_000)).replace(".0M", "M")
    if n >= 1_000:
        return ("%.1fK" % (n / 1_000)).replace(".0K", "K")
    return str(int(n))


def fmt_ts(seconds):
    return None if seconds is None else "%d:%02d" % divmod(int(seconds), 60)


def draft_pitch(prof, row, activity, rate):
    """Short cold pitch that leads with the creator's own unpaid mention as proof."""
    best = row["receipts"][0]
    q = best["quote"] or {}
    said = (q.get("core") or q.get("text") or "").strip()
    if len(said) > 160:
        a = max(0, (q.get("hit") or [0])[0] - 60)
        said = ("..." if a else "") + said[a:a + 160].strip() + "..."
    when = " at %s" % fmt_ts(q.get("start")) if q.get("start") is not None else ""
    unpaid = row["organic"]
    views = fmt_num(row["organicViews"])
    handle_line = "@%s on %s, %s followers, %s median views" % (prof["handle"], PLATFORM_NAMES[prof["platform"]],
                                                                fmt_num(prof["followers"]), fmt_num(prof["medianViews"]))
    subject = "Already a %s fan on camera: %s unpaid %s, %s views" % (row["brand"], unpaid, "mention" if unpaid == 1 else "mentions", views)
    lines = [
        "Hi %s team," % row["brand"],
        "",
        "I've talked about %s on my channel %s %s without being paid, and those videos have %s views so far." % (
            row["brand"], unpaid, "time" if unpaid == 1 else "times", views),
        "Here's one%s: \"%s\" (%s)" % (when, said, best["url"]),
        "",
        "%s. My audience already hears me recommend you, so a paid post would read like more of the same, not an ad." % handle_line,
    ]
    if activity and activity.get("sponsored"):
        lines += ["", "I noticed you've run %s disclosed creator posts in the last %s days, so I'm guessing the programme is live." % (
            activity["sponsored"], activity["windowDays"])]
    lines += ["",
              "Proposal: one dedicated %s in the same style as the clip above, plus 30 days of paid usage. Rate: $%s-$%s "
              "(usage +$%s)." % ("TikTok" if prof["platform"] == "tiktok" else "Reel", rate["low"], rate["high"], rate["usage"]),
              "", "Who's the right person for creator partnerships? Happy to send a one-page media kit.", "",
              "Thanks,", prof.get("name") or "@" + prof["handle"]]
    return {"subject": subject, "body": "\n".join(lines)}


# ---------------------------------------------------------------- data

def fetch_videos(platform, handle):
    if demo.active():
        return demo.creator_videos(platform, handle), "demo"
    filters = {"profileHandle": {"exactMatch": {"values": [handle]}}, "platform": {"includes": [platform]}}
    videos = server.oriane(filters, limit=100, sort="publishedAt")["data"]["results"]
    if not videos:
        raise ApiError(404, "Oriane has no videos indexed for @%s on %s yet." % (handle, platform))
    return videos, "live"


def brand_entry(name):
    b = next((b for b in CATALOG if b["name"].lower() == name.lower()), None)
    if not b:
        raise ApiError(404, "Unknown brand.")
    return b


def sponsor_activity(b, platform):
    """Disclosed creator posts about a brand in the last ACTIVITY_DAYS, cached for a day."""
    source = "demo" if demo.active() else "live"
    with connect() as db:
        cached = db.execute("SELECT result FROM brand_activity WHERE brand = %s AND platform = %s AND source = %s"
                            " AND created_at > now() - interval '1 day'", (b["name"], platform, source)).fetchone()
    if cached:
        return cached["result"]
    if source == "demo":
        result = demo.activity(b["name"], platform)
    else:
        brand, variants, filters, _ = server.parse({"brand": b["name"], "variants": ",".join(b["terms"][1:6]), "platform": platform, "days": 90})
        data = server.oriane(filters, limit=100, sort="publishedAt")
        results = data["data"]["results"]
        paid = [r for r in results if server.classify(r, brand, variants)[0] == "sponsored"]
        creators = {r["profileHandle"]: r.get("profileFollowersCount") or 0 for r in paid}
        tiers = Counter(tier(f) for f in creators.values())
        examples = [{"handle": r["profileHandle"], "followers": r.get("profileFollowersCount") or 0, "views": r.get("viewsCount") or 0,
                     "publishedAt": r.get("publishedAt"), "url": post_url(r), "text": (r.get("caption") or "")[:140]}
                    for r in sorted(paid, key=lambda r: -(r.get("viewsCount") or 0))[:3]]
        result = {"windowDays": ACTIVITY_DAYS, "videosSeen": len(results), "sponsored": len(paid), "creators": len(creators),
                  "tiers": {t: tiers.get(t, 0) for t in ("nano", "micro", "mid", "macro")},
                  "lastPaid": max((r.get("publishedAt") or "" for r in paid), default=None) or None, "examples": examples}
    result = {**result, "brand": b["name"], "platform": platform, "source": source, "verdict": verdict(result), "checkedAt": now_iso()}
    with connect() as db:
        db.execute("INSERT INTO brand_activity (brand, platform, source, result) VALUES (%s, %s, %s, %s)"
                   " ON CONFLICT (brand, platform, source) DO UPDATE SET result = EXCLUDED.result, created_at = now()",
                   (b["name"], platform, source, Jsonb(result)))
    return result


def tier(followers):
    return "nano" if followers < 10_000 else "micro" if followers < 100_000 else "mid" if followers < 500_000 else "macro"


def verdict(a):
    if not a["sponsored"]:
        return "quiet"
    recent = a["lastPaid"] and a["lastPaid"][:10] >= (date.today() - timedelta(days=30)).isoformat()
    return "active" if a["sponsored"] >= 5 and recent else "some"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def track(user_id, name, data=None):
    with connect() as db:
        db.execute("INSERT INTO events (user_id, name, data) VALUES (%s, %s, %s)", (user_id, name, Jsonb(data or {})))


# ---------------------------------------------------------------- accounts

def hash_password(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (salt, h.hex())


def check_password(pw, stored):
    _, salt, _ = stored.split("$")
    return hmac.compare_digest(hash_password(pw, salt), stored)


def public_user(u):
    return {"id": u["id"], "email": u["email"], "plan": u["plan"], "limits": PLANS[u["plan"]], "billing": bool(u.get("stripe_customer"))}


def start_session(db, user_id):
    token = secrets.token_urlsafe(32)
    db.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, now() + interval '%s days')",
               (hashlib.sha256(token.encode()).hexdigest(), user_id, SESSION_DAYS))
    return ("Set-Cookie", "%s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=%d" % (COOKIE, token, SESSION_DAYS * 86400))


def clear_cookie():
    return ("Set-Cookie", "%s=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" % COOKIE)


def current_user(headers):
    cookie = SimpleCookie(headers.get("Cookie") or "")
    if COOKIE not in cookie:
        return None
    token = hashlib.sha256(cookie[COOKIE].value.encode()).hexdigest()
    with connect() as db:
        return db.execute("SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = %s AND s.expires_at > now()",
                          (token,)).fetchone()


def require_user(headers):
    u = current_user(headers)
    if not u:
        raise ApiError(401, "Sign in to continue.")
    return u


def credentials(body):
    email, pw = str(body.get("email", "")).strip().lower(), str(body.get("password", ""))
    if not EMAIL_RE.fullmatch(email) or len(email) > 254:
        raise ApiError(400, "Enter a valid email.")
    if len(pw) < 8:
        raise ApiError(400, "Password needs at least 8 characters.")
    return email, pw


def signup(body):
    email, pw = credentials(body)
    with connect() as db:
        if db.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone():
            raise ApiError(409, "That email already has an account. Sign in instead.")
        u = db.execute("INSERT INTO users (email, password) VALUES (%s, %s) RETURNING *", (email, hash_password(pw))).fetchone()
        cookie = start_session(db, u["id"])
    track(u["id"], "signup")
    return Reply({"user": public_user(u)}, 201, [cookie])


def login(body):
    email, pw = credentials(body)
    with connect() as db:
        u = db.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        if not u or not check_password(pw, u["password"]):
            raise ApiError(401, "Wrong email or password.")
        cookie = start_session(db, u["id"])
    return Reply({"user": public_user(u)}, 200, [cookie])


def logout(headers):
    cookie = SimpleCookie(headers.get("Cookie") or "")
    if COOKIE in cookie:
        with connect() as db:
            db.execute("DELETE FROM sessions WHERE token = %s", (hashlib.sha256(cookie[COOKIE].value.encode()).hexdigest(),))
    return Reply({"ok": True}, 200, [clear_cookie()])


# ---------------------------------------------------------------- scans

def gate(rows, plan):
    """Free plan: full receipts for the top N brands, the rest locked (counts visible, name hidden)."""
    n = PLANS[plan]["brands"]
    if n is None:
        return rows
    out = []
    for i, r in enumerate(rows):
        if i < n:
            out.append(r)
        else:
            out.append({"locked": True, "category": r["category"], "spoken": r["spoken"], "tagged": r["tagged"], "sponsored": r["sponsored"],
                        "organic": r["organic"], "organicViews": r["organicViews"], "status": r["status"], "score": r["score"]})
    return out


def scan_view(row, plan):
    brands = row["brands"]
    return {"id": row["id"], "platform": row["platform"], "handle": row["handle"], "source": row["source"], "profile": row["profile"],
            "createdAt": row["created_at"], "brands": gate(brands, plan), "totalBrands": len(brands),
            "unpaidBrands": sum(1 for b in brands if b["status"] == "unpaid"), "plan": plan,
            "rate": rate_card(row["profile"]["medianViews"])}


def run_scan(user, body):
    platform, handle = body.get("platform"), str(body.get("handle", "")).strip().lstrip("@").lower()
    if platform not in ("instagram", "tiktok") or not HANDLE_RE.fullmatch(handle):
        raise ApiError(400, "Enter a TikTok or Instagram handle.")
    with connect() as db:
        recent = db.execute("SELECT * FROM scans WHERE user_id = %s AND platform = %s AND handle = %s AND created_at > now() - interval '1 day'"
                            " ORDER BY id DESC LIMIT 1", (user["id"], platform, handle)).fetchone()
        if recent and not body.get("fresh"):
            return scan_view(recent, user["plan"])
        used = db.execute("SELECT count(*) AS n FROM scans WHERE user_id = %s AND created_at > now() - interval '7 days'",
                          (user["id"],)).fetchone()["n"]
    limit = PLANS[user["plan"]]["scansPerWeek"]
    if used >= limit:
        track(user["id"], "paywall", {"reason": "scan_quota", "plan": user["plan"]})
        raise ApiError(402, "You've used your %d free scan this week. Pro gets %d." % (limit, PLANS["pro"]["scansPerWeek"]) if user["plan"] == "free"
                       else "Scan limit reached (%d per week)." % limit)
    videos, source = fetch_videos(platform, handle)
    rows = aggregate(videos)
    prof = profile(platform, handle, videos)
    with connect() as db:
        with db.cursor() as cur:
            cur.executemany(VIDEO_UPSERT, [video_row(v) for v in videos])
        row = db.execute("INSERT INTO scans (user_id, platform, handle, source, profile, brands) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                         (user["id"], platform, handle, source, Jsonb(prof), Jsonb(rows))).fetchone()
    track(user["id"], "scan", {"platform": platform, "handle": handle, "brands": len(rows), "source": source, "videos": len(videos)})
    return scan_view(row, user["plan"])


def load_scan(user, sid):
    with connect() as db:
        row = db.execute("SELECT * FROM scans WHERE id = %s AND user_id = %s", (sid, user["id"])).fetchone()
    if not row:
        raise ApiError(404, "Scan not found.")
    return row


def list_scans(user):
    with connect() as db:
        return db.execute('SELECT id, platform, handle, source, created_at AS "createdAt", jsonb_array_length(brands) AS brands,'
                          " profile->>'name' AS name, profile->>'pfp' AS pfp FROM scans WHERE user_id = %s ORDER BY id DESC LIMIT 30",
                          (user["id"],)).fetchall()


def unlocked_brand(user, sid, name, feature):
    row = load_scan(user, sid)
    b = brand_entry(name)
    if not PLANS[user["plan"]][feature]:
        track(user["id"], "paywall", {"reason": feature, "brand": b["name"]})
        raise ApiError(402, "Pro unlocks %s." % ("the sponsor check" if feature == "activity" else "pitch drafts"))
    brand_row = next((r for r in row["brands"] if r["brand"] == b["name"]), None)
    if not brand_row:
        raise ApiError(404, "That brand isn't in this scan.")
    return row, b, brand_row


def activity_route(user, sid, name):
    row, b, _ = unlocked_brand(user, sid, name, "activity")
    result = sponsor_activity(b, row["platform"])
    track(user["id"], "activity", {"brand": b["name"], "verdict": result["verdict"]})
    return result


def pitch_route(user, sid, name):
    row, b, brand_row = unlocked_brand(user, sid, name, "pitch")
    with connect() as db:
        cached = db.execute("SELECT result FROM brand_activity WHERE brand = %s AND platform = %s AND created_at > now() - interval '1 day'",
                            (b["name"], row["platform"])).fetchone()
    pitch = draft_pitch(row["profile"], brand_row, cached["result"] if cached else None, rate_card(row["profile"]["medianViews"]))
    track(user["id"], "pitch", {"brand": b["name"]})
    return pitch


# ---------------------------------------------------------------- billing

def stripe_config():
    return {k: os.environ.get(k) for k in ("STRIPE_SECRET_KEY", "STRIPE_PRICE_PRO", "STRIPE_WEBHOOK_SECRET")}


def billing_enabled():
    c = stripe_config()
    return bool(c["STRIPE_SECRET_KEY"] and c["STRIPE_PRICE_PRO"])


def app_url():
    return os.environ.get("APP_URL", "http://127.0.0.1:%s" % os.environ.get("PORT", "8000")).rstrip("/")


def stripe(path, params):
    req = request.Request(STRIPE_API + path, data=urlencode(params).encode(), method="POST",
                          headers={"Authorization": "Bearer " + stripe_config()["STRIPE_SECRET_KEY"],
                                   "Content-Type": "application/x-www-form-urlencoded"})
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


def billing_config():
    return {"enabled": billing_enabled(), "price": PRO_PRICE_USD, "devSwitch": os.environ.get("DEV_PLAN_SWITCH") == "1",
            "plans": PLANS, "demo": demo.active()}


def checkout(user):
    if not billing_enabled():
        raise ApiError(503, "Billing isn't configured on this server yet.")
    if user["plan"] == "pro":
        raise ApiError(400, "You're already on Pro.")
    params = {"mode": "subscription", "line_items[0][price]": stripe_config()["STRIPE_PRICE_PRO"], "line_items[0][quantity]": 1,
              "success_url": app_url() + "/creators/?upgraded=1", "cancel_url": app_url() + "/creators/?upgraded=0",
              "client_reference_id": user["id"], "metadata[user_id]": user["id"], "allow_promotion_codes": "true"}
    if user["stripe_customer"]:
        params["customer"] = user["stripe_customer"]
    else:
        params["customer_email"] = user["email"]
    session = stripe("checkout/sessions", params)
    track(user["id"], "checkout_started")
    return {"url": session["url"]}


def portal(user):
    if not billing_enabled() or not user["stripe_customer"]:
        raise ApiError(400, "No billing account yet.")
    session = stripe("billing_portal/sessions", {"customer": user["stripe_customer"], "return_url": app_url() + "/creators/"})
    return {"url": session["url"]}


def verify_stripe_signature(payload, header, secret, tolerance=300):
    parts = dict(p.split("=", 1) for p in (header or "").split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, v) for k, v in [p.split("=", 1) for p in header.split(",") if "=" in p] if k == "v1")


def set_plan(db, where, args, plan, customer=None, subscription=None):
    db.execute("UPDATE users SET plan = %s, stripe_customer = coalesce(%s, stripe_customer),"
               " stripe_subscription = coalesce(%s, stripe_subscription) WHERE " + where, (plan, customer, subscription, *args))


def webhook(payload, headers):
    secret = stripe_config()["STRIPE_WEBHOOK_SECRET"]
    if not secret or not verify_stripe_signature(payload, headers.get("Stripe-Signature"), secret):
        raise ApiError(400, "Bad signature.")
    event = json.loads(payload)
    obj = event.get("data", {}).get("object", {})
    kind = event.get("type", "")
    with connect() as db:
        if kind == "checkout.session.completed" and obj.get("mode") == "subscription":
            uid = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
            if uid:
                set_plan(db, "id = %s", (int(uid),), "pro", obj.get("customer"), obj.get("subscription"))
                track(int(uid), "subscribed", {"subscription": obj.get("subscription")})
        elif kind in ("customer.subscription.updated", "customer.subscription.deleted"):
            active = kind.endswith("updated") and obj.get("status") in ("active", "trialing", "past_due")
            set_plan(db, "stripe_customer = %s", (obj.get("customer"),), "pro" if active else "free", None, obj.get("id"))
    return {"received": True}


def dev_switch(user, body):
    if os.environ.get("DEV_PLAN_SWITCH") != "1":
        raise ApiError(404, "Not found.")
    plan = body.get("plan")
    if plan not in PLANS:
        raise ApiError(400, "Unknown plan.")
    with connect() as db:
        set_plan(db, "id = %s", (user["id"],), plan)
    return {"user": public_user({**user, "plan": plan})}


# ---------------------------------------------------------------- dispatch

def read_json(handler, limit=10_000):
    try:
        return json.loads(handler.rfile.read(min(int(handler.headers.get("Content-Length") or 0), limit)) or b"{}")
    except ValueError:
        raise ApiError(400, "Invalid JSON.")


def dispatch(handler):
    """Route one /api/creators/* request. Returns a JSON-able body or a Reply."""
    url = urlparse(handler.path)
    path, method, headers = url.path.removeprefix("/api/creators"), handler.command, handler.headers
    if method == "POST" and path == "/billing/webhook":
        return webhook(handler.rfile.read(min(int(headers.get("Content-Length") or 0), 1_000_000)), headers)
    if method == "GET":
        if path == "/me":
            u = current_user(headers)
            return {"user": public_user(u) if u else None, "billing": billing_config()}
        if path == "/billing/config":
            return billing_config()
        if path == "/brands":
            return [{"name": b["name"], "category": b["category"]} for b in CATALOG]
        if path == "/scans":
            return list_scans(require_user(headers))
        m = re.fullmatch(r"/scans/(\d+)", path)
        if m:
            u = require_user(headers)
            return scan_view(load_scan(u, int(m.group(1))), u["plan"])
        raise ApiError(404, "Not found.")
    if method != "POST":
        raise ApiError(405, "Method not allowed.")
    # JSON bodies only: browsers can't send cross-site application/json without CORS, which we don't enable.
    if not (headers.get("Content-Type") or "").startswith("application/json"):
        raise ApiError(415, "Send application/json.")
    body = read_json(handler)
    if not isinstance(body, dict):
        raise ApiError(400, "Invalid request.")
    if path == "/signup":
        return signup(body)
    if path == "/login":
        return login(body)
    if path == "/logout":
        return logout(headers)
    u = require_user(headers)
    if path == "/scans":
        return run_scan(u, body)
    m = re.fullmatch(r"/scans/(\d+)/brands/([^/]+)/(activity|pitch)", path)
    if m:
        sid, name, feature = int(m.group(1)), unquote(m.group(2)), m.group(3)
        return activity_route(u, sid, name) if feature == "activity" else pitch_route(u, sid, name)
    if path == "/billing/checkout":
        return checkout(u)
    if path == "/billing/portal":
        return portal(u)
    if path == "/billing/dev":
        return dev_switch(u, body)
    if path == "/report":
        track(u["id"], "report", {"text": str(body.get("text", ""))[:500], "scan": body.get("scan")})
        return {"ok": True}
    raise ApiError(404, "Not found.")
