"""Searches and creator handles Oriane does not index well, for the operator to forward manually."""
from urllib.parse import parse_qs, urlparse

import digest
from server import ApiError, connect

THIN = 5
DEFAULT_DAYS = 30


def _bounded_days(value):
    try:
        days = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "Days must be an integer between 1 and 365.") from None
    if not 1 <= days <= 365:
        raise ApiError(400, "Days must be an integer between 1 and 365.")
    return days


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
              AND NOT (params ? 'digest')
              AND NOT (params ? 'seeding')
            GROUP BY lower(brand)
            HAVING max(total_count) < %s
            ORDER BY people DESC, searches DESC, "lastAt" DESC
            LIMIT 200
            """,
            (days, THIN),
        ).fetchall()
        creators = db.execute(
            """
            SELECT e.data->>'platform' AS platform,
                   lower(e.data->>'handle') AS handle,
                   (array_agg(coalesce(e.data->>'reason', 'unknown')
                              ORDER BY e.created_at DESC, e.id DESC))[1] AS reason,
                   count(*)::int AS attempts,
                   count(DISTINCT e.user_id)::int AS people,
                   max(e.created_at) AS "lastAt"
            FROM events e
            WHERE e.name = 'scan_miss'
              AND e.created_at >= now() - (%s * interval '1 day')
              AND coalesce(e.data->>'reason', 'unknown') <> 'demo'
              AND e.data->>'platform' IS NOT NULL
              AND e.data->>'handle' IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1
                  FROM events s
                  WHERE s.name = 'scan'
                    AND s.data->>'platform' = e.data->>'platform'
                    AND lower(s.data->>'handle') = lower(e.data->>'handle')
                    AND s.created_at > e.created_at
              )
            GROUP BY e.data->>'platform', lower(e.data->>'handle')
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
