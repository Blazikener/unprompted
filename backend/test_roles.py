"""Accounts keep one role and role-specific APIs reject other accounts."""
import http.cookiejar
import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["DIGEST_SCHEDULER"] = "0"

import pytest
from psycopg.types.json import Jsonb

import creator  # noqa: E402
import rosters  # noqa: E402
import server  # noqa: E402


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


@pytest.fixture(autouse=True)
def schema_and_cleanup():
    server.init_db()
    yield
    with server.connect() as db:
        users = db.execute("SELECT id FROM users WHERE email LIKE 'roles-%@example.test'").fetchall()
        ids = [row["id"] for row in users]
        if ids:
            db.execute("DELETE FROM events WHERE user_id = ANY(%s)", (ids,))
            db.execute("DELETE FROM searches WHERE user_id = ANY(%s)", (ids,))
            db.execute("DELETE FROM checks WHERE user_id = ANY(%s)", (ids,))
            db.execute("DELETE FROM digests WHERE user_id = ANY(%s)", (ids,))
            db.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))


class Client:
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method, path, body=None):
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with self.opener.open(req, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)


def signup(client, role=None):
    email = "roles-%s@example.test" % secrets.token_hex(8)
    body = {"email": email, "password": "password-123"}
    if role is not None:
        body["role"] = role
    return client.request("POST", "/api/creators/signup", body)


def test_signup_roles_default_invalid_and_me(base):
    creator_client, brand_client, manager_client = (Client(base) for _ in range(3))
    status, creator_signup = signup(creator_client)
    assert status == 201 and creator_signup["user"]["role"] == "creator"
    status, brand_signup = signup(brand_client, "brand")
    assert status == 201 and brand_signup["user"]["role"] == "brand"
    status, manager_signup = signup(manager_client, "manager")
    assert status == 201 and manager_signup["user"]["role"] == "manager"
    assert creator_client.request("GET", "/api/creators/me")[1]["user"]["role"] == "creator"
    assert brand_client.request("GET", "/api/creators/me")[1]["user"]["role"] == "brand"
    assert manager_client.request("GET", "/api/creators/me")[1]["user"]["role"] == "manager"
    status, invalid = signup(Client(base), "admin")
    assert status == 400 and "creator, brand or manager" in invalid["error"]


def test_role_gates_return_role_and_search_list_is_brand_only(base):
    creator_client, brand_client = Client(base), Client(base)
    status, creator_signup = signup(creator_client)
    assert status == 201
    assert signup(brand_client, "brand")[0] == 201

    status, error = brand_client.request("POST", "/api/creators/scans", {"platform": "tiktok", "handle": "maya.eats"})
    assert status == 403 and error["role"] == "brand" and "creator accounts" in error["error"]

    for path, body in (
        ("/api/searches", {"brand": "Alpha"}),
        ("/api/checks", {"platform": "tiktok", "handle": "maya.eats", "brand": "Alpha"}),
    ):
        status, error = creator_client.request("POST", path, body)
        assert status == 403 and error["role"] == "creator" and "brand accounts" in error["error"]
    assert creator_client.request("GET", "/api/searches") == (200, [])

    status, error = creator_client.request("GET", "/api/rosters/mine")
    assert status == 403 and error["role"] == "creator" and "manager accounts" in error["error"]
    status, error = brand_client.request("GET", "/api/licenses/mine")
    assert status == 403 and error["role"] == "brand" and "creator accounts" in error["error"]
    assert Client(base).request("GET", "/api/rosters/mine")[0] == 401

    with pytest.raises(server.ApiError) as error:
        rosters.admin_grant({"email": creator_signup["user"]["email"]})
    assert error.value.status == 409
    assert str(error.value) == "That account isn't a manager account; ask them to sign up at /creators/roster."


def test_dashboard_contains_only_the_signed_in_role_section(base, monkeypatch):
    monkeypatch.setattr(creator, "brand_usage", lambda user: pytest.fail("non-brand dashboard queried brand usage"))
    creator_client = Client(base)
    assert signup(creator_client)[0] == 201
    creator_data = creator_client.request("GET", "/api/creators/dashboard")[1]
    assert creator_data["role"] == "creator"
    assert creator_data["creator"] is not None and creator_data["licenses"] is not None
    assert creator_data["brand"] is creator_data["manager"] is None
    assert set(creator_data["usage"]) == {"scans"}

    monkeypatch.undo()
    brand_client = Client(base)
    assert signup(brand_client, "brand")[0] == 201
    brand_data = brand_client.request("GET", "/api/creators/dashboard")[1]
    assert brand_data["role"] == "brand"
    assert brand_data["brand"] is not None
    assert brand_data["creator"] is brand_data["licenses"] is brand_data["manager"] is None
    assert set(brand_data["usage"]) == {"searches", "checks"}

    manager_client = Client(base)
    assert signup(manager_client, "manager")[0] == 201
    manager_data = manager_client.request("GET", "/api/creators/dashboard")[1]
    assert manager_data["role"] == "manager"
    assert manager_data["manager"] is not None
    assert manager_data["creator"] is manager_data["licenses"] is manager_data["brand"] is None
    assert manager_data["usage"] == {}


