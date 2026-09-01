from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from app.client import WorkbenchClient
from scripts.personal_wechat_crypto import (
    PersonalWechatUnsupportedError,
    WechatDataset,
    decrypted_dataset,
    discover_dataset,
)
from scripts.personal_wechat_keys import load_key
from scripts.transcription import transcribe_material
from scripts.wechat_sync import _ocr_image, group_messages


FIELD_ALIASES = {
    "local_id": ("local_id", "localid", "localId", "msgLocalId"),
    "server_id": ("server_id", "serverid", "msgSvrId", "mesSvrId"),
    "create_time": ("create_time", "createtime", "createTime", "msgCreateTime"),
    "sort_seq": ("sort_seq", "sortseq", "sortSeq", "sequence"),
    "talker": ("talker", "username", "session_id", "sessionId", "chatName"),
    "sender": ("sender", "sender_username", "senderUsername", "fromUser"),
    "is_send": ("is_send", "issend", "isSend"),
    "kind": ("type", "msg_type", "msgType", "messageType", "localType"),
    "content": ("content", "message", "msgContent", "compressContent"),
    "path": ("path", "local_path", "localPath", "filePath", "mediaPath"),
}


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, str]:
    return {
        str(row[1]).lower(): str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _column(columns: dict[str, str], name: str) -> str | None:
    return next(
        (
            columns.get(alias.lower())
            for alias in FIELD_ALIASES[name]
            if alias.lower() in columns
        ),
        None,
    )


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return str(value)
    raw = bytes(value)
    for candidate in (raw, _zstd(raw)):
        try:
            return candidate.decode("utf-8").rstrip("\x00")
        except UnicodeDecodeError:
            continue
    return ""


def _zstd(raw: bytes) -> bytes:
    try:
        import zstandard

        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=4_000_000)
    except Exception:
        return raw


def _xml_text(content: str) -> str:
    if not content.lstrip().startswith("<"):
        return content
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return content
    parts: list[str] = []
    for tag in ("title", "des", "url", "content"):
        for element in root.iter(tag):
            text = (element.text or "").strip()
            if text and text not in parts:
                parts.append(text)
    return "\n".join(parts) or content


def _message_kind(raw_type: Any, content: str, path: str) -> str:
    try:
        value = int(raw_type or 0)
    except (TypeError, ValueError):
        value = 0
    if value == 1:
        return "text"
    if value == 3:
        return "image"
    if value == 34:
        return "voice"
    if value in {43, 62}:
        return "video"
    if value == 49:
        lowered = f"{content} {path}".lower()
        if any(
            suffix in lowered
            for suffix in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")
        ):
            return "file"
        return "link" if "<url>" in lowered else "quote"
    return "text" if content.strip() else "other"


def _split_group_sender(sender: str, content: str) -> tuple[str, str]:
    if sender:
        return sender, content
    match = re.match(r"^([^:\n]{3,128}):\n(.+)$", content, re.S)
    return (match.group(1), match.group(2)) if match else ("", content)


