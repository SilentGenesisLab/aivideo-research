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

# 时区：所有显示和记录的时间统一用东八区，不随服务器/容器时区变化。
# 默认用 POSIX 写法 CST-8（不依赖系统装 tzdata，精简镜像里也生效）；可用 APP_TZ 改成别的，如 Asia/Tokyo
import time as _time  # noqa: E402

os.environ["TZ"] = os.environ.get("APP_TZ") or "CST-8"
_time.tzset()

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
# deepseek-flash 支持图片输入；deepseek-v4-pro 只支持文本
VISION_MODEL = os.environ.get("VISION_MODEL", "deepseek-flash")
TEXT_MODEL = os.environ.get("TEXT_MODEL", "deepseek-flash")

# 可选：已登录 B 站的 cookie 字符串（至少含 SESSDATA），可显著降低搜索风控
BILIBILI_COOKIE = os.environ.get("BILIBILI_COOKIE", "")

# 可选：抖音 / 小红书解析所需的登录态 cookies（Netscape 格式，浏览器插件 "Get cookies.txt" 导出）
SOCIAL_COOKIES_DIR = Path(os.environ.get("SOCIAL_COOKIES_DIR", ROOT / "data" / "cookies"))
DOUYIN_COOKIES_FILE = os.environ.get("DOUYIN_COOKIES_FILE") or str(SOCIAL_COOKIES_DIR / "douyin_cookies.txt")
XHS_COOKIES_FILE = os.environ.get("XHS_COOKIES_FILE") or str(SOCIAL_COOKIES_DIR / "xhs_cookies.txt")

# 出口代理（大陆机器连不上 YouTube / TikTok / Google / Facebook；机房 IP 访问 B 站网页会被 412 风控）。
# 留空表示直连；用 socks5h:// 让 DNS 也在代理侧解析
PROXY_URL = os.environ.get("PROXY_URL", "")
PROXY_PLATFORMS = ("youtube", "tiktok", "bilibili", "gads", "meta")

# 下载可以走单独出口：搜索要的是「IP 干净」，下载要的是「带宽稳定」，两者不一定是同一台机器
DOWNLOAD_PROXY_URL = os.environ.get("DOWNLOAD_PROXY_URL") or PROXY_URL

# 出口可以配多个（逗号分隔），按顺序探测，坏了自动换下一个
PROXY_URLS = [u.strip() for u in
              (os.environ.get("PROXY_URLS") or PROXY_URL).split(",") if u.strip()]
DOWNLOAD_PROXY_URLS = [u.strip() for u in
                       (os.environ.get("DOWNLOAD_PROXY_URLS") or DOWNLOAD_PROXY_URL).split(",")
                       if u.strip()]
_PROXY_CACHE = {"t": 0.0, "url": ""}


def _probe(proxy: str, timeout: float = 3.0) -> bool:
    import requests

    try:
        r = requests.get("https://www.youtube.com/generate_204", timeout=timeout,
                         proxies={"http": proxy, "https": proxy})
        return r.status_code < 400
    except Exception:
        return False


def active_proxy(download: bool = False) -> str:
    """挑第一个可用出口，60 秒内复用结果；全挂时返回第一个，让调用方拿到真实报错。"""
    urls = DOWNLOAD_PROXY_URLS if download else PROXY_URLS
    if len(urls) <= 1:
        return urls[0] if urls else ""
    now = _time.time()
    if now - _PROXY_CACHE["t"] < 60 and _PROXY_CACHE["url"] in urls:
        return _PROXY_CACHE["url"]
    for u in urls:
        if _probe(u):
            _PROXY_CACHE.update(t=now, url=u)
            return u
    return urls[0]


# 按平台指定出口：Google/Meta 建议用另一台 IP，避免和 YouTube 抢同一个出口的限流额度
# 格式：PLATFORM_PROXY=gads=socks5h://127.0.0.1:1083,meta=socks5h://127.0.0.1:1083
PLATFORM_PROXY = dict(
    kv.split("=", 1) for kv in os.environ.get("PLATFORM_PROXY", "").split(",") if "=" in kv
)


def proxy_for(platform: str, download: bool = False) -> str:
    return PLATFORM_PROXY.get(platform) or active_proxy(download)


# Google 广告透明度中心限流很紧，可以单独配几个出口轮换（逗号分隔）
GADS_PROXY_URLS = [u.strip() for u in os.environ.get("GADS_PROXY_URLS", "").split(",") if u.strip()]

# YouTube Data API：配了 key 就优先走官方接口（没有风控、字段全），失败或配额用完回退 yt-dlp
YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_DAILY_QUOTA = int(os.environ.get("YOUTUBE_DAILY_QUOTA") or 10000)
YOUTUBE_CACHE_TTL = int(os.environ.get("YOUTUBE_CACHE_TTL") or 21600)  # 搜索结果缓存 6 小时

# Meta 广告资料库 Graph API：需要完成身份验证的 access token；不配时该来源只报错不检索
META_AD_LIBRARY_TOKEN = os.environ.get("META_AD_LIBRARY_TOKEN", "")
META_AD_LIBRARY_VERSION = os.environ.get("META_AD_LIBRARY_VERSION", "v23.0")
META_AD_LIBRARY_COUNTRIES = os.environ.get(
    "META_AD_LIBRARY_COUNTRIES", "US,GB,DE,FR,JP").split(",")

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
