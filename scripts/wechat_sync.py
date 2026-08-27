from __future__ import annotations

import asyncio
import hashlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.client import WorkbenchClient
from scripts.ciphertalk_client import CipherTalkClient, open_ciphertalk


VISION_OCR_SCRIPT = Path(__file__).with_name("vision_ocr.swift")


def _epoch_ms(value: Any) -> int:
    raw = int(value or 0)
    return raw if raw > 10_000_000_000 else raw * 1000


def _cursor(message: dict[str, Any]) -> tuple[int, int, int]:
    cursor = message.get("cursor") if isinstance(message.get("cursor"), dict) else {}
    return (
        int(cursor.get("sortSeq") or 0),
        int(cursor.get("createTime") or message.get("timestamp") or 0),
        int(cursor.get("localId") or message.get("messageId") or 0),
    )


def _is_missing_session(error: Exception) -> bool:
    return "Session not found" in str(error)


def group_messages(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_ms = 0
    for message in sorted(messages, key=_cursor):
        message_ms = _epoch_ms(message.get("timestampMs") or message.get("timestamp"))
        if current and (len(current) >= 200 or message_ms - previous_ms > 24 * 60 * 60 * 1000):
            windows.append(current)
            current = []
        current.append(message)
        previous_ms = message_ms
    if current:
        windows.append(current)
    return windows


async def _sessions(ciphertalk: CipherTalkClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await ciphertalk.call("list_sessions", {"offset": offset, "limit": 100})
        batch = page.get("items") if isinstance(page.get("items"), list) else []
        items.extend(batch)
        if not page.get("hasMore") or not batch:
            return items
        offset += len(batch)


async def _messages(
    ciphertalk: CipherTalkClient, session_id: str, start_ms: int
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = await ciphertalk.call(
            "get_messages",
            {
                "sessionId": session_id,
                "offset": offset,
                "limit": 200,
                "order": "asc",
                "startTime": max(1, start_ms),
                "includeMediaPaths": True,
            },
        )
        batch = page.get("items") if isinstance(page.get("items"), list) else []
        items.extend(batch)
        if not page.get("hasMore") or not batch:
            return items
        offset += len(batch)


def _ocr_image(local_path: str) -> str:
    path = Path(local_path.removeprefix("file://")).expanduser()
    if not path.is_file():
        return ""
    completed = subprocess.run(
        ["/usr/bin/swift", str(VISION_OCR_SCRIPT), str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "图片识别失败")
    return completed.stdout.strip()[:100_000]


async def _enrich_messages(
    ciphertalk: CipherTalkClient,
    session_id: str,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    for message in messages:
        if str(message.get("text") or "").strip():
            continue
        media = message.get("media") if isinstance(message.get("media"), dict) else {}
        kind = str(message.get("kind") or media.get("type") or "").strip().lower()
        if kind in {"voice", "audio"}:
            transcript = str(media.get("transcript") or "").strip()
            cursor = message.get("cursor") if isinstance(message.get("cursor"), dict) else {}
            if not transcript and cursor.get("localId") is not None and cursor.get("createTime"):
                try:
                    result = await ciphertalk.call(
                        "transcribe_voice_message",
                        {
                            "sessionId": session_id,
                            "localId": cursor["localId"],
                            "createTime": cursor["createTime"],
                        },
                    )
                    transcript = str(result.get("transcript") or "").strip()
                except RuntimeError:
                    transcript = ""
            if transcript:
                media["transcript"] = transcript
                message["media"] = media
                message["text"] = f"[语音转写] {transcript}"
        elif kind == "image" and media.get("localPath"):
            try:
                ocr_text = await asyncio.to_thread(_ocr_image, str(media["localPath"]))
            except (OSError, RuntimeError, subprocess.SubprocessError):
                ocr_text = ""
            if ocr_text:
                media["ocrText"] = ocr_text
                message["media"] = media
                message["text"] = f"[图片识别] {ocr_text}"
    return messages


async def _run(client: WorkbenchClient, mode: str) -> dict[str, int]:
    conversations = {
        item["session_id"]: item
        for item in client.request_json(
            "GET", "/api/wechat/conversations?source=personal_wechat"
        )
    }
    cutoff_ms = int((datetime.now(UTC) - timedelta(days=7)).timestamp() * 1000)
    scanned = 0
    created = 0
    skipped = 0
    async with open_ciphertalk() as ciphertalk:
        status = await ciphertalk.call("get_status")
        account_hint = str(
            status.get("activeAccountId")
            or status.get("myWxid")
            or status.get("account")
            or "local-wechat"
        )
        account_fingerprint = hashlib.sha256(account_hint.encode("utf-8")).hexdigest()[:24]
        for session in await _sessions(ciphertalk):
            if session.get("kind") not in {"friend", "group"}:
                continue
            last_ms = _epoch_ms(session.get("lastTimestampMs") or session.get("lastTimestamp"))
            existing = conversations.get(str(session.get("sessionId")))
            if existing and existing.get("listen_status") == "blocked":
                continue
            if not existing and last_ms < cutoff_ms:
                continue
            start_ms = cutoff_ms
            if existing and existing.get("create_time") and mode != "rescan":
                start_ms = max(cutoff_ms, int(existing["create_time"]) * 1000 - 5 * 60 * 1000)
            if existing and existing.get("listen_from"):
                listen_from = datetime.fromisoformat(
                    str(existing["listen_from"]).replace("Z", "+00:00")
                )
                start_ms = max(start_ms, int(listen_from.timestamp() * 1000))
            try:
                messages = await _messages(ciphertalk, str(session["sessionId"]), start_ms)
            except RuntimeError as error:
                if not _is_missing_session(error):
                    raise
                skipped += 1
                continue
            messages = await _enrich_messages(
                ciphertalk, str(session["sessionId"]), messages
            )
            scanned += len(messages)
            for window in group_messages(messages):
                result = client.request_json(
                    "POST",
                    "/api/wechat/windows",
                    {
                        "source": "personal_wechat",
                        "account_fingerprint": account_fingerprint,
                        "session_id": str(session["sessionId"]),
                        "display_name": str(session.get("displayName") or "未命名会话"),
                        "kind": str(session.get("kind") or "other"),
                        "messages": window,
                    },
                )
                created += int(bool(result.get("created")))
    return {"messages": scanned, "windows": created, "skipped": skipped}


def run_ciphertalk_sync(client: WorkbenchClient, mode: str = "incremental") -> dict[str, int]:
    return asyncio.run(_run(client, mode))
