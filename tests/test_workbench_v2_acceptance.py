from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


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


def seed_matter(
    client: TestClient,
    *,
    matter_id: str,
    action_statuses: tuple[str, ...] = (),
    reminder_statuses: tuple[str, ...] = (),
    review_statuses: tuple[str, ...] = (),
) -> dict[str, Any]:
    now = utc_timestamp()
    database = client.app.state.database
    action_ids: list[str] = []
    reminder_ids: list[str] = []
    review_ids: list[str] = []
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO matters "
            "(id, title, status, summary, goal, completion_criteria, owner, "
            "target_date, created_at, updated_at) "
            "VALUES (?, ?, 'active', ?, '', '', '财务负责人', NULL, ?, ?)",
            (matter_id, f"v2 验收事项 {matter_id}", "测试事项", now, now),
        )
        for index, status in enumerate(action_statuses):
            action_id = f"{matter_id}-action-{index}"
            action_ids.append(action_id)
            flow_state = "completed" if status == "done" else "needs_action"
            completion_evidence = '["人工处理"]' if status != "open" else "[]"
            connection.execute(
                "INSERT INTO actions "
                "(id, matter_id, kind, title, detail, status, owner, due_date, "
                "evidence_id, created_by, created_at, updated_at, flow_state, "
                "waiting_on, blocked_reason, next_follow_up_at, schedule_basis, "
                "estimated_minutes, pinned_at, snoozed_until, completion_evidence) "
                "VALUES (?, ?, 'task', ?, '', ?, '财务负责人', NULL, NULL, 'test', "
                "?, ?, ?, '', '', NULL, 'legacy', NULL, NULL, NULL, ?)",
                (
                    action_id,
                    matter_id,
                    f"行动步骤 {index}",
                    status,
                    now,
                    now,
                    flow_state,
                    completion_evidence,
                ),
            )
        for index, status in enumerate(reminder_statuses):
            reminder_id = f"{matter_id}-reminder-{index}"
            reminder_ids.append(reminder_id)
            connection.execute(
                "INSERT INTO reminders "
                "(id, matter_id, action_id, kind, title, reason, status, "
                "fingerprint, due_at, created_at, updated_at) "
                "VALUES (?, ?, NULL, 'follow_up', ?, '测试提醒', ?, ?, NULL, ?, ?)",
                (
                    reminder_id,
                    matter_id,
                    f"提醒 {index}",
                    status,
                    f"{matter_id}:reminder:{index}",
                    now,
                    now,
                ),
            )
        for index, status in enumerate(review_statuses):
            review_id = f"{matter_id}-review-{index}"
            review_ids.append(review_id)
            resolved_at = None if status == "pending" else now
            connection.execute(
                "INSERT INTO review_items "
                "(id, matter_id, material_id, evidence_id, kind, title, payload_json, "
                "confidence, status, created_at, resolved_at, resolution_note) "
                "VALUES (?, ?, NULL, NULL, 'decision', ?, '{}', 1.0, ?, ?, ?, ?)",
                (
                    review_id,
                    matter_id,
                    f"待确认 {index}",
                    status,
                    now,
                    resolved_at,
                    "人工处理" if resolved_at else None,
                ),
            )
    return {
        "id": matter_id,
        "updated_at": now,
        "action_ids": action_ids,
        "reminder_ids": reminder_ids,
        "review_ids": review_ids,
    }


def child_snapshot(client: TestClient, matter_id: str) -> dict[str, list[dict[str, Any]]]:
    database = client.app.state.database
    return {
        table: database.fetch_all(
            f"SELECT * FROM {table} WHERE matter_id = ? ORDER BY id", (matter_id,)
        )
        for table in ("actions", "reminders", "review_items")
    }


def matter_snapshot(client: TestClient, matter_id: str) -> dict[str, Any]:
    return client.app.state.database.fetch_one(
        "SELECT * FROM matters WHERE id = ?", (matter_id,)
    )


