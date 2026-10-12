"""Future paid collabs: brand proposals, creator pitches, counters, the watchlist and the Monday email (no Oriane calls).

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_collabs.py
"""
import os
import secrets

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["APP_URL"] = "https://u.test"

import pytest  # noqa: E402
from psycopg.types.json import Jsonb  # noqa: E402

import collabs  # noqa: E402
import creator  # noqa: E402
import digest  # noqa: E402
import server  # noqa: E402
from server import ApiError  # noqa: E402

HANDLE = "collab.ali"
TERMS = {"deliverables": "2 TikToks + 1 story", "timing": "June", "budgetUsd": 2600, "brief": "Your style, your call."}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    server.init_db()

    def cleanup():
        with server.connect() as db:
            db.execute("DELETE FROM collabs WHERE handle = %s OR lower(brand) LIKE 'collabtest%%'", (HANDLE,))
            db.execute("DELETE FROM brand_profiles WHERE lower(brand) LIKE 'collabtest%%'")
            db.execute("DELETE FROM creator_handles WHERE handle = %s", (HANDLE,))
            db.execute("DELETE FROM mentions WHERE video_id LIKE 'collabs-proof-%%'")
            db.execute("DELETE FROM searches WHERE brand LIKE 'CollabTest Proof%%'")
            db.execute("DELETE FROM videos WHERE id LIKE 'collabs-proof-%%'")
            db.execute("DELETE FROM users WHERE email LIKE 'collab-%%@example.test'")

    cleanup()
    monkeypatch.setenv("LICENSE_NOTIFY_EMAIL", "ops@u.test")
    yield
    cleanup()


@pytest.fixture
def mail(monkeypatch):
    sent = []
    monkeypatch.setattr(digest, "send", lambda to, subject, body: sent.append((to, subject, body)) or True)
    return sent


def new_user(role):
    with server.connect() as db:
        return db.execute("INSERT INTO users (email, password, role) VALUES (%s, 'test', %s) RETURNING *",
                          ("collab-%s@example.test" % secrets.token_hex(6), role)).fetchone()


def verified_creator():
    u = new_user("creator")
    with server.connect() as db:
        db.execute("INSERT INTO creator_handles (user_id, platform, handle, code, verified_at, verified_by)"
                   " VALUES (%s, 'tiktok', %s, 'c', now(), 'operator')", (u["id"], HANDLE))
    return u


def proof_video(handle):
    video_id = "collabs-proof-" + secrets.token_hex(8)
    with server.connect() as db:
        db.execute(
            "INSERT INTO videos (id, platform, handle, published_at, views, raw) "
            "VALUES (%s, 'tiktok', %s, now(), 1200, %s)",
            (video_id, handle, Jsonb({
                "platform": "tiktok", "profileHandle": handle, "platformId": video_id,
                "viewsCount": 1200, "publishedAt": "2026-10-01T00:00:00Z",
            })),
        )
    return video_id


def proof_request(user, handle, video_id, side=None):
    with server.connect() as db:
        cookie = creator.start_session(db, user["id"])[1].split(";", 1)[0]

    class Handler:
        command = "GET"
        path = "/api/collabs/proof?v=%s&platform=tiktok&handle=%s%s" % (
            video_id, handle, "&as=" + side if side else "",
        )
        headers = {"Cookie": cookie}

    return collabs.dispatch(Handler())


def test_video_proof_includes_brand_and_kind_and_null_brand_without_mention():
    mentioned_video = proof_video(HANDLE)
    unmentioned_video = proof_video("collab.other")
    with server.connect() as db:
        search_id = db.execute(
            "INSERT INTO searches (brand, params, total_count, total_views) VALUES (%s, %s, 1, 1200) RETURNING id",
            ("CollabTest Proof", Jsonb({})),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO mentions (search_id, video_id, kind, hits, quote) VALUES (%s, %s, 'spoken', 1, %s)",
            (search_id, mentioned_video, Jsonb({"text": "I love this brand"})),
        )

    proof = collabs.video_proof(mentioned_video, "tiktok", HANDLE)
    assert proof["brand"] == "CollabTest Proof" and proof["kind"] == "spoken"
    assert collabs.video_proof(unmentioned_video, "tiktok", "collab.other")["brand"] is None


