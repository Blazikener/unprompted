"""The whole licence workflow over HTTP, in the order docs/CONCIERGE.md describes it (Oriane, TikTok, Stripe and mail
mocked: nothing real is searched, charged or sent). Ends by running the Monday funnel SQL from docs/LAUNCH.md as written.

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_workflow.py
"""
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DEMO_MODE"] = "1"
os.environ["APP_URL"] = "https://u.test"
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"
os.environ["RESEND_API_KEY"] = ""
os.environ.pop("MAIL_RELAY_URL", None)
os.environ.pop("MAIL_RELAY_SECRET", None)

import psycopg  # noqa: E402
import pytest  # noqa: E402

import digest  # noqa: E402
import payments  # noqa: E402
import server  # noqa: E402
import tiktok_public  # noqa: E402

OPERATOR, CREATOR, BRAND = "ops-flow@unprompted.test", "lena-flow@creator.test", "growth@brand.test"
OP = {"Authorization": "Bearer run-secret"}


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    server.init_db()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM digests")
        db.execute("DELETE FROM users WHERE email = ANY(%s)", ([OPERATOR, CREATOR],))
        db.execute("DELETE FROM creator_handles WHERE handle = 'lena'")
        db.execute("DELETE FROM mentions WHERE search_id IN (SELECT id FROM searches WHERE brand = 'Glow Recipe')")
        db.execute("DELETE FROM searches WHERE brand = 'Glow Recipe'")
        db.execute("DELETE FROM events WHERE created_at > now() - interval '1 day'")
    for k, v in {"LICENSE_NOTIFY_EMAIL": "ops@u.test", "LICENSE_PAYMENTS": "1", "STRIPE_SECRET_KEY": "sk_test_x",
                 "STRIPE_WEBHOOK_SECRET": "whsec_test"}.items():
        monkeypatch.setenv(k, v)


class Client:
    """A browser: keeps its session cookie."""
    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def __call__(self, method, path, body=None, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with self.opener.open(req, timeout=20) as res:
                return res.status, json.load(res)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)


def video(vid, days_ago=1):
    text = "honestly the Glow Recipe watermelon toner is the only thing I use"
    return {"id": vid, "platform": "tiktok", "platformId": vid, "profileHandle": "lena", "profileFollowersCount": 40_000,
            "viewsCount": 48_000, "caption": "", "hashtags": [], "mentions": [], "coAuthors": [], "transcript": text,
            "transcriptChunks": [{"startSeconds": 42.0, "endSeconds": 47.0, "text": text}],
            "publishedAt": (date.today() - timedelta(days=days_ago)).isoformat() + "T10:00:00Z", "frames": [], "thumbnailMediaUrl": ""}


def page(*videos):
    return {"data": {"results": list(videos), "aggregations": {"totalViewsCount": 48_000 * len(videos)}},
            "metadata": {"pagination": {"totalCount": len(videos)}}}


def signed(event):
    payload = json.dumps(event).encode()
    ts = int(time.time())
    return payload, "t=%d,v1=%s" % (ts, hmac.new(b"whsec_test", b"%d.%s" % (ts, payload), hashlib.sha256).hexdigest())


def webhook(base, event):
    payload, sig = signed(event)
    req = urllib.request.Request(base + "/api/creators/billing/webhook", data=payload, method="POST",
                                 headers={"Content-Type": "application/json", "Stripe-Signature": sig})
    with urllib.request.urlopen(req, timeout=20) as res:
        return res.status


