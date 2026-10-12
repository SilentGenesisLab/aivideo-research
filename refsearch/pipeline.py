"""主流程：识图拆词 → 多平台检索 → 重排 → 报告 → （可选）下载。"""
import json
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config
from .analyze import analyze_product
from .downloader import download
from .platforms import platform_links
from .rank import rank_dimension
from .report import write_markdown
from .searchers import SEARCHERS, SOURCE_NAMES, SOURCES, canonical


def _noop(msg: str) -> None:
    pass


DEFAULT_SOURCES = tuple(SOURCES)
MAX_CANDIDATES = 90  # 每个维度送去打分的候选上限，控制 DeepSeek 成本


def run_search(image_path: str, brief: str = "", platforms=DEFAULT_SOURCES,
               per_query: int = 8, max_duration: int = 300, top_per_dim: int = 10,
               min_score: int = 5, download_mode: str = "none", download_top: int = 5,
               max_height: int = 720, progress=_noop) -> dict:
    t0 = time.time()
    config.RUNS_DIR.mkdir(parents=True, exist_ok=True)
    base = time.strftime("%Y%m%d-%H%M%S")
    for i in range(100):  # 多个任务同一秒开始时，目录名加序号避免互相覆盖
        run_id = base if i == 0 else f"{base}-{i}"
        run_dir = config.RUNS_DIR / run_id
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            continue
    img_copy = run_dir / f"product{Path(image_path).suffix or '.jpg'}"
    shutil.copy(image_path, img_copy)

    # 1. 识图拆词
    progress("① 识别产品并拆解关键词…")
    analysis = analyze_product(str(img_copy), brief)
    p = analysis["product"]
    progress(f"识别为：{p.get('brand','')} {p.get('name','')}（{p.get('category','')}），"
             f"{len(analysis['dimensions'])} 个参考维度")

    # 2. 检索：每个来源只搜它擅长的语言（见 SEARCHERS 定义）
    platforms = [p for p in platforms if p in SEARCHERS]
    tasks = [(d["key"], pf, q)
             for d in analysis["dimensions"]
             for pf in platforms
             for lang in SEARCHERS[pf][1]
             for q in d[f"queries_{lang}"]]
    progress(f"② 在 {'、'.join(SOURCE_NAMES[p] for p in platforms)} 上并发执行 {len(tasks)} 次检索…")

    errors = []

    def do(task):
        key, pf, q = task
        try:
            vids = SEARCHERS[pf][0](q, per_query)
            for v in vids:
                v["query"], v["dim"] = q, key
            return vids
        except Exception as e:
            errors.append(f"{SOURCE_NAMES[pf]} 「{q}」：{str(e)[:150]}")
            return []

    with ThreadPoolExecutor(max_workers=12) as ex:
        raw = [v for vids in ex.map(do, tasks) for v in vids]
    progress(f"共获得 {len(raw)} 条原始结果" + (f"，{len(errors)} 次检索失败" if errors else ""))

    # 去重 + 时长过滤（太长的多为合集/长视频，太短的多为片段）
    by_dim: dict[str, dict[str, dict]] = {}
    for v in raw:
        if v.get("duration") and not (5 <= v["duration"] <= max_duration):
            continue
        v["url"] = canonical(v["url"])
        by_dim.setdefault(v["dim"], {}).setdefault(v["url"], v)

    # 3. 重排（各维度并行）
    progress("③ AI 评估相关性并标注参考点…")
    dims = [d for d in analysis["dimensions"] if by_dim.get(d["key"])]

    def rank(d):
        try:
            cands = _balanced(list(by_dim[d["key"]].values()), MAX_CANDIDATES, keep_order=True)
            return d["key"], rank_dimension(analysis, d, cands)
        except Exception as e:
            errors.append(f"重排「{d.get('name')}」：{str(e)[:150]}")
            return d["key"], list(by_dim[d["key"]].values())

    with ThreadPoolExecutor(max_workers=5) as ex:
        ranked = dict(ex.map(rank, dims))

    # 跨维度去重：同一条视频只保留在得分最高的维度
    best: dict[str, tuple[int, str]] = {}
    for key, vids in ranked.items():
        for v in vids:
            if v["url"] not in best or v.get("score", 0) > best[v["url"]][0]:
                best[v["url"]] = (v.get("score", 0), key)

    groups = []
    for d in analysis["dimensions"]:
        vids = [v for v in ranked.get(d["key"], [])
                if best.get(v["url"], (0, None))[1] == d["key"] and v.get("score", 0) >= min_score]
        groups.append({**d, "videos": _balanced(vids, top_per_dim)})

    result = {
        "run_id": run_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image": img_copy.name, "brief": brief, "sources": list(platforms), "analysis": analysis,
        "groups": groups, "platform_links": platform_links(analysis),
        "stats": {"queries": len(tasks), "raw": len(raw),
                  "kept": sum(len(g["videos"]) for g in groups),
                  "seconds": round(time.time() - t0, 1)},
        "errors": errors, "downloads": [],
    }
    save(result)
    progress(f"完成检索：保留 {result['stats']['kept']} 条参考视频，用时 {result['stats']['seconds']}s")

    # 4. 可选下载
    if download_mode != "none":
        all_vids = sorted((v for g in groups for v in g["videos"]),
                          key=lambda v: v.get("score", 0), reverse=True)
        pick = all_vids if download_mode == "all" else all_vids[:download_top]
        download_into(result, pick, max_height, progress)
    return result


