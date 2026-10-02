# Licence concierge: the 4-week playbook (Phase 1)

What this tests (research tests 1 and 2, see `docs/NEXT-STEPS.md`): **will brands pay upfront to run creators'
unprompted videos as ads, and will creators accept a pre-priced 30-day licence?** Everything below is done by hand.
The app shows prices, takes requests and tracks them; it does not charge anyone.

## Before day 1

- [ ] Render: `LICENSE_NOTIFY_EMAIL` set; weekly runs reliable (paid plan, or a daily cron calling `/api/digests/run`).
- [ ] `/admin/` opens with your operator token (`DIGEST_RUN_TOKEN`).
- [ ] Stripe account that can create Payment Links (Dashboard → Payment Links), and a way to pay creators (Wise, PayPal
      or bank transfer).
- [ ] The licence below reviewed once by a lawyer, and an accountant's answer on how to invoice it (as a brokered
      service, or on the creator's behalf) and on VAT where you invoice from.
- [ ] A sheet (or the shortlist's table) with columns: brand, contact, sent, replied, watching, requested, paid, renewed.

## Week 1: shortlist and first messages

1. Build the shortlist. Each brand costs one Oriane search (40 credits), and `--limit` keeps one run inside a day's
   budget; results are cached, so run it again each day until it says nothing is left:

   ```
   ORIANE_API_KEY=... DATABASE_URL=... APP_URL=https://receipts-thc8.onrender.com python3 backend/prospect_brands.py --limit 12
   ```

   Defaults: beauty, skincare and fashion brands from the dictionary, last 30 days, creators with 5K-500K followers,
   3+ unprompted mentions to make the list. Add brands outside the dictionary with `--brands "Kayali,Glow Recipe"`.
   The output (`brand-shortlist.md`) and cache name real creators: keep them out of git.
2. Pick 20 brands. Prefer brands that already run creator or paid social (they know what a Spark Ad is), and check that
   the three clips in each message really are about the brand.
3. Find the person: head of growth, creator or influencer lead, or paid social manager (LinkedIn, the brand's careers
   page, its agency).
4. Send the message under each brand in the shortlist, from your own account. Replace `[name]`; change nothing else
   unless a clip is wrong. One follow-up after 4 days, then stop.
5. When they say yes: open `/brands/`, search the brand with the same filters, press **Watch weekly**, and put their
   email in. They confirm from their inbox. Note it in the sheet.

Target: 20 brands contacted in week 1, 10+ watching by the end of week 2.

## Every day: answer requests within 24 hours

A request arrives by email (`LICENSE_NOTIFY_EMAIL`) and appears at the top of `/admin/`. Rows waiting over 24 hours
turn red.

1. **Contact the creator.** Press **Copy message to creator**, then send it to the email in their bio (best) or by DM from
   a personal account. Set the row to `contacted` and save.
2. **Follow up** at 24 and 48 hours if there's no answer. After 72 hours with no answer, set `declined` and note
   "no answer"; tell the brand and offer another clip.
3. **Counter-offer:** put the creator's number in *Creator $*. If the brand accepts it, put the final price in *Final $*.
   If not, set `declined`.
4. **Yes:** send the licence below with the numbers filled in, set `accepted`, then take payment (next section).

## Money

1. Create a Stripe Payment Link for the final price (one-time), named "Licence: @handle × Brand, N days", and send it
   to the brand. The code is released only after payment.
2. When Stripe shows it paid, tick **Brand paid**.
3. Ask the creator for the ad code (TikTok) or to approve the partnership request (Instagram), and pass it to the brand.
   Set the row `live`: that stamps the code as received, starts the window today and sets the end date.
4. Pay the creator their 85% within 3 days by Wise, PayPal or bank transfer, and tick **Creator paid**.
5. If the creator declines after the brand has paid, refund the brand in Stripe the same day.

## Renewals

Licences ending within 7 days are highlighted in `/admin/`, and the **Renewals due** count goes up. Press **Copy renewal
note to brand** and send it. A renewal needs a new ad code: TikTok codes can't be extended once they expire. A renewal is
the strongest signal in this test, so always ask.

## Every Monday

1. Run the funnel queries in `docs/LAUNCH.md` section 7 (reports sent, licence page views, requests, by status, paid
   dollars, median hours to answer).
2. Read the console's top row: *Accepted within 72h* and *Median hours to answer* are Gate 1's creator numbers;
   *Paid by brands* and renewals are its brand numbers.
3. Write three lines in the sheet: what moved, what stalled, what to change this week.

## Gate 1 (end of week 4)

Pass: **3+ brands paid for at least one licence, 1+ renewed, and 50%+ of contacted creators accepted within 72 hours.**
These are decision rules, not findings. `docs/NEXT-STEPS.md` says what to do for each way it can fail.

---

## Licence terms (one page, DRAFT: have a lawyer review before first use)

This is a starting template, not legal advice. Fill in the brackets for each deal.

> **Content licence**
>
> **Parties:** [Creator name] (@[handle], "Creator"); [Brand legal name] ("Brand"); [Your company] ("Unprompted"),
> who arranges this licence and handles payment.
>
> **Video:** [URL], posted by Creator on [date] (the "Video").
>
> **What Brand may do:** run the Video as a paid ad on [TikTok as a Spark Ad / Instagram and Facebook as a
> partnership ad], using the platform's own ad-authorisation tools, for [N] days starting on the day the ad code or
> partnership approval is delivered (the "Term"). No other use: not on Brand's website, in email, in print, in
> stores, or in other ads.
>
> **Edits:** captions, cropping and trimming only. No changes to what Creator says, no voice-over or dubbing, and no
> added claims presented as Creator's.
>
> **Fee:** Brand pays Unprompted $[final price] before the Term starts. Unprompted pays Creator $[85%] within 3 business
> days of the Term starting.
>
> **Ownership:** Creator keeps all rights in the Video. Nothing here is exclusive: Creator may work with any other brand,
> including competitors.
>
> **End of Term:** all paid use stops on day [N]. Brand may ask to renew at a new fee; renewing needs a new ad code.
>
> **Music and third-party content:** Brand is responsible for any commercial licence for music or other third-party
> material in the Video, and accepts that the platform may mute or limit it.
>
> **If the Video is removed:** if Creator deletes the Video during the Term, the ad stops and Brand receives a pro-rata
> refund for the unused days.
>
> **Disclosure:** the ad is labelled as paid by the platform. Creator is not asked to post anything new.
>
> **Governing law:** [jurisdiction].
>
> Agreed by Creator ______ Brand ______ Unprompted ______  Date ______

---

## Phase 2: the creator side (once it's live)

Creators now answer in the product instead of by DM. Your job shifts from relaying messages to moving money.

- **Creator links.** Every request has a private offer link (`/offer/<token>`). **Copy message to creator** in `/admin/`
  includes it. The creator accepts, counters (with the amount *they* want; the brand price shown in the console
  becomes that ÷ 0.85) or declines, and after accepting pastes the TikTok ad code or confirms the Instagram
  partnership approval. You're emailed each step; the row shows "via app" and the code.
- **Claimed handles.** Creators who sign in at `/creators/licenses` and verify a handle get every new offer by email
  (the console says "Handle claimed"). TikTok verifies itself from a code in the bio. Instagram claims appear at the top
  of `/admin/`: press **Verify** only after the code arrives by DM from that handle, or from the email in its bio.
- **Rules.** A creator's minimum, auto-yes and auto-decline lists answer new requests on the spot ("via auto"). An
  auto-accepted request still needs payment and the code, exactly like a manual yes.
- **Weekly summary.** The same cron that runs brand reports (`POST /api/digests/run`) emails each verified creator a
  summary when something is waiting, ending, or owed to them (never an empty email). Creators can turn it off.
- **Gate 2 (end of week 7):** *Answered in app* at 70% or more in the console, with a median under 48 hours.

### Unlicensed-reuse sample (research test 7, by hand, before building any alert)

1. Take 50 creators from the shortlist with unprompted mentions in the last 30 days.
2. For each, look up the mentioned brand on the Meta Ad Library (facebook.com/ads/library) and TikTok's Creative Center
   top ads, and scroll the brand's own TikTok and Instagram for the last 30 days.
3. Count reposts or ads that use the creator's footage without a disclosed partnership or credit.
4. Build the "a brand reused your video" alert only if it would fire at least monthly for an active creator. Record the
   count and examples in the sheet either way.

---

## Phase 3: money moves without you (payments, payouts, renewals)

Off until you switch it on; until then everything above stays manual. Expiry, renewal reminders and creator rates
work either way.

**Switching it on (Stripe test mode first):**
1. In Render, set `LICENSE_PAYMENTS=1`, with `STRIPE_SECRET_KEY` (start with `sk_test_…`) and `STRIPE_WEBHOOK_SECRET`.
   Set `STRIPE_PLATFORM_COUNTRY` to your Stripe account's country (e.g. `AE`): creators banking elsewhere are onboarded
   as payout-only recipients.
2. In Stripe, turn on Connect (Express accounts) and check which payout countries it allows for your account; the inbox
   offers `payments.CONNECT_COUNTRIES`.
3. On the existing webhook (`/api/creators/billing/webhook`), enable the events `checkout.session.completed` and
   `account.updated`.
4. Run one test licence end to end with Stripe's test card (4242 4242 4242 4242): brand pays → creator sends a code →
   it goes live → a test transfer appears in Stripe. Only then switch to live keys.

**What happens on its own now:**
- The creator accepts (or their rule does) → the brand's licence page shows **Pay $X** (Stripe Checkout).
- Paid + code received → the licence goes **live** by itself: the window starts, the brand gets the code by email and
  on its page, and the creator's 85% is transferred to their Stripe account (once they've set up payouts in their
  inbox; anything owed goes out the moment they finish).
- A counter-offer shows on the brand's page with **Accept / Decline**; accepting moves straight to payment.
- The creator declines after the brand paid → full refund, automatically.
- A week before the end, the brand gets a **Renew** email (one click: same window and price; the creator confirms and
  sends a new code). At the end the licence is marked `ended`.
- A creator's own 30-day rate (set in their inbox) replaces the rule-of-thumb price for their videos everywhere.

**What still needs you:** brands that paid by your Payment Link (tick **Brand paid**; the rest follows), creators
without Stripe payouts (pay by hand, tick **Creator paid**), transfers that fail (you're emailed), and refunds for a
video deleted mid-licence (pro-rata, by hand in Stripe).
