"""Weekly brand-discovery signals for claimed creator handles."""

from datetime import datetime

from server import connect


def week(user, days=7):
    user_id = user["id"]
    query = """
    WITH owned AS (
      SELECT DISTINCT platform, lower(handle) AS handle
      FROM creator_handles
      WHERE user_id = %s AND verified_at IS NOT NULL
    ),
    scanned AS (
      SELECT DISTINCT platform, lower(handle) AS handle
      FROM scans
      WHERE user_id = %s
      EXCEPT
      SELECT platform, handle FROM owned
    ),
    target_handles AS (
      SELECT platform, handle, true AS is_owned FROM owned
      UNION ALL
      SELECT platform, handle, false AS is_owned FROM scanned
    ),
    brand_matches AS (
      SELECT s.id AS search_id, s.brand, s.created_at, v.id AS video_id,
             h.platform, h.handle, h.is_owned
      FROM searches s
      LEFT JOIN digests d ON d.id::text = s.params->>'digest'
      JOIN mentions m ON m.search_id = s.id AND m.kind NOT IN ('owned', 'unverified')
      JOIN videos v ON v.id = m.video_id
      JOIN target_handles h ON h.platform = v.platform AND h.handle = lower(v.handle)
      WHERE s.created_at > now() - make_interval(days => %s)
        AND (
          (s.user_id IS NOT NULL AND s.user_id <> %s)
          OR (s.params ? 'digest' AND d.id IS NOT NULL AND d.user_id IS DISTINCT FROM %s)
        )
    ),
    search_brands AS (
      SELECT lower(brand) AS brand_key,
             (array_agg(brand ORDER BY created_at DESC, search_id DESC))[1] AS brand,
             count(DISTINCT search_id)::int AS searches,
             count(DISTINCT video_id)::int AS videos,
             0::int AS checks,
             max(created_at) AS last_at
      FROM brand_matches
      WHERE is_owned
      GROUP BY lower(brand)
    ),
    check_brands AS (
      SELECT lower(c.brand) AS brand_key,
             (array_agg(c.brand ORDER BY c.created_at DESC, c.id DESC))[1] AS brand,
             0::int AS searches,
             0::int AS videos,
             count(*)::int AS checks,
             max(c.created_at) AS last_at
      FROM checks c
      JOIN owned h ON h.platform = c.platform AND h.handle = lower(c.handle)
      WHERE c.user_id IS NOT NULL
        AND c.user_id IS DISTINCT FROM %s
        AND c.created_at > now() - make_interval(days => %s)
      GROUP BY lower(c.brand)
    ),
    merged_brands AS (
      SELECT coalesce(s.brand_key, c.brand_key) AS brand_key,
             coalesce(s.brand, c.brand) AS brand,
             coalesce(s.searches, 0) AS searches,
             coalesce(s.videos, 0) AS videos,
             coalesce(c.checks, 0) AS checks,
             greatest(s.last_at, c.last_at) AS last_at
      FROM search_brands s
      FULL OUTER JOIN check_brands c USING (brand_key)
    ),
    ranked_brands AS (
      SELECT * FROM merged_brands
      ORDER BY videos + checks DESC, last_at DESC
      LIMIT 12
    ),
    locked_handles AS (
      SELECT platform, handle, count(DISTINCT search_id)::int AS searches
      FROM brand_matches
      WHERE NOT is_owned
      GROUP BY platform, handle
    )
    SELECT 'brand' AS row_type, brand, searches, videos, checks, last_at,
           NULL::text AS platform, NULL::text AS handle
    FROM ranked_brands
    UNION ALL
    SELECT 'locked' AS row_type, NULL::text AS brand, searches, 0 AS videos, 0 AS checks,
           NULL::timestamptz AS last_at, platform, handle
    FROM locked_handles
    """
    with connect() as db:
        rows = db.execute(query, (user_id, user_id, days, user_id, user_id, user_id, days)).fetchall()
        tracked = {
            row["brand"].lower()
            for row in db.execute("SELECT brand FROM creator_watch WHERE user_id = %s", (user_id,)).fetchall()
        }

    brand_rows = []
    locked = []
    for row in rows:
        if row["row_type"] == "locked":
            locked.append({"platform": row["platform"], "handle": row["handle"], "searches": row["searches"]})
            continue
        brand_rows.append({
            "brand": row["brand"],
            "searches": row["searches"],
            "videos": row["videos"],
            "checks": row["checks"],
            "lastAt": row["last_at"].isoformat(),
            "open": False,
            "tracked": row["brand"].lower() in tracked,
        })

    if brand_rows:
        import collabs

        open_brands = {row["brand"].lower() for row in collabs.partners()}
        for row in brand_rows:
            row["open"] = row["brand"].lower() in open_brands

    brand_rows.sort(key=lambda row: (
        -(row["videos"] + row["checks"]),
        -datetime.fromisoformat(row["lastAt"]).timestamp(),
    ))
    locked.sort(key=lambda row: (-row["searches"], row["platform"], row["handle"]))
    return {
        "days": days,
        "brands": brand_rows[:12],
        "locked": locked,
        "verifiedHandles": db_count_owned(user_id),
    }


def db_count_owned(user_id):
    with connect() as db:
        return db.execute(
            "SELECT count(*) AS n FROM (SELECT DISTINCT platform, lower(handle) FROM creator_handles "
            "WHERE user_id = %s AND verified_at IS NOT NULL) owned",
            (user_id,),
        ).fetchone()["n"]
