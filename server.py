"""网页界面：python server.py [--host 127.0.0.1] [--port 8765]

在 .env 配置 APP_PASSWORD 后启用登录（HTTP Basic Auth，用户名 APP_USER，默认 admin）。
未配置密码时拒绝监听非本机地址。"""
import argparse
import base64
import os
import secrets
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import api_v1
from refsearch import config, pipeline, platforms, player, security, tasks
from refsearch.searchers import SOURCES

app = FastAPI(title="参考视频检索", docs_url=None, redoc_url=None, openapi_url=None)  # 对外文档见 /api/v1/docs
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    # /api/v1 用 API Key 鉴权，/files 用签名校验，都不走网页登录
    path = request.url.path
    public = path in ("/api/health", "/developer") or path.startswith(("/api/v1/", "/files/", "/developer/"))
    if APP_PASSWORD and not public:
        ok = False
        auth = request.headers.get("authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
                ok = (secrets.compare_digest(user, APP_USER)
                      & secrets.compare_digest(pwd, APP_PASSWORD))
            except Exception:
                ok = False
        if not ok:
            return Response("需要登录", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="ref-video-search", charset="UTF-8"'})
    return await call_next(request)


WEB = Path(__file__).parent / "web"
UPLOADS = config.RUNS_DIR / "_uploads"


NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers=NO_CACHE)


@app.get("/method")
def method():
    return FileResponse(WEB / "method.html", headers=NO_CACHE)


DOCS = Path(__file__).parent / "docs"
EXAMPLES = {"python_client.py": "text/x-python", "node_client.mjs": "text/javascript"}


@app.get("/developer", include_in_schema=False)
def developer(request: Request):
    """对外接入文档（公开，不需要登录）：渲染 docs/API.md，并把示例地址换成本服务的实际地址。"""
    import html
    import markdown
    from markdown.extensions.toc import slugify_unicode

    base = html.escape(security.absolute(f"{request.url.scheme}://{request.url.netloc}/", "").rstrip("/"))
    src = (DOCS / "API.md").read_text(encoding="utf-8")
    src = src.replace("https://<服务地址>", base).replace("<服务地址>", base.split("://", 1)[1])
    md = markdown.Markdown(extensions=["tables", "fenced_code", "toc"],
                           extension_configs={"toc": {"slugify": slugify_unicode, "toc_depth": "2-3"}})
    body = md.convert(src)
    page = (WEB / "developer.html").read_text(encoding="utf-8")
    return Response(page.replace("{{TOC}}", md.toc).replace("{{CONTENT}}", body),
                    media_type="text/html; charset=utf-8", headers=NO_CACHE)


@app.get("/developer/examples/{name}", include_in_schema=False)
def developer_example(name: str):
    if name not in EXAMPLES:
        raise HTTPException(404, "示例不存在")
    return FileResponse(Path(__file__).parent / "examples" / name, media_type=f"{EXAMPLES[name]}; charset=utf-8")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/sources")
def sources():
    return [{"key": k, "name": v[2], "group": v[3]} for k, v in SOURCES.items()]


@app.get("/api/runs")
def runs():
    return pipeline.list_runs()


class RerunReq(BaseModel):
    run_id: str
    platforms: list[str] = []
    brief: str | None = None


@app.post("/api/rerun")
def rerun(req: RerunReq):
    try:
        old = pipeline.load(req.run_id)
        image = pipeline.run_image(req.run_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "找不到该次检索")
    pfs = tuple(p for p in req.platforms if p in SOURCES) or pipeline.DEFAULT_SOURCES
    brief = old.get("brief", "") if req.brief is None else req.brief

    def work(progress):
        return pipeline.run_search(str(image), brief, pfs, progress=progress)

    title = (old.get("analysis", {}).get("product", {}).get("name") or brief or req.run_id)[:40]
    return {"job_id": tasks.submit("search", f"重新检索：{title}", work,
                                   {"from_run": req.run_id, "image": f"/runs/{req.run_id}/{image.name}",
                                    "platforms": list(pfs)})}


@app.post("/api/search")
async def search(image: UploadFile = File(...), brief: str = Form(""),
                 platforms: str = Form(",".join(pipeline.DEFAULT_SOURCES)), per_query: int = Form(8),
                 max_duration: int = Form(300), download_mode: str = Form("none"),
                 download_top: int = Form(5)):
    suffix = Path(image.filename or "x.jpg").suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
        raise HTTPException(400, "仅支持 jpg / png / webp")
    if download_mode not in ("none", "top", "all"):
        raise HTTPException(400, "download_mode 无效")
    data = await image.read()
    if len(data) > 15 * 1024 * 1024:
        raise HTTPException(400, "图片不能超过 15MB")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    tmp = UPLOADS / f"{uuid.uuid4().hex}{suffix}"
    tmp.write_bytes(data)
    pfs = tuple(p for p in platforms.split(",") if p in pipeline.DEFAULT_SOURCES) or pipeline.DEFAULT_SOURCES

    def work(progress):
        try:
            return pipeline.run_search(str(tmp), brief, pfs, per_query, max_duration,
                                       download_mode=download_mode, download_top=download_top,
                                       progress=progress)
        finally:
            tmp.unlink(missing_ok=True)

    title = brief.strip().splitlines()[0][:40] if brief.strip() else (image.filename or "产品图")
    return {"job_id": tasks.submit("search", title, work, {"platforms": list(pfs)})}


class DownloadReq(BaseModel):
    run_id: str
    urls: list[str]
    max_height: int = 720


@app.post("/api/download")
def download(req: DownloadReq):
    try:
        result = pipeline.load(req.run_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "找不到该次检索")
    vids = pipeline.find_videos(result, req.urls)
    if not vids:
        raise HTTPException(400, "没有选中有效视频")

    def work(progress):
        latest = pipeline.load(req.run_id)  # 排队期间可能已有别的下载写入，取最新的
        pipeline.download_into(latest, pipeline.find_videos(latest, req.urls), req.max_height, progress)
        return latest

    return {"job_id": tasks.submit("download", f"下载 {len(vids)} 条视频", work, {"run_id": req.run_id})}


@app.get("/api/play")
def play(url: str):
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "地址无效")
    return player.resolve(url)


