from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import utc_now
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
        max_upload_bytes=4096,
        lease_seconds=30,
    )
    with TestClient(create_app(settings)) as test_client:
        login = test_client.post("/api/auth/login", json={"passcode": "owner-pass"})
        assert login.status_code == 200, login.text
        yield test_client


def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-token"}


def mcp_headers() -> dict[str, str]:
    return {"Authorization": "Bearer mcp-token"}


def test_mcp_can_prepare_work_package_but_cannot_change_business_state(
    client: TestClient,
) -> None:
    matter = client.app.state.service.create_matter("合成权限边界事项", "仅用于权限测试")

    generated = client.post(
        f"/api/matters/{matter['id']}/work-package/generate",
        headers=mcp_headers(),
    )
    assert generated.status_code == 200, generated.text

    forbidden_requests = [
        client.post(
            f"/api/matters/{matter['id']}/work-package/apply",
            json={"step_indexes": [0]},
            headers=mcp_headers(),
        ),
        client.post(
            f"/api/matters/{matter['id']}/close",
            json={"completion_note": "不应由 MCP 关闭"},
            headers=mcp_headers(),
        ),
        client.post(
            "/api/actions/synthetic-action/resolve",
            json={"status": "done"},
            headers=mcp_headers(),
        ),
        client.post(
            "/api/reviews/synthetic-review/resolve",
            json={"resolution": "accepted", "note": ""},
            headers=mcp_headers(),
        ),
    ]

    assert [response.status_code for response in forbidden_requests] == [403, 403, 403, 403]


def start_analysis(client: TestClient) -> dict:
    received = client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": "请于 2099-01-02 完成合成复核。"},
        headers={"Idempotency-Key": "suggestion-only-material"},
    )
    assert received.status_code == 201, received.text
    released = client.post("/api/analysis/run")
    assert released.status_code == 200, released.text
    claimed = client.post(
        "/api/jobs/claim",
        json={"worker_id": "synthetic-worker"},
        headers=worker_headers(),
    )
    assert claimed.status_code == 200, claimed.text
    job = claimed.json()["job"]
    started = client.post(
        f"/api/jobs/{job['id']}/start",
        json={"worker_id": "synthetic-worker", "lease_token": job["lease_token"]},
        headers=worker_headers(),
    )
    assert started.status_code == 200, started.text
    return job


