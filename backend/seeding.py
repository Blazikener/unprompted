"""Seeding report (Phase 5): which gifted creators actually posted, including the ones tag tracking can't see.

A brand pastes or uploads its gifting list on its watch page (/digest/<token>): handle or profile URL, platform, ship
date, and whether its own tracking (tag or discount code) already caught a post. Every weekly run then looks for each
unposted gift's first video mentioning the brand after it shipped: first in that run's brand search (no extra cost),
then on the creator's own videos: TikTok from the public page (free), and at most ORIANE_READS lookups through Oriane
per run (Instagram creators, or TikTok pages that can't be read). An upload's own check never spends Oriane credits.
Posts found are added to the run's results, so they get receipts and licence buttons like any other mention.

The headline number is research test 4's: posters found here versus what the brand's own tracking had (Gate 5: 2x).
"""
import csv
import io
import re
import traceback
from datetime import date, timedelta

import creator
import demo
import server
from server import HANDLE_RE, ApiError, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS seeding_gifts (
  id           serial PRIMARY KEY,
  digest_id    int  NOT NULL REFERENCES digests ON DELETE CASCADE,
  platform     text NOT NULL,
  handle       text NOT NULL,                 -- lowercase, no @
  shipped_on   date NOT NULL,
  tracked      boolean NOT NULL DEFAULT false, -- the brand's own tracking (tag or discount code) already caught a post
  video_id     text,                          -- the first post mentioning the brand after shipping
  kind         text,                          -- spoken | tagged | sponsored
  posted_on    date,
  video_url    text,
  quote        text,
  found_at     timestamptz,
  checked_at   timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (digest_id, platform, handle, shipped_on)
);
"""

MAX_GIFTS = 500
WINDOW_DAYS = 90                    # stop looking this long after shipping
TIKTOK_READS = 15                   # creators read from TikTok's public page per check (free, but slow)
ORIANE_READS = 5                    # creators looked up through Oriane per weekly run (a search, plus a profile lookup on a miss)
POSTED = ("spoken", "tagged", "sponsored")
PLATFORM_WORDS = {"tiktok": "tiktok", "tt": "tiktok", "instagram": "instagram", "ig": "instagram", "insta": "instagram"}
YES = {"yes", "y", "true", "1", "tracked", "posted", "x"}


# ---------------------------------------------------------------- the gifting list

def parse(text, default_platform):
    """Rows from a pasted or uploaded list: (platform, handle, shipped_on, tracked), plus skipped lines with a reason.
    Each line needs a handle or profile URL; platform comes from the URL, a column, or the watch's platform; the ship
    date is YYYY-MM-DD (today when missing); a yes/tracked column marks posts the brand's own tracking already caught."""
    rows, skipped = [], []
    for n, cells in enumerate(csv.reader(io.StringIO(text.replace("\t", ",").replace(";", ","))), 1):
        cells = [c.strip() for c in cells if c.strip()]
        if not cells or (n == 1 and any(c.lower() in ("handle", "creator", "username", "account") for c in cells)):
            continue
        platform = handle = shipped = None
        tracked = False
        for c in cells:
            low = c.lower()
            url = creator.HANDLE_URL_RE.search(c)
            if url:
                platform, handle = url.group(1).lower(), url.group(2).lower()
            elif low in PLATFORM_WORDS:
                platform = PLATFORM_WORDS[low]
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", c):
                try:
                    shipped = date.fromisoformat(c)
                except ValueError:
                    pass
            elif low in YES:
                tracked = True
            elif low in ("no", "n", "false", "0"):
                pass
            elif not handle and HANDLE_RE.fullmatch(c.lstrip("@")):
                handle = c.lstrip("@").lower()
        platform = platform or (default_platform if default_platform in ("tiktok", "instagram") else None)
        if not handle:
            skipped.append({"line": n, "reason": "No handle or profile URL."})
        elif not platform:
            skipped.append({"line": n, "reason": "Say tiktok or instagram (this watch covers both)."})
        elif shipped and shipped > date.today():
            skipped.append({"line": n, "reason": "Ship date is in the future."})
        else:
            rows.append((platform, handle, shipped or date.today(), tracked))
    return rows, skipped


