import json
from urllib.error import HTTPError, URLError

import pytest

from backend import tiktok_public


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