def add_scan(u, brand):
    receipts = [{"videoId": "v1", "kind": "spoken", "views": 4100, "publishedAt": "2026-09-01T00:00:00Z",
                 "url": "https://tiktok.test/v1", "quote": {"text": "I love %s" % brand, "start": 12}, "caption": ""}]
    with server.connect() as db:
        return db.execute("INSERT INTO scans (user_id, platform, handle, source, profile, brands) VALUES (%s, 'tiktok', %s, 'demo', %s, %s)"
                          " RETURNING id", (u["id"], HANDLE, Jsonb({}), Jsonb([{"brand": brand, "receipts": receipts}]))).fetchone()["id"]


def test_brand_invites_creator_counter_then_booked(mail):
    brand, ali = new_user("brand"), verified_creator()
    c = collabs.propose(brand, {**TERMS, "brand": "CollabTest Tims", "platform": "tiktok", "handle": "@Collab.Ali"})
    assert c["status"] == "sent" and c["side"] == "brand" and not c["yourTurn"] and c["canWithdraw"]
    assert mail[-1][0] == ali["email"] and "CollabTest Tims wants to collab with you: $2,600" == mail[-1][1]
    assert "/collab/" in mail[-1][2] and "License" not in mail[-1][2]

    inbox = collabs.mine(ali)
    item = inbox["collabs"][0]
    assert inbox["counts"]["yourTurn"] == 1 and item["yourTurn"] and item["deliverables"] == "2 TikToks + 1 story"
    with pytest.raises(ApiError) as e:
        collabs.act(*collabs.load_mine(brand, c["id"]), {"action": "accept"})
    assert e.value.status == 409                                  # the brand can't accept its own offer

    countered = collabs.act(*collabs.load_mine(ali, c["id"]), {"action": "counter", "budgetUsd": 3200, "note": "Add a reel"})
    assert countered["status"] == "countered" and countered["budgetUsd"] == 3200 and not countered["yourTurn"]
    assert mail[-1][0] == brand["email"] and mail[-1][1].startswith("Counter-offer: $3,200")

    token = collabs.view(collabs.load_mine(brand, c["id"])[0], "brand")["url"].rsplit("/", 1)[1]
    booked = collabs.act(*collabs.load_token(token), {"action": "accept"})
    assert booked["status"] == "accepted" and booked["label"] == "Booked"
    assert {m[0] for m in mail[-1:]} == {ali["email"]} and mail[-1][1].startswith("Collab booked")
    assert collabs.mine(ali)["counts"] == {"yourTurn": 0, "waiting": 0, "booked": 1, "bookedUsd": 3200}
    with pytest.raises(ApiError):
        collabs.act(*collabs.load_token(token), {"action": "decline"})


def test_unclaimed_handle_goes_to_operator_and_duplicates_are_refused(mail):
    brand = new_user("brand")
    collabs.propose(brand, {**TERMS, "brand": "CollabTest Tims", "platform": "tiktok", "handle": HANDLE})
    assert mail[-1][0] == "ops@u.test" and mail[-1][1].startswith("Pass on to @%s" % HANDLE)
    with pytest.raises(ApiError) as e:
        collabs.propose(brand, {**TERMS, "brand": "collabtest tims", "platform": "tiktok", "handle": HANDLE})
    assert e.value.status == 409
    with pytest.raises(ApiError):
        collabs.propose(brand, {**TERMS, "brand": "CollabTest Other", "platform": "tiktok", "handle": HANDLE, "budgetUsd": 10})


