"""Found-you signals are built only from locally stored search, scan and check rows."""
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["ORIANE_API_KEY"] = ""

import pytest  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

import collabs  # noqa: E402
import dashboard  # noqa: E402
import found  # noqa: E402
import server  # noqa: E402

UTC = timezone.utc


@pytest.fixture
def prefix():
    server.init_db()
    value = "found-" + uuid.uuid4().hex[:10]
    yield value
    with server.connect() as db:
        user_ids = [row["id"] for row in db.execute(
            "SELECT id FROM users WHERE email LIKE %s", (f"{value}-%@found.test",)
        ).fetchall()]
        if user_ids:
            db.execute("DELETE FROM events WHERE user_id = ANY(%s)", (user_ids,))
            db.execute(
                "DELETE FROM checks WHERE user_id = ANY(%s) OR lower(brand) LIKE %s",
                (user_ids, value.lower() + " %"),
            )
            db.execute(
                "DELETE FROM digests WHERE user_id = ANY(%s) OR lower(email) LIKE %s OR lower(brand) LIKE %s",
                (user_ids, value.lower() + "-%@found.test", value.lower() + " %"),
            )
            db.execute("DELETE FROM mentions WHERE video_id LIKE %s", (value + "-%",))
            db.execute("DELETE FROM searches WHERE lower(brand) LIKE %s", (value.lower() + " %",))
            db.execute("DELETE FROM videos WHERE id LIKE %s", (value + "-%",))
            db.execute("DELETE FROM users WHERE id = ANY(%s)", (user_ids,))


def new_user(prefix, label, role="creator"):
    with server.connect() as db:
        return db.execute(
            "INSERT INTO users (email, password, role) VALUES (%s, 'test', %s) RETURNING *",
            (f"{prefix}-{label}@found.test", role),
        ).fetchone()


def verified_handle(user, handle, platform="tiktok"):
    with server.connect() as db:
        return db.execute(
            "INSERT INTO creator_handles (user_id, platform, handle, code, verified_at, verified_by) "
            "VALUES (%s, %s, %s, 'found-test', now(), 'operator') RETURNING *",
            (user["id"], platform, handle),
        ).fetchone()


def scanned_handle(user, handle, platform="tiktok"):
    with server.connect() as db:
        return db.execute(
            "INSERT INTO scans (user_id, platform, handle, source, profile, brands) "
            "VALUES (%s, %s, %s, 'demo', %s, %s) RETURNING id",
            (user["id"], platform, handle, Jsonb({}), Jsonb([])),
        ).fetchone()["id"]


def video(prefix, label, handle, platform="tiktok"):
    video_id = f"{prefix}-{label}"
    with server.connect() as db:
        db.execute(
            "INSERT INTO videos (id, platform, handle, published_at, views, raw) "
            "VALUES (%s, %s, %s, now(), 1200, %s)",
            (video_id, platform, handle, Jsonb({})),
        )
    return video_id


def search(prefix, brand, user_id, params=None, created_at=None):
    with server.connect() as db:
        return db.execute(
            "INSERT INTO searches (brand, params, total_count, total_views, user_id, created_at) "
            "VALUES (%s, %s, 1, 1200, %s, coalesce(%s, now())) RETURNING id",
            (brand, Jsonb(params or {}), user_id, created_at),
        ).fetchone()["id"]


def mention(search_id, video_id, kind="spoken"):
    with server.connect() as db:
        db.execute(
            "INSERT INTO mentions (search_id, video_id, kind, hits) VALUES (%s, %s, %s, 1)",
            (search_id, video_id, kind),
        )


def digest_id(user, brand):
    with server.connect() as db:
        return db.execute(
            "INSERT INTO digests (email, brand, params, token, user_id) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user["email"], brand, Jsonb({}), secrets.token_urlsafe(16), user["id"]),
        ).fetchone()["id"]


def test_verified_handle_reports_other_users_search(prefix):
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    verified_handle(user, "maya.eats")
    video_id = video(prefix, "verified", "Maya.Eats")
    brand = f"{prefix} Trail Co"
    search_id = search(prefix, brand, finder["id"])
    mention(search_id, video_id)

    result = found.week(user)

    assert result["verifiedHandles"] == 1 and result["locked"] == []
    assert result["brands"][0]["brand"] == brand
    assert (result["brands"][0]["searches"], result["brands"][0]["videos"], result["brands"][0]["checks"]) == (1, 1, 0)
    assert result["brands"][0]["open"] is False and result["brands"][0]["tracked"] is False
    assert datetime.fromisoformat(result["brands"][0]["lastAt"]).tzinfo is not None
    assert not {"user_id", "email"} & result["brands"][0].keys()