def scoped_audits(client: TestClient, matter_id: str) -> list[dict[str, Any]]:
    return client.app.state.database.fetch_all(
        "SELECT * FROM audit_events WHERE matter_id = ? ORDER BY created_at, id",
        (matter_id,),
    )


def scoped_events(client: TestClient, matter_id: str) -> list[dict[str, Any]]:
    return client.app.state.database.fetch_all(
        "SELECT * FROM matter_events WHERE matter_id = ? ORDER BY created_at, id",
        (matter_id,),
    )


def package_body(response) -> dict[str, Any]:
    body = response.json()
    return body.get("work_package", body)


def work_package_draft() -> dict[str, Any]:
    return {
        "conclusion": "按现有材料继续推进复核。",
        "basis": [],
        "gaps": [],
        "risks": [],
        "steps": [
            {
                "title": "核对预算",
                "detail": "核对预算明细和原始凭证。",
                "kind": "task",
                "owner": "财务负责人",
                "flow_state": "needs_action",
            },
            {
                "title": "补充差异说明",
                "detail": "整理未解决的差异说明。",
                "kind": "task",
                "owner": "财务负责人",
                "flow_state": "needs_action",
            },
            {
                "title": "汇报复核结果",
                "detail": "形成内部汇报口径。",
                "kind": "task",
                "owner": "财务负责人",
                "flow_state": "needs_action",
            },
        ],
        "questions": [],
        "reply_draft": {"purpose": "内部汇报", "text": "复核结果待确认。"},
    }


def test_analysis_prepares_missing_work_package_without_creating_actions(
    client: TestClient,
) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-package-prepare",
        action_statuses=("open",),
    )
    before_actions = client.app.state.database.fetch_one(
        "SELECT COUNT(*) AS count FROM actions WHERE matter_id = ?",
        (matter["id"],),
    )["count"]

    first = client.post("/api/analysis/run")
    assert first.status_code == 200, first.text
    assert first.json()["work_packages_prepared"] == 1
    assert first.json()["work_packages_failed"] == []
    assert package_body(
        client.get(f"/api/matters/{matter['id']}/work-package")
    )["status"] == "draft"
    after_actions = client.app.state.database.fetch_one(
        "SELECT COUNT(*) AS count FROM actions WHERE matter_id = ?",
        (matter["id"],),
    )["count"]
    assert after_actions == before_actions

    second = client.post("/api/analysis/run")
    assert second.status_code == 200, second.text
    assert second.json()["work_packages_prepared"] == 0


def test_close_preview_is_read_only_and_lists_open_children(client: TestClient) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-close-preview",
        action_statuses=("open",),
        reminder_statuses=("open",),
        review_statuses=("pending",),
    )
    before_matter = matter_snapshot(client, matter["id"])
    before_children = child_snapshot(client, matter["id"])
    before_audits = scoped_audits(client, matter["id"])
    before_events = scoped_events(client, matter["id"])

    response = client.get(f"/api/matters/{matter['id']}/close-preview")

    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["matter_id"] == matter["id"]
    assert preview["matter_status"] == "active"
    assert preview["can_close"] is False
    assert preview["blocker_count"] == 3
    assert [item["id"] for item in preview["blockers"]["actions"]] == matter[
        "action_ids"
    ]
    assert [item["id"] for item in preview["blockers"]["reminders"]] == matter[
        "reminder_ids"
    ]
    assert [item["id"] for item in preview["blockers"]["reviews"]] == matter[
        "review_ids"
    ]
    assert preview["blockers"]["active_jobs"] == []
    assert matter_snapshot(client, matter["id"]) == before_matter
    assert child_snapshot(client, matter["id"]) == before_children
    assert scoped_audits(client, matter["id"]) == before_audits
    assert scoped_events(client, matter["id"]) == before_events


