"""异步任务队列：提交后立即返回任务号，后台按队列执行。

- 检索、下载、中转播放分开排队，互不阻塞；检索默认同时跑 2 个（SEARCH_WORKERS）。
- 任务状态写到 runs/_tasks/<id>.json，刷新页面、换电脑都能看到；服务重启时未完成的任务标记为中断。
"""
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import config

TASK_DIR = config.RUNS_DIR / "_tasks"
KEEP = 2000  # 最多保留的任务记录数（看板统计用）

_POOLS = {
    "search": ThreadPoolExecutor(int(os.environ.get("SEARCH_WORKERS", "2")), thread_name_prefix="search"),
    # 下载会改写同一份 result.json，串行执行避免互相覆盖
    "download": ThreadPoolExecutor(1, thread_name_prefix="download"),
    "relay": ThreadPoolExecutor(3, thread_name_prefix="relay"),
}
_tasks: dict[str, dict] = {}
_lock = threading.Lock()
# 重试：任务提交时记下“怎么再提交一次”（retry 规格，可 JSON 序列化，服务重启后也能重试），
# 由各入口注册处理函数 handler(spec) -> 新任务 ID
_RETRY: dict[str, callable] = {}


class RetryError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def register_retry(name: str, fn) -> None:
    _RETRY[name] = fn


def retry(tid: str) -> str:
    t = _tasks.get(tid)
    if not t:
        raise RetryError(404, "任务不存在")
    if t["status"] != "error":
        raise RetryError(400, "只有失败的任务可以重试")
    if t.get("retried_by"):
        raise RetryError(400, "这个任务已经重试过，请看新任务的结果")
    spec = t.get("retry")
    if not spec or spec.get("handler") not in _RETRY:
        raise RetryError(400, "这个任务不支持重试，请重新提交")
    new = _RETRY[spec["handler"]]({**spec, "retry_of": tid})
    t["retried_by"] = new
    _save(t)
    return new


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_save_lock = threading.Lock()


def _save(t: dict) -> None:
    if t["kind"] == "relay":  # 播放中转只是临时任务，不落盘
        return
    # 同一任务可能被两个线程同时写（如重试时请求线程记 retry_of、工作线程记开始运行），加锁避免临时文件互相覆盖
    with _save_lock:
        TASK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TASK_DIR / f"{t['id']}.json.tmp"
        tmp.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
        tmp.replace(TASK_DIR / f"{t['id']}.json")


def _restore() -> None:
    """启动时载入历史任务；上次没跑完的标记为中断。"""
    files = sorted(TASK_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
    for f in files[:-KEEP]:
        f.unlink(missing_ok=True)
    for f in files[-KEEP:]:
        try:
            t = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if t.get("status") in ("queued", "running"):
            t.update(status="error", finished=_now(),
                     error="服务重启，任务中断" + ("，可点“重试”" if t.get("retry") else "，请重新提交"))
            _save(t)
        _tasks[t["id"]] = t


def submit(kind: str, title: str, fn, meta: dict | None = None, on_finish=None, retry: dict | None = None) -> str:
    """fn(progress) 返回 dict；检索/下载任务的返回值里有 run_id，会记到任务上。
    on_finish(task) 在任务结束（成功或失败）后调用，用于 Webhook 通知。
    retry：失败后重试用的规格 {"handler": 注册名, ...}，见 register_retry。"""
    tid = uuid.uuid4().hex[:12]
    if retry and retry.get("retry_of"):  # 由 retry() 提交：记下原任务，规格里不保留这个字段
        meta = {**(meta or {}), "retry_of": retry["retry_of"]}
        retry = {k: v for k, v in retry.items() if k != "retry_of"}
    t = {"id": tid, "kind": kind, "title": title, "status": "queued", "created": _now(), "seq": time.time_ns(),
         "started": None, "finished": None, "log": [], "error": None, "run_id": None, "pct": 0, "stage": "",
         "result": None, "meta": meta or {}, "retry": retry}
    with _lock:
        _tasks[tid] = t
    _save(t)

    def progress(msg: str):
        t["log"].append(msg)
        del t["log"][:-60]
        _save(t)

    last_save = [0.0]

    def update(pct: float | None = None, stage: str | None = None, run_id: str | None = None):
        """中间态：进度百分比、当前阶段、提前确定的 run_id。高频调用，最多每秒落盘一次。"""
        if pct is not None:
            t["pct"] = max(t.get("pct") or 0, min(99, round(pct)))
        if stage is not None:
            t["stage"] = stage
        if run_id is not None:
            t["run_id"] = run_id
        if time.time() - last_save[0] > 1 or run_id is not None:
            last_save[0] = time.time()
            _save(t)

    progress.update = update  # 支持中间态的任务（检索）通过 getattr(progress, "update") 使用

    def run():
        try:  # 整个过程都在 try 里：任何异常都要把任务标成失败，不能停在“进行中”
            t.update(status="running", started=_now())
            _save(t)
            res = fn(progress) or {}
            t["run_id"] = res.get("run_id") or t["meta"].get("run_id")
            if kind == "relay":
                t["result"] = res
            t["status"], t["pct"], t["stage"] = "done", 100, "完成"
        except Exception as e:
            t["error"], t["status"] = str(e)[:500], "error"
        t["finished"] = _now()
        _save(t)
        if on_finish:
            try:
                on_finish(t)
            except Exception:
                pass

    _POOLS[kind].submit(run)
    return tid


def get(tid: str) -> dict | None:
    return _tasks.get(tid)


def position(t: dict) -> int:
    """排队中的任务前面还有几个同类任务。"""
    if t["status"] != "queued":
        return 0
    return sum(1 for x in list(_tasks.values())
               if x["kind"] == t["kind"] and x["status"] in ("queued", "running")
               and x.get("seq", 0) < t.get("seq", 0))


def recent(limit: int = 50, client: str | None = None) -> list[dict]:
    items = [t for t in list(_tasks.values()) if t["kind"] != "relay"
             and (client is None or t["meta"].get("client") == client)]
    items.sort(key=lambda t: t.get("seq", 0), reverse=True)
    return [{**{k: v for k, v in t.items() if k not in ("log", "result", "retry")},
             "last": t["log"][-1] if t["log"] else "", "ahead": position(t),
             "can_retry": t["status"] == "error" and not t.get("retried_by") and bool(t.get("retry"))}
            for t in items[:limit]]


_restore()
