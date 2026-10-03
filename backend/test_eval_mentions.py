"""Arabic matching (server.term_pattern, classify, the creator scan) and the Arabic check (eval_mentions.py).
Mocked Oriane: zero credits.

DATABASE_URL=postgresql:///unprompted_test pytest backend/test_eval_mentions.py
"""
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql:///unprompted_test")
os.environ["DIGEST_RUN_TOKEN"] = "run-secret"

import psycopg  # noqa: E402
import pytest  # noqa: E402

import brands  # noqa: E402
import creator  # noqa: E402
import eval_mentions  # noqa: E402
import server  # noqa: E402
from server import ApiError, classify, mention_re  # noqa: E402


def raw(transcript="", caption="", handle="fan", vid="v1"):
    return {"id": vid, "platform": "tiktok", "platformId": "7300000000000000001", "profileHandle": handle, "caption": caption,
            "hashtags": [], "mentions": [], "coAuthors": [], "transcript": transcript, "transcriptLanguage": "ar",
            "transcriptChunks": [{"startSeconds": 1.0, "endSeconds": 6.0, "text": transcript}] if transcript else []}


@pytest.mark.parametrize("term, text, found", [
    ("ستاربكس", "رحت بستاربكس اليوم", True),          # a preposition glued on
    ("ستاربكس", "وستاربكس افضل", True),               # and "and"
    ("ستاربكس", "الستاربكس زحمة", True),              # and "the"
    ("ستاربكس", "قهوة من ستَاربِكس", True),           # short vowels written in
    ("ستاربكس", "ستاربكسات", False),                  # no ending inside a longer word
    ("نون", "هذا قانون جديد", False),                  # no start inside a word: law, not Noon
    ("نون", "النون", False),                           # short words don't take "the"
    ("المراعي", "حليب بالمراعي", True), ("المراعي", "للمراعي", True),
    ("أديداس", "اديداس", True), ("اديداس", "أديداس", True),   # alef with or without hamza
    ("هدى بيوتي", "هدى بيوتى", True),                 # ى and ي
    ("كوكا كولا", "كوكاكولا", True),                   # spacing
    ("كيا", "سافرت تركيا", False),                     # Kia isn't in Turkey
    ("Tim Hortons", "my Tim Horton order", True), ("Nike", "afternike", False),   # Latin terms as before
])
def test_arabic_matching(term, text, found):
    assert bool(mention_re([term]).search(text)) == found


def test_everyday_spellings_need_context():
    assert classify(raw("عندي طلبات كثيرة في الشغل"), "Talabat", ["طلبات"])[0] == "unverified"         # "orders"
    kind, hits, q = classify(raw("طلبت عشاء من طلبات والتوصيل كان سريع"), "Talabat", ["طلبات"])
    assert kind == "spoken" and hits == 1 and q["text"].startswith("طلبت")
    assert classify(raw("كريم مرطب للبشرة"), "Careem", ["كريم"])[0] == "unverified"                    # "cream"
    assert classify(raw("ركبت كريم والكابتن كان محترم"), "Careem", ["كريم"])[0] == "spoken"
    assert classify(raw("ركبت كريم والكابتن كان محترم"), "Careem", [])[0] == "unverified"              # no spelling given
    assert classify(raw("", caption="طلبات اليوم كثيرة"), "Talabat", ["طلبات"])[0] == "unverified"
    assert classify(raw("", caption="عشاء من #طلبات توصيل سريع"), "Talabat", ["طلبات"])[0] == "tagged"
    # Someone else's disclosed ad isn't a sponsored post for this brand; this brand's is.
    assert classify(raw("أحلى قهوة", caption="#اعلان #نسكافيه"), "Starbucks", ["ستاربكس"])[0] == "unverified"
    assert classify(raw("قهوة ستاربكس", caption="#اعلان"), "Starbucks", ["ستاربكس"])[0] == "sponsored"


