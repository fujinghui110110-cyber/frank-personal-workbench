from __future__ import annotations

import json
import hashlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .assignees import prefill_matter_contact
from .db import Database, utc_now


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return fallback


_SUBJECT_PREFIX_RE = re.compile(
    r"^(?:(?:re|fw|fwd|回复|转发)\s*[:：]\s*)+", re.IGNORECASE
)
_FORWARDING_CHAIN_RE = re.compile(
    r"^(?:[^，。；]{0,120}发起[，,]\s*)?[^，。；]{1,80}转发[，,]\s*"
)


def _clean_email_subject(value: Any) -> str:
    text = str(value or "").strip()
    return _SUBJECT_PREFIX_RE.sub("", text).strip()


def _clean_email_routing(value: Any) -> str:
    return _FORWARDING_CHAIN_RE.sub("", str(value or "").strip()).strip()


class EmailWorkService:
    def __init__(self, database: Database):
        self.database = database

    def request_sync(self, actor: str) -> dict[str, Any]:
        if not self.database.fetch_one("SELECT account_id FROM email_accounts LIMIT 1"):
            return {
                "status": "not_configured",
                "configured": False,
                "message": "尚未配置邮箱账号，请先登记邮箱后再同步",
                "request": None,
            }
        existing = self.database.fetch_one(
            "SELECT * FROM email_sync_requests WHERE status IN ('pending', 'running') "
            "ORDER BY requested_at DESC LIMIT 1"
        )
        if existing:
            return existing
        now = utc_now()
        request_id = _id("email_sync")
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO email_sync_requests "
                "(id, status, requested_by, requested_at) VALUES (?, 'pending', ?, ?)",
                (request_id, actor, now),
            )
        return self.database.fetch_one(
            "SELECT * FROM email_sync_requests WHERE id = ?", (request_id,)
        ) or {}

    def claim_sync(self, worker_id: str) -> dict[str, Any] | None:
        now = utc_now()
        stale_before = (datetime.now(UTC) - timedelta(minutes=15)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE email_sync_requests SET status = 'pending', worker_id = NULL, "
                "claimed_at = NULL, error = '上次邮箱检查中断，已自动续跑' "
                "WHERE status = 'running' AND claimed_at < ?",
                (stale_before,),
            )
            row = connection.execute(
                "SELECT * FROM email_sync_requests WHERE status = 'pending' "
                "ORDER BY requested_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                "UPDATE email_sync_requests SET status = 'running', claimed_at = ?, "
                "worker_id = ?, error = '' WHERE id = ? AND status = 'pending'",
                (now, worker_id, row["id"]),
            )
            if updated.rowcount != 1:
                return None
        return self.database.fetch_one(
            "SELECT * FROM email_sync_requests WHERE id = ?", (row["id"],)
        )

    def register_account(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO email_accounts "
                "(account_id, address_hint, imap_host, folder, status, updated_at) "
                "VALUES (?, ?, ?, ?, 'ready', ?) "
                "ON CONFLICT(account_id) DO UPDATE SET address_hint = excluded.address_hint, "
                "imap_host = excluded.imap_host, folder = excluded.folder, updated_at = excluded.updated_at",
                (
                    payload["account_id"],
                    payload["address_hint"],
                    payload["imap_host"],
                    payload.get("folder") or "INBOX",
                    now,
                ),
            )
        return self.database.fetch_one(
            "SELECT * FROM email_accounts WHERE account_id = ?", (payload["account_id"],)
        ) or {}

    def account_state(self, account_id: str) -> dict[str, Any]:
        return self.database.fetch_one(
            "SELECT * FROM email_accounts WHERE account_id = ?", (account_id,)
        ) or {"account_id": account_id, "last_uid": 0, "uid_validity": ""}

    def finish_sync(self, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = self.database.fetch_one(
            "SELECT * FROM email_sync_requests WHERE id = ?", (request_id,)
        )
        if not request:
            raise KeyError("邮箱检查任务不存在")
        if request["status"] != "running" or request["worker_id"] != payload["worker_id"]:
            raise ValueError("邮箱检查任务已失效")
        now = utc_now()
        status = payload["status"]
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE email_sync_requests SET status = ?, completed_at = ?, error = ?, "
                "scanned_count = ?, pending_count = ?, ignored_count = ? WHERE id = ?",
                (
                    status,
                    now,
                    payload.get("error", "")[:1000],
                    int(payload.get("scanned_count") or 0),
                    int(payload.get("pending_count") or 0),
                    int(payload.get("ignored_count") or 0),
                    request_id,
                ),
            )
            account_id = payload.get("account_id")
            if account_id:
                connection.execute(
                    "UPDATE email_accounts SET status = ?, uid_validity = ?, last_uid = ?, "
                    "last_success_at = CASE WHEN ? = 'completed' THEN ? ELSE last_success_at END, "
                    "last_error = ?, updated_at = ? WHERE account_id = ?",
                    (
                        "ready" if status == "completed" else "failed",
                        payload.get("uid_validity", ""),
                        int(payload.get("last_uid") or 0),
                        status,
                        now,
                        payload.get("error", "")[:1000],
                        now,
                        account_id,
                    ),
                )
        return self.database.fetch_one(
            "SELECT * FROM email_sync_requests WHERE id = ?", (request_id,)
        ) or {}

    def _matter_is_open(self, matter_id: str) -> bool:
        row = self.database.fetch_one(
            "SELECT m.id, "
            "(SELECT COUNT(*) FROM actions a WHERE a.matter_id = m.id AND a.status = 'open') + "
            "(SELECT COUNT(*) FROM review_items r WHERE r.matter_id = m.id AND r.status = 'pending') + "
            "(SELECT COUNT(*) FROM reminders x WHERE x.matter_id = m.id "
            "AND x.status NOT IN ('done', 'dismissed')) AS open_count "
            "FROM matters m WHERE m.id = ?",
            (matter_id,),
        )
        return bool(row and row["open_count"])

    def ingest_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        account = self.database.fetch_one(
            "SELECT * FROM email_accounts WHERE account_id = ?", (payload["account_id"],)
        )
        if not account:
            raise KeyError("邮箱尚未登记")
        payload = {
            **payload,
            "subject": _clean_email_subject(payload.get("subject")),
            "summary": _clean_email_routing(payload.get("summary")),
            "matter_title": _clean_email_subject(
                _clean_email_routing(payload.get("matter_title"))
            ),
            "evidence": [
                _clean_email_routing(item) for item in payload.get("evidence", [])
            ],
            "actions": [
                {
                    **item,
                    "title": _clean_email_routing(item.get("title")),
                    "detail": _clean_email_routing(item.get("detail")),
                }
                for item in payload.get("actions", [])
                if isinstance(item, dict)
            ],
        }
        duplicate = self.database.fetch_one(
            "SELECT * FROM email_messages WHERE account_id = ? AND folder = ? "
            "AND uid_validity = ? AND uid = ?",
            (
                payload["account_id"],
                payload.get("folder") or "INBOX",
                payload["uid_validity"],
                payload["uid"],
            ),
        )
        if duplicate:
            return self._public_message(duplicate)

        if payload["classification"] == "pending":
            source_text = str(payload.get("source_text") or "").strip()
            if not source_text:
                raise ValueError("邮件原文为空")
            now = utc_now()
            message_id = _id("email")
            material_id = _id("mat")
            job_id = _id("job")
            digest = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            metadata = {
                "email_message_id": message_id,
                "attachment_paths": payload.get("attachment_paths", [])[:20],
                "sender_key": payload.get("sender_key", ""),
            }
            with self.database.connect() as connection:
                connection.execute(
                    "INSERT INTO materials "
                    "(id, idempotency_key, sha256, source_type, filename, content_type, size, "
                    "text_note, status, received_at, updated_at, metadata_json) "
                    "VALUES (?, ?, ?, 'email_auto', ?, 'text/plain', ?, ?, "
                    "'awaiting_analysis', ?, ?, ?)",
                    (
                        material_id,
                        f"email:{payload['account_id']}:{payload.get('folder') or 'INBOX'}:"
                        f"{payload['uid_validity']}:{payload['uid']}",
                        digest,
                        (payload.get("subject") or "待整理邮件")[:255],
                        len(source_text.encode("utf-8")),
                        source_text,
                        now,
                        now,
                        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                connection.execute(
                    "INSERT INTO email_messages "
                    "(id, account_id, folder, uid_validity, uid, message_id_hash, thread_key, "
                    "sender_key, sender_name, sender_hint, subject, sent_at, classification, "
                    "needs_follow_up, summary, reason, evidence_json, source_text, material_id, "
                    "status, matter_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', '', '[]', "
                    "'', ?, 'pending', NULL, ?, ?)",
                    (
                        message_id,
                        payload["account_id"],
                        payload.get("folder") or "INBOX",
                        payload["uid_validity"],
                        payload["uid"],
                        payload.get("message_id_hash", ""),
                        payload["thread_key"],
                        payload["sender_key"],
                        payload.get("sender_name", "")[:160],
                        payload.get("sender_hint", "")[:160],
                        payload.get("subject", "")[:300],
                        payload.get("sent_at"),
                        material_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    "INSERT INTO jobs "
                    "(id, material_id, job_type, status, requires_local, priority, created_at, updated_at) "
                    "VALUES (?, ?, 'email_classify', 'held', 1, 85, ?, ?)",
                    (job_id, material_id, now, now),
                )
            return self._public_message(
                self.database.fetch_one(
                    "SELECT * FROM email_messages WHERE id = ?", (message_id,)
                )
                or {}
            )

        work = payload["classification"] == "work" and payload["needs_follow_up"]
        if not work:
            return {
                "classification": "irrelevant",
                "needs_follow_up": False,
                "status": "ignored",
                "stored": False,
            }
        now = utc_now()
        message_id = _id("email")
        matter_id = payload.get("matter_id") if work else None
        if matter_id and not self._matter_is_open(matter_id):
            matter_id = None
        if work and not matter_id:
            existing = self.database.fetch_one(
                "SELECT matter_id FROM email_messages WHERE account_id = ? AND thread_key = ? "
                "AND matter_id IS NOT NULL ORDER BY sent_at DESC LIMIT 1",
                (payload["account_id"], payload["thread_key"]),
            )
            if existing and self._matter_is_open(existing["matter_id"]):
                matter_id = existing["matter_id"]

        with self.database.connect() as connection:
            if work and not matter_id:
                matter_id = _id("matter")
                title = (payload.get("matter_title") or payload["subject"] or "邮件工作事项")[:120]
                connection.execute(
                    "INSERT INTO matters (id, title, summary, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (matter_id, title, payload.get("summary", "")[:1000], now, now),
                )
            elif work and matter_id:
                connection.execute(
                    "UPDATE matters SET summary = ?, updated_at = ? WHERE id = ?",
                    (payload.get("summary", "")[:1000], now, matter_id),
                )

            connection.execute(
                "INSERT INTO email_messages "
                "(id, account_id, folder, uid_validity, uid, message_id_hash, thread_key, "
                "sender_key, sender_name, sender_hint, subject, sent_at, classification, "
                "needs_follow_up, summary, reason, evidence_json, status, matter_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message_id,
                    payload["account_id"],
                    payload.get("folder") or "INBOX",
                    payload["uid_validity"],
                    payload["uid"],
                    payload.get("message_id_hash", ""),
                    payload["thread_key"],
                    payload["sender_key"],
                    payload.get("sender_name", "")[:160],
                    payload.get("sender_hint", "")[:160],
                    payload.get("subject", "")[:300],
                    payload.get("sent_at"),
                    payload["classification"],
                    int(bool(payload["needs_follow_up"])),
                    payload.get("summary", "")[:1000],
                    payload.get("reason", "")[:500],
                    json.dumps(payload.get("evidence", [])[:4], ensure_ascii=False),
                    "active",
                    matter_id,
                    now,
                    now,
                ),
            )
            if work and matter_id:
                for item in payload.get("actions", [])[:12]:
                    title = str(item.get("title") or "").strip()
                    if not title:
                        continue
                    connection.execute(
                        "INSERT INTO actions "
                        "(id, matter_id, material_id, kind, title, detail, status, owner, due_date, "
                        "created_by, created_at, updated_at) VALUES (?, ?, NULL, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                        (
                            _id("action"),
                            matter_id,
                            item.get("kind") if item.get("kind") in {"task", "risk", "decision", "waiting", "conclusion"} else "task",
                            title[:160],
                            str(item.get("detail") or "")[:1000],
                            str(item.get("owner") or "")[:80] or None,
                            item.get("due_date") or None,
                            f"email:{message_id}",
                            now,
                            now,
                        ),
                    )
                if not payload.get("actions"):
                    connection.execute(
                        "INSERT INTO actions "
                        "(id, matter_id, material_id, kind, title, detail, status, owner, created_by, created_at, updated_at) "
                        "VALUES (?, ?, NULL, 'task', ?, ?, 'open', '财务负责人', ?, ?, ?)",
                        (
                            _id("action"),
                            matter_id,
                            (payload.get("summary") or payload["subject"] or "跟进邮件要求")[:160],
                            payload.get("reason", "")[:1000],
                            f"email:{message_id}",
                            now,
                            now,
                        ),
                    )

        row = self.database.fetch_one("SELECT * FROM email_messages WHERE id = ?", (message_id,))
        return self._public_message(row or {})

    def complete_analysis(self, message_id: str, result: dict[str, Any]) -> dict[str, Any]:
        message = self.database.fetch_one(
            "SELECT * FROM email_messages WHERE id = ?", (message_id,)
        )
        if not message:
            raise KeyError("邮件不存在")
        if message["classification"] != "pending":
            return self._public_message(message)

        classification = str(result.get("classification") or "irrelevant").strip().lower()
        if classification not in {"work", "irrelevant"}:
            classification = "irrelevant"
        needs_follow_up = classification == "work" and bool(result.get("needs_follow_up"))
        try:
            confidence = float(result.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        policy_relevant = bool(result.get("_policy_relevant"))
        summary = _clean_email_routing(result.get("summary"))[:1000]
        reason = _clean_email_routing(result.get("reason"))[:500]
        evidence = [
            _clean_email_routing(item)
            for item in (result.get("evidence") or [])[:4]
            if str(item or "").strip()
        ]
        actions = [item for item in (result.get("actions") or [])[:12] if isinstance(item, dict)]
        work = classification == "work" and needs_follow_up
        accepted_work = work and confidence >= 0.95
        matter_id = result.get("matter_id") if work else None
        if not accepted_work:
            matter_id = None
        if matter_id and not self._matter_is_open(str(matter_id)):
            matter_id = None
        if work and not matter_id:
            existing = self.database.fetch_one(
                "SELECT matter_id FROM email_messages WHERE account_id = ? AND thread_key = ? "
                "AND matter_id IS NOT NULL ORDER BY sent_at DESC LIMIT 1",
                (message["account_id"], message["thread_key"]),
            )
            if existing and self._matter_is_open(existing["matter_id"]):
                matter_id = existing["matter_id"]

        material_metadata: dict[str, Any] = {}
        if message.get("material_id"):
            material = self.database.fetch_one(
                "SELECT metadata_json FROM materials WHERE id = ?", (message["material_id"],)
            )
            material_metadata = _json(
                material.get("metadata_json") if material else "{}", {}
            )
        now = utc_now()
        with self.database.connect() as connection:
            if accepted_work and not matter_id:
                matter_id = _id("matter")
                title = (
                    _clean_email_subject(result.get("matter_title"))
                    or message.get("subject")
                    or "邮件工作事项"
                )[:120]
                connection.execute(
                    "INSERT INTO matters (id, title, summary, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (matter_id, title, summary, now, now),
                )
            elif accepted_work and matter_id:
                connection.execute(
                    "UPDATE matters SET summary = ?, updated_at = ? WHERE id = ?",
                    (summary, now, matter_id),
                )

            status = "active" if work or policy_relevant else "ignored"
            connection.execute(
                "UPDATE email_messages SET classification = ?, needs_follow_up = ?, "
                "summary = ?, reason = ?, evidence_json = ?, confidence = ?, status = ?, matter_id = ?, "
                "source_text = '', updated_at = ? WHERE id = ?",
                (
                    classification,
                    int(needs_follow_up),
                    summary,
                    reason,
                    json.dumps(evidence, ensure_ascii=False),
                    confidence,
                    status,
                    matter_id,
                    now,
                    message_id,
                ),
            )
            if accepted_work and matter_id:
                for item in actions:
                    title = _clean_email_routing(item.get("title"))
                    if not title:
                        continue
                    connection.execute(
                        "INSERT INTO actions "
                        "(id, matter_id, material_id, kind, title, detail, status, owner, due_date, "
                        "created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)",
                        (
                            _id("action"),
                            matter_id,
                            message.get("material_id"),
                            item.get("kind")
                            if item.get("kind") in {"task", "risk", "decision", "waiting", "conclusion"}
                            else "task",
                            title[:160],
                            _clean_email_routing(item.get("detail"))[:1000],
                            str(item.get("owner") or "")[:80] or None,
                            item.get("due_date") or None,
                            f"email:{message_id}",
                            now,
                            now,
                        ),
                    )
            if accepted_work and matter_id:
                prefill_matter_contact(connection, matter_id, actions, now)

            if message.get("material_id"):
                if status == "ignored":
                    connection.execute(
                        "UPDATE materials SET text_note = '', size = 0, metadata_json = '{}', "
                        "status = 'processed', updated_at = ? WHERE id = ?",
                        (now, message["material_id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE materials SET status = 'processed', matter_id = ?, updated_at = ? "
                        "WHERE id = ?",
                        (matter_id, now, message["material_id"]),
                    )

        if not work and not policy_relevant:
            for value in material_metadata.get("attachment_paths", []):
                path = Path(str(value))
                if path.is_file():
                    path.unlink(missing_ok=True)
        row = self.database.fetch_one("SELECT * FROM email_messages WHERE id = ?", (message_id,))
        return self._public_message(row or {})

    def confirm_message(self, message_id: str, actor: str) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM email_messages WHERE id = ?", (message_id,))
        if not row:
            raise KeyError("邮件不存在")
        if row.get("matter_id"):
            return self._public_message(row)
        if row.get("classification") != "work" or not row.get("needs_follow_up"):
            raise ValueError("这封邮件不是待确认工作")
        now = utc_now()
        matter_id = _id("matter")
        action_id = _id("action")
        title = (row.get("subject") or row.get("summary") or "邮件工作事项")[:120]
        action_title = (row.get("summary") or row.get("subject") or "跟进邮件要求")[:160]
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO matters (id, title, summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (matter_id, title, row.get("summary") or "", now, now),
            )
            connection.execute(
                "INSERT INTO actions (id, matter_id, material_id, kind, title, detail, status, "
                "created_by, created_at, updated_at) VALUES (?, ?, ?, 'task', ?, ?, 'open', ?, ?, ?)",
                (
                    action_id,
                    matter_id,
                    row.get("material_id"),
                    action_title,
                    row.get("reason") or "",
                    f"email:{message_id}",
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE email_messages SET matter_id = ?, updated_at = ? WHERE id = ?",
                (matter_id, now, message_id),
            )
            if row.get("material_id"):
                connection.execute(
                    "UPDATE materials SET matter_id = ?, status = 'processed', updated_at = ? WHERE id = ?",
                    (matter_id, now, row["material_id"]),
                )
        self.database.audit(
            _id("audit"), actor, "email.confirmed", "email_message", message_id, matter_id
        )
        confirmed = self.database.fetch_one("SELECT * FROM email_messages WHERE id = ?", (message_id,))
        return self._public_message(confirmed or {})

    def ignore_message(self, message_id: str, actor: str) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM email_messages WHERE id = ?", (message_id,))
        if not row:
            raise KeyError("邮件工作不存在")
        now = utc_now()
        with self.database.connect() as connection:
            action_rows = connection.execute(
                "SELECT id FROM actions WHERE created_by = ? AND status = 'open'",
                (f"email:{message_id}",),
            ).fetchall()
            ignored_action_ids = [row["id"] for row in action_rows]
            connection.execute(
                "UPDATE email_messages SET status = 'ignored', updated_at = ? WHERE id = ?",
                (now, message_id),
            )
            if ignored_action_ids:
                placeholders = ",".join("?" for _ in ignored_action_ids)
                connection.execute(
                    f"UPDATE actions SET status = 'dismissed', updated_at = ? "
                    f"WHERE id IN ({placeholders}) AND status = 'open'",
                    (now, *ignored_action_ids),
                )
            connection.execute(
                "UPDATE email_messages SET ignored_action_ids_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(ignored_action_ids, ensure_ascii=False), now, message_id),
            )
        self.database.audit(
            _id("audit"), actor, "email.ignored", "email_message", message_id, row.get("matter_id")
        )
        return self._public_message(
            self.database.fetch_one("SELECT * FROM email_messages WHERE id = ?", (message_id,)) or {}
        )

    def restore_message(self, message_id: str, actor: str) -> dict[str, Any]:
        row = self.database.fetch_one("SELECT * FROM email_messages WHERE id = ?", (message_id,))
        if not row:
            raise KeyError("邮件工作不存在")
        now = utc_now()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE email_messages SET status = 'active', ignored_action_ids_json = '[]', "
                "updated_at = ? WHERE id = ?",
                (now, message_id),
            )
            ignored_action_ids = _json(row.get("ignored_action_ids_json"), [])
            ignored_action_ids = [item for item in ignored_action_ids if isinstance(item, str)]
            if ignored_action_ids:
                placeholders = ",".join("?" for _ in ignored_action_ids)
                connection.execute(
                    f"UPDATE actions SET status = 'open', updated_at = ? "
                    f"WHERE id IN ({placeholders}) AND status = 'dismissed'",
                    (now, *ignored_action_ids),
                )
        self.database.audit(
            _id("audit"), actor, "email.restored", "email_message", message_id, row.get("matter_id")
        )
        return self._public_message(
            self.database.fetch_one("SELECT * FROM email_messages WHERE id = ?", (message_id,)) or {}
        )

    def list_messages(self, status: str = "active", limit: int = 100) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT e.*, m.title AS matter_title FROM email_messages e "
            "LEFT JOIN matters m ON m.id = e.matter_id WHERE e.status = ? "
            "AND (? <> 'active' OR "
            "e.matter_id IS NULL OR "
            "EXISTS (SELECT 1 FROM actions a WHERE a.matter_id = e.matter_id AND a.status = 'open') OR "
            "EXISTS (SELECT 1 FROM review_items r WHERE r.matter_id = e.matter_id AND r.status = 'pending') OR "
            "EXISTS (SELECT 1 FROM reminders x WHERE x.matter_id = e.matter_id "
            "AND x.status NOT IN ('done', 'dismissed'))) "
            "ORDER BY COALESCE(e.sent_at, e.created_at) DESC LIMIT ?",
            (status, status, limit),
        )
        return [self._public_message(row) for row in rows]

    def matter_ids(self) -> set[str]:
        return {
            row["matter_id"]
            for row in self.database.fetch_all(
                "SELECT DISTINCT matter_id FROM email_messages WHERE matter_id IS NOT NULL"
            )
        }

    def status(self) -> dict[str, Any]:
        latest = self.database.fetch_one(
            "SELECT * FROM email_sync_requests ORDER BY requested_at DESC LIMIT 1"
        )
        accounts = self.database.fetch_all(
            "SELECT account_id, address_hint, imap_host, folder, status, last_success_at, "
            "last_error, updated_at FROM email_accounts ORDER BY updated_at DESC"
        )
        counts = self.database.fetch_one(
            "SELECT SUM(e.status = 'active' AND ("
            "EXISTS (SELECT 1 FROM actions a WHERE a.matter_id = e.matter_id AND a.status = 'open') OR "
            "EXISTS (SELECT 1 FROM review_items r WHERE r.matter_id = e.matter_id AND r.status = 'pending') OR "
            "EXISTS (SELECT 1 FROM reminders x WHERE x.matter_id = e.matter_id "
            "AND x.status NOT IN ('done', 'dismissed')))) AS active, "
            "SUM(e.status = 'ignored') AS ignored FROM email_messages e"
        ) or {"active": 0, "ignored": 0}
        return {
            "configured": bool(accounts),
            "accounts": accounts,
            "latest": latest,
            "counts": {"active": int(counts["active"] or 0), "ignored": int(counts["ignored"] or 0)},
        }

    @staticmethod
    def _public_message(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result.pop("source_text", None)
        result["evidence"] = _json(result.pop("evidence_json", "[]"), [])
        result["needs_follow_up"] = bool(result.get("needs_follow_up"))
        return result
