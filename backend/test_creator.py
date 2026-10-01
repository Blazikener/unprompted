"""Receipts (creator product) tests: detection, pitch, billing webhook, and the HTTP flow end to end.

    DATABASE_URL=postgresql:///unprompted_test python3 -m pytest backend/test_creator.py

Uses demo fixtures (no ORIANE_API_KEY needed) and a real Postgres database, which it truncates.
"""
import hashlib
import hmac
import http.cookiejar
import json
import os
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

import psycopg
from psycopg.types.json import Jsonb
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["DEV_PLAN_SWITCH"] = "1"
os.environ["STRIPE_WEBHOOK_SECRET"] = "whsec_test"

import creator  # noqa: E402
import demo  # noqa: E402
import server  # noqa: E402
import tiktok_public  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def db():
    server.init_db()
    with psycopg.connect(server.DB_URL) as c:
        c.execute("TRUNCATE users, sessions, scans, brand_activity, events, mentions, searches, videos, checks RESTART IDENTITY CASCADE")


def video(transcript="", caption="", hashtags=(), mentions=(), co_authors=(), views=1000):
    return {"id": "v%s" % hash((transcript, caption)), "platform": "tiktok", "platformId": "1", "profileHandle": "maya", "viewsCount": views,
            "publishedAt": "2026-09-01T00:00:00Z", "caption": caption, "hashtags": list(hashtags),
            "mentions": [{"profileHandle": m} for m in mentions], "coAuthors": [{"profileHandle": c} for c in co_authors],
            "transcript": transcript, "transcriptChunks": [{"startSeconds": 3.0, "text": transcript}] if transcript else [],
            "frames": [{"timestampSeconds": 3.0, "url": "f.jpg"}], "thumbnailMediaUrl": "t.jpg"}


def kinds(raw):
    return {b["name"]: kind for b, kind, _ in creator.brand_mentions(raw)}


def test_spoken_tagged_sponsored_split():
    assert kinds(video("I always get a Tim Hortons iced capp")) == {"Tim Hortons": "spoken"}
    assert kinds(video("best coffee", caption="morning run #timhortons"))["Tim Hortons"] == "tagged"
    assert kinds(video("hello", mentions=["timhortonsgcc"]))["Tim Hortons"] == "tagged"
    assert kinds(video("this is sponsored", caption="#ad", mentions=["noon"]))["Noon"] == "sponsored"
    assert kinds(video("great app", co_authors=["talabat"]))["Talabat"] == "sponsored"
    assert kinds(video("my new ninja creami", caption="#ninjapartner"))["Ninja Kitchen"] == "sponsored"
    assert kinds(video("my new ninja creami", caption="#partnerworkout"))["Ninja Kitchen"] == "spoken"
    # In a disclosed video, a brand that's only spoken (not tagged) isn't assumed to be the sponsor.
    k = kinds(video("this is sponsored by noon but I also drink Starbucks", caption="#ad", mentions=["noon"]))
    assert k["Noon"] == "sponsored" and k["Starbucks"] == "spoken"


def test_word_boundaries_and_ambiguous_names():
    assert "du" not in kinds(video("living in Dubai during summer"))
    assert kinds(video("my du sim has a 5g plan"))["du"] == "spoken"
    assert "Apple" not in kinds(video("an apple a day"))
    assert kinds(video("filming on my iPhone"))["Apple"] == "spoken"
    assert kinds(video("I love my Nikes"))["Nike"] == "spoken"
    assert "Target" not in kinds(video("my target is 10k"))
    assert kinds(video("anything", caption="#target haul"))["Target"] == "tagged"
    # place names that are also brands need hotel context, in captions too
    assert "Jumeirah" not in kinds(video("brunch time", caption="café hopping in Jumeirah"))
    assert kinds(video("we checked in to the Jumeirah beach hotel"))["Jumeirah"] == "spoken"
    assert kinds(video("hi", mentions=["jumeirahgroup"]))["Jumeirah"] == "tagged"
    assert "Beats" not in kinds(video("nothing beats a sunday roast"))


