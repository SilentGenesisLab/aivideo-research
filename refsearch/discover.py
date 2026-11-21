"""按账号 / 关键词发现视频，并按互动量排序。

三种来源：
- **账号主页**（TikTok / YouTube）：yt-dlp 列表接口，一次拿一批。
- **YouTube 关键词**：yt-dlp 的 ytsearch，稳定性一般（同一关键词每次结果都在变）。
- **TikTok 关键词**：yt-dlp 的话题标签提取器已被上游标记为 broken（TikTokTagIE._WORKING=False），
  所以走无头浏览器打开 tiktok.com/search，让页面自己的 JS 生成 X-Bogus 签名去请求
  /api/search/general/full/，我们只拦截响应。滚动可翻页，一次约能拿 12～120 条。
  注意：该接口按出口 IP 限流，同一 IP 连跑几次就会返回空，所以要串行 + 缓存 + 换地区。

结果落在 runs/_discover/<id>/，与 /searches 的结果目录分开，不进「检索结果」列表。
"""
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import config

DIR = config.RUNS_DIR / "_discover"
MAX_ACCOUNTS = 30
MAX_KEYWORDS = 10
MAX_PER_SOURCE = 50
MAX_LIMIT = 200
MAX_JOBS = 120
WORKERS = 4
TIMEOUT = 240
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
SORTS = {"views": "view_count", "likes": "like_count", "comments": "comment_count", "recent": "timestamp"}

REGION_LABELS = {
    "sea": "东南亚 · 泰国 曼谷", "eu": "欧美 · 英国 伦敦", "jp": "日本 · 东京",
    "kr": "韩国 · 首尔", "br": "南美 · 巴西 圣保罗", "mx": "拉美 · 墨西哥城",
    "au": "澳洲 · 悉尼", "ng": "非洲 · 尼日利亚 埃基蒂",
}
# 浏览器时区跟着出口走，减少"这个 IP 在别处"的违和信号
REGION_TZ = {
    "sea": "Asia/Bangkok", "eu": "Europe/London", "jp": "Asia/Tokyo", "kr": "Asia/Seoul",
    "br": "America/Sao_Paulo", "mx": "America/Mexico_City", "au": "Australia/Sydney",
    "ng": "Africa/Lagos",
}

# 地区别名 → 出口 id
REGION_ALIASES = {
    "sea": ["sea"], "southeast_asia": ["sea"], "东南亚": ["sea"],
    "eu": ["eu"], "eu_us": ["eu"], "europe": ["eu"], "欧美": ["eu"],
    "jp": ["jp"], "japan": ["jp"], "日本": ["jp"],
    "kr": ["kr"], "korea": ["kr"], "韩国": ["kr"],
    "jp_kr": ["jp", "kr"], "japan_korea": ["jp", "kr"], "日韩": ["jp", "kr"],
    "latam": ["br", "mx"], "南美": ["br"], "south_america": ["br"],
    "br": ["br"], "mx": ["mx"],
    "africa": ["ng"], "ng": ["ng"], "非洲": ["ng"],
    "au": ["au"], "oceania": ["au"], "澳洲": ["au"],
}

TK_CACHE_TTL = 1800  # TikTok 关键词结果缓存 30 分钟，避免反复触发限流
TK_BLOCK_TTL = 900   # 某出口被判限流后，15 分钟内不再优先尝试
_TK_LOCK = threading.Lock()      # 一台机器内存有限，浏览器搜索串行执行
_TK_CACHE: dict[tuple, tuple] = {}
_TK_BLOCKED: dict[str | None, float] = {}


class DiscoverError(RuntimeError):
    pass


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def region_list() -> list[dict]:
    """当前配置了哪些区域出口。"""
    return [{"id": k, "label": REGION_LABELS.get(k, k)} for k in config.REGION_PROXY]


def expand_regions(value) -> list[str | None]:
    """region 参数 → 出口 id 列表；没传返回 [None]，表示用平台默认出口。"""
    if not value:
        return [None]
    ids: list[str] = []
    for part in str(value).replace(" ", "").split(","):
        if not part:
            continue
        if part == "all":
            cand = list(config.REGION_PROXY)
        else:
            cand = REGION_ALIASES.get(part, [part])
        for c in cand:
            if c not in ids:
                ids.append(c)
    return ids or [None]


def _proxy(platform: str, region: str | None = None) -> str:
    if region:
        return config.region_proxy(region, platform)
    return config.proxy_for(platform) if platform in config.PROXY_PLATFORMS else ""


