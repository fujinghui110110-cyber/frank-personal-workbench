#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = "http://127.0.0.1:8000"
STATE_NOTE = Path(
    "/Users/frank/知识库/微信企业微信工作知识库/02_配置指南/"
    "Codex微信企微增量自动化状态.md"
)
OUTPUT_DIRS = {
    "personal_wechat": Path("/Users/frank/微信数据"),
    "wecom": Path("/Users/frank/企业微信导出数据/texts"),
}
SOURCE_NAMES = {"personal_wechat": "微信", "wecom": "企业微信"}
MESSAGE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\S+)\s+(.+?)：\s*(.*)$")
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
GENERATED_BY = "finance-workbench-listening-export"


def _watermarks() -> dict[tuple[str, str], datetime]:
    if not STATE_NOTE.exists():
        return {}
    match = re.search(r"```json\s*(\{.*?\})\s*```", STATE_NOTE.read_text(encoding="utf-8"), re.S)
    if not match:
        return {}
    try:
        state = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    result: dict[tuple[str, str], datetime] = {}
    for item in state.values():
        if not isinstance(item, dict):
            continue
        value = item.get("last_time")
        if not value:
            continue
        try:
            seen_at = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        key = (str(item.get("source") or ""), str(item.get("session_name") or ""))
        result[key] = max(result.get(key, seen_at), seen_at)
    return result


def _rows(base_url: str = BASE_URL) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        query = urllib.parse.urlencode({"limit": 500, "offset": offset})
        with urllib.request.urlopen(f"{base_url}/api/wechat/export?{query}", timeout=30) as response:
            page = json.load(response)
        rows.extend(page)
        if len(page) < 500:
            return rows
        offset += len(page)


def _messages(text: str) -> list[dict]:
    messages: list[dict] = []
    for line in text.splitlines():
        match = MESSAGE_RE.match(line.strip())
        if not match:
            continue
        try:
            moment = datetime.fromisoformat(match.group(1).replace("Z", "+00:00"))
        except ValueError:
            continue
        local = moment.astimezone(LOCAL_TZ)
        messages.append(
            {
                "formattedTime": local.strftime("%Y-%m-%d %H:%M:%S"),
                "createTime": int(moment.timestamp()),
                "senderDisplayName": match.group(2).strip(),
                "content": match.group(3).strip(),
            }
        )
    return messages


def _safe_name(value: str) -> str:
    clean = re.sub(r"[\\/:*?\"<>|\s]+", "_", value).strip("_") or "未命名会话"
    return clean[:80]


def export_listening_chats(base_url: str = BASE_URL) -> dict:
    watermarks = _watermarks()
    grouped: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in _rows(base_url):
        source = str(row["source"])
        name = str(row["display_name"])
        kind = "群聊" if row.get("conversation_kind") == "group" else "私聊"
        cutoff = watermarks.get((SOURCE_NAMES[source], name))
        for message in _messages(str(row.get("text_note") or "")):
            moment = datetime.strptime(message["formattedTime"], "%Y-%m-%d %H:%M:%S")
            if cutoff is None or moment > cutoff:
                grouped[(source, name, kind)].append(message)

    written = 0
    message_count = 0
    written_paths: set[Path] = set()
    for (source, name, kind), messages in grouped.items():
        unique = {
            (item["formattedTime"], item["senderDisplayName"], item["content"]): item
            for item in messages
        }
        ordered = sorted(unique.values(), key=lambda item: (item["createTime"], item["senderDisplayName"]))
        if not ordered:
            continue
        suffix = hashlib.sha256(f"{source}:{name}".encode()).hexdigest()[:10]
        path = OUTPUT_DIRS[source] / f"工作台_{kind}_{_safe_name(name)}_{suffix}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "generated_by": GENERATED_BY,
                    "source": SOURCE_NAMES[source],
                    "session": {"name": name, "type": kind},
                    "messages": ordered,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        path.chmod(0o600)
        written_paths.add(path)
        written += 1
        message_count += len(ordered)

    for directory in OUTPUT_DIRS.values():
        for path in directory.glob("工作台_*.json"):
            if path in written_paths:
                continue
            try:
                generated = json.loads(path.read_text(encoding="utf-8")).get("generated_by")
            except (OSError, json.JSONDecodeError, AttributeError):
                continue
            if generated == GENERATED_BY:
                path.unlink()

    return {
        "conversation_count": written,
        "message_count": message_count,
        "file_count": written,
        "output_paths": [str(path) for path in OUTPUT_DIRS.values()],
    }


def main() -> None:
    result = export_listening_chats()
    print(
        f"已导出监听中会话 {result['conversation_count']} 个，"
        f"新增消息 {result['message_count']} 条。"
    )


if __name__ == "__main__":
    main()
