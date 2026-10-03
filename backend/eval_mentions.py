"""Arabic precision check (Phase 6, research test 6): does the mention classifier hold up on Arabic videos?

In /admin/ the operator collects Arabic videos for a set of GCC brands (one Oriane search per brand: Arabic
transcripts from the last year matching the brand's Latin name or an Arabic spelling, 40 credits each, under the daily
credit cap), labels a random sample of each by hand (does the video really mention the brand?), and reads precision and
recall of server.classify twice: with the Latin name only, and with the brand's Arabic spellings (brands.ARABIC) as
variants, the way a brand search adds them. Gate 6: 80%+ precision with the Arabic spellings on 100+ labelled videos.

The export is the fixture: rerun the numbers offline after changing the matcher or the spellings. It holds creators'
transcripts, so keep it out of the repository.

    python3 backend/eval_mentions.py arabic-check.jsonl
"""
import json
import os
import random
import re
import sys
from urllib.parse import urlparse

import brands
import server
from server import ApiError, Jsonb, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_items (             -- the Arabic check's hand-labelled videos (eval_mentions.py)
  id          serial PRIMARY KEY,
  brand       text NOT NULL,
  video_id    text NOT NULL,
  raw         jsonb NOT NULL,                       -- the Oriane fields classify reads
  label       text CHECK (label IN ('yes', 'no')),  -- does it really mention the brand? NULL: not labelled yet
  labelled_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (brand, video_id)
);
"""
# Everyday-word spellings (Talabat, Noon, Careem, Namshi, Shein) next to unambiguous ones, Latin-named and Arabic-named.
BRANDS = ["Talabat", "Noon", "Careem", "Namshi", "Shein", "Almarai", "Al Baik", "Starbucks", "McDonald's", "Samsung"]
PER_BRAND = 10
GATE_PRECISION = 0.80
GATE_N = 100
KEEP = ("id", "platform", "platformId", "profileHandle", "caption", "hashtags", "mentions", "coAuthors", "transcript",
        "transcriptChunks", "transcriptLanguage", "publishedAt", "viewsCount")
SAID = ("owned", "sponsored", "tagged", "spoken")    # the kinds that claim the brand is in the video


# ---------------------------------------------------------------- collecting and labelling

def collect(names=None, per_brand=PER_BRAND):
    """One Oriane search per brand not collected yet; a random sample of its results is kept for labelling. Stops at
    the first error (the daily credit cap, usually), keeping what it has."""
    with connect() as db:
        have = {r["brand"] for r in db.execute("SELECT DISTINCT brand FROM eval_items")}
    out = {"collected": {}, "skipped": [], "stopped": None}
    for name in names or BRANDS:
        if name in have:
            out["skipped"].append(name)
            continue
        spellings = brands.ARABIC.get(name, [])
        _, _, filters, _ = server.parse({"brand": name, "variants": ", ".join(spellings[:5]), "lang": "ar", "days": 365})
        try:
            results = server.oriane(filters, limit=100)["data"]["results"]
        except ApiError as e:
            out["stopped"] = str(e)
            break
        sample = random.Random(name).sample(results, min(per_brand, len(results)))
        with connect() as db, db.cursor() as cur:
            cur.executemany("INSERT INTO eval_items (brand, video_id, raw) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            [(name, v["id"], Jsonb({k: v.get(k) for k in KEEP})) for v in sample])
        out["collected"][name] = len(sample)
    return out


def item_view(r):
    """One video to label: where the brand might be (any spelling, everyday words included), or how it opens."""
    raw, terms = r["raw"], [r["brand"], *brands.ARABIC.get(r["brand"], [])]
    rx, text = server.mention_re(terms), raw.get("transcript") or ""
    snippets, end = [], 0
    for m in rx.finditer(text):
        if m.start() < end:
            continue
        a, end = max(0, m.start() - 120), min(len(text), m.end() + 120)
        snippets.append({"text": text[a:end], "hit": [m.start() - a, m.end() - a]})
        if len(snippets) == 3:
            break
    return {"id": r["id"], "brand": r["brand"], "label": r["label"], "platform": raw.get("platform"),
            "handle": raw.get("profileHandle"), "url": server.post_url(raw) if raw.get("platformId") else None,
            "caption": (raw.get("caption") or "")[:500], "snippets": snippets, "opening": "" if snippets else text[:400],
            "spellings": terms}


def label(item_id, body):
    value = body.get("label")
    if value not in ("yes", "no", None):
        raise ApiError(400, "Label is yes, no or null.")
    with connect() as db:
        row = db.execute("UPDATE eval_items SET label = %s, labelled_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END"
                         " WHERE id = %s RETURNING id", (value, value, item_id)).fetchone()
    if not row:
        raise ApiError(404, "No such video.")
    return {"id": item_id, "label": value}


# ---------------------------------------------------------------- the numbers

def rates(c):
    said, true = c["tp"] + c["fp"], c["tp"] + c["fn"]
    return {"precision": round(c["tp"] / said, 3) if said else None, "recall": round(c["tp"] / true, 3) if true else None}


def evaluate(rows, arabic):
    """Precision and recall of server.classify on labelled rows, with the Latin name only or with the Arabic spellings.
    A video counts as found when classify says the brand is in it (owned, sponsored, tagged or spoken)."""
    total, per, errors = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}, {}, []
    for r in rows:
        kind, _, q = server.classify(r["raw"], r["brand"], brands.ARABIC.get(r["brand"], []) if arabic else [])
        truth = r["label"] == "yes"
        cell = ("tp" if truth else "fp") if kind in SAID else ("fn" if truth else "tn")
        total[cell] += 1
        per.setdefault(r["brand"], {"tp": 0, "fp": 0, "fn": 0, "tn": 0})[cell] += 1
        if cell in ("fp", "fn"):
            errors.append({"id": r["id"], "brand": r["brand"], "error": cell, "kind": kind, "label": r["label"],
                           "quote": (q or {}).get("text"), "url": server.post_url(r["raw"]) if r["raw"].get("platformId") else None})
    return {"n": len(rows), **total, **rates(total), "errors": errors,
            "brands": [{"brand": b, **c, **rates(c)} for b, c in sorted(per.items())]}


def report():
    with connect() as db:
        rows = db.execute("SELECT * FROM eval_items ORDER BY brand, id").fetchall()
        used = db.execute("SELECT coalesce(sum(credits), 0) AS n FROM oriane_calls"
                          " WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')").fetchone()["n"]
    labelled = [r for r in rows if r["label"]]
    latin, arabic = evaluate(labelled, False), evaluate(labelled, True)
    by_brand = {}
    for r in rows:
        b = by_brand.setdefault(r["brand"], {"brand": r["brand"], "collected": 0, "labelled": 0})
        b["collected"] += 1
        b["labelled"] += bool(r["label"])
    return {"collected": len(rows), "labelled": len(labelled), "brands": list(by_brand.values()),
            "queue": [item_view(r) for r in rows if not r["label"]][:20],
            "latin": {k: v for k, v in latin.items() if k != "errors"}, "arabic": arabic,
            "gate": {"precision": GATE_PRECISION, "n": GATE_N,
                     "passed": len(labelled) >= GATE_N and (arabic["precision"] or 0) >= GATE_PRECISION},
            "defaults": {"brands": BRANDS, "perBrand": PER_BRAND, "creditsPerBrand": server.ORIANE_COST["contents"]},
            "credits": {"usedToday": used, "dailyBudget": int(os.environ.get("ORIANE_DAILY_BUDGET", "600"))}}


def export():
    with connect() as db:
        rows = db.execute("SELECT brand, label, raw FROM eval_items WHERE label IS NOT NULL ORDER BY brand, id").fetchall()
    return {"items": rows}


# ---------------------------------------------------------------- operator routes (/admin/)

def admin_route(handler):
    import digest
    digest.require_operator(handler)
    path = urlparse(handler.path).path
    if handler.command == "GET" and path == "/api/admin/eval":
        return report()
    if handler.command == "GET" and path == "/api/admin/eval/export":
        return export()
    if handler.command == "POST" and path == "/api/admin/eval/collect":
        body = digest.read_json(handler)
        names, per = body.get("brands") or None, body.get("perBrand", PER_BRAND)
        if names is not None and not (isinstance(names, list) and len(names) <= 20
                                      and all(isinstance(n, str) and 2 <= len(n.strip()) <= 60 for n in names)):
            raise ApiError(400, "Brands are a list of up to 20 names.")
        if not isinstance(per, int) or not 1 <= per <= 50:
            raise ApiError(400, "Videos per brand: 1 to 50.")
        return {**collect([n.strip() for n in names] if names else None, per), "report": report()}
    m = re.fullmatch(r"/api/admin/eval/(\d+)", path)
    if handler.command == "POST" and m:
        return label(int(m.group(1)), digest.read_json(handler))
    raise ApiError(404, "Not found.")


# ---------------------------------------------------------------- offline: python3 backend/eval_mentions.py file.jsonl

def pct(x):
    return "  n/a" if x is None else "%4.0f%%" % (x * 100)


def main(path):
    with open(path, encoding="utf-8") as f:
        items = [json.loads(line) for line in f if line.strip()]
    rows = [{"id": i, "brand": it["brand"], "label": it["label"], "raw": it["raw"]} for i, it in enumerate(items, 1) if it.get("label")]
    for title, arabic in (("Latin name only", False), ("With Arabic spellings", True)):
        r = evaluate(rows, arabic)
        print("%-22s %d labelled   precision %s   recall %s" % (title, r["n"], pct(r["precision"]), pct(r["recall"])))
        for b in r["brands"]:
            print("  %-20s precision %s   recall %s   (tp %d, fp %d, fn %d, tn %d)"
                  % (b["brand"], pct(b["precision"]), pct(b["recall"]), b["tp"], b["fp"], b["fn"], b["tn"]))
    final = evaluate(rows, True)
    passed = len(rows) >= GATE_N and (final["precision"] or 0) >= GATE_PRECISION
    print("Gate 6 (%d%% precision on %d+ videos): %s" % (GATE_PRECISION * 100, GATE_N, "pass" if passed else "not yet"))
    return 0 if passed else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