def test_close_rejects_unprocessed_children_without_mutating_them(
    client: TestClient,
) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-close-blocked",
        action_statuses=("open",),
        reminder_statuses=("open",),
        review_statuses=("pending",),
    )
    before_matter = matter_snapshot(client, matter["id"])
    before_children = child_snapshot(client, matter["id"])
    before_audits = scoped_audits(client, matter["id"])
    before_events = scoped_events(client, matter["id"])

    response = client.post(
        f"/api/matters/{matter['id']}/close",
        json={
            "expected_updated_at": matter["updated_at"],
            "completion_note": "尝试关闭但仍有未处理子项。",
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]
    assert matter_snapshot(client, matter["id"]) == before_matter
    assert child_snapshot(client, matter["id"]) == before_children
    assert scoped_audits(client, matter["id"]) == before_audits
    assert scoped_events(client, matter["id"]) == before_events


def test_close_only_closes_matter_after_children_are_handled(client: TestClient) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-close-success",
        action_statuses=("done", "dismissed"),
        reminder_statuses=("done", "dismissed"),
        review_statuses=("confirmed", "rejected"),
    )
    before_children = child_snapshot(client, matter["id"])

    response = client.post(
        f"/api/matters/{matter['id']}/close",
        json={
            "expected_updated_at": matter["updated_at"],
            "completion_note": "行动、提醒和待确认项均已处理。",
        },
    )

    assert response.status_code == 200, response.text
    closed = response.json()
    assert closed["status"] == "completed"
    assert closed["is_completed"] is True
    assert matter_snapshot(client, matter["id"])["status"] == "completed"
    assert child_snapshot(client, matter["id"]) == before_children


def test_reopen_does_not_change_handled_children(client: TestClient) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-reopen",
        action_statuses=("done", "dismissed"),
        reminder_statuses=("done", "dismissed"),
        review_statuses=("confirmed", "rejected"),
    )
    closed = client.post(
        f"/api/matters/{matter['id']}/close",
        json={
            "expected_updated_at": matter["updated_at"],
            "completion_note": "先完成收尾，再验证重新打开。",
        },
    )
    assert closed.status_code == 200, closed.text
    children_after_close = child_snapshot(client, matter["id"])

    response = client.patch(
        f"/api/matters/{matter['id']}",
        json={
            "status": "active",
            "expected_updated_at": closed.json()["updated_at"],
            "reason": "重新打开继续观察",
        },
    )

    assert response.status_code == 200, response.text
    reopened = response.json()
    assert reopened["status"] == "active"
    assert reopened["is_completed"] is False
    assert child_snapshot(client, matter["id"]) == children_after_close


def test_work_package_generation_does_not_create_actions(client: TestClient) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-package-generate",
        action_statuses=("open",),
        reminder_statuses=("open",),
        review_statuses=("pending",),
    )
    before_children = child_snapshot(client, matter["id"])
    before_matter = matter_snapshot(client, matter["id"])
    before_audits = scoped_audits(client, matter["id"])
    before_events = scoped_events(client, matter["id"])

    empty = client.get(f"/api/matters/{matter['id']}/work-package")
    assert empty.status_code == 200, empty.text
    assert empty.json() is None

    generated = client.post(f"/api/matters/{matter['id']}/work-package/generate")

    assert generated.status_code == 200, generated.text
    package = package_body(generated)
    assert package["matter_id"] == matter["id"]
    assert {"id", "status", "draft", "updated_at"} <= package.keys()
    stored = client.get(f"/api/matters/{matter['id']}/work-package")
    assert stored.status_code == 200, stored.text
    assert package_body(stored)["id"] == package["id"]
    assert child_snapshot(client, matter["id"]) == before_children
    assert matter_snapshot(client, matter["id"]) == before_matter
    assert not any(
        row["action"] == "action.created" for row in scoped_audits(client, matter["id"])
    )
    assert not any(
        row["event_type"] == "action.created" for row in scoped_events(client, matter["id"])
    )
    assert len(scoped_audits(client, matter["id"])) >= len(before_audits)
    assert len(scoped_events(client, matter["id"])) >= len(before_events)