def _rows(connection: sqlite3.Connection, table: str) -> Iterable[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    yield from (dict(row) for row in connection.execute(f'SELECT * FROM "{table}"'))


def _contacts(databases: Iterable[Path]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in databases:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for table in _tables(connection):
                columns = _columns(connection, table)
                username = next(
                    (
                        columns.get(name)
                        for name in ("username", "user_name", "wxid")
                        if name in columns
                    ),
                    None,
                )
                display = next(
                    (
                        columns.get(name)
                        for name in ("remark", "nickname", "display_name", "name")
                        if name in columns
                    ),
                    None,
                )
                if not username or not display:
                    continue
                for row in _rows(connection, table):
                    key, value = _decode(row.get(username)), _decode(row.get(display))
                    if key and value:
                        result[key] = value
        finally:
            connection.close()
    return result


def _media_paths(databases: Iterable[Path]) -> dict[int, str]:
    candidates: dict[int, set[str]] = defaultdict(set)
    for path in databases:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for table in _tables(connection):
                columns = _columns(connection, table)
                local_id = _column(columns, "local_id")
                media_path = _column(columns, "path")
                if not local_id or not media_path:
                    continue
                for row in _rows(connection, table):
                    try:
                        key = int(row.get(local_id) or 0)
                    except (TypeError, ValueError):
                        continue
                    value = _decode(row.get(media_path))
                    if key and value:
                        candidates[key].add(value)
        finally:
            connection.close()
    return {
        key: next(iter(values))
        for key, values in candidates.items()
        if len(values) == 1
    }


def read_messages(
    clear: Path, dataset: WechatDataset, start_ms: int
) -> dict[str, dict[str, Any]]:
    databases = sorted(clear.rglob("*.db"))
    contacts = _contacts(
        path for path in databases if path.name in {"contact.db", "session.db"}
    )
    media_paths = _media_paths(
        path
        for path in databases
        if path.name.startswith("media_") or path.name == "message_resource.db"
    )
    conversations: dict[str, dict[str, Any]] = {}
    recognized = 0
    for path in (path for path in databases if path.name.startswith("message_")):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for table in _tables(connection):
                columns = _columns(connection, table)
                selected = {name: _column(columns, name) for name in FIELD_ALIASES}
                if (
                    not selected["local_id"]
                    or not selected["create_time"]
                    or not selected["content"]
                ):
                    continue
                if not selected["talker"] and not selected["sender"]:
                    continue
                recognized += 1
                for row in _rows(connection, table):
                    timestamp = int(row.get(selected["create_time"]) or 0)
                    timestamp_ms = (
                        timestamp if timestamp > 10_000_000_000 else timestamp * 1000
                    )
                    if timestamp_ms < start_ms:
                        continue
                    content = _decode(row.get(selected["content"]))
                    talker = (
                        _decode(row.get(selected["talker"]))
                        if selected["talker"]
                        else ""
                    )
                    sender = (
                        _decode(row.get(selected["sender"]))
                        if selected["sender"]
                        else ""
                    )
                    sender, content = _split_group_sender(sender, content)
                    local_id = int(row.get(selected["local_id"]) or 0)
                    server_id = (
                        _decode(row.get(selected["server_id"]))
                        if selected["server_id"]
                        else ""
                    )
                    if server_id == "0":
                        server_id = ""
                    raw_path = (
                        _decode(row.get(selected["path"])) if selected["path"] else ""
                    ) or media_paths.get(local_id, "")
                    local_path = _resolve_media_path(dataset.root.parent, raw_path)
                    kind = _message_kind(
                        row.get(selected["kind"]) if selected["kind"] else 0,
                        content,
                        raw_path,
                    )
                    is_send = (
                        int(row.get(selected["is_send"]) or 0)
                        if selected["is_send"]
                        else 0
                    )
                    session_id = talker or sender
                    if not session_id:
                        continue
                    message_id = (
                        server_id or f"{path.stem}:{table}:{local_id}:{timestamp}"
                    )
                    media = {"type": kind}
                    if local_path:
                        media["localPath"] = str(local_path)
                    item = {
                        "messageId": message_id,
                        "timestamp": timestamp_ms // 1000,
                        "timestampMs": timestamp_ms,
                        "direction": "out" if is_send else "in",
                        "kind": kind,
                        "text": _xml_text(content)
                        if kind in {"text", "link", "quote"}
                        else "",
                        "cursor": {
                            "sortSeq": int(row.get(selected["sort_seq"]) or 0)
                            if selected["sort_seq"]
                            else 0,
                            "createTime": timestamp_ms // 1000,
                            "localId": local_id,
                        },
                        "sender": {
                            "username": sender,
                            "name": contacts.get(sender, sender),
                            "isSelf": bool(is_send),
                        },
                        "media": media,
                    }
                    conversation = conversations.setdefault(
                        session_id,
                        {
                            "session_id": session_id,
                            "display_name": contacts.get(session_id, session_id),
                            "kind": "group"
                            if session_id.endswith("@chatroom")
                            else "friend",
                            "messages": [],
                        },
                    )
                    conversation["messages"].append(item)
        finally:
            connection.close()
    if not recognized:
        raise PersonalWechatUnsupportedError("当前微信版本暂不支持")
    return conversations


def compatibility_summary(clear: Path) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    message_count = 0
    recognized = 0
    latest_timestamp = 0
    shards = sorted(clear.glob("message/message_*.db"))
    for path in shards:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for table in _tables(connection):
                columns = _columns(connection, table)
                local_id = _column(columns, "local_id")
                create_time = _column(columns, "create_time")
                content = _column(columns, "content")
                kind = _column(columns, "kind")
                talker = _column(columns, "talker")
                sender = _column(columns, "sender")
                if (
                    not local_id
                    or not create_time
                    or not content
                    or not (talker or sender)
                ):
                    continue
                recognized += 1
                count, latest = connection.execute(
                    f'SELECT COUNT(*), COALESCE(MAX("{create_time}"), 0) FROM "{table}"'
                ).fetchone()
                message_count += int(count or 0)
                latest_timestamp = max(latest_timestamp, int(latest or 0))
                if kind:
                    for raw_type, total in connection.execute(
                        f'SELECT "{kind}", COUNT(*) FROM "{table}" GROUP BY "{kind}"'
                    ):
                        counts[str(raw_type)] += int(total or 0)
        finally:
            connection.close()
    if not recognized:
        raise PersonalWechatUnsupportedError("当前微信版本暂不支持")
    return {
        "supported": True,
        "database_count": len(list(clear.rglob("*.db"))),
        "message_shards": len(shards),
        "message_count": message_count,
        "latest_timestamp": latest_timestamp,
        "type_counts": dict(sorted(counts.items())),
    }


def read_message_metadata(clear: Path, start_ms: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    recognized = 0
    for path in sorted(clear.glob("message/message_*.db")):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            for table in _tables(connection):
                columns = _columns(connection, table)
                local_id = _column(columns, "local_id")
                server_id = _column(columns, "server_id")
                create_time = _column(columns, "create_time")
                kind = _column(columns, "kind")
                talker = _column(columns, "talker")
                sender = _column(columns, "sender")
                content = _column(columns, "content")
                if (
                    not local_id
                    or not create_time
                    or not content
                    or not (talker or sender)
                ):
                    continue
                recognized += 1
                selected = [local_id, create_time]
                selected.extend(
                    column for column in (server_id, kind, talker, sender) if column
                )
                quoted = ", ".join(f'"{column}"' for column in selected)
                connection.row_factory = sqlite3.Row
                for raw in connection.execute(
                    f'SELECT {quoted} FROM "{table}" WHERE "{create_time}" >= ?',
                    (start_ms // 1000,),
                ):
                    row = dict(raw)
                    timestamp = int(row.get(create_time) or 0)
                    timestamp_ms = (
                        timestamp if timestamp > 10_000_000_000 else timestamp * 1000
                    )
                    identifier = _decode(row.get(server_id)) if server_id else ""
                    if identifier == "0":
                        identifier = ""
                    items.append(
                        {
                            "id": identifier
                            or f"{path.stem}:{table}:{row.get(local_id)}:{timestamp}",
                            "timestamp_ms": timestamp_ms,
                            "kind": _message_kind(row.get(kind) if kind else 0, "", ""),
                            "session_id": _decode(row.get(talker))
                            if talker
                            else _decode(row.get(sender)),
                        }
                    )
        finally:
            connection.close()
    if not recognized:
        raise PersonalWechatUnsupportedError("当前微信版本暂不支持")
    return items


def _resolve_media_path(account_root: Path, value: str) -> Path | None:
    if not value:
        return None
    path = Path(value.removeprefix("file://")).expanduser()
    if not path.is_absolute():
        path = account_root / path
    try:
        resolved = path.resolve()
        resolved.relative_to(account_root.resolve())
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _decode_dat(source: Path, destination: Path) -> bool:
    raw = source.read_bytes()
    signatures = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")
    if any(raw.startswith(signature) for signature in signatures):
        destination.write_bytes(raw)
        return True
    for signature in signatures:
        key = raw[0] ^ signature[0] if raw else 0
        decoded = bytes(value ^ key for value in raw)
        if decoded.startswith(signature):
            destination.write_bytes(decoded)
            return True
    return False


def _transcribe_voice(path: Path) -> str:
    import pysilk

    with tempfile.TemporaryDirectory(prefix="finance-workbench-voice-") as temporary:
        pcm = Path(temporary) / "voice.pcm"
        wav = Path(temporary) / "voice.wav"
        with path.open("rb") as source, pcm.open("wb") as output:
            pysilk.decode(source, output, 24000)
        subprocess.run(
            [
                "/opt/homebrew/bin/ffmpeg",
                "-v",
                "error",
                "-f",
                "s16le",
                "-ar",
                "24000",
                "-ac",
                "1",
                "-i",
                str(pcm),
                str(wav),
            ],
            check=True,
            timeout=60,
        )
        result = transcribe_material({"filename": "微信语音.wav"}, wav.read_bytes())
        return str(result.get("text") or "").strip()


def enrich_media(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for message in messages:
        media = message.get("media") if isinstance(message.get("media"), dict) else {}
        kind = str(message.get("kind") or "")
        path_value = str(media.get("localPath") or "")
        if not path_value:
            if kind in {"image", "voice"}:
                media["extractionStatus"] = "failed"
                message["media"] = media
            continue
        path = Path(path_value)
        try:
            if kind == "image":
                with tempfile.NamedTemporaryFile(suffix=".jpg") as decoded:
                    image = path
                    if path.suffix.lower() == ".dat":
                        image = Path(decoded.name)
                        if not _decode_dat(path, image):
                            media["extractionStatus"] = "failed"
                            continue
                    text = _ocr_image(str(image))
                if text:
                    media["ocrText"] = text
                    media["extractionStatus"] = "completed"
                    message["text"] = f"[图片识别] {text}"
                else:
                    media["extractionStatus"] = "failed"
            elif kind == "voice":
                text = _transcribe_voice(path)
                if text:
                    media["transcript"] = text
                    media["extractionStatus"] = "completed"
                    message["text"] = f"[语音转写] {text}"
                else:
                    media["extractionStatus"] = "failed"
        except (ImportError, OSError, RuntimeError, subprocess.SubprocessError):
            media["extractionStatus"] = "failed"
        message["media"] = media
    return messages


def _enabled_at() -> int:
    raw = os.getenv("PERSONAL_WECHAT_DIRECT_ENABLED_AT", "").strip()
    if not raw:
        raise RuntimeError("个人微信直读尚未设置启用时间")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError("个人微信直读启用时间无效") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def run_direct_wechat_sync(
    client: WorkbenchClient, mode: str = "incremental"
) -> dict[str, int]:
    dataset = discover_dataset()
    start_ms = _enabled_at()
    existing = {
        item["session_id"]: item
        for item in client.request_json(
            "GET", "/api/wechat/conversations?source=personal_wechat"
        )
    }
    with decrypted_dataset(dataset, load_key) as clear:
        conversations = read_messages(clear, dataset, start_ms)
    scanned = created = skipped = 0
    prepared: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    for session_id, conversation in conversations.items():
        current = existing.get(session_id)
        if current and current.get("listen_status") == "blocked":
            continue
        messages = enrich_media(conversation["messages"])
        if current and current.get("create_time") and mode != "rescan":
            floor = int(current["create_time"]) * 1000 - 5 * 60 * 1000
            messages = [item for item in messages if int(item["timestampMs"]) >= floor]
        if any(
            (item.get("media") or {}).get("extractionStatus") == "failed"
            for item in messages
        ):
            raise RuntimeError("部分图片或语音暂不可读取，将在下次检查时重试")
        prepared.append((session_id, conversation, messages))
    for session_id, conversation, messages in prepared:
        scanned += len(messages)
        for window in group_messages(messages):
            result = client.request_json(
                "POST",
                "/api/wechat/windows",
                {
                    "source": "personal_wechat",
                    "account_fingerprint": dataset.fingerprint,
                    "session_id": session_id,
                    "display_name": conversation["display_name"],
                    "kind": conversation["kind"],
                    "messages": window,
                },
            )
            created += int(bool(result.get("created")))
    return {"messages": scanned, "windows": created, "skipped": skipped}
