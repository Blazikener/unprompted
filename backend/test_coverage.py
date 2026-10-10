"""Operator report for brand searches and creator scan misses, with local fixtures only."""
import json
import os
import threading
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"

import psycopg  # noqa: E402
import pytest  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

import coverage  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def init_db():
    server.init_db()


@pytest.fixture
def prefix():
    value = "coverage-" + uuid.uuid4().hex[:10]
    yield value
    user_pattern = value + "%@coverage.test"
    with psycopg.connect(server.DB_URL) as db:
        db.execute(
            "DELETE FROM events WHERE user_id IN (SELECT id FROM users WHERE email LIKE %s)"
            " OR (name = 'scan_miss' AND lower(data->>'handle') LIKE %s)",
            (user_pattern, value.lower() + "%"),
        )
        db.execute("DELETE FROM searches WHERE lower(brand) LIKE %s", (value.lower() + "%",))
        db.execute("DELETE FROM users WHERE email LIKE %s", (user_pattern,))


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


def user(db, email):
    return db.execute("INSERT INTO users (email, password) VALUES (%s, 'x') RETURNING id", (email,)).fetchone()["id"]


def search(db, brand, user_id, count, days_old=0):
    return db.execute(
        "INSERT INTO searches (brand, params, total_count, total_views, user_id, created_at)"
        " VALUES (%s, '{}'::jsonb, %s, 0, %s, now() - (%s * interval '1 day')) RETURNING id",
        (brand, count, user_id, days_old),
    ).fetchone()["id"]


def test_brand_gaps_aggregate_searches_and_exclude_thick_or_old(prefix):
    with psycopg.connect(server.DB_URL, row_factory=psycopg.rows.dict_row) as db:
        first = user(db, f"{prefix}-one@coverage.test")
        second = user(db, f"{prefix}-two@coverage.test")
        thin = f"{prefix} Thin Brand"
        search(db, thin, first, 0)
        search(db, thin, second, 3)

        mixed = f"{prefix} Mixed Brand"
        search(db, mixed, first, 2)
        search(db, mixed, second, 40)

        old = f"{prefix} Old Brand"
        search(db, old, first, 1, days_old=31)

    report = coverage.gaps()
    by_brand = {row["brand"].lower(): row for row in report["brands"]}
    thin_row = by_brand[thin.lower()]
    assert (thin_row["searches"], thin_row["people"], thin_row["best"]) == (2, 2, 3)
    assert mixed.lower() not in by_brand and old.lower() not in by_brand


def test_creator_misses_group_case_insensitively_and_ignore_demo(prefix):
    handle = prefix + "-creator"
    demo_handle = prefix + "-demo"
    with psycopg.connect(server.DB_URL, row_factory=psycopg.rows.dict_row) as db:
        first = user(db, f"{prefix}-one@coverage.test")
        second = user(db, f"{prefix}-two@coverage.test")
        db.execute(
            "INSERT INTO events (user_id, name, data, created_at) VALUES"
            " (%s, 'scan_miss', %s, now() - interval '2 hours'),"
            " (%s, 'scan_miss', %s, now() - interval '1 hour'),"
            " (%s, 'scan_miss', %s, now() - interval '30 minutes'),"
            " (%s, 'scan_miss', %s, now())",
            (
                first, Jsonb({"platform": "tiktok", "handle": handle, "reason": "profile_only"}),
                second, Jsonb({"platform": "tiktok", "handle": handle.upper(), "reason": "other_platform"}),
                first, Jsonb({"platform": "tiktok", "handle": handle, "reason": "demo"}),
                first, Jsonb({"platform": "instagram", "handle": demo_handle, "reason": "demo"}),
            ),
        )

    report = coverage.gaps()
    by_handle = {(row["platform"], row["handle"]): row for row in report["creators"]}
    row = by_handle[("tiktok", handle.lower())]
    assert (row["attempts"], row["people"], row["reason"]) == (2, 2, "other_platform")
    assert ("instagram", demo_handle) not in by_handle


def request(base, method, path, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = b"{}" if method == "POST" else None
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def test_admin_route_auth_methods_and_no_email(base, prefix):
    brand = prefix + " HTTP Brand"
    handle = prefix + "-http-creator"
    email = f"{prefix}-person@coverage.test"
    with psycopg.connect(server.DB_URL, row_factory=psycopg.rows.dict_row) as db:
        uid = user(db, email)
        search(db, brand, uid, 1)
        db.execute(
            "INSERT INTO events (user_id, name, data) VALUES (%s, 'scan_miss', %s)",
            (uid, Jsonb({"platform": "tiktok", "handle": handle, "reason": "unknown"})),
        )

    assert request(base, "GET", "/api/admin/coverage")[0] == 401
    status, report = request(base, "GET", "/api/admin/coverage", "run-secret")
    assert status == 200 and brand in [row["brand"] for row in report["brands"]]
    assert handle in [row["handle"] for row in report["creators"]]
    assert email not in json.dumps(report)
    assert request(base, "POST", "/api/admin/coverage", "run-secret")[0] == 405
