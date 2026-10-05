"""Authenticated, read-only account aggregates for the shared dashboard."""
from datetime import datetime, timedelta, timezone

import creator
import licenses
import rosters
from server import connect


def stamp(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def month_keys():
    now = datetime.now(timezone.utc)
    index = now.year * 12 + now.month - 1
    return [f"{(index + offset) // 12:04d}-{(index + offset) % 12 + 1:02d}" for offset in range(-11, 1)]


def view(user):
    user_id = user["id"]
    role = user["role"]
    creator_section = licenses_section = brand_section = manager_section = None
    usage = {}

    if role == "creator":
        plan = creator.plan_of(user)
        timeline = {month: 0 for month in month_keys()}
        with connect() as db:
            scans_used = db.execute(
                "SELECT count(*) AS n FROM scans WHERE user_id = %s AND created_at > now() - interval '7 days'",
                (user_id,),
            ).fetchone()["n"]
            scan_count = db.execute("SELECT count(*) AS n FROM scans WHERE user_id = %s", (user_id,)).fetchone()["n"]
            latest_scans = db.execute(
                "SELECT DISTINCT ON (platform, handle) id, platform, handle, profile, brands, created_at "
                "FROM scans WHERE user_id = %s ORDER BY platform, handle, created_at DESC, id DESC",
                (user_id,),
            ).fetchall()

        brands = {}
        brand_scan_latest = {}
        handles = []
        for scan in latest_scans:
            profile = scan["profile"] or {}
            handles.append({"platform": scan["platform"], "handle": scan["handle"], "pfp": profile.get("pfp")})
            for brand in scan["brands"] or []:
                receipts = brand.get("receipts") or []
                mentions = len(receipts)
                row = brands.setdefault(brand["brand"], {
                    "brand": brand["brand"], "category": brand.get("category"), "mentions": 0, "organicViews": 0,
                    "status": brand.get("status"), "scanId": scan["id"],
                })
                row["mentions"] += mentions
                row["organicViews"] += brand.get("organicViews") or 0
                scan_key = (scan["created_at"], scan["id"])
                if brand["brand"] not in brand_scan_latest or scan_key > brand_scan_latest[brand["brand"]]:
                    row["scanId"] = scan["id"]
                    brand_scan_latest[brand["brand"]] = scan_key
                if brand.get("status") == "unpaid":
                    row["status"] = "unpaid"
                elif row["status"] != "unpaid" and brand.get("status") == "past-sponsor":
                    row["status"] = "past-sponsor"
                for receipt in receipts:
                    published = str(receipt.get("publishedAt") or "")
                    if published[:7] in timeline:
                        timeline[published[:7]] += 1

        top_brands = sorted(brands.values(), key=lambda row: (-row["mentions"], row["brand"].lower()))[:8]
        receipts_count = sum(len(brand.get("receipts") or []) for scan in latest_scans for brand in scan["brands"] or [])
        organic_views = sum(brand.get("organicViews") or 0 for scan in latest_scans for brand in scan["brands"] or [])
        unpaid = sum((brand.get("organic") or 0) for scan in latest_scans for brand in scan["brands"] or []
                     if brand.get("status") == "unpaid")
        license_inbox = licenses.mine(user)
        offers = license_inbox["offers"]
        recent = [
            {key: stamp(row[key]) if key == "createdAt" else row[key]
             for key in ("id", "platform", "handle", "pfp", "brands", "createdAt")}
            for row in creator.list_scans(user)[:6]
        ]
        usage = {"scans": {"used": scans_used, "limit": creator.PLANS[plan]["scansPerWeek"]}}
        creator_section = {
            "scans": scan_count,
            "handles": handles,
            "brands": len(brands),
            "receipts": receipts_count,
            "organicViews": organic_views,
            "unpaid": unpaid,
            "topBrands": top_brands,
            "timeline": [{"month": month, "mentions": timeline[month]} for month in timeline],
            "recent": recent,
        }
        licenses_section = {
            "verifiedHandles": sum(bool(handle["verified"]) for handle in license_inbox["handles"]),
            "waiting": sum(o["status"] in ("requested", "contacted") and not (o["counterUsd"] and o["respondedVia"])
                           for o in offers),
            "accepted": sum(o["status"] == "accepted" for o in offers),
            "live": license_inbox["money"]["live"],
            "paidUsd": license_inbox["money"]["paidUsd"],
            "owedUsd": license_inbox["money"]["owedUsd"],
        }
    elif role == "brand":
        with connect() as db:
            digest_counts = db.execute(
                "SELECT count(*) FILTER (WHERE confirmed_at IS NOT NULL AND paused_at IS NULL) AS active, "
                "count(*) FILTER (WHERE confirmed_at IS NULL) AS pending FROM digests WHERE user_id = %s",
                (user_id,),
            ).fetchone()
            brand_counts = db.execute(
                "SELECT count(*) AS mentions, coalesce(sum(unique_videos.views), 0) AS views FROM ("
                "SELECT DISTINCT v.id, v.views FROM searches s JOIN mentions m ON m.search_id = s.id "
                "JOIN videos v ON v.id = m.video_id "
                "WHERE s.user_id = %s AND m.kind NOT IN ('owned', 'unverified')"
                ") unique_videos",
                (user_id,),
            ).fetchone()
            utc_today = datetime.now(timezone.utc).date()
            weekly_start = utc_today - timedelta(days=utc_today.weekday(), weeks=7)
            weekly_end = weekly_start + timedelta(weeks=8)
            weekly_rows = db.execute(
                "SELECT date_trunc('week', v.published_at AT TIME ZONE 'UTC')::date AS week, "
                "count(DISTINCT v.id) AS mentions "
                "FROM searches s JOIN mentions m ON m.search_id = s.id JOIN videos v ON v.id = m.video_id "
                "WHERE s.user_id = %s AND m.kind NOT IN ('owned', 'unverified') "
                "AND v.published_at >= %s AND v.published_at < %s GROUP BY week",
                (user_id, weekly_start, weekly_end),
            ).fetchall()
            top_creators = db.execute(
                "SELECT platform, handle, count(*) AS mentions, coalesce(sum(views), 0) AS views FROM ("
                "SELECT DISTINCT v.id, v.platform, v.handle, v.views "
                "FROM searches s JOIN mentions m ON m.search_id = s.id JOIN videos v ON v.id = m.video_id "
                "WHERE s.user_id = %s AND m.kind NOT IN ('owned', 'unverified')"
                ") unique_videos GROUP BY platform, handle ORDER BY mentions DESC, views DESC, handle LIMIT 6",
                (user_id,),
            ).fetchall()
            recent_searches = db.execute(
                'SELECT id, brand, total_count AS "totalCount", created_at AS "createdAt" FROM ('
                "SELECT DISTINCT ON (lower(brand)) id, brand, total_count, created_at FROM searches "
                "WHERE user_id = %s ORDER BY lower(brand), created_at DESC, id DESC"
                ') recent ORDER BY "createdAt" DESC, id DESC LIMIT 6',
                (user_id,),
            ).fetchall()
            license_requests = db.execute(
                "SELECT count(*) FILTER (WHERE r.status IN ('requested', 'contacted')) AS requested, "
                "count(*) FILTER (WHERE r.status = 'accepted') AS accepted, "
                "count(*) FILTER (WHERE r.status = 'live') AS live, "
                "count(*) FILTER (WHERE r.status = 'declined') AS declined "
                "FROM license_requests r JOIN digests d ON d.id = r.digest_id WHERE d.user_id = %s",
                (user_id,),
            ).fetchone()
        weekly_map = {row["week"].isoformat(): row["mentions"] for row in weekly_rows}
        weekly = [
            {"week": (weekly_start + timedelta(weeks=i)).isoformat(),
             "mentions": weekly_map.get((weekly_start + timedelta(weeks=i)).isoformat(), 0)}
            for i in range(8)
        ]
        usage = creator.brand_usage(user)
        brand_section = {
            "searches": search_count(user_id),
            "digests": {"active": digest_counts["active"], "pending": digest_counts["pending"]},
            "mentions": brand_counts["mentions"],
            "views": brand_counts["views"],
            "weekly": weekly,
            "topCreators": [dict(row) for row in top_creators],
            "recentSearches": [{**dict(row), "createdAt": stamp(row["createdAt"])} for row in recent_searches],
            "licenseRequests": {key: license_requests[key] for key in ("requested", "accepted", "live", "declined")},
        }
    elif role == "manager":
        manager_roster = rosters.mine(user)
        roster = rosters.load(user_id)
        report_count, last_report_at = 0, None
        if roster:
            with connect() as db:
                report_count = db.execute(
                    "SELECT count(*) AS n FROM roster_reports WHERE roster_id = %s", (roster["id"],)
                ).fetchone()["n"]
                last = db.execute(
                    "SELECT created_at FROM roster_reports WHERE roster_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
                    (roster["id"],),
                ).fetchone()
                last_report_at = stamp(last["created_at"]) if last else None
        manager_section = {
            "hasRoster": manager_roster["hasRoster"],
            "access": rosters.access(roster),
            "creators": len(manager_roster["creators"]),
            "reports": report_count,
            "lastReportAt": last_report_at,
            "deals": len(manager_roster["deals"]),
            "dealsUsd": sum(deal["amountUsd"] or 0 for deal in manager_roster["deals"]),
        }

    with connect() as db:
        activity = db.execute(
            'WITH meaningful AS ('
            "SELECT name, data, created_at, id, "
            "lag(name) OVER (ORDER BY created_at DESC, id DESC) AS previous_name, "
            "lag(data) OVER (ORDER BY created_at DESC, id DESC) AS previous_data "
            "FROM events WHERE user_id = %s AND name IN ("
            "'signup', 'scan', 'share', 'checkout_intent', 'checkout_started', 'subscribed', "
            "'digest_subscribe', 'digest_confirm', 'offer_accept', 'offer_decline', 'offer_counter', 'offer_code', "
            "'license_checkout', 'license_request', "
            "'roster_pilot_request', 'roster_deal', 'roster_commit', 'roster_report', "
            "'roster_creator_added', 'roster_creator_removed')) "
            'SELECT name, data, created_at AS "createdAt" FROM meaningful '
            "WHERE name IS DISTINCT FROM previous_name OR data IS DISTINCT FROM previous_data "
            "ORDER BY created_at DESC, id DESC LIMIT 10",
            (user_id,),
        ).fetchall()

    return {
        "role": role,
        "user": creator.public_user(user),
        "memberSince": stamp(user.get("created_at")),
        "usage": usage,
        "creator": creator_section,
        "licenses": licenses_section,
        "brand": brand_section,
        "manager": manager_section,
        "activity": [{**dict(row), "createdAt": stamp(row["createdAt"])} for row in activity],
    }


def search_count(user_id):
    with connect() as db:
        return db.execute("SELECT count(*) AS n FROM searches WHERE user_id = %s", (user_id,)).fetchone()["n"]
