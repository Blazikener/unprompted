"""Public TikTok fallback for creators Oriane hasn't indexed.

TikTok's embed page (tiktok.com/embed/@handle) server-renders the creator's latest ~10 videos; each video page
carries stats, hashtags, @mentions and, when TikTok generated them, auto-captions as WebVTT. Results are reshaped
to the Oriane content schema so the classifier, receipts and pitch code don't know the difference. Costs no
Oriane credits. Cover and caption URLs are signed and expire after a few days.
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib import error, request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
EMBED_URL = "https://www.tiktok.com/embed/@%s"
VIDEO_URL = "https://www.tiktok.com/@%s/video/%s"
STATE_RE = re.compile(r'<script id="(?:__FRONTITY_CONNECT_STATE__|__UNIVERSAL_DATA_FOR_REHYDRATION__)"'
                      r' type="application/json">(.*?)</script>', re.S)
CUE_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d+)\s+-->\s+(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d+)")
MAX_VIDEOS = 12
WORKERS = 5
TIMEOUT = 20


def get(url):
    req = request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def embedded_state(html):
    m = STATE_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def find_key(obj, key):
    """First dict anywhere in obj that has key."""
    if isinstance(obj, dict):
        if key in obj:
            return obj
        for v in obj.values():
            hit = find_key(v, key)
            if hit:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find_key(v, key)
            if hit:
                return hit
    return None


def log(handle, what):
    print("tiktok_public @%s: %s" % (handle, what), file=sys.stderr, flush=True)


def creator_page(handle):
    """(user info, latest video ids) from the embed page; (None, []) when TikTok shows nothing public."""
    try:
        html = get(EMBED_URL % handle)
    except (error.URLError, OSError, ValueError) as e:
        log(handle, "embed failed: %r" % e)
        return None, []
    page = find_key(embedded_state(html), "videoList")
    if not page:
        log(handle, "embed had no videoList (%d bytes, state=%s)" % (len(html), embedded_state(html) is not None))
        return None, []
    return page.get("userInfo") or {}, [v["id"] for v in page["videoList"] if v.get("id")]


def video_detail(handle, video_id):
    try:
        state = embedded_state(get(VIDEO_URL % (handle, video_id)))
        return state["__DEFAULT_SCOPE__"]["webapp.video-detail"]["itemInfo"]["itemStruct"]
    except (error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        log(handle, "video %s failed: %r" % (video_id, e))
        return None


def seconds(h, m, s, frac):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + float("0." + frac)


def vtt_chunks(text):
    """WebVTT cues -> Oriane-style transcriptChunks."""
    chunks = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        for i, ln in enumerate(lines):
            m = CUE_RE.match(ln)
            if m:
                said = re.sub(r"<[^>]+>", "", " ".join(lines[i + 1:])).strip()
                if said:
                    chunks.append({"startSeconds": seconds(*m.groups()[:4]), "endSeconds": seconds(*m.groups()[4:]),
                                   "text": said})
                break
    return chunks


def pick_subtitle(infos):
    """TikTok's own speech recognition first (English, then any), machine translation last."""
    def rank(s):
        return (s.get("Source") != "ASR", not str(s.get("LanguageCodeName", "")).startswith("eng"))
    usable = [s for s in infos or [] if s.get("Url") and str(s.get("Format", "")).lower() == "webvtt"]
    return min(usable, key=rank) if usable else None


def subtitles(item):
    """(transcriptChunks, language code) for one video, or ([], None)."""
    sub = pick_subtitle((item.get("video") or {}).get("subtitleInfos"))
    if not sub:
        return [], None
    try:
        return vtt_chunks(get(sub["Url"])), (sub.get("LanguageCodeName") or "")[:3] or None
    except (error.URLError, OSError):
        return [], None


def iso(ts):
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def as_oriane(handle, item, user, chunks, lang):
    stats = item.get("stats") or {}
    views = int(stats.get("playCount") or 0)
    likes, comments, shares = (int(stats.get(k) or 0) for k in ("diggCount", "commentCount", "shareCount"))
    author, astats = item.get("author") or {}, item.get("authorStats") or {}
    extra = item.get("textExtra") or []
    video = item.get("video") or {}
    cover = video.get("cover") or video.get("originCover") or video.get("dynamicCover")
    transcript = " ".join(c["text"] for c in chunks)
    return {
        "id": "tiktok-public-%s" % item["id"], "platform": "tiktok", "platformId": str(item["id"]), "profileHandle": handle,
        "profileDisplayName": author.get("nickname") or user.get("nickname"),
        "profilePictureUrl": author.get("avatarMedium") or user.get("avatarMedium"),
        "profileFollowersCount": astats.get("followerCount") or user.get("followerCount") or 0,
        "profileVerified": bool(author.get("verified", user.get("verified"))),
        "publishedAt": iso(item["createTime"]) if item.get("createTime") else None,
        "viewsCount": views, "likesCount": likes, "commentsCount": comments,
        "engagementRatePerViews": round((likes + comments + shares) / views * 100, 2) if views else None,
        "caption": item.get("desc") or "",
        "hashtags": [t["hashtagName"] for t in extra if t.get("hashtagName")],
        "mentions": [{"profileHandle": t["userUniqueId"]} for t in extra if t.get("userUniqueId")],
        "coAuthors": [], "transcript": transcript or None, "transcriptChunks": chunks, "transcriptLanguage": lang,
        "duration": video.get("duration"), "thumbnailMediaUrl": cover,
        "frames": [{"timestampSeconds": 0, "url": cover}] if cover else [],
        "source": "public",
    }


def fetch(handle, limit=MAX_VIDEOS):
    """Latest public videos for a TikTok handle in Oriane shape, or [] when nothing is reachable."""
    user, ids = creator_page(handle)
    if not ids:
        return []
    with ThreadPoolExecutor(WORKERS) as pool:
        items = [it for it in pool.map(lambda v: video_detail(handle, v), ids[:limit]) if it and it.get("id")]
        subs = list(pool.map(subtitles, items))
    log(handle, "%d/%d videos, %d with captions" % (len(items), len(ids[:limit]), sum(1 for c, _ in subs if c)))
    return [as_oriane(handle, it, user or {}, chunks, lang) for it, (chunks, lang) in zip(items, subs)]
