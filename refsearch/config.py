"""配置：从 .env / 环境变量读取，不在代码里写死任何密钥。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-flash 支持图片输入；deepseek-v4-pro 只支持文本
VISION_MODEL = os.environ.get("VISION_MODEL", "deepseek-flash")
TEXT_MODEL = os.environ.get("TEXT_MODEL", "deepseek-flash")

# 可选：已登录 B 站的 cookie 字符串（至少含 SESSDATA），可显著降低搜索风控
BILIBILI_COOKIE = os.environ.get("BILIBILI_COOKIE", "")

# 可选：抖音下载所需的 cookies.txt（Netscape 格式，用浏览器插件 "Get cookies.txt" 导出）
DOUYIN_COOKIES_FILE = os.environ.get("DOUYIN_COOKIES_FILE", "")

RUNS_DIR = Path(os.environ.get("RUNS_DIR", ROOT / "runs"))
RUNS_DIR.mkdir(parents=True, exist_ok=True)


def ffmpeg_path() -> str | None:
    """优先用系统 ffmpeg，否则用 imageio-ffmpeg 自带的静态二进制。"""
    from shutil import which

    if which("ffmpeg"):
        return which("ffmpeg")
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
