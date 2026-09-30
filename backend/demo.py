"""Demo data for the creator scan when ORIANE_API_KEY is missing (or DEMO_MODE=1).

Two fixture creators in Oriane's result shape, plus deterministic sponsor-activity fixtures, so the whole
Receipts flow (scan, receipts, paywall, sponsor check, pitch) can be clicked through without a key. Every
payload carries source="demo" and the UI labels it.
"""
import hashlib
import os
from datetime import date, timedelta
from urllib.parse import quote

from server import ApiError

HANDLES = {"maya.eats", "sami.lifts"}


def active():
    return os.environ.get("DEMO_MODE") == "1" or not os.environ.get("ORIANE_API_KEY")


def frame(label, hue):
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="hsl(%d 60%% 30%%)"/><stop offset="1" stop-color="hsl(%d 70%% 12%%)"/></linearGradient></defs>'
           '<rect width="360" height="640" fill="url(#g)"/>'
           '<text x="24" y="600" font-family="sans-serif" font-size="26" fill="#fff" opacity=".85">%s</text></svg>'
           % (hue, (hue + 40) % 360, label))
    return "data:image/svg+xml;charset=utf-8," + quote(svg)


# (days_ago, views, er%, caption, hashtags, @mentions, transcript sentences)
MAYA = [
    (3, 412000, 9.1, "iced capp season is officially open", ["dubaifood", "coffee"], [],
     ["okay it is 42 degrees outside so you know where I'm going.", "Tim Hortons iced capp, extra large, nobody talk to me.",
      "honestly the Timmies here hits different than the Toronto ones, fight me.", "rating this a nine out of ten."]),
    (6, 88000, 6.2, "grocery haul but make it aesthetic", ["grocery", "haul"], ["spinneysuae"],
     ["quick Spinneys run before the week starts.", "Al Ain water, the big pack, because Dubai.", "Almarai laban obviously.",
      "and Puck cream cheese for the bagels."]),
    (9, 1250000, 11.4, "the 2am shawarma review nobody asked for", ["shawarma", "dubai", "latenight"], [],
     ["it is 2am and Talabat said forty minutes, it took twelve.", "this shawarma is unreal.",
      "I also got a Vimto because I'm a child.", "ten out of ten, goodnight."]),
    (12, 64000, 5.8, "#ad my go-to Yellow Friday picks on noon", ["ad", "noon", "yellowfriday"], ["noon"],
     ["this video is sponsored by noon.", "these are my top five kitchen picks from the Yellow Friday sale.",
      "the Ninja Creami is finally in stock, and the Philips airfryer is half price."]),
    (15, 230000, 8.4, "morning routine: dubai edition", ["morningroutine", "grwm"], [],
     ["first things first, Nespresso, two capsules, no debate.", "then I do my skin: CeraVe cleanser, Beauty of Joseon sunscreen.",
      "then I drive to work with a Tim Hortons because the Nespresso was apparently not enough."]),
    (18, 155000, 7.7, "trying every iced coffee in Dubai Marina pt 1", ["icedcoffee", "dubaimarina"], [],
     ["stop one, Starbucks, iced shaken espresso, seven out of ten.", "stop two, Costa, the iced latte was fine.",
      "stop three, a tiny place I won't name because it was bad."]),
    (21, 97000, 6.9, "what I order at Shake Shack every single time", ["shakeshack", "burger"], ["shakeshack"],
     ["Shake Shack order: shackburger, cheese fries, and the black and white shake.", "the Dubai menu has a date shake, get it."]),
    (24, 540000, 10.2, "airport food but it's actually good??", ["dxb", "travel"], [],
     ["flying Emirates to London and the lounge food is genuinely good.", "got a Pret at Heathrow anyway because tradition."]),
    (27, 76000, 6.1, "rating GCC chocolate", ["chocolate", "gcc"], [],
     ["Galaxy is the best chocolate bar, this is not up for discussion.", "Kinder Bueno second.", "Cadbury Dairy Milk is third."]),
    (30, 188000, 8.8, "the Timmies drive-thru hack", ["timhortons", "coffee", "dubai"], ["timhortonsgcc"],
     ["okay Tim Hortons hack: ask for the iced capp with cold brew instead of the base.", "you're welcome."]),
    (34, 45000, 5.2, "what's in my bag: food creator edition", ["whatsinmybag"], [],
     ["I always have an Owala bottle.", "Anker power bank because filming kills my iPhone.", "and gum."]),
    (38, 320000, 9.6, "paid partnership with Careem: the Careem Plus thing explained", ["careemplus", "dubai"], ["careem"],
     ["paid partnership with Careem, but genuinely I have used Careem Plus for a year.", "free delivery on food orders is the main thing."]),
    (42, 110000, 7.0, "a very honest Deliveroo vs Talabat review", ["deliveroo", "talabat", "dubai"], [],
     ["Deliveroo has the better restaurants, Talabat has the better deals.", "I use Talabat more, I'm not going to lie."]),
    (46, 68000, 6.4, "we tried the new McDonald's McArabia", ["mcdonalds", "dubai"], [],
     ["the McArabia is back at McDonald's and I have opinions.", "grilled chicken, seven out of ten."]),
    (50, 92000, 7.3, "my Dyson airwrap made my morning 20 minutes shorter", ["airwrap", "hair"], [],
     ["not a hair channel but the Dyson Airwrap changed my life.", "no this is not sponsored, I wish."]),
    (55, 201000, 8.1, "night market food tour", ["dubai", "foodtour"], [],
     ["karak tea, then a Vimto slushie, then regret.", "I ended the night with a Tim Hortons donut, which is on brand."]),
    (60, 57000, 5.5, "the Kibsons box unboxing", ["kibsons", "grocery"], ["kibsons"],
     ["Kibsons box for the week, the mangoes are insane right now.", "Almarai yoghurt, Al Ain water, done."]),
    (66, 74000, 6.6, "café hopping in Jumeirah", ["dubaicafe", "jumeirah"], [],
     ["another Starbucks? yes, another Starbucks, the pistachio latte is back."]),
    (72, 139000, 7.9, "cooking with Nutella: three recipes", ["nutella", "recipe"], [],
     ["Nutella stuffed dates, Nutella karak, and Nutella on Lurpak toast which is elite."]),
    (80, 61000, 5.9, "trying Costa's new summer menu", ["costa", "coffee"], ["costacoffee"],
     ["Costa Coffee summer menu: the mango cooler is good, the rest is fine."]),
    (88, 330000, 9.9, "what 100 dirhams gets you at Carrefour vs Lulu", ["carrefour", "lulu", "budget"], [],
     ["Carrefour won on snacks, Lulu won on fruit.", "I bought Pringles at both, for science."]),
    (95, 44000, 5.0, "study with me: exam week", ["studywithme"], [],
     ["Notion open, Spotify on, iced Tim Hortons in hand."]),
]

