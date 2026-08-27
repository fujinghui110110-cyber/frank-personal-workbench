from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services import build_attention


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
    assert response.status_code == 200, response.text


def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-token"}


def complete_result(
    client: TestClient,
    *,
    key: str,
    note: str,
    result: dict,
    source_type: str = "text",
) -> dict:
    received = client.post(
        "/api/intake",
        data={"source_type": source_type, "text_note": note},
        headers={"Idempotency-Key": key},
    )
    assert received.status_code == 201, received.text
    if result.get("matter_id"):
        assigned = client.post(
            f"/api/materials/{received.json()['material']['id']}/assign",
            json={"matter_id": result["matter_id"]},
        )
        assert assigned.status_code == 200, assigned.text
    released = client.post("/api/analysis/run")
    assert released.status_code == 200, released.text
    claimed = client.post(
        "/api/jobs/claim",
        json={"worker_id": key},
        headers=worker_headers(),
    )
    assert claimed.status_code == 200, claimed.text
    job = claimed.json()["job"]
    lease = {"worker_id": key, "lease_token": job["lease_token"]}
    started = client.post(
        f"/api/jobs/{job['id']}/start",
        json=lease,
        headers=worker_headers(),
    )
    assert started.status_code == 200, started.text
    completed = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={**lease, "result": result},
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


