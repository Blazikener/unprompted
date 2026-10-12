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


def relay_response(body, *, ok=True, status=200):
    return Response(json.dumps({"ok": ok, "status": status, "body": body}).encode())


def test_direct_503_uses_relay_body(monkeypatch):
    relay_env(monkeypatch)
    direct_error = HTTPError("https://www.tiktok.com/embed/@maya.eats", 503, "Unavailable", None, None)
    calls = []

    def urlopen(req, timeout):
        calls.append((req, timeout))
        if req.full_url == "https://script.example/exec":
            return relay_response("<html>relay page</html>")
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    assert tiktok_public.get("https://www.tiktok.com/embed/@maya.eats") == "<html>relay page</html>"
    assert [req.full_url for req, _ in calls] == [
        "https://www.tiktok.com/embed/@maya.eats",
        "https://script.example/exec",
    ]
    assert all(timeout == tiktok_public.TIMEOUT for _, timeout in calls)


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

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if req.full_url == "https://script.example/exec":
            return Response(json.dumps(payload).encode())
        raise direct_error

    monkeypatch.setattr(tiktok_public.request, "urlopen", urlopen)
    with pytest.raises(HTTPError) as raised:
        tiktok_public.get("https://www.tiktok.com/embed/@maya.eats")
    assert raised.value is direct_error
    assert calls == ["https://www.tiktok.com/embed/@maya.eats", "https://script.example/exec"]


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
