"""对外开放接口 /api/v1：供其他网站后端接入。

- 鉴权：请求头 X-API-Key: <key>（或 Authorization: Bearer <key>），key 在 .env 的 API_KEYS 配置。
- 检索、下载、中转播放都是异步任务：创建后返回 task_id，轮询 GET /tasks/{id} 或等 Webhook 回调。
- 每个 API Key 只能看到自己创建的任务和检索结果。
- 结果里的产品图、下载文件、报告都换成免登录的限时签名链接，可直接放进 <img>/<video>。
- 错误统一为 {"error": {"code": "...", "message": "..."}}。
"""
import base64
import binascii
import json
import threading
import time
import uuid
from urllib.parse import unquote
from pathlib import Path
from typing import Literal

import requests
from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from refsearch import config, filters as flt, pipeline, platforms, player, security, stats, tasks
from refsearch.searchers import SOURCES

UPLOADS = config.RUNS_DIR / "_uploads"
STATUS = {"queued": "queued", "running": "running", "done": "succeeded", "error": "failed"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


# ---------------- 鉴权 ----------------
_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="API Key")
_bearer = HTTPBearer(auto_error=False, description="与 X-API-Key 二选一：Authorization: Bearer <key>")


def client(key: str | None = Depends(_key_header),
           bearer: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> str:
    token = key or (bearer.credentials if bearer else None)
    name = security.client_of(token) if token else None
    if not name:
        raise ApiError(401, "unauthorized", "缺少或无效的 API Key")
    return name


router = APIRouter(dependencies=[Depends(client)])


# ---------------- 数据模型（即接口文档里的 Schema） ----------------
class Source(BaseModel):
    key: str = Field(description="渠道标识，创建检索时传入 sources")
    name: str
    group: str = Field(description="国内视频 / 海外视频 / 广告案例库")
    languages: list[str] = Field(description="该渠道使用的关键词语言")


class Filters(BaseModel):
    types: list[Literal[tuple(flt.VIDEO_TYPES)]] = Field(
        [], description="目标视频类型，见 GET /video-types：tvc / brand / ugc / product / story / cg / case / review；不传则不限")
    type_mode: Literal["prefer", "only"] = Field(
        "prefer", description="prefer：优先这些类型（打分倾斜）；only：只保留这些类型")
    keywords: list[str] = Field([], description="附加检索词，会拼上品类词单独检索，如 [\"开学季\", \"vlog\"]")
    must: list[str] = Field([], description="结果的标题/频道/简介必须包含其中任一词")
    exclude: list[str] = Field([], description="排除词，标题/频道/简介含任一词即丢弃，如 [\"合集\", \"教程\"]")
    min_duration: int = Field(0, ge=0, le=3600, description="最短秒数")
    ad_only: bool = Field(False, description="只保留广告正片（is_ad=true）")


class SearchCreate(BaseModel):
    image_url: str | None = Field(None, description="产品图公网地址（jpg/png/webp，≤15MB）。image_url / image_base64 / query 三选一")
    image_base64: str | None = Field(None, description="产品图 base64，可带 data:image/...;base64, 前缀")
    query: str | None = Field(None, min_length=1, max_length=100,
                              description="文字检索：品牌名或产品名（不传产品图时使用），配合 query_type")
    query_type: Literal["brand", "product"] = Field(
        "product", description="query 的含义：brand 品牌（检索该品牌全部产品线 + 品牌官方 + 竞品）/ product 产品（本品 + 同品类 + 竞品等）")
    brief: str = Field("", max_length=2000, description="简报：目标、人群、时长、调性、想参考的竞品等，越具体越准")
    sources: list[str] | None = Field(None, description="检索渠道 key 列表，见 GET /sources；不传则全部")
    depth: Literal["quick", "standard", "deep"] = Field(
        "standard", description="检索深度：quick 快速（每维度每语言 1 个关键词、每词 6 条，约 1 分钟）/ "
                                "standard 标准（2 个、8 条，2~4 分钟）/ deep 深度（3 个、15 条，4~8 分钟）")
    target_count: int | None = Field(None, ge=5, le=200, description="目标结果总数，会在各维度间分配；"
                                     "达标视频不够时会少于目标。传了就忽略 top_per_dim")
    per_query: int | None = Field(None, ge=1, le=20, description="每个关键词每个渠道取几条；不传则按 depth")
    max_duration: int = Field(300, ge=10, le=1800, description="视频最长秒数，只要 TVC 建议 90")
    top_per_dim: int = Field(10, ge=1, le=30, description="每个参考维度最多保留几条")
    min_score: int = Field(5, ge=0, le=10, description="AI 评分低于此值的丢弃")
    filters: Filters = Field(default_factory=Filters, description="类型定向与结果筛选")
    download: Literal["none", "top", "all"] = Field("none", description="检索完是否顺带下载：不下载 / 前 N 条 / 全部")
    download_top: int = Field(5, ge=1, le=50, description="download=top 时下载几条")
    max_height: Literal[360, 480, 720, 1080] = Field(720, description="下载清晰度上限")
    callback_url: str | None = Field(None, description="任务结束时 POST 通知的地址（Webhook）")
    external_id: str | None = Field(None, max_length=128, description="调用方自己的业务 ID，原样带回")


class RerunCreate(BaseModel):
    sources: list[str] | None = Field(None, description="不传则全部渠道")
    brief: str | None = Field(None, description="不传则沿用原简报")
    filters: Filters | None = Field(None, description="不传则沿用原筛选条件")
    depth: Literal["quick", "standard", "deep"] | None = Field(None, description="不传则沿用原检索深度")
    target_count: int | None = Field(None, ge=0, le=200, description="不传则沿用原目标数量；0 表示不设目标")
    callback_url: str | None = None
    external_id: str | None = Field(None, max_length=128)


class DownloadCreate(BaseModel):
    urls: list[str] | None = Field(None, description="要下载的视频地址（取自结果里的 videos[].url）")
    top: int | None = Field(None, ge=1, le=50, description="或者：下载评分最高的前 N 条")
    max_height: Literal[360, 480, 720, 1080] = 720
    callback_url: str | None = None


class RelayCreate(BaseModel):
    url: str = Field(description="视频页面地址")


class Task(BaseModel):
    task_id: str
    type: Literal["search", "download", "relay"]
    status: Literal["queued", "running", "succeeded", "failed"]
    title: str
    queue_position: int = Field(description="排队中时，前面还有几个同类任务")
    progress: str = Field(description="最新一条进度说明")
    percent: int = Field(0, description="进度百分比 0~100（检索任务；识图 0~15，检索 15~55，AI 打分 55~98）")
    stage: str = Field("", description="当前阶段，如“多平台检索 32/70”")
    created_at: str
    started_at: str | None
    finished_at: str | None
    error: str | None
    run_id: str | None = Field(description="检索/下载对应的结果 ID；检索任务开始运行后就有")
    result_url: str | None = Field(description="结果地址：GET /api/v1/runs/{run_id}")
    can_retry: bool = Field(False, description="失败且还没重试过：可调用 POST /tasks/{task_id}/retry")
    retry_of: str | None = Field(None, description="这是哪个失败任务的重试")
    retried_by: str | None = Field(None, description="已被哪个新任务重试")
    partial_url: str | None = Field(None, description="检索进行中：中间结果地址 GET /api/v1/runs/{run_id}/partial")
    play_url: str | None = Field(None, description="仅 relay 任务：可直接播放的 mp4 地址")
    external_id: str | None = None


class TaskCreated(BaseModel):
    task_id: str
    status: str
    status_url: str


class Download(BaseModel):
    ok: bool
    file_url: str | None = Field(description="mp4 限时下载链接")
    size_mb: float
    error: str | None


class Video(BaseModel):
    platform: str
    url: str = Field(description="原视频页面地址")
    title: str
    channel: str
    duration: int | None = Field(description="秒")
    views: int | None
    thumbnail: str
    published: str
    score: float = Field(description="AI 参考价值评分 0~10")
    is_ad: bool | None = Field(description="是否为广告正片")
    video_type: str | None = Field(description="视频类型 key（AI 判断），见 GET /video-types；other 为其他")
    video_type_name: str | None
    reference: str = Field(description="具体参考点")
    reason: str
    embed_url: str | None = Field(description="可直接放进 iframe 的官方播放器地址；null 时调用 /play 解析")
    download: Download | None


class Dimension(BaseModel):
    key: str = Field(description="type 类型定向（指定了 filters.types 或 keywords 时才有）/ category 同品类 / "
                                 "competitor 竞品 / emotion 情绪主题 / structure 结构形式 / award 获奖标杆")
    name: str
    what_to_learn: str = Field(description="这个维度参考什么")
    queries_zh: list[str]
    queries_en: list[str]
    pending: bool = Field(False, description="仅中间结果：该维度还在检索/打分")
    videos: list[Video]


class ReferencePlatform(BaseModel):
    name: str
    kind: str
    kind_name: str
    learn: str
    query: str
    search_url: str
    direct: bool = Field(description="true 时可调用 /reference-platforms/items 直接取作品")


class Run(BaseModel):
    run_id: str
    created_at: str
    external_id: str | None
    brief: str
    query: str | None = Field(None, description="文字检索的检索词；图片检索为 null")
    query_type: str | None = Field(None, description="brand / product；图片检索为 null")
    sources: list[str]
    product_image_url: str | None = Field(description="产品图限时链接；文字检索为 null")
    report_url: str = Field(description="Markdown 报告限时链接")
    product: dict = Field(description="产品画像：brand/name/category/appearance/selling_points/target_audience/tone/competitors")
    dimensions: list[Dimension]
    reference_platforms: list[ReferencePlatform]
    filters: dict = Field(description="本次使用的筛选条件")
    options: dict = Field(description="本次使用的检索参数：depth/target_count/per_query/top_per_dim/min_score/max_duration")
    stats: dict = Field(description="queries/raw/kept/seconds；target 目标数量、eligible 达标总数、filtered 各原因筛掉的条数")
    errors: list[str]


class RunSummary(BaseModel):
    run_id: str
    created_at: str
    external_id: str | None
    brief: str
    query: str | None = None
    query_type: str | None = None
    product: str
    category: str
    kept: int
    product_image_url: str | None


class PlayInfo(BaseModel):
    kind: Literal["iframe", "video", "relay"] = Field(
        description="iframe：src 放进 <iframe>；video：src 放进 <video>；relay：需调用 POST /relay 由服务器中转")
    src: str | None


class PlatformItem(BaseModel):
    platform: str
    url: str
    title: str
    thumbnail: str
    duration: int | None = None
    media: Literal["video", "image"]


# ---------------- 序列化 ----------------
def _abs(request_base: str, path: str) -> str:
    return security.absolute(request_base, path)


def _file(base: str, run_id: str, rel: str) -> str:
    return _abs(base, security.signed_path(f"{run_id}/{rel}"))


def task_out(t: dict, base: str) -> dict:
    play = None
    if t["kind"] == "relay" and t.get("result", {}) and t["result"].get("src"):
        play = _abs(base, security.signed_path(t["result"]["src"].removeprefix("/runs/")))
    return {
        "task_id": t["id"], "type": t["kind"], "status": STATUS[t["status"]], "title": t["title"],
        "queue_position": tasks.position(t), "progress": t["log"][-1] if t["log"] else "",
        "created_at": t["created"], "started_at": t["started"], "finished_at": t["finished"],
        "error": t["error"], "run_id": t["run_id"],
        "result_url": _abs(base, f"/api/v1/runs/{t['run_id']}") if t["run_id"] and t["status"] == "done" else None,
        "play_url": play, "external_id": t["meta"].get("external_id"),
        "retry_of": t["meta"].get("retry_of"), "retried_by": t.get("retried_by"),
        "can_retry": t["status"] == "error" and not t.get("retried_by") and bool(t.get("retry")),
        "percent": t.get("pct") or (100 if t["status"] == "done" else 0), "stage": t.get("stage") or "",
        "partial_url": _abs(base, f"/api/v1/runs/{t['run_id']}/partial")
        if t["kind"] == "search" and t["run_id"] and t["status"] == "running" else None,
    }


def run_out(r: dict, base: str) -> dict:
    dl = {d["url"]: d for d in r.get("downloads", [])}
    rid = r["run_id"]

    def video(v):
        d = dl.get(v["url"])
        return {**{k: v.get(k) for k in ("platform", "url", "title", "channel", "duration", "views",
                                          "thumbnail", "published", "is_ad", "reason")},
                "score": v.get("score", 0), "reference": v.get("reference", ""),
                "video_type": v.get("vtype"), "video_type_name": flt.TYPE_NAMES.get(v.get("vtype")),
                "embed_url": player.embed_url(v["url"]),
                "download": None if not d else {
                    "ok": d["ok"], "size_mb": d.get("size_mb", 0), "error": d.get("error"),
                    "file_url": _file(base, rid, d["file"]) if d.get("file") else None}}

    return {
        "run_id": rid, "created_at": r.get("created", ""), "external_id": r.get("external_id"),
        "brief": r.get("brief", ""), "query": r.get("query"), "query_type": r.get("query_type"),
        "sources": r.get("sources", []),
        "product_image_url": _file(base, rid, r.get("image", "product.jpg")) if r.get("image", "product.jpg") else None,
        "report_url": _file(base, rid, "report.md"),
        "product": r["analysis"].get("product", {}),
        "dimensions": [{**{k: g.get(k) for k in ("key", "name", "what_to_learn")},
                        "queries_zh": g.get("queries_zh", []), "queries_en": g.get("queries_en", []),
                        "pending": bool(g.get("pending")),
                        "videos": [video(v) for v in g["videos"]]} for g in r["groups"]],
        "reference_platforms": [{**{k: p.get(k, "") for k in ("name", "kind", "kind_name", "learn", "query")},
                                 "search_url": p["url"], "direct": "fetch" in next(
                                     (x for x in platforms.PLATFORMS if x["name"] == p["name"]), {})}
                                for p in r.get("platform_links", [])],
        "filters": flt.normalize(r.get("filters")), "options": r.get("options") or {},
        "stats": r.get("stats", {}), "errors": r.get("errors", []),
    }


def _base(request: Request) -> str:
    """站点根地址（不含 /api/v1）。反向代理/HTTPS 部署时请在 .env 设 PUBLIC_BASE_URL。"""
    return f"{request.url.scheme}://{request.url.netloc}/"


def _own_run(run_id: str, who: str) -> dict:
    try:
        r = pipeline.load(run_id)
    except (FileNotFoundError, ValueError):
        raise ApiError(404, "not_found", "检索结果不存在")
    if r.get("client") != who:
        raise ApiError(404, "not_found", "检索结果不存在")
    return r


def _check_callback(url: str | None):
    if url:
        try:
            security.check_public_url(url)
        except security.UnsafeURL as e:
            raise ApiError(400, "invalid_callback_url", str(e))


def _notifier(callback_url: str | None, base: str):
    """任务结束后 POST 回调，失败重试 3 次（间隔 5s/30s/120s）。"""
    if not callback_url:
        return None

    def send(t: dict):
        body = json.dumps({"event": "task.succeeded" if t["status"] == "done" else "task.failed",
                           "task": task_out(t, base)}, ensure_ascii=False).encode()

        def run():
            for delay in (0, 5, 30, 120):
                time.sleep(delay)
                ts = str(int(time.time()))
                headers = {"Content-Type": "application/json", "X-Webhook-Timestamp": ts,
                           "X-Webhook-Id": t["id"]}
                if security.webhook_secret():
                    headers["X-Webhook-Signature"] = security.sign_webhook(body, ts)
                try:
                    security.check_public_url(callback_url)
                    if requests.post(callback_url, data=body, headers=headers, timeout=10).status_code < 300:
                        return
                except Exception:
                    continue

        threading.Thread(target=run, daemon=True).start()

    return send


def _sources(keys: list[str] | None) -> tuple:
    if not keys:
        return pipeline.DEFAULT_SOURCES
    bad = [k for k in keys if k not in SOURCES]
    if bad:
        raise ApiError(400, "invalid_sources", f"未知渠道：{', '.join(bad)}，可用值见 GET /api/v1/sources")
    return tuple(keys)


def _launch(image: Path | None, p: SearchCreate | RerunCreate, brief: str, pfs: tuple, who: str, base: str,
            title: str, cleanup: bool, opts: dict | None = None, query: tuple[str, str] | None = None) -> dict:
    """image 与 query（(检索词, brand/product)）二选一。"""
    tid = _search_job({"image": str(image) if image else None, "query": query, "brief": brief, "platforms": list(pfs), "who": who,
                       "external_id": p.external_id, "callback_url": p.callback_url, "base": base,
                       "title": title, "cleanup": cleanup, "opts": opts or {}})
    return {"task_id": tid, "status": "queued", "status_url": _abs(base, f"/api/v1/tasks/{tid}")}


def _search_job(spec: dict) -> str:
    """提交与失败重试共用。上传的临时图只在成功后删除，失败时留着供重试。"""
    image, who, ext_id = Path(spec["image"]) if spec.get("image") else None, spec["who"], spec["external_id"]
    query, qtype = (spec.get("query") or (None, None))
    if image and not image.exists():
        raise tasks.RetryError(410, "原图已被清理，请重新创建检索")

    def work(progress):
        res = pipeline.run_search(str(image) if image else None, spec["brief"], tuple(spec["platforms"]), progress=progress,
                                  extra={"client": who, "external_id": ext_id}, query=query, query_type=qtype, **spec["opts"])
        if image and spec.get("cleanup"):
            image.unlink(missing_ok=True)
        res["client"], res["external_id"] = who, ext_id
        pipeline.save(res)
        return res

    return tasks.submit("search", spec["title"], work,
                        {"client": who, "external_id": ext_id, "platforms": list(spec["platforms"])},
                        on_finish=_notifier(spec["callback_url"], spec["base"]),
                        retry={**spec, "handler": "api_search"})


def _download_job(spec: dict) -> str:
    def work(progress):
        latest = pipeline.load(spec["run_id"])
        pipeline.download_into(latest, pipeline.find_videos(latest, spec["urls"]), spec["max_height"], progress)
        return latest

    return tasks.submit("download", f"下载 {len(spec['urls'])} 条视频", work,
                        {"client": spec["who"], "run_id": spec["run_id"], "external_id": spec["external_id"]},
                        on_finish=_notifier(spec["callback_url"], spec["base"]),
                        retry={**spec, "handler": "api_download"})


tasks.register_retry("api_search", _search_job)
tasks.register_retry("api_download", _download_job)


def _save_upload(data: bytes, ext: str) -> Path:
    if len(data) > 15 * 1024 * 1024:
        raise ApiError(400, "image_too_large", "图片不能超过 15MB")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    path = UPLOADS / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(data)
    return path


def _opts(p: SearchCreate) -> dict:
    return {"per_query": p.per_query, "depth": p.depth, "target_count": p.target_count, "max_duration": p.max_duration, "top_per_dim": p.top_per_dim,
            "min_score": p.min_score, "download_mode": p.download, "download_top": p.download_top,
            "max_height": p.max_height, "filters": p.filters.model_dump()}


# ---------------- 接口 ----------------
class VideoType(BaseModel):
    key: str
    name: str
    desc: str


@router.get("/video-types", response_model=list[VideoType], summary="视频类型列表（用于 filters.types）", tags=["基础"])
def list_video_types():
    return flt.type_list()


@router.get("/sources", response_model=list[Source], summary="检索渠道列表", tags=["基础"])
def list_sources():
    return [{"key": k, "name": v[2], "group": v[3], "languages": list(v[1])} for k, v in SOURCES.items()]


@router.post("/searches", response_model=TaskCreated, status_code=202, tags=["检索"],
             summary="创建检索任务（JSON，图片用 URL 或 base64）")
def create_search(p: SearchCreate, request: Request, who: str = Depends(client)):
    query = (p.query or "").strip()
    if sum(map(bool, (p.image_url, p.image_base64, query))) != 1:
        raise ApiError(400, "invalid_image", "image_url、image_base64、query 必须且只能传一个")
    _check_callback(p.callback_url)
    pfs = _sources(p.sources)
    if query:  # 文字检索：品牌名 / 产品名，不需要产品图
        label = "品牌" if p.query_type == "brand" else "产品"
        return _launch(None, p, p.brief, pfs, who, _base(request), f"{label}：{query}"[:40], False, _opts(p),
                       query=(query, p.query_type))
    if p.image_url:
        try:
            data, ext = security.fetch_image(p.image_url)
        except security.UnsafeURL as e:
            raise ApiError(400, "invalid_image", str(e))
        except requests.RequestException as e:
            raise ApiError(400, "image_fetch_failed", f"图片下载失败：{e}"[:300])
    else:
        raw = p.image_base64.split(",", 1)[1] if p.image_base64.startswith("data:") else p.image_base64
        try:
            data = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError):
            raise ApiError(400, "invalid_image", "image_base64 不是有效的 base64")
        ext = ".png" if data[:4] == b"\x89PNG" else ".webp" if data[8:12] == b"WEBP" else ".jpg"
        if ext == ".jpg" and data[:2] != b"\xff\xd8":
            raise ApiError(400, "invalid_image", "仅支持 jpg / png / webp")
    path = _save_upload(data, ext)
    return _launch(path, p, p.brief, pfs, who, _base(request),
                   p.brief.strip()[:40] or "API 检索", True, _opts(p))


