import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from backend import tiktok_public

JSON_LD_FIXTURE = Path(__file__).parent / "fixtures" / "tiktok-video-jsonld.html"


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture(autouse=True)
def reset_direct_failure_window(monkeypatch):
    monkeypatch.setattr(tiktok_public, "_direct_failure_until", 0.0)


def relay_env(monkeypatch):
    monkeypatch.setenv("MAIL_RELAY_URL", "https://script.example/exec")
    monkeypatch.setenv("MAIL_RELAY_SECRET", "test-secret")


def relay_response(body, *, ok=True, status=200, error=None):
    payload = {"ok": ok, "status": status, "body": body}
    if error is not None:
        payload["error"] = error
    return Response(json.dumps(payload).encode())


def test_direct_503_retries_embed_relay_then_returns_body(monkeypatch, capsys):
    relay_env(monkeypatch)
    direct_error = HTTPError("https://www.tiktok.com/embed/@maya.eats", 503, "Unavailable", None, None)
    calls = []
    sleeps = []
    relay_error = "failed at https://script.example/exec with test-secret"

    def urlopen(req, timeout):
        calls.append((req, timeout))
        if req.full_url == "https://script.example/exec":
            if sum(call.full_url == req.full_url for call, _ in calls) == 1:
                return relay_response("", status=503, error=relay_error)
            return relay_response("<html>relay page</html>")
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    monkeypatch.setattr(tiktok_public.time, "sleep", sleeps.append)
    assert tiktok_public.get("https://www.tiktok.com/embed/@maya.eats") == "<html>relay page</html>"
    assert [req.full_url for req, _ in calls] == [
        "https://www.tiktok.com/embed/@maya.eats",
        "https://script.example/exec",
        "https://script.example/exec",
    ]
    assert all(timeout == tiktok_public.TIMEOUT for _, timeout in calls)
    assert sleeps == [1.5]
    stderr = capsys.readouterr().err
    assert "relay failed: status=503" in stderr
    assert "via relay" in stderr
    assert "test-secret" not in stderr
    assert "https://script.example/exec" not in stderr


def test_embed_relay_retries_three_times_then_reraises_direct_error(monkeypatch, capsys):
    relay_env(monkeypatch)
    direct_error = HTTPError("https://www.tiktok.com/embed/@maya.eats", 503, "Unavailable", None, None)
    relay_calls = []
    sleeps = []

    def urlopen(req, timeout):
        if req.full_url == "https://script.example/exec":
            relay_calls.append(req.full_url)
            return relay_response("", status=503, error="TikTok overloaded")
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    monkeypatch.setattr(tiktok_public.time, "sleep", sleeps.append)
    with pytest.raises(HTTPError) as raised:
        tiktok_public.get("https://www.tiktok.com/embed/@maya.eats")
    assert raised.value is direct_error
    assert len(relay_calls) == 3
    assert sleeps == [1.5, 3]
    stderr = capsys.readouterr().err
    assert "relay failed: status=503" in stderr
    assert "test-secret" not in stderr
    assert "https://script.example/exec" not in stderr


def test_embed_retries_relay_network_error(monkeypatch, capsys):
    relay_env(monkeypatch)
    direct_error = HTTPError("https://www.tiktok.com/embed/@maya.eats", 503, "Unavailable", None, None)
    relay_calls = []
    sleeps = []

    def urlopen(req, timeout):
        if req.full_url == "https://script.example/exec":
            relay_calls.append(req.full_url)
            if len(relay_calls) == 1:
                raise URLError("timed out at https://script.example/exec with test-secret")
            return relay_response("<html>relay page</html>")
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    monkeypatch.setattr(tiktok_public.time, "sleep", sleeps.append)
    assert tiktok_public.get("https://www.tiktok.com/embed/@maya.eats") == "<html>relay page</html>"
    assert len(relay_calls) == 2
    assert sleeps == [1.5]
    stderr = capsys.readouterr().err
    assert "relay failed: status=None" in stderr
    assert "test-secret" not in stderr
    assert "https://script.example/exec" not in stderr


def test_unconfigured_relay_reraises_original_http_error(monkeypatch):
    monkeypatch.delenv("MAIL_RELAY_URL", raising=False)
    monkeypatch.delenv("MAIL_RELAY_SECRET", raising=False)
    direct_error = HTTPError("https://www.tiktok.com/embed/@maya.eats", 503, "Unavailable", None, None)
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    with pytest.raises(HTTPError) as raised:
        tiktok_public.get("https://www.tiktok.com/embed/@maya.eats")
    assert raised.value is direct_error
    assert calls == ["https://www.tiktok.com/embed/@maya.eats"]