def test_work_package_apply_only_creates_selected_steps_once(client: TestClient) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-package-apply",
        action_statuses=("open",),
    )
    generated = client.post(f"/api/matters/{matter['id']}/work-package/generate")
    assert generated.status_code == 200, generated.text
    generated_package = package_body(generated)
    draft = work_package_draft()
    updated = client.patch(
        f"/api/matters/{matter['id']}/work-package",
        json={"draft": draft, "expected_updated_at": generated_package["updated_at"]},
    )
    assert updated.status_code == 200, updated.text
    updated_package = package_body(updated)
    assert updated_package["draft"]["steps"] == draft["steps"]
    before_audits = scoped_audits(client, matter["id"])
    before_events = scoped_events(client, matter["id"])

    first_apply = client.post(
        f"/api/matters/{matter['id']}/work-package/apply",
        json={
            "step_indexes": [0, 2],
            "expected_updated_at": updated_package["updated_at"],
        },
    )

    assert first_apply.status_code == 200, first_apply.text
    first_detail = client.get(f"/api/matters/{matter['id']}")
    assert first_detail.status_code == 200, first_detail.text
    titles = [item["title"] for item in first_detail.json()["actions"]]
    assert titles.count("行动步骤 0") == 1
    assert titles.count("核对预算") == 1
    assert titles.count("汇报复核结果") == 1
    assert "补充差异说明" not in titles
    assert len(titles) == 3
    assert len(scoped_audits(client, matter["id"])) > len(before_audits)
    assert len(scoped_events(client, matter["id"])) > len(before_events)

    latest = client.get(f"/api/matters/{matter['id']}/work-package")
    assert latest.status_code == 200, latest.text
    second_apply = client.post(
        f"/api/matters/{matter['id']}/work-package/apply",
        json={
            "step_indexes": [0, 2],
            "expected_updated_at": package_body(latest)["updated_at"],
        },
    )

    assert second_apply.status_code == 200, second_apply.text
    repeated = client.get(f"/api/matters/{matter['id']}")
    assert repeated.status_code == 200, repeated.text
    repeated_titles = [item["title"] for item in repeated.json()["actions"]]
    assert repeated_titles.count("核对预算") == 1
    assert repeated_titles.count("汇报复核结果") == 1
    assert "补充差异说明" not in repeated_titles
    assert len(repeated_titles) == 3


def test_stale_expected_updated_at_returns_409_without_partial_write(
    client: TestClient,
) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-stale-close",
        action_statuses=("done",),
        reminder_statuses=("done",),
        review_statuses=("confirmed",),
    )
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "UPDATE matters SET summary = ?, updated_at = ? WHERE id = ?",
            ("另一个客户端已经修改", "2099-01-01T00:00:00Z", matter["id"]),
        )
    before_matter = matter_snapshot(client, matter["id"])
    before_children = child_snapshot(client, matter["id"])
    before_audits = scoped_audits(client, matter["id"])
    before_events = scoped_events(client, matter["id"])

    response = client.post(
        f"/api/matters/{matter['id']}/close",
        json={
            "expected_updated_at": matter["updated_at"],
            "completion_note": "使用过期版本关闭。",
        },
    )

    assert response.status_code == 409, response.text
    assert matter_snapshot(client, matter["id"]) == before_matter
    assert child_snapshot(client, matter["id"]) == before_children
    assert scoped_audits(client, matter["id"]) == before_audits
    assert scoped_events(client, matter["id"]) == before_events


