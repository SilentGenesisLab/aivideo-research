"""对外接口用到的安全工具：API Key、文件签名链接、外部地址校验（防 SSRF）、Webhook 签名。"""
import hashlib
import hmac
import ipaddress
import os
import secrets
import socket
import time
from urllib.parse import quote, urljoin, urlparse

import requests

from . import config


# ---------------- API Key ----------------
def api_keys() -> dict[str, str]:
    """API_KEYS=站点A:key1,站点B:key2 → {key: 站点名}。只写 key 时站点名为 default。"""
    out = {}
    for item in os.environ.get("API_KEYS", "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, key = item.rpartition(":")
        out[key.strip()] = name.strip() or "default"
    return out


def client_of(key: str) -> str | None:
    for k, name in api_keys().items():
        if secrets.compare_digest(k, key):
            return name
    return None


# ---------------- 文件签名链接 ----------------
_SECRET_FILE = config.RUNS_DIR / ".signing_secret"


def _secret() -> bytes:
    if s := os.environ.get("SIGNING_SECRET"):
        return s.encode()
    if not _SECRET_FILE.exists():  # 自动生成并保存在数据目录，重启后签名链接仍有效
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SECRET_FILE.write_text(secrets.token_hex(32))
        _SECRET_FILE.chmod(0o600)
    return _SECRET_FILE.read_text().strip().encode()


def _sig(path: str, exp: int) -> str:
    return hmac.new(_secret(), f"{path}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


FILE_TTL = int(os.environ.get("FILE_URL_TTL", 7 * 86400))


def signed_path(path: str, ttl: int = FILE_TTL) -> str:
    """runs 目录下的相对路径 → /files/... 免登录限时链接（默认 7 天）。"""
    exp = int(time.time()) + ttl
    return f"/files/{quote(path)}?exp={exp}&sig={_sig(path, exp)}"


def verify_file(path: str, exp: int, sig: str) -> bool:
    return exp >= time.time() and hmac.compare_digest(_sig(path, exp), sig)


def absolute(base_url: str, path: str) -> str:
    base = os.environ.get("PUBLIC_BASE_URL") or base_url
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


# ---------------- 外部地址校验 ----------------
class UnsafeURL(ValueError):
    pass


def check_public_url(url: str) -> None:
    """只允许 http(s) 且解析到公网 IP，防止借服务器访问内网（SSRF）。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL("只支持 http/https 地址")
    if os.environ.get("ALLOW_PRIVATE_URLS") == "1":
        return
    try:
        infos = socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))
    except socket.gaierror:
        raise UnsafeURL(f"无法解析域名：{u.hostname}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise UnsafeURL("不允许访问内网或保留地址")


def fetch_image(url: str, max_bytes: int = 15 * 1024 * 1024) -> tuple[bytes, str]:
    """下载图片（校验每一跳重定向），返回 (内容, 扩展名)。"""
    for _ in range(4):
        check_public_url(url)
        r = requests.get(url, timeout=20, stream=True, allow_redirects=False,
                         headers={"User-Agent": "aivideo-research/1.0"})
        if r.is_redirect:
            url = urljoin(url, r.headers["location"])
            continue
        break
    else:
        raise UnsafeURL("重定向次数过多")
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(ctype)
    if not ext:
        raise UnsafeURL(f"图片格式不支持：{ctype or '未知'}（仅 jpg/png/webp）")
    data = b""
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) > max_bytes:
            raise UnsafeURL("图片不能超过 15MB")
    return data, ext


# ---------------- Webhook ----------------
def webhook_secret() -> str:
    return os.environ.get("WEBHOOK_SECRET", "")


def sign_webhook(body: bytes, ts: str) -> str:
    """签名 = HMAC-SHA256(WEBHOOK_SECRET, "{时间戳}.{原始请求体}")。"""
    return "sha256=" + hmac.new(webhook_secret().encode(), ts.encode() + b"." + body,
                                hashlib.sha256).hexdigest()
