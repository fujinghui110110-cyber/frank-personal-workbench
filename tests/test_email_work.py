from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ACCOUNT = {
    "account_id": "a" * 64,
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


def owner_login(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"passcode": "owner-pass"})
    assert response.status_code == 200, response.text


def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-token"}


def message(uid: int, *, work: bool, thread: str = "thread-a", matter_id: str | None = None) -> dict:
    return {
        "account_id": ACCOUNT["account_id"],
        "folder": "INBOX",
        "uid_validity": "123",
        "uid": uid,
        "message_id_hash": f"hash-{uid}",
        "thread_key": (thread * 20)[:40],
        "sender_key": "sender-key-001",
        "sender_name": "业务联系人",
        "sender_hint": "b***@example.com",
        "subject": "预算复核要求",
        "sent_at": "2026-08-20T02:00:00Z",
        "classification": "work" if work else "irrelevant",
        "needs_follow_up": work,
        "summary": "需要在本周内复核预算差异并反馈。" if work else "订阅通知",
        "reason": "存在明确期限和反馈要求" if work else "没有工作跟进价值",
        "evidence": ["请于本周内完成复核并回复"],
        "matter_id": matter_id,
        "matter_title": "预算差异复核",
        "actions": [
            {
                "kind": "task",
                "title": "复核预算差异并回复",
                "detail": "核对预算表并反馈差异原因",
                "owner": "财务负责人",
                "due_date": "2026-08-22",
            }
        ] if work else [],
    }


def pending_message(uid: int, *, attachment_paths: list[str] | None = None) -> dict:
    payload = message(uid, work=False)
    payload.update(
        {
            "classification": "pending",
            "summary": "",
            "reason": "",
            "evidence": [],
            "actions": [],
            "source_text": "主题：预算复核要求\n正文：请于本周内复核预算差异并回复。",
            "attachment_paths": attachment_paths or [],
        }
    )
    return payload


def claim_email_analysis(client: TestClient, worker_id: str = "mac-air") -> dict:
    claimed = client.post(
        "/api/jobs/claim",
        json={"worker_id": worker_id},
        headers=worker_headers(),
    )
    assert claimed.status_code == 200, claimed.text
    return claimed.json()["job"]


