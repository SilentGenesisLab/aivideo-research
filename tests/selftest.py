"""自测：python tests/selftest.py [产品图]      （不带图片只跑离线单元测试）"""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from refsearch import config, pipeline  # noqa: E402
from refsearch.llm import parse_json  # noqa: E402
from refsearch.platforms import platform_links  # noqa: E402
from refsearch.searchers import _dur  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  —— {detail}" if detail and not cond else ""))


def offline():
    print("【离线单元测试】")
    check("JSON 解析：围栏", parse_json('```json\n{"a":1}\n```') == {"a": 1})
    check("JSON 解析：前后有杂字", parse_json('好的：{"a":[1,2]} 以上') == {"a": [1, 2]})
    check("时长解析", (_dur("1:30"), _dur("1:02:03"), _dur(""), _dur("x")) == (90, 3723, None, None))
    vids = [{"platform": "YouTube", "score": s, "url": f"y{s}"} for s in (10, 9, 9, 8)] + \
           [{"platform": "Bilibili", "score": s, "url": f"b{s}"} for s in (7, 6)]
    got = pipeline._balanced(vids, 4)
    check("平台轮选：两个平台都入选", {v["platform"] for v in got} == {"YouTube", "Bilibili"}, got)
    check("平台轮选：按分数排序", [v["score"] for v in got] == sorted([v["score"] for v in got], reverse=True))
    links = platform_links({"product": {"brand": "X", "competitors": ["元气森林（无糖气泡水）"]},
                            "dimensions": [{"key": "category", "queries_zh": ["气泡水 广告"],
                                            "queries_en": ["soda ad"]},
                                           {"key": "award", "queries_en": ["Cannes Lions drinks"]}]})
    meta = next(l for l in links if l["name"].startswith("Meta"))
    cannes = next(l for l in links if l["name"].startswith("戛纳"))
    check("竞品名去括号", meta["query"] == "元气森林", meta["query"])
    check("戛纳关键词不重复", cannes["url"].lower().count("cannes") == 1, cannes["url"])
    from refsearch.player import embed_url
    cases = {"https://www.youtube.com/watch?v=drZIKbUMEuY": "youtube.com/embed/drZIKbUMEuY",
             "https://www.bilibili.com/video/BV1Bu4y1q7Yy": "bvid=BV1Bu4y1q7Yy",
             "https://v.youku.com/v_show/id_XNDc0Nzg5Mzk0OA==.html": "youku.com/embed/XNDc0Nzg5Mzk0OA",
             "https://v.qq.com/x/page/z0891zr71qn.html": "vid=z0891zr71qn",
             "https://www.douyin.com/video/7694849932179672360": "vid=7694849932179672360",
             "https://www.acfun.cn/v/ac41316767": "player/ac41316767",
             "https://www.dailymotion.com/video/x28fved": "embed/video/x28fved"}
    check("内嵌播放器地址", all(want in (embed_url(u) or "") for u, want in cases.items()),
          {u: embed_url(u) for u in cases})
    check("不可内嵌的站点返回空", embed_url("https://www.ispot.tv/ad/121P/x") is None)
    try:
        pipeline.load("../../etc")
        check("run_id 路径穿越防护", False)
    except (ValueError, FileNotFoundError):
        check("run_id 路径穿越防护", True)


def offline_filters():
    from refsearch import filters as flt
    f = flt.normalize({"types": ["ugc", "tvc", "bogus"], "type_mode": "only", "keywords": "开学季 vlog",
                       "exclude": ["合集"], "must": [], "min_duration": 10})
    check("筛选：未知类型被忽略", f["types"] == ["ugc", "tvc"], f)
    td = flt.type_dimension({"product": {"category": "电动牙刷", "search_en": "electric toothbrush"}}, f)
    check("类型定向：附加词在前、带类型词", td["queries_zh"][0] == "电动牙刷 开学季"
          and any("种草" in q for q in td["queries_zh"]) and any("TV commercial" in q for q in td["queries_en"]), td)
    check("类型定向：每种语言不超过上限", len(td["queries_zh"]) <= flt.MAX_TYPE_QUERIES >= len(td["queries_en"]))
    check("未选类型时不加定向维度", flt.type_dimension({"product": {}}, flt.normalize({})) is None)
    check("标题兜底识别类型", [flt.guess_type({"title": t}) for t in
                              ("某某牙刷 TVC 15s", "宿舍好物种草", "开箱测评", "Cannes Lions case film", "vlog")]
          == ["tvc", "ugc", "review", "case", "other"])
    check("硬筛选：排除词/时长", (flt.pre_filter({"title": "广告合集", "duration": 30}, f, 300),
                              flt.pre_filter({"title": "x", "duration": 5}, f, 300),
                              flt.pre_filter({"title": "x", "duration": 30}, f, 300)) == ("exclude", "duration", None))
    check("类型筛选：only 模式丢弃其他类型", (flt.post_filter({"vtype": "review"}, f),
                                            flt.post_filter({"vtype": "ugc"}, f)) == ("type", None))
    check("旧结果无筛选字段时视为不筛选", flt.describe(flt.normalize(None)) == "")


