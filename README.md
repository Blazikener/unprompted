# Unprompted

Find the creators who already talk about your brand on camera. Built at the Replit x Oriane hackathon.

Unprompted searches Oriane's transcript index of Instagram and TikTok videos for spoken brand mentions, separates unprompted mentions from sponsored ones, and ranks creators as advocates. Every match comes with its receipt: the quote, the timestamp and the video frame. A score check adds brand safety, a performance benchmark and a moodboard from each creator's last 30 videos, and outreach drafts quote the creator's own words back in English or Arabic. Every search is stored in Postgres and reopens instantly.

## Run

1. Postgres running locally with a database named `unprompted` (`createdb unprompted`), or set `DATABASE_URL`.
2. `cp .env.example .env` and add your `ORIANE_API_KEY`.
3. `python3 -m pip install "psycopg[binary]"`
4. `python3 backend/server.py`, then open http://127.0.0.1:8000

Self-check: `python3 backend/test_server.py`

Hosted: `app/main.py` wraps the same server as an ASGI app (`uvicorn app.main:app`) for platforms that expect FastAPI,
and reads a `.env` next to `pyproject.toml` if present. Any Postgres works as `DATABASE_URL` (a free Supabase project via
its session pooler is fine). Without Stripe keys the "Go Pro" button records a `checkout_intent` event instead of opening checkout.

## Stack

- `backend/server.py`: Python standard-library HTTP server with psycopg 3. Oriane client, mention classifier, score check, Postgres storage.
- `frontend/index.html`: single-file vanilla JS dashboard, no build step.
- Oriane content search API for transcripts, frames and engagement data. No LLMs: classification, brand safety and scoring are rule-based.

## Receipts: the creator side (`/creators/`)

The same transcript index, pointed the other way. A creator pastes their TikTok or Instagram handle and gets every brand they've said, tagged or featured in their recent videos, split into unpaid mentions and disclosed ads, each with the receipt (quote, timestamp, frame, views, link). Pro ($29/mo) unlocks every brand, a check of which brands are paying creators right now, and a pitch draft that quotes the creator's own video back to the brand.

- Backend: `backend/creator.py` (routes under `/api/creators/*`), brand dictionary in `backend/brands.py`, fixture creators in `backend/demo.py`.
- Accounts are email + password (scrypt), sessions are HttpOnly cookies. Billing is Stripe Checkout + webhooks and stays disabled until `STRIPE_SECRET_KEY`, `STRIPE_PRICE_PRO` and `STRIPE_WEBHOOK_SECRET` are set; `DEV_PLAN_SWITCH=1` gives a local plan toggle instead.
- Without `ORIANE_API_KEY` (or with `DEMO_MODE=1`) the scan runs on two fixture creators, `@maya.eats` and `@sami.lifts`, and the UI labels everything as demo data. `/creators/?sample=1` shows that fixture as a full Pro report to logged-out visitors in any mode.
- A creator can publish a scan as a read-only receipt page (`POST /api/creators/scans/<id>/share` → `/creators/r/<token>`), gated at the owner's plan, to drop into a pitch or media kit.
- `python3 backend/prospect.py` (needs the Oriane key) ranks real creators who name-drop paying brands unpaid and writes `first-users.md` with a DM per creator; that file is personal data and gitignored.
- Tests: `DATABASE_URL=postgresql:///unprompted_test python3 -m pytest backend` (needs `createdb unprompted_test`; the suite truncates it). Lint: `ruff check backend`.
- Why this product: see `docs/MARKET-VALIDATION.md`.

## Weekly digest: the push side (`/`, "Email me weekly")

Save a brand search once with just an email and get the new videos where creators mention the brand every week:
the quote, who said it, and whether it was said on camera, tagged, or a disclosed partnership. Built as an
[Oriane](https://oriane.xyz) Community Tool: every digest is one Oriane content search (videos published since the
last run) and the emails and pages credit Oriane.

- `POST /api/digests` `{email, brand, variants, platform, lang, searchId}`: saves the digest. With `RESEND_API_KEY`
  set it is double opt-in (confirmation email); without it the digest is active at once and emails are logged, not
  sent. Videos already on the dashboard (`searchId`) count as seen, so the first digest only has new ones.
- `/digest/<token>`: manage page (pause, resume, unsubscribe, past runs, latest email preview).
- `POST /api/digests/run` with `Authorization: Bearer $DIGEST_RUN_TOKEN`: runs due digests (`{"force": true}` all,
  `{"id": n}` one). `DIGEST_SCHEDULER=1` does the same in-process every 15 minutes. Both are off until configured, so
  a fresh deployment spends no Oriane credits.
- Cost: 40 Oriane credits per digest per week (one search, `sort=publishedAt`, limit 100). Quiet weeks send nothing.

Tests: `DATABASE_URL=postgresql:///unprompted_test python3 -m pytest backend/test_digest.py` (Oriane is mocked).
