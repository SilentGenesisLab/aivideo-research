from pathlib import Path
import json
import math
import subprocess


def run(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('媒体处理失败：'+result.stderr.decode('utf-8','replace')[-1500:])
    return result.stdout


def probe(path):
    result = json.loads(run(['ffprobe','-v','error','-show_format','-show_streams','-of','json',str(path)]))
    video = next((x for x in result['streams'] if x['codec_type']=='video'),None)
    duration = float(result.get('format',{}).get('duration') or (video or {}).get('duration') or 0)
    if not video or not math.isfinite(duration) or duration <= 0:
        raise ValueError('文件不是时长有效的视频。')
    return dict(duration_seconds=duration,video_start_seconds=float(video.get('start_time') or 0),video_duration_seconds=float(video.get('duration') or duration),width=video['width'],height=video['height'],video_codec=video.get('codec_name'),audio_present=any(x['codec_type']=='audio' for x in result['streams']),bytes=Path(path).stat().st_size)


