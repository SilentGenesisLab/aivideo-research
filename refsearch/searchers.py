"""第 2 步：视频平台检索。统一输出 Video 字典。

- YouTube：优先 YouTube Data API（配了 YOUTUBE_API_KEY），失败回退 yt-dlp 的 ytsearch
- Google 广告透明度中心：官方前端 RPC，视频创意里的 YouTube id 直接可下载
- Meta 广告资料库：Graph API ads_archive（需要身份验证过的 access token）
- B 站：官方搜索接口，需 buvid3 + bili_ticket cookie 和 WBI 签名，否则触发风控
"""
import hashlib
import hmac
import html
import json
import random
import re
import threading
import time
import urllib.parse

import requests

from . import config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


def _proxies(platform: str) -> dict | None:
    """只有登记过的平台才走出口代理：抖音/小红书/优酷在国内直连更快。"""
    if platform not in config.PROXY_PLATFORMS:
        return None
    p = config.proxy_for(platform)
    return {"http": p, "https": p} if p else None



def _video(platform, vid, url, title, channel="", duration=None, views=None,
           thumbnail="", description="", published=""):
    return {
        "platform": platform, "id": vid, "url": url, "title": title,
        "channel": channel, "duration": duration, "views": views,
        "thumbnail": thumbnail, "description": (description or "")[:300],
        "published": published,
    }


# ---------------- YouTube ----------------
# Data API 配额按单位计费：search.list = 100，videos.list = 1（每次最多 50 个 id）。
# 默认 10000 单位/天 ≈ 100 次搜索，所以结果要缓存，且当天用完就回退 yt-dlp。
_YT_QUOTA_FILE = config.RUNS_DIR / "_yt_quota.json"
_YT_CACHE_DIR = config.RUNS_DIR / "_yt_cache"
_yt_lock = threading.Lock()


def _yt_cache_key(query: str, limit: int) -> str:
    return hashlib.sha1(f"{query}|{limit}".encode()).hexdigest()


def _yt_cache_get(query: str, limit: int) -> list[dict] | None:
    p = _YT_CACHE_DIR / f"{_yt_cache_key(query, limit)}.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - d.get("t", 0) > config.YOUTUBE_CACHE_TTL:
        return None
    return d.get("v")


def _yt_cache_put(query: str, limit: int, videos: list[dict]) -> None:
    try:
        _YT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _YT_CACHE_DIR / f"{_yt_cache_key(query, limit)}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"t": time.time(), "v": videos}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def _yt_take_quota(cost: int) -> bool:
    day = time.strftime("%Y-%m-%d")
    with _yt_lock:
        try:
            q = json.loads(_YT_QUOTA_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            q = {}
        used = q.get(day, 0)
        if used + cost > config.YOUTUBE_DAILY_QUOTA:
            return False
        tmp = _YT_QUOTA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps({day: used + cost}), encoding="utf-8")
        tmp.replace(_YT_QUOTA_FILE)
        return True


def _iso_dur(s: str) -> int | None:
    """PT1M30S → 90"""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return None
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def _youtube_api(query: str, limit: int) -> list[dict]:
    if not _yt_take_quota(100):
        raise RuntimeError("YouTube Data API 当日配额已用完")
    r = requests.get("https://www.googleapis.com/youtube/v3/search", timeout=20,
                     proxies=_proxies("youtube"),
                     params={"key": config.YOUTUBE_API_KEY, "part": "snippet", "type": "video",
                             "q": query, "maxResults": min(50, max(1, limit))})
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"].get("message", "YouTube API 错误"))
    entries = j.get("items") or []
    ids = [e.get("id", {}).get("videoId") for e in entries]
    ids = [i for i in ids if i]
    stats = {}
    if ids and _yt_take_quota(1):
        vr = requests.get("https://www.googleapis.com/youtube/v3/videos", timeout=20,
                          proxies=_proxies("youtube"),
                          params={"key": config.YOUTUBE_API_KEY,
                                  "part": "contentDetails,statistics", "id": ",".join(ids)}).json()
        stats = {v["id"]: v for v in vr.get("items") or []}
    out = []
    for e in entries:
        vid = e.get("id", {}).get("videoId")
        if not vid:
            continue
        sn = e.get("snippet") or {}
        th = sn.get("thumbnails") or {}
        thumb = (th.get("maxres") or th.get("high") or th.get("medium") or th.get("default") or {}).get("url")
        v = stats.get(vid) or {}
        out.append(_video(
            "YouTube", vid, f"https://www.youtube.com/watch?v={vid}", sn.get("title", ""),
            sn.get("channelTitle", ""), _iso_dur((v.get("contentDetails") or {}).get("duration")),
            (v.get("statistics") or {}).get("viewCount"), thumb or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            sn.get("description") or "", (sn.get("publishedAt") or "")[:10],
        ))
    return out


