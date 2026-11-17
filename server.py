"""网页界面：python server.py [--host 127.0.0.1] [--port 8765]

在 .env 配置 APP_PASSWORD 后启用登录（登录页 /login，用户名 APP_USER，默认 admin；脚本可用 HTTP Basic Auth）。
未配置密码时拒绝监听非本机地址。"""
import argparse
import json
import os
import sys
import uuid
from urllib.parse import quote
from pathlib import Path

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

import api_v1
import time

from refsearch import auth, config, downloader, filters, images, pipeline, platforms, player, security, stats, tasks
from refsearch.searchers import SOURCES

app = FastAPI(title="参考视频检索", docs_url=None, redoc_url=None, openapi_url=None)  # 对外文档见 /api/v1/docs
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


PUBLIC_PATHS = ("/api/health", "/developer", "/login", "/api/login", "/logout",
                "/favicon.ico", "/favicon.svg", "/apple-touch-icon.png")


@app.middleware("http")
async def login_required(request: Request, call_next):
    """网页登录：签名 Cookie 会话（登录页），脚本/curl 仍可用 HTTP Basic Auth。
    /api/v1 用 API Key 鉴权，/files 用签名校验，都不走网页登录。"""
    path = request.url.path
    public = path in PUBLIC_PATHS or path.startswith(("/api/v1/", "/files/", "/developer/"))
    if auth.enabled() and not public:
        if not (auth.read_session(request.cookies.get(auth.COOKIE))
                or auth.check_basic(request.headers.get("authorization", ""))):
            if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
                target = path + (f"?{request.url.query}" if request.url.query else "")
                return RedirectResponse(f"/login?next={quote(target)}", status_code=302)
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
    return await call_next(request)


@app.middleware("http")
async def api_log(request: Request, call_next):
    """记录 /api/v1 每次调用（谁、哪个接口、状态码、耗时），供数据看板“接口调用”使用。"""
    path = request.url.path
    if not path.startswith("/api/v1/") or path.startswith(("/api/v1/docs", "/api/v1/openapi", "/api/v1/redoc")):
        return await call_next(request)
    t0 = time.time()
    key = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    who = (security.client_of(key) if key else None) or ("无效 Key" if key else "未带 Key")
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        tpl = "/api/v1" + route.path if route is not None and hasattr(route, "path") else path
        # 经 nginx 时用它写入的 X-Real-IP（客户端无法伪造）；request.client 已由 uvicorn 按可信代理处理
        ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "")
        stats.log_request(who, request.method, tpl, path, status, int((time.time() - t0) * 1000), ip)


stats.init()
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


IMG_CACHE = {"Cache-Control": "public, max-age=2592000, immutable"}