def test_ai_result_cannot_reassign_unbound_material_or_create_actions(
    client: TestClient,
) -> None:
    login(client)
    existing = client.app.state.service.create_matter(
        "既有事项",
        "test",
        "不得由 AI 自动归属",
    )
    received = client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": "一条尚未人工归属的新材料"},
        headers={"Idempotency-Key": "ai-matter-boundary"},
    )
    assert received.status_code == 201, received.text
    material_id = received.json()["material"]["id"]

    assert client.post("/api/analysis/run").status_code == 200
    claimed = client.post(
        "/api/jobs/claim",
        json={"worker_id": "ai-matter-boundary"},
        headers=worker_headers(),
    )
    job = claimed.json()["job"]
    lease = {
        "worker_id": "ai-matter-boundary",
        "lease_token": job["lease_token"],
    }
    assert client.post(f"/api/jobs/{job['id']}/start", json=lease).status_code == 200
    completed = client.post(
        f"/api/jobs/{job['id']}/complete",
        json={
            **lease,
            "result": {
                "matter_id": existing["id"],
                "matter_title": "AI 建议的新事项",
                "summary": "仅作为待确认事项草稿",
                "facts": [],
                "inferences": [],
                "actions": [
                    {
                        "kind": "task",
                        "title": "AI 建议动作",
                        "detail": "不得直接创建正式行动",
                    }
                ],
            },
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text

    material = client.app.state.database.fetch_one(
        "SELECT matter_id FROM materials WHERE id = ?",
        (material_id,),
    )
    assert material["matter_id"] != existing["id"]
    drafted = client.get(f"/api/matters/{material['matter_id']}").json()
    assert drafted["status"] == "needs_decision"
    assert not [
        item
        for item in client.get("/api/actions", params={"status": "all"}).json()
        if item["matter_id"] == material["matter_id"]
    ]


def complete_material(
    client: TestClient,
    *,
    key: str,
    title: str,
    note: str,
    due_date: str | None = None,
    inference: bool = False,
    assignee: str | None = None,
) -> dict:
    action = {
        "kind": "task",
        "title": title,
        "detail": note,
        "owner": "财务负责人",
        "due_date": due_date,
        "flow_state": "needs_action",
        "waiting_on": "",
        "blocked_reason": "",
        "next_follow_up_at": None,
        "estimated_minutes": 30,
        "assignee_suggestions": [],
    }
    if assignee:
        action["assignee_suggestions"] = [
            {
                "person": assignee,
                "detected_alias": assignee,
                "reason": f"原文明确由{assignee}跟进",
                "evidence": [f"{assignee}继续跟进"],
                "confidence": 0.95,
            }
        ]
    result = {
        "matter_title": title,
        "summary": note,
        "actions": [action],
        "inferences": [],
    }
    if inference:
        result["inferences"] = [
            {
                "field_type": "事项类型",
                "value": title,
                "source_locator": "原文第1行",
                "quote": note,
                "confidence": 0.8,
            }
        ]
    matter = complete_result(client, key=key, note=note, result=result)
    package_response = client.get(f"/api/matters/{matter['id']}/work-package")
    assert package_response.status_code == 200, package_response.text
    package = package_response.json().get("work_package", package_response.json())
    applied = client.post(
        f"/api/matters/{matter['id']}/work-package/apply",
        json={"step_indexes": [0], "expected_updated_at": package["updated_at"]},
    )
    assert applied.status_code == 200, applied.text
    refreshed = client.get(f"/api/matters/{matter['id']}")
    assert refreshed.status_code == 200, refreshed.text
    return refreshed.json()


def action_for_matter(client: TestClient, matter_id: str) -> dict:
    response = client.get(
        "/api/actions",
        params={"status": "all", "include_snoozed": "true"},
    )
    assert response.status_code == 200, response.text
    return next(item for item in response.json() if item["matter_id"] == matter_id)


def suggest_completion(
    client: TestClient,
    *,
    key: str,
    matter_id: str,
    action_id: str,
    evidence: str,
) -> None:
    complete_result(
        client,
        key=key,
        note=evidence,
        result={
            "matter_id": matter_id,
            "summary": evidence,
            "actions": [],
            "inferences": [],
            "completion_suggestions": [
                {
                    "action_id": action_id,
                    "reason": "后续材料提供了明确完成证明",
                    "evidence": [evidence],
                }
            ],
        },
    )


def test_attention_queue_is_unique_and_follows_priority_order() -> None:
    now = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
    actions = [
        {
            "id": "action-decision",
            "matter_id": "matter-1",
            "matter_title": "预算拍板",
            "title": "确认预算口径",
            "flow_state": "needs_action",
        },
        {
            "id": "action-assignee",
            "matter_id": "matter-2",
            "matter_title": "库存跟进",
            "title": "确认仓管负责人",
            "flow_state": "needs_action",
        },
        {
            "id": "action-overdue",
            "matter_id": "matter-3",
            "matter_title": "逾期复核",
            "title": "补做复核",
            "due_date": "2026-08-25",
            "flow_state": "needs_action",
        },
        {
            "id": "action-follow-up",
            "matter_id": "matter-4",
            "matter_title": "等待反馈",
            "title": "跟进反馈",
            "flow_state": "waiting",
            "next_follow_up_at": "2026-08-26T07:00:00Z",
        },
        {
            "id": "action-risk",
            "matter_id": "matter-5",
            "matter_title": "阻塞风险",
            "title": "处理缺失资料",
            "kind": "risk",
            "flow_state": "blocked",
        },
        {
            "id": "action-normal",
            "matter_id": "matter-6",
            "matter_title": "普通行动",
            "title": "整理台账",
            "flow_state": "needs_action",
        },
    ]
    reviews = [
        {
            "id": "review-decision",
            "matter_id": "matter-1",
            "matter_title": "预算拍板",
            "title": "确认预算口径",
            "payload": {"action_id": "action-decision"},
        }
    ]
    assignee_reviews = [
        {
            "action_id": "action-assignee",
            "matter_id": "matter-2",
            "matter_title": "库存跟进",
            "action_title": "确认仓管负责人",
        }
    ]
    reminders = [
        {
            "id": "reminder-review",
            "matter_id": "matter-1",
            "matter_title": "预算拍板",
            "title": "确认预算口径",
            "reason": "待人工确认",
            "status": "open",
            "fingerprint": "review:review-decision",
            "due_at": "2026-08-26T07:00:00Z",
        },
        {
            "id": "reminder-unlinked",
            "matter_id": "matter-7",
            "matter_title": "独立提醒",
            "title": "查看新反馈",
            "reason": "提醒已到期",
            "status": "open",
            "due_at": "2026-08-26T07:00:00Z",
        }
    ]

    items, counts = build_attention(
        actions, reviews, assignee_reviews, reminders, now.date(), now
    )

    assert [item["item_type"] for item in items] == [
        "decision",
        "assignee_confirmation",
        "overdue",
        "follow_up",
        "reminder",
        "risk",
        "action",
    ]
    assert {item["id"] for item in items} == {
        "review-decision",
        "action-assignee",
        "action-overdue",
        "action-follow-up",
        "reminder-unlinked",
        "action-risk",
        "action-normal",
    }
    assert len(items) == len({(item["item_type"], item["id"]) for item in items})
    assert counts["total"] == len(items)
    assert counts["decision"] == 1
    assert counts["assignee_confirmation"] == 1


def test_unverified_action_date_becomes_suggestion_and_does_not_remind(
    client: TestClient,
) -> None:
    login(client)
    due_date = date.today().isoformat()
    matter = complete_result(
        client,
        key="suggested-date-boundary",
        note="模型建议今天完成复核，但材料没有明确日期证据。",
        result={
            "matter_title": "日期边界测试",
            "summary": "验证未核实日期只进入待确认。",
            "actions": [
                {
                    "kind": "task",
                    "title": "今天完成复核",
                    "detail": "日期来自模型建议。",
                    "owner": "财务负责人",
                    "due_date": due_date,
                }
            ],
            "facts": [],
        },
    )

    package_response = client.get(f"/api/matters/{matter['id']}/work-package")
    assert package_response.status_code == 200, package_response.text
    package = package_response.json().get("work_package", package_response.json())
    step = package["draft"]["steps"][0]
    assert step["schedule_basis"] == "suggested"
    assert step["due_date"] is None
    assert step["suggested_due_date"] == due_date
    assert not [
        item
        for item in client.get("/api/actions", params={"status": "all"}).json()
        if item["matter_id"] == matter["id"]
    ]
    reviews = client.get("/api/reviews").json()
    assert not any(
        item["kind"] == "schedule" and item["matter_id"] == matter["id"]
        for item in reviews
    )
    scan = client.post("/api/proactive/scan")
    assert scan.status_code == 200
    reminders = client.app.state.database.fetch_all(
        "SELECT action_id, kind FROM reminders WHERE matter_id = ?",
        (matter["id"],),
    )
    assert all(item["action_id"] is None for item in reminders)
    assert all(item["kind"] != "overdue" for item in reminders)


def test_explicit_action_date_can_create_local_reminder(client: TestClient) -> None:
    login(client)
    due_date = date.today().isoformat()
    matter = complete_result(
        client,
        key="explicit-date-boundary",
        note=f"请于 {due_date} 完成复核。",
        result={
            "matter_title": "明确日期测试",
            "summary": "验证同一材料中的明确日期可以排程。",
            "facts": [
                {
                    "field_type": "日期",
                    "value": due_date,
                    "source_locator": "原文第1行",
                    "quote": f"请于 {due_date} 完成复核。",
                    "confidence": 1.0,
                }
            ],
            "actions": [
                {
                    "kind": "task",
                    "title": "完成复核",
                    "detail": "材料明确给出截止日期。",
                    "owner": "财务负责人",
                    "due_date": due_date,
                    "schedule_basis": "material_explicit",
                }
            ],
        },
    )

    package_response = client.get(f"/api/matters/{matter['id']}/work-package")
    assert package_response.status_code == 200, package_response.text
    package = package_response.json().get("work_package", package_response.json())
    step = package["draft"]["steps"][0]
    assert step["schedule_basis"] == "material_explicit"
    assert step["due_date"] == due_date
    applied = client.post(
        f"/api/matters/{matter['id']}/work-package/apply",
        json={"step_indexes": [0], "expected_updated_at": package["updated_at"]},
    )
    assert applied.status_code == 200, applied.text
    action = action_for_matter(client, matter["id"])
    assert action["schedule_basis"] == "user_entered"
    assert action["due_date"] == due_date
    scan = client.post("/api/proactive/scan")
    assert scan.status_code == 200, scan.text
    brief = client.get("/api/today/brief", params={"brief_date": due_date})
    assert brief.status_code == 200, brief.text
    attention = [
        item for item in brief.json()["attention"] if item["matter_id"] == matter["id"]
    ]
    assert len(attention) == 1
    reminders = client.app.state.database.fetch_all(
        "SELECT action_id, status, due_at FROM reminders WHERE matter_id = ?",
        (matter["id"],),
    )
    assert any(
        item["action_id"] == action["id"]
        and item["status"] == "open"
        and str(item["due_at"]).startswith(due_date)
        for item in reminders
    )


def test_manual_follow_up_uses_user_entered_schedule_basis(client: TestClient) -> None:
    login(client)
    matter = complete_material(
        client,
        key="manual-follow-up-boundary",
        title="手工跟进日期",
        note="等待反馈后继续处理。",
    )
    action = action_for_matter(client, matter["id"])
    follow_up = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    updated = client.patch(
        f"/api/actions/{action['id']}/planning-state",
        json={"next_follow_up_at": follow_up},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["schedule_basis"] == "user_entered"


def test_brief_and_overview_share_workflow_statistics(client: TestClient) -> None:
    login(client)
    complete_material(
        client,
        key="stats-review",
        title="待确认统计",
        note="需要确认统计口径。",
        inference=True,
    )
    complete_material(
        client,
        key="stats-assignee",
        title="负责人统计",
        note="会议要求李静继续跟进。",
        assignee="李静",
    )
    brief = client.get("/api/today/brief")
    overview = client.get("/api/overview")
    assert brief.status_code == overview.status_code == 200
    brief_counts = brief.json()["counts"]
    overview_counts = overview.json()["counts"]
    for key in (
        "open_actions",
        "attention",
        "pending_reviews",
        "open_reminders",
        "due_today",
        "overdue",
    ):
        assert brief_counts[key] == overview_counts[key]
    assert brief.json()["attention_counts"] == overview_counts["attention_counts"]


def test_node_status_preserves_stale_and_historical_lifecycles(
    client: TestClient,
) -> None:
    login(client)
    now = datetime.now(UTC)
    rows = [
        ("node-current", "当前节点", now - timedelta(minutes=1)),
        ("node-stale", "短期离线节点", now - timedelta(minutes=6)),
        ("node-history", "历史离线节点", now - timedelta(hours=25)),
    ]
    with client.app.state.database.connect() as connection:
        connection.executemany(
            "INSERT INTO nodes (id, name, status, last_seen_at, metadata_json) "
            "VALUES (?, ?, 'online', ?, '{}')",
            [
                (node_id, name, timestamp.isoformat().replace("+00:00", "Z"))
                for node_id, name, timestamp in rows
            ],
        )

    response = client.get("/api/nodes")
    assert response.status_code == 200, response.text
    nodes = {item["id"]: item for item in response.json()}
    assert nodes["node-current"]["status"] == "online"
    assert nodes["node-current"]["lifecycle"] == "current"
    assert nodes["node-stale"]["status"] == "offline"
    assert nodes["node-stale"]["lifecycle"] == "stale"
    assert nodes["node-history"]["status"] == "offline"
    assert nodes["node-history"]["lifecycle"] == "historical"
    assert set(nodes) == {row[0] for row in rows}


def test_daily_brief_is_idempotent_and_has_one_now_and_three_next(
    client: TestClient,
) -> None:
    login(client)
    for index in range(5):
        complete_material(
            client,
            key=f"brief-{index}",
            title=f"待办事项{index}",
            note=f"需要完成第{index}项复核",
            due_date=f"2099-01-{index + 1:02d}",
        )
    first = client.get("/api/today/brief")
    second = client.get("/api/today/brief")
    assert first.status_code == second.status_code == 200
    first_body = first.json()
    second_body = second.json()
    assert first_body["id"] == second_body["id"]
    assert first_body["generated_at"] == second_body["generated_at"]
    assert first_body["source_watermark"] == second_body["source_watermark"]
    assert first_body["now"] is not None
    assert len(first_body["next"]) == 3
    assert first_body == second_body


def test_planning_state_pin_snooze_and_wait_are_respected(client: TestClient) -> None:
    login(client)
    matter = complete_material(
        client,
        key="planning-state",
        title="月末报表复核",
        note="复核月末报表后提交",
        due_date="2099-02-01",
    )
    action = action_for_matter(client, matter["id"])
    pinned = client.patch(
        f"/api/actions/{action['id']}/planning-state",
        json={"pinned": True, "estimated_minutes": 45},
    )
    assert pinned.status_code == 200, pinned.text
    assert pinned.json()["pinned_at"]
    assert pinned.json()["estimated_minutes"] == 45
    assert client.get("/api/today/brief").json()["now"]["id"] == action["id"]
    snoozed = client.patch(
        f"/api/actions/{action['id']}/planning-state",
        json={"pinned": False, "snoozed_until": "2099-02-02T09:00:00Z"},
    )
    assert snoozed.status_code == 200, snoozed.text
    brief = client.get("/api/today/brief").json()
    execution_ids = [
        item["id"]
        for item in [brief.get("now"), *brief["next"]]
        if item is not None
    ]
    assert action["id"] not in execution_ids
    waiting = client.patch(
        f"/api/actions/{action['id']}/planning-state",
        json={
            "flow_state": "waiting",
            "waiting_on": "李静反馈库存明细",
            "next_follow_up_at": "2099-02-03T09:00:00Z",
            "snoozed_until": None,
        },
    )
    assert waiting.status_code == 200, waiting.text
    brief = client.get("/api/today/brief").json()
    assert action["id"] in {item["id"] for item in brief["waiting"]}
    assert action["id"] not in {
        item["id"] for item in [brief.get("now"), *brief["next"]] if item
    }


def test_search_covers_work_and_excludes_unconfirmed_auto_chat(
    client: TestClient,
) -> None:
    login(client)
    complete_material(
        client,
        key="fts-match",
        title="供应商付款复核",
        note="供应商付款资料待复核",
    )
    hidden = client.post(
        "/api/intake",
        data={"source_type": "wechat_auto", "text_note": "私人晚餐暗号"},
        headers={"Idempotency-Key": "fts-hidden-chat"},
    )
    assert hidden.status_code == 201, hidden.text
    found = client.get("/api/search", params={"q": "付款"})
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["items"]
    assert body["answer"] is None
    assert all(item["searchable"] for item in body["items"])
    assert any(item["title"] == "供应商付款复核" for item in body["items"])
    answered = client.get(
        "/api/search", params={"q": "付款", "include_answer": "true"}
    ).json()
    assert 1 <= len(answered["answer"]["sources"]) <= 4
    excluded = client.get("/api/search", params={"q": "私人晚餐暗号"})
    assert excluded.status_code == 200, excluded.text
    assert excluded.json()["items"] == []


def test_matter_timeline_is_chronological_and_provenanced(client: TestClient) -> None:
    login(client)
    matter = complete_material(
        client,
        key="timeline-material",
        title="预算复核",
        note="预算金额需要复核",
        inference=True,
    )
    response = client.get(f"/api/matters/{matter['id']}/timeline")
    assert response.status_code == 200, response.text
    events = response.json()
    assert len(events) >= 2
    assert [item["created_at"] for item in events] == sorted(
        item["created_at"] for item in events
    )
    assert all(
        {"type", "summary", "source", "created_at"} <= item.keys()
        for item in events
    )


def test_activity_receipts_use_chinese_business_labels(client: TestClient) -> None:
    login(client)
    complete_material(
        client,
        key="activity-receipt",
        title="合同归档",
        note="整理合同并归档",
    )
    response = client.get("/api/activity/receipts", params={"limit": 20})
    assert response.status_code == 200, response.text
    receipts = response.json()
    assert receipts
    assert all(
        {"action", "summary", "source", "created_at"} <= item.keys()
        for item in receipts
    )
    assert any(item["action"] == "已完成材料整理" for item in receipts)


def test_review_queue_resolves_normal_items_in_one_request(client: TestClient) -> None:
    login(client)
    matter_ids = []
    for index in range(2):
        matter = complete_material(
            client,
            key=f"review-batch-{index}",
            title=f"库存复核{index}",
            note=f"复核第{index}份库存资料",
            inference=True,
        )
        matter_ids.append(matter["id"])
    reviews = client.get("/api/reviews")
    assert reviews.status_code == 200, reviews.text
    review_ids = [
        item["id"]
        for item in reviews.json()
        if item["matter_id"] in matter_ids and item["status"] == "pending"
    ]
    assert len(review_ids) == 2
    resolved = client.post(
        "/api/review-queue/resolve",
        json={"review_ids": review_ids, "resolution": "accepted"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["updated_count"] == 2


def test_completion_suggestion_is_deduplicated_in_work_package_without_closing_follow_up(
    client: TestClient,
) -> None:
    login(client)
    matter = complete_material(
        client,
        key="completion-origin",
        title="盘点差异复核",
        note="李静继续跟进盘点差异",
        due_date="2099-03-01",
        assignee="李静",
    )
    action = action_for_matter(client, matter["id"])
    now = datetime.now(UTC).isoformat()
    with client.app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO reminders "
            "(id, matter_id, action_id, kind, title, reason, status, fingerprint, due_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
            (
                "reminder-completion-test",
                matter["id"],
                action["id"],
                "follow_up",
                "跟进盘点差异",
                "等待反馈",
                "completion-test",
                "2099-03-02T09:00:00Z",
                now,
                now,
            ),
        )
    suggest_completion(
        client,
        key="completion-proof-1",
        matter_id=matter["id"],
        action_id=action["id"],
        evidence="盘点差异表已复核并确认无误",
    )
    suggest_completion(
        client,
        key="completion-proof-2",
        matter_id=matter["id"],
        action_id=action["id"],
        evidence="再次确认盘点差异已经处理完成",
    )
    completion_reviews = [
        item
        for item in client.get("/api/reviews").json()
        if item["kind"] == "action_completion" and item["status"] == "pending"
    ]
    assert completion_reviews == []
    package_response = client.get(f"/api/matters/{matter['id']}/work-package")
    assert package_response.status_code == 200, package_response.text
    package = package_response.json().get("work_package", package_response.json())
    prefix = f"是否确认行动 {action['id']} 已完成"
    questions = [
        question
        for question in package["draft"]["questions"]
        if question.startswith(prefix)
    ]
    assert len(questions) == 1
    assert "再次确认盘点差异已经处理完成" not in questions[0]
    assert action_for_matter(client, matter["id"])["status"] == "open"
    reminder = client.app.state.database.fetch_one(
        "SELECT status FROM reminders WHERE id = ?",
        ("reminder-completion-test",),
    )
    assert reminder["status"] == "open"
    pending_assignees = client.get(
        "/api/assignee-reviews", params={"status": "pending"}
    )
    assert pending_assignees.status_code == 200, pending_assignees.text
    assert any(item["action_id"] == action["id"] for item in pending_assignees.json())
    timeline = client.get(f"/api/matters/{matter['id']}/timeline").json()
    assert not any(item["type"] == "action.completed" for item in timeline)


def test_unapplied_completion_question_keeps_action_open(client: TestClient) -> None:
    login(client)
    matter = complete_material(
        client,
        key="completion-reject-origin",
        title="合同盖章跟进",
        note="继续跟进合同盖章",
    )
    action = action_for_matter(client, matter["id"])
    suggest_completion(
        client,
        key="completion-reject-proof",
        matter_id=matter["id"],
        action_id=action["id"],
        evidence="对方说材料可能已经盖章",
    )
    assert not any(
        item["kind"] == "action_completion" and item["status"] == "pending"
        for item in client.get("/api/reviews").json()
    )
    package_response = client.get(f"/api/matters/{matter['id']}/work-package")
    assert package_response.status_code == 200, package_response.text
    package = package_response.json().get("work_package", package_response.json())
    assert any(
        question.startswith(f"是否确认行动 {action['id']} 已完成")
        for question in package["draft"]["questions"]
    )
    assert action_for_matter(client, matter["id"])["status"] == "open"


def test_people_view_exposes_self_counts_and_identity_endpoint(client: TestClient) -> None:
    login(client)
    people = client.get("/api/people")
    assert people.status_code == 200, people.text
    self_rows = [item for item in people.json() if item.get("is_self")]
    assert len(self_rows) == 1
    assert {
        "open_action_count",
        "pending_action_count",
        "aliases",
    } <= self_rows[0].keys()
    identities = client.get(f"/api/people/{self_rows[0]['id']}/identities")
    assert identities.status_code == 200, identities.text
    assert isinstance(identities.json(), list)


def test_matter_target_date_and_progress_are_recorded_in_timeline(
    client: TestClient,
) -> None:
    login(client)
    matter = complete_material(
        client,
        key="matter-progress",
        title="月末盘点闭环",
        note="需要完成月末盘点差异复核",
    )
    changed = client.patch(
        f"/api/matters/{matter['id']}", json={"target_date": "2026-08-28"}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["target_date"] == "2026-08-28"

    progress = client.post(
        f"/api/matters/{matter['id']}/progress",
        json={"summary": "已完成第一轮差异核对", "detail": "还差仓库签字确认。"},
    )
    assert progress.status_code == 200, progress.text
    assert progress.json()["type"] == "progress.note"

    timeline = client.get(f"/api/matters/{matter['id']}/timeline").json()
    assert any(item["type"] == "matter.updated" for item in timeline)
    assert any(
        item["type"] == "progress.note"
        and item["summary"] == "已完成第一轮差异核对"
        and item["payload"]["detail"] == "还差仓库签字确认。"
        for item in timeline
    )


def test_matter_close_requires_clean_preview_and_can_reopen(client: TestClient) -> None:
    login(client)
    matter = complete_material(
        client,
        key="matter-manual-status",
        title="手工关闭事项",
        note="确认事项状态修改会同步关闭开放行动",
    )
    assert action_for_matter(client, matter["id"])["status"] == "open"

    rejected = client.patch(
        f"/api/matters/{matter['id']}", json={"status": "completed"}
    )
    assert rejected.status_code == 422, rejected.text
    action = action_for_matter(client, matter["id"])
    assert action["status"] == "open"

    resolved = client.post(
        f"/api/actions/{action['id']}/resolve", json={"status": "done"}
    )
    assert resolved.status_code == 200, resolved.text
    ready = client.get(f"/api/matters/{matter['id']}/close-preview").json()
    assert ready["can_close"] is True
    closed = client.post(
        f"/api/matters/{matter['id']}/close",
        json={
            "expected_updated_at": ready["updated_at"],
            "completion_note": "已人工核对全部未完成内容。",
        },
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["is_completed"] is True

    reopened = client.patch(
        f"/api/matters/{matter['id']}", json={"status": "active"}
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["status"] == "active"
    assert reopened.json()["is_completed"] is False
    assert action_for_matter(client, matter["id"])["status"] == "done"
    timeline = client.get(f"/api/matters/{matter['id']}/timeline").json()
    assert any(item["type"] == "matter.closed" for item in timeline)
    assert any(item["type"] == "matter.updated" for item in timeline)
