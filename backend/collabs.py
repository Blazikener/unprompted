"""Future paid collaborations between brands and creators.

A past on-camera mention is the proof; what gets agreed here is new work. Either side starts one:
- a brand invites a creator who already talks about it (from its dashboard results or a weekly report link),
- a creator pitches a brand that is open to pitches on Unprompted (a brand account's profile).
A proposal names the deliverables, the timing, the budget and a brief. The other side accepts, counters (a new budget
and a note) or declines; a counter goes back to the first side. Each side has a link that works without signing in
(/collab/<token>) and an inbox at /collabs. Nothing is charged here.

Creators also keep a watchlist of brands they want to work with; a Monday email lists the collabs waiting for them and
which watched brands are open to pitches. No Oriane credits are spent anywhere in this module.
"""
import re
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

from psycopg.types.json import Jsonb

import creator
import digest
import licenses
from server import ApiError, connect, post_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS brand_profiles (       -- a brand account's own brand, and whether creators may pitch it
  user_id    int PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  brand      text NOT NULL,
  about      text,
  open       boolean NOT NULL DEFAULT true,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS collabs (
  id              serial PRIMARY KEY,
  origin          text NOT NULL,                  -- brand (the brand invited) | creator (the creator pitched)
  brand           text NOT NULL,
  brand_user_id   int REFERENCES users ON DELETE SET NULL,
  brand_email     text NOT NULL,
  digest_id       int REFERENCES digests ON DELETE SET NULL,
  creator_user_id int REFERENCES users ON DELETE SET NULL,
  platform        text NOT NULL,
  handle          text NOT NULL,                  -- lowercase, no @
  proof           jsonb,                          -- the receipt behind it: {url, quote, views, publishedAt, kind}
  deliverables    text NOT NULL,
  timing          text NOT NULL,
  budget_usd      int  NOT NULL,                  -- the current offer; a counter replaces it
  brief           text,
  note            text,                           -- the last counter's note
  decline_reason  text,
  status          text NOT NULL DEFAULT 'sent',   -- sent | countered | accepted | declined | withdrawn
  last_by         text NOT NULL,                  -- brand | creator: who moved last; the other side answers
  creator_token   text UNIQUE NOT NULL DEFAULT replace(gen_random_uuid()::text, '-', ''),
  brand_token     text UNIQUE NOT NULL DEFAULT replace(gen_random_uuid()::text, '-', ''),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS collabs_open ON collabs (lower(brand), platform, handle) WHERE status IN ('sent', 'countered');
CREATE TABLE IF NOT EXISTS creator_watch (        -- brands a creator wants to work with
  user_id    int  NOT NULL REFERENCES users ON DELETE CASCADE,
  brand      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, brand)
);
CREATE TABLE IF NOT EXISTS collab_weekly (        -- the creators' Monday email: when it went out, which open brands it named
  user_id  int PRIMARY KEY REFERENCES users ON DELETE CASCADE,
  sent_at  timestamptz NOT NULL,
  open_seen text[] NOT NULL DEFAULT '{}'
);
"""

STATUSES = ("sent", "countered", "accepted", "declined", "withdrawn")
OPEN = ("sent", "countered")
MAX_WATCH = 25
BRAND_PER_DAY = 20
CREATOR_PER_WEEK = 10
WEEKLY_EVERY = timedelta(days=7) - timedelta(hours=1)
LABEL = {"sent": "Waiting for an answer", "countered": "Counter-offer", "accepted": "Booked", "declined": "Declined",
         "withdrawn": "Withdrawn"}


def url(token):
    return "%s/collab/%s" % (digest.app_url(), token)


def inbox_url():
    return "%s/collabs" % digest.app_url()


def brand_key(name):
    return re.sub(r"\s+", " ", str(name or "").strip())[:60]


def text(body, key, lo, hi, message):
    value = re.sub(r"[ \t]+", " ", str(body.get(key) or "")).strip()
    if not lo <= len(value) <= hi:
        raise ApiError(400, message)
    return value


def money(value, message="Budget is whole dollars, from $50."):
    if not isinstance(value, int) or isinstance(value, bool) or not 50 <= value <= 1_000_000:
        raise ApiError(400, message)
    return value


def terms(body):
    brief = str(body.get("brief") or "").strip()[:1000] or None
    return (text(body, "deliverables", 3, 200, "Say what gets made, e.g. 2 TikToks + 1 story."),
            text(body, "timing", 2, 120, "Say when it posts, e.g. in June."),
            money(body.get("budgetUsd")), brief)


# ---------------------------------------------------------------- brand profile (who creators can pitch)

def profile(user):
    with connect() as db:
        p = db.execute("SELECT * FROM brand_profiles WHERE user_id = %s", (user["id"],)).fetchone()
    return {"brand": p["brand"], "about": p["about"], "open": p["open"]} if p else None


def set_profile(user, body):
    brand = brand_key(body.get("brand"))
    if len(brand) < 2:
        raise ApiError(400, "Enter your brand's name.")
    about = str(body.get("about") or "").strip()[:120] or None
    with connect() as db:
        db.execute("INSERT INTO brand_profiles (user_id, brand, about, open) VALUES (%s, %s, %s, %s) ON CONFLICT (user_id)"
                   " DO UPDATE SET brand = excluded.brand, about = excluded.about, open = excluded.open, updated_at = now()",
                   (user["id"], brand, about, body.get("open") is not False))
    creator.track(user["id"], "brand_profile", {"brand": brand, "open": body.get("open") is not False})
    return profile(user)


def partners():
    """Brands open to pitches: one row per brand name (the newest profile wins)."""
    with connect() as db:
        rows = db.execute("SELECT DISTINCT ON (lower(p.brand)) p.brand, p.about, p.user_id, u.email, u.role FROM brand_profiles p"
                          " JOIN users u ON u.id = p.user_id WHERE p.open"
                          " ORDER BY lower(p.brand), p.updated_at DESC").fetchall()
    return [r for r in rows if creator.has_role(r, "brand")]


def partner(name):
    return next((p for p in partners() if p["brand"].lower() == brand_key(name).lower()), None)


# ---------------------------------------------------------------- proof: the receipt that makes the ask credible

def video_proof(video_id, platform, handle):
    with connect() as db:
        row = db.execute("SELECT v.raw, m.kind, m.quote FROM videos v LEFT JOIN mentions m ON m.video_id = v.id"
                         " WHERE v.id = %s ORDER BY m.search_id DESC NULLS LAST LIMIT 1", (video_id,)).fetchone()
    if not row:
        return None
    raw = row["raw"] or {}
    if (raw.get("platform") or "").lower() != platform or (raw.get("profileHandle") or "").lower() != handle:
        raise ApiError(400, "That video isn't by @%s." % handle)
    return {"url": post_url(raw), "quote": row["quote"], "views": raw.get("viewsCount") or 0,
            "publishedAt": raw.get("publishedAt"), "kind": row["kind"]}


def scan_proof(user, scan_id, brand):
    """The creator's best receipt for `brand` from one of their own scans, and the scanned handle."""
    with connect() as db:
        scan = db.execute("SELECT platform, handle, brands FROM scans WHERE id = %s AND user_id = %s", (scan_id, user["id"])).fetchone()
    if not scan:
        raise ApiError(404, "Scan not found.")
    row = next((b for b in scan["brands"] or [] if b.get("brand", "").lower() == brand.lower()), None)
    receipts = sorted((row or {}).get("receipts") or [], key=lambda r: -(r.get("views") or 0))
    best = receipts[0] if receipts else None
    proof = {"url": best.get("url"), "quote": best.get("quote"), "views": best.get("views") or 0,
             "publishedAt": best.get("publishedAt"), "kind": best.get("kind"), "mentions": len(receipts)} if best else None
    return scan["platform"], scan["handle"].lower(), proof


# ---------------------------------------------------------------- starting a collab

def creator_owner(platform, handle):
    with connect() as db:
        return db.execute("SELECT u.* FROM creator_handles h JOIN users u ON u.id = h.user_id WHERE h.platform = %s"
                          " AND h.handle = %s AND h.verified_at IS NOT NULL", (platform, handle)).fetchone()


def insert(row):
    cols = list(row)
    try:
        with connect() as db:
            return db.execute("INSERT INTO collabs (%s) VALUES (%s) RETURNING *" % (", ".join(cols), ", ".join(["%s"] * len(cols))),
                              [row[c] for c in cols]).fetchone()
    except Exception as e:
        if "collabs_open" in str(e):
            raise ApiError(409, "There's already an open collab between %s and @%s." % (row["brand"], row["handle"]))
        raise


def jsonb(value):
    return Jsonb(value) if value is not None else None


def propose(user, body, d=None):
    """A brand invites a creator: signed in with a brand account, or from its weekly report link (`d`, the digest)."""
    vid = str(body.get("videoId") or "")[:64]
    if d is not None:
        video = digest.license_video(d, vid)               # from a report link: only creators in this brand's reports
        platform, handle = video["platform"], video["handle"].lower()
        proof = {k: video[k] for k in ("url", "quote", "views", "publishedAt", "kind")}
    else:
        platform, handle = licenses.norm(body.get("platform"), body.get("handle"))
        proof = video_proof(vid, platform, handle) if vid else None
    deliverables, timing, budget, brief = terms(body)
    if d is not None:
        brand, email, brand_user_id, digest_id = d["brand"], d["email"], d["user_id"], d["id"]
    else:
        prof = profile(user)
        brand = brand_key(body.get("brand")) or (prof or {}).get("brand") or ""
        if len(brand) < 2:
            raise ApiError(400, "Say which brand the collab is for.")
        email, brand_user_id, digest_id = user["email"], user["id"], None
    with connect() as db:
        sent = db.execute("SELECT count(*) AS n FROM collabs WHERE origin = 'brand' AND lower(brand_email) = lower(%s)"
                          " AND created_at > now() - interval '1 day'", (email,)).fetchone()["n"]
    if sent >= BRAND_PER_DAY:
        raise ApiError(429, "That's %d proposals today. Send more tomorrow." % BRAND_PER_DAY)
    owner = creator_owner(platform, handle)
    c = insert({"origin": "brand", "brand": brand, "brand_user_id": brand_user_id, "brand_email": email, "digest_id": digest_id,
                "creator_user_id": owner["id"] if owner else None, "platform": platform, "handle": handle,
                "proof": jsonb(proof), "deliverables": deliverables, "timing": timing, "budget_usd": budget, "brief": brief,
                "last_by": "brand"})
    creator.track(brand_user_id, "collab_propose", {"collab": c["id"], "brand": brand, "budget": budget})
    tell(c, "proposed")
    return view(c, "brand")


def pitch(user, body):
    """A creator pitches a brand that's open to pitches, with a receipt from their own scan."""
    p = partner(body.get("brand"))
    if not p:
        raise ApiError(404, "That brand isn't taking pitches on Unprompted yet. Add it to your watchlist and we'll tell you when it is.")
    deliverables, timing, budget, brief = terms(body)
    scan_id = body.get("scanId")
    if isinstance(scan_id, int) and not isinstance(scan_id, bool):
        platform, handle, proof = scan_proof(user, scan_id, p["brand"])
    else:
        platform, handle = licenses.norm(body.get("platform"), body.get("handle"))
        proof = None
    with connect() as db:
        sent = db.execute("SELECT count(*) AS n FROM collabs WHERE origin = 'creator' AND creator_user_id = %s"
                          " AND created_at > now() - interval '7 days'", (user["id"],)).fetchone()["n"]
    if sent >= CREATOR_PER_WEEK:
        raise ApiError(429, "That's %d pitches this week. Send more next week." % CREATOR_PER_WEEK)
    c = insert({"origin": "creator", "brand": p["brand"], "brand_user_id": p["user_id"], "brand_email": p["email"],
                "creator_user_id": user["id"], "platform": platform, "handle": handle, "proof": jsonb(proof),
                "deliverables": deliverables, "timing": timing, "budget_usd": budget, "brief": brief, "last_by": "creator"})
    creator.track(user["id"], "collab_pitch", {"collab": c["id"], "brand": p["brand"], "budget": budget})
    tell(c, "pitched")
    return view(c, "creator")


# ---------------------------------------------------------------- answering

def load_token(token):
    with connect() as db:
        c = db.execute("SELECT * FROM collabs WHERE creator_token = %s OR brand_token = %s", (token, token)).fetchone()
    if not c:
        raise ApiError(404, "This collab link isn't valid.")
    return c, "creator" if c["creator_token"] == token else "brand"


def load_mine(user, cid):
    with connect() as db:
        c = db.execute("SELECT * FROM collabs WHERE id = %s", (cid,)).fetchone()
    side = side_of(user, c) if c else None
    if not side:
        raise ApiError(404, "Collab not found.")
    return c, side


def side_of(user, c):
    if c["brand_user_id"] == user["id"] and creator.has_role(user, "brand"):
        return "brand"
    if creator.has_role(user, "creator") and (c["creator_user_id"] == user["id"] or owns(user, c)):
        return "creator"
    return None


def owns(user, c):
    owner = creator_owner(c["platform"], c["handle"])
    return bool(owner and owner["id"] == user["id"])


def act(c, side, body):
    """accept | counter {budgetUsd, note} | decline {reason} answer the other side's last move; withdraw takes back your own."""
    action = body.get("action")
    if action not in ("accept", "counter", "decline", "withdraw"):
        raise ApiError(400, "Unknown action.")
    if c["status"] not in OPEN:
        raise ApiError(409, "This collab is already %s." % LABEL[c["status"]].lower())
    if action == "withdraw":
        if c["last_by"] != side:
            raise ApiError(409, "It's your turn to answer: accept, counter or decline.")
        sql, args = "status = 'withdrawn'", ()
    elif c["last_by"] == side:
        raise ApiError(409, "Waiting for the other side to answer.")
    elif action == "accept":
        sql, args = "status = 'accepted'", ()
    elif action == "counter":
        budget = money(body.get("budgetUsd"))
        if budget == c["budget_usd"]:
            raise ApiError(400, "A counter needs a different budget. To agree, accept.")
        sql, args = "status = 'countered', budget_usd = %s, note = %s", (budget, str(body.get("note") or "").strip()[:500] or None)
    else:
        sql, args = "status = 'declined', decline_reason = %s", (str(body.get("reason") or "").strip()[:300] or None,)
    with connect() as db:
        c = db.execute("UPDATE collabs SET " + sql + ", last_by = %s, updated_at = now() WHERE id = %s AND updated_at = %s RETURNING *",
                       (*args, side, c["id"], c["updated_at"])).fetchone()
    if not c:
        raise ApiError(409, "This collab just changed. Reload to see the latest.")
    creator.track(c["brand_user_id"] if side == "brand" else c["creator_user_id"], "collab_" + action,
                  {"collab": c["id"], "budget": c["budget_usd"]})
    tell(c, {"accept": "accepted", "counter": "countered", "decline": "declined", "withdraw": "withdrew"}[action], side)
    return view(c, side)


# ---------------------------------------------------------------- views

def view(c, side):
    yours = c["status"] in OPEN and c["last_by"] != side
    return {"id": c["id"], "side": side, "origin": c["origin"], "brand": c["brand"], "platform": c["platform"], "handle": c["handle"],
            "profile": digest.profile_url(c["platform"], c["handle"]), "proof": c["proof"], "deliverables": c["deliverables"],
            "timing": c["timing"], "budgetUsd": c["budget_usd"], "brief": c["brief"], "note": c["note"],
            "declineReason": c["decline_reason"], "status": c["status"], "label": LABEL[c["status"]], "lastBy": c["last_by"],
            "yourTurn": yours, "canWithdraw": c["status"] in OPEN and c["last_by"] == side,
            "url": url(c["creator_token"] if side == "creator" else c["brand_token"]),
            "createdAt": c["created_at"], "updatedAt": c["updated_at"]}


def mine(user, as_role=None):
    """The inbox for a creator (by account or verified handle) or a brand account, newest first."""
    sides = [r for r in creator.roles_of(user) if r in ("creator", "brand")]
    role = as_role if as_role in sides else next(iter(sides), user["role"])
    if role == "brand":
        sql, args = "brand_user_id = %s", (user["id"],)
        side = "brand"
    elif role == "creator":
        sql = ("creator_user_id = %s OR (platform, handle) IN (SELECT platform, handle FROM creator_handles"
               " WHERE user_id = %s AND verified_at IS NOT NULL)")
        args, side = (user["id"], user["id"]), "creator"
    else:
        raise ApiError(403, "Collabs are for creator and brand accounts.")
    with connect() as db:
        rows = db.execute("SELECT * FROM collabs WHERE " + sql + " ORDER BY updated_at DESC LIMIT 100", args).fetchall()
    items = [view(c, side) for c in rows]
    out = {"role": role, "roles": sides, "collabs": items,
           "counts": {"yourTurn": sum(i["yourTurn"] for i in items), "waiting": sum(i["status"] in OPEN and not i["yourTurn"] for i in items),
                      "booked": sum(i["status"] == "accepted" for i in items),
                      "bookedUsd": sum(i["budgetUsd"] for i in items if i["status"] == "accepted")}}
    if side == "brand":
        out["profile"] = profile(user)
    else:
        out["watch"] = watchlist(user)
        out["partners"] = [{"brand": p["brand"], "about": p["about"]} for p in partners()]
    return out


# ---------------------------------------------------------------- the creator's watchlist

def watchlist(user):
    with connect() as db:
        rows = db.execute("SELECT brand FROM creator_watch WHERE user_id = %s ORDER BY created_at", (user["id"],)).fetchall()
    open_ = {p["brand"].lower() for p in partners()}
    return [{"brand": r["brand"], "open": r["brand"].lower() in open_} for r in rows]


def watch(user, body):
    brand = brand_key(body.get("brand"))
    if len(brand) < 2:
        raise ApiError(400, "Enter a brand name.")
    with connect() as db:
        if body.get("remove"):
            db.execute("DELETE FROM creator_watch WHERE user_id = %s AND lower(brand) = lower(%s)", (user["id"], brand))
        else:
            n = db.execute("SELECT count(*) AS n FROM creator_watch WHERE user_id = %s", (user["id"],)).fetchone()["n"]
            exists = db.execute("SELECT 1 FROM creator_watch WHERE user_id = %s AND lower(brand) = lower(%s)", (user["id"], brand)).fetchone()
            if not exists:
                if n >= MAX_WATCH:
                    raise ApiError(429, "You're watching %d brands. Remove one first." % MAX_WATCH)
                db.execute("INSERT INTO creator_watch (user_id, brand) VALUES (%s, %s)", (user["id"], brand))
    creator.track(user["id"], "watch_brand", {"brand": brand, "remove": bool(body.get("remove"))})
    return {"watch": watchlist(user)}


# ---------------------------------------------------------------- mail

def tell(c, what, by=None):
    """Email the side that has to act next (or both, when it's settled). A creator without a verified handle has no
    address: the operator gets their link to pass on."""
    to_creator = by != "creator"
    to_brand = by != "brand"
    if what in ("proposed",):
        to_brand = False
    if what in ("pitched",):
        to_creator = False
    rows = [("Brand", c["brand"]), ("Creator", "@%s on %s" % (c["handle"], c["platform"])), ("Makes", c["deliverables"]),
            ("When", c["timing"]), ("Budget", "$%s" % format(c["budget_usd"], ",")), ("Brief", c["brief"] or "-")]
    if c["note"] and what == "countered":
        rows.append(("Note", c["note"]))
    subject = {"proposed": "%s wants to collab with you: $%s" % (c["brand"], format(c["budget_usd"], ",")),
               "pitched": "@%s pitched a collab for %s" % (c["handle"], c["brand"]),
               "accepted": "Collab booked: %s x @%s" % (c["brand"], c["handle"]),
               "countered": "Counter-offer: $%s for %s x @%s" % (format(c["budget_usd"], ","), c["brand"], c["handle"]),
               "declined": "Collab declined: %s x @%s" % (c["brand"], c["handle"]),
               "withdrew": "Collab withdrawn: %s x @%s" % (c["brand"], c["handle"])}[what]
    lead = "Accept, counter or decline from the link. No account needed." if what in ("proposed", "pitched", "countered") else \
        "Agree the details directly; Unprompted doesn't take payment for collabs yet." if what == "accepted" else ""
    if to_brand:
        licenses.notify(subject, lead, rows + [("Open", url(c["brand_token"]))], to=c["brand_email"])
    if to_creator:
        owner = creator_owner(c["platform"], c["handle"])
        email = owner["email"] if owner else None
        if not email and c["creator_user_id"]:
            with connect() as db:
                u = db.execute("SELECT email FROM users WHERE id = %s", (c["creator_user_id"],)).fetchone()
            email = u["email"] if u else None
        if email:
            licenses.notify(subject, lead, rows + [("Open", url(c["creator_token"]))], to=email)
        else:
            licenses.notify("Pass on to @%s: %s" % (c["handle"], subject),
                            "This creator hasn't claimed their handle. Send them the link below.",
                            rows + [("Creator link", url(c["creator_token"]))])


def weekly(user, force=False):
    """(subject, html, open brands named) of a creator's Monday email, or None when nothing needs them."""
    inbox = mine(user)
    turn = [i for i in inbox["collabs"] if i["yourTurn"]]
    with connect() as db:
        last = db.execute("SELECT open_seen FROM collab_weekly WHERE user_id = %s", (user["id"],)).fetchone()
    seen = set(last["open_seen"]) if last else set()
    opened = [w["brand"] for w in inbox["watch"] if w["open"] and w["brand"].lower() not in seen]
    if not (turn or opened):
        return None
    bits = (["%d collab%s waiting for you" % (len(turn), "" if len(turn) == 1 else "s")] if turn else []) + \
           (["%s now taking pitches" % ", ".join(opened[:3])] if opened else [])
    e, font = digest.esc, digest.FONT

    def row(title, note, link, cta):
        return ('<tr><td style="padding:10px 0;border-top:1px solid #e6ebe8;font:14px %s;color:#1b2a23;"><b>%s</b> &middot; %s'
                ' <a href="%s" style="margin-left:8px;color:#2f6b4f;font-weight:600;">%s &rarr;</a></td></tr>'
                % (font, e(title), e(note), e(link), cta))
    sections = []
    if turn:
        sections.append(("Your turn", "".join(row(i["brand"], "$%s &middot; %s" % (format(i["budgetUsd"], ","), i["deliverables"]),
                                                  i["url"], "Answer") for i in turn)))
    if opened:
        sections.append(("Brands you watch, now taking pitches",
                         "".join(row(b, "pitch them with your receipts", inbox_url() + "?pitch=" + b, "Pitch") for b in opened)))
    subject = "This week: " + ", ".join(bits)
    body = ('<!doctype html><html><body style="margin:0;padding:24px 12px;background:#f3f6f4;">'
            '<table role="presentation" width="100%%" style="max-width:560px;margin:0 auto;background:#fff;border-radius:16px;padding:24px 26px;">'
            '<tr><td style="font:500 12px %s;letter-spacing:.08em;text-transform:uppercase;color:#6b7a73;">Receipts &middot; Monday</td></tr>'
            '<tr><td style="padding:8px 0 4px;font:600 22px/1.25 %s;color:#1b2a23;">%s</td></tr>%s'
            '<tr><td style="padding-top:18px;font:12px/1.6 %s;color:#6b7a73;">Your collabs and watchlist: <a href="%s" style="color:#2f6b4f;">%s</a>.'
            '</td></tr></table></body></html>' % (
                font, font, e(", ".join(bits).capitalize()),
                "".join('<tr><td style="padding-top:14px;font:600 13px %s;color:#1b2a23;">%s</td></tr>%s' % (font, e(t), r) for t, r in sections),
                font, e(inbox_url()), e(inbox_url())))
    return subject, body, [w["brand"].lower() for w in inbox["watch"] if w["open"]]


def run_weekly(force=False):
    """Monday email to every creator with a collab waiting on them or a watched brand that just opened to pitches."""
    with connect() as db:
        users = db.execute(
            "SELECT DISTINCT u.* FROM users u LEFT JOIN collab_weekly s ON s.user_id = u.id"
            " WHERE (u.id IN (SELECT user_id FROM creator_watch)"
            " OR u.id IN (SELECT creator_user_id FROM collabs WHERE status IN ('sent', 'countered'))"
            " OR u.id IN (SELECT h.user_id FROM creator_handles h JOIN collabs c ON c.platform = h.platform AND c.handle = h.handle"
            " WHERE h.verified_at IS NOT NULL AND c.status IN ('sent', 'countered')))"
            " AND (%s OR s.sent_at IS NULL OR s.sent_at < now() - %s)", (force, WEEKLY_EVERY)).fetchall()
    sent = 0
    for u in users:
        if not creator.has_role(u, "creator"):
            continue
        mail = weekly(u)
        if not mail:
            continue
        with connect() as db:
            claimed = db.execute("INSERT INTO collab_weekly (user_id, sent_at, open_seen) VALUES (%s, now(), %s) ON CONFLICT (user_id)"
                                 " DO UPDATE SET sent_at = now(), open_seen = excluded.open_seen"
                                 " WHERE %s OR collab_weekly.sent_at < now() - %s RETURNING user_id",
                                 (u["id"], mail[2], force, WEEKLY_EVERY)).fetchone()
        if claimed:
            digest.send(u["email"], mail[0], mail[1])
            creator.track(u["id"], "collab_weekly", {"subject": mail[0]})
            sent += 1
    return sent


# ---------------------------------------------------------------- routes

def dispatch(handler):
    """GET  /api/collabs/mine | /partners | /t/<token> | /proof?v=&platform=&handle= (brand) | /digest/<token>?v=
    POST /api/collabs/propose (brand) | /pitch (creator) | /watch (creator) | /profile (brand)
         /t/<token> (either side, no account) | /<id> (either side, signed in)
         /digest/<token> (a brand from its weekly report link)"""
    path, method = urlparse(handler.path).path.removeprefix("/api/collabs"), handler.command
    tok = re.fullmatch(r"/t/([0-9a-f]{32})", path)
    if method == "GET":
        if tok:
            c, side = load_token(tok.group(1))
            return view(c, side)
        if path == "/mine":
            return mine(creator.require_user(handler.headers), (parse_qs(urlparse(handler.path).query).get("as") or [None])[0])
        dg = re.fullmatch(r"/digest/([A-Za-z0-9_-]{16,32})", path)
        if dg:
            d, _, _ = digest.load_digest(dg.group(1))
            vid = (parse_qs(urlparse(handler.path).query).get("v") or [""])[0][:64]
            return {"brand": d["brand"], "video": digest.license_video(d, vid)}
        if path == "/proof":
            creator.require_role(handler.headers, "brand")
            q = parse_qs(urlparse(handler.path).query)
            platform, handle = licenses.norm((q.get("platform") or [""])[0], (q.get("handle") or [""])[0])
            return {"proof": video_proof((q.get("v") or [""])[0][:64], platform, handle)}
        if path == "/partners":
            return {"partners": [{"brand": p["brand"], "about": p["about"]} for p in partners()]}
        raise ApiError(404, "Not found.")
    if method != "POST":
        raise ApiError(405, "Method not allowed.")
    if not (handler.headers.get("Content-Type") or "").startswith("application/json"):
        raise ApiError(415, "Send application/json.")
    body = digest.read_json(handler)
    if tok:
        return act(*load_token(tok.group(1)), body)
    dg = re.fullmatch(r"/digest/([A-Za-z0-9_-]{16,32})", path)
    if dg:
        d, _, _ = digest.load_digest(dg.group(1))
        return propose(None, body, d)
    m = re.fullmatch(r"/(\d+)", path)
    if m:
        return act(*load_mine(creator.require_user(handler.headers), int(m.group(1))), body)
    if path == "/propose":
        return propose(creator.require_role(handler.headers, "brand"), body)
    if path == "/profile":
        return set_profile(creator.require_role(handler.headers, "brand"), body)
    if path == "/pitch":
        return pitch(creator.require_role(handler.headers, "creator"), body)
    if path == "/watch":
        return watch(creator.require_role(handler.headers, "creator"), body)
    raise ApiError(404, "Not found.")

