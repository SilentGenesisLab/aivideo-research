"""对外接口 /api/v1 端到端测试：.venv/bin/python tests/api_test.py <产品图>

在独立端口启动服务（临时 API Key + Webhook 密钥），本地起一个回调接收端验证 Webhook 签名。"""
import base64
import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PASS, FAIL = [], []
KEY, KEY2, HOOK_SECRET = "test_key_A_" + os.urandom(6).hex(), "test_key_B_" + os.urandom(6).hex(), "whsec_test"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + ("" if cond else f"  —— {str(detail)[:300]}"))


HOOKS = []


class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        HOOKS.append((dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def wait(base, h, tid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = requests.get(f"{base}/api/v1/tasks/{tid}", headers=h).json()
        if t["status"] in ("succeeded", "failed"):
            return t
        time.sleep(2)
    return t


def offline():
    print("【离线：安全工具】")
    from refsearch import security
    os.environ.pop("ALLOW_PRIVATE_URLS", None)
    for bad in ("http://127.0.0.1/x.jpg", "http://10.0.0.1/", "http://169.254.169.254/latest", "file:///etc/passwd",
                "http://localhost:8770/"):
        try:
            security.check_public_url(bad)
            check(f"拒绝内网地址 {bad}", False)
        except security.UnsafeURL:
            check(f"拒绝内网地址 {bad}", True)
    p = security.signed_path("x/product.jpg")
    exp, sig = p.split("exp=")[1].split("&sig=")
    check("签名链接校验通过", security.verify_file("x/product.jpg", int(exp), sig))
    check("篡改路径签名失效", not security.verify_file("y/product.jpg", int(exp), sig))
    check("过期签名失效", not security.verify_file("x/product.jpg", 1, sig))
    os.environ["API_KEYS"] = "siteA:k1, k2"
    check("API_KEYS 解析", security.client_of("k1") == "siteA" and security.client_of("k2") == "default"
          and security.client_of("k3") is None)


def online(image):
    print("\n【在线：/api/v1】")
    hook_srv = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=hook_srv.serve_forever, daemon=True).start()
    hook_url = f"http://127.0.0.1:{hook_srv.server_port}/hook"
    port = 8798
    env = {**os.environ, "API_KEYS": f"siteA:{KEY},siteB:{KEY2}", "WEBHOOK_SECRET": HOOK_SECRET,
           "ALLOW_PRIVATE_URLS": "1", "APP_PASSWORD": "web-pass-for-test"}
    srv = subprocess.Popen([sys.executable, "server.py", "--port", str(port)], cwd=ROOT, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base, h, h2 = f"http://127.0.0.1:{port}", {"X-API-Key": KEY}, {"Authorization": f"Bearer {KEY2}"}
    try:
        for _ in range(40):
            try:
                requests.get(f"{base}/api/health", timeout=1); break
            except requests.ConnectionError:
                time.sleep(0.5)
        r = requests.get(f"{base}/api/v1/sources")
        check("无 Key 返回 401 + 统一错误格式", r.status_code == 401 and r.json()["error"]["code"] == "unauthorized", r.text)
        check("错误 Key 返回 401", requests.get(f"{base}/api/v1/sources", headers={"X-API-Key": "bad"}).status_code == 401)
        check("网页仍需登录", requests.get(base).status_code == 401)
        check("接口文档公开可看", requests.get(f"{base}/api/v1/docs").status_code == 200)
        spec = requests.get(f"{base}/api/v1/openapi.json").json()
        check("OpenAPI 含全部接口", len(spec["paths"]) >= 12, list(spec["paths"]))
        srcs = requests.get(f"{base}/api/v1/sources", headers=h).json()
        check("渠道列表", len(srcs) >= 15 and {"key", "name", "group", "languages"} <= set(srcs[0]))
        check("Bearer 方式鉴权", requests.get(f"{base}/api/v1/sources", headers=h2).status_code == 200)

        b64 = base64.b64encode(Path(image).read_bytes()).decode()
        r = requests.post(f"{base}/api/v1/searches", headers=h, json={"image_base64": b64, "sources": ["nope"]})
        check("未知渠道 400", r.status_code == 400 and r.json()["error"]["code"] == "invalid_sources", r.text)
        r = requests.post(f"{base}/api/v1/searches", headers=h, json={"brief": "x"})
        check("缺图片 400", r.status_code == 400 and r.json()["error"]["code"] == "invalid_image", r.text)
        r = requests.post(f"{base}/api/v1/searches", headers=h, json={"image_base64": b64, "per_query": 99})
        check("参数越界 422", r.status_code == 422 and r.json()["error"]["code"] == "invalid_request", r.text)
        r = requests.post(f"{base}/api/v1/searches", headers=h, json={"image_base64": "data:image/png;base64,AAAA"})
        check("非图片 base64 400", r.status_code == 400, r.text)

        t0 = time.time()
        r = requests.post(f"{base}/api/v1/searches", headers=h, json={
            "image_base64": "data:image/jpeg;base64," + b64, "brief": "30秒TVC，年轻白领，零糖",
            "sources": ["youku", "digitaling", "acfun", "bilibili"], "per_query": 5, "max_duration": 120,
            "download": "top", "download_top": 1, "callback_url": hook_url, "external_id": "order-123"})
        check("创建检索 202 秒回", r.status_code == 202 and time.time() - t0 < 5, r.text)
        created = r.json()
        with open(image, "rb") as f:
            r = requests.post(f"{base}/api/v1/searches/upload", headers=h2, files={"image": ("p.jpg", f, "image/jpeg")},
                              data={"brief": "上传方式", "sources": "youku", "per_query": "3"})
        check("multipart 上传创建检索", r.status_code == 202, r.text)
        up = r.json()
        check("其他站点看不到别人的任务",
              requests.get(f"{base}/api/v1/tasks/{created['task_id']}", headers=h2).status_code == 404)
        lst = requests.get(f"{base}/api/v1/tasks", headers=h).json()
        check("任务列表只含本站点任务", lst and all(x["task_id"] != up["task_id"] for x in lst))

        t = wait(base, h, created["task_id"])
        print(f"     用时 {time.time() - t0:.0f}s，状态 {t['status']}")
        check("任务成功", t["status"] == "succeeded", t.get("error"))
        check("任务带回 external_id 和结果地址", t["external_id"] == "order-123" and t["result_url"], t)
        run = requests.get(t["result_url"], headers=h).json()
        check("结果结构完整", {"product", "dimensions", "reference_platforms", "product_image_url"} <= set(run)
              and len(run["dimensions"]) == 5, list(run))
        vids = [v for d in run["dimensions"] for v in d["videos"]]
        check("结果有视频与参考点", vids and all(v["url"] and "reference" in v for v in vids), len(vids))
        check("可内嵌视频带 embed_url", any(v["embed_url"] for v in vids))
        img = requests.get(run["product_image_url"])
        check("产品图签名链接免登录可访问", img.status_code == 200 and img.headers["content-type"].startswith("image/"))
        check("篡改签名 403", requests.get(run["product_image_url"].replace("sig=", "sig=0")).status_code == 403)
        dls = [v["download"] for v in vids if v["download"]]
        check("顺带下载执行了 1 条（个别站点下载失败会写明原因）", len(dls) == 1 and (dls[0]["ok"] or dls[0]["error"]), dls)
        check("其他站点读不到别人的结果",
              requests.get(f"{base}/api/v1/runs/{run['run_id']}", headers=h2).status_code == 404)
        check("历史列表", any(x["run_id"] == run["run_id"] for x in requests.get(f"{base}/api/v1/runs", headers=h).json()))

        time.sleep(1)
        hook = next((x for x in HOOKS if json.loads(x[1])["task"]["task_id"] == created["task_id"]), None)
        check("收到 Webhook 回调", hook is not None)
        if hook:
            hd, body = hook
            want = "sha256=" + hmac.new(HOOK_SECRET.encode(), hd["X-Webhook-Timestamp"].encode() + b"." + body,
                                        hashlib.sha256).hexdigest()
            check("Webhook 签名正确", hd.get("X-Webhook-Signature") == want)
            check("Webhook 事件为成功", json.loads(body)["event"] == "task.succeeded")

        pick = [v["url"] for v in vids if v["platform"] in ("Bilibili", "AcFun", "YouTube")][:2]
        r = requests.post(f"{base}/api/v1/runs/{run['run_id']}/downloads", headers=h,
                          json={"urls": pick} if pick else {"top": 2})
        check("追加下载任务", r.status_code == 202, r.text)
        d = wait(base, h, r.json()["task_id"])
        check("下载任务完成", d["status"] == "succeeded" and d["type"] == "download", d.get("error"))
        run = requests.get(t["result_url"], headers=h).json()
        ok = [v["download"] for dd in run["dimensions"] for v in dd["videos"] if v["download"] and v["download"]["ok"]]
        print(f"     下载成功 {len(ok)} 条")
        if ok:
            r = requests.get(ok[0]["file_url"], headers={"Range": "bytes=0-99"})
            check("下载文件签名链接免登录、支持断点续传", r.status_code == 206, r.status_code)

        r = requests.post(f"{base}/api/v1/runs/{run['run_id']}/rerun", headers=h, json={"sources": ["acfun"]})
        check("重新检索", r.status_code == 202, r.text)

        r = requests.get(f"{base}/api/v1/play", headers=h, params={"url": "https://www.bilibili.com/video/BV1Bu4y1q7Yy"}).json()
        check("播放解析", r["kind"] == "iframe", r)
        r = requests.post(f"{base}/api/v1/relay", headers=h, json={"url": "https://www.youtube.com/watch?v=o_s2tvaVxt4"})
        rt = wait(base, h, r.json()["task_id"], 300)
        check("中转播放给出 play_url", rt["status"] == "succeeded" and rt["play_url"], rt)
        if rt.get("play_url"):
            check("play_url 免登录可播放", requests.get(rt["play_url"], headers={"Range": "bytes=0-9"}).status_code == 206)
        r = requests.get(f"{base}/api/v1/reference-platforms/items", headers=h, params={"name": "花瓣", "q": "气泡水"})
        check("参考平台作品", r.status_code == 200 and len(r.json()) >= 3, r.text[:200])
        r = requests.get(f"{base}/api/v1/reference-platforms/items", headers=h, params={"name": "小红书", "q": "x"})
        check("不支持的参考平台 404", r.status_code == 404 and r.json()["error"]["code"] == "not_found")
        auth = ("admin", "web-pass-for-test")
        usage = requests.get(f"{base}/api/admin/api-usage", auth=auth).json()
        names = {c["client"] for c in usage["by_client"]}
        check("接口调用日志按调用方记录", {"siteA", "siteB", "未带 Key", "无效 Key"} <= names, names)
        check("接口调用日志记录路由模板", any(r["route"] == "GET /api/v1/tasks/{task_id}" for r in usage["by_route"]),
              [r["route"] for r in usage["by_route"]])
        check("接口调用日志统计错误", usage["errors"] >= 5, usage["errors"])
        u = wait(base, h2, up["task_id"])
        check("multipart 任务完成", u["status"] == "succeeded", u.get("error"))
    finally:
        srv.terminate()
        hook_srv.shutdown()


if __name__ == "__main__":
    offline()
    if len(sys.argv) > 1:
        online(sys.argv[1])
    print(f"\n通过 {len(PASS)}，失败 {len(FAIL)}" + (f"：{FAIL}" if FAIL else ""))
    sys.exit(1 if FAIL else 0)