def test_non_allowlisted_url_never_uses_relay(monkeypatch):
    relay_env(monkeypatch)
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        raise URLError("blocked")

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    with pytest.raises(URLError):
        tiktok_public.get("https://v16-webapp.tiktok.com/caption.vtt")
    assert calls == ["https://v16-webapp.tiktok.com/caption.vtt"]


@pytest.mark.parametrize(
    "payload",
    [
        {"ok": True, "status": 403, "body": "<html>blocked</html>"},
        {"ok": False, "status": 200, "body": "<html>error</html>"},
    ],
)
def test_rejected_relay_response_reraises_original_error(monkeypatch, payload):
    relay_env(monkeypatch)
    direct_error = HTTPError("https://www.tiktok.com/embed/@maya.eats", 503, "Unavailable", None, None)
    calls = []
    sleeps = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if req.full_url == "https://script.example/exec":
            return Response(json.dumps(payload).encode())
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    monkeypatch.setattr(tiktok_public.time, "sleep", sleeps.append)
    with pytest.raises(HTTPError) as raised:
        tiktok_public.get("https://www.tiktok.com/embed/@maya.eats")
    assert raised.value is direct_error
    assert calls == ["https://www.tiktok.com/embed/@maya.eats"] + ["https://script.example/exec"] * 3
    assert sleeps == [1.5, 3]


def test_url_not_allowed_relay_response_is_not_retried(monkeypatch):
    relay_env(monkeypatch)
    direct_error = HTTPError("https://www.tiktok.com/embed/@maya.eats", 503, "Unavailable", None, None)
    calls = []
    sleeps = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if req.full_url == "https://script.example/exec":
            return relay_response("", ok=False, status=403, error="URL not allowed")
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    monkeypatch.setattr(tiktok_public.time, "sleep", sleeps.append)
    with pytest.raises(HTTPError) as raised:
        tiktok_public.get("https://www.tiktok.com/embed/@maya.eats")
    assert raised.value is direct_error
    assert calls == ["https://www.tiktok.com/embed/@maya.eats", "https://script.example/exec"]
    assert sleeps == []


def test_video_page_relay_503_is_attempted_once(monkeypatch):
    relay_env(monkeypatch)
    direct_error = HTTPError("https://www.tiktok.com/@maya.eats/video/123456789", 503, "Unavailable", None, None)
    relay_calls = []
    sleeps = []

    def urlopen(req, timeout):
        if req.full_url == "https://script.example/exec":
            relay_calls.append(req.full_url)
            return relay_response("", status=503, error="TikTok overloaded")
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    monkeypatch.setattr(tiktok_public.time, "sleep", sleeps.append)
    with pytest.raises(HTTPError) as raised:
        tiktok_public.get("https://www.tiktok.com/@maya.eats/video/123456789")
    assert raised.value is direct_error
    assert len(relay_calls) == 1
    assert sleeps == []


def test_recent_direct_failure_tries_relay_first(monkeypatch):
    relay_env(monkeypatch)
    direct_error = HTTPError("https://www.tiktok.com/embed/@maya.eats", 503, "Unavailable", None, None)
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if req.full_url == "https://script.example/exec":
            return relay_response("<html>relay page</html>")
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    assert tiktok_public.get("https://www.tiktok.com/embed/@maya.eats") == "<html>relay page</html>"
    calls.clear()
    assert tiktok_public.get("https://www.tiktok.com/@maya.eats/video/123456789") == "<html>relay page</html>"
    assert calls == ["https://script.example/exec"]


def test_video_detail_without_rehydration_data_returns_none(monkeypatch, capsys):
    monkeypatch.setattr(
        tiktok_public.request,
        "urlopen",
        lambda req, timeout: Response(b"<html><body>JSON-LD only</body></html>"),
    )
    assert tiktok_public.video_detail("maya.eats", "123456789") is None
    stderr = capsys.readouterr().err
    assert "video 123456789: no item data in page" in stderr
    assert "TypeError" not in stderr


