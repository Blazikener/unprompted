"""Dashboard aggregates use only locally stored, user-scoped data."""
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")

import creator  # noqa: E402
import dashboard  # noqa: E402
import server  # noqa: E402

UTC = timezone.utc


@pytest.fixture(scope="session", autouse=True)
def init_dashboard_schema():
    server.init_db()


def new_user():
    email = "dashboard-%s@example.test" % secrets.token_hex(8)
    with server.connect() as db:
        return db.execute("INSERT INTO users (email, password) VALUES (%s, 'test') RETURNING *", (email,)).fetchone()


def delete_user(user):
    with server.connect() as db:
        db.execute("DELETE FROM events WHERE user_id = %s", (user["id"],))
        db.execute("DELETE FROM digests WHERE user_id = %s OR lower(email) = lower(%s)", (user["id"], user["email"]))
        db.execute("DELETE FROM users WHERE id = %s", (user["id"],))


@pytest.fixture
def user():
    row = new_user()
    yield row
    delete_user(row)


def add_scan(user, platform, handle, brands, created_at=None):
    profile = {"pfp": "https://example.test/%s.jpg" % handle, "name": handle}
    with server.connect() as db:
        return db.execute(
            "INSERT INTO scans (user_id, platform, handle, source, profile, brands, created_at) "
            "VALUES (%s, %s, %s, 'demo', %s, %s, coalesce(%s, now())) RETURNING id",
            (user["id"], platform, handle, Jsonb(profile), Jsonb(brands), created_at),
        ).fetchone()["id"]


def brand_row(name, mentions, dates, category="Food & drink", views=100, status="unpaid"):
    receipts = [
        {"videoId": "%s-%s" % (name.lower().replace(" ", "-"), i), "kind": "spoken", "views": views,
         "publishedAt": date}
        for i, date in enumerate(dates)
    ]
    return {
        "brand": name, "category": category, "spoken": mentions, "tagged": 0, "sponsored": 0,
        "organic": mentions, "organicViews": views * mentions, "status": status, "receipts": receipts,
    }


def insert_video(video_id, handle, views, published_at, platform="tiktok"):
    with server.connect() as db:
        db.execute(
            "INSERT INTO videos (id, platform, handle, published_at, views, raw) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (video_id, platform, handle, published_at, views, Jsonb({"id": video_id})),
        )


def insert_search(user_id, brand, created_at):
    with server.connect() as db:
        return db.execute(
            "INSERT INTO searches (brand, params, total_count, total_views, user_id, created_at) "
            "VALUES (%s, %s, 0, 0, %s, %s) RETURNING id",
            (brand, Jsonb({}), user_id, created_at),
        ).fetchone()["id"]


def insert_mention(search_id, video_id, kind):
    with server.connect() as db:
        db.execute("INSERT INTO mentions (search_id, video_id, kind, hits) VALUES (%s, %s, %s, 1)",
                   (search_id, video_id, kind))


def current_month(offset=0):
    now = datetime.now(UTC)
    index = now.year * 12 + now.month - 1 + offset
    return "%04d-%02d-15T12:00:00+00:00" % (index // 12, index % 12 + 1)


def monday(offset_weeks=0):
    today = datetime.now(UTC).date()
    day = today - timedelta(days=today.weekday()) + timedelta(weeks=offset_weeks)
    return datetime.combine(day, datetime.min.time(), UTC)


def test_empty_dashboard_has_zeroed_series_and_authenticated_route(user, monkeypatch):
    monkeypatch.setattr(server, "oriane", lambda *args, **kwargs: pytest.fail("dashboard must not call Oriane"))
    out = dashboard.view(user)
    assert out["user"]["id"] == user["id"] and out["memberSince"]
    assert out["usage"]["scans"]["used"] == 0 and out["usage"]["scans"]["limit"] == creator.PLANS["free"]["scansPerWeek"]
    assert out["usage"]["searches"]["used"] == out["usage"]["checks"]["used"] == 0
    assert out["creator"] == {
        "scans": 0, "handles": [], "brands": 0, "receipts": 0, "organicViews": 0, "unpaid": 0,
        "topBrands": [], "timeline": [{"month": month, "mentions": 0} for month in dashboard.month_keys()], "recent": [],
    }
    assert out["brand"]["searches"] == out["brand"]["mentions"] == out["brand"]["views"] == 0
    assert out["brand"]["digests"] == {"active": 0, "pending": 0}
    assert set(out["brand"]["licenseRequests"].values()) == {0}
    assert len(out["brand"]["weekly"]) == 8 and all(row["mentions"] == 0 for row in out["brand"]["weekly"])
    assert out["brand"]["recentSearches"] == out["brand"]["topCreators"] == []
    assert out["manager"]["hasRoster"] is False
    assert out["manager"]["creators"] == out["manager"]["reports"] == out["manager"]["deals"] == out["manager"]["dealsUsd"] == 0
    assert out["licenses"]["verifiedHandles"] == out["licenses"]["waiting"] == out["licenses"]["accepted"] == 0
    assert out["licenses"]["paidUsd"] == out["licenses"]["owedUsd"] == out["licenses"]["live"] == 0
    assert out["activity"] == []

    class Handler:
        command = "GET"
        path = "/api/creators/dashboard"
        headers = {}

    with pytest.raises(server.ApiError) as error:
        creator.dispatch(Handler())
    assert error.value.status == 401 and str(error.value) == "Sign in to continue."

    token = secrets.token_urlsafe(24)
    with server.connect() as db:
        db.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (%s, %s, now() + interval '1 day')",
                   (hashlib.sha256(token.encode()).hexdigest(), user["id"]))
    Handler.headers = {"Cookie": "%s=%s" % (creator.COOKIE, token)}
    assert creator.dispatch(Handler())["user"]["id"] == user["id"]