def test_spoken_disclosure_is_sponsored():
    assert kinds(video("so excited to partner with ClassPass on this"))["ClassPass"] == "sponsored"
    assert kinds(video("I booked it on ClassPass"))["ClassPass"] == "spoken"


def test_demo_activity_dates_agree():
    for brand in ("Tim Hortons", "MyProtein", "Sephora"):
        a = demo.activity(brand, "tiktok")
        assert a["lastPaid"] == max((e["publishedAt"] for e in a["examples"]), default=None)


def test_quote_has_receipt_fields():
    (_, kind, q), = creator.brand_mentions(video("okay Tim Hortons iced capp, extra large"))
    assert kind == "spoken" and q["start"] == 3.0 and q["frame"] == "f.jpg"
    assert q["text"][q["hit"][0]:q["hit"][1]].lower() == "tim hortons"


def test_aggregate_orders_unpaid_first():
    rows = creator.aggregate(demo.creator_videos("tiktok", "maya.eats"))
    by = {r["brand"]: r for r in rows}
    assert rows[0]["brand"] == "Tim Hortons" and rows[0]["status"] == "unpaid" and rows[0]["spoken"] == 4
    assert by["Noon"]["status"] == "sponsored-only" and by["Noon"]["organicViews"] == 0
    assert rows[-1]["status"] == "sponsored-only"
    assert rows[0]["receipts"][0]["kind"] == "spoken"
    assert all(0 <= r["score"] <= 100 for r in rows)


def test_rate_card_and_pitch():
    rate = creator.rate_card(103_500)
    assert rate["low"] == 1050 and rate["high"] == 2600 and rate["usage"] == 650
    vids = demo.creator_videos("tiktok", "maya.eats")
    rows, prof = creator.aggregate(vids), creator.profile("tiktok", "maya.eats", vids)
    p = creator.draft_pitch(prof, rows[0], {"sponsored": 6, "windowDays": 90}, rate)
    assert "Tim Hortons" in p["subject"] and "5 unpaid mentions" in p["subject"]
    assert "6 disclosed creator posts" in p["body"] and "$1050-$2600" in p["body"] and "tiktok.com/@maya.eats" in p["body"]
    assert "on camera %d times and tagged you in %d more post" % (rows[0]["spoken"], rows[0]["tagged"]) in p["body"]
    assert rows[0]["spoken"] == 4 and rows[0]["tagged"] == 1


def test_gate_hides_names_beyond_free_limit():
    rows = creator.aggregate(demo.creator_videos("instagram", "sami.lifts"))
    gated = creator.gate(rows, "free")
    assert len(gated) == len(rows) and "brand" in gated[0] and gated[3]["locked"] and "brand" not in gated[3]
    assert creator.gate(rows, "pro") == rows


def test_oriane_wallet_error_is_ours_not_the_users(monkeypatch):
    import io
    from urllib import error

    def broke(req, timeout=0):
        body = io.BytesIO(b'{"error":{"message":"The wallet does not have enough credits."}}')
        raise error.HTTPError(req.full_url, 402, "Payment Required", {}, body)

    monkeypatch.setenv("ORIANE_API_KEY", "k")
    monkeypatch.setattr(server.request, "urlopen", broke)
    with pytest.raises(server.ApiError) as e:
        server.oriane({"platform": {"includes": ["tiktok"]}})
    assert e.value.status == 503 and "quota" in str(e.value) and "402" not in str(e.value)


def test_parse_handle_accepts_links_and_decides_platform():
    assert creator.parse_handle("@Maya.Eats", "tiktok") == ("tiktok", "maya.eats")
    assert creator.parse_handle("https://www.tiktok.com/@Maya.Eats/video/7301?lang=en", "instagram") == ("tiktok", "maya.eats")
    assert creator.parse_handle("instagram.com/sami.lifts/", "tiktok") == ("instagram", "sami.lifts")
    assert creator.parse_handle("  maya eats ", "tiktok") == ("tiktok", "maya eats")


