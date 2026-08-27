from __future__ import annotations

import json
import re
import zipfile
from io import BytesIO
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


TEXT_CONTENT_TYPES = {
    "text/plain",
    "text/markdown",
    "application/json",
    "text/csv",
}
TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".json", ".csv", ".log"}
AMOUNT_PATTERN = re.compile(
    r"(?<!\d)(?:人民币|￥|¥)?\s*(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?\s*(?:万元|万|元)(?!\w)"
)
DATE_PATTERN = re.compile(
    r"(?<!\d)(?:20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?|\d{1,2}月\d{1,2}日)(?!\d)"
)
PERSON_PATTERN = re.compile(r"(?:负责人|经办人|申请人|审批人|联系人)\s*[：:]\s*([^，,；;\s]{2,12})")
ACKNOWLEDGEMENTS = {"好", "好的", "收到", "谢谢", "感谢", "可以", "对", "是", "加油"}


def decode_material(content: bytes, filename: str | None, content_type: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".docx", ".pptx", ".xlsx"}:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                if suffix == ".docx":
                    names = ["word/document.xml"]
                elif suffix == ".pptx":
                    names = sorted(
                        name
                        for name in archive.namelist()
                        if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                    )
                else:
                    names = [
                        name
                        for name in archive.namelist()
                        if name == "xl/sharedStrings.xml"
                        or re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
                    ]
                parts: list[str] = []
                for name in names:
                    if name not in archive.namelist():
                        continue
                    root = ElementTree.fromstring(archive.read(name))
                    text = " ".join(
                        value.strip()
                        for value in root.itertext()
                        if value and value.strip()
                    )
                    if text:
                        parts.append(text)
                return "\n".join(parts).strip()
        except (zipfile.BadZipFile, ElementTree.ParseError, KeyError):
            return ""
    if content_type not in TEXT_CONTENT_TYPES and suffix not in TEXT_SUFFIXES:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return ""


def clean_message_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    if not text or re.fullmatch(r"\[[^\]]+\](?:\s+https?://\S+)?", text):
        return ""
    return text


def chat_message_time(item: dict[str, Any]) -> str:
    formatted = clean_message_text(item.get("formattedTime"))
    if formatted:
        return formatted
    value = item.get("timestamp", item.get("createTime"))
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    seconds = value / 1000 if value > 10_000_000_000 else value
    try:
        return datetime.fromtimestamp(seconds).astimezone().strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def parse_chat_export(text: str, filename: str | None) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return None

    if isinstance(payload.get("weflow"), dict):
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        title = session.get("displayName") or session.get("nickname")
        allowed_types: tuple[Any, ...] = ("文本消息", "引用消息")
        sender_fields = ("senderDisplayName", "senderUsername")
    elif isinstance(payload.get("chatlab"), dict):
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        title = meta.get("name")
        allowed_types = (0, 25, "0", "25")
        sender_fields = ("accountName", "sender")
    else:
        return None

    messages: list[dict[str, Any]] = []
    for index, item in enumerate(payload["messages"], start=1):
        if not isinstance(item, dict) or item.get("type") not in allowed_types:
            continue
        content = clean_message_text(item.get("content"))
        if not content:
            continue
        sender = next(
            (clean_message_text(item.get(field)) for field in sender_fields if clean_message_text(item.get(field))),
            "",
        )
        messages.append(
            {
                "index": index,
                "sender": sender,
                "content": content,
                "time": chat_message_time(item),
            }
        )

    clean_title = clean_message_text(title) or Path(filename or "").stem
    return {"title": clean_title[:80] or "微信沟通记录", "messages": messages}


