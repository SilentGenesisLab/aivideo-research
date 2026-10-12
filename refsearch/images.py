"""图片缩略图缓存：产品图、外站视频封面统一由服务器缩小后缓存，页面只从本站加载。

- 解决：历史列表直接加载原图（单张可达 7MB）、外站封面（如 YouTube 在国内打不开）拖慢页面。
- 缓存在 runs/_thumbs，按地址+宽度命名；外站加载失败的地址 1 小时内不再重试。
"""
import hashlib
import io
import threading
import time
from pathlib import Path

import requests
from PIL import Image, ImageOps

from . import config, security
from .searchers import UA

THUMB_DIR = config.RUNS_DIR / "_thumbs"
WIDTHS = (96, 160, 320, 480, 960)
MAX_BYTES = 8 * 1024 * 1024
_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()
_failed: dict[str, float] = {}


def _lock(key: str) -> threading.Lock:
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _width(w: int) -> int:
    return min(WIDTHS, key=lambda x: abs(x - w))


def _encode(data: bytes, width: int) -> bytes:
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im)
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA" if "transparency" in im.info else "RGB")
    if im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "WEBP", quality=78, method=4)
    return out.getvalue()


def _cached(key: str, width: int, produce) -> Path | None:
    width = _width(width)
    path = THUMB_DIR / f"{hashlib.sha1(key.encode()).hexdigest()[:20]}_{width}.webp"
    if path.exists():
        return path
    if _failed.get(key, 0) > time.time():
        return None
    with _lock(key):
        if path.exists():
            return path
        try:
            data = produce()
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(_encode(data, width))
            tmp.replace(path)
            return path
        except Exception:
            _failed[key] = time.time() + 3600
            return None


def local_thumb(rel: str, width: int) -> Path | None:
    """runs 目录下的图片（如产品图）。"""
    src = (config.RUNS_DIR / rel).resolve()
    if not src.is_relative_to(config.RUNS_DIR.resolve()) or not src.is_file():
        return None
    key = f"local:{rel}:{src.stat().st_mtime_ns}"
    return _cached(key, width, src.read_bytes)


def remote_thumb(url: str, width: int) -> Path | None:
    """外站封面图：服务器代为下载，缩小后缓存。"""
    def fetch():
        security.check_public_url(url)
        r = requests.get(url, headers={"User-Agent": UA}, timeout=8, stream=True)
        r.raise_for_status()
        if not r.headers.get("content-type", "image/").startswith("image/"):
            raise ValueError("not image")
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError("too large")
        return data

    return _cached(f"url:{url}", width, fetch)