def _youtube_ytdlp(query: str, limit: int) -> list[dict]:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    if config.PROXY_URL:
        opts["proxy"] = config.PROXY_URL
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    out = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        thumbs = e.get("thumbnails") or []
        out.append(_video(
            "YouTube", e["id"], e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            e.get("title", ""), e.get("channel") or e.get("uploader") or "",
            int(e["duration"]) if e.get("duration") else None, e.get("view_count"),
            thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{e['id']}/hqdefault.jpg",
            e.get("description") or "",
        ))
    return out


def search_youtube(query: str, limit: int = 8) -> list[dict]:
    hit = _yt_cache_get(query, limit)
    if hit is not None:
        return hit
    if config.YOUTUBE_API_KEY:
        try:
            out = _youtube_api(query, limit)
        except Exception:
            out = _youtube_ytdlp(query, limit)
    else:
        out = _youtube_ytdlp(query, limit)
    if out:
        _yt_cache_put(query, limit, out)
    return out


# ---------------- Google 广告透明度中心 ----------------
# 没有官方 API，前端走 adstransparency.google.com/anji/_/rpc 的私有 RPC。
# 视频创意本身不直接给 mp4，但预览 JS 里有 YouTube 视频 id，转成 watch 链接后
# 就能复用平台的解析/下载链路（/resolve、下载队列都认识 YouTube）。
_GADS = "https://adstransparency.google.com"
_YTID_RE = re.compile(
    r"(?:i\.ytimg\.com/vi/|youtu\.be/|youtube\.com/(?:watch\?v=|embed/|shorts/))([\w-]{11})")
_MEDIA_RE = re.compile(r'https?://[^\s"\'<>\\]*(?:googlevideo\.com|\.mp4)[^\s"\'<>\\]*')
# Google 对这个私有接口按 IP 限流，短时间批量取创意会被 429：放慢节奏、多出口轮换、结果缓存
_GADS_PACE = {"lock": threading.Lock(), "last": 0.0}
_GADS_GAP = 0.8
_GADS_PROXY_IDX = [0]
_GADS_RATE_HIT = {"t": 0.0}
_GADS_BLOCK = {"until": 0.0}
_GADS_CACHE_DIR = config.RUNS_DIR / "_gads_cache"


def _gads_proxy() -> dict | None:
    """gads 可以配多个出口轮换（GADS_PROXY_URLS），被限流时换下一个。"""
    urls = config.GADS_PROXY_URLS or [config.proxy_for("gads")]
    urls = [u for u in urls if u]
    if not urls:
        return None
    p = urls[_GADS_PROXY_IDX[0] % len(urls)]
    return {"http": p, "https": p}


def _gads_http(method: str, url: str, **kw):
    with _GADS_PACE["lock"]:  # 只拉开请求起点，网络等待不占锁
        wait = _GADS_GAP - (time.time() - _GADS_PACE["last"])
        if wait > 0:
            time.sleep(wait)
        _GADS_PACE["last"] = time.time()
    r = requests.request(method, url, proxies=_gads_proxy(), **kw)
    if r.status_code == 429:
        _GADS_RATE_HIT["t"] = time.time()
        _GADS_BLOCK["until"] = time.time() + 300  # 一段时间内直接放弃，别每个关键词都去撞墙
        _GADS_PROXY_IDX[0] += 1  # 下次换出口
    return r


def _gads_rpc(path: str, body: dict) -> dict:
    for attempt in range(2):
        r = _gads_http("POST", _GADS + path, timeout=25, params={"authuser": ""},
                       data={"f.req": json.dumps(body, separators=(",", ":"))},
                       headers={"User-Agent": UA, "X-Same-Domain": "1",
                                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                                "Origin": _GADS, "Referer": _GADS + "/"})
        if r.status_code == 200:
            text = r.text[4:] if r.text.startswith(")]}'") else r.text
            return json.loads(text)
        if r.status_code != 429:
            break
        time.sleep(2.5)
    raise RuntimeError(f"Google 广告透明度接口返回 {r.status_code}")


