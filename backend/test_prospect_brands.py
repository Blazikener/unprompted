"""Brand shortlist for the licence concierge test (mocked Oriane: zero credits).

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_prospect_brands.py
"""
import os
from datetime import date, timedelta

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")

import pytest  # noqa: E402

import creator  # noqa: E402
import prospect_brands as pb  # noqa: E402
import server  # noqa: E402


def video(vid, transcript, handle="fan", followers=40_000, views=48_000, caption="", hashtags=()):
    return {"id": vid, "platform": "tiktok", "platformId": vid, "profileHandle": handle, "profileFollowersCount": followers,
            "viewsCount": views, "caption": caption, "hashtags": list(hashtags), "mentions": [], "coAuthors": [],
            "transcript": transcript, "publishedAt": (date.today() - timedelta(days=3)).isoformat() + "T10:00:00Z",
            "transcriptChunks": [{"startSeconds": 65.0, "endSeconds": 70.0, "text": transcript}] if transcript else [],
            "frames": [], "thumbnailMediaUrl": ""}


def page(*videos):
    return {"data": {"results": list(videos), "aggregations": {"totalViewsCount": 0}},
            "metadata": {"pagination": {"totalCount": len(videos) + 40}}}


RHODE = [
    video("r1", "my rhode peptide lip tint is the only gloss I wear", handle="ali", views=90_000),
    video("r2", "morning", handle="sara", caption="obsessed with my Rhode blush", views=20_000),
    video("r3", "rhode sent me the new glazing milk", handle="paid", caption="#ad", hashtags=["ad"]),
    video("r4", "new rhode drop is here", handle="rhode"),                                   # the brand's own account
    video("r5", "rhode lip case on repeat", handle="tiny", followers=1_200),                 # too small
    video("r6", "rhode barrier butter saved my skin", handle="huge", followers=2_000_000),   # too big
    video("r7", "rhode skin is so good on the barrier", handle="omar", views=12_000),
]
COACH = [video("c1", "my coach said run five more", handle="gym"),                           # no bag/purse context
         video("c2", "this coach bag is my everyday bag", handle="lena")]


def fake_oriane(calls, pages):
    def oriane(filters, limit=100, sort="transcriptRelevance", offset=0, index="contents"):
        terms = filters["transcript"].get("includesExactly", {}).get("values", []) + filters["transcript"].get("includesFuzzy", {}).get("values", [])
        key = next((t for t in terms if t in ("Rhode", "Coach", "Kayali")), terms[0])
        calls.append(key)
        if isinstance(pages.get(key), Exception):
            raise pages[key]
        return page(*pages.get(key, []))
    return oriane


def test_scan_keeps_unprompted_clips_from_mid_size_creators(monkeypatch):
    monkeypatch.setattr(server, "oriane", fake_oriane([], {"Rhode": RHODE, "Coach": COACH}))
    row = pb.scan_brand(creator.brand_entry("Rhode"), 30)
    assert [c["handle"] for c in row["clips"]] == ["ali", "omar", "sara"]          # on camera first, by views; then tagged
    assert row["creators"] == 3 and row["spoken"] == 2 and row["matched"] == 47
    ali = row["clips"][0]
    assert ali["kind"] == "spoken" and ali["at"] == 65 and "rhode peptide lip tint" in ali["quote"].lower()
    assert ali["price"] == 550 and ali["url"] == "https://www.tiktok.com/@ali/video/r1"
    # An ambiguous name needs its context words ("bag", "purse", ...) before it counts.
    assert [c["handle"] for c in pb.scan_brand(creator.brand_entry("Coach"), 30)["clips"]] == ["lena"]


def test_run_caches_resumes_and_stops_at_the_budget(monkeypatch, tmp_path):
    cache = str(tmp_path / "shortlist.json")
    calls = []
    pool = pb.candidates(set(), ["Rhode", "Coach", "Kayali"])
    assert [b["name"] for b in pool] == ["Rhode", "Coach", "Kayali"] and pool[2]["handles"] == []   # not in the dictionary
    budget = server.ApiError(503, "Live search is paused for today to protect our data credits.")
    monkeypatch.setattr(server, "oriane", fake_oriane(calls, {"Rhode": RHODE, "Coach": budget}))
    rows = pb.run(pool, 30, limit=12, cache_path=cache)
    assert [r["brand"] for r in rows] == ["Rhode"] and calls == ["Rhode", "Coach"]     # stopped: Kayali never tried

    monkeypatch.setattr(server, "oriane", fake_oriane(calls, {"Coach": COACH, "Kayali": []}))
    rows = pb.run(pool, 30, limit=1, cache_path=cache)
    assert [r["brand"] for r in rows] == ["Rhode", "Coach"] and calls[2:] == ["Coach"]  # Rhode came from the cache
    assert [r["brand"] for r in pb.run(pool, 30, limit=12, cache_path=cache)] == ["Rhode", "Coach", "Kayali"]
    assert calls[3:] == ["Kayali"]


def test_render_ranks_brands_and_drafts_the_message(monkeypatch):
    monkeypatch.setattr(server, "oriane", fake_oriane([], {"Rhode": RHODE, "Coach": COACH}))
    rows = [pb.scan_brand(creator.brand_entry(n), 30) for n in ("Rhode", "Coach")]
    text, n = pb.render(rows, 3, "https://u.test/brands/")
    assert n == 1 and "| 1 | Rhode | 3 | 3 | 2 |" in text and "Below 3 unprompted mentions: Coach." in text
    msg = pb.outreach(rows[0], "https://u.test/brands/")
    assert msg.startswith("Hi [name], 3 creators talked about Rhode on camera in the last 30 days")
    assert msg.count("\n- @") == 3 and "from about $" in msg and "https://u.test/brands/" in msg


@pytest.mark.parametrize("name", ["Rhode", "Kayali"])
def test_candidates_default_to_the_first_vertical(name):
    pool = pb.candidates(set(pb.CATEGORIES), [name])
    assert {b["category"] for b in pool if b.get("category")} <= set(pb.CATEGORIES)
    assert sum(b["name"] == name for b in pool) == 1
