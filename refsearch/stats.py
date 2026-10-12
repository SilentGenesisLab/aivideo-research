"""数据看板：接口调用日志（SQLite）、全部检索结果的视频库索引、概览统计。"""
import json
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import config, tasks

DB = config.RUNS_DIR / "_stats.sqlite3"
KEEP_DAYS = 90
WEB = "网页"
_lock = threading.Lock()


# ---------------- 接口调用日志 ----------------
def _conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    DB.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS requests (
            ts REAL, day TEXT, client TEXT, method TEXT, route TEXT, path TEXT,
            status INTEGER, ms INTEGER, ip TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_req_ts ON requests(ts)")
        c.execute("DELETE FROM requests WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))


def log_request(client: str, method: str, route: str, path: str, status: int, ms: int, ip: str):
    now = time.time()
    try:
        with _lock, _conn() as c:
            c.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?)",
                      (now, time.strftime("%Y-%m-%d", time.localtime(now)), client, method, route,
                       path[:300], status, ms, ip))
    except sqlite3.Error:
        pass


def api_usage(days: int = 7) -> dict:
    since = time.time() - days * 86400
    with _conn() as c:
        by_client = [dict(r) for r in c.execute("""
            SELECT client, COUNT(*) calls, SUM(status >= 400) errors, CAST(AVG(ms) AS INT) avg_ms,
                   MAX(ts) last_ts FROM requests WHERE ts >= ? GROUP BY client ORDER BY calls DESC""", (since,))]
        by_route = [dict(r) for r in c.execute("""
            SELECT method || ' ' || route AS route, COUNT(*) calls, SUM(status >= 400) errors,
                   CAST(AVG(ms) AS INT) avg_ms FROM requests WHERE ts >= ?
            GROUP BY method, route ORDER BY calls DESC""", (since,))]
        by_status = {str(r[0]): r[1] for r in c.execute(
            "SELECT status, COUNT(*) FROM requests WHERE ts >= ? GROUP BY status ORDER BY status", (since,))}
        per_day = {r[0]: r[1] for r in c.execute(
            "SELECT day, COUNT(*) FROM requests WHERE ts >= ? GROUP BY day", (since,))}
    for r in by_client:
        r["last"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.pop("last_ts")))
    return {"days": days, "by_client": by_client, "by_route": by_route, "by_status": by_status,
            "per_day": _fill_days(per_day, days), "total": sum(r["calls"] for r in by_client),
            "errors": sum(r["errors"] or 0 for r in by_client)}


def recent_requests(limit: int = 100, client: str = "", errors_only: bool = False) -> list[dict]:
    sql, args = "SELECT * FROM requests WHERE 1=1", []
    if client:
        sql += " AND client = ?"
        args.append(client)
    if errors_only:
        sql += " AND status >= 400"
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
    for r in rows:
        r["time"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.pop("ts")))
    return rows


def _fill_days(counts: dict, days: int) -> list[dict]:
    today = datetime.now().date()
    return [{"day": d, "count": counts.get(d, 0)}
            for d in ((today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1))]


# ---------------- 视频库索引 ----------------
_index = {"sig": None, "runs": [], "videos": []}


def _load_index():
    files = sorted(config.RUNS_DIR.glob("*/result.json"))
    sig = tuple((f.parent.name, f.stat().st_mtime) for f in files)
    if sig == _index["sig"]:
        return _index
    runs, videos = [], []
    for f in files:
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        p = r.get("analysis", {}).get("product", {})
        dl = {d["url"]: d for d in r.get("downloads", [])}
        who = r.get("client") or WEB
        run = {"run_id": r["run_id"], "created": r.get("created", ""), "client": who,
               "product": p.get("name", ""), "brand": p.get("brand", ""), "category": p.get("category", ""),
               "brief": r.get("brief", ""), "sources": r.get("sources", []), "stats": r.get("stats", {}),
               "kept": 0, "downloads_ok": sum(1 for d in dl.values() if d.get("ok")), "downloads": len(dl)}
        for g in r.get("groups", []):
            for v in g.get("videos", []):
                d = dl.get(v["url"])
                videos.append({
                    "run_id": r["run_id"], "created": run["created"], "client": who,
                    "product": run["product"], "brand": run["brand"], "category": run["category"],
                    "dim": g.get("key", ""), "dim_name": g.get("name", ""),
                    **{k: v.get(k) for k in ("platform", "url", "title", "channel", "duration", "views",
                                             "thumbnail", "score", "is_ad", "reference")},
                    "local": f"/runs/{r['run_id']}/{d['file']}" if d and d.get("ok") else ""})
                run["kept"] += 1
        runs.append(run)
    _index.update(sig=sig, runs=runs, videos=videos)
    return _index


