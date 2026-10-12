"""OAuth handle verification uses mocked provider HTTP and local Postgres only."""
import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["DIGEST_SCHEDULER"] = "0"

import pytest  # noqa: E402

import creator  # noqa: E402
import licenses  # noqa: E402
import oauth_handles  # noqa: E402
import server  # noqa: E402


@pytest.fixture(scope="module")
def base():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_port
    httpd.shutdown()


@pytest.fixture
def sandbox(monkeypatch):
    prefix = "oauth-" + secrets.token_hex(6) + "-"
    server.init_db()
    monkeypatch.setenv("APP_URL", "https://u.test")
    monkeypatch.setenv("MULTI_ROLE_ACCOUNTS", "")
    for key in ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(licenses, "notify", lambda *args, **kwargs: False)
    yield prefix
    with server.connect() as db:
        db.execute("DELETE FROM users WHERE email LIKE %s", (prefix + "%",))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(base, path, cookie=None):
    headers = {"Cookie": cookie} if cookie else {}
    req = urllib.request.Request(base + path, headers=headers)
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(req, timeout=5) as response:
            body = response.read()
            return response.status, response.headers.get("Location"), body
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Location"), error.read()


def get_json(base, path):
    status, _, body = get(base, path)
    return status, json.loads(body)


def add_user(prefix, name, role="creator"):
    email = prefix + name + "@example.test"
    with server.connect() as db:
        user = db.execute(
            "INSERT INTO users (email, password, role) VALUES (%s, 'test', %s) RETURNING *",
            (email, role),
        ).fetchone()
        cookie = creator.start_session(db, user["id"])[1].split(";", 1)[0]
    return user, cookie


def add_state(state, user_id, platform="tiktok", age_minutes=0):
    with server.connect() as db:
        db.execute(
            "INSERT INTO oauth_states (state, user_id, platform, created_at)"
            " VALUES (%s, %s, %s, now() - make_interval(mins => %s))",
            (state, user_id, platform, age_minutes),
        )


def set_provider(monkeypatch, platform):
    if platform == "tiktok":
        monkeypatch.setenv("TIKTOK_CLIENT_KEY", "fake-client-key")
        monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "fake-client-secret")
    else:
        monkeypatch.setenv("INSTAGRAM_APP_ID", "fake-app-id")
        monkeypatch.setenv("INSTAGRAM_APP_SECRET", "fake-app-secret")


def callback(base, platform, state, cookie, **query):
    params = {"state": state, **query}
    return get(base, f"/auth/{platform}/callback/?" + urllib.parse.urlencode(params), cookie)


def connect_query(location):
    assert location.startswith("/verify?")
    return urllib.parse.parse_qs(urllib.parse.urlparse(location).query)


def test_provider_status_is_off_without_both_keys_and_on_with_both(base, monkeypatch, sandbox):
    assert get_json(base, "/api/handles/oauth") == (200, {"tiktok": False, "instagram": False})
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "key")
    assert get_json(base, "/api/handles/oauth")[1]["tiktok"] is False
    monkeypatch.setenv("TIKTOK_CLIENT_SECRET", "secret")
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    assert get_json(base, "/api/handles/oauth") == (200, {"tiktok": True, "instagram": True})


def test_disabled_and_signed_out_start(base, monkeypatch, sandbox):
    status, _, _ = get(base, "/auth/tiktok/start")
    assert status == 404
    set_provider(monkeypatch, "tiktok")
    status, location, _ = get(base, "/auth/tiktok/start")
    assert status == 302 and location == "/verify?connect=signin"


