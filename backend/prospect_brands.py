"""Shortlist brands for the licence concierge test: which brands have creators praising them on camera, unpaid, now.

    ORIANE_API_KEY=... python3 backend/prospect_brands.py [--category beauty skincare fashion] [--brands "Rhode,Kayali"]
                                                          [--days 30] [--min 3] [--limit 12] [--out brand-shortlist.md]

One Oriane content search per brand (40 credits) over the last --days, classified with the dashboard's rules. Keeps
spoken and tagged mentions (no disclosed ads, no brand-owned accounts) from creators with 5K-500K followers, and
writes a ranked markdown shortlist: unprompted mentions, distinct creators, the best clips with their indicative licence
price, and an outreach message quoting three receipts. Results are cached in --cache after each brand, so a run that
hits the daily Oriane budget stops cleanly and the next run carries on (--limit keeps one run inside a day's budget).
The output names real creators: keep it and the cache out of the repo.
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(__file__))
import creator  # noqa: E402
import digest  # noqa: E402
import server  # noqa: E402

CATEGORIES = ("beauty", "skincare", "fashion")          # the research's first vertical
MIN_FOLLOWERS, MAX_FOLLOWERS = 5_000, 500_000
LICENSABLE = digest.LICENSABLE


def candidates(categories, names):
    """Dictionary entries for the given categories plus any named brands (named ones not in the dictionary are kept
    as bare names: no aliases, no own handles)."""
    pool = [b for b in creator.CATALOG if b["category"] in categories]
    for name in names:
        try:
            b = creator.brand_entry(name)
        except server.ApiError:
            b = {"name": name, "terms": [name], "handles": [], "context": []}
        if all(x["name"] != b["name"] for x in pool):
            pool.append(b)
    return pool


def snippet(q, width=140):
    """The sentence around the brand, short enough to quote in a message."""
    said = (q.get("core") or "").strip()
    if said and len(said) <= width:
        return said
    text, (a, _) = q.get("text") or "", q.get("hit") or (0, 0)
    start = max(0, a - width // 3)
    return ("…" if start else "") + text[start:start + width].strip() + ("…" if start + width < len(text) else "")


def scan_brand(b, days, platform="all"):
    """One search; the brand's unprompted clips from mid-size creators, best first."""
    brand = b["name"]
    _, variants, filters, _ = server.parse({"brand": brand, "variants": ", ".join(b["terms"][1:4]), "platform": platform,
                                            "days": 30 if days == 30 else 90 if days <= 90 else 365})
    filters["publishedAt"] = {"after": (date.today() - timedelta(days=days)).isoformat()}
    data = server.oriane(filters, limit=100)
    clips = []
    for r in data["data"]["results"]:
        handle, followers = r.get("profileHandle") or "", r.get("profileFollowersCount") or 0
        if not handle or handle.lower() in b["handles"] or not MIN_FOLLOWERS <= followers <= MAX_FOLLOWERS:
            continue
        kind, hits, q = server.classify(r, brand, variants)
        if kind not in LICENSABLE:
            continue
        caption = r.get("caption") or ""
        if b["context"] and not any(w in ((q or {}).get("text", "") + " " + caption).lower() for w in b["context"]):
            continue                                    # ambiguous name ("Coach", "Dove") with nothing around it
        views = r.get("viewsCount") or 0
        clips.append({"handle": handle, "platform": r.get("platform"), "followers": followers, "views": views,
                      "kind": kind, "url": server.post_url(r), "publishedAt": (r.get("publishedAt") or "")[:10],
                      "quote": snippet(q) if q else caption[:140], "at": int(q["start"]) if q and q.get("start") is not None else None,
                      "price": digest.quote({"platform": r.get("platform"), "handle": handle, "views": views}, 30)})
    clips.sort(key=lambda c: (c["kind"] != "spoken", -c["views"]))
    total = (data["metadata"].get("pagination") or {}).get("totalCount", len(data["data"]["results"]))
    return {"brand": brand, "days": days, "matched": total, "clips": clips,
            "creators": len({c["handle"].lower() for c in clips}), "spoken": sum(c["kind"] == "spoken" for c in clips)}


def outreach(row, app_url):
    """A first message to the brand's creator or paid-social lead, quoting three receipts."""
    top = []
    for c in row["clips"]:
        if c["handle"].lower() not in {t["handle"].lower() for t in top}:
            top.append(c)
        if len(top) == 3:
            break
    lines = ["@%s (%s views): “%s” %s" % (c["handle"], creator.fmt_num(c["views"]), c["quote"], c["url"]) for c in top]
    low = min(c["price"] for c in top)
    return ("Hi [name], %d creators talked about %s on camera in the last %d days with no tag and no deal. Three of them:\n\n%s\n\n"
            "You can run any of these as a Spark Ad or partnership ad: we handle the creator, from about $%d for 30 days. "
            "If you want these every week, the watch is here: %s" % (
                row["creators"], row["brand"], row["days"], "\n".join("- " + line for line in lines), low, app_url))


