"""第 1 步：产品图 → 产品画像 + 分维度检索计划（关键词 + 参考什么）。"""
from .llm import chat_json

SYSTEM = """你是资深广告创意总监兼 TVC 导演，擅长为新产品寻找参考广告片。
只输出 JSON，不要任何解释。"""

PROMPT = """请仔细观察这张产品图{brief_part}，完成两件事：

一、产品画像：识别品牌、产品名、品类、外观（包装/颜色/材质）、推测的核心卖点、目标人群、适合的广告调性。看不清的字段写"未知"，不要编造品牌。

二、参考片检索计划：围绕"为这个产品拍一条 TVC/短视频广告"，拆出 5 个参考维度，必须覆盖：
  1. 同品类：同类产品的广告怎么拍
  2. 竞品：直接竞品品牌的广告（写出具体竞品品牌名）
  3. 情绪/主题：适合该产品的情绪或叙事主题
  4. 结构/形式：适合的广告结构或形式（如 15 秒钩子、一镜到底、反转、产品特写蒙太奇）
  5. 获奖标杆：该品类或相近品类的获奖/经典广告
每个维度给出：
  - what_to_learn：从这类参考片里具体学什么（具体到开头钩子、情绪曲线、产品露出方式、镜头语言等，一两句话）
  - queries_zh：{nq} 个中文搜索词（用于 B 站，2~6 个词，含"广告""TVC""广告片"等词，彼此角度不同）
  - queries_en：{nq} 个英文搜索词（用于 YouTube，含 commercial / ad / TVC 等词，彼此角度不同）
搜索词要像真人在视频平台上搜的那样短而具体，不要写成句子。
{hint}

输出 JSON 格式：
{{
  "product": {{
    "brand": "", "name": "", "category": "", "appearance": "",
    "selling_points": [""], "target_audience": "", "tone": "", "competitors": [""],
    "search_zh": "2~6 字的中文核心品类词，如 电动牙刷", "search_en": "英文核心品类词，如 electric toothbrush"
  }},
  "dimensions": [
    {{"key": "category", "name": "同品类", "what_to_learn": "", "queries_zh": ["",""], "queries_en": ["",""]}}
  ]
}}
dimensions 的 key 依次为 category / competitor / emotion / structure / award。"""


def analyze_product(image_path: str, brief: str = "", hint: str = "", n_queries: int = 2) -> dict:
    brief_part = f"，并参考用户补充的简报：「{brief.strip()}」" if brief.strip() else ""
    hint = f"\n三、检索偏好（必须遵守）：\n{hint}\n" if hint else ""
    data = chat_json(SYSTEM, PROMPT.format(brief_part=brief_part, hint=hint, nq=n_queries), image_path=image_path)
    dims = data.get("dimensions") or []
    if not dims:
        raise ValueError(f"模型未返回检索维度：{data}")
    for d in dims:
        d["queries_zh"] = [q for q in d.get("queries_zh", []) if q][:n_queries]
        d["queries_en"] = [q for q in d.get("queries_en", []) if q][:n_queries]
    return data


# ---------------- 文字检索：输入品牌名或产品名（不传产品图） ----------------
TEXT_COMMON = """搜索词要求：
  - queries_zh：{nq} 个中文搜索词（用于 B 站/抖音，2~8 个字，带上品牌名或品类词，并含"广告""TVC""种草""测评"等词之一，彼此角度不同）
  - queries_en：{nq} 个英文搜索词（用于 YouTube，含 commercial / ad / TVC 等词，彼此角度不同；国内品牌没有英文名时用品类英文词）
  像真人在视频平台上搜的那样短而具体，不要写成句子。
不认识的品牌/产品：如实按名称字面推断品类，不要编造产品线或卖点，拿不准的字段写"未知"。{brief_part}
{hint}"""