def _gads_advertiser(query: str) -> tuple[str, str] | None:
    """关键词 → 广告主。品牌名通常在查询开头，所以从短到长试；都不行就当作域名查。"""
    words = query.split()
    for q in dict.fromkeys([words[0] if words else query, " ".join(words[:2]), query]):
        if not q:
            continue
        try:
            j = _gads_rpc("/anji/_/rpc/SearchService/SearchSuggestions", {"1": q, "2": 10, "3": 0})
        except Exception as e:
            if "429" in str(e):
                return None
            continue
        for s in j.get("1") or []:
            adv = s.get("1") or {}
            if adv.get("2"):
                return adv["2"], adv.get("1", "")
    try:
        j = _gads_rpc("/anji/_/rpc/SearchService/SearchCreatives",
                      {"2": 40, "3": {"12": {"1": query}}, "7": {"1": 1}})
    except Exception:
        return None
    for a in j.get("1") or []:
        if a.get("1"):
            return a["1"], a.get("12", "")
    return None


def _gads_media(preview_url: str) -> tuple[str, str]:
    """预览编译产物里挖 YouTube id；挖不到再看有没有直链 mp4。返回 (url, 封面)。"""
    try:
        js = _gads_http("GET", preview_url, timeout=20, headers={"User-Agent": UA}).text
    except Exception:
        return "", ""
    m = _YTID_RE.search(js)
    if m:
        vid = m.group(1)
        return (f"https://www.youtube.com/watch?v={vid}",
                f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
    m = _MEDIA_RE.search(js)
    return (m.group(0), "") if m else ("", "")


def _yt_meta(url: str) -> dict:
    try:
        return requests.get("https://www.youtube.com/oembed", timeout=12,
                            proxies=_proxies("youtube"),
                            params={"url": url, "format": "json"}).json()
    except Exception:
        return {}


def _gads_cache_get(query: str, limit: int) -> list[dict] | None:
    p = _GADS_CACHE_DIR / f"{hashlib.sha1(f'{query}|{limit}'.encode()).hexdigest()}.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d.get("v") if time.time() - d.get("t", 0) <= 21600 else None


def _gads_cache_put(query: str, limit: int, videos: list[dict]) -> None:
    try:
        _GADS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _GADS_CACHE_DIR / f"{hashlib.sha1(f'{query}|{limit}'.encode()).hexdigest()}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"t": time.time(), "v": videos}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError:
        pass


def search_gads(query: str, limit: int = 8) -> list[dict]:
    if time.time() < _GADS_BLOCK["until"]:
        return []
    hit = _gads_cache_get(query, limit)
    if hit is not None:
        return hit
    adv = _gads_advertiser(query)
    if not adv:
        return []
    aid, name = adv
    try:
        j = _gads_rpc("/anji/_/rpc/SearchService/SearchCreatives",
                      {"2": min(100, max(limit * 5, 40)),
                       "3": {"12": {"1": "", "2": True}, "13": {"1": [aid]}}, "7": {"1": 1}})
    except Exception:
        return []
    items = [i for i in (j.get("1") or []) if i.get("4") == 3 and i.get("2")]
    items = items[:min(len(items), max(limit * 3, 12))]

    out, seen = [], set()
    for it in items:  # 逐个取，命中限流就停：创意很多，没必要为凑数把 IP 打黑
        if len(out) >= limit:
            break
        preview = ((it.get("3") or {}).get("1") or {}).get("4") or ""
        if not preview:
            continue
        url, thumb = _gads_media(preview)
        if time.time() - _GADS_RATE_HIT["t"] < 1:  # 刚被限流，这一轮不再请求
            break
        if not url or url in seen:
            continue
        seen.add(url)
        title, channel = "", ""
        if "youtube.com" in url:
            meta = _yt_meta(url)
            title, channel = meta.get("title", ""), meta.get("author_name", "")
        ts = ((it.get("6") or {}).get("1") or "")
        published = time.strftime("%Y-%m-%d", time.localtime(int(ts))) if str(ts).isdigit() else ""
        out.append(_video(
            "Google广告透明度", it["2"], url, title or f"{name} 视频广告", channel or name,
            None, None, thumb,
            "Google 广告透明度中心的在投视频广告（按域名/关键词检索到的创意）", published))
    if out:
        _gads_cache_put(query, limit, out)
    return out


# ---------------- Meta 广告资料库 ----------------
# 官方 Graph API：ads_archive。需要完成身份验证的 access token；
# 国内直连不通，走出口代理。拿不到视频直链时退回 ad_snapshot_url（可打开原广告页）。
_META_VID_RE = re.compile(r'"(?:video_hd_url|video_sd_url|video_url)"\s*:\s*"([^"]+)"')


def _meta_video(snapshot_url: str) -> str:
    if not snapshot_url:
        return ""
    try:
        t = requests.get(snapshot_url, timeout=20, proxies=_proxies("meta"),
                         headers={"User-Agent": UA}).text
    except Exception:
        return ""
    m = _META_VID_RE.search(t)
    return m.group(1).replace("\\/", "/") if m else ""


def search_meta_ads(query: str, limit: int = 8) -> list[dict]:
    hit = _gads_cache_get("meta:" + query, limit)
    if hit is not None:
        return hit
    out = _meta_graph(query, limit) if config.META_AD_LIBRARY_TOKEN else _meta_scrape(query, limit)
    if out:
        _gads_cache_put("meta:" + query, limit, out)
    return out


# 没有 access token 时走公开广告库页面：渲染后用位置把 <video> 归属到广告卡片上。
# 只有完成身份验证的开发者账号才能用 Graph API，而验证要等；这条路当天就能用。
_META_CARD_ID = re.compile(r"Library ID:\s*(\d+)")
_META_START = re.compile(r"Started running on (\w{3} \d{1,2}, \d{4})")


def _meta_scrape(query: str, limit: int) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []
    country = (config.META_AD_LIBRARY_COUNTRIES or ["US"])[0].strip() or "US"
    url = ("https://www.facebook.com/ads/library/?active_status=active&ad_type=all"
           f"&country={country}&search_type=keyword_unordered&media_type=video&q={urllib.parse.quote(query)}")
    proxy = config.proxy_for("meta") or config.active_proxy()
    if proxy.startswith("socks5h://"):  # Chromium 不认 socks5h，它不支持代理侧解析 DNS
        proxy = "socks5://" + proxy[len("socks5h://"):]
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True,
                                         proxy={"server": proxy} if proxy else None)
            page = browser.new_context(
                locale="en-US", viewport={"width": 1400, "height": 1000},
                user_agent=UA).new_page()
            page.goto(url, timeout=90000, wait_until="domcontentloaded")
            page.wait_for_timeout(8000)
            for _ in range(2):  # 往下滚两屏，够 limit 条就停
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(2200)
            raw = page.evaluate("""() => {
              const cards = [];
              for (const d of document.querySelectorAll('div')) {
                const t = d.innerText || '';
                if (!/Library ID:\\s*\\d+/.test(t) || t.length > 1200) continue;
                const r = d.getBoundingClientRect();
                cards.push({text: t, top: r.top, bottom: r.bottom});
              }
              const vids = [...document.querySelectorAll('video')].map(v => {
                const r = v.getBoundingClientRect();
                return {src: v.src || v.currentSrc || '', top: r.top, bottom: r.bottom};
              });
              return {cards, vids};
            }""")
            browser.close()
    except Exception:
        return []

    out, seen = [], set()
    for v in raw.get("vids") or []:
        if len(out) >= limit or not v.get("src"):
            continue
        mid = (v["top"] + v["bottom"]) / 2
        # 位置落在哪个卡片里就归给谁；卡片有嵌套，取最内层（最矮）的那张
        hits = sorted((c for c in raw.get("cards") or []
                       if c["top"] - 40 <= mid <= c["bottom"] + 40),
                      key=lambda c: c["bottom"] - c["top"])
        if not hits:
            continue
        text = hits[0]["text"]
        m = _META_CARD_ID.search(text)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        lines = [x.strip() for x in text.split("\n") if x.strip()]
        page_name, body = "", ""
        if "Sponsored" in lines:
            i = lines.index("Sponsored")
            page_name = lines[i - 1] if i else ""
            rest = [x for x in lines[i + 1:] if x not in ("See ad details", "Open Dropdown",
                                                          "This ad has multiple versions")]
            body = rest[0] if rest else ""
        start = _META_START.search(text)
        published = ""
        if start:
            try:
                published = time.strftime("%Y-%m-%d", time.strptime(start.group(1), "%b %d, %Y"))
            except ValueError:
                published = ""
        out.append(_video(
            "Meta 广告库", m.group(1), v["src"].split("&bytestart")[0].split("&byteend")[0],
            (body or f"{page_name} 视频广告")[:120], page_name, None, None, "",
            f"Meta 广告资料库在投视频广告；广告主：{page_name}；开始投放：{published or '未知'}。素材文案：{body}",
            published))
    return out


