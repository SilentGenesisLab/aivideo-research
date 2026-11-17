#!/usr/bin/env python3
"""边缘端取片：在 4090 这类“有算力有大盘”的机器上跑，视频本体不经过 videodata 服务器。

分工（服务器 VM 只有 1 核 1G 19G 盘，且和 4090 之间的跨境流量很贵）：
  服务器  = 搜索、打分、记结论（几 KB 的 JSON 往返）
  4090    = 下载视频本体、ffprobe / 镜头切点 / 关键帧拼图，结论回传服务器

用法：
  edge_fetch.py run <run_id> [--top 5] [--urls U1 U2 ...] [--max-height 720]
  edge_fetch.py url <视频链接> [--run <run_id>]      # 单条；不带 --run 只存本地不回传

配置（环境变量，或 ~/.config/videodata/ 下的文件）：
  VIDEODATA_BASE   默认 https://videodata.sligenai.cn
  VIDEODATA_KEY    或文件 ~/.config/videodata/api_key（权限 600）
  EDGE_STORE       视频与关键帧存放目录，默认 ~/claude-work/videodata-store
  EDGE_HOST        回传笔记时的机器名，默认 gpu4090

路由：
  抖音 / 小红书  -> 向服务器要 CDN 直链（POST /resolve direct=true，只传几 KB），4090 直连 CDN 下载；
      老版本服务没有 direct 时，改用它的代理播放地址 stream_url
  B站 / 优酷 / 腾讯 / 爱奇艺 / 芒果等国内站 -> 4090 自己跑 yt-dlp 直连源站
  YouTube / TikTok / Meta / iSpot 等海外站 -> 4090 连不上，不下载；记为 skipped，
      YouTube 链接可让 Gemini 直接读（不用下载）
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

BASE = (os.environ.get("VIDEODATA_BASE") or "https://videodata.sligenai.cn").rstrip("/")
STORE = Path(os.environ.get("EDGE_STORE") or Path.home() / "claude-work" / "videodata-store")
HOST = os.environ.get("EDGE_HOST") or "gpu4090"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

OVERSEAS = ("youtube.com", "youtu.be", "tiktok.com", "facebook.com", "fb.watch", "instagram.com", "ispot.tv",
            "vimeo.com", "twitter.com", "x.com", "reddit.com", "adstransparency.google.com", "dailymotion.com")
CN_DIRECT = ("douyin.com", "iesdouyin.com", "xiaohongshu.com", "xhslink.com")


def api_key() -> str:
    k = os.environ.get("VIDEODATA_KEY")
    if k:
        return k.strip()
    f = Path.home() / ".config" / "videodata" / "api_key"
    if f.is_file():
        return f.read_text().strip()
    sys.exit("缺少 API Key：设 VIDEODATA_KEY，或写入 ~/.config/videodata/api_key（chmod 600）")


def call(method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + "/api/v1" + path, data=data, method=method,
                                 headers={"X-API-Key": api_key(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{method} {path} -> {e.code} {e.read().decode('utf-8', 'replace')[:300]}")


def kind_of(url: str) -> str:
    host = (urlparse(url).netloc or "").lower()
    if any(host == d or host.endswith("." + d) for d in OVERSEAS):
        return "overseas"
    if any(host == d or host.endswith("." + d) for d in CN_DIRECT):
        return "cn-direct"
    return "cn-ytdlp"


def slug_of(url: str) -> str:
    host = (urlparse(url).netloc or "x").lower().replace("www.", "").split(".")[0]
    return f"{host}_{hashlib.sha1(url.encode()).hexdigest()[:10]}"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_to(url: str, dest: Path, headers: dict) -> int:
    """直连 CDN 下载；返回字节数。"""
    h = {"User-Agent": UA, **(headers or {})}
    req = urllib.request.Request(url, headers=h)
    n = 0
    with urllib.request.urlopen(req, timeout=60) as r, dest.open("wb") as f:
        for chunk in iter(lambda: r.read(1 << 20), b""):
            f.write(chunk)
            n += len(chunk)
    return n


def download_direct(url: str, d: Path) -> Path:
    """抖音 / 小红书：向服务器要直链（几 KB），然后 4090 直连 CDN。"""
    r = call("POST", "/resolve", {"url": url, "direct": True}, timeout=120)
    dr = r.get("direct")
    out = d / "video.mp4"
    if not dr:
        # 老版本服务没有 direct：改走它的代理播放地址（服务器在国内时是国内流量；音视频齐全的平台是直通转发，不落盘）
        if not r.get("stream_url"):
            raise RuntimeError("服务器既没给直链也没给播放地址")
        fetch_to(r["stream_url"], out, {})
    elif dr.get("muxed", True) or not dr.get("audio_url"):
        fetch_to(dr["video_url"], out, dr.get("headers") or {})
    else:
        v, a = d / "v.part", d / "a.part"
        fetch_to(dr["video_url"], v, dr.get("headers") or {})
        fetch_to(dr["audio_url"], a, dr.get("headers") or {})
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(v), "-i", str(a), "-c", "copy", str(out)],
                       check=True, timeout=300)
        v.unlink(missing_ok=True)
        a.unlink(missing_ok=True)
    return out


def download_ytdlp(url: str, d: Path, max_height: int) -> Path:
    cmd = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-playlist",
           "-f", f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b",
           "-S", "vcodec:h264,acodec:aac", "--merge-output-format", "mp4",
           "--retries", "3", "--socket-timeout", "30",
           "-o", str(d / "video.%(ext)s"), "--print", "after_move:filepath"]
    if "bilibili.com" in url:
        cmd += ["--add-header", "Referer:https://www.bilibili.com/"]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    lines = [x for x in p.stdout.splitlines() if x.strip()]
    if p.returncode != 0 or not lines or not Path(lines[-1]).exists():
        err = (p.stderr.strip().splitlines() or ["yt-dlp 失败"])[-1]
        raise RuntimeError(err[:300])
    return Path(lines[-1])


def probe(p: Path) -> dict:
    j = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(p)],
        capture_output=True, text=True, timeout=60, check=True).stdout)
    v = next((s for s in j["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in j["streams"] if s["codec_type"] == "audio"), None)
    num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
    fps = round(float(num) / float(den or 1), 2) if float(den or 1) else None
    return {"duration_s": round(float(j["format"].get("duration") or 0), 2),
            "width": v.get("width"), "height": v.get("height"), "fps": fps,
            "vcodec": v.get("codec_name"), "acodec": a.get("codec_name") if a else None,
            "has_audio": bool(a), "bitrate_kbps": round(int(j["format"].get("bit_rate") or 0) / 1000)}


def shots(p: Path, dur: float) -> dict:
    """镜头切点：ffmpeg 场景变化检测（阈值 0.3）。"""
    r = subprocess.run(["ffmpeg", "-v", "info", "-i", str(p), "-vf", "select='gt(scene,0.3)',showinfo",
                        "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=300)
    cuts = [round(float(m), 2) for m in re.findall(r"pts_time:([0-9.]+)", r.stderr)]
    n = len(cuts) + 1
    return {"count": n, "avg_len_s": round(dur / n, 2) if dur else None, "cuts_s": cuts[:60]}


def frames(p: Path, d: Path, dur: float) -> str | None:
    """8 张关键帧 + 一张 4x2 拼图，留在 4090 上，给后面的 AI 拆解用。"""
    fd = d / "frames"
    fd.mkdir(exist_ok=True)
    if dur <= 0:
        return None
    fps = 8 / dur
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(p), "-vf", f"fps={fps:.6f},scale=360:-2",
                    "-frames:v", "8", str(fd / "f%02d.jpg")], timeout=180)
    sheet = d / "sheet.jpg"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(p), "-vf", f"fps={fps:.6f},scale=360:-2,tile=4x2",
                    "-frames:v", "1", str(sheet)], timeout=180)
    return str(sheet) if sheet.exists() else None


def summarize(media: dict, sh: dict) -> str:
    parts = [f"{media['duration_s']}s", f"{media['width']}x{media['height']}", f"{media['fps']}fps",
             f"{media['vcodec']}/{media['acodec'] or '无音轨'}", f"镜头 {sh['count']} 个，平均 {sh['avg_len_s']}s"]
    return " · ".join(str(x) for x in parts)


def process(url: str, run_id: str | None, max_height: int) -> dict:
    kind = kind_of(url)
    if kind == "overseas":
        return {"url": url, "status": "skipped",
                "reason": "海外站，4090 连不上；YouTube 链接可让 Gemini 直接读，不必下载"}
    d = STORE / (run_id or "adhoc") / slug_of(url)
    d.mkdir(parents=True, exist_ok=True)
    video = next(iter(sorted(d.glob("video.*"))), None)
    t0 = time.time()
    try:
        if not video:
            if kind == "cn-direct":
                try:
                    video = download_direct(url, d)
                except Exception as e:  # 直链拿不到时退回 yt-dlp
                    print(f"  直链失败（{e}），改用 yt-dlp", file=sys.stderr)
                    video = download_ytdlp(url, d, max_height)
            else:
                video = download_ytdlp(url, d, max_height)
        media = probe(video)
        sh = shots(video, media["duration_s"])
        sheet = frames(video, d, media["duration_s"])
        size_mb = round(video.stat().st_size / 1e6, 2)
        note = {"url": url, "status": "ok", "path": str(video), "sha256": sha256(video), "size_mb": size_mb,
                "media": media, "shots": sh, "summary": summarize(media, sh)}
        (d / "facts.json").write_text(json.dumps({**note, "sheet": sheet}, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
        print(f"  ✓ {slug_of(url)}  {size_mb}MB  {note['summary']}  用时 {time.time() - t0:.0f}s")
        return note
    except Exception as e:
        print(f"  ✗ {slug_of(url)}  {e}", file=sys.stderr)
        return {"url": url, "status": "failed", "reason": str(e)[:480]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="取某次检索结果里的视频")
    r.add_argument("run_id")
    r.add_argument("--top", type=int, default=5)
    r.add_argument("--urls", nargs="*")
    r.add_argument("--max-height", type=int, default=720)
    r.add_argument("--no-post", action="store_true", help="只在本地做，不回传服务器")
    u = sub.add_parser("url", help="取单条链接")
    u.add_argument("url")
    u.add_argument("--run")
    u.add_argument("--max-height", type=int, default=720)
    a = ap.parse_args()
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        sys.exit("需要 ffmpeg / ffprobe")

    if a.cmd == "url":
        urls, run_id, post = [a.url], a.run, bool(a.run)
    else:
        res = call("GET", f"/runs/{a.run_id}", timeout=120)
        vids = [v for g in res["dimensions"] for v in g["videos"]]
        if a.urls:
            urls = a.urls
        else:
            urls = [v["url"] for v in sorted(vids, key=lambda v: v.get("score", 0), reverse=True)[:a.top]]
        run_id, post = a.run_id, not a.no_post
    print(f"取 {len(urls)} 条 -> {STORE}")
    notes = [process(x, run_id, a.max_height) for x in urls]
    if run_id:  # 4090 上永远留一份完整清单
        mf = STORE / run_id / "manifest.json"
        old = {n["url"]: n for n in json.loads(mf.read_text(encoding="utf-8"))} if mf.exists() else {}
        old.update({n["url"]: n for n in notes})
        mf.write_text(json.dumps(list(old.values()), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"本地清单：{mf}")
    if post:
        try:
            out = call("POST", f"/runs/{run_id}/edge-notes", {"host": HOST, "items": notes})
            print(f"已回传笔记 {len(notes)} 条（服务器累计 {out['count']} 条，只含结论与路径，不含视频）")
        except RuntimeError as e:  # 服务端还没升级到有 edge-notes 的版本：不影响本地结果
            print(f"回传跳过（服务端暂无 edge-notes 接口，结论只在 4090 本地清单里）：{str(e)[:120]}", file=sys.stderr)
    ok = sum(n["status"] == "ok" for n in notes)
    print(f"完成：成功 {ok} / 跳过 {sum(n['status'] == 'skipped' for n in notes)} / "
          f"失败 {sum(n['status'] == 'failed' for n in notes)}")
    return 0 if ok or not notes else 1


if __name__ == "__main__":
    sys.exit(main())