def search_videos(q: str = "", platform: str = "", dim: str = "", min_score: float = 0, client: str = "",
                  downloaded: bool = False, dedupe: bool = True, sort: str = "score",
                  limit: int = 60, offset: int = 0) -> dict:
    vids = _load_index()["videos"]
    words = [w.lower() for w in q.split() if w]

    def hit(v):
        if platform and v["platform"] != platform or dim and v["dim"] != dim or client and v["client"] != client:
            return False
        if (v["score"] or 0) < min_score or downloaded and not v["local"]:
            return False
        text = " ".join(str(v[k] or "") for k in ("title", "reference", "channel", "product", "brand",
                                                    "category", "platform")).lower()
        return all(w in text for w in words)

    found = [v for v in vids if hit(v)]
    if dedupe:  # 同一视频出现在多次检索里时只保留一条（优先已下载、分数高的）
        best = {}
        for v in found:
            k = v["url"]
            if k not in best or (bool(v["local"]), v["score"] or 0) > (bool(best[k]["local"]), best[k]["score"] or 0):
                best[k] = v
        found = list(best.values())
    key = {"score": lambda v: (v["score"] or 0, v["created"]), "new": lambda v: v["created"],
           "views": lambda v: v["views"] or 0}.get(sort, lambda v: v["score"] or 0)
    found.sort(key=key, reverse=True)
    facets = Counter(v["platform"] for v in found)
    return {"total": len(found), "items": found[offset:offset + limit],
            "platforms": facets.most_common(), "all_platforms": sorted({v["platform"] for v in vids}),
            "clients": sorted({v["client"] for v in vids})}


# ---------------- 概览 ----------------
def _secs(a: str | None, b: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
    except (TypeError, ValueError):
        return None


def overview(days: int = 30) -> dict:
    idx = _load_index()
    runs, vids = idx["runs"], idx["videos"]
    today = datetime.now().strftime("%Y-%m-%d")
    week = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    per_day = Counter(r["created"][:10] for r in runs)
    by_client = Counter(r["client"] for r in runs)
    uniq = {v["url"]: v for v in vids}

    all_tasks = [t for t in tasks.recent(limit=100000) if t["kind"] == "search"]
    finished = [t for t in all_tasks if t["status"] in ("done", "error")]
    durs = [d for t in finished if t["status"] == "done" and (d := _secs(t["started"], t["finished"]))]
    waits = [d for t in finished if (d := _secs(t["created"], t["started"])) is not None]

    usage = api_usage(7)
    with _conn() as c:
        api_today = c.execute("SELECT COUNT(*) FROM requests WHERE day = ?", (today,)).fetchone()[0]

    return {
        "kpi": {
            "runs_total": len(runs), "runs_today": per_day.get(today, 0),
            "runs_7d": sum(n for d, n in per_day.items() if d >= week),
            "videos_unique": len(uniq), "videos_ad": sum(1 for v in uniq.values() if v["is_ad"]),
            "videos_downloaded": sum(1 for v in uniq.values() if v["local"]),
            "avg_kept": round(sum(r["kept"] for r in runs) / len(runs), 1) if runs else 0,
            "task_success_rate": round(100 * sum(t["status"] == "done" for t in finished) / len(finished), 1)
            if finished else None,
            "task_avg_seconds": round(sum(durs) / len(durs)) if durs else None,
            "task_avg_wait": round(sum(waits) / len(waits)) if waits else None,
            "tasks_active": sum(t["status"] in ("queued", "running") for t in all_tasks),
            "api_calls_today": api_today, "api_calls_7d": usage["total"], "api_errors_7d": usage["errors"],
        },
        "runs_per_day": _fill_days(per_day, days),
        "by_client": by_client.most_common(),
        "by_platform": Counter(v["platform"] for v in uniq.values()).most_common(20),
        "by_dimension": Counter(v["dim_name"] for v in vids).most_common(),
        "by_category": Counter((r["category"] or "未识别").split("／")[0].split("/")[0].strip()[:16]
                               for r in runs).most_common(10),
        "score_hist": [{"score": s, "count": sum(1 for v in uniq.values() if round(v["score"] or 0) == s)}
                       for s in range(5, 11)],
        "recent_runs": sorted(runs, key=lambda r: r["created"], reverse=True)[:8],
    }


def task_log(limit: int = 200, client: str = "", kind: str = "", status: str = "") -> list[dict]:
    out = []
    for t in tasks.recent(limit=100000):
        who = t["meta"].get("client") or WEB
        if client and who != client or kind and t["kind"] != kind or status and t["status"] != status:
            continue
        out.append({**t, "client": who, "wait": _secs(t["created"], t["started"]),
                    "duration": _secs(t["started"], t["finished"]),
                    "external_id": t["meta"].get("external_id"),
                    "platforms": len(t["meta"].get("platforms", []))})
        if len(out) >= limit:
            break
    return out