def _cmd(platform: str, extra: list[str], region: str | None = None) -> list[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--socket-timeout", "30"]
    if p := _proxy(platform, region):
        cmd += ["--proxy", p]
    if platform == "tiktok":
        cmd += ["--user-agent", UA]
    return cmd + extra


def _clean(err: str) -> str:
    err = re.sub(r"^\s*ERROR:\s*", "", err or "").strip()
    err = re.sub(r"\[[a-z:]+?\]\s*", "", err)
    return err[:300] or "yt-dlp 失败"


def _item(e: dict, platform: str) -> dict | None:
    vid = e.get("id")
    if not vid:
        return None
    page = e.get("webpage_url") or e.get("url") or ""
    if platform == "tiktok":
        name = "TikTok"
        if not page.startswith("http"):
            up = e.get("uploader_id") or e.get("channel") or ""
            page = (f"https://www.tiktok.com/@{up}/video/{vid}" if up
                    else f"https://www.tiktok.com/video/{vid}")
    else:
        name = "YouTube"
        if not page.startswith("http"):
            page = f"https://www.youtube.com/watch?v={vid}"
    return {
        "platform": name,
        "id": str(vid),
        "url": page,
        "title": (e.get("title") or e.get("description") or "")[:200],
        "author": e.get("uploader") or e.get("channel") or "",
        "duration": e.get("duration"),
        "timestamp": e.get("timestamp"),
        "cover": e.get("thumbnail") or "",
        "view_count": e.get("view_count") or 0,
        "like_count": e.get("like_count") or 0,
        "comment_count": e.get("comment_count") or 0,
        "repost_count": e.get("repost_count") or 0,
    }


def _fetch(url: str, platform: str, limit: int, region: str | None) -> list[dict]:
    cmd = _cmd(platform, ["-J", "--flat-playlist", "--playlist-end", str(limit), url], region)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    if p.returncode != 0:
        raise DiscoverError(_clean((p.stderr.strip().splitlines() or [""])[-1]))
    try:
        data = json.loads(p.stdout)
    except ValueError:
        raise DiscoverError("yt-dlp 返回的不是 JSON")
    entries = data.get("entries") if data.get("_type") == "playlist" else [data]
    return [x for x in (_item(e, platform) for e in (entries or []) if e) if x]


# ---------------- TikTok 关键词：无头浏览器 + 拦截搜索接口 ----------------

def _tk_item(it: dict) -> dict | None:
    vid = it.get("id")
    if not vid:
        return None
    v = it.get("video") or {}
    st = it.get("stats") or {}
    a = it.get("author") or {}
    uid = a.get("uniqueId") or ""
    return {
        "platform": "TikTok",
        "id": str(vid),
        "url": f"https://www.tiktok.com/@{uid}/video/{vid}",
        "title": (it.get("desc") or "")[:200],
        "author": uid or a.get("nickname") or "",
        "duration": v.get("duration"),
        "timestamp": it.get("createTime"),
        "cover": v.get("cover") or "",
        "view_count": st.get("playCount") or 0,
        "like_count": st.get("diggCount") or 0,
        "comment_count": st.get("commentCount") or 0,
        "repost_count": st.get("shareCount") or 0,
    }


def _tk_browser_search(query: str, limit: int, region: str | None) -> list[dict]:
    from .resolve import _chrome_path
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise DiscoverError("服务器未安装 playwright，无法做 TikTok 关键词检索")

    proxy = _proxy("tiktok", region)
    items: dict[str, dict] = {}

    def on_response(r):
        if "/api/search/general/full/" not in r.url:
            return
        try:
            d = r.json()
        except Exception:
            return
        for x in (d.get("data") or []):
            if isinstance(x, dict) and x.get("type") == 1 and x.get("item"):
                if (it := _tk_item(x["item"])):
                    items.setdefault(it["id"], it)

    with _TK_LOCK, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path=_chrome_path(),
            proxy={"server": proxy} if proxy else None,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"])
        try:
            ctx = browser.new_context(
                user_agent=UA, locale="en-US", viewport={"width": 1440, "height": 900},
                timezone_id=REGION_TZ.get(region or "", "UTC"))
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            page = ctx.new_page()
            page.on("response", on_response)
            page.goto(f"https://www.tiktok.com/search?q={query}",
                      wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(8000)
            # 滚动触发翻页；结果够了就停
            for _ in range(10):
                if len(items) >= limit:
                    break
                page.mouse.move(700, 500)
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1100)
                page.keyboard.press("End")
                page.wait_for_timeout(1100)
                page.evaluate(
                    "() => { window.scrollTo(0,1e9);"
                    " [...document.querySelectorAll('*')].forEach(e => {"
                    "   if (e.scrollHeight > e.clientHeight + 100) e.scrollTop = e.scrollHeight; }); }")
                page.wait_for_timeout(1100)
        finally:
            browser.close()

    return list(items.values())


def _tk_keyword(query: str, limit: int, region: str | None) -> list[dict]:
    """没指定地区时自动挑一个没被限流的出口；指定的地区被限流就如实返回空。"""
    key = (query.lower(), region)
    if (hit := _TK_CACHE.get(key)) and time.time() - hit[0] < TK_CACHE_TTL:
        return hit[1][:limit]

    if region:
        tries = [region]
    else:
        now = time.time()
        pool = [None] + list(config.REGION_PROXY)
        tries = [r for r in pool if now - _TK_BLOCKED.get(r, 0) > TK_BLOCK_TTL][:3] or pool[:1]

    for r in tries:
        items = _tk_browser_search(query, limit, r)
        if items:
            _TK_BLOCKED.pop(r, None)
            for it in items:
                it["region"] = r or ""
            _TK_CACHE[key] = (time.time(), items)
            return items[:limit]
        _TK_BLOCKED[r] = time.time()  # 空响应就是被限流，换下一个出口
    return []


def account_url(platform: str, account: str) -> str:
    a = account.strip()
    if a.startswith(("http://", "https://")):
        return a
    h = a.lstrip("@")
    if platform == "tiktok":
        return f"https://www.tiktok.com/@{h}"
    if h.startswith("UC") and len(h) == 24:  # 频道 ID 不是 handle
        return f"https://www.youtube.com/channel/{h}/videos"
    return f"https://www.youtube.com/@{h}/videos"


def _jobs(spec: dict) -> list[tuple[str, str, str, int, str | None]]:
    """→ [(标签, 平台, 目标, 条数, 出口)]；以 tksearch: 开头的目标走浏览器检索。"""
    platforms = spec.get("platforms") or ["tiktok", "youtube"]
    per = min(spec.get("per_source") or 30, MAX_PER_SOURCE)
    regions = expand_regions(spec.get("region"))
    keywords = [k.strip() for k in (spec.get("keywords") or [])[:MAX_KEYWORDS] if k.strip()]
    jobs: list[tuple[str, str, str, int, str | None]] = []
    for region in regions:
        tag = "" if region is None else f"[{region}]"
        for acc in (spec.get("accounts") or [])[:MAX_ACCOUNTS]:
            for plat in platforms:
                jobs.append((f"{plat}:{acc}{tag}", plat, account_url(plat, acc), per, region))
        for kw in keywords:
            if "tiktok" in platforms:
                jobs.append((f"tiktok:search:{kw}{tag}", "tiktok", "tksearch:" + kw, per, region))
            if "youtube" in platforms:
                jobs.append((f"youtube:search:{kw}{tag}", "youtube", f"ytsearch{per}:{kw}", per, region))
    return jobs[:MAX_JOBS]


def _run_job(url: str, platform: str, n: int, region: str | None) -> list[dict]:
    if url.startswith("tksearch:"):
        return _tk_keyword(url[len("tksearch:"):], n, region)
    return _fetch(url, platform, n, region)


def _sort_key(sort: str):
    field = SORTS.get(sort, "view_count")
    if field == "timestamp":
        return lambda i: (i.get("timestamp") or 0)
    return lambda i: (i.get(field) or 0)


def discover(spec: dict, progress=lambda m: None) -> dict:
    did = spec.get("discover_id") or new_id()
    jobs = _jobs(spec)
    if not jobs:
        raise DiscoverError("accounts 与 keywords 至少传一个")

    found: dict[tuple[str, str], dict] = {}
    errors: list[dict] = []
    done = 0
    with ThreadPoolExecutor(WORKERS) as pool:
        futs = {pool.submit(_run_job, url, plat, n, region): (label, plat, region, url)
                for label, plat, url, n, region in jobs}
        for fut in as_completed(futs):
            label, plat, region, url = futs[fut]
            done += 1
            try:
                got = fut.result()
                if not got and url.startswith("tksearch:"):
                    errors.append({"source": label, "error":
                                   "0 条：TikTok 搜索接口按出口 IP 限流，请稍后重试或换一个 region"})
                for it in got:
                    it["source"] = "search" if ":search:" in label or ":tag:" in label else "account"
                    it["found_by"] = label
                    key = (it["platform"], it["id"])
                    if prev := found.get(key):
                        if region and region not in prev["regions"]:
                            prev["regions"].append(region)
                        continue
                    it.setdefault("region", region or "")
                    it["regions"] = [it["region"]] if it["region"] else []
                    found[key] = it
            except Exception as e:  # 单个来源失败不影响其它
                errors.append({"source": label, "error": str(e)[:300]})
            progress(f"发现：{done}/{len(jobs)} 个来源完成，已收 {len(found)} 条")

    items = list(found.values())
    if spec.get("min_views"):
        items = [i for i in items if (i.get("view_count") or 0) >= spec["min_views"]]
    items.sort(key=_sort_key(spec.get("sort") or "views"), reverse=True)
    items = items[:min(spec.get("limit") or 50, MAX_LIMIT)]

    rec = {
        "discover_id": did,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "client": spec.get("who", ""),
        "spec": {k: v for k, v in spec.items() if k not in ("who", "base", "discover_id")},
        "count": len(items),
        "items": items,
        "errors": errors,
        "downloads": [],
    }
    save(rec)
    progress(f"发现完成：{len(items)} 条")
    return rec


def save(rec: dict) -> None:
    d = DIR / rec["discover_id"]
    d.mkdir(parents=True, exist_ok=True)
    (d / "items.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def load(discover_id: str) -> dict:
    d = (DIR / discover_id).resolve()
    if not d.is_relative_to(DIR.resolve()) or not (d / "items.json").is_file():
        raise FileNotFoundError(discover_id)
    return json.loads((d / "items.json").read_text(encoding="utf-8"))


def videos_dir(discover_id: str) -> Path:
    return DIR / discover_id / "videos"