def test_email_sync_filters_nonwork_and_merges_open_thread(client: TestClient) -> None:
    owner_login(client)
    assert client.get("/api/email/status").json()["configured"] is False

    requested = client.post("/api/email/sync/run")
    assert requested.status_code == 200
    assert requested.json()["status"] == "not_configured"
    assert requested.json()["request"] is None
    registered = client.post(
        "/api/email/accounts/register", json=ACCOUNT, headers=worker_headers()
    )
    assert registered.status_code == 200, registered.text
    requested = client.post("/api/email/sync/run")
    assert requested.status_code == 200
    claimed = client.post(
        "/api/email/sync/claim", json={"worker_id": "mac-air"}, headers=worker_headers()
    ).json()["request"]
    assert claimed["status"] == "running"

    ignored = client.post(
        "/api/email/messages", json=message(1, work=False), headers=worker_headers()
    )
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["status"] == "ignored"
    assert ignored.json()["stored"] is False
    ignored_count = client.app.state.database.fetch_one(
        "SELECT COUNT(*) AS count FROM email_messages WHERE status = 'ignored'"
    )
    assert ignored_count == {"count": 0}
    assert client.get("/api/email/matters").json() == []

    first = client.post(
        "/api/email/messages", json=message(2, work=True), headers=worker_headers()
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["status"] == "active"
    assert first_body["matter_id"]

    duplicate = client.post(
        "/api/email/messages", json=message(2, work=True), headers=worker_headers()
    )
    assert duplicate.json()["id"] == first_body["id"]

    follow_up = client.post(
        "/api/email/messages", json=message(3, work=True), headers=worker_headers()
    )
    assert follow_up.json()["matter_id"] == first_body["matter_id"]

    email_matters = client.get("/api/email/matters").json()
    assert len(email_matters) == 1
    assert email_matters[0]["open_action_count"] == 2

    finished = client.post(
        f"/api/email/sync/{claimed['id']}/finish",
        json={
            "worker_id": "mac-air",
            "status": "completed",
            "account_id": ACCOUNT["account_id"],
            "uid_validity": "123",
            "last_uid": 3,
            "scanned_count": 7,
            "pending_count": 2,
            "ignored_count": 5,
        },
        headers=worker_headers(),
    )
    assert finished.status_code == 200, finished.text
    status = client.get("/api/email/status").json()
    assert status["configured"] is True
    assert status["counts"] == {"active": 2, "ignored": 0}
    assert status["latest"]["scanned_count"] == 7
    assert status["latest"]["pending_count"] == 2
    assert status["latest"]["ignored_count"] == 5


def test_pending_email_waits_for_manual_analysis_and_creates_work(client: TestClient) -> None:
    owner_login(client)
    client.post("/api/email/accounts/register", json=ACCOUNT, headers=worker_headers())
    pending = client.post(
        "/api/email/messages", json=pending_message(20), headers=worker_headers()
    )
    assert pending.status_code == 200, pending.text
    assert pending.json()["status"] == "pending"
    assert client.get("/api/analysis/status").json()["pending"] == 1
    assert claim_email_analysis(client) is None

    released = client.post("/api/analysis/run")
    assert released.status_code == 200, released.text
    assert released.json()["released"] == 1
    job = claim_email_analysis(client)
    assert job["job_type"] == "email_classify"
    lease = {"worker_id": "mac-air", "lease_token": job["lease_token"]}
    started = client.post(
        f"/api/jobs/{job['id']}/start", json=lease, headers=worker_headers()
    )
    assert started.status_code == 200, started.text
    completed = client.post(
        f"/api/email/jobs/{job['id']}/complete",
        json={
            **lease,
            "message_id": pending.json()["id"],
            "result": {
                "classification": "work",
                "needs_follow_up": True,
                "summary": "需要在本周内复核预算差异并反馈。",
                "reason": "存在明确期限和反馈要求",
                "matter_title": "预算差异复核",
                "evidence": ["请于本周内复核预算差异并回复"],
                "actions": [{"kind": "task", "title": "复核预算差异并回复"}],
            },
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "active"
    assert completed.json()["matter_id"]
    assert client.get("/api/analysis/status").json()["pending"] == 0


def test_email_ai_prefills_dynamic_contact(client: TestClient) -> None:
    owner_login(client)
    client.post("/api/email/accounts/register", json=ACCOUNT, headers=worker_headers())
    pending = client.post(
        "/api/email/messages",
        json=pending_message(88),
        headers=worker_headers(),
    ).json()
    client.post("/api/analysis/run")
    job = claim_email_analysis(client)
    lease = {"worker_id": "mac-air", "lease_token": job["lease_token"]}
    client.post(
        f"/api/jobs/{job['id']}/start",
        json=lease,
        headers=worker_headers(),
    )
    completed = client.post(
        f"/api/email/jobs/{job['id']}/complete",
        json={
            **lease,
            "message_id": pending["id"],
            "result": {
                "classification": "work",
                "needs_follow_up": True,
                "summary": "预算差异需要复核并反馈",
                "reason": "邮件明确安排了跟进人",
                "evidence": ["请赵楠复核预算差异并反馈"],
                "matter_title": "预算差异复核",
                "actions": [
                    {
                        "kind": "task",
                        "title": "复核预算差异并反馈",
                        "detail": "由赵楠负责复核",
                        "assignee_suggestions": [
                            {
                                "person": "赵楠",
                                "detected_alias": "赵楠",
                                "reason": "邮件明确安排赵楠负责",
                                "evidence": ["请赵楠复核预算差异并反馈"],
                                "confidence": 0.95,
                            }
                        ],
                    }
                ],
            },
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text
    matter = client.get(f"/api/matters/{completed.json()['matter_id']}").json()
    assert matter["contact_name"] == "赵楠"


def test_irrelevant_email_analysis_erases_content_and_attachment(
    client: TestClient, tmp_path: Path
) -> None:
    owner_login(client)
    client.post("/api/email/accounts/register", json=ACCOUNT, headers=worker_headers())
    attachment = tmp_path / "temporary-policy.pdf"
    attachment.write_bytes(b"temporary")
    pending = client.post(
        "/api/email/messages",
        json=pending_message(21, attachment_paths=[str(attachment)]),
        headers=worker_headers(),
    ).json()
    client.post("/api/analysis/run")
    job = claim_email_analysis(client)
    lease = {"worker_id": "mac-air", "lease_token": job["lease_token"]}
    client.post(f"/api/jobs/{job['id']}/start", json=lease, headers=worker_headers())
    completed = client.post(
        f"/api/email/jobs/{job['id']}/complete",
        json={
            **lease,
            "message_id": pending["id"],
            "result": {
                "classification": "irrelevant",
                "needs_follow_up": False,
                "summary": "非工作邮件",
                "reason": "没有需要持续跟进的工作内容",
                "evidence": [],
                "actions": [],
            },
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "ignored"
    assert not attachment.exists()
    material = client.app.state.database.fetch_one(
        "SELECT text_note, size, metadata_json FROM materials WHERE id = ?",
        (pending["material_id"],),
    )
    assert material == {"text_note": "", "size": 0, "metadata_json": "{}"}


def test_forwarding_metadata_is_removed_from_email_work(client: TestClient) -> None:
    owner_login(client)
    client.post("/api/email/accounts/register", json=ACCOUNT, headers=worker_headers())
    payload = message(9, work=True)
    payload.update(
        {
            "subject": "转发：【重要】成本费用投入案例收集的说明",
            "summary": "集团（cohl.com）徐彩蝶发起，傅京晖转发，要求8月25日前提交案例。",
            "matter_title": "转发：【重要】成本费用投入案例收集的说明",
            "evidence": ["傅京晖转发，要求8月25日前提交案例。"],
            "actions": [
                {
                    "kind": "task",
                    "title": "傅京晖转发，提交成本投入案例",
                    "detail": "徐彩蝶发起，傅京晖转发，截止8月25日。",
                    "owner": "财务负责人",
                    "due_date": "2026-08-25",
                }
            ],
        }
    )
    response = client.post("/api/email/messages", json=payload, headers=worker_headers())
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["subject"] == "【重要】成本费用投入案例收集的说明"
    assert saved["summary"] == "要求8月25日前提交案例。"

    matter = client.get(f"/api/matters/{saved['matter_id']}").json()
    assert matter["title"] == "【重要】成本费用投入案例收集的说明"
    assert matter["summary"] == "要求8月25日前提交案例。"
    assert matter["actions"][0]["title"] == "提交成本投入案例"
    assert matter["actions"][0]["detail"] == "截止8月25日。"

    static_js = (Path(__file__).parents[1] / "app/static/app.js").read_text(
        encoding="utf-8"
    )
    assert 'item.sender_name || item.sender_hint' not in static_js
    assert "emailSubject(item.subject || item.matter_title)" in static_js


def test_completed_matter_is_not_reused_and_false_positive_closes_action(
    client: TestClient,
) -> None:
    owner_login(client)
    client.post("/api/email/accounts/register", json=ACCOUNT, headers=worker_headers())
    first = client.post(
        "/api/email/messages", json=message(10, work=True), headers=worker_headers()
    ).json()
    matter = client.get(f"/api/matters/{first['matter_id']}").json()
    for action in matter["actions"]:
        response = client.post(f"/api/actions/{action['id']}/resolve", json={"status": "done"})
        assert response.status_code == 200, response.text
    assert client.get("/api/email/matters").json()[0]["is_completed"] is True
    assert client.get(f"/api/matters/{first['matter_id']}").json()["status"] == "completed"
    assert client.get("/api/email/messages").json() == []
    assert client.get("/api/email/status").json()["counts"]["active"] == 0

    later = message(11, work=True, thread="different", matter_id=first["matter_id"])
    later_saved = client.post(
        "/api/email/messages", json=later, headers=worker_headers()
    ).json()
    assert later_saved["matter_id"] != first["matter_id"]

    ignored = client.post(f"/api/email/messages/{later_saved['id']}/ignore")
    assert ignored.status_code == 200, ignored.text
    assert ignored.json()["status"] == "ignored"
    later_matter = client.get(f"/api/matters/{later_saved['matter_id']}").json()
    assert all(action["status"] == "dismissed" for action in later_matter["actions"])


def test_stale_email_sync_is_reclaimed(client: TestClient) -> None:
    owner_login(client)
    registered = client.post(
        "/api/email/accounts/register", json=ACCOUNT, headers=worker_headers()
    )
    assert registered.status_code == 200, registered.text
    requested = client.post("/api/email/sync/run").json()
    first = client.post(
        "/api/email/sync/claim", json={"worker_id": "mac-first"}, headers=worker_headers()
    ).json()["request"]
    assert first["id"] == requested["id"]
    with client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE email_sync_requests SET claimed_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00Z", requested["id"]),
        )
    second = client.post(
        "/api/email/sync/claim", json={"worker_id": "mac-second"}, headers=worker_headers()
    ).json()["request"]
    assert second["id"] == requested["id"]
    assert second["worker_id"] == "mac-second"


def test_bad_email_job_lease_has_no_business_side_effects(client: TestClient) -> None:
    owner_login(client)
    client.post("/api/email/accounts/register", json=ACCOUNT, headers=worker_headers())
    email = client.post(
        "/api/email/messages", json=pending_message(90), headers=worker_headers()
    ).json()
    client.post("/api/analysis/run")
    job = claim_email_analysis(client)
    lease = {"worker_id": "mac-air", "lease_token": job["lease_token"]}
    client.post(f"/api/jobs/{job['id']}/start", json=lease, headers=worker_headers())
    response = client.post(
        f"/api/email/jobs/{job['id']}/complete",
        json={
            **lease,
            "lease_token": "wrong-token-value-1234",
            "message_id": email["id"],
            "result": {
                "classification": "work",
                "needs_follow_up": True,
                "summary": "不应写入",
                "matter_title": "不应创建事项",
                "actions": [{"kind": "task", "title": "不应创建行动"}],
            },
        },
        headers=worker_headers(),
    )
    assert response.status_code == 422
    assert client.app.state.database.fetch_one(
        "SELECT status, matter_id FROM email_messages WHERE id = ?", (email["id"],)
    ) == {"status": "pending", "matter_id": None}
    assert client.app.state.database.fetch_one(
        "SELECT COUNT(*) AS total FROM matters WHERE title = ?", ("不应创建事项",)
    ) == {"total": 0}


def test_ignored_email_can_be_restored(client: TestClient) -> None:
    owner_login(client)
    client.post("/api/email/accounts/register", json=ACCOUNT, headers=worker_headers())
    saved = client.post(
        "/api/email/messages", json=message(91, work=True), headers=worker_headers()
    ).json()
    client.post(f"/api/email/messages/{saved['id']}/ignore")
    restored = client.post(f"/api/email/messages/{saved['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "active"
    matter = client.get(f"/api/matters/{saved['matter_id']}").json()
    assert any(action["status"] == "open" for action in matter["actions"])


def test_frontend_exposes_failure_review_and_email_undo() -> None:
    root = Path(__file__).parents[1]
    script = (root / "app/static/app.js").read_text(encoding="utf-8")
    markup = (root / "app/static/index.html").read_text(encoding="utf-8")
    assert "查看需检查内容" in script
    assert "/restore" in script
    assert "data-email-restore-inline" in script
    assert "sourceReceiptDismissed" in script
    assert 'id="session-menu"' not in markup


def test_frontend_only_offers_unfinished_matters_for_merge() -> None:
    script = (Path(__file__).parents[1] / "app/static/app.js").read_text(encoding="utf-8")
    assert "function mergeableMatters" in script
    assert "!matter.is_completed" in script
    assert "mergeableMatters(matters)" in script
    assert "mergeableMatters(state.matters)" in script