SAMI = [
    (2, 260000, 8.9, "push day in 60 seconds", ["gym", "pushday"], [],
     ["push day, let's go.", "Myprotein clear whey after, the peach flavour is the only good one.",
      "Owala bottle because I'm hydrated and annoying."]),
    (5, 91000, 7.4, "#ad Whoop 4.0 after 90 days", ["ad", "whoop", "recovery"], ["whoop"],
     ["this is sponsored by Whoop.", "ninety days of recovery data, here's what actually changed."]),
    (8, 720000, 12.1, "the gym shoe debate is over", ["gymshoes", "lifting"], [],
     ["Nike Metcons for lifting, Hokas for the treadmill, that's it.", "stop squatting in running shoes."]),
    (12, 130000, 7.8, "what I eat in a day, Dubai edition", ["whatieatinaday", "dubai"], [],
     ["Almarai skyr in the morning.", "Talabat for lunch because I'm lazy, usually a poke bowl.", "Celsius pre-workout, the orange one."]),
    (16, 55000, 6.0, "gym bag essentials", ["gymbag"], [],
     ["Gymshark shorts, three pairs.", "Beats Fit Pro, they don't fall out.", "Anker power bank."]),
    (20, 340000, 10.5, "trying Fitness First's new club", ["fitnessfirst", "dubai"], ["fitnessfirstme"],
     ["Fitness First just opened in Business Bay and honestly it's stacked.", "no this isn't paid, I pay for my membership."]),
    (25, 88000, 6.7, "supplements I actually buy", ["supplements"], [],
     ["Optimum Nutrition gold standard, boring but it works.", "AG1, I know, I know, but I like it.", "Myprotein creatine."]),
    (30, 47000, 5.4, "morning walk and coffee", ["walk", "dubai"], [],
     ["Starbucks cold brew, walk on the Kite Beach track, done before eight."]),
    (36, 210000, 9.2, "leg day with my brother", ["legday", "gym"], [],
     ["Gymshark matching sets, this was not planned.", "Celsius before, Myprotein after."]),
    (44, 63000, 6.3, "the Garmin vs Apple Watch question", ["garmin", "applewatch"], [],
     ["Garmin for running, Apple Watch for everything else.", "I wear the Garmin more."]),
    (52, 150000, 8.0, "paid partnership with Gymshark: the new lifting club drop", ["gymshark", "paidpartnership"], ["gymshark"],
     ["paid partnership with Gymshark, but you know I've worn this stuff for years."]),
    (60, 72000, 6.5, "meal prep sunday", ["mealprep"], [],
     ["Carrefour run, chicken, rice, Almarai yoghurt.", "Ninja air fryer does all the work."]),
]