def render(rows, min_mentions, app_url):
    keep = sorted((r for r in rows if len(r["clips"]) >= min_mentions),
                  key=lambda r: (-r["creators"], -r["spoken"], -sum(c["views"] for c in r["clips"])))
    out = ["# Licence concierge: brand shortlist", "",
           "Brands with %d+ unprompted mentions (said on camera or tagged, no disclosed ads) from 5K-500K-follower creators." % min_mentions,
           "Prices are indicative (`digest.license_price`). Generated by `backend/prospect_brands.py` from Oriane's index;",
           "do not commit this file.", "",
           "| # | Brand | Creators | Unprompted | On camera only | Best clip views | Contact | Stage |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(keep, 1):
        out.append("| %d | %s | %d | %d | %d | %s | | |" % (i, r["brand"], r["creators"], len(r["clips"]), r["spoken"],
                                                            creator.fmt_num(max(c["views"] for c in r["clips"]))))
    out.append("")
    for i, r in enumerate(keep, 1):
        out += ["## %d. %s" % (i, r["brand"]), "",
                "%d unprompted mentions from %d creators in %d days (%d on camera only); %s total matches." % (
                    len(r["clips"]), r["creators"], r["days"], r["spoken"], creator.fmt_num(r["matched"])), ""]
        for c in r["clips"][:5]:
            out.append("- @%s · %s · %s followers · %s views · %s · ~$%d/30 days · %s%s" % (
                c["handle"], c["platform"], creator.fmt_num(c["followers"]), creator.fmt_num(c["views"]),
                "on camera only" if c["kind"] == "spoken" else "tagged", c["price"], c["url"],
                " at %d:%02d" % divmod(c["at"], 60) if c["at"] is not None else ""))
            out.append("  > “%s”" % c["quote"])
        out += ["", "**Message:**", "", "> " + outreach(r, app_url).replace("\n", "\n> "), ""]
    skipped = [r["brand"] for r in rows if len(r["clips"]) < min_mentions]
    if skipped:
        out += ["Below %d unprompted mentions: %s." % (min_mentions, ", ".join(sorted(skipped))), ""]
    return "\n".join(out), len(keep)


def run(pool, days, limit, cache_path):
    """Scan brands not yet in the cache, saving after each; stop at the first server-side error (budget, missing
    key, Oriane down) so nothing is retried blindly. Returns every cached row."""
    cache = {}
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cache = json.load(f)
    todo = [b for b in pool if b["name"] not in cache][:limit]
    for b in todo:
        try:
            cache[b["name"]] = scan_brand(b, days)
        except server.ApiError as e:
            print("  %s: %s" % (b["name"], e), file=sys.stderr)
            if e.status >= 500:
                print("stopped; run again later to continue (%d brands cached)" % len(cache), file=sys.stderr)
                break
            continue
        row = cache[b["name"]]
        print("  %s: %d unprompted from %d creators" % (b["name"], len(row["clips"]), row["creators"]), file=sys.stderr)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    left = len([b for b in pool if b["name"] not in cache])
    if left:
        print("%d brands still to scan; run again to continue" % left, file=sys.stderr)
    return [cache[b["name"]] for b in pool if b["name"] in cache]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", nargs="*", default=list(CATEGORIES))
    ap.add_argument("--brands", default="", help="comma-separated extra brands, e.g. GCC names not in the dictionary")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--min", type=int, default=3, help="unprompted mentions needed to make the shortlist")
    ap.add_argument("--limit", type=int, default=12, help="brands to scan this run (40 Oriane credits each)")
    ap.add_argument("--cache", default="brand-shortlist.json")
    ap.add_argument("--out", default="brand-shortlist.md")
    ap.add_argument("--app-url", default=os.environ.get("APP_URL", "https://YOUR-DOMAIN") + "/brands/")
    a = ap.parse_args()
    pool = candidates(set(a.category), [n.strip() for n in a.brands.split(",") if n.strip()])
    print("== %d candidate brands, scanning up to %d" % (len(pool), a.limit), file=sys.stderr)
    text, n = render(run(pool, a.days, a.limit, a.cache), max(1, a.min), a.app_url)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(text)
    print("wrote %s (%d brands on the shortlist)" % (a.out, n), file=sys.stderr)


if __name__ == "__main__":
    main()
