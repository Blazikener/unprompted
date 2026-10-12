"""Public TikTok fallback for creators Oriane hasn't indexed.

TikTok's embed page (tiktok.com/embed/@handle) server-renders the creator's latest ~10 videos; each video page
carries stats, hashtags, @mentions and, when TikTok generated them, auto-captions as WebVTT. Results are reshaped
to the Oriane content schema so the classifier, receipts and pitch code don't know the difference. Costs no
Oriane credits. Cover and caption URLs are signed and expire after a few days.
"""
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib import error, request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
EMBED_URL = "https://www.tiktok.com/embed/@%s"
VIDEO_URL = "https://www.tiktok.com/@%s/video/%s"
STATE_RE = re.compile(r'<script id="(?:__FRONTITY_CONNECT_STATE__|__UNIVERSAL_DATA_FOR_REHYDRATION__)"'
                      r' type="application/json">(.*?)</script>', re.S)
VIDEO_OBJECT_RE = re.compile(r'<script[^>]*id="VideoObject"[^>]*>(.*?)</script>', re.S)
HASHTAG_RE = re.compile(r"#(\w+)")
MENTION_RE = re.compile(r"@([\w.]{2,24})")
DURATION_RE = re.compile(r"^PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?$")
CUE_RE = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d+)\s+-->\s+(?:(\d+):)?(\d{1,2}):(\d{1,2})[.,](\d+)")
MAX_VIDEOS = 12
WORKERS = 5
TIMEOUT = 20
RELAY_TIKTOK_URL = re.compile(
    r"^https://www\.tiktok\.com/(embed/@[\w.]{1,40}|@[\w.]{1,40}/video/\d{1,25})$",
    re.ASCII,
)
DIRECT_FAILURE_TTL = 600
_direct_failure_until = 0.0
RELAY_RETRY_STATUSES = {403, 429, 500, 502, 503, 504}
RELAY_RETRY_DELAYS = (1.5, 3)


def _retryable_direct_error(exc):
    if isinstance(exc, error.HTTPError):
        return exc.code in (403, 429, 503)
    return isinstance(exc, (error.URLError, TimeoutError, OSError))


def _fetch_via_relay(url, relay_url, secret):
    req = request.Request(relay_url, method="POST", data=json.dumps({"secret": secret, "fetch": url}).encode(),
                          headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=TIMEOUT) as response:
            try:
                result = json.load(response)
            except (ValueError, TypeError) as exc:
                return None, None, repr(exc), False
    except (error.URLError, OSError) as exc:
        return None, getattr(exc, "code", None), repr(exc), True
    if not isinstance(result, dict):
        return None, None, "invalid relay response", False
    status = result.get("status")
    body = result.get("body")
    if result.get("ok") is True and status == 200 and isinstance(body, str) and body.strip():
        return body, status, None, False
    detail = result.get("error")
    if detail is None:
        if result.get("ok") is True and status != 200:
            detail = "relay returned status %s" % status
        elif result.get("ok") is True:
            detail = "relay returned an empty body"
        elif result.get("ok") is False:
            detail = "relay returned ok=false"
        else:
            detail = "invalid relay response"
    retryable = (
        result.get("ok") is True and status in RELAY_RETRY_STATUSES
        or result.get("ok") is False and detail != "URL not allowed"
    )
    return None, status, detail, retryable


def _handle_from_url(url):
    path = RELAY_TIKTOK_URL.fullmatch(url).group(1)
    return path.removeprefix("embed/@") if path.startswith("embed/@") else path[1:].split("/video/", 1)[0]


def _safe_relay_text(value, relay_url, secret):
    text = str(value).replace("\n", " ").replace("\r", " ")
    for sensitive in (relay_url, secret):
        if sensitive:
            text = text.replace(sensitive, "[redacted]")
    return text


def _log_relay_failure(url, status, detail, relay_url, secret):
    status_text = "None" if status is None else _safe_relay_text(status, relay_url, secret)
    error_text = _safe_relay_text(detail, relay_url, secret)[:120]
    log(_handle_from_url(url), "relay failed: status=%s error=%s" % (status_text, error_text))


def _relay_body(url, relay_url, secret):
    path = RELAY_TIKTOK_URL.fullmatch(url).group(1)
    attempts = 3 if path.startswith("embed/@") else 1
    for attempt in range(attempts):
        body, status, detail, retryable = _fetch_via_relay(url, relay_url, secret)
        if body is not None:
            _log_relay(url)
            return body
        _log_relay_failure(url, status, detail, relay_url, secret)
        if not retryable or attempt + 1 == attempts:
            break
        time.sleep(RELAY_RETRY_DELAYS[attempt])
    return None


def _log_relay(url):
    log(_handle_from_url(url), "via relay")


def get(url):
    global _direct_failure_until
    match = RELAY_TIKTOK_URL.fullmatch(url)
    relay_url = os.environ.get("MAIL_RELAY_URL")
    relay_secret = os.environ.get("MAIL_RELAY_SECRET")
    can_relay = bool(match and relay_url and relay_secret)
    relay_attempted = False
    if can_relay and time.monotonic() < _direct_failure_until:
        relay_attempted = True
        body = _relay_body(url, relay_url, relay_secret)
        if body is not None:
            return body
    req = request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    try:
        with request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except (error.URLError, OSError) as exc:
        if _retryable_direct_error(exc):
            _direct_failure_until = time.monotonic() + DIRECT_FAILURE_TTL
            if can_relay and not relay_attempted:
                body = _relay_body(url, relay_url, relay_secret)
                if body is not None:
                    return body
        raise


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


