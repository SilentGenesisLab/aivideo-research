"""网页正文客户端：把站外的 WebGrab 解析服务接成检索源。

WebGrab 自己按域名分流（http / tavily / 无头浏览器 / 专用接口），这里只负责
调用、并发控制和错误归一，不重复实现抓取逻辑。

配置见 config.py（.env）：WEBGRAB_BASE / WEBGRAB_TOKEN / WEBGRAB_TIMEOUT / WEBGRAB_WORKERS
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from . import config

BASE = config.WEBGRAB_BASE
TOKEN = config.WEBGRAB_TOKEN
TIMEOUT = config.WEBGRAB_TIMEOUT
WORKERS = config.WEBGRAB_WORKERS

# 页面“抓到了”但只是登录墙/风控提示：上游把它当成功返回，这里改判为失败。
# (标记, 正文长度上限)——抖音的“服务器出现问题”会出现在带正文的页面页脚里，所以只在正文很短时才判失败
_BLOCK_PAGES = (
    ("当前笔记暂时无法浏览", None),
    ("请打开小红书App扫码查看", None),
    ("你访问的页面不见了", None),
    ("服务器出现问题 请稍后重试", 3000),
)


def _block_marker(text: str | None) -> str | None:
    text = text or ""
    for marker, limit in _BLOCK_PAGES:
        if marker in text and (limit is None or len(text) < limit):
            return marker
    return None


def _vet(res: dict) -> dict:
    """登录/风控提示页不算读取成功。"""
    if not res.get("ok"):
        return res
    marker = _block_marker(res.get("text"))
    if marker:
        return {**res, "ok": False, "text": None, "error": f"抓到的是登录/风控提示页，没有正文（{marker}）"}
    return res


# 全域搜索的站点登记表（scope：定向检索时用的 site: 前缀，默认是域名；小红书、TikTok 不限定路径的话会搜出招聘页、帮助页）。
# search/read 均为 2026-11-17 在线上服务上实测（每站 1~2 条样本）：
#   search: 用 site:域名 检索，该站返回 10/10 条站内结果
#   read:   ok 能读到正文 / partial 只有部分页面或部分时候能读 / no 读不到
SITES = [
    {"key": "juejin", "name": "掘金", "group": "中文社区", "domains": ["juejin.cn"],
     "read": "ok", "via": "api", "note": "文章页走内容 API，约 0.5s"},
    {"key": "zhihu", "name": "知乎", "group": "中文社区", "domains": ["zhihu.com"],
     "read": "partial", "via": "chrome", "note": "专栏文章（zhuanlan.zhihu.com）6/6 能读，约 40~60s；问题/回答页被登录墙拦截"},
    {"key": "sspai", "name": "少数派", "group": "中文社区", "domains": ["sspai.com"],
     "read": "ok", "via": "tavily", "note": None},
    {"key": "36kr", "name": "36氪", "group": "中文社区", "domains": ["36kr.com"],
     "read": "ok", "via": "chrome", "note": "约 40~50s；正文后面会带页脚文字"},
    {"key": "infoq", "name": "InfoQ", "group": "中文社区", "domains": ["infoq.cn"],
     "read": "ok", "via": "http", "note": "xie.infoq.cn 的文章要走浏览器，约 30s"},
    {"key": "tencent_cloud", "name": "腾讯云社区", "group": "中文社区", "domains": ["cloud.tencent.com"],
     "read": "ok", "via": "tavily", "note": None},
    {"key": "csdn", "name": "CSDN", "group": "中文社区", "domains": ["csdn.net"],
     "read": "ok", "via": "chrome", "note": "约 40s；偶尔被限流"},
    {"key": "wechat", "name": "公众号", "group": "中文社区", "domains": ["mp.weixin.qq.com"],
     "read": "ok", "via": "http", "note": "6/6 读到正文，约 1~2s"},
    {"key": "toutiao", "name": "头条", "group": "中文社区", "domains": ["toutiao.com"],
     "read": "ok", "via": "tavily", "note": "6/6 读到正文；正文前后带导航文字"},
    {"key": "weibo", "name": "微博", "group": "中文社区", "domains": ["weibo.com"],
     "read": "ok", "via": "chrome", "note": "5/6 读到正文（话题页读不到），约 45~55s"},
    {"key": "jike", "name": "即刻", "group": "中文社区", "domains": ["okjike.com"],
     "read": "partial", "via": "tavily", "note": "m.okjike.com 的单条动态能读；web.okjike.com 是登录页，读不到"},
    {"key": "bilibili", "name": "B站", "group": "视频平台", "domains": ["bilibili.com"],
     "read": "ok", "via": "chrome", "note": "只有标题、简介和推荐列表，没有字幕；约 20~50s。要视频文件用 POST /resolve"},
    {"key": "douyin", "name": "抖音", "group": "视频平台", "domains": ["douyin.com"],
     "read": "ok", "via": "chrome", "note": "只有标题、作者、推荐列表；约 50s。要视频文件用 POST /resolve"},
    {"key": "xiaohongshu", "name": "小红书", "group": "视频平台", "domains": ["xiaohongshu.com"], "scope": "xiaohongshu.com/explore",
     "read": "no", "via": None, "note": "笔记页只返回登录壳（“当前笔记暂时无法浏览”），读不到正文，本服务判为失败；搜索只给标题和摘要。单条视频用 POST /resolve（链接要带 xsec_token）"},
    {"key": "youtube", "name": "YouTube", "group": "视频平台", "domains": ["youtube.com"],
     "read": "ok", "via": "tavily", "note": "返回标题、简介和字幕转录"},
    {"key": "tiktok", "name": "TikTok", "group": "视频平台", "domains": ["tiktok.com"], "scope": "tiktok.com/@",
     "read": "ok", "via": "tavily", "note": "只有视频描述。要视频文件用 POST /resolve"},
    {"key": "x", "name": "X", "group": "海外社区", "domains": ["x.com"],
     "read": "ok", "via": "api", "note": "单条推文可读；主页只读到最近几条（未登录视图）"},
    {"key": "reddit", "name": "Reddit", "group": "海外社区", "domains": ["reddit.com"],
     "read": "partial", "via": "tavily", "note": "帖子正文约 1/5 能读到，其余被拦截；搜索可用"},
    {"key": "hn", "name": "Hacker News", "group": "海外社区", "domains": ["news.ycombinator.com"],
     "read": "ok", "via": "tavily", "note": None},
    {"key": "medium", "name": "Medium", "group": "海外社区", "domains": ["medium.com"],
     "read": "ok", "via": "tavily", "note": None},
    {"key": "substack", "name": "Substack", "group": "海外社区", "domains": ["substack.com"],
     "read": "ok", "via": "tavily", "note": None},
    {"key": "linkedin", "name": "LinkedIn", "group": "海外社区", "domains": ["linkedin.com"],
     "read": "ok", "via": "tavily", "note": "Pulse 文章可读；正文后带语言列表"},
    {"key": "github", "name": "GitHub", "group": "技术", "domains": ["github.com"],
     "read": "ok", "via": "chrome", "note": "约 55s；正文前带导航文字"},
    {"key": "arxiv", "name": "arXiv", "group": "技术", "domains": ["arxiv.org"],
     "read": "ok", "via": "tavily", "note": "abs 页给摘要，html/pdf 页给全文"},
]

_BY_KEY = {s["key"]: s for s in SITES}
_BY_NAME = {s["name"].lower(): s for s in SITES}
SEARCH_WORKERS = 6


class WebGrabError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def enabled() -> bool:
    return bool(TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _handle(r: requests.Response) -> dict:
    if r.status_code == 401:
        raise WebGrabError("webgrab_unauthorized", "WebGrab token 无效或缺失")
    if r.status_code == 502:
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = r.text[:200]
        raise WebGrabError("page_unavailable", f"所有抓取通道均失败：{detail}"[:300])
    if r.status_code >= 400:
        raise WebGrabError("webgrab_error", f"WebGrab 返回 {r.status_code}：{r.text[:200]}")
    try:
        return r.json()
    except ValueError:
        raise WebGrabError("webgrab_error", "WebGrab 返回的不是 JSON")


def read(url: str, refresh: bool = False) -> dict:
    """取单页正文。返回 {url, ok, via, chars, ms, text}；失败抛 WebGrabError。"""
    if not enabled():
        raise WebGrabError("webgrab_disabled", "未配置 WEBGRAB_TOKEN")
    r = requests.get(f"{BASE}/read", params={"url": url, "refresh": str(bool(refresh)).lower()},
                     headers=_headers(), timeout=TIMEOUT)
    return _vet(_handle(r))


def read_many(urls: list[str], refresh: bool = False) -> list[dict]:
    """批量取正文，单条失败不中断。返回与 urls 同序的结果列表。"""
    if not enabled():
        raise WebGrabError("webgrab_disabled", "未配置 WEBGRAB_TOKEN")
    if len(urls) > 20:
        raise WebGrabError("too_many_urls", "单次最多 20 个 URL")
    if len(urls) == 1:
        try:
            return [read(urls[0], refresh)]
        except WebGrabError as e:
            return [{"url": urls[0], "ok": False, "error": e.message}]

    def one(u: str) -> dict:
        try:
            return read(u, refresh)
        except WebGrabError as e:
            return {"url": u, "ok": False, "error": e.message}
        except requests.RequestException as e:
            return {"url": u, "ok": False, "error": f"请求失败：{e}"[:200]}

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(urls))) as pool:
        return list(pool.map(one, urls))


def search(query: str, n: int = 10, raw: bool = True) -> list[dict]:
    """全网检索（Tavily），raw=True 时每条带全文。"""
    if not enabled():
        raise WebGrabError("webgrab_disabled", "未配置 WEBGRAB_TOKEN")
    r = requests.post(f"{BASE}/search", json={"query": query, "n": min(n, 20), "raw": raw},
                      headers=_headers(), timeout=TIMEOUT)
    items = _handle(r).get("results", [])
    for it in items:
        if _block_marker(it.get("raw_content")):
            it["raw_content"] = None
    return items


def resolve_sites(sites: list[str]) -> list[dict]:
    """把调用方传的站点 key / 中文名 / 域名 / "all" 展开成站点登记项。"""
    out, seen = [], set()

    def add(s):
        if s["key"] not in seen:
            seen.add(s["key"])
            out.append(s)

    for raw in sites or []:
        k = (raw or "").strip().lower()
        if not k:
            continue
        if k == "all":
            for s in SITES:
                add(s)
            continue
        s = _BY_KEY.get(k) or _BY_NAME.get(k)
        if s is None:
            host = re.sub(r"^https?://", "", k).split("/")[0]
            if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", host):
                raise WebGrabError("invalid_sites", f"未知站点：{raw}。可用值见 GET /pages/sites，也可以直接传域名")
            s = {"key": host, "name": host, "group": "自定义", "domains": [host], "read": None, "via": None, "note": None}
        add(s)
    if not out:
        raise WebGrabError("invalid_sites", "sites 不能为空")
    return out


def _on_site(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).netloc or "").lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in domains)


def search_sites(query: str, sites: list[str], per_site: int = 5, raw: bool = False) -> tuple[list[dict], list[dict]]:
    """全域搜索：对每个站点各做一次 site: 定向检索，并发执行，结果标上站点。

    返回 (items, by_site)。by_site 逐站说明条数、耗时和失败原因，调用方能看出哪个站没搜到，
    而不是整体看起来“成功”却少了某个站。
    """
    targets = resolve_sites(sites)

    def one(s: dict):
        t0 = time.time()
        try:
            found = search(f"site:{s.get('scope') or s['domains'][0]} {query}", per_site, raw)
        except WebGrabError as e:
            return s, [], {"ok": False, "error": e.message, "ms": int((time.time() - t0) * 1000)}
        except requests.RequestException as e:
            return s, [], {"ok": False, "error": f"请求失败：{e}"[:200], "ms": int((time.time() - t0) * 1000)}
        items = []
        for it in found:
            if _on_site(it.get("url") or "", s["domains"]):
                items.append({**it, "site": s["key"], "site_name": s["name"]})
        return s, items[:per_site], {"ok": True, "error": None, "ms": int((time.time() - t0) * 1000)}

    items, by_site = [], []
    with ThreadPoolExecutor(max_workers=min(SEARCH_WORKERS, len(targets))) as pool:
        for s, got, st in pool.map(one, targets):
            items.extend(got)
            by_site.append({"site": s["key"], "name": s["name"], "count": len(got), **st})
    if by_site and not any(b["ok"] for b in by_site):
        raise WebGrabError("search_failed", "所有站点检索都失败：" + (by_site[0]["error"] or ""))
    seen, uniq = set(), []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            uniq.append(it)
    return uniq, by_site


def health() -> dict:
    r = requests.get(f"{BASE}/health", timeout=10)
    return _handle(r)


# 能力矩阵（/pages/sources）：由 SITES 生成，保持旧字段 site / ok / via / note
CAPABILITIES = [
    {"site": s["name"], "ok": s["read"] != "no", "via": s["via"],
     "note": (("部分可读：" if s["read"] == "partial" else "") + (s["note"] or "")) or None}
    for s in SITES
]
