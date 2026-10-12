"""第 3 步：让 DeepSeek 基于标题/频道/时长/播放量对候选视频打分，并写出"参考什么"。

注意：模型看不到视频画面，参考点是基于元数据的推断，需要人看片核实。"""
from concurrent.futures import ThreadPoolExecutor

from . import filters
from .llm import chat_json

TYPES = "；".join(f"{k}={v['name']}（{v['desc']}）" for k, v in filters.VIDEO_TYPES.items()) + "；other=其他（教程、新闻、Vlog、合集、无关）"

SYSTEM = "你是资深广告创意总监，负责从搜索结果里挑出值得拉片学习的参考广告。只输出 JSON。"

PROMPT = """产品：{product}

参考维度「{dim_name}」——要学的是：{what}
{hint}
下面是视频平台的搜索结果（只有元数据，你看不到画面）：
{items}

请逐条评估，输出 JSON：
{{"items": [{{"idx": 序号, "score": 0-10 的整数, "is_ad": true/false, "type": "类型 key",
  "reference": "这条片子具体能参考什么（20~50 字，具体到钩子/情绪/产品露出/镜头/结构）",
  "reason": "打分理由（15 字以内）"}}]}}

打分标准：
- 10：正是该维度要找的广告正片，品牌明确、和产品高度相关
- 6~8：相关的广告片或高质量品牌片
- 3~5：相关但不是广告（测评、开箱、Vlog），或广告合集
- 0~2：无关、搬运营销号、教程、低质内容
is_ad 表示是否是广告/TVC/品牌片本身。
type 从下面选一个最贴切的 key：{types}
每条都必须返回。"""


def _fmt(v: dict, i: int) -> str:
    dur = f"{v['duration']}s" if v.get("duration") else "?"
    views = v.get("views") if v.get("views") is not None else "?"
    desc = (v.get("description") or "").replace("\n", " ")[:80]
    return (f"[{i}] ({v['platform']}) {v['title']} | 频道:{v.get('channel','')} | "
            f"时长:{dur} | 播放:{views} | 搜索词:{v.get('query','')} | 简介:{desc}")


def _rank_batch(product: str, dim: dict, batch: list[dict], hint: str) -> None:
    items = "\n".join(_fmt(v, i) for i, v in enumerate(batch))
    data = chat_json(SYSTEM, PROMPT.format(product=product, dim_name=dim.get("name", ""),
                                           what=dim.get("what_to_learn", ""), items=items,
                                           hint=f"\n{hint}\n" if hint else "", types=TYPES))
    for r in data.get("items", []):
        try:
            v = batch[int(r["idx"])]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        v["score"] = int(r.get("score", 0))
        v["is_ad"] = bool(r.get("is_ad"))
        v["reference"] = r.get("reference", "")
        v["reason"] = r.get("reason", "")
        t = str(r.get("type", "")).strip().lower()
        v["vtype"] = t if t in filters.TYPE_NAMES else filters.guess_type(v)


def rank_dimension(analysis: dict, dim: dict, videos: list[dict], batch_size: int = 25, hint: str = "") -> list[dict]:
    p = analysis.get("product", {})
    product = (f"{p.get('brand','')} {p.get('name','')}（{p.get('category','')}）"
               f"卖点：{'、'.join(p.get('selling_points', []))}；调性：{p.get('tone','')}")
    batches = [videos[i:i + batch_size] for i in range(0, len(videos), batch_size)]
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda b: _rank_batch(product, dim, b, hint), batches))
    for v in videos:
        v.setdefault("score", 0)
        v.setdefault("reference", "")
        v.setdefault("vtype", filters.guess_type(v))
    return sorted(videos, key=lambda v: (v["score"], v.get("views") or 0), reverse=True)