def chat_claims(
    messages: list[dict[str, Any]],
    pattern: re.Pattern[str] | None,
    field_type: str,
    keyword: str | None = None,
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages:
        content = message["content"]
        matches = list(pattern.finditer(content)) if pattern else ([None] if keyword in content else [])
        for match in matches:
            value = (
                match.group(1).strip()
                if match and match.lastindex
                else match.group(0).strip()
                if match
                else content[:200]
            )
            key = f"{field_type}:{value}"
            if key in seen:
                continue
            seen.add(key)
            sender = f"{message['sender']}：" if message["sender"] else ""
            claims.append(
                {
                    "field_type": field_type,
                    "value": value,
                    "source_locator": f"聊天消息 {message['index']}",
                    "quote": f"{sender}{content}"[:500],
                    "confidence": 1.0,
                }
            )
    return claims[:12 if pattern else 4]


def summarize_chat(messages: list[dict[str, Any]]) -> str:
    recent: list[str] = []
    for message in reversed(messages):
        compact = re.sub(r"[^\w\u4e00-\u9fff]+", "", message["content"])
        if len(compact) < 6 or compact in ACKNOWLEDGEMENTS:
            continue
        content = message["content"]
        snippet = content if len(content) <= 90 else f"{content[:87]}..."
        recent.append(f"{message['sender']}：{snippet}" if message["sender"] else snippet)
        if len(recent) == 3:
            break
    lead = f"已整理 {len(messages)} 条有效聊天消息"
    return f"{lead}。近期沟通：{'；'.join(reversed(recent))}" if recent else f"{lead}。"


def line_claims(text: str, pattern: re.Pattern[str], field_type: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        for match in pattern.finditer(line):
            value = match.group(1).strip() if match.lastindex else match.group(0).strip()
            key = f"{field_type}:{value}"
            if key in seen:
                continue
            seen.add(key)
            claims.append(
                {
                    "field_type": field_type,
                    "value": value,
                    "source_locator": f"第 {line_number} 行",
                    "quote": line[:500],
                    "confidence": 1.0,
                }
            )
    return claims[:12]


def keyword_claims(text: str, keyword: str, field_type: str) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if keyword not in line:
            continue
        claims.append(
            {
                "field_type": field_type,
                "value": line[:200],
                "source_locator": f"第 {line_number} 行",
                "quote": line[:500],
                "confidence": 1.0,
            }
        )
    return claims[:4]


def first_title(text: str, filename: str | None) -> str:
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if 4 <= len(heading) <= 80:
                return heading
    stem = Path(filename or "").stem.strip()
    if stem:
        return stem[:80]
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if 4 <= len(line) <= 80:
            return line
    return "待归并事项"


def concise_action_title(line: str, keywords: tuple[str, ...]) -> str:
    sentences = [part.strip() for part in re.split(r"[。；;！？!?]+", line) if part.strip()]
    candidate = next(
        (part for part in sentences if any(keyword in part for keyword in keywords)),
        line,
    )
    return candidate if len(candidate) <= 72 else f"{candidate[:69]}..."


def extract_actions(text: str) -> list[dict[str, Any]]:
    rules = (
        ("risk", ("风险", "异常", "超预算", "缺失", "逾期")),
        ("decision", ("待确认", "需要决策", "是否", "请确认")),
        ("waiting", ("等待", "待回复", "待反馈")),
        ("conclusion", ("结论", "同意", "确认通过")),
        ("task", ("需要", "请于", "完成", "跟进", "办理")),
    )
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for kind, keywords in rules:
            if not any(keyword in line for keyword in keywords):
                continue
            key = f"{kind}:{line}"
            if key in seen:
                break
            seen.add(key)
            due_match = DATE_PATTERN.search(line)
            due_date = None
            if due_match:
                value = due_match.group(0)
                normalized = re.sub(r"[/.年]", "-", value).replace("月", "-").replace("日", "")
                if normalized.count("-") == 2 and len(normalized.split("-")[0]) == 4:
                    parts = normalized.split("-")
                    due_date = f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
            actions.append(
                {
                    "kind": kind,
                    "title": concise_action_title(line, keywords),
                    "detail": "由本地规则从原文识别，提交人工确认。",
                    "owner": "财务负责人" if kind == "task" else "",
                    "due_date": due_date,
                    "schedule_basis": "material_explicit" if due_date else "legacy",
                }
            )
            break
        if len(actions) >= 10:
            break
    return actions


def analyze_material(material: dict[str, Any], content: bytes) -> dict[str, Any]:
    text = decode_material(content, material.get("filename"), material.get("content_type"))
    note = str(material.get("text_note") or "").strip()
    chat = parse_chat_export(text, material.get("filename"))
    messages = chat["messages"] if chat else []
    message_text = "\n".join(message["content"] for message in messages)
    combined = "\n".join(part for part in (note, message_text if chat else text) if part).strip()
    facts: list[dict[str, Any]] = []
    if note:
        facts.extend(line_claims(note, AMOUNT_PATTERN, "金额"))
        facts.extend(line_claims(note, DATE_PATTERN, "日期"))
        facts.extend(line_claims(note, PERSON_PATTERN, "人员"))
        facts.extend(keyword_claims(note, "制度", "制度依据"))
        facts.extend(keyword_claims(note, "审批", "审批状态"))
    if chat:
        facts.extend(chat_claims(messages, AMOUNT_PATTERN, "金额"))
        facts.extend(chat_claims(messages, DATE_PATTERN, "日期"))
        facts.extend(chat_claims(messages, PERSON_PATTERN, "人员"))
        facts.extend(chat_claims(messages, None, "制度依据", "制度"))
        facts.extend(chat_claims(messages, None, "审批状态", "审批"))
    elif text:
        facts.extend(line_claims(text, AMOUNT_PATTERN, "金额"))
        facts.extend(line_claims(text, DATE_PATTERN, "日期"))
        facts.extend(line_claims(text, PERSON_PATTERN, "人员"))
        facts.extend(keyword_claims(text, "制度", "制度依据"))
        facts.extend(keyword_claims(text, "审批", "审批状态"))
    source_label = {
        "wechat_markdown": "个人微信记录",
        "wecom_approval": "企业微信审批",
        "audio": "会议录音",
        "video": "会议视频",
        "image": "图片材料",
        "text": "文字记录",
    }.get(str(material.get("source_type")), "文件材料")
    inferences = [
        {
            "field_type": "材料类型",
            "value": source_label,
            "source_locator": "基于文件类型和投递入口",
            "quote": material.get("filename") or note[:120],
            "confidence": 0.9,
        }
    ]
    actions = extract_actions(combined)
    if not combined:
        actions.append(
            {
                "kind": "task",
                "title": f"使用 WorkBuddy 解析{source_label}",
                "detail": "材料已完整保留，需由本地执行节点完成转写、OCR 或文件解析。",
                "owner": "WorkBuddy",
                "due_date": None,
            }
        )
    return {
        "matter_title": chat["title"] if chat else first_title(combined, material.get("filename")),
        "summary": (
            summarize_chat(messages)
            if chat and messages
            else combined[:500]
            if combined
            else f"已收到{source_label}，等待本地深度处理。"
        ),
        "facts": facts,
        "inferences": inferences,
        "actions": actions,
    }
