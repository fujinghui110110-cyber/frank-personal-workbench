from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.wechat import _merged_summary, _public_candidate, review_worthy_wechat_result
from scripts import wechat_sync
from scripts.wechat_sync import (
    _is_missing_session,
    group_messages,
    run_personal_wechat_sync,
)


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
        lease_seconds=30,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"passcode": "owner-pass"})
    assert response.status_code == 200


def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-token"}


def test_only_missing_ciphertalk_sessions_are_skipped() -> None:
    assert _is_missing_session(RuntimeError("Session not found."))
    assert not _is_missing_session(RuntimeError("CipherTalk 尚未准备好"))


def test_personal_wechat_reader_switch_uses_direct_as_default(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.delenv("PERSONAL_WECHAT_READER", raising=False)
    monkeypatch.setattr(
        "scripts.personal_wechat_sync.run_direct_wechat_sync",
        lambda _client, mode: calls.append(("direct", mode)) or {"messages": 0},
    )
    assert run_personal_wechat_sync(object(), "incremental") == {"messages": 0}
    assert calls == [("direct", "incremental")]


def test_personal_wechat_reader_switch_uses_direct_only_when_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PERSONAL_WECHAT_READER", "direct")
    monkeypatch.setattr(
        "scripts.personal_wechat_sync.run_direct_wechat_sync",
        lambda _client, mode: {"messages": 2, "mode": mode},
    )
    assert run_personal_wechat_sync(object(), "rescan") == {
        "messages": 2,
        "mode": "rescan",
    }


def test_wecom_bridge_notice_is_filtered_without_breaking_pagination() -> None:
    class CipherTalk:
        async def call(self, method, payload=None):
            assert method == "get_messages"
            if payload["offset"] == 0:
                return {
                    "items": [
                        message(
                            1,
                            1_800_000_000,
                            "你收到一条消息，请在企业微信中查看",
                        )
                    ],
                    "hasMore": True,
                }
            return {
                "items": [message(2, 1_800_000_001, "请跟进合同付款")],
                "hasMore": False,
            }

    result = asyncio.run(wechat_sync._messages(CipherTalk(), "session", 1))

    assert [item["messageId"] for item in result] == [2]


def message(local_id: int, timestamp: int, text: str = "请跟进合同付款") -> dict:
    return {
        "messageId": local_id,
        "timestamp": timestamp,
        "timestampMs": timestamp * 1000,
        "direction": "in",
        "kind": "text",
        "text": text,
        "cursor": {"sortSeq": local_id, "createTime": timestamp, "localId": local_id},
        "sender": {"username": "contact", "isSelf": False},
    }


def window_payload(messages: list[dict], session_id: str = "session-a") -> dict:
    return {
        "account_fingerprint": "account-fingerprint",
        "session_id": session_id,
        "display_name": "财务协同群",
        "kind": "group",
        "messages": messages,
    }


def test_message_windows_split_on_silence_and_limit() -> None:
    base = 1_800_000_000
    messages = [message(index + 1, base + index) for index in range(41)]
    messages.append(message(99, base + 3 * 60 * 60))
    windows = group_messages(messages)
    assert [len(item) for item in windows] == [42]


def test_merged_candidate_summary_stays_readable() -> None:
    summary = _merged_summary(
        "；".join(f"第{index}条需要持续跟进的业务线索" for index in range(30))
    )

    assert len(summary) <= 280
    assert "另有" in summary


def test_waiting_business_status_is_localized_without_rewriting_storage_status() -> (
    None
):
    candidate = _public_candidate(
        {
            "status": "processing",
            "material_status": "queued",
            "evidence_json": "[]",
            "extracted_json": "{}",
        }
    )

    assert candidate["status"] == "processing"
    assert candidate["material_status"] == "queued"
    assert candidate["status_label"] == "等待整理"
    assert candidate["material_status_label"] == "等待整理"


def test_only_concrete_work_with_a_follow_up_reaches_confirmation() -> None:
    relation_only = {
        "classification": "relevant",
        "confidence": 0.98,
        "evidence": ["我是新任财务对接人，已添加好友"],
        "extracted": {
            "matter_title": "建立财务联系",
            "actions": [{"kind": "waiting", "title": "等待后续沟通"}],
        },
    }
    no_follow_up = {
        "classification": "relevant",
        "confidence": 0.96,
        "evidence": ["合同付款资料已收到"],
        "extracted": {"matter_title": "合同付款", "actions": []},
    }
    concrete = {
        "classification": "relevant",
        "confidence": 0.93,
        "evidence": ["合同明天付款"],
        "extracted": {
            "matter_title": "合同付款",
            "actions": [{"kind": "task", "title": "明天安排合同付款"}],
        },
    }
    media_only = {
        "classification": "relevant",
        "confidence": 0.99,
        "evidence": ["非文字消息 [附件：image]"],
        "extracted": {
            "matter_title": "图片材料",
            "actions": [{"kind": "task", "title": "查看图片"}],
        },
    }

    assert not review_worthy_wechat_result(
        relation_only, "对方：我是新任财务对接人，已添加好友"
    )
    assert not review_worthy_wechat_result(no_follow_up, "对方：合同付款资料已收到")
    assert review_worthy_wechat_result(concrete, "对方：合同明天付款")
    assert not review_worthy_wechat_result(media_only, "对方：非文字消息 [附件：image]")


def _complete_next_wechat_job(client: TestClient, result: dict) -> dict:
    released = client.post("/api/analysis/run")
    assert released.status_code == 200, released.text
    claimed = client.post(
        "/api/jobs/claim", json={"worker_id": "merge-test"}, headers=worker_headers()
    ).json()["job"]
    lease = {"worker_id": "merge-test", "lease_token": claimed["lease_token"]}
    client.post(
        f"/api/jobs/{claimed['id']}/start", json=lease, headers=worker_headers()
    )
    response = client.post(
        f"/api/wechat/jobs/{claimed['id']}/complete",
        json={**lease, "result": result},
        headers=worker_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_same_conversation_keeps_one_pending_confirmation_and_all_evidence(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    results = [
        {
            "classification": "relevant",
            "summary": "需要核对合同付款安排",
            "confidence": 0.92,
            "evidence": ["请核对合同付款"],
            "extracted": {
                "matter_title": "合同付款",
                "amounts": [],
                "dates": [],
                "people": [],
                "approval": "",
                "risks": [],
                "actions": [{"kind": "task", "title": "核对合同付款"}],
            },
        },
        {
            "classification": "relevant",
            "summary": "合同付款时间更新为明天",
            "confidence": 0.94,
            "evidence": ["合同付款明天处理"],
            "extracted": {
                "matter_title": "合同付款",
                "amounts": [],
                "dates": ["明天"],
                "people": [],
                "approval": "",
                "risks": [],
                "actions": [{"kind": "task", "title": "明天处理合同付款"}],
            },
        },
    ]
    created = []
    for index, result in enumerate(results, start=1):
        created.append(
            client.post(
                "/api/wechat/windows",
                json=window_payload(
                    [message(100 + index, now + index * 3600, result["evidence"][0])]
                ),
                headers=worker_headers(),
            ).json()
        )
        _complete_next_wechat_job(client, result)

    assert (
        len(client.get("/api/wechat/candidates?candidate_status=pending").json()) == 2
    )
    reconciled = client.post("/api/analysis/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    pending = client.get("/api/wechat/candidates?candidate_status=pending").json()

    assert len(pending) == 1
    assert set(pending[0]["evidence"]) == {"请核对合同付款", "合同付款明天处理"}
    assert pending[0]["window_start"] < pending[0]["window_end"]
    statuses = client.app.state.database.fetch_all(
        "SELECT status FROM wechat_candidates ORDER BY created_at, id"
    )
    assert sorted(item["status"] for item in statuses) == ["pending", "retracted"]
    assert all(
        client.app.state.database.fetch_one(
            "SELECT id FROM materials WHERE id = ?", (item["material_id"],)
        )
        for item in created
    )


def test_same_conversation_different_topics_keep_separate_pending_cards(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    results = [
        {
            "classification": "relevant",
            "summary": "供应商付款需要复核",
            "confidence": 0.94,
            "evidence": ["供应商付款请今天复核"],
            "extracted": {
                "matter_title": "供应商付款复核",
                "amounts": ["10万元"],
                "dates": ["今天"],
                "people": [],
                "approval": "待确认",
                "risks": [],
                "actions": [{"kind": "task", "title": "复核供应商付款"}],
            },
        },
        {
            "classification": "relevant",
            "summary": "员工招聘面试需要安排",
            "confidence": 0.94,
            "evidence": ["请安排下周招聘面试"],
            "extracted": {
                "matter_title": "员工招聘面试",
                "amounts": [],
                "dates": ["下周"],
                "people": [],
                "approval": "",
                "risks": [],
                "actions": [{"kind": "task", "title": "安排招聘面试"}],
            },
        },
    ]

    for index, result in enumerate(results, start=1):
        created = client.post(
            "/api/wechat/windows",
            json=window_payload(
                [message(300 + index, now + index * 3600, result["evidence"][0])]
            ),
            headers=worker_headers(),
        )
        assert created.status_code == 200, created.text
        _complete_next_wechat_job(client, result)

    pending = client.get("/api/wechat/candidates?candidate_status=pending")
    assert pending.status_code == 200, pending.text
    cards = pending.json()
    assert len(cards) == 2
    assert {item["extracted"]["matter_title"] for item in cards} == {
        "供应商付款复核",
        "员工招聘面试",
    }


def test_high_confidence_new_chat_continues_one_open_matter_with_audit(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    first = client.post(
        "/api/wechat/windows",
        json=window_payload(
            [message(501, now, "合同付款请继续跟进")], session_id="old-chat"
        ),
        headers=worker_headers(),
    ).json()
    first_result = {
        "classification": "relevant",
        "summary": "合同付款需要持续跟进",
        "confidence": 0.96,
        "evidence": ["合同付款请继续跟进"],
        "extracted": {
            "matter_title": "合同付款",
            "actions": [{"kind": "task", "title": "跟进合同付款"}],
        },
    }
    _complete_next_wechat_job(client, first_result)
    accepted = client.post(
        f"/api/wechat/candidates/{first['candidate_id']}/resolve",
        json={"action": "accept"},
    )
    assert accepted.status_code == 200, accepted.text
    matter_id = accepted.json()["matter_id"]

    second = client.post(
        "/api/wechat/windows",
        json=window_payload(
            [message(502, now + 3600, "合同付款明天继续处理")], session_id="new-chat"
        ),
        headers=worker_headers(),
    ).json()
    _complete_next_wechat_job(
        client,
        {
            "classification": "relevant",
            "summary": "合同付款时间更新",
            "confidence": 0.95,
            "evidence": ["合同付款明天继续处理"],
            "extracted": {
                "matter_title": "合同付款",
                "actions": [{"kind": "task", "title": "明天继续处理合同付款"}],
            },
        },
    )

    reconciled = client.post("/api/analysis/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    continued = client.get(
        "/api/wechat/candidates?candidate_status=accepted&limit=20"
    ).json()
    new_candidate = next(
        item for item in continued if item["id"] == second["candidate_id"]
    )
    assert new_candidate["matter_id"] == matter_id
    assert new_candidate["status_label"] == "已纳入事项"
    audit = client.get("/api/audit?limit=100").json()
    assert any(
        item["action"] == "wechat.candidate.accepted"
        and item["object_id"] == second["candidate_id"]
        for item in audit
    )


def test_batch_reconcile_can_continue_a_non_chat_open_matter(
    client: TestClient,
) -> None:
    login(client)
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    with client.app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO matters (id, title, summary, created_at, updated_at) "
            "VALUES ('matter-existing', '预算差异原因核对', '等待补充差异说明', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO actions (id, matter_id, kind, title, detail, status, created_by, created_at, updated_at) "
            "VALUES ('action-existing', 'matter-existing', 'task', '继续核对预算差异原因', '', 'open', 'test', ?, ?)",
            (now, now),
        )
    epoch = int(datetime.now(UTC).timestamp())
    created = client.post(
        "/api/wechat/windows",
        json=window_payload(
            [message(620, epoch, "预算差异原因明天继续核对")],
            session_id="budget-follow-up",
        ),
        headers=worker_headers(),
    ).json()
    _complete_next_wechat_job(
        client,
        {
            "classification": "relevant",
            "summary": "预算差异原因需要继续核对",
            "confidence": 0.96,
            "evidence": ["预算差异原因明天继续核对"],
            "extracted": {
                "matter_title": "预算差异原因核对",
                "actions": [{"kind": "task", "title": "继续核对预算差异原因"}],
            },
        },
    )
    reconciled = client.post("/api/analysis/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    accepted = client.get(
        "/api/wechat/candidates?candidate_status=accepted&limit=20"
    ).json()
    candidate = next(item for item in accepted if item["id"] == created["candidate_id"])
    assert candidate["matter_id"] == "matter-existing"


def test_low_confidence_or_conflicting_new_chat_stays_pending(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    created = client.post(
        "/api/wechat/windows",
        json=window_payload(
            [message(601, now, "合同付款可能是另一项")], session_id="uncertain-chat"
        ),
        headers=worker_headers(),
    ).json()
    _complete_next_wechat_job(
        client,
        {
            "classification": "relevant",
            "summary": "可能涉及另一项合同付款",
            "confidence": 0.88,
            "evidence": ["合同付款可能是另一项"],
            "extracted": {
                "matter_title": "合同付款",
                "conflicts": ["无法确认是否为原事项"],
                "actions": [{"kind": "task", "title": "确认合同归属"}],
            },
        },
    )
    pending = client.get(
        "/api/wechat/candidates?candidate_status=pending&limit=20"
    ).json()
    candidate = next(item for item in pending if item["id"] == created["candidate_id"])
    assert candidate["status"] == "pending"
    assert candidate["status_label"] == "待确认"


def test_existing_low_value_pending_candidate_is_auto_ignored(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    created = client.post(
        "/api/wechat/windows",
        json=window_payload(
            [message(201, now, "别打卡"), message(202, now + 1, "好的")]
        ),
        headers=worker_headers(),
    ).json()
    _complete_next_wechat_job(
        client,
        {
            "classification": "uncertain",
            "summary": "可能是关于考勤打卡的简短指令，但没有上下文",
            "uncertainty_reason": "无法判断是不是工作，且没有金额、日期、审批等上下文",
            "confidence": 0.55,
            "evidence": ["别打卡", "好的"],
            "extracted": {
                "matter_title": "打卡安排",
                "amounts": [],
                "dates": [],
                "people": [],
                "approval": "",
                "risks": [],
                "actions": [],
            },
        },
    )
    with client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE wechat_candidates SET classification = 'uncertain', status = 'pending', "
            "resolved_at = NULL WHERE id = ?",
            (created["candidate_id"],),
        )
        connection.execute(
            "UPDATE materials SET status = 'pending_review' WHERE id = ?",
            (created["material_id"],),
        )

    result = client.app.state.wechat.consolidate_pending_candidates()

    assert result == {"ignored": 1, "merged": 0, "continued": 0}
    candidate = client.app.state.database.fetch_one(
        "SELECT classification, status FROM wechat_candidates WHERE id = ?",
        (created["candidate_id"],),
    )
    assert candidate == {"classification": "irrelevant", "status": "ignored"}


def test_new_high_confidence_wechat_task_enters_open_matters_after_reconcile(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    created = client.post(
        "/api/wechat/windows",
        json=window_payload(
            [message(301, now, "请赵楠完成月度预算差异复核并反馈")],
            session_id="new-actionable-task",
        ),
        headers=worker_headers(),
    ).json()
    _complete_next_wechat_job(
        client,
        {
            "classification": "relevant",
            "summary": "月度预算差异需要复核并反馈",
            "confidence": 0.96,
            "evidence": ["请赵楠完成月度预算差异复核并反馈"],
            "extracted": {
                "matter_title": "月度预算差异复核",
                "actions": [
                    {
                        "kind": "task",
                        "title": "复核月度预算差异并反馈",
                        "owner": "赵楠",
                    }
                ],
            },
        },
    )

    reconciled = client.post("/api/analysis/reconcile")

    assert reconciled.status_code == 200, reconciled.text
    candidate = next(
        item
        for item in client.get(
            "/api/wechat/candidates?candidate_status=accepted&limit=20"
        ).json()
        if item["id"] == created["candidate_id"]
    )
    matter = next(
        item
        for item in client.get("/api/matters?limit=500").json()
        if item["id"] == candidate["matter_id"]
    )
    assert matter["title"] == "月度预算差异复核"
    assert matter["is_completed"] is False
    assert matter["open_action_count"] == 1


def test_initial_sync_reads_only_the_last_week(monkeypatch) -> None:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    class Client:
        def request_json(self, method, path, payload=None):
            assert (method, path, payload) == (
                "GET",
                "/api/wechat/conversations?source=personal_wechat",
                None,
            )
            return []

    class CipherTalk:
        def __init__(self) -> None:
            self.message_calls = []

        async def call(self, method, payload=None):
            if method == "get_status":
                return {"activeAccountId": "test-account"}
            if method == "list_sessions":
                return {
                    "items": [
                        {
                            "sessionId": "recent",
                            "displayName": "近期会话",
                            "kind": "friend",
                            "lastTimestampMs": now_ms - 6 * 24 * 60 * 60 * 1000,
                        },
                        {
                            "sessionId": "old",
                            "displayName": "过往会话",
                            "kind": "friend",
                            "lastTimestampMs": now_ms - 8 * 24 * 60 * 60 * 1000,
                        },
                    ],
                    "hasMore": False,
                }
            if method == "get_messages":
                self.message_calls.append(payload)
                return {"items": [], "hasMore": False}
            raise AssertionError(method)

    ciphertalk = CipherTalk()

    @asynccontextmanager
    async def fake_open_ciphertalk():
        yield ciphertalk

    monkeypatch.setattr(wechat_sync, "open_ciphertalk", fake_open_ciphertalk)

    result = wechat_sync.run_ciphertalk_sync(Client())

    assert result == {"messages": 0, "windows": 0, "skipped": 0}
    assert [item["sessionId"] for item in ciphertalk.message_calls] == ["recent"]
    assert ciphertalk.message_calls[0]["startTime"] >= (
        now_ms - 7 * 24 * 60 * 60 * 1000 - 2_000
    )


def test_media_messages_are_enriched_before_intake(monkeypatch) -> None:
    class CipherTalk:
        async def call(self, method, payload=None):
            assert method == "transcribe_voice_message"
            assert payload == {
                "sessionId": "session-media",
                "localId": 301,
                "createTime": 1_800_000_000,
            }
            return {"transcript": "请明天核对合同付款"}

    messages = [
        message(301, 1_800_000_000, "")
        | {"kind": "voice", "media": {"type": "voice", "localPath": "/tmp/a.silk"}},
        message(302, 1_800_000_001, "")
        | {"kind": "image", "media": {"type": "image", "localPath": "/tmp/a.png"}},
    ]
    monkeypatch.setattr(wechat_sync, "_ocr_image", lambda path: "发票金额 100 元")

    enriched = asyncio.run(
        wechat_sync._enrich_messages(CipherTalk(), "session-media", messages)
    )

    assert enriched[0]["text"] == "[语音转写] 请明天核对合同付款"
    assert enriched[0]["media"]["transcript"] == "请明天核对合同付款"
    assert enriched[1]["text"] == "[图片识别] 发票金额 100 元"
    assert enriched[1]["media"]["ocrText"] == "发票金额 100 元"


def test_media_recognition_failure_does_not_block_sync(monkeypatch) -> None:
    class CipherTalk:
        async def call(self, method, payload=None):
            raise RuntimeError("语音模型暂时不可用")

    def fail_ocr(path):
        raise RuntimeError("图片暂时无法识别")

    messages = [
        message(401, 1_800_000_000, "")
        | {"kind": "voice", "media": {"type": "voice", "localPath": "/tmp/a.silk"}},
        message(402, 1_800_000_001, "")
        | {"kind": "image", "media": {"type": "image", "localPath": "/tmp/a.png"}},
    ]
    monkeypatch.setattr(wechat_sync, "_ocr_image", fail_ocr)

    enriched = asyncio.run(
        wechat_sync._enrich_messages(CipherTalk(), "session-media", messages)
    )

    assert [item["text"] for item in enriched] == ["", ""]


def test_wechat_window_accepts_the_sync_window_limit(client: TestClient) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    payload = window_payload([message(index, now + index) for index in range(1, 201)])

    response = client.post(
        "/api/wechat/windows", json=payload, headers=worker_headers()
    )

    assert response.status_code == 200, response.text


def test_wechat_ingest_is_idempotent_and_validates_cursor(client: TestClient) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    payload = window_payload([message(1, now), message(1, now)])
    first = client.post("/api/wechat/windows", json=payload, headers=worker_headers())
    assert first.status_code == 200, first.text
    assert first.json()["created"] is True
    duplicate = client.post(
        "/api/wechat/windows", json=payload, headers=worker_headers()
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["created"] is False
    bad = payload | {
        "messages": [{"timestamp": now, "direction": "in", "kind": "text"}]
    }
    assert (
        client.post(
            "/api/wechat/windows", json=bad, headers=worker_headers()
        ).status_code
        == 422
    )


def test_failed_media_holds_cursor_while_later_text_is_saved_and_deduplicated(
    client: TestClient,
) -> None:
    login(client)
    base = int(datetime.now(UTC).timestamp()) - 3600
    first = message(1, base, "合同付款文本先保存")
    failed = message(2, base + 10 * 60, "") | {
        "kind": "image",
        "media": {
            "type": "image",
            "localPath": "/tmp/retry-image.png",
            "extractionStatus": "failed",
        },
    }
    later = message(3, base + 20 * 60, "预算差异继续核对")
    payload = window_payload([first, failed, later], session_id="retry-session")

    initial = client.post(
        "/api/wechat/windows", json=payload, headers=worker_headers()
    )

    assert initial.status_code == 200, initial.text
    assert initial.json()["created"] is True
    material = client.app.state.database.fetch_one(
        "SELECT text_note FROM materials WHERE id = ?", (initial.json()["material_id"],)
    )
    assert "合同付款文本先保存" in material["text_note"]
    assert "预算差异继续核对" in material["text_note"]
    assert "retry-image" not in material["text_note"]
    cursor = client.app.state.database.fetch_one(
        "SELECT create_time, local_id FROM wechat_sync_state WHERE session_id = 'retry-session'"
    )
    assert cursor == {"create_time": base + 10 * 60, "local_id": 2}

    repaired = {**failed, "media": {**failed["media"], "extractionStatus": "completed"}}
    retry_payload = window_payload([first, repaired, later], session_id="retry-session")
    retried = client.post(
        "/api/wechat/windows", json=retry_payload, headers=worker_headers()
    )
    duplicate = client.post(
        "/api/wechat/windows", json=retry_payload, headers=worker_headers()
    )

    assert retried.status_code == 200, retried.text
    assert retried.json()["created"] is True
    assert duplicate.json() == {"created": False, "reason": "duplicate"}
    seen = client.app.state.database.fetch_one(
        "SELECT COUNT(*) AS count FROM wechat_seen_messages WHERE session_id = 'retry-session'"
    )
    assert seen == {"count": 3}
    cursor = client.app.state.database.fetch_one(
        "SELECT create_time, local_id FROM wechat_sync_state WHERE session_id = 'retry-session'"
    )
    assert cursor == {"create_time": base + 20 * 60, "local_id": 3}


def test_successful_media_evidence_survives_storage_and_batch_review(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    image_message = message(50, now, "") | {
        "kind": "image",
        "media": {
            "type": "image",
            "localPath": "/tmp/contract.png",
            "extractionStatus": "completed",
        },
    }
    created = client.post(
        "/api/wechat/windows",
        json=window_payload([image_message], session_id="media-evidence-session"),
        headers=worker_headers(),
    )
    assert created.status_code == 200, created.text
    _complete_next_wechat_job(
        client,
        {
            "classification": "relevant",
            "summary": "合同付款资料需要复核",
            "confidence": 0.90,
            "evidence": ["图片显示合同付款资料待复核"],
            "extracted": {
                "matter_title": "合同付款复核",
                "actions": [{"kind": "task", "title": "复核合同付款资料"}],
            },
            "media_evidence": [
                {
                    "source": "/tmp/contract.png",
                    "media_type": "image/png",
                    "status": "model_processed",
                }
            ],
        },
    )

    before = client.app.state.database.fetch_one(
        "SELECT status, extracted_json FROM wechat_candidates WHERE id = ?",
        (created.json()["candidate_id"],),
    )
    assert before["status"] == "pending"
    assert (
        json.loads(before["extracted_json"])["media_evidence"][0]["status"]
        == "model_processed"
    )

    result = client.app.state.wechat.consolidate_pending_candidates()

    assert result == {"ignored": 0, "merged": 0, "continued": 0}
    after = client.app.state.database.fetch_one(
        "SELECT status, extracted_json FROM wechat_candidates WHERE id = ?",
        (created.json()["candidate_id"],),
    )
    assert after["status"] == "pending"
    assert json.loads(after["extracted_json"])["media_evidence"] == json.loads(
        before["extracted_json"]
    )["media_evidence"]


def test_conversations_list_active_by_latest_then_blocked(client: TestClient) -> None:
    login(client)
    base = int(datetime.now(UTC).timestamp())
    for session_id, offset in (
        ("active-old", 10),
        ("blocked-newest", 30),
        ("active-new", 20),
    ):
        response = client.post(
            "/api/wechat/windows",
            json=window_payload(
                [message(offset, base + offset)], session_id=session_id
            ),
            headers=worker_headers(),
        )
        assert response.status_code == 200, response.text

    assert (
        client.post("/api/wechat/conversations/blocked-newest/block").status_code == 200
    )
    conversations = client.get("/api/wechat/conversations").json()

    assert [item["session_id"] for item in conversations] == [
        "active-new",
        "active-old",
        "blocked-newest",
    ]


def test_technical_session_id_is_not_shown_as_conversation_name(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    payload = window_payload([message(70, now)], session_id="52280232047@chatroom")
    payload["display_name"] = payload["session_id"]

    response = client.post(
        "/api/wechat/windows",
        json=payload,
        headers=worker_headers(),
    )

    assert response.status_code == 200, response.text
    conversations = client.get("/api/wechat/conversations").json()
    assert conversations[0]["display_name"] == "未命名群聊"


def test_chat_export_only_returns_listening_conversations(client: TestClient) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    active = client.post(
        "/api/wechat/windows",
        json=window_payload([message(71, now)], session_id="export-active"),
        headers=worker_headers(),
    ).json()
    client.post(
        "/api/wechat/windows",
        json=window_payload([message(72, now + 1)], session_id="export-blocked"),
        headers=worker_headers(),
    )
    client.post("/api/wechat/conversations/export-blocked/block")

    rows = client.get("/api/wechat/export?limit=1&offset=0").json()

    assert len(rows) == 1
    assert rows[0]["material_id"] == active["material_id"]
    assert rows[0]["session_id"] == "export-active"
    assert "text_note" in rows[0]


def test_sender_display_name_is_used_and_repairs_existing_candidate(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    old_message = message(801, now, "请 Hank 跟进合同付款")
    old_message["sender"] = {
        "username": "pandora110",
        "isSelf": False,
    }
    created = client.post(
        "/api/wechat/windows",
        json=window_payload([old_message], session_id="sender-old"),
        headers=worker_headers(),
    ).json()
    _complete_next_wechat_job(
        client,
        {
            "classification": "relevant",
            "summary": "pandora110 要求跟进合同付款",
            "confidence": 0.94,
            "evidence": ["pandora110：请跟进合同付款"],
            "extracted": {
                "matter_title": "合同付款",
                "amounts": [],
                "dates": [],
                "people": ["pandora110"],
                "approval": "",
                "risks": [],
                "actions": [{"kind": "task", "title": "跟进合同付款"}],
            },
        },
    )

    new_message = message(802, now + 10, "补充合同付款时间")
    new_message["sender"] = {
        "username": "pandora110",
        "displayName": "Hank",
        "isSelf": False,
    }
    client.post(
        "/api/wechat/windows",
        json=window_payload([new_message], session_id="sender-new"),
        headers=worker_headers(),
    )

    material = client.get(f"/api/materials/{created['material_id']}").json()
    assert "pandora110" not in material["text_note"]
    assert "Hank" in material["text_note"]
    candidate = client.get("/api/wechat/candidates?candidate_status=pending").json()[0]
    serialized = json.dumps(candidate, ensure_ascii=False)
    assert "pandora110" not in serialized
    assert "Hank" in serialized


def test_same_matter_is_merged_across_conversations(client: TestClient) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    cases = [
        (
            "case-private",
            "请更新牧阳人案件合同策划报告 PPT",
            "更新《牧阳人诉球会案件合同策划报告》",
        ),
        (
            "case-lawyers",
            "请核实牧羊人租赁协议和主体更换证据",
            "牧羊人租赁协议",
        ),
    ]
    for index, (session_id, text, matter_title) in enumerate(cases, start=1):
        client.post(
            "/api/wechat/windows",
            json=window_payload(
                [message(820 + index, now + index, text)],
                session_id=session_id,
            ),
            headers=worker_headers(),
        )
        _complete_next_wechat_job(
            client,
            {
                "classification": "relevant",
                "summary": text,
                "confidence": 0.95,
                "evidence": [text],
                "extracted": {
                    "matter_title": matter_title,
                    "amounts": [],
                    "dates": [],
                    "people": [],
                    "approval": "",
                    "risks": [],
                    "actions": [{"kind": "task", "title": text}],
                },
            },
        )

    reconciled = client.post("/api/analysis/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    accepted = client.get("/api/wechat/candidates?candidate_status=accepted").json()
    assert len(accepted) == 1
    assert len(accepted[0]["evidence"]) == 2
    assert len(accepted[0]["extracted"]["actions"]) == 2
    assert len(client.get("/api/matters?limit=500").json()) == 1


def test_accepted_follow_up_reuses_only_open_same_topic_matter(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())

    def create_and_accept(
        session_id: str, local_id: int, matter_title: str, text: str
    ) -> dict:
        created = client.post(
            "/api/wechat/windows",
            json=window_payload([message(local_id, now + local_id, text)], session_id),
            headers=worker_headers(),
        ).json()
        _complete_next_wechat_job(
            client,
            {
                "classification": "relevant",
                "summary": text,
                "confidence": 0.95,
                "evidence": [text],
                "extracted": {
                    "matter_title": matter_title,
                    "amounts": [],
                    "dates": [],
                    "people": [],
                    "approval": "",
                    "risks": [],
                    "actions": [{"kind": "task", "title": text}],
                },
            },
        )
        response = client.post(
            f"/api/wechat/candidates/{created['candidate_id']}/resolve",
            json={"action": "accept", "matter_id": None},
        )
        assert response.status_code == 200, response.text
        return response.json()

    first = create_and_accept(
        "accepted-private",
        901,
        "牧阳人合同纠纷案件",
        "请整理牧阳人合同纠纷证据",
    )
    follow_up = create_and_accept(
        "accepted-lawyers",
        902,
        "牧羊人案件证据收集",
        "律师补充了牧羊人案件会议纪要，请继续核实",
    )
    assert follow_up["matter_id"] == first["matter_id"]
    matter = client.get(f"/api/matters/{first['matter_id']}").json()
    assert len(matter["materials"]) == 2
    assert matter["assistant"]["display_name"] == "贾维斯"
    assert matter["assistant"]["source"] == "事项推进方案"

    other_topic = create_and_accept(
        "accepted-lawyers",
        903,
        "游艇保险续保",
        "请确认游艇保险续保报价",
    )
    assert other_topic["matter_id"] != first["matter_id"]

    for action in matter["actions"]:
        resolved = client.post(
            f"/api/actions/{action['id']}/resolve", json={"status": "done"}
        )
        assert resolved.status_code == 200, resolved.text

    reopened_topic = create_and_accept(
        "accepted-new-round",
        904,
        "牧羊人合同纠纷后续",
        "牧羊人旧事项完成后出现新的处理要求",
    )
    assert reopened_topic["matter_id"] != first["matter_id"]


def test_export_status_is_reported_in_wechat_status(client: TestClient) -> None:
    login(client)
    report = client.post(
        "/api/wechat/export/report",
        json={
            "status": "completed",
            "conversation_count": 63,
            "message_count": 4214,
            "file_count": 63,
            "output_paths": [
                "/Users/frank/微信数据",
                "/Users/frank/企业微信导出数据/texts",
            ],
            "error": "",
        },
        headers=worker_headers(),
    )
    assert report.status_code == 200, report.text
    export = client.get("/api/wechat/status").json()["export"]
    assert export["status"] == "completed"
    assert export["conversation_count"] == 63
    assert export["message_count"] == 4214
    assert export["output_paths"][0].endswith("微信数据")


def test_block_conversation_ignores_candidates_and_is_idempotent(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    created = client.post(
        "/api/wechat/windows",
        json=window_payload([message(50, now)], session_id="session-block"),
        headers=worker_headers(),
    ).json()

    first = client.post("/api/wechat/conversations/session-block/block")
    second = client.post("/api/wechat/conversations/session-block/block")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["listen_status"] == "blocked"
    ignored = client.get("/api/wechat/candidates?candidate_status=ignored").json()
    assert [item["id"] for item in ignored] == [created["candidate_id"]]
    assert client.get("/api/wechat/candidates?candidate_status=processing").json() == []
    assert (
        client.get(f"/api/materials/{created['material_id']}").json()["status"]
        == "processed"
    )

    unblocked = client.post(
        "/api/wechat/conversations/session-block/unblock",
        json={"rescan_days": None},
    )
    assert unblocked.status_code == 200, unblocked.text
    assert [
        item["id"]
        for item in client.get("/api/wechat/candidates?candidate_status=ignored").json()
    ] == [created["candidate_id"]]


def test_wechat_classification_accept_ignore_restore_and_block(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    created = client.post(
        "/api/wechat/windows",
        json=window_payload([message(2, now)]),
        headers=worker_headers(),
    ).json()
    released = client.post("/api/analysis/run")
    assert released.status_code == 200, released.text
    claimed = client.post(
        "/api/jobs/claim", json={"worker_id": "mac-test"}, headers=worker_headers()
    ).json()["job"]
    lease = {"worker_id": "mac-test", "lease_token": claimed["lease_token"]}
    client.post(
        f"/api/jobs/{claimed['id']}/start", json=lease, headers=worker_headers()
    )
    with client.app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO reminders "
            "(id, matter_id, action_id, kind, title, reason, status, fingerprint, "
            "due_at, created_at, updated_at) "
            "VALUES (?, NULL, NULL, 'processing', ?, ?, 'open', ?, NULL, ?, ?)",
            (
                "reminder-wechat-format-error",
                "本地处理需要关注",
                "WorkBuddy 返回格式不完整",
                f"job:{claimed['id']}:retryable_failed",
                "2026-08-19T08:00:00Z",
                "2026-08-19T08:00:00Z",
            ),
        )
    result = {
        "classification": "relevant",
        "summary": "需要跟进合同付款时间",
        "uncertainty_reason": "",
        "confidence": 0.92,
        "evidence": ["请跟进合同付款"],
        "extracted": {
            "matter_title": "合同付款跟进",
            "actions": [{"kind": "task", "title": "确认付款日期"}],
        },
    }
    completed = client.post(
        f"/api/wechat/jobs/{claimed['id']}/complete",
        json={**lease, "result": result},
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text
    reminder = client.app.state.database.fetch_one(
        "SELECT status FROM reminders WHERE id = ?",
        ("reminder-wechat-format-error",),
    )
    assert reminder == {"status": "done"}
    pending = client.get("/api/wechat/candidates?candidate_status=pending").json()
    assert pending[0]["id"] == created["candidate_id"]
    ignored = client.post(
        f"/api/wechat/candidates/{created['candidate_id']}/resolve",
        json={"action": "ignore", "matter_id": None},
    )
    assert ignored.status_code == 200
    restored = client.post(
        f"/api/wechat/candidates/{created['candidate_id']}/resolve",
        json={"action": "restore", "matter_id": None},
    )
    assert restored.json()["status"] == "pending"
    accepted = client.post(
        f"/api/wechat/candidates/{created['candidate_id']}/resolve",
        json={"action": "accept", "matter_id": None},
    )
    assert accepted.json()["status"] == "accepted"
    blocked = client.post("/api/wechat/conversations/session-a/block")
    assert blocked.json()["listen_status"] == "blocked"
    suppressed = client.post(
        "/api/wechat/windows",
        json=window_payload([message(3, now + 1)]),
        headers=worker_headers(),
    )
    assert suppressed.json()["reason"] == "blocked"
    unblocked = client.post(
        "/api/wechat/conversations/session-a/unblock", json={"rescan_days": None}
    )
    assert unblocked.json()["listen_status"] == "active"


def test_wechat_sync_request_lifecycle(client: TestClient) -> None:
    login(client)
    requested = client.post(
        "/api/wechat/sync/run", json={"sources": ["personal_wechat"]}
    )
    assert requested.json()["requests"][0]["status"] == "pending"
    claimed = client.post(
        "/api/wechat/sync/claim",
        json={"worker_id": "mac-test"},
        headers=worker_headers(),
    ).json()["request"]
    assert claimed["status"] == "running"
    finished = client.post(
        f"/api/wechat/sync/{claimed['id']}/finish",
        json={
            "worker_id": "mac-test",
            "status": "completed",
            "error": "",
            "message_count": 18,
            "window_count": 4,
            "skipped_count": 2,
        },
        headers=worker_headers(),
    )
    assert finished.json()["status"] == "completed"
    latest = client.get("/api/wechat/status").json()["sources"]["personal_wechat"][
        "latest"
    ]
    assert latest["message_count"] == 18
    assert latest["window_count"] == 4
    assert latest["skipped_count"] == 2


@pytest.mark.parametrize(
    ("error", "message"),
    [
        ("个人微信密钥不完整，需要重新配置", "需要重新配置密钥"),
        ("当前微信版本暂不支持", "当前微信版本暂不支持"),
    ],
)
def test_personal_wechat_direct_failures_use_business_status(
    client: TestClient, error: str, message: str
) -> None:
    login(client)
    client.post("/api/wechat/sync/run", json={"sources": ["personal_wechat"]})
    claimed = client.post(
        "/api/wechat/sync/claim",
        json={"worker_id": "mac-test"},
        headers=worker_headers(),
    ).json()["request"]
    client.post(
        f"/api/wechat/sync/{claimed['id']}/finish",
        json={"worker_id": "mac-test", "status": "failed", "error": error},
        headers=worker_headers(),
    )
    source = client.get("/api/wechat/status").json()["sources"]["personal_wechat"]
    assert source["available"] is False
    assert source["message"] == message


def test_default_chat_sync_requests_both_sources(client: TestClient) -> None:
    login(client)
    response = client.post("/api/wechat/sync/run")
    assert response.status_code == 200
    assert {item["source"] for item in response.json()["requests"]} == {
        "personal_wechat",
        "wecom",
    }


def test_chat_sources_are_namespaced_and_filtered(client: TestClient) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    personal = window_payload([message(901, now)], session_id="shared-session")
    wecom = window_payload([message(901, now)], session_id="shared-session")
    wecom["source"] = "wecom"
    wecom["display_name"] = "企业微信协同群"

    assert (
        client.post(
            "/api/wechat/windows", json=personal, headers=worker_headers()
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/wechat/windows", json=wecom, headers=worker_headers()
        ).status_code
        == 200
    )

    personal_conversations = client.get(
        "/api/wechat/conversations?source=personal_wechat"
    ).json()
    wecom_conversations = client.get("/api/wechat/conversations?source=wecom").json()
    assert [item["session_id"] for item in personal_conversations] == ["shared-session"]
    assert [item["session_id"] for item in wecom_conversations] == [
        "wecom:shared-session"
    ]

    personal_candidates = client.get(
        "/api/wechat/candidates?candidate_status=processing&source=personal_wechat"
    ).json()
    wecom_candidates = client.get(
        "/api/wechat/candidates?candidate_status=processing&source=wecom"
    ).json()
    assert {item["source"] for item in personal_candidates} == {"personal_wechat"}
    assert {item["source"] for item in wecom_candidates} == {"wecom"}

    blocked = client.post("/api/wechat/conversations/wecom%3Ashared-session/block")
    assert blocked.status_code == 200
    assert blocked.json()["listen_status"] == "blocked"
    assert (
        client.get("/api/wechat/conversations?source=personal_wechat").json()[0][
            "listen_status"
        ]
        == "active"
    )


def test_channel_intake_keeps_channels_distinct(client: TestClient) -> None:
    login(client)
    materials = []
    for channel, source_type in (
        ("wechat", "wechat_channel"),
        ("wecom", "wecom_channel"),
        ("assistant", "assistant_channel"),
    ):
        response = client.post(
            "/api/channel-intake",
            json={
                "channel": channel,
                "external_message_id": "same-message-id",
                "text": "同一段测试材料",
            },
        )
        assert response.status_code == 201, response.text
        material = response.json()["material"]
        assert material["source_type"] == source_type
        materials.append(material["id"])
    assert len(set(materials)) == 3
    replay = client.post(
        "/api/channel-intake",
        json={
            "channel": "wechat",
            "external_message_id": "same-message-id",
            "text": "同一段测试材料",
        },
    )
    assert replay.status_code == 200
    assert replay.json()["material"]["id"] == materials[0]


def test_duplicate_channel_intake_is_not_offered_as_undoable(
    client: TestClient,
) -> None:
    login(client)
    payload = {
        "channel": "wechat",
        "external_message_id": "duplicate-message",
        "text": "需要记录的工作材料",
    }
    first = client.post("/api/channel-intake", json=payload)
    second = client.post("/api/channel-intake", json=payload)
    assert first.status_code == 201
    assert first.json()["undoable"] is True
    assert second.status_code == 200
    assert second.json()["undoable"] is False
    assert client.app.state.database.fetch_one(
        "SELECT COUNT(*) AS total FROM channel_intakes WHERE channel = ?",
        ("wechat",),
    ) == {"total": 1}


def test_ai_prefills_dynamic_contact_and_manual_edit_remains_available(
    client: TestClient,
) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    created = client.post(
        "/api/wechat/windows",
        json=window_payload(
            [message(901, now, "请赵楠继续跟进采购预算复核并反馈")],
            session_id="contact-prefill",
        ),
        headers=worker_headers(),
    ).json()
    _complete_next_wechat_job(
        client,
        {
            "classification": "relevant",
            "summary": "采购预算需要继续复核并反馈",
            "confidence": 0.96,
            "evidence": ["请赵楠继续跟进采购预算复核并反馈"],
            "extracted": {
                "matter_title": "采购预算复核",
                "actions": [
                    {
                        "kind": "task",
                        "title": "继续复核采购预算并反馈",
                        "detail": "由赵楠继续核对预算差异",
                        "assignee_suggestions": [
                            {
                                "person": "赵楠",
                                "detected_alias": "赵楠",
                                "reason": "原文明确要求赵楠继续跟进",
                                "evidence": ["请赵楠继续跟进采购预算复核并反馈"],
                                "confidence": 0.96,
                            }
                        ],
                    }
                ],
            },
        },
    )
    accepted = client.post(
        f"/api/wechat/candidates/{created['candidate_id']}/resolve",
        json={"action": "accept"},
    )
    assert accepted.status_code == 200, accepted.text
    matter_id = accepted.json()["matter_id"]
    matter = client.get(f"/api/matters/{matter_id}").json()
    assert matter["contact_name"] == "赵楠"
    assert any(
        item["display_name"] == "赵楠" for item in client.get("/api/people").json()
    )

    edited = client.patch(
        f"/api/matters/{matter_id}",
        json={"contact_name": "李静"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["contact_name"] == "李静"

    later = client.post(
        "/api/wechat/windows",
        json=window_payload(
            [message(902, now + 60, "采购预算复核继续推进")],
            session_id="contact-prefill-later",
        ),
        headers=worker_headers(),
    ).json()
    _complete_next_wechat_job(
        client,
        {
            "classification": "relevant",
            "summary": "采购预算复核继续推进",
            "confidence": 0.97,
            "evidence": ["采购预算复核继续推进"],
            "extracted": {
                "matter_title": "采购预算复核",
                "actions": [
                    {
                        "kind": "task",
                        "title": "继续推进采购预算复核",
                        "assignee_suggestions": [
                            {
                                "person": "王强",
                                "detected_alias": "王强",
                                "reason": "后续信息中的建议",
                                "evidence": ["采购预算复核继续推进"],
                                "confidence": 0.97,
                            }
                        ],
                    }
                ],
            },
        },
    )
    reconciled = client.post("/api/analysis/reconcile")
    assert reconciled.status_code == 200, reconciled.text
    merged = next(
        item
        for item in client.get(
            "/api/wechat/candidates?candidate_status=accepted&limit=20"
        ).json()
        if item["id"] == later["candidate_id"]
    )
    assert merged["matter_id"] == matter_id
    assert client.get(f"/api/matters/{matter_id}").json()["contact_name"] == "李静"


def test_wechat_user_actions_are_audited(client: TestClient) -> None:
    login(client)
    now = int(datetime.now(UTC).timestamp())
    created = client.post(
        "/api/wechat/windows",
        json=window_payload([message(11, now)]),
        headers=worker_headers(),
    ).json()
    released = client.post("/api/analysis/run")
    assert released.status_code == 200, released.text
    claimed = client.post(
        "/api/jobs/claim", json={"worker_id": "audit-test"}, headers=worker_headers()
    ).json()["job"]
    lease = {"worker_id": "audit-test", "lease_token": claimed["lease_token"]}
    client.post(
        f"/api/jobs/{claimed['id']}/start", json=lease, headers=worker_headers()
    )
    client.post(
        f"/api/wechat/jobs/{claimed['id']}/complete",
        json={
            **lease,
            "result": {
                "classification": "uncertain",
                "summary": "等待确认的测试线索",
                "confidence": 0.6,
                "evidence": ["测试原文"],
                "extracted": {},
            },
        },
        headers=worker_headers(),
    )
    candidate_id = created["candidate_id"]
    assert (
        client.post(
            f"/api/wechat/candidates/{candidate_id}/resolve", json={"action": "ignore"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/wechat/candidates/{candidate_id}/resolve", json={"action": "restore"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/wechat/candidates/{candidate_id}/resolve", json={"action": "accept"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/wechat/candidates/{candidate_id}/resolve", json={"action": "undo"}
        ).status_code
        == 200
    )
    assert client.post("/api/wechat/conversations/session-a/block").status_code == 200
    assert (
        client.post(
            "/api/wechat/conversations/session-a/unblock", json={"rescan_days": None}
        ).status_code
        == 200
    )
    actions = {event["action"] for event in client.get("/api/audit").json()}
    assert {
        "wechat.candidate.ignored",
        "wechat.candidate.restored",
        "wechat.candidate.classified",
        "wechat.candidate.accepted",
        "wechat.candidate.undone",
        "wechat.conversation.blocked",
        "wechat.conversation.unblocked",
    } <= actions
