"""The console's setup and health panel (/admin/): which production settings are missing, whether the weekly job is
actually running, and the people waiting on an answer only the operator can give. Settings are reported as set or not,
never their values."""
import os

import digest
from server import ApiError, connect

# (name, settings that must all be set, what it unlocks)
SETUP = [
    ("Oriane key", ["ORIANE_API_KEY"], "live searches, scans and weekly reports (without it everything runs on demo data)"),
    ("Email", None, "every email: weekly reports, offers, summaries (MAIL_RELAY_URL + MAIL_RELAY_SECRET, or RESEND_API_KEY + DIGEST_FROM)"),
    ("Operator email", ["LICENSE_NOTIFY_EMAIL"], "licence requests, creators' answers and pilot requests reach you"),
    ("Stripe", ["STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"], "any payment"),
    ("Pro price", ["STRIPE_PRICE_PRO"], "Pro checkout ($29/month); without it the button takes reservations"),
    ("Weekly leads price", ["STRIPE_PRICE_LEADS"], "Weekly leads checkout ($9/month); without it the button takes reservations"),
    ("Roster price", ["STRIPE_PRICE_ROSTER"], "the roster plan ($99/month); without it the button records a commitment"),
    ("Licence payments", ["LICENSE_PAYMENTS"], "brands pay licences online and creators are paid by Stripe Connect"),
    ("In-app scheduler", ["DIGEST_SCHEDULER"], "weekly jobs run inside the app; otherwise a cron must call POST /api/digests/run"),
]
FLAGS = ("LICENSE_PAYMENTS", "DIGEST_SCHEDULER")      # on only when "1"


def is_set(name):
    value = os.environ.get(name) or ""
    return value == "1" if name in FLAGS else bool(value.strip())


def setup():
    rows = []
    for name, keys, unlocks in SETUP:
        ok = (all(map(is_set, ("MAIL_RELAY_URL", "MAIL_RELAY_SECRET"))) or all(map(is_set, ("RESEND_API_KEY", "DIGEST_FROM")))
              if keys is None else all(map(is_set, keys)))
        rows.append({"name": name, "ok": ok, "unlocks": unlocks, "settings": keys or []})
    return rows


def report():
    with connect() as db:
        overdue = db.execute(
            "SELECT count(*) AS n, min(coalesce(last_run_at, created_at)) AS since FROM digests"
            " WHERE confirmed_at IS NOT NULL AND paused_at IS NULL AND coalesce(last_run_at, created_at) < now() - interval '%d days'"
            % (digest.EVERY_DAYS + 1)).fetchone()
        last = db.execute(
            "SELECT (SELECT max(created_at) FROM digest_runs) AS reports,"
            " (SELECT max(created_at) FROM events WHERE name = 'creator_summary') AS summaries,"
            " (SELECT max(created_at) FROM roster_reports) AS rosters,"
            " (SELECT max(created_at) FROM events WHERE name = 'feed_sent') AS feeds").fetchone()
        used = db.execute("SELECT coalesce(sum(credits), 0) AS n FROM oriane_calls"
                          " WHERE created_at >= (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')").fetchone()["n"]
        pilots = db.execute(
            "SELECT DISTINCT ON (u.id) u.email, e.created_at AS at, e.data->>'note' AS note FROM events e JOIN users u ON u.id = e.user_id"
            " WHERE e.name = 'roster_pilot_request' AND NOT EXISTS (SELECT 1 FROM rosters r WHERE r.user_id = u.id)"
            " ORDER BY u.id, e.created_at DESC").fetchall()
        reservations = db.execute(
            "SELECT DISTINCT ON (u.id) u.email, e.data->>'plan' AS plan, e.created_at AS at FROM events e JOIN users u ON u.id = e.user_id"
            " WHERE e.name = 'checkout_intent' AND u.plan = 'free' ORDER BY u.id, e.created_at DESC").fetchall()
        commitments = db.execute(
            "SELECT DISTINCT ON (u.id) u.email, e.created_at AS at FROM events e JOIN users u ON u.id = e.user_id"
            " WHERE e.name = 'roster_commit' AND u.plan <> 'roster' ORDER BY u.id, e.created_at DESC").fetchall()
    return {"setup": setup(),
            "weekly": {"overdueWatches": overdue["n"], "overdueSince": overdue["since"], "lastRuns": dict(last),
                       "creditsToday": used, "dailyBudget": int(os.environ.get("ORIANE_DAILY_BUDGET", "600"))},
            "waiting": {"pilots": sorted(pilots, key=lambda r: r["at"]), "reservations": sorted(reservations, key=lambda r: r["at"]),
                        "commitments": sorted(commitments, key=lambda r: r["at"])}}


def admin_route(handler):
    digest.require_operator(handler)
    if handler.command == "GET":
        return report()
    raise ApiError(405, "Method not allowed.")
