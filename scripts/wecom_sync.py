from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from app.client import WorkbenchClient
from scripts.wechat_sync import group_messages
from scripts.wecom_crypto import create_snapshot, discover_dataset


_USEFUL_TEXT = re.compile(r"[\u3400-\u9fffA-Za-z0-9]")
_LONG_IDENTIFIER = re.compile(r"^[A-Za-z0-9_:/+.-]{24,}$")
_MEDIA_TYPES = {
    4: "voice",
    14: "image",
    15: "image",
    16: "image",
    20: "file",
    29: "file",
    36: "file",
    101: "link",
    123: "file",
    141: "video",
    145: "link",
    561: "link",
    573: "link",
}


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data) and shift < 70:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7
    raise ValueError("消息字段不完整")


def _protobuf_strings(data: bytes, depth: int = 0) -> list[str]:
    values: list[str] = []
    offset = 0
    while offset < len(data):
        try:
            tag, offset = _varint(data, offset)
            wire = tag & 7
            if wire == 0:
                _, offset = _varint(data, offset)
            elif wire == 1:
                offset += 8
            elif wire == 5:
                offset += 4
            elif wire == 2:
                length, offset = _varint(data, offset)
                if length < 0 or offset + length > len(data):
                    break
                payload = data[offset : offset + length]
                offset += length
                try:
                    text = payload.decode("utf-8").strip("\x00 \r\n\t")
                except UnicodeDecodeError:
                    text = ""
                if (
                    text
                    and len(text) <= 1000
                    and _USEFUL_TEXT.search(text)
                    and not _LONG_IDENTIFIER.fullmatch(text)
                ):
                    values.append(text)
                if depth < 3 and payload:
                    values.extend(_protobuf_strings(payload, depth + 1))
            else:
                break
        except (ValueError, IndexError):
            break
    return values


def message_text(content: bytes | None) -> str:
    if not content:
        return ""
    result: list[str] = []
    seen: set[str] = set()
    for value in _protobuf_strings(bytes(content)):
        cleaned = " ".join(value.split())
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= 8:
            break
    return "；".join(result)[:4000]


def _normalized_message(
    row: sqlite3.Row, users: dict[str, str], self_user_id: str = ""
) -> dict[str, Any]:
    content_type = int(row["content_type"] or 0)
    text = message_text(row["content"])
    media_type = _MEDIA_TYPES.get(content_type)
    sender_id = str(row["sender_id"] or "")
    is_self = bool(self_user_id and sender_id == self_user_id)
    message: dict[str, Any] = {
        "messageId": int(row["message_id"]),
        "timestamp": int(row["send_time"]),
        "timestampMs": int(row["send_time"]) * 1000,
        "direction": "out" if is_self else "in",
        "kind": media_type or "text",
        "text": text,
        "cursor": {
            "sortSeq": int(row["sequence"] or row["message_id"]),
            "createTime": int(row["send_time"]),
            "localId": int(row["message_id"]),
        },
        "sender": {
            "username": sender_id,
            "name": users.get(sender_id, "企微联系人"),
            "isSelf": is_self,
        },
    }
    if media_type:
        message["media"] = {"type": media_type, "parsed": bool(text)}
    return message


def _self_user_id(messages: sqlite3.Connection) -> str:
    row = messages.execute(
        "SELECT sender_id, COUNT(DISTINCT conversation_id) AS conversation_count "
        "FROM message_table WHERE conversation_id LIKE 'S:%' "
        "GROUP BY sender_id ORDER BY conversation_count DESC, COUNT(*) DESC LIMIT 1"
    ).fetchone()
    return str(row["sender_id"] or "") if row else ""


def _private_display_name(
    messages: sqlite3.Connection,
    users: dict[str, str],
    conversation_id: str,
    self_user_id: str,
) -> str:
    participants = messages.execute(
        "SELECT sender_id, MAX(send_time) AS last_send_time FROM message_table "
        "WHERE conversation_id = ? GROUP BY sender_id ORDER BY last_send_time DESC",
        (conversation_id,),
    ).fetchall()
    for participant in participants:
        sender_id = str(participant["sender_id"] or "")
        if sender_id and sender_id != self_user_id and users.get(sender_id):
            return users[sender_id]
    return "企微联系人"


def run_wecom_sync(client: WorkbenchClient, mode: str = "incremental") -> dict[str, int]:
    existing = {
        item["session_id"]: item
        for item in client.request_json("GET", "/api/wechat/conversations?source=wecom")
    }
    snapshot = create_snapshot()
    cutoff = int((datetime.now(UTC) - timedelta(days=7)).timestamp())
    scanned = 0
    created = 0
    try:
        messages = sqlite3.connect(f"file:{snapshot / 'message.db'}?mode=ro", uri=True)
        sessions = sqlite3.connect(f"file:{snapshot / 'session.db'}?mode=ro", uri=True)
        users_db = sqlite3.connect(f"file:{snapshot / 'user.db'}?mode=ro", uri=True)
        messages.row_factory = sqlite3.Row
        sessions.row_factory = sqlite3.Row
        users_db.row_factory = sqlite3.Row
        users = {
            str(row["id"]): str(row["name"] or "企微联系人")
            for row in users_db.execute("SELECT id, name FROM user_table")
        }
        session_names = {
            str(row["id"]): str(row["name"] or "")
            for row in sessions.execute("SELECT id, name FROM conversation_table")
        }
        self_user_id = _self_user_id(messages)
        account_hint = discover_dataset().parent.name
        account_fingerprint = hashlib.sha256(account_hint.encode()).hexdigest()[:24]
        for session in messages.execute(
            "SELECT conversation_id AS id, MAX(send_time) AS last_message_time "
            "FROM message_table WHERE send_time >= ? "
            "AND (conversation_id LIKE 'R:%' OR conversation_id LIKE 'S:%') "
            "GROUP BY conversation_id ORDER BY last_message_time",
            (cutoff,),
        ):
            raw_session_id = str(session["id"])
            session_id = f"wecom:{raw_session_id}"
            current = existing.get(session_id)
            if current and current.get("listen_status") == "blocked":
                continue
            start = cutoff
            if current and current.get("create_time") and mode != "rescan":
                start = max(cutoff, int(current["create_time"]) - 5 * 60)
            rows = messages.execute(
                "SELECT message_id, sequence, sender_id, content_type, send_time, content "
                "FROM message_table WHERE conversation_id = ? AND send_time >= ? "
                "ORDER BY send_time, sequence, message_id",
                (raw_session_id, start),
            ).fetchall()
            normalized = [
                _normalized_message(row, users, self_user_id) for row in rows
            ]
            scanned += len(normalized)
            is_group = raw_session_id.startswith("R:")
            display_name = session_names.get(raw_session_id) or (
                "未命名企微群聊"
                if is_group
                else _private_display_name(messages, users, raw_session_id, self_user_id)
            )
            for window in group_messages(normalized):
                result = client.request_json(
                    "POST",
                    "/api/wechat/windows",
                    {
                        "source": "wecom",
                        "account_fingerprint": account_fingerprint,
                        "session_id": raw_session_id,
                        "display_name": display_name,
                        "kind": "group" if is_group else "friend",
                        "messages": window,
                    },
                )
                created += int(bool(result.get("created")))
        messages.close()
        sessions.close()
        users_db.close()
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
    return {"messages": scanned, "windows": created, "skipped": 0}