def _meta_graph(query: str, limit: int) -> list[dict]:
    countries = [c.strip() for c in config.META_AD_LIBRARY_COUNTRIES if c.strip()]
    r = requests.get(
        f"https://graph.facebook.com/{config.META_AD_LIBRARY_VERSION}/ads_archive",
        timeout=25, proxies=_proxies("meta"),
        params={"access_token": config.META_AD_LIBRARY_TOKEN, "search_terms": query,
                "ad_reached_countries": json.dumps(countries), "ad_type": "ALL",
                "fields": ("id,page_name,ad_creative_bodies,ad_snapshot_url,"
                           "ad_delivery_start_time,ad_delivery_stop_time,publisher_platforms"),
                "limit": min(50, max(1, limit * 2))})
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"].get("message", "Meta 广告资料库错误"))
    out = []
    for it in (j.get("data") or [])[:limit]:
        body = (it.get("ad_creative_bodies") or [""])[0]
        snap = it.get("ad_snapshot_url") or ""
        out.append(_video(
            "Meta 广告库", it.get("id", ""), _meta_video(snap) or snap,
            body[:120] or it.get("page_name", ""), it.get("page_name", ""),
            None, None, "",
            f"Meta 广告资料库在投视频广告；广告主：{it.get('page_name', '')}。素材文案：{body}",
            (it.get("ad_delivery_start_time") or "")[:10],
        ))
    return out