@router.post("/searches/upload", response_model=TaskCreated, status_code=202, tags=["检索"],
             summary="创建检索任务（multipart 上传图片）")
async def create_search_upload(request: Request, image: UploadFile = File(..., description="产品图 jpg/png/webp"),
                               brief: str = Form(""), sources: str = Form("", description="逗号分隔，不传则全部"),
                               per_query: int | None = Form(None), max_duration: int = Form(300),
                               depth: str = Form("standard"), target_count: int | None = Form(None),
                               top_per_dim: int = Form(10), min_score: int = Form(5),
                               download: str = Form("none"), download_top: int = Form(5),
                               max_height: int = Form(720), callback_url: str = Form(""),
                               filters: str = Form("", description="JSON 字符串，结构同 Filters"),
                               external_id: str = Form(""), who: str = Depends(client)):
    ext = Path(image.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        raise ApiError(400, "invalid_image", "仅支持 jpg / png / webp")
    try:
        p = SearchCreate(image_base64="-", brief=brief, sources=[s for s in sources.split(",") if s] or None,
                         per_query=per_query, max_duration=max_duration, depth=depth,
                         target_count=target_count, top_per_dim=top_per_dim,
                         min_score=min_score, download=download, download_top=download_top,
                         max_height=max_height, callback_url=callback_url or None,
                         external_id=external_id or None,
                         filters=Filters(**json.loads(filters)) if filters.strip() else Filters())
    except (ValueError, TypeError) as e:
        raise ApiError(422, "invalid_request", str(e)[:500])
    _check_callback(p.callback_url)
    pfs = _sources(p.sources)
    path = _save_upload(await image.read(), ext)
    return _launch(path, p, p.brief, pfs, who, _base(request),
                   p.brief.strip()[:40] or (image.filename or "API 检索"), True, _opts(p))


@router.get("/tasks/{task_id}", response_model=Task, tags=["任务"], summary="查询任务状态")
def get_task(task_id: str, request: Request, who: str = Depends(client)):
    t = tasks.get(task_id)
    if not t or t["meta"].get("client") != who:
        raise ApiError(404, "not_found", "任务不存在")
    return task_out(t, _base(request))


@router.post("/tasks/{task_id}/retry", response_model=TaskCreated, status_code=202, tags=["任务"],
             summary="重试失败的任务（参数与原任务相同，生成新任务）")
def retry_task(task_id: str, request: Request, who: str = Depends(client)):
    t = tasks.get(task_id)
    if not t or t["meta"].get("client") != who:
        raise ApiError(404, "not_found", "任务不存在")
    try:
        tid = tasks.retry(task_id)
    except tasks.RetryError as e:
        raise ApiError(e.status, {400: "not_retryable", 404: "not_found", 410: "image_gone"}.get(e.status, "error"), str(e))
    return {"task_id": tid, "status": "queued", "status_url": _abs(_base(request), f"/api/v1/tasks/{tid}")}


@router.get("/tasks", response_model=list[Task], tags=["任务"], summary="最近的任务（最新在前）")
def list_tasks(request: Request, limit: int = Query(20, ge=1, le=100), who: str = Depends(client)):
    base = _base(request)
    return [task_out(tasks.get(x["id"]), base) for x in tasks.recent(limit, client=who)]


@router.get("/runs/{run_id}", response_model=Run, tags=["结果"], summary="获取检索结果")
def get_run(run_id: str, request: Request, who: str = Depends(client)):
    return run_out(_own_run(run_id, who), _base(request))


class PartialRun(BaseModel):
    complete: bool = Field(description="true 表示已检索完，请改用 GET /runs/{run_id} 取最终结果")
    percent: int
    stage: str
    searched: dict = Field(description="各来源已拿到的原始结果条数")
    run: Run | None = Field(description="中间结果；识别产品完成前为 null。打完分的维度先给出视频，"
                                        "最终结果还会跨维度去重，条目可能略有变化")


@router.get("/runs/{run_id}/partial", response_model=PartialRun, tags=["结果"],
            summary="检索进行中的中间结果（可提前渲染）")
def get_partial(run_id: str, request: Request, who: str = Depends(client)):
    try:
        done = pipeline.load(run_id)
    except (FileNotFoundError, ValueError):
        done = None
    if done:
        if done.get("client") != who:
            raise ApiError(404, "not_found", "检索结果不存在")
        return {"complete": True, "percent": 100, "stage": "完成", "searched": {}, "run": run_out(done, _base(request))}
    r = pipeline.load_partial(run_id)
    if not r or r.get("client") != who:
        raise ApiError(404, "not_found", "检索结果不存在或已结束")
    pg = r.get("progress", {})
    return {"complete": False, "percent": pg.get("pct", 0), "stage": pg.get("stage", ""),
            "searched": r.get("searched", {}), "run": run_out(r, _base(request)) if r.get("analysis") else None}


@router.get("/runs", response_model=list[RunSummary], tags=["结果"], summary="历史检索（最新在前）")
def list_runs(request: Request, limit: int = Query(20, ge=1, le=200), who: str = Depends(client)):
    base, out = _base(request), []
    for f in sorted(config.RUNS_DIR.glob("*/result.json"), reverse=True):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("client") != who:
            continue
        p = r.get("analysis", {}).get("product", {})
        out.append({"run_id": r["run_id"], "created_at": r.get("created", ""),
                    "external_id": r.get("external_id"), "brief": r.get("brief", ""),
                    "query": r.get("query"), "query_type": r.get("query_type"),
                    "product": p.get("name", ""), "category": p.get("category", ""),
                    "kept": sum(len(g["videos"]) for g in r.get("groups", [])),
                    "product_image_url": _file(base, r["run_id"], r.get("image", "product.jpg"))
                    if r.get("image", "product.jpg") else None})
        if len(out) >= limit:
            break
    return out


@router.post("/runs/{run_id}/rerun", response_model=TaskCreated, status_code=202, tags=["检索"],
             summary="用同一张产品图重新检索（可换渠道/简报）")
def rerun(run_id: str, p: RerunCreate, request: Request, who: str = Depends(client)):
    old = _own_run(run_id, who)
    _check_callback(p.callback_url)
    brief = old.get("brief", "") if p.brief is None else p.brief
    text = pipeline.run_query(run_id)
    image = None if text else pipeline.run_image(run_id)
    if p.external_id is None:
        p.external_id = old.get("external_id")
    title = "重新检索：" + (old["analysis"].get("product", {}).get("name") or run_id)[:30]
    f = p.filters.model_dump() if p.filters else old.get("filters")
    o = old.get("options") or {}
    target = o.get("target_count") if p.target_count is None else (p.target_count or None)
    opts = {"filters": f, "depth": p.depth or o.get("depth") or "standard", "target_count": target,
            "min_score": o.get("min_score", 5), "max_duration": o.get("max_duration", 300)}
    return _launch(image, p, brief, _sources(p.sources), who, _base(request), title, False, opts, query=text)


@router.post("/runs/{run_id}/downloads", response_model=TaskCreated, status_code=202, tags=["下载"],
             summary="下载结果中的视频（mp4，H.264/AAC）")
def create_download(run_id: str, p: DownloadCreate, request: Request, who: str = Depends(client)):
    r = _own_run(run_id, who)
    _check_callback(p.callback_url)
    if p.urls:
        vids = pipeline.find_videos(r, p.urls)
    elif p.top:
        vids = sorted((v for g in r["groups"] for v in g["videos"]),
                      key=lambda v: v.get("score", 0), reverse=True)[:p.top]
    else:
        raise ApiError(400, "invalid_request", "urls 与 top 至少传一个")
    if not vids:
        raise ApiError(400, "invalid_request", "没有匹配到结果中的视频")
    base = _base(request)
    tid = _download_job({"run_id": run_id, "urls": [v["url"] for v in vids], "max_height": p.max_height,
                         "who": who, "external_id": r.get("external_id"), "callback_url": p.callback_url,
                         "base": base})
    return {"task_id": tid, "status": "queued", "status_url": _abs(base, f"/api/v1/tasks/{tid}")}


class LibraryVideo(BaseModel):
    platform: str
    url: str
    title: str
    channel: str | None
    duration: int | None
    views: int | None
    thumbnail: str | None
    score: float
    is_ad: bool | None
    video_type: str | None
    video_type_name: str | None
    reference: str | None
    embed_url: str | None
    dimension: str = Field(description="当初被检索到时所属的参考维度名称，如“同品类”“竞品”")
    brand: str | None = Field(description="当初检索的品牌")
    category: str | None = Field(description="当初检索的品类")
    found_at: str = Field(description="首次被检索到的时间")


class LibraryResult(BaseModel):
    total: int
    items: list[LibraryVideo]


@router.get("/library", response_model=LibraryResult, tags=["结果"],
            summary="在已有视频库里检索（历次检索找到的全部参考视频，立即返回，不产生新检索）")
def library(q: str = Query(min_length=1, max_length=100, description="关键词，如品牌名或产品名；空格分隔多个词时须全部命中"),
            min_score: float = Query(5, ge=0, le=10), limit: int = Query(60, ge=1, le=200),
            types: str = Query("", description="视频类型 key，逗号分隔，见 GET /video-types")):
    """匹配标题、参考点、频道，以及当初检索的品牌 / 产品 / 品类。同一视频只返回一条（分数最高的）。
    视频库是各调用方共享的公开视频索引；不返回别的调用方的检索 ID 和简报。"""
    r = stats.search_videos(q=q, min_score=min_score, dedupe=True, sort="score", limit=limit, vtype=types)
    return {"total": r["total"], "items": [{
        **{k: v.get(k) for k in ("platform", "url", "title", "channel", "duration", "views", "thumbnail",
                                  "is_ad", "reference", "brand", "category")},
        "score": v.get("score") or 0, "video_type": v.get("vtype"), "video_type_name": v.get("vtype_name"),
        "embed_url": player.embed_url(v["url"]), "dimension": v.get("dim_name") or "", "found_at": v.get("created") or "",
    } for v in r["items"]]}


@router.get("/play", response_model=PlayInfo, tags=["播放"], summary="解析视频的站内播放方式")
def play(request: Request, url: str = Query(description="视频页面地址")):
    try:
        security.check_public_url(url)
    except security.UnsafeURL as e:
        raise ApiError(400, "invalid_url", str(e))
    r = player.resolve(url)
    if r["kind"] == "video" and r["src"].startswith("/runs/"):
        r["src"] = _abs(_base(request), security.signed_path(unquote(r["src"].removeprefix("/runs/"))))
    return r


@router.post("/relay", response_model=TaskCreated, status_code=202, tags=["播放"],
             summary="服务器中转：下载 480p 缓存后给出可播放地址（任务完成后看 play_url）")
def relay(p: RelayCreate, request: Request, who: str = Depends(client)):
    try:
        security.check_public_url(p.url)
    except security.UnsafeURL as e:
        raise ApiError(400, "invalid_url", str(e))
    tid = tasks.submit("relay", "中转播放", lambda progress: player.relay(p.url, progress), {"client": who})
    return {"task_id": tid, "status": "queued", "status_url": _abs(_base(request), f"/api/v1/tasks/{tid}")}


@router.get("/reference-platforms/items", response_model=list[PlatformItem], tags=["参考平台"],
            summary="直接抓取某个参考平台的作品（direct=true 的平台）")
def platform_items(name: str = Query(description="reference_platforms[].name"),
                   q: str = Query(description="关键词，一般用 reference_platforms[].query")):
    try:
        items = platforms.fetch_items(name, q)
    except StopIteration:
        raise ApiError(404, "not_found", "该平台不支持直接抓取，请使用 search_url")
    except Exception as e:
        raise ApiError(502, "upstream_error", f"抓取失败：{e}"[:300])
    return [{**{k: it.get(k) for k in ("platform", "url", "title", "thumbnail", "duration")},
             "media": it.get("media", "video")} for it in items]


# ---------------- 子应用：独立的 OpenAPI 文档 ----------------
api = FastAPI(
    title="参考视频检索 API", version="1.0.0",
    description=("上传产品图 → AI 拆解关键词 → 16 个渠道检索参考广告片 → AI 打分并写出参考点 → 可选下载。\n\n"
                 "鉴权：请求头 `X-API-Key: <key>`。所有耗时操作都是异步任务，返回 `task_id` 后轮询 "
                 "`GET /tasks/{task_id}` 或通过 `callback_url` 接收回调。完整接入说明见 [接入文档](/developer)。"),
    docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json")
api.include_router(router)


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@api.exception_handler(ApiError)
async def _api_error(request, e: ApiError):
    return _err(e.status, e.code, e.message)


@api.exception_handler(RequestValidationError)
async def _validation_error(request, e: RequestValidationError):
    msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'][1:])}: {err['msg']}" for err in e.errors())
    return _err(422, "invalid_request", msg[:500])


@api.exception_handler(StarletteHTTPException)
async def _http_error(request, e: StarletteHTTPException):
    return _err(e.status_code, {404: "not_found", 405: "method_not_allowed"}.get(e.status_code, "error"),
                str(e.detail))


@api.exception_handler(Exception)
async def _internal_error(request, e: Exception):
    return _err(500, "internal_error", "服务器内部错误")
