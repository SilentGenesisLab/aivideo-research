"""站内播放：把视频地址换成可内嵌的官方播放器地址；换不了的，由服务器用 yt-dlp
下载一份低清缓存（中转播放），页面用 <video> 直接播放。"""
import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config, downloader
from .searchers import UA

CACHE_DIR = config.RUNS_DIR / "_cache"
CACHE_LIMIT = 3 * 1024 ** 3  # 中转缓存上限 3GB，超出删最旧的
RELAY_HEIGHT = 480

# (地址匹配, 官方内嵌播放器)
_RULES = [
    (r"youtube\.com/watch\?.*?v=([\w-]{11})|youtu\.be/([\w-]{11})|youtube\.com/(?:shorts|embed)/([\w-]{11})",
     "https://www.youtube.com/embed/{}?autoplay=1&rel=0"),
    (r"bilibili\.com/video/(BV\w{10})",
     "https://player.bilibili.com/player.html?bvid={}&autoplay=1&high_quality=1&danmaku=0"),
    (r"youku\.com/v_show/id_(\w+)|player\.youku\.com/embed/(\w+)",
     "https://player.youku.com/embed/{}"),
    (r"v\.qq\.com/(?:x/page|x/cover/\w+)/(\w{11})\.html|v\.qq\.com/txp/iframe/player\.html\?vid=(\w{11})",
     "https://v.qq.com/txp/iframe/player.html?vid={}&autoplay=1"),
    (r"douyin\.com/video/(\d{15,})",
     "https://open.douyin.com/player/video?vid={}&autoplay=1"),
    (r"dailymotion\.com/video/(\w+)|dai\.ly/(\w+)",
     "https://www.dailymotion.com/embed/video/{}?autoplay=1"),
    (r"acfun\.cn/v/ac(\d+)",
     "https://www.acfun.cn/player/ac{}"),
    (r"player\.vimeo\.com/video/(\d+)|vimeo\.com/(\d+)",
     "https://player.vimeo.com/video/{}?autoplay=1"),
]


def embed_url(url: str) -> str | None:
    for pat, tpl in _RULES:
        m = re.search(pat, url or "")
        if m:
            return tpl.format(next(g for g in m.groups() if g))
    return None


def _page_embed(url: str) -> dict | None:
    """案例页（数英、Adweek 等）里通常嵌着腾讯/优酷/YouTube 播放器或 mp4，挖出来。"""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=10)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    for src in re.findall(r'<iframe[^>]+src="([^"]+)"', r.text):
        src = src.replace("&amp;", "&")
        src = "https:" + src if src.startswith("//") else src
        if e := embed_url(src):
            return {"kind": "iframe", "src": e}
    m = re.search(r'https?://[^"\'\s<>]+?\.mp4(?:\?[^"\'\s<>]*)?', r.text)
    if m:
        return {"kind": "video", "src": m.group(0).replace("&amp;", "&")}
    return None


def _direct(url: str) -> str | None:
    """用 yt-dlp 只解析不下载，找一个浏览器能直接播放的单文件（音画一体的 http 直链）。"""
    try:
        p = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--no-playlist", "--no-warnings",
                            "--socket-timeout", "15", url], capture_output=True, text=True, timeout=40)
        info = json.loads(p.stdout) if p.returncode == 0 else {}
    except (subprocess.TimeoutExpired, ValueError):
        return None
    ok = [f for f in info.get("formats") or [info]
          if f.get("url") and f.get("protocol", "https") in ("http", "https")
          and f.get("vcodec") != "none" and f.get("acodec") != "none"
          and f.get("ext") in ("mp4", "webm", "m4v", "unknown_video", None)]
    if not ok:
        return None
    small = [f for f in ok if (f.get("height") or 0) <= 720]
    return (max(small, key=lambda f: f.get("height") or 0) if small else ok[0])["url"]


def resolve(url: str) -> dict:
    """返回 {kind: iframe|video|relay, src}；relay 表示需要服务器中转。"""
    if e := embed_url(url):
        return {"kind": "iframe", "src": e}
    if cached := cached_file(url):
        return {"kind": "video", "src": cached}
    if "iqiyi.com" not in urlparse(url).netloc:
        if found := _page_embed(url):
            return found
    if src := _direct(url):
        return {"kind": "video", "src": src}
    return {"kind": "relay", "src": None}


def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def cached_file(url: str) -> str | None:
    for f in CACHE_DIR.glob(f"*_{_key(url)}_x.mp4"):
        f.touch()
        return f"/runs/_cache/{f.name}"
    return None


def _prune():
    files = sorted(CACHE_DIR.glob("*.mp4"), key=lambda f: f.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    while files and total > CACHE_LIMIT:
        f = files.pop(0)
        total -= f.stat().st_size
        f.unlink(missing_ok=True)


def relay(url: str, progress=None) -> dict:
    """服务器下载 480p 缓存后返回播放地址。"""
    if cached := cached_file(url):
        return {"ok": True, "src": cached}
    if progress:
        progress("服务器正在拉取视频（通常 10~60 秒）…")
    t = time.time()
    res = downloader.download([{"url": url, "platform": "Bilibili" if "bilibili.com" in url else "relay",
                                "id": _key(url), "title": "x"}],
                              CACHE_DIR, max_height=RELAY_HEIGHT, timeout=300)[0]
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    f = Path(res["file"])
    if f.suffix != ".mp4":  # 通用解析器下载的直链可能没有扩展名
        f.rename(f.with_suffix(".mp4"))
    _prune()
    if progress:
        progress(f"拉取完成，用时 {time.time() - t:.0f}s")
    return {"ok": True, "src": cached_file(url)}