def creator_embed(handle):
    """(user info, latest video entries) from the embed page; (None, []) when nothing is public."""
    try:
        html = get(EMBED_URL % handle)
    except (error.URLError, OSError, ValueError) as e:
        log(handle, "embed failed: %r" % e)
        return None, []
    state = embedded_state(html)
    page = find_key(state, "videoList")
    if not page:
        log(handle, "embed had no videoList (%d bytes, state=%s)" % (len(html), state is not None))
        return None, []
    return page.get("userInfo") or {}, page.get("videoList") or []


def creator_page(handle):
    """(user info, latest video ids) from the embed page; (None, []) when TikTok shows nothing public."""
    user, entries = creator_embed(handle)
    return user, [entry["id"] for entry in entries if isinstance(entry, dict) and entry.get("id")]


def _caption_text_extra(desc):
    return ([{"hashtagName": tag} for tag in HASHTAG_RE.findall(desc)]
            + [{"userUniqueId": handle} for handle in MENTION_RE.findall(desc)])


def ld_item(html, video_id):
    match = VIDEO_OBJECT_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
        stats = {"playCount": 0, "diggCount": 0, "shareCount": 0, "commentCount": int(data.get("commentCount") or 0)}
        actions = {"WatchAction": "playCount", "LikeAction": "diggCount", "ShareAction": "shareCount"}
        for stat in data.get("interactionStatistic") or []:
            interaction = stat.get("interactionType") or {}
            action = interaction.get("@type") if isinstance(interaction, dict) else interaction
            field = actions.get(str(action).rsplit("/", 1)[-1])
            if field:
                stats[field] = int(stat.get("userInteractionCount") or 0)

        creator = data.get("creator") or {}
        upload_date = data.get("uploadDate")
        create_time = int(datetime.fromisoformat(upload_date.replace("Z", "+00:00")).timestamp()) if upload_date else None
        duration_match = DURATION_RE.fullmatch(data.get("duration") or "")
        duration = (int(float(duration_match.group(1) or 0) * 3600
                        + float(duration_match.group(2) or 0) * 60
                        + float(duration_match.group(3) or 0)) if duration_match else None)
        desc = data.get("name") or ""
        return {
            "id": video_id,
            "desc": desc,
            "createTime": create_time,
            "stats": stats,
            "author": {"uniqueId": creator.get("alternateName"), "nickname": creator.get("name")},
            "video": {"duration": duration},
            "textExtra": _caption_text_extra(desc),
            "captionOnly": True,
        }
    except (ValueError, TypeError, AttributeError):
        return None


def video_detail(handle, video_id):
    try:
        html = get(VIDEO_URL % (handle, video_id))
        state = embedded_state(html)
        scope = state.get("__DEFAULT_SCOPE__") if isinstance(state, dict) else None
        page = scope.get("webapp.video-detail") if isinstance(scope, dict) else None
        info = page.get("itemInfo") if isinstance(page, dict) else None
        item = info.get("itemStruct") if isinstance(info, dict) else None
        if isinstance(item, dict):
            return item
        item = ld_item(html, video_id)
        if item:
            log(handle, "video %s: caption only (json-ld)" % video_id)
            return item
        log(handle, "video %s: no item data in page" % video_id)
        return None
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
    if item.get("captionOnly"):
        return [], None
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
        "profilePictureUrl": author.get("avatarMedium") or user.get("avatarMedium") or user.get("avatarThumbUrl"),
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
        "captionOnly": bool(item.get("captionOnly")),
    }


def _embed_item(entry):
    video_id = entry["id"]
    try:
        create_time = int(video_id) >> 32
    except (TypeError, ValueError):
        create_time = None
    desc = entry.get("desc") or ""
    return {
        "id": video_id,
        "desc": desc,
        "createTime": create_time,
        "stats": {"playCount": entry.get("playCount") or 0},
        "author": {"uniqueId": entry.get("authorUniqueId")},
        "video": {"cover": entry.get("coverUrl") or entry.get("originCoverUrl")},
        "textExtra": _caption_text_extra(desc),
        "captionOnly": True,
    }


def fetch(handle, limit=MAX_VIDEOS):
    """Latest public videos for a TikTok handle in Oriane shape, or [] when nothing is reachable."""
    user, entries = creator_embed(handle)
    if not entries:
        time.sleep(1)  # one retry: TikTok occasionally serves the embed without its video list
        user, entries = creator_embed(handle)
    entries = [entry for entry in entries if isinstance(entry, dict) and entry.get("id")][:limit]
    if not entries:
        return []
    with ThreadPoolExecutor(WORKERS) as pool:
        details = list(pool.map(lambda entry: video_detail(handle, entry["id"]), entries))
        items = []
        for entry, item in zip(entries, details):
            if item is None:
                item = _embed_item(entry)
            else:
                video = item.get("video")
                if not isinstance(video, dict):
                    video = {}
                cover = entry.get("coverUrl") or entry.get("originCoverUrl")
                if cover:
                    video["cover"] = cover
                item["video"] = video
            if item.get("id"):
                items.append(item)
        subs = list(pool.map(subtitles, items))
    log(handle, "%d/%d videos, %d with captions" % (len(items), len(entries), sum(1 for c, _ in subs if c)))
    return [as_oriane(handle, it, user or {}, chunks, lang) for it, (chunks, lang) in zip(items, subs)]