class RelayReq(BaseModel):
    url: str


@app.post("/api/relay")
def relay(req: RelayReq):
    if not req.url.startswith(("http://", "https://")):
        raise HTTPException(400, "地址无效")
    return {"job_id": tasks.submit("relay", "中转播放", lambda progress: player.relay(req.url, progress))}


@app.get("/api/direct_platforms")
def direct_platforms():
    return [p["name"] for p in platforms.PLATFORMS if "fetch" in p]


@app.get("/api/platform_items")
def platform_items(name: str, q: str):
    try:
        return {"items": platforms.fetch_items(name, q)}
    except StopIteration:
        raise HTTPException(404, "该平台不支持直接抓取")
    except Exception as e:
        return {"items": [], "error": f"抓取失败：{e}"[:200]}


@app.get("/api/tasks")
def task_list():
    return tasks.recent()


@app.get("/api/jobs/{jid}")
@app.get("/api/tasks/{jid}")
def job(jid: str):
    t = tasks.get(jid)
    if not t:
        raise HTTPException(404, "任务不存在")
    out = {**t, "ahead": tasks.position(t)}
    if t["status"] == "done" and t["kind"] != "relay" and t["run_id"]:
        try:
            out["result"] = pipeline.load(t["run_id"])
        except (FileNotFoundError, ValueError):
            out["result"] = None
    return out


@app.get("/files/{path:path}", include_in_schema=False)
def signed_file(path: str, exp: int = 0, sig: str = ""):
    """对外接口给出的限时签名链接（产品图、下载的视频、报告、中转缓存）。"""
    f = (config.RUNS_DIR / path).resolve()
    if (not security.verify_file(path, exp, sig) or not f.is_relative_to(config.RUNS_DIR.resolve())
            or f.name.startswith(".") or not f.is_file()):
        raise HTTPException(403, "链接无效或已过期")
    return FileResponse(f)


app.mount("/api/v1", api_v1.api)
app.mount("/runs", StaticFiles(directory=config.RUNS_DIR), name="runs")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost") and not APP_PASSWORD:
        sys.exit("对外监听前请先在 .env 配置 APP_PASSWORD")
    uvicorn.run(app, host=a.host, port=a.port)
