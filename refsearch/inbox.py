"""收件箱服务（4090）客户端：微信视频号下载 / 检索，以及给 4090 传文件。

视频号没有公开网页，也没有 yt-dlp 提取器，只能由 4090 上的下载器去抓；一条约 3–4 分钟，
所以对外必须走异步任务。协议与本地客户端 inbox_client.py 一致：

  1. GET {SITE}/index.html，取页面里的加密凭证块，用 INBOX_KEY 派生密钥 AESGCM 解密；
  2. 请求体写成 JSON，作为对象直传到 OSS 的 _inbox/_req-<rid>.json（用凭证里的 policy/signature）；
  3. 轮询 {SITE}/out/<rid>/result.json，status 为 done 后按 files 里的地址取回文件。

密钥只从环境变量 INBOX_KEY 读，不写盘、不进仓库。
"""
import base64
import datetime
import hashlib
import json
import re
import secrets
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

from . import config

UA = "videodata-inbox/1"
_cred = None


class InboxError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def available() -> bool:
    return bool(config.INBOX_KEY)


def _http_get(url: str, timeout: int = 30) -> bytes:
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(url + sep + "t=" + str(int(time.time())),
                                 headers={"Cache-Control": "no-cache", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"[\s​-‏⁠﻿]", "", s)
    return re.sub(r"[‐-―−－]", "-", s).lower()


def _get_cred():
    """解密 index.html 里的凭证块。缓存到进程内存，不落盘。"""
    global _cred
    if not config.INBOX_KEY:
        raise InboxError("inbox_unconfigured", "服务端未配置 INBOX_KEY，视频号功能不可用")
    if _cred and _cred.get("exp", "") > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + 3600)):
        return _cred, _cred["h"]
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError:
        raise InboxError("inbox_unavailable", "服务端缺少 cryptography，视频号功能不可用")
    try:
        html = _http_get(config.INBOX_SITE + "/index.html").decode("utf-8")
    except Exception as e:  # noqa: BLE001
        raise InboxError("inbox_unreachable", f"收件箱服务不可达：{type(e).__name__}: {str(e)[:120]}")
    m = re.search(r"const BLOB=(\{.*?\});", html)
    if not m:
        raise InboxError("inbox_protocol_changed", "收件箱页面格式变了，没找到凭证块")
    blob = json.loads(m.group(1))
    d = base64.b64decode
    k = PBKDF2HMAC(hashes.SHA256(), 32, d(blob["s"]), blob["n"]).derive(_norm(config.INBOX_KEY).encode())
    try:
        cred = json.loads(AESGCM(k).decrypt(d(blob["i"]), d(blob["c"]), None))
    except Exception:  # noqa: BLE001
        raise InboxError("inbox_key_invalid", "INBOX_KEY 不正确，解不开收件箱凭证")
    cred["h"] = blob["h"]
    _cred = cred
    return cred, blob["h"]


