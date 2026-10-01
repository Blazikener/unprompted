# Market validation: a creator-side product for Unprompted

Date: 2026-09-30. Desk research only (public reports, competitor pricing pages, brand-side hiring advice). No creator interviews or paid pilots yet; the gates at the end say what has to happen before this counts as validated demand.

## The question

Which single feature would creators pay for today, in a market that is real, still young, not saturated, and easy to charge for?

## What creators already pay for (proven willingness to pay)

Creators pay monthly for tools that promise **more brand deals**, priced at $15 to $59 per month, and the crowded segments prove the budget exists:

| Category | Examples and list prices | Saturation |
| --- | --- | --- |
| YouTube sponsor databases ("brands already paying creators like you") | GetSponsored Pro $29/mo (list $79), SponsorTrace $39-49/mo, MeetSponsors, ThoughtLeaders from $60/mo | Saturated for YouTube: 4+ entrants in ~12 months |
| Pitch automation and brand contact lists | Scout24 $29/mo, Pitchanite, Repped | Crowded, converging on the same feature set |
| Deal CRM, invoices, rate cards | SponsorKit $29-59/mo, Beacons, mediakit.bio $15/mo | Saturated, many free tiers |
| Contract review for brand deals | Black X (score first free), Repped | New, 2+ entrants, legal-adjacent |
| Rate transparency | FYPM (crowdsourced, rate calculator beta 2026), $25 benchmark report | Community-driven, hard to copy |

Demand signals behind that spend:

- Brand sponsorships are the primary income of 32% of creators earning $101k+ (Creator Spotlight 2025 monetization report, n=427). 94% of surveyed creators post brand partnerships at least monthly (Visa Monetized 2025 report).
- US creator ad spend is projected at $37B for 2025, growing 4x faster than media overall (IAB 2025). Brands work with 33% more micro-influencers year over year (Status 2025); UGC creator count grew 93% YoY (Collabstr 2025).
- 87% of TikTok and 76% of Instagram influencers are nano (1k-10k followers) (HypeAuditor 2025). These creators do their own sales and are the buyers of $29/mo tools.
- Goldman Sachs estimates ~67M creators globally in 2025 growing ~10% a year.

## The gap: proof of fandom on short-form video

Brand-side gatekeepers say the strongest thing a creator can put in a pitch is evidence they already use and talk about the product:

- Cohley (UGC marketplace, analysing brand acceptance of creator applications): "Say so if you already use the product. This was the single strongest thing we found." Applications that mentioned already owning or using the product were accepted "noticeably more often ... across almost every kind of brief."
- Forbes, "Why your cold pitch emails aren't getting you brand deals" (Mar 2026): brand managers want "proof the creator is a fan of the brand, evidence that they've organically shared the brand in the past"; creators "can even use these organic mentions as performance data."
- brandID (2026 YouTube sponsorship guide): "Brands you already use ... convert best because the endorsement is already true", with the pitch line "I already use [product] ... and mentioned it unpaid in [link], which did 31,000 views."
- HypeAuditor's outreach survey: for 39% of influencers only one or two brand inquiries a month become paid work; the creator's own outbound has to carry the rest.

Nobody serves this on TikTok and Instagram:

- YouTube-only: Resoneva scans a channel's own videos for brands it mentions, but for affiliate links, via the YouTube API.
- Brand- or agency-side only: Syncly ("find creators mentioning your brand without tagging"), Bisket (agency roster brand history), CreatorDB (brand sponsorship graph). These sell to the buyer, not the creator.
- TikTok sponsor intelligence for creators (fypp) is still a waitlist with no published pricing.

Unprompted already has the hard part: an Oriane transcript and frame index for Instagram and TikTok, a mention classifier that separates spoken, tagged and sponsored mentions, and the receipt UI (quote, timestamp, frame). Turning it toward the creator is a product change, not a research project.

## The product: Receipts (working name)

For a creator or their manager: paste a TikTok or Instagram handle.

1. **Brands you already talk about.** Every brand said on camera, tagged or hashtagged in the creator's recent videos, split into unpaid mentions and disclosed partnerships, each with the quote, timestamp, frame and view count.
2. **Which of them are paying creators right now.** For each brand, a live check of disclosed sponsored videos on TikTok and Instagram in the last 90 days: how many, which creator tiers, last seen. A brand that is both loved and buying is the shortlist.
3. **A pitch that quotes the creator's own words.** Drafted from the receipts: the line, the views, the brand's current creator activity, and a rate ask from a transparent CPM benchmark.