def test_creator_pitches_an_open_brand_with_a_receipt(mail):
    brand, ali = new_user("brand"), verified_creator()
    scan = add_scan(ali, "CollabTest Candles")
    with pytest.raises(ApiError) as e:
        collabs.pitch(ali, {**TERMS, "brand": "CollabTest Candles", "scanId": scan})
    assert e.value.status == 404                                  # not taking pitches yet

    collabs.set_profile(brand, {"brand": "CollabTest Candles", "about": "Candles", "open": True})
    assert "CollabTest Candles" in [p["brand"] for p in collabs.mine(ali)["partners"]]
    c = collabs.pitch(ali, {**TERMS, "brand": "collabtest candles", "scanId": scan})
    assert c["brand"] == "CollabTest Candles" and c["origin"] == "creator" and c["proof"]["views"] == 4100
    assert c["proof"]["quote"]["text"] == "I love CollabTest Candles"
    assert mail[-1][0] == brand["email"] and mail[-1][1] == "@%s pitched a collab for CollabTest Candles" % HANDLE

    b = collabs.mine(brand)
    assert b["counts"]["yourTurn"] == 1 and b["profile"]["brand"] == "CollabTest Candles"
    declined = collabs.act(*collabs.load_mine(brand, c["id"]), {"action": "decline", "reason": "Budget is set for Q3"})
    assert declined["status"] == "declined" and mail[-1][0] == ali["email"]

    collabs.set_profile(brand, {"brand": "CollabTest Candles", "open": False})
    assert "CollabTest Candles" not in [p["brand"] for p in collabs.partners()]


def test_creator_pitches_an_open_brand_with_verified_handle_and_no_scan(mail):
    brand, ali = new_user("brand"), verified_creator()
    collabs.set_profile(brand, {"brand": "CollabTest NoScan", "about": "Candles", "open": True})

    c = collabs.pitch(ali, {**TERMS, "brand": "CollabTest NoScan", "platform": "tiktok", "handle": HANDLE})

    assert c["brand"] == "CollabTest NoScan" and c["handle"] == HANDLE and c["proof"] is None
    assert c["origin"] == "creator" and mail[-1][0] == brand["email"]


def test_creator_primary_brand_side_can_be_discovered_and_pitched(mail):
    brand_side, ali = new_user("creator"), verified_creator()
    with server.connect() as db:
        db.execute("UPDATE users SET extra_roles = %s WHERE id = %s", (["brand"], brand_side["id"]))
    scan = add_scan(ali, "CollabTest Added Side")
    collabs.set_profile(brand_side, {"brand": "CollabTest Added Side", "about": "Candles", "open": True})

    assert "CollabTest Added Side" in [p["brand"] for p in collabs.partners()]
    pitch = collabs.pitch(ali, {**TERMS, "brand": "CollabTest Added Side", "scanId": scan})
    assert pitch["brand"] == "CollabTest Added Side" and pitch["origin"] == "creator"
    assert mail[-1][0] == brand_side["email"]


def test_roles_cannot_see_each_others_collabs(mail):
    brand, other, ali = new_user("brand"), new_user("brand"), verified_creator()
    c = collabs.propose(brand, {**TERMS, "brand": "CollabTest Tims", "platform": "tiktok", "handle": HANDLE})
    with pytest.raises(ApiError):
        collabs.load_mine(other, c["id"])
    with pytest.raises(ApiError):
        collabs.mine(new_user("manager"))
    assert collabs.load_mine(ali, c["id"])[1] == "creator"


def test_brand_side_can_view_unowned_proof_but_explicit_creator_side_cannot():
    user = new_user("creator")
    with server.connect() as db:
        db.execute("UPDATE users SET extra_roles = %s WHERE id = %s", (["brand"], user["id"]))
    handle = "someone.else"
    video_id = proof_video(handle)

    proof = proof_request(user, handle, video_id)
    assert proof["proof"]["url"].endswith(video_id)

    with pytest.raises(ApiError) as error:
        proof_request(user, handle, video_id, side="creator")
    assert error.value.status == 403


def test_creator_only_cannot_view_unowned_proof():
    user = new_user("creator")
    handle = "someone.else"
    video_id = proof_video(handle)

    with pytest.raises(ApiError) as error:
        proof_request(user, handle, video_id)
    assert error.value.status == 403


