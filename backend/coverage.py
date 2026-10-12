"""Searches and creator handles Oriane does not index well, for the operator to forward manually."""
from urllib.parse import parse_qs, urlparse

import digest
from server import ApiError, connect

THIN = 5
DEFAULT_DAYS = 30


def _bounded_days(value):
    try:
        return max(1, min(365, int(value)))
    except (TypeError, ValueError):
        raise ApiError(400, "Days must be an integer between 1 and 365.") from None


def gaps(days=DEFAULT_DAYS):
    days = _bounded_days(days)
    with connect() as db:
        brands = db.execute(
            """
            SELECT (array_agg(brand ORDER BY created_at DESC, id DESC))[1] AS brand,
                   count(*)::int AS searches,
                   count(DISTINCT user_id)::int AS people,
                   max(total_count)::int AS best,
                   max(created_at) AS "lastAt"
            FROM searches
            WHERE created_at >= now() - (%s * interval '1 day')
            GROUP BY lower(brand)
            HAVING max(total_count) < %s
            ORDER BY people DESC, searches DESC, "lastAt" DESC
            LIMIT 200
            """,
            (days, THIN),
        ).fetchall()
        creators = db.execute(
            """
            SELECT data->>'platform' AS platform,
                   lower(data->>'handle') AS handle,
                   (array_agg(coalesce(data->>'reason', 'unknown')
                              ORDER BY created_at DESC, id DESC))[1] AS reason,
                   count(*)::int AS attempts,
                   count(DISTINCT user_id)::int AS people,
                   max(created_at) AS "lastAt"
            FROM events
            WHERE name = 'scan_miss'
              AND created_at >= now() - (%s * interval '1 day')
              AND coalesce(data->>'reason', 'unknown') <> 'demo'
              AND data->>'platform' IS NOT NULL
              AND data->>'handle' IS NOT NULL
            GROUP BY data->>'platform', lower(data->>'handle')
            ORDER BY people DESC, attempts DESC, "lastAt" DESC
            LIMIT 200
            """,
            (days,),
        ).fetchall()
    return {"days": days, "thin": THIN, "brands": brands, "creators": creators}


def admin_route(handler):
    digest.require_operator(handler)
    if handler.command != "GET":
        raise ApiError(405, "Method not allowed.")
    query = parse_qs(urlparse(handler.path).query)
    days = query.get("days", [DEFAULT_DAYS])[0]
    return gaps(_bounded_days(days))
