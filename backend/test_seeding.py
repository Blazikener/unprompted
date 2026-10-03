"""Seeding report: gifting lists, finding which gifted creators posted, the email section and Gate 5's numbers
(demo creators, mocked Oriane and mail: zero credits).

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_seeding.py
"""
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["APP_URL"] = "https://u.test"
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"
os.environ["RESEND_API_KEY"] = ""
os.environ.pop("MAIL_RELAY_URL", None)
os.environ.pop("MAIL_RELAY_SECRET", None)

import psycopg  # noqa: E402
import pytest  # noqa: E402

import creator  # noqa: E402
import digest  # noqa: E402
import seeding  # noqa: E402
import server  # noqa: E402

TODAY = date.today()


@pytest.fixture(autouse=True)
def clean():
    server.init_db()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM digests")
        db.execute("DELETE FROM mentions WHERE search_id IN (SELECT id FROM searches WHERE brand = 'Tim Hortons')")
        db.execute("DELETE FROM searches WHERE brand = 'Tim Hortons'")


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(digest, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


def video(vid, handle, days_ago, transcript="", caption="", platform="instagram"):
    return {"id": vid, "platform": platform, "platformId": vid, "profileHandle": handle, "viewsCount": 9000, "caption": caption,
            "hashtags": [], "mentions": [], "coAuthors": [], "transcript": transcript,
            "transcriptChunks": [{"startSeconds": 5.0, "endSeconds": 9.0, "text": transcript}] if transcript else [],
            "publishedAt": (TODAY - timedelta(days=days_ago)).isoformat() + "T10:00:00Z", "frames": [], "thumbnailMediaUrl": ""}


def watch(platform="all"):
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons", "platform": platform})
    return d, digest.load_digest(d["token"])[0]


def test_parse_takes_messy_lists():
    ship = (TODAY - timedelta(days=20)).isoformat()
    text = ("handle,platform,shipped,tracked\n"
            "@Maya.Eats, TikTok, %s\n"
            "https://www.instagram.com/sami.lifts/;%s;yes\n"
            "nobody\t%s\n"
            "@future, tt, 2099-01-01\n"
            ", , \n"
            "not a handle!!, ig\n" % (ship, ship, ship))
    rows, skipped = seeding.parse(text, "all")
    assert rows == [("tiktok", "maya.eats", date.fromisoformat(ship), False), ("instagram", "sami.lifts", date.fromisoformat(ship), True)]
    assert [(s["line"], s["reason"][:12]) for s in skipped] == [(4, "Say tiktok o"), (5, "Ship date is"), (7, "No handle or")]
    rows, _ = seeding.parse("nobody", "tiktok")                       # a TikTok-only watch fills in the platform
    assert rows == [("tiktok", "nobody", TODAY, False)]


def test_weekly_run_finds_gifted_posters_and_reports_them(monkeypatch, mail):
    d, row = watch()
    ship = (TODAY - timedelta(days=20)).isoformat()
    # Upload: TikTok creators are read from their public page straight away (demo: @maya.eats says it on camera).
    out = seeding.upload(row, {"text": "@maya.eats, tiktok, %s\n@sara.eats, instagram, %s, yes\n@ghost, tiktok, %s\n@old, tiktok, 2026-01-01"
                                       % (ship, ship, ship)})
    assert out["added"] == 4 and out["skipped"] == []
    s = out["seeding"]
    assert (s["gifted"], s["posted"], s["spokenOnly"], s["tracked"]) == (4, 1, 1, 1)
    maya = next(g for g in s["gifts"] if g["handle"] == "maya.eats")
    assert maya["kind"] == "spoken" and maya["postedOn"] == TODAY - timedelta(days=15)   # first post after it shipped
    assert next(g for g in s["gifts"] if g["handle"] == "ghost")["checkedAt"]          # looked, nothing (unknown creator)
    assert next(g for g in s["gifts"] if g["handle"] == "old")["checkedAt"] is None     # outside the 90-day window
    assert maya["licenseUrl"] == "https://u.test/license/%s/%s" % (d["token"], maya["videoId"])
    assert digest.license_view(d["token"], maya["videoId"])["video"]["kind"] == "spoken"   # found at upload, still licensable

    # The weekly run: @sara.eats turns up in the brand's own search (tagged, after shipping); an earlier post doesn't count.
    results = [video("sara_before", "sara.eats", 30, caption="#timhortons haul"),
               video("sara_after", "sara.eats", 2, caption="#timhortons iced capp"),
               video("fan1", "fan", 1, transcript="a Tim Hortons iced capp every morning")]
    monkeypatch.setattr(server, "oriane", lambda *a, **k: {"data": {"results": results, "aggregations": {"totalViewsCount": 0}}, "metadata": {}})
    run = digest.run_due(force=True)["results"][0]
    assert run["giftedFound"] == 1 and run["sent"]
    s = seeding.summary(row)
    sara = next(g for g in s["gifts"] if g["handle"] == "sara.eats")
    assert sara["kind"] == "tagged" and sara["videoId"] == "sara_after" and (s["posted"], s["untrackedPosts"], s["multiple"]) == (2, 1, 2.0)

    subject, body = mail[-1][1], mail[-1][2]
    assert subject.startswith("2 new creator mentions of Tim Hortons")
    assert "Gifted creators" in body and "2 of 4 gifted creators have posted, 1 only on camera" in body and "your own tracking had 1" in body
    assert "@sara.eats" in body and "https://u.test/license/%s/sara_after" % d["token"] in body
    assert digest.license_view(d["token"], "sara_after")["video"]["handle"] == "sara.eats"   # the licence button works

    # A week with no new mentions but a gifted creator posting still sends, with its own subject.
    seeding.upload(row, {"text": "@maya.eats, tiktok, %s" % (TODAY - timedelta(days=5)).isoformat()})
    monkeypatch.setattr(server, "oriane", lambda *a, **k: {"data": {"results": [], "aggregations": {"totalViewsCount": 0}}, "metadata": {}})
    with psycopg.connect(server.DB_URL) as db:                     # pretend the upload's own check hadn't run yet
        db.execute("UPDATE seeding_gifts SET video_id = NULL, kind = NULL, checked_at = NULL WHERE shipped_on = %s", (TODAY - timedelta(days=5),))
    run = digest.run_due(force=True)["results"][0]
    assert run["new"] == 0 and run["giftedFound"] == 1 and mail[-1][1] == "1 gifted creator posted about Tim Hortons"
    assert "License for ads" in mail[-1][2]                        # found through the creator's own page: stored with the run


def test_checks_never_overspend_oriane(monkeypatch, mail):
    d, row = watch()
    ship = (TODAY - timedelta(days=10)).isoformat()
    calls = []

    def oriane(filters, limit=100, sort="", offset=0, index="contents"):
        calls.append(index)
        return {"data": []} if index == "profiles" else {"data": {"results": [], "aggregations": {"totalViewsCount": 0}}, "metadata": {}}
    monkeypatch.setattr(seeding.demo, "active", lambda: False)
    monkeypatch.setattr(creator, "public_videos", lambda platform, handle: [])     # every TikTok page unreadable
    monkeypatch.setattr(server, "oriane", oriane)
    text = "\n".join("@seedcost.%s%d, %s, %s" % (p[:2], i, p, ship) for p in ("tiktok", "instagram") for i in range(8))
    assert seeding.upload(row, {"text": text})["added"] == 16
    assert calls == []                                       # an upload never spends Oriane credits
    digest.run_due(force=True)
    assert calls.count("contents") <= 1 + seeding.ORIANE_READS   # the brand search, then at most ORIANE_READS creators
    unread = [g for g in seeding.summary(row)["gifts"] if g["checkedAt"] is None]
    assert len(unread) == 16 - seeding.ORIANE_READS            # the rest wait their turn (first in line next week)


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


def call(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_routes_and_limits(base, monkeypatch, mail):
    d, row = watch(platform="tiktok")
    assert call(base, "POST", "/api/digests/%s/gifts" % d["token"], {"text": "  "})[0] == 400
    status, out = call(base, "POST", "/api/digests/%s/gifts" % d["token"], {"text": "@maya.eats, 2026-09-01\n@ghost"})
    assert status == 200 and out["added"] == 2 and out["seeding"]["posted"] == 1
    maya = next(g for g in out["seeding"]["gifts"] if g["videoId"])
    status, lic = call(base, "GET", "/api/digests/%s/license/%s" % (d["token"], maya["videoId"]))   # its licence link opens
    assert status == 200 and lic["video"]["handle"] == "maya.eats"
    status, view = call(base, "GET", "/api/digests/" + d["token"])
    assert status == 200 and view["seeding"]["gifted"] == 2
    status, rows = call(base, "GET", "/api/admin/seeding", None, {"Authorization": "Bearer run-secret"})
    assert status == 200 and next(w for w in rows["watches"] if w["email"] == "brand@example.com")["posted"] == 1
    assert call(base, "GET", "/api/admin/seeding")[0] == 401
    monkeypatch.setattr(seeding, "MAX_GIFTS", 3)
    assert call(base, "POST", "/api/digests/%s/gifts" % d["token"], {"text": "a1\na2"})[0] == 409
    assert call(base, "POST", "/api/digests/%s/gifts/clear" % d["token"], {})[1]["seeding"]["gifted"] == 0