def test_v2_mutations_emit_audit_and_matter_events(client: TestClient) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-events",
        action_statuses=("done",),
        reminder_statuses=("done",),
        review_statuses=("confirmed",),
    )
    response = client.post(
        f"/api/matters/{matter['id']}/close",
        json={
            "expected_updated_at": matter["updated_at"],
            "completion_note": "事件验收关闭说明。",
        },
    )

    assert response.status_code == 200, response.text
    audits = scoped_audits(client, matter["id"])
    events = scoped_events(client, matter["id"])
    close_audits = [row for row in audits if row["action"] == "matter.closed"]
    close_events = [row for row in events if row["event_type"] == "matter.closed"]
    assert len(close_audits) == 1
    assert len(close_events) == 1
    audit = close_audits[0]
    event = close_events[0]
    assert {
        audit["actor"],
        audit["object_type"],
        audit["object_id"],
        audit["matter_id"],
    } == {"财务负责人", "matter", matter["id"]}
    assert event["actor"] == "财务负责人"
    assert event["matter_id"] == matter["id"]
    assert event["object_type"] == "matter"
    assert event["object_id"] == matter["id"]
    assert json.loads(audit["metadata_json"])["completion_note"] == "事件验收关闭说明。"
    assert json.loads(event["payload_json"])["after"] == "completed"


def test_work_package_apply_rolls_back_when_audit_write_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    matter = seed_matter(client, matter_id="matter-package-audit-rollback")
    generated = client.post(f"/api/matters/{matter['id']}/work-package/generate")
    package = package_body(generated)
    updated = client.patch(
        f"/api/matters/{matter['id']}/work-package",
        json={"draft": work_package_draft(), "expected_updated_at": package["updated_at"]},
    )
    updated_package = package_body(updated)

    def fail_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(client.app.state.database, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        client.app.state.service.apply_work_package(
            matter["id"], [0], updated_package["updated_at"], "合成测试"
        )

    assert child_snapshot(client, matter["id"])["actions"] == []
    stored = client.app.state.database.fetch_one(
        "SELECT status FROM work_packages WHERE matter_id = ?", (matter["id"],)
    )
    assert stored == {"status": "draft"}


def test_close_rolls_back_when_audit_write_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    matter = seed_matter(client, matter_id="matter-close-audit-rollback")

    def fail_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(client.app.state.database, "audit", fail_audit)
    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        client.app.state.service.close_matter(
            matter["id"], "合成测试收尾说明", matter["updated_at"], "合成测试"
        )

    stored = client.app.state.database.fetch_one(
        "SELECT status FROM matters WHERE id = ?", (matter["id"],)
    )
    assert stored == {"status": "active"}


def test_action_and_reminder_human_edits_are_audited_and_conflict_safe(
    client: TestClient,
) -> None:
    login(client)
    matter = seed_matter(
        client,
        matter_id="matter-human-edit",
        action_statuses=("open",),
        reminder_statuses=("open",),
    )
    detail = client.get(f"/api/matters/{matter['id']}").json()
    action = detail["actions"][0]
    reminder = detail["reminders"][0]

    action_response = client.patch(
        f"/api/actions/{action['id']}/planning-state",
        json={
            "title": "复核修正后的预算",
            "detail": "以人工核对结果为准。",
            "kind": "waiting",
            "due_date": "2026-09-01",
            "expected_updated_at": action["updated_at"],
            "change_reason": "修正系统提取错误。",
        },
    )
    assert action_response.status_code == 200, action_response.text
    updated_action = action_response.json()
    assert updated_action["title"] == "复核修正后的预算"
    assert updated_action["flow_state"] == "waiting"
    assert updated_action["schedule_basis"] == "user_entered"
    stale_action = client.patch(
        f"/api/actions/{action['id']}/planning-state",
        json={
            "title": "过期修改",
            "expected_updated_at": action["updated_at"],
        },
    )
    assert stale_action.status_code == 409

    reminder_response = client.patch(
        f"/api/reminders/{reminder['id']}",
        json={
            "title": "提醒复核预算",
            "due_at": "2026-09-01T09:00:00Z",
            "expected_updated_at": reminder["updated_at"],
            "change_reason": "人工调整提醒时间。",
        },
    )
    assert reminder_response.status_code == 200, reminder_response.text
    assert reminder_response.json()["title"] == "提醒复核预算"
    audits = scoped_audits(client, matter["id"])
    assert any(row["action"] == "action.planning_updated" for row in audits)
    assert any(row["action"] == "reminder.updated" for row in audits)


def test_fact_correction_and_material_reassignment_preserve_history(
    client: TestClient,
) -> None:
    login(client)
    source = seed_matter(client, matter_id="matter-source")
    target = seed_matter(client, matter_id="matter-target")
    now = utc_timestamp()
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO materials "
            "(id, idempotency_key, sha256, source_type, filename, size, text_note, "
            "status, matter_id, received_at, updated_at) "
            "VALUES ('material-edit', 'material-edit-key', 'sha-edit', 'text', "
            "'合成材料.txt', 0, '', 'assigned', ?, ?, ?)",
            (source["id"], now, now),
        )
        connection.execute(
            "INSERT INTO evidence "
            "(id, matter_id, material_id, claim_type, field_type, value, "
            "source_locator, quote, confidence, status, created_by, created_at) "
            "VALUES ('evidence-edit', ?, 'material-edit', 'fact', '金额', '100', "
            "'合成材料', '原始记录', 1.0, 'confirmed', 'jarvis', ?)",
            (source["id"], now),
        )

    corrected = client.post(
        "/api/evidence/evidence-edit/correct",
        json={
            "value": "200",
            "reason": "人工核对原始凭证后修正。",
            "expected_created_at": now,
        },
    )
    assert corrected.status_code == 200, corrected.text
    corrected_id = corrected.json()["id"]
    assert database.fetch_one(
        "SELECT status FROM evidence WHERE id = 'evidence-edit'"
    )["status"] == "superseded"

    reassigned = client.post(
        "/api/materials/material-edit/reassign",
        json={
            "matter_id": target["id"],
            "reason": "材料归入了错误事项。",
            "expected_updated_at": now,
        },
    )
    assert reassigned.status_code == 200, reassigned.text
    assert database.fetch_one(
        "SELECT matter_id FROM evidence WHERE id = ?", (corrected_id,)
    )["matter_id"] == target["id"]
    assert database.fetch_one(
        "SELECT matter_id FROM materials WHERE id = 'material-edit'"
    )["matter_id"] == target["id"]


