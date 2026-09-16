from __future__ import annotations

import sqlite3
from pathlib import Path

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
        max_upload_bytes=4096,
        lease_seconds=30,
    )
    with TestClient(create_app(settings)) as test_client:
        assert test_client.post(
            "/api/auth/login", json={"passcode": "owner-pass"}
        ).status_code == 200
        yield test_client


def seed_search_records(client: TestClient) -> None:
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO matters (id,title,target_date,created_at,updated_at) "
            "VALUES ('matter-insurance','保险资料收集','2026-09-30','2026-09-01','2026-09-01')"
        )
        connection.execute(
            "INSERT INTO materials (id,idempotency_key,sha256,source_type,size,text_note,status,matter_id,received_at,updated_at) "
            "VALUES ('material-chat','key-chat','sha-chat','wechat_auto',1,'保险资料','processed',"
            "'matter-insurance','2026-09-02','2026-09-02')"
        )
        connection.execute(
            "INSERT INTO actions (id,matter_id,material_id,kind,title,status,created_by,created_at,updated_at) "
            "VALUES ('action-insurance','matter-insurance','material-chat','task','采购保险资料目前待补齐',"
            "'open','Frank','2026-09-02','2026-09-02')"
        )
        connection.execute(
            "INSERT INTO people (id,display_name,created_at,updated_at) "
            "VALUES ('person-owner','已确认负责人','2026-09-02','2026-09-02')"
        )
        connection.execute(
            "INSERT INTO action_assignees (id,action_id,person_id,status,confirmed_by,confirmed_at,created_at,updated_at) "
            "VALUES ('aa-insurance','action-insurance','person-owner','confirmed','Frank','2026-09-02',"
            "'2026-09-02','2026-09-02')"
        )


def test_progress_and_review_date_are_atomic_and_do_not_change_target_date(
    client: TestClient,
) -> None:
    client.app.state.database.initialize()
    columns = client.app.state.database.fetch_all("PRAGMA table_info(matters)")
    assert [row["name"] for row in columns].count("next_review_date") == 1
    seed_search_records(client)
    response = client.post(
        "/api/matters/matter-insurance/progress",
        json={
            "summary": "已催收缺少的身份证明",
            "detail": "等待对方补充",
            "next_review_date": "2026-09-18",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["payload"]["next_review_date"] == "2026-09-18"
    matter = client.get("/api/matters/matter-insurance").json()
    assert matter["target_date"] == "2026-09-30"
    assert matter["next_review_date"] == "2026-09-18"
    database = client.app.state.database
    assert database.fetch_one(
        "SELECT id FROM audit_events WHERE action='matter.progress.recorded' AND object_id=?",
        ("matter-insurance",),
    )

    with database.connect() as connection:
        connection.execute(
            "CREATE TRIGGER fail_progress_audit BEFORE INSERT ON audit_events "
            "WHEN NEW.action='matter.progress.recorded' BEGIN SELECT RAISE(ABORT,'audit failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        client.app.state.service.add_matter_progress(
            "matter-insurance",
            "这次事务必须回滚",
            "",
            "Frank",
            next_review_date="2026-09-19",
            update_next_review_date=True,
        )
    assert client.app.state.database.fetch_one(
        "SELECT next_review_date FROM matters WHERE id='matter-insurance'"
    )["next_review_date"] == "2026-09-18"
    assert not client.app.state.database.fetch_one(
        "SELECT id FROM matter_events WHERE summary='这次事务必须回滚'"
    )
    with database.connect() as connection:
        connection.execute("DROP TRIGGER fail_progress_audit")
    unchanged = client.post(
        "/api/matters/matter-insurance/progress",
        json={"summary": "旧客户端只记录进展", "detail": ""},
    )
    assert unchanged.status_code == 200, unchanged.text
    assert client.get("/api/matters/matter-insurance").json()["next_review_date"] == "2026-09-18"


def test_explicit_action_feedback_creates_toggleable_consumed_rules(client: TestClient) -> None:
    seed_search_records(client)
    changed = client.patch(
        "/api/actions/action-insurance/planning-state",
        json={"pinned": True, "snoozed_until": "2026-09-20T09:00:00Z"},
    )
    assert changed.status_code == 200, changed.text
    rules = client.get("/api/learning-rules").json()
    feedback = {item["rule_type"]: item for item in rules if item["rule_type"].startswith("action_")}
    assert set(feedback) == {"action_pin", "action_defer"}
    assert feedback["action_pin"]["source"]["type"] == "action.planning_updated"
    assert feedback["action_pin"]["scope"] == "exact_action"

    disabled = client.patch(
        f"/api/learning-rules/{feedback['action_pin']['id']}", json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    assert client.app.state.database.fetch_one(
        "SELECT pinned_at FROM actions WHERE id='action-insurance'"
    )["pinned_at"] is None
    ordinary = client.patch(
        "/api/actions/action-insurance/planning-state", json={"estimated_minutes": 20}
    )
    assert ordinary.status_code == 200, ordinary.text
    still_disabled = client.get("/api/learning-rules").json()
    assert next(item for item in still_disabled if item["id"] == feedback["action_pin"]["id"])[
        "enabled"
    ] is False
    restored = client.patch(
        f"/api/learning-rules/{feedback['action_pin']['id']}", json={"enabled": True}
    )
    assert restored.status_code == 200, restored.text
    assert client.app.state.database.fetch_one(
        "SELECT pinned_at FROM actions WHERE id='action-insurance'"
    )["pinned_at"] is not None
    defer_rule = feedback["action_defer"]
    with client.app.state.database.connect() as connection:
        connection.execute(
            "UPDATE actions SET snoozed_until='2026-09-25T09:00:00Z' "
            "WHERE id='action-insurance'"
        )
    assert client.patch(
        f"/api/learning-rules/{defer_rule['id']}", json={"enabled": False}
    ).status_code == 200
    assert client.app.state.database.fetch_one(
        "SELECT snoozed_until FROM actions WHERE id='action-insurance'"
    )["snoozed_until"] == "2026-09-25T09:00:00Z"

    with client.app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO wechat_conversations "
            "(session_id,source,display_name,kind,listen_status,blocked_at,created_at,updated_at) "
            "VALUES ('blocked-session','personal_wechat','非工作群','group','blocked','2026-09-01',"
            "'2026-09-01','2026-09-01')"
        )
    conversation_rule = next(
        item for item in client.get("/api/learning-rules").json()
        if item["rule_type"] == "conversation_ignore"
        and item["pattern"]["session_id"] == "blocked-session"
    )
    assert client.patch(
        f"/api/learning-rules/{conversation_rule['id']}", json={"enabled": False}
    ).status_code == 200
    assert client.app.state.database.fetch_one(
        "SELECT listen_status FROM wechat_conversations WHERE session_id='blocked-session'"
    )["listen_status"] == "active"

    with client.app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO materials (id,idempotency_key,sha256,source_type,size,text_note,status,received_at,updated_at) "
            "VALUES ('material-merge','key-merge','sha-merge','text',1,'保险资料补件','pending_review',"
            "'2026-09-03','2026-09-03')"
        )
    client.app.state.service.assign_material(
        "material-merge", "Frank", matter_id="matter-insurance"
    )
    merge_rule = next(
        item for item in client.get("/api/learning-rules").json()
        if item["rule_type"] == "topic_merge"
    )
    assert merge_rule["source"]["ref"] == "material-merge"
    assert merge_rule["pattern"]["matter_id"] == "matter-insurance"
    received = client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": "保险资料补件"},
        headers={"Idempotency-Key": "learned-merge-consumer"},
    )
    assert received.status_code == 201, received.text
    assert client.post("/api/analysis/run").status_code == 200
    worker_headers = {"Authorization": "Bearer worker-token"}
    claimed = client.post(
        "/api/jobs/claim", json={"worker_id": "rule-worker"}, headers=worker_headers
    ).json()["job"]
    lease = {"worker_id": "rule-worker", "lease_token": claimed["lease_token"]}
    assert client.post(
        f"/api/jobs/{claimed['id']}/start", json=lease, headers=worker_headers
    ).status_code == 200
    consumed = client.post(
        f"/api/jobs/{claimed['id']}/complete",
        json={
            **lease,
            "result": {
                "matter_title": "保险资料补件",
                "summary": "补件内容已到达",
                "facts": [],
                "inferences": [],
                "actions": [],
            },
        },
        headers=worker_headers,
    )
    assert consumed.status_code == 200, consumed.text
    assert consumed.json()["id"] == "matter-insurance"