@app.get("/img", include_in_schema=False)
def img(u: str = "", run: str = "", w: int = 480):
    """缩略图：外站封面（u=地址）或某次检索的产品图（run=ID），缩小后缓存，浏览器缓存 30 天。"""
    if run:
        try:
            name = pipeline.load(run).get('image', 'product.jpg')
        except (FileNotFoundError, ValueError):
            raise HTTPException(404)
        path = images.local_thumb(f"{run}/{name}", w) if name else None  # 文字检索没有产品图
    elif u.startswith(("http://", "https://")):
        path = images.remote_thumb(u, w)
    else:
        raise HTTPException(400)
    if not path:  # 外站图片拿不到：返回占位图（短缓存，稍后会重试）
        return Response(_PLACEHOLDER, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=600"})
    return FileResponse(path, media_type="image/webp", headers=IMG_CACHE)


_PLACEHOLDER = (b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 9"><rect width="16" height="9" fill="#1f2937"/>'
                b'<path d="M6.5 2.8v3.4L9.5 4.5z" fill="#6b7280"/></svg>')


class LoginReq(BaseModel):
    username: str
    password: str
    remember: bool = True


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    if not auth.enabled() or auth.read_session(request.cookies.get(auth.COOKIE)):
        return RedirectResponse("/", status_code=302)
    return FileResponse(WEB / "login.html", headers=NO_CACHE)


@app.post("/api/login", include_in_schema=False)
def login(req: LoginReq, request: Request):
    ip = _client_ip(request)
    if wait := auth.blocked(ip):
        raise HTTPException(429, f"尝试次数过多，请 {wait // 60 + 1} 分钟后再试")
    if not auth.check(req.username.strip(), req.password):
        auth.record_fail(ip)
        raise HTTPException(401, "用户名或密码错误")
    auth.clear_fails(ip)
    token, ttl = auth.make_session(req.remember)
    resp = JSONResponse({"ok": True, "user": auth.user()})
    resp.set_cookie(auth.COOKIE, token, max_age=ttl if req.remember else None, httponly=True,
                    secure=request.url.scheme == "https", samesite="lax", path="/")
    return resp


@app.get("/logout", include_in_schema=False)
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/api/me", include_in_schema=False)
def me(request: Request):
    return {"user": auth.read_session(request.cookies.get(auth.COOKIE)) or auth.user()}


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
@app.get("/apple-touch-icon.png", include_in_schema=False)
def favicon(request: Request):
    return FileResponse(WEB / request.url.path.lstrip("/"), headers={"Cache-Control": "public, max-age=86400"})


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return FileResponse(WEB / "dashboard.html", headers=NO_CACHE)


@app.get("/api/admin/overview")
def admin_overview(days: int = 30):
    return stats.overview(min(max(days, 7), 180))


@app.get("/api/admin/videos")
def admin_videos(q: str = "", platform: str = "", dim: str = "", min_score: float = 0, client: str = "",
                 downloaded: bool = False, dedupe: bool = True, sort: str = "score",
                 limit: int = 60, offset: int = 0, vtype: str = "", ad_only: bool = False, exclude: str = ""):
    return stats.search_videos(q, platform, dim, min_score, client, downloaded, dedupe, sort,
                               min(max(limit, 1), 200), max(offset, 0), vtype, ad_only, exclude)


@app.get("/api/admin/tasks")
def admin_tasks(client: str = "", kind: str = "", status: str = "", limit: int = 200):
    return stats.task_log(min(max(limit, 1), 1000), client, kind, status)


@app.get("/api/admin/api-usage")
def admin_api_usage(days: int = 7, client: str = "", errors_only: bool = False, limit: int = 100):
    return {**stats.api_usage(min(max(days, 1), 90)),
            "recent": stats.recent_requests(min(max(limit, 1), 500), client, errors_only)}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/sources")
def sources():
    return [{"key": k, "name": v[2], "group": v[3], "languages": list(v[1])} for k, v in SOURCES.items()]


@app.get("/api/depths")
def depths():
    return [{"key": k, **v} for k, v in pipeline.DEPTHS.items()]


@app.get("/api/video_types")
def video_types():
    return filters.type_list()


def _filters(raw: str) -> dict:
    try:
        return filters.normalize(json.loads(raw) if raw.strip() else {})
    except (ValueError, AttributeError):
        raise HTTPException(400, "filters 不是有效的 JSON")


@app.get("/api/runs")
def runs():
    return pipeline.list_runs()


class RerunReq(BaseModel):
    run_id: str
    platforms: list[str] = []
    brief: str | None = None
    filters: dict | None = None  # 不传则沿用原筛选条件
    depth: str | None = None  # 不传则沿用原检索深度
    target_count: int | None = None  # 不传则沿用原目标数量；0 表示不设目标


# ---------------- 检索 / 下载任务（提交与失败重试共用） ----------------
def _search_job(spec: dict) -> str:
    """spec：image（服务器上的图片路径）或 query（[检索词, brand/product]，文字检索）、brief、platforms、
    opts（run_search 参数）、title、meta、cleanup。"""
    image = Path(spec["image"]) if spec.get("image") else None
    query, qtype = spec.get("query") or (None, None)
    if image and not image.exists():
        raise tasks.RetryError(410, "原图已被清理，请重新上传图片后检索")

    def work(progress):
        res = pipeline.run_search(str(image) if image else None, spec["brief"], tuple(spec["platforms"]),
                                  progress=progress, query=query, query_type=qtype, **spec["opts"])
        if image and spec.get("cleanup"):  # 上传的临时图只在成功后删；失败时留着，供“重试”使用
            image.unlink(missing_ok=True)
        return res

    return tasks.submit("search", spec["title"], work, dict(spec["meta"]), retry={**spec, "handler": "web_search"})


def _download_job(spec: dict) -> str:
    def work(progress):
        latest = pipeline.load(spec["run_id"])  # 排队期间可能已有别的下载写入，取最新的
        pipeline.download_into(latest, pipeline.find_videos(latest, spec["urls"]), spec["max_height"], progress)
        return latest

    return tasks.submit("download", spec["title"], work, {"run_id": spec["run_id"]},
                        retry={**spec, "handler": "web_download"})


tasks.register_retry("web_search", _search_job)
tasks.register_retry("web_download", _download_job)


def _clean_uploads(days: int = 7):
    """失败任务留下的上传图（供重试）超过 7 天就删掉。"""
    cutoff = time.time() - days * 86400
    for f in UPLOADS.glob("*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


@app.post("/api/tasks/{jid}/retry")
def retry_task(jid: str):
    try:
        return {"job_id": tasks.retry(jid)}
    except tasks.RetryError as e:
        raise HTTPException(e.status, str(e))


@app.post("/api/rerun")
def rerun(req: RerunReq):
    try:
        old = pipeline.load(req.run_id)
        text = pipeline.run_query(req.run_id)
        image = None if text else pipeline.run_image(req.run_id)
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "找不到该次检索")
    pfs = tuple(p for p in req.platforms if p in SOURCES) or pipeline.DEFAULT_SOURCES
    brief = old.get("brief", "") if req.brief is None else req.brief

    f = filters.normalize(old.get("filters") if req.filters is None else req.filters)
    o = old.get("options") or {}
    depth = req.depth or o.get("depth") or "standard"
    target = o.get("target_count") if req.target_count is None else (req.target_count or None)

    title = (old.get("analysis", {}).get("product", {}).get("name") or brief or req.run_id)[:40]
    return {"job_id": _search_job({
        "image": str(image) if image else None, "query": list(text) if text else None,
        "brief": brief, "platforms": list(pfs), "cleanup": False, "title": f"重新检索：{title}",
        "opts": {"filters": f, "depth": depth, "target_count": target,
                 "min_score": o.get("min_score", 5), "max_duration": o.get("max_duration", 300)},
        "meta": {"from_run": req.run_id, "image": f"/runs/{req.run_id}/{image.name}" if image else "", "platforms": list(pfs)}})}


@app.post("/api/search")
async def search(image: UploadFile = File(...), brief: str = Form(""),
                 platforms: str = Form(",".join(pipeline.DEFAULT_SOURCES)), per_query: int | None = Form(None),
                 depth: str = Form("standard"), target_count: int | None = Form(None),
                 max_duration: int = Form(300), download_mode: str = Form("none"),
                 download_top: int = Form(5), min_score: int = Form(5), filters: str = Form("")):
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
    f = _filters(filters)
    min_score = min(max(min_score, 0), 10)

    title = brief.strip().splitlines()[0][:40] if brief.strip() else (image.filename or "产品图")
    return {"job_id": _search_job({
        "image": str(tmp), "brief": brief, "platforms": list(pfs), "cleanup": True, "title": title,
        "opts": {"per_query": per_query, "max_duration": max_duration, "min_score": min_score,
                 "download_mode": download_mode, "download_top": download_top, "depth": depth,
                 "target_count": target_count, "filters": f},
        "meta": {"platforms": list(pfs)}})}


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

    return {"job_id": _download_job({"run_id": req.run_id, "urls": [v["url"] for v in vids],
                                     "max_height": req.max_height, "title": f"下载 {len(vids)} 条视频"})}


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
    out = {**{k: v for k, v in t.items() if k != "retry"}, "ahead": tasks.position(t),
           "can_retry": t["status"] == "error" and not t.get("retried_by") and bool(t.get("retry"))}
    if t["status"] == "running" and t["kind"] == "search" and t["run_id"]:
        out["partial"] = pipeline.load_partial(t["run_id"])
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


class CachedStatic(StaticFiles):
    """页面引用静态文件时都带 ?v=版本号，可以放心让浏览器缓存 7 天。"""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=604800"
        return resp


app.mount("/api/v1", api_v1.api)
app.mount("/static", CachedStatic(directory=WEB), name="static")
app.mount("/runs", StaticFiles(directory=config.RUNS_DIR), name="runs")

class SelectiveGZip:
    """压缩页面、脚本和 JSON；视频、图片、下载文件不压缩（已是压缩格式，且要支持断点续传）。"""
    SKIP = ("/runs/", "/files/", "/img", "/favicon", "/apple-touch-icon")

    def __init__(self, app):
        self.app, self.gzip = app, GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (not path.startswith(self.SKIP) or path.endswith((".json", ".md"))) \
                and not path.endswith((".mp4", ".png", ".jpg", ".webp", ".ico")):
            return await self.gzip(scope, receive, send)
        return await self.app(scope, receive, send)


app.add_middleware(SelectiveGZip)
downloader.backfill()
_clean_uploads()

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.host not in ("127.0.0.1", "localhost") and not APP_PASSWORD:
        sys.exit("对外监听前请先在 .env 配置 APP_PASSWORD")
    uvicorn.run(app, host=a.host, port=a.port)