def test_creator_only_can_view_verified_handle_proof():
    user = verified_creator()
    video_id = proof_video(HANDLE)

    proof = proof_request(user, HANDLE, video_id)
    assert proof["proof"]["url"].endswith(video_id)


def test_proposal_from_a_weekly_report_link(mail, monkeypatch):
    brand = new_user("brand")
    seen = {"platform": "tiktok", "handle": "Collab.Ali", "url": "https://tiktok.test/v9", "quote": {"text": "Tims run", "start": 3},
            "views": 900, "publishedAt": None, "kind": "spoken"}
    monkeypatch.setattr(digest, "license_video", lambda d, vid: seen if vid == "v9" else (_ for _ in ()).throw(ApiError(404, "no")))
    with server.connect() as db:
        d = db.execute("INSERT INTO digests (email, brand, params, user_id, token) VALUES (%s, 'CollabTest Tims', %s, %s, %s)"
                       " RETURNING *", (brand["email"], Jsonb({}), brand["id"], secrets.token_urlsafe(16))).fetchone()
    try:
        c = collabs.propose(None, {**TERMS, "videoId": "v9", "handle": "someone.else"}, d)   # the handle comes from the report
        assert c["brand"] == "CollabTest Tims" and c["handle"] == HANDLE and c["proof"]["views"] == 900
        assert collabs.mine(brand)["collabs"][0]["id"] == c["id"]
        with pytest.raises(ApiError):
            collabs.propose(None, {**TERMS, "videoId": "nope"}, d)
    finally:
        with server.connect() as db:
            db.execute("DELETE FROM digests WHERE id = %s", (d["id"],))


def test_watchlist_and_monday_email(mail):
    ali, brand = verified_creator(), new_user("brand")
    collabs.watch(ali, {"brand": "CollabTest Glow"})
    collabs.watch(ali, {"brand": "collabtest glow"})               # no duplicates
    assert collabs.watchlist(ali) == [{"brand": "CollabTest Glow", "open": False}]
    assert collabs.weekly(ali) is None                            # nothing to say yet

    collabs.set_profile(brand, {"brand": "CollabTest Glow"})
    collabs.propose(brand, {**TERMS, "brand": "CollabTest Glow", "platform": "tiktok", "handle": HANDLE})
    mail.clear()
    assert collabs.run_weekly(force=True) >= 1
    mine = [m for m in mail if m[0] == ali["email"]]
    assert len(mine) == 1 and "1 collab waiting for you" in mine[0][1] and "CollabTest Glow now taking pitches" in mine[0][1]
    assert "?pitch=CollabTest Glow" in mine[0][2]

    mail.clear()
    collabs.run_weekly()                                          # sent this week already
    assert not [m for m in mail if m[0] == ali["email"]]
    collabs.watch(ali, {"brand": "CollabTest Glow", "remove": True})
    assert collabs.watchlist(ali) == []


def test_weekly_uses_creator_side_for_brand_primary_user(mail):
    user = new_user("brand")
    with server.connect() as db:
        db.execute("UPDATE users SET extra_roles = %s WHERE id = %s", (["creator"], user["id"]))
    collabs.watch(user, {"brand": "CollabTest Glow"})
    collabs.set_profile(user, {"brand": "CollabTest Glow"})

    collabs.run_weekly(force=True)
    sent = [message for message in mail if message[0] == user["email"]]
    assert len(sent) == 1 and "CollabTest Glow now taking pitches" in sent[0][1]


def test_weekly_continues_after_one_users_failure(mail, monkeypatch):
    broken, healthy = new_user("creator"), new_user("creator")
    collabs.watch(broken, {"brand": "CollabTest Broken"})
    collabs.watch(healthy, {"brand": "CollabTest Healthy"})

    def build(user):
        if user["id"] == broken["id"]:
            raise RuntimeError("broken weekly report")
        return "Weekly", "<p>Ready</p>", []

    monkeypatch.setattr(collabs, "weekly", build)
    collabs.run_weekly(force=True)
    assert any(message[0] == healthy["email"] for message in mail)
    assert not any(message[0] == broken["email"] for message in mail)
