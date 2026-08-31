from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "workbench",
        owner_passcode="owner-pass",
        session_secret="test-session-secret",
        owner_token="owner-token",
        worker_token="worker-token",
        mcp_token="mcp-token",
        max_upload_bytes=4096,
        lease_seconds=1,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def owner_login(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"passcode": "owner-pass"})
    assert response.status_code == 200, response.text


def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-token"}


def receive(
    client: TestClient,
    *,
    key: str,
    note: str = "待处理材料",
    source_type: str = "text",
    files=None,
):
    return client.post(
        "/api/intake",
        data={"source_type": source_type, "text_note": note},
        files=files,
        headers={"Idempotency-Key": key},
    )


def claim_and_start(client: TestClient, worker_id: str = "mac-air") -> tuple[dict, str]:
    released = client.post("/api/analysis/run")
    assert released.status_code == 200, released.text
    claimed_response = client.post(
        "/api/jobs/claim",
        json={"worker_id": worker_id},
        headers=worker_headers(),
    )
    assert claimed_response.status_code == 200, claimed_response.text
    job = claimed_response.json()["job"]
    assert job["status"] == "claimed"
    lease_token = job["lease_token"]

    started_response = client.post(
        f"/api/jobs/{job['id']}/start",
        json={"worker_id": worker_id, "lease_token": lease_token},
        headers=worker_headers(),
    )
    assert started_response.status_code == 200, started_response.text
    assert started_response.json()["status"] == "processing"
    return job, lease_token


def complete_result(*, fact: bool = False, inference: bool = False) -> dict:
    result = {
        "matter_title": "预算审查",
        "summary": "材料已归并，等待财务负责人确认例外。",
        "actions": [
            {
                "kind": "task",
                "title": "复核预算",
                "detail": "核对原始凭证。",
                "owner": "财务负责人",
                "due_date": "2099-01-01",
                "schedule_basis": "legacy",
            }
        ],
    }
    if fact:
        result["facts"] = [
            {
                "field_type": "金额",
                "value": "10万元",
                "source_locator": "原文第1行",
                "quote": "金额：10万元",
                "confidence": 1.0,
            }
        ]
    if inference:
        result["inferences"] = [
            {
                "field_type": "事项类型",
                "value": "预算",
                "source_locator": "基于材料类型",
                "quote": "文字记录",
                "confidence": 0.8,
            }
        ]
    return result


def test_claimed_job_cannot_skip_start(client: TestClient) -> None:
    owner_login(client)
    receive(client, key="claimed-must-start")
    client.post("/api/analysis/run")
    job = client.post(
        "/api/jobs/claim", json={"worker_id": "mac-air"}, headers=worker_headers()
    ).json()["job"]
    lease = {"worker_id": "mac-air", "lease_token": job["lease_token"]}
    failed = client.post(
        f"/api/jobs/{job['id']}/fail",
        json={**lease, "error": "不应跳过开始"},
        headers=worker_headers(),
    )
    completed = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={**lease, "result": complete_result()},
        headers=worker_headers(),
    )
    assert failed.status_code == 403
    assert completed.status_code == 403
    assert client.app.state.database.fetch_one(
        "SELECT status FROM jobs WHERE id = ?", (job["id"],)
    ) == {"status": "claimed"}


def test_expired_processing_job_resets_material_status(client: TestClient) -> None:
    owner_login(client)
    material = receive(client, key="expired-processing").json()["material"]
    job, _ = claim_and_start(client, worker_id="mac-first")
    with client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00Z", job["id"]),
        )
    reclaimed = client.post(
        "/api/jobs/claim", json={"worker_id": "mac-second"}, headers=worker_headers()
    )
    assert reclaimed.status_code == 200, reclaimed.text
    assert reclaimed.json()["job"]["id"] == job["id"]
    assert client.app.state.database.fetch_one(
        "SELECT status FROM materials WHERE id = ?", (material["id"],)
    ) == {"status": "queued"}


