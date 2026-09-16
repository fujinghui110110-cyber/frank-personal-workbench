from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = "https://api.deepseek.com"
TEXT_MODEL = "deepseek-v4-flash"
VISION_MODEL = "deepseek-v4-flash-vision-exp"
KEYCHAIN_SERVICE = "finance-workbench-deepseek"
KEYCHAIN_ACCOUNT = "api-key"
MAX_IMAGE_COUNT = 8
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def get_api_key() -> str:
    configured = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if configured:
        return configured
    try:
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed and completed.returncode == 0 and completed.stdout.strip():
        return completed.stdout.strip()
    raise RuntimeError(
        "尚未配置 DeepSeek API 密钥，请运行："
        ".venv/bin/python -m scripts.configure_deepseek"
    )


def _image_type(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def _collect_images(
    image_paths: list[str] | None,
    image_bytes: bytes | None,
) -> list[tuple[str, bytes, str]]:
    images: list[tuple[str, bytes, str]] = []
    total_bytes = 0
    candidates: list[tuple[bytes, str]] = []
    if image_bytes:
        candidates.append((image_bytes, "inline"))
    for raw_path in image_paths or []:
        if len(candidates) >= MAX_IMAGE_COUNT:
            break
        try:
            content = Path(raw_path).expanduser().read_bytes()
        except (OSError, TypeError):
            continue
        candidates.append((content, str(raw_path)))
    for content, source in candidates:
        media_type = _image_type(content)
        if not media_type:
            continue
        if total_bytes + len(content) > MAX_IMAGE_BYTES:
            continue
        images.append((media_type, content, source))
        total_bytes += len(content)
        if len(images) >= MAX_IMAGE_COUNT:
            break
    return images


def call_deepseek_json(
    prompt: str,
    timeout: int,
    schema: dict[str, Any],
    *,
    image_paths: list[str] | None = None,
    image_bytes: bytes | None = None,
    media_evidence: list[dict[str, Any]] | None = None,
    api_key: str | None = None,
) -> str:
    images = _collect_images(image_paths, image_bytes)
    content: str | list[dict[str, Any]] = prompt
    if images:
        content = [{"type": "text", "text": prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {
                    "url": "data:"
                    f"{media_type};base64,{base64.b64encode(data).decode('ascii')}",
                    "detail": "auto",
                },
            }
            for media_type, data, _source in images
        )
    payload = {
        "model": VISION_MODEL if images else TEXT_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "只输出符合给定结构的 JSON 对象，不要输出 Markdown。"
                f"\nJSON 结构：{json.dumps(schema, ensure_ascii=False)}",
            },
            {"role": "user", "content": content},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "stream": False,
    }
    secret = api_key or get_api_key()
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            raise RuntimeError("DeepSeek 密钥无效或已失效，请重新配置") from None
        if error.code == 429:
            raise RuntimeError("DeepSeek 当前请求较多或额度不足，请稍后重试") from None
        raise RuntimeError(f"DeepSeek 暂时无法处理请求（{error.code}）") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise RuntimeError("暂时无法连接 DeepSeek，请检查网络后重试") from None
    try:
        response_data = json.loads(raw)
        output = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        raise RuntimeError("DeepSeek 返回内容不完整，请重试") from None
    if not isinstance(output, str) or not output.strip():
        raise RuntimeError("DeepSeek 没有返回可用内容，请重试")
    if media_evidence is not None:
        media_evidence.extend(
            {
                "source": source,
                "media_type": media_type,
                "status": "model_processed",
            }
            for media_type, _data, source in images
        )
    return output
