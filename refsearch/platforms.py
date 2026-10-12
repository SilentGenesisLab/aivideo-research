"""参考平台：每个平台说明"参考什么"。能从服务器抓到结果的平台（fetch）直接给出作品地址，
需登录或拒绝服务器访问的平台只给搜索页链接。"""
import re
from urllib.parse import quote, quote_plus

import requests

from . import searchers
from .searchers import SOURCES, UA


def _huaban(q: str, limit: int) -> list[dict]:
    r = requests.get("https://api.huaban.com/search/file",
                     params={"text": q, "sort": "all", "limit": limit, "page": 1,
                             "position": "search_pin", "fields": "pins:PIN|total"},
                     headers={"User-Agent": UA}, timeout=12)
    out = []
    for p in r.json().get("pins", [])[:limit]:
        f = p.get("file") or {}
        if not f.get("key"):
            continue
        out.append({"platform": "花瓣", "url": f"https://huaban.com/pins/{p['pin_id']}",
                    "title": (p.get("raw_text") or "").strip()[:80] or "花瓣采集",
                    "thumbnail": f"https://{f.get('bucket') or 'gd-hbimg'}.huaban.com/{f['key']}_fw658",
                    "duration": None, "media": "image"})
    return out


def _cannes(q: str, limit: int) -> list[dict]:
    return searchers.search_youtube(q if "cannes" in q.lower() else q + " Cannes Lions", limit)

# kind: creative=优秀创意  performance=投放效果  visual=视觉镜头  social=用户反馈
PLATFORMS = [
    # ---- 优秀创意：找"好"的标准 ----
    {"name": "数英网 DIGITALING", "fetch": searchers.search_digitaling, "kind": "creative", "lang": "zh",
     "learn": "国内品牌 TVC 案例与创意说明，看洞察和创意阐述",
     "url": lambda q: f"https://www.bing.com/search?q=site%3Adigitaling.com+{quote_plus(q)}"},
    {"name": "广告门 adquan", "kind": "creative", "lang": "zh",
     "learn": "国内广告行业案例，看同行最新作品",
     "url": lambda q: f"https://www.bing.com/search?q=site%3Aadquan.com+{quote_plus(q)}"},
    {"name": "Ads of the World", "fetch": SOURCES["adsoftheworld"][0], "kind": "creative", "lang": "en",
     "learn": "全球广告案例库，按品类看国外同类片怎么拍",
     "url": lambda q: f"https://www.bing.com/search?q=site%3Aadsoftheworld.com+{quote_plus(q)}"},
    {"name": "戛纳狮子奖 / 获奖作品", "fetch": _cannes, "kind": "creative", "lang": "en",
     "learn": "行业顶级创意标杆，看创意高度与叙事手法",
     "url": lambda q: ("https://www.bing.com/videos/search?q="
                       + quote_plus(q if "cannes" in q.lower() else q + " Cannes Lions"))},
    {"name": "新片场", "fetch": SOURCES["xinpianchang"][0], "kind": "creative", "lang": "zh",
     "learn": "国内广告片/TVC 创作者社区，看导演作品与制作幕后",
     "url": lambda q: f"https://www.xinpianchang.com/search?kw={quote(q)}"},
    {"name": "iSpot.tv", "fetch": SOURCES["ispot"][0], "kind": "creative", "lang": "en",
     "learn": "美国电视广告全库，按品牌查历年 TVC",
     "url": lambda q: f"https://www.ispot.tv/search/{quote(q)}"},
    {"name": "Vimeo", "kind": "creative", "lang": "en",
     "learn": "导演与制作公司作品集，看高质量品牌片的影像质感",
     "url": lambda q: f"https://vimeo.com/search?q={quote_plus(q)}"},
    # ---- 投放效果：找"有效"的标准 ----
    {"name": "Meta 广告资料库", "kind": "performance", "lang": "en",
     "learn": "竞品正在投放的 FB/IG 广告，看在投素材和版本数量",
     "url": lambda q: ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all"
                       f"&country=ALL&search_type=keyword_unordered&q={quote(q)}")},
    {"name": "TikTok Creative Center", "kind": "performance", "lang": "en",
     "learn": "海外 TikTok 高表现广告，按行业/地区筛选，看前 3 秒钩子",
     "url": lambda q: "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en"},
    {"name": "巨量创意", "kind": "performance", "lang": "zh",
     "learn": "抖音投放效果好的广告素材（需登录巨量引擎账号）",
     "url": lambda q: "https://cc.oceanengine.com/"},
    # ---- 用户真实反应 ----
    {"name": "抖音", "fetch": searchers.search_douyin, "kind": "social", "lang": "zh",
     "learn": "同品类爆款短视频和评论区真实反馈",
     "url": lambda q: f"https://www.douyin.com/search/{quote(q)}"},
    {"name": "小红书", "kind": "social", "lang": "zh",
     "learn": "种草笔记与用户口碑，提炼真实痛点和用户原话",
     "url": lambda q: f"https://www.xiaohongshu.com/search_result?keyword={quote(q)}"},
    # ---- 视觉与镜头 ----
    {"name": "FILMGRAB", "kind": "visual", "lang": "en",
     "learn": "电影级画面截图，参考构图与色调",
     "url": lambda q: f"https://film-grab.com/?s={quote_plus(q)}"},
    {"name": "Pinterest", "kind": "visual", "lang": "en",
     "learn": "做情绪板：色调、光影、产品摆拍",
     "url": lambda q: f"https://www.pinterest.com/search/pins/?q={quote_plus(q)}"},
    {"name": "花瓣", "fetch": _huaban, "kind": "visual", "lang": "zh",
     "learn": "中文情绪板与视觉参考",
     "url": lambda q: f"https://huaban.com/search?q={quote(q)}"},
]

KIND_NAMES = {"creative": "优秀创意（找“好”的标准）", "performance": "投放效果（找“有效”的标准）",
              "social": "用户真实反应", "visual": "视觉与镜头参考"}


def platform_links(analysis: dict) -> list[dict]:
    p = analysis.get("product", {})
    dims = {d.get("key"): d for d in analysis.get("dimensions", [])}

    def first(key, lang):
        d = dims.get(key) or {}
        qs = d.get(f"queries_{lang}") or []
        return qs[0] if qs else ""

    brand = p.get("brand") if p.get("brand") not in (None, "", "未知") else ""
    competitors = [re.sub(r"[（(].*?[)）]", "", c).strip() for c in p.get("competitors", []) if c]
    competitors = [c for c in competitors if c]
    out = []
    for pf in PLATFORMS:
        lang = pf["lang"]
        if pf["kind"] == "performance":
            q = competitors[0] if competitors else (brand or first("category", lang))
        elif pf["kind"] == "visual":
            q = first("structure", lang) or first("category", lang)
        elif pf["name"].startswith("戛纳"):
            q = first("award", "en") or first("category", "en")
        else:
            q = first("category", lang)
        out.append({"name": pf["name"], "kind": pf["kind"], "kind_name": KIND_NAMES[pf["kind"]],
                    "learn": pf["learn"], "query": q, "url": pf["url"](q),
                    "direct": "fetch" in pf})
    return out


def fetch_items(name: str, query: str, limit: int = 8) -> list[dict]:
    """直接抓取某个参考平台的作品；不支持的平台抛 KeyError。"""
    pf = next(p for p in PLATFORMS if p["name"] == name and "fetch" in p)
    items = pf["fetch"](query, limit)
    for it in items:
        it.setdefault("media", "video")
    return items