BRAND_PROMPT = """用户想找品牌「{query}」的爆款视频作参考（文字输入，没有产品图）。请完成两件事：

一、品牌画像：识别这个品牌（中文名/英文名）、所属行业与主品类、主要产品线（最多 3 条，按知名度排序）、品牌调性、目标人群、主要竞品品牌。

二、检索计划，维度依次为：
  1. key=brand「品牌官方」：该品牌自己的广告片、TVC、品牌片（覆盖全部产品）
  2. key=category「产品线：<第 1 条产品线>」：该产品线的同品类爆款视频怎么拍
  3. key=line2「产品线：<第 2 条产品线>」（没有第 2 条就省略）
  4. key=line3「产品线：<第 3 条产品线>」（没有第 3 条就省略）
  5. key=competitor「竞品」：主要竞品品牌的爆款广告（写出具体品牌名）
每个维度给出 what_to_learn（从这类视频具体学什么，一两句话）和搜索词。
""" + TEXT_COMMON

PRODUCT_PROMPT = """用户想找产品「{query}」的爆款视频作参考（文字输入，没有产品图）。请完成两件事：

一、产品画像：识别品牌（名称里没有就写"未知"）、产品名、品类、推测的核心卖点、目标人群、适合的广告调性、直接竞品。

二、检索计划，维度依次为：
  1. key=product「本品」：这个产品自己的广告、种草、测评视频
  2. key=category「同品类」：同类产品的爆款广告怎么拍
  3. key=competitor「竞品」：直接竞品品牌的广告（写出具体品牌名）
  4. key=emotion「情绪主题」：适合该产品的情绪或叙事主题
  5. key=structure「结构形式」：适合的广告结构或形式（如 15 秒钩子、开箱、对比测评）
每个维度给出 what_to_learn（从这类视频具体学什么，一两句话）和搜索词。
""" + TEXT_COMMON

TEXT_OUTPUT = """
输出 JSON 格式：
{{
  "product": {{
    "brand": "", "name": "", "category": "", "appearance": "未知（文字检索）",
    "product_lines": [""], "selling_points": [""], "target_audience": "", "tone": "", "competitors": [""],
    "search_zh": "2~6 字的中文核心品类词", "search_en": "英文核心品类词"
  }},
  "dimensions": [
    {{"key": "", "name": "", "what_to_learn": "", "queries_zh": ["",""], "queries_en": ["",""]}}
  ]
}}"""

QUERY_TYPES = ("brand", "product")


def analyze_text(query: str, query_type: str, brief: str = "", hint: str = "", n_queries: int = 2) -> dict:
    """品牌名 / 产品名 → 与 analyze_product 相同结构的画像与检索计划（维度 key 不同，见上面的提示词）。"""
    if query_type not in QUERY_TYPES:
        raise ValueError(f"query_type 只能是 {QUERY_TYPES}")
    tpl = BRAND_PROMPT if query_type == "brand" else PRODUCT_PROMPT
    brief_part = f"\n用户补充的简报（检索时参考）：「{brief.strip()}」" if brief.strip() else ""
    hint = f"\n检索偏好（必须遵守）：\n{hint}\n" if hint else ""
    prompt = (tpl + TEXT_OUTPUT).format(query=query.strip(), nq=n_queries, brief_part=brief_part, hint=hint)
    data = chat_json(SYSTEM, prompt)
    dims, seen = [], set()
    for d in data.get("dimensions") or []:
        key = str(d.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        d["queries_zh"] = [q for q in d.get("queries_zh", []) if q][:n_queries]
        d["queries_en"] = [q for q in d.get("queries_en", []) if q][:n_queries]
        if d["queries_zh"] or d["queries_en"]:
            dims.append(d)
    if not dims:
        raise ValueError(f"模型未返回检索维度：{data}")
    data["dimensions"] = dims
    p = data.setdefault("product", {})
    if query_type == "brand" and p.get("brand") in (None, "", "未知"):
        p["brand"] = query.strip()
    if query_type == "product" and p.get("name") in (None, "", "未知"):
        p["name"] = query.strip()
    return data
