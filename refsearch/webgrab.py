"""网页正文客户端：把站外的 WebGrab 解析服务接成检索源。

WebGrab 自己按域名分流（http / tavily / 无头浏览器 / 专用接口），这里只负责
调用、并发控制和错误归一，不重复实现抓取逻辑。

配置见 config.py（.env）：WEBGRAB_BASE / WEBGRAB_TOKEN / WEBGRAB_TIMEOUT / WEBGRAB_WORKERS
"""
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import urlparse

import requests

from . import config

BASE = config.WEBGRAB_BASE
TOKEN = config.WEBGRAB_TOKEN
TIMEOUT = config.WEBGRAB_TIMEOUT
PROVIDER = config.SEARCH_PROVIDER
BOCHA_KEY = config.BOCHA_API_KEY
BOCHA_BASE = config.BOCHA_BASE
BOCHA_TIMEOUT = config.BOCHA_TIMEOUT
BOCHA_WORKERS = config.BOCHA_WORKERS
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
SEARCH_WORKERS = config.WEBGRAB_SEARCH_WORKERS
SEARCH_BUDGET = config.WEBGRAB_SEARCH_BUDGET

# 每站一次 Tavily 调用，站点数直接决定成本：24 站 × 2 credits(advanced) = 48 credits/次搜索，
# 4000/月的额度只够搜 83 次。`"all"` 默认只展开这 8 个核心站（覆盖中英文、视频/社区/长文），
# 其余站点仍可按 key 显式指定。用 .env 的 WEBGRAB_CORE_SITES 覆盖。
DEFAULT_CORE = ["bilibili", "douyin", "zhihu", "xiaohongshu",
                "youtube", "reddit", "x", "wechat"]
CORE = [k.strip() for k in (config.WEBGRAB_CORE_SITES or "").split(",") if k.strip()] or DEFAULT_CORE
CORE = [k for k in CORE if k in _BY_KEY] or DEFAULT_CORE


class WebGrabError(Exception):
    def __init__(self, code: str, message: str, error_class: str = "upstream_fail"):
        self.code, self.message, self.error_class = code, message, error_class
        super().__init__(message)


