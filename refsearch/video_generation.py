"""Text-only generation with an upstream capacity check and durable submission receipts."""
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re
from pathlib import Path

import requests

from .video_motion.store import read_json, write_json

BASE = os.getenv("VIDEODATA_AICENTRE_BASE", "https://aicentre2.sligenai.cn:8443").rstrip("/")
VIDEO = "/v1/video-generations/jobs"
H3 = "/v1/video-generations/minimax-h3"
MODELS = {"fast": ("seedance-2.0-fast", "libtv"), "normal": ("seedance-2.0", "jmapi")}
TERMINAL = {"succeeded", "failed", "cancelled", "rejected"}
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def headers(key):
    return {"Authorization": f"Bearer {key}"}


def status(key, path):
    try:
        response = requests.get(BASE + path, headers=headers(key), timeout=(4, 8))
        if path == "/health" and response.status_code == 503:
            return {"status": "unavailable"}
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}
    except (requests.RequestException, ValueError):
        return {}


def preflight(key, route, receipts=(), queue_reference=None):
    health = status(key, "/health").get("status")
    result = {"route": route, "service_status": health, "reasons": []}
    reasons = result["reasons"]
    if health in {"unavailable", "down", "error", "unhealthy"}:
        reasons.append("生成服务当前不可用")
    if route == "h3":
        workers = status(key, H3 + "/workers/status")
        result["workers"] = {k: workers[k] for k in ("online", "idle", "busy")
                             if type(workers.get(k)) is int}
        queue = {}
        known_jobs = [r["job_id"] for r in receipts if r.get("route") == route and r.get("job_id")]
        for job in ([queue_reference] if queue_reference else []) + known_jobs[:3]:
            if not JOB_ID.fullmatch(job):
                continue
            queue = status(key, f"{H3}/jobs/{job}/queue-position")
            if type(queue.get("queued_total")) is int:
                break
        idle = result["workers"].get("idle")
        depth = queue.get("queued_total")
        result["queued_total"] = depth
        result["queue_scope"] = "global_h3" if type(depth) is int else "worker_capacity_only"
        if not result["workers"]:
            reasons.append("无法取得 H3 节点状态")
        elif result["workers"].get("online") == 0 or idle == 0:
            reasons.append("H3 当前没有空闲节点")
        elif type(idle) is int and type(depth) is int and depth >= idle:
            reasons.append("H3 等待任务已占满空闲节点")
    else:
        tracked = [r for r in receipts if r.get("route") == route and r.get("job_id")][:3]
        snapshots = [r if r["status"] in TERMINAL else status(key, f"{VIDEO}/{r['job_id']}")
                     for r in tracked]
        result["queue_scope"] = "tracked_same_route_jobs"
        result["tracked_queued"] = sum(s.get("status") == "queued" for s in snapshots)
        if result["tracked_queued"]:
            reasons.append("同通道已有跟踪任务排队")
        for snapshot in snapshots:
            timed_out = snapshot.get("upstream_timeout") or "upstream generation timed out" in str(snapshot.get("error"))
            if snapshot.get("status") == "failed" and timed_out:
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(snapshot["finished_at"])).total_seconds()
                except (KeyError, TypeError, ValueError):
                    continue
                if 0 <= age < 300:
                    reasons.append("同通道最近五分钟内出现上游生成超时")
                    break
    result["decision"] = "defer" if reasons else "proceed"
    return result


def make_prompt(analysis, event_ids):
    events = {event["id"]: event for event in analysis["events"]}
    pairs = {event["id"]: event for event in analysis.get("transition_events", [])
             if event["kind"] in ("surface_change", "shot_change")
             and event.get("foot_pose_before") and event.get("foot_pose_after")}
    if not event_ids or any(event_id not in events and event_id not in pairs for event_id in event_ids):
        raise ValueError("事件不存在或未通过证据校验")
    chosen = list(dict.fromkeys(event_ids))
    selected = sorted((events[i] for i in chosen if i in events), key=lambda e: e["start"])
    transitions = sorted((pairs[i] for i in chosen if i in pairs), key=lambda e: e["time"])
    starts = [e["start"] for e in selected] + [e["before_time"] for e in transitions]
    ends = [e["end"] for e in selected] + [e["time"] for e in transitions]
    origin = min(starts)
    if max(ends) - origin > 12:
        raise ValueError("请选择同一段 12 秒内的事件")
    rows = [
        "制作一条 4 秒的 16:9 视频，依据以下画面事件制作。"
        "保持主体动作与环境变化的先后关系。"
    ]
    if transitions:
        rows.extend(_timed_directions(selected, transitions, origin))
    else:
        for event in selected:
            rows.append("\n".join(event[field] for field in
                                  ("subject", "subject_motion", "background_change", "camera_relation", "mechanism")))
    prompt = "\n\n".join(rows)
    if re.search(r"https?://|data:(?:image|video|audio)|(?:[A-Za-z]:[\\/])|/(?:home|mnt|srv|var|tmp|opt|data)/"
                 r"|reference_(?:video|image|audio)", prompt, re.I):
        raise ValueError("制作文本包含媒体引用或本机路径")
    return prompt


