"""网页正文客户端：把站外的 WebGrab 解析服务接成检索源。

WebGrab 自己按域名分流（http / tavily / 无头浏览器 / 专用接口），这里只负责
调用、并发控制和错误归一，不重复实现抓取逻辑。

配置见 config.py（.env）：WEBGRAB_BASE / WEBGRAB_TOKEN / WEBGRAB_TIMEOUT / WEBGRAB_WORKERS
"""
from concurrent.futures import ThreadPoolExecutor

import requests

from . import config

BASE = config.WEBGRAB_BASE
TOKEN = config.WEBGRAB_TOKEN
TIMEOUT = config.WEBGRAB_TIMEOUT
WORKERS = config.WEBGRAB_WORKERS

_UNSUPPORTED = ("知乎", "即刻")


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
    return _handle(r)


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
    return _handle(r).get("results", [])


def health() -> dict:
    r = requests.get(f"{BASE}/health", timeout=10)
    return _handle(r)


# 能力矩阵：告诉调用方哪些站能读、走什么通道。与 WebGrab README 保持一致。
CAPABILITIES = [
    {"site": "公众号", "ok": True, "via": "http"},
    {"site": "少数派", "ok": True, "via": "tavily"},
    {"site": "InfoQ", "ok": True, "via": "http"},
    {"site": "腾讯云社区", "ok": True, "via": "tavily"},
    {"site": "36氪", "ok": True, "via": "chrome"},
    {"site": "CSDN", "ok": True, "via": "chrome", "note": "需登录态"},
    {"site": "小红书", "ok": True, "via": "chrome", "note": "需登录态"},
    {"site": "B站", "ok": True, "via": "chrome"},
    {"site": "微博", "ok": True, "via": "chrome"},
    {"site": "头条", "ok": True, "via": "tavily"},
    {"site": "抖音", "ok": True, "via": "chrome"},
    {"site": "掘金", "ok": True, "via": "api", "note": "文章页走内容 API"},
    {"site": "GitHub", "ok": True, "via": "chrome"},
    {"site": "arXiv", "ok": True, "via": "tavily"},
    {"site": "Substack", "ok": True, "via": "tavily"},
    {"site": "Hacker News", "ok": True, "via": "tavily"},
    {"site": "Medium", "ok": True, "via": "tavily"},
    {"site": "Reddit", "ok": True, "via": "chrome"},
    {"site": "LinkedIn", "ok": True, "via": "tavily"},
    {"site": "YouTube", "ok": True, "via": "tavily", "note": "返回字幕转录"},
    {"site": "X", "ok": True, "via": "api", "note": "仅单条推文"},
    {"site": "知乎", "ok": False, "via": None, "note": "登录墙 403"},
    {"site": "即刻", "ok": False, "via": None, "note": "需 App 登录"},
    {"site": "X 主页/时间线", "ok": False, "via": None, "note": "登录墙"},
]