def upload(d, body):
    """Add gifts to a watch (duplicates update the tracked flag), then do a first free check right away."""
    text = str(body.get("text") or "")
    if not text.strip():
        raise ApiError(400, "Paste or upload your gifting list: one creator per line.")
    if len(text) > 200_000:
        raise ApiError(413, "That list is too long: up to %d creators per watch." % MAX_GIFTS)
    rows, skipped = parse(text, d["params"].get("platform"))
    with connect() as db:
        have = db.execute("SELECT count(*) AS n FROM seeding_gifts WHERE digest_id = %s", (d["id"],)).fetchone()["n"]
        if have + len(rows) > MAX_GIFTS:
            raise ApiError(409, "A watch holds up to %d gifted creators; you'd have %d." % (MAX_GIFTS, have + len(rows)))
        with db.cursor() as cur:
            cur.executemany("INSERT INTO seeding_gifts (digest_id, platform, handle, shipped_on, tracked) VALUES (%s, %s, %s, %s, %s)"
                            " ON CONFLICT (digest_id, platform, handle, shipped_on) DO UPDATE SET tracked = excluded.tracked",
                            [(d["id"], *r) for r in rows])
    creator.track(d["user_id"], "seeding_upload", {"digest": d["id"], "rows": len(rows), "skipped": len(skipped)})
    check(d, oriane=False)          # TikTok's public pages are free: show what's there now
    return {"added": len(rows), "skipped": skipped, "seeding": summary(d)}


def clear(d):
    with connect() as db:
        db.execute("DELETE FROM seeding_gifts WHERE digest_id = %s", (d["id"],))
    return {"seeding": summary(d)}


# ---------------------------------------------------------------- finding the posts

def mark(gift, v, kind, quote):
    with connect() as db:
        db.execute("UPDATE seeding_gifts SET video_id = %s, kind = %s, posted_on = %s, video_url = %s, quote = %s, found_at = now()"
                   " WHERE id = %s AND video_id IS NULL",
                   (v["id"], kind, (v.get("publishedAt") or "")[:10] or None, v["url"], (quote or "")[:300] or None, gift["id"]))


def read(g, budget):
    """A gifted creator's recent videos, or None when this check has no reads left for them (they go first next time).
    TikTok's public page is free; Oriane is only used while `budget["oriane"]` lasts, never on an unreadable page alone."""
    if demo.active():
        return demo.creator_videos(g["platform"], g["handle"])
    if g["platform"] == "tiktok":
        if budget["public"] <= 0:
            return None
        budget["public"] -= 1
        videos = creator.public_videos("tiktok", g["handle"])
        if videos:
            return videos
    if budget["oriane"] <= 0:
        return None
    budget["oriane"] -= 1
    return creator.fetch_videos(g["platform"], g["handle"], plan="pro")[0]   # Oriane first: the public page was just tried


def check(d, sid=None, oriane=True):
    """Look for posts by this watch's unposted gifts. `sid` is a weekly run's search (found posts are added to it, so
    they get licence buttons). Returns the gifts found posting in this check."""
    brand, variants = d["brand"], d["params"].get("variants") or []
    about = d["params"].get("about", ())
    with connect() as db:
        open_gifts = db.execute("SELECT * FROM seeding_gifts WHERE digest_id = %s AND video_id IS NULL AND shipped_on > %s"
                                " ORDER BY checked_at NULLS FIRST, id", (d["id"], date.today() - timedelta(days=WINDOW_DAYS))).fetchall()
    if not open_gifts:
        return []
    found = []
    # 1. The run's own brand search: free.
    if sid:
        for v in sorted(server.load(sid)["videos"], key=lambda v: v.get("publishedAt") or ""):
            for g in open_gifts:
                if g["id"] not in found and v["platform"] == g["platform"] and v["handle"].lower() == g["handle"] \
                        and v["kind"] in POSTED and (v.get("publishedAt") or "")[:10] >= g["shipped_on"].isoformat():
                    mark(g, v, v["kind"], (v.get("quote") or {}).get("text"))
                    found.append(g["id"])
    # 2. The creator's own videos (read() keeps Oriane spending inside this check's budget).
    budget = {"public": TIKTOK_READS, "oriane": ORIANE_READS if oriane else 0}
    held = []                                # posts found outside a weekly run (the upload's own check)
    for g in open_gifts:
        if g["id"] in found:
            continue
        try:
            videos = read(g, budget)
        except ApiError:
            videos = []
        except Exception:                   # one unreadable creator mustn't stop the others
            traceback.print_exc()
            videos = []
        if videos is None:
            continue
        posts = []
        for raw in videos:
            if (raw.get("publishedAt") or "")[:10] < g["shipped_on"].isoformat():
                continue
            kind, _, q = server.classify(raw, brand, variants, about)
            if kind in POSTED:
                posts.append((raw, kind, q))
        if posts:
            raw, kind, q = min(posts, key=lambda p: p[0].get("publishedAt") or "")
            if sid:
                server.store_results(sid, [raw], brand, variants, about)
            else:
                held.append(raw)
            mark(g, {"id": raw["id"], "publishedAt": raw.get("publishedAt"), "url": server.post_url(raw)}, kind, (q or {}).get("text"))
            found.append(g["id"])
        with connect() as db:
            db.execute("UPDATE seeding_gifts SET checked_at = now() WHERE id = %s", (g["id"],))
    if held:                                 # kept under this watch so their licence links work; already shown, so seen
        hold = server.new_search(brand, {**d["params"], "digest": d["id"], "seeding": True}, server.EMPTY_PAGE)
        server.store_results(hold, held, brand, variants, about)
        with connect() as db:
            db.execute("INSERT INTO digest_seen (digest_id, video_id) SELECT %s, video_id FROM mentions WHERE search_id = %s"
                       " ON CONFLICT DO NOTHING", (d["id"], hold))
    if found:
        creator.track(d["user_id"], "seeding_found", {"digest": d["id"], "found": len(found)})
    with connect() as db:
        return db.execute("SELECT * FROM seeding_gifts WHERE id = ANY(%s) ORDER BY posted_on", (found,)).fetchall()


