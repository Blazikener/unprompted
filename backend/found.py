"""Weekly brand-discovery signals for claimed creator handles."""

from server import connect


def week(user, days=7):
    user_id = user["id"]
    import collabs

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
    )
    SELECT s.id AS search_id, s.brand, s.created_at, v.id AS video_id, v.published_at,
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
    ORDER BY s.created_at ASC, s.id ASC
    """
    with connect() as db:
        matches = db.execute(query, (user_id, user_id, days, user_id, user_id)).fetchall()
        checks = db.execute(
            "SELECT c.id, c.brand, c.created_at, c.platform, lower(c.handle) AS handle"
            " FROM checks c JOIN creator_handles h ON h.user_id = %s AND h.verified_at IS NOT NULL"
            " AND h.platform = c.platform AND lower(h.handle) = lower(c.handle)"
            " WHERE c.user_id IS NOT NULL AND c.user_id <> %s"
            " AND c.created_at > now() - make_interval(days => %s)"
            " ORDER BY c.created_at ASC, c.id ASC",
            (user_id, user_id, days),
        ).fetchall()
        tracked = db.execute("SELECT brand FROM creator_watch WHERE user_id = %s", (user_id,)).fetchall()

    def brand_key(name):
        return collabs.brand_key(name).casefold()

    groups = {}

    def group_for(name):
        key = brand_key(name)
        return groups.setdefault(key, {
            "brand": name, "search_ids": set(), "video_ids": set(), "check_ids": set(),
            "last_at": None, "handles": {}, "check_handles": {},
        })

    events = [(row["created_at"], row["search_id"], "search", row) for row in matches]
    events.extend((row["created_at"], row["id"], "check", row) for row in checks)
    events.sort(key=lambda event: (event[0], event[1], event[2]))
    locked_handles = {}
    for created_at, _, kind, row in events:
        if kind == "search" and not row["is_owned"]:
            key = (row["platform"], row["handle"])
            locked_handles.setdefault(key, set()).add(row["search_id"])
            continue
        entry = group_for(row["brand"])
        entry["last_at"] = max(entry["last_at"], created_at) if entry["last_at"] else created_at
        if kind == "check":
            entry["check_ids"].add(row["id"])
            key = (row["platform"], row["handle"])
            handle = entry["check_handles"].setdefault(key, {"ids": set(), "latest": None})
            handle["ids"].add(row["id"])
            handle["latest"] = max(handle["latest"], (created_at, row["id"])) if handle["latest"] else (created_at, row["id"])
        elif row["is_owned"]:
            entry["search_ids"].add(row["search_id"])
            entry["video_ids"].add(row["video_id"])
            key = (row["platform"], row["handle"])
            handle = entry["handles"].setdefault(key, {"video_ids": set(), "latest": None, "video_id": None})
            handle["video_ids"].add(row["video_id"])
            latest = (row["published_at"] or created_at, created_at, row["search_id"], str(row["video_id"]))
            if handle["latest"] is None or latest > handle["latest"]:
                handle["latest"], handle["video_id"] = latest, row["video_id"]

    open_brands = {brand_key(row["brand"]) for row in collabs.partners()}
    tracked_brands = {brand_key(row["brand"]) for row in tracked}
    brand_rows = []
    for key, entry in groups.items():
        if entry["handles"]:
            platform, handle = max(
                entry["handles"].items(),
                key=lambda item: (len(item[1]["video_ids"]), item[1]["latest"], item[0]),
            )[0]
            video_id = entry["handles"][(platform, handle)]["video_id"]
        else:
            platform, handle = max(
                entry["check_handles"].items(),
                key=lambda item: (len(item[1]["ids"]), item[1]["latest"], item[0]),
            )[0]
            video_id = None
        brand_rows.append({
            "brand": entry["brand"],
            "searches": len(entry["search_ids"]),
            "videos": len(entry["video_ids"]),
            "checks": len(entry["check_ids"]),
            "lastAt": entry["last_at"].isoformat(),
            "platform": platform,
            "handle": handle,
            "videoId": video_id,
            "open": key in open_brands,
            "tracked": key in tracked_brands,
            "_lastAt": entry["last_at"],
        })
    brand_rows.sort(key=lambda row: (-(row["videos"] + row["checks"]), -row["_lastAt"].timestamp()))
    for row in brand_rows:
        del row["_lastAt"]
    locked = [
        {"platform": platform, "handle": handle, "searches": len(search_ids)}
        for (platform, handle), search_ids in locked_handles.items()
    ]
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
