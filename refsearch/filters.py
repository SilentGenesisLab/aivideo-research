"""视频类型与筛选：检索前定向（类型词、附加关键词），检索后筛选（类型、必含/排除词、时长、广告正片）。

- 类型由 AI 重排时根据标题/频道/简介判断（看不到画面），AI 失败时用标题关键词兜底。
- 筛选条件随结果保存在 result["filters"]，被筛掉的数量记在 stats["filtered"]。
"""
import re

# key → 显示名、说明、拼检索词用的中英文类型词、标题兜底识别的正则
VIDEO_TYPES = {
    "tvc": {"name": "TVC 广告", "desc": "电视/贴片广告正片，15~60 秒为主",
            "zh": ["TVC", "广告片"], "en": ["TV commercial", "TVC"],
            "re": r"tvc|广告片|电视广告|commercial|\bspot\b|\bad\b|\b(15|30|60)s\b|(15|30|60)秒"},
    "brand": {"name": "品牌片", "desc": "品牌宣传片、品牌形象片、发布会短片",
              "zh": ["品牌片", "品牌宣传片"], "en": ["brand film", "brand campaign"],
              "re": r"品牌片|品牌宣传|形象片|宣传片|brand film|brand video|manifesto|campaign film"},
    "ugc": {"name": "UGC / 达人种草", "desc": "达人口播、真实使用、种草带货类短视频",
            "zh": ["种草", "达人推荐"], "en": ["UGC ad", "TikTok ad"],
            "re": r"种草|好物|安利|口播|达人|ugc|tiktok made me|haul|testimonial"},
    "product": {"name": "产品展示", "desc": "产品功能演示、卖点讲解、电商主图视频",
                "zh": ["产品展示视频", "产品视频"], "en": ["product video", "product demo"],
                "re": r"产品展示|产品视频|主图视频|功能演示|product (video|demo|showcase)|demo"},
    "story": {"name": "剧情广告", "desc": "微电影、剧情短片、情感故事类广告",
              "zh": ["微电影广告", "剧情广告"], "en": ["short film ad", "storytelling ad"],
              "re": r"微电影|剧情|短片|故事|short film|storytelling"},
    "cg": {"name": "CG / 动画", "desc": "三维产品渲染、CG 特效、MG 动画",
           "zh": ["CG广告", "三维产品动画"], "en": ["CGI commercial", "3D product animation"],
           "re": r"\bcg\b|cgi|三维|3d|c4d|动画|渲染|animation|motion graphics"},
    "case": {"name": "案例 / 获奖", "desc": "广告节获奖作品、Case Film 案例视频",
             "zh": ["获奖广告", "广告案例"], "en": ["award winning ad", "case film"],
             "re": r"获奖|戛纳|金狮|one show|cannes|lions|case (film|study|video)|案例"},
    "review": {"name": "开箱测评", "desc": "开箱、测评、对比（非广告，但常用来看卖点怎么讲）",
               "zh": ["开箱测评"], "en": ["unboxing review"],
               "re": r"开箱|测评|评测|对比|上手|体验|unboxing|review|hands.on|vs\.?"},
}
OTHER = "other"
TYPE_NAMES = {**{k: v["name"] for k, v in VIDEO_TYPES.items()}, OTHER: "其他"}
MAX_TYPE_QUERIES = 4  # 类型定向维度每种语言最多几个检索词，控制检索次数


def type_list() -> list[dict]:
    return [{"key": k, "name": v["name"], "desc": v["desc"]} for k, v in VIDEO_TYPES.items()]


def _words(x) -> list[str]:
    if isinstance(x, str):
        x = re.split(r"[,，;；\n]+|\s{2,}", x) if re.search(r"[,，;；\n]", x) else x.split()
    return [w.strip() for w in (x or []) if w and w.strip()][:20]


def normalize(f: dict | None) -> dict:
    """统一筛选参数；缺省值即“不筛选”，旧结果没有该字段时也按此处理。"""
    f = dict(f or {})
    types = [t for t in f.get("types") or [] if t in VIDEO_TYPES]
    mode = f.get("type_mode") if f.get("type_mode") in ("prefer", "only") else "prefer"
    try:
        lo = max(0, int(f.get("min_duration") or 0))
        hi = max(0, int(f.get("max_duration") or 0))
    except (TypeError, ValueError):
        lo, hi = 0, 0
    return {"types": types, "type_mode": mode, "keywords": _words(f.get("keywords")),
            "must": _words(f.get("must")), "exclude": _words(f.get("exclude")),
            "min_duration": lo, "max_duration": hi, "ad_only": bool(f.get("ad_only"))}


def describe(f: dict) -> str:
    parts = []
    if f["types"]:
        parts.append(("只要" if f["type_mode"] == "only" else "优先") + "、".join(TYPE_NAMES[t] for t in f["types"]))
    if f["keywords"]:
        parts.append("附加词 " + " ".join(f["keywords"]))
    if f["must"]:
        parts.append("必含 " + " / ".join(f["must"]))
    if f["exclude"]:
        parts.append("排除 " + " ".join(f["exclude"]))
    if f["min_duration"] or f["max_duration"]:
        parts.append(f"时长 {f['min_duration']}~{f['max_duration'] or '不限'} 秒")
    if f["ad_only"]:
        parts.append("只要广告正片")
    return "；".join(parts)