def test_completing_action_closes_scheduled_reminder(client: TestClient) -> None:
    owner_login(client)
    receive(client, key="scheduled-reminder")
    job, token = claim_and_start(client)
    result = complete_result()
    result["actions"] = [{"kind": "task", "title": "核对预算"}]
    matter = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={"worker_id": "mac-air", "lease_token": token, "result": result},
        headers=worker_headers(),
    ).json()
    action = matter["actions"][0]
    with client.app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO reminders "
            "(id, matter_id, action_id, kind, title, reason, status, fingerprint, due_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 'follow_up', ?, '', 'scheduled', ?, ?, ?, ?)",
            (
                "scheduled-reminder-test", matter["id"], action["id"], "等待核对",
                "scheduled-reminder-fingerprint", "2099-01-02T09:00:00Z",
                "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z",
            ),
        )
    response = client.post(f"/api/actions/{action['id']}/resolve", json={"status": "done"})
    assert response.status_code == 200, response.text
    assert client.app.state.database.fetch_one(
        "SELECT status FROM reminders WHERE id = ?", ("scheduled-reminder-test",)
    ) == {"status": "done"}


def test_login_session_and_scope_boundaries(client: TestClient) -> None:
    assert client.get("/api/materials").status_code == 401
    assert client.post("/api/auth/login", json={"passcode": "wrong"}).status_code == 401

    owner_login(client)
    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json() == {
        "actor": "财务负责人",
        "role": "owner",
        "scopes": ["*"],
        "password_required": True,
    }

    worker_attempt = client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": "x"},
        headers={**worker_headers(), "Idempotency-Key": "scope-check"},
    )
    assert worker_attempt.status_code == 201

    audit_attempt = client.get("/api/audit", headers=worker_headers())
    assert audit_attempt.status_code == 403


def test_password_can_be_disabled_for_local_workbench(settings: Settings) -> None:
    local_settings = Settings(**{**settings.__dict__, "password_disabled": True})
    with TestClient(create_app(local_settings)) as local_client:
        session = local_client.get("/api/auth/session")
        materials = local_client.get("/api/materials")

    assert session.status_code == 200
    assert session.json()["password_required"] is False
    assert materials.status_code == 200


def test_intake_idempotency_and_exact_hash_dedupe(client: TestClient) -> None:
    owner_login(client)
    first = receive(client, key="request-1", note="同一份材料")
    assert first.status_code == 201, first.text
    first_body = first.json()
    first_material = first_body["material"]
    assert first_body["created"] is True

    same_key = receive(client, key="request-1", note="请求重试时的不同说明")
    assert same_key.status_code == 200, same_key.text
    assert same_key.json()["created"] is False
    assert same_key.json()["material"]["id"] == first_material["id"]
    assert same_key.json()["material"]["text_note"] == "同一份材料"

    same_bytes = receive(client, key="request-2", note="同一份材料")
    assert same_bytes.status_code == 200, same_bytes.text
    assert same_bytes.json()["created"] is False
    assert same_bytes.json()["material"]["id"] == first_material["id"]
    assert same_bytes.json()["material"]["sha256"] == first_material["sha256"]

    different_source = receive(
        client, key="request-3", note="同一份材料", source_type="image"
    )
    assert different_source.status_code == 201, different_source.text
    assert different_source.json()["material"]["id"] != first_material["id"]


def test_mac_off_receipt_stays_queued(client: TestClient) -> None:
    owner_login(client)
    response = receive(client, key="offline-receipt", note="Mac 关机期间收到的材料")
    assert response.status_code == 201, response.text
    material = response.json()["material"]
    assert material["status"] == "awaiting_analysis"

    waiting = client.get(
        "/api/materials", params={"material_status": "awaiting_analysis"}
    )
    assert waiting.status_code == 200
    assert any(item["id"] == material["id"] for item in waiting.json())