def probe(path):
    ff = config.ffmpeg_path()
    out = subprocess.run([ff, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    v = "h264" if "Video: h264" in out else out.split("Video: ")[1].split()[0] if "Video: " in out else "?"
    a = "aac" if "Audio: aac" in out else out.split("Audio: ")[1].split()[0] if "Audio: " in out else "?"
    return v, a


def online(image):
    print("\n【在线端到端测试：网页服务】")
    port = 8799
    srv = subprocess.Popen([sys.executable, "server.py", "--port", str(port)], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(40):
            try:
                requests.get(base, timeout=1); break
            except requests.ConnectionError:
                time.sleep(0.5)
        check("首页可访问", requests.get(base).status_code == 200)
        check("方法说明页可访问", "检索方法说明" in requests.get(f"{base}/method").text)
        check("首页禁止缓存", "no-cache" in requests.get(base).headers.get("cache-control", ""))
        srcs = requests.get(f"{base}/api/sources").json()
        check("渠道列表 ≥ 15 个", len(srcs) >= 15, len(srcs))
        r = requests.post(f"{base}/api/search", files={"image": ("a.txt", b"x")})
        check("拒绝非图片上传", r.status_code == 400)

        t = time.time()
        with open(image, "rb") as f:
            r = requests.post(f"{base}/api/search", files={"image": ("p.jpg", f, "image/jpeg")},
                              data={"brief": "30秒TVC，年轻白领，零糖无负担", "per_query": 8})
        jid = r.json()["job_id"]
        # 异步：第一个任务没跑完时，立刻再提交两个，都应秒回任务号并各自完成
        t1 = time.time()
        with open(image, "rb") as f:
            r2 = requests.post(f"{base}/api/search", files={"image": ("p.jpg", f, "image/jpeg")},
                               data={"brief": "并发测试", "platforms": "youku,digitaling", "per_query": 4})
        with open(image, "rb") as f:
            r3 = requests.post(f"{base}/api/search", files={"image": ("p.jpg", f, "image/jpeg")},
                               data={"brief": "并发测试2", "platforms": "acfun", "per_query": 4})
        check("异步：任务运行中可继续提交且秒回", r2.status_code == r3.status_code == 200 and time.time() - t1 < 5,
              f"{time.time() - t1:.1f}s")
        jids = [r2.json()["job_id"], r3.json()["job_id"]]
        lst = requests.get(f"{base}/api/tasks").json()
        check("异步：任务列表能看到全部任务", {jid, *jids} <= {x["id"] for x in lst})
        check("异步：超出并发数的任务在排队", any(x["status"] == "queued" for x in lst if x["id"] in jids),
              [(x["title"], x["status"], x["ahead"]) for x in lst[:3]])
        while (j := requests.get(f"{base}/api/jobs/{jid}").json())["status"] in ("queued", "running"):
            time.sleep(2)
        check("检索任务完成", j["status"] == "done", j.get("error"))
        res = j["result"]
        vids = [v for g in res["groups"] for v in g["videos"]]
        pfs = {v["platform"] for v in vids}
        print(f"     用时 {time.time() - t:.0f}s，{res['stats']}，平台分布 "
              f"{ {p: sum(v['platform'] == p for v in vids) for p in pfs} }")
        check("识别出产品品类", bool(res["analysis"]["product"].get("category")))
        check("生成 5 个参考维度", len(res["groups"]) == 5, len(res["groups"]))
        check("每个维度都有参考视频", all(g["videos"] for g in res["groups"]),
              [g["name"] for g in res["groups"] if not g["videos"]])
        check("结果覆盖 ≥ 8 个来源", len(pfs) >= 8, pfs)
        check("每条都有参考点和地址", all(v.get("reference") and v["url"].startswith("https://") for v in vids))
        check("参考平台链接 ≥ 10 个", len(res["platform_links"]) >= 10)
        b_fail = [e for e in res["errors"] if e.startswith("B 站")]
        check("B 站检索无失败（被风控时自动改走必应兜底）", not b_fail, b_fail)
        check("最终结果包含 B 站视频", "Bilibili" in pfs, pfs)
        check("报告文件可访问", requests.get(f"{base}/runs/{res['run_id']}/report.md").status_code == 200)

        # 勾选下载：每个平台各取最短的一条
        pick = [min((v for v in vids if v["platform"] == p), key=lambda v: v.get("duration") or 999)["url"]
                for p in sorted(pfs & {"YouTube", "Bilibili"})]
        r = requests.post(f"{base}/api/download", json={"run_id": res["run_id"], "urls": pick})
        jid = r.json()["job_id"]
        while (j := requests.get(f"{base}/api/jobs/{jid}").json())["status"] in ("queued", "running"):
            time.sleep(2)
        dls = j["result"]["downloads"]
        check("勾选下载全部成功", len(dls) == len(pick) and all(d["ok"] for d in dls),
              [d["error"] for d in dls if not d["ok"]])
        for d in dls:
            if d["ok"]:
                path = config.RUNS_DIR / res["run_id"] / d["file"]
                v, a = probe(str(path))
                check(f"编码 H.264+AAC：{Path(d['file']).name[:40]}", (v, a) == ("h264", "aac"), (v, a))
                url = f"{base}/runs/{res['run_id']}/{d['file']}"
                check("下载文件可通过网页访问", requests.head(url).status_code in (200, 405)
                      or requests.get(url, stream=True).status_code == 200)
        r = requests.post(f"{base}/api/download", json={"run_id": "../x", "urls": pick})
        check("下载接口拒绝非法 run_id", r.status_code == 404)

        hist = requests.get(f"{base}/api/runs").json()
        check("历史列表包含本次检索", any(h["run_id"] == res["run_id"] for h in hist))
        check("历史产品图可访问",
              requests.get(base + next(h["image"] for h in hist if h["run_id"] == res["run_id"])).status_code == 200)
        r = requests.post(f"{base}/api/rerun", json={"run_id": res["run_id"], "platforms": ["youku", "digitaling"]})
        jid = r.json()["job_id"]
        while (j := requests.get(f"{base}/api/jobs/{jid}").json())["status"] in ("queued", "running"):
            time.sleep(2)
        rr = j.get("result") or {}
        check("重新检索成功并生成新记录", j["status"] == "done" and rr.get("run_id") != res["run_id"], j.get("error"))
        check("重新检索只用了所选渠道", rr.get("sources") == ["youku", "digitaling"], rr.get("sources"))
        check("重新检索沿用原简报", rr.get("brief") == res["brief"])

        others = []
        for x in jids:
            while (o := requests.get(f"{base}/api/jobs/{x}").json())["status"] in ("queued", "running"):
                time.sleep(2)
            others.append(o)
        check("异步：并发任务全部完成", all(o["status"] == "done" for o in others),
              [o.get("error") for o in others])
        ids = {res["run_id"], *(o["run_id"] for o in others)}
        check("异步：每个任务结果目录独立", len(ids) == 3, ids)

        # 数据看板
        check("数据看板页面", requests.get(f"{base}/dashboard").status_code == 200)
        check("共享播放器脚本", "playAttrs" in requests.get(f"{base}/static/player.js").text)
        ov = requests.get(f"{base}/api/admin/overview").json()
        check("看板概览统计", ov["kpi"]["runs_total"] >= 1 and len(ov["runs_per_day"]) == 30, ov["kpi"])
        vs = requests.get(f"{base}/api/admin/videos", params={"min_score": 5, "limit": 5}).json()
        check("视频库检索", vs["total"] > 0 and len(vs["items"]) <= 5 and vs["all_platforms"], vs.get("total"))
        kw = vs["items"][0]["title"].split()[0]
        vk = requests.get(f"{base}/api/admin/videos", params={"q": kw}).json()
        check("视频库关键词检索", vk["total"] >= 1 and all(kw.lower() in (v["title"] + v["reference"] + v["channel"]
              + v["product"] + v["brand"] + v["category"] + v["platform"]).lower() for v in vk["items"]), kw)
        tl = requests.get(f"{base}/api/admin/tasks").json()
        check("任务日志含本次检索", any(t["run_id"] == res["run_id"] and t["duration"] for t in tl))
        check("接口调用统计", "by_client" in requests.get(f"{base}/api/admin/api-usage").json())

        # 站内播放
        r = requests.get(f"{base}/api/play", params={"url": "https://www.bilibili.com/video/BV1xx411c7mD"}).json()
        check("站内播放：B 站内嵌", r["kind"] == "iframe" and "player.bilibili.com" in r["src"], r)
        got = [d for d in (requests.get(f"{base}/runs/{res['run_id']}/result.json").json().get("downloads") or []) if d["ok"]]
        if got:
            r = requests.get(f"{base}/api/play", params={"url": got[0]["url"]}).json()
            check("站内播放：已下载的视频直接播放本地文件", r["kind"] == "video" and r.get("local")
                  and requests.get(base + r["src"], headers={"Range": "bytes=0-9"}).status_code == 206, r)
            jid = requests.post(f"{base}/api/download", json={"run_id": res["run_id"], "urls": [got[0]["url"]]}).json()["job_id"]
            t0 = time.time()
            while (j := requests.get(f"{base}/api/jobs/{jid}").json())["status"] in ("queued", "running"):
                time.sleep(0.5)
            check("下载缓存：同一视频再次下载直接复用", j["status"] == "done" and time.time() - t0 < 5
                  and any("命中下载缓存" in x for x in j["log"]), j["log"][-2:])
        th = requests.get(f"{base}/img", params={"run": res["run_id"], "w": 160})
        check("缩略图：产品图缩小为 webp 并长期缓存", th.headers.get("content-type") == "image/webp"
              and len(th.content) < 60000 and "immutable" in th.headers.get("cache-control", ""), len(th.content))
        vth = next(v["thumbnail"] for g in res["groups"] for v in g["videos"] if v.get("thumbnail"))
        th = requests.get(f"{base}/img", params={"u": vth, "w": 480})
        check("缩略图：外站封面由服务器代理缓存", th.status_code == 200 and th.headers.get("content-type", "").startswith("image/"))
        check("缩略图：拒绝内网地址", "svg" in requests.get(f"{base}/img", params={"u": "http://127.0.0.1:22/x.png"}).headers.get("content-type", ""))
        check("页面与接口启用 gzip", requests.get(f"{base}/api/runs", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") == "gzip")
        check("静态文件可缓存", "max-age" in requests.get(f"{base}/static/layout.js").headers.get("cache-control", ""))
        r = requests.get(f"{base}/api/play", params={"url": "https://www.digitaling.com/projects/167565.html"}).json()
        check("站内播放：数英案例页挖出播放器", r["kind"] == "iframe", r)
        r = requests.get(f"{base}/api/play", params={"url": "https://www.adsoftheworld.com/campaigns/michael-buble-makes-fresh-sparkling-water"}).json()
        check("站内播放：Ads of the World 解析出视频直链", r["kind"] == "video", r)
        check("站内播放：拒绝非 http 地址",
              requests.get(f"{base}/api/play", params={"url": "file:///etc/passwd"}).status_code == 400)
        relay_url = "https://www.youtube.com/watch?v=o_s2tvaVxt4"
        t0 = time.time()
        jid = requests.post(f"{base}/api/relay", json={"url": relay_url}).json()["job_id"]
        while (j := requests.get(f"{base}/api/jobs/{jid}").json())["status"] in ("queued", "running"):
            time.sleep(2)
        rr = j.get("result") or {}
        check("中转播放：拉取成功", rr.get("ok"), rr or j.get("error"))
        if rr.get("ok"):
            print(f"     中转用时 {time.time() - t0:.0f}s：{rr['src']}")
            check("中转播放：文件可访问", requests.get(base + rr["src"], headers={"Range": "bytes=0-99"}).status_code == 206)
            from refsearch.player import cached_file
            check("中转播放：再次打开直接命中缓存", cached_file(relay_url) == rr["src"])

        # 参考平台直达
        for pl in res["platform_links"]:
            if pl["direct"]:
                r = requests.get(f"{base}/api/platform_items", params={"name": pl["name"], "q": pl["query"]}).json()
                print(f"     {pl['name']}：{len(r['items'])} 条 {r.get('error', '')}")
        direct = [pl for pl in res["platform_links"] if pl["direct"]]
        check("参考平台：可直达的平台 ≥ 6 个", len(direct) >= 6, [p["name"] for p in direct])
        r = requests.get(f"{base}/api/platform_items", params={"name": "数英网 DIGITALING", "q": "气泡水"}).json()
        check("参考平台：数英直接返回作品", len(r["items"]) >= 3 and "/projects/" in r["items"][0]["url"], r)
        r = requests.get(f"{base}/api/platform_items", params={"name": "花瓣", "q": "气泡水 海报"}).json()
        check("参考平台：花瓣返回图片", len(r["items"]) >= 3 and r["items"][0]["media"] == "image", r)
        check("参考平台：需登录的平台返回 404",
              requests.get(f"{base}/api/platform_items", params={"name": "小红书", "q": "x"}).status_code == 404)
        print(f"     运行目录：runs/{res['run_id']}")
    finally:
        srv.terminate()


if __name__ == "__main__":
    offline()
    offline_filters()
    if len(sys.argv) > 1:
        online(sys.argv[1])
    print(f"\n通过 {len(PASS)}，失败 {len(FAIL)}" + (f"：{FAIL}" if FAIL else ""))
    sys.exit(1 if FAIL else 0)
