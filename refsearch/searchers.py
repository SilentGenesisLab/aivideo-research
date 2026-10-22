"""第 2 步：视频平台检索。统一输出 Video 字典。

- YouTube：yt-dlp 的 ytsearch（无需 API key）
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


def _video(platform, vid, url, title, channel="", duration=None, views=None,
           thumbnail="", description="", published=""):
    return {
        "platform": platform, "id": vid, "url": url, "title": title,
        "channel": channel, "duration": duration, "views": views,
        "thumbnail": thumbnail, "description": (description or "")[:300],
        "published": published,
    }


# ---------------- YouTube ----------------
def search_youtube(query: str, limit: int = 8) -> list[dict]:
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
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
}
SEARCHERS = {k: (v[0], v[1]) for k, v in SOURCES.items()}
SOURCE_NAMES = {k: v[2] for k, v in SOURCES.items()}
