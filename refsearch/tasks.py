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


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _save(t: dict) -> None:
    if t["kind"] == "relay":  # 播放中转只是临时任务，不落盘
        return
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
            t.update(status="error", error="服务重启，任务中断，请重新提交", finished=_now())
            _save(t)
        _tasks[t["id"]] = t


def submit(kind: str, title: str, fn, meta: dict | None = None, on_finish=None) -> str:
    """fn(progress) 返回 dict；检索/下载任务的返回值里有 run_id，会记到任务上。
    on_finish(task) 在任务结束（成功或失败）后调用，用于 Webhook 通知。"""
    tid = uuid.uuid4().hex[:12]
    t = {"id": tid, "kind": kind, "title": title, "status": "queued", "created": _now(), "seq": time.time_ns(),
         "started": None, "finished": None, "log": [], "error": None, "run_id": None,
         "result": None, "meta": meta or {}}
    with _lock:
        _tasks[tid] = t
    _save(t)

    def progress(msg: str):
        t["log"].append(msg)
        del t["log"][:-60]
        _save(t)

    def run():
        t.update(status="running", started=_now())
        _save(t)
        try:
            res = fn(progress) or {}
            t["run_id"] = res.get("run_id") or t["meta"].get("run_id")
            if kind == "relay":
                t["result"] = res
            t["status"] = "done"
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
    return [{**{k: v for k, v in t.items() if k not in ("log", "result")},
             "last": t["log"][-1] if t["log"] else "", "ahead": position(t)} for t in items[:limit]]


_restore()
