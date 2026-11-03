"""单条分享链接 → 元数据 + 可下载直链（抖音 / 小红书 / B站 / YouTube / TikTok）。

各平台的坑不一样，实现方式也不同：

- 抖音：网页详情接口有 a_bogus 签名，纯 HTTP 一律 403（ArgusSecurityPlugin），
  只能用真实浏览器打开页面、拦 `/aweme/v1/web/aweme/detail/` 的响应。yt-dlp 的抖音插件
  缺 msToken，报 "Fresh cookies are needed"，给 cookies 也过不了。
- 小红书：必须登录态 cookie，且链接要带 `xsec_token`（46 字符、会过期），否则 noteDetailMap 为空。
  yt-dlp 的小红书插件不填标题和作者，单独从详情页的 og 标签补。
- TikTok：2026-09 起会因 User-Agent 把 yt-dlp 判成异常（yt-dlp#17604），
  指定浏览器 UA 即可，**不需要账号或 cookies**。
- B站：加 Referer；香港等境外 IP 对部分投稿有地域限制，属预期内失败。
- 视频号：没有公开网页，只做元数据，不解析。
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

from . import config

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")

SHORT = {
    "v.douyin.com": "douyin",
    "xhslink.com": "xhs",
    "b23.tv": "bilibili",
    "vm.tiktok.com": "tiktok",
}

PLATFORMS = {
    "douyin.com": "douyin",
    "xiaohongshu.com": "xhs",
    "bilibili.com": "bilibili",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}

NAMES = {"douyin": "抖音", "xhs": "小红书", "bilibili": "B站",
         "youtube": "YouTube", "tiktok": "TikTok"}


class ResolveError(RuntimeError):
    pass


# 一台机器 961MB 内存，开一个无头浏览器就占 300~500MB，抖音解析串行执行
_DOUYIN_LOCK = threading.Lock()

# 解析结果缓存：直链会过期，且页面 URL 就是同一作品
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 1800

STREAM_DIR = config.RUNS_DIR / "_resolve"
STREAM_LIMIT = 1024 ** 3  # 直链拿不到时才落盘，上限 1GB


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url).split("/")[0].lower()


def platform_of(url: str) -> str | None:
    host = _host(url)
    for domain, name in PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def expand(url: str) -> str:
    """分享短链跟随跳转，拿到真实地址。"""
    host = _host(url)
    if not any(host == d or host.endswith("." + d) for d in SHORT):
        return url
    try:
        return requests.get(url, headers={"User-Agent": UA}, allow_redirects=True, timeout=20).url
    except requests.RequestException as e:
        raise ResolveError(f"短链跳转失败：{e}")


def _load_cookies(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            out[parts[5]] = parts[6]
    return out


def _netscape(path: Path) -> list[dict]:
    cookies = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        cookies.append({"name": parts[5], "value": parts[6], "domain": parts[0], "path": parts[2],
                        "secure": parts[3] == "TRUE",
                        "expires": int(parts[4]) if parts[4].lstrip("-").isdigit() else -1})
    return cookies


def _ytdlp_cmd(url: str, platform: str) -> list[str]:
    cmd = [sys.executable, "-m", "yt_dlp"]
    if platform == "tiktok":
        cmd += ["--user-agent", UA]
    elif platform == "xhs":
        ck = Path(config.XHS_COOKIES_FILE)
        if not ck.exists():
            raise ResolveError("小红书需要登录态：把 cookies 放到 " + str(ck))
        cmd += ["--user-agent", UA, "--cookies", str(ck)]
    elif platform == "bilibili":
        cmd += ["--add-header", "Referer:https://www.bilibili.com/"]
    return cmd


# ---------------- yt-dlp 平台 ----------------
def _info(url: str, platform: str) -> dict:
    cmd = _ytdlp_cmd(url, platform) + [
        "-J", "--skip-download", "--no-warnings", "--socket-timeout", "20", url]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        err = (p.stderr.strip().splitlines() or ["yt-dlp 失败"])[-1]
        if "Unsupported URL" in err:
            raise ResolveError("该链接暂不支持解析")
        if "geo-restricted" in err or "geo restriction" in err.lower():
            raise ResolveError("该视频在当前服务器所在地区不可用（地域限制），换一条试试")
        raise ResolveError(err[:300])
    data = json.loads(p.stdout)
    if data.get("_type") == "playlist":
        entries = data.get("entries") or []
        if not entries:
            raise ResolveError("链接里没有可用视频")
        data = entries[0]

    formats = [f for f in data.get("formats") or [] if f.get("url")]
    if not formats:
        raise ResolveError("这条内容没有视频（可能是图文笔记）")
    # 只选音视频齐全的 http 渐进式格式：DASH/HLS 分片浏览器不能直接放进 <video>，
    # 纯视频流没音轨（B站、YouTube 的默认 best 都是这种）。
    playable = [f for f in formats
                if (f.get("protocol") or "").startswith("http")
                and f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")]
    best = max(playable or formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    return {
        "platform": platform,
        "platform_name": NAMES.get(platform, platform),
        "video_id": data.get("id"),
        "title": data.get("title") or "",
        "author": data.get("uploader") or data.get("channel") or "",
        "duration": data.get("duration"),
        "cover": data.get("thumbnail") or "",
        "video_url": best["url"],
        "headers": best.get("http_headers") or {},
        "page_url": data.get("webpage_url") or url,
        "max_height": best.get("height"),
        # 没有音视频齐全的渐进式格式时（YouTube 1080p 只有 DASH），直链没法直接播，
        # 播放要走 yt-dlp 合并成本地 mp4
        "muxed": bool(playable),
    }


def _meta(html: str, prop: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(prop), html)
    return m.group(1).strip() if m else ""


def _xhs_meta(url: str) -> dict:
    """yt-dlp 的小红书插件不填标题/作者，从详情页补。

    __INITIAL_STATE__ 里有一段是双重转义，整体 json.loads 会失败，所以只做定点提取。
    """
    ck = Path(config.XHS_COOKIES_FILE)
    if not ck.exists():
        return {}
    try:
        r = requests.get(url, headers={"User-Agent": UA}, cookies=_load_cookies(ck), timeout=25)
        html = r.text
        names = re.findall(r'"nickname":"((?:[^"\\]|\\.)*)"', html)
        return {
            "title": re.sub(r"\s*-\s*小红书\s*$", "", _meta(html, "og:title")),
            "desc": _meta(html, "og:description"),
            # 第一个 nickname 是当前登录账号，笔记作者在最后一个
            "author": names[-1] if names else "",
            "like": _meta(html, "og:xhs:note_like"),
            "collect": _meta(html, "og:xhs:note_collect"),
            "comment": _meta(html, "og:xhs:note_comment"),
        }
    except Exception:
        return {}


# ---------------- 抖音单条 ----------------
def _chrome_path() -> str | None:
    override = os.environ.get("CHROME_PATH")
    if override and Path(override).exists():
        return override
    base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    for pat in ("chromium-*/chrome-linux64/chrome", "chromium-*/chrome-linux/chrome"):
        found = sorted(base.glob(pat))
        if found:
            return str(found[-1])
    return None


def _douyin(url: str) -> dict:
    ck = Path(config.DOUYIN_COOKIES_FILE)
    if not ck.exists():
        raise ResolveError("抖音需要登录态：把 cookies 放到 " + str(ck))
    m = re.search(r"/video/(\d+)", url)
    if not m:
        raise ResolveError("无法从链接里取出作品 ID")
    video_id = m.group(1)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ResolveError("服务器未安装 playwright，无法解析抖音")

    found: dict = {}

    def on_response(resp):
        if "/aweme/v1/web/aweme/detail/" not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            return
        a = data.get("aweme_detail")
        if not a:
            detail = (data.get("filter_detail") or {})
            if detail.get("detail_msg"):
                found["blocked"] = detail["detail_msg"]
            return
        v = a.get("video") or {}
        for br in sorted(v.get("bit_rate") or [], key=lambda x: x.get("bit_rate") or 0, reverse=True):
            urls = (br.get("play_addr") or {}).get("url_list") or []
            if urls:
                found["url"] = urls[0]
                break
        if not found.get("url"):
            urls = (v.get("play_addr") or {}).get("url_list") or []
            if urls:
                found["url"] = urls[0]
        found["desc"] = (a.get("desc") or "").strip()
        found["author"] = ((a.get("author") or {}).get("nickname") or "")
        found["cover"] = ((v.get("cover") or {}).get("url_list") or [""])[0]
        found["duration"] = round((v.get("duration") or 0) / 1000, 2)

    with _DOUYIN_LOCK:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, executable_path=_chrome_path(),
                                        args=["--disable-blink-features=AutomationControlled",
                                              "--no-sandbox"])
            try:
                ctx = browser.new_context(user_agent=UA, locale="zh-CN",
                                          viewport={"width": 1280, "height": 860})
                ctx.add_cookies([x for x in _netscape(ck) if "douyin" in x["domain"]])
                page = ctx.new_page()
                page.on("response", on_response)
                page.goto(f"https://www.douyin.com/video/{video_id}",
                          wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(6000)
            finally:
                browser.close()

    if not found.get("url"):
        raise ResolveError(found.get("blocked")
                           or "没能拿到播放地址（cookies 可能已失效，或该作品不是视频）")
    return {
        "platform": "douyin",
        "platform_name": "抖音",
        "video_id": video_id,
        "title": found.get("desc", ""),
        "author": found.get("author", ""),
        "duration": found.get("duration"),
        "cover": found.get("cover", ""),
        "video_url": found["url"],
        "headers": {"User-Agent": UA, "Referer": "https://www.douyin.com/"},
        "page_url": f"https://www.douyin.com/video/{video_id}",
        "max_height": None,
    }


def resolve(url: str) -> dict:
    url = expand(url.strip())
    platform = platform_of(url)
    if not platform:
        if "channels.weixin.qq.com" in url or "weixin.qq.com" in url:
            raise ResolveError("视频号没有公开网页，无法解析；第一版只用数据源给的元数据")
        raise ResolveError(f"不认识的链接：{url}")
    if platform == "douyin":
        return _douyin(url)
    info = _info(url, platform)
    if platform == "xhs":
        for k, v in _xhs_meta(url).items():
            if v:
                info[k] = v
    return info


def resolve_cached(url: str) -> dict:
    """同一作品短时间反复播放时不重复解析（抖音一次要开浏览器 8~10 秒）。"""
    hit = _CACHE.get(url)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    info = resolve(url)
    now = time.time()
    _CACHE[url] = (now, info)
    _CACHE[info["page_url"]] = (now, info)
    if len(_CACHE) > 400:
        for k, _ in sorted(_CACHE.items(), key=lambda x: x[1][0])[:100]:
            _CACHE.pop(k, None)
    return info


def open_upstream(info: dict, rng: str | None = None):
    """直连 CDN 拿字节流；拿不到（TikTok 裸请求 403）返回 None，调用方改用本地缓存。"""
    headers = dict(info.get("headers") or {})
    if rng:
        headers["Range"] = rng
    try:
        r = requests.get(info["video_url"], headers=headers, stream=True, timeout=60)
    except requests.RequestException:
        return None
    if r.status_code >= 400:
        r.close()
        return None
    return r


def _prune() -> None:
    files = sorted(STREAM_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while files and total > STREAM_LIMIT:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def cache_local(info: dict, max_height: int = 720) -> Path:
    path = download(info, STREAM_DIR, max_height)
    _prune()
    return path


def _safe_name(text: str, limit: int = 50) -> str:
    cleaned = re.sub(r"[^\w一-鿿\-]+", "_", text or "", flags=re.UNICODE)
    return re.sub(r"_+", "_", cleaned).strip("_")[:limit] or "video"


def download(info: dict, out_dir: Path, max_height: int = 720) -> Path:
    """抖音走 CDN 直链（带 Referer 就够）；其余交给 yt-dlp，TikTok 的 CDN 对裸请求返回 403。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{info['platform']}_{info['video_id']}_{_safe_name(info.get('title'))}"

    if info["platform"] == "douyin":
        path = out_dir / f"{stem}.mp4"
        with requests.get(info["video_url"], headers=info["headers"], stream=True, timeout=60) as r:
            r.raise_for_status()
            tmp = path.with_suffix(".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
            tmp.replace(path)
        return path

    cmd = _ytdlp_cmd(info["page_url"], info["platform"]) + [
        "--quiet", "--no-warnings", "--no-playlist",
        "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
        "--merge-output-format", "mp4", "-S", "vcodec:h264,acodec:aac",
        "-o", str(out_dir / f"{stem}.%(ext)s"),
        "--retries", "3", "--socket-timeout", "30", "--print", "after_move:filepath",
    ]
    if config.ffmpeg_path():
        cmd += ["--ffmpeg-location", config.ffmpeg_path()]
    cmd.append(info["page_url"])
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    if p.returncode != 0 or not lines:
        err = (p.stderr.strip().splitlines() or ["下载失败"])[-1]
        raise ResolveError(err[:300])
    path = Path(lines[-1])
    if not path.exists():
        hits = [f for f in out_dir.glob(f"{stem}.*") if f.suffix != ".part"]
        if not hits:
            raise ResolveError("yt-dlp 报告成功但没有找到文件")
        path = max(hits, key=lambda f: f.stat().st_size)
    return path