@pytest.mark.parametrize("platform", ["tiktok", "instagram"])
def test_start_redirect_has_static_callback_and_persists_state(base, monkeypatch, sandbox, platform):
    set_provider(monkeypatch, platform)
    user, cookie = add_user(sandbox, platform)
    stale = sandbox + "stale-" + platform
    add_state(stale, user["id"], platform=platform, age_minutes=60 * 25)
    status, location, _ = get(base, f"/auth/{platform}/start", cookie)
    assert status == 302
    parsed = urllib.parse.urlparse(location)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [f"https://u.test/auth/{platform}/callback/"]
    assert "?" not in params["redirect_uri"][0]
    state = params["state"][0]
    with server.connect() as db:
        row = db.execute("SELECT user_id, platform FROM oauth_states WHERE state = %s", (state,)).fetchone()
        old_count = db.execute("SELECT count(*) AS n FROM oauth_states WHERE state = %s", (stale,)).fetchone()["n"]
    assert row == {"user_id": user["id"], "platform": platform}
    assert old_count == 0
    if platform == "tiktok":
        assert parsed.scheme == "https" and parsed.netloc == "www.tiktok.com"
        assert parsed.path == "/v2/auth/authorize/"
        assert params["client_key"] == ["fake-client-key"]
        assert params["scope"] == ["user.info.basic,user.info.profile"]
        assert set(params) == {"client_key", "response_type", "scope", "redirect_uri", "state"}
        assert "fake-client-secret" not in location
    else:
        assert parsed.scheme == "https" and parsed.netloc == "www.instagram.com"
        assert parsed.path == "/oauth/authorize"
        assert params["client_id"] == ["fake-app-id"]
        assert params["scope"] == ["instagram_business_basic"]
        assert set(params) == {"client_id", "redirect_uri", "response_type", "scope", "state"}
        assert "fake-app-secret" not in location


def test_start_requires_creator_side(base, monkeypatch, sandbox):
    set_provider(monkeypatch, "tiktok")
    _, cookie = add_user(sandbox, "brand", role="brand")
    status, _, body = get(base, "/auth/tiktok/start", cookie)
    assert status == 403 and json.loads(body)["missing"] == "creator"


def test_invalid_callback_states_are_consumed_and_never_verify_a_handle(base, monkeypatch, sandbox):
    set_provider(monkeypatch, "tiktok")
    owner, owner_cookie = add_user(sandbox, "owner")
    _, other_cookie = add_user(sandbox, "other")
    expired, mismatched, denied = (sandbox + label for label in ("expired", "mismatch", "denied"))
    add_state(expired, owner["id"], age_minutes=11)
    add_state(mismatched, owner["id"])
    add_state(denied, owner["id"])

    for state, cookie, query in (
        ("unknown", owner_cookie, {"code": "x"}),
        (expired, owner_cookie, {"code": "x"}),
        (mismatched, other_cookie, {"code": "x"}),
        (denied, owner_cookie, {"error": "access_denied"}),
    ):
        status, location, _ = callback(base, "tiktok", state, cookie, **query)
        assert status == 302 and connect_query(location) == {"connect": ["error"]}

    status, location, _ = callback(base, "tiktok", denied, owner_cookie, code="reuse")
    assert status == 302 and connect_query(location) == {"connect": ["error"]}
    with server.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM oauth_states WHERE state = ANY(%s)",
                          ([expired, mismatched, denied],)).fetchone()["n"] == 0
        assert db.execute("SELECT count(*) AS n FROM creator_handles h JOIN users u ON u.id = h.user_id"
                          " WHERE u.email LIKE %s AND h.verified_at IS NOT NULL",
                          (sandbox + "%",)).fetchone()["n"] == 0


def test_tiktok_callback_verifies_normalized_username(base, monkeypatch, sandbox):
    set_provider(monkeypatch, "tiktok")
    user, cookie = add_user(sandbox, "tiktok-success")
    state = sandbox + "state"
    add_state(state, user["id"])
    posts, gets = [], []

    def post(url, data):
        posts.append((url, data))
        return {"access_token": "mock-access-token"}

    def get_profile(url, headers):
        gets.append((url, headers))
        return {"data": {"user": {"username": "Found.Creator", "display_name": "Found Creator"}}}

    monkeypatch.setattr(oauth_handles, "_post_form", post)
    monkeypatch.setattr(oauth_handles, "_get_json", get_profile)
    status, location, _ = callback(base, "tiktok", state, cookie, code="mock-code")
    assert status == 302
    assert connect_query(location) == {"connect": ["done"], "platform": ["tiktok"], "handle": ["found.creator"]}
    assert posts == [("https://open.tiktokapis.com/v2/oauth/token/", {
        "client_key": "fake-client-key",
        "client_secret": "fake-client-secret",
        "code": "mock-code",
        "grant_type": "authorization_code",
        "redirect_uri": "https://u.test/auth/tiktok/callback/",
    })]
    assert gets == [("https://open.tiktokapis.com/v2/user/info/?fields=open_id,username,display_name",
                     {"Authorization": "Bearer mock-access-token"})]
    with server.connect() as db:
        handle = db.execute("SELECT handle, verified_by, verified_at FROM creator_handles WHERE user_id = %s",
                            (user["id"],)).fetchone()
    assert handle["handle"] == "found.creator" and handle["verified_by"] == "oauth" and handle["verified_at"]