def test_role_backfill_is_idempotent_and_creator_with_scans_stays_creator():
    with server.connect() as db:
        db.execute("ALTER TABLE users ALTER COLUMN role DROP NOT NULL")
        rows = {}
        for label in ("brand", "manager", "creator", "mixed"):
            rows[label] = db.execute(
                "INSERT INTO users (email, password, role) VALUES (%s, 'test', NULL) RETURNING id",
                ("roles-%s@example.test" % label,),
            ).fetchone()["id"]
        search_ids = [
            db.execute(
                "INSERT INTO searches (brand, params, total_count, total_views, user_id) "
                "VALUES (%s, %s, 0, 0, %s) RETURNING id",
                (label, Jsonb({}), rows[label]),
            ).fetchone()["id"]
            for label in ("brand", "mixed")
        ]
        db.execute("INSERT INTO rosters (user_id) VALUES (%s)", (rows["manager"],))
        db.execute(
            "INSERT INTO scans (user_id, platform, handle, source, profile, brands) "
            "VALUES (%s, 'tiktok', 'roles.mixed', 'demo', %s, %s)",
            (rows["mixed"], Jsonb({}), Jsonb([])),
        )

    server.init_db()
    server.init_db()
    with server.connect() as db:
        roles = {
            row["id"]: row["role"]
            for row in db.execute("SELECT id, role FROM users WHERE id = ANY(%s)", (list(rows.values()),))
        }
        not_null = db.execute(
            "SELECT attnotnull FROM pg_attribute WHERE attrelid = 'users'::regclass AND attname = 'role'"
        ).fetchone()["attnotnull"]
        constraint = db.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid = 'users'::regclass AND conname = 'users_role_check'"
        ).fetchone()
    assert roles == {
        rows["brand"]: "brand",
        rows["manager"]: "manager",
        rows["creator"]: "creator",
        rows["mixed"]: "creator",
    }
    assert not_null and constraint
    assert len(search_ids) == 2


def test_listed_accounts_use_both_creator_and_brand_sides(base, monkeypatch):
    dual, plain = Client(base), Client(base)
    status, signed = signup(dual)
    assert status == 201 and signed["user"]["roles"] == ["creator"]
    assert signup(plain)[0] == 201
    monkeypatch.setenv("MULTI_ROLE_ACCOUNTS", "someone@else.test, %s" % signed["user"]["email"].upper())

    me = dual.request("GET", "/api/creators/me")[1]["user"]
    assert me["role"] == "creator" and me["roles"] == ["creator", "brand"]
    status, profile = dual.request("POST", "/api/collabs/profile", {"brand": "RolesTest Dual", "about": "", "open": True})
    assert status == 200 and profile["brand"] == "RolesTest Dual"
    assert dual.request("GET", "/api/searches")[0] == 200

    inbox = dual.request("GET", "/api/collabs/mine")[1]
    assert inbox["role"] == "creator" and inbox["roles"] == ["creator", "brand"] and "watch" in inbox
    brand_inbox = dual.request("GET", "/api/collabs/mine?as=brand")[1]
    assert brand_inbox["role"] == "brand" and brand_inbox["profile"]["brand"] == "RolesTest Dual"

    as_brand = dual.request("GET", "/api/creators/dashboard?as=brand")[1]
    assert as_brand["role"] == "brand" and as_brand["brand"] is not None and as_brand["creator"] is None
    assert dual.request("GET", "/api/creators/dashboard")[1]["role"] == "creator"
    assert dual.request("GET", "/api/creators/dashboard?as=manager")[1]["role"] == "creator"
    assert dual.request("GET", "/api/rosters/mine")[0] == 403

    status, error = plain.request("POST", "/api/collabs/profile", {"brand": "RolesTest Plain", "about": "", "open": True})
    assert status == 403 and error["role"] == "creator"
    assert plain.request("GET", "/api/creators/me")[1]["user"]["roles"] == ["creator"]

    monkeypatch.setenv("MULTI_ROLE_ACCOUNTS", "@example.test")
    assert plain.request("GET", "/api/creators/me")[1]["user"]["roles"] == ["creator", "brand"]