# ---------------- 检索前：定向 ----------------
def _is_en(w: str) -> bool:
    return not re.search(r"[一-鿿]", w)


def type_dimension(analysis: dict, f: dict) -> dict | None:
    """按目标类型和附加关键词拼出一组定向检索词，作为单独的“类型定向”维度。"""
    if not f["types"] and not f["keywords"]:
        return None
    p = analysis.get("product", {})
    core_zh = (p.get("search_zh") or p.get("category") or p.get("name") or "").split("／")[0].split("/")[0].strip()
    core_en = (p.get("search_en") or "").strip() or core_zh
    zh, en = [], []
    for i in range(2):  # 先每种类型取第一个词，名额有剩再取第二个
        for t in f["types"]:
            terms = VIDEO_TYPES[t]
            if i < len(terms["zh"]):
                zh.append(f"{core_zh} {terms['zh'][i]}")
            if i < len(terms["en"]):
                en.append(f"{core_en} {terms['en'][i]}")
    kw = [(w, _is_en(w)) for w in f["keywords"]]  # 附加关键词排在类型词前面，名额优先给它们
    zh = [f"{core_zh} {w}" for w, en_ in kw if not en_] + zh
    en = [f"{core_en} {w}" for w, en_ in kw if en_] + en
    zh, en = list(dict.fromkeys(zh))[:MAX_TYPE_QUERIES], list(dict.fromkeys(en))[:MAX_TYPE_QUERIES]
    if not zh and en:
        zh = en[:2]
    names = "、".join(TYPE_NAMES[t] for t in f["types"]) or "附加关键词"
    return {"key": "type", "name": f"类型定向：{names}",
            "what_to_learn": f"按指定类型（{names}）直接检索，看同品类在这种形式下怎么拍、怎么讲卖点",
            "queries_zh": zh, "queries_en": en or zh[:2]}


def prompt_hint(f: dict) -> str:
    """写进识图拆词提示词，让 5 个维度的检索词也偏向目标类型。"""
    lines = []
    if f["types"]:
        lines.append("用户想找的视频类型：" + "、".join(
            f"{VIDEO_TYPES[t]['name']}（{VIDEO_TYPES[t]['desc']}，可用词：{'/'.join(VIDEO_TYPES[t]['zh'] + VIDEO_TYPES[t]['en'])}）"
            for t in f["types"]) + "。各维度的搜索词要尽量带上这些类型词，不要只搜泛泛的“广告”。")
    if f["keywords"]:
        lines.append("用户指定的附加关键词：" + "、".join(f["keywords"]) + "，请融入合适维度的搜索词。")
    if f["exclude"]:
        lines.append("用户不想要：" + "、".join(f["exclude"]) + "，搜索词里不要出现。")
    return "\n".join(lines)


def rank_hint(f: dict) -> str:
    if not f["types"]:
        return ""
    names = "、".join(TYPE_NAMES[t] for t in f["types"])
    if f["type_mode"] == "only":
        return f"用户只要这些类型：{names}。属于这些类型的按相关度正常打分（即使不是广告正片也可以高分）；不属于的最高 3 分。"
    return f"用户优先要这些类型：{names}。属于这些类型的按相关度正常打分（即使不是广告正片也可以高分），其他类型适当降分。"


# ---------------- 检索后：筛选 ----------------
def guess_type(v: dict) -> str:
    text = f"{v.get('title', '')} {v.get('channel', '')} {v.get('description', '')}".lower()
    for k in ("case", "tvc", "brand", "ugc", "review", "cg", "story", "product"):  # 明确写了“广告片/TVC”的优先
        if re.search(VIDEO_TYPES[k]["re"], text):
            return k
    return OTHER


def _text(v: dict) -> str:
    return f"{v.get('title', '')} {v.get('channel', '')} {v.get('description', '')}".lower()


def pre_filter(v: dict, f: dict, default_max: int) -> str | None:
    """重排前的硬筛选（省掉 AI 打分成本）。返回被筛掉的原因，None 表示保留。"""
    d = v.get("duration")
    hi = f["max_duration"] or default_max
    if d and (d < max(5, f["min_duration"]) or (hi and d > hi)):
        return "duration"
    text = _text(v)
    if any(w.lower() in text for w in f["exclude"]):
        return "exclude"
    if f["must"] and not any(w.lower() in text for w in f["must"]):
        return "must"
    return None


def post_filter(v: dict, f: dict) -> str | None:
    """重排后的筛选：类型、广告正片。"""
    if f["types"] and f["type_mode"] == "only" and v.get("vtype") not in f["types"]:
        return "type"
    if f["ad_only"] and not v.get("is_ad"):
        return "ad"
    return None


FILTER_REASONS = {"duration": "时长不符", "exclude": "含排除词", "must": "不含必含词",
                  "type": "类型不符", "ad": "非广告正片", "score": "评分过低"}
