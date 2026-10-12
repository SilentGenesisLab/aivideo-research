"""最小接入示例：提交产品图 → 等待结果 → 打印每个维度的参考视频。

    export REFSEARCH_URL=http://<服务地址>:8770
    export REFSEARCH_KEY=sk_live_xxx
    python examples/python_client.py product.jpg "30秒TVC，年轻白领，零糖" [渠道,逗号分隔]

只依赖 requests。生产环境建议用 callback_url 接收回调，代替轮询。
"""
import os
import sys
import time

import requests

BASE = os.environ.get("REFSEARCH_URL", "http://127.0.0.1:8770").rstrip("/")
HEADERS = {"X-API-Key": os.environ["REFSEARCH_KEY"]}


def api(method: str, path: str, **kw) -> dict:
    r = requests.request(method, BASE + "/api/v1" + path, headers=HEADERS, timeout=60, **kw)
    if r.status_code >= 400:
        err = r.json().get("error", {})
        raise RuntimeError(f"{r.status_code} {err.get('code')}: {err.get('message')}")
    return r.json()


def search(image: str, brief: str = "", sources: str = "") -> dict:
    with open(image, "rb") as f:
        task = api("POST", "/searches/upload", files={"image": f},
                   data={"brief": brief, "sources": sources, "external_id": os.path.basename(image)})
    print("任务已提交：", task["task_id"])
    while True:
        t = api("GET", f"/tasks/{task['task_id']}")
        if t["status"] == "succeeded":
            return api("GET", f"/runs/{t['run_id']}")
        if t["status"] == "failed":
            raise RuntimeError(t["error"])
        print(f"  [{t['status']}]", t["progress"] or f"排队中，前面 {t['queue_position']} 个")
        time.sleep(4)


if __name__ == "__main__":
    run = search(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "", sys.argv[3] if len(sys.argv) > 3 else "")
    p = run["product"]
    print(f"\n产品：{p['brand']} · {p['name']}（{p['category']}）")
    for d in run["dimensions"]:
        print(f"\n【{d['name']}】参考什么：{d['what_to_learn']}")
        for v in d["videos"][:3]:
            print(f"  {v['score']}分 [{v['platform']}] {v['title']}\n      {v['url']}\n      参考：{v['reference']}")
    print("\n报告：", run["report_url"])