# ---------------------------------------------------------------- the numbers

def summary(d):
    """Counts for the watch page and the email; Gate 5 compares `posted` with `tracked`."""
    import digest
    with connect() as db:
        gifts = db.execute("SELECT * FROM seeding_gifts WHERE digest_id = %s ORDER BY video_id IS NULL, posted_on DESC NULLS LAST, id",
                           (d["id"],)).fetchall()
    posted = [g for g in gifts if g["video_id"]]
    tracked = sum(g["tracked"] for g in gifts)
    return {"gifted": len(gifts), "posted": len(posted), "spokenOnly": sum(g["kind"] == "spoken" for g in posted),
            "tracked": tracked, "untrackedPosts": sum(not g["tracked"] for g in posted),
            "multiple": round(len(posted) / tracked, 1) if tracked else None,
            "gifts": [{"platform": g["platform"], "handle": g["handle"], "shippedOn": g["shipped_on"], "tracked": g["tracked"],
                       "kind": g["kind"], "postedOn": g["posted_on"], "url": g["video_url"], "quote": g["quote"],
                       "checkedAt": g["checked_at"], "videoId": g["video_id"],
                       "licenseUrl": digest.license_url(d["token"], g["video_id"]) if g["video_id"] and g["kind"] != "sponsored" else None,
                       "collabUrl": digest.collab_url(d["token"], g["video_id"]) if g["video_id"] and g["kind"] != "sponsored" else None}
                      for g in gifts]}


def email_section(d, found, collab_url):
    """The seeding block for the weekly email: the running tally and the gifted creators found posting this week."""
    import digest
    s = summary(d)
    if not s["gifted"]:
        return ""
    e, font = digest.esc, digest.FONT
    rows = "".join(
        '<tr><td style="padding:8px 0;border-top:1px solid #e6ebe8;font:14px %s;color:#1b2a23;"><b>@%s</b> '
        '<span style="color:#6b7a73;">&middot; %s &middot; posted %s</span>%s%s</td></tr>' % (
            font, e(g["handle"]), "said it on camera only" if g["kind"] == "spoken" else "tagged" if g["kind"] == "tagged" else "disclosed",
            e("%d %s" % (g["posted_on"].day, g["posted_on"].strftime("%b")) if g["posted_on"] else ""),
            ' <a href="%s" style="color:#2f6b4f;">Watch &rarr;</a>' % e(g["video_url"]) if g["video_url"] else "",
            ' <a href="%s" style="color:#2f6b4f;">Invite to collab &rarr;</a>' % e(collab_url(g["video_id"])) if g["kind"] != "sponsored" else "")
        for g in found)
    tally = "%d of %d gifted creators have posted%s%s." % (
        s["posted"], s["gifted"], ", %d only on camera (no tag to track)" % s["spokenOnly"] if s["spokenOnly"] else "",
        "; your own tracking had %d" % s["tracked"] if s["tracked"] else "")
    return ('<tr><td style="padding-top:20px;font:600 16px %s;color:#1b2a23;">Gifted creators</td></tr>'
            '<tr><td style="padding:4px 0 6px;font:14px %s;color:#48564f;">%s%s</td></tr>%s' % (
                font, font, e(tally), " New this week:" if found else "", rows))


# ---------------------------------------------------------------- operator (/admin/)

def admin_list():
    with connect() as db:
        rows = db.execute("SELECT d.id, d.brand, d.email, count(g.id) AS gifted, count(g.video_id) AS posted,"
                          " count(*) FILTER (WHERE g.kind = 'spoken') AS spoken_only, count(*) FILTER (WHERE g.tracked) AS tracked"
                          " FROM digests d JOIN seeding_gifts g ON g.digest_id = d.id GROUP BY d.id ORDER BY d.id DESC").fetchall()
    return {"watches": [{"brand": r["brand"], "email": r["email"], "gifted": r["gifted"], "posted": r["posted"],
                         "spokenOnly": r["spoken_only"], "tracked": r["tracked"],
                         "multiple": round(r["posted"] / r["tracked"], 1) if r["tracked"] else None} for r in rows]}


def admin_route(handler):
    import digest
    digest.require_operator(handler)
    if handler.command == "GET":
        return admin_list()
    raise ApiError(405, "Method not allowed.")
