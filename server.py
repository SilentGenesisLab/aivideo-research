"""网页界面：python server.py [--host 127.0.0.1] [--port 8765]

在 .env 配置 APP_PASSWORD 后启用登录（HTTP Basic Auth，用户名 APP_USER，默认 admin）。
未配置密码时拒绝监听非本机地址。"""
import argparse
import base64
import os
import secrets
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from refsearch import config, pipeline, platforms, player
from refsearch.searchers import SOURCES

app = FastAPI(title="参考视频检索")
APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if APP_PASSWORD:
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


JOBS: dict[str, dict] = {}
WEB = Path(__file__).parent / "web"


def _start(fn) -> str:
    jid = uuid.uuid4().hex[:12]
    job = JOBS[jid] = {"status": "running", "log": [], "result": None, "error": None}

    def run():
        try:
            job["result"] = fn(lambda m: job["log"].append(m))
            job["status"] = "done"
        except Exception as e:
            job["error"] = str(e)
            job["status"] = "error"

    threading.Thread(target=run, daemon=True).start()
    return jid


NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/")
def index():
    return FileResponse(WEB / "index.html", headers=NO_CACHE)


@app.get("/method")
def method():
    return FileResponse(WEB / "method.html", headers=NO_CACHE)


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

    return {"job_id": _start(work)}


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
    tmp = Path(tempfile.mkstemp(suffix=suffix)[1])
    tmp.write_bytes(data)
    pfs = tuple(p for p in platforms.split(",") if p in pipeline.DEFAULT_SOURCES) or pipeline.DEFAULT_SOURCES

    def work(progress):
        try:
            return pipeline.run_search(str(tmp), brief, pfs, per_query, max_duration,
                                       download_mode=download_mode, download_top=download_top,
                                       progress=progress)
        finally:
            tmp.unlink(missing_ok=True)

    return {"job_id": _start(work)}


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
        pipeline.download_into(result, vids, req.max_height, progress)
        return result

    return {"job_id": _start(work)}


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
    return {"job_id": _start(lambda progress: player.relay(req.url, progress))}


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


@app.get("/api/jobs/{jid}")
def job(jid: str):
    if jid not in JOBS:
        raise HTTPException(404, "任务不存在")
    return JOBS[jid]


app.mount("/runs", StaticFiles(directory=config.RUNS_DIR), name="runs")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost") and not APP_PASSWORD:
        sys.exit("对外监听前请先在 .env 配置 APP_PASSWORD")
    uvicorn.run(app, host=a.host, port=a.port)