@pytest.mark.parametrize("token_response", [
    {"access_token": "mock-access-token"},
    {"data": [{"access_token": "mock-access-token"}]},
])
def test_instagram_callback_accepts_both_token_shapes(base, monkeypatch, sandbox, token_response):
    set_provider(monkeypatch, "instagram")
    notices = []
    monkeypatch.setattr(licenses, "notify", lambda *args, **kwargs: notices.append(args))
    user, cookie = add_user(sandbox, "instagram-" + str(bool(token_response.get("data"))))
    state = sandbox + "instagram-state-" + str(bool(token_response.get("data")))
    add_state(state, user["id"], platform="instagram")
    posts = []

    def post(url, data):
        posts.append((url, data))
        return token_response

    def get_profile(url, headers):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert params == {"fields": ["user_id,username"], "access_token": ["mock-access-token"]}
        assert headers == {}
        return {"user_id": "ig-user", "username": "Insta.Creator"}

    monkeypatch.setattr(oauth_handles, "_post_form", post)
    monkeypatch.setattr(oauth_handles, "_get_json", get_profile)
    status, location, _ = callback(base, "instagram", state, cookie, code="mock-code")
    assert status == 302
    assert connect_query(location) == {"connect": ["done"], "platform": ["instagram"], "handle": ["insta.creator"]}
    assert posts == [("https://api.instagram.com/oauth/access_token", {
        "client_id": "fake-app-id",
        "client_secret": "fake-app-secret",
        "grant_type": "authorization_code",
        "redirect_uri": "https://u.test/auth/instagram/callback/",
        "code": "mock-code",
    })]
    with server.connect() as db:
        row = db.execute("SELECT handle, verified_by FROM creator_handles WHERE user_id = %s", (user["id"],)).fetchone()
    assert row == {"handle": "insta.creator", "verified_by": "oauth"}
    assert notices == []


def test_already_verified_handle_redirects_as_taken(base, monkeypatch, sandbox):
    set_provider(monkeypatch, "tiktok")
    owner, _ = add_user(sandbox, "taken-owner")
    claimant, cookie = add_user(sandbox, "taken-claimant")
    claimed = licenses.claim(owner, {"platform": "tiktok", "handle": "already.taken"})
    licenses.mark_verified(claimed["id"], "bio")
    state = sandbox + "taken-state"
    add_state(state, claimant["id"])
    monkeypatch.setattr(oauth_handles, "_post_form", lambda *args: {"access_token": "mock-access-token"})
    monkeypatch.setattr(oauth_handles, "_get_json", lambda *args: {"data": {"user": {"username": "Already.Taken"}}})

    status, location, _ = callback(base, "tiktok", state, cookie, code="mock-code")
    assert status == 302
    assert connect_query(location) == {"connect": ["taken"], "platform": ["tiktok"], "handle": ["already.taken"]}
    with server.connect() as db:
        rows = db.execute("SELECT user_id, verified_by FROM creator_handles WHERE platform = 'tiktok' AND handle = 'already.taken'"
                          " ORDER BY user_id").fetchall()
    assert rows[0]["user_id"] == owner["id"] and rows[0]["verified_by"] == "bio"
    assert rows[1]["user_id"] == claimant["id"] and rows[1]["verified_by"] is None


def test_provider_http_failure_redirects_to_generic_error(base, monkeypatch, sandbox):
    set_provider(monkeypatch, "tiktok")
    user, cookie = add_user(sandbox, "http-failure")
    state = sandbox + "http-failure-state"
    add_state(state, user["id"])

    def fail(*args):
        raise OSError("private provider response")

    monkeypatch.setattr(oauth_handles, "_post_form", fail)
    status, location, _ = callback(base, "tiktok", state, cookie, code="mock-code")
    assert status == 302 and connect_query(location) == {"connect": ["error"]}
    assert "private provider response" not in location
    with server.connect() as db:
        assert db.execute("SELECT count(*) AS n FROM creator_handles WHERE user_id = %s AND verified_at IS NOT NULL",
                          (user["id"],)).fetchone()["n"] == 0
