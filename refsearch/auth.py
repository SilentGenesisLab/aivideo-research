"""网页登录：登录页 + 签名 Cookie 会话（保留 HTTP Basic Auth 供脚本/curl 使用）。

- 会话 Cookie 内容为 用户名|过期时间|签名，签名密钥含当前密码：改密码后所有旧会话自动失效。
- 同一 IP 10 分钟内登录失败 10 次，暂停该 IP 登录 10 分钟。
"""
import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

from . import security

COOKIE = "vd_session"
SESSION_TTL = 12 * 3600            # 不勾选“记住我”：12 小时
REMEMBER_TTL = 30 * 86400          # 勾选“记住我”：30 天
_fails: dict[str, list[float]] = {}
_lock = threading.Lock()


def user() -> str:
    return os.environ.get("APP_USER", "admin")


def password() -> str:
    return os.environ.get("APP_PASSWORD", "")


def enabled() -> bool:
    return bool(password())


def check(u: str, p: str) -> bool:
    return secrets.compare_digest(u.encode(), user().encode()) & secrets.compare_digest(p.encode(), password().encode())


def _key() -> bytes:
    return hashlib.sha256(security._secret() + b"|" + password().encode()).digest()


def make_session(remember: bool) -> tuple[str, int]:
    ttl = REMEMBER_TTL if remember else SESSION_TTL
    exp = int(time.time()) + ttl
    body = f"{user()}|{exp}"
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:40]
    return base64.urlsafe_b64encode(f"{body}|{sig}".encode()).decode(), ttl


def read_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        u, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 2)
    except Exception:
        return None
    want = hmac.new(_key(), f"{u}|{exp}".encode(), hashlib.sha256).hexdigest()[:40]
    if not hmac.compare_digest(want, sig) or int(exp) < time.time() or u != user():
        return None
    return u


def check_basic(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        u, _, p = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    return check(u, p)


def blocked(ip: str) -> int:
    """返回还需等待的秒数，0 表示可以尝试。"""
    now = time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if t > now - 600]
        _fails[ip] = recent
        return int(recent[0] + 600 - now) if len(recent) >= 10 else 0


def record_fail(ip: str):
    with _lock:
        _fails.setdefault(ip, []).append(time.time())


def clear_fails(ip: str):
    with _lock:
        _fails.pop(ip, None)
