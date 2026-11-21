"""全域搜索探针：真跑一次单站检索，验证 count>0 与耗时；异常退出码非 0。

每站是一次计费的 Tavily 调用，所以探针只打 1 个站。2026-11-19 实测：3 站 × 每 5 分钟
= 1728 credits/天，两天就能烧光月额度，而探针只需要回答"链路通不通"。

配置了 TAVILY_API_KEY 时额外查一次额度：2026-11-19 额度耗尽曾让搜索静默全挂 20 分钟。
plan 或 paygo 任一用到 90% 时探针退出码也置非 0。

用法：
  tools/probe_pages.py [--sites bilibili] [--budget 60] [--max-ms 45000]
退出码：0 正常；1 异常（cron 可据此告警）
输出追加到 runs/_probe_pages.log；每次一行 JSON。
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from refsearch import config, webgrab  # noqa: E402

LOG = config.RUNS_DIR / "_probe_pages.log"
QUERY = "咖啡 品牌 广告"
QUOTA_WARN = 0.90   # plan 用到这个比例就报警
# hybrid 下 Tavily 只服务少数回落站（约 3/24），用量速度降一个量级，用 90% 会天天响
QUOTA_WARN_HYBRID = 0.97


def _warn_at() -> float:
    return QUOTA_WARN_HYBRID if webgrab.PROVIDER == "hybrid" else QUOTA_WARN



def tavily_quota() -> dict | None:
    """查 Tavily 额度。没配 key 或查询失败返回 None，不影响主探针。"""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return None
    try:
        req = urllib.request.Request("https://api.tavily.com/usage",
                                     headers={"Authorization": "Bearer " + key})
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open(req, timeout=15) as r:
            j = json.loads(r.read().decode())
        a = j.get("account") or {}
        used, limit = a.get("plan_usage"), a.get("plan_limit")
        paygo, paygo_limit = a.get("paygo_usage"), a.get("paygo_limit")
        ratio = round(used / limit, 3) if (used is not None and limit) else None
        # 2026-11-19 实测：爆的是 paygo（2005/2000），plan 只用了 75%。
        # 只盯 plan 会完全漏掉真正会打满的那个池子。
        paygo_ratio = round(paygo / paygo_limit, 3) if (paygo is not None and paygo_limit) else None
        return {"plan_usage": used, "plan_limit": limit, "ratio": ratio,
                "paygo_usage": paygo, "paygo_limit": paygo_limit, "paygo_ratio": paygo_ratio,
                "over_warn": bool((ratio is not None and ratio >= _warn_at())
                                  or (paygo_ratio is not None and paygo_ratio >= _warn_at()))}
    except Exception as e:  # noqa: BLE001 - 额度查询失败不该影响功能探测
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sites", default="bilibili,douyin,youtube")
    ap.add_argument("--budget", type=float, default=60)
    ap.add_argument("--max-ms", type=int, default=45000)
    a = ap.parse_args()
    sites = [s.strip() for s in a.sites.split(",") if s.strip()]

    t0 = time.time()
    rec = {"ts": round(t0, 3), "provider": webgrab.PROVIDER, "sites": sites, "ok": False, "reason": None}
    try:
        items, by_site = webgrab.search_sites(QUERY, sites, 3, False, budget=a.budget)
        rec["total_ms"] = int((time.time() - t0) * 1000)
        rec["count"] = len(items)
        rec["by_site"] = [{k: b.get(k) for k in ("site", "count", "ok", "error_class", "ms")}
                          for b in by_site]
        bad = [b for b in by_site if b.get("error_class")]
        reasons = []
        if not items:
            reasons.append("零结果")
        if bad:
            reasons.append("异常站 " + ",".join("%s:%s" % (b["site"], b["error_class"]) for b in bad))
        if rec["total_ms"] > a.max_ms:
            reasons.append("耗时 %dms 超阈值 %dms" % (rec["total_ms"], a.max_ms))
        rec["ok"] = not reasons
        rec["reason"] = "; ".join(reasons) or None
    except webgrab.WebGrabError as e:
        rec["total_ms"] = int((time.time() - t0) * 1000)
        rec["reason"] = "WebGrabError[%s] %s" % (e.error_class, e.message)
    except Exception as e:  # noqa: BLE001 - 探针要把任何异常都记下来
        rec["total_ms"] = int((time.time() - t0) * 1000)
        rec["reason"] = "%s: %s" % (type(e).__name__, str(e)[:200])

    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass

    # 只有实际在用 Tavily 时额度才有意义；博查后端下这是假警报
    q = tavily_quota() if webgrab.PROVIDER in ("tavily", "hybrid") else None
    if q is not None:
        rec["quota"] = q
        if q.get("over_warn"):
            rec["ok"] = False
            parts = []
            if q.get("ratio") is not None and q["ratio"] >= _warn_at():
                parts.append("plan %.0f%%（%s/%s）" % (q["ratio"] * 100, q["plan_usage"], q["plan_limit"]))
            if q.get("paygo_ratio") is not None and q["paygo_ratio"] >= _warn_at():
                parts.append("paygo %.0f%%（%s/%s）" % (q["paygo_ratio"] * 100,
                                                       q["paygo_usage"], q["paygo_limit"]))
            if parts:
                rec["reason"] = ((rec.get("reason") or "") + "; " if rec.get("reason") else "") + \
                    "Tavily 额度告警：" + "，".join(parts)
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": rec["ts"], "type": "quota", "quota": q},
                                   ensure_ascii=False) + "\n")
        except OSError:
            pass

    print(json.dumps(rec, ensure_ascii=False))
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
