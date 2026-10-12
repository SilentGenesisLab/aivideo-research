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
  - queries_zh：2 个中文搜索词（用于 B 站，2~6 个词，含"广告""TVC""广告片"等词）
  - queries_en：2 个英文搜索词（用于 YouTube，含 commercial / ad / TVC 等词）
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


def analyze_product(image_path: str, brief: str = "", hint: str = "") -> dict:
    brief_part = f"，并参考用户补充的简报：「{brief.strip()}」" if brief.strip() else ""
    hint = f"\n三、检索偏好（必须遵守）：\n{hint}\n" if hint else ""
    data = chat_json(SYSTEM, PROMPT.format(brief_part=brief_part, hint=hint), image_path=image_path)
    dims = data.get("dimensions") or []
    if not dims:
        raise ValueError(f"模型未返回检索维度：{data}")
    for d in dims:
        d["queries_zh"] = [q for q in d.get("queries_zh", []) if q][:3]
        d["queries_en"] = [q for q in d.get("queries_en", []) if q][:3]
    return data
