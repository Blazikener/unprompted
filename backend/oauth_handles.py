"""OAuth ownership verification for creator handles."""
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import psycopg

import creator
import digest
import licenses
from server import ApiError, connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_states (
  state      text PRIMARY KEY,
  user_id    int NOT NULL REFERENCES users ON DELETE CASCADE,
  platform   text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
"""

PROVIDERS = {
    "tiktok": ("TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET"),
    "instagram": ("INSTAGRAM_APP_ID", "INSTAGRAM_APP_SECRET"),
}


def enabled(platform):
    key, secret = PROVIDERS[platform]
    return bool(os.environ.get(key) and os.environ.get(secret))


def status():
    return {platform: enabled(platform) for platform in PROVIDERS}


def callback_uri(platform):
    return "%s/auth/%s/callback/" % (digest.app_url(), platform)


def _post_form(url, data):
    req = Request(url, data=urlencode(data).encode(), method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def _get_json(url, headers):
    req = Request(url, headers=headers)
    with urlopen(req, timeout=15) as response:
        return json.load(response)


def _redirect(location):
    return creator.Reply({}, 302, [("Location", location), ("Cache-Control", "no-store")])


def start(handler, platform):
    if platform not in PROVIDERS or not enabled(platform):
        raise ApiError(404, "Not found.")
    user = creator.current_user(handler.headers)
    if not user:
        return _redirect("/verify?connect=signin")
    if not creator.has_role(user, "creator"):
        creator.require_role(handler.headers, "creator")

    state = secrets.token_urlsafe(32)
    with connect() as db:
        db.execute("DELETE FROM oauth_states WHERE created_at < now() - interval '1 day'")
        db.execute("INSERT INTO oauth_states (state, user_id, platform) VALUES (%s, %s, %s)",
                   (state, user["id"], platform))

    if platform == "tiktok":
        key, _ = PROVIDERS[platform]
        params = {
            "client_key": os.environ[key],
            "response_type": "code",
            "scope": "user.info.basic,user.info.profile",
            "redirect_uri": callback_uri(platform),
            "state": state,
        }
        target = "https://www.tiktok.com/v2/auth/authorize/?" + urlencode(params)
    else:
        app_id, _ = PROVIDERS[platform]
        params = {
            "client_id": os.environ[app_id],
            "redirect_uri": callback_uri(platform),
            "response_type": "code",
            "scope": "instagram_business_basic",
            "state": state,
        }
        target = "https://www.instagram.com/oauth/authorize?" + urlencode(params)
    return _redirect(target)


def _username(platform, code):
    if platform == "tiktok":
        token_response = _post_form("https://open.tiktokapis.com/v2/oauth/token/", {
            "client_key": os.environ["TIKTOK_CLIENT_KEY"],
            "client_secret": os.environ["TIKTOK_CLIENT_SECRET"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": callback_uri(platform),
        })
        token = token_response.get("access_token")
        if not token:
            raise ValueError("OAuth token missing.")
        profile = _get_json("https://open.tiktokapis.com/v2/user/info/?fields=open_id,username,display_name",
                            {"Authorization": "Bearer " + token})
        return profile["data"]["user"]["username"]

    app_id, app_secret = PROVIDERS[platform]
    token_response = _post_form("https://api.instagram.com/oauth/access_token", {
        "client_id": os.environ[app_id],
        "client_secret": os.environ[app_secret],
        "grant_type": "authorization_code",
        "redirect_uri": callback_uri(platform),
        "code": code,
    })
    token = token_response.get("access_token")
    if not token and isinstance(token_response.get("data"), list) and token_response["data"]:
        token = token_response["data"][0].get("access_token")
    if not token:
        raise ValueError("OAuth token missing.")
    profile_url = "https://graph.instagram.com/me?" + urlencode({
        "fields": "user_id,username",
        "access_token": token,
    })
    profile = _get_json(profile_url, {})
    return profile["username"]


def callback(handler, platform):
    handle = None
    try:
        query = parse_qs(urlparse(handler.path).query)
        state = (query.get("state") or [None])[0]
        with connect() as db:
            row = db.execute("DELETE FROM oauth_states WHERE state = %s"
                             " RETURNING user_id, platform, created_at", (state,)).fetchone() if state else None

        user = creator.current_user(handler.headers)
        if (query.get("error") or not enabled(platform) or not row or row["platform"] != platform or not user
                or row["user_id"] != user["id"]
                or row["created_at"] < datetime.now(timezone.utc) - timedelta(minutes=10)):
            return _redirect("/verify?connect=error")

        code = (query.get("code") or [None])[0]
        if not code:
            return _redirect("/verify?connect=error")
        _, handle = licenses.norm(platform, _username(platform, code))
        with connect() as db:
            claimed = db.execute(
                "INSERT INTO creator_handles (user_id, platform, handle, code) VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (user_id, platform, handle) DO UPDATE SET code = creator_handles.code RETURNING *",
                (user["id"], platform, handle, "UNP-" + secrets.token_hex(3).upper()),
            ).fetchone()
        licenses.mark_verified(claimed["id"], "oauth")
        return _redirect("/verify?" + urlencode({"connect": "done", "platform": platform, "handle": handle}))
    except ApiError as error:
        if error.status == 409 and handle:
            return _redirect("/verify?" + urlencode({"connect": "taken", "platform": platform, "handle": handle}))
        return _redirect("/verify?connect=error")
    except psycopg.errors.UniqueViolation:
        if handle:
            return _redirect("/verify?" + urlencode({"connect": "taken", "platform": platform, "handle": handle}))
        return _redirect("/verify?connect=error")
    except Exception:
        return _redirect("/verify?connect=error")
