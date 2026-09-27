"""Self-check for the mention logic: python3 backend/test_server.py"""
from datetime import date

from server import check, classify, find_quote, mention_re, parse, score


def raw(**kw):
    base = {"profileHandle": "fan", "caption": "", "hashtags": [], "mentions": [], "coAuthors": [],
            "transcript": "", "transcriptChunks": [], "frames": [], "thumbnailMediaUrl": "thumb.webp"}
    return {**base, **kw}


def said(*texts):
    chunks = [{"startSeconds": i * 2.0, "endSeconds": i * 2.0 + 2, "text": t} for i, t in enumerate(texts)]
    return {"transcript": " ".join(texts), "transcriptChunks": chunks}


rx = mention_re(["Tim Hortons"])
assert rx.search("#TimHortons") and rx.search("Tim Horton's coffee") and rx.search("tim  hortons")
assert not rx.search("justimhortons")  # no match inside another word
assert mention_re(["ستاربكس"]).search("رحت بستاربكس")  # Arabic prefix glued on

assert classify(raw(profileHandle="timhortonsgcc", **said("Tim Hortons")), "Tim Hortons", [])[0] == "owned"
assert classify(raw(caption="#ad crispy", **said("Tim Hortons")), "Tim Hortons", [])[0] == "sponsored"
assert classify(raw(caption="#adventure", **said("Tim Hortons")), "Tim Hortons", [])[0] == "spoken"
assert classify(raw(caption="إعلان", **said("Tim Hortons")), "Tim Hortons", [])[0] == "sponsored"
assert classify(raw(mentions=[{"profileHandle": "timhortons"}], **said("Tim Hortons")), "Tim Hortons", [])[0] == "tagged"
assert classify(raw(**said("Timmies run")), "Tim Hortons", ["Timmies"])[:2] == ("spoken", 1)
assert classify(raw(**said("nothing here")), "Tim Hortons", [])[0] == "unverified"

# Name split across two transcript chunks: quote starts at the first chunk, context on both sides.
q = find_quote(raw(frames=[{"timestampSeconds": 2.1, "url": "f2"}, {"timestampSeconds": 9, "url": "f9"}],
                   **said("Morning.", "I love Tim", "Hortons coffee", "Bye.")), rx)
assert q["start"] == 2.0 and q["core"] == "I love Tim Hortons coffee" and q["frame"] == "f2"
assert q["text"][q["hit"][0]:q["hit"][1]] == "Tim Hortons" and q["text"].startswith("Morning.")

# Accuracy: a generic word from a multi-word brand is not a mention ("Al Ain water" vs "water").
assert classify(raw(**said("drink more water every day")), "Al Ain water", [])[0] == "unverified"
assert mention_re(["Tim Hortons"]).search("my Tim Horton order") and mention_re(["Starbucks"]).search("a Starbuck run")
assert not mention_re(["Tims"]).search("Tim is here")  # short words keep their s
filters = parse({"brand": "Al Ain water", "variants": "Masafi"})[2]["transcript"]
assert filters["includesExactly"]["values"] == ["Al Ain water"] and filters["includesFuzzy"]["values"] == ["Masafi"]

assert score(3, 1, 10**6, 8.0)["total"] == 100
assert score(0, 0, 0, None)["total"] == 0
# Score check: one clean brand mention, one video with profanity and alcohol.
today = date.today().isoformat() + "T10:00:00.000Z"
base = dict(platform="tiktok", profileFollowersCount=1000, viewsCount=300, engagementRatePerViews=8.0, publishedAt=today)
res = check([raw(id="1", platformId="1", caption="Tim Hortons run", **base, **said("so good")),
             raw(id="2", platformId="2", **base, **said("that was a fucking great beer")),
             raw(id="3", platformId="3", caption="#foodporn day", **base, **said("shooting a video today"))], "Tim Hortons", [], known=["old-1"])
assert res["safety"]["categories"] == {"Profanity": 1, "Alcohol & drugs": 1}, res["safety"]["categories"]
assert res["safety"]["score"] == 79 and res["safety"]["flagged"] == 1 and res["safety"]["flags"][0]["quote"]["start"] == 0.0
assert res["affinity"] == {"score": 67, "mentions": 2, "recent": 1}  # 1 recent + 1 known older mention
assert res["performance"]["score"] == 85 and res["performance"]["posts30d"] == 3
assert (res["total"], res["verdict"]) == (78, "Ready to pitch"), (res["total"], res["verdict"])
print("ok")
