from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .assignees import (
    normalize_suggestions,
    prefill_matter_contact,
    resolve_person_alias,
    upsert_action_suggestions,
)
from .db import Database, utc_now


_WORK_SIGNAL_RE = re.compile(
    r"付款|支付|合同|协议|发票|开票|报销|预算|收入|成本|费用|税务|纳税|资金|"
    r"账户|银行|工资|薪酬|供应商|采购|库存|盘点|经营|结算|对账|回款|欠款|"
    r"审计|审批|报表|凭证|记账|收款|现金|借款|还款|投标|报价|保险|社保|"
    r"公积金|月报|年报|GOP|NPI|风险|整改|验收|决算|税率|税金|印章|盖章|"
    r"招聘|面试|员工|人事|培训|排班|考勤",
    re.IGNORECASE,
)
_RELATION_ONLY_RE = re.compile(
    r"添加好友|已添加|通过好友|开始聊天|自我介绍|建立联系|新任.{0,8}(?:财务|会计|出纳|对接人)|"
    r"尚无具体工作|待后续沟通",
    re.IGNORECASE,
)
_MEDIA_PLACEHOLDER_RE = re.compile(
    r"(?:非文字消息|\[(?:附件|图片|语音|视频)[^\]]*\]|【(?:附件|图片|语音|视频)[^】]*】)",
    re.IGNORECASE,
)
_FOLLOW_UP_KINDS = {"task", "risk", "decision", "waiting"}
_CHAT_SOURCES = {"personal_wechat", "wecom"}
_WECHAT_STATUS_LABELS = {
    "held": "等待整理",
    "queued": "等待整理",
    "processing": "等待整理",
    "pending": "待确认",
    "accepted": "已纳入事项",
    "ignored": "已忽略",
    "retracted": "已撤回",
}
_TECHNICAL_SESSION_NAME_RE = re.compile(
    r"^(?:wxid_[a-z0-9_-]+|wecom:.+|\d+@chatroom)$",
    re.IGNORECASE,
)
_MATTER_BREAK_RE = re.compile(
    r"案件|项目|合同|协议|付款|支付|审批|报告|PPT|租赁|解约|主体|证据|发票|开票|报销|预算|对账|回款|采购|保险|整改|验收"
)
_GENERIC_MATTER_ANCHORS = {
    "工作",
    "事项",
    "业务",
    "财务",
    "公司",
    "集团",
    "球会",
    "酒店",
}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _public_candidate(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["evidence"] = _json(result.pop("evidence_json", "[]"), [])
    result["extracted"] = _json(result.pop("extracted_json", "{}"), {})
    result.pop("text_note", None)
    result["status_label"] = _WECHAT_STATUS_LABELS.get(
        str(result.get("status") or ""), "等待处理"
    )
    if result.get("material_status"):
        result["material_status_label"] = _WECHAT_STATUS_LABELS.get(
            str(result["material_status"]), "等待处理"
        )
    return result


def _iso_from_epoch(value: int | float | None) -> str:
    raw = float(value or 0)
    if raw > 10_000_000_000:
        raw /= 1000
    return datetime.fromtimestamp(raw, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cursor(message: dict[str, Any]) -> tuple[int, int, int]:
    cursor = message.get("cursor") if isinstance(message.get("cursor"), dict) else {}
    return (
        int(cursor.get("sortSeq") or 0),
        int(cursor.get("createTime") or message.get("timestamp") or 0),
        int(cursor.get("localId") or message.get("messageId") or 0),
    )


def review_worthy_wechat_result(result: dict[str, Any], source_text: str = "") -> bool:
    classification = str(result.get("classification") or "").strip().lower()
    if classification != "relevant":
        return False
    extracted = result.get("extracted") if isinstance(result.get("extracted"), dict) else {}
    message_text = "\n".join(
        line
        for line in source_text.splitlines()
        if not line.startswith(("微信会话：", "时间："))
    )
    visible_text = _MEDIA_PLACEHOLDER_RE.sub("", message_text)
    try:
        confidence = float(result.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    actions = extracted.get("actions") if isinstance(extracted.get("actions"), list) else []
    has_follow_up = any(
        isinstance(item, dict)
        and str(item.get("kind") or "").strip().lower() in _FOLLOW_UP_KINDS
        and str(item.get("title") or "").strip()
        for item in actions
    )
    has_approval = bool(str(extracted.get("approval") or "").strip())
    has_risk = bool(extracted.get("risks"))
    has_concrete_work = bool(_WORK_SIGNAL_RE.search(visible_text))
    if _RELATION_ONLY_RE.search(visible_text) and not has_concrete_work:
        return False
    return bool(
        confidence >= 0.85
        and has_concrete_work
        and (has_follow_up or has_approval or has_risk)
    )


def _unique(items: list[Any], limit: int = 100) -> list[Any]:
    result: list[Any] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, dict) else str(item)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _sender_label(message: dict[str, Any]) -> str:
    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    if message.get("direction") == "out" or sender.get("isSelf") is True:
        return "我"
    return str(
        sender.get("displayName")
        or sender.get("name")
        or sender.get("username")
        or "对方"
    ).strip()


def _sender_aliases(messages: list[dict[str, Any]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for message in messages:
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        username = str(sender.get("username") or "").strip()
        display_name = str(sender.get("displayName") or sender.get("name") or "").strip()
        if (
            username
            and display_name
            and username != display_name
            and (username.startswith("wxid_") or (len(username) >= 6 and username.isascii()))
        ):
            aliases[username] = display_name
    return aliases


def _record_wecom_sender_identities(connection: Any, messages: list[dict[str, Any]], now: str) -> None:
    for message in messages:
        sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
        stable_id = str(sender.get("username") or "").strip()
        display_name = str(sender.get("displayName") or sender.get("name") or "").strip()
        if not stable_id:
            continue
        match = (
            {"person_id": "person_self"}
            if sender.get("isSelf")
            else resolve_person_alias(display_name)
        )
        if not match:
            continue
        connection.execute(
            "INSERT INTO person_identities "
            "(id, person_id, source, stable_id, display_name, created_at, updated_at) "
            "VALUES (?, ?, 'wecom', ?, ?, ?, ?) "
            "ON CONFLICT(source, stable_id) DO UPDATE SET "
            "person_id = excluded.person_id, display_name = excluded.display_name, "
            "updated_at = excluded.updated_at",
            (
                f"identity_wecom_{stable_id}",
                match["person_id"],
                stable_id,
                display_name[:160],
                now,
                now,
            ),
        )


def _record_personal_conversation_identity(
    connection: Any,
    session_id: str,
    display_name: str,
    kind: str,
    now: str,
) -> None:
    if kind == "group":
        return
    match = resolve_person_alias(display_name)
    if not match:
        return
    connection.execute(
        "INSERT INTO person_identities "
        "(id, person_id, source, stable_id, display_name, created_at, updated_at) "
        "VALUES (?, ?, 'personal_wechat', ?, ?, ?, ?) "
        "ON CONFLICT(source, stable_id) DO UPDATE SET "
        "person_id = excluded.person_id, display_name = excluded.display_name, "
        "updated_at = excluded.updated_at",
        (
            f"identity_personal_wechat_{session_id}",
            match["person_id"],
            session_id,
            display_name[:160],
            now,
            now,
        ),
    )


def _identity_names(connection: Any, source: str) -> dict[str, str]:
    return {
        row["stable_id"]: row["display_name"]
        for row in connection.execute(
            "SELECT pi.stable_id, p.display_name FROM person_identities pi "
            "JOIN people p ON p.id = pi.person_id WHERE pi.source = ?",
            (source,),
        ).fetchall()
    }


def _conversation_display_name(session_id: str, display_name: str, kind: str) -> str:
    name = display_name.strip()
    if not name or name == session_id or _TECHNICAL_SESSION_NAME_RE.fullmatch(name):
        return "未命名群聊" if kind == "group" or session_id.endswith("@chatroom") else "未命名联系人"
    return name


def _replace_aliases(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, str):
        for username, display_name in aliases.items():
            value = value.replace(username, display_name)
        return value
    if isinstance(value, list):
        return [_replace_aliases(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: _replace_aliases(item, aliases) for key, item in value.items()}
    return value


def _apply_sender_aliases(
    connection: Any, source: str, aliases: dict[str, str], now: str
) -> None:
    if not aliases:
        return
    rows = connection.execute(
        "SELECT x.id, x.material_id, x.summary, x.uncertainty_reason, "
        "x.evidence_json, x.extracted_json, m.text_note "
        "FROM wechat_candidates x JOIN materials m ON m.id = x.material_id "
        "WHERE x.source = ?",
        (source,),
    ).fetchall()
    for raw_row in rows:
        row = dict(raw_row)
        text_note = _replace_aliases(row["text_note"], aliases)
        summary = _replace_aliases(row["summary"], aliases)
        uncertainty = _replace_aliases(row["uncertainty_reason"], aliases)
        evidence = _replace_aliases(_json(row["evidence_json"], []), aliases)
        extracted = _replace_aliases(_json(row["extracted_json"], {}), aliases)
        connection.execute(
            "UPDATE materials SET text_note = ?, updated_at = ? WHERE id = ?",
            (text_note, now, row["material_id"]),
        )
        connection.execute(
            "UPDATE wechat_candidates SET summary = ?, uncertainty_reason = ?, "
            "evidence_json = ?, extracted_json = ?, updated_at = ? WHERE id = ?",
            (
                summary,
                uncertainty,
                json.dumps(evidence, ensure_ascii=False),
                json.dumps(extracted, ensure_ascii=False),
                now,
                row["id"],
            ),
        )
    for username, display_name in aliases.items():
        connection.execute(
            "UPDATE wechat_conversations SET display_name = ?, updated_at = ? "
            "WHERE source = ? AND display_name = ?",
            (display_name, now, source, username),
        )


def _matter_anchor(candidate: dict[str, Any]) -> str:
    extracted = _json(candidate.get("extracted_json"), {})
    value = str(extracted.get("matter_title") or candidate.get("summary") or "")
    value = value.replace("牧阳人", "牧羊人")
    value = re.sub(r"[\s·，。；：、/\\()（）《》\[\]【】_-]+", "", value)
    value = re.sub(r"^(?:更新|核实|查找|推进|整理|制作|补充|处理|跟进)+", "", value)
    prefix = _MATTER_BREAK_RE.split(value, maxsplit=1)[0]
    if len(prefix) < 3 or prefix in _GENERIC_MATTER_ANCHORS:
        return ""
    return prefix[:40]


def _same_matter(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _same_matter_topic(
        left,
        right,
        same_conversation=left.get("session_id") == right.get("session_id"),
    )


def _topic_text(candidate: dict[str, Any]) -> str:
    extracted = _json(candidate.get("extracted_json"), {})
    text = "".join(
        str(value or "")
        for value in (
            extracted.get("matter_title"),
            candidate.get("summary"),
        )
    )
    text = text.replace("牧阳人", "牧羊人").lower()
    return re.sub(r"[\s·，。；：、/\\()（）《》\[\]【】_\-]+", "", text)


def _topic_bigrams(value: str) -> set[str]:
    return {value[index : index + 2] for index in range(max(len(value) - 1, 0))}


def _same_matter_topic(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    same_conversation: bool = False,
) -> bool:
    left_title = re.sub(
        r"[\s·，。；：、/\\()（）《》\[\]【】_\-]+",
        "",
        str(_json(left.get("extracted_json"), {}).get("matter_title") or ""),
    ).replace("牧阳人", "牧羊人")
    right_title = re.sub(
        r"[\s·，。；：、/\\()（）《》\[\]【】_\-]+",
        "",
        str(_json(right.get("extracted_json"), {}).get("matter_title") or ""),
    ).replace("牧阳人", "牧羊人")
    if len(left_title) >= 3 and left_title == right_title:
        return True
    left_anchor = _matter_anchor(left)
    right_anchor = _matter_anchor(right)
    if left_anchor and right_anchor:
        shorter, longer = sorted((left_anchor, right_anchor), key=len)
        if shorter == longer or (len(shorter) >= 3 and longer.startswith(shorter)):
            return True

    left_text = _topic_text(left)
    right_text = _topic_text(right)
    if len(left_text) < 4 or len(right_text) < 4:
        return False
    left_pairs = _topic_bigrams(left_text)
    right_pairs = _topic_bigrams(right_text)
    if not left_pairs or not right_pairs:
        return False
    similarity = 2 * len(left_pairs & right_pairs) / (len(left_pairs) + len(right_pairs))
    return similarity >= (0.42 if same_conversation else 0.58)


def _merged_extracted(target: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    left = _json(target.get("extracted_json"), {})
    right = _json(source.get("extracted_json"), {})
    merged = {**right, **left}
    for key in ("amounts", "dates", "people", "risks"):
        merged[key] = _unique([*(left.get(key) or []), *(right.get(key) or [])], 6)
    merged["actions"] = _unique(
        [*(left.get("actions") or []), *(right.get("actions") or [])], 40
    )
    merged["approval"] = "；".join(
        _unique([left.get("approval") or "", right.get("approval") or ""], 4)
    )[:300]
    merged["source_material_ids"] = _unique(
        [
            target["material_id"],
            *(left.get("source_material_ids") or []),
            source["material_id"],
            *(right.get("source_material_ids") or []),
        ],
        100,
    )
    return merged


def _compact_extracted(extracted: dict[str, Any]) -> dict[str, Any]:
    result = dict(extracted)
    for key in ("amounts", "dates", "people", "risks"):
        result[key] = _unique(list(result.get(key) or []), 6)
    result["actions"] = _unique(list(result.get("actions") or []), 40)
    result["approval"] = str(result.get("approval") or "")[:300]
    result["source_material_ids"] = _unique(
        list(result.get("source_material_ids") or []), 100
    )
    return result


def _merged_summary(*values: str) -> str:
    parts = _unique(
        [part.strip() for value in values for part in str(value or "").split("；")],
        20,
    )
    selected: list[str] = []
    used = 0
    for part in parts:
        extra = len(part) + (1 if selected else 0)
        if selected and used + extra > 240:
            break
        selected.append(part)
        used += extra
    remaining = len(parts) - len(selected)
    suffix = f"；另有 {remaining} 条相关线索" if remaining else ""
    return ("；".join(selected) + suffix)[:280]


def _merge_pending_rows(connection: Any, target: dict[str, Any], source: dict[str, Any], now: str) -> None:
    classification = (
        "relevant"
        if "relevant" in {target.get("classification"), source.get("classification")}
        else "uncertain"
    )
    reasons = _unique(
        [target.get("uncertainty_reason") or "", source.get("uncertainty_reason") or ""],
        4,
    )
    evidence = _unique(
        [
            *_json(target.get("evidence_json"), []),
            *_json(source.get("evidence_json"), []),
        ],
        20,
    )
    confidences = [
        float(value)
        for value in (target.get("confidence"), source.get("confidence"))
        if value is not None
    ]
    connection.execute(
        "UPDATE wechat_candidates SET window_start = ?, window_end = ?, classification = ?, "
        "summary = ?, uncertainty_reason = ?, confidence = ?, evidence_json = ?, "
        "extracted_json = ?, updated_at = ? WHERE id = ?",
        (
            min(target["window_start"], source["window_start"]),
            max(target["window_end"], source["window_end"]),
            classification,
            _merged_summary(target.get("summary") or "", source.get("summary") or ""),
            "" if classification == "relevant" else "；".join(reasons)[:500],
            max(confidences) if confidences else None,
            json.dumps(evidence, ensure_ascii=False),
            json.dumps(_merged_extracted(target, source), ensure_ascii=False),
            now,
            target["id"],
        ),
    )
    connection.execute(
        "UPDATE wechat_candidates SET status = 'retracted', resolved_at = ?, updated_at = ? "
        "WHERE id = ?",
        (now, now, source["id"]),
    )
    connection.execute(
        "UPDATE materials SET status = 'processed', updated_at = ? WHERE id = ?",
        (now, source["material_id"]),
    )


class WechatService:
    def __init__(self, database: Database):
        self.database = database

    def consolidate_pending_candidates(self) -> dict[str, int]:
        now = utc_now()
        released = self.database.fetch_one(
            "SELECT created_at FROM audit_events WHERE action = 'analysis.released' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        if not released:
            return {"ignored": 0, "merged": 0, "continued": 0}
        released_at = str(released["created_at"])
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        ignored = 0
        merged = 0
        keeper_ids: list[str] = []
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT x.*, m.text_note FROM wechat_candidates x "
                "JOIN materials m ON m.id = x.material_id "
                "WHERE x.status = 'pending' AND x.updated_at >= ? "
                "ORDER BY x.session_id, x.created_at, x.id",
                (released_at,),
            ).fetchall()
            keepers: list[dict[str, Any]] = []
            for raw_row in rows:
                row = dict(raw_row)
                result = {
                    "classification": row.get("classification"),
                    "summary": row.get("summary"),
                    "uncertainty_reason": row.get("uncertainty_reason"),
                    "confidence": row.get("confidence"),
                    "evidence": _json(row.get("evidence_json"), []),
                    "extracted": _json(row.get("extracted_json"), {}),
                }
                if row["window_end"] < cutoff or not review_worthy_wechat_result(
                    result, str(row.get("text_note") or "")
                ):
                    connection.execute(
                        "UPDATE wechat_candidates SET classification = 'irrelevant', "
                        "status = 'ignored', uncertainty_reason = '', resolved_at = ?, "
                        "updated_at = ? WHERE id = ?",
                        (now, now, row["id"]),
                    )
                    connection.execute(
                        "UPDATE materials SET status = 'processed', updated_at = ? WHERE id = ?",
                        (now, row["material_id"]),
                    )
                    ignored += 1
                    continue
                compact_summary = _merged_summary(row.get("summary") or "")
                compact_extracted = _compact_extracted(result["extracted"])
                connection.execute(
                    "UPDATE wechat_candidates SET summary = ?, extracted_json = ?, updated_at = ? "
                    "WHERE id = ?",
                    (
                        compact_summary,
                        json.dumps(compact_extracted, ensure_ascii=False),
                        now,
                        row["id"],
                    ),
                )
                row["summary"] = compact_summary
                row["extracted_json"] = json.dumps(compact_extracted, ensure_ascii=False)
                keeper = next(
                    (candidate for candidate in keepers if _same_matter(candidate, row)),
                    None,
                )
                if keeper:
                    _merge_pending_rows(connection, keeper, row, now)
                    updated = connection.execute(
                        "SELECT * FROM wechat_candidates WHERE id = ?", (keeper["id"],)
                    ).fetchone()
                    keepers[keepers.index(keeper)] = dict(updated)
                    merged += 1
                else:
                    keepers.append(row)
            keeper_ids = [str(row["id"]) for row in keepers]
        continued = 0
        for candidate_id in keeper_ids:
            candidate = self.database.fetch_one(
                "SELECT * FROM wechat_candidates WHERE id = ? AND status = 'pending'",
                (candidate_id,),
            )
            if candidate and self._auto_accept_actionable_candidate(candidate, "jarvis-batch"):
                continued += 1
        return {"ignored": ignored, "merged": merged, "continued": continued}

    def request_sync(
        self,
        actor: str,
        mode: str = "incremental",
        sources: list[str] | None = None,
    ) -> dict[str, Any]:
        requested_sources = list(dict.fromkeys(sources or ["personal_wechat", "wecom"]))
        if not requested_sources or any(source not in _CHAT_SOURCES for source in requested_sources):
            raise ValueError("不支持的聊天来源")
        now = utc_now()
        requests: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            for source in requested_sources:
                existing = connection.execute(
                    "SELECT * FROM wechat_sync_requests WHERE source = ? "
                    "AND status IN ('pending', 'running') ORDER BY requested_at DESC LIMIT 1",
                    (source,),
                ).fetchone()
                if existing:
                    requests.append(dict(existing))
                    continue
                request_id = _id("wsync")
                connection.execute(
                    "INSERT INTO wechat_sync_requests "
                    "(id, source, status, mode, requested_by, requested_at) "
                    "VALUES (?, ?, 'pending', ?, ?, ?)",
                    (request_id, source, mode, actor, now),
                )
                created = connection.execute(
                    "SELECT * FROM wechat_sync_requests WHERE id = ?", (request_id,)
                ).fetchone()
                requests.append(dict(created))
        return {"requests": requests}

    def claim_sync(self, worker_id: str) -> dict[str, Any] | None:
        now = utc_now()
        stale_before = (datetime.now(UTC) - timedelta(minutes=15)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE wechat_sync_requests SET status = 'pending', worker_id = NULL, "
                "started_at = NULL, error = '上次检查中断，已自动续跑' "
                "WHERE status = 'running' AND started_at < ?",
                (stale_before,),
            )
            row = connection.execute(
                "SELECT * FROM wechat_sync_requests WHERE status = 'pending' "
                "ORDER BY requested_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                "UPDATE wechat_sync_requests SET status = 'running', worker_id = ?, "
                "started_at = ?, error = NULL WHERE id = ? AND status = 'pending'",
                (worker_id, now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
        return self.database.fetch_one(
            "SELECT * FROM wechat_sync_requests WHERE id = ?", (row["id"],)
        )

    def finish_sync(
        self,
        request_id: str,
        worker_id: str,
        status: str,
        error: str = "",
        message_count: int = 0,
        window_count: int = 0,
        skipped_count: int = 0,
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "unsupported"}:
            raise ValueError("不支持的微信检查状态")
        now = utc_now()
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE wechat_sync_requests SET status = ?, completed_at = ?, error = ?, "
                "message_count = ?, window_count = ?, skipped_count = ? "
                "WHERE id = ? AND worker_id = ? AND status = 'running'",
                (
                    status,
                    now,
                    error[:1000] or None,
                    message_count,
                    window_count,
                    skipped_count,
                    request_id,
                    worker_id,
                ),
            )
            if updated.rowcount != 1:
                raise PermissionError("微信检查任务已失效")
        return self.database.fetch_one(
            "SELECT * FROM wechat_sync_requests WHERE id = ?", (request_id,)
        ) or {}

    def report_export(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        last_success_at = now if payload["status"] == "completed" else None
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO wechat_export_state "
                "(id, status, conversation_count, message_count, file_count, "
                "output_paths_json, last_success_at, error, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
                "conversation_count = excluded.conversation_count, "
                "message_count = excluded.message_count, file_count = excluded.file_count, "
                "output_paths_json = excluded.output_paths_json, "
                "last_success_at = COALESCE(excluded.last_success_at, wechat_export_state.last_success_at), "
                "error = excluded.error, updated_at = excluded.updated_at",
                (
                    payload["status"],
                    payload.get("conversation_count", 0),
                    payload.get("message_count", 0),
                    payload.get("file_count", 0),
                    json.dumps(payload.get("output_paths") or [], ensure_ascii=False),
                    last_success_at,
                    payload.get("error", "")[:1000],
                    now,
                ),
            )
        return self.export_status()

    def export_status(self) -> dict[str, Any]:
        state = self.database.fetch_one(
            "SELECT * FROM wechat_export_state WHERE id = 1"
        ) or {
            "status": "idle",
            "conversation_count": 0,
            "message_count": 0,
            "file_count": 0,
            "output_paths_json": "[]",
            "last_success_at": None,
            "error": "",
            "updated_at": None,
        }
        state["output_paths"] = _json(state.pop("output_paths_json", "[]"), [])
        state.pop("id", None)
        return state

    def status(self) -> dict[str, Any]:
        latest = self.database.fetch_one(
            "SELECT * FROM wechat_sync_requests ORDER BY requested_at DESC LIMIT 1"
        )
        counts = self.database.fetch_one(
            "SELECT "
            "(SELECT COUNT(*) FROM wechat_candidates WHERE status = 'pending') AS pending, "
            "(SELECT COUNT(*) FROM wechat_candidates WHERE status = 'processing') AS processing, "
            "(SELECT COUNT(*) FROM wechat_candidates WHERE status = 'ignored' "
            "AND created_at >= datetime('now', '-7 day')) AS ignored_recent, "
            "(SELECT COUNT(*) FROM wechat_candidates WHERE status = 'accepted') AS accepted, "
            "(SELECT COUNT(*) FROM wechat_conversations WHERE listen_status = 'active') AS listening, "
            "(SELECT COUNT(*) FROM wechat_conversations WHERE listen_status = 'blocked') AS blocked"
        ) or {}
        last_success = self.database.fetch_one(
            "SELECT completed_at FROM wechat_sync_requests WHERE status = 'completed' "
            "ORDER BY completed_at DESC LIMIT 1"
        )
        sources: dict[str, dict[str, Any]] = {}
        for source in ("personal_wechat", "wecom"):
            source_latest = self.database.fetch_one(
                "SELECT * FROM wechat_sync_requests WHERE source = ? "
                "ORDER BY requested_at DESC LIMIT 1",
                (source,),
            )
            source_success = self.database.fetch_one(
                "SELECT completed_at FROM wechat_sync_requests "
                "WHERE source = ? AND status = 'completed' ORDER BY completed_at DESC LIMIT 1",
                (source,),
            )
            source_counts = self.database.fetch_one(
                "SELECT "
                "(SELECT COUNT(*) FROM wechat_candidates WHERE source = ? AND status = 'pending') AS pending, "
                "(SELECT COUNT(*) FROM wechat_candidates WHERE source = ? AND status = 'processing') AS processing, "
                "(SELECT COUNT(*) FROM wechat_candidates WHERE source = ? AND status = 'ignored' "
                "AND created_at >= datetime('now', '-7 day')) AS ignored_recent, "
                "(SELECT COUNT(*) FROM wechat_candidates WHERE source = ? AND status = 'accepted') AS accepted, "
                "(SELECT COUNT(*) FROM wechat_conversations WHERE source = ? AND listen_status = 'active') AS listening",
                (source, source, source, source, source),
            ) or {}
            available = source == "personal_wechat" or bool(
                source_latest and source_latest.get("status") == "completed"
            )
            sources[source] = {
                "available": available,
                "latest": source_latest,
                "last_success_at": source_success["completed_at"] if source_success else None,
                "counts": source_counts,
                "message": (
                    "可读取本机个人微信新增消息"
                    if source == "personal_wechat"
                    else (
                        "可读取本机企业微信新增消息"
                        if available
                        else "本机企业微信读取尚未完成"
                    )
                ),
            }
        return {
            "counts": counts,
            "latest": latest,
            "last_success_at": last_success["completed_at"] if last_success else None,
            "sources": sources,
            "export": self.export_status(),
        }

    def list_conversations(
        self, query: str = "", source: str | None = None
    ) -> list[dict[str, Any]]:
        if source and source not in _CHAT_SOURCES:
            raise ValueError("不支持的聊天来源")
        clauses: list[str] = []
        params: list[Any] = []
        if query.strip():
            clauses.append("c.display_name LIKE ?")
            params.append(f"%{query.strip()}%")
        if source:
            clauses.append("c.source = ?")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.database.fetch_all(
            "SELECT c.*, s.sort_seq, s.create_time, s.local_id, s.last_success_at, s.error, "
            "(SELECT COUNT(*) FROM wechat_candidates x WHERE x.session_id = c.session_id "
            "AND x.status = 'pending') AS pending_count "
            "FROM wechat_conversations c LEFT JOIN wechat_sync_state s "
            "ON s.session_id = c.session_id "
            f"{where} ORDER BY CASE WHEN c.listen_status = 'blocked' THEN 1 ELSE 0 END, "
            "c.last_message_at DESC, c.display_name",
            tuple(params),
        )
        for row in rows:
            row["display_name"] = _conversation_display_name(
                str(row.get("session_id") or ""),
                str(row.get("display_name") or ""),
                str(row.get("kind") or ""),
            )
        return rows

    def block_conversation(self, session_id: str, actor: str) -> dict[str, Any]:
        now = utc_now()
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE wechat_conversations SET listen_status = 'blocked', "
                "blocked_at = COALESCE(blocked_at, ?), "
                "updated_at = ? WHERE session_id = ?",
                (now, now, session_id),
            )
            if updated.rowcount != 1:
                raise KeyError("没有找到这个微信会话")
            connection.execute(
                "UPDATE materials SET status = 'processed', updated_at = ? WHERE id IN ("
                "SELECT material_id FROM wechat_candidates WHERE session_id = ? "
                "AND status IN ('pending', 'processing'))",
                (now, session_id),
            )
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE material_id IN ("
                "SELECT material_id FROM wechat_candidates WHERE session_id = ? "
                "AND status IN ('pending', 'processing')) "
                "AND status NOT IN ('succeeded', 'cancelled')",
                (now, session_id),
            )
            connection.execute(
                "UPDATE wechat_candidates SET status = 'ignored', resolved_at = ?, "
                "updated_at = ? WHERE session_id = ? AND status IN ('pending', 'processing')",
                (now, now, session_id),
            )
        result = self.database.fetch_one(
            "SELECT * FROM wechat_conversations WHERE session_id = ?", (session_id,)
        ) or {}
        self.database.audit(
            _id("audit"), actor, "wechat.conversation.blocked",
            "wechat_conversation", session_id,
        )
        return result

    def unblock_conversation(
        self, session_id: str, actor: str, rescan_days: int | None = None
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        listen_from = now - timedelta(days=rescan_days) if rescan_days else now
        listen_from_text = listen_from.isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE wechat_conversations SET listen_status = 'active', blocked_at = NULL, "
                "listen_from = ?, updated_at = ? WHERE session_id = ?",
                (listen_from_text, utc_now(), session_id),
            )
            if updated.rowcount != 1:
                raise KeyError("没有找到这个微信会话")
            if rescan_days:
                connection.execute(
                    "DELETE FROM wechat_sync_state WHERE session_id = ?", (session_id,)
                )
                connection.execute(
                    "DELETE FROM wechat_seen_messages WHERE session_id = ?", (session_id,)
                )
        result = self.database.fetch_one(
            "SELECT * FROM wechat_conversations WHERE session_id = ?", (session_id,)
        ) or {}
        self.request_sync(
            actor,
            "rescan" if rescan_days else "incremental",
            [str(result.get("source") or "personal_wechat")],
        )
        self.database.audit(
            _id("audit"), actor, "wechat.conversation.unblocked",
            "wechat_conversation", session_id,
            metadata={"rescan_days": rescan_days},
        )
        return result

    def ingest_window(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = sorted(payload["messages"], key=_cursor)
        now = utc_now()
        source = str(payload.get("source") or "personal_wechat")
        if source not in _CHAT_SOURCES:
            raise ValueError("不支持的聊天来源")
        session_id = str(payload["session_id"])
        if source == "wecom" and not session_id.startswith("wecom:"):
            session_id = f"wecom:{session_id}"
        display_name = _conversation_display_name(
            session_id,
            str(payload.get("display_name") or ""),
            str(payload.get("kind") or ""),
        )
        latest_at = _iso_from_epoch(messages[-1].get("timestampMs") or messages[-1].get("timestamp"))
        aliases = _sender_aliases(messages)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO wechat_conversations "
                "(session_id, source, display_name, kind, last_message_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                "source = excluded.source, "
                "display_name = excluded.display_name, kind = excluded.kind, "
                "last_message_at = MAX(COALESCE(wechat_conversations.last_message_at, ''), "
                "excluded.last_message_at), updated_at = excluded.updated_at",
                (
                    session_id,
                    source,
                    display_name[:160],
                    payload["kind"],
                    latest_at,
                    now,
                    now,
                ),
            )
            conversation = connection.execute(
                "SELECT * FROM wechat_conversations WHERE session_id = ?", (session_id,)
            ).fetchone()
            if conversation["listen_status"] == "blocked":
                return {"created": False, "reason": "blocked"}
            _apply_sender_aliases(connection, source, aliases, now)
            if source == "wecom":
                _record_wecom_sender_identities(connection, messages, now)
            else:
                _record_personal_conversation_identity(
                    connection,
                    session_id,
                    display_name,
                    str(payload.get("kind") or ""),
                    now,
                )
            identity_names = _identity_names(connection, source)
            listen_from = conversation["listen_from"]
            fresh: list[dict[str, Any]] = []
            fresh_keys: set[tuple[int, int, int]] = set()
            for message in messages:
                if listen_from and _iso_from_epoch(
                    message.get("timestampMs") or message.get("timestamp")
                ) < listen_from:
                    continue
                cursor = _cursor(message)
                if cursor in fresh_keys:
                    continue
                exists = connection.execute(
                    "SELECT 1 FROM wechat_seen_messages WHERE source = ? AND session_id = ? AND sort_seq = ? "
                    "AND create_time = ? AND local_id = ?",
                    (source, session_id, *cursor),
                ).fetchone()
                if not exists:
                    fresh.append(message)
                    fresh_keys.add(cursor)
            if not fresh:
                self._update_cursor(
                    connection,
                    payload["account_fingerprint"],
                    session_id,
                    messages[-1],
                    source,
                )
                return {"created": False, "reason": "duplicate"}

            first_cursor = _cursor(fresh[0])
            last_cursor = _cursor(fresh[-1])
            material_id = _id("mat")
            job_id = _id("job")
            candidate_id = _id("wcand")
            first_at = _iso_from_epoch(fresh[0].get("timestampMs") or fresh[0].get("timestamp"))
            last_at = _iso_from_epoch(fresh[-1].get("timestampMs") or fresh[-1].get("timestamp"))
            digest = hashlib.sha256(
                f"{session_id}:".encode("utf-8")
                + json.dumps(fresh, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
            channel_label = "企业微信" if source == "wecom" else "个人微信"
            lines = [f"{channel_label}会话：{display_name}", f"时间：{first_at} 至 {last_at}"]
            for message in fresh:
                when = _iso_from_epoch(message.get("timestampMs") or message.get("timestamp"))
                sender = _sender_label(message)
                sender_data = (
                    message.get("sender")
                    if isinstance(message.get("sender"), dict)
                    else {}
                )
                if message.get("direction") != "out" and not sender_data.get("isSelf"):
                    stable_sender_id = (
                        str(sender_data.get("username") or "").strip()
                        if source == "wecom"
                        else session_id
                    )
                    sender = identity_names.get(stable_sender_id, sender)
                text = str(message.get("text") or "").strip()
                media = message.get("media") if isinstance(message.get("media"), dict) else {}
                media_note = ""
                if media:
                    media_note = f" [附件：{media.get('fileName') or media.get('type') or message.get('kind')}]"
                lines.append(f"{when} {sender}：{text or '非文字消息'}{media_note}")
            metadata = {
                "channel": "wecom" if source == "wecom" else "wechat",
                "source": source,
                "session_id": session_id,
                "conversation_name": display_name,
                "conversation_kind": payload["kind"],
                "message_count": len(fresh),
                "first_cursor": first_cursor,
                "last_cursor": last_cursor,
                "media_paths": [
                    message["media"].get("localPath")
                    for message in fresh
                    if isinstance(message.get("media"), dict)
                    and message["media"].get("localPath")
                ],
            }
            connection.execute(
                "INSERT INTO materials "
                "(id, idempotency_key, sha256, source_type, filename, content_type, size, "
                "text_note, status, received_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, 'text/plain', 0, ?, 'queued', ?, ?, ?)",
                (
                    material_id,
                    f"{source}:{session_id}:{first_cursor}:{last_cursor}",
                    digest,
                    "wecom_auto" if source == "wecom" else "wechat_auto",
                    f"{channel_label}线索-{display_name[:80]}.txt",
                    "\n".join(lines),
                    now,
                    now,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                ),
            )
            connection.execute(
                "INSERT INTO jobs "
                "(id, material_id, job_type, status, requires_local, priority, created_at, updated_at) "
                    "VALUES (?, ?, 'wechat_classify', 'held', 1, 90, ?, ?)",
                (job_id, material_id, now, now),
            )
            connection.execute(
                "INSERT INTO wechat_candidates "
                "(id, material_id, session_id, source, window_start, window_end, summary, status, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, '等待 贾维斯 理解', "
                "'processing', ?, ?)",
                (candidate_id, material_id, session_id, source, first_at, last_at, now, now),
            )
            for message in fresh:
                connection.execute(
                    "INSERT INTO wechat_seen_messages "
                    "(session_id, source, sort_seq, create_time, local_id, material_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (session_id, source, *_cursor(message), material_id, now),
                )
            self._update_cursor(
                connection, payload["account_fingerprint"], session_id, messages[-1], source
            )
        return {"created": True, "material_id": material_id, "candidate_id": candidate_id}

    def _update_cursor(
        self,
        connection: Any,
        account_fingerprint: str,
        session_id: str,
        message: dict[str, Any],
        source: str = "personal_wechat",
    ) -> None:
        sort_seq, create_time, local_id = _cursor(message)
        now = utc_now()
        connection.execute(
            "INSERT INTO wechat_sync_state "
            "(account_fingerprint, session_id, source, sort_seq, create_time, local_id, last_success_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(account_fingerprint, session_id) "
            "DO UPDATE SET source = excluded.source, sort_seq = excluded.sort_seq, create_time = excluded.create_time, "
            "local_id = excluded.local_id, last_success_at = excluded.last_success_at, "
            "error = NULL, updated_at = excluded.updated_at",
            (account_fingerprint, session_id, source, sort_seq, create_time, local_id, now, now),
        )

    def _auto_accept_actionable_candidate(
        self, candidate: dict[str, Any], actor: str
    ) -> dict[str, Any] | None:
        if candidate.get("classification") != "relevant":
            return None
        try:
            confidence = float(candidate.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        if confidence < 0.95 or candidate.get("uncertainty_reason"):
            return None
        extracted = _json(candidate.get("extracted_json"), {})
        if extracted.get("conflict") or extracted.get("conflicts"):
            return None
        actions = extracted.get("actions") or []
        if not any(
            isinstance(item, dict)
            and str(item.get("kind") or "task") in _FOLLOW_UP_KINDS
            and str(item.get("title") or "").strip()
            for item in actions
        ):
            return None
        matter_id = self._find_open_matter(candidate, require_unique=True)
        if matter_id:
            return self.resolve_candidate(candidate["id"], "accept", actor, matter_id)
        if self._find_open_matter(candidate):
            return None
        return self.resolve_candidate(candidate["id"], "accept", actor)

    def complete_classification(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        job = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job or job["job_type"] != "wechat_classify":
            raise KeyError("微信理解任务不存在")
        if job["lease_owner"] != worker_id or job["lease_token"] != lease_token:
            raise PermissionError("任务租约无效")
        classification = str(result.get("classification") or "").strip()
        if classification not in {"relevant", "uncertain", "irrelevant"}:
            raise ValueError("贾维斯 没有给出有效的工作相关性判断")
        material = self.database.fetch_one(
            "SELECT text_note FROM materials WHERE id = ?", (job["material_id"],)
        ) or {}
        if not review_worthy_wechat_result(result, str(material.get("text_note") or "")):
            classification = "irrelevant"
        candidate_status = "ignored" if classification == "irrelevant" else "pending"
        material_status = "processed" if classification == "irrelevant" else "pending_review"
        now = utc_now()
        with self.database.connect() as connection:
            reserved = connection.execute(
                "UPDATE jobs SET result_version = -1, updated_at = ? "
                "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
                "AND lease_token = ? AND result_version = 0",
                (now, job_id, worker_id, lease_token),
            )
        if reserved.rowcount != 1:
            raise PermissionError("任务已经由其他处理流程接管")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE wechat_candidates SET classification = ?, summary = ?, "
                "uncertainty_reason = ?, confidence = ?, status = ?, evidence_json = ?, "
                "extracted_json = ?, updated_at = ? WHERE material_id = ?",
                (
                    classification,
                    str(result.get("summary") or "贾维斯 已完成初步判断")[:1000],
                    str(result.get("uncertainty_reason") or "")[:500],
                    result.get("confidence"),
                    candidate_status,
                    json.dumps(result.get("evidence") or [], ensure_ascii=False),
                    json.dumps(result.get("extracted") or {}, ensure_ascii=False),
                    now,
                    job["material_id"],
                ),
            )
            current_row = connection.execute(
                "SELECT * FROM wechat_candidates WHERE material_id = ?", (job["material_id"],)
            ).fetchone()
            current = dict(current_row) if current_row else {}
            completed = connection.execute(
                "UPDATE jobs SET status = 'succeeded', error = NULL, result_version = result_version + 1, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
                "AND lease_token = ? AND result_version = -1",
                (now, job_id, worker_id, lease_token),
            )
            if completed.rowcount != 1:
                raise PermissionError("任务结果写入权已失效")
            connection.execute(
                "UPDATE reminders SET status = 'done', updated_at = ? "
                "WHERE status = 'open' AND fingerprint LIKE ?",
                (now, f"job:{job_id}:%"),
            )
            connection.execute(
                "UPDATE materials SET status = ?, updated_at = ? WHERE id = ?",
                (material_status, now, job["material_id"]),
            )
        candidate = self.database.fetch_one(
            "SELECT * FROM wechat_candidates WHERE id = ?",
            (current.get("id"),),
        ) or {}
        if candidate:
            self.database.audit(
                _id("audit"), worker_id, "wechat.candidate.classified",
                "wechat_candidate", candidate["id"],
                metadata={
                        "classification": classification,
                        "status": candidate["status"],
                        "material_id": job["material_id"],
                        "merged": False,
                },
            )
        return candidate

    def list_candidates(
        self,
        status: str = "pending",
        limit: int = 100,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        allowed = {"pending", "processing", "ignored", "accepted", "retracted", "all"}
        if status not in allowed:
            raise ValueError("不支持的微信线索状态")
        if source and source not in _CHAT_SOURCES:
            raise ValueError("不支持的聊天来源")
        params: list[Any] = []
        clauses: list[str] = []
        if status == "ignored":
            clauses.extend(
                ["x.status = 'ignored'", "x.created_at >= datetime('now', '-7 day')"]
            )
        elif status != "all":
            clauses.append("x.status = ?")
            params.append(status)
        if source:
            clauses.append("x.source = ?")
            params.append(source)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(min(max(limit, 1), 500))
        rows = self.database.fetch_all(
            "SELECT x.*, c.display_name, c.kind AS conversation_kind, c.listen_status, "
            "m.text_note, m.status AS material_status FROM wechat_candidates x "
            "JOIN wechat_conversations c ON c.session_id = x.session_id "
            "JOIN materials m ON m.id = x.material_id "
            f"{where} ORDER BY x.created_at DESC LIMIT ?",
            tuple(params),
        )
        for row in rows:
            row["evidence"] = _json(row.pop("evidence_json", "[]"), [])
            row["extracted"] = _json(row.pop("extracted_json", "{}"), {})
            row.pop("text_note", None)
            row["status_label"] = _WECHAT_STATUS_LABELS.get(
                str(row.get("status") or ""), "等待处理"
            )
            if row.get("material_status"):
                row["material_status_label"] = _WECHAT_STATUS_LABELS.get(
                    str(row["material_status"]), "等待处理"
                )
        return rows

    def export_listening_windows(
        self,
        since: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        clauses = ["c.listen_status = 'active'", "m.status <> 'retracted'"]
        if since:
            clauses.append("x.window_end > ?")
            params.append(since)
        params.extend((min(max(limit, 1), 500), max(offset, 0)))
        return self.database.fetch_all(
            "SELECT x.material_id, x.source, x.session_id, x.window_start, x.window_end, "
            "c.display_name, c.kind AS conversation_kind, m.text_note "
            "FROM wechat_candidates x "
            "JOIN wechat_conversations c ON c.session_id = x.session_id "
            "JOIN materials m ON m.id = x.material_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY x.window_end, x.material_id LIMIT ? OFFSET ?",
            tuple(params),
        )

    def _find_open_matter(
        self, candidate: dict[str, Any], require_unique: bool = False
    ) -> str | None:
        rows = self.database.fetch_all(
            "SELECT m.id AS matter_id, m.title AS matter_title, '' AS session_id, "
            "m.title || ' ' || m.summary || ' ' || "
            "COALESCE(GROUP_CONCAT(CASE WHEN a.status = 'open' THEN a.title END, ' '), '') "
            "AS summary FROM matters m LEFT JOIN actions a ON a.matter_id = m.id WHERE ("
            "EXISTS (SELECT 1 FROM actions a WHERE a.matter_id = m.id AND a.status = 'open') OR "
            "EXISTS (SELECT 1 FROM review_items r WHERE r.matter_id = m.id AND r.status = 'pending') OR "
            "EXISTS (SELECT 1 FROM reminders x WHERE x.matter_id = m.id "
            "AND x.status NOT IN ('done', 'dismissed')) OR "
            "EXISTS (SELECT 1 FROM materials x JOIN jobs j ON j.material_id = x.id "
            "WHERE x.matter_id = m.id AND j.status IN "
            "('queued', 'claimed', 'processing', 'retryable_failed', 'needs_review'))"
            ") GROUP BY m.id ORDER BY m.updated_at DESC"
        )
        for row in rows:
            row["extracted_json"] = json.dumps(
                {"matter_title": row.pop("matter_title")}, ensure_ascii=False
            )
        matches = list(
            dict.fromkeys(
                str(row["matter_id"])
                for row in rows
                if _same_matter_topic(candidate, row)
            )
        )
        if require_unique and len(matches) != 1:
            return None
        return matches[0] if matches else None

    def resolve_candidate(
        self, candidate_id: str, action: str, actor: str, matter_id: str | None = None
    ) -> dict[str, Any]:
        candidate = self.database.fetch_one(
            "SELECT * FROM wechat_candidates WHERE id = ?", (candidate_id,)
        )
        if not candidate:
            raise KeyError("微信线索不存在")
        now = utc_now()
        audit_action = {
            "ignore": "wechat.candidate.ignored",
            "restore": "wechat.candidate.restored",
            "undo": "wechat.candidate.undone",
            "accept": "wechat.candidate.accepted",
            "merge": "wechat.candidate.merged",
        }.get(action)
        if not audit_action:
            raise ValueError("不支持的微信线索操作")

        if action == "ignore":
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE wechat_candidates SET status = 'ignored', resolved_at = ?, "
                    "updated_at = ? WHERE id = ?",
                    (now, now, candidate_id),
                )
                connection.execute(
                    "UPDATE materials SET status = 'processed', updated_at = ? WHERE id = ?",
                    (now, candidate["material_id"]),
                )
        elif action in {"restore", "undo"}:
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE wechat_candidates SET status = 'pending', matter_id = NULL, "
                    "resolved_at = NULL, updated_at = ? WHERE id = ?",
                    (now, candidate_id),
                )
                connection.execute(
                    "UPDATE materials SET matter_id = NULL, status = 'pending_review', updated_at = ? "
                    "WHERE id = ?",
                    (now, candidate["material_id"]),
                )
                connection.execute(
                    "UPDATE actions SET status = 'dismissed', updated_at = ? "
                    "WHERE created_by = ? AND status = 'open'",
                    (now, f"wechat_candidate:{candidate_id}"),
                )
                connection.execute(
                    "UPDATE action_assignees SET status = 'superseded', updated_at = ? "
                    "WHERE action_id IN (SELECT id FROM actions WHERE created_by = ?)",
                    (now, f"wechat_candidate:{candidate_id}"),
                )
        else:
            extracted = _json(candidate.get("extracted_json"), {})
            if action == "merge" and not matter_id:
                raise ValueError("请选择要合并的事项")
            if action == "accept" and not matter_id:
                matter_id = self._find_open_matter(candidate)
            if matter_id:
                matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
                if not matter:
                    raise KeyError("要合并的事项不存在")
            else:
                matter_id = _id("matter")
                title = str(extracted.get("matter_title") or candidate["summary"] or "微信工作线索")
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT INTO matters (id, title, summary, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (matter_id, title[:120], candidate["summary"][:1000], now, now),
                    )
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE wechat_candidates SET status = 'accepted', matter_id = ?, "
                    "resolved_at = ?, updated_at = ? WHERE id = ?",
                    (matter_id, now, now, candidate_id),
                )
                connection.execute(
                    "UPDATE materials SET matter_id = ?, status = 'processed', updated_at = ? "
                    "WHERE id = ?",
                    (matter_id, now, candidate["material_id"]),
                )
                connection.execute(
                    "UPDATE matters SET updated_at = ? WHERE id = ?", (now, matter_id)
                )
                for item in (extracted.get("actions") or [])[:20]:
                    if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                        continue
                    kind = str(item.get("kind") or "task")
                    if kind not in {"conclusion", "task", "risk", "decision", "waiting"}:
                        kind = "task"
                    action_id = _id("action")
                    connection.execute(
                        "INSERT INTO actions "
                        "(id, matter_id, material_id, kind, title, detail, status, owner, due_date, "
                        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                        (
                            action_id,
                            matter_id,
                            candidate["material_id"],
                            kind,
                            str(item.get("title"))[:160],
                            str(item.get("detail") or "")[:1000],
                            str(item.get("owner") or "")[:80] or None,
                            item.get("due_date"),
                            f"wechat_candidate:{candidate_id}",
                            now,
                            now,
                        ),
                    )
                    upsert_action_suggestions(
                        connection,
                        action_id,
                        candidate["material_id"],
                        normalize_suggestions(item),
                        now,
                    )

                prefill_matter_contact(
                    connection,
                    matter_id,
                    [
                        item
                        for item in (extracted.get("actions") or [])[:20]
                        if isinstance(item, dict)
                    ],
                    now,
                )

        self.database.audit(
            _id("audit"),
            actor,
            audit_action,
            "wechat_candidate",
            candidate_id,
            matter_id,
            {"action": action},
        )
        resolved = self.database.fetch_one(
            "SELECT * FROM wechat_candidates WHERE id = ?", (candidate_id,)
        )
        if not resolved:
            raise RuntimeError("微信线索状态写入失败")
        resolved["evidence"] = _json(resolved.pop("evidence_json", "[]"), [])
        resolved["extracted"] = _json(resolved.pop("extracted_json", "{}"), {})
        resolved["status_label"] = _WECHAT_STATUS_LABELS.get(
            str(resolved.get("status") or ""), "等待处理"
        )
        return resolved

    def retract_material(self, material_id: str, actor: str) -> dict[str, Any]:
        material = self.database.fetch_one("SELECT * FROM materials WHERE id = ?", (material_id,))
        if not material:
            raise KeyError("材料不存在")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE materials SET status = 'retracted', matter_id = NULL, updated_at = ? "
                "WHERE id = ?", (now, material_id)
            )
            connection.execute(
                "UPDATE jobs SET status = 'cancelled', lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, updated_at = ? WHERE material_id = ? "
                "AND status NOT IN ('succeeded', 'cancelled')", (now, material_id)
            )
            connection.execute(
                "UPDATE wechat_candidates SET status = 'retracted', resolved_at = ?, updated_at = ? "
                "WHERE material_id = ?", (now, now, material_id)
            )
            connection.execute(
                "UPDATE channel_intakes SET retracted_at = ? WHERE material_id = ?", (now, material_id)
            )
        self.database.audit(
            _id("audit"), actor, "material.retracted", "material", material_id,
            metadata={"source_type": material["source_type"]},
        )
        return self.database.fetch_one("SELECT * FROM materials WHERE id = ?", (material_id,)) or {}

    def record_channel_intake(
        self, material_id: str, channel: str, external_message_id: str | None,
        sender: str | None, sent_at: str | None, file_type: str | None,
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO channel_intakes "
                "(id, material_id, channel, external_message_id, sender, sent_at, file_type, created_at) "
                "SELECT ?, ?, ?, ?, ?, ?, ?, ? WHERE NOT EXISTS "
                "(SELECT 1 FROM channel_intakes WHERE material_id = ? AND channel = ? "
                "AND retracted_at IS NULL)",
                (
                    _id("channel"), material_id, channel, external_message_id,
                    sender, sent_at, file_type, utc_now(), material_id, channel,
                ),
            )

    def undo_last_channel_intake(self, channel: str, actor: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            "SELECT * FROM channel_intakes WHERE channel = ? AND retracted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (channel,),
        )
        if not row:
            raise KeyError("最近没有可撤销的收件")
        return self.retract_material(row["material_id"], actor)
