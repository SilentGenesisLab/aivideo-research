"""DeepSeek 客户端（OpenAI 兼容接口），支持图片输入与 JSON 输出。"""
import base64
import json
import mimetypes
import re
import time

import requests

from . import config


class LLMError(RuntimeError):
    pass


def _image_part(image_path: str) -> dict:
    mime = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def parse_json(text: str):
    """容错解析：去掉 ```json 围栏，截取第一个完整 JSON 对象。"""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def chat_json(system: str, user: str, image_path: str | None = None,
              model: str | None = None, max_tokens: int = 8000, retries: int = 2) -> dict:
    if not config.DEEPSEEK_API_KEY:
        raise LLMError("缺少 DEEPSEEK_API_KEY，请在 .env 中配置")
    content = [{"type": "text", "text": user}]
    if image_path:
        content.append(_image_part(image_path))
    body = {
        "model": model or (config.VISION_MODEL if image_path else config.TEXT_MODEL),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": content if image_path else user},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": max_tokens,
    }
    last_err = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(
                f"{config.DEEPSEEK_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"},
                json=body,
                timeout=180,
            )
            if r.status_code in (429, 500, 502, 503, 504):
                raise LLMError(f"HTTP {r.status_code}: {r.text[:200]}")
            if r.status_code != 200:
                # 非重试类错误直接抛出
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            msg = r.json()["choices"][0]["message"]["content"] or ""
            return parse_json(msg)
        except (requests.RequestException, LLMError, json.JSONDecodeError, KeyError) as e:
            last_err = e
            if isinstance(e, LLMError) and "HTTP 4" in str(e) and "HTTP 429" not in str(e):
                break
            time.sleep(2 * (attempt + 1))
    raise LLMError(f"DeepSeek 调用失败：{last_err}")