def test_brand_name_uses_latest_search_and_searches_are_distinct(prefix):
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    verified_handle(user, "maya.eats")
    video_id = video(prefix, "same-video", "maya.eats")
    now = datetime.now(UTC)
    earlier = search(prefix, f"{prefix} Trail Co", finder["id"], created_at=now - timedelta(minutes=2))
    latest = search(prefix, f"{prefix} TRAIL CO", finder["id"], created_at=now - timedelta(minutes=1))
    mention(earlier, video_id)
    mention(latest, video_id)

    row = found.week(user)["brands"][0]

    assert row["brand"] == f"{prefix} TRAIL CO"
    assert (row["searches"], row["videos"]) == (2, 1)


def test_own_old_owned_and_unverified_searches_do_not_count(prefix):
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    verified_handle(user, "maya.eats")
    own_video = video(prefix, "own", "maya.eats")
    old_video = video(prefix, "old", "maya.eats")
    owned_video = video(prefix, "owned", "maya.eats")
    unverified_video = video(prefix, "unverified", "maya.eats")
    now = datetime.now(UTC)
    own = search(prefix, f"{prefix} Own Search", user["id"])
    old = search(prefix, f"{prefix} Old Search", finder["id"], created_at=now - timedelta(days=8))
    owned = search(prefix, f"{prefix} Owned Mention", finder["id"])
    unverified = search(prefix, f"{prefix} Unverified Mention", finder["id"])
    mention(own, own_video)
    mention(old, old_video)
    mention(owned, owned_video, "owned")
    mention(unverified, unverified_video, "unverified")

    result = found.week(user)

    assert result["brands"] == [] and result["locked"] == []


def test_other_digest_run_counts_but_own_digest_run_does_not(prefix):
    user, other = new_user(prefix, "creator"), new_user(prefix, "other", "brand")
    verified_handle(user, "maya.eats")
    other_digest = digest_id(other, f"{prefix} Other Digest")
    own_digest = digest_id(user, f"{prefix} Own Digest")
    other_search = search(prefix, f"{prefix} Other Digest", None, {"digest": other_digest})
    own_search = search(prefix, f"{prefix} Own Digest", None, {"digest": own_digest})
    other_video = video(prefix, "other-digest", "maya.eats")
    own_video = video(prefix, "own-digest", "maya.eats")
    mention(other_search, other_video)
    mention(own_search, own_video)

    result = found.week(user)

    assert [row["brand"] for row in result["brands"]] == [f"{prefix} Other Digest"]


def test_handle_matching_is_case_insensitive_and_other_users_checks_are_counted(prefix):
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    verified_handle(user, "found.case")
    video_id = video(prefix, "case", "Found.Case")
    searched_brand = f"{prefix} Search Brand"
    check_brand = f"{prefix} Check Brand"
    search_id = search(prefix, searched_brand, finder["id"])
    mention(search_id, video_id)
    with server.connect() as db:
        db.execute(
            "INSERT INTO checks (platform, handle, brand, result, user_id) "
            "VALUES ('tiktok', 'FOUND.CASE', %s, %s, %s), ('tiktok', 'found.case', %s, %s, %s)",
            (searched_brand, Jsonb({}), finder["id"], check_brand, Jsonb({}), finder["id"]),
        )

    rows = {row["brand"]: row for row in found.week(user)["brands"]}

    assert rows[searched_brand]["videos"] == 1
    assert (rows[searched_brand]["searches"], rows[searched_brand]["checks"]) == (1, 1)
    assert rows[check_brand]["checks"] == 1
    assert (rows[check_brand]["searches"], rows[check_brand]["videos"]) == (0, 0)


