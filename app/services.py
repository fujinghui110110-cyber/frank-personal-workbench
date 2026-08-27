from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import unicodedata
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import UploadFile

from .assignees import PEOPLE_SEED, normalize_suggestions, upsert_action_suggestions
from .config import Settings
from .db import Database, utc_now


SOURCE_TYPES = {
    "text",
    "image",
    "file",
    "audio",
    "video",
    "wechat_markdown",
    "wecom_approval",
    "email_auto",
    "wechat_auto",
    "wechat_channel",
    "wecom_channel",
    "workbuddy_channel",
}
ACTION_KINDS = {"conclusion", "task", "risk", "decision", "waiting"}
FLOW_STATES = {"needs_action", "waiting", "blocked", "needs_decision"}
SCHEDULE_BASES = {"material_explicit", "user_entered", "suggested", "legacy"}
AUTO_SCHEDULE_BASES = {"material_explicit", "user_entered"}


class StaleWriteError(RuntimeError):
    pass


def next_updated_at(current: str | None) -> str:
    now = datetime.now(UTC)
    if current:
        try:
            previous = datetime.fromisoformat(current.replace("Z", "+00:00"))
            now = max(now, previous + timedelta(seconds=1))
        except ValueError:
            pass
    return now.isoformat(timespec="seconds").replace("+00:00", "Z")


def matter_stage(
    status: str,
    open_actions: int,
    pending_reviews: int,
    open_reminders: int,
    active_jobs: int,
    blocked_actions: int = 0,
    waiting_actions: int = 0,
) -> str:
    if status == "completed":
        return "completed"
    if pending_reviews:
        return "needs_decision"
    if blocked_actions:
        return "blocked"
    if waiting_actions:
        return "waiting"
    if not any((open_actions, pending_reviews, open_reminders, active_jobs)):
        return "ready_to_close"
    return "in_progress"


def has_explicit_date_fact(facts: list[dict[str, Any]], value: Any) -> bool:
    if not value:
        return False
    token = str(value)[:10]
    try:
        token = date.fromisoformat(token).isoformat()
    except ValueError:
        return False
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        field_type = str(fact.get("field_type") or "")
        if "日期" not in field_type and "时间" not in field_type:
            continue
        evidence = " ".join(
            str(fact.get(key) or "") for key in ("value", "quote", "source_locator")
        )
        if token in evidence:
            return True
    return False


def normalize_schedule(
    action: dict[str, Any],
    facts: list[dict[str, Any]],
) -> tuple[str, Any, Any]:
    due_date = action.get("due_date") or None
    next_follow_up_at = action.get("next_follow_up_at") or None
    basis = str(action.get("schedule_basis") or "").strip()
    if basis not in SCHEDULE_BASES:
        if due_date or next_follow_up_at:
            basis = "material_explicit" if (
                (not due_date or has_explicit_date_fact(facts, due_date))
                and (
                    not next_follow_up_at
                    or has_explicit_date_fact(facts, next_follow_up_at)
                )
            ) else "suggested"
        else:
            basis = "legacy"
    if basis == "material_explicit" and not (
        (not due_date or has_explicit_date_fact(facts, due_date))
        and (
            not next_follow_up_at
            or has_explicit_date_fact(facts, next_follow_up_at)
        )
    ):
        basis = "suggested"
    if basis == "suggested":
        due_date = None
        next_follow_up_at = None
    return basis, due_date, next_follow_up_at


ATTENTION_PRIORITIES = {
    "decision": 0,
    "assignee_confirmation": 1,
    "overdue": 2,
    "follow_up": 3,
    "reminder": 4,
    "risk": 5,
    "action": 6,
}


