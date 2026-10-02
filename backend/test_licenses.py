"""Creator side of licensing: offer links, handle claims, automatic rules, inbox (mocked Oriane and TikTok).

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_licenses.py
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
import licenses  # noqa: E402
import server  # noqa: E402
import tiktok_public  # noqa: E402

EMAILS = ("ali@creator.test", "other@creator.test")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    server.init_db()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM digests")
        db.execute("DELETE FROM mentions WHERE search_id IN (SELECT id FROM searches WHERE brand = 'Tim Hortons')")
        db.execute("DELETE FROM searches WHERE brand = 'Tim Hortons'")
        db.execute("DELETE FROM users WHERE email = ANY(%s)", (list(EMAILS),))
    monkeypatch.setenv("LICENSE_NOTIFY_EMAIL", "ops@u.test")


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(digest, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


def user(email):
    with psycopg.connect(server.DB_URL, row_factory=psycopg.rows.dict_row) as db:
        return db.execute("INSERT INTO users (email, password) VALUES (%s, %s) RETURNING *",
                          (email, creator.hash_password("password-123"))).fetchone()


def video(vid, handle="ali", views=1200):
    text = "a Tim Hortons iced capp every single morning"
    return {"id": vid, "platform": "tiktok", "platformId": vid, "profileHandle": handle, "viewsCount": views, "caption": "", "hashtags": [],
            "mentions": [], "coAuthors": [], "transcript": text, "transcriptChunks": [{"startSeconds": 12.0, "endSeconds": 16.0, "text": text}],
            "publishedAt": (date.today() - timedelta(days=1)).isoformat() + "T10:00:00Z", "frames": [], "thumbnailMediaUrl": ""}


def request_for(monkeypatch, *vids, handle="ali"):
    """A watch whose weekly report had these videos, and a 30-day request for each; returns their creator tokens."""
    d = digest.subscribe({"email": "brand@example.com", "brand": "Tim Hortons"})
    monkeypatch.setattr(server, "oriane", lambda *a, **k: {"data": {"results": [video(v, handle) for v in vids],
                                                                    "aggregations": {"totalViewsCount": 0}}, "metadata": {}})
    digest.run_due(force=True)
    for v in vids:
        digest.license_view(d["token"], v, {"days": 30})
    with psycopg.connect(server.DB_URL) as db:
        return [db.execute("SELECT creator_token FROM license_requests WHERE video_id = %s ORDER BY id DESC LIMIT 1", (v,)).fetchone()[0]
                for v in vids]


def test_offer_link_answers_without_an_account(monkeypatch, mail):
    [token] = request_for(monkeypatch, "lo1")
    assert any(licenses.offer_url(token) in body for to, _, body in mail if to == "ops@u.test")   # the operator gets the link
    offer = licenses.offer_view(licenses.load_offer(token))
    assert offer["brand"] == "Tim Hortons" and offer["video"]["handle"] == "ali" and offer["claimed"] is False
    assert offer["shareUsd"] == 42 and offer["brandPriceUsd"] == 50 and offer["status"] == "requested"

    with pytest.raises(server.ApiError) as err:
        licenses.answer(token, {"action": "code", "code": "X"})
    assert err.value.status == 409
    n = len(mail)
    countered = licenses.answer(token, {"action": "counter", "price": 60})
    assert countered["status"] == "contacted" and countered["shareUsd"] == 60 and countered["brandPriceUsd"] == 71
    assert countered["respondedVia"] == "app" and "countered: wants $60" in mail[n][1]
    assert licenses.answer(token, {"action": "accept"})["status"] == "accepted"
    coded = licenses.answer(token, {"action": "code", "code": " SPARK-123 "})
    assert coded["adCode"] == "SPARK-123" and "sent the ad code" in mail[-1][1]
    assert licenses.answer(token, {"action": "decline", "reason": "changed my mind"})["status"] == "declined"
    for body in ({"action": "accept"}, {"action": "nope"}):
        with pytest.raises(server.ApiError):
            licenses.answer(token, body)
    with pytest.raises(server.ApiError) as err:
        licenses.load_offer("0" * 32)
    assert err.value.status == 404


def test_claim_verify_rules_and_inbox(monkeypatch, mail):
    ali, other = user(EMAILS[0]), user(EMAILS[1])
    claim = licenses.claim(ali, {"platform": "tiktok", "handle": "@Ali"})
    assert claim["handle"] == "ali" and claim["code"].startswith("UNP-") and not claim["verified"]
    bios = {"ali": "coffee every day " + claim["code"]}
    monkeypatch.setattr(tiktok_public, "creator_page", lambda h: ({"signature": bios[h]} if h in bios else None, []))
    assert licenses.verify(ali, {"platform": "tiktok", "handle": "ali"})["verified"]
    # Someone else can start a claim, but not verify a handle that's already taken.
    other_claim = licenses.claim(other, {"platform": "tiktok", "handle": "ali"})
    bios["ali"] += " " + other_claim["code"]
    with pytest.raises(server.ApiError) as err:
        licenses.verify(other, {"platform": "tiktok", "handle": "ali"})
    assert err.value.status == 409 and "already verified" in str(err.value)
    licenses.claim(other, {"platform": "tiktok", "handle": "nobio"})
    with pytest.raises(server.ApiError) as err:
        licenses.verify(other, {"platform": "tiktok", "handle": "nobio"})
    assert err.value.status == 503
    ig = licenses.claim(other, {"platform": "instagram", "handle": "other.ig"})
    with pytest.raises(server.ApiError) as err:
        licenses.verify(other, {"platform": "instagram", "handle": "other.ig"})
    assert "checked by hand" in str(err.value) and any("Instagram handle claim" in s for _, s, _ in mail)
    assert licenses.mark_verified(ig["id"], "operator")["verifiedBy"] == "operator"

    # Rules: below the minimum is declined for them (no email); an approved brand is accepted (email says so).
    licenses.set_prefs(ali, {"minPriceUsd": 100, "approveBrands": [], "blockBrands": []})
    n = len(mail)
    [low] = request_for(monkeypatch, "lo2")
    offer = licenses.offer_view(licenses.load_offer(low))
    assert offer["status"] == "declined" and offer["respondedVia"] == "auto" and "$100 minimum" in offer["declineReason"]
    assert not any(to == EMAILS[0] for to, _, _ in mail[n:]) and any("own rule" in b for _, _, b in mail[n:])
    licenses.set_prefs(ali, {"minPriceUsd": None, "approveBrands": ["tim hortons", " "], "blockBrands": []})
    n = len(mail)
    [yes] = request_for(monkeypatch, "lo3")
    assert licenses.offer_view(licenses.load_offer(yes))["status"] == "accepted"
    assert any(to == EMAILS[0] and "will run your video" in s and licenses.offer_url(yes) in b for to, s, b in mail[n:])

    inbox = licenses.mine(ali)
    assert [o["status"] for o in inbox["offers"]][:2] == ["accepted", "declined"] and inbox["prefs"]["approveBrands"] == ["tim hortons"]
    assert [h["handle"] for h in inbox["handles"]] == ["ali"] and inbox["money"] == {"paidUsd": 0, "owedUsd": 0, "live": 0}
    assert licenses.mine(other)["offers"] == []             # pending or other handles see nothing
    assert licenses.remove(ali, {"platform": "tiktok", "handle": "ali"})["handles"] == []
    with pytest.raises(server.ApiError):
        licenses.claim(ali, {"platform": "youtube", "handle": "ali"})


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
            return res.status, json.load(res) if res.headers.get("Content-Type", "").startswith("application/json") else res.read()
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_http_routes(base, monkeypatch, mail):
    [token] = request_for(monkeypatch, "lo4")
    status, offer = call(base, "GET", "/api/licenses/offer/" + token)
    assert status == 200 and offer["shareUsd"] == 42
    assert call(base, "POST", "/api/licenses/offer/" + token, {"action": "accept"})[1]["status"] == "accepted"
    assert call(base, "GET", "/api/licenses/offer/" + "f" * 32)[0] == 404
    assert call(base, "GET", "/api/licenses/mine")[0] == 401
    assert call(base, "POST", "/api/licenses/handles", {"platform": "tiktok", "handle": "x"})[0] == 401
    op = {"Authorization": "Bearer run-secret"}
    assert call(base, "GET", "/api/admin/handles")[0] == 401 and call(base, "GET", "/api/admin/handles", headers=op)[0] == 200
    row = next(r for r in call(base, "GET", "/api/admin/licenses", headers=op)[1]["requests"] if r["offerUrl"].endswith(token))
    assert row["respondedVia"] == "app" and row["claimed"] is False and row["status"] == "accepted"
    for page, marker in (("/offer/" + token, b"Offer for your video"), ("/creators/licenses", b"License offers")):
        status, html = call(base, "GET", page)
        assert status == 200 and marker in html
