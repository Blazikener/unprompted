# Receipts: getting the first users

Goal for the first week: 100 free scans, 5 checkout starts (the go/no-go gate in `MARKET-VALIDATION.md`). Everything
below is written so the founder can execute it in an afternoon. Nothing here is automated outreach; each message is
sent by a person from their own account.

## 0. Before sending anything

1. Deploy behind a public URL with `ORIANE_API_KEY`, `APP_URL=https://<domain>`, and Stripe test keys first, then live
   keys (`STRIPE_SECRET_KEY`, `STRIPE_PRICE_PRO` for a $29/mo price, `STRIPE_WEBHOOK_SECRET` from a webhook on
   `/api/creators/billing/webhook`). Without Stripe the paywall offers "Reserve Pro at $29/mo" and logs a `checkout_intent`
   event per click, so the gate can be measured before billing is live; real checkout starts replace it once Stripe is on.
   Current production: Render free web service `receipts` (Docker runtime from this repo's `Dockerfile`, region
   Singapore, health check `/creators/`) at https://receipts-thc8.onrender.com, Postgres on Supabase (session pooler).
   `DATABASE_URL`, `ORIANE_API_KEY` and `APP_URL` live in the service's environment on Render, never in the repo.
   It auto-deploys on push to the linked branch; after the PR merges, point the service at `main`
   (`PATCH /v1/services/<id>` with `{"branch": "main"}` or Settings → Build & Deploy). Free instances sleep after
   15 minutes idle, so the first request after a pause takes 30–50 s; upgrade the plan before any paid campaign.
   Fallback hosts: `fly.toml` is ready for `fly launch --copy-config --no-deploy && fly secrets set … && fly deploy`;
   Railway picks the same `Dockerfile` up automatically.
2. Expect misses on Instagram only. Oriane's profile index is far broader than its video index, so a creator can be
   known (followers shown) with zero videos to scan. For TikTok, `backend/tiktok_public.py` reads the creator
   straight from TikTok's public embed page (latest ~10 videos, captions, tags, TikTok's own auto-captions) at zero
   Oriane cost; Free TikTok scans use it first (Oriane only if the public page is empty), Pro scans go Oriane-first
   with the public read as fallback. Public scans are labelled "Public data". Instagram has no logged-out public surface, so misses there
   explain themselves and offer same-handle-other-platform or close-handle chips.
   Watch `scan_miss / (scan + scan_miss)` weekly; if it stays above ~40%, prospect only from creators Oriane
   already indexes (`backend/prospect.py` does) and say "works for creators in our index" in the copy.
   Credits: a Free TikTok scan is normally 0; a Pro or Instagram scan is one content search (40), a miss adds one profile lookup (30 → 70 total); misses are
   remembered per handle for 7 days (`misses` table) so retries and other users trying the same handle cost 0.
3. Open `/creators/?sample=1` on the deployed URL: this is the link that goes in every message.
4. Run `python3 backend/prospect.py --app-url https://receipts-thc8.onrender.com/creators/` to regenerate
   `first-users.md` with live links.

## 1. Direct DMs (highest yield, ~30 a day)

`first-users.md` lists creators who name-drop paying brands unpaid, with a DM per creator that quotes their own words.
Send from a personal account, not a brand account; TikTok DMs from unknown accounts land in "message requests", so
Instagram DMs and the email in the creator's bio work better. Log Sent / Replied / Signed up in the table.

Rules: one message, no follow-up unless they reply; if they ask how it works, say it reads public transcripts through
Oriane's index and never touches their account.

## 2. Communities (one post each, honest framing)

Post as a builder asking for feedback, with the sample link and an offer of a free Pro month for the first 20 who reply
with their handle. Do not post the same text twice.

- r/InfluencerMarketing, r/Tiktokhelp, r/NewTubers (short-form thread), r/Instagram (check each sub's self-promo rules first)
- Indie Hackers "Show IH", Hacker News "Show HN" (ready-to-use title below)
- Creator Discords you are already in, X/Threads with a 30-second screen recording of a real scan

Show HN title: "Show HN: Receipts – find brands you already mention on camera, then pitch them with the quote"

## 3. The scan-as-content loop

Every scan can be published as `/creators/r/<token>`. Ask each early user for permission to quote their receipt page
in a post ("@creator has said Rhode on camera 6 times, 410K views, never paid"). Creators share these because it is
flattering and brands read them because it is evidence. Track shares in the `events` table (`name = 'share'`).

## 4. What to measure (all in the `events` table)

| Event | Meaning |
| --- | --- |
| `signup` | account created |
| `scan` | scan completed (`data.source` = live/demo, `data.brands`) |
| `paywall` | free user hit a Pro gate (`data.reason` = scan_quota / activity / pitch) |
| `checkout_started` | Stripe Checkout opened |
| `subscribed` | webhook confirmed a paid subscription |
| `share` | receipt page published |
| `report` | "brand missing" feedback (`data.text`) |
| `scan_miss` | live scan found no videos (`data.reason` = profile_only / other_platform / unknown, `data.suggested`) |

`SELECT name, count(*) FROM events WHERE created_at > now() - interval '7 days' GROUP BY 1;` is the weekly funnel.
Gate: `checkout_started / scan >= 5%` over the first 100 scans, or change the offer before spending more on acquisition.

## 5. Copy that has to stay honest

- "Reads your public videos' transcripts" (true); not "AI analyses your account" (we never log in).
- Rate ranges are a CPM rule of thumb, labelled as such in the UI; never call them market data.
- The sample report is fixture data and says so; never present it as a real creator.

## 6. Weekly digest (Oriane Community Tool)

The brand-side push product discussed with Oriane's CTO: save a search, email only, weekly digest of new mentions.
It lives on the brand dashboard at `/brands/` (card under the mention mix) and does not touch Receipts.

- Runs on Oriane only (no public-page fallback) and credits Oriane in the card, the manage page, and every email.
- Zero credits until switched on: set `DIGEST_RUN_TOKEN` (manual/cron runs) or `DIGEST_SCHEDULER=1` on Render; the scheduler
  checks about a minute after boot, then every 15 minutes while awake. Render free instances sleep after 15 idle minutes,
  so it runs whenever awake (any visit wakes it); use an external cron calling `POST /api/digests/run` with `{"force": false}`
  for a reliable wall-clock trigger.
- Email delivery needs `RESEND_API_KEY` + `DIGEST_FROM` (Resend free tier, verified domain). Without them the flow
  still works end to end: subscribers are active immediately and see the latest digest on `/digest/<token>`.
- Review checklist before sending Oriane the link: a real brand search → subscribe → `POST /api/digests/run {"id": n}`
  (40 credits) → email received → dashboard link opens the digest's search.