def test_creator_aggregates_use_latest_scan_per_handle_and_receipt_months(user):
    older = datetime.now(UTC) - timedelta(days=3)
    newer = datetime.now(UTC) - timedelta(days=2)
    add_scan(user, "tiktok", "maya.eats", [
        brand_row("Old Brand", 4, [current_month()] * 4),
    ], older)
    add_scan(user, "tiktok", "maya.eats", [
        brand_row("Glow Co", 2, [current_month(), current_month(-1)], views=50),
        brand_row("Unpaid Co", 1, [current_month()], views=70),
    ], newer)
    add_scan(user, "instagram", "sami.lifts", [
        brand_row("Glow Co", 3, [current_month(-1), current_month(-1), current_month(-2)], views=80),
        brand_row("New Brand", 1, [current_month(-2)], views=90),
    ], newer + timedelta(seconds=1))

    out = dashboard.view(user)
    creator_data = out["creator"]
    assert creator_data["scans"] == 3 and len(creator_data["handles"]) == 2
    assert {row["handle"] for row in creator_data["handles"]} == {"maya.eats", "sami.lifts"}
    assert creator_data["brands"] == 3 and creator_data["receipts"] == 7
    assert creator_data["organicViews"] == 500 and creator_data["unpaid"] == 7
    assert [row["brand"] for row in creator_data["topBrands"]] == ["Glow Co", "New Brand", "Unpaid Co"]
    assert creator_data["topBrands"][0]["mentions"] == 5
    assert creator_data["timeline"][-1]["mentions"] == 2
    assert creator_data["timeline"][-2]["mentions"] == 3
    assert creator_data["timeline"][-3]["mentions"] == 2
    assert out["usage"]["scans"]["used"] == 3
    assert len(creator_data["recent"]) == 3