def test_natural_search_filters_before_limit_and_returns_precise_source(client: TestClient) -> None:
    seed_search_records(client)
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO actions (id,matter_id,material_id,kind,title,status,created_by,created_at,updated_at) "
            "VALUES ('action-other','matter-insurance','material-chat','task','采购保险他人行动',"
            "'open','Frank','2026-09-02','2026-09-02')"
        )
        connection.execute(
            "INSERT INTO actions (id,matter_id,material_id,kind,title,status,created_by,created_at,updated_at) "
            "VALUES ('action-unassigned','matter-insurance','material-chat','task','采购保险未分配行动',"
            "'open','Frank','2026-09-02','2026-09-02')"
        )
        connection.execute(
            "INSERT INTO people (id,display_name,created_at,updated_at) "
            "VALUES ('person-other','其他负责人','2026-09-02','2026-09-02')"
        )
        connection.execute(
            "INSERT INTO action_assignees (id,action_id,person_id,status,confirmed_by,confirmed_at,created_at,updated_at) "
            "VALUES ('aa-other','action-other','person-other','confirmed','Frank','2026-09-02',"
            "'2026-09-02','2026-09-02')"
        )
        for index in range(205):
            connection.execute(
                "INSERT INTO company_policies "
                "(id,canonical_key,title,publisher,topic,scope,summary,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'active',?,?)",
                (
                    f"policy-{index}",
                    f"policy-key-{index}",
                    f"保险规定{index}",
                    "公司",
                    "保险",
                    "公司",
                    "保险",
                    "2026-09-10",
                    "2026-09-10",
                ),
            )
    result = client.get(
        "/api/search",
        params={
            "q": "保险目前进展到哪里",
            "limit": 1,
            "business_type": "action",
            "channel": "personal_wechat",
            "person_id": "person-owner",
        },
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["interpreted_query"] == "保险"
    assert body["intent"] == "progress"
    assert [item["entity_id"] for item in body["items"]] == ["action-insurance"]
    item = body["items"][0]
    assert item["source_href"].endswith(
        "?source_type=action&source_id=action-insurance"
    )
    assert body["answer"]["sources"][0]["href"] == item["source_href"]
    detail = client.get("/api/search/sources/action/action-insurance")
    assert detail.status_code == 200, detail.text
    assert detail.json()["record"]["id"] == "action-insurance"

    exact_person = client.get(
        "/api/search",
        params={
            "q": "采购",
            "business_type": "action",
            "person_id": "person-owner",
        },
    ).json()
    assert [item["entity_id"] for item in exact_person["items"]] == ["action-insurance"]

    policy = client.get(
        "/api/search", params={"q": "保险规定204", "business_type": "policy", "limit": 1}
    ).json()["items"][0]
    assert policy["source_href"] == "#/policies/policy-204"
