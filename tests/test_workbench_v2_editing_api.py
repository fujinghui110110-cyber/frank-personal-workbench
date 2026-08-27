from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "workbench",
        owner_passcode="owner-pass",
        session_secret="test-session-secret",
        owner_token="owner-token",
        worker_token="worker-token",
        mcp_token="mcp-token",
        policy_vault_dir=tmp_path / "vault" / "公司最新规定",
    )
    with TestClient(create_app(settings)) as test_client:
        response = test_client.post("/api/auth/login", json={"passcode": "owner-pass"})
        assert response.status_code == 200, response.text
        yield test_client


def seed_email(client: TestClient) -> dict[str, Any]:
    account_id = "a" * 64
    response = client.post(
        "/api/email/accounts/register",
        json={
            "account_id": account_id,
            "address_hint": "f***@example.com",
            "imap_host": "imap.example.com",
            "folder": "INBOX",
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(
        "/api/email/messages",
        json={
            "account_id": account_id,
            "folder": "INBOX",
            "uid_validity": "uid-validity",
            "uid": 1,
            "message_id_hash": "message-hash-1",
            "thread_key": "thread-edit-1",
            "sender_key": "sender-edit-1",
            "sender_name": "业务联系人",
            "sender_hint": "b***@example.com",
            "subject": "原始邮件主题",
            "sent_at": "2026-08-26T02:00:00Z",
            "classification": "pending",
            "needs_follow_up": False,
            "summary": "",
            "reason": "",
            "evidence": [],
            "matter_title": "",
            "actions": [],
            "source_text": "原始邮件正文，不应被人工摘要覆盖。",
            "attachment_paths": [],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def seed_wechat(client: TestClient) -> dict[str, Any]:
    timestamp = 1_777_000_000
    response = client.post(
        "/api/wechat/windows",
        json={
            "source": "personal_wechat",
            "account_fingerprint": "wechat-account-edit",
            "session_id": "wechat-edit-session",
            "display_name": "合成业务会话",
            "kind": "friend",
            "messages": [
                {
                    "timestamp": timestamp,
                    "timestampMs": timestamp * 1000,
                    "direction": "in",
                    "kind": "text",
                    "text": "原始聊天材料，不应被线索摘要覆盖。",
                    "cursor": {
                        "sortSeq": 1,
                        "createTime": timestamp,
                        "localId": 1,
                    },
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def seed_policy_records(client: TestClient) -> dict[str, str]:
    database = client.app.state.database
    material_text = "原始规定材料，不应被候选编辑覆盖。"
    material_id = "policy-material-edit"
    candidate_id = "policy-candidate-edit"
    policy_id = "policy-edit"
    now = "2026-08-26T00:00:00Z"
    snapshot = {
        "id": policy_id,
        "title": "原规定标题",
        "publisher": "合成发布方",
        "topic": "采购管理",
        "scope": "球会采购",
        "summary": "原规定摘要",
        "requirements": ["原规定要求"],
        "effective_date": "2026-08-01",
        "status": "active",
        "version": 1,
        "updated_at": now,
    }
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO materials "
            "(id, idempotency_key, sha256, source_type, filename, content_type, size, "
            "text_note, status, received_at, updated_at, metadata_json) "
            "VALUES (?, ?, ?, 'wecom', ?, 'text/plain', ?, ?, 'processed', ?, ?, '{}')",
            (
                material_id,
                "synthetic:policy-material-edit",
                hashlib.sha256(material_text.encode()).hexdigest(),
                "合成规定材料.txt",
                len(material_text.encode()),
                material_text,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO company_policy_candidates "
            "(id, source_type, source_ref, source_label, material_id, change_type, title, "
            "publisher, topic, scope, summary, requirements_json, change_summary, "
            "effective_date, confidence, is_authority, evidence_json, attachments_json, "
            "status, created_at, updated_at) "
            "VALUES (?, 'wecom', ?, '合成规定来源', ?, 'revision', ?, ?, ?, ?, ?, ?, ?, ?, "
            "0.8, 1, '[]', '[]', 'pending', ?, ?)",
            (
                candidate_id,
                "synthetic:policy-candidate-edit",
                material_id,
                "原候选标题",
                "合成发布方",
                "采购管理",
                "球会采购",
                "原候选摘要",
                json.dumps(["原候选要求"], ensure_ascii=False),
                "原候选变更说明",
                "2026-08-01",
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO company_policies "
            "(id, canonical_key, title, publisher, topic, scope, summary, requirements_json, "
            "effective_date, status, version, obsidian_path, last_verified_at, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, '', ?, ?, ?)",
            (
                policy_id,
                "synthetic-policy-key-edit",
                snapshot["title"],
                snapshot["publisher"],
                snapshot["topic"],
                snapshot["scope"],
                snapshot["summary"],
                json.dumps(snapshot["requirements"], ensure_ascii=False),
                snapshot["effective_date"],
                now,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO company_policy_versions "
            "(id, policy_id, version, change_type, snapshot_json, evidence_json, "
            "attachments_json, candidate_id, created_at) "
            "VALUES (?, ?, 1, 'new', ?, '[]', '[]', ?, ?)",
            (
                "policy-version-edit-1",
                policy_id,
                json.dumps(snapshot, ensure_ascii=False),
                candidate_id,
                now,
            ),
        )
    return {
        "material_id": material_id,
        "candidate_id": candidate_id,
        "policy_id": policy_id,
        "updated_at": now,
    }


def audit_metadata(client: TestClient, object_id: str) -> dict[str, Any]:
    row = client.app.state.database.fetch_one(
        "SELECT metadata_json FROM audit_events WHERE object_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT 1",
        (object_id,),
    )
    assert row is not None
    return json.loads(row["metadata_json"])


def audit_ids(client: TestClient, object_id: str) -> list[str]:
    return [
        row["id"]
        for row in client.app.state.database.fetch_all(
            "SELECT id FROM audit_events WHERE object_id = ? ORDER BY created_at, id",
            (object_id,),
        )
    ]


def test_editing_routes_preserve_sources_audit_changes_and_policy_history(
    client: TestClient,
) -> None:
    email = seed_email(client)
    email_response = client.patch(
        f"/api/email/messages/{email['id']}",
        json={
            "subject": "人工修订邮件主题",
            "summary": "人工修订邮件摘要",
            "expected_updated_at": email["updated_at"],
            "reason": "人工复核邮件原文后修订摘要",
        },
    )
    assert email_response.status_code == 200, email_response.text
    database = client.app.state.database
    email_row = database.fetch_one(
        "SELECT * FROM email_messages WHERE id = ?", (email["id"],)
    )
    assert email_row is not None
    assert email_row["subject"] == "人工修订邮件主题"
    assert email_row["summary"] == "人工修订邮件摘要"
    assert email_row["source_text"] == ""
    email_material = database.fetch_one(
        "SELECT text_note FROM materials WHERE id = ?", (email_row["material_id"],)
    )
    assert email_material == {"text_note": "原始邮件正文，不应被人工摘要覆盖。"}
    assert audit_metadata(client, email["id"]) == {
        "old": {"subject": "原始邮件主题", "summary": "",},
        "new": {"subject": "人工修订邮件主题", "summary": "人工修订邮件摘要"},
        "reason": "人工复核邮件原文后修订摘要",
    }

    wechat = seed_wechat(client)
    wechat_id = wechat["candidate_id"]
    wechat_before = database.fetch_one(
        "SELECT updated_at, material_id FROM wechat_candidates WHERE id = ?",
        (wechat_id,),
    )
    assert wechat_before is not None
    wechat_material_before = database.fetch_one(
        "SELECT text_note FROM materials WHERE id = ?",
        (wechat_before["material_id"],),
    )
    wechat_response = client.patch(
        f"/api/wechat/candidates/{wechat_id}",
        json={
            "summary": "人工修订聊天线索摘要",
            "classification": "relevant",
            "extracted": {"matter_title": "人工确认事项"},
            "evidence": ["人工核对原始聊天"],
            "expected_updated_at": wechat_before["updated_at"],
            "reason": "人工核对聊天材料后修订线索",
        },
    )
    assert wechat_response.status_code == 200, wechat_response.text
    assert database.fetch_one(
        "SELECT text_note FROM materials WHERE id = ?",
        (wechat_before["material_id"],),
    ) == wechat_material_before
    assert audit_metadata(client, wechat_id) == {
        "old": {
            "summary": "等待 贾维斯 理解",
            "classification": None,
            "extracted": {},
            "evidence": [],
        },
        "new": {
            "summary": "人工修订聊天线索摘要",
            "classification": "relevant",
            "extracted": {"matter_title": "人工确认事项"},
            "evidence": ["人工核对原始聊天"],
        },
        "reason": "人工核对聊天材料后修订线索",
    }

    policy = seed_policy_records(client)
    candidate_response = client.patch(
        f"/api/policy-candidates/{policy['candidate_id']}",
        json={
            "summary": "人工修订候选摘要",
            "requirements": ["人工修订候选要求"],
            "expected_updated_at": policy["updated_at"],
            "reason": "人工核对规定材料后修订候选",
        },
    )
    assert candidate_response.status_code == 200, candidate_response.text
    assert database.fetch_one(
        "SELECT text_note FROM materials WHERE id = ?", (policy["material_id"],)
    ) == {"text_note": "原始规定材料，不应被候选编辑覆盖。"}
    assert audit_metadata(client, policy["candidate_id"]) == {
        "before": {"summary": "原候选摘要", "requirements": ["原候选要求"]},
        "after": {
            "summary": "人工修订候选摘要",
            "requirements": ["人工修订候选要求"],
        },
        "reason": "人工核对规定材料后修订候选",
    }

    policy_row = database.fetch_one(
        "SELECT updated_at FROM company_policies WHERE id = ?", (policy["policy_id"],)
    )
    assert policy_row is not None
    policy_response = client.patch(
        f"/api/policies/{policy['policy_id']}",
        json={
            "summary": "人工修订现行规定摘要",
            "change_type": "revision",
            "expected_updated_at": policy_row["updated_at"],
            "reason": "人工复核现行规定后更新摘要",
        },
    )
    assert policy_response.status_code == 200, policy_response.text
    assert policy_response.json()["version"] == 2
    versions_response = client.get(f"/api/policies/{policy['policy_id']}/versions")
    assert versions_response.status_code == 200, versions_response.text
    versions = versions_response.json()
    assert [version["version"] for version in versions] == [2, 1]
    assert versions[0]["snapshot"]["summary"] == "人工修订现行规定摘要"
    assert versions[1]["snapshot"]["summary"] == "原规定摘要"
    metadata = audit_metadata(client, policy["policy_id"])
    assert metadata["reason"] == "人工复核现行规定后更新摘要"
    assert metadata["before"]["summary"] == "原规定摘要"
    assert metadata["after"]["summary"] == "人工修订现行规定摘要"


def test_editing_conflicts_return_409_without_partial_writes(
    client: TestClient,
) -> None:
    database = client.app.state.database

    email = seed_email(client)
    first_email = client.patch(
        f"/api/email/messages/{email['id']}",
        json={
            "summary": "第一次人工邮件编辑",
            "expected_updated_at": email["updated_at"],
            "reason": "第一次人工编辑",
        },
    )
    assert first_email.status_code == 200, first_email.text
    email_after = database.fetch_one(
        "SELECT subject, summary, updated_at, source_text FROM email_messages WHERE id = ?",
        (email["id"],),
    )
    email_audits = audit_ids(client, email["id"])
    second_email = client.patch(
        f"/api/email/messages/{email['id']}",
        json={
            "summary": "不应写入的邮件编辑",
            "expected_updated_at": email["updated_at"],
            "reason": "第二次冲突编辑",
        },
    )
    assert second_email.status_code == 409, second_email.text
    assert database.fetch_one(
        "SELECT subject, summary, updated_at, source_text FROM email_messages WHERE id = ?",
        (email["id"],),
    ) == email_after
    assert audit_ids(client, email["id"]) == email_audits

    wechat = seed_wechat(client)
    wechat_id = wechat["candidate_id"]
    wechat_before = database.fetch_one(
        "SELECT * FROM wechat_candidates WHERE id = ?", (wechat_id,)
    )
    assert wechat_before is not None
    first_wechat = client.patch(
        f"/api/wechat/candidates/{wechat_id}",
        json={
            "summary": "第一次人工聊天编辑",
            "expected_updated_at": wechat_before["updated_at"],
            "reason": "第一次人工编辑",
        },
    )
    assert first_wechat.status_code == 200, first_wechat.text
    wechat_after = database.fetch_one(
        "SELECT * FROM wechat_candidates WHERE id = ?", (wechat_id,)
    )
    wechat_audits = audit_ids(client, wechat_id)
    second_wechat = client.patch(
        f"/api/wechat/candidates/{wechat_id}",
        json={
            "summary": "不应写入的聊天编辑",
            "expected_updated_at": wechat_before["updated_at"],
            "reason": "第二次冲突编辑",
        },
    )
    assert second_wechat.status_code == 409, second_wechat.text
    assert database.fetch_one(
        "SELECT * FROM wechat_candidates WHERE id = ?", (wechat_id,)
    ) == wechat_after
    assert audit_ids(client, wechat_id) == wechat_audits

    policy = seed_policy_records(client)
    first_candidate = client.patch(
        f"/api/policy-candidates/{policy['candidate_id']}",
        json={
            "summary": "第一次人工候选编辑",
            "expected_updated_at": policy["updated_at"],
            "reason": "第一次人工编辑",
        },
    )
    assert first_candidate.status_code == 200, first_candidate.text
    candidate_after = database.fetch_one(
        "SELECT * FROM company_policy_candidates WHERE id = ?",
        (policy["candidate_id"],),
    )
    candidate_audits = audit_ids(client, policy["candidate_id"])
    second_candidate = client.patch(
        f"/api/policy-candidates/{policy['candidate_id']}",
        json={
            "summary": "不应写入的候选编辑",
            "expected_updated_at": policy["updated_at"],
            "reason": "第二次冲突编辑",
        },
    )
    assert second_candidate.status_code == 409, second_candidate.text
    assert database.fetch_one(
        "SELECT * FROM company_policy_candidates WHERE id = ?",
        (policy["candidate_id"],),
    ) == candidate_after
    assert audit_ids(client, policy["candidate_id"]) == candidate_audits

    policy_before = database.fetch_one(
        "SELECT * FROM company_policies WHERE id = ?", (policy["policy_id"],)
    )
    assert policy_before is not None
    first_policy = client.patch(
        f"/api/policies/{policy['policy_id']}",
        json={
            "summary": "第一次人工现行规定编辑",
            "change_type": "revision",
            "expected_updated_at": policy["updated_at"],
            "reason": "第一次人工编辑",
        },
    )
    assert first_policy.status_code == 200, first_policy.text
    policy_after = database.fetch_one(
        "SELECT * FROM company_policies WHERE id = ?", (policy["policy_id"],)
    )
    assert policy_after is not None
    candidate_count = database.fetch_one(
        "SELECT COUNT(*) AS count FROM company_policy_candidates"
    )
    version_count = database.fetch_one(
        "SELECT COUNT(*) AS count FROM company_policy_versions WHERE policy_id = ?",
        (policy["policy_id"],),
    )
    policy_audits = audit_ids(client, policy["policy_id"])
    second_policy = client.patch(
        f"/api/policies/{policy['policy_id']}",
        json={
            "summary": "不应写入的现行规定编辑",
            "change_type": "revision",
            "expected_updated_at": policy["updated_at"],
            "reason": "第二次冲突编辑",
        },
    )
    assert second_policy.status_code == 409, second_policy.text
    assert database.fetch_one(
        "SELECT * FROM company_policies WHERE id = ?", (policy["policy_id"],)
    ) == policy_after
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM company_policy_candidates"
    ) == candidate_count
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM company_policy_versions WHERE policy_id = ?",
        (policy["policy_id"],),
    ) == version_count
    assert audit_ids(client, policy["policy_id"]) == policy_audits


def seed_transactional_edit_records(client: TestClient) -> dict[str, str]:
    database = client.app.state.database
    matter_id = "transaction-matter"
    action_id = "transaction-action"
    reminder_id = "transaction-reminder"
    now = "2026-08-26T00:00:00Z"
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO matters (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (matter_id, "事务一致性测试事项", now, now),
        )
        connection.execute(
            "INSERT INTO actions "
            "(id, matter_id, kind, title, created_by, created_at, updated_at) "
            "VALUES (?, ?, 'task', ?, 'tester', ?, ?)",
            (action_id, matter_id, "事务一致性测试行动", now, now),
        )
        connection.execute(
            "INSERT INTO reminders "
            "(id, matter_id, action_id, kind, title, reason, fingerprint, created_at, updated_at) "
            "VALUES (?, ?, ?, 'follow_up', ?, ?, ?, ?, ?)",
            (
                reminder_id,
                matter_id,
                action_id,
                "事务一致性测试提醒",
                "原提醒原因",
                "transactional-edit-reminder",
                now,
                now,
            ),
        )
    return {
        "matter_id": matter_id,
        "action_id": action_id,
        "reminder_id": reminder_id,
        "updated_at": now,
    }


@pytest.mark.parametrize("failure", ["audit", "matter_event"])
def test_workbench_edits_roll_back_when_audit_or_matter_event_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    records = seed_transactional_edit_records(client)
    database = client.app.state.database
    service = client.app.state.service

    if failure == "audit":
        def fail_audit(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("injected audit failure")

        monkeypatch.setattr(database, "audit", fail_audit)
        expected_error = "injected audit failure"
    else:
        def fail_matter_event(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("injected matter event failure")

        monkeypatch.setattr(service, "record_matter_event", fail_matter_event)
        expected_error = "injected matter event failure"

    cases = [
        (
            f"/api/matters/{records['matter_id']}",
            {"summary": "不应提交的事项修改", "expected_updated_at": records["updated_at"]},
            "SELECT summary, updated_at FROM matters WHERE id = ?",
            (records["matter_id"],),
            records["matter_id"],
        ),
        (
            f"/api/actions/{records['action_id']}/planning-state",
            {"flow_state": "blocked", "expected_updated_at": records["updated_at"]},
            "SELECT flow_state, updated_at FROM actions WHERE id = ?",
            (records["action_id"],),
            records["action_id"],
        ),
        (
            f"/api/reminders/{records['reminder_id']}",
            {"reason": "不应提交的提醒修改", "expected_updated_at": records["updated_at"]},
            "SELECT reason, updated_at FROM reminders WHERE id = ?",
            (records["reminder_id"],),
            records["reminder_id"],
        ),
    ]

    for path, payload, row_query, row_params, object_id in cases:
        before = database.fetch_one(row_query, row_params)
        assert before is not None
        with pytest.raises(RuntimeError, match=expected_error):
            client.patch(path, json=payload)
        assert database.fetch_one(row_query, row_params) == before
        assert database.fetch_one(
            "SELECT COUNT(*) AS count FROM audit_events WHERE object_id = ?",
            (object_id,),
        ) == {"count": 0}
        assert database.fetch_one(
            "SELECT COUNT(*) AS count FROM matter_events WHERE matter_id = ?",
            (records["matter_id"],),
        ) == {"count": 0}
