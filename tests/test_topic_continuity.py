from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.wechat import review_worthy_wechat_result


ACCOUNT = {
    "account_id": "c" * 64,
    "address_hint": "f***@example.com",
    "imap_host": "imap.example.com",
    "folder": "INBOX",
}


@pytest.fixture
def client(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "workbench",
        owner_passcode="owner-pass",
        session_secret="test-session-secret",
        owner_token="owner-token",
        worker_token="worker-token",
        mcp_token="mcp-token",
        max_upload_bytes=4096,
        lease_seconds=1,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _pending_email(uid: int) -> dict:
    return {
        "account_id": ACCOUNT["account_id"],
        "folder": "INBOX",
        "uid_validity": "123",
        "uid": uid,
        "message_id_hash": f"hash-{uid}",
        "thread_key": f"unrelated-thread-{uid}",
        "sender_key": "sender-key",
        "sender_name": "业务联系人",
        "sender_hint": "b***@example.com",
        "subject": "供应商合同付款复核",
        "sent_at": "2026-09-08T02:00:00Z",
        "classification": "pending",
        "needs_follow_up": False,
        "summary": "",
        "reason": "",
        "evidence": [],
        "matter_title": "",
        "actions": [],
        "source_text": "主题：供应商合同付款复核\n正文：请继续复核付款资料。",
    }


def test_media_placeholder_needs_real_text_or_a_read_media_result() -> None:
    result = {
        "classification": "relevant",
        "confidence": 0.97,
        "summary": "合同付款资料需要复核",
        "evidence": ["图片显示合同付款资料待复核"],
        "extracted": {
            "matter_title": "合同付款复核",
            "actions": [{"kind": "task", "title": "复核合同付款资料"}],
        },
    }
    placeholder = "个人微信会话：供应商\n时间：今天\n对方：非文字消息 [附件：图片]"

    assert review_worthy_wechat_result(result, placeholder) is False
    assert review_worthy_wechat_result(
        result, placeholder, media_was_read=True
    ) is True


def test_email_follow_up_reuses_cross_source_matter_without_duplicate_action(
    client: TestClient,
) -> None:
    database = client.app.state.database
    now = "2026-09-08T01:00:00Z"
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO matters (id, title, summary, created_at, updated_at) "
            "VALUES ('matter-chat', '供应商合同付款复核', '人工确认后的摘要', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO actions (id, matter_id, kind, title, detail, status, created_by, created_at, updated_at) "
            "VALUES ('action-chat', 'matter-chat', 'task', '复核供应商合同付款', '', 'open', 'manual', ?, ?)",
            (now, now),
        )
    client.app.state.email_work.register_account(ACCOUNT)
    saved = client.app.state.email_work.ingest_message(_pending_email(1))

    completed = client.app.state.email_work.complete_analysis(
        saved["id"],
        {
            "classification": "work",
            "confidence": 0.98,
            "needs_follow_up": True,
            "summary": "合同付款资料需要继续复核",
            "reason": "邮件包含明确复核要求",
            "matter_title": "供应商合同付款复核",
            "evidence": ["请继续复核付款资料"],
            "actions": [{"kind": "task", "title": "继续复核供应商合同付款"}],
        },
    )

    assert completed["matter_id"] == "matter-chat"
    matter = database.fetch_one("SELECT summary FROM matters WHERE id = 'matter-chat'")
    assert matter == {"summary": "人工确认后的摘要"}
    count = database.fetch_one(
        "SELECT COUNT(*) AS count FROM actions WHERE matter_id = 'matter-chat' AND status = 'open'"
    )
    assert count == {"count": 1}
    event = database.fetch_one(
        "SELECT event_type, object_type, summary, payload_json FROM matter_events "
        "WHERE matter_id = 'matter-chat' ORDER BY created_at DESC LIMIT 1"
    )
    assert event["event_type"] == "source.follow_up"
    assert event["object_type"] == "email_message"
    assert json.loads(event["payload_json"])["continued_action_ids"] == ["action-chat"]


def test_batch_compares_new_chat_with_older_pending_confirmation(client: TestClient) -> None:
    database = client.app.state.database
    now = datetime.now(UTC)
    old_at = (now - timedelta(days=9)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    released_at = (now - timedelta(days=8)).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    delayed_at = (now - timedelta(days=7, hours=1)).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    extracted = json.dumps(
        {
            "matter_title": "供应商合同付款复核",
            "actions": [{"kind": "task", "title": "复核供应商合同付款"}],
        },
        ensure_ascii=False,
    )
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO wechat_conversations "
            "(session_id, source, display_name, kind, last_message_at, created_at, updated_at) "
            "VALUES ('supplier-chat', 'personal_wechat', '供应商', 'private', ?, ?, ?)",
            (delayed_at,) * 3,
        )
        connection.execute(
            "INSERT INTO audit_events "
            "(id, actor, action, object_type, object_id, metadata_json, created_at) "
            "VALUES ('audit-release', 'owner', 'analysis.released', 'analysis', 'pending', '{}', ?)",
            (released_at,),
        )
        for suffix, updated_at, text in (
            ("old", old_at, "供应商合同付款请复核"),
            ("new", delayed_at, "供应商合同付款继续复核"),
        ):
            connection.execute(
                "INSERT INTO materials "
                "(id, idempotency_key, sha256, source_type, filename, content_type, size, "
                "text_note, status, received_at, updated_at, metadata_json) "
                "VALUES (?, ?, ?, 'wechat_auto', ?, 'text/plain', 0, ?, 'pending_review', ?, ?, '{}')",
                (
                    f"mat-{suffix}",
                    f"key-{suffix}",
                    f"hash-{suffix}",
                    f"chat-{suffix}.txt",
                    text,
                    updated_at,
                    updated_at,
                ),
            )
            connection.execute(
                "INSERT INTO wechat_candidates "
                "(id, material_id, session_id, source, window_start, window_end, classification, "
                "summary, confidence, status, evidence_json, extracted_json, created_at, updated_at) "
                "VALUES (?, ?, 'supplier-chat', 'personal_wechat', ?, ?, 'relevant', ?, 0.9, "
                "'pending', ?, ?, ?, ?)",
                (
                    f"candidate-{suffix}",
                    f"mat-{suffix}",
                    updated_at,
                    updated_at,
                    "供应商合同付款需要复核",
                    json.dumps([text], ensure_ascii=False),
                    extracted,
                    updated_at,
                    updated_at,
                ),
            )

    result = client.app.state.wechat.consolidate_pending_candidates()

    assert result == {"ignored": 0, "merged": 1, "continued": 0}
    statuses = database.fetch_all(
        "SELECT id, status FROM wechat_candidates ORDER BY id"
    )
    assert statuses == [
        {"id": "candidate-new", "status": "retracted"},
        {"id": "candidate-old", "status": "pending"},
    ]
