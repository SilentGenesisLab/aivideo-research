"""数据看板：接口调用日志（SQLite）、全部检索结果的视频库索引、概览统计。"""
import json
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import config, filters, tasks

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
        # 全域搜索：批次级汇总 + 逐站明细。requests 只有状态码，看不出"哪一站变薄了"
        c.execute("""CREATE TABLE IF NOT EXISTS page_searches (
            ts REAL, day TEXT, batch_id TEXT, client TEXT, query TEXT, ip TEXT,
            sites_requested INTEGER, sites_total INTEGER, sites_normal INTEGER,
            sites_empty INTEGER, sites_never_arrived INTEGER, sites_timeout INTEGER,
            sites_upstream_fail INTEGER, sites_config_error INTEGER,
            total_ms INTEGER, slowest_site TEXT, slowest_ms INTEGER,
            sites_dropped_filter INTEGER, sites_rate_limited INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS page_search_sites (
            batch_id TEXT, ts REAL, site TEXT, ok INTEGER, count INTEGER,
            error_class TEXT, ms INTEGER, start_ts REAL, end_ts REAL,
            raw_count INTEGER)""")
        # 老库补列（2026-11-19 之前建的没有这两列）
        for tbl, col in (("page_searches", "sites_dropped_filter INTEGER"),
                         ("page_searches", "sites_rate_limited INTEGER"),
                         ("page_search_sites", "raw_count INTEGER")):
            cols = {r[1] for r in c.execute("PRAGMA table_info(%s)" % tbl)}
            if col.split()[0] not in cols:
                c.execute("ALTER TABLE %s ADD COLUMN %s" % (tbl, col))
        c.execute("CREATE INDEX IF NOT EXISTS idx_ps_ts ON page_searches(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pss_batch ON page_search_sites(batch_id)")
        c.execute("DELETE FROM page_searches WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))
        c.execute("DELETE FROM page_search_sites WHERE ts < ?", (time.time() - KEEP_DAYS * 86400,))


def log_request(client: str, method: str, route: str, path: str, status: int, ms: int, ip: str):
    now = time.time()
    try:
        with _lock, _conn() as c:
            c.execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?)",
                      (now, time.strftime("%Y-%m-%d", time.localtime(now)), client, method, route,
                       path[:300], status, ms, ip))
    except sqlite3.Error:
        pass


def log_page_search(client: str, ip: str, query: str, by_site: list[dict], sites_requested: int,
                    total_ms: int, batch_id: str | None = None) -> None:
    """记录一次全域搜索：批次汇总 + 逐站明细。

    error_class 为 None 才算正常；empty_result（Tavily 真返回 0 条）和 off_domain
    （Tavily 返回了但都是站外结果）要分开计——只看状态码会把它俩都当成成功。
    """
    if not by_site:
        return
    now = time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    batch_id = batch_id or by_site[0].get("batch_id") or ""
    cls = Counter(b.get("error_class") for b in by_site)
    slow = max(by_site, key=lambda b: b.get("ms") or 0)
    try:
        with _lock, _conn() as c:
            c.execute("INSERT INTO page_searches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (now, day, batch_id, client, query[:200], ip[:60],
                       sites_requested, len(by_site), cls.get(None, 0),
                       cls.get("empty_result", 0), cls.get("never_arrived", 0),
                       cls.get("timeout", 0), cls.get("upstream_fail", 0),
                       cls.get("config_error", 0) + cls.get("invalid", 0),
                       total_ms, slow.get("site"), slow.get("ms") or 0,
                       cls.get("dropped_by_filter", 0), cls.get("rate_limited", 0)))
            c.executemany(
                "INSERT INTO page_search_sites (batch_id, ts, site, ok, count, error_class,"
                " ms, start_ts, end_ts, raw_count) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(batch_id, now, b.get("site"), 1 if b.get("ok") else 0,
                  b.get("count") or 0, b.get("error_class"), b.get("ms") or 0,
                  b.get("start_ts") or now, b.get("end_ts") or now,
                  b.get("raw_count")) for b in by_site])
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
                    # 加类型识别之前的旧结果没有 vtype，按标题关键词粗分
                    "vtype": (vt := v.get("vtype") or filters.guess_type(v)), "vtype_name": filters.TYPE_NAMES[vt],
                    "vtype_guessed": not v.get("vtype"),
                    "local": f"/runs/{r['run_id']}/{d['file']}" if d and d.get("ok") else ""})
                run["kept"] += 1
        runs.append(run)
    _index.update(sig=sig, runs=runs, videos=videos)
    return _index


def search_videos(q: str = "", platform: str = "", dim: str = "", min_score: float = 0, client: str = "",
                  downloaded: bool = False, dedupe: bool = True, sort: str = "score",
                  limit: int = 60, offset: int = 0, vtype: str = "", ad_only: bool = False,
                  exclude: str = "") -> dict:
    vids = _load_index()["videos"]
    words = [w.lower() for w in q.split() if w]
    bad = [w.lower() for w in exclude.split() if w]
    types = set(vtype.split(",")) - {""}

    def hit(v):
        if platform and v["platform"] != platform or dim and v["dim"] != dim or client and v["client"] != client:
            return False
        if (v["score"] or 0) < min_score or downloaded and not v["local"] or ad_only and not v["is_ad"]:
            return False
        if types and v["vtype"] not in types:
            return False
        text = " ".join(str(v[k] or "") for k in ("title", "reference", "channel", "product", "brand",
                                                    "category", "platform")).lower()
        return all(w in text for w in words) and not any(w in text for w in bad)

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
    type_facets = Counter(v["vtype"] for v in found)
    return {"total": len(found), "items": found[offset:offset + limit],
            "platforms": facets.most_common(),
            "types": [[k, filters.TYPE_NAMES[k], type_facets[k]] for k in filters.TYPE_NAMES if type_facets[k]], "all_platforms": sorted({v["platform"] for v in vids}),
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
        "by_type": Counter(v["vtype_name"] for v in uniq.values()).most_common(),
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
        out.append({**t, "client": who,  # recent() 已去掉重试规格并算好 can_retry "wait": _secs(t["created"], t["started"]),
                    "duration": _secs(t["started"], t["finished"]),
                    "external_id": t["meta"].get("external_id"),
                    "platforms": len(t["meta"].get("platforms", []))})
        if len(out) >= limit:
            break
    return out