def test_not_indexed_explains_the_gap_with_one_lookup(monkeypatch):
    monkeypatch.setenv("ORIANE_API_KEY", "k")
    profiles = [{"platform": "tiktok", "handle": "khaby.lame", "followersCount": 163_000_000},
                {"platform": "instagram", "handle": "khaby_lame_fans", "followersCount": 91_108},
                {"platform": "tiktok", "handle": "quiet.one", "followersCount": 5_000}]
    calls = []

    def fake(filters, limit=100, sort="x", offset=0, index="contents"):
        calls.append(index)
        assert index == "profiles" and "platform" not in filters
        q = filters["handle"]["includesFuzzy"]["values"][0].split(".")[0]
        return {"data": [p for p in profiles if p["handle"].replace("_", ".").startswith(q)
                         and p["followersCount"] >= filters["followersCount"]["min"]]}

    monkeypatch.setattr(server, "oriane", fake)
    e = creator.not_indexed("instagram", "khaby.lame")
    assert e.status == 404 and "indexed on TikTok, not Instagram" in str(e) and e.extra["reason"] == "other_platform"
    assert [s["handle"] for s in e.extra["suggestions"]] == ["khaby.lame", "khaby_lame_fans"]
    e = creator.not_indexed("tiktok", "khaby")
    assert e.extra["reason"] == "unknown" and "Did you mean" in str(e) and [s["handle"] for s in e.extra["suggestions"]] == ["khaby.lame"]
    e = creator.not_indexed("tiktok", "quiet.one")
    assert e.extra["reason"] == "profile_only" and "5K followers" in str(e) and e.extra["suggestions"] == []
    e = creator.not_indexed("tiktok", "nobody")
    assert e.extra == {"reason": "unknown", "suggestions": []} and "Did you mean" not in str(e)
    assert calls == ["profiles"] * 4

    def broken(*a, **k):
        raise server.ApiError(502, "down")

    monkeypatch.setattr(server, "oriane", broken)
    e = creator.not_indexed("tiktok", "x")
    assert e.status == 404 and e.extra == {"reason": "unknown", "suggestions": []}


VTT = """WEBVTT

00:00:00.000 --> 00:00:02.740
Day 2 of what if Disney princesses were Pakistani baddies?

00:00:02.741 --> 00:00:04.741
<c>And today</c> we have Jasmine from Aladdin.

00:11.020 --> 00:12.20
I'm wearing Ninja creami merch.
"""


def test_vtt_chunks_and_subtitle_preference():
    chunks = tiktok_public.vtt_chunks(VTT)
    assert [c["text"] for c in chunks] == ["Day 2 of what if Disney princesses were Pakistani baddies?",
                                           "And today we have Jasmine from Aladdin.", "I'm wearing Ninja creami merch."]
    assert chunks[0]["startSeconds"] == 0 and chunks[1]["endSeconds"] == 4.741 and chunks[2]["startSeconds"] == 11.02
    infos = [{"Url": "u1", "Format": "webvtt", "Source": "MT", "LanguageCodeName": "eng-US"},
             {"Url": "u2", "Format": "webvtt", "Source": "ASR", "LanguageCodeName": "urd-PK"},
             {"Url": "u3", "Format": "webvtt", "Source": "ASR", "LanguageCodeName": "eng-GB"},
             {"Url": "u4", "Format": "srt", "Source": "ASR", "LanguageCodeName": "eng-US"}]
    assert tiktok_public.pick_subtitle(infos)["Url"] == "u3"
    assert tiktok_public.pick_subtitle(infos[:2])["Url"] == "u2"
    assert tiktok_public.pick_subtitle([]) is None


