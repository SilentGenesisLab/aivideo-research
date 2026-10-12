"""主流程：识图拆词 → 多平台检索 → 重排 → 报告 → （可选）下载。"""
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, filters as flt
from .analyze import analyze_product
from .downloader import download
from .platforms import platform_links
from .rank import rank_dimension
from .report import write_markdown
from .searchers import SEARCHERS, SOURCE_NAMES, SOURCES, canonical


def _noop(msg: str) -> None:
    pass


class _Partial:
    """检索中间态：写 runs/<id>/partial.json 供页面提前渲染，并通过 progress.update 上报进度百分比。

    进度划分：识图 0~15%，检索 15~55%（按完成的检索次数），AI 打分 55~98%（按完成的打分批次）。"""

    def __init__(self, run_dir: Path, base: dict, progress):
        self.path, self.data, self.lock = run_dir / "partial.json", base, threading.Lock()
        self.errors: list[str] = []  # 检索线程会并发追加，写盘时取快照
        self.t0 = time.time()
        self.update = getattr(progress, "update", None) or (lambda **kw: None)
        self.last = 0.0

    def set(self, pct: float, stage: str, force: bool = False, **fields):
        with self.lock:
            self.data.update(fields)
            self.data["progress"] = {"pct": round(pct), "stage": stage, "rev": self.data["progress"]["rev"] + 1}
            self.update(pct=pct, stage=stage)
            if force or time.time() - self.last > 1:  # 高频事件（每次检索完成）最多每秒写一次
                self.last = time.time()
                tmp = self.path.with_suffix(".tmp")
                self.data["errors"] = list(self.errors)
                self.data["stats"] = {**self.data["stats"], "seconds": round(time.time() - self.t0, 1)}
                tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.path)

    def done(self):
        self.path.unlink(missing_ok=True)


def load_partial(run_id: str) -> dict | None:
    path = config.RUNS_DIR / run_id / "partial.json"
    if not path.resolve().is_relative_to(config.RUNS_DIR.resolve()):
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


DEFAULT_SOURCES = tuple(SOURCES)
MAX_CANDIDATES = 90  # 每个维度送去打分的候选上限，控制 DeepSeek 成本
RANK_BATCH = 25


