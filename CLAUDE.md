# Unprompted

Hackathon prototype (Replit x Oriane). Finds creators who mention a brand on camera through Oriane's transcript search, ranks them as advocates, and score-checks them (brand safety, performance benchmark, moodboard).

## Run
- Postgres 16 (Homebrew), database `unprompted`:
  `/opt/homebrew/opt/postgresql@16/bin/pg_ctl -D /opt/homebrew/var/postgresql@16 -l /opt/homebrew/var/log/postgresql@16.log start`
  (or `brew services start postgresql@16` to start it at login).
- `python3 backend/server.py` serves Receipts at http://127.0.0.1:8000/ and the brand dashboard at http://127.0.0.1:8000/brands/. Schema is created on start.
- Self-check: `python3 backend/test_server.py`
- Env (`.env`, gitignored): `ORIANE_API_KEY`; optional `DATABASE_URL`, `HOST`, `PORT`. On Replit: `HOST=0.0.0.0` plus Replit's `DATABASE_URL`.

## Layout
- `backend/server.py`: stdlib HTTP server + psycopg 3. Oriane client, mention classifier, score check, Postgres storage (`searches`, `videos`, `mentions`, `checks`).
- `frontend/index.html`: single-file dashboard, vanilla JS, no build step.
- `frontend/hero3d.js`: the landing hero's 3D "listening drum" (three.js pinned on jsDelivr, imported lazily on screens ≥760px wide and wider than tall; the CSS wave is the fallback). Quotes in it are illustrative templates around the typed brand.
- `backend/licenses.py`: the creator side of "License for ads": `/offer/<token>` answers one request without an account; `/creators/licenses` (Receipts login) claims handles, lists offers, sets rules and the weekly summary. `docs/CONCIERGE.md` is the operator playbook; `/admin/` is the console.
- `backend/payments.py`: licence money and renewals: Stripe Checkout for brands, Connect transfers to creators, refunds, expiry and renewal reminders, creator rates; Stripe parts only with `LICENSE_PAYMENTS=1`.
- `PRODUCT.md`: design context (users, personality, principles).

## Oriane API notes
- `POST https://connect.oriane.xyz/rest/contents/search` with `Authorization: Bearer $ORIANE_API_KEY`. Paginated results come back as HTTP 206.
- Per-video `engagementRatePerViews` is a percentage (10.3 means 10.3%), unlike the docs example.
- Instagram `platformId` is the post shortcode; TikTok's is the numeric video id.
- Only content search is exposed. Moodboard, Benchmark and Creator Checker are rebuilt locally from search results.

## Receipts (creator product)

`frontend/creators/index.html` + `backend/creator.py` reuse the Oriane fetch, `term_pattern` and `find_quote` from `server.py` to scan one creator's own videos against the brand dictionary in `backend/brands.py`. `backend/demo.py` supplies fixture creators when `ORIANE_API_KEY` is missing or `DEMO_MODE=1`. Auth, plans (free/pro), Stripe and event logging live in `creator.py`; its tables are created by `server.init_db()`.

- Tests: `DATABASE_URL=postgresql:///unprompted_test python3 -m pytest backend`
- Lint: `ruff check backend` (config in `pyproject.toml`)
