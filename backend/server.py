"""Unprompted: find creators who say a brand out loud, using Oriane's transcript index.

    python3 backend/server.py        # dashboard + API on http://127.0.0.1:8000

Env (or ../.env): ORIANE_API_KEY, DATABASE_URL (default postgresql:///unprompted), HOST, PORT.
"""
import json
import math
import mimetypes
import os
import posixpath
import re
import statistics
import sys
import time
import traceback
from collections import Counter
from functools import cache

if __name__ == "__main__":  # `python3 backend/server.py`: the other modules' `import server` must get this module, not a
    sys.modules["server"] = sys.modules[__name__]  # second copy whose ApiError the handler here wouldn't catch
from datetime import date, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, request
from urllib.parse import parse_qs, quote, unquote, urlparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

ROOT = Path(__file__).resolve().parent.parent
mimetypes.add_type("font/woff2", ".woff2")
if (ROOT / ".env").exists():
    for line in (ROOT / ".env").read_text().splitlines():
        key, sep, val = line.partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), val.strip().strip("\"'"))

ORIANE_URL = "https://connect.oriane.xyz/rest/%s/search"
ORIANE_COST = {"contents": 40, "profiles": 30}
LANGS = {
    "en": ["en", "eng"], "fr": ["fr", "fra", "fre"], "es": ["es", "spa"],
    "pt": ["pt", "por"], "it": ["it", "ita"], "de": ["de", "deu", "ger"],
    "nl": ["nl", "nld", "dut"], "hi": ["hi", "hin"], "ja": ["ja", "jpn"],
    "id": ["id", "ind"], "tr": ["tr", "tur"], "ar": ["ar", "ara"],
}
DB_URL = os.environ.get("DATABASE_URL", "postgresql:///unprompted")


def waitlist_only():
    return os.environ.get("WAITLIST_ONLY", "").strip().lower() in ("1", "true", "yes", "on")