def test_the_whole_licence_workflow(base, monkeypatch):
    mail, stripe = [], []
    monkeypatch.setattr(digest, "send", lambda to, subject, body: mail.append((to, subject, body)) or True)

    def fake_stripe(path, params=None, key=None):
        stripe.append((path, params, key))
        return {"checkout/sessions": {"id": "cs_flow", "url": "https://checkout.stripe.test/flow"}, "transfers": {"id": "tr_flow"},
                "accounts": {"id": "acct_lena"}, "account_links": {"url": "https://connect.stripe.test/lena"}
                }.get(path) or ({"latest_charge": "ch_flow"} if path.startswith("payment_intents/") else {"payouts_enabled": True})
    monkeypatch.setattr(payments, "stripe_call", fake_stripe)
    inbox = lambda to: [(s, b) for t, s, b in mail if t == to]  # noqa: E731

    # Week 1 (CONCIERGE "Week 1", step 5): the operator searches the brand and starts a watch for the brand's contact.
    ops = Client(base)
    assert ops("POST", "/api/creators/signup",
               {"email": OPERATOR, "password": "password-123", "role": "brand"})[0] == 201
    monkeypatch.setattr(server, "oriane", lambda *a, **k: page(video("wf_old", days_ago=40)))
    status, found = ops("POST", "/api/searches", {"brand": "Glow Recipe"})
    assert status == 200 and found["search"]["brand"] == "Glow Recipe"
    s = found["search"]
    status, watch = ops("POST", "/api/digests", {"email": BRAND, "brand": "Glow Recipe", "variants": "", "platform": "all",
                                                  "lang": "any", "searchId": s["id"]})
    assert status == 200 and watch["status"] == "active"
    assert next(r for r in ops("GET", "/api/searches")[1] if r["id"] == s["id"])["watch"]["status"] == "active"

    # The weekly run (cron: POST /api/digests/run) mails the brand a report with a licence button on the new video.
    monkeypatch.setattr(server, "oriane", lambda *a, **k: page(video("wf_old", days_ago=40), video("wf_new")))
    status, run = ops("POST", "/api/digests/run", {}, OP)
    assert status == 200 and run["ran"] == 1 and run["results"][0]["new"] == 1 and run["licenses"] == {"ended": 0, "reminded": 0}
    report = inbox(BRAND)[-1]
    link = re.search(r'href="https://u\.test(/license/[^"]+/wf_new)"', report[1])
    assert report[0] == "1 new creator mention of Glow Recipe this week" and link
    api = "/api/digests/%s/license/wf_new" % watch["token"]

    # The brand opens it, sees the rule-of-thumb price, and requests 30 days.
    brand = Client(base)
    status, view = brand("GET", api)
    assert status == 200 and view["prices"][0] == {"days": 30, "usd": 300} and view["request"] is None
    status, view = brand("POST", api, {"days": 30, "note": "Ramadan campaign"})
    assert view["request"]["status"] == "requested"
    ops_mail = [b for t, s_, b in mail if t == "ops@u.test" and s_.startswith("License request: Glow Recipe")]
    offer_path = re.search(r"https://u\.test(/offer/[0-9a-f]{32})", ops_mail[-1]).group(1)

    # "Every day": the request is in /admin/, waiting on the operator; the creator answers from the link, no account.
    rows = ops("GET", "/api/admin/licenses", None, OP)[1]["requests"]
    row = next(r for r in rows if r["offerUrl"].endswith(offer_path))
    assert row["status"] == "requested" and row["claimed"] is False and row["priceUsd"] == 300
    creator_web = Client(base)
    api_offer = "/api/licenses" + offer_path
    assert creator_web("GET", api_offer)[1]["shareUsd"] == 255
    assert creator_web("POST", api_offer, {"action": "accept"})[1]["status"] == "accepted"
    assert any(s_ == "@lena said yes: pay $300 to start the licence" and "wf_new" in b for s_, b in inbox(BRAND))   # the brand is told

    # Phase 3 "What happens on its own": the brand pays through Stripe; the webhook marks it paid.
    status, out = brand("POST", api, {"action": "pay"})
    assert status == 200 and out == {"checkoutUrl": "https://checkout.stripe.test/flow"}
    rid = row["id"]
    assert webhook(base, {"type": "checkout.session.completed", "data": {"object": {
        "mode": "payment", "payment_status": "paid", "amount_total": 30000, "payment_intent": "pi_flow",
        "metadata": {"license_request": str(rid)}}}}) == 200
    assert brand("GET", api)[1]["request"]["paid"] is True

    # The creator sends the code: it goes live, the brand is emailed the code; the creator has no payouts yet, so it's owed.
    live = creator_web("POST", api_offer, {"action": "code", "code": "SPARK-FLOW"})[1]
    assert live["status"] == "live" and live["creatorPaidAt"] is None
    assert any(s_.startswith("Live: @lena") and "SPARK-FLOW" in b for s_, b in inbox(BRAND))
    req = brand("GET", api)[1]["request"]
    assert req["status"] == "live" and req["adCode"] == "SPARK-FLOW" and not [p for p, _, _ in stripe if p == "transfers"]

    # The creator signs up, claims and verifies the handle (code in bio), and sets up payouts: the owed share goes out.
    lena = Client(base)
    assert lena("POST", "/api/creators/signup",
                {"email": CREATOR, "password": "password-123", "role": "creator"})[0] == 201
    claim = lena("POST", "/api/licenses/handles", {"platform": "tiktok", "handle": "@lena"})[1]
    monkeypatch.setattr(tiktok_public, "creator_page", lambda h: ({"signature": "skincare " + claim["code"]}, []))
    assert lena("POST", "/api/licenses/verify", {"platform": "tiktok", "handle": "lena"})[1]["verified"]
    assert lena("POST", "/api/licenses/payouts/connect", {"country": "AE"})[1]["url"] == "https://connect.stripe.test/lena"
    assert webhook(base, {"type": "account.updated", "data": {"object": {"id": "acct_lena", "payouts_enabled": True}}}) == 200
    transfer = next(p for path, p, _ in stripe if path == "transfers")
    assert transfer["amount"] == 25500 and transfer["destination"] == "acct_lena" and transfer["source_transaction"] == "ch_flow"
    mine = lena("GET", "/api/licenses/mine")[1]
    assert mine["money"] == {"paidUsd": 255, "owedUsd": 0, "live": 1} and mine["offers"][0]["status"] == "live"
    row = next(r for r in ops("GET", "/api/admin/licenses", None, OP)[1]["requests"] if r["id"] == rid)
    assert row["paidOnline"] and row["transfer"] == "tr_flow" and row["claimed"] and row["respondedVia"] == "app"

    # A week before the end the brand is reminded; it renews; the (now claimed) creator is emailed the new offer.
    with psycopg.connect(server.DB_URL) as db:
        db.execute("UPDATE license_requests SET expires_at = now() + interval '3 days' WHERE id = %s", (rid,))
    assert ops("POST", "/api/digests/run", {}, OP)[1]["licenses"] == {"ended": 0, "reminded": 1}
    assert any(s_.startswith("Ends ") and "wf_new" in b for s_, b in inbox(BRAND))
    assert brand("GET", api)[1]["request"]["renewable"] is True
    brand("POST", api, {"action": "renew"})
    renewal = brand("GET", api)[1]["request"]
    assert renewal["renewal"] and renewal["status"] == "requested" and renewal["priceUsd"] == 300
    assert any("wants to run your video" in s_ for s_, _ in inbox(CREATOR))

    # The original licence ends.
    with psycopg.connect(server.DB_URL) as db:
        db.execute("UPDATE license_requests SET expires_at = now() - interval '1 hour' WHERE id = %s", (rid,))
    assert ops("POST", "/api/digests/run", {}, OP)[1]["licenses"]["ended"] == 1

    # Monday review (CONCIERGE "Every Monday"): the funnel SQL in docs/LAUNCH.md section 7 runs as written.
    launch = (Path(__file__).resolve().parent.parent / "docs" / "LAUNCH.md").read_text(encoding="utf-8")
    sql = re.search(r"```sql\n(.*?)```", launch.split("## 7.")[1], re.S).group(1)
    with psycopg.connect(server.DB_URL) as db:
        first, second = [q.strip() for q in sql.split(";") if q.strip()]
        funnel = dict(db.execute(first).fetchall())
        by_status = {r[0]: r for r in db.execute(second).fetchall()}
    assert funnel["report_sent"] >= 1 and funnel["license_view"] >= 1 and funnel["license_request"] >= 1
    assert by_status["ended"][2] == 300 and "requested" in by_status


def test_one_failing_job_does_not_stop_the_others(base, monkeypatch):
    import licenses
    monkeypatch.setattr(licenses, "run_summaries", lambda force=False: 1 / 0)
    status, out = Client(base)("POST", "/api/digests/run", {}, OP)
    assert status == 200 and out["summaries"] == {"error": "division by zero"}
    assert out["licenses"] == {"ended": 0, "reminded": 0} and "rosters" in out and "ran" in out
