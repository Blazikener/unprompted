"""Find first users for Receipts: creators who already name-drop paying brands, unpaid.

    ORIANE_API_KEY=... python3 backend/prospect.py [--platform tiktok|instagram] [--n 25] [--out first-users.md]

Pulls the last 90 days of transcript mentions for a set of brands that run creator programmes, keeps
micro/mid creators (5K-500K followers) whose mention was not disclosed, scans each candidate's own videos with
the Receipts classifier, and writes a ranked markdown list with a ready-to-send DM per creator. The output is
personal data about real people: keep it out of the repo.
"""
import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
import creator  # noqa: E402
import server  # noqa: E402

SEED_BRANDS = ["Sephora", "Rhode", "Sol de Janeiro", "Glossier", "Gymshark", "MyProtein", "Celsius", "Poppi", "HelloFresh",
               "NordVPN", "Skims", "Lululemon", "Dyson", "Stanley", "Crocs", "Uber Eats", "Tim Hortons", "Shein", "Notion", "Duolingo"]
MIN_FOLLOWERS, MAX_FOLLOWERS = 5_000, 500_000


def candidates(platform, brands):
    """handle -> {brands mentioned unpaid, followers, name} from recent transcript mentions."""
    seen = defaultdict(lambda: {"brands": set(), "followers": 0, "name": ""})
    for name in brands:
        try:
            b = creator.brand_entry(name)
        except server.ApiError:
            continue
        _, variants, filters, _ = server.parse({"brand": b["name"], "variants": ",".join(b["terms"][1:4]), "platform": platform,
                                                "days": 90, "lang": "en"})
        try:
            results = server.oriane(filters, limit=100, sort="publishedAt")["data"]["results"]
        except server.ApiError as e:
            print("  skip %s: %s" % (name, e), file=sys.stderr)
            continue
        for r in results:
            h, f = r.get("profileHandle"), r.get("profileFollowersCount") or 0
            if not h or h.lower() in b["handles"] or not MIN_FOLLOWERS <= f <= MAX_FOLLOWERS:
                continue
            if server.classify(r, b["name"], variants)[0] == "sponsored":
                continue
            seen[h]["brands"].add(b["name"])
            seen[h]["followers"] = max(seen[h]["followers"], f)
            seen[h]["name"] = r.get("profileDisplayName") or h
    return seen


def dm(prof, rows, app_url):
    top = rows[0]
    q = top["receipts"][0]["quote"] or {}
    said = (q.get("core") or "").strip()
    if not said or len(said) > 110:
        text, hit = q.get("text") or "", q.get("hit") or [0, 0]
        a = max(0, hit[0] - 40)
        said = ("…" if a else "") + text[a:a + 110].strip() + ("…" if a + 110 < len(text) else "")
    first = (prof.get("name") or prof["handle"]).split()[0]
    others = ", ".join(r["brand"] for r in rows[1:3])
    return ("Hey %s, I built a tool that reads creators' transcripts and finds brands they mention without a deal. "
            "Ran yours: you've said %s on camera %s (%s views, unpaid)%s%s. "
            "Want the full receipt page? Free, takes 10 seconds: %s" % (
                first, top["brand"], "%d times" % top["organic"] if top["organic"] > 1 else "once", creator.fmt_num(top["organicViews"]),
                ' — "%s"' % said if said else "", " — plus %s" % others if others else "", app_url))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--platform", choices=["tiktok", "instagram", "both"], default="both")
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--pool", type=int, default=40, help="candidates to deep-scan per platform")
    ap.add_argument("--out", default="first-users.md")
    ap.add_argument("--app-url", default=os.environ.get("APP_URL", "https://YOUR-DOMAIN") + "/creators/")
    a = ap.parse_args()
    platforms = ["tiktok", "instagram"] if a.platform == "both" else [a.platform]
    ranked = []
    for platform in platforms:
        print("== %s: collecting candidates" % platform, file=sys.stderr)
        pool = candidates(platform, SEED_BRANDS)
        top = sorted(pool.items(), key=lambda kv: (-len(kv[1]["brands"]), -kv[1]["followers"]))[:a.pool]
        for handle, info in top:
            try:
                videos, _ = creator.fetch_videos(platform, handle)
            except server.ApiError as e:
                print("  skip @%s: %s" % (handle, e), file=sys.stderr)
                continue
            rows = [r for r in creator.aggregate(videos) if r["status"] == "unpaid"]
            if len(rows) < 2:
                continue
            prof = creator.profile(platform, handle, videos)
            score = sum(r["spoken"] * 2 + r["tagged"] for r in rows) * (1 + min(prof["medianViews"], 200_000) / 200_000)
            ranked.append((score, platform, handle, prof, rows))
            print("  @%s: %d unpaid brands, %s median views" % (handle, len(rows), creator.fmt_num(prof["medianViews"])), file=sys.stderr)
    ranked.sort(key=lambda t: -t[0])
    out = ["# Receipts: first-user prospects", "",
           "Creators who name-drop paying brands on camera without a deal, ranked by how much unpaid brand talk they have.",
           "Each entry has a DM you can send as-is from your own account. Generated by `backend/prospect.py` from Oriane's",
           "index; do not commit this file.", "",
           "| # | Creator | Platform | Followers | Median views | Unpaid brands | Sent | Replied | Signed up |",
           "|---|---|---|---|---|---|---|---|---|"]
    for i, (score, platform, handle, prof, rows) in enumerate(ranked[:a.n], 1):
        out.append("| %d | [@%s](%s) | %s | %s | %s | %s | | | |" % (i, handle, prof["url"], creator.PLATFORM_NAMES[platform],
                                                                     creator.fmt_num(prof["followers"]), creator.fmt_num(prof["medianViews"]),
                                                                     ", ".join(r["brand"] for r in rows[:4])))
    out.append("")
    for i, (score, platform, handle, prof, rows) in enumerate(ranked[:a.n], 1):
        out += ["## %d. @%s (%s, %s followers)" % (i, handle, creator.PLATFORM_NAMES[platform], creator.fmt_num(prof["followers"])),
                "", "%s · %s videos read · %s median views · %s%% median engagement" % (
                    prof["url"], prof["videos"], creator.fmt_num(prof["medianViews"]), prof["medianEr"]), ""]
        for r in rows[:3]:
            q = r["receipts"][0]["quote"] or {}
            out.append("- **%s**: %d unpaid (%d said on camera), %s views. \"%s\" %s" % (
                r["brand"], r["organic"], r["spoken"], creator.fmt_num(r["organicViews"]), (q.get("core") or q.get("text") or "").strip()[:160],
                r["receipts"][0]["url"]))
        out += ["", "**DM:**", "", "> " + dm(prof, rows, a.app_url), ""]
    with open(a.out, "w") as f:
        f.write("\n".join(out))
    print("wrote %s (%d creators)" % (a.out, min(a.n, len(ranked))), file=sys.stderr)


if __name__ == "__main__":
    main()