def build_attention(
    actions: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    assignee_reviews: list[dict[str, Any]],
    reminders: list[dict[str, Any]],
    target_date: date,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    suppressed_actions: set[str] = set()
    pending_review_ids = {
        str(review.get("id") or "").strip() for review in reviews
    }
    counts = {key: 0 for key in ATTENTION_PRIORITIES}

    def add(
        item_type: str,
        item_id: Any,
        *,
        matter_id: Any,
        matter_title: Any,
        title: Any,
        reason: str,
        due_at: Any,
        target: str,
        source: str,
        requires_confirmation: bool,
        key: tuple[str, str] | None = None,
    ) -> None:
        normalized_id = str(item_id or "").strip()
        identity = key or (item_type, normalized_id)
        if not normalized_id or identity in seen:
            return
        seen.add(identity)
        counts[item_type] += 1
        items.append(
            {
                "id": normalized_id,
                "item_type": item_type,
                "source": source,
                "matter_id": matter_id,
                "matter_title": matter_title,
                "title": title or "需要处理",
                "reason": reason,
                "due_at": due_at,
                "priority": ATTENTION_PRIORITIES[item_type],
                "target": target,
                "requires_confirmation": requires_confirmation,
            }
        )

    for review in reviews:
        payload = review.get("payload") or {}
        action_id = str(payload.get("action_id") or "").strip()
        if action_id:
            suppressed_actions.add(action_id)
        add(
            "decision",
            review.get("id"),
            matter_id=review.get("matter_id"),
            matter_title=review.get("matter_title"),
            title=review.get("title") or "需要确认",
            reason="需要你确认后才能继续",
            due_at=None,
            target="reviews",
            source="review",
            requires_confirmation=True,
        )

    for review in assignee_reviews:
        action_id = str(review.get("action_id") or "").strip()
        if not action_id:
            continue
        suppressed_actions.add(action_id)
        add(
            "assignee_confirmation",
            action_id,
            matter_id=review.get("matter_id"),
            matter_title=review.get("matter_title"),
            title=review.get("action_title") or "确认跟进人",
            reason="建议的跟进人尚未确认",
            due_at=None,
            target="reviews",
            source="assignee",
            requires_confirmation=True,
        )

    reminder_action_ids: set[str] = set()
    for reminder in reminders:
        if reminder.get("status") != "open":
            continue
        fingerprint = str(reminder.get("fingerprint") or "")
        if fingerprint.startswith("review:") and fingerprint[7:] in pending_review_ids:
            continue
        due = reminder.get("due_at")
        if due:
            due_text = str(due)
            if len(due_text) == 10:
                try:
                    if date.fromisoformat(due_text) > target_date:
                        continue
                except ValueError:
                    pass
                parsed_due = None
            else:
                try:
                    parsed_due = datetime.fromisoformat(due_text.replace("Z", "+00:00"))
                except ValueError:
                    parsed_due = None
                if parsed_due and parsed_due.tzinfo is None:
                    parsed_due = parsed_due.replace(tzinfo=UTC)
                if parsed_due and parsed_due > now:
                    continue
        action_id = str(reminder.get("action_id") or "").strip()
        if action_id:
            reminder_action_ids.add(action_id)
        add(
            "reminder",
            reminder.get("id"),
            matter_id=reminder.get("matter_id"),
            matter_title=reminder.get("matter_title"),
            title=reminder.get("title") or "需要关注",
            reason=reminder.get("reason") or "提醒已到期",
            due_at=reminder.get("due_at"),
            target="matter" if reminder.get("matter_id") else "today",
            source="reminder",
            requires_confirmation=False,
            key=("action", action_id) if action_id else None,
        )

    for action in actions:
        action_id = str(action.get("id") or "").strip()
        if not action_id or action_id in suppressed_actions or action_id in reminder_action_ids:
            continue
        snoozed_until = action.get("snoozed_until")
        if snoozed_until:
            try:
                snoozed = datetime.fromisoformat(
                    str(snoozed_until).replace("Z", "+00:00")
                )
            except ValueError:
                snoozed = None
            if snoozed and snoozed.tzinfo is None:
                snoozed = snoozed.replace(tzinfo=UTC)
            if snoozed and snoozed > now:
                continue
        due_date = None
        if action.get("due_date"):
            try:
                due_date = date.fromisoformat(str(action["due_date"])[:10])
            except ValueError:
                pass
        follow_up = None
        if action.get("next_follow_up_at"):
            try:
                follow_up = datetime.fromisoformat(
                    str(action["next_follow_up_at"]).replace("Z", "+00:00")
                )
            except ValueError:
                pass
            if follow_up and follow_up.tzinfo is None:
                follow_up = follow_up.replace(tzinfo=UTC)
        waiting = action.get("flow_state") == "waiting"
        if waiting and (not follow_up or follow_up > now):
            continue
        if action.get("flow_state") == "needs_decision":
            item_type, reason = "decision", "需要你确认后才能继续"
        elif due_date and due_date < target_date:
            item_type, reason = "overdue", f"已逾期 {(target_date - due_date).days} 天"
        elif waiting and follow_up and follow_up <= now:
            item_type, reason = "follow_up", "已到约定跟进时间"
        elif action.get("kind") == "risk" or action.get("flow_state") == "blocked":
            item_type, reason = "risk", "存在阻塞或风险，需要尽快处理"
        else:
            item_type, reason = "action", "按截止时间和最近变化排序"
        add(
            item_type,
            action_id,
            matter_id=action.get("matter_id"),
            matter_title=action.get("matter_title"),
            title=action.get("title"),
            reason=reason,
            due_at=action.get("due_date") or action.get("next_follow_up_at"),
            target="matter",
            source="action",
            requires_confirmation=item_type == "decision",
        )

    items.sort(
        key=lambda item: (
            item["priority"],
            str(item.get("due_at") or "9999-12-31"),
            str(item.get("id") or ""),
        )
    )
    unique_items: list[dict[str, Any]] = []
    seen_matters: set[str] = set()
    for item in items:
        identity = str(item.get("matter_id") or f"{item['item_type']}:{item['id']}")
        if identity in seen_matters:
            continue
        seen_matters.add(identity)
        selected = dict(item)
        if selected.get("matter_title"):
            selected["driver_title"] = selected.get("title")
            selected["title"] = selected["matter_title"]
        unique_items.append(selected)
    counts = {key: 0 for key in ATTENTION_PRIORITIES}
    for item in unique_items:
        counts[item["item_type"]] += 1
    counts["total"] = len(unique_items)
    return unique_items, counts


def build_workflow_stats(
    actions: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    assignee_reviews: list[dict[str, Any]],
    reminders: list[dict[str, Any]],
    target_date: date,
    now: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    attention, attention_counts = build_attention(
        actions,
        reviews,
        assignee_reviews,
        reminders,
        target_date,
        now,
    )
    due_today = 0
    overdue = 0
    for action in actions:
        if not action.get("due_date"):
            continue
        try:
            due_date = date.fromisoformat(str(action["due_date"])[:10])
        except ValueError:
            continue
        if due_date == target_date:
            due_today += 1
        elif due_date < target_date:
            overdue += 1
    return attention, {
        "open_actions": len(actions),
        "attention": attention_counts["total"],
        "attention_counts": attention_counts,
        "pending_reviews": len(reviews) + len(assignee_reviews),
        "open_reminders": sum(
            1 for reminder in reminders if reminder.get("status") == "open"
        ),
        "due_today": due_today,
        "overdue": overdue,
    }


def default_flow_state(kind: str) -> str:
    if kind == "waiting":
        return "waiting"
    if kind == "decision":
        return "needs_decision"
    return "needs_action"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def parse_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def public_labels(value: Any) -> Any:
    if isinstance(value, str):
        return (
            value.replace("WorkBuddy 财务参谋长", "贾维斯")
            .replace("WorkBuddy", "贾维斯")
            .replace("workbuddy", "assistant")
            .replace("财务参谋长", "Frank 的个人工作台")
        )
    if isinstance(value, dict):
        return {public_labels(key): public_labels(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_labels(item) for item in value]
    return value


def friendly_analysis_error(value: str | None) -> str:
    error = str(value or "").strip()
    if "规定变更类型不完整" in error:
        return "这条内容的规定类型判断不完整，重新整理后会按待确认内容保留。"
    if "尚未提取出可供" in error and "文字" in error:
        return "文件正文上次没有成功提取，重新整理后会再次读取文档内容。"
    if any(marker in error.lower() for marker in ("timeout", "timed out", "超时")):
        return "整理服务上次等待超时，可以直接重新整理。"
    if any(marker in error.lower() for marker in ("401", "403", "unauthorized", "密钥")):
        return "整理服务的连接授权需要重新检查。"
    return "上次整理没有完成，原材料仍安全保留，可以重新整理。"


def normalized_title(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized[:120] or "待归并事项"


def public_material(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    metadata = parse_json(result.pop("metadata_json", "{}"), {})
    assistant = metadata.pop("workbuddy", None)
    if isinstance(assistant, dict):
        metadata["assistant"] = {**assistant, "display_name": "贾维斯"}
    result["metadata"] = metadata
    if result.get("source_type") == "workbuddy_channel":
        result["source_type"] = "assistant_channel"
    result.pop("storage_key", None)
    result["has_file"] = bool(row.get("storage_key"))
    return public_labels(result)


def public_node(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    metadata = parse_json(result.pop("metadata_json", "{}"), {})
    if metadata.get("agent"):
        metadata["agent"] = "贾维斯"
    result["metadata"] = metadata
    if "WorkBuddy" in str(result.get("name", "")):
        result["name"] = "Frank 的个人工作台执行节点"
    return public_labels(result)


def _json_list(value: str | None) -> list[Any]:
    parsed = parse_json(value, [])
    return parsed if isinstance(parsed, list) else []


class WorkbenchService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    async def receive_material(
        self,
        source_type: str,
        idempotency_key: str,
        text_note: str,
        upload: UploadFile | None,
        actor: str,
        matter_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if source_type not in SOURCE_TYPES:
            raise ValueError("不支持的材料类型")
        if not idempotency_key.strip():
            raise ValueError("缺少幂等键")
        existing = self.database.fetch_one(
            "SELECT * FROM materials WHERE idempotency_key = ?",
            (idempotency_key.strip(),),
        )
        if existing:
            return public_material(existing), False

        digest = hashlib.sha256()
        note_bytes = text_note.encode("utf-8")
        has_upload = bool(upload and upload.filename)
        if not has_upload:
            digest.update(note_bytes)
        size = 0 if has_upload else len(note_bytes)
        temp_path: Path | None = None
        filename: str | None = None
        content_type: str | None = None
        if has_upload and upload:
            filename = Path(upload.filename).name[:255]
            content_type = (upload.content_type or "application/octet-stream")[:120]
            temp_path = self.settings.objects_dir / f".upload-{uuid4().hex}"
            with temp_path.open("wb") as destination:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.settings.max_upload_bytes:
                        temp_path.unlink(missing_ok=True)
                        raise ValueError("材料超过允许的大小")
                    digest.update(chunk)
                    destination.write(chunk)
        if size == 0:
            raise ValueError("材料不能为空")

        sha256 = digest.hexdigest()
        duplicate = self.database.fetch_one(
            "SELECT * FROM materials WHERE sha256 = ? AND source_type = ?",
            (sha256, source_type),
        )
        if duplicate:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            return public_material(duplicate), False

        storage_key: str | None = None
        if temp_path:
            object_dir = self.settings.objects_dir / sha256[:2]
            object_dir.mkdir(parents=True, exist_ok=True)
            object_path = object_dir / sha256
            if object_path.exists():
                temp_path.unlink(missing_ok=True)
            else:
                temp_path.replace(object_path)
            storage_key = str(object_path.relative_to(self.settings.data_dir))

        material_id = new_id("mat")
        job_id = new_id("job")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO materials "
                "(id, idempotency_key, sha256, source_type, filename, content_type, size, "
                "text_note, storage_key, status, matter_id, received_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_analysis', ?, ?, ?, '{}')",
                (
                    material_id,
                    idempotency_key.strip(),
                    sha256,
                    source_type,
                    filename,
                    content_type,
                    size,
                    text_note.strip(),
                    storage_key,
                    matter_id,
                    now,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO jobs "
                "(id, material_id, job_type, status, requires_local, created_at, updated_at) "
                "VALUES (?, ?, 'workbuddy_analyze', 'held', 1, ?, ?)",
                (job_id, material_id, now, now),
            )
        self.database.audit(
            new_id("audit"),
            actor,
            "material.received",
            "material",
            material_id,
            matter_id,
            {"source_type": source_type, "size": size, "sha256": sha256},
        )
        created = self.database.fetch_one("SELECT * FROM materials WHERE id = ?", (material_id,))
        if not created:
            raise RuntimeError("材料写入失败")
        return public_material(created), True

    def list_materials(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.database.fetch_all(
                "SELECT * FROM materials WHERE status = ? ORDER BY received_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = self.database.fetch_all(
                "SELECT * FROM materials ORDER BY received_at DESC LIMIT ?", (limit,)
            )
        materials: list[dict[str, Any]] = []
        for row in rows:
            material = public_material(row)
            material["job"] = self.database.fetch_one(
                "SELECT status, error, attempt_count, updated_at FROM jobs "
                "WHERE material_id = ? ORDER BY created_at DESC LIMIT 1",
                (row["id"],),
            )
            materials.append(material)
        return materials

    def get_material(self, material_id: str) -> dict[str, Any] | None:
        row = self.database.fetch_one("SELECT * FROM materials WHERE id = ?", (material_id,))
        if not row:
            return None
        material = public_material(row)
        material["job"] = self.database.fetch_one(
            "SELECT status, error, attempt_count, updated_at FROM jobs "
            "WHERE material_id = ? ORDER BY created_at DESC LIMIT 1",
            (material_id,),
        )
        return material

    def queue_workbuddy_analysis(self, material_id: str, actor: str) -> dict[str, Any]:
        material = self.database.fetch_one("SELECT * FROM materials WHERE id = ?", (material_id,))
        if not material:
            raise KeyError("材料不存在")
        now = utc_now()
        existing = self.database.fetch_one(
            "SELECT * FROM jobs WHERE material_id = ? AND job_type = 'workbuddy_analyze'",
            (material_id,),
        )
        transcription = parse_json(material.get("metadata_json"), {}).get("transcription")
        material_status = "transcribed" if transcription else "queued"
        with self.database.connect() as connection:
            if existing:
                connection.execute(
                    "UPDATE jobs SET status = 'queued', attempt_count = 0, error = NULL, "
                    "next_attempt_at = NULL, lease_owner = NULL, lease_token = NULL, "
                    "lease_expires_at = NULL, result_version = 0, updated_at = ? WHERE id = ?",
                    (now, existing["id"]),
                )
            else:
                connection.execute(
                    "INSERT INTO jobs "
                    "(id, material_id, job_type, status, requires_local, priority, "
                    "created_at, updated_at) VALUES (?, ?, 'workbuddy_analyze', 'queued', 1, 80, ?, ?)",
                    (new_id("job"), material_id, now, now),
                )
            connection.execute(
                "UPDATE materials SET status = ?, updated_at = ? WHERE id = ?",
                (material_status, now, material_id),
            )
        self.database.audit(
            new_id("audit"),
            actor,
            "material.workbuddy_queued",
            "material",
            material_id,
            material.get("matter_id"),
        )
        queued = self.get_material(material_id)
        if not queued:
            raise RuntimeError("贾维斯 任务创建失败")
        return queued

    def analysis_status(self) -> dict[str, Any]:
        counts = self.database.fetch_all(
            "SELECT status, COUNT(*) AS count FROM jobs WHERE job_type IN "
            "('workbuddy_analyze', 'wechat_classify', 'email_classify') "
            "GROUP BY status"
        )
        by_status = {row["status"]: int(row["count"]) for row in counts}
        pending = by_status.get("held", 0)
        running = sum(
            by_status.get(status, 0)
            for status in ("queued", "claimed", "processing")
        )
        last_release = self.database.fetch_one(
            "SELECT created_at FROM audit_events WHERE action = 'analysis.released' "
            "ORDER BY created_at DESC LIMIT 1"
        )
        return {
            "pending": pending,
            "running": running,
            "retrying": by_status.get("retryable_failed", 0),
            "needs_review": by_status.get("needs_review", 0),
            "last_started_at": last_release.get("created_at") if last_release else None,
        }

    def analysis_issues(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT j.id, j.job_type, j.status, j.attempt_count, j.max_attempts, "
            "j.error, j.updated_at, m.id AS material_id, m.filename, m.source_type, "
            "m.content_type, m.received_at, m.matter_id "
            "FROM jobs j JOIN materials m ON m.id = j.material_id "
            "WHERE j.status = 'needs_review' "
            "AND j.job_type IN ('workbuddy_analyze', 'wechat_classify', 'email_classify') "
            "ORDER BY j.updated_at DESC, j.id DESC"
        )
        for row in rows:
            row["reason"] = friendly_analysis_error(row.pop("error", ""))
            row["source_label"] = {
                "personal_wechat": "个人微信",
                "wechat_auto": "个人微信",
                "wecom_auto": "企业微信",
                "email": "邮箱",
                "audio": "录音",
                "video": "视频",
                "image": "图片",
                "file": "文件",
            }.get(str(row.get("source_type") or ""), "手工投递")
        return public_labels(rows)

    def retry_analysis_issue(self, job_id: str, actor: str) -> dict[str, Any]:
        job = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            raise KeyError("需要检查的内容不存在")
        if job["status"] not in {"needs_review", "retryable_failed"}:
            raise ValueError("这份内容当前不需要重新整理")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = 'queued', attempt_count = 0, error = NULL, "
                "next_attempt_at = NULL, lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, result_version = 0, updated_at = ? WHERE id = ?",
                (now, job_id),
            )
            connection.execute(
                "UPDATE materials SET status = 'queued', updated_at = ? WHERE id = ?",
                (now, job["material_id"]),
            )
        self.database.audit(
            new_id("audit"), actor, "analysis.issue_retried", "job", job_id,
            metadata={"material_id": job["material_id"]},
        )
        return {"queued": True, **self.analysis_status()}

    def release_analysis(self, actor: str) -> dict[str, Any]:
        now = utc_now()
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET status = 'queued', error = NULL, next_attempt_at = NULL, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE status = 'held' AND job_type IN "
                "('workbuddy_analyze', 'wechat_classify', 'email_classify')",
                (now,),
            )
            connection.execute(
                "UPDATE materials SET status = 'queued', updated_at = ? WHERE id IN "
                "(SELECT material_id FROM jobs WHERE status = 'queued' AND job_type IN "
                "('workbuddy_analyze', 'wechat_classify', 'email_classify'))",
                (now,),
            )
        released = int(updated.rowcount)
        self.database.audit(
            new_id("audit"), actor, "analysis.released", "analysis", "pending",
            metadata={"released": released},
        )
        prepared = self.prepare_attention_work_packages(actor)
        return {
            "released": released,
            "work_packages_prepared": prepared["prepared"],
            "work_packages_failed": prepared["failed"],
            **self.analysis_status(),
        }

    def validate_auxiliary_job(
        self, job_id: str, worker_id: str, lease_token: str
    ) -> dict[str, Any]:
        job = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            raise KeyError("任务不存在")
        if (
            job["status"] != "processing"
            or job.get("lease_owner") != worker_id
            or job.get("lease_token") != lease_token
        ):
            raise ValueError("任务租约无效")
        return job

    def reserve_job_result(
        self, job_id: str, worker_id: str, lease_token: str
    ) -> dict[str, Any]:
        now = utc_now()
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET result_version = -1, updated_at = ? "
                "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
                "AND lease_token = ? AND result_version = 0",
                (now, job_id, worker_id, lease_token),
            )
        if updated.rowcount != 1:
            job = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
            if not job:
                raise KeyError("任务不存在")
            if (
                job["status"] != "processing"
                or job.get("lease_owner") != worker_id
                or job.get("lease_token") != lease_token
            ):
                raise ValueError("任务租约无效")
            raise PermissionError("任务已经由其他处理流程接管")
        return self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {}

    def release_job_result(
        self, job_id: str, worker_id: str, lease_token: str
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE jobs SET result_version = 0 WHERE id = ? AND status = 'processing' "
                "AND lease_owner = ? AND lease_token = ? AND result_version = -1",
                (job_id, worker_id, lease_token),
            )

    def complete_auxiliary_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        *,
        reserved: bool = False,
    ) -> dict[str, Any]:
        job = (
            self.validate_auxiliary_job(job_id, worker_id, lease_token)
            if reserved
            else self.reserve_job_result(job_id, worker_id, lease_token)
        )
        now = utc_now()
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET status = 'succeeded', error = NULL, result_version = 1, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
                "AND lease_token = ? AND result_version = -1",
                (now, job_id, worker_id, lease_token),
            )
            if updated.rowcount != 1:
                raise PermissionError("任务结果写入权已失效")
        self.database.audit(
            new_id("audit"), worker_id, "job.completed", "job", job_id,
            metadata={"job_type": job["job_type"]},
        )
        return self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,)) or {}

    def save_transcript(
        self,
        material_id: str,
        worker_id: str,
        lease_token: str,
        text: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        job = self.database.fetch_one(
            "SELECT * FROM jobs WHERE material_id = ? ORDER BY created_at DESC LIMIT 1",
            (material_id,),
        )
        if not job:
            raise KeyError("材料处理任务不存在")
        if (
            job["status"] != "processing"
            or job["lease_owner"] != worker_id
            or not secrets.compare_digest(job["lease_token"] or "", lease_token)
        ):
            raise PermissionError("任务租约无效")
        row = self.database.fetch_one("SELECT * FROM materials WHERE id = ?", (material_id,))
        if not row:
            raise KeyError("材料不存在")
        current_metadata = parse_json(row.get("metadata_json"), {})
        current_metadata["transcription"] = metadata
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE materials SET text_note = ?, status = 'transcribed', metadata_json = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    text,
                    json.dumps(current_metadata, ensure_ascii=False, separators=(",", ":")),
                    now,
                    material_id,
                ),
            )
        self.database.audit(
            new_id("audit"),
            worker_id,
            "material.transcribed",
            "material",
            material_id,
            row.get("matter_id"),
            metadata={"characters": len(text), **metadata},
        )
        saved = self.get_material(material_id)
        if not saved:
            raise RuntimeError("会议转写保存失败")
        return saved

    def get_transcript(self, material_id: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            "SELECT text_note, metadata_json FROM materials WHERE id = ?", (material_id,)
        )
        if not row:
            raise KeyError("材料不存在")
        metadata = parse_json(row.get("metadata_json"), {}).get("transcription", {})
        return {"text": row.get("text_note") or "", "metadata": metadata}

    def material_content_path(self, material_id: str) -> Path | None:
        row = self.database.fetch_one(
            "SELECT storage_key FROM materials WHERE id = ?", (material_id,)
        )
        if not row or not row["storage_key"]:
            return None
        path = (self.settings.data_dir / row["storage_key"]).resolve()
        if self.settings.objects_dir not in path.parents:
            raise ValueError("非法材料路径")
        return path

    def create_matter(self, title: str, actor: str, summary: str = "") -> dict[str, Any]:
        clean_title = normalized_title(title)
        existing = self.database.fetch_one(
            "SELECT * FROM matters WHERE lower(title) = lower(?) ORDER BY created_at LIMIT 1",
            (clean_title,),
        )
        if existing:
            return existing
        matter_id = new_id("matter")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO matters (id, title, summary, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (matter_id, clean_title, summary.strip()[:1000], now, now),
            )
        self.database.audit(
            new_id("audit"), actor, "matter.created", "matter", matter_id, matter_id
        )
        self.record_matter_event(
            matter_id,
            "matter.created",
            actor,
            "matter",
            matter_id,
            "事项已创建",
        )
        matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
        if not matter:
            raise RuntimeError("事项创建失败")
        return matter

    def record_matter_event(
        self,
        matter_id: str | None,
        event_type: str,
        actor: str,
        object_type: str | None = None,
        object_id: str | None = None,
        summary: str = "",
        payload: dict[str, Any] | None = None,
        *,
        created_at: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if not matter_id:
            return None
        event_id = new_id("event")
        timestamp = created_at or utc_now()
        values = (
            event_id,
            matter_id,
            event_type,
            actor,
            object_type,
            object_id,
            str(summary or "")[:500],
            json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
            timestamp,
        )
        query = (
            "INSERT INTO matter_events "
            "(id, matter_id, event_type, actor, object_type, object_id, summary, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        if connection is not None:
            connection.execute(query, values)
            row = connection.execute(
                "SELECT * FROM matter_events WHERE id = ?", (event_id,)
            ).fetchone()
            row = dict(row) if row else None
        else:
            with self.database.connect() as own_connection:
                own_connection.execute(query, values)
            row = self.database.fetch_one(
                "SELECT * FROM matter_events WHERE id = ?", (event_id,)
            )
        if not row:
            return None
        row["payload"] = parse_json(row.pop("payload_json", "{}"), {})
        row["type"] = row["event_type"]
        row["source"] = {
            "actor": row.get("actor"),
            "object_type": row.get("object_type"),
            "object_id": row.get("object_id"),
        }
        return public_labels(row)

    def assign_material(
        self,
        material_id: str,
        actor: str,
        matter_id: str | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        material = self.database.fetch_one("SELECT * FROM materials WHERE id = ?", (material_id,))
        if not material:
            raise KeyError("材料不存在")
        if matter_id:
            matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
            if not matter:
                raise KeyError("事项不存在")
        else:
            matter = self.create_matter(title or material["filename"] or "待归并事项", actor)
            matter_id = matter["id"]
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE materials SET matter_id = ?, updated_at = ? WHERE id = ?",
                (matter_id, now, material_id),
            )
            connection.execute(
                "UPDATE matters SET updated_at = ? WHERE id = ?", (now, matter_id)
            )
        self.database.audit(
            new_id("audit"),
            actor,
            "material.assigned",
            "material",
            material_id,
            matter_id,
        )
        detail = self.get_matter(matter_id)
        if not detail:
            raise RuntimeError("事项关联失败")
        return detail

    def reassign_material_preview(
        self, material_id: str, matter_id: str
    ) -> dict[str, Any]:
        material = self.database.fetch_one(
            "SELECT id, matter_id, filename, updated_at FROM materials WHERE id = ?",
            (material_id,),
        )
        if not material:
            raise KeyError("材料不存在")
        target = self.database.fetch_one(
            "SELECT id, title FROM matters WHERE id = ?", (matter_id,)
        )
        if not target:
            raise KeyError("事项不存在")
        actions = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM actions WHERE material_id = ?",
            (material_id,),
        )["count"]
        counts = {
            "facts": self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM evidence WHERE material_id = ?",
                (material_id,),
            )["count"],
            "actions": actions,
            "reminders": self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM reminders WHERE action_id IN "
                "(SELECT id FROM actions WHERE material_id = ?)",
                (material_id,),
            )["count"],
            "reviews": self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM review_items WHERE material_id = ?",
                (material_id,),
            )["count"],
            "email_records": self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM email_messages WHERE material_id = ?",
                (material_id,),
            )["count"],
            "chat_records": self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM wechat_candidates WHERE material_id = ?",
                (material_id,),
            )["count"],
        }
        return public_labels({
            "material_id": material_id,
            "from_matter_id": material.get("matter_id"),
            "to_matter_id": matter_id,
            "to_matter_title": target["title"],
            "updated_at": material["updated_at"],
            "counts": counts,
            "will_change": material.get("matter_id") != matter_id,
        })

    def reassign_material(
        self,
        material_id: str,
        matter_id: str,
        reason: str,
        expected_updated_at: str | None,
        actor: str,
    ) -> dict[str, Any]:
        material = self.database.fetch_one(
            "SELECT * FROM materials WHERE id = ?", (material_id,)
        )
        if not material:
            raise KeyError("材料不存在")
        target = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
        if not target:
            raise KeyError("事项不存在")
        note = reason.strip()
        if not note:
            raise ValueError("请填写重新归属原因")
        if expected_updated_at and expected_updated_at != material.get("updated_at"):
            raise StaleWriteError("材料已被更新，请刷新后再操作")
        old_matter_id = material.get("matter_id")
        if old_matter_id == matter_id:
            result = self.get_matter(matter_id) or target
            result["_material_updated_at"] = material.get("updated_at")
            result["changed"] = False
            return result
        now = next_updated_at(material.get("updated_at"))
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE materials SET matter_id = ?, updated_at = ? "
                "WHERE id = ? AND updated_at = ?",
                (matter_id, now, material_id, material["updated_at"]),
            )
            if updated.rowcount != 1:
                raise StaleWriteError("材料已被更新，请刷新后再操作")
            connection.execute(
                "UPDATE reminders SET matter_id = ?, updated_at = ? "
                "WHERE action_id IN (SELECT id FROM actions WHERE material_id = ?)",
                (matter_id, now, material_id),
            )
            for table in ("evidence", "actions", "review_items"):
                connection.execute(
                    f"UPDATE {table} SET matter_id = ? WHERE material_id = ?",
                    (matter_id, material_id),
                )
            connection.execute(
                "UPDATE email_messages SET matter_id = ?, updated_at = ? WHERE material_id = ?",
                (matter_id, now, material_id),
            )
            connection.execute(
                "UPDATE wechat_candidates SET matter_id = ?, updated_at = ? WHERE material_id = ?",
                (matter_id, now, material_id),
            )
            affected = [value for value in (old_matter_id, matter_id) if value]
            for affected_id in dict.fromkeys(affected):
                connection.execute(
                    "UPDATE matters SET updated_at = ? WHERE id = ?", (now, affected_id)
                )
                connection.execute(
                    "UPDATE work_packages SET status = 'stale', updated_at = ? "
                    "WHERE matter_id = ?",
                    (now, affected_id),
                )
            payload = {
                "before": old_matter_id,
                "after": matter_id,
                "reason": note[:500],
            }
            self.database.audit(
                new_id("audit"), actor, "material.reassigned", "material", material_id,
                matter_id, payload, connection=connection,
            )
            if old_matter_id:
                self.record_matter_event(
                    old_matter_id, "material.reassigned_out", actor, "material", material_id,
                    "材料已移出本事项", payload, connection=connection,
                )
            self.record_matter_event(
                matter_id, "material.reassigned_in", actor, "material", material_id,
                "材料已重新归入本事项", payload, connection=connection,
            )
        result = self.get_matter(matter_id)
        if not result:
            raise RuntimeError("材料重新归属失败")
        result["_material_updated_at"] = now
        result["changed"] = True
        return result

    def correct_fact(
        self,
        evidence_id: str,
        value: str,
        field_type: str | None,
        reason: str,
        expected_created_at: str | None,
        actor: str,
    ) -> dict[str, Any]:
        evidence = self.database.fetch_one(
            "SELECT * FROM evidence WHERE id = ?", (evidence_id,)
        )
        if not evidence:
            raise KeyError("事实依据不存在")
        if evidence.get("status") == "superseded":
            raise StaleWriteError("该事实已有修正版，请刷新后再操作")
        if expected_created_at and expected_created_at != evidence.get("created_at"):
            raise StaleWriteError("事实依据已被更新，请刷新后再操作")
        corrected_value = value.strip()
        correction_reason = reason.strip()
        if not corrected_value or not correction_reason:
            raise ValueError("请填写修正内容和修正原因")
        new_id_value = new_id("evidence")
        now = utc_now()
        payload = {
            "before_id": evidence_id,
            "after_id": new_id_value,
            "before": evidence["value"],
            "after": corrected_value[:2000],
            "reason": correction_reason[:500],
        }
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE evidence SET status = 'superseded' "
                "WHERE id = ? AND status != 'superseded'",
                (evidence_id,),
            )
            if updated.rowcount != 1:
                raise StaleWriteError("该事实已有修正版，请刷新后再操作")
            connection.execute(
                "INSERT INTO evidence "
                "(id, matter_id, material_id, claim_type, field_type, value, "
                "source_locator, quote, confidence, status, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
                (
                    new_id_value,
                    evidence["matter_id"],
                    evidence["material_id"],
                    evidence["claim_type"],
                    (field_type or evidence["field_type"])[:160],
                    corrected_value[:2000],
                    evidence["source_locator"],
                    evidence.get("quote") or "",
                    evidence.get("confidence"),
                    actor,
                    now,
                ),
            )
            connection.execute(
                "UPDATE review_items SET evidence_id = ? "
                "WHERE evidence_id = ? AND status = 'pending'",
                (new_id_value, evidence_id),
            )
            connection.execute(
                "UPDATE matters SET updated_at = ? WHERE id = ?",
                (now, evidence["matter_id"]),
            )
            connection.execute(
                "UPDATE work_packages SET status = 'stale', updated_at = ? WHERE matter_id = ?",
                (now, evidence["matter_id"]),
            )
            self.database.audit(
                new_id("audit"), actor, "evidence.corrected", "evidence", new_id_value,
                evidence["matter_id"], payload, connection=connection,
            )
            self.record_matter_event(
                evidence["matter_id"], "evidence.corrected", actor, "evidence",
                new_id_value, "已建立事实修正版", payload, connection=connection,
            )
        result = self.database.fetch_one(
            "SELECT * FROM evidence WHERE id = ?", (new_id_value,)
        )
        if not result:
            raise RuntimeError("事实修正失败")
        return public_labels(result)

    def claim_job(self, worker_id: str) -> dict[str, Any] | None:
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        expires = (now_dt + timedelta(seconds=self.settings.lease_seconds)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        lease_token = secrets.token_urlsafe(24)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE materials SET status = 'queued', updated_at = ? WHERE id IN "
                "(SELECT material_id FROM jobs WHERE status IN ('claimed', 'processing') "
                "AND lease_expires_at < ?)",
                (now, now),
            )
            connection.execute(
                "UPDATE jobs SET status = 'queued', lease_owner = NULL, lease_token = NULL, "
                "lease_expires_at = NULL, result_version = 0, updated_at = ? "
                "WHERE status IN ('claimed', 'processing') AND lease_expires_at < ?",
                (now, now),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE "
                "(status = 'queued' OR (status = 'retryable_failed' AND next_attempt_at <= ?)) "
                "AND requires_local = 1 ORDER BY priority DESC, created_at LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return None
            job = dict(row)
            updated = connection.execute(
                "UPDATE jobs SET status = 'claimed', lease_owner = ?, lease_token = ?, "
                "lease_expires_at = ?, attempt_count = attempt_count + 1, updated_at = ? "
                "WHERE id = ? AND status IN ('queued', 'retryable_failed')",
                (worker_id, lease_token, expires, now, job["id"]),
            )
            if updated.rowcount != 1:
                return None
        claimed = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job["id"],))
        if not claimed:
            return None
        claimed["lease_token"] = lease_token
        claimed["material"] = self.get_material(claimed["material_id"])
        self.database.audit(
            new_id("audit"), worker_id, "job.claimed", "job", claimed["id"]
        )
        return claimed

    def start_job(self, job_id: str, worker_id: str, lease_token: str) -> dict[str, Any]:
        now = utc_now()
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE jobs SET status = 'processing', updated_at = ? "
                "WHERE id = ? AND status = 'claimed' AND lease_owner = ? AND lease_token = ?",
                (now, job_id, worker_id, lease_token),
            )
            if updated.rowcount != 1:
                raise PermissionError("任务租约无效")
            connection.execute(
                "UPDATE materials SET status = 'processing', updated_at = ? "
                "WHERE id = (SELECT material_id FROM jobs WHERE id = ?)",
                (now, job_id),
            )
        self.database.audit(
            new_id("audit"), worker_id, "job.started", "job", job_id
        )
        job = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            raise RuntimeError("任务不存在")
        return job

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        error: str,
    ) -> dict[str, Any]:
        job = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            raise KeyError("任务不存在")
        if (
            job["status"] != "processing"
            or job["lease_owner"] != worker_id
            or job["lease_token"] != lease_token
        ):
            raise PermissionError("任务租约无效")
        terminal = int(job["attempt_count"]) >= int(job["max_attempts"])
        status = "needs_review" if terminal else "retryable_failed"
        delay = min(300, 15 * (2 ** max(0, int(job["attempt_count"]) - 1)))
        next_attempt_at = None
        if not terminal:
            next_attempt_at = (
                datetime.now(UTC) + timedelta(seconds=delay)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        now = utc_now()
        with self.database.connect() as connection:
            failed = connection.execute(
                "UPDATE jobs SET status = ?, error = ?, next_attempt_at = ?, lease_owner = NULL, "
                "lease_token = NULL, lease_expires_at = NULL, result_version = 0, updated_at = ? "
                "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
                "AND lease_token = ? AND result_version = 0",
                (
                    status,
                    error[:1000],
                    next_attempt_at,
                    now,
                    job_id,
                    worker_id,
                    lease_token,
                ),
            )
            if failed.rowcount != 1:
                raise PermissionError("任务已经由其他处理流程接管")
            connection.execute(
                "UPDATE materials SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, job["material_id"]),
            )
        self.database.audit(
            new_id("audit"),
            worker_id,
            "job.failed",
            "job",
            job_id,
            metadata={"status": status, "error": error[:200]},
        )
        result = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not result:
            raise RuntimeError("任务失败状态写入失败")
        return result

    def _merge_analysis_work_package(
        self,
        connection: sqlite3.Connection,
        matter_id: str,
        material_id: str,
        result: dict[str, Any],
        facts: list[dict[str, Any]],
        actions: list[dict[str, Any]],
        now: str,
        actor: str,
    ) -> None:
        existing_row = connection.execute(
            "SELECT * FROM work_packages WHERE matter_id = ?", (matter_id,)
        ).fetchone()
        existing = dict(existing_row) if existing_row else None
        if existing and existing["status"] != "adopted":
            draft = parse_json(existing.get("draft_json"), {})
        else:
            draft = {}
        if not isinstance(draft, dict):
            draft = {}
        draft.setdefault("conclusion", "")
        draft.setdefault("basis", [])
        draft.setdefault("gaps", [])
        draft.setdefault("risks", [])
        draft.setdefault("steps", [])
        draft.setdefault("questions", [])
        draft.setdefault("reply_draft", {"purpose": "", "text": ""})
        if not draft["conclusion"]:
            draft["conclusion"] = str(
                result.get("summary") or result.get("matter_title") or ""
            )[:2000]

        def append_unique(key: str, value: Any) -> None:
            text = str(value or "").strip()
            values = draft.get(key)
            if text and isinstance(values, list) and text not in values:
                values.append(text)

        for item in facts:
            append_unique(
                "basis",
                f"待确认：{str(item.get('value') or '').strip()}"
                + (
                    f"（{str(item.get('source_locator') or '').strip()}）"
                    if item.get("source_locator")
                    else ""
                ),
            )

        brief = result.get("brief") if isinstance(result.get("brief"), dict) else {}
        for key, draft_key in (("gaps", "gaps"), ("missing", "gaps"), ("risks", "risks")):
            values = brief.get(key) or []
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                for value in values:
                    append_unique(draft_key, value)

        steps = draft.get("steps")
        if not isinstance(steps, list):
            steps = []
            draft["steps"] = steps
        known_steps = {
            (
                str(step.get("kind") or "task"),
                str(step.get("title") or "").strip(),
                str(step.get("detail") or "").strip(),
            )
            for step in steps
            if isinstance(step, dict)
        }
        for action in actions:
            if not isinstance(action, dict):
                continue
            kind = str(action.get("kind") or "task")
            if kind not in ACTION_KINDS:
                raise ValueError("不支持的行动类型")
            basis, due_date, next_follow_up_at = normalize_schedule(action, facts)
            title = str(action.get("title") or "待处理").strip()[:200]
            detail = str(action.get("detail") or "").strip()[:2000]
            identity = (kind, title, detail)
            if identity in known_steps:
                continue
            flow_state = str(action.get("flow_state") or default_flow_state(kind))
            if flow_state not in FLOW_STATES:
                flow_state = default_flow_state(kind)
            estimated_minutes = action.get("estimated_minutes")
            steps.append(
                {
                    "kind": kind,
                    "title": title,
                    "detail": detail,
                    "owner": str(action.get("owner") or "")[:160],
                    "due_date": due_date,
                    "flow_state": flow_state,
                    "waiting_on": str(action.get("waiting_on") or "")[:160],
                    "blocked_reason": str(action.get("blocked_reason") or "")[:500],
                    "next_follow_up_at": next_follow_up_at,
                    "schedule_basis": basis,
                    "suggested_due_date": action.get("due_date") if basis == "suggested" else None,
                    "suggested_next_follow_up_at": (
                        action.get("next_follow_up_at") if basis == "suggested" else None
                    ),
                    "estimated_minutes": (
                        estimated_minutes
                        if isinstance(estimated_minutes, int) and estimated_minutes > 0
                        else None
                    ),
                    "assignee_suggestions": normalize_suggestions(action),
                    "source_material_id": material_id,
                }
            )
            known_steps.add(identity)

        for suggestion in result.get("completion_suggestions") or []:
            if not isinstance(suggestion, dict):
                continue
            reason = str(suggestion.get("reason") or "").strip()
            action_id = str(suggestion.get("action_id") or "").strip()
            prefix = f"是否确认行动 {action_id} 已完成"
            if isinstance(draft.get("questions"), list):
                draft["questions"] = [
                    question
                    for question in draft["questions"]
                    if not str(question).startswith(prefix)
                ]
            append_unique(
                "questions",
                prefix + (f"：{reason}" if reason else ""),
            )

        next_check_at = brief.get("next_check_at")
        next_check_reason = str(brief.get("next_check_reason") or "").strip()
        if next_check_at and next_check_reason:
            append_unique(
                "questions",
                f"是否在 {str(next_check_at)[:50]} 复查：{next_check_reason[:500]}",
            )

        package_id = existing["id"] if existing else new_id("package")
        version = int(existing["version"]) + 1 if existing else 1
        if existing and now <= existing["updated_at"]:
            now = (
                datetime.fromisoformat(existing["updated_at"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        fingerprint = self._work_package_fingerprint(matter_id, connection=connection)
        connection.execute(
            "INSERT INTO work_packages "
            "(id, matter_id, version, draft_json, source_fingerprint, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?) "
            "ON CONFLICT(matter_id) DO UPDATE SET version = excluded.version, "
            "draft_json = excluded.draft_json, source_fingerprint = excluded.source_fingerprint, "
            "status = 'draft', updated_at = excluded.updated_at",
            (
                package_id,
                matter_id,
                version,
                json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                fingerprint,
                existing["created_at"] if existing else now,
                now,
            ),
        )
        payload = {
            "material_id": material_id,
            "version": version,
            "suggested_steps": len(actions),
        }
        self.database.audit(
            new_id("audit"),
            actor,
            "work_package.prepared",
            "work_package",
            package_id,
            matter_id,
            payload,
            connection=connection,
        )
        self.record_matter_event(
            matter_id,
            "work_package.prepared",
            actor,
            "work_package",
            package_id,
            "贾维斯已准备办理方案，等待人工核对",
            payload,
            connection=connection,
        )

    def complete_job(
        self,
        job_id: str,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        job = self.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            raise KeyError("任务不存在")
        if job["status"] == "succeeded":
            material = self.database.fetch_one(
                "SELECT matter_id FROM materials WHERE id = ?", (job["material_id"],)
            )
            if material and material["matter_id"]:
                matter = self.get_matter(material["matter_id"])
                if matter:
                    return matter
        if (
            job["status"] != "processing"
            or job["lease_owner"] != worker_id
            or job["lease_token"] != lease_token
        ):
            raise PermissionError("任务租约无效")

        material = self.database.fetch_one(
            "SELECT * FROM materials WHERE id = ?", (job["material_id"],)
        )
        if not material:
            raise RuntimeError("任务材料不存在")
        matter_id = material["matter_id"]
        facts = result.get("facts", [])
        for fact in facts:
            if not fact.get("source_locator"):
                raise ValueError("财务事实必须提供原文定位")
        self.reserve_job_result(job_id, worker_id, lease_token)
        if matter_id:
            matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
            if not matter:
                raise KeyError("事项不存在")
        else:
            matter = self.create_matter(
                result.get("matter_title") or material["filename"] or "待确认事项建议",
                worker_id,
                result.get("summary", ""),
            )
            matter_id = matter["id"]
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE matters SET status = 'needs_decision' WHERE id = ?",
                    (matter_id,),
                )

        inferences = result.get("inferences", [])
        actions = result.get("actions", [])
        now = utc_now()
        material_metadata = parse_json(material.get("metadata_json"), {})
        brief = result.get("brief") if isinstance(result.get("brief"), dict) else {}
        agent_trace = (
            result.get("agent_trace") if isinstance(result.get("agent_trace"), dict) else {}
        )
        if brief or agent_trace:
            material_metadata["workbuddy"] = {
                **agent_trace,
                "brief": brief,
                "job_id": job_id,
                "updated_at": now,
            }
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE materials SET matter_id = ?, status = 'processed', metadata_json = ?, "
                "updated_at = ? "
                "WHERE id = ?",
                (
                    matter_id,
                    json.dumps(material_metadata, ensure_ascii=False, separators=(",", ":")),
                    now,
                    job["material_id"],
                ),
            )
            for claim_type, items in (("fact", facts), ("inference", inferences)):
                for item in items:
                    evidence_id = new_id("evidence")
                    connection.execute(
                        "INSERT INTO evidence "
                        "(id, matter_id, material_id, claim_type, field_type, value, "
                        "source_locator, quote, confidence, status, created_by, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            evidence_id,
                            matter_id,
                            job["material_id"],
                            claim_type,
                            str(item.get("field_type", "其他"))[:50],
                            str(item.get("value", ""))[:1000],
                            str(item.get("source_locator", "模型推断"))[:255],
                            str(item.get("quote", ""))[:1000],
                            item.get("confidence"),
                            "pending",
                            worker_id,
                            now,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO review_items "
                        "(id, matter_id, material_id, evidence_id, kind, title, payload_json, "
                        "confidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_id("review"),
                            matter_id,
                            job["material_id"],
                            evidence_id,
                            claim_type,
                            f"确认{'事实' if claim_type == 'fact' else '推断'}：{str(item.get('value', ''))[:80]}",
                            json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                            item.get("confidence"),
                            now,
                        ),
                    )
            self._merge_analysis_work_package(
                connection,
                matter_id,
                job["material_id"],
                result,
                facts,
                actions,
                now,
                worker_id,
            )
            completed = connection.execute(
                "UPDATE jobs SET status = 'succeeded', error = NULL, result_version = 1, "
                "lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'processing' AND lease_owner = ? "
                "AND lease_token = ? AND result_version = -1",
                (now, job_id, worker_id, lease_token),
            )
            if completed.rowcount != 1:
                raise PermissionError("任务结果写入权已失效")
        self.database.audit(
            new_id("audit"),
            worker_id,
            "job.completed",
            "job",
            job_id,
            matter_id,
            {
                "facts_pending": len(facts),
                "inferences_pending": len(inferences),
                "suggested_actions": len(actions),
            },
        )
        self.record_matter_event(
            matter_id,
            "worker.result",
            worker_id,
            "job",
            job_id,
            "材料处理结果已写入事项",
            {
                "material_id": job["material_id"],
                "facts_pending": len(facts),
                "inferences_pending": len(inferences),
                "suggested_actions": len(actions),
            },
        )
        detail = self.get_matter(matter_id)
        if not detail:
            raise RuntimeError("处理结果写入失败")
        return detail

    def list_matters(self, limit: int = 100) -> list[dict[str, Any]]:
        matters = self.database.fetch_all(
            "SELECT m.*, "
            "(SELECT COUNT(*) FROM materials x WHERE x.matter_id = m.id) AS material_count, "
            "(SELECT COUNT(*) FROM actions a WHERE a.matter_id = m.id AND a.status = 'open') "
            "AS open_action_count, "
            "(SELECT COUNT(*) FROM actions a WHERE a.matter_id = m.id AND a.status = 'open' "
            "AND a.flow_state = 'blocked') AS blocked_action_count, "
            "(SELECT COUNT(*) FROM actions a WHERE a.matter_id = m.id AND a.status = 'open' "
            "AND a.flow_state = 'waiting') AS waiting_action_count, "
            "((SELECT COUNT(*) FROM review_items r WHERE r.matter_id = m.id AND r.status = 'pending') + "
            "(SELECT COUNT(*) FROM action_assignees aa JOIN actions a ON a.id = aa.action_id "
            "WHERE a.matter_id = m.id AND aa.status = 'pending')) "
            "AS pending_review_count, "
            "(SELECT COUNT(*) FROM reminders x WHERE x.matter_id = m.id "
            "AND x.status NOT IN ('done', 'dismissed')) AS open_reminder_count, "
            "(SELECT COUNT(*) FROM materials x JOIN jobs j ON j.material_id = x.id "
            "WHERE x.matter_id = m.id AND j.status IN "
            "('queued', 'claimed', 'processing', 'retryable_failed', 'needs_review')) "
            "AS active_job_count "
            "FROM matters m ORDER BY m.updated_at DESC LIMIT ?",
            (limit,),
        )
        for matter in matters:
            matter.pop("status_override", None)
            matter["is_completed"] = matter["status"] == "completed"
            matter["stage"] = matter_stage(
                matter["status"],
                matter["open_action_count"],
                matter["pending_review_count"],
                matter["open_reminder_count"],
                matter["active_job_count"],
                matter["blocked_action_count"],
                matter["waiting_action_count"],
            )
        return public_labels(matters)

    def _attach_action_assignees(self, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not actions:
            return actions
        for action in actions:
            action["completion_evidence"] = parse_json(
                action.get("completion_evidence"), []
            )
        ids = [item["id"] for item in actions]
        placeholders = ",".join("?" for _ in ids)
        rows = self.database.fetch_all(
            "SELECT aa.*, p.display_name, p.role FROM action_assignees aa "
            "JOIN people p ON p.id = aa.person_id "
            f"WHERE aa.action_id IN ({placeholders}) ORDER BY aa.created_at",
            tuple(ids),
        )
        grouped: dict[str, list[dict[str, Any]]] = {action_id: [] for action_id in ids}
        for row in rows:
            row["evidence"] = _json_list(row.pop("evidence_json", "[]"))
            grouped.setdefault(row["action_id"], []).append(row)
        for action in actions:
            items = grouped.get(action["id"], [])
            action["assignees"] = [item for item in items if item["status"] == "confirmed"]
            action["assignee_suggestions"] = [
                item for item in items if item["status"] == "pending"
            ]
        return public_labels(actions)

    def list_people(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT p.*, "
            "(SELECT COUNT(DISTINCT a.id) FROM action_assignees aa "
            "JOIN actions a ON a.id = aa.action_id "
            "WHERE aa.person_id = p.id AND aa.status = 'confirmed' AND a.status = 'open') "
            "AS open_action_count, "
            "(SELECT COUNT(DISTINCT a.id) FROM action_assignees aa "
            "JOIN actions a ON a.id = aa.action_id "
            "WHERE aa.person_id = p.id AND aa.status = 'pending' AND a.status = 'open') "
            "AS pending_action_count "
            "FROM people p WHERE p.enabled = 1 ORDER BY p.is_self DESC, p.created_at"
        )
        for row in rows:
            row["aliases"] = _json_list(row.pop("aliases_json", "[]"))
            row["phonetic_aliases"] = _json_list(row.pop("phonetic_aliases_json", "[]"))
            row["is_self"] = bool(row.get("is_self"))
            row["enabled"] = bool(row.get("enabled"))
        return public_labels(rows)

    def list_person_identities(self, person_id: str) -> list[dict[str, Any]]:
        if person_id not in {person["id"] for person in PEOPLE_SEED}:
            raise KeyError("人员不存在")
        return public_labels(
            self.database.fetch_all(
                "SELECT source, stable_id "
                "FROM person_identities WHERE person_id = ? ORDER BY updated_at DESC",
                (person_id,),
            )
        )

    def list_actions(
        self, status: str = "open", person_id: str | None = None
    ) -> list[dict[str, Any]]:
        if status not in {"open", "done", "dismissed", "all"}:
            raise ValueError("不支持的行动状态")
        params: list[Any] = []
        where = ["a.created_at >= (SELECT MIN(created_at) FROM people)"]
        if status != "all":
            where.append("a.status = ?")
            params.append(status)
        if person_id:
            if person_id == "unassigned":
                where.append(
                    "NOT EXISTS (SELECT 1 FROM action_assignees aa "
                    "WHERE aa.action_id = a.id AND aa.status = 'confirmed')"
                )
            else:
                where.append(
                    "EXISTS (SELECT 1 FROM action_assignees aa "
                    "WHERE aa.action_id = a.id AND aa.person_id = ? AND aa.status = 'confirmed')"
                )
                params.append(person_id)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            f"JOIN matters m ON m.id = a.matter_id {clause} "
        "ORDER BY CASE WHEN a.due_date IS NULL THEN 1 ELSE 0 END, "
        "a.due_date, a.created_at DESC, a.id DESC",
            tuple(params),
        )
        return self._attach_action_assignees(rows)

    def update_action_planning(
        self,
        action_id: str,
        changes: dict[str, Any],
        actor: str,
    ) -> dict[str, Any]:
        action = self.database.fetch_one("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not action:
            raise KeyError("行动不存在")

        expected_updated_at = changes.pop("expected_updated_at", None)
        reason = str(changes.pop("change_reason", "") or "")[:500]
        if expected_updated_at and expected_updated_at != action.get("updated_at"):
            raise StaleWriteError("行动已被更新，请刷新后再保存")
        allowed = {
            "title",
            "detail",
            "kind",
            "due_date",
            "flow_state",
            "waiting_on",
            "blocked_reason",
            "next_follow_up_at",
            "estimated_minutes",
            "pinned",
            "snoozed_until",
            "completion_evidence",
        }
        changes = {key: value for key, value in changes.items() if key in allowed}
        if not changes:
            raise ValueError("至少提供一项规划字段")

        values: dict[str, Any] = {}
        if "title" in changes:
            title = str(changes["title"] or "").strip()
            if not title:
                raise ValueError("行动标题不能为空")
            values["title"] = title[:200]
        if "detail" in changes:
            values["detail"] = str(changes["detail"] or "")[:2000]
        if "kind" in changes:
            kind = str(changes["kind"] or "")
            if kind not in ACTION_KINDS:
                raise ValueError("不支持的行动类型")
            values["kind"] = kind
            if "flow_state" not in changes:
                values["flow_state"] = default_flow_state(kind)
        if "due_date" in changes:
            due_date = changes["due_date"] or None
            if due_date:
                try:
                    date.fromisoformat(str(due_date))
                except ValueError as exc:
                    raise ValueError("行动日期格式不正确") from exc
            values["due_date"] = due_date
        if "flow_state" in changes:
            flow_state = changes["flow_state"] or default_flow_state(action["kind"])
            if flow_state not in FLOW_STATES:
                raise ValueError("不支持的规划状态")
            values["flow_state"] = flow_state
        if "waiting_on" in changes:
            values["waiting_on"] = str(changes["waiting_on"] or "")[:160]
        if "blocked_reason" in changes:
            values["blocked_reason"] = str(changes["blocked_reason"] or "")[:500]
        for field in ("next_follow_up_at", "snoozed_until"):
            if field not in changes:
                continue
            value = changes[field]
            if value:
                try:
                    datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("时间格式不正确") from exc
            values[field] = value or None
        if "due_date" in changes or "next_follow_up_at" in changes:
            final_due_date = values.get("due_date", action.get("due_date"))
            final_follow_up = values.get(
                "next_follow_up_at", action.get("next_follow_up_at")
            )
            values["schedule_basis"] = (
                "user_entered" if final_due_date or final_follow_up else "legacy"
            )
        if "estimated_minutes" in changes:
            minutes = changes["estimated_minutes"]
            if minutes is not None and (not isinstance(minutes, int) or minutes < 1):
                raise ValueError("预计分钟数必须为正整数")
            values["estimated_minutes"] = minutes
        if "pinned" in changes:
            values["pinned_at"] = utc_now() if changes["pinned"] else None
        if "completion_evidence" in changes:
            evidence = changes["completion_evidence"] or []
            if not isinstance(evidence, list):
                raise ValueError("完成证明必须为文本数组")
            values["completion_evidence"] = json.dumps(
                [str(item)[:500] for item in evidence[:12]],
                ensure_ascii=False,
                separators=(",", ":"),
            )

        now = utc_now()
        if now <= action["updated_at"]:
            now = (
                datetime.fromisoformat(action["updated_at"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        assignments = ", ".join(f"{key} = ?" for key in values)
        audit_payload = {
            "before": {key: action.get(key) for key in values},
            "after": values,
            "reason": reason,
        }
        with self.database.connect() as connection:
            updated_row = connection.execute(
                f"UPDATE actions SET {assignments}, updated_at = ? "
                "WHERE id = ? AND updated_at = ?",
                (*values.values(), now, action_id, action["updated_at"]),
            )
            if updated_row.rowcount != 1:
                raise StaleWriteError("行动已被更新，请刷新后再保存")
            self.database.audit(
                new_id("audit"),
                actor,
                "action.planning_updated",
                "action",
                action_id,
                action["matter_id"],
                audit_payload,
                connection=connection,
            )
            self.record_matter_event(
                action["matter_id"],
                "action.planning_updated",
                actor,
                "action",
                action_id,
                "行动规划已更新",
                audit_payload,
                connection=connection,
            )
        updated = self.database.fetch_one("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not updated:
            raise RuntimeError("行动规划写入失败")
        return self._attach_action_assignees([updated])[0]

    def get_matter(self, matter_id: str) -> dict[str, Any] | None:
        matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
        if not matter:
            return None
        matter["materials"] = [
            public_material(row)
            for row in self.database.fetch_all(
                "SELECT * FROM materials WHERE matter_id = ? ORDER BY received_at DESC",
                (matter_id,),
            )
        ]
        matter["assistant"] = next(
            (
                item["metadata"]["assistant"]
                for item in matter["materials"]
                if isinstance(item.get("metadata", {}).get("assistant"), dict)
            ),
            None,
        )
        matter["evidence"] = self.database.fetch_all(
            "SELECT * FROM evidence WHERE matter_id = ? ORDER BY created_at DESC",
            (matter_id,),
        )
        matter["actions"] = self.database.fetch_all(
            "SELECT * FROM actions WHERE matter_id = ? ORDER BY created_at DESC",
            (matter_id,),
        )
        matter["actions"] = self._attach_action_assignees(matter["actions"])
        matter["reminders"] = self.database.fetch_all(
            "SELECT * FROM reminders WHERE matter_id = ? ORDER BY created_at DESC",
            (matter_id,),
        )
        matter["reviews"] = [
            {**row, "payload": parse_json(row.pop("payload_json", "{}"), {})}
            for row in self.database.fetch_all(
                "SELECT * FROM review_items WHERE matter_id = ? ORDER BY created_at DESC",
                (matter_id,),
            )
        ]
        if matter["assistant"] is None and (
            matter["actions"] or matter["evidence"] or matter["reviews"]
        ):
            matter["assistant"] = {
                "display_name": "贾维斯",
                "source": "事项推进方案",
                "brief": {
                    "headline": matter.get("summary") or matter["title"],
                    "what_i_did": ["已从原始材料中提取推进事项和跟进重点"],
                },
            }
        active_job = self.database.fetch_one(
            "SELECT COUNT(*) AS total FROM materials x JOIN jobs j ON j.material_id = x.id "
            "WHERE x.matter_id = ? AND j.status IN "
            "('queued', 'claimed', 'processing', 'retryable_failed', 'needs_review')",
            (matter_id,),
        )
        matter.pop("status_override", None)
        open_actions = [item for item in matter["actions"] if item.get("status") == "open"]
        pending_reviews = [item for item in matter["reviews"] if item.get("status") == "pending"]
        pending_assignees = [
            assignee
            for action in matter["actions"]
            for assignee in action.get("assignee_suggestions", [])
            if assignee.get("status") == "pending"
        ]
        open_reminders = [
            item
            for item in matter["reminders"]
            if item.get("status") not in {"done", "dismissed"}
        ]
        matter["is_completed"] = matter["status"] == "completed"
        matter["stage"] = matter_stage(
            matter["status"],
            len(open_actions),
            len(pending_reviews) + len(pending_assignees),
            len(open_reminders),
            int((active_job or {}).get("total") or 0),
            sum(item.get("flow_state") == "blocked" for item in open_actions),
            sum(item.get("flow_state") == "waiting" for item in open_actions),
        )
        return public_labels(matter)

    def _work_package_fingerprint(
        self,
        matter_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        def fetch_one(query: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
            if connection is None:
                return self.database.fetch_one(query, params)
            row = connection.execute(query, params).fetchone()
            return dict(row) if row else None

        def fetch_all(query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
            if connection is None:
                return self.database.fetch_all(query, params)
            return [dict(row) for row in connection.execute(query, params).fetchall()]

        matter = fetch_one(
            "SELECT id, title, status, summary, goal, completion_criteria, owner, "
            "target_date, updated_at FROM matters WHERE id = ?",
            (matter_id,),
        )
        if not matter:
            raise KeyError("事项不存在")
        source = {
            "matter": matter,
            "materials": fetch_all(
                "SELECT id, sha256, status, updated_at FROM materials WHERE matter_id = ? "
                "ORDER BY id",
                (matter_id,),
            ),
            "evidence": fetch_all(
                "SELECT id, status, created_at FROM evidence WHERE matter_id = ? ORDER BY id",
                (matter_id,),
            ),
            "actions": fetch_all(
                "SELECT id, status, updated_at FROM actions WHERE matter_id = ? ORDER BY id",
                (matter_id,),
            ),
            "reminders": fetch_all(
                "SELECT id, status, updated_at FROM reminders WHERE matter_id = ? ORDER BY id",
                (matter_id,),
            ),
            "reviews": fetch_all(
                "SELECT id, status, resolved_at FROM review_items WHERE matter_id = ? ORDER BY id",
                (matter_id,),
            ),
        }
        return hashlib.sha256(
            json.dumps(source, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest()

    def get_work_package(self, matter_id: str) -> dict[str, Any] | None:
        if not self.database.fetch_one("SELECT id FROM matters WHERE id = ?", (matter_id,)):
            raise KeyError("事项不存在")
        package = self.database.fetch_one(
            "SELECT * FROM work_packages WHERE matter_id = ?", (matter_id,)
        )
        if not package:
            return None
        if (
            package["status"] in {"draft", "stale"}
            and package["source_fingerprint"] != self._work_package_fingerprint(matter_id)
        ):
            package["status"] = "stale"
        package["draft"] = parse_json(package.pop("draft_json", "{}"), {})
        return public_labels(package)

    def generate_work_package(self, matter_id: str, actor: str) -> dict[str, Any]:
        matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
        if not matter:
            raise KeyError("事项不存在")
        fingerprint = self._work_package_fingerprint(matter_id)
        evidence = self.database.fetch_all(
            "SELECT value, source_locator FROM evidence "
            "WHERE matter_id = ? AND status = 'confirmed' ORDER BY created_at",
            (matter_id,),
        )
        reviews = self.database.fetch_all(
            "SELECT title FROM review_items "
            "WHERE matter_id = ? AND status = 'pending' ORDER BY created_at",
            (matter_id,),
        )
        blocked_actions = self.database.fetch_all(
            "SELECT title, blocked_reason FROM actions "
            "WHERE matter_id = ? AND status = 'open' AND flow_state = 'blocked' "
            "ORDER BY created_at",
            (matter_id,),
        )
        draft = {
            "conclusion": matter.get("summary") or matter["title"],
            "basis": [
                item["value"]
                + (f"（{item['source_locator']}）" if item.get("source_locator") else "")
                for item in evidence
            ],
            "gaps": [],
            "risks": [
                item.get("blocked_reason") or f"{item['title']}当前受阻"
                for item in blocked_actions
            ],
            "steps": [],
            "questions": [item["title"] for item in reviews],
            "reply_draft": {"purpose": "", "text": ""},
        }
        existing = self.database.fetch_one(
            "SELECT * FROM work_packages WHERE matter_id = ?", (matter_id,)
        )
        package_id = existing["id"] if existing else new_id("package")
        version = int(existing["version"]) + 1 if existing else 1
        now = utc_now()
        if existing and now <= existing["updated_at"]:
            now = (
                datetime.fromisoformat(existing["updated_at"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO work_packages "
                "(id, matter_id, version, draft_json, source_fingerprint, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 'draft', ?, ?) "
                "ON CONFLICT(matter_id) DO UPDATE SET version = excluded.version, "
                "draft_json = excluded.draft_json, source_fingerprint = excluded.source_fingerprint, "
                "status = 'draft', updated_at = excluded.updated_at",
                (
                    package_id,
                    matter_id,
                    version,
                    json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                    fingerprint,
                    now,
                    now,
                ),
            )
        payload = {"version": version, "source_fingerprint": fingerprint}
        self.database.audit(
            new_id("audit"), actor, "work_package.generated", "work_package", package_id,
            matter_id, payload,
        )
        self.record_matter_event(
            matter_id, "work_package.generated", actor, "work_package", package_id,
            "事项工作包已生成", payload,
        )
        package = self.get_work_package(matter_id)
        if not package:
            raise RuntimeError("工作包生成失败")
        return package

    def prepare_attention_work_packages(self, actor: str) -> dict[str, Any]:
        rows = self.database.fetch_all(
            "SELECT DISTINCT m.id FROM matters m "
            "LEFT JOIN work_packages wp ON wp.matter_id = m.id "
            "WHERE m.status != 'completed' AND wp.id IS NULL AND ("
            "EXISTS (SELECT 1 FROM actions a WHERE a.matter_id = m.id AND a.status = 'open') "
            "OR EXISTS (SELECT 1 FROM review_items r WHERE r.matter_id = m.id AND r.status = 'pending') "
            "OR EXISTS (SELECT 1 FROM reminders n WHERE n.matter_id = m.id "
            "AND n.status IN ('open', 'scheduled')) "
            "OR EXISTS (SELECT 1 FROM materials x JOIN jobs j ON j.material_id = x.id "
            "WHERE x.matter_id = m.id AND j.status IN ('retryable_failed', 'needs_review'))"
            ") ORDER BY m.updated_at DESC"
        )
        prepared = 0
        failed: list[dict[str, str]] = []
        for row in rows:
            try:
                self.generate_work_package(row["id"], actor)
                prepared += 1
            except Exception:
                failed.append({"matter_id": row["id"], "message": "办理方案准备失败"})
        return {"prepared": prepared, "failed": failed}

    def update_work_package(
        self,
        matter_id: str,
        draft: dict[str, Any],
        expected_updated_at: str | None,
        actor: str,
    ) -> dict[str, Any]:
        package = self.database.fetch_one(
            "SELECT * FROM work_packages WHERE matter_id = ?", (matter_id,)
        )
        if not package:
            if not self.database.fetch_one("SELECT id FROM matters WHERE id = ?", (matter_id,)):
                raise KeyError("事项不存在")
            raise KeyError("工作包不存在")
        if expected_updated_at and expected_updated_at != package["updated_at"]:
            raise StaleWriteError("工作包已被更新，请刷新后再保存")
        if not isinstance(draft, dict) or not isinstance(draft.get("steps", []), list):
            raise ValueError("工作包草稿格式不正确")
        if any(not isinstance(step, dict) for step in draft.get("steps", [])):
            raise ValueError("工作包步骤格式不正确")
        now = utc_now()
        if now <= package["updated_at"]:
            now = (
                datetime.fromisoformat(package["updated_at"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        version = int(package["version"]) + 1
        current_fingerprint = self._work_package_fingerprint(matter_id)
        next_status = (
            "stale"
            if package["status"] == "stale"
            or current_fingerprint != package["source_fingerprint"]
            else "draft"
        )
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE work_packages SET version = ?, draft_json = ?, status = ?, "
                "updated_at = ? WHERE id = ? AND updated_at = ?",
                (
                    version,
                    json.dumps(draft, ensure_ascii=False, separators=(",", ":")),
                    next_status,
                    now,
                    package["id"],
                    package["updated_at"],
                ),
            )
            if updated.rowcount != 1:
                raise StaleWriteError("工作包已被更新，请刷新后再保存")
        payload = {"version": version, "step_count": len(draft.get("steps", []))}
        self.database.audit(
            new_id("audit"), actor, "work_package.updated", "work_package", package["id"],
            matter_id, payload,
        )
        self.record_matter_event(
            matter_id, "work_package.updated", actor, "work_package", package["id"],
            "事项工作包已修改", payload,
        )
        result = self.get_work_package(matter_id)
        if not result:
            raise RuntimeError("工作包更新失败")
        return result

    def apply_work_package(
        self,
        matter_id: str,
        step_indexes: list[int],
        expected_updated_at: str | None,
        actor: str,
    ) -> dict[str, Any]:
        package = self.database.fetch_one(
            "SELECT * FROM work_packages WHERE matter_id = ?", (matter_id,)
        )
        if not package:
            if not self.database.fetch_one("SELECT id FROM matters WHERE id = ?", (matter_id,)):
                raise KeyError("事项不存在")
            raise KeyError("工作包不存在")
        if expected_updated_at and expected_updated_at != package["updated_at"]:
            raise StaleWriteError("工作包已被更新，请刷新后再应用")
        if package["status"] != "applied" and (
            package["status"] == "stale"
            or package["source_fingerprint"] != self._work_package_fingerprint(matter_id)
        ):
            raise StaleWriteError("事项内容已有变化，请重新生成办理方案后再采纳")
        draft = parse_json(package["draft_json"], {})
        steps = draft.get("steps", []) if isinstance(draft, dict) else []
        indexes = list(dict.fromkeys(step_indexes))
        if any(not isinstance(index, int) or isinstance(index, bool) for index in indexes):
            raise ValueError("工作包步骤序号必须是整数")
        if any(index < 0 or index >= len(steps) for index in indexes):
            raise ValueError("工作包步骤序号超出范围")
        now = utc_now()
        if now <= package["updated_at"]:
            now = (
                datetime.fromisoformat(package["updated_at"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        created: list[dict[str, Any]] = []
        with self.database.connect() as connection:
            for index in indexes:
                step = steps[index]
                source_key = f"work_package:{package['id']}:v{package['version']}:step:{index}"
                if connection.execute(
                    "SELECT id FROM actions WHERE matter_id = ? AND created_by = ?",
                    (matter_id, source_key),
                ).fetchone():
                    continue
                kind = str(step.get("kind") or "task")
                if kind not in ACTION_KINDS:
                    kind = "task"
                title = str(step.get("title") or "").strip()
                if not title:
                    raise ValueError("选中的工作包步骤缺少标题")
                flow_state = str(step.get("flow_state") or default_flow_state(kind))
                if flow_state not in FLOW_STATES:
                    flow_state = default_flow_state(kind)
                action_id = new_id("action")
                connection.execute(
                    "INSERT INTO actions "
                    "(id, matter_id, kind, title, detail, status, owner, due_date, created_by, "
                    "created_at, updated_at, flow_state, waiting_on, blocked_reason, "
                    "next_follow_up_at, schedule_basis, estimated_minutes, completion_evidence) "
                    "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, 'user_entered', ?, '[]')",
                    (
                        action_id,
                        matter_id,
                        kind,
                        title[:200],
                        str(step.get("detail") or "")[:2000],
                        str(step.get("owner") or "")[:160] or None,
                        step.get("due_date") or None,
                        source_key,
                        now,
                        now,
                        flow_state,
                        str(step.get("waiting_on") or "")[:160],
                        str(step.get("blocked_reason") or "")[:500],
                        step.get("next_follow_up_at") or None,
                        step.get("estimated_minutes")
                        if isinstance(step.get("estimated_minutes"), int)
                        and step["estimated_minutes"] > 0
                        else None,
                    ),
                )
                upsert_action_suggestions(
                    connection,
                    action_id,
                    str(step.get("source_material_id") or "") or None,
                    step.get("assignee_suggestions")
                    if isinstance(step.get("assignee_suggestions"), list)
                    else [],
                    now,
                )
                created.append({"id": action_id, "step_index": index, "title": title[:200]})
            updated = connection.execute(
                "UPDATE work_packages SET status = 'applied', updated_at = ? "
                "WHERE id = ? AND updated_at = ?",
                (now, package["id"], package["updated_at"]),
            )
            if updated.rowcount != 1:
                raise StaleWriteError("工作包已被更新，请刷新后再应用")
            for action in created:
                payload = {
                    "work_package_id": package["id"],
                    "step_index": action["step_index"],
                }
                self.database.audit(
                    new_id("audit"),
                    actor,
                    "action.created",
                    "action",
                    action["id"],
                    matter_id,
                    payload,
                    connection=connection,
                )
                self.record_matter_event(
                    matter_id,
                    "action.created",
                    actor,
                    "action",
                    action["id"],
                    f"已从工作包创建行动：{action['title']}",
                    payload,
                    connection=connection,
                )
            payload = {
                "selected_step_indexes": indexes,
                "created_action_ids": [action["id"] for action in created],
            }
            self.database.audit(
                new_id("audit"),
                actor,
                "work_package.applied",
                "work_package",
                package["id"],
                matter_id,
                payload,
                connection=connection,
            )
            self.record_matter_event(
                matter_id,
                "work_package.applied",
                actor,
                "work_package",
                package["id"],
                "事项工作包已应用",
                payload,
                connection=connection,
            )
        result = self.get_work_package(matter_id)
        if not result:
            raise RuntimeError("工作包应用失败")
        return {"work_package": result, "created_actions": public_labels(created)}

    def matter_timeline(self, matter_id: str, limit: int = 200) -> list[dict[str, Any]]:
        if not self.database.fetch_one("SELECT id FROM matters WHERE id = ?", (matter_id,)):
            raise KeyError("事项不存在")
        rows = self.database.fetch_all(
            "SELECT * FROM matter_events WHERE matter_id = ? "
            "ORDER BY created_at ASC, id ASC LIMIT ?",
            (matter_id, min(max(limit, 1), 500)),
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            event = dict(row)
            event["payload"] = parse_json(event.pop("payload_json", "{}"), {})
            event["type"] = event["event_type"]
            event["source"] = {
                "actor": event.get("actor"),
                "object_type": event.get("object_type"),
                "object_id": event.get("object_id"),
            }
            events.append(event)
        return public_labels(events)

    def update_matter(
        self, matter_id: str, changes: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
        if not matter:
            raise KeyError("事项不存在")
        expected_updated_at = changes.pop("expected_updated_at", None)
        reason = str(changes.pop("reason", "") or "")[:500]
        if expected_updated_at and expected_updated_at != matter.get("updated_at"):
            raise StaleWriteError("事项已被更新，请刷新后再保存")
        if changes.get("status") == "completed":
            raise ValueError("请先完成收尾检查，再关闭事项")
        allowed = {
            "title",
            "summary",
            "goal",
            "completion_criteria",
            "owner",
            "target_date",
            "status",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            raise ValueError("请提供需要修改的事项内容")
        if values.get("status") not in {None, "active"}:
            raise ValueError("事项状态只能重新打开为正在推进")
        if "target_date" in values:
            values["target_date"] = values["target_date"] or None
            if values["target_date"]:
                try:
                    date.fromisoformat(str(values["target_date"]))
                except ValueError as exc:
                    raise ValueError("要求闭环日期格式不正确") from exc
        for field in ("title", "summary", "goal", "completion_criteria", "owner"):
            if field in values:
                values[field] = str(values[field] or "").strip()
        if "title" in values and not values["title"]:
            raise ValueError("事项标题不能为空")
        now = utc_now()
        if now <= matter["updated_at"]:
            now = (
                datetime.fromisoformat(matter["updated_at"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        before = {key: matter.get(key) for key in values}
        assignments = ", ".join(f"{key} = ?" for key in values)
        payload = {"before": before, "after": values, "reason": reason}
        with self.database.connect() as connection:
            updated_row = connection.execute(
                f"UPDATE matters SET {assignments}, status_override = 1, updated_at = ? "
                "WHERE id = ? AND updated_at = ?",
                (*values.values(), now, matter_id, matter["updated_at"]),
            )
            if updated_row.rowcount != 1:
                raise StaleWriteError("事项已被更新，请刷新后再保存")
            connection.execute(
                "UPDATE work_packages SET status = 'stale', updated_at = ? "
                "WHERE matter_id = ? AND status = 'draft'",
                (now, matter_id),
            )
            self.database.audit(
                new_id("audit"),
                actor,
                "matter.updated",
                "matter",
                matter_id,
                matter_id,
                payload,
                connection=connection,
            )
            self.record_matter_event(
                matter_id,
                "matter.updated",
                actor,
                "matter",
                matter_id,
                "事项内容已修改",
                payload,
                connection=connection,
            )
        updated = self.get_matter(matter_id)
        if not updated:
            raise RuntimeError("事项更新失败")
        return updated

    def close_preview(self, matter_id: str) -> dict[str, Any]:
        matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
        if not matter:
            raise KeyError("事项不存在")
        actions = self.database.fetch_all(
            "SELECT id, title, status, flow_state, completion_evidence FROM actions "
            "WHERE matter_id = ? AND status = 'open' ORDER BY created_at",
            (matter_id,),
        )
        reminders = self.database.fetch_all(
            "SELECT id, title, status, due_at FROM reminders WHERE matter_id = ? "
            "AND status NOT IN ('done', 'dismissed') AND kind != 'review' ORDER BY created_at",
            (matter_id,),
        )
        reviews = self.database.fetch_all(
            "SELECT id, title, status FROM review_items WHERE matter_id = ? "
            "AND status = 'pending' ORDER BY created_at",
            (matter_id,),
        )
        assignee_reviews = self.database.fetch_all(
            "SELECT aa.id, aa.action_id, a.title, aa.status FROM action_assignees aa "
            "JOIN actions a ON a.id = aa.action_id WHERE a.matter_id = ? "
            "AND aa.status = 'pending' ORDER BY aa.created_at",
            (matter_id,),
        )
        active_jobs = self.database.fetch_all(
            "SELECT j.id, j.status FROM jobs j JOIN materials x ON x.id = j.material_id "
            "WHERE x.matter_id = ? AND j.status IN "
            "('queued', 'claimed', 'processing', 'retryable_failed', 'needs_review')",
            (matter_id,),
        )
        blockers = {
            "actions": actions,
            "reminders": reminders,
            "reviews": reviews,
            "assignee_reviews": assignee_reviews,
            "active_jobs": active_jobs,
        }
        return public_labels(
            {
                "matter_id": matter_id,
                "matter_status": matter["status"],
                "updated_at": matter["updated_at"],
                "can_close": not any(blockers.values()) and matter["status"] != "completed",
                "blockers": blockers,
                "blocker_count": sum(len(items) for items in blockers.values()),
            }
        )

    def close_matter(
        self,
        matter_id: str,
        completion_note: str,
        expected_updated_at: str | None,
        actor: str,
    ) -> dict[str, Any]:
        note = completion_note.strip()
        if not note:
            raise ValueError("请填写事项收尾说明")
        matter = self.database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter_id,))
        if not matter:
            raise KeyError("事项不存在")
        if expected_updated_at and expected_updated_at != matter.get("updated_at"):
            raise StaleWriteError("事项已被更新，请刷新收尾检查后再关闭")
        preview = self.close_preview(matter_id)
        if matter["status"] == "completed":
            return self.get_matter(matter_id) or matter
        if not preview["can_close"]:
            raise ValueError("仍有未处理的行动、提醒、待确认项或后台任务")
        now = utc_now()
        payload = {
            "before": matter["status"],
            "after": "completed",
            "completion_note": note,
        }
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            locked_row = connection.execute(
                "SELECT * FROM matters WHERE id = ?", (matter_id,)
            ).fetchone()
            if not locked_row:
                raise KeyError("事项不存在")
            locked_matter = dict(locked_row)
            if expected_updated_at and expected_updated_at != locked_matter.get("updated_at"):
                raise StaleWriteError("事项已被更新，请刷新收尾检查后再关闭")
            locked_preview = self.close_preview(matter_id)
            if not locked_preview["can_close"]:
                raise ValueError("仍有未处理的行动、提醒、待确认项或后台任务")
            updated = connection.execute(
                "UPDATE matters SET status = 'completed', status_override = 1, updated_at = ? "
                "WHERE id = ? AND updated_at = ?",
                (now, matter_id, locked_matter["updated_at"]),
            )
            if updated.rowcount != 1:
                raise StaleWriteError("事项已被更新，请重新执行收尾检查")
            self.database.audit(
                new_id("audit"),
                actor,
                "matter.closed",
                "matter",
                matter_id,
                matter_id,
                payload,
                connection=connection,
            )
            self.record_matter_event(
                matter_id,
                "matter.closed",
                actor,
                "matter",
                matter_id,
                "事项已完成收尾检查并关闭",
                payload,
                connection=connection,
            )
        closed = self.get_matter(matter_id)
        if not closed:
            raise RuntimeError("事项关闭失败")
        return closed

    def add_matter_progress(
        self, matter_id: str, summary: str, detail: str, actor: str
    ) -> dict[str, Any]:
        matter = self.database.fetch_one("SELECT id FROM matters WHERE id = ?", (matter_id,))
        if not matter:
            raise KeyError("事项不存在")
        summary = summary.strip()
        detail = detail.strip()
        if not summary:
            raise ValueError("请填写本次推进情况")
        event = self.record_matter_event(
            matter_id,
            "progress.note",
            actor,
            "matter",
            matter_id,
            summary,
            {"detail": detail},
        )
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE matters SET updated_at = ? WHERE id = ?", (now, matter_id)
            )
        self.database.audit(
            new_id("audit"), actor, "matter.progress.recorded", "matter", matter_id,
            matter_id, {"summary": summary, "detail": detail},
        )
        if not event:
            raise RuntimeError("推进记录保存失败")
        return event

    def _rebuild_search_index(self) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM search_fts")

            matters = connection.execute(
                "SELECT id, title, summary, created_at FROM matters"
            ).fetchall()
            connection.executemany(
                "INSERT INTO search_fts "
                "(entity_id, entity_type, matter_id, title, body, created_at) "
                "VALUES (?, 'matter', ?, ?, ?, ?)",
                [
                    (row["id"], row["id"], row["title"], row["summary"] or "", row["created_at"])
                    for row in matters
                ],
            )

            actions = connection.execute(
                "SELECT id, matter_id, title, detail, waiting_on, blocked_reason, "
                "completion_evidence, created_at FROM actions"
            ).fetchall()
            connection.executemany(
                "INSERT INTO search_fts "
                "(entity_id, entity_type, matter_id, title, body, created_at) "
                "VALUES (?, 'action', ?, ?, ?, ?)",
                [
                    (
                        row["id"],
                        row["matter_id"],
                        row["title"],
                        " ".join(
                            item
                            for item in (
                                row["detail"] or "",
                                row["waiting_on"] or "",
                                row["blocked_reason"] or "",
                                row["completion_evidence"] or "",
                            )
                            if item
                        ),
                        row["created_at"],
                    )
                    for row in actions
                ],
            )

            materials = connection.execute(
                "SELECT id, matter_id, filename, text_note, source_type, received_at "
                "FROM materials WHERE source_type NOT IN "
                "('wechat_auto', 'wechat_channel', 'wecom_channel', 'workbuddy_channel') "
                "OR matter_id IS NOT NULL"
            ).fetchall()
            connection.executemany(
                "INSERT INTO search_fts "
                "(entity_id, entity_type, matter_id, title, body, created_at) "
                "VALUES (?, 'material', ?, ?, ?, ?)",
                [
                    (
                        row["id"],
                        row["matter_id"],
                        row["filename"] or "材料",
                        row["text_note"] or "",
                        row["received_at"],
                    )
                    for row in materials
                ],
            )

            evidence = connection.execute(
                "SELECT id, matter_id, field_type, value, quote, source_locator, created_at "
                "FROM evidence WHERE status NOT IN ('rejected', 'superseded')"
            ).fetchall()
            connection.executemany(
                "INSERT INTO search_fts "
                "(entity_id, entity_type, matter_id, title, body, created_at) "
                "VALUES (?, 'evidence', ?, ?, ?, ?)",
                [
                    (
                        row["id"],
                        row["matter_id"],
                        row["field_type"],
                        " ".join(
                            item
                            for item in (
                                row["value"] or "",
                                row["quote"] or "",
                                row["source_locator"] or "",
                            )
                            if item
                        ),
                        row["created_at"],
                    )
                    for row in evidence
                ],
            )

            emails = connection.execute(
                "SELECT id, matter_id, subject, summary, reason, evidence_json, "
                "COALESCE(sent_at, created_at) AS occurred_at "
                "FROM email_messages WHERE status = 'active' AND classification = 'work'"
            ).fetchall()
            connection.executemany(
                "INSERT INTO search_fts "
                "(entity_id, entity_type, matter_id, title, body, created_at) "
                "VALUES (?, 'email', ?, ?, ?, ?)",
                [
                    (
                        row["id"],
                        row["matter_id"],
                        row["subject"] or "工作邮件",
                        " ".join(
                            item
                            for item in (
                                row["summary"] or "",
                                row["reason"] or "",
                                row["evidence_json"] or "",
                            )
                            if item
                        ),
                        row["occurred_at"],
                    )
                    for row in emails
                ],
            )

            policies = connection.execute(
                "SELECT id, title, publisher, topic, scope, summary, requirements_json, "
                "updated_at FROM company_policies"
            ).fetchall()
            connection.executemany(
                "INSERT INTO search_fts "
                "(entity_id, entity_type, matter_id, title, body, created_at) "
                "VALUES (?, 'policy', NULL, ?, ?, ?)",
                [
                    (
                        row["id"],
                        row["title"],
                        " ".join(
                            item
                            for item in (
                                row["publisher"] or "",
                                row["topic"] or "",
                                row["scope"] or "",
                                row["summary"] or "",
                                row["requirements_json"] or "",
                            )
                            if item
                        ),
                        row["updated_at"],
                    )
                    for row in policies
                ],
            )

            connection.execute(
                "INSERT INTO search_index_state(id, version) VALUES (1, 1) "
                "ON CONFLICT(id) DO UPDATE SET version = excluded.version"
            )

    def ensure_search_index(self) -> None:
        state = self.database.fetch_one(
            "SELECT version FROM search_index_state WHERE id = 1"
        )
        if not state or int(state.get("version") or 0) != 1:
            self._rebuild_search_index()

    def search(
        self,
        query: str,
        limit: int = 50,
        *,
        source: str = "",
        status: str = "",
        date_from: str = "",
        date_to: str = "",
        amount: str = "",
        include_answer: bool = False,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            return {
                "query": "",
                "items": [],
                "answer": {"facts": [], "inferences": [], "missing": [], "sources": []},
            }

        allowed_sources = {"matter", "action", "material", "evidence", "email", "policy"}
        if source and source not in allowed_sources:
            raise ValueError("不支持的来源筛选")
        for value in (date_from, date_to):
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError("筛选日期格式不正确") from exc

        terms = [
            term
            for term in re.findall(r"[\w\u3400-\u9fff]+", query, re.UNICODE)
            if term
        ]
        if not terms:
            return {
                "query": query,
                "items": [],
                "answer": {
                    "facts": [],
                    "inferences": [],
                    "missing": ["没有识别到可检索的关键词"],
                    "sources": [],
                },
            }

        match_query = " AND ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
        )
        rows = self.database.fetch_all(
            "SELECT entity_id, entity_type, matter_id, title, body, created_at, "
            "snippet(search_fts, 4, '<mark>', '</mark>', '…', 12) AS snippet, "
            "bm25(search_fts) AS relevance FROM search_fts WHERE search_fts MATCH ? "
            "ORDER BY CASE WHEN lower(title) = lower(?) THEN 0 "
            "WHEN lower(title) LIKE lower(?) THEN 1 ELSE 2 END, relevance, "
            "created_at DESC, entity_type ASC, entity_id ASC LIMIT 200",
            (match_query, query, f"%{query}%"),
        )
        if not rows:
            like_terms = [
                term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                for term in terms
            ]
            like_clause = " AND ".join(
                "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\')"
                for _ in like_terms
            )
            like_params = [
                value
                for term in like_terms
                for value in (f"%{term}%", f"%{term}%")
            ]
            rows = self.database.fetch_all(
                "SELECT entity_id, entity_type, matter_id, title, body, created_at, "
                "body AS snippet, 0 AS relevance FROM search_fts WHERE "
                f"{like_clause} ORDER BY CASE WHEN lower(title) = lower(?) THEN 0 "
                "WHEN lower(title) LIKE lower(?) THEN 1 ELSE 2 END, "
                "created_at DESC, entity_type ASC, entity_id ASC LIMIT 200",
                (*like_params, query, f"%{query}%"),
            )
        status_maps = {
            "matter": {
                row["id"]: row["status"]
                for row in self.database.fetch_all("SELECT id, status FROM matters")
            },
            "action": {
                row["id"]: row["status"]
                for row in self.database.fetch_all("SELECT id, status FROM actions")
            },
            "material": {
                row["id"]: row["status"]
                for row in self.database.fetch_all("SELECT id, status FROM materials")
            },
            "evidence": {
                row["id"]: row["status"]
                for row in self.database.fetch_all("SELECT id, status FROM evidence")
            },
            "email": {
                row["id"]: row["status"]
                for row in self.database.fetch_all("SELECT id, status FROM email_messages")
            },
            "policy": {
                row["id"]: row["status"]
                for row in self.database.fetch_all("SELECT id, status FROM company_policies")
            },
        }
        labels = {
            "matter": "事项",
            "action": "行动",
            "material": "材料",
            "evidence": "依据",
            "email": "邮件",
            "policy": "公司规定",
        }
        wanted_amount = re.sub(r"[,，\s]", "", amount)
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item_status = status_maps.get(item["entity_type"], {}).get(item["entity_id"], "")
            created_date = str(item.get("created_at") or "")[:10]
            if source and item["entity_type"] != source:
                continue
            if status and item_status != status:
                continue
            if date_from and created_date < date_from:
                continue
            if date_to and created_date > date_to:
                continue
            searchable_text = re.sub(
                r"[,，\s]", "", f"{item.get('title') or ''}{item.get('body') or ''}"
            )
            if wanted_amount and wanted_amount not in searchable_text:
                continue

            item["status"] = item_status
            item["type_label"] = labels.get(item["entity_type"], "记录")
            item["searchable"] = True
            item["summary"] = re.sub(
                r"<[^>]+>", "", item.get("snippet") or item.get("body") or ""
            )[:260]
            item["href"] = (
                f"#/matters/{item['matter_id']}"
                if item.get("matter_id")
                else "#/policies"
                if item["entity_type"] == "policy"
                else "#/search"
            )
            item["sort_reason"] = (
                "标题完全匹配"
                if str(item.get("title") or "").casefold() == query.casefold()
                else "标题包含关键词"
                if query.casefold() in str(item.get("title") or "").casefold()
                else "正文包含关键词"
            )
            items.append(item)
            if len(items) >= min(max(limit, 1), 200):
                break

        sources = [
            {
                "type": item["type_label"],
                "title": item["title"],
                "summary": item["summary"],
                "href": item["href"],
            }
            for item in items[:4]
        ]
        facts = [
            f"{item['type_label']}：{item['title']}。{item['summary']}".strip("。")
            for item in items[:4]
        ]
        answer = {
            "facts": facts,
            "inferences": [],
            "missing": [] if items else ["当前允许检索的资料中没有找到直接证据"],
            "sources": sources,
        } if include_answer else None
        return public_labels({"query": query, "items": items, "answer": answer})

    def today_brief(self, brief_date: str | None = None) -> dict[str, Any]:
        self.refresh_reminders()
        target = brief_date or datetime.now(UTC).date().isoformat()
        try:
            target_date = date.fromisoformat(target)
        except ValueError as exc:
            raise ValueError("日期格式不正确") from exc

        actions = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id WHERE a.status = 'open'"
        )
        self._attach_action_assignees(actions)
        reviews = self.list_reviews("pending")
        assignee_reviews = self.list_assignee_reviews("pending")
        reminders = self.database.fetch_all(
            "SELECT r.*, m.title AS matter_title FROM reminders r "
            "LEFT JOIN matters m ON m.id = r.matter_id "
            "WHERE r.status IN ('open', 'scheduled')"
        )

        now = datetime.now(UTC)
        attention, workflow_stats = build_workflow_stats(
            actions,
            reviews,
            assignee_reviews,
            reminders,
            target_date,
            now,
        )
        watermark_source = {
            "actions": [
                [
                    item.get("id"),
                    item.get("status"),
                    item.get("flow_state"),
                    item.get("waiting_on"),
                    item.get("blocked_reason"),
                    item.get("next_follow_up_at"),
                    item.get("pinned_at"),
                        item.get("snoozed_until"),
                        item.get("due_date"),
                        item.get("schedule_basis"),
                        item.get("updated_at"),
                ]
                for item in sorted(actions, key=lambda row: str(row.get("id") or ""))
            ],
            "reviews": [
                [item.get("id"), item.get("status"), item.get("resolved_at")]
                for item in sorted(reviews, key=lambda row: str(row.get("id") or ""))
                ],
                "assignee_reviews": [
                    [
                        item.get("action_id"),
                        item.get("status"),
                        item.get("updated_at"),
                    ]
                    for item in assignee_reviews
                ],
                "reminders": [
                [item.get("id"), item.get("status"), item.get("updated_at")]
                for item in sorted(reminders, key=lambda row: str(row.get("id") or ""))
            ],
        }
        source_watermark = hashlib.sha256(
            json.dumps(watermark_source, sort_keys=True).encode("utf-8")
        ).hexdigest()[:20]
        existing = self.database.fetch_one(
            "SELECT * FROM daily_briefs WHERE brief_date = ?", (target,)
        )
        if existing:
            cached = parse_json(existing.get("payload_json"), {})
            if (
                cached.get("source_watermark") == source_watermark
                and cached.get("attention_version") == 3
                and "attention" in cached
                and "attention_counts" in cached
            ):
                return public_labels(
                    {
                        "id": existing["id"],
                        "brief_date": target,
                        "generated_at": existing["generated_at"],
                        **cached,
                    }
                )

        self_person_ids = {
            row["id"]
            for row in self.database.fetch_all("SELECT id FROM people WHERE is_self = 1")
        }

        def parsed_date(value: str | None) -> date | None:
            if not value:
                return None
            try:
                return date.fromisoformat(str(value)[:10])
            except ValueError:
                return None

        def parsed_time(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

        def is_self_action(item: dict[str, Any]) -> bool:
            if any(
                assignee.get("person_id") in self_person_ids
                for assignee in item.get("assignees", [])
            ):
                return True
            owner = str(item.get("owner") or "")
            return any(label in owner for label in ("Frank", "傅京晖", "我自己"))

        def reason_and_rank(item: dict[str, Any]) -> tuple[tuple[Any, ...], str]:
            due = parsed_date(item.get("due_date"))
            follow_up = parsed_time(item.get("next_follow_up_at"))
            pinned = bool(item.get("pinned_at"))
            overdue = due is not None and due < target_date
            high_risk = item.get("kind") == "risk" or item.get("flow_state") == "blocked"
            due_soon = due is not None and due <= target_date + timedelta(days=2)
            follow_up_due = (
                item.get("flow_state") == "waiting"
                and follow_up is not None
                and follow_up <= now
            )
            if pinned:
                bucket, reason = 0, "你已固定为最高优先"
            elif overdue:
                bucket = 1
                reason = f"已逾期 {(target_date - due).days} 天"
            elif high_risk:
                bucket, reason = 1, "存在阻塞或风险，需要尽快处理"
            elif due_soon:
                days = (due - target_date).days if due else 0
                bucket = 2
                reason = "今天到期" if days == 0 else f"距离截止还有 {days} 天"
            elif is_self_action(item):
                bucket, reason = 3, "已确认由你负责"
            elif follow_up_due:
                bucket, reason = 4, "已到约定跟进时间"
            else:
                bucket, reason = 5, "按截止时间和最近变化排序"
            return (
                bucket,
                due.isoformat() if due else "9999-12-31",
                item.get("updated_at") or "",
                item["id"],
            ), reason

        execution: list[dict[str, Any]] = []
        waiting: list[dict[str, Any]] = []
        risks: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []

        for item in actions:
            snoozed = parsed_time(item.get("snoozed_until"))
            follow_up = parsed_time(item.get("next_follow_up_at"))
            rank, reason = reason_and_rank(item)
            item["sort_reason"] = reason
            item["pinned"] = bool(item.get("pinned_at"))
            item["rank"] = rank
            item["evidence_refs"] = [
                value
                for value in (item.get("evidence_id"), item.get("material_id"))
                if value
            ]

            is_waiting = item.get("flow_state") == "waiting"
            follow_up_due = is_waiting and follow_up is not None and follow_up <= now
            if is_waiting and not follow_up_due:
                waiting.append(item)
            elif snoozed is None or snoozed <= now:
                execution.append(item)

            if (
                item.get("kind") == "risk"
                or item.get("flow_state") == "blocked"
                or (
                    parsed_date(item.get("due_date")) is not None
                    and parsed_date(item.get("due_date")) < target_date
                )
            ):
                risks.append(item)
            if item.get("flow_state") == "needs_decision":
                decisions.append(item)

        execution.sort(key=lambda item: item["rank"])
        waiting.sort(
            key=lambda item: (
                item.get("next_follow_up_at") or "9999-12-31",
                item.get("due_date") or "9999-12-31",
                item["id"],
            )
        )
        risks.sort(key=lambda item: item["rank"])
        review_decisions = [
            {
                "id": item["id"],
                "item_type": "review",
                "matter_id": item.get("matter_id"),
                "matter_title": item.get("matter_title"),
                "title": item.get("title") or "需要确认",
                "kind": item.get("kind"),
                "created_at": item.get("created_at"),
                "sort_reason": "需要你确认后才能继续",
                "payload": item.get("payload") or {},
            }
            for item in reviews
        ]
        decisions.extend(review_decisions)

        for collection in (execution, waiting, risks):
            for item in collection:
                item.pop("rank", None)

        payload = {
            "headline": f"{target} 工作简报",
            "source_watermark": source_watermark,
            "attention_version": 3,
            "now": execution[0] if execution else None,
            "next": execution[1:4],
            "waiting": waiting[:12],
            "risks": risks[:12],
            "decisions": decisions[:20],
            "attention": attention,
            "attention_counts": workflow_stats["attention_counts"],
            "counts": {
                **{
                    key: value
                    for key, value in workflow_stats.items()
                    if key != "attention_counts"
                },
                "waiting": len(waiting),
                "risks": len(risks),
            },
        }
        generated_at = utc_now()
        brief_id = existing["id"] if existing else new_id("brief")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO daily_briefs (id, brief_date, generated_at, payload_json) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(brief_date) DO UPDATE SET "
                "generated_at = excluded.generated_at, payload_json = excluded.payload_json",
                (
                    brief_id,
                    target,
                    generated_at,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ),
            )
        return public_labels(
            {
                "id": brief_id,
                "brief_date": target,
                "generated_at": generated_at,
                **payload,
            }
        )

    def activity_receipts(
        self, limit: int = 100, matter_id: str | None = None
    ) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if matter_id:
            where = "WHERE matter_id = ?"
            params.append(matter_id)
        params.append(min(max(limit, 1), 500))
        rows = self.database.fetch_all(
            "SELECT * FROM audit_events "
            f"{where} ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        )
        labels = {
            "material.received": "已接收材料",
            "material.assigned": "已归入事项",
            "material.retracted": "已撤销材料",
            "job.completed": "已完成材料整理",
            "action.created": "已建立行动",
            "action.completed": "已确认行动完成",
            "action.planning_updated": "已调整行动安排",
            "review.resolved": "已处理待确认内容",
            "wechat.candidate.accepted": "已采纳聊天线索",
            "wechat.candidate.merged": "已合并聊天线索",
            "wechat.candidate.ignored": "已忽略无关聊天",
            "policy.updated": "已更新公司规定",
            "policy.evidence_merged": "已补充规定依据",
        }

        def label_for(code: str) -> str:
            if code in labels:
                return labels[code]
            if "merged" in code:
                return "已自动合并相关内容"
            if "ignored" in code or "filtered" in code:
                return "已过滤无关内容"
            if "completed" in code:
                return "已完成处理"
            if "updated" in code:
                return "已更新记录"
            return "已记录工作台操作"

        for row in rows:
            code = str(row.get("action") or "")
            row["metadata"] = parse_json(row.pop("metadata_json", "{}"), {})
            row["action_code"] = code
            row["action"] = label_for(code)
            row["summary"] = row["action"]
            row["source"] = row.get("actor")
        return public_labels(rows)

    def resolve_reviews(
        self,
        review_ids: list[str],
        resolution: str,
        note: str,
        actor: str,
    ) -> dict[str, Any]:
        unique_ids = list(dict.fromkeys(item for item in review_ids if item))
        if not unique_ids:
            raise ValueError("缺少待确认项")
        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.database.fetch_all(
            f"SELECT * FROM review_items WHERE id IN ({placeholders})",
            tuple(unique_ids),
        )
        if len(rows) != len(unique_ids):
            raise KeyError("部分待确认项不存在")
        if len(unique_ids) > 1:
            protected_terms = ("付款", "支付", "审批", "对外发送", "重大规定", "规定修订", "废止")
            for row in rows:
                payload = parse_json(row.get("payload_json"), {})
                text_value = " ".join(
                    (
                        str(row.get("kind") or ""),
                        str(row.get("title") or ""),
                        json.dumps(payload, ensure_ascii=False),
                    )
                )
                if any(term in text_value for term in protected_terms):
                    raise ValueError("高影响内容需要逐条确认")

        results = [
            self.resolve_review(review_id, resolution, note, actor)
            for review_id in unique_ids
        ]
        return {"updated_count": len(results), "items": results}

    def weekly_review(self) -> dict[str, Any]:
        today = datetime.now(UTC).date()
        week_start = today - timedelta(days=today.weekday())
        since = f"{week_start.isoformat()}T00:00:00Z"
        completed = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id "
            "WHERE a.status = 'done' AND a.updated_at >= ? "
            "ORDER BY a.updated_at DESC",
            (since,),
        )
        waiting = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id "
            "WHERE a.status = 'open' AND a.flow_state = 'waiting' "
            "ORDER BY COALESCE(a.next_follow_up_at, '9999-12-31'), a.id"
        )
        blocked = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id "
            "WHERE a.status = 'open' AND a.flow_state = 'blocked' "
            "ORDER BY COALESCE(a.due_date, '9999-12-31'), a.id"
        )
        overdue = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id "
            "WHERE a.status = 'open' AND a.due_date < ? "
            "ORDER BY a.due_date, a.id",
            (today.isoformat(),),
        )
        people = self.database.fetch_all(
            "SELECT p.id, p.display_name, "
            "COUNT(DISTINCT CASE WHEN a.status = 'open' THEN a.id END) AS open_count, "
            "COUNT(DISTINCT CASE WHEN a.status = 'open' AND a.due_date < ? THEN a.id END) "
            "AS overdue_count "
            "FROM people p LEFT JOIN action_assignees aa "
            "ON aa.person_id = p.id AND aa.status = 'confirmed' "
            "LEFT JOIN actions a ON a.id = aa.action_id "
            "WHERE p.enabled = 1 GROUP BY p.id, p.display_name "
            "HAVING open_count > 0 ORDER BY overdue_count DESC, open_count DESC, p.display_name",
            (today.isoformat(),),
        )
        stale_matters = self.database.fetch_all(
            "SELECT id, title, summary, updated_at FROM matters "
            "WHERE status = 'active' AND updated_at < ? "
            "ORDER BY updated_at LIMIT 20",
            ((today - timedelta(days=14)).isoformat(),),
        )
        policy_changes = self.database.fetch_all(
            "SELECT id, title, publisher, status, updated_at FROM company_policies "
            "WHERE updated_at >= ? ORDER BY updated_at DESC",
            (since,),
        )
        receipts = self.activity_receipts(500)
        automatic = [
            item
            for item in receipts
            if item.get("created_at", "") >= since
            and any(
                keyword in str(item.get("action_code") or "")
                for keyword in ("merged", "ignored", "filtered", "retracted")
            )
        ]
        return public_labels(
            {
                "week_start": week_start.isoformat(),
                "week_end": today.isoformat(),
                "completed": completed,
                "waiting": waiting,
                "blocked": blocked,
                "overdue": overdue,
                "people": people,
                "stale_matters": stale_matters,
                "policy_changes": policy_changes,
                "automatic_receipts": automatic[:30],
                "counts": {
                    "completed": len(completed),
                    "waiting": len(waiting),
                    "blocked": len(blocked),
                    "overdue": len(overdue),
                    "policy_changes": len(policy_changes),
                },
            }
        )

    def _sync_conversation_rules(self) -> None:
        rows = self.database.fetch_all(
            "SELECT session_id, source, display_name, listen_status, blocked_at, updated_at "
            "FROM wechat_conversations WHERE listen_status = 'blocked'"
        )
        now = utc_now()
        with self.database.connect() as connection:
            for row in rows:
                rule_id = "rule_" + hashlib.sha256(
                    f"conversation:{row['source']}:{row['session_id']}".encode("utf-8")
                ).hexdigest()[:24]
                pattern = json.dumps(
                    {
                        "source": row["source"],
                        "session_id": row["session_id"],
                        "display_name": row["display_name"],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                connection.execute(
                    "INSERT INTO learned_rules "
                    "(id, rule_type, description, pattern_json, source_count, enabled, "
                    "created_at, updated_at) VALUES (?, 'conversation_ignore', ?, ?, 1, 1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET description = excluded.description, "
                    "pattern_json = excluded.pattern_json, source_count = 1, "
                    "updated_at = excluded.updated_at",
                    (
                        rule_id,
                        f"不监听“{row['display_name']}”中的聊天线索",
                        pattern,
                        row.get("blocked_at") or now,
                        row.get("updated_at") or now,
                    ),
                )

    def list_learning_rules(self) -> list[dict[str, Any]]:
        self._sync_conversation_rules()
        rows = self.database.fetch_all(
            "SELECT * FROM learned_rules "
            "ORDER BY enabled DESC, source_count DESC, updated_at DESC, id"
        )
        for row in rows:
            row["pattern"] = parse_json(row.pop("pattern_json", "{}"), {})
            row["enabled"] = bool(row.get("enabled"))
        return public_labels(rows)

    def update_learning_rule(
        self, rule_id: str, enabled: bool, actor: str
    ) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM learned_rules WHERE id = ?", (rule_id,))
        if not row:
            raise KeyError("个人规则不存在")
        now = utc_now()
        pattern = parse_json(row.get("pattern_json"), {})
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE learned_rules SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), now, rule_id),
            )
            if row.get("rule_type") == "conversation_ignore" and pattern.get("session_id"):
                if enabled:
                    connection.execute(
                        "UPDATE wechat_conversations SET listen_status = 'blocked', "
                        "blocked_at = COALESCE(blocked_at, ?), updated_at = ? "
                        "WHERE session_id = ?",
                        (now, now, pattern["session_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE wechat_conversations SET listen_status = 'active', "
                        "blocked_at = NULL, listen_from = ?, updated_at = ? "
                        "WHERE session_id = ?",
                        (now, now, pattern["session_id"]),
                    )
        self.database.audit(
            new_id("audit"),
            actor,
            "learning_rule.updated",
            "learning_rule",
            rule_id,
            metadata={"enabled": enabled},
        )
        updated = self.database.fetch_one(
            "SELECT * FROM learned_rules WHERE id = ?", (rule_id,)
        )
        if not updated:
            raise RuntimeError("个人规则更新失败")
        updated["pattern"] = parse_json(updated.pop("pattern_json", "{}"), {})
        updated["enabled"] = bool(updated.get("enabled"))
        return public_labels(updated)

    def list_reviews(self, status: str = "pending") -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT r.*, m.title AS matter_title FROM review_items r "
            "LEFT JOIN matters m ON m.id = r.matter_id WHERE r.status = ? "
            "ORDER BY r.created_at DESC",
            (status,),
        )
        for row in rows:
            row["payload"] = parse_json(row.pop("payload_json", "{}"), {})
        return rows

    def resolve_review(
        self, review_id: str, resolution: str, note: str, actor: str
    ) -> dict[str, Any]:
        if resolution not in {"accepted", "rejected", "edited"}:
            raise ValueError("不支持的确认结果")
        review = self.database.fetch_one("SELECT * FROM review_items WHERE id = ?", (review_id,))
        if not review:
            raise KeyError("待确认项不存在")
        now = utc_now()
        evidence_status = "confirmed" if resolution in {"accepted", "edited"} else "rejected"
        review_payload = parse_json(review.get("payload_json"), {})
        completion_action_id = (
            str(review_payload.get("action_id") or "").strip()
            if review.get("kind") == "action_completion"
            else ""
        )
        completion_evidence = review_payload.get("evidence") or []
        if isinstance(completion_evidence, str):
            completion_evidence = [completion_evidence]
        completion_evidence = [
            str(item)[:500]
            for item in completion_evidence[:12]
            if str(item).strip()
        ]
        completion_event = {
            "source_review_id": review_id,
            "reason": str(review_payload.get("reason") or "")[:500],
            "evidence": completion_evidence,
        }
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE review_items SET status = ?, resolution_note = ?, resolved_at = ? "
                "WHERE id = ?",
                (resolution, note[:500], now, review_id),
            )
            connection.execute(
                "UPDATE reminders SET status = 'done', updated_at = ? WHERE fingerprint = ?",
                (now, f"review:{review_id}"),
            )
            if resolution == "accepted" and completion_action_id:
                connection.execute(
                    "UPDATE actions SET status = 'done', completion_evidence = ?, updated_at = ? "
                    "WHERE id = ? AND matter_id = ? AND status = 'open'",
                    (
                        json.dumps(
                            completion_evidence,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                        completion_action_id,
                        review["matter_id"],
                    ),
                )
                connection.execute(
                    "UPDATE reminders SET status = 'done', updated_at = ? "
                    "WHERE action_id = ? AND status IN ('open', 'scheduled')",
                    (now, completion_action_id),
                )
                connection.execute(
                    "UPDATE action_assignees SET status = 'superseded', updated_at = ? "
                    "WHERE action_id = ? AND status = 'pending'",
                    (now, completion_action_id),
                )
            if review["evidence_id"]:
                if resolution == "edited" and note.strip():
                    original = connection.execute(
                        "SELECT * FROM evidence WHERE id = ?", (review["evidence_id"],)
                    ).fetchone()
                    if not original:
                        raise KeyError("事实依据不存在")
                    corrected_id = new_id("evidence")
                    connection.execute(
                        "UPDATE evidence SET status = 'superseded' WHERE id = ?",
                        (review["evidence_id"],),
                    )
                    connection.execute(
                        "INSERT INTO evidence "
                        "(id, matter_id, material_id, claim_type, field_type, value, "
                        "source_locator, quote, confidence, status, created_by, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?)",
                        (
                            corrected_id,
                            original["matter_id"],
                            original["material_id"],
                            original["claim_type"],
                            original["field_type"],
                            note.strip()[:1000],
                            original["source_locator"],
                            original["quote"],
                            original["confidence"],
                            actor,
                            now,
                        ),
                    )
                    connection.execute(
                        "UPDATE review_items SET evidence_id = ? WHERE id = ?",
                        (corrected_id, review_id),
                    )
                else:
                    connection.execute(
                        "UPDATE evidence SET status = ? WHERE id = ?",
                        (evidence_status, review["evidence_id"]),
                    )
        self.database.audit(
            new_id("audit"),
            actor,
            "review.resolved",
            "review",
            review_id,
            review["matter_id"],
            {"resolution": resolution},
        )
        self.record_matter_event(
            review["matter_id"],
            "review.resolved",
            actor,
            "review",
            review_id,
            "待确认项已处理",
            {"resolution": resolution, "kind": review.get("kind")},
        )
        if resolution == "accepted" and completion_action_id:
            self.database.audit(
                new_id("audit"),
                actor,
                "action.completed",
                "action",
                completion_action_id,
                review["matter_id"],
                completion_event,
            )
            self.record_matter_event(
                review["matter_id"],
                "action.completed",
                actor,
                "action",
                completion_action_id,
                "行动完成已确认",
                completion_event,
            )
        resolved = self.database.fetch_one(
            "SELECT * FROM review_items WHERE id = ?", (review_id,)
        )
        if not resolved:
            raise RuntimeError("确认结果写入失败")
        resolved["payload"] = parse_json(resolved.pop("payload_json", "{}"), {})
        return resolved

    def refresh_reminders(self) -> int:
        created = 0
        now = utc_now()
        today = date.today().isoformat()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE reminders SET status = 'open', updated_at = ? "
                "WHERE status = 'scheduled' AND due_at IS NOT NULL "
                "AND ((length(due_at) = 10 AND date(due_at) <= date('now', 'localtime')) "
                "OR (length(due_at) > 10 AND datetime(due_at) <= datetime('now')))",
                (now,),
            )
        overdue = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id "
            "WHERE a.status = 'open' AND a.due_date IS NOT NULL "
            "AND a.due_date < ? "
            "AND a.schedule_basis IN ('material_explicit', 'user_entered')",
            (today,),
        )
        due_today = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id "
            "WHERE a.status = 'open' AND a.due_date = ? "
            "AND a.schedule_basis IN ('material_explicit', 'user_entered')",
            (today,),
        )
        follow_ups = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id "
            "WHERE a.status = 'open' AND a.next_follow_up_at IS NOT NULL "
            "AND datetime(a.next_follow_up_at) <= datetime('now') "
            "AND a.schedule_basis IN ('material_explicit', 'user_entered')",
        )
        review_rows = self.database.fetch_all(
            "SELECT r.id, r.matter_id, r.title, m.title AS matter_title "
            "FROM review_items r LEFT JOIN matters m ON m.id = r.matter_id "
            "WHERE r.status = 'pending' AND r.kind != 'schedule'"
        )
        failed_jobs = self.database.fetch_all(
            "SELECT j.id, j.material_id, j.status, j.error, x.matter_id "
            "FROM jobs j JOIN materials x ON x.id = j.material_id "
            "WHERE j.status IN ('retryable_failed', 'needs_review')"
        )
        candidates: list[dict[str, Any]] = []
        for action in overdue:
            candidates.append(
                {
                    "kind": "overdue",
                    "title": action["title"],
                    "reason": f"截止日期 {action['due_date']} 已过，仍未关闭",
                    "matter_id": action["matter_id"],
                    "action_id": action["id"],
                    "fingerprint": f"overdue:{action['id']}:{action['due_date']}",
                    "due_at": action["due_date"],
                }
            )
        for action in due_today:
            candidates.append(
                {
                    "kind": "overdue",
                    "title": action["title"],
                    "reason": f"截止日期 {action['due_date']} 是今天，仍未关闭",
                    "matter_id": action["matter_id"],
                    "action_id": action["id"],
                    "fingerprint": f"due:{action['id']}:{action['due_date']}",
                    "due_at": action["due_date"],
                }
            )
        for action in follow_ups:
            candidates.append(
                {
                    "kind": "follow_up",
                    "title": action["title"],
                    "reason": "已到约定跟进时间，仍未关闭",
                    "matter_id": action["matter_id"],
                    "action_id": action["id"],
                    "fingerprint": f"follow_up:{action['id']}:{action['next_follow_up_at']}",
                    "due_at": action["next_follow_up_at"],
                }
            )
        for review in review_rows:
            candidates.append(
                {
                    "kind": "review",
                    "title": review["title"],
                    "reason": "贾维斯 结果尚未经过人工确认",
                    "matter_id": review["matter_id"],
                    "action_id": None,
                    "fingerprint": f"review:{review['id']}",
                    "due_at": None,
                }
            )
        for job in failed_jobs:
            candidates.append(
                {
                    "kind": "processing",
                    "title": "本地处理需要关注",
                    "reason": job["error"] or "执行节点未能完成材料处理",
                    "matter_id": job["matter_id"],
                    "action_id": None,
                    "fingerprint": f"job:{job['id']}:{job['status']}",
                    "due_at": None,
                }
            )
        with self.database.connect() as connection:
            for candidate in candidates:
                try:
                    connection.execute(
                        "INSERT INTO reminders "
                        "(id, matter_id, action_id, kind, title, reason, fingerprint, due_at, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            new_id("reminder"),
                            candidate["matter_id"],
                            candidate["action_id"],
                            candidate["kind"],
                            candidate["title"],
                            candidate["reason"],
                            candidate["fingerprint"],
                            candidate["due_at"],
                            now,
                            now,
                        ),
                    )
                    created += 1
                except sqlite3.IntegrityError:
                    continue
        return created

    def overview(self) -> dict[str, Any]:
        self.refresh_reminders()
        now = datetime.now(UTC)
        open_actions = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id WHERE a.status = 'open'"
        )
        self._attach_action_assignees(open_actions)
        pending_reviews = self.list_reviews("pending")
        pending_assignee_reviews = self.list_assignee_reviews("pending")
        open_reminders = self.database.fetch_all(
            "SELECT r.*, m.title AS matter_title FROM reminders r "
            "LEFT JOIN matters m ON m.id = r.matter_id "
            "WHERE r.status IN ('open', 'scheduled')"
        )
        attention, workflow_stats = build_workflow_stats(
            open_actions,
            pending_reviews,
            pending_assignee_reviews,
            open_reminders,
            now.date(),
            now,
        )
        counts = self.database.fetch_one(
            "SELECT "
            "(SELECT COUNT(*) FROM materials WHERE status IN ('queued', 'retryable_failed')) AS queued, "
            "((SELECT COUNT(*) FROM review_items WHERE status = 'pending') + "
            "(SELECT COUNT(*) FROM action_assignees aa JOIN actions a ON a.id = aa.action_id "
            "WHERE aa.status = 'pending' AND a.status = 'open')) AS pending_review, "
            "(SELECT COUNT(*) FROM action_assignees aa JOIN actions a ON a.id = aa.action_id "
            "WHERE aa.status = 'pending' AND a.status = 'open') AS pending_assignee_review, "
            "(SELECT COUNT(*) FROM reminders WHERE status = 'open') AS open_reminders, "
            "(SELECT COUNT(*) FROM actions WHERE status = 'open' AND due_date = date('now')) AS due_today, "
            "(SELECT COUNT(*) FROM actions WHERE status = 'open' AND due_date < date('now')) AS overdue"
        ) or {}
        counts.update(
            {
                "pending_reviews": workflow_stats["pending_reviews"],
                "pending_review": workflow_stats["pending_reviews"],
                "pending_assignee_review": len(pending_assignee_reviews),
                "open_actions": workflow_stats["open_actions"],
                "attention": workflow_stats["attention"],
                "attention_counts": workflow_stats["attention_counts"],
                "open_reminders": workflow_stats["open_reminders"],
                "due_today": workflow_stats["due_today"],
                "overdue": workflow_stats["overdue"],
            }
        )
        reminders = self.database.fetch_all(
            "SELECT r.*, m.title AS matter_title FROM reminders r "
            "LEFT JOIN matters m ON m.id = r.matter_id WHERE r.status = 'open' "
            "ORDER BY CASE r.kind WHEN 'processing' THEN 0 WHEN 'overdue' THEN 1 ELSE 2 END, "
            "r.created_at DESC LIMIT 12"
        )
        actions = self.database.fetch_all(
            "SELECT a.*, m.title AS matter_title FROM actions a "
            "JOIN matters m ON m.id = a.matter_id WHERE a.status = 'open' "
            "ORDER BY CASE WHEN a.due_date < date('now') THEN 0 WHEN a.due_date = date('now') "
            "THEN 1 ELSE 2 END, a.due_date, a.created_at DESC LIMIT 12"
        )
        next_follow_up = self.database.fetch_one(
            "SELECT r.*, m.title AS matter_title FROM reminders r "
            "LEFT JOIN matters m ON m.id = r.matter_id "
            "WHERE r.status = 'scheduled' ORDER BY datetime(r.due_at) LIMIT 1"
        )
        agent_activity: list[dict[str, Any]] = []
        for row in self.database.fetch_all(
            "SELECT x.id, x.filename, x.source_type, x.status, x.matter_id, x.updated_at, "
            "x.metadata_json, m.title AS matter_title FROM materials x "
            "LEFT JOIN matters m ON m.id = x.matter_id ORDER BY x.updated_at DESC LIMIT 30"
        ):
            assistant = parse_json(row.pop("metadata_json", "{}"), {}).get("workbuddy")
            if not isinstance(assistant, dict):
                continue
            row["assistant"] = {**assistant, "display_name": "贾维斯"}
            if row.get("source_type") == "workbuddy_channel":
                row["source_type"] = "assistant_channel"
            agent_activity.append(row)
            if len(agent_activity) == 6:
                break
        if next_follow_up is None:
            for item in agent_activity:
                assistant = item.get("assistant") or {}
                brief = assistant.get("brief") or {}
                if brief.get("next_check_at") and brief.get("next_check_reason"):
                    next_follow_up = {
                        "kind": "assistant_check",
                        "matter_id": item.get("matter_id"),
                        "matter_title": item.get("matter_title"),
                        "title": "贾维斯复查",
                        "reason": str(brief["next_check_reason"])[:1000],
                        "due_at": str(brief["next_check_at"])[:50],
                    }
                    break
        return public_labels({
            "counts": counts,
            "attention": attention,
            "reminders": reminders,
            "actions": actions,
            "recent_matters": self.list_matters(6),
            "nodes": self.node_status(),
            "next_follow_up": next_follow_up,
            "agent_activity": agent_activity,
        })

    def update_reminder(
        self, reminder_id: str, changes: dict[str, Any], actor: str
    ) -> dict[str, Any]:
        reminder = self.database.fetch_one(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        )
        if not reminder:
            raise KeyError("提醒不存在")
        expected_updated_at = changes.pop("expected_updated_at", None)
        change_reason = str(changes.pop("change_reason", "") or "")[:500]
        if expected_updated_at and expected_updated_at != reminder.get("updated_at"):
            raise StaleWriteError("提醒已被更新，请刷新后再保存")
        values = {
            key: value
            for key, value in changes.items()
            if key in {"title", "reason", "due_at", "action_id"}
        }
        if not values:
            raise ValueError("请提供需要修改的提醒内容")
        if "title" in values:
            values["title"] = str(values["title"] or "").strip()[:200]
            if not values["title"]:
                raise ValueError("提醒标题不能为空")
        if "reason" in values:
            values["reason"] = str(values["reason"] or "")[:1000]
        if "due_at" in values:
            values["due_at"] = values["due_at"] or None
            if values["due_at"]:
                try:
                    datetime.fromisoformat(str(values["due_at"]).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("提醒时间格式不正确") from exc
        if "action_id" in values:
            values["action_id"] = values["action_id"] or None
            if values["action_id"]:
                action = self.database.fetch_one(
                    "SELECT matter_id FROM actions WHERE id = ?", (values["action_id"],)
                )
                if not action or action["matter_id"] != reminder.get("matter_id"):
                    raise ValueError("关联行动不属于当前事项")
        now = utc_now()
        if now <= reminder["updated_at"]:
            now = (
                datetime.fromisoformat(reminder["updated_at"].replace("Z", "+00:00"))
                + timedelta(seconds=1)
            ).isoformat(timespec="seconds").replace("+00:00", "Z")
        assignments = ", ".join(f"{key} = ?" for key in values)
        payload = {
            "before": {key: reminder.get(key) for key in values},
            "after": values,
            "reason": change_reason,
        }
        with self.database.connect() as connection:
            updated = connection.execute(
                f"UPDATE reminders SET {assignments}, updated_at = ? "
                "WHERE id = ? AND updated_at = ?",
                (*values.values(), now, reminder_id, reminder["updated_at"]),
            )
            if updated.rowcount != 1:
                raise StaleWriteError("提醒已被更新，请刷新后再保存")
            self.database.audit(
                new_id("audit"), actor, "reminder.updated", "reminder", reminder_id,
                reminder.get("matter_id"), payload, connection=connection,
            )
            if reminder.get("matter_id"):
                self.record_matter_event(
                    reminder["matter_id"], "reminder.updated", actor, "reminder",
                    reminder_id, "提醒内容已修改", payload, connection=connection,
                )
        result = self.database.fetch_one(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        )
        if not result:
            raise RuntimeError("提醒写入失败")
        return result

    def resolve_reminder(self, reminder_id: str, status: str, actor: str) -> dict[str, Any]:
        if status not in {"done", "dismissed", "snoozed"}:
            raise ValueError("不支持的提醒状态")
        now = utc_now()
        with self.database.connect() as connection:
            updated = connection.execute(
                "UPDATE reminders SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, reminder_id),
            )
        if updated.rowcount != 1:
            raise KeyError("提醒不存在")
        self.database.audit(
            new_id("audit"), actor, "reminder.resolved", "reminder", reminder_id,
            metadata={"status": status}
        )
        result = self.database.fetch_one("SELECT * FROM reminders WHERE id = ?", (reminder_id,))
        if not result:
            raise RuntimeError("提醒状态写入失败")
        return result

    def set_action_assignees(
        self, action_id: str, person_ids: list[str], note: str, actor: str
    ) -> dict[str, Any]:
        action = self.database.fetch_one("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not action:
            raise KeyError("行动不存在")
        selected = []
        seen = set()
        valid_ids = {person["id"] for person in PEOPLE_SEED}
        for person_id in person_ids:
            if person_id not in valid_ids:
                raise ValueError("跟进人不存在")
            if person_id not in seen:
                selected.append(person_id)
                seen.add(person_id)
        now = utc_now()
        with self.database.connect() as connection:
            existing = {
                row["person_id"]
                for row in connection.execute(
                    "SELECT person_id FROM action_assignees WHERE action_id = ?",
                    (action_id,),
                ).fetchall()
            }
            for person_id in selected:
                if person_id not in existing:
                    connection.execute(
                        "INSERT INTO action_assignees "
                        "(id, action_id, person_id, status, reason, evidence_json, source_material_id, "
                        "suggested_by, confirmed_by, confirmed_at, created_at, updated_at) "
                        "VALUES (?, ?, ?, 'confirmed', ?, '[]', ?, 'manual', ?, ?, ?, ?)",
                        (
                            f"assignee_{action_id}_{person_id}",
                            action_id,
                            person_id,
                            note[:500],
                            action.get("material_id"),
                            actor,
                            now,
                            now,
                            now,
                        ),
                    )
            if selected:
                placeholders = ",".join("?" for _ in selected)
                connection.execute(
                    "UPDATE action_assignees SET status = 'confirmed', confirmed_by = ?, "
                    "confirmed_at = ?, updated_at = ? "
                    f"WHERE action_id = ? AND person_id IN ({placeholders})",
                    (actor, now, now, action_id, *selected),
                )
                connection.execute(
                    "UPDATE action_assignees SET status = 'rejected', confirmed_by = ?, "
                    "confirmed_at = ?, updated_at = ? "
                    f"WHERE action_id = ? AND person_id NOT IN ({placeholders}) "
                    "AND status IN ('pending', 'confirmed')",
                    (actor, now, now, action_id, *selected),
                )
            else:
                connection.execute(
                    "UPDATE action_assignees SET status = 'rejected', confirmed_by = ?, "
                    "confirmed_at = ?, updated_at = ? "
                    "WHERE action_id = ? AND status IN ('pending', 'confirmed')",
                    (actor, now, now, action_id),
                )
            names = [
                row["display_name"]
                for row in connection.execute(
                    "SELECT display_name FROM people WHERE id IN "
                    f"({','.join('?' for _ in selected)}) ORDER BY is_self DESC, created_at",
                    tuple(selected),
                ).fetchall()
            ] if selected else []
            connection.execute(
                "UPDATE actions SET owner = ?, updated_at = ? WHERE id = ?",
                ("、".join(names) or None, now, action_id),
            )
            connection.execute(
                "INSERT INTO audit_events "
                "(id, actor, action, object_type, object_id, matter_id, metadata_json, created_at) "
                "VALUES (?, ?, 'action.assignees.updated', 'action', ?, ?, ?, ?)",
                (
                    new_id("audit"),
                    actor,
                    action_id,
                    action["matter_id"],
                    json.dumps({"person_ids": selected, "note": note[:500]}, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
        result = self.database.fetch_one(
            "SELECT a.*, m.title AS matter_title FROM actions a JOIN matters m ON m.id = a.matter_id "
            "WHERE a.id = ?",
            (action_id,),
        )
        if not result:
            raise RuntimeError("跟进人写入失败")
        return self._attach_action_assignees([result])[0]

    def list_assignee_reviews(self, status: str = "pending") -> list[dict[str, Any]]:
        if status not in {"pending", "confirmed", "rejected", "superseded", "all"}:
            raise ValueError("不支持的负责人确认状态")
        params: list[Any] = []
        clause = ""
        if status != "all":
            clause = "AND aa.status = ?"
            params.append(status)
        rows = self.database.fetch_all(
            "SELECT aa.*, p.display_name, p.role, a.title AS action_title, "
            "a.detail AS action_detail, a.kind AS action_kind, a.status AS action_status, "
            "a.matter_id, m.title AS matter_title "
            "FROM action_assignees aa "
            "JOIN people p ON p.id = aa.person_id "
            "JOIN actions a ON a.id = aa.action_id "
            "JOIN matters m ON m.id = a.matter_id "
            f"WHERE a.status = 'open' {clause} "
            "ORDER BY a.created_at DESC, aa.created_at",
            tuple(params),
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            evidence = _json_list(row.pop("evidence_json", "[]"))
            action_id = row["action_id"]
            item = grouped.setdefault(
                action_id,
                {
                    "action_id": action_id,
                    "matter_id": row["matter_id"],
                    "matter_title": row["matter_title"],
                    "action_title": row["action_title"],
                    "action_detail": row["action_detail"],
                    "action_kind": row["action_kind"],
                    "action_status": row["action_status"],
                    "status": row["status"],
                    "suggested_people": [],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                },
            )
            item["suggested_people"].append(
                {
                    "person_id": row["person_id"],
                    "display_name": row["display_name"],
                    "role": row["role"],
                    "status": row["status"],
                    "alias": row.get("alias") or "",
                    "reason": row.get("reason") or "",
                    "evidence": evidence,
                    "suggested_by": row.get("suggested_by") or "jarvis",
                    "confirmed_by": row.get("confirmed_by"),
                    "confirmed_at": row.get("confirmed_at"),
                }
            )
        return public_labels(list(grouped.values()))

    def resolve_action(self, action_id: str, status: str, actor: str) -> dict[str, Any]:
        if status not in {"open", "done", "dismissed"}:
            raise ValueError("不支持的行动状态")
        now = utc_now()
        action = self.database.fetch_one("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not action:
            raise KeyError("行动不存在")
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE actions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, action_id),
            )
            if status != "open":
                connection.execute(
                    "UPDATE reminders SET status = 'done', updated_at = ? "
                    "WHERE action_id = ? AND status NOT IN ('done', 'dismissed')",
                    (now, action_id),
                )
                connection.execute(
                    "UPDATE action_assignees SET status = 'superseded', updated_at = ? "
                    "WHERE action_id = ? AND status = 'pending'",
                    (now, action_id),
                )
        self.database.audit(
            new_id("audit"),
            actor,
            "action.resolved",
            "action",
            action_id,
            action["matter_id"],
            {"status": status},
        )
        self.record_matter_event(
            action["matter_id"],
            "action.resolved",
            actor,
            "action",
            action_id,
            "行动状态已更新",
            {"status": status},
        )
        result = self.database.fetch_one("SELECT * FROM actions WHERE id = ?", (action_id,))
        if not result:
            raise RuntimeError("行动状态写入失败")
        return self._attach_action_assignees([result])[0]

    def heartbeat(self, node_id: str, name: str, metadata: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        metadata_json = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO nodes (id, name, status, last_seen_at, metadata_json) "
                "VALUES (?, ?, 'online', ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "name = excluded.name, status = 'online', last_seen_at = excluded.last_seen_at, "
                "metadata_json = excluded.metadata_json",
                (node_id, name[:120], now, metadata_json),
            )
        row = self.database.fetch_one("SELECT * FROM nodes WHERE id = ?", (node_id,))
        if not row:
            raise RuntimeError("执行节点状态写入失败")
        return public_node(row)

    def node_status(self) -> list[dict[str, Any]]:
        rows = self.database.fetch_all("SELECT * FROM nodes ORDER BY last_seen_at DESC")
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=5)
        historical_cutoff = now - timedelta(hours=24)
        for row in rows:
            last_seen = datetime.fromisoformat(row["last_seen_at"].replace("Z", "+00:00"))
            row["status"] = "online" if last_seen >= cutoff else "offline"
            public = public_node(row)
            public["lifecycle"] = (
                "current"
                if last_seen >= cutoff
                else "historical"
                if last_seen < historical_cutoff
                else "stale"
            )
            row.clear()
            row.update(public)
        return rows

    def audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.database.fetch_all(
            "SELECT * FROM audit_events ORDER BY created_at DESC LIMIT ?", (limit,)
        )
