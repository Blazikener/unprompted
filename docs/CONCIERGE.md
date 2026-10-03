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
- [ ] Your own `/brands/` account on Pro: free accounts get 2 brand searches a week and week 1 needs about 20. Upgrade
      it, or set it in the database (`UPDATE users SET plan = 'pro' WHERE email = '<you>'`).
- [ ] Room for the pilot: `DIGEST_MAX_ACTIVE` (active watches site-wide, default 25) at 40 or more, and either
      `ORIANE_DAILY_BUDGET` above 600 or the 20 searches spread over two days (each search and each weekly run is 40
      credits, shared with the shortlist script and Receipts).

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
   "no answer". Setting `declined` emails the brand; reply to that thread to offer another clip.
3. **Counter-offer:** put the creator's number in *Creator $*. If the brand accepts it, put the final price in *Final $*.
   If not, set `declined`.
4. **Yes:** send the licence below with the numbers filled in, set `accepted` (the brand is emailed that the creator said
   yes), then take payment (next section).

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
note to brand** and send it. (Once Phase 3 is deployed the brand gets a renewal email automatically a week before the
end, with a Renew button on its licence page: skip the manual note then.) A renewal needs a new ad code: TikTok codes can't be extended once they expire. A renewal is
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
  includes it. The creator accepts, counters (with the amount *they* want; the console then shows "brand pays" as that
  ÷ 0.85, and with Phase 3 the brand can accept it on its own page) or declines, and after accepting pastes the TikTok ad code or confirms the Instagram
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
- The creator accepts (or their rule does) → the brand is emailed and its licence page shows **Pay $X** (Stripe
  Checkout). A counter or a no is emailed to the brand the same way.
- Paid + code received → the licence goes **live** by itself: the window starts, the brand gets the code by email and
  on its page, and the creator's 85% is transferred to their Stripe account (once they've set up payouts in their
  inbox; anything owed goes out the moment they finish).
- A counter-offer shows on the brand's page with **Accept / Decline**; accepting moves straight to payment.
- The creator declines after the brand paid → full refund, automatically.
- A week before the end, the brand gets a **Renew** email (one click: same window and price; the creator confirms and
  sends a new code). The renewal starts when the current licence ends, so no paid days overlap. At the end the licence
  is marked `ended`.
- A creator's own 30-day rate (set in their inbox) replaces the rule-of-thumb price for their videos: in reports, on
  licence pages and in the shortlist script.