def test_worker_claim_start_complete_and_provenance(client: TestClient) -> None:
    owner_login(client)
    received = receive(client, key="worker-flow", note="金额：10万元\n负责人：张三")
    assert received.status_code == 201, received.text
    material_id = received.json()["material"]["id"]

    job, lease_token = claim_and_start(client)
    response = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={
            "worker_id": "mac-air",
            "lease_token": lease_token,
            "result": complete_result(fact=True, inference=True),
        },
        headers=worker_headers(),
    )
    assert response.status_code == 200, response.text
    matter = response.json()
    assert matter["title"] == "预算审查"
    assert matter["materials"][0]["id"] == material_id
    assert matter["materials"][0]["status"] == "processed"

    evidence = {item["claim_type"]: item for item in matter["evidence"]}
    assert evidence["fact"]["material_id"] == material_id
    assert evidence["fact"]["source_locator"] == "原文第1行"
    assert evidence["fact"]["quote"] == "金额：10万元"
    assert evidence["inference"]["source_locator"] == "基于材料类型"

    reviews = matter["reviews"]
    assert len(reviews) == 1
    assert reviews[0]["status"] == "pending"
    assert reviews[0]["evidence_id"] == evidence["inference"]["id"]

    detail = client.get(f"/api/matters/{matter['id']}")
    assert detail.status_code == 200
    assert detail.json()["materials"][0]["sha256"] == received.json()["material"]["sha256"]


def test_facts_require_source_locator(client: TestClient) -> None:
    owner_login(client)
    response = receive(client, key="missing-locator", note="金额：10万元")
    assert response.status_code == 201, response.text
    job, lease_token = claim_and_start(client)

    completion = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={
            "worker_id": "mac-air",
            "lease_token": lease_token,
            "result": {
                "matter_title": "缺少定位",
                "facts": [{"field_type": "金额", "value": "10万元"}],
            },
        },
        headers=worker_headers(),
    )
    assert completion.status_code == 422
    assert "原文定位" in completion.json()["detail"]


def test_reminder_scan_deduplicates_review_reminder(client: TestClient) -> None:
    owner_login(client)
    response = receive(client, key="review-reminder", note="待确认推断")
    assert response.status_code == 201, response.text
    job, lease_token = claim_and_start(client)
    completion = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={
            "worker_id": "mac-air",
            "lease_token": lease_token,
            "result": complete_result(inference=True),
        },
        headers=worker_headers(),
    )
    assert completion.status_code == 200, completion.text

    first_scan = client.post("/api/proactive/scan")
    second_scan = client.post("/api/proactive/scan")
    assert first_scan.status_code == 200
    assert second_scan.status_code == 200
    assert first_scan.json() == {"created": 1}
    assert second_scan.json() == {"created": 0}

    overview = client.get("/api/overview")
    assert overview.status_code == 200
    assert overview.json()["counts"]["open_reminders"] == 1