def test_public_video_matches_oriane_shape_and_classifies():
    item = {"id": "7647902360420748562", "desc": "Jasmine as Pakistani Baddie #disney #grwm @ninjakitchen", "createTime": 1780666035,
            "stats": {"playCount": 66700, "diggCount": 5000, "commentCount": 100, "shareCount": 50},
            "author": {"nickname": "Tahaiyya", "avatarMedium": "https://a/pfp.jpg", "verified": False},
            "authorStats": {"followerCount": 261700},
            "textExtra": [{"hashtagName": "disney"}, {"hashtagName": "grwm"}, {"userUniqueId": "ninjakitchen"}],
            "video": {"duration": 53, "cover": "https://a/cover.jpg"}}
    v = tiktok_public.as_oriane("nikki.bae_", item, {}, tiktok_public.vtt_chunks(VTT), "eng")
    assert v["platformId"] == "7647902360420748562" and v["publishedAt"] == "2026-06-05T13:27:15Z" and v["viewsCount"] == 66700
    assert v["hashtags"] == ["disney", "grwm"] and v["mentions"] == [{"profileHandle": "ninjakitchen"}]
    assert v["engagementRatePerViews"] == 7.72 and v["thumbnailMediaUrl"] == "https://a/cover.jpg"
    assert server.post_url(v) == "https://www.tiktok.com/@nikki.bae_/video/7647902360420748562"
    kinds = {b["name"]: (kind, q) for b, kind, q in creator.brand_mentions(v)}
    assert kinds["Ninja Kitchen"][0] == "tagged" and kinds["Ninja Kitchen"][1]["start"] == 11.02
    assert creator.profile("tiktok", "nikki.bae_", [v])["followers"] == 261700


def test_public_tiktok_fallback_runs_after_oriane_and_on_cached_misses(monkeypatch):
    monkeypatch.setenv("ORIANE_API_KEY", "k")
    monkeypatch.setattr(demo, "active", lambda: False)
    calls = []
    monkeypatch.setattr(server, "oriane", lambda *a, index="contents", **k: calls.append(index)
                        or {"data": {"results": []} if index == "contents" else []})
    public = {"tiktok": [{"id": "tiktok-public-1", "platform": "tiktok", "platformId": "1", "profileHandle": "fresh.face", "caption": ""}]}
    monkeypatch.setattr(tiktok_public, "fetch", lambda h, limit=12: calls.append("public") or public.get(h, []))
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM misses")
    with pytest.raises(server.ApiError) as e:
        creator.fetch_videos("instagram", "fresh.face")
    assert "scan that handle" in str(e.value) and calls == ["contents", "profiles"]
    calls.clear()
    monkeypatch.setitem(public, "fresh.face", public.pop("tiktok"))
    assert creator.fetch_videos("tiktok", "fresh.face") == (public["fresh.face"], "public") and calls == ["contents", "public"]
    calls.clear()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("INSERT INTO misses (platform, handle, body) VALUES ('tiktok', 'fresh.face', %s)",
                   (Jsonb({"error": "x", "reason": "unknown", "suggestions": []}),))
    assert creator.fetch_videos("tiktok", "fresh.face")[1] == "public" and calls == ["public"]


def test_misses_are_cached_so_retries_cost_no_credits(monkeypatch):
    monkeypatch.setenv("ORIANE_API_KEY", "k")
    monkeypatch.setattr(demo, "active", lambda: False)
    calls = []

    def fake(filters, limit=100, sort="x", offset=0, index="contents"):
        calls.append(index)
        return {"data": {"results": []} if index == "contents" else []}

    monkeypatch.setattr(server, "oriane", fake)
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM misses")
    for _ in range(2):
        with pytest.raises(server.ApiError) as e:
            creator.fetch_videos("tiktok", "ghost.handle")
        assert e.value.status == 404 and e.value.extra["reason"] == "unknown"
    assert calls == ["contents", "profiles"] and e.value.extra["cached"] is True