**What still needs you:** brands that paid by your Payment Link (tick **Brand paid**; the rest follows), creators
without Stripe payouts (pay by hand, tick **Creator paid**), transfers that fail (you're emailed), and refunds for a
video deleted mid-licence (pro-rata, by hand in Stripe).

---

## Phase 4: the roster pilot for talent managers

A second paying segment that needs the product every week, whatever happens to licensing (research test 5).

**Discovery (weeks 1-4, alongside Phase 1).** Talk to 10 managers with 5-25 creators. Ask how they find deals today, what
they'd want on a Monday, and whether one extra deal a quarter is worth $99/month (their rule: a tool must cost less
than one deal's commission). Send the ones who say yes to `/creators/roster`.

**Running the pilot (weeks 5-10):**
1. The manager signs up at `/creators/roster` and presses **Ask for a pilot** (you're emailed).
2. In `/admin/` → **Roster pilots**, enter their account email and **Grant pilot** (6 weeks by default). They're emailed.
3. They add up to 25 creators and press **Run the first report now**: each creator's recent videos, the brands they
   mention unpaid with the receipt, whether each brand is paying creators now, and a pitch draft to copy.
4. Every Monday (UTC) after that, the digest cron (`POST /api/digests/run`) emails each active roster the brands
   mentioned in videos new since the last report. A quiet week still sends a one-line email.
5. When a pitch from a report turns into a deal, the manager presses **We closed a deal** under that brand.

**Credits.** TikTok creators are read from TikTok's public page first (free). Instagram creators cost one Oriane search
each (40 credits) per report, and each run looks up at most 3 brands' paid-creator activity fresh (cached a day). Ten
rosters of 15 Instagram creators is ~6,000 credits a week: ask Oriane for a pilot quota or raise `ORIANE_DAILY_BUDGET`.

**In week 6, offer the paid plan.** The **Keep it** button records a commitment (you're emailed) until `STRIPE_PRICE_ROSTER`
is set to a $99/month Stripe price; then it opens Stripe Checkout and puts the account on the `roster` plan.

**Gate 4:** in `/admin/` → Roster pilots, **Deals** ≥ 1 across the cohort and **Committed** = yes for 3+ managers.

---

## Phase 5: the seeding report pilot

Brands that gift product track who posted through tags and discount codes, which misses everyone who only says the
brand on camera. The seeding report finds those posts and puts a licence button next to them (research test 4).

**Who:** 5 Shopify brands from the Phase 1 shortlist that send product to creators every month and already have a
weekly watch. Ask each for last quarter's gifting list (handle, ship date) and how many posts their own tracking found.

**Running it:**
1. Open the brand's watch page (`/digest/<token>`, the **Manage** link in any report) → **Gifted creators**. Paste the
   list or pick a CSV: one creator per line with handle or profile URL, platform, ship date (YYYY-MM-DD) and `yes` if
   their own tracking caught the post. Lines it can't read are listed back with the reason.
2. TikTok creators are checked straight away from their public page (free; an upload never spends Oriane credits).
   Each weekly report then checks the rest: first the brand search the report already ran, then up to 15 TikTok
   creators' public pages, and at most 5 creators through Oriane (Instagram, or a TikTok page that can't be read): about
   200-400 credits a run at most. Creators not reached go first the next week.
3. A creator counts as posted from their first video that says, tags or discloses the brand after the ship date, within
   90 days. The weekly report gets a **Gifted creators** block (posted X of Y, how many only on camera, versus their own
   tracking) and the new posts with **License for ads**; a week with only gifted posts still sends.
4. `/admin/` → **Seeding reports** shows each brand's gifted, posted, on-camera-only and tracked counts.

**Gate 5:** for 3 of the 5 brands, posters found ≥ 2× what their own tracking had (the console's ×). Vendors claim 3-4×;
below 2× the add-on isn't worth selling separately.

---

## Phase 6: the Arabic check and GCC agencies

GCC agencies will only pay if the Arabic results are right. Measure that before pitching anyone (research test 6).

**The Arabic check (in `/admin/` → Arabic check):**
1. **Collect.** Each brand is one Oriane search for Arabic videos from the last year that say its Latin name or one of
   its Arabic spellings (40 credits); 10 of the results are kept at random. The default list mixes spellings that are
   also everyday words (Talabat طلبات, Noon نون, Careem كريم, Namshi نمشي, Shein شين) with plain ones. Ten brands is
   400 credits: do 5 a day to leave the weekly reports room under `ORIANE_DAILY_BUDGET`.
2. **Label** each video: **Yes** only if it's really about the brand (the company, its app, its product), not the
   everyday word, a place or a person with the same name. Keys: `y`, `n`, `s` to skip. Watch the video when the
   transcript isn't clear.
3. **Read the numbers.** Precision and recall, with the Latin name only and with the Arabic spellings on, overall and
   per brand. "Where it's wrong" lists each mistake: fix a wrong label there, or fix the matching (next step).
4. **Fix and re-measure.** A missed video usually means a missing spelling (add it to `brands.ARABIC`); a wrong find,
   a spelling that is an everyday word (add it to `brands.ARABIC_AMBIGUOUS` with context words in
   `brands.ARABIC_CONTEXT`). Download the labelled set and rerun offline: `python3 backend/eval_mentions.py
   arabic-check.jsonl`. Keep that file out of the repository: it holds creators' transcripts.

**Gate 6, part one:** 80%+ precision with the Arabic spellings on 100+ labelled videos (the console says "passed").

**Agencies (only after part one passes):**
1. Confirm the current list of agencies accredited by the UAE Media Council on the Council's own site (the research's
   list came from search snippets), and contact 3-5 of them, plus Saudi agencies working under Mawthooq.
2. Pitch the weekly watch in Arabic and English: spoken mentions, with the quote and the licence button.
3. Get legal advice before selling any compliance angle (an unpaid mention as a disclosure or licence flag).
4. Build the "licensed creator" flag on roster creators only if an agency asks for it.

**Gate 6, part two:** one paid agency pilot.

---

## Phase 7: the Receipts pricing test

Three ways to charge creators, shown side by side to new visitors of `/creators/` (each browser gets one, by cookie,
and an account keeps the one it signed up under):
- **(a) Pro, $29/month:** today's offer. Free shows the top 3 brands; Pro unlocks every brand, the sponsor check and pitch drafts.
- **(b) Free, paid by licences:** every brand and pitch draft free (no sponsor check); creators earn when brands license
  their videos, and we keep 15%.
- **(c) Weekly leads, $9/month:** free as in (a); the paid plan emails, every Monday, the brands the creator mentions
  that are paying creators that week, with the receipt, and unlocks what Pro does in the app.

**Before you drive traffic:**
1. In Stripe, create a $9/month price and set `STRIPE_PRICE_LEADS` on Render (until then (c)'s button records a
   reservation, like Pro's did). Check `STRIPE_PRICE_PRO` is set too.
2. Look at each offer as a visitor sees it: `/creators/?arm=a`, `?arm=b`, `?arm=c` (previews aren't counted).
3. The Weekly leads email goes out Mondays (UTC) from the same digest cron (`POST /api/digests/run` returns `feeds`).

**Running it:** drive about 300 visitors to each offer through the outreach in `docs/LAUNCH.md` (900 in all; the split
is random, so just send people to `/creators/`). `/admin/` → **Receipts pricing test** shows, per offer: visitors,
signups, scans, week-4 active (of accounts at least 28 days old), paid (a subscription, or a licence a brand paid for),
subscriptions, licence fees kept and checkout clicks.

**Reading it (about 4 weeks after the last visitor batch):** keep the offer with the best week-4 active rate and paid
conversion; when they disagree, prefer the one that brings creators back, since the licence money depends on them
being there. Then set `RECEIPTS_ARMS` on Render to the winner's letter: every visitor and account gets that offer
(accounts keep their history in the numbers). Paying subscribers on a dropped plan keep it until they cancel.