CREATORS = {
    "maya.eats": {"name": "Maya Haddad", "followers": 184000, "verified": False, "hue": 25, "videos": MAYA},
    "sami.lifts": {"name": "Sami Al Rashid", "followers": 96000, "verified": False, "hue": 210, "videos": SAMI},
}


def creator_videos(platform, handle):
    """Fixture videos for one demo creator, shaped like Oriane results."""
    spec = CREATORS.get(handle.lower())
    if not spec:
        raise ApiError(404, "Demo mode (no ORIANE_API_KEY): try @maya.eats or @sami.lifts.")
    out = []
    for i, (days, views, er, caption, tags, mentions, sentences) in enumerate(spec["videos"]):
        vid = "demo-%s-%s-%02d" % (platform, handle, i)
        pid = str(7300000000000000000 + i * 1234567) if platform == "tiktok" else "C%s%02d" % (handle[:3].upper(), i)
        t, chunks = 0.0, []
        for s in sentences:
            chunks.append({"startSeconds": round(t, 1), "endSeconds": round(t + 2.4 + len(s) / 18, 1), "text": s})
            t += 2.4 + len(s) / 18
        out.append({
            "id": vid, "platform": platform, "platformId": pid, "profileHandle": handle, "profileDisplayName": spec["name"],
            "profilePictureUrl": frame(spec["name"][0], spec["hue"]), "profileFollowersCount": spec["followers"],
            "profileVerified": spec["verified"], "publishedAt": (date.today() - timedelta(days=days)).isoformat() + "T12:00:00Z",
            "viewsCount": views, "likesCount": round(views * er / 100 * 0.9), "commentsCount": round(views * er / 100 * 0.1),
            "engagementRatePerViews": er, "caption": caption, "hashtags": tags, "mentions": [{"profileHandle": m} for m in mentions],
            "coAuthors": [], "transcript": " ".join(sentences), "transcriptChunks": chunks, "transcriptLanguage": "en",
            "duration": round(t, 1), "thumbnailMediaUrl": frame(caption[:28], (spec["hue"] + i * 17) % 360),
            "frames": [{"timestampSeconds": c["startSeconds"], "url": frame(caption[:28], (spec["hue"] + i * 17 + k * 9) % 360)}
                       for k, c in enumerate(chunks)],
        })
    return out


def activity(brand, platform):
    """Deterministic fake sponsor activity for a brand: same input, same numbers."""
    h = int(hashlib.sha256(("%s|%s" % (brand, platform)).encode()).hexdigest(), 16)
    sponsored = [0, 0, 1, 2, 3, 4, 6, 8, 11, 14, 19, 27][h % 12]
    creators = max(0, sponsored - h % 3) if sponsored else 0
    names = ["lina.cooks", "omar.reviews", "the.dubai.diaries", "fitwithnour", "abdullah.tech", "hana.glow", "yousef.eats", "sara.travels"]
    followers = [4200, 18000, 52000, 130000, 410000, 27000, 8600, 760000]
    examples = []
    for k in range(min(3, sponsored)):
        j = (h >> (k * 5)) % len(names)
        examples.append({"handle": names[j], "followers": followers[j], "views": followers[j] * (2 + (h >> k) % 5) // 2,
                         "publishedAt": (date.today() - timedelta(days=3 + (h >> (k * 3)) % 60)).isoformat(), "url": "#demo",
                         "text": "#ad ... %s ... " % brand})
    tiers = {"nano": 0, "micro": 0, "mid": 0, "macro": 0}
    for k in range(creators):
        tiers[["nano", "micro", "micro", "mid", "macro"][(h >> k) % 5]] += 1
    return {"windowDays": 90, "videosSeen": sponsored * 4 + h % 40, "sponsored": sponsored, "creators": creators, "tiers": tiers,
            "lastPaid": (date.today() - timedelta(days=2 + h % 45)).isoformat() if sponsored else None, "examples": examples}
