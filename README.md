# Unprompted

Find the creators who already talk about your brand on camera. Built at the Replit x Oriane hackathon.

Unprompted searches Oriane's transcript index of Instagram and TikTok videos for spoken brand mentions, separates unprompted mentions from sponsored ones, and ranks creators as advocates. Every match comes with its receipt: the quote, the timestamp and the video frame. A score check adds brand safety, a performance benchmark and a moodboard from each creator's last 30 videos, and outreach drafts quote the creator's own words back in English or Arabic. Every search is stored in Postgres and reopens instantly.

## Run

1. Postgres running locally with a database named `unprompted` (`createdb unprompted`), or set `DATABASE_URL`.
2. `cp .env.example .env` and add your `ORIANE_API_KEY`.
3. `python3 -m pip install "psycopg[binary]"`
4. `python3 backend/server.py`, then open http://127.0.0.1:8000

Self-check: `python3 backend/test_server.py`

## Stack

- `backend/server.py`: Python standard-library HTTP server with psycopg 3. Oriane client, mention classifier, score check, Postgres storage.
- `frontend/index.html`: single-file vanilla JS dashboard, no build step.
- Oriane content search API for transcripts, frames and engagement data. No LLMs: classification, brand safety and scoring are rule-based.