def test_analysis_completion_only_prepares_suggestions(client: TestClient) -> None:
    job = start_analysis(client)
    database = client.app.state.database
    matter = client.app.state.service.create_matter(
        "人工维护中的事项", "Frank", "人工摘要不得被整理结果覆盖"
    )
    now = utc_now()
    with database.connect() as connection:
        connection.execute(
            "UPDATE materials SET matter_id = ?, updated_at = ? WHERE id = ?",
            (matter["id"], now, job["material_id"]),
        )
        connection.execute(
            "INSERT INTO actions "
            "(id, matter_id, material_id, kind, title, detail, status, owner, created_by, "
            "created_at, updated_at, flow_state, waiting_on, blocked_reason, schedule_basis, "
            "completion_evidence) VALUES "
            "('existing-action', ?, ?, 'task', '人工行动', '', 'open', 'Frank', 'Frank', "
            "?, ?, 'needs_action', '', '', 'user_entered', '[]')",
            (matter["id"], job["material_id"], now, now),
        )
        connection.execute(
            "INSERT INTO reminders "
            "(id, matter_id, action_id, kind, title, reason, status, fingerprint, due_at, "
            "created_at, updated_at) VALUES "
            "('existing-reminder', ?, 'existing-action', 'follow_up', '人工提醒', '', "
            "'scheduled', 'synthetic-existing-reminder', '2099-01-03', ?, ?)",
            (matter["id"], now, now),
        )
        connection.execute(
            "INSERT INTO evidence "
            "(id, matter_id, material_id, claim_type, field_type, value, source_locator, quote, "
            "confidence, status, created_by, created_at) VALUES "
            "('existing-evidence', ?, ?, 'fact', '人工事实', '已人工确认', '人工录入', '', "
            "1.0, 'confirmed', 'Frank', ?)",
            (matter["id"], job["material_id"], now),
        )
        connection.execute(
            "INSERT INTO review_items "
            "(id, matter_id, material_id, evidence_id, kind, title, payload_json, confidence, "
            "status, created_at) VALUES "
            "('existing-review', ?, ?, NULL, 'decision', '人工待确认', '{}', NULL, 'pending', ?)",
            (matter["id"], job["material_id"], now),
        )

    payload = {
        "matter_id": matter["id"],
        "matter_title": "贾维斯建议的新标题",
        "summary": "贾维斯建议的新摘要",
        "facts": [
            {
                "field_type": "日期",
                "value": "2099-01-02",
                "source_locator": "原文第1行",
                "quote": "请于 2099-01-02 完成合成复核。",
                "confidence": 1.0,
            }
        ],
        "inferences": [
            {
                "field_type": "责任判断",
                "value": "可能由财务负责人复核",
                "source_locator": "贾维斯推断",
                "quote": "",
                "confidence": 0.7,
            }
        ],
        "actions": [
            {
                "kind": "task",
                "title": "完成合成复核",
                "detail": "核对合成数据。",
                "owner": "财务负责人",
                "due_date": "2099-01-02",
                "schedule_basis": "material_explicit",
            }
        ],
        "completion_suggestions": [
            {
                "action_id": "existing-action",
                "reason": "材料看起来已经处理",
                "evidence": ["合成证据"],
            }
        ],
        "brief": {
            "next_check_at": "2099-01-03",
            "next_check_reason": "复查合成结果",
            "schedule_basis": "material_explicit",
        },
    }
    lease = {"worker_id": "synthetic-worker", "lease_token": job["lease_token"]}
    completed = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={**lease, "result": payload},
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text

    current_matter = database.fetch_one("SELECT * FROM matters WHERE id = ?", (matter["id"],))
    assert current_matter["title"] == "人工维护中的事项"
    assert current_matter["summary"] == "人工摘要不得被整理结果覆盖"
    assert database.fetch_one("SELECT status FROM actions WHERE id = 'existing-action'")["status"] == "open"
    assert database.fetch_one("SELECT status FROM reminders WHERE id = 'existing-reminder'")["status"] == "scheduled"
    assert database.fetch_one("SELECT status FROM review_items WHERE id = 'existing-review'")["status"] == "pending"
    assert database.fetch_one("SELECT status FROM evidence WHERE id = 'existing-evidence'")["status"] == "confirmed"
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM actions WHERE matter_id = ?", (matter["id"],)
    )["count"] == 1

    generated_evidence = database.fetch_all(
        "SELECT claim_type, status FROM evidence WHERE matter_id = ? AND id != 'existing-evidence' "
        "ORDER BY claim_type",
        (matter["id"],),
    )
    assert generated_evidence == [
        {"claim_type": "fact", "status": "pending"},
        {"claim_type": "inference", "status": "pending"},
    ]
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM review_items WHERE matter_id = ? AND status = 'pending'",
        (matter["id"],),
    )["count"] == 3
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM reminders WHERE matter_id = ?", (matter["id"],)
    )["count"] == 1

    package = client.get(f"/api/matters/{matter['id']}/work-package")
    assert package.status_code == 200, package.text
    work_package = package.json().get("work_package", package.json())
    assert work_package["status"] == "draft"
    assert work_package["draft"]["steps"][0]["title"] == "完成合成复核"
    assert work_package["draft"]["steps"][0]["due_date"] == "2099-01-02"
    assert "existing-action" in "\n".join(work_package["draft"]["questions"])

    repeated = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={**lease, "result": payload},
        headers=worker_headers(),
    )
    assert repeated.status_code == 200, repeated.text
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM actions WHERE matter_id = ?", (matter["id"],)
    )["count"] == 1
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM work_packages WHERE matter_id = ?", (matter["id"],)
    )["count"] == 1
    saved = database.fetch_one(
        "SELECT draft_json FROM work_packages WHERE matter_id = ?", (matter["id"],)
    )
    assert len(json.loads(saved["draft_json"])["steps"]) == 1