# ---------------- Bilibili ----------------
_MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
          33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
          26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
          20, 34, 44, 52]


class _Bili:
    """维护一个带风控 cookie 和 WBI 密钥的会话，串行请求以免触发限流。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.session = None
        self.mixin_key = None
        self.created = 0.0

    def _init(self):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Referer": "https://search.bilibili.com/"})
        if config.BILIBILI_COOKIE:
            # 已登录用户的 cookie，风控宽松得多
            for kv in config.BILIBILI_COOKIE.split(";"):
                if "=" in kv:
                    k, v = kv.strip().split("=", 1)
                    s.cookies.set(k, v, domain=".bilibili.com")
        s.get("https://www.bilibili.com/", timeout=15)
        if "buvid3" not in s.cookies:
            spi = s.get("https://api.bilibili.com/x/frontend/finger/spi", timeout=15).json()["data"]
            s.cookies.set("buvid3", spi["b_3"], domain=".bilibili.com")
            s.cookies.set("buvid4", spi["b_4"], domain=".bilibili.com")
        ts = int(time.time())
        sign = hmac.new(b"XgwSnGZ1p", f"ts{ts}".encode(), hashlib.sha256).hexdigest()
        tk = s.post("https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket",
                    params={"key_id": "ec02", "hexsign": sign, "context[ts]": ts, "csrf": ""},
                    timeout=15).json()
        if tk.get("code") == 0:
            s.cookies.set("bili_ticket", tk["data"]["ticket"], domain=".bilibili.com")
        wbi = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=15).json()["data"]["wbi_img"]
        key = lambda u: u.rsplit("/", 1)[1].split(".")[0]
        raw = key(wbi["img_url"]) + key(wbi["sub_url"])
        self.mixin_key = "".join(raw[i] for i in _MIXIN)[:32]
        self.session = s
        self.created = time.time()

    def _get(self, path: str, params: dict) -> dict:
        params = {**params, "wts": int(time.time())}
        q = urllib.parse.urlencode(sorted(params.items()))
        params["w_rid"] = hashlib.md5((q + self.mixin_key).encode()).hexdigest()
        return self.session.get(f"https://api.bilibili.com{path}", params=params, timeout=15).json()

    def _try(self, query: str) -> list | None:
        """依次尝试两个搜索接口；被风控（返回 v_voucher）时返回 None。"""
        r = self._get("/x/web-interface/wbi/search/type",
                      {"search_type": "video", "keyword": query, "page": 1})
        data = r.get("data") or {}
        if r.get("code") == 0 and "result" in data:
            return data["result"] or []
        if r.get("code") not in (0, None):  # 签名/cookie 失效之类，重建会话
            self._init()
        time.sleep(1)
        r = self._get("/x/web-interface/wbi/search/all/v2", {"keyword": query})
        data = r.get("data") or {}
        if r.get("code") == 0 and "result" in data:
            for blk in data["result"] or []:
                if blk.get("result_type") == "video":
                    return blk.get("data") or []
            return []
        return None

    def search(self, query: str, limit: int) -> list[dict]:
        with self.lock:
            if not self.session or time.time() - self.created > 1800:
                self._init()
            items = None
            for attempt in range(2):
                items = self._try(query)
                if items is not None:
                    break
                time.sleep(4 * (attempt + 1))  # 风控冷却
            time.sleep(1.2 + random.random())  # 礼貌间隔，降低触发风控概率
            if items is None:
                hint = "" if config.BILIBILI_COOKIE else "（可在 .env 配置 BILIBILI_COOKIE 缓解）"
                raise RuntimeError(f"B 站风控拦截{hint}")
            data = {"result": items}
        out = []
        for v in (data.get("result") or [])[:limit]:
            pic = v.get("pic", "")
            out.append(_video(
                "Bilibili", v["bvid"], f"https://www.bilibili.com/video/{v['bvid']}",
                html.unescape(re.sub(r"<[^>]+>", "", v.get("title", ""))),
                v.get("author", ""), _dur(v.get("duration")), v.get("play"),
                ("https:" + pic) if pic.startswith("//") else pic,
                v.get("description", ""),
                time.strftime("%Y-%m-%d", time.localtime(v["pubdate"])) if v.get("pubdate") else "",
            ))
        return out


def _dur(s) -> int | None:
    if not s:
        return None
    try:
        parts = [int(p) for p in str(s).split(":")]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


_bili = _Bili()


_bili_blocked_until = 0.0


def search_bilibili(query: str, limit: int = 8) -> list[dict]:
    """优先用 B 站官方接口（有播放量、发布时间）；被风控时改走必应检索 B 站视频，
    并熔断 10 分钟，期间直接走必应，避免每次都白等退避重试。"""
    global _bili_blocked_until
    if time.time() >= _bili_blocked_until:
        try:
            return _bili.search(query, limit)
        except RuntimeError:
            _bili_blocked_until = time.time() + 600
    return _bing_site(query, limit, "bilibili.com", ("bilibili", "B站"))


# ---------------- 必应视频（聚合多个视频站） ----------------
# 必应视频结果页的每条结果都带 vrhm 属性（JSON），含真实地址、时长、标题、缩略图 id。
# 中文站（cn.bing.com）主要覆盖 B 站 / 抖音 / 爱奇艺 / 腾讯视频；
# 国际站（www.bing.com）能覆盖 iSpot.tv、Ads of the World、CulturePub 等广告库。
_DOMAINS = {
    "bilibili.com": "Bilibili", "douyin.com": "抖音", "iqiyi.com": "爱奇艺", "v.qq.com": "腾讯视频",
    "youku.com": "优酷", "ixigua.com": "西瓜视频", "sohu.com": "搜狐视频", "weibo.com": "微博",
    "youtube.com": "YouTube", "vimeo.com": "Vimeo", "ispot.tv": "iSpot.tv",
    "adsoftheworld.com": "Ads of the World", "culturepub.fr": "CulturePub", "thedrum.com": "The Drum",
    "adforum.com": "AdForum", "lbbonline.com": "LBB", "shots.net": "shots",
    "adweek.com": "Adweek", "xinpianchang.com": "新片场", "digitaling.com": "数英", "acfun.cn": "AcFun",
    "dailymotion.com": "Dailymotion", "superbowl-ads.com": "Super Bowl Ads", "bestadsontv.com": "Best Ads on TV",
}
_bing_lock = threading.Semaphore(4)  # 限制并发，避免被必应限流
_bing_session = requests.Session()
_bing_session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})


def _platform_of(url: str) -> str:
    host = urllib.parse.urlparse(url).netloc.lower()
    for dom, name in _DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return name
    return host.removeprefix("www.")


def _bing_videos(host: str, query: str, limit: int, only: str | None = None) -> list[dict]:
    with _bing_lock:
        t = _bing_session.get(f"https://{host}/videos/search", params={"q": query}, timeout=15).text
    out, seen = [], set()
    for raw in re.findall(r'vrhm="([^"]+)"', t):
        try:
            i = json.loads(html.unescape(raw))
        except ValueError:
            continue
        url = i.get("purl") or i.get("pgurl") or ""
        if not url.startswith("http") or url in seen:
            continue
        if only and only not in url:
            continue
        seen.add(url)
        m = re.match(r"更多\s*(.+?)\s*视频$|More from\s*(.+)$", (i.get("capt") or {}).get("crtnamemore", ""))
        channel = (m.group(1) or m.group(2)) if m else ""
        vid = canonical(url).rstrip("/").rsplit("/", 1)[-1]
        out.append(_video(
            _platform_of(url), vid, canonical(url), html.unescape(i.get("vt", "")), channel,
            _dur(i.get("du")), None,
            f"https://tse1.mm.bing.net/th?id={i['thid']}&w=480&h=270&c=7" if i.get("thid") else "",
        ))
        if len(out) >= limit:
            break
    return out


def _bing_site(query: str, limit: int, only: str, suffixes: tuple[str, ...],
               host: str = "cn.bing.com") -> list[dict]:
    """借必应视频检索某个站点：依次尝试几种后缀写法，只保留标题含关键词的结果。
    必应对个别写法会“跑偏”（返回无关视频），所以要做相关性过滤并换写法补足。"""
    tokens = [t.lower() for t in query.split() if len(t) >= 2]
    out, seen = [], set()
    for suffix in suffixes:
        for v in _bing_videos(host, f"{query} {suffix}", limit * 3, only=only):
            if v["url"] not in seen and any(t in v["title"].lower() for t in tokens):
                seen.add(v["url"])
                out.append(v)
        if len(out) >= min(3, limit):
            break
    return out[:limit]


def search_douyin(query: str, limit: int = 8) -> list[dict]:
    """抖音站内搜索有强反爬，借必应视频检索抖音视频（site: 前缀实测不稳定）。"""
    return _bing_site(query, limit, "douyin.com", ("抖音", "douyin"))


def search_web_cn(query: str, limit: int = 8) -> list[dict]:
    return _bing_videos("cn.bing.com", query, limit)


def search_web_intl(query: str, limit: int = 8) -> list[dict]:
    return _bing_videos("www.bing.com", query, limit)


def canonical(url: str) -> str:
    """统一地址，便于跨来源去重：B 站去掉参数和末尾斜杠，YouTube 只保留 v。"""
    u = urllib.parse.urlparse(url)
    host = u.netloc.lower()
    if "bilibili.com" in host:
        m = re.search(r"(BV[0-9A-Za-z]{10})", u.path)
        if m:
            return f"https://www.bilibili.com/video/{m.group(1)}"
    if "youtube.com" in host:
        v = urllib.parse.parse_qs(u.query).get("v")
        if v:
            return f"https://www.youtube.com/watch?v={v[0]}"
    if "douyin.com" in host:
        m = re.search(r"/video/(\d+)", u.path)
        if m:
            return f"https://www.douyin.com/video/{m.group(1)}"
    return url


# ---------------- 各站自有搜索接口 ----------------
_direct = requests.Session()
_direct.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})


def search_youku(query: str, limit: int = 8) -> list[dict]:
    """优酷搜索接口：收录大量电视广告（卫视/央视播出版）。"""
    j = _direct.get("https://search.youku.com/api/search", params={"keyword": query, "pg": 1},
                    timeout=15).json()
    out = []
    for comp in j.get("pageComponentList") or []:
        for block in (comp.get("componentMap") or {}).values():
            for d in (block.get("data") if isinstance(block, dict) else None) or []:
                vid = d.get("videoId")
                title = (d.get("titleDTO") or {}).get("displayName", "")
                if not vid or not title:
                    continue
                shot = d.get("screenShotDTO") or {}
                out.append(_video("优酷", vid, f"https://v.youku.com/v_show/id_{vid}.html", title,
                                  d.get("userName", ""), _dur(shot.get("rightBottomText")), None,
                                  shot.get("thumbUrl", ""), published=d.get("publishTime", "")))
    return out[:limit]


def _acfun_title(log: str) -> str | None:
    """从卡片的 data-click-log（JSON）里取标题。以前用正则截到 "}'> 为止，标题里带双引号时会一路吃进后面的
    网页源码；现在按 JSON 解析，解析不了再退回正则，并丢弃含尖括号或过长的结果。"""
    try:
        title = json.loads(html.unescape(log)).get("title")
    except (ValueError, AttributeError):
        m = re.search(r'"title":"(.*)"\}\s*$', log, re.S)
        title = html.unescape(m.group(1)) if m else None
    if not isinstance(title, str):
        return None
    title = title.strip()
    return title if title and len(title) <= 200 and "<" not in title and ">" not in title else None


def search_acfun(query: str, limit: int = 8) -> list[dict]:
    t = _direct.get("https://www.acfun.cn/search", params={"keyword": query}, timeout=15).text
    t = t.replace('\\"', '"').replace("\\/", "/")
    return _parse_acfun(t, limit)


def _parse_acfun(t: str, limit: int = 8) -> list[dict]:
    out, seen = [], set()
    pat = (r'<a href="/v/ac(\d+)"[^>]*data-click-log=\'([^\']*)\'>\s*<img src="([^"]+)"'
           r'.*?video__duration">([\d:]+)<')
    for ac, log, img, dur in re.findall(pat, t, re.S):
        title = _acfun_title(log)
        if ac in seen or not title:
            continue
        seen.add(ac)
        out.append(_video("AcFun", f"ac{ac}", f"https://www.acfun.cn/v/ac{ac}",
                          title, "", _dur(dur), None, img.split("?")[0]))
    return out[:limit]


def search_digitaling(query: str, limit: int = 8) -> list[dict]:
    """数英：国内最大的广告案例库。返回的是案例页（含视频与创意说明），优先带视频的案例。"""
    # 数英本身就是广告库，关键词里的“广告/TVC”等词反而会让结果变少
    kw = " ".join(w for w in query.split()
                  if not re.fullmatch(r"(广告片?|TVC|tvc|创意|短片|宣传片|视频|获奖)", w)) or query
    t = _direct.get("https://www.digitaling.com/search/projects", params={"kw": kw}, timeout=15).text
    out, seen = [], set()
    pat = (r'<a href="(https://www\.digitaling\.com/projects/(\d+)\.html)"[^>]*class="thumb[^"]*">'
           r'\s*<img src="([^"]*)" alt="([^"]*)"')  # 标题含 <em> 高亮，不能用 [^>]* 跨过去
    for m in re.finditer(pat, t):
        url, pid, img, title = m.groups()
        if pid in seen:
            continue
        seen.add(pid)
        has_video = "gicon_video" in t[m.end():m.end() + 300]
        out.append(_video("数英", pid, url, re.sub(r"<[^>]+>", "", html.unescape(title)), "",
                          None, None, img, "含视频" if has_video else "图文案例"))
    out.sort(key=lambda v: v["description"] != "含视频")
    return out[:limit]


def search_dailymotion(query: str, limit: int = 8) -> list[dict]:
    j = _direct.get("https://api.dailymotion.com/videos", params={
        "search": query, "limit": limit,
        "fields": "id,title,duration,views_total,thumbnail_480_url,owner.screenname,created_time"},
        timeout=15).json()
    return [_video("Dailymotion", v["id"], f"https://www.dailymotion.com/video/{v['id']}", v["title"],
                   v.get("owner.screenname", ""), v.get("duration"), v.get("views_total"),
                   v.get("thumbnail_480_url", ""), "",
                   time.strftime("%Y-%m-%d", time.localtime(v["created_time"])) if v.get("created_time") else "")
            for v in j.get("list", [])]


# 借必应定向检索的站点：(站点域名, 关键词后缀, 必应站点)
def _site(only, suffixes, host="cn.bing.com"):
    return lambda query, limit=8: _bing_site(query, limit, only, suffixes, host)


# key → (函数, 搜哪种语言的关键词, 显示名, 分组)
SOURCES = {
    # 国内视频平台
    "bilibili": (search_bilibili, ("zh",), "B 站", "国内视频"),
    "douyin": (search_douyin, ("zh",), "抖音", "国内视频"),
    "youku": (search_youku, ("zh",), "优酷", "国内视频"),
    "tencent": (_site("v.qq.com", ("腾讯", "腾讯视频")), ("zh",), "腾讯视频", "国内视频"),
    "iqiyi": (_site("iqiyi.com", ("爱奇艺", "iqiyi")), ("zh",), "爱奇艺", "国内视频"),
    "acfun": (search_acfun, ("zh",), "AcFun", "国内视频"),
    "web_cn": (search_web_cn, ("zh",), "全网视频（中文）", "国内视频"),
    # 海外视频平台
    "youtube": (search_youtube, ("en", "zh"), "YouTube", "海外视频"),
    "dailymotion": (search_dailymotion, ("en",), "Dailymotion", "海外视频"),
    "web_intl": (search_web_intl, ("en",), "全网视频（海外）", "海外视频"),
    # 广告案例库
    "digitaling": (search_digitaling, ("zh",), "数英", "广告案例库"),
    "xinpianchang": (_site("xinpianchang.com", ("新片场",)), ("zh",), "新片场", "广告案例库"),
    "ispot": (_site("ispot.tv", ("ispot tv spot",), "www.bing.com"), ("en",), "iSpot.tv", "广告案例库"),
    "adsoftheworld": (_site("adsoftheworld.com", ("ads of the world",), "www.bing.com"), ("en",),
                      "Ads of the World", "广告案例库"),
    "adweek": (_site("adweek.com", ("adweek video", "adweek"), "www.bing.com"), ("en",), "Adweek", "广告案例库"),
    "culturepub": (_site("culturepub.fr", ("culturepub",), "www.bing.com"), ("en",), "CulturePub", "广告案例库"),
    # 投放侧广告库：看竞品正在投什么
    "gads": (search_gads, ("en",), "Google 广告透明度", "广告案例库"),
    "meta_ads": (search_meta_ads, ("en",), "Meta 广告库", "广告案例库"),
}
SEARCHERS = {k: (v[0], v[1]) for k, v in SOURCES.items()}
SOURCE_NAMES = {k: v[2] for k, v in SOURCES.items()}