def test_material_reassignment_is_monotonic_and_same_target_is_a_noop(
    client: TestClient,
) -> None:
    login(client)
    source = seed_matter(client, matter_id="matter-reassign-source")
    target = seed_matter(client, matter_id="matter-reassign-target")
    now = utc_timestamp()
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO materials "
            "(id, idempotency_key, sha256, source_type, filename, size, text_note, "
            "status, matter_id, received_at, updated_at) "
            "VALUES ('material-reassign', 'material-reassign-key', 'sha-reassign', "
            "'text', '合成材料.txt', 0, '', 'assigned', ?, ?, ?)",
            (source["id"], now, now),
        )

    changed = client.post(
        "/api/materials/material-reassign/reassign",
        json={
            "matter_id": target["id"],
            "reason": "合成测试重新归属。",
            "expected_updated_at": now,
        },
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["changed"] is True
    changed_at = changed.json()["_material_updated_at"]
    assert changed_at > now
    audit_count = database.fetch_one(
        "SELECT COUNT(*) AS count FROM audit_events "
        "WHERE object_type = 'material' AND object_id = 'material-reassign'"
    )["count"]

    unchanged = client.post(
        "/api/materials/material-reassign/reassign",
        json={
            "matter_id": target["id"],
            "reason": "同一事项无需移动。",
            "expected_updated_at": changed_at,
        },
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["changed"] is False
    assert unchanged.json()["_material_updated_at"] == changed_at
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM audit_events "
        "WHERE object_type = 'material' AND object_id = 'material-reassign'"
    )["count"] == audit_count
