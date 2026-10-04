"""The UI system's backend pieces: the landing showcase (only an operator-picked search, never someone's private one),
mention flags, and the shared design-system files being served.

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_ui.py
"""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")

import psycopg  # noqa: E402
import pytest  # noqa: E402

import server  # noqa: E402


@pytest.fixture(scope="module")
def base():
    server.init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


def call(base, method, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as res:
            raw = res.read()
            return res.status, (json.loads(raw) if res.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def video(vid, transcript, kind_caption="", views=1000, handle="fan"):
    return {"id": vid, "platform": "tiktok", "platformId": vid, "profileHandle": handle, "viewsCount": views, "caption": kind_caption,
            "hashtags": [], "mentions": [], "coAuthors": [], "transcript": transcript, "thumbnailMediaUrl": "https://img.test/%s.jpg" % vid,
            "transcriptChunks": [{"startSeconds": 12.0, "endSeconds": 16.0, "text": transcript}] if transcript else [],
            "publishedAt": "2026-09-20T10:00:00Z", "frames": []}


def test_showcase_shows_only_the_picked_search(base, monkeypatch):
    monkeypatch.delenv("SHOWCASE_SEARCH_ID", raising=False)
    assert call(base, "GET", "/api/showcase") == (200, {"receipts": []})          # nothing private leaks by default
    vids = [video("sc1", "I always get a Showcase Coffee latte", views=900), video("sc2", "Showcase Coffee before class", views=5000),
            video("sc3", "morning", kind_caption="#showcasecoffee", views=7000), video("sc4", "no mention here", views=99999)]
    page = {"data": {"results": vids, "aggregations": {"totalViewsCount": 0}}, "metadata": {}}
    sid = server.new_search("Showcase Coffee", {"variants": [], "platform": "all", "lang": "any", "days": 365, "match": 2}, page)
    server.store_results(sid, vids, "Showcase Coffee", [])
    monkeypatch.setenv("SHOWCASE_SEARCH_ID", str(sid))
    server.SHOWCASE.clear()
    status, out = call(base, "GET", "/api/showcase")
    assert status == 200 and out["brand"] == "Showcase Coffee" and out["analyzed"] == 4
    assert [r["kind"] for r in out["receipts"]] == ["spoken", "spoken"]           # a hashtag with nothing said has no quote to show
    first = out["receipts"][0]
    assert first["views"] == 5000 and first["quote"]["start"] == 12.0 and first["frame"] and first["url"].startswith("https://www.tiktok.com/")
    assert set(first["quote"]) == {"text", "hit", "start"}
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM mentions WHERE search_id = %s", (sid,))
        db.execute("DELETE FROM searches WHERE id = %s", (sid,))


def test_mention_flags(base):
    assert call(base, "POST", "/api/feedback", {"videoId": "x", "reason": "because"})[0] == 400
    assert call(base, "POST", "/api/feedback", {"videoId": "../etc", "reason": "not_a_mention"})[0] == 400
    assert call(base, "POST", "/api/feedback", {"searchId": 7, "videoId": "sc2", "reason": "not_a_mention", "note": "x" * 900}) == (200, {"ok": True})
    with psycopg.connect(server.DB_URL) as db:
        data = db.execute("SELECT data FROM events WHERE name = 'mention_flag' ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert data["video"] == "sc2" and data["reason"] == "not_a_mention" and len(data["note"]) == 300


def test_design_system_is_served(base):
    for path, marker in (("/ui/ui.css", b"@layer reset, tokens, base, components, utilities, page;"), ("/ui/ui.js", b"window.U ="),
                         ("/ui/kit.html", b"Make the receipt the whole interface.")):
        status, body = call(base, "GET", path)
        assert status == 200 and marker in body