def _post_object(cred: dict, host: str, objkey: str, data: bytes) -> None:
    """OSS POST 表单直传。这里只传几 KB 的 JSON 请求，不做流式。"""
    bd = "----inbox" + secrets.token_hex(8)

    def field(n: str, v: str) -> bytes:
        return f'--{bd}\r\nContent-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'.encode()

    head = b"".join([field("key", objkey), field("OSSAccessKeyId", cred["id"]), field("policy", cred["policy"]),
                     field("Signature", cred["sig"]), field("success_action_status", "200"),
                     f'--{bd}\r\nContent-Disposition: form-data; name="file"; filename="f"\r\n'
                     f'Content-Type: application/octet-stream\r\n\r\n'.encode()])
    body = head + data + f"\r\n--{bd}--\r\n".encode()
    req = urllib.request.Request(host, data=body, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=" + bd,
                                          "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            if r.status != 200:
                raise InboxError("inbox_upload_failed", f"OSS 上传返回 {r.status}")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        code = (re.search(r"<Code>(.*?)</Code>", txt) or [None, str(e.code)])[1]
        hint = "（凭证可能过期，联系管理员）" if code in ("AccessDenied", "ExpiredToken") else ""
        raise InboxError("inbox_upload_failed", f"上传失败：{code}{hint}")
    except InboxError:
        raise
    except Exception as e:  # noqa: BLE001
        raise InboxError("inbox_upload_failed", f"上传失败：{type(e).__name__}: {str(e)[:120]}")


def send_req(body: dict, timeout: int | None = None, progress=None, wait_msg: str = "") -> dict:
    """投递一条请求并轮询结果。失败抛 InboxError（错误原文放在 message 里）。"""
    cred, host = _get_cred()
    rid = secrets.token_hex(16)
    data = json.dumps(body, ensure_ascii=False).encode()
    _post_object(cred, host, f"_inbox/_req-{rid}.json", data)
    url = f"{config.INBOX_SITE}/out/{rid}/result.json"
    if progress:
        progress(wait_msg or "已送达 4090，等待处理")
    t0, last = time.time(), ""
    limit = timeout or config.INBOX_TIMEOUT
    while time.time() - t0 < limit:
        time.sleep(5)
        try:
            r = json.loads(_http_get(url))
        except Exception:  # noqa: BLE001  404 未生成、网络抖动都接着等
            continue
        stage = (r.get("stage") or "").strip()
        if stage and stage != last:
            last = stage
            if progress:
                progress(stage)
        if r.get("status") in ("done", "failed"):
            if r["status"] == "failed":
                raise InboxError("wxv_failed", str(r.get("error") or "4090 返回 failed，但没给错误原文")[:300])
            return r
    raise InboxError("inbox_timeout", f"等了 {limit} 秒还没结果（请求号 {rid}），服务可能离线")


def wxv_add(text: str, note: str = "", timeout: int | None = None, progress=None) -> dict:
    return send_req({"op": "wxv_add", "text": text, "note": note}, timeout, progress,
                    "已送达 4090，正在下载（一条约 3–4 分钟）")


def wxv_find(q: str, n: int = 20, timeout: int = 180) -> dict:
    return send_req({"op": "wxv_find", "q": q, "n": n}, timeout, wait_msg="正在检索 4090 已入库的视频号")


def wxv_fetch(item_id: str, timeout: int = 300, progress=None) -> dict:
    return send_req({"op": "wxv_fetch", "id": item_id}, timeout, progress, f"正在取回 {item_id}")


def save_files(r: dict, out_dir: Path) -> list[dict]:
    """把结果里的 mp4 / 封面 / 拼图取到 out_dir/<item_id>/，local 里放相对路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    items = []
    for it in r.get("items", []):
        local = {}
        for kind, u in (it.get("files") or {}).items():
            name = u.rsplit("/", 1)[-1].split("-", 1)[-1] or f"{kind}.bin"
            dst = out_dir / it["id"] / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                with urllib.request.urlopen(u, timeout=300) as resp, open(dst, "wb") as f:
                    while True:
                        c = resp.read(1 << 20)
                        if not c:
                            break
                        f.write(c)
            except Exception as e:  # noqa: BLE001
                raise InboxError("file_fetch_failed", f"取回 {kind} 失败：{type(e).__name__}: {str(e)[:120]}")
            local[kind] = dst.relative_to(out_dir).as_posix()
        items.append({**{k: it.get(k) for k in ("id", "title", "author", "desc", "note", "url", "duration")},
                      "local": local})
    return items


def status() -> dict:
    try:
        s = json.loads(_http_get(config.INBOX_SITE + "/status.json"))
    except Exception as e:  # noqa: BLE001
        raise InboxError("inbox_unreachable", f"读不到心跳：{type(e).__name__}: {str(e)[:120]}")
    try:
        age = time.time() - datetime.datetime.fromisoformat(s["ts"]).timestamp()
        s["online"] = abs(age) < 300
    except Exception:  # noqa: BLE001
        s["online"] = None
    return s