def _timed_directions(events, transitions, origin):
    end = transitions[-1]["time"] - origin
    scale = 3.6 / end if end > 3.6 else 1.0
    lines = []
    if events:
        lines.append("镜头与连续动作。\n" + "\n".join(
            f"{e['subject_motion']} {e['camera_relation']}" for e in events
        ))
    lines.append("依照下列时刻逐次发生变化。每一项都有独立的前后状态。")
    for event in transitions:
        second = (event["time"] - origin) * scale
        action = ("脚下地表瞬间切换" if event["kind"] == "surface_change"
                  else f"切换镜头，{event['visible_change']}")
        lines.append(
            f"{second:.2f} 秒，{action}。"
            f"地表由{event['ground_before']}切换为{event['ground_after']}。"
            f"切换前的脚部状态，{event['foot_pose_before']}。切换后的脚部状态，{event['foot_pose_after']}。"
            + ("保持这一脚步的动作衔接。" if event["continuity"] == "continuous" else "")
        )
    last = transitions[-1]
    lines.append(f"完成最后一次变化后，地表保持为{last['ground_after']}，延续最后一次变化后的脚步动作到片尾。")
    return lines


@contextmanager
def lock_submission(folder):
    import fcntl
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".submit.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def submit(folder, key, route, prompt, queue_reference=None, take=""):
    request = {"prompt": prompt, "duration_seconds": 4, "resolution": "720p", "aspect_ratio": "16:9"}
    if route == "h3":
        endpoint = H3 + "/jobs"
        request["quality"] = "medium"
    else:
        endpoint = VIDEO
        request["model"], request["channel"] = MODELS[route]
        request["sound"] = False
    identity = sha256(json.dumps([str(folder), route, request, take], sort_keys=True).encode()).hexdigest()[:32]
    request["external_ref"] = "videodata-" + identity
    path = folder / (identity + ".json")
    with lock_submission(folder):
        existing = read_json(path)
        if existing:
            return existing
        receipts = [read_json(p) for p in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]
        check = preflight(key, route, receipts, queue_reference)
        if check["decision"] == "defer":
            return {"status": "deferred", "preflight": check}
        record = {"id": identity, "route": route, "status": "submitting", "preflight": check}
        write_json(path, record)
        try:
            response = requests.post(BASE + endpoint, headers={**headers(key), "Idempotency-Key": identity},
                                     json=request, timeout=(5, 40))
            data = response.json() if response.ok else {}
            job_id = data.get("job_id") if isinstance(data, dict) else None
            if isinstance(job_id, str) and JOB_ID.fullmatch(job_id):
                record.update(status="submitted", job_id=job_id)
            elif 400 <= response.status_code < 500 and response.status_code not in (408, 409):
                record.update(status="rejected", http_status=response.status_code)
            else:
                record.update(status="unknown", http_status=response.status_code)
        except (requests.RequestException, ValueError):
            record.update(status="unknown")
        write_json(path, record)
        return record


def refresh(folder, key, identity):
    path = Path(folder) / (identity + ".json")
    if not path.is_file():
        return None
    with lock_submission(folder):
        record = read_json(path)
        if not record.get("job_id") or record["status"] in TERMINAL:
            return record
        endpoint = H3 + "/jobs" if record["route"] == "h3" else VIDEO
        data = status(key, f"{endpoint}/{record['job_id']}")
        if data.get("status"):
            record.update(status=data["status"], stage=data.get("stage"), finished_at=data.get("finished_at"),
                          upstream_timeout="upstream generation timed out" in str(data.get("error")))
            if data["status"] == "succeeded":
                urls = data.get("result_urls") or [data.get("result_url")]
                record["result_urls"] = [url for url in urls if isinstance(url, str) and url.startswith("https://")]
            write_json(path, record)
        return record
