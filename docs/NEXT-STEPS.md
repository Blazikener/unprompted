# Next steps: from a weekly email to weekly money

Date: 2026-10-02. Built on `reports/Creator brand deal community research.md`.

The research reduces everything to one question: **will money move through Unprompted every week?** The plan answers that
by hand first (4 weeks, almost no new code), and only then builds automation around whichever side pays. Every phase ends
in a gate; if a gate fails, the plan says what to do instead.

Tags: **[Ops]** is founder work (outreach, brokering, reviews). **[Code]** is a change to this repo.

## Where we are

- Brand side: weekly watch on `/brands/`, "Watching · weekly" tags, 7-day schedule, and a "License for ads" button on
  each organic mention in the email. Requests land in `license_requests` and are emailed to `LICENSE_NOTIFY_EMAIL`.
  Nothing is charged yet.
- Creator side: Receipts scan at `/`, free and Pro ($29/month) plans, Stripe Checkout and webhook in `backend/creator.py`.
- Not live yet: branch `weekly-watch-license-hero3d` is pushed to GitHub but not merged into `main`.

## The plan at a glance

| Phase | Weeks | Goal | Research test | Gate at the end |
|---|---|---|---|---|
| 0. Ship and instrument | Days 1-3 | Live, reliable, measurable | — | Licence funnel visible in `events` |
| 1. Concierge licensing | 1-4 | Brands pay; creators accept | Tests 1 and 2 | 3+ brands pay, 1+ renews; 50%+ creators accept in 72h |
| 2. Creator licence inbox | 5-7 | Creators approve requests themselves | Test 7 (reuse alerts) | 70%+ of requests answered in-app |
| 3. Payments and renewals | 6-9 | Money moves without you | — | First fully automated paid licence |
| 4. Roster seat for managers | 5-10 | A second paying segment | Test 5 | 3+ managers commit at $99+ |
| 5. Seeding ROI add-on | 8-11 | A reason for brands to keep the watch | Test 4 | 2x more posters found than tags/codes |
| 6. GCC agency pilot | 6-12 | Regional wedge | Test 6 | 80%+ Arabic precision; 1 paid pilot |
| 7. Receipts packaging test | 9-12 | Right creator offer | Test 3 | Best arm by week-4 activity and paid conversion |

Phases 1, 4 (discovery only) and 6 (evaluation only) can start in parallel. Phases 2 and 3 start only if Gate 1 passes.

---

## Phase 0. Ship and instrument (days 1-3)

**0.1 Go live [Ops]**
1. Merge `weekly-watch-license-hero3d` into `main`; Render redeploys.
2. In Render's environment: set `LICENSE_NOTIFY_EMAIL` to your address.
3. Make the weekly run reliable. Render's free plan sleeps after 15 minutes, so the in-process scheduler only runs when
   someone visits. Either upgrade the service (Starter plan) before the pilot, or add an external daily cron that calls
   `POST /api/digests/run` with `Authorization: Bearer $DIGEST_RUN_TOKEN` and `{"force": false}`.
4. Smoke test on production: search a brand, start a watch, force one run for that watch (`{"id": n}`), open the email,
   click a licence button, submit, and confirm the notification arrives.

*Done when:* a real email with licence buttons reaches you, and a test request reaches `LICENSE_NOTIFY_EMAIL`.

**0.2 Licence funnel events [Code]** (`backend/digest.py`)
1. In `run_one`: `creator.track(d["user_id"], "report_sent", {"digest": id, "new": n, "licensable": k})`.
2. In `license_view` (GET): `track(..., "license_view", {"digest", "video", "price"})`. This doubles as the email
   click-through, since the button is the only way to reach the page.
3. In `license_view` (POST, first insert only): `track(..., "license_request", {"digest", "video", "days", "price"})`.
4. Add the weekly funnel query to `docs/LAUNCH.md`: reports sent → licence views → requests → accepted → paid.

*Done when:* one test run produces all three events.

**0.3 Operator console for licence requests [Code]**
Brokering 20 brands from raw SQL won't hold up. Build a small private page.
1. Schema (in `digest.SCHEMA`, using `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`): add to `license_requests`
   `contacted_at`, `responded_at`, `creator_price_usd`, `final_price_usd`, `code_received_at`, `brand_paid_at`,
   `creator_paid_at`, `starts_at`, `expires_at`, `ops_note`.