def test_scanned_unverified_handle_is_only_reported_as_locked(prefix):
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    scanned_handle(user, "waiting.creator")
    video_id = video(prefix, "locked", "Waiting.Creator")
    secret_brand = f"{prefix} Secret Brand"
    search_id = search(prefix, secret_brand, finder["id"])
    other_search = search(prefix, f"{prefix} Another Secret Brand", finder["id"])
    mention(search_id, video_id)
    mention(other_search, video_id)

    result = found.week(user)

    assert result["brands"] == []
    assert result["locked"] == [{"platform": "tiktok", "handle": "waiting.creator", "searches": 2}]
    assert secret_brand not in str(result)


def test_open_and_tracked_flags_are_case_insensitive(prefix):
    user = new_user(prefix, "creator")
    brand_user = new_user(prefix, "brand", "brand")
    verified_handle(user, "maya.eats")
    video_id = video(prefix, "tracked", "maya.eats")
    brand = f"{prefix} Open Brand"
    search_id = search(prefix, brand, brand_user["id"])
    mention(search_id, video_id)
    collabs.set_profile(brand_user, {"brand": brand.upper(), "about": "Open to pitches", "open": True})
    collabs.watch(user, {"brand": brand.lower()})

    row = found.week(user)["brands"][0]

    assert row["open"] is True and row["tracked"] is True


def test_weekly_includes_found_brand_when_it_is_the_only_signal(prefix, monkeypatch):
    monkeypatch.setenv("APP_URL", "https://u.test")
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    verified_handle(user, "maya.eats")
    brand = f"{prefix} Spotlight Brand"
    video_id = video(prefix, "weekly-found", "maya.eats")
    search_id = search(prefix, brand, finder["id"])
    mention(search_id, video_id)

    subject, body, _ = collabs.weekly(user)

    assert subject.startswith("This week: 1 brand found your videos")
    assert "Brands that found you this week" in body and brand in body
    assert "See it" in body and "u.test/dashboard?as=creator" in body


def test_weekly_masks_unverified_brands_and_links_to_claim(prefix, monkeypatch):
    monkeypatch.setenv("APP_URL", "https://u.test")
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    scanned_handle(user, "wait+creator")
    secret_brand = f"{prefix} Secret Brand"
    video_id = video(prefix, "weekly-locked", "wait+creator")
    search_id = search(prefix, secret_brand, finder["id"])
    mention(search_id, video_id)

    subject, body, _ = collabs.weekly(user)

    assert subject.startswith("This week: your videos showed up in 1 brand searches")
    assert "Verify to see who" in body
    assert "u.test/creators/licenses?claim=tiktok:wait%2Bcreator" in body
    assert secret_brand not in subject and secret_brand not in body


def test_weekly_returns_none_without_any_signals(prefix):
    assert collabs.weekly(new_user(prefix, "creator")) is None


def test_weekly_sends_to_verified_handle_owner_without_watchlist_or_collabs(prefix, monkeypatch):
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    verified_handle(user, "maya.eats")
    brand = f"{prefix} Scheduler Brand"
    video_id = video(prefix, "scheduler", "maya.eats")
    search_id = search(prefix, brand, finder["id"])
    mention(search_id, video_id)
    sent = []
    actual_weekly = collabs.weekly
    monkeypatch.setattr(collabs.digest, "send", lambda email, subject, body: sent.append((email, subject, body)))
    monkeypatch.setattr(
        collabs, "weekly",
        lambda candidate, force=False: actual_weekly(candidate, force=force) if candidate["id"] == user["id"] else None,
    )

    assert collabs.run_weekly(force=True) == 1
    assert len(sent) == 1 and sent[0][0] == user["email"] and brand in sent[0][2]
    with server.connect() as db:
        assert db.execute("SELECT 1 FROM collab_weekly WHERE user_id = %s", (user["id"],)).fetchone()


def test_dashboard_exposes_found_only_for_creator_role(prefix):
    user, finder = new_user(prefix, "creator"), new_user(prefix, "finder", "brand")
    brand_user = new_user(prefix, "dashboard-brand", "brand")
    verified_handle(user, "maya.eats")
    brand = f"{prefix} Dashboard Brand"
    video_id = video(prefix, "dashboard", "maya.eats")
    search_id = search(prefix, brand, finder["id"])
    mention(search_id, video_id)

    creator_view = dashboard.view(user, "creator")
    brand_view = dashboard.view(brand_user, "brand")

    assert creator_view["creator"]["found"]["brands"][0]["brand"] == brand
    assert brand_view["creator"] is None