def test_stripe_signature():
    payload, secret = b'{"type":"x"}', "whsec_test"
    ts = int(time.time())
    sig = hmac.new(secret.encode(), b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest()
    assert creator.verify_stripe_signature(payload, "t=%d,v1=%s" % (ts, sig), secret)
    assert not creator.verify_stripe_signature(payload, "t=%d,v1=%s" % (ts, "0" * 64), secret)
    assert not creator.verify_stripe_signature(payload, "t=%d,v1=%s" % (ts - 1000, sig), secret)
    assert not creator.verify_stripe_signature(payload, "garbage", secret)


def test_password_hashing():
    h = creator.hash_password("correct horse")
    assert h.startswith("scrypt$") and creator.check_password("correct horse", h) and not creator.check_password("wrong", h)


# ---------------------------------------------------------------- HTTP flow

@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


class Client:
    def __init__(self, base):
        self.base, self.jar = base, http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, headers=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with self.opener.open(req) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)


def test_http_flow(base):
    c = Client(base)
    status, me = c.call("GET", "/api/creators/me")
    assert status == 200 and me["user"] is None and me["billing"]["demo"] and not me["billing"]["enabled"]

    assert c.call("POST", "/api/creators/scans", {"platform": "tiktok", "handle": "maya.eats"})[0] == 401
    assert c.call("POST", "/api/creators/signup", {"email": "bad", "password": "12345678"})[0] == 400
    assert c.call("POST", "/api/creators/signup", {"email": "maya@example.com", "password": "short"})[0] == 400
    status, out = c.call("POST", "/api/creators/signup", {"email": "Maya@Example.com", "password": "longenough"})
    assert status == 201 and out["user"]["email"] == "maya@example.com" and out["user"]["plan"] == "free"
    assert c.call("POST", "/api/creators/signup", {"email": "maya@example.com", "password": "longenough"})[0] == 409

    # a pasted link works, spaces get a specific hint, and a miss is tracked with its reason in the error body
    assert c.call("POST", "/api/creators/scans", {"platform": "tiktok", "handle": "maya eats"})[1]["error"].startswith("Enter a TikTok")
    status, err = c.call("POST", "/api/creators/scans", {"platform": "tiktok", "handle": "https://www.tiktok.com/@nobody.here"})
    assert status == 404 and "fixture creators" in err["error"]
    with psycopg.connect(server.DB_URL) as db:
        miss = db.execute("SELECT data FROM events WHERE name = 'scan_miss'").fetchall()
    assert len(miss) == 1 and miss[0][0]["handle"] == "nobody.here" and miss[0][0]["platform"] == "tiktok"

    # form posts are rejected (CSRF), JSON is required
    status, _ = c.call("POST", "/api/creators/scans", raw=b"platform=tiktok", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert status == 415

    status, scan = c.call("POST", "/api/creators/scans", {"platform": "tiktok", "handle": "@Maya.Eats"})
    assert status == 200 and scan["source"] == "demo" and scan["handle"] == "maya.eats" and scan["plan"] == "free"
    assert scan["brands"][0]["brand"] == "Tim Hortons" and scan["brands"][3]["locked"] and scan["totalBrands"] > 10
    assert scan["profile"]["followers"] == 184000 and scan["rate"]["low"] > 0
    sid = scan["id"]

    # same handle within a day is served from the stored scan and doesn't burn quota; a new one hits the free quota
    assert c.call("POST", "/api/creators/scans", {"platform": "tiktok", "handle": "maya.eats"})[1]["id"] == sid
    status, err = c.call("POST", "/api/creators/scans", {"platform": "instagram", "handle": "sami.lifts"})
    assert status == 402 and "free scan" in err["error"]

    # Pro-only features paywall on free
    assert c.call("POST", "/api/creators/scans/%d/brands/Tim%%20Hortons/activity" % sid, {})[0] == 402
    assert c.call("POST", "/api/creators/scans/%d/brands/Tim%%20Hortons/pitch" % sid, {})[0] == 402
    assert c.call("POST", "/api/creators/billing/checkout", {})[0] == 503
    assert c.call("POST", "/api/creators/billing/interest", {"scan": sid})[0] == 200

    status, out = c.call("POST", "/api/creators/billing/dev", {"plan": "pro"})
    assert status == 200 and out["user"]["plan"] == "pro"
    status, scan = c.call("GET", "/api/creators/scans/%d" % sid)
    assert status == 200 and scan["plan"] == "pro" and not any(b.get("locked") for b in scan["brands"])

    status, act = c.call("POST", "/api/creators/scans/%d/brands/Tim%%20Hortons/activity" % sid, {})
    assert status == 200 and act["source"] == "demo" and act["verdict"] in ("active", "some", "quiet") and act["brand"] == "Tim Hortons"
    status, pitch = c.call("POST", "/api/creators/scans/%d/brands/Tim%%20Hortons/pitch" % sid, {})
    assert status == 200 and "Tim Hortons" in pitch["subject"] and "maya.eats" in pitch["body"]
    assert c.call("POST", "/api/creators/scans/%d/brands/Nope/pitch" % sid, {})[0] == 404

    status, second = c.call("POST", "/api/creators/scans", {"platform": "instagram", "handle": "sami.lifts"})
    assert status == 200 and second["brands"][0]["brand"] == "MyProtein"
    status, hist = c.call("GET", "/api/creators/scans")
    assert status == 200 and [h["handle"] for h in hist] == ["sami.lifts", "maya.eats"]

    # another user can't see this scan, but a published receipt page is public and read-only at the owner's plan
    other = Client(base)
    other.call("POST", "/api/creators/signup", {"email": "other@example.com", "password": "longenough"})
    assert other.call("GET", "/api/creators/scans/%d" % sid)[0] == 404
    assert other.call("POST", "/api/creators/scans/%d/share" % sid, {})[0] == 404
    status, link = c.call("POST", "/api/creators/scans/%d/share" % sid, {})
    assert status == 200 and link["url"].endswith("/creators/r/" + link["token"])
    assert c.call("POST", "/api/creators/scans/%d/share" % sid, {})[1]["token"] == link["token"]
    status, pub = other.call("GET", "/api/creators/shared/" + link["token"])
    assert status == 200 and pub["shared"] and pub["handle"] == "maya.eats" and not any(b.get("locked") for b in pub["brands"])
    assert other.call("GET", "/api/creators/shared/nope12345")[0] == 404
    with urllib.request.urlopen(base + "/creators/r/" + link["token"]) as res:
        assert res.status == 200 and b"Receipts" in res.read()
    status, sample = other.call("GET", "/api/creators/sample")
    assert status == 200 and sample["sample"] and sample["source"] == "demo" and sample["plan"] == "pro"
    assert sample["brands"][0]["brand"] == "Tim Hortons"

    # webhook: bad signature rejected, good one flips the plan
    payload = json.dumps({"type": "customer.subscription.deleted", "data": {"object": {"customer": "cus_1", "id": "sub_1"}}}).encode()
    assert c.call("POST", "/api/creators/billing/webhook", raw=payload, headers={"Stripe-Signature": "t=1,v1=bad"})[0] == 400
    with psycopg.connect(server.DB_URL) as db:
        db.execute("UPDATE users SET stripe_customer = 'cus_1' WHERE email = 'maya@example.com'")
    ts = int(time.time())
    sig = hmac.new(b"whsec_test", b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest()
    status, out = c.call("POST", "/api/creators/billing/webhook", raw=payload, headers={"Stripe-Signature": "t=%d,v1=%s" % (ts, sig)})
    assert status == 200 and out["received"]
    assert c.call("GET", "/api/creators/me")[1]["user"]["plan"] == "free"

    assert c.call("POST", "/api/creators/logout", {})[0] == 200
    assert c.call("GET", "/api/creators/scans")[0] == 401
    status, out = c.call("POST", "/api/creators/login", {"email": "maya@example.com", "password": "longenough"})
    assert status == 200 and out["user"]["plan"] == "free"
    assert c.call("POST", "/api/creators/login", {"email": "maya@example.com", "password": "wrongpass1"})[0] == 401

    with psycopg.connect(server.DB_URL) as db:
        names = [r[0] for r in db.execute("SELECT DISTINCT name FROM events").fetchall()]
    assert {"signup", "scan", "paywall", "activity", "pitch", "checkout_intent"} <= set(names)
