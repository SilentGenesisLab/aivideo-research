"""一次性迁移：把改时区之前按 UTC 记录的时间改成东八区（+8 小时）。

只处理 runs/.tz_migrated 不存在时；处理完写入该标记，重复执行不会再改。
需在服务停止时运行：python tools/migrate_tz.py
"""
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

RUNS = Path(__file__).resolve().parent.parent / "runs"
MARK = RUNS / ".tz_migrated"
FMT = "%Y-%m-%d %H:%M:%S"


def shift(s):
    try:
        return (datetime.strptime(s, FMT) + timedelta(hours=8)).strftime(FMT)
    except (TypeError, ValueError):
        return s


def main():
    if MARK.exists():
        print("已迁移过，跳过")
        return
    n_tasks = n_runs = 0
    for f in (RUNS / "_tasks").glob("*.json"):
        t = json.loads(f.read_text(encoding="utf-8"))
        for k in ("created", "started", "finished"):
            t[k] = shift(t.get(k))
        f.write_text(json.dumps(t, ensure_ascii=False), encoding="utf-8")
        n_tasks += 1
    for f in RUNS.glob("*/result.json"):
        r = json.loads(f.read_text(encoding="utf-8"))
        r["created"] = shift(r.get("created"))
        f.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        n_runs += 1
    db = RUNS / "_stats.sqlite3"
    if db.exists():
        with sqlite3.connect(db) as c:
            c.execute("UPDATE requests SET day = date(ts, 'unixepoch', '+8 hours')")
    MARK.write_text("UTC -> UTC+8\n")
    print(f"已迁移：任务 {n_tasks} 个，检索结果 {n_runs} 个，接口日志按东八区重新分日")


if __name__ == "__main__":
    sys.exit(main())
