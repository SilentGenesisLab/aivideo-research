"""边缘端（4090）回传的视频笔记：视频本体留在边缘端，本机只存结论和指针（几 KB）。"""
import json
import threading
from datetime import datetime, timedelta, timezone

from . import config

MAX_BYTES = 256 * 1024  # 单次检索的笔记总量上限
_lock = threading.Lock()


def _file(run_id: str):
    return config.RUNS_DIR / run_id / "edge_notes.json"


def load(run_id: str) -> list[dict]:
    try:
        return json.loads(_file(run_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def merge(run_id: str, items: list[dict], host: str) -> list[dict]:
    """按 url 合并；同一条再传会覆盖。超过 MAX_BYTES 抛 ValueError。"""
    now = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    with _lock:
        cur = {i["url"]: i for i in load(run_id)}
        for it in items:
            cur[it["url"]] = {**it, "host": host, "updated_at": now}
        out = list(cur.values())
        data = json.dumps(out, ensure_ascii=False)
        if len(data.encode("utf-8")) > MAX_BYTES:
            raise ValueError(f"笔记超过 {MAX_BYTES // 1024}KB 上限；视频本体和关键帧请留在边缘端，这里只放结论")
        f = _file(run_id)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(f)
    return out