def test_worker_failure_retry_and_recovery(client, app) -> None:
    owner_login(client)
    response = receive(client, key="retry-flow", note="需要本地处理")
    assert response.status_code == 201, response.text
    material_id = response.json()["material"]["id"]
    job, lease_token = claim_and_start(client, worker_id="mac-first")

    failed = client.post(
        f"/api/jobs/{job['id']}/fail",
        json={
            "worker_id": "mac-first",
            "lease_token": lease_token,
            "error": "本地节点暂时不可用",
        },
        headers=worker_headers(),
    )
    assert failed.status_code == 200, failed.text
    assert failed.json()["status"] == "retryable_failed"

    with app.state.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET next_attempt_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", job["id"]),
        )

    retry_job, retry_token = claim_and_start(client, worker_id="mac-retry")
    assert retry_job["id"] == job["id"]
    assert retry_job["attempt_count"] == 2
    completed = client.post(
        f"/api/jobs/{retry_job['id']}/complete",
        json={
            "worker_id": "mac-retry",
            "lease_token": retry_token,
            "result": {"matter_title": "重试恢复"},
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text
    material = client.get(f"/api/materials/{material_id}")
    assert material.status_code == 200
    assert material.json()["status"] == "processed"
    row = app.state.database.fetch_one("SELECT status FROM jobs WHERE id = ?", (job["id"],))
    assert row == {"status": "succeeded"}


def test_analysis_issue_list_matches_terminal_failures_and_can_retry(client, app) -> None:
    owner_login(client)
    received = receive(client, key="analysis-issue", note="需要重新整理的材料")
    material_id = received.json()["material"]["id"]
    job = app.state.database.fetch_one(
        "SELECT id FROM jobs WHERE material_id = ?", (material_id,)
    )
    with app.state.database.connect() as connection:
        connection.execute(
            "UPDATE jobs SET status = 'needs_review', attempt_count = max_attempts, "
            "error = '材料尚未提取出可供 贾维斯 理解的文字' WHERE id = ?",
            (job["id"],),
        )
        connection.execute(
            "UPDATE materials SET status = 'needs_review' WHERE id = ?", (material_id,)
        )

    issues = client.get("/api/analysis/issues")
    assert issues.status_code == 200, issues.text
    assert len(issues.json()) == 1
    assert issues.json()[0]["material_id"] == material_id
    assert "重新整理" in issues.json()[0]["reason"]

    retried = client.post(f"/api/analysis/issues/{job['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["queued"] is True
    assert client.get("/api/analysis/issues").json() == []
    reset = app.state.database.fetch_one("SELECT * FROM jobs WHERE id = ?", (job["id"],))
    assert reset["status"] == "queued"
    assert reset["attempt_count"] == 0

def test_worker_saves_audio_transcript_before_analysis(client: TestClient) -> None:
    owner_login(client)
    received = receive(
        client,
        key="audio-transcript",
        note="",
        source_type="audio",
        files={"upload": ("经营分析会.wav", b"RIFF-test-audio", "audio/wav")},
    )
    assert received.status_code == 201, received.text
    material_id = received.json()["material"]["id"]
    job, lease_token = claim_and_start(client)

    saved = client.post(
        f"/api/materials/{material_id}/transcript",
        json={
            "worker_id": "mac-air",
            "lease_token": lease_token,
            "text": "# 经营分析会\n\n[00:00:01 - 00:00:04] 财务部准备经营分析报告。",
            "language": "zh",
            "model": "mlx-community/whisper-large-v3-turbo",
            "duration_seconds": 4,
            "segment_count": 1,
        },
        headers=worker_headers(),
    )

    assert saved.status_code == 200, saved.text
    assert saved.json()["status"] == "transcribed"
    assert saved.json()["job"]["status"] == "processing"
    transcript = client.get(f"/api/materials/{material_id}/transcript")
    assert transcript.status_code == 200
    assert "财务部准备经营分析报告" in transcript.json()["text"]
    assert transcript.json()["metadata"]["segment_count"] == 1
    assert job["material_id"] == material_id


def test_workbuddy_trace_and_follow_up_are_visible(client: TestClient) -> None:
    owner_login(client)
    received = receive(client, key="workbuddy-visible", note="等待预算口径确认")
    material_id = received.json()["material"]["id"]
    assigned = client.post(
        f"/api/materials/{material_id}/assign", json={"title": "原始材料标题"}
    )
    assert assigned.status_code == 200, assigned.text
    matter_id = assigned.json()["id"]
    job, lease_token = claim_and_start(client, worker_id="workbuddy-mac")
    with client.app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO reminders "
            "(id, matter_id, action_id, kind, title, reason, status, fingerprint, "
            "due_at, created_at, updated_at) "
            "VALUES (?, ?, NULL, 'processing', ?, ?, 'open', ?, NULL, ?, ?)",
            (
                "reminder-old-job-error",
                matter_id,
                "本地处理需要关注",
                "WorkBuddy env: node: No such file or directory",
                f"job:{job['id']}:needs_review",
                "2099-01-01T00:00:00Z",
                "2099-01-01T00:00:00Z",
            ),
        )

    completed = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={
            "worker_id": "workbuddy-mac",
            "lease_token": lease_token,
            "result": {
                "matter_title": "预算口径确认",
                "summary": "WorkBuddy 已梳理待确认口径。",
                "actions": [
                    {
                        "kind": "waiting",
                        "title": "等待经营部门确认口径",
                        "detail": "收到答复后更新预测。",
                        "owner": "经营部门",
                        "due_date": None,
                    }
                ],
                "brief": {
                    "headline": "当前只差经营部门确认口径",
                    "what_i_did": ["梳理了待确认问题"],
                    "needs_you": "无需立即处理",
                    "next_check_at": "2099-01-02T09:00:00+08:00",
                    "next_check_reason": "检查经营部门是否已回复",
                },
                "agent_trace": {
                    "display_name": "贾维斯",
                    "provider": "workbuddy",
                    "status": "completed",
                },
            },
        },
        headers=worker_headers(),
    )

    assert completed.status_code == 200, completed.text
    matter = completed.json()
    assert matter["title"] == "预算口径确认"
    assert matter["assistant"]["display_name"] == "贾维斯"
    assert matter["assistant"]["brief"]["headline"] == "当前只差经营部门确认口径"
    assert matter["actions"][0]["material_id"] == material_id

    overview = client.get("/api/overview").json()
    assert all(
        reminder["reason"] != "WorkBuddy env: node: No such file or directory"
        for reminder in overview["reminders"]
    )
    assert overview["agent_activity"][0]["id"] == material_id
    assert overview["next_follow_up"]["reason"] == "检查经营部门是否已回复"


def test_requeue_replaces_unconfirmed_machine_output(client: TestClient) -> None:
    owner_login(client)
    received = receive(client, key="workbuddy-requeue", note="第一次分析")
    material_id = received.json()["material"]["id"]
    first_job, first_token = claim_and_start(client, worker_id="workbuddy-mac")
    first = client.post(
        f"/api/jobs/{first_job['id']}/complete",
        json={
            "worker_id": "workbuddy-mac",
            "lease_token": first_token,
            "result": {
                "matter_title": "重整测试",
                "summary": "第一次结果",
                "inferences": [
                    {
                        "field_type": "待确认",
                        "value": "旧判断",
                        "source_locator": "材料",
                        "quote": "第一次分析",
                        "confidence": 0.6,
                    }
                ],
                "actions": [{"kind": "task", "title": "旧动作"}],
            },
        },
        headers=worker_headers(),
    )
    assert first.status_code == 200, first.text

    queued = client.post(f"/api/materials/{material_id}/assistant")
    assert queued.status_code == 200, queued.text
    assert queued.json()["job"]["status"] == "queued"
    second_job, second_token = claim_and_start(client, worker_id="workbuddy-mac")
    second = client.post(
        f"/api/jobs/{second_job['id']}/complete",
        json={
            "worker_id": "workbuddy-mac",
            "lease_token": second_token,
            "result": {
                "matter_id": first.json()["id"],
                "matter_title": "重整测试",
                "summary": "第二次结果",
                "actions": [{"kind": "task", "title": "新动作"}],
            },
        },
        headers=worker_headers(),
    )

    assert second.status_code == 200, second.text
    open_actions = [item["title"] for item in second.json()["actions"] if item["status"] == "open"]
    assert open_actions == ["新动作"]
    assert all(item["status"] != "pending" for item in second.json()["reviews"])


def test_matters_expose_reliable_completion_state(client: TestClient) -> None:
    owner_login(client)
    receive(client, key="matter-completion", note="复核预算后关闭事项")
    job, lease_token = claim_and_start(client)
    completed = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={
            "worker_id": "mac-air",
            "lease_token": lease_token,
            "result": complete_result(),
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text

    before = client.get("/api/matters").json()[0]
    assert before["open_action_count"] == 1
    assert before["open_reminder_count"] == 0
    assert before["active_job_count"] == 0
    assert before["is_completed"] is False

    action_id = completed.json()["actions"][0]["id"]
    resolved = client.post(
        f"/api/actions/{action_id}/resolve",
        json={"status": "done"},
    )
    assert resolved.status_code == 200, resolved.text

    after = client.get("/api/matters").json()[0]
    assert after["open_action_count"] == 0
    assert after["is_completed"] is True


def test_action_assignees_are_suggested_and_confirmed(client: TestClient) -> None:
    owner_login(client)
    receive(client, key="assignee-suggestion", note="会议要求静姐明天把盘点表发过来")
    job, lease_token = claim_and_start(client)
    completed = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={
            "worker_id": "mac-air",
            "lease_token": lease_token,
            "result": {
                "matter_title": "盘点表跟进",
                "summary": "需要仓管补充盘点资料。",
                "actions": [
                    {
                        "kind": "task",
                        "title": "静姐明天把盘点表发过来",
                        "detail": "盘点表收到后复核库存数量。",
                        "owner": "",
                        "due_date": "2099-01-01",
                    }
                ],
            },
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text
    action = completed.json()["actions"][0]
    assert action["assignees"] == []
    assert action["assignee_suggestions"][0]["person_id"] == "person_li_jing"

    reviews = client.get("/api/assignee-reviews?status=pending")
    assert reviews.status_code == 200
    assert reviews.json()[0]["suggested_people"][0]["display_name"] == "李静"

    saved = client.put(
        f"/api/actions/{action['id']}/assignees",
        json={"person_ids": ["person_li_jing", "person_ou_bo"], "note": "Frank确认"},
    )
    assert saved.status_code == 200, saved.text
    assert [item["display_name"] for item in saved.json()["assignees"]] == ["李静", "欧波"]
    assert saved.json()["owner"] == "李静、欧波"

    people = client.get("/api/people").json()
    counts = {item["id"]: item["open_action_count"] for item in people}
    assert counts["person_li_jing"] == 1
    assert counts["person_ou_bo"] == 1


def test_pending_assignees_close_when_action_is_dismissed(client: TestClient) -> None:
    owner_login(client)
    receive(client, key="assignee-superseded", note="会议要求欧哥负责核对入库数量")
    job, lease_token = claim_and_start(client)
    completed = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={
            "worker_id": "mac-air",
            "lease_token": lease_token,
            "result": {
                "matter_title": "入库数量核对",
                "summary": "需要核对仓库入库数量。",
                "actions": [
                    {
                        "kind": "task",
                        "title": "欧哥负责核对入库数量",
                        "detail": "核对后反馈差异。",
                        "owner": "",
                        "due_date": None,
                    }
                ],
            },
        },
        headers=worker_headers(),
    )
    action_id = completed.json()["actions"][0]["id"]
    assert client.get("/api/assignee-reviews?status=pending").json()

    resolved = client.post(
        f"/api/actions/{action_id}/resolve",
        json={"status": "dismissed"},
    )
    assert resolved.status_code == 200, resolved.text
    assert client.get("/api/assignee-reviews?status=pending").json() == []


def test_security_headers_block_inline_content(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    policy = response.headers["content-security-policy"]
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy
    assert "'unsafe-inline'" not in policy


def test_frontend_markup_matches_javascript_and_hidden_contracts(
    client: TestClient,
) -> None:
    response = client.get("/")
    stylesheet = client.get("/static/app.css")
    service_worker = client.get("/sw.js")

    assert response.status_code == 200
    assert 'id="login-submit"' in response.text
    assert stylesheet.status_code == 200
    assert "[hidden] { display: none !important; }" in stylesheet.text
    assert '/static/app.css?v=55' in response.text
    assert '/static/app-evolution.css' not in response.text
    assert '/static/app.js?v=55' in response.text
    assert service_worker.status_code == 200
    assert "frank-personal-workbench-shell-v55" in service_worker.text
    assert "fetch(request).then" in service_worker.text
    assert ".catch(() => caches.match(request))" in service_worker.text
    assert "Mac 关机时" in response.text
    assert "财务工作台投递箱" in response.text


def test_frontend_keeps_platform_sync_and_review_entries_visible(
    client: TestClient,
) -> None:
    markup = client.get("/").text
    script = client.get("/static/app.js").text

    assert 'id="run-source-sync-global"' in markup
    assert "读取新信息" in markup
    assert "个人微信、企业微信和邮箱" in markup
    assert 'class="work-queue-bar"' in markup
    assert 'href="#/wechat"' in markup
    assert 'href="#/email"' in markup
    assert 'href="#/reviews"' in markup
    assert 'href="#/nodes"' in markup
    assert "runtime-disclosure" not in markup

    sync_body = script.split("async function runSourceSync()", 1)[1].split(
        "function setupIntake", 1
    )[0]
    assert 'api("/api/wechat/sync/run"' in sync_body
    assert 'api("/api/email/sync/run"' in sync_body
    assert 'api("/api/analysis/run"' not in sync_body


def test_frontend_refreshes_actions_without_full_page_jump(client: TestClient) -> None:
    script = client.get("/static/app.js").text

    assert "async function refreshRouteWithoutJump(focusSelectorOverride = null)" in script
    assert 'data-today-disclosure="rules"' in script
    assert "focusTarget.focus({ preventScroll: true })" in script
    assert 'if (routeFromHash().name === "today") refreshRouteWithoutJump();' in script
    for start, end in (
        ("async function resolveWechatCandidate", "async function changeWechatConversation"),
        ("async function resolveReview", "async function resolveAction"),
        ("async function resolveAction", "async function reanalyzeMaterial"),
        ("async function reanalyzeMaterial", "function updateReviewCount"),
    ):
        body = script.split(start, 1)[1].split(end, 1)[0]
        assert "refreshRouteWithoutJump" in body
        assert "renderRoute();" not in body


def test_frontend_separates_open_and_completed_matters(client: TestClient) -> None:
    script = client.get("/static/app.js").text

    assert 'data-matter-view="open"' in script
    assert 'data-matter-view="completed"' in script
    assert "未完成" in script
    assert "已完成" in script
    assert "item.is_completed" in script


def test_matter_page_exposes_reminder_and_action_resolution(client: TestClient) -> None:
    script = client.get("/static/app.js").text

    assert "未完成提醒" in script
    assert "已处理" in script
    assert "不再提醒" in script
    assert "完成对应行动" in script
    assert "标记完成" in script
    assert "无需继续" in script
    assert "重新打开" in script
    assert "async function resolveReminder" in script
    reminder_body = script.split("async function resolveReminder", 1)[1].split(
        "async function resolveAction", 1
    )[0]
    assert "refreshRouteWithoutJump" in reminder_body
    assert "renderRoute();" not in reminder_body


def test_invalid_and_oversize_intake_are_rejected(client: TestClient, tmp_path: Path) -> None:
    owner_login(client)
    invalid = receive(client, key="invalid-type", note="x", source_type="not-a-source")
    assert invalid.status_code == 422
    assert "不支持的材料类型" in invalid.json()["detail"]

    small_settings = Settings(
        data_dir=tmp_path / "small-workbench",
        owner_passcode="owner-pass",
        session_secret="test-session-secret",
        max_upload_bytes=8,
    )
    with TestClient(create_app(small_settings)) as small_client:
        owner_login(small_client)
        oversize = receive(
            small_client,
            key="oversize-file",
            note="",
            files={"upload": ("payload.txt", b"123456789", "text/plain")},
        )
        assert oversize.status_code == 422
    assert "超过允许的大小" in oversize.json()["detail"]


def test_frontend_exposes_persistent_source_receipt_and_useful_work_rules(
    client: TestClient,
) -> None:
    markup = client.get("/").text
    script = client.get("/static/app.js").text

    assert 'id="source-sync-receipt"' in markup
    assert 'id="source-sync-result-grid"' in markup
    assert 'id="source-sync-summary"' in markup
    assert 'id="source-sync-organize"' in markup
    assert "本次读取结果" in script
    assert "读取和整理分开" in script
    assert "同一件事持续合并" in script
    assert 'item.rule_type !== "conversation_ignore"' in script
    assert "管理监听范围" in script


def test_duplicate_job_completion_creates_result_once(client: TestClient) -> None:
    owner_login(client)
    material = receive(client, key="duplicate-completion").json()["material"]
    job, lease_token = claim_and_start(client)
    result = complete_result()
    payload = {
        "worker_id": "mac-air",
        "lease_token": lease_token,
        "result": result,
    }

    first = client.post(
        f"/api/jobs/{job['id']}/complete",
        json=payload,
        headers=worker_headers(),
    )
    second = client.post(
        f"/api/jobs/{job['id']}/complete",
        json=payload,
        headers=worker_headers(),
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    database = client.app.state.database
    assert database.fetch_one(
        "SELECT COUNT(*) AS total FROM actions WHERE material_id = ?",
        (material["id"],),
    )["total"] == len(result["actions"])
    assert database.fetch_one(
        "SELECT COUNT(*) AS total FROM evidence WHERE material_id = ?",
        (material["id"],),
    )["total"] == len(result.get("facts", [])) + len(result.get("inferences", []))


def test_reserved_completion_cannot_be_overwritten_by_failure(client: TestClient) -> None:
    owner_login(client)
    receive(client, key="completion-failure-race")
    job, lease_token = claim_and_start(client)
    service = client.app.state.service

    service.reserve_job_result(job["id"], "mac-air", lease_token)
    with pytest.raises(PermissionError):
        service.fail_job(job["id"], "mac-air", lease_token, "late failure")

    saved = client.app.state.database.fetch_one(
        "SELECT status, result_version FROM jobs WHERE id = ?", (job["id"],)
    )
    assert saved == {"status": "processing", "result_version": -1}