def test_creator_scan_reads_arabic():
    def names(transcript, caption=""):
        return {b["name"]: kind for b, kind, _ in creator.brand_mentions(raw(transcript, caption))}
    assert names("رحت ستاربكس الصبح وبعدين نايك")["Starbucks"] == "spoken"
    assert names("رحت ستاربكس الصبح وبعدين نايك")["Nike"] == "spoken"
    assert "Talabat" in names("طلبت من طلبات والتوصيل سريع") and "Talabat" not in names("عندي طلبات كثيرة")
    assert "Careem" not in names("كريم مرطب للبشرة الجافة") and "Noon" not in names("هذا قانون جديد")
    assert "Noon" in names("اشتريت سماعة من نون") and "Noon" not in names("نون")      # an ambiguous brand needs context in Arabic too


def test_dictionary_spellings_are_consistent():
    names = {n for n, *_ in brands.BRANDS}
    assert set(brands.ARABIC) <= names and set(brands.ARABIC_CONTEXT) <= names
    for n, spellings in brands.ARABIC.items():
        needs_context = n in brands.AMBIGUOUS or any(t in brands.ARABIC_AMBIGUOUS for t in spellings)
        assert not needs_context or brands.ARABIC_CONTEXT.get(n), n
    assert brands.ARABIC_AMBIGUOUS <= {t for ts in brands.ARABIC.values() for t in ts}
    hints = brands.arabic_spellings()
    assert hints["starbucks"]["spellings"] == ["ستاربكس", "ستار بكس"] and hints["talabat"]["everyday"] == ["طلبات"]


# ---------------------------------------------------------------- the Arabic check

LABELLED = [  # (brand, transcript, caption, label): Latin-only and Arabic-spelling results worked out by hand
    ("Talabat", "طلبت عشاء من طلبات والتوصيل كان سريع", "", "yes"),   # Latin misses it; Arabic finds it
    ("Talabat", "عندي طلبات كثيرة في الشغل اليوم", "", "no"),          # "orders": neither counts it
    ("Talabat", "talabat delivery is so fast", "", "yes"),               # both
    ("Starbucks", "رحت ستاربكس الصبح", "", "yes"),                       # Arabic only
    ("Starbucks", "قهوة الصبح", "#اعلان #نسكافيه", "no"),                # someone else's ad: neither
    ("Noon", "هذا قانون جديد في البلد", "", "no"),                       # neither
    ("Noon", "طلبت الجوال من نون والتوصيل يوم واحد", "", "yes"),        # Arabic only
    ("Careem", "كريم مرطب للبشرة الجافة", "", "no"),                     # neither
    ("Careem", "careem captain was late", "", "yes"),                    # both
    ("Almarai", "حليب المراعي كل يوم", "", "yes"),                       # Arabic only
    ("Noon", "we meet at noon tomorrow", "", "no"),                      # both count it: the one wrong find
    ("Shein", "شي شين والله", "", "no"),                                  # "ugly": neither
]


def rows():
    return [{"id": i, "brand": b, "label": lab, "raw": raw(t, c, vid="e%d" % i)} for i, (b, t, c, lab) in enumerate(LABELLED, 1)]


def test_precision_and_recall_both_ways():
    latin, arabic = eval_mentions.evaluate(rows(), False), eval_mentions.evaluate(rows(), True)
    assert (latin["tp"], latin["fp"], latin["fn"], latin["tn"]) == (2, 1, 4, 5)
    assert (latin["precision"], latin["recall"]) == (0.667, 0.333)
    assert (arabic["tp"], arabic["fp"], arabic["fn"], arabic["tn"]) == (6, 1, 0, 5)
    assert (arabic["precision"], arabic["recall"]) == (0.857, 1.0)
    assert [(e["brand"], e["error"], e["kind"]) for e in arabic["errors"]] == [("Noon", "fp", "spoken")]
    noon = next(b for b in arabic["brands"] if b["brand"] == "Noon")
    assert (noon["tp"], noon["fp"], noon["precision"]) == (1, 1, 0.5)


