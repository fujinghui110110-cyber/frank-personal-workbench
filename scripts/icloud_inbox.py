from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from app.client import WorkbenchClient


DEFAULT_SYNC_INBOX = Path.home() / "财务工作台投递箱"
PENDING_DIR = "待处理"
RECEIVED_DIR = "已接收"
ROOT_GUIDE = "手机投递说明.md"

AUDIO_SUFFIXES = {
    ".aac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}
VIDEO_SUFFIXES = {
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".webm",
}
IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
CHAT_SUFFIXES = {".json", ".markdown", ".md"}
CHAT_NAME_MARKERS = ("微信", "聊天", "群聊", "私聊", "wechat")


def source_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if "审批" in name:
        return "wecom_approval"
    if suffix in CHAT_SUFFIXES and any(marker in name for marker in CHAT_NAME_MARKERS):
        return "wechat_markdown"
    return "file"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def archive_path(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 2
    while (directory / f"{stem} ({index}){suffix}").exists():
        index += 1
    return directory / f"{stem} ({index}){suffix}"


def scan_icloud_inbox(
    client: WorkbenchClient,
    root: Path,
    *,
    min_age_seconds: int = 5,
    now: float | None = None,
) -> list[dict[str, Any]]:
    pending = root / PENDING_DIR
    received = root / RECEIVED_DIR
    try:
        pending.mkdir(parents=True, exist_ok=True)
        received.mkdir(parents=True, exist_ok=True)
        candidates = sorted(
            [*pending.iterdir(), *(item for item in root.iterdir() if item.is_file())],
            key=lambda item: item.name.lower(),
        )
    except OSError as error:
        print(f"同步投递箱暂不可用，稍后重试：{error}", flush=True)
        return []

    imported: list[dict[str, Any]] = []
    current_time = time.time() if now is None else now
    for path in candidates:
        if (
            path.name.startswith(".")
            or path.name == ROOT_GUIDE
            or path.suffix.lower() == ".icloud"
        ):
            continue
        try:
            stat = path.stat()
            if not path.is_file() or stat.st_size == 0:
                continue
            if current_time - stat.st_mtime < min_age_seconds:
                continue
            source_type = source_type_for(path)
            digest = file_sha256(path)
            verified = path.stat()
            if (verified.st_size, verified.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise OSError("文件仍在同步，稍后自动重试")
            result = client.intake(
                source_type=source_type,
                file_path=path,
                idempotency_key=f"icloud:{source_type}:{digest}",
            )
            destination = archive_path(received, path.name)
            path.replace(destination)
            imported.append(result)
            state = "新收件" if result.get("created") else "已去重"
            print(f"同步投递箱 {state}：{path.name} → 已接收", flush=True)
        except (OSError, RuntimeError, ValueError) as error:
            print(f"同步投递箱等待重试：{path.name}（{error}）", flush=True)
    return imported