def test_brand_aggregates_are_user_scoped_and_filter_owned_kinds(user):
    other = new_user()
    try:
        now = datetime.now(UTC)
        prefix = secrets.token_hex(4)
        search_a = insert_search(user["id"], "Trail Co", now - timedelta(minutes=3))
        search_b = insert_search(user["id"], "Trail Co", now - timedelta(minutes=2))
        search_c = insert_search(user["id"], "Kitchen", now - timedelta(minutes=1))
        other_search = insert_search(other["id"], "Trail Co", now)
        video_ids = {name: "%s-%s" % (prefix, name) for name in
                     ("week-1", "week-2", "week-3", "old", "owned", "unverified", "other")}
        insert_video(video_ids["week-1"], "maya.eats", 100, monday())
        insert_video(video_ids["week-2"], "sami.lifts", 200, monday())
        insert_video(video_ids["week-3"], "noor.cooks", 300, monday())
        insert_video(video_ids["old"], "maya.eats", 400, monday(-2))
        insert_video(video_ids["owned"], "brand.account", 500, monday())
        insert_video(video_ids["unverified"], "unknown.creator", 600, monday())
        insert_video(video_ids["other"], "other.creator", 700, monday())
        insert_mention(search_a, video_ids["week-1"], "spoken")
        insert_mention(search_a, video_ids["week-2"], "tagged")
        insert_mention(search_a, video_ids["owned"], "owned")
        insert_mention(search_a, video_ids["unverified"], "unverified")
        insert_mention(search_b, video_ids["week-1"], "spoken")
        insert_mention(search_b, video_ids["week-3"], "sponsored")
        insert_mention(search_c, video_ids["old"], "spoken")
        insert_mention(other_search, video_ids["other"], "tagged")

        active = now - timedelta(days=2)
        with server.connect() as db:
            active_digest = db.execute(
                "INSERT INTO digests (email, brand, params, token, confirmed_at, user_id) "
                "VALUES (%s, 'Trail Co', %s, %s, %s, %s) RETURNING id",
                (user["email"], Jsonb({}), secrets.token_urlsafe(16), active, user["id"]),
            ).fetchone()["id"]
            db.execute(
                "INSERT INTO digests (email, brand, params, token, user_id) VALUES (%s, 'Kitchen', %s, %s, %s) RETURNING id",
                (user["email"], Jsonb({"pending": True}), secrets.token_urlsafe(16), user["id"]),
            )
            other_digest = db.execute(
                "INSERT INTO digests (email, brand, params, token, confirmed_at, user_id) "
                "VALUES (%s, 'Other', %s, %s, %s, %s) RETURNING id",
                (other["email"], Jsonb({}), secrets.token_urlsafe(16), active, other["id"]),
            ).fetchone()["id"]
        license_ids = ["%s-license-%s" % (prefix, i) for i in range(5)]
        for i, video_id in enumerate(license_ids):
            insert_video(video_id, "license.creator", 50, monday())
        statuses = ["requested", "contacted", "accepted", "live", "declined"]
        with server.connect() as db:
            for video_id, status in zip(license_ids, statuses):
                db.execute("INSERT INTO license_requests (digest_id, video_id, days, price_usd, status) "
                           "VALUES (%s, %s, 30, 100, %s)", (active_digest, video_id, status))
        other_license_id = "%s-license-other" % prefix
        insert_video(other_license_id, "other.license.creator", 40, monday())
        with server.connect() as db:
            db.execute("INSERT INTO license_requests (digest_id, video_id, days, price_usd, status) "
                       "VALUES (%s, %s, 30, 100, 'accepted')", (other_digest, other_license_id))

        out = dashboard.view(user)["brand"]
        assert out["searches"] == 3
        assert out["digests"] == {"active": 1, "pending": 1}
        assert out["mentions"] == 4 and out["views"] == 1_000
        weeks = {row["week"]: row["mentions"] for row in out["weekly"]}
        assert weeks[monday().date().isoformat()] == 3
        assert weeks[monday(-2).date().isoformat()] == 1
        assert len(out["weekly"]) == 8 and sum(row["mentions"] for row in out["weekly"]) == 4
        assert out["topCreators"][0]["handle"] == "maya.eats" and out["topCreators"][0]["mentions"] == 2
        assert len(out["recentSearches"]) == 2
        assert out["recentSearches"][0]["id"] == search_c
        assert out["licenseRequests"] == {"requested": 2, "accepted": 1, "live": 1, "declined": 1}
    finally:
        delete_user(other)


def test_activity_filters_noise_and_collapses_consecutive_duplicates(user):
    now = datetime.now(UTC)
    events = [
        ("signup", Jsonb({}), now - timedelta(minutes=5)),
        ("scan", Jsonb({"handle": "maya.eats"}), now - timedelta(minutes=4)),
        ("visit", Jsonb({"page": "/brands/"}), now - timedelta(minutes=3, seconds=30)),
        ("scan", Jsonb({"handle": "maya.eats"}), now - timedelta(minutes=3)),
        ("digest_confirm", Jsonb({"brand": "Trail Co"}), now - timedelta(minutes=2)),
        ("paywall", Jsonb({"reason": "scan_quota"}), now - timedelta(minutes=1)),
        ("offer_accept", Jsonb({"request": 7}), now),
    ]
    with server.connect() as db:
        for name, data, created_at in events:
            db.execute(
                "INSERT INTO events (user_id, name, data, created_at) VALUES (%s, %s, %s, %s)",
                (user["id"], name, data, created_at),
            )

    activity = dashboard.view(user)["activity"]
    assert [row["name"] for row in activity] == ["offer_accept", "digest_confirm", "scan", "signup"]


def test_manager_roster_summary_reuses_roster_access(user):
    now = datetime.now(UTC)
    with server.connect() as db:
        roster_id = db.execute(
            "INSERT INTO rosters (user_id, pilot_until) VALUES (%s, %s) RETURNING id",
            (user["id"], now + timedelta(days=7)),
        ).fetchone()["id"]
        db.execute("INSERT INTO roster_creators (roster_id, platform, handle) VALUES (%s, 'tiktok', 'maya.eats'), (%s, 'instagram', 'sami.lifts')",
                   (roster_id, roster_id))
        db.execute("INSERT INTO roster_reports (roster_id, items, sent, created_at) VALUES (%s, %s, true, %s), (%s, %s, true, %s)",
                   (roster_id, Jsonb([]), now - timedelta(days=7), roster_id, Jsonb([]), now))
        db.execute(
            "INSERT INTO roster_deals (roster_id, handle, brand, amount_usd) "
            "VALUES (%s, 'maya.eats', 'Glow Co', 150), (%s, 'sami.lifts', 'Trail Co', 100)",
            (roster_id, roster_id),
        )
    manager = dashboard.view(user)["manager"]
    assert manager == {
        "hasRoster": True, "access": "pilot", "creators": 2, "reports": 2,
        "lastReportAt": now.isoformat(), "deals": 2, "dealsUsd": 250,
    }