def test_offline_run(tmp_path, capsys):
    path = tmp_path / "arabic-check.jsonl"
    path.write_text("".join(json.dumps({"brand": r["brand"], "label": r["label"], "raw": r["raw"]}) + "\n" for r in rows()), encoding="utf-8")
    assert eval_mentions.main(str(path)) == 1                                  # 12 videos: too few for the gate
    out = capsys.readouterr().out
    assert "Latin name only        12 labelled   precision   67%   recall   33%" in out
    assert "With Arabic spellings  12 labelled   precision   86%   recall  100%" in out and "Gate 6" in out


@pytest.fixture
def clean():
    server.init_db()
    with psycopg.connect(server.DB_URL) as db:
        db.execute("DELETE FROM eval_items")


def results(brand, n):
    return [{**raw("طلبت من %s فيديو رقم %d والتوصيل سريع" % (brand, i), vid="%s-%d" % (brand, i)), "viewsCount": 100 + i} for i in range(n)]


def test_collect_samples_each_brand_once_and_stops_at_the_cap(clean, monkeypatch):
    calls = []

    def oriane(filters, limit=100, **_):
        calls.append(filters)
        if len(calls) > 1:
            raise ApiError(503, "Live search is paused for today to protect our data credits.")
        return {"data": {"results": results("طلبات", 15)}}
    monkeypatch.setattr(server, "oriane", oriane)
    out = eval_mentions.collect(["Talabat"], per_brand=5)
    assert out == {"collected": {"Talabat": 5}, "skipped": [], "stopped": None}
    assert calls[0]["transcriptLanguage"] == {"includes": ["ar"]} and set(calls[0]["transcript"]["includesFuzzy"]["values"]) == {"Talabat", "طلبات"}
    out = eval_mentions.collect(["Talabat", "Noon", "Careem"], per_brand=5)
    assert out["skipped"] == ["Talabat"] and out["collected"] == {} and out["stopped"].startswith("Live search is paused")
    report = eval_mentions.report()
    assert (report["collected"], report["labelled"], len(report["queue"])) == (5, 0, 5)
    item = report["queue"][0]
    assert item["snippets"] and item["snippets"][0]["text"][slice(*item["snippets"][0]["hit"])] == "طلبات"


@pytest.fixture(scope="module")
def base():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % srv.server_port
    srv.shutdown()


def call(base, method, path, body=None, token="run-secret"):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.load(res)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_console_routes(clean, base, monkeypatch):
    monkeypatch.setattr(server, "oriane", lambda filters, limit=100, **_: {"data": {"results": results("نون", 4)}})
    assert call(base, "GET", "/api/admin/eval", token=None)[0] == 401
    assert call(base, "POST", "/api/admin/eval/collect", {"perBrand": 0})[0] == 400
    status, out = call(base, "POST", "/api/admin/eval/collect", {"brands": ["Noon"], "perBrand": 3})
    assert status == 200 and out["collected"] == {"Noon": 3} and out["report"]["collected"] == 3
    first = out["report"]["queue"][0]["id"]
    assert call(base, "POST", "/api/admin/eval/%d" % first, {"label": "maybe"})[0] == 400
    assert call(base, "POST", "/api/admin/eval/999999", {"label": "yes"})[0] == 404
    assert call(base, "POST", "/api/admin/eval/%d" % first, {"label": "yes"}) == (200, {"id": first, "label": "yes"})
    status, report = call(base, "GET", "/api/admin/eval")
    assert status == 200 and report["labelled"] == 1 and report["arabic"]["tp"] == 1 and report["latin"]["fn"] == 1
    assert report["gate"] == {"precision": 0.8, "n": 100, "passed": False}
    status, export = call(base, "GET", "/api/admin/eval/export")
    assert status == 200 and [(x["brand"], x["label"]) for x in export["items"]] == [("Noon", "yes")]
    status, spellings = call(base, "GET", "/api/brands/arabic", token=None)
    assert status == 200 and spellings["brands"]["noon"]["everyday"] == ["نون"]
