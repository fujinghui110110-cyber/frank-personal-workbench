from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import struct
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
from scripts.personal_wechat_keys import load_image_keys, load_key
from scripts.transcription import transcribe_material
from scripts.wechat_sync import (
    _ocr_image,
    group_messages,
    is_ignored_personal_wechat_notice,
)


FIELD_ALIASES = {
    "local_id": ("local_id", "localid", "localId", "msgLocalId"),
    "server_id": ("server_id", "serverid", "msgSvrId", "mesSvrId"),
    "create_time": ("create_time", "createtime", "createTime", "msgCreateTime"),
    "sort_seq": ("sort_seq", "sortseq", "sortSeq", "sequence"),
    "talker": ("talker", "username", "session_id", "sessionId", "chatName"),
    "sender": ("sender", "sender_username", "senderUsername", "fromUser"),
    "is_send": ("is_send", "issend", "isSend"),
    "kind": (
        "local_type",
        "type",
        "msg_type",
        "msgType",
        "messageType",
        "localType",
    ),
    "content": (
        "message_content",
        "messageContent",
        "content",
        "message",
        "msgContent",
        "compress_content",
        "compressContent",
    ),
    "path": ("path", "local_path", "localPath", "filePath", "mediaPath"),
    "packed_info": ("packed_info_data", "packedInfoData"),
}

