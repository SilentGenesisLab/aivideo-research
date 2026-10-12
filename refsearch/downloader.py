"""可选下载模块：yt-dlp 下载并合并为 mp4（默认最高 720p，够拉片用且省空间）。

每条视频在独立子进程里下载并设硬超时：有些站点（如 iSpot.tv）会长时间“连着但不给数据”，
yt-dlp 自身的 socket 超时管不住，只能整体超时后结束进程。"""
import re
import subprocess
import sys
from pathlib import Path

from . import config

TIMEOUT = 180  # 单条视频最长下载时间（秒）


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
        except subprocess.TimeoutExpired:
            for part in out_dir.glob(f"{stem}.*"):  # 清理半成品
                part.unlink(missing_ok=True)
            results.append(_fail(v, f"超过 {timeout} 秒未完成，已放弃"))
            continue
        lines = [l for l in p.stdout.splitlines() if l.strip()]
        path = Path(lines[-1]) if lines else None
        if p.returncode == 0 and path and path.exists():
            results.append({"url": v["url"], "title": v.get("title", ""), "ok": True,
                            "file": str(path), "size_mb": round(path.stat().st_size / 1e6, 2),
                            "error": None})
        else:
            err = (p.stderr.strip().splitlines() or ["未知错误"])[-1]
            if "Unsupported URL" in err:
                err = "该网站暂不支持自动下载，请打开原链接观看"
            results.append(_fail(v, err[:300]))
    return results