def run_search(image_path: str, brief: str = "", platforms=DEFAULT_SOURCES,
               per_query: int = 8, max_duration: int = 300, top_per_dim: int = 10,
               min_score: int = 5, download_mode: str = "none", download_top: int = 5,
               max_height: int = 720, filters: dict | None = None, extra: dict | None = None,
               progress=_noop) -> dict:
    """extra：要并入结果的调用方信息（如 client、external_id），中间态里也会带上。"""
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
    f = flt.normalize(filters)
    platforms = [p for p in platforms if p in SEARCHERS]
    part = _Partial(run_dir, {
        "run_id": run_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "image": img_copy.name,
        "brief": brief, "sources": list(platforms), "filters": f, **(extra or {}),
        "analysis": None, "groups": [], "platform_links": [], "errors": [], "downloads": [],
        "stats": {"queries": 0, "raw": 0, "kept": 0, "seconds": 0},
        "searched": {}, "progress": {"pct": 0, "stage": "", "rev": 0}}, progress)
    getattr(progress, "update", lambda **kw: None)(run_id=run_id)

    # 1. 识图拆词
    progress("① 识别产品并拆解关键词…")
    part.set(3, "识别产品", force=True)
    if flt.describe(f):
        progress(f"筛选条件：{flt.describe(f)}")
    analysis = analyze_product(str(img_copy), brief, flt.prompt_hint(f))
    if (td := flt.type_dimension(analysis, f)):
        analysis["dimensions"].insert(0, td)
    p = analysis["product"]
    progress(f"识别为：{p.get('brand','')} {p.get('name','')}（{p.get('category','')}），"
             f"{len(analysis['dimensions'])} 个参考维度")

    # 2. 检索：每个来源只搜它擅长的语言（见 SEARCHERS 定义）
    tasks = [(d["key"], pf, q)
             for d in analysis["dimensions"]
             for pf in platforms
             for lang in SEARCHERS[pf][1]
             for q in d[f"queries_{lang}"]]
    progress(f"② 在 {'、'.join(SOURCE_NAMES[p] for p in platforms)} 上并发执行 {len(tasks)} 次检索…")

    errors = part.errors
    links = platform_links(analysis)
    pending = [{**d, "videos": [], "pending": True} for d in analysis["dimensions"]]
    part.set(15, "多平台检索", force=True, analysis=analysis, groups=pending, platform_links=links,
             stats={"queries": len(tasks), "raw": 0, "kept": 0, "seconds": 0})
    searched: dict[str, int] = {}
    n_done = [0]

    def do(task):
        key, pf, q = task
        vids = []
        try:
            vids = SEARCHERS[pf][0](q, per_query)
            for v in vids:
                v["query"], v["dim"] = q, key
        except Exception as e:
            errors.append(f"{SOURCE_NAMES[pf]} 「{q}」：{str(e)[:150]}")
        with part.lock:
            n_done[0] += 1
            searched[SOURCE_NAMES[pf]] = searched.get(SOURCE_NAMES[pf], 0) + len(vids)
            raw_n = sum(searched.values())
        part.set(15 + 40 * n_done[0] / max(1, len(tasks)), f"多平台检索 {n_done[0]}/{len(tasks)}",
                 searched=dict(searched), stats={**part.data["stats"], "raw": raw_n})
        return vids

    with ThreadPoolExecutor(max_workers=12) as ex:
        raw = [v for vids in ex.map(do, tasks) for v in vids]
    progress(f"共获得 {len(raw)} 条原始结果" + (f"，{len(errors)} 次检索失败" if errors else ""))

    # 去重 + 硬筛选：时长（太长的多为合集/长视频，太短的多为片段）、排除词、必含词
    by_dim: dict[str, dict[str, dict]] = {}
    dropped: dict[str, set] = {}
    for v in raw:
        v["url"] = canonical(v["url"])
        if (why := flt.pre_filter(v, f, max_duration)):
            dropped.setdefault(why, set()).add(v["url"])
            continue
        by_dim.setdefault(v["dim"], {}).setdefault(v["url"], v)

    # 没有候选的维度直接标记完成，不再显示“检索中”
    part.set(55, "去重筛选", force=True, groups=[{**g, "pending": g["key"] in by_dim} for g in part.data["groups"]])

    # 3. 重排（各维度并行）
    progress("③ AI 评估相关性并标注参考点…")
    dims = [d for d in analysis["dimensions"] if by_dim.get(d["key"])]
    cands = {d["key"]: _balanced(list(by_dim[d["key"]].values()), MAX_CANDIDATES, keep_order=True) for d in dims}
    n_batches = sum(-(-len(c) // RANK_BATCH) for c in cands.values())
    b_done, shown = [0], set()
    part.set(55, "AI 评估", force=True)

    def batch_done():
        with part.lock:
            b_done[0] += 1
        part.set(55 + 43 * b_done[0] / max(1, n_batches), f"AI 评估 {b_done[0]}/{n_batches} 批")

    def show(key, vids):
        """某个维度打完分就先放进中间态（最终结果还会做跨维度去重，条目可能略有变化）。"""
        with part.lock:
            keep = [v for v in vids if v["url"] not in shown and not flt.post_filter(v, f)
                    and v.get("score", 0) >= min_score]
            keep = _balanced(keep, top_per_dim)
            shown.update(v["url"] for v in keep)
            for v in keep:
                v["vtype_name"] = flt.TYPE_NAMES.get(v.get("vtype"), "其他")
            groups = [{**g, "videos": keep, "pending": False} if g["key"] == key else g
                      for g in part.data["groups"]]
            kept = sum(len(g["videos"]) for g in groups)
        part.set(part.data["progress"]["pct"], part.data["progress"]["stage"], force=True,
                 groups=groups, stats={**part.data["stats"], "kept": kept})

    def rank(d):
        try:
            vids = rank_dimension(analysis, d, cands[d["key"]], batch_size=RANK_BATCH,
                                  hint=flt.rank_hint(f), on_batch=batch_done)
        except Exception as e:
            errors.append(f"重排「{d.get('name')}」：{str(e)[:150]}")
            vids = list(by_dim[d["key"]].values())
            for v in vids:
                v.setdefault("vtype", flt.guess_type(v))
        show(d["key"], vids)
        return d["key"], vids

    with ThreadPoolExecutor(max_workers=5) as ex:
        ranked = dict(ex.map(rank, dims))

    # 跨维度去重：同一条视频只保留在得分最高的维度
    best: dict[str, tuple[int, str]] = {}
    for key, vids in ranked.items():
        for v in vids:
            if v["url"] not in best or v.get("score", 0) > best[v["url"]][0]:
                best[v["url"]] = (v.get("score", 0), key)

    # 重排后筛选：类型、广告正片、最低分
    groups = []
    for d in analysis["dimensions"]:
        vids = []
        for v in ranked.get(d["key"], []):
            if best.get(v["url"], (0, None))[1] != d["key"]:
                continue
            why = flt.post_filter(v, f) or ("score" if v.get("score", 0) < min_score else None)
            if why:
                dropped.setdefault(why, set()).add(v["url"])
            else:
                v["vtype_name"] = flt.TYPE_NAMES.get(v.get("vtype"), "其他")
                vids.append(v)
        groups.append({**d, "videos": _balanced(vids, top_per_dim)})

    result = {
        "run_id": run_id, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image": img_copy.name, "brief": brief, "sources": list(platforms), "analysis": analysis,
        "filters": f,
        "groups": groups, "platform_links": links, **(extra or {}),
        "stats": {"queries": len(tasks), "raw": len(raw),
                  "kept": sum(len(g["videos"]) for g in groups),
                  "filtered": {k: len(v) for k, v in dropped.items()},
                  "seconds": round(time.time() - t0, 1)},
        "errors": errors, "downloads": [],
    }
    save(result)
    part.done()
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
            "filters": flt.describe(flt.normalize(r.get("filters"))),
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