_IMAGE_SIGNATURES = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"RIFF")
_WECHAT_IMAGE_HEADERS = (b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07")


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


def _matching_columns(columns: dict[str, str], name: str) -> list[str]:
    return [
        columns[alias.lower()]
        for alias in FIELD_ALIASES[name]
        if alias.lower() in columns
    ]


def _clean_account_name(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("wxid_"):
        match = re.match(r"^(wxid_[a-zA-Z0-9]+)", value, re.I)
        return match.group(1) if match else value
    match = re.match(r"^(.+)_([a-zA-Z0-9]{4})$", value)
    return match.group(1) if match else value


def _message_table_sessions(connection: sqlite3.Connection) -> dict[str, str]:
    tables = set(_tables(connection))
    if "Name2Id" not in tables:
        return {}
    return {
        table: username
        for (username,) in connection.execute("SELECT user_name FROM Name2Id")
        if isinstance(username, str)
        and (
            table := f"Msg_{hashlib.md5(username.encode('utf-8')).hexdigest()}"
        )
        in tables
    }


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


def _row_content(row: dict[str, Any], columns: dict[str, str]) -> str:
    for column in _matching_columns(columns, "content"):
        content = _decode(row.get(column))
        if content:
            return content
    return ""


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ValueError
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
    raise ValueError


def _protobuf_strings(data: bytes, depth: int = 0) -> list[str]:
    if depth > 5 or not data:
        return []
    result: list[str] = []
    offset = 0
    try:
        while offset < len(data):
            key, offset = _read_varint(data, offset)
            wire_type = key & 7
            if wire_type == 0:
                _, offset = _read_varint(data, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 5:
                offset += 4
            elif wire_type == 2:
                size, offset = _read_varint(data, offset)
                payload = data[offset : offset + size]
                if len(payload) != size:
                    raise ValueError
                offset += size
                try:
                    value = payload.decode("utf-8").strip("\x00\"")
                except UnicodeDecodeError:
                    value = ""
                if value and all(character.isprintable() for character in value):
                    result.append(value)
                result.extend(_protobuf_strings(payload, depth + 1))
            else:
                raise ValueError
    except (IndexError, ValueError):
        return result
    return result


def _protobuf_length_fields(data: bytes) -> list[tuple[int, bytes]]:
    result: list[tuple[int, bytes]] = []
    offset = 0
    try:
        while offset < len(data):
            key, offset = _read_varint(data, offset)
            field, wire_type = key >> 3, key & 7
            if wire_type == 0:
                _, offset = _read_varint(data, offset)
            elif wire_type == 1:
                offset += 8
            elif wire_type == 5:
                offset += 4
            elif wire_type == 2:
                size, offset = _read_varint(data, offset)
                payload = data[offset : offset + size]
                if len(payload) != size:
                    raise ValueError
                offset += size
                result.append((field, payload))
            else:
                raise ValueError
    except (IndexError, ValueError):
        pass
    return result


def _voice_transcript(packed_info: Any) -> str:
    if isinstance(packed_info, memoryview):
        packed_info = packed_info.tobytes()
    if not isinstance(packed_info, (bytes, bytearray)):
        return ""
    for field, payload in _protobuf_length_fields(bytes(packed_info)):
        if field != 5:
            continue
        for inner_field, value in _protobuf_length_fields(payload):
            if inner_field != 2:
                continue
            try:
                return value.decode("utf-8").strip()
            except UnicodeDecodeError:
                return ""
    return ""


def _media_file_index(account_root: Path) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = defaultdict(list)
    for path in (account_root / "msg").rglob("*"):
        if path.is_file():
            for key in {
                path.name.lower(),
                path.stem.lower(),
                path.stem.split("_", 1)[0].lower(),
            }:
                result[key].append(path)
    return result


def _packed_media_path(
    packed_info: Any,
    index: dict[str, list[Path]],
    kind: str,
) -> Path | None:
    if isinstance(packed_info, memoryview):
        packed_info = packed_info.tobytes()
    if not isinstance(packed_info, (bytes, bytearray)):
        return None
    candidates: list[Path] = []
    for value in _protobuf_strings(bytes(packed_info)):
        name = Path(value).name.lower()
        keys = {value.lower(), name, Path(name).stem}
        candidates.extend(path for key in keys for path in index.get(key, ()))
    if not candidates:
        return None

    if kind == "image":
        candidates = [path for path in candidates if path.suffix.lower() == ".dat"]
    elif kind == "video":
        candidates = [path for path in candidates if path.suffix.lower() == ".mp4"]
    elif kind == "file":
        candidates = [
            path
            for path in candidates
            if path.suffix.lower() not in {"", ".dat", ".mp4"}
        ]
    if not candidates:
        return None

    def rank(path: Path) -> tuple[int, int]:
        suffix = path.stem[32:] if kind == "image" else ""
        priority = {"": 0, "_h": 1, "_t": 2}.get(suffix, 3)
        return priority, -path.stat().st_mtime_ns

    return min(set(candidates), key=rank)


def _voice_data(databases: Iterable[Path], start_ms: int) -> dict[tuple[str, int], bytes]:
    candidates: dict[tuple[str, int], set[bytes]] = defaultdict(set)
    for path in databases:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if not {"Name2Id", "VoiceInfo"}.issubset(_tables(connection)):
                continue
            for session_id, local_id, raw in connection.execute(
                "SELECT n.user_name, v.local_id, v.voice_data FROM VoiceInfo v "
                "LEFT JOIN Name2Id n ON v.chat_name_id = n.rowid "
                "WHERE v.create_time >= ?",
                (start_ms // 1000,),
            ):
                if isinstance(session_id, str) and isinstance(
                    raw, (bytes, bytearray, memoryview)
                ):
                    candidates[(session_id, int(local_id or 0))].add(bytes(raw))
        finally:
            connection.close()
    return {
        key: next(iter(values))
        for key, values in candidates.items()
        if key[1] and len(values) == 1
    }


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
    value &= 0xFFFFFFFF
    if value == 1:
        return "text"
    if value == 3:
        return "image"
    if value == 34:
        return "voice"
    if value in {43, 62}:
        return "video"
    if value == 47:
        return "emoji"
    if value == 50:
        return "voip"
    if value == 49:
        lowered = f"{content} {path}".lower()
        if any(
            suffix in lowered
            for suffix in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")
        ):
            return "file"
        return "link" if "<url>" in lowered else "quote"
    if value in {10000, 10002}:
        return "system"
    return "text" if content.strip() else "other"


def _split_group_sender(sender: str, content: str) -> tuple[str, str]:
    if sender:
        return sender, content
    match = re.match(r"^([^:\n]{3,128}):\n(.+)$", content, re.S)
    return (match.group(1), match.group(2)) if match else ("", content)


def _rows(connection: sqlite3.Connection, table: str) -> Iterable[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    yield from (dict(row) for row in connection.execute(f'SELECT * FROM "{table}"'))


def _message_rows(
    connection: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
    start_ms: int,
) -> Iterable[dict[str, Any]]:
    create_time = _column(columns, "create_time")
    if not create_time:
        return
    threshold = start_ms // 1000
    connection.row_factory = sqlite3.Row
    if "real_sender_id" not in columns or "Name2Id" not in _tables(connection):
        yield from (
            dict(row)
            for row in connection.execute(
                f'SELECT * FROM "{table}" WHERE "{create_time}" >= ?',
                (threshold,),
            )
        )
        return
    yield from (
        dict(row)
        for row in connection.execute(
            f'SELECT m.*, n.user_name AS __sender_username FROM "{table}" m '
            "LEFT JOIN Name2Id n ON m.real_sender_id = n.rowid "
            f'WHERE m."{create_time}" >= ?',
            (threshold,),
        )
    )


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
                displays = [
                    columns[name]
                    for name in (
                        "remark",
                        "nickname",
                        "nick_name",
                        "display_name",
                        "name",
                    )
                    if name in columns
                ]
                if not username or not displays:
                    continue
                for row in _rows(connection, table):
                    key = _decode(row.get(username))
                    value = next(
                        (
                            decoded
                            for display in displays
                            if (decoded := _decode(row.get(display)))
                        ),
                        "",
                    )
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
    media_files = _media_file_index(dataset.root.parent)
    voice_data = _voice_data(
        (path for path in databases if path.name.startswith("media_")), start_ms
    )
    conversations: dict[str, dict[str, Any]] = {}
    recognized = 0
    my_username = _clean_account_name(dataset.account)
    for path in clear.glob("message/message_[0-9]*.db"):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            table_sessions = _message_table_sessions(connection)
            for table in _tables(connection):
                columns = _columns(connection, table)
                selected = {name: _column(columns, name) for name in FIELD_ALIASES}
                if (
                    not selected["local_id"]
                    or not selected["create_time"]
                    or not _matching_columns(columns, "content")
                ):
                    continue
                if (
                    not selected["talker"]
                    and not selected["sender"]
                    and table not in table_sessions
                ):
                    continue
                recognized += 1
                for row in _message_rows(connection, table, columns, start_ms):
                    timestamp = int(row.get(selected["create_time"]) or 0)
                    timestamp_ms = (
                        timestamp if timestamp > 10_000_000_000 else timestamp * 1000
                    )
                    if timestamp_ms < start_ms:
                        continue
                    content = _row_content(row, columns)
                    talker = (
                        _decode(row.get(selected["talker"]))
                        if selected["talker"]
                        else ""
                    )
                    sender = (
                        _decode(row.get(selected["sender"]))
                        if selected["sender"]
                        else _decode(row.get("__sender_username"))
                    )
                    sender, content = _split_group_sender(sender, content)
                    if is_ignored_personal_wechat_notice(content):
                        continue
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
                    packed_info = (
                        row.get(selected["packed_info"])
                        if selected["packed_info"]
                        else None
                    )
                    if kind in {"image", "video", "file"} and not local_path:
                        local_path = _packed_media_path(
                            packed_info, media_files, kind
                        )
                    is_send = (
                        int(row.get(selected["is_send"]) or 0)
                        if selected["is_send"]
                        else int(sender == my_username)
                    )
                    session_id = talker or table_sessions.get(table, "") or sender
                    if not session_id:
                        continue
                    message_id = (
                        server_id or f"{path.stem}:{table}:{local_id}:{timestamp}"
                    )
                    transcript = _voice_transcript(packed_info) if kind == "voice" else ""
                    media = {"type": kind}
                    if local_path:
                        media["localPath"] = str(local_path)
                    if transcript:
                        media["transcript"] = transcript
                        media["extractionStatus"] = "completed"
                    elif kind == "voice" and (
                        raw_voice := voice_data.get((session_id, local_id))
                    ):
                        media["_voiceData"] = raw_voice
                    item = {
                        "messageId": message_id,
                        "timestamp": timestamp_ms // 1000,
                        "timestampMs": timestamp_ms,
                        "direction": "out" if is_send else "in",
                        "kind": kind,
                        "text": (
                            f"[语音转写] {transcript}"
                            if transcript
                            else _xml_text(content)
                            if kind in {"text", "link", "quote"}
                            else ""
                        ),
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
    shards = sorted(clear.glob("message/message_[0-9]*.db"))
    for path in shards:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            table_sessions = _message_table_sessions(connection)
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
                    or not (talker or sender or table in table_sessions)
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
    for path in sorted(clear.glob("message/message_[0-9]*.db")):
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            table_sessions = _message_table_sessions(connection)
            for table in _tables(connection):
                columns = _columns(connection, table)
                local_id = _column(columns, "local_id")
                server_id = _column(columns, "server_id")
                create_time = _column(columns, "create_time")
                kind = _column(columns, "kind")
                talker = _column(columns, "talker")
                sender = _column(columns, "sender")
                sort_seq = _column(columns, "sort_seq")
                if (
                    not local_id
                    or not create_time
                    or not (talker or sender or table in table_sessions)
                ):
                    continue
                recognized += 1
                selected = [local_id, create_time]
                selected.extend(
                    column
                    for column in (server_id, kind, talker, sender, sort_seq)
                    if column
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
                            "local_id": int(row.get(local_id) or 0),
                            "sort_seq": int(row.get(sort_seq) or 0) if sort_seq else 0,
                            "session_id": (
                                _decode(row.get(talker))
                                if talker
                                else table_sessions.get(table, "")
                                or _decode(row.get(sender))
                            ),
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


def _decode_wxgf(data: bytes) -> bytes | None:
    if not data.startswith(b"wxgf"):
        return data
    starts = [position for marker in (b"\x00\x00\x00\x01", b"\x00\x00\x01") if (position := data.find(marker)) >= 0]
    if not starts:
        return None
    completed = subprocess.run(
        [
            "/opt/homebrew/bin/ffmpeg",
            "-v",
            "error",
            "-f",
            "hevc",
            "-i",
            "pipe:0",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        input=data[min(starts) :],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _decrypt_image_dat(data: bytes, image_keys: tuple[int, bytes]) -> bytes | None:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad

    if len(data) < 15 or data[:6] not in _WECHAT_IMAGE_HEADERS:
        return None
    _, aes_size, xor_size = struct.unpack("<6sLLx", data[:15])
    encrypted_size = aes_size + AES.block_size - aes_size % AES.block_size
    if encrypted_size > len(data) - 15 or xor_size > len(data) - 15 - encrypted_size:
        return None
    rest = data[15:]
    try:
        prefix = unpad(
            AES.new(image_keys[1], AES.MODE_ECB).decrypt(rest[:encrypted_size]),
            AES.block_size,
        )
    except ValueError:
        return None
    middle_end = len(rest) - xor_size if xor_size else len(rest)
    middle = rest[encrypted_size:middle_end]
    tail = bytes(value ^ image_keys[0] for value in rest[middle_end:])
    return prefix + middle + tail


def _decode_dat(
    source: Path,
    destination: Path,
    image_keys: tuple[int, bytes] | None = None,
) -> bool:
    raw = source.read_bytes()
    if raw[:6] in _WECHAT_IMAGE_HEADERS:
        if not image_keys or not (raw := _decrypt_image_dat(raw, image_keys)):
            return False
        if raw.startswith(b"wxgf"):
            raw = _decode_wxgf(raw) or b""
    if any(raw.startswith(signature) for signature in _IMAGE_SIGNATURES):
        destination.write_bytes(raw)
        return True
    for signature in _IMAGE_SIGNATURES:
        key = raw[0] ^ signature[0] if raw else 0
        decoded = bytes(value ^ key for value in raw)
        if decoded.startswith(signature):
            destination.write_bytes(decoded)
            return True
    return False


def probe_image_keys(
    candidates: Iterable[Path], image_keys: tuple[int, bytes]
) -> bool:
    checked = 0
    with tempfile.TemporaryDirectory(prefix="finance-workbench-image-check-") as temporary:
        destination = Path(temporary) / "image.png"
        for source in candidates:
            try:
                if source.read_bytes()[:6] not in _WECHAT_IMAGE_HEADERS:
                    continue
                checked += 1
                if _decode_dat(source, destination, image_keys) and _image_decodes(
                    destination
                ):
                    return True
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
            if checked >= 30:
                break
    return False


def _image_decodes(path: Path) -> bool:
    completed = subprocess.run(
        [
            "/opt/homebrew/bin/ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.returncode == 0


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


def enrich_media(
    messages: list[dict[str, Any]],
    image_keys: tuple[int, bytes] | None = None,
) -> list[dict[str, Any]]:
    for message in messages:
        media = message.get("media") if isinstance(message.get("media"), dict) else {}
        kind = str(message.get("kind") or "")
        path_value = str(media.get("localPath") or "")
        inline_voice = media.pop("_voiceData", None)
        if kind == "voice" and media.get("extractionStatus") == "completed":
            message["media"] = media
            continue
        if not path_value and not inline_voice:
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
                        if not _decode_dat(path, image, image_keys):
                            media["extractionStatus"] = "failed"
                            continue
                    text = _ocr_image(str(image))
                if text:
                    media["ocrText"] = text
                    media["extractionStatus"] = "completed"
                    message["text"] = f"[图片识别] {text}"
                else:
                    media["extractionStatus"] = "completed"
            elif kind == "voice":
                if inline_voice:
                    with tempfile.NamedTemporaryFile(suffix=".silk") as voice:
                        voice.write(bytes(inline_voice))
                        voice.flush()
                        text = _transcribe_voice(Path(voice.name))
                else:
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


def _message_cursor(message: dict[str, Any]) -> tuple[int, int, int]:
    cursor = message.get("cursor") if isinstance(message.get("cursor"), dict) else {}
    return (
        int(cursor.get("sortSeq") or 0),
        int(cursor.get("createTime") or message.get("timestamp") or 0),
        int(cursor.get("localId") or message.get("messageId") or 0),
    )


def _retry_safe_windows(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(messages, key=_message_cursor)
    barrier = next(
        (
            message
            for message in ordered
            if (message.get("media") or {}).get("extractionStatus") == "failed"
        ),
        None,
    )
    if barrier is None:
        return group_messages(ordered)

    result: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_ms = 0
    for message in ordered:
        raw_timestamp = int(
            message.get("timestampMs") or message.get("timestamp") or 0
        )
        message_ms = (
            raw_timestamp if raw_timestamp > 10_000_000_000 else raw_timestamp * 1000
        )
        if current and (
            len(current) >= 200 or message_ms - previous_ms > 24 * 60 * 60 * 1000
        ):
            result.append(current)
            current = (
                [barrier]
                if _message_cursor(message) > _message_cursor(barrier)
                else []
            )
        if message not in current:
            current.append(message)
        previous_ms = message_ms
    if current:
        result.append(current)
    return result


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
    image_keys = load_image_keys(dataset.account)
    scanned = created = skipped = 0
    for session_id, conversation in conversations.items():
        current = existing.get(session_id)
        if current and current.get("listen_status") == "blocked":
            continue
        messages = enrich_media(conversation["messages"], image_keys)
        if current and current.get("create_time") and mode != "rescan":
            floor = int(current["create_time"]) * 1000 - 5 * 60 * 1000
            messages = [item for item in messages if int(item["timestampMs"]) >= floor]
        scanned += len(messages)
        for window in _retry_safe_windows(messages):
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
