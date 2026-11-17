"""Analyze video files already downloaded into a Video Data run."""
import os
from hashlib import file_digest
from dataclasses import dataclass, field
from pathlib import Path
import re
from shutil import copyfile

from . import config
from .video_motion.adaptive import inspect
from .video_motion.media import probe
from .video_motion.provider import Provider
from .video_motion.store import write_json


@dataclass
class MotionSettings:
    key: str = field(repr=False)
    api_base: str = field(default_factory=lambda: os.getenv(
        "VIDEODATA_ARK_BASE", "https://ark.cn-beijing.volces.com/api/v3"))
    model: str = field(default_factory=lambda: os.getenv(
        "VIDEODATA_ARK_MODEL", "doubao-seed-2-1-pro-260915"))
    adaptive_parallelism: int = 2
    adaptive_max_windows: int = 12
    adaptive_max_seconds: float = 6
    max_request_bytes: int = 63_000_000
    request_timeout: int = 300

    def api_key(self):
        return self.key


def analysis_dir(run_id: str, analysis_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", analysis_id):
        raise ValueError("分析记录不存在")
    run = (config.RUNS_DIR / run_id).resolve()
    if not run.is_relative_to(config.RUNS_DIR.resolve()):
        raise ValueError("分析记录不存在")
    return run / "video-analyses" / analysis_id


def downloaded_video(run: dict, url: str) -> Path:
    run_dir = (config.RUNS_DIR / run["run_id"]).resolve()
    for item in run.get("downloads", []):
        if item.get("url") == url and item.get("ok") and item.get("file"):
            target = (run_dir / item["file"]).resolve()
            if target.is_relative_to(run_dir / "videos") and target.is_file():
                return target
    raise FileNotFoundError("请先在本次检索中下载该视频")


def analyze_video(source, destination, key, max_windows, progress):
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = destination / ".source-video"
    copyfile(source, snapshot)
    info = probe(snapshot)
    settings = MotionSettings(key, adaptive_max_windows=max_windows)
    with snapshot.open("rb") as stream:
        source_hash = file_digest(stream, "sha256").hexdigest()
    write_json(destination / "source.json", {"sha256": source_hash, "media": info, "model": settings.model})
    progress("扫描原生视频帧")

    def update(event):
        progress(f"局部分析 {event['completed']}/{event['total']}，窗口 {event['window']['id']}")

    return inspect(snapshot, [], info["duration_seconds"], destination, settings, Provider(settings), update)
