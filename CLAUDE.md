# Unprompted

Hackathon prototype (Replit x Oriane). Finds creators who mention a brand on camera through Oriane's transcript search, ranks them as advocates, and score-checks them (brand safety, performance benchmark, moodboard).

## Run
- Postgres 16 (Homebrew), database `unprompted`:
  `/opt/homebrew/opt/postgresql@16/bin/pg_ctl -D /opt/homebrew/var/postgresql@16 -l /opt/homebrew/var/log/postgresql@16.log start`
  (or `brew services start postgresql@16` to start it at login).
- `python3 backend/server.py` serves the dashboard and API on http://127.0.0.1:8000. Schema is created on start.
- Self-check: `python3 backend/test_server.py`
- Env (`.env`, gitignored): `ORIANE_API_KEY`; optional `DATABASE_URL`, `HOST`, `PORT`. On Replit: `HOST=0.0.0.0` plus Replit's `DATABASE_URL`.

## Layout
- `backend/server.py`: stdlib HTTP server + psycopg 3. Oriane client, mention classifier, score check, Postgres storage (`searches`, `videos`, `mentions`, `checks`).
- `frontend/index.html`: single-file dashboard, vanilla JS, no build step.
- `PRODUCT.md`: design context (users, personality, principles).

## Oriane API notes
- `POST https://connect.oriane.xyz/rest/contents/search` with `Authorization: Bearer $ORIANE_API_KEY`. Paginated results come back as HTTP 206.
- Per-video `engagementRatePerViews` is a percentage (10.3 means 10.3%), unlike the docs example.
- Instagram `platformId` is the post shortcode; TikTok's is the numeric video id.
- Only content search is exposed. Moodboard, Benchmark and Creator Checker are rebuilt locally from search results.