def enabled() -> bool:
    if PROVIDER in ("bocha", "hybrid"):
        return bool(BOCHA_KEY)
    return bool(TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _snip(r: requests.Response, n: int = 200) -> str:
    try:
        return r.text[:n]
    except Exception:
        return ""


def _looks_json(r: requests.Response) -> bool:
    """nginx 的 502 是 HTML，WebGrab 自己抛的是 FastAPI JSON。"""
    return _snip(r, 1).lstrip().startswith("{")


def _handle(r: requests.Response, endpoint: str = "read") -> dict:
    """把 HTTP 状态翻译成带 error_class 的异常。

    状态语义按端点不同（读 WebGrab server.py 核实）：
      /search  Tavily 异常是裸 raise → 500；TAVILY_KEY 未配 → 503；鉴权 → 401
      /read    通道全失败 → 502（FastAPI JSON）
      nginx 够不到 8907 → 502（HTML）；nginx 等满 300s → 504
    所以 502 在 /search 上意味着请求没到 WebGrab，不是上游质量差。
    """
    s = r.status_code
    if s == 401:
        raise WebGrabError("webgrab_unauthorized", "WebGrab token 无效或缺失", "config_error")
    if s == 503:
        raise WebGrabError("webgrab_unconfigured", f"WebGrab 未配置：{_snip(r)}"[:300], "config_error")
    if s == 504:
        raise WebGrabError("webgrab_timeout", "WebGrab 超时（nginx 300s 内未返回）", "timeout")
    if s == 502:
        if endpoint == "search" or not _looks_json(r):
            raise WebGrabError("webgrab_unreachable", "WebGrab 服务不可达（nginx 502）", "never_arrived")
        raise WebGrabError("page_unavailable", f"所有抓取通道均失败：{_snip(r)}"[:300], "upstream_fail")
    if s >= 500:
        raise WebGrabError("webgrab_upstream_fail",
                           f"WebGrab 内部错误 {s}（响应体不含原因，需查 WebGrab 日志）", "upstream_fail")
    if s >= 400:
        raise WebGrabError("webgrab_error", f"WebGrab 返回 {s}：{_snip(r)}", "invalid")
    try:
        return r.json()
    except ValueError:
        raise WebGrabError("webgrab_error", "WebGrab 返回的不是 JSON", "upstream_fail")


def read(url: str, refresh: bool = False) -> dict:
    """取单页正文。返回 {url, ok, via, chars, ms, text}；失败抛 WebGrabError。"""
    if not TOKEN:
        raise WebGrabError("webgrab_disabled", "未配置 WEBGRAB_TOKEN", "config_error")
    r = requests.get(f"{BASE}/read", params={"url": url, "refresh": str(bool(refresh)).lower()},
                     headers=_headers(), timeout=TIMEOUT)
    return _vet(_handle(r, "read"))


def read_many(urls: list[str], refresh: bool = False) -> list[dict]:
    """批量取正文，单条失败不中断。返回与 urls 同序的结果列表。"""
    if not TOKEN:
        raise WebGrabError("webgrab_disabled", "未配置 WEBGRAB_TOKEN", "config_error")
    if len(urls) > 20:
        raise WebGrabError("too_many_urls", "单次最多 20 个 URL", "invalid")
    if len(urls) == 1:
        try:
            return [read(urls[0], refresh)]
        except WebGrabError as e:
            return [{"url": urls[0], "ok": False, "error": e.message, "error_class": e.error_class}]

    def one(u: str) -> dict:
        try:
            return read(u, refresh)
        except WebGrabError as e:
            return {"url": u, "ok": False, "error": e.message, "error_class": e.error_class}
        except requests.Timeout:
            return {"url": u, "ok": False, "error": "请求超时", "error_class": "timeout"}
        except requests.RequestException as e:
            return {"url": u, "ok": False, "error": f"请求失败：{e}"[:200], "error_class": "never_arrived"}

    with ThreadPoolExecutor(max_workers=min(WORKERS, len(urls))) as pool:
        return list(pool.map(one, urls))


def _tavily(query: str, n: int, raw: bool, timeout: float | None) -> list[dict]:
    """经 WebGrab 的 /search 走 Tavily。"""
    if not TOKEN:
        raise WebGrabError("webgrab_disabled", "未配置 WEBGRAB_TOKEN", "config_error")
    r = requests.post(f"{BASE}/search", json={"query": query, "n": min(n, 20), "raw": raw},
                      headers=_headers(), timeout=TIMEOUT if timeout is None else timeout)
    items = _handle(r, "search").get("results", [])
    for it in items:
        it["provider"] = "tavily"
        if _block_marker(it.get("raw_content")):
            it["raw_content"] = None
    return items


def _bocha(query: str, n: int, raw: bool, timeout: float | None) -> list[dict]:
    """直连博查 web-search。

    与 Tavily 的差别要注意：博查的 summary 是**模型生成的摘要**，不是网页正文；
    Tavily 的 raw_content 是抓到的正文。同样叫 raw，语义不同，别混用。

    site: 限定博查是硬约束（实测 24 站站内率 100%），Tavily 是软约束会掺站外结果。
    """
    if not BOCHA_KEY:
        raise WebGrabError("bocha_disabled", "未配置 BOCHA_API_KEY", "config_error")
    body = {"query": query, "count": min(n, 20), "summary": bool(raw)}
    # 博查限并发，偶发 429；退避重试两次，实测能吸收掉突发
    for attempt in range(3):
        try:
            r = requests.post(BOCHA_BASE, json=body,
                              timeout=BOCHA_TIMEOUT if timeout is None else timeout,
                              headers={"Authorization": f"Bearer {BOCHA_KEY}",
                                       "Content-Type": "application/json"})
        except requests.Timeout:
            raise WebGrabError("bocha_timeout", "博查请求超时", "timeout")
        except requests.RequestException as e:
            raise WebGrabError("bocha_unreachable", f"博查不可达：{e}"[:200], "never_arrived")
        if r.status_code != 429 or attempt == 2:
            break
        time.sleep(0.6 * (attempt + 1))

    if r.status_code in (401, 403):
        raise WebGrabError("bocha_unauthorized", "博查 key 无效或无权限", "config_error")
    if r.status_code == 429:
        raise WebGrabError("bocha_rate_limited", "博查限流（429）", "rate_limited")
    if r.status_code >= 500:
        raise WebGrabError("bocha_upstream_fail", f"博查服务错误 {r.status_code}", "upstream_fail")
    if r.status_code >= 400:
        raise WebGrabError("bocha_error", f"博查返回 {r.status_code}：{r.text[:200]}", "invalid")
    try:
        j = r.json()
    except ValueError:
        raise WebGrabError("bocha_error", "博查返回的不是 JSON", "upstream_fail")
    # 博查出错时 HTTP 仍是 200，靠 body 里的 code 区分
    if j.get("code") not in (200, None):
        raise WebGrabError("bocha_error", f"博查返回 code={j.get('code')}：{str(j.get('msg'))[:200]}",
                           "upstream_fail")
    vals = (((j.get("data") or {}).get("webPages") or {}).get("value")) or []
    out = []
    for v in vals:
        out.append({"title": v.get("name") or "",
                    "url": v.get("url") or "",
                    "content": v.get("snippet") or "",
                    "raw_content": (v.get("summary") or None) if raw else None,
                    "score": None, "provider": "bocha"})
    for it in out:
        if _block_marker(it.get("raw_content")):
            it["raw_content"] = None
    return out


def search(query: str, n: int = 10, raw: bool = True, timeout: float | None = None) -> list[dict]:
    """检索。后端由 SEARCH_PROVIDER 决定：bocha（默认）/ tavily / hybrid。

    hybrid：先用博查，该站空结果时再回落 Tavily——用于补博查覆盖不到的站（知乎/X/Medium）。
    """
    if PROVIDER == "tavily":
        return _tavily(query, n, raw, timeout)
    if PROVIDER == "hybrid":
        items = _bocha(query, n, raw, timeout)
        if items:
            return items
        return _tavily(query, n, raw, timeout)
    return _bocha(query, n, raw, timeout)


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
            # 每站一次计费调用。Tavily advanced 每次 2 credits（24 站 = 48），
            # 博查每次 1 次额度且总额 5000——所以只在 tavily 后端收窄到核心站。
            for key in (CORE if PROVIDER == "tavily" else list(_BY_KEY)):
                add(_BY_KEY[key])
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


def search_sites(query: str, sites: list[str], per_site: int = 5, raw: bool = False,
                 budget: float | None = None,
                 batch_id: str | None = None) -> tuple[list[dict], list[dict]]:
    """全域搜索：对每个站点各做一次 site: 定向检索，并发执行，结果标上站点。

    返回 (items, by_site)。by_site 逐站说明条数、耗时、失败原因和 error_class，
    调用方能看出哪个站没搜到，而不是整体看起来“成功”却少了某个站。

    error_class 分档（与 WebGrab 侧日志可对账；None 表示正常）：
      empty_result      上游返回 0 条（真空返回）
      dropped_by_filter 上游有结果但全被域名过滤丢弃——**不归因**，可能是上游无视 site:
                        返回站外，也可能是过滤规则与该站 URL 形态不符，要判哪种得看原始 URL
      never_arrived  请求没到 WebGrab（连接失败 / nginx 502）
      timeout        预算内没拿回来（客户端超时或 nginx 504）
      upstream_fail  到了但上游失败（/search 的 500）
      config_error   token / TAVILY_KEY 等配置问题
      invalid        其它 4xx

    注意 count 是**域名过滤后**的条数，raw_count 才是上游原始条数。
    别把它俩混用：上游日志里的 count 对应 raw_count。

    每站独立一次请求，24 站就是 24 次调用；整批共用 batch_id 和总预算 budget 秒。
    """
    targets = resolve_sites(sites)
    budget = SEARCH_BUDGET if budget is None else budget
    batch_id = batch_id or uuid.uuid4().hex[:12]
    deadline = time.time() + budget

    def one(s: dict):
        t0 = time.time()
        label = f"site:{s.get('scope') or s['domains'][0]} {query}"
        try:
            found = search(label, per_site, raw, timeout=min(TIMEOUT, max(1.0, deadline - t0)))
        except WebGrabError as e:
            return s, [], {"ok": False, "count": 0, "error": e.message,
                           "error_class": e.error_class, "ms": int((time.time() - t0) * 1000),
                           "start_ts": t0}
        except requests.Timeout:
            return s, [], {"ok": False, "count": 0, "error": "请求超时", "error_class": "timeout",
                           "ms": int((time.time() - t0) * 1000), "start_ts": t0}
        except requests.RequestException as e:
            return s, [], {"ok": False, "count": 0, "error": f"请求失败：{e}"[:200],
                           "error_class": "never_arrived", "ms": int((time.time() - t0) * 1000),
                           "start_ts": t0}
        items, raw_n = [], len(found)
        for it in found:
            if _on_site(it.get("url") or "", s["domains"]):
                items.append({**it, "site": s["key"], "site_name": s["name"]})
        items = items[:per_site]
        via = (items[0].get("provider") if items else None) or "none"
        # raw_count 是上游原始条数，count 是域名过滤后的条数。
        # 二者不等 = 结果被过滤器丢掉了，但**不归因**：可能是上游无视 site: 返回站外，
        # 也可能是 _on_site 的匹配规则与该站实际 URL 形态不符。要判哪种，只能看原始 URL。
        if items:
            cls, err = None, None
        elif raw_n:
            cls, err = "dropped_by_filter", "上游返回 %d 条但都被域名过滤丢弃（原因未判，需看原始 URL）" % raw_n
        else:
            cls, err = "empty_result", "该站 0 条结果"
        return s, items, {"ok": True, "count": len(items), "raw_count": raw_n, "provider": via,
                          "error": err, "error_class": cls,
                          "ms": int((time.time() - t0) * 1000), "start_ts": t0}

    submit_ts, futs, by_key, found_items = {}, {}, {}, []
    n_workers = BOCHA_WORKERS if PROVIDER == "bocha" else SEARCH_WORKERS
    pool = ThreadPoolExecutor(max_workers=min(n_workers, len(targets)))
    try:
        for s in targets:
            submit_ts[s["key"]] = time.time()
            futs[pool.submit(one, s)] = s
        done, pending = wait(futs, timeout=budget)
    finally:
        pool.shutdown(wait=False)  # 未完成的线程让它在后台跑完，不阻塞响应

    for f in done:
        s = futs[f]
        try:
            s, got, st = f.result()
        except Exception as e:  # noqa: BLE001 - 单站意外错误不该让整批 500
            st, got = {"ok": False, "count": 0, "error": f"{type(e).__name__}: {str(e)[:160]}",
                       "error_class": "upstream_fail", "start_ts": submit_ts[s["key"]]}, []
            st["ms"] = int((time.time() - st["start_ts"]) * 1000)
        by_key[s["key"]] = {**st, "site": s["key"], "name": s["name"], "batch_id": batch_id,
                            "end_ts": st["start_ts"] + st["ms"] / 1000}
        for it in got:
            it["batch_id"] = batch_id
            found_items.append(it)
    for f in pending:
        s = futs[f]
        t = submit_ts[s["key"]]
        by_key[s["key"]] = {"site": s["key"], "name": s["name"], "batch_id": batch_id,
                            "ok": False, "count": 0, "error_class": "timeout",
                            "error": f"超出 {int(budget)}s 总预算仍未返回",
                            "ms": int((time.time() - t) * 1000), "start_ts": t, "end_ts": time.time()}

    by_site = [by_key[s["key"]] for s in targets if s["key"] in by_key]
    if by_site and not any(b["ok"] for b in by_site):
        e = WebGrabError("search_failed", "所有站点检索都失败：" + (by_site[0]["error"] or ""),
                         by_site[0].get("error_class") or "upstream_fail")
        e.by_site = by_site  # 整批失败时 by_site 才是最有价值的东西，调用方要拿它落库
        raise e
    seen, uniq = set(), []
    for it in found_items:
        if it["url"] not in seen:
            seen.add(it["url"])
            uniq.append(it)
    return uniq, by_site


WEB_KEY = "_web"
WEB_NAME = "全网通用"


def search_default(query: str, per_site: int = 5, n_web: int = 10, raw: bool = False,
                   budget: float | None = None) -> tuple[list[dict], list[dict]]:
    """默认检索：核心站定向 + 一次通用全网，两类结果用每条的 source 字段区分。

      source="site"  定向：每站一次 site: 查询，覆盖登记平台（B站/抖音/小红书/知乎…），
                     用来找"某个平台上"的具体内容
      source="web"   通用：一次不限站点的查询，用来捞素材、灵感、行业通稿这类
                     散落在任意站点的内容——单靠定向会漏掉它们

    只有**两类都失败**才抛异常；一类失败时另一类照常返回，失败方在 by_site 里标出来。
    """
    batch_id = uuid.uuid4().hex[:12]
    budget = SEARCH_BUDGET if budget is None else budget
    items, by_site = [], []
    site_err = web_err = None

    try:
        sitems, by_site = search_sites(query, CORE, per_site, raw, budget, batch_id=batch_id)
        for it in sitems:
            it["source"] = "site"
            items.append(it)
    except WebGrabError as e:
        site_err = e
        by_site = list(getattr(e, "by_site", []) or [])

    t0 = time.time()
    try:
        wit = search(query, n_web, raw)
        for it in wit:
            it["source"] = "web"
            it["site"] = None
            it["site_name"] = None
            it["batch_id"] = batch_id
            items.append(it)
        by_site.append({"site": WEB_KEY, "name": WEB_NAME, "batch_id": batch_id, "ok": True,
                        "count": len(wit), "raw_count": len(wit), "error": None, "error_class": None,
                        "provider": (wit[0].get("provider") if wit else PROVIDER),
                        "ms": int((time.time() - t0) * 1000), "start_ts": t0, "end_ts": time.time()})
    except WebGrabError as e:
        web_err = e
        by_site.append({"site": WEB_KEY, "name": WEB_NAME, "batch_id": batch_id, "ok": False,
                        "count": 0, "raw_count": 0, "error": e.message, "error_class": e.error_class,
                        "provider": None, "ms": int((time.time() - t0) * 1000),
                        "start_ts": t0, "end_ts": time.time()})

    if site_err and web_err:
        err = WebGrabError("search_failed", "定向站与通用检索都失败：" + site_err.message[:120],
                           site_err.error_class)
        err.by_site = by_site
        raise err

    seen, uniq = set(), []
    for it in items:  # 定向在前，去重时优先保留带站点标注的那条
        u = it.get("url") or ""
        if u and u not in seen:
            seen.add(u)
            uniq.append(it)
    return uniq, by_site


def health() -> dict:
    r = requests.get(f"{BASE}/health", timeout=10)
    return _handle(r, "health")


# 能力矩阵（/pages/sources）：由 SITES 生成，保持旧字段 site / ok / via / note
CAPABILITIES = [
    {"site": s["name"], "ok": s["read"] != "no", "via": s["via"],
     "note": (("部分可读：" if s["read"] == "partial" else "") + (s["note"] or "")) or None}
    for s in SITES
]