Monetization mirrors the proven price points: free scan showing the top brands, Pro at $29/mo for all brands, live sponsor checks, pitch drafts and weekly re-scans, Manager at $99/mo for a roster. Stripe Checkout, monthly, cancel anytime.

## Why this fits the brief

- **Real:** the underlying spend (brand deals) is the top revenue line for professional creators and growing 26% a year in the US.
- **New:** no creator-facing product does spoken-mention proof on short-form video; the closest analogues are YouTube-only or sold to brands.
- **Not saturated:** the YouTube sponsor-database segment has 4+ entrants; the TikTok/Instagram side has one waitlist.
- **Easily monetizable:** comparable tools already charge $29-49/mo with a "one deal pays for a year" pitch; the outcome (a better pitch, a booked deal) is direct and provable.

## Risks and what is not yet validated

1. **Willingness to pay for this specific product is inferred from adjacent tools, not measured.** Gate: 100 free scans with a paywall shown, target 5% start a checkout.
2. **Oriane API access.** The API is in private beta; a consumer SaaS needs a commercial agreement and per-query cost that fits a $29 plan. Gate: quote per 1,000 searches and a cache strategy (a scan is roughly 1 + N brand checks).
3. **Coverage.** Oriane indexes Instagram and TikTok; nano creators with few videos may return thin results. Mitigation: the scan still works from captions and @mentions; the free tier sets expectations.
4. **Brand detection precision.** The MVP uses a curated brand dictionary plus @mentions and hashtags, not an LLM; unknown brands are missed. Gate: track "brand missing" reports and grow the dictionary weekly.
5. **Copyability.** GetSponsored-style teams could add TikTok. The defensible part is the receipt, which needs transcript-level indexing they do not have.

## Sources

- Creator Spotlight, 2025 Monetization Report: https://www.creatorspotlight.com/p/monetization-report-2025
- Visa, Monetized: 2025 Creator Report (PDF): https://images.globalclient.visa.com/Web/InovantElqVisaCheckout/%7B0cadbc53-5daf-42f9-ab7c-8abe1faeb333%7D_VS0248i_Creators_Whitepaper_v7.1_251028_MA_Final_11032025_ACCESSIBLE_V2.pdf
- IAB, Creator Ad Spend & Strategy Report 2025: https://www.iab.com/wp-content/uploads/2025/11/IAB_Creator_Ad_Spend_and_Strategy_Report_2025.pdf
- HypeAuditor, State of Influencer Marketing 2025: https://hypeauditor.com/state-of-influencer-marketing-2025/
- Collabstr, 2025 Influencer Marketing Report: https://collabstr.com/2025-influencer-marketing-report
- Status, Micro-influencer benchmarks: https://brands.joinstatus.com/micro-influencer-marketing-benchmarks
- Goldman Sachs, Creator Economy framing (Mar 2025): https://creatorswithinfluence.com/wp-content/uploads/2025/04/Goldman-Sachs-Global-Investment-Research-Creator-Economy-Framing-Market-Opportunity-Download-Report-March-26-2025.pdf
- Cohley, How can I make my application stand out: https://cohley.freshdesk.com/support/solutions/articles/48001256904-how-can-i-make-my-application-stand-out-
- Forbes, Why your cold pitch emails aren't getting you brand deals: https://www.forbes.com/sites/kristenbousquet/2026/03/31/why-your-cold-pitch-emails-arent-getting-you-brand-deals/
- brandID, How to get sponsors on YouTube in 2026: https://brandid.app/blog/how-to-get-sponsors-on-youtube/
- HypeAuditor, Influencer Outreach Survey: https://blog.hypeauditor.com/influencer-outreach-survey/
- Competitor pricing: https://getsponsored.io/pricing, https://sponsortrace.com/pricing, https://www.mysponsorkit.com/, https://mediakit.bio/pricing, https://scout.creator24.ai/, https://blackx.app/creator-contract-review, https://www.fypm.vip/, https://fypp.app/, https://resoneva.com/, https://syncly.app/creator/creator-discovery, https://bisket.io/influencer-brand-matching
- Oriane API and enterprise: https://www.oriane.xyz/enterprise
