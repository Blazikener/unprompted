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
import tiktok_public
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
ALTER TABLE scans ADD COLUMN IF NOT EXISTS share_token text UNIQUE;  -- set once the owner publishes a receipt page
CREATE TABLE IF NOT EXISTS brand_activity (
  brand      text NOT NULL,
  platform   text NOT NULL,
  source     text NOT NULL,
  result     jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (brand, platform, source)
);
CREATE TABLE IF NOT EXISTS misses (                -- live handles Oriane had no videos for; saves provider credits on retries
  platform   text NOT NULL,
  handle     text NOT NULL,
  body       jsonb NOT NULL,                      -- {error, reason, suggestions}
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (platform, handle)
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
# "so excited to partner with [BFF and] ClassPass": a disclosure verb at most three words before the brand.
# "#ninjapartner", "#sephoraambassador": the disclosure tag carries the brand's name as a prefix
BRANDED_DISCLOSURE = re.compile(r"(.{2,}?)(partner|partnership|ambassador|sponsored|collab)")
SPOKEN_DISCLOSURE = re.compile(r"(?:partner(?:ed|ing)? with|partnership with|sponsored by|sponsoring|thanks to|ad for|gifted by|"
                               r"ambassador for|collab(?:oration)? with)\W+(?:[^\W_]+\W+){0,3}$", re.IGNORECASE)
PRO_PRICE_USD = 29
PLANS = {
    "free": {"scansPerWeek": 1, "brands": 3, "activity": False, "pitch": False,
             "brandSearchesPerWeek": 2, "checksPerWeek": 3},
    "pro": {"scansPerWeek": 50, "brands": None, "activity": True, "pitch": True,
            "brandSearchesPerWeek": 30, "checksPerWeek": 50},
    # Talent managers (rosters.py): Pro, plus a Monday report on up to 25 creators.
    "roster": {"scansPerWeek": 50, "brands": None, "activity": True, "pitch": True,
               "brandSearchesPerWeek": 30, "checksPerWeek": 50, "rosterCreators": 25},
    # The packaging test (packaging.py). Arm (b)'s free plan: every brand and pitch draft; the sponsor check stays off
    # (Oriane credits). Arm (c)'s Weekly leads, $9/month: the Monday email, plus what Pro unlocks in the app.
    "open": {"scansPerWeek": 1, "brands": None, "activity": False, "pitch": True,
             "brandSearchesPerWeek": 2, "checksPerWeek": 3},
    "leads": {"scansPerWeek": 3, "brands": None, "activity": True, "pitch": True,
              "brandSearchesPerWeek": 2, "checksPerWeek": 3},
}
PLAN_PRICE_ENV = {"pro": "STRIPE_PRICE_PRO", "roster": "STRIPE_PRICE_ROSTER", "leads": "STRIPE_PRICE_LEADS"}
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
    _b["rx_everyday"] = brand_re(_b["everyday"]) if _b["everyday"] else None   # Arabic spellings that are ordinary words
    _b["context"] = [server.fold(w) for w in _b["context"]]
    _b["everydayContext"] = [server.fold(w) for w in _b["everydayContext"]]


class Reply:
    """Route result with a status and extra headers (Set-Cookie), for server.Handler.api."""
    def __init__(self, body, status=200, headers=()):
        self.body, self.status, self.headers = body, status, list(headers)


# ---------------------------------------------------------------- detection

def brand_mentions(raw):
    """Every catalog brand this video mentions: (entry, kind, quote).

    kind: sponsored (disclosed video that tags the brand, co-authored with it, or a disclosure said in the same
    breath: "so excited to partner with X"), tagged (caption, hashtag or
    @mention), spoken (only said on camera). Ambiguous names (Apple, Target, Jumeirah) need a context word in the
    quote or caption unless the brand's own account is tagged; so does an Arabic spelling that is also an everyday
    word (طلبات, "orders") when nothing else matched.
    """
    caption = " ".join([raw.get("caption") or "", *(raw.get("hashtags") or [])])
    tags = {t[1:] for t in re.findall(r"#[^\W_]+", caption.lower())}
    branded = [m.group(1) for m in map(BRANDED_DISCLOSURE.fullmatch, tags) if m]
    disclosed = bool(tags & DISCLOSURE_TAGS or branded) or any(p in caption.lower() for p in DISCLOSURE_PHRASES)
    mentioned = {m["profileHandle"].lower() for m in raw.get("mentions") or []}
    co_authors = {c["profileHandle"].lower() for c in raw.get("coAuthors") or []}
    thumb = raw.get("thumbnailMediaUrl")
    for b in CATALOG:
        handles = set(b["handles"])
        in_handles = bool(handles & (mentioned | co_authors))
        rx = b["rx"]
        in_caption = bool(rx.search(caption))
        q = find_quote(raw, rx)
        if not (in_handles or in_caption or q) and b["rx_everyday"]:
            rx = b["rx_everyday"]
            in_caption, q = bool(rx.search(caption)), find_quote(raw, rx)
            hay = server.fold((q["text"] if q else "") + " " + (caption if in_caption else ""))
            if not any(w in hay for w in b["everydayContext"]):
                continue
        if not (in_handles or in_caption or q):
            continue
        if b["context"] and not in_handles:
            hay = server.fold((q["text"] if q else "") + " " + (caption if in_caption else ""))
            if not any(w in hay for w in b["context"]):
                continue
        own_tag = any(b["rx"].search(t) or t.startswith(b["name"].split()[0].lower()) for t in branded)
        if handles & co_authors or own_tag or (disclosed and (in_caption or in_handles)):
            kind = "sponsored"
        elif q and SPOKEN_DISCLOSURE.search(q["text"][max(0, q["hit"][0] - 80):q["hit"][0]]):
            kind = "sponsored"
        elif in_caption or in_handles:
            kind = "tagged"
        else:
            kind = "spoken"
        if not q:
            if in_caption:
                q = text_quote(caption, rx, thumb)
            else:
                tagged_as = "@" + next(iter(handles & (mentioned | co_authors)))
                plain = (raw.get("caption") or "").strip()[:200] or tagged_as
                q = {"start": None, "text": plain, "hit": None, "core": plain, "frame": thumb, "tag": tagged_as}
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
    spoken = best["kind"] == "spoken"
    handle_line = "@%s on %s, %s followers, %s median views" % (prof["handle"], PLATFORM_NAMES[prof["platform"]],
                                                                fmt_num(prof["followers"]), fmt_num(prof["medianViews"]))
    posts = "%s unpaid %s" % (unpaid, ("mention" if spoken else "post") + ("" if unpaid == 1 else "s"))
    subject = "Already %s %s: %s, %s views" % ("talking about" if spoken else "featuring", row["brand"], posts, views)
    if spoken:
        n, extra = row["spoken"], row["tagged"]
        also = " and tagged you in %s more %s" % (extra, "post" if extra == 1 else "posts") if extra else ""
        proof = "I've talked about %s on camera %s %s%s without being paid, and those videos have %s views so far." % (
            row["brand"], n, "time" if n == 1 else "times", also, views)
        one = "Here's one%s: \"%s\" (%s)" % (when, said, best["url"])
        why = "My audience already hears me recommend you, so a paid post would read like more of the same, not an ad."
    else:
        proof = "I've tagged or featured %s in %s of my posts without being paid, and those posts have %s views so far." % (
            row["brand"], unpaid, views)
        one = "Here's one: %s" % best["url"] if not said or said.startswith("@") else "Here's one: \"%s\" (%s)" % (said, best["url"])
        why = "My audience already sees me use you, so a paid post would read like more of the same, not an ad."
    lines = ["Hi %s team," % row["brand"], "", proof, one, "", "%s. %s" % (handle_line, why)]
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

HANDLE_URL_RE = re.compile(r"(?:https?://)?(?:www\.|m\.|vm\.)?(tiktok|instagram)\.com/@?([A-Za-z0-9._]+)", re.I)


def parse_handle(raw, platform=None):
    """Accept "@Name", "name", or a pasted profile/video URL; a URL also decides the platform."""
    raw = str(raw or "").strip()
    m = HANDLE_URL_RE.search(raw)
    if m:
        platform, raw = m.group(1).lower(), m.group(2)
    handle = raw.split("?")[0].strip().strip("/").lstrip("@").lower()
    return platform, handle


def not_indexed(platform, handle):
    """Why a live scan found nothing: profile known but not crawled, same name elsewhere, or close handles.

    One profile lookup (fuzzy, both platforms) answers all three; exact hits are picked out of it.
    """
    name = PLATFORM_NAMES[platform]
    base = "Oriane has no videos indexed for @%s on %s yet." % (handle, name)
    if platform == "instagram":
        base += " Instagram only works for creators in our index so far; if they're on TikTok too, scan that handle."
    try:
        found = server.oriane({"handle": {"includesFuzzy": {"values": [handle]}}, "followersCount": {"min": 1000}},
                              limit=8, sort="followersCount", index="profiles")["data"]
    except ApiError:
        return ApiError(404, base, reason="unknown", suggestions=[])
    same = [r for r in found if r["handle"] == handle and r["platform"] == platform]
    if same:
        msg = ("Oriane knows @%s on %s (%s followers) but hasn't indexed their videos yet, so there's nothing to scan today. "
               "Try again in a few days." % (handle, name, fmt_num(same[0]["followersCount"])))
        return ApiError(404, msg, reason="profile_only", suggestions=[])
    other = [r for r in found if r["handle"] == handle]
    close = [r for r in found if r["handle"] != handle and r["platform"] == platform]
    picks = [{"platform": r["platform"], "handle": r["handle"], "followers": r["followersCount"]} for r in other + close][:4]
    if other:
        base = "Oriane has @%s indexed on %s, not %s." % (handle, PLATFORM_NAMES[other[0]["platform"]], name)
    elif picks:
        base += " Did you mean one of these?"
    return ApiError(404, base, reason="other_platform" if other else "unknown", suggestions=picks)


MISS_TTL_DAYS = 7


def public_videos(platform, handle):
    """Read the creator straight from the platform when Oriane has nothing; TikTok only so far."""
    return tiktok_public.fetch(handle) if platform == "tiktok" else []


def fetch_videos(platform, handle, plan="free"):
    """Free TikTok scans read the public page first (no Oriane credits); Pro and Instagram go to Oriane first.

    Whichever source is second is the fallback, so a creator missing from one is still served by the other.
    """
    if demo.active():
        return demo.creator_videos(platform, handle), "demo"
    public_first = platform == "tiktok" and plan != "pro"
    if public_first:
        videos = public_videos(platform, handle)
        if videos:
            return videos, "public"
    with connect() as db:
        cached = db.execute("SELECT body FROM misses WHERE platform = %s AND handle = %s AND created_at > now() - interval '%s days'"
                            % ("%s", "%s", MISS_TTL_DAYS), (platform, handle)).fetchone()
    if cached:
        videos = [] if public_first else public_videos(platform, handle)
        if videos:
            return videos, "public"
        body = cached["body"]
        raise ApiError(404, body["error"], reason=body["reason"], suggestions=body["suggestions"], cached=True)
    filters = {"profileHandle": {"exactMatch": {"values": [handle]}}, "platform": {"includes": [platform]}}
    videos = server.oriane(filters, limit=100, sort="publishedAt")["data"]["results"]
    if videos:
        return videos, "live"
    videos = [] if public_first else public_videos(platform, handle)
    if videos:
        return videos, "public"
    miss = not_indexed(platform, handle)
    with connect() as db:
        db.execute("INSERT INTO misses (platform, handle, body) VALUES (%s, %s, %s)"
                   " ON CONFLICT (platform, handle) DO UPDATE SET body = EXCLUDED.body, created_at = now()",
                   (platform, handle, Jsonb({"error": str(miss), **miss.extra})))
    raise miss


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
        # Brand mentions alone are mostly organic, so ask for disclosed posts: hashtag disclosures, then caption phrases.
        seen = {}
        for extra in ({"hashtags": {"exactMatch": {"values": ["#" + t for t in sorted(DISCLOSURE_TAGS)]}}},
                      {"caption": {"operator": "or", "includesExactly": {"values": list(DISCLOSURE_PHRASES)}}}):
            for r in server.oriane({**filters, **extra}, limit=100, sort="publishedAt")["data"]["results"]:
                seen.setdefault(r["id"], r)
        results = list(seen.values())
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


def plan_of(user):
    """The plan whose limits apply (packaging.py: arm (b)'s free plan is "open")."""
    import packaging
    return packaging.plan_of(user)


def public_user(u):
    import packaging
    return {"id": u["id"], "email": u["email"], "plan": u["plan"], "limits": PLANS[plan_of(u)], "billing": bool(u.get("stripe_customer")),
            "arm": packaging.user_arm(u)}


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


def brand_usage(user):
    limits = PLANS[user["plan"]]
    with connect() as db:
        searches = db.execute("SELECT count(*) AS n FROM searches WHERE user_id = %s AND created_at > now() - interval '7 days'",
                              (user["id"],)).fetchone()["n"]
        checks = db.execute("SELECT count(*) AS n FROM checks WHERE user_id = %s AND created_at > now() - interval '7 days'",
                            (user["id"],)).fetchone()["n"]
    return {
        "searches": {"used": searches, "limit": limits["brandSearchesPerWeek"]},
        "checks": {"used": checks, "limit": limits["checksPerWeek"]},
    }


def credentials(body):
    email, pw = str(body.get("email", "")).strip().lower(), str(body.get("password", ""))
    if not EMAIL_RE.fullmatch(email) or len(email) > 254:
        raise ApiError(400, "Enter a valid email.")
    if len(pw) < 8:
        raise ApiError(400, "Password needs at least 8 characters.")
    return email, pw


def signup(body, headers=None):
    import packaging
    email, pw = credentials(body)
    arm, forced = packaging.signup_arm(headers or {})       # the offer this browser was shown on /creators/
    with connect() as db:
        if db.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone():
            raise ApiError(409, "That email already has an account. Sign in instead.")
        u = db.execute("INSERT INTO users (email, password, arm, arm_forced) VALUES (%s, %s, %s, %s) RETURNING *",
                       (email, hash_password(pw), arm, forced)).fetchone()
        cookie = start_session(db, u["id"])
    if headers:
        packaging.link_visitor(headers, u["id"])
    track(u["id"], "signup", {"arm": arm} if arm else None)
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
    platform, handle = parse_handle(body.get("handle"), body.get("platform"))
    if platform not in ("instagram", "tiktok") or not HANDLE_RE.fullmatch(handle):
        raise ApiError(400, "Enter a TikTok or Instagram handle (letters, numbers, dots, underscores; no spaces)."
                       if " " in handle else "Enter a TikTok or Instagram handle, or paste your profile link.")
    with connect() as db:
        recent = db.execute("SELECT * FROM scans WHERE user_id = %s AND platform = %s AND handle = %s AND created_at > now() - interval '1 day'"
                            " ORDER BY id DESC LIMIT 1", (user["id"], platform, handle)).fetchone()
        if recent and not body.get("fresh"):
            return scan_view(recent, plan_of(user))
        used = db.execute("SELECT count(*) AS n FROM scans WHERE user_id = %s AND created_at > now() - interval '7 days'",
                          (user["id"],)).fetchone()["n"]
    limit = PLANS[plan_of(user)]["scansPerWeek"]
    if used >= limit:
        import packaging
        track(user["id"], "paywall", {"reason": "scan_quota", "plan": plan_of(user)})
        raise ApiError(402, packaging.quota_message(user, limit))
    try:
        videos, source = fetch_videos(platform, handle, user["plan"])
    except ApiError as e:
        if e.status == 404:
            track(user["id"], "scan_miss", {"platform": platform, "handle": handle, "reason": e.extra.get("reason", "demo"),
                                            "suggested": len(e.extra.get("suggestions", [])), "cached": e.extra.get("cached", False)})
        raise
    rows = aggregate(videos)
    prof = profile(platform, handle, videos)
    with connect() as db:
        with db.cursor() as cur:
            cur.executemany(VIDEO_UPSERT, [video_row(v) for v in videos])
        row = db.execute("INSERT INTO scans (user_id, platform, handle, source, profile, brands) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                         (user["id"], platform, handle, source, Jsonb(prof), Jsonb(rows))).fetchone()
    track(user["id"], "scan", {"platform": platform, "handle": handle, "brands": len(rows), "source": source, "videos": len(videos)})
    return scan_view(row, plan_of(user))


def load_scan(user, sid):
    with connect() as db:
        row = db.execute("SELECT * FROM scans WHERE id = %s AND user_id = %s", (sid, user["id"])).fetchone()
    if not row:
        raise ApiError(404, "Scan not found.")
    return row


def share_scan(user, sid):
    """Publish a scan at /creators/r/<token>: a read-only receipt page the creator can send to a brand."""
    row = load_scan(user, sid)
    token = row["share_token"]
    if not token:
        token = secrets.token_urlsafe(9)
        with connect() as db:
            db.execute("UPDATE scans SET share_token = %s WHERE id = %s", (token, sid))
        track(user["id"], "share", {"scan": sid})
    return {"url": app_url() + "/creators/r/" + token, "token": token}


def shared_view(token):
    with connect() as db:
        row = db.execute("SELECT s.*, u.plan, u.arm FROM scans s JOIN users u ON u.id = s.user_id WHERE s.share_token = %s",
                         (token,)).fetchone()
    if not row:
        raise ApiError(404, "This receipt page doesn't exist or was taken down.")
    return {**scan_view(row, plan_of(row)), "shared": True}


def sample_view():
    """The fixture creator as a full Pro report, so visitors can see the product before signing up."""
    videos = demo.creator_videos("tiktok", "maya.eats")
    row = {"id": 0, "platform": "tiktok", "handle": "maya.eats", "source": "demo", "created_at": now_iso(),
           "profile": profile("tiktok", "maya.eats", videos), "brands": aggregate(videos)}
    return {**scan_view(row, "pro"), "shared": True, "sample": True}


def list_scans(user):
    with connect() as db:
        return db.execute('SELECT id, platform, handle, source, created_at AS "createdAt", jsonb_array_length(brands) AS brands,'
                          " profile->>'name' AS name, profile->>'pfp' AS pfp FROM scans WHERE user_id = %s ORDER BY id DESC LIMIT 30",
                          (user["id"],)).fetchall()


def unlocked_brand(user, sid, name, feature):
    row = load_scan(user, sid)
    b = brand_entry(name)
    if not PLANS[plan_of(user)][feature]:
        import packaging
        track(user["id"], "paywall", {"reason": feature, "brand": b["name"]})
        what = "the sponsor check" if feature == "activity" else "pitch drafts"
        name = packaging.paid_name(user)
        raise ApiError(402, "%s unlocks %s." % (name, what) if name else "Your plan doesn't include %s." % what)
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


def checkout(user, plan="pro"):
    price = os.environ.get(PLAN_PRICE_ENV[plan])
    if not (stripe_config()["STRIPE_SECRET_KEY"] and price):
        raise ApiError(503, "Billing isn't configured on this server yet.")
    if user["plan"] == plan:
        raise ApiError(400, "You're already on %s." % {"pro": "Pro", "roster": "the roster plan", "leads": "Weekly leads"}[plan])
    back = app_url() + ("/creators/roster?subscribed=" if plan == "roster" else "/creators/?upgraded=")
    params = {"mode": "subscription", "line_items[0][price]": price, "line_items[0][quantity]": 1,
              "success_url": back + "1", "cancel_url": back + "0", "client_reference_id": user["id"], "metadata[user_id]": user["id"],
              "metadata[plan]": plan, "allow_promotion_codes": "true",
              # On the subscription too, so its later updates know which plan it is (Pro, Weekly leads or roster).
              "subscription_data[metadata][plan]": plan, "subscription_data[metadata][user_id]": user["id"]}
    if user["stripe_customer"]:
        params["customer"] = user["stripe_customer"]
    else:
        params["customer_email"] = user["email"]
    session = stripe("checkout/sessions", params)
    track(user["id"], "checkout_started", {"plan": plan})
    return {"url": session["url"]}


def portal(user):
    if not stripe_config()["STRIPE_SECRET_KEY"] or not user["stripe_customer"]:   # Pro, Weekly leads or the roster plan
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
                plan = (obj.get("metadata") or {}).get("plan")
                set_plan(db, "id = %s", (int(uid),), plan if plan in PLANS and plan != "free" else "pro",
                         obj.get("customer"), obj.get("subscription"))
                track(int(uid), "subscribed", {"subscription": obj.get("subscription"), "plan": plan if plan in PLANS else "pro"})
        elif kind in ("customer.subscription.updated", "customer.subscription.deleted"):
            active = kind.endswith("updated") and obj.get("status") in ("active", "trialing", "past_due")
            plan = (obj.get("metadata") or {}).get("plan")
            plan = plan if plan in PLANS and plan not in ("free", "open") else None
            # Still active: the plan the subscription was bought for (older ones without it keep their paid plan, or
            # Pro); ended drops to free.
            db.execute("UPDATE users SET plan = CASE WHEN %s THEN coalesce(%s, CASE WHEN plan = 'free' THEN 'pro' ELSE plan END) ELSE 'free' END,"
                       " stripe_subscription = coalesce(%s, stripe_subscription) WHERE stripe_customer = %s",
                       (active, plan, obj.get("id"), obj.get("customer")))
    import payments  # licence payments and creator payout accounts share this endpoint
    payments.on_event(event)
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
            return {"user": public_user(u) if u else None, "billing": billing_config(),
                    "brandUsage": brand_usage(u) if u else None}
        if path == "/billing/config":
            return billing_config()
        if path == "/brands":
            return [{"name": b["name"], "category": b["category"]} for b in CATALOG]
        if path == "/scans":
            return list_scans(require_user(headers))
        if path == "/sample":
            return sample_view()
        m = re.fullmatch(r"/shared/([A-Za-z0-9_-]{8,32})", path)
        if m:
            return shared_view(m.group(1))
        m = re.fullmatch(r"/scans/(\d+)", path)
        if m:
            u = require_user(headers)
            return scan_view(load_scan(u, int(m.group(1))), plan_of(u))
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
        return signup(body, headers)
    if path == "/visit":                    # the Receipts page's packaging-test arm (packaging.py)
        import packaging
        return packaging.visit(headers, body)
    if path == "/login":
        return login(body)
    if path == "/logout":
        return logout(headers)
    u = require_user(headers)
    if path == "/scans":
        return run_scan(u, body)
    m = re.fullmatch(r"/scans/(\d+)/share", path)
    if m:
        return share_scan(u, int(m.group(1)))
    m = re.fullmatch(r"/scans/(\d+)/brands/([^/]+)/(activity|pitch)", path)
    if m:
        sid, name, feature = int(m.group(1)), unquote(m.group(2)), m.group(3)
        return activity_route(u, sid, name) if feature == "activity" else pitch_route(u, sid, name)
    if path == "/billing/checkout":
        import packaging
        plan = "pro" if body.get("plan") == "pro" else packaging.offer_plan(u)
        return checkout(u, plan)
    if path == "/billing/portal":
        return portal(u)
    if path == "/billing/dev":
        return dev_switch(u, body)
    if path == "/billing/interest":  # checkout not live yet: the click is the signal the validation gate counts
        import packaging
        if body.get("plan") == "pro":
            data = {"plan": "pro", "price": PRO_PRICE_USD, "scan": body.get("scan"), "from": "brands"}
        else:
            plan = packaging.offer_plan(u)
            data = {"plan": plan, "price": packaging.offers()[packaging.user_arm(u)]["price"], "scan": body.get("scan")}
        track(u["id"], "checkout_intent", data)
        return {"ok": True}
    if path == "/report":
        track(u["id"], "report", {"text": str(body.get("text", ""))[:500], "scan": body.get("scan")})
        return {"ok": True}
    raise ApiError(404, "Not found.")
