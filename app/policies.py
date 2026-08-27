from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from uuid import uuid4

from .config import Settings
from .db import Database, utc_now


POLICY_CHANGES = {"new", "revision", "repeal", "interpretation", "evidence"}
SOURCE_TYPES = {"personal_wechat", "wecom", "email"}
FORMAL_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
SOURCE_LABELS = {
    "personal_wechat": "个人微信",
    "wecom": "企业微信",
    "email": "邮件",
}
CHANGE_LABELS = {
    "new": "新增规定",
    "revision": "修订规定",
    "repeal": "废止规定",
    "interpretation": "解释口径",
    "evidence": "补充证据",
}
STATUS_LABELS = {
    "pending": "等待确认",
    "applied": "已生效",
    "auto_applied": "已自动补充",
    "ignored": "不是规定",
    "temporary": "已转为临时事项",
    "undone": "已撤销",
}


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _list(value: Any, limit: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _clean(item, 1000)
        if text and text not in result:
            result.append(text)
    return result[:limit]


def _normal(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", value.casefold())


def _canonical_key(publisher: str, topic: str, scope: str, title: str) -> str:
    value = "|".join(_normal(item) for item in (publisher, topic, scope, title))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _safe_name(value: str, fallback: str = "未命名规定") -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", value).strip(" ._")
    return (text or fallback)[:100]


def _date_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d")


class PolicyService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.root = settings.policy_vault_dir

    def ingest_candidate(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_type = _clean(payload.get("source_type"), 40)
        source_ref = _clean(payload.get("source_ref"), 300)
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        classification = _clean(result.get("classification"), 40)
        if source_type not in SOURCE_TYPES:
            raise ValueError("不支持的规定来源")
        if not source_ref:
            raise ValueError("规定来源定位不能为空")
        if classification in {"not_policy", "temporary_task"}:
            return {"stored": False, "classification": classification}
        change_type = _clean(result.get("change_type"), 40)
        if classification not in {"policy", "uncertain"} or change_type not in POLICY_CHANGES:
            raise ValueError("贾维斯 返回的规定判断不完整")

        title = _clean(result.get("title"), 160)
        summary = _clean(result.get("summary"), 2000)
        if not title or not summary:
            raise ValueError("贾维斯 未说明规定标题或内容")
        existing = self.database.fetch_one(
            "SELECT * FROM company_policy_candidates WHERE source_type = ? AND source_ref = ?",
            (source_type, source_ref),
        )
        if existing:
            return self._candidate_public(existing)

        publisher = _clean(result.get("publisher"), 160)
        topic = _clean(result.get("topic"), 160) or "综合管理"
        scope = _clean(result.get("scope"), 500)
        requirements = _list(result.get("requirements"), 20)
        evidence = _list(result.get("evidence"), 4)
        attachments = _list(result.get("attachments"), 20)
        matched_policy_id = self._matched_policy_id(result, publisher, topic, scope, title)
        confidence = max(0.0, min(float(result.get("confidence") or 0), 1.0))
        is_authority = result.get("is_authority") is True
        status = "pending"
        now = utc_now()
        candidate_id = _id("policy_candidate")

        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO company_policy_candidates "
                "(id, source_type, source_ref, source_label, material_id, email_message_id, "
                "change_type, title, publisher, topic, scope, summary, requirements_json, "
                "change_summary, effective_date, confidence, is_authority, evidence_json, attachments_json, "
                "matched_policy_id, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    source_type,
                    source_ref,
                    _clean(payload.get("source_label"), 300),
                    payload.get("material_id"),
                    payload.get("email_message_id"),
                    change_type,
                    title,
                    publisher,
                    topic,
                    scope,
                    summary,
                    json.dumps(requirements, ensure_ascii=False),
                    _clean(result.get("change_summary"), 1000),
                    _clean(result.get("effective_date"), 40) or None,
                    confidence,
                    int(is_authority),
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(attachments, ensure_ascii=False),
                    matched_policy_id,
                    status,
                    now,
                    now,
                ),
            )
        if change_type == "evidence" and matched_policy_id and confidence >= 0.85 and is_authority:
            try:
                return self.resolve_candidate(candidate_id, "apply", "贾维斯", matched_policy_id, auto=True)
            except OSError as error:
                self.record_sync_error(candidate_id, error)
        return self.get_candidate(candidate_id)

    def _matched_policy_id(
        self,
        result: dict[str, Any],
        publisher: str,
        topic: str,
        scope: str,
        title: str,
    ) -> str | None:
        requested = _clean(result.get("matched_policy_id"), 160)
        if requested:
            row = self.database.fetch_one(
                "SELECT id FROM company_policies WHERE id = ? AND status = 'active'", (requested,)
            )
            if row:
                return str(row["id"])
        exact = self.database.fetch_one(
            "SELECT id FROM company_policies WHERE canonical_key = ? AND status = 'active'",
            (_canonical_key(publisher, topic, scope, title),),
        )
        if exact:
            return str(exact["id"])
        best_id: str | None = None
        best_score = 0.0
        for row in self.database.fetch_all(
            "SELECT id, publisher, topic, scope, title FROM company_policies WHERE status = 'active'"
        ):
            publisher_score = SequenceMatcher(None, _normal(publisher), _normal(row["publisher"])).ratio()
            topic_score = SequenceMatcher(None, _normal(topic), _normal(row["topic"])).ratio()
            title_score = SequenceMatcher(None, _normal(title), _normal(row["title"])).ratio()
            scope_score = SequenceMatcher(None, _normal(scope), _normal(row["scope"])).ratio() if scope else 0.5
            score = publisher_score * 0.3 + topic_score * 0.3 + title_score * 0.3 + scope_score * 0.1
            if score > best_score:
                best_score, best_id = score, str(row["id"])
        return best_id if best_score >= 0.72 else None

    def resolve_candidate(
        self,
        candidate_id: str,
        action: str,
        actor: str,
        policy_id: str | None = None,
        *,
        auto: bool = False,
    ) -> dict[str, Any]:
        candidate = self.database.fetch_one(
            "SELECT * FROM company_policy_candidates WHERE id = ?", (candidate_id,)
        )
        if not candidate:
            raise KeyError("规定候选不存在")
        if action in {"ignore", "temporary"}:
            status = "ignored" if action == "ignore" else "temporary"
            now = utc_now()
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE company_policy_candidates SET status = ?, resolved_at = ?, "
                    "updated_at = ?, obsidian_error = '' WHERE id = ?",
                    (status, now, now, candidate_id),
                )
            self._audit(actor, f"policy.candidate.{status}", candidate_id)
            return self.get_candidate(candidate_id)
        if action == "undo":
            return self._undo_auto_candidate(candidate, actor)
        if action not in {"apply", "merge"}:
            raise ValueError("不支持的规定处理方式")
        if candidate["status"] not in {"pending", "undone"}:
            return self._candidate_public(candidate)

        target_id = policy_id or candidate.get("matched_policy_id")
        if action == "merge" and not target_id:
            raise ValueError("请选择要关联的工作台现行规定")
        now = utc_now()
        undo_until = (
            datetime.now(UTC) + timedelta(days=30)
        ).isoformat(timespec="seconds").replace("+00:00", "Z") if auto else None
        with self.database.connect() as connection:
            policy = None
            if target_id:
                row = connection.execute(
                    "SELECT * FROM company_policies WHERE id = ?", (target_id,)
                ).fetchone()
                policy = dict(row) if row else None
                if not policy:
                    raise KeyError("要合并的现行规定不存在")
            requirements = _json(candidate["requirements_json"], [])
            if not policy:
                target_id = _id("policy")
                policy = {
                    "id": target_id,
                    "canonical_key": _canonical_key(
                        candidate["publisher"], candidate["topic"], candidate["scope"], candidate["title"]
                    ),
                    "title": candidate["title"],
                    "publisher": candidate["publisher"] or "上级公司",
                    "topic": candidate["topic"] or "综合管理",
                    "scope": candidate["scope"],
                    "summary": candidate["summary"],
                    "requirements_json": json.dumps(requirements, ensure_ascii=False),
                    "effective_date": candidate["effective_date"],
                    "status": "retired" if candidate["change_type"] == "repeal" else "active",
                    "version": 1,
                    "obsidian_path": "",
                    "last_verified_at": now,
                    "created_at": now,
                    "updated_at": now,
                }
                connection.execute(
                    "INSERT INTO company_policies "
                    "(id, canonical_key, title, publisher, topic, scope, summary, requirements_json, "
                    "effective_date, status, version, obsidian_path, last_verified_at, created_at, updated_at) "
                    "VALUES (:id, :canonical_key, :title, :publisher, :topic, :scope, :summary, "
                    ":requirements_json, :effective_date, :status, :version, :obsidian_path, "
                    ":last_verified_at, :created_at, :updated_at)",
                    policy,
                )
            else:
                version = int(policy["version"]) + 1
                change_type = candidate["change_type"]
                if change_type in {"revision", "interpretation"}:
                    policy.update(
                        title=candidate["title"] or policy["title"],
                        publisher=candidate["publisher"] or policy["publisher"],
                        topic=candidate["topic"] or policy["topic"],
                        scope=candidate["scope"] or policy["scope"],
                        summary=candidate["summary"] or policy["summary"],
                        requirements_json=json.dumps(requirements or _json(policy["requirements_json"], []), ensure_ascii=False),
                        effective_date=candidate["effective_date"] or policy["effective_date"],
                    )
                if change_type == "repeal":
                    policy["status"] = "retired"
                policy.update(
                    canonical_key=_canonical_key(
                        policy["publisher"], policy["topic"], policy["scope"], policy["title"]
                    ),
                    version=version,
                    last_verified_at=now,
                    updated_at=now,
                )
                connection.execute(
                    "UPDATE company_policies SET canonical_key = :canonical_key, title = :title, "
                    "publisher = :publisher, topic = :topic, scope = :scope, summary = :summary, "
                    "requirements_json = :requirements_json, effective_date = :effective_date, "
                    "status = :status, version = :version, last_verified_at = :last_verified_at, "
                    "updated_at = :updated_at WHERE id = :id",
                    policy,
                )

            snapshot = self._policy_snapshot(policy)
            connection.execute(
                "INSERT INTO company_policy_versions "
                "(id, policy_id, version, change_type, snapshot_json, evidence_json, "
                "attachments_json, candidate_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _id("policy_version"),
                    target_id,
                    policy["version"],
                    candidate["change_type"],
                    json.dumps(snapshot, ensure_ascii=False),
                    candidate["evidence_json"],
                    candidate["attachments_json"],
                    candidate_id,
                    now,
                ),
            )
            candidate_status = "auto_applied" if auto else "applied"
            connection.execute(
                "UPDATE company_policy_candidates SET status = ?, matched_policy_id = ?, "
                "resolved_at = ?, updated_at = ?, obsidian_error = '', auto_undo_until = ? WHERE id = ?",
                (candidate_status, target_id, now, now, undo_until, candidate_id),
            )
            connection.execute(
                "INSERT INTO company_policy_identity_feedback "
                "(source_type, source_key, publisher, is_authority, confidence, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(source_type, source_key) DO UPDATE SET "
                "publisher = excluded.publisher, is_authority = excluded.is_authority, "
                "confidence = excluded.confidence, "
                "updated_at = excluded.updated_at",
                (
                    candidate["source_type"],
                    candidate["source_label"] or candidate["source_ref"],
                    policy["publisher"],
                    int(bool(candidate["is_authority"])),
                    candidate["confidence"],
                    now,
                ),
            )
            self._sync_policy(connection, target_id)
        self._audit(actor, "policy.candidate.applied", candidate_id, target_id)
        return self.get_candidate(candidate_id)

    def _undo_auto_candidate(self, candidate: dict[str, Any], actor: str) -> dict[str, Any]:
        if candidate["status"] != "auto_applied":
            raise ValueError("只有自动补充的规定可以直接撤销")
        deadline = candidate.get("auto_undo_until") or ""
        if deadline and datetime.fromisoformat(deadline.replace("Z", "+00:00")) < datetime.now(UTC):
            raise ValueError("自动补充已超过可撤销期限")
        policy_id = candidate.get("matched_policy_id")
        with self.database.connect() as connection:
            latest = connection.execute(
                "SELECT * FROM company_policy_versions WHERE policy_id = ? ORDER BY version DESC LIMIT 1",
                (policy_id,),
            ).fetchone()
            if not latest or latest["candidate_id"] != candidate["id"]:
                raise ValueError("这次补充之后规定已有新版本，不能直接撤销")
            previous = connection.execute(
                "SELECT * FROM company_policy_versions WHERE policy_id = ? AND version < ? "
                "ORDER BY version DESC LIMIT 1",
                (policy_id, latest["version"]),
            ).fetchone()
            if not previous:
                raise ValueError("首个规定版本不能通过撤销补充删除")
            snapshot = _json(previous["snapshot_json"], {})
            connection.execute("DELETE FROM company_policy_versions WHERE id = ?", (latest["id"],))
            connection.execute(
                "UPDATE company_policies SET title = ?, publisher = ?, topic = ?, scope = ?, "
                "summary = ?, requirements_json = ?, effective_date = ?, status = ?, version = ?, "
                "last_verified_at = ?, updated_at = ? WHERE id = ?",
                (
                    snapshot["title"], snapshot["publisher"], snapshot["topic"], snapshot["scope"],
                    snapshot["summary"], json.dumps(snapshot["requirements"], ensure_ascii=False),
                    snapshot.get("effective_date"), snapshot["status"], previous["version"],
                    snapshot.get("last_verified_at"), utc_now(), policy_id,
                ),
            )
            connection.execute(
                "UPDATE company_policy_candidates SET status = 'undone', resolved_at = ?, "
                "updated_at = ? WHERE id = ?",
                (utc_now(), utc_now(), candidate["id"]),
            )
            self._sync_policy(connection, str(policy_id))
        self._audit(actor, "policy.candidate.undone", candidate["id"], str(policy_id))
        return self.get_candidate(candidate["id"])

    def sync_obsidian(self) -> dict[str, Any]:
        try:
            with self.database.connect() as connection:
                ids = [row["id"] for row in connection.execute("SELECT id FROM company_policies")]
                for policy_id in ids:
                    self._sync_policy(connection, str(policy_id), update_state=False)
                if not ids:
                    self._write_batch(
                        [
                            (
                                self.root / "00-公司最新规定总览.md",
                                self._render_index(connection).encode("utf-8"),
                            )
                        ]
                    )
                self._update_sync_state(connection, "completed", len(ids), "")
        except OSError as error:
            with self.database.connect() as connection:
                self._update_sync_state(connection, "failed", 0, str(error))
            raise
        return self.status()["obsidian"]

    def record_sync_error(self, candidate_id: str, error: OSError) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE company_policy_candidates SET obsidian_error = ?, updated_at = ? WHERE id = ?",
                (str(error)[:1000], utc_now(), candidate_id),
            )
            self._update_sync_state(connection, "failed", 0, str(error))

    def _sync_policy(
        self, connection: Any, policy_id: str, *, update_state: bool = True
    ) -> None:
        row = connection.execute(
            "SELECT * FROM company_policies WHERE id = ?", (policy_id,)
        ).fetchone()
        if not row:
            return
        policy = dict(row)
        versions = [
            dict(item)
            for item in connection.execute(
                "SELECT * FROM company_policy_versions WHERE policy_id = ? ORDER BY version",
                (policy_id,),
            ).fetchall()
        ]
        category = _safe_name(policy["topic"] or "综合管理", "综合管理")
        title = _safe_name(policy["title"])
        current_path = self.root / "10-当前有效" / category / f"{title}.md"
        old_path = Path(policy["obsidian_path"]) if policy.get("obsidian_path") else None
        operations: list[tuple[Path, bytes | None]] = []
        if old_path and old_path != current_path:
            operations.append((old_path, None))
        if policy["status"] == "active":
            operations.append(
                (current_path, self._render_note(policy, versions, current=True).encode("utf-8"))
            )
            obsidian_path = str(current_path)
        else:
            operations.append((current_path, None))
            obsidian_path = ""
        connection.execute(
            "UPDATE company_policies SET obsidian_path = ?, updated_at = ? WHERE id = ?",
            (obsidian_path, utc_now(), policy_id),
        )
        policy["obsidian_path"] = obsidian_path

        for version in versions:
            created = str(version.get("created_at") or "")[:10].replace("-", "") or _date_stamp()
            history = self.root / "90-历史版本" / category / title / f"v{version['version']}-{created}.md"
            operations.append(
                (history, self._render_version(version).encode("utf-8"))
            )
            operations.extend(self._attachment_operations(policy, version))
        operations.append((self.root / "00-公司最新规定总览.md", self._render_index(connection).encode("utf-8")))
        self._write_batch(operations)
        if update_state:
            self._update_sync_state(connection, "completed", 1, "")

    def _attachment_operations(
        self, policy: dict[str, Any], version: dict[str, Any]
    ) -> list[tuple[Path, bytes]]:
        operations: list[tuple[Path, bytes]] = []
        for value in _json(version.get("attachments_json"), []):
            source = Path(str(value)).expanduser()
            if source.suffix.lower() not in FORMAL_EXTENSIONS or not source.is_file():
                continue
            content = source.read_bytes()
            digest = hashlib.sha256(content).hexdigest()[:12]
            target = self.root / "附件" / _safe_name(policy["topic"]) / _safe_name(policy["title"]) / f"{digest}-{_safe_name(source.name)}"
            if not target.exists():
                operations.append((target, content))
        return operations

    @staticmethod
    def _write_batch(operations: list[tuple[Path, bytes | None]]) -> None:
        originals: dict[Path, bytes | None] = {}
        completed: list[Path] = []
        try:
            for path, content in operations:
                originals[path] = path.read_bytes() if path.exists() else None
                if content is None:
                    if path.exists():
                        path.unlink()
                    completed.append(path)
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                temporary.write_bytes(content)
                os.chmod(temporary, 0o600)
                temporary.replace(path)
                completed.append(path)
        except OSError:
            for path in reversed(completed):
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(original)
            raise

    def _render_index(self, connection: Any) -> str:
        rows = connection.execute(
            "SELECT title, publisher, topic, effective_date, status, obsidian_path, updated_at "
            "FROM company_policies ORDER BY status, topic, updated_at DESC"
        ).fetchall()
        active = [row for row in rows if row["status"] == "active"]
        retired = [row for row in rows if row["status"] != "active"]
        lines = [
            "# 公司最新规定",
            "",
            "> 由 Frank 的个人工作台持续整理。新增、修订和废止均保留来源与版本记录。",
            "",
            f"- 当前有效：{len(active)} 条",
            f"- 已废止：{len(retired)} 条",
            f"- 最近同步：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}",
            "",
            "## 当前有效规定",
            "",
        ]
        for row in active:
            path = Path(row["obsidian_path"]) if row["obsidian_path"] else None
            link = f"[[{path.stem}]]" if path else row["title"]
            lines.append(
                f"- {link}｜{row['publisher']}｜{row['topic']}｜生效日期：{row['effective_date'] or '待确认'}"
            )
        lines.extend(["", "## 已废止", ""])
        for row in retired:
            lines.append(f"- {row['title']}｜{row['publisher']}｜{row['topic']}")
        return "\n".join(lines).rstrip() + "\n"

    def _render_note(
        self, policy: dict[str, Any], versions: list[dict[str, Any]], *, current: bool
    ) -> str:
        requirements = _json(policy["requirements_json"], [])
        latest = versions[-1] if versions else {}
        evidence = _json(latest.get("evidence_json"), [])[:4]
        lines = [
            "---",
            f"状态: {'当前有效' if policy['status'] == 'active' else '已废止'}",
            f"发布单位: {_clean(policy['publisher'], 160)}",
            f"主题: {_clean(policy['topic'], 160)}",
            f"版本: {policy['version']}",
            f"生效日期: {policy['effective_date'] or ''}",
            f"最后核实: {policy['last_verified_at'] or ''}",
            "---",
            "",
            f"# {policy['title']}",
            "",
            "## 一句话规定",
            "",
            policy["summary"],
            "",
            "## 适用范围",
            "",
            policy["scope"] or "待进一步明确",
            "",
            "## 具体要求与执行口径",
            "",
        ]
        lines.extend(f"- {item}" for item in requirements)
        if not requirements:
            lines.append("- 以来源原文和后续确认内容为准。")
        lines.extend(["", "## 来源证据", ""])
        lines.extend(f"- {item}" for item in evidence)
        if not evidence:
            lines.append("- 来源证据已保存在本机工作台。")
        lines.extend(["", "## 版本记录", ""])
        for version in reversed(versions):
            lines.append(
                f"- v{version['version']}｜{CHANGE_LABELS.get(version['change_type'], version['change_type'])}｜{version['created_at']}"
            )
        return "\n".join(lines).rstrip() + "\n"

    def _render_version(self, version: dict[str, Any]) -> str:
        snapshot = _json(version["snapshot_json"], {})
        policy = {
            **snapshot,
            "requirements_json": json.dumps(snapshot.get("requirements") or [], ensure_ascii=False),
        }
        return self._render_note(policy, [version], current=False)

    @staticmethod
    def _policy_snapshot(policy: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": policy["title"],
            "publisher": policy["publisher"],
            "topic": policy["topic"],
            "scope": policy["scope"],
            "summary": policy["summary"],
            "requirements": _json(policy["requirements_json"], []),
            "effective_date": policy["effective_date"],
            "status": policy["status"],
            "version": policy["version"],
            "last_verified_at": policy["last_verified_at"],
        }

    def list_policies(self, status: str = "active", query: str = "") -> list[dict[str, Any]]:
        if status not in {"active", "retired", "all"}:
            raise ValueError("不支持的规定状态")
        clauses: list[str] = []
        params: list[Any] = []
        if status != "all":
            clauses.append("status = ?")
            params.append(status)
        if query.strip():
            clauses.append("(title LIKE ? OR publisher LIKE ? OR topic LIKE ? OR summary LIKE ?)")
            value = f"%{query.strip()}%"
            params.extend((value, value, value, value))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.database.fetch_all(
            f"SELECT * FROM company_policies {where} ORDER BY updated_at DESC", tuple(params)
        )
        return [self._policy_public(row) for row in rows]

    def versions(self, policy_id: str) -> list[dict[str, Any]]:
        rows = self.database.fetch_all(
            "SELECT version, change_type, snapshot_json, evidence_json, attachments_json, created_at "
            "FROM company_policy_versions WHERE policy_id = ? ORDER BY version DESC",
            (policy_id,),
        )
        for row in rows:
            row["change_label"] = CHANGE_LABELS.get(row["change_type"], "规定更新")
            row["snapshot"] = _json(row.pop("snapshot_json"), {})
            row["evidence"] = _json(row.pop("evidence_json"), [])
            row["attachments"] = _json(row.pop("attachments_json"), [])
        return rows

    def identity_hint(self, source_type: str, source_key: str) -> dict[str, Any]:
        normalized_key = _clean(source_key, 300)
        if source_type not in SOURCE_TYPES or not normalized_key:
            return {}
        row = self.database.fetch_one(
            "SELECT publisher, is_authority, confidence, updated_at "
            "FROM company_policy_identity_feedback WHERE source_type = ? AND source_key = ?",
            (source_type, normalized_key),
        )
        return dict(row) if row else {}

    def list_candidates(self, status: str = "pending", limit: int = 100) -> list[dict[str, Any]]:
        if status not in {"pending", "applied", "auto_applied", "ignored", "temporary", "undone", "all"}:
            raise ValueError("不支持的规定候选状态")
        where = "" if status == "all" else "WHERE c.status = ?"
        params: tuple[Any, ...] = (min(max(limit, 1), 300),) if status == "all" else (status, min(max(limit, 1), 300))
        rows = self.database.fetch_all(
            "SELECT c.*, p.title AS matched_policy_title FROM company_policy_candidates c "
            "LEFT JOIN company_policies p ON p.id = c.matched_policy_id "
            f"{where} ORDER BY c.created_at DESC LIMIT ?",
            params,
        )
        return [self._candidate_public(row) for row in rows]

    def get_candidate(self, candidate_id: str) -> dict[str, Any]:
        row = self.database.fetch_one(
            "SELECT c.*, p.title AS matched_policy_title FROM company_policy_candidates c "
            "LEFT JOIN company_policies p ON p.id = c.matched_policy_id WHERE c.id = ?",
            (candidate_id,),
        )
        if not row:
            raise KeyError("规定候选不存在")
        return self._candidate_public(row)

    def status(self) -> dict[str, Any]:
        counts = self.database.fetch_one(
            "SELECT "
            "SUM(status = 'pending') AS pending, "
            "SUM(status IN ('applied', 'auto_applied') AND date(resolved_at) = date('now')) AS updated_today, "
            "SUM(change_type = 'new' AND status IN ('applied', 'auto_applied')) AS added, "
            "SUM(change_type = 'revision' AND status IN ('applied', 'auto_applied')) AS revised "
            "FROM company_policy_candidates"
        ) or {}
        policy_counts = self.database.fetch_one(
            "SELECT SUM(status = 'active') AS active, SUM(status = 'retired') AS retired FROM company_policies"
        ) or {}
        sync = self.database.fetch_one("SELECT * FROM company_policy_sync_state WHERE id = 1") or {
            "status": "idle",
            "written_count": 0,
            "last_success_at": None,
            "error": "",
            "updated_at": None,
        }
        sync.pop("id", None)
        sync["path"] = str(self.root)
        return {
            "counts": {**{key: int(value or 0) for key, value in counts.items()}, **{key: int(value or 0) for key, value in policy_counts.items()}},
            "obsidian": sync,
        }

    @staticmethod
    def _candidate_public(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["requirements"] = _json(result.pop("requirements_json", "[]"), [])
        result["evidence"] = _json(result.pop("evidence_json", "[]"), [])
        result["attachments"] = _json(result.pop("attachments_json", "[]"), [])
        result["source_name"] = SOURCE_LABELS.get(result.get("source_type"), "工作消息")
        result["change_label"] = CHANGE_LABELS.get(result.get("change_type"), "规定更新")
        result["status_label"] = STATUS_LABELS.get(result.get("status"), "等待处理")
        return result

    @staticmethod
    def _policy_public(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["requirements"] = _json(result.pop("requirements_json", "[]"), [])
        result["status_label"] = "当前有效" if result["status"] == "active" else "已废止"
        return result

    @staticmethod
    def _update_sync_state(connection: Any, status: str, count: int, error: str) -> None:
        now = utc_now()
        connection.execute(
            "INSERT INTO company_policy_sync_state "
            "(id, status, written_count, last_success_at, error, updated_at) "
            "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "status = excluded.status, written_count = excluded.written_count, "
            "last_success_at = CASE WHEN excluded.status = 'completed' THEN excluded.last_success_at "
            "ELSE company_policy_sync_state.last_success_at END, error = excluded.error, "
            "updated_at = excluded.updated_at",
            (status, count, now if status == "completed" else None, error[:1000], now),
        )

    def _audit(self, actor: str, action: str, candidate_id: str, policy_id: str | None = None) -> None:
        self.database.audit(
            _id("audit"), actor, action, "company_policy_candidate", candidate_id,
            metadata={"policy_id": policy_id} if policy_id else {},
        )