def _balanced(vids: list[dict], n: int, keep_order: bool = False) -> list[dict]:
    """按平台轮流取结果，避免某个平台霸榜；默认最终按分数排序。"""
    queues: dict[str, list[dict]] = {}
    for v in vids:  # vids 已按分数降序
        queues.setdefault(v["platform"], []).append(v)
    picked = []
    while len(picked) < n and any(queues.values()):
        for q in queues.values():
            if q and len(picked) < n:
                picked.append(q.pop(0))
    return picked if keep_order else sorted(picked, key=lambda v: v.get("score", 0), reverse=True)


def download_into(result: dict, videos: list[dict], max_height: int = 720, progress=_noop) -> list[dict]:
    progress(f"④ 开始下载 {len(videos)} 条视频…")
    run_dir = config.RUNS_DIR / result["run_id"]
    res = download(videos, run_dir / "videos", max_height, progress)
    for r in res:
        if r["file"]:
            r["file"] = str(Path(r["file"]).relative_to(run_dir))
    result["downloads"] = [d for d in result.get("downloads", [])
                           if d["url"] not in {r["url"] for r in res}] + res
    save(result)
    ok = sum(r["ok"] for r in res)
    progress(f"下载完成：成功 {ok}/{len(res)}")
    return res


def save(result: dict) -> None:
    run_dir = config.RUNS_DIR / result["run_id"]
    (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(result, run_dir / "report.md")


def list_runs() -> list[dict]:
    """历史检索摘要，最新的在前。"""
    out = []
    for f in sorted(config.RUNS_DIR.glob("*/result.json"), reverse=True):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        p = r.get("analysis", {}).get("product", {})
        vids = [v for g in r.get("groups", []) for v in g.get("videos", [])]
        out.append({
            "run_id": r["run_id"], "created": r.get("created", ""), "brief": r.get("brief", ""),
            "image": f"/runs/{r['run_id']}/{r.get('image', 'product.jpg')}",
            "thumb": f"/img?run={r['run_id']}&w=160",
            "product": f"{p.get('brand', '')} {p.get('name', '')}".strip(),
            "category": p.get("category", ""), "kept": len(vids),
            "platforms": sorted({v["platform"] for v in vids}),
            "downloads": sum(1 for d in r.get("downloads", []) if d.get("ok")),
        })
    return out


def run_image(run_id: str) -> Path:
    result = load(run_id)
    return config.RUNS_DIR / run_id / result.get("image", "product.jpg")


def load(run_id: str) -> dict:
    path = config.RUNS_DIR / run_id / "result.json"
    if not path.resolve().is_relative_to(config.RUNS_DIR.resolve()):
        raise ValueError("非法 run_id")
    return json.loads(path.read_text(encoding="utf-8"))


def find_videos(result: dict, urls: list[str]) -> list[dict]:
    idx = {v["url"]: v for g in result["groups"] for v in g["videos"]}
    return [idx[u] for u in urls if u in idx]
