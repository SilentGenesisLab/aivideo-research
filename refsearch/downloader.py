"""可选下载模块：yt-dlp 下载并合并为 mp4（默认最高 720p，够拉片用且省空间）。

每条视频在独立子进程里下载并设硬超时：有些站点（如 iSpot.tv）会长时间“连着但不给数据”，
yt-dlp 自身的 socket 超时管不住，只能整体超时后结束进程。"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config
from .searchers import canonical

TIMEOUT = 180  # 单条视频最长下载时间（秒）
_TRANSIENT = ("No video formats found", "KeyError", "HTTP Error 412", "HTTP Error 429")


# ---------------- 下载缓存：同一视频同一清晰度只下载一次 ----------------
_IDX = config.RUNS_DIR / "_downloads.json"
_idx_lock = threading.Lock()


def _load_idx() -> dict:
    try:
        return json.loads(_IDX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def cached_path(url: str, max_height: int | None = None) -> Path | None:
    """已下载过的文件；不指定清晰度时返回清晰度最高的一份。"""
    c = canonical(url)
    idx = _load_idx()
    keys = [f"{c}|{max_height}"] if max_height else sorted(
        (k for k in idx if k.rsplit("|", 1)[0] == c), key=lambda k: int(k.rsplit("|", 1)[1]), reverse=True)
    for k in keys:
        if k in idx and (f := config.RUNS_DIR / idx[k]).is_file():
            return f
    return None


def _remember(url: str, max_height: int, path: Path) -> None:
    with _idx_lock:
        idx = _load_idx()
        idx[f"{canonical(url)}|{max_height}"] = str(path.resolve().relative_to(config.RUNS_DIR.resolve()))
        tmp = _IDX.with_suffix(".tmp")
        tmp.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_IDX)


def backfill() -> int:
    """把加入缓存功能之前已下载的视频登记进索引（按默认 720p）。"""
    idx, n = _load_idx(), 0
    for f in config.RUNS_DIR.glob("*/result.json"):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for d in r.get("downloads", []):
            p = f.parent / (d.get("file") or "")
            k = f"{canonical(d['url'])}|720"
            if d.get("ok") and p.is_file() and k not in idx:
                idx[k] = str(p.resolve().relative_to(config.RUNS_DIR.resolve()))
                n += 1
    if n:
        with _idx_lock:
            _IDX.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    return n


def _reuse(src: Path, out_dir: Path) -> Path:
    dest = out_dir / src.name
    if dest.resolve() != src.resolve() and not dest.exists():
        try:
            os.link(src, dest)  # 硬链接：不占额外空间
        except OSError:
            shutil.copy2(src, dest)
    return dest


def _safe(name: str, n: int = 60) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_")[:n] or "video"


def _fail(v: dict, error: str) -> dict:
    return {"url": v["url"], "title": v.get("title", ""), "ok": False, "file": None,
            "size_mb": 0, "error": error}


def download(videos: list[dict], out_dir: Path, max_height: int = 720,
             on_progress=None, timeout: int = TIMEOUT) -> list[dict]:
    """逐个下载，单条失败或超时不影响其他。返回每条的 {url, ok, file, error}。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for i, v in enumerate(videos, 1):
        if on_progress:
            on_progress(f"下载 {i}/{len(videos)}：{v.get('title','')[:40]}")
        hit = cached_path(v["url"], max_height)
        if hit:
            dest = _reuse(hit, out_dir)
            results.append({"url": v["url"], "title": v.get("title", ""), "ok": True, "file": str(dest),
                            "size_mb": round(dest.stat().st_size / 1e6, 2), "error": None, "cached": True})
            if on_progress:
                on_progress(f"命中下载缓存，直接复用：{v.get('title','')[:40]}")
            continue
        stem = f"{v.get('platform','')}_{v.get('id','')}_{_safe(v.get('title',''))}"
        cmd = [
            sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-playlist",
            "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
            "--merge-output-format", "mp4",
            # 优先 H.264 + AAC：AV1/Opus 在 QuickTime、Premiere、剪映里经常打不开
            "-S", "vcodec:h264,acodec:aac",
            "-o", str(out_dir / f"{stem}.%(ext)s"),
            "--retries", "3", "--socket-timeout", "30",
            "--print", "after_move:filepath",
        ]
        if config.ffmpeg_path():
            cmd += ["--ffmpeg-location", config.ffmpeg_path()]
        if v.get("platform") == "Bilibili":
            cmd += ["--add-header", "Referer:https://www.bilibili.com/"]
        if v.get("platform") == "抖音":
            if not config.DOUYIN_COOKIES_FILE:
                results.append(_fail(v, "抖音下载需要 cookies：在 .env 配置 DOUYIN_COOKIES_FILE"))
                continue
            cmd += ["--cookies", config.DOUYIN_COOKIES_FILE]
        cmd.append(v["url"])
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            # B 站短时间请求多了会临时不给播放地址（No video formats / KeyError），稍等重试一次
            if p.returncode != 0 and any(x in p.stderr for x in _TRANSIENT):
                if on_progress:
                    on_progress("平台临时限流，15 秒后重试…")
                time.sleep(15)
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            for part in out_dir.glob(f"{stem}.*"):  # 清理半成品
                part.unlink(missing_ok=True)
            results.append(_fail(v, f"超过 {timeout} 秒未完成，已放弃"))
            continue
        lines = [l for l in p.stdout.splitlines() if l.strip()]
        path = Path(lines[-1]) if lines else None
        if p.returncode == 0 and path and path.exists():
            try:
                _remember(v["url"], max_height, path)
            except (OSError, ValueError):
                pass
            results.append({"url": v["url"], "title": v.get("title", ""), "ok": True,
                            "file": str(path), "size_mb": round(path.stat().st_size / 1e6, 2),
                            "error": None})
        else:
            err = (p.stderr.strip().splitlines() or ["未知错误"])[-1]
            if "Unsupported URL" in err:
                err = "该网站暂不支持自动下载，请打开原链接观看"
            results.append(_fail(v, err[:300]))
    return results