2. API: `GET /api/admin/licenses` and `POST /api/admin/licenses/<id>` guarded by the same bearer token as the digest run
   (`DIGEST_RUN_TOKEN`). The POST updates status and any of the fields above, and stamps timestamps on status changes.
3. Page: `frontend/admin/licenses.html`, a table of requests (brand, creator, video, price, status, age in hours) with a
   status menu and fields for counter price, final price and notes. The token is pasted once and kept in
   `sessionStorage`.
4. Tests in `backend/test_digest.py`: auth required; a status change stamps `contacted_at` and `responded_at`; the
   brand's licence page shows the new status.

*Done when:* you can run the whole brokering loop without opening a database client.

---

## Phase 1. Concierge licensing (weeks 1-4): research tests 1 and 2

Goal: prove that brands pay upfront for unprompted clips, and that creators accept a pre-priced 30-day licence.

Day-to-day steps, message drafts and the licence template: `docs/CONCIERGE.md`. Shortlist script: `backend/prospect_brands.py`.

**1.1 Shortlist 20 brands with unprompted mentions [Code + Ops]**
1. [Code] Add `backend/prospect_brands.py`, modelled on `backend/prospect.py`.
   - Input: a list of 40-60 candidate brands in beauty, fashion and DTC (the research's first vertical), plus GCC brands.
   - For each brand: one Oriane search over the last 30 days. Classify mentions with `server.classify`, and keep spoken
     and tagged mentions only (no disclosed ads).
   - Output: a markdown file (gitignored, since it holds personal data) listing, per brand: unprompted mentions,
     distinct creators, the 3 best clips by views, and each clip's indicative price from `digest.license_price`.
   - Respect `ORIANE_DAILY_BUDGET`. Each brand costs one search (40 credits), so 50 brands take 2,000 credits, about
     4 days at the default 600 per day. Add a `--resume` flag to continue the next day.
2. [Ops] Keep brands with 3+ unprompted mentions from creators with 5K-500K followers. Aim for 20.

**1.2 Put each brand on the weekly email [Ops]**
1. Find the person who runs creators or paid social (head of growth, creator or influencer lead, paid social manager).
2. Send one message with three real receipts (quote, views, link) and one line: "Want these every week? You can license
   any of them as a Spark Ad or partnership ad, about $X for 30 days."
3. With their yes, start a watch for their email from the dashboard. They confirm by email (double opt-in).
4. Log each brand's stage (contacted, replied, watching, requested, paid) in a sheet, or later in the console.

*Target:* 20 brands contacted in week 1, 10+ watching by week 2.

**1.3 Broker each request with the creator [Ops]**
1. Within 24 hours of a request, contact the creator by email from their bio, or by DM from a personal account.
2. Say who the brand is, which video, the window (30 days), the price, and what they would do (issue a TikTok Spark code
   or approve an Instagram partnership ad). Link the video.
3. Attach a one-page licence: paid ads only, named platforms, 30 days, no edits beyond captions and cropping, no
   perpetual use, and the creator keeps ownership. Get a lawyer to check this once before the first deal.
4. Log the response in the console: accepted, declined, or a counter price, with timestamps. Test 2 measures time to
   answer and counters.

**1.4 Take payment by hand [Ops]**
1. When the creator accepts, send the brand a Stripe Payment Link or invoice for the final price, payable before the code
   is released.
2. When the code is delivered: record `code_received_at`, set `starts_at` and `expires_at`, and pay the creator 85-90%
   by bank transfer, Wise or PayPal. Record `creator_paid_at`.
3. If the creator declines, refund the brand at once.
4. Ask an accountant how to invoice this during the pilot (as a brokered service or on the creator's behalf), including
   UAE VAT if you invoice from there.

**1.5 Renewals and reminders [Ops]**
Seven days before `expires_at`, email the brand: renew for another 30 days at the same price? A renewal is the strongest
signal in the whole plan.

**1.6 Weekly review [Ops]**
Every Monday, run the funnel query. Record brands watching, licence views, requests, creator acceptance rate, median
hours to accept, licences paid, renewals, and revenue (gross and our cut).

**Gate 1 (end of week 4).** These thresholds are decision rules proposed in the research, not findings.

| Result | What to do |
|---|---|
| 3+ brands paid for at least one licence, 1+ renewed, and 50%+ of creators accepted within 72h | Pass. Start phases 2 and 3. |
| Brands pay, but creators decline or counter a lot | Re-price: raise the floor and per-month rate, target 50K-500K creators, and pre-agree rates with creators before showing brands. Repeat for 2 weeks. |
| Creators accept, but brands don't pay | The licence isn't the wedge. Make the roster seat (phase 4) primary, and keep licence buttons as a free extra. |
| Neither side moves | Stop licensing work. Keep the watch as a lead-in to the roster seat and the seeding report. |

---

## Phase 2. Creator licence inbox in Receipts (weeks 5-7, only if Gate 1 passes)

Goal: creators answer requests in the product, not by DM, and keep Receipts installed because offers arrive there.

**2.1 Claim a handle [Code]** (`backend/creator.py`, `frontend/creators/index.html`)
1. Table `creator_handles` (`user_id`, `platform`, `handle`, `code`, `verified_at`).
2. TikTok: the creator adds a short code to their bio, and Receipts checks the public profile through
   `backend/tiktok_public.py`. Check first that the profile data it reads includes the bio text.
3. Instagram has no public profile surface we can read, so verify by hand during the pilot: the creator DMs the code
   from the account.

**2.2 Licence inbox [Code]**
1. A list of requests for my handles: brand, video, window, price, and Accept / Decline / Counter.
2. Accept asks for the Spark code, or for confirmation that the partnership ad is approved, and stamps
   `code_received_at`.
3. Preferences: a minimum price, brands to pre-approve (ShopMy's "Instant Spotlights" shows this matters), and brands
   to always decline.
4. Unclaimed creators: the operator outreach email links to `/creators/claim/<request-token>`, so each brand request
   brings a creator into Receipts.

**2.3 Weekly creator email [Code]**
Open requests, licences expiring soon, earnings this month, and new on-camera mentions this week (Pro re-scan).

**2.4 Unlicensed-reuse sample (research test 7) [Ops]**
Before building any alert: take 50 creators with recent unprompted mentions, and check those brands' accounts and the
Meta Ad Library and TikTok Creative Center for reposts. Only build the alert if reuse would fire at least monthly for an
active creator.

*Gate 2:* 70%+ of new requests answered in the inbox, with a median time to answer under 48 hours.

---

## Phase 3. Payments and renewals (weeks 6-9, only if Gate 1 passes)

**3.1 Brand pays upfront [Code]**
When a creator accepts, Unprompted creates a one-time Stripe Checkout session for the brand, reusing the Stripe setup in
`creator.py`. The webhook stamps `brand_paid_at`, and the code is released to the brand only after payment.

**3.2 Creator payouts [Code]**
Use Stripe Connect Express to onboard creators, and transfer the creator's share after the code is delivered, with a
10-15% application fee. Check Connect availability for the countries your creators are in, including the UAE and
Saudi Arabia, before building.

**3.3 Expiry and renewals [Code]**
1. Use `expires_at` to put a "licences ending this week" block in the brand's weekly email, with one-click renew.
2. Send the creator a reminder to issue a new code if the brand renews. Spark codes cannot be extended once they expire.

**3.4 Pricing [Code]**
1. Replace the flat 25% rule in `license_price` with the creator's own rate when one is set.
2. Price the brand subscription: the research's take-rate arithmetic says one $299/month brand seat equals 13-20 active
   micro-creator licences. So once the pilot shows repeat use, offer a weekly watch with licensing at about
   $99-299/month, month-to-month (lock-ins are what brands hate most in competitors), plus the per-licence cut.

---

## Phase 4. Roster seat for talent managers (discovery in weeks 1-4, pilot in weeks 5-10)

Goal: a second paying segment that needs the product every week, whatever happens to licensing.

**4.1 Discovery [Ops, weeks 1-4]**
Talk to 10 managers with 5-25 creators each. Ask how they find deals today, what they would want on a Monday, and
whether a tool that finds one extra deal a quarter is worth $99/month. Their buying rule: it must cost less than one
deal's commission.

**4.2 Roster watch [Code, week 5]**
1. Tables `rosters` (owner `user_id`, plan) and `roster_creators` (platform, handle, name).
2. A weekly Monday job: for each creator, fetch new videos (free for TikTok through `tiktok_public.py`; Oriane for
   Instagram), classify brands with the Receipts scan code, and diff against last week.
3. A Monday email per roster: for each creator, new brand mentions with receipts, which of those brands are paying
   creators now (the `brand_activity` cache), and a draft pitch (`creator.draft_pitch`).
4. Credits: 10 managers × 15 creators × 40 credits is about 6,000 credits a week on Instagram, well over the 600/day
   budget. Use the free TikTok reader where possible, and ask Oriane for a pilot quota before starting.

**4.3 Pilot and price [Ops, weeks 5-10]**
Six free weeks for 10 managers. In week 6, offer $99/month for up to 25 creators: a new entry in `PLANS` with a Stripe
price.

*Gate:* 1+ closed deal across the cohort that came from the Monday email, and 3+ managers commit at $99 or more.

---

## Phase 5. Seeding ROI add-on (weeks 8-11)

Brands gift creators every week and track who posted by hand. Showing posts they can't see, spoken but untagged, gives
the watch a reason to stay.

1. [Code] On the brand dashboard, a CSV upload of gifted handles with ship dates (from Shopify orders), stored per brand.
2. [Code] The weekly run checks those creators' videos since the ship date, and reports: posted X of Y, of which Z only
   said it on camera (invisible to tag tracking), with receipts and licence buttons.
3. [Ops] Pilot with 5 Shopify brands from phase 1.

*Gate (research test 4):* at least 2x more posters found than their tags and discount codes show. Vendors claim 3-4x.

---

## Phase 6. GCC agency pilot (evaluation from week 6, pilot weeks 8-12)

1. [Code] An Arabic precision check: `backend/eval_mentions.py` runs `server.classify` over a hand-labelled set of 100
   Arabic and Arabic-English videos (stored as a fixture) and reports precision and recall. Add brand spellings in Arabic
   script as variants (the dashboard already hints at this), and measure again.
2. [Ops] Confirm the current list of agencies accredited by the UAE Media Council on the Council's own site (the
   research's list came from search snippets), and contact 3-5 of them, plus Saudi agencies working under Mawthooq.
3. [Code, only if agencies ask] A "licensed creator" flag on roster creators, and a shortlist filter for it.
4. [Ops] Get legal advice before selling any compliance angle (an unpaid mention as a disclosure or licence flag).

*Gate (research test 6):* 80%+ precision on Arabic content, and one paid agency pilot.

---

## Phase 7. Receipts packaging test (weeks 9-12, needs phase 2)

1. [Code] Three arms on `/creators/`, assigned by cookie and recorded in `events`:
   - A: today's $29 Pro scan
   - B: free, with licence payouts (phase 2)
   - C: a weekly warm-lead feed ("brands you mention that are paying creators this week") at about $9/month, anchored to
     the $5-48 creators already pay for lead lists
2. [Ops] Drive about 300 visitors per arm through the outreach in `docs/LAUNCH.md`.

*Measure:* week-4 active rate and paid conversion per arm. Keep the winner and drop the others.

---

## What not to build

These come straight from the research's failure cases:
- A deal-management CRM for creators. 600 creators were asked and none bit.
- An open two-sided marketplace before one side pays (Sponsorgap, FameBit).
- Another YouTube sponsor database (SponsorTrace, GetSponsored and others already exist).
- Earned media value as the headline number. Finance teams don't trust it.

## Risks and dependencies

- **Oriane credits and terms:** the pilot fits the 600/day budget, but the roster seat and seeding report don't. Agree a
  pilot quota and per-credit price with Oriane before phase 4.
- **Hosting:** Render's free plan sleeps, so upgrade before phase 1, or reports will arrive late.
- **Email limits:** the Gmail relay allows about 100 recipients a day. That's fine for the pilot; move to Resend with a
  verified domain before growth.
- **Competition:** ShopMy Spotlights, YouTube Brand Partner Access and TikTok's back-catalog licensing all anchor creators
  at commission-only or $0. Our edge has to be upfront payment, consent, TikTok and Instagram spoken mentions, and Arabic.
- **Small creators convert unevenly as ads.** Track each licence's results with the brand. If micro-creator clips
  underperform, move the target to 50K-500K creators.
- **Legal and tax:** licence terms, holding money in Phase 1, UAE VAT, and the GCC licensing rules all need a
  professional check once before money moves.

## Weekly rhythm for the founder

- **Monday:** funnel review (phase 1.6), roster emails out (phase 4), and replies to every brand that viewed a licence
  page but didn't request.
- **Daily:** answer new licence requests within 24 hours, and chase creators at 24 and 48 hours.
- **Friday:** pay creators for codes delivered, send renewal reminders, and write one line on what changed this week.