def test_ld_item_parses_relay_video_jsonld():
    item = tiktok_public.ld_item(JSON_LD_FIXTURE.read_text(), "7502836286051421471")
    assert item["desc"] == "Things I will NEVER do when I’m a mom.. 👶🤱 #GRWM #beautytok #makeup #makeuptok #storytime #fyp"
    assert item["createTime"] == 1746890228
    assert item["stats"] == {
        "playCount": 12300000, "diggCount": 2000000, "shareCount": 30600, "commentCount": 11500,
    }
    assert item["author"] == {"uniqueId": "haileyvanrenen", "nickname": "Hailey van Renen"}
    assert item["video"]["duration"] == 72
    assert {"hashtagName": "GRWM"} in item["textExtra"]
    assert item["captionOnly"] is True


@pytest.mark.parametrize("html", ["<html><body>no structured data</body></html>",
                                  '<script id="VideoObject">{"uploadDate":</script>'])
def test_ld_item_returns_none_for_missing_or_malformed_jsonld(html):
    assert tiktok_public.ld_item(html, "7502836286051421471") is None


def test_video_detail_prefers_rehydration_item(monkeypatch):
    video_id = "7502836286051421471"
    item = {"id": video_id, "desc": "hydrated"}
    state = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": item}}}}
    html = ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
            + json.dumps(state) + "</script>")
    calls = []
    monkeypatch.setattr(tiktok_public, "get", lambda url: calls.append(url) or html)
    assert tiktok_public.video_detail("haileyvanrenen", video_id) == item
    assert calls == [tiktok_public.VIDEO_URL % ("haileyvanrenen", video_id)]


def test_video_detail_uses_jsonld_without_refetching(monkeypatch, capsys):
    video_id = "7502836286051421471"
    html = JSON_LD_FIXTURE.read_text()
    calls = []
    monkeypatch.setattr(tiktok_public, "get", lambda url: calls.append(url) or html)
    item = tiktok_public.video_detail("haileyvanrenen", video_id)
    assert item == tiktok_public.ld_item(html, video_id)
    assert calls == [tiktok_public.VIDEO_URL % ("haileyvanrenen", video_id)]
    assert "video %s: caption only (json-ld)" % video_id in capsys.readouterr().err


def test_fetch_uses_jsonld_and_embed_caption_fallback(monkeypatch):
    first_id, second_id = "7502836286051421471", "7502836286051421472"
    entries = [
        {"id": first_id, "desc": "JSON-LD caption #GRWM", "playCount": 12300000,
         "coverUrl": "https://cdn.example/first.jpg", "authorUniqueId": "haileyvanrenen"},
        {"id": second_id, "desc": "Embed caption #coffee @maya.eats", "playCount": 900,
         "coverUrl": "https://cdn.example/second.jpg", "authorUniqueId": "haileyvanrenen"},
    ]
    embed_state = {"userInfo": {"uniqueId": "haileyvanrenen", "nickname": "Hailey", "avatarThumbUrl": "https://cdn.example/avatar.jpg"},
                   "videoList": entries}
    embed_html = ('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">'
                  + json.dumps(embed_state) + "</script>")
    detail_urls = []

    def fake_get(url):
        if url == tiktok_public.EMBED_URL % "haileyvanrenen":
            return embed_html
        detail_urls.append(url)
        if url.endswith("/" + first_id):
            return JSON_LD_FIXTURE.read_text()
        raise URLError("video page unavailable")

    monkeypatch.setattr(tiktok_public, "get", fake_get)
    videos = tiktok_public.fetch("haileyvanrenen")
    assert len(videos) == 2
    assert all(video["captionOnly"] for video in videos)
    assert [video["platformId"] for video in videos] == [first_id, second_id]
    assert videos[0]["thumbnailMediaUrl"] == "https://cdn.example/first.jpg"
    assert videos[0]["publishedAt"] == "2025-05-10T15:17:08Z"
    assert videos[1]["caption"] == entries[1]["desc"]
    assert videos[1]["publishedAt"] == tiktok_public.iso(int(second_id) >> 32)
    assert videos[1]["thumbnailMediaUrl"] == "https://cdn.example/second.jpg"
    assert videos[1]["profilePictureUrl"] == "https://cdn.example/avatar.jpg"
    assert len(detail_urls) == 2


def test_subtitles_skips_caption_only_items(monkeypatch):
    monkeypatch.setattr(tiktok_public, "get", lambda _url: pytest.fail("caption-only item fetched subtitles"))
    assert tiktok_public.subtitles(
        {"captionOnly": True, "video": {"subtitleInfos": [{"Url": "https://example.com"}]}}
    ) == ([], None)