WAITLIST_ALLOW = re.compile(
    r"(?:"
    r"/waitlist(?:/.*)?"
    r"|/join/?"
    r"|/ui/.+"
    r"|/favicon\.ico"
    r"|/admin(?:/.*)?"
    r"|/api/admin/.*"
    r"|/digest/[^/]+"
    r"|/offer/[^/]+"
    r"|/license/[^/]+/[^/]+"
    r"|/api/digests/(?:[A-Za-z0-9_-]{16,32}(?:/.*)?|run|sample)"
    r"|/api/licenses/offer/[0-9a-f]{32}"
    r"|/api/waitlist(?:/.*)?"
    r")"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
  id          serial PRIMARY KEY,
  brand       text NOT NULL,
  params      jsonb NOT NULL,
  total_count int NOT NULL,
  total_views bigint NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS videos (
  id           text PRIMARY KEY,   -- Oriane content id
  platform     text NOT NULL,
  handle       text NOT NULL,
  published_at timestamptz,
  views        bigint NOT NULL,
  raw          jsonb NOT NULL,     -- full Oriane result: transcript, frames, comments
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mentions (
  search_id int  NOT NULL REFERENCES searches ON DELETE CASCADE,
  video_id  text NOT NULL REFERENCES videos,
  kind      text NOT NULL,         -- spoken | tagged | sponsored | owned | unverified
  hits      int  NOT NULL,         -- times the brand is said in the transcript
  quote     jsonb,                 -- {start, text, hit, core, frame, exact}
  PRIMARY KEY (search_id, video_id)
);
CREATE TABLE IF NOT EXISTS checks (
  id         serial PRIMARY KEY,
  platform   text NOT NULL,
  handle     text NOT NULL,
  brand      text NOT NULL,
  result     jsonb NOT NULL,       -- brand safety, performance, affinity, moodboard
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS checks_creator ON checks (platform, handle, created_at DESC);
ALTER TABLE searches ADD COLUMN IF NOT EXISTS fetch_ms int;  -- Oriane fetch + ranking time
CREATE TABLE IF NOT EXISTS oriane_calls (
  id         serial PRIMARY KEY,
  endpoint   text NOT NULL,
  credits    int NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS oriane_calls_created ON oriane_calls (created_at);
"""

VIDEO_UPSERT = """
INSERT INTO videos (id, platform, handle, published_at, views, raw) VALUES (%s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET views = EXCLUDED.views, raw = EXCLUDED.raw, updated_at = now()
"""

# Advocates = creators with organic (spoken or tagged) mentions. Brand-owned accounts are excluded.
CREATORS_SQL = """
SELECT v.platform, v.handle,
       max(v.raw->>'profileDisplayName') AS name,
       max(v.raw->>'profilePictureUrl') AS pfp,
       coalesce(bool_or((v.raw->>'profileVerified')::bool), false) AS verified,
       max((v.raw->>'profileFollowersCount')::bigint) AS followers,
       count(*) FILTER (WHERE m.kind IN ('spoken', 'tagged')) AS organic,
       count(*) FILTER (WHERE m.kind = 'spoken') AS spoken,
       count(*) FILTER (WHERE m.kind = 'sponsored') AS sponsored,
       coalesce(sum(v.views) FILTER (WHERE m.kind IN ('spoken', 'tagged')), 0)::bigint AS views,
       avg((v.raw->>'engagementRatePerViews')::float) FILTER (WHERE m.kind IN ('spoken', 'tagged')) AS er,
       array_agg(v.id ORDER BY v.views DESC) AS "videoIds"
FROM mentions m JOIN videos v ON v.id = m.video_id
WHERE m.search_id = %s AND m.kind <> 'owned'
GROUP BY v.platform, v.handle
HAVING count(*) FILTER (WHERE m.kind IN ('spoken', 'tagged')) > 0
"""

DISCLOSURE_TAGS = {"ad", "ads", "sponsored", "sponsor", "partner", "partnership", "paidpartnership",
                   "collab", "gifted", "advert", "إعلان", "اعلان", "تعاون"}
DISCLOSURE_PHRASES = ("paid partnership", "sponsored by", "in partnership with", "gifted by", "إعلان")

# Brand-safety categories: (points lost per flagged video, terms). GCC-weighted: alcohol and gambling
# matter to regional brands. ponytail: keyword lists; swap for a classifier when false positives bite.
SAFETY = {
    "Adult content": (25, ["onlyfans", "pornhub", "nsfw", "xxx"]),
    "Gambling": (20, ["casino", "gambling", "bet365", "sportsbook", "poker", "roulette", "قمار"]),
    "Alcohol & drugs": (15, ["beer", "vodka", "whiskey", "tequila", "wine", "cocktail", "drunk", "hangover",
                             "weed", "cocaine", "vape", "vaping", "خمر", "كحول"]),
    "Violence": (12, ["murder", "stabbed", "stabbing", "firearm"]),
    "Profanity": (6, ["fuck", "motherfuck", "shit", "bullshit", "bitch", "asshole", "goddamn", "wtf"]),
}
HANDLE_RE = re.compile(r"[A-Za-z0-9._]{1,40}")


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


# Arabic: one spelling has to match the ways people and speech-to-text write it. Letters that are often swapped share
# a class, short vowels and tatweel may sit between letters, and و/ف plus a preposition or "ال" can be glued on the
# front (وستاربكس، بستاربكس، للمراعي). A term can't start or end inside another word (نون in قانون).
ARABIC_RE = re.compile("[\u0600-\u06ff]")
AR_MARKS = "\u0610-\u061a\u064b-\u065f\u0670\u0640"
AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ة": "ه", "ى": "ي", "ی": "ي", "ئ": "ي", "ک": "ك", "ؤ": "و"})
AR_CLASS = {"ا": "اأإآٱ", "ه": "هة", "ي": "يىیئ", "ك": "كک", "و": "وؤ"}


def fold(text):
    """Lowercased, with Arabic letter variants, short vowels and tatweel folded: for comparing words, not for display."""
    return re.sub("[%s]" % AR_MARKS, "", text).translate(AR_FOLD).lower()


def arabic_pattern(term):
    def word(w):
        return ("[%s]*" % AR_MARKS).join("[%s]" % AR_CLASS[c] if c in AR_CLASS else re.escape(c) for c in w)
    words = re.findall(r"[^\W_]+", fold(term))
    first = words[0]
    if first.startswith("ال") and len(first) > 3:      # المراعي: والمراعي، بالمراعي، للمراعي
        head = "[وف]?(?:[بك]?%s|لل)%s" % (word("ال"), word(first[2:]))
    elif len(first) > 3:                                 # ستاربكس: وستاربكس، بستاربكس، الستاربكس، للستاربكس
        head = "[وف]?(?:لل|[بك]?%s|[بلك])?%s" % (word("ال"), word(first))
    else:                                                # short words don't take "ال": النون isn't Noon
        head = "[وف]?[بلك]?" + word(first)
    body = head + "".join(r"[\W_]*" + word(w) for w in words[1:])
    return "(?<![^\\W_])(?<![%s])%s[%s]*(?![^\\W_])" % (AR_MARKS, body, AR_MARKS)


def term_pattern(term):
    """Regex for one brand term, tolerant of spacing and punctuation: "TimHortons", "Tim Horton's"; Arabic terms go
    through arabic_pattern. Latin terms get a leading word boundary (accented ones, as before, don't)."""
    if ARABIC_RE.search(term):
        return arabic_pattern(term)
    words = re.findall(r"[^\W_]+", term.lower().replace("'", "").replace("’", ""))
    words = [re.escape(w[:-1]) + ("(?:['’]?s)?" if len(w) >= 6 else "['’]?s") if w.endswith("s") and len(w) > 3
             else re.escape(w) for w in words]
    return ("(?<![^\\W_])" if term.isascii() else "") + r"[\W_]*".join(words)


@cache
def everyday_spellings():
    """{folded Arabic brand spelling that is also an everyday word: its folded context words} (brands.py)."""
    return {fold(t): [fold(w) for w in ws] for t, ws in brands.arabic_everyday().items()}


def mention_re(terms):
    return re.compile("|".join("(?:%s)" % term_pattern(t) for t in terms), re.IGNORECASE)


def nearest_frame(raw, seconds):
    frames = raw.get("frames") or []
    if not frames:
        return raw.get("thumbnailMediaUrl")
    return min(frames, key=lambda f: abs(f["timestampSeconds"] - seconds))["url"]


def text_quote(text, rx, frame):
    """Evidence from caption text (no timestamp): the match with some context around it."""
    m = rx.search(text)
    a = max(0, m.start() - 80)
    snippet = text[a:m.end() + 80]
    return {"start": None, "text": snippet, "hit": [m.start() - a, m.end() - a], "core": snippet, "frame": frame}


def find_quote(raw, rx):
    """First transcript moment matching rx, with one chunk of context either side."""
    chunks = raw.get("transcriptChunks") or []
    for i, chunk in enumerate(chunks):
        # Search chunk + next chunk so a name split across chunks ("Tim" | "Hortons") still matches.
        nxt = chunks[i + 1]["text"] if i + 1 < len(chunks) else ""
        m = rx.search(chunk["text"] + " " + nxt)
        if not m or m.start() >= len(chunk["text"]):
            continue
        spans = m.end() > len(chunk["text"])
        core = chunk["text"] + (" " + nxt if spans else "")
        before = chunks[i - 1]["text"] + " " if i else ""
        after_i = i + 2 if spans else i + 1
        after = " " + chunks[after_i]["text"] if after_i < len(chunks) else ""
        a = len(before) + m.start()
        return {"start": chunk["startSeconds"], "text": before + core + after, "hit": [a, a + len(m.group())],
                "core": core, "frame": nearest_frame(raw, chunk["startSeconds"])}
    return None


def classify(raw, brand, variants, about=()):
    """(kind, hits, quote) for one Oriane result.

    kind, first match wins: owned (brand's own account), sponsored (co-authored with the brand, or disclosed and
    the brand is in it), tagged (brand in caption, hashtags or @mentions), spoken (only said on camera: invisible to
    caption-based social listening), unverified (Oriane matched it, but the brand isn't in the transcript or caption).
    An Arabic variant that is also an everyday word (طلبات, "orders") counts only with one of its context words in
    the same quote or caption.
    """
    everyday = {v: everyday_spellings().get(" ".join(fold(v).split())) for v in variants}
    context = [w for v in variants for w in everyday[v] or []]
    rx = mention_re([brand, *(v for v in variants if not everyday[v])])
    rx_everyday = mention_re([v for v in variants if everyday[v]]) if context else None

    def in_context(text):
        return any(w in fold(text) for w in context)
    caption = " ".join([raw.get("caption") or "", *(raw.get("hashtags") or [])])
    tags = {t[1:] for t in re.findall(r"#[^\W_]+", caption.lower())}
    co_authors = " ".join(c["profileHandle"] for c in raw.get("coAuthors") or [])
    mentioned = " ".join(m["profileHandle"] for m in raw.get("mentions") or [])
    in_caption = bool(rx.search(caption + " " + mentioned)) or bool(rx_everyday and rx_everyday.search(caption) and in_context(caption))
    transcript = raw.get("transcript") or ""
    q = find_quote(raw, rx)
    if not q and rx_everyday:
        q = find_quote(raw, rx_everyday)
        q = q if q and in_context(q["text"]) else None
    on_topic = (not about or bool(mention_re(about).search(caption) or mention_re(about).search(transcript)
                                  or mention_re([brand]).search(mentioned)))
    if mention_re([brand]).search(raw.get("profileHandle") or ""):
        kind = "owned"
    elif rx.search(co_authors):
        kind = "sponsored"
    elif not on_topic:
        kind = "unverified"
    elif not (in_caption or q):
        kind = "unverified"  # a fuzzy match, or someone else's ad: the brand isn't in what was said or written
    elif tags & DISCLOSURE_TAGS or any(p in caption.lower() for p in DISCLOSURE_PHRASES):
        kind = "sponsored"
    elif in_caption:
        kind = "tagged"
    else:
        kind = "spoken"
    hits = len(rx.findall(transcript)) + (len(rx_everyday.findall(transcript)) if rx_everyday and in_context(transcript) else 0)
    return kind, hits, q


def score(organic, spoken, views, er):
    """0-100 advocate score. ponytail: hand-tuned linear blend; fit weights to campaign outcomes later."""
    parts = {
        "loyalty": round(min(organic, 3) / 3 * 40),               # 3+ unprompted videos = max
        "reach": round(min(math.log10(views + 1) / 6, 1) * 30),   # 1M organic views = max
        "engagement": round(min((er or 0) / 8, 1) * 20),          # er is a percentage; 8% per view = max
        "spoken": 10 if spoken else 0,                             # said it on camera, not just tagged
    }
    return {**parts, "total": sum(parts.values())}


def post_url(raw):
    if raw["platform"] == "tiktok":
        return "https://www.tiktok.com/@%s/video/%s" % (quote(raw["profileHandle"]), quote(raw["platformId"]))
    return "https://www.instagram.com/p/%s/" % quote(raw["platformId"])


def profile_url(platform, handle):
    return ("https://www.tiktok.com/@%s" if platform == "tiktok" else "https://www.instagram.com/%s/") % quote(handle)


def parse(body):
    """Validate the search form. Returns (brand, variants, filters, params)."""
    if not isinstance(body, dict):
        raise ApiError(400, "Invalid request")
    brand = str(body.get("brand", "")).strip()
    if not 2 <= len(brand) <= 60 or not re.search(r"[^\W_]", brand):
        raise ApiError(400, "Enter a brand name (2-60 characters).")
    variants = [v.strip() for v in str(body.get("variants", "")).split(",") if re.search(r"[^\W_]", v)]
    variants = [v for v in dict.fromkeys(variants) if v.lower() != brand.lower()][:5]
    if any(len(v) > 60 for v in variants):
        raise ApiError(400, "Each spelling variant must be under 60 characters.")
    about = body.get("about", [])
    if isinstance(about, str):
        about = about.split(",")
    elif not isinstance(about, list):
        about = []
    about_terms = []
    for term in about:
        if not isinstance(term, str):
            continue
        term = term.strip().lower()
        if not term:
            continue
        if len(term) > 40:
            raise ApiError(400, "Each 'what it sells' word must be under 40 characters.")
        if re.search(r"[^\W\d_]", term):
            about_terms.append(term)
    platform, lang, days = body.get("platform", "all"), body.get("lang", "any"), body.get("days", 365)
    if platform not in ("all", "instagram", "tiktok") or lang not in ("any", *LANGS) or days not in (30, 90, 365, 0):
        raise ApiError(400, "Invalid filter value.")

    # Fuzzy multi-word search matches loosely ("Al Ain water" pulled 72K videos about water), so multi-word
    # names must appear as a phrase. Single words stay fuzzy to catch speech-to-text spellings.
    phrases = [t for t in [brand, *variants] if len(re.findall(r"[^\W_]+", t)) > 1]
    words = [t for t in [brand, *variants] if t not in phrases]
    transcript = {"operator": "or"}
    if phrases:
        transcript["includesExactly"] = {"values": phrases}
    if words:
        transcript["includesFuzzy"] = {"values": words}
    filters = {"transcript": transcript}
    if platform != "all":
        filters["platform"] = {"includes": [platform]}
    if lang != "any":
        filters["transcriptLanguage"] = {"includes": LANGS[lang]}
    if days:
        filters["publishedAt"] = {"after": (date.today() - timedelta(days=days)).isoformat()}
    params = {"variants": variants, "platform": platform, "lang": lang, "days": days, "match": 2}
    if about_terms:
        params["about"] = list(dict.fromkeys(about_terms))[:5]
    return brand, variants, filters, params


EMPTY_PAGE = {"data": {"results": [], "aggregations": {"totalViewsCount": 0}}, "metadata": {}}


def record_oriane_call(index):
    cost = ORIANE_COST[index]
    budget = int(os.environ.get("ORIANE_DAILY_BUDGET", "600"))
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(471478343)")
        used = db.execute(
            "SELECT coalesce(sum(credits), 0) AS n FROM oriane_calls"
            " WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"
        ).fetchone()["n"]
        if used + cost > budget:
            raise ApiError(503, "Live search is paused for today to protect our data credits. "
                           "Nothing was taken from your quota; try again tomorrow.")
        db.execute("INSERT INTO oriane_calls (endpoint, credits) VALUES (%s, %s)", (index, cost))


def oriane(filters, limit=100, sort="transcriptRelevance", offset=0, index="contents"):
    key = os.environ.get("ORIANE_API_KEY")
    if not key:
        raise ApiError(500, "ORIANE_API_KEY is not set.")
    req = request.Request(
        "%s?projection=full&sort=%s&limit=%d&offset=%d" % (ORIANE_URL % index, sort, limit, offset),
        data=json.dumps({"operator": "and", "filters": filters}).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    record_oriane_call(index)
    try:
        with request.urlopen(req, timeout=45) as res:
            return json.load(res)
    except error.HTTPError as e:
        if e.code == 416:  # offset past the last result
            return EMPTY_PAGE
        try:
            msg = json.loads(e.read())["error"]["message"]
        except (ValueError, KeyError, TypeError):
            msg = e.reason
        if e.code in (401, 402, 403):  # key or wallet problem: ours to fix, not the user's
            print("oriane auth/billing error %s: %s" % (e.code, msg), file=sys.stderr)
            raise ApiError(503, "Scanning is paused on our side for a moment (data provider). Nothing was taken from your quota; try again shortly.")
        raise ApiError(502, "Oriane returned %s: %s" % (e.code, msg))
    except OSError as e:
        raise ApiError(502, "Can't reach Oriane (%s)." % e)


def connect():
    return psycopg.connect(DB_URL, row_factory=dict_row)


def video_row(r):
    return r["id"], r["platform"], r["profileHandle"], r.get("publishedAt"), r.get("viewsCount") or 0, Jsonb(r)


def new_search(brand, params, data, user_id=None):
    total = (data["metadata"].get("pagination") or {}).get("totalCount", len(data["data"]["results"]))
    with connect() as db:
        return db.execute(
            "INSERT INTO searches (brand, params, total_count, total_views, user_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (brand, Jsonb(params), total, data["data"]["aggregations"]["totalViewsCount"], user_id)).fetchone()["id"]


def store_results(sid, results, brand, variants, about=()):
    """Store raw videos and their classified mentions for one search."""
    results = list({r["id"]: r for r in results}.values())
    rows = []
    for r in results:
        kind, hits, q = classify(r, brand, variants, about)
        rows.append((sid, r["id"], kind, hits, Jsonb(q) if q else None))
    with connect() as db:
        with db.cursor() as cur:
            cur.executemany(VIDEO_UPSERT, [video_row(r) for r in results])
            cur.executemany("INSERT INTO mentions (search_id, video_id, kind, hits, quote) VALUES (%s, %s, %s, %s, %s)"
                            " ON CONFLICT DO NOTHING", rows)
    return len(results)


def recent_search(brand, params, user_id=None):
    """The same brand and filters searched in the last 15 minutes: reuse it instead of paying Oriane again."""
    with connect() as db:
        row = db.execute(
            "SELECT id FROM searches WHERE lower(brand) = lower(%s) AND params = %s"
            " AND user_id IS NOT DISTINCT FROM %s AND created_at > now() - interval '15 minutes'"
            " ORDER BY id DESC LIMIT 1", (brand, Jsonb(params), user_id)).fetchone()
    return row["id"] if row else None


def search_events(brand, variants, filters, params, user=None, progress=True):
    """Yield the dashboard as Oriane's top 100 lands in one call.

    A repeat of a search from the last 15 minutes is served straight from Postgres.
    """
    t0 = time.perf_counter()
    user_id = user["id"] if user else None
    sid = recent_search(brand, params, user_id)
    source = "cache" if sid else "oriane"
    if not sid:
        if user:
            usage = creator.brand_usage(user)["searches"]
            if usage["used"] >= usage["limit"]:
                if user["plan"] == "free":
                    raise ApiError(402, "You've used your %d free brand searches this week. Pro gets %d." %
                                   (usage["limit"], creator.PLANS["pro"]["brandSearchesPerWeek"]))
                raise ApiError(402, "Search limit reached (%d per week)." % usage["limit"])
        data = oriane(filters, 100, "transcriptRelevance", 0)
        results = data["data"]["results"]
        sid = new_search(brand, params, data if results else EMPTY_PAGE, user_id=user_id)
        if results:
            heard = store_results(sid, results, brand, variants, params.get("about", ()))
            if progress:
                yield {"type": "progress", "heard": heard, "data": load(sid)}
        with connect() as db:
            db.execute("UPDATE searches SET fetch_ms = %s WHERE id = %s", (round((time.perf_counter() - t0) * 1000), sid))
    timing = {"source": source, "ms": round((time.perf_counter() - t0) * 1000)}
    yield {"type": "done", "data": {**load(sid), "timing": timing}}


def run_search(body, user):
    """One-shot search: run search_events to the end and return the final dashboard."""
    for event in search_events(*parse(body), user=user, progress=False):
        pass
    return event["data"]


def video_detail(vid, query):
    """Everything the evidence player needs: frames, timed transcript, and where the brand (or a flag) is said."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", vid):
        raise ApiError(400, "Invalid video id.")
    with connect() as db:
        row = db.execute("SELECT raw FROM videos WHERE id = %s", (vid,)).fetchone()
    if not row:
        raise ApiError(404, "Video not found.")
    raw = row["raw"]
    terms = [t for t in [query.get("brand", ""), *query.get("variants", "").split(",")]
             if re.search(r"[^\W_]", t) and len(t) <= 60][:6]
    brand_rx = mention_re(terms) if terms else None
    flag_rx = mention_re(SAFETY[query["flag"]][1]) if query.get("flag") in SAFETY else None

    def spans(rx, text):
        return [[m.start(), m.end()] for m in rx.finditer(text)] if rx else []

    chunks = [{"start": c["startSeconds"], "end": c["endSeconds"], "text": c["text"],
               "brand": spans(brand_rx, c["text"]), "flag": spans(flag_rx, c["text"])}
              for c in raw.get("transcriptChunks") or []]
    frames = sorted(({"t": f["timestampSeconds"], "url": f["url"]} for f in raw.get("frames") or []), key=lambda f: f["t"])
    duration = raw.get("duration") or max([c["end"] for c in chunks] + [f["t"] for f in frames] + [1])
    return {"id": raw["id"], "platform": raw["platform"], "handle": raw["profileHandle"], "name": raw.get("profileDisplayName"),
            "pfp": raw.get("profilePictureUrl"), "url": post_url(raw), "publishedAt": raw.get("publishedAt"),
            "views": raw.get("viewsCount") or 0, "duration": duration, "thumb": raw.get("thumbnailMediaUrl"),
            "lang": raw.get("transcriptLanguage"), "frames": frames, "chunks": chunks}


def check(recent, brand, variants, known=()):
    """Score check from a creator's recent videos: brand safety, performance, brand affinity, moodboard.

    known: ids of older videos where this creator already mentioned the brand unprompted (from past searches).
    """
    flags, lost = [], Counter()
    for r in recent:
        caption = " ".join([r.get("caption") or "", *(r.get("hashtags") or [])])
        for cat, (_, terms) in SAFETY.items():
            rx = mention_re(terms)
            hay = next((t for t in (caption, r.get("transcript") or "") if rx.search(t)), None)
            if hay is not None:
                lost[cat] += 1
                q = find_quote(r, rx) or text_quote(hay, rx, r.get("thumbnailMediaUrl"))
                flags.append({"id": r["id"], "category": cat, "url": post_url(r), "platform": r["platform"], "lang": r.get("transcriptLanguage"),
                              "publishedAt": r.get("publishedAt"), "views": r.get("viewsCount") or 0, "quote": q})
    safety = max(0, 100 - sum(SAFETY[cat][0] * min(n, 3) for cat, n in lost.items()))  # 3+ videos per category caps

    views = [r.get("viewsCount") or 0 for r in recent]
    ers = [r["engagementRatePerViews"] for r in recent if r.get("engagementRatePerViews") is not None]
    followers = max(r.get("profileFollowersCount") or 0 for r in recent)
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    posts_30d = sum(1 for r in recent if (r.get("publishedAt") or "")[:10] >= cutoff)
    median_views, median_er = statistics.median(views), statistics.median(ers) if ers else 0
    watch = median_views / followers if followers else 0
    performance = round(min(median_er / 8, 1) * 45 + min(watch / 0.3, 1) * 35 + min(posts_30d / 12, 1) * 20)

    brand_rx = mention_re([brand, *variants])
    recent_ids = {r["id"] for r in recent if brand_rx.search(" ".join(
        [r.get("caption") or "", *(r.get("hashtags") or []), r.get("transcript") or ""]))}
    mentions = len(recent_ids | set(known))
    affinity = round(min(mentions / 3, 1) * 100)  # 3+ videos mentioning the brand = full marks

    total = round(0.4 * safety + 0.35 * performance + 0.25 * affinity)
    verdict = "Risky" if safety < 60 else "Ready to pitch" if total >= 70 else "Review first"
    top = sorted(recent, key=lambda r: -(r.get("viewsCount") or 0))[:8]
    dates = sorted(r["publishedAt"] for r in recent if r.get("publishedAt"))
    return {
        "total": total, "verdict": verdict, "videos": len(recent), "window": [dates[0], dates[-1]] if dates else None,
        "safety": {"score": safety, "flagged": len({f["url"] for f in flags}), "categories": dict(lost), "flags": flags[:6]},
        "performance": {"score": performance, "medianViews": median_views, "medianEr": median_er, "watchRate": watch,
                        "posts30d": posts_30d, "followers": followers},
        "affinity": {"score": affinity, "mentions": mentions, "recent": len(recent_ids)},
        "moodboard": {
            "frames": [{"id": r["id"], "image": r.get("thumbnailMediaUrl") or nearest_frame(r, (r.get("duration") or 0) / 2),
                        "url": post_url(r), "views": r.get("viewsCount") or 0} for r in top],
            "hashtags": Counter(t.lower() for r in recent for t in r.get("hashtags") or []).most_common(8),
            "languages": dict(Counter(r["transcriptLanguage"] for r in recent if r.get("transcriptLanguage"))),
        },
    }


def run_check(body, user):
    """Score-check a creator, using the global 24h cache unless a Pro user requests a fresh result."""
    platform, handle = body.get("platform"), str(body.get("handle", ""))
    if platform not in ("instagram", "tiktok") or not HANDLE_RE.fullmatch(handle):
        raise ApiError(400, "Invalid creator.")
    brand, variants, _, _ = parse({"brand": body.get("brand"), "variants": body.get("variants", "")})
    with connect() as db:
        cached = db.execute(
            'SELECT result, created_at AS "createdAt" FROM checks WHERE platform = %s AND handle = %s'
            " AND lower(brand) = lower(%s) AND created_at > now() - interval '1 day' ORDER BY created_at DESC LIMIT 1",
            (platform, handle, brand)).fetchone()
        known = [r["video_id"] for r in db.execute(
            "SELECT DISTINCT m.video_id FROM mentions m JOIN videos v ON v.id = m.video_id JOIN searches s ON s.id = m.search_id"
            " WHERE v.platform = %s AND v.handle = %s AND lower(s.brand) = lower(%s) AND m.kind IN ('spoken', 'tagged')",
            (platform, handle, brand)).fetchall()]
    fresh = bool(body.get("fresh")) and user["plan"] == "pro"
    if cached and not fresh:
        return {**cached["result"], "createdAt": cached["createdAt"]}
    usage = creator.brand_usage(user)["checks"]
    if usage["used"] >= usage["limit"]:
        if user["plan"] == "free":
            raise ApiError(402, "You've used your %d free creator checks this week. Pro gets %d." %
                           (usage["limit"], creator.PLANS["pro"]["checksPerWeek"]))
        raise ApiError(402, "Check limit reached (%d per week)." % usage["limit"])
    filters = {"profileHandle": {"exactMatch": {"values": [handle]}}, "platform": {"includes": [platform]}}
    recent = oriane(filters, limit=30, sort="publishedAt")["data"]["results"]
    if not recent:
        raise ApiError(404, "Oriane has no videos indexed for @%s yet." % handle)
    result = check(recent, brand, variants, known)
    with connect() as db:
        with db.cursor() as cur:
            cur.executemany(VIDEO_UPSERT, [video_row(r) for r in recent])
        created = db.execute("INSERT INTO checks (platform, handle, brand, result, user_id) VALUES (%s, %s, %s, %s, %s)"
                             " RETURNING created_at",
                             (platform, handle, brand, Jsonb(result), user["id"])).fetchone()["created_at"]
    return {**result, "createdAt": created}


def load(sid):
    with connect() as db:
        search = db.execute(
            'SELECT id, brand, params, total_count AS "totalCount", total_views AS "totalViews", created_at AS "createdAt",'
            ' fetch_ms AS "fetchMs" FROM searches WHERE id = %s', (sid,)).fetchone()
        if not search:
            raise ApiError(404, "Search not found.")
        rows = db.execute(
            "SELECT v.raw, m.kind, m.hits, m.quote FROM mentions m JOIN videos v ON v.id = m.video_id"
            " WHERE m.search_id = %s ORDER BY v.views DESC", (sid,)).fetchall()
        creators = db.execute(CREATORS_SQL, (sid,)).fetchall()
        checks = db.execute(
            'SELECT DISTINCT ON (platform, handle) platform, handle, result, created_at AS "createdAt" FROM checks'
            " WHERE lower(brand) = lower(%s) ORDER BY platform, handle, created_at DESC", (search["brand"],)).fetchall()
    videos = [{
        "id": r["raw"]["id"], "platform": r["raw"]["platform"], "handle": r["raw"]["profileHandle"],
        "url": post_url(r["raw"]), "thumb": r["raw"].get("thumbnailMediaUrl"),
        "views": r["raw"].get("viewsCount") or 0, "er": r["raw"].get("engagementRatePerViews"),
        "publishedAt": r["raw"].get("publishedAt"), "caption": (r["raw"].get("caption") or "")[:280],
        "lang": r["raw"].get("transcriptLanguage"), "kind": r["kind"], "hits": r["hits"], "quote": r["quote"],
    } for r in rows]
    for c in creators:
        c["profileUrl"] = profile_url(c["platform"], c["handle"])
        c["score"] = score(c["organic"], c["spoken"], c["views"], c["er"])
    creators.sort(key=lambda c: (-c["score"]["total"], -c["views"]))
    return {"search": search, "videos": videos, "creators": creators,
            "checks": {"%s:%s" % (c["platform"], c["handle"]): {**c["result"], "createdAt": c["createdAt"]} for c in checks}}


SHOWCASE = {}   # search id -> (loaded at, payload): the landing's real receipts, cached for 10 minutes


def showcase():
    """GET /api/showcase: real receipts for the brand landing's hero, from the one saved search the operator picks
    (SHOWCASE_SEARCH_ID). Searches are private, so nothing is shown unless that setting names one."""
    sid = os.environ.get("SHOWCASE_SEARCH_ID", "").strip()
    if not sid.isdigit():
        return {"receipts": []}
    hit = SHOWCASE.get(sid)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    data = load(int(sid))
    said = [v for v in data["videos"] if v["kind"] in ("spoken", "tagged") and v["quote"]]
    said.sort(key=lambda v: (v["kind"] != "spoken", -(v["views"] or 0)))
    out = {"brand": data["search"]["brand"], "searchedAt": data["search"]["createdAt"], "analyzed": len(data["videos"]),
           "creators": len(data["creators"]), "spokenOnly": sum(v["kind"] == "spoken" for v in data["videos"]),
           "receipts": [{"handle": v["handle"], "platform": v["platform"], "url": v["url"], "views": v["views"], "publishedAt": v["publishedAt"],
                         "kind": v["kind"], "frame": (v["quote"] or {}).get("frame") or v["thumb"],
                         "quote": {k: (v["quote"] or {}).get(k) for k in ("text", "hit", "start")}} for v in said[:5]]}
    SHOWCASE[sid] = (time.time(), out)
    return out


FLAG_REASONS = ("not_a_mention", "wrong_brand", "other")


def flag_mention(body, user):
    """POST /api/feedback: someone says a result isn't a real mention. Recorded for review; nothing is hidden by it."""
    reason = body.get("reason")
    if reason not in FLAG_REASONS or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", str(body.get("videoId") or "")):
        raise ApiError(400, "Say which video and why.")
    creator.track(user["id"] if user else None, "mention_flag", {"search": body.get("searchId"), "video": body["videoId"], "reason": reason,
                                                                 "note": str(body.get("note") or "")[:300]})
    return {"ok": True}


def list_searches(user=None):
    """The user's 20 latest searches; `watch` is set when they watch that brand and filters (any period)."""
    if not user or user["role"] != "brand":
        return []
    with connect() as db:
        rows = db.execute(
            'SELECT s.id, s.brand, s.params, s.total_count AS "totalCount", s.created_at AS "createdAt", s.fetch_ms AS "fetchMs",'
            " count(m.video_id) AS analyzed, w.token AS watch_token,"
            " CASE WHEN w.paused_at IS NOT NULL THEN 'paused' WHEN w.confirmed_at IS NOT NULL THEN 'active' ELSE 'pending' END AS watch_status"
            " FROM searches s LEFT JOIN mentions m ON m.search_id = s.id"
            " LEFT JOIN LATERAL (SELECT * FROM digests d WHERE d.user_id = s.user_id AND d.brand = s.brand AND d.params ="
            "   jsonb_build_object('variants', s.params->'variants', 'platform', s.params->'platform', 'lang', s.params->'lang')"
            "   || CASE WHEN s.params ? 'about' THEN jsonb_build_object('about', s.params->'about') ELSE '{}'::jsonb END"
            "   ORDER BY d.id LIMIT 1) w ON true"
            " WHERE s.user_id = %s GROUP BY s.id, w.token, w.paused_at, w.confirmed_at ORDER BY s.id DESC LIMIT 20",
            (user["id"],)).fetchall()
    for r in rows:
        token, status = r.pop("watch_token"), r.pop("watch_status")
        r["watch"] = {"status": status, "manageUrl": digest.manage_url(token)} if token else None
    return rows


def require_brand_user(headers):
    return creator.require_role(headers, "brand", "Sign in to search.")


def not_found():
    raise ApiError(404, "Not found.")


class Stream:
    """Marks a route result as NDJSON events to stream, not one JSON body."""
    def __init__(self, events):
        self.events = events


def dumps(obj):
    return json.dumps(obj, default=lambda o: o.isoformat() if hasattr(o, "isoformat") else str(o)).encode()


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT / "frontend"), **kwargs)

    def _waitlist_gate(self, method):
        url = urlparse(self.path)
        path = posixpath.normpath(unquote(unquote(url.path)))
        if not waitlist_only() or WAITLIST_ALLOW.fullmatch(path):
            return False
        if method in ("GET", "HEAD") and not path.startswith("/api/"):
            location = "/waitlist" + ("?" + url.query if url.query else "")
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.api(not_found)
        return True

    def do_GET(self):
        if self._waitlist_gate("GET"):
            return
        url = urlparse(self.path)
        path = url.path
        m = re.fullmatch(r"/api/searches/(\d+)", path)
        video = re.fullmatch(r"/api/videos/([^/]+)", path)
        if path.startswith("/api/creators/"):
            self.api(lambda: creator.dispatch(self))
        elif path.startswith("/api/digests"):
            self.api(lambda: digest.dispatch(self))
        elif path.startswith("/api/licenses/") or path.startswith("/api/admin/handles"):
            self.api(lambda: licenses.dispatch(self))
        elif path.startswith("/api/rosters/") or path == "/api/admin/rosters":
            self.api(lambda: rosters.dispatch(self))
        elif path == "/api/admin/seeding":
            self.api(lambda: seeding.admin_route(self))
        elif path == "/api/admin/eval" or path.startswith("/api/admin/eval/"):
            self.api(lambda: eval_mentions.admin_route(self))
        elif path == "/api/admin/packaging":
            self.api(lambda: packaging.admin_route(self))
        elif path == "/api/admin/health":
            self.api(lambda: ops.admin_route(self))
        elif path == "/api/admin/waitlist":
            self.api(lambda: waitlist.admin_route(self))
        elif path == "/api/brands/arabic":
            self.api(lambda: {"brands": brands.arabic_spellings()})
        elif path == "/api/showcase":
            self.api(showcase)
        elif path.startswith("/api/waitlist"):
            self.api(lambda: waitlist.dispatch(self))
        elif path.startswith("/api/admin/"):
            self.api(lambda: digest.admin(self))
        elif path == "/api/searches":
            self.api(lambda: list_searches(creator.current_user(self.headers)))
        elif m:
            self.api(lambda: load(int(m.group(1))))
        elif video:
            self.api(lambda: video_detail(video.group(1), {k: v[0] for k, v in parse_qs(url.query).items()}))
        elif path.startswith("/api/"):
            self.api(not_found)
        else:
            if path == "/":
                self.path = "/creators/index.html"
            elif path in ("/brands", "/brands/"):
                self.path = "/index.html"
            elif path in ("/dashboard", "/dashboard/"):
                self.path = "/dashboard/index.html"
            elif re.fullmatch(r"/creators/r/[^/]+", path):
                self.path = "/creators/index.html"
            elif re.fullmatch(r"/digest/[^/]+", path):
                self.path = "/digest/index.html"
            elif re.fullmatch(r"/license/[^/]+/[^/]+", path):
                self.path = "/license/index.html"
            elif re.fullmatch(r"/offer/[^/]+", path):
                self.path = "/offer/index.html"
            elif path in ("/creators/licenses", "/creators/licenses/"):
                self.path = "/creators/licenses.html"
            elif path in ("/creators/roster", "/creators/roster/"):
                self.path = "/creators/roster.html"
            elif path in ("/demo/credits", "/demo/credits/"):
                self.path = "/demo/credits.html"
            elif path in ("/waitlist", "/waitlist/", "/join", "/join/"):
                self.path = "/waitlist/index.html"
            elif path in ("/waitlist/admin", "/waitlist/admin/"):
                self.path = "/waitlist/admin.html"
            super().do_GET()

    def do_HEAD(self):
        if self._waitlist_gate("HEAD"):
            return
        super().do_HEAD()

    def do_POST(self):
        if self._waitlist_gate("POST"):
            return
        routes = {"/api/searches": run_search, "/api/checks": run_check,
                  "/api/searches/stream": lambda body, user: Stream(search_events(*parse(body), user=user))}
        route = routes.get(urlparse(self.path).path)
        if self.path.startswith("/api/creators/"):
            return self.api(lambda: creator.dispatch(self))
        if self.path.startswith("/api/digests"):
            return self.api(lambda: digest.dispatch(self))
        if self.path.startswith("/api/waitlist"):
            return self.api(lambda: waitlist.dispatch(self))
        if self.path.startswith("/api/licenses/") or self.path.startswith("/api/admin/handles"):
            return self.api(lambda: licenses.dispatch(self))
        if self.path.startswith("/api/rosters/") or self.path == "/api/admin/rosters":
            return self.api(lambda: rosters.dispatch(self))
        if self.path.startswith("/api/admin/eval/"):
            return self.api(lambda: eval_mentions.admin_route(self))
        if self.path == "/api/feedback":
            return self.api(lambda: flag_mention(digest.read_json(self), creator.current_user(self.headers)))
        if self.path.startswith("/api/admin/"):
            return self.api(lambda: digest.admin(self))
        if not route:
            return self.api(not_found)

        def handle():
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 10000)) or b"{}")
            except ValueError:
                raise ApiError(400, "Invalid JSON.")
            return route(body, require_brand_user(self.headers))
        self.api(handle)

    def api(self, fn):
        try:
            status, body = 200, fn()
        except ApiError as e:
            status, body = e.status, {"error": str(e), **e.extra}
        except psycopg.OperationalError:
            traceback.print_exc()
            status, body = 503, {"error": "Database unavailable. Is Postgres running?"}
        except Exception:
            traceback.print_exc()
            status, body = 500, {"error": "Something broke on our side. Check the server log."}
        if isinstance(body, Stream):
            return self.stream(body.events)
        headers = []
        if isinstance(body, creator.Reply):
            status, body, headers = body.status, body.body, body.headers
        data = dumps(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def stream(self, events):
        """One JSON event per line; the connection closing marks the end (HTTP/1.0)."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for event in events:
                self.wfile.write(dumps(event) + b"\n")
                self.wfile.flush()
        except ApiError as e:
            self.wfile.write(dumps({"type": "error", "error": str(e)}) + b"\n")
        except psycopg.OperationalError:
            traceback.print_exc()
            self.wfile.write(dumps({"type": "error", "error": "Database unavailable. Is Postgres running?"}) + b"\n")
        except Exception:
            traceback.print_exc()
            self.wfile.write(dumps({"type": "error", "error": "Something broke on our side. Check the server log."}) + b"\n")


sys.modules.setdefault("server", sys.modules[__name__])  # creator imports us back; don't load this file twice
import creator  # noqa: E402
import digest  # noqa: E402
import licenses  # noqa: E402
import payments  # noqa: E402
import rosters  # noqa: E402
import seeding  # noqa: E402
import brands  # noqa: E402
import eval_mentions  # noqa: E402
import packaging  # noqa: E402
import ops  # noqa: E402
import waitlist  # noqa: E402


def init_db():
    with psycopg.connect(DB_URL) as db:
        db.execute(SCHEMA)
        db.execute(creator.SCHEMA)
        db.execute("ALTER TABLE searches ADD COLUMN IF NOT EXISTS user_id int REFERENCES users ON DELETE SET NULL")
        db.execute("ALTER TABLE checks ADD COLUMN IF NOT EXISTS user_id int REFERENCES users ON DELETE SET NULL")
        db.execute("CREATE INDEX IF NOT EXISTS searches_user ON searches (user_id, created_at DESC)")
        db.execute("CREATE INDEX IF NOT EXISTS checks_user ON checks (user_id, created_at DESC)")
        db.execute(digest.SCHEMA)
        db.execute(licenses.SCHEMA)
        db.execute(payments.SCHEMA)
        db.execute(rosters.SCHEMA)
        db.execute(seeding.SCHEMA)
        db.execute(eval_mentions.SCHEMA)
        db.execute(packaging.SCHEMA)
        db.execute(creator.ROLE_MIGRATION)
        db.execute(waitlist.SCHEMA)


if __name__ == "__main__":
    init_db()
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print("Unprompted on http://%s:%d" % (host, port), flush=True)
    digest.scheduler()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
