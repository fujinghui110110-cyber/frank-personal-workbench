from __future__ import annotations

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


def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-token"}


def test_timeline_limit_returns_latest_events_in_chronological_order(
    client: TestClient,
) -> None:
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO matters (id, title, created_at, updated_at) VALUES ('m1', '测试事项', '2026-01-01', '2026-01-01')"
        )
        for index in range(1, 4):
            connection.execute(
                "INSERT INTO matter_events "
                "(id, matter_id, event_type, actor, summary, created_at) "
                "VALUES (?, 'm1', 'progress.note', 'Frank', ?, ?)",
                (f"e{index}", f"进展{index}", f"2026-01-0{index}T00:00:00Z"),
            )

    response = client.get("/api/matters/m1/timeline", params={"limit": 2})
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()] == ["e2", "e3"]


def test_related_information_returns_business_excerpt_and_today_brief(
    client: TestClient,
) -> None:
    database = client.app.state.database
    long_summary = "需要继续核对供应商付款资料。" * 24 + "末尾原文不应整段返回"
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO matters (id, title, created_at, updated_at) "
            "VALUES ('matter-related', '供应商付款复核', '2026-09-09', '2026-09-09')"
        )
        connection.execute(
            "INSERT INTO actions "
            "(id, matter_id, kind, title, status, owner, due_date, created_by, created_at, updated_at) "
            "VALUES ('action-related', 'matter-related', 'task', '复核付款资料', "
            "'open', 'Frank', '2026-09-09', 'Frank', '2026-09-09', '2026-09-09')"
        )
        connection.execute(
            "INSERT INTO wechat_conversations "
            "(session_id, source, display_name, kind, last_message_at, created_at, updated_at) "
            "VALUES ('supplier-session', 'personal_wechat', '供应商王经理', 'private', "
            "'2026-09-09T09:30:00Z', '2026-09-09', '2026-09-09')"
        )
        connection.execute(
            "INSERT INTO materials "
            "(id, idempotency_key, sha256, source_type, filename, content_type, size, "
            "text_note, status, matter_id, received_at, updated_at) "
            "VALUES ('material-related', 'related-key', 'related-sha', 'wechat_auto', "
            "'聊天记录', 'text/plain', 1, '', 'processed', 'matter-related', "
            "'2026-09-09T09:30:00Z', '2026-09-09T09:30:00Z')"
        )
        connection.execute(
            "INSERT INTO wechat_candidates "
            "(id, material_id, session_id, source, window_start, window_end, classification, "
            "summary, confidence, status, evidence_json, extracted_json, matter_id, created_at, updated_at, resolved_at) "
            "VALUES ('candidate-related', 'material-related', 'supplier-session', 'personal_wechat', "
            "'2026-09-09T09:00:00Z', '2026-09-09T09:30:00Z', 'work', ?, 0.95, "
            "'accepted', '[]', '{}', 'matter-related', '2026-09-09', '2026-09-09', '2026-09-09')",
            (long_summary,),
        )

    response = client.get("/api/matters/matter-related/related-information")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"] == {"personal_wechat": 1}
    assert body["items"][0]["display_name"] == "供应商王经理"
    assert body["items"][0]["source_label"] == "个人微信"
    assert body["items"][0]["occurred_at"] == "2026-09-09T09:30:00Z"
    assert len(body["items"][0]["summary"]) == 180
    assert body["items"][0]["summary"].endswith("…")
    assert "末尾原文不应整段返回" not in body["items"][0]["summary"]

    brief = client.get("/api/today/brief").json()
    assert brief["now"]["matter_id"] == "matter-related"
    assert brief["related_information"] == body


def test_source_summary_and_brief_use_business_reading_status(client: TestClient) -> None:
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO wechat_sync_requests "
            "(id, source, status, requested_by, requested_at, completed_at, message_count, window_count) "
            "VALUES ('w1', 'personal_wechat', 'completed', 'Frank', '2026-09-08T08:00:00Z', "
            "'2026-09-08T08:01:00Z', 8, 2)"
        )
        connection.execute(
            "INSERT INTO wechat_sync_requests "
            "(id, source, status, requested_by, requested_at, error) "
            "VALUES ('w2', 'wecom', 'failed', 'Frank', '2026-09-08T08:00:00Z', '读取失败')"
        )

    summary = client.get("/api/source-summary")
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["summary"] == "可能有未读取信息"
    assert body["possible_missing"] is True
    assert {item["label"] for item in body["sources"]} == {"个人微信", "企业微信", "邮箱"}
    assert next(item for item in body["sources"] if item["key"] == "personal_wechat")[
        "new_items"
    ] == 2
    assert client.get("/api/today/brief").json()["source_summary"] == body


def test_external_contacts_and_commitments_do_not_use_people_table(
    client: TestClient,
) -> None:
    identified = client.post(
        "/api/contacts/identify",
        headers=worker_headers(),
        json={
            "source": "wecom",
            "stable_id": "external-supplier-1",
            "display_name": "供应商王经理",
            "organization": "测试供应商",
            "role": "业务联系人",
        },
    )
    assert identified.status_code == 200, identified.text
    contact = identified.json()
    recorded = client.post(
        "/api/relationships/commitments",
        headers=worker_headers(),
        json={
            "source": "wecom",
            "source_ref": "message-1",
            "category": "their_commitment",
            "summary": "王经理承诺明天补齐报价单",
            "contact_id": contact["id"],
            "due_at": "2026-09-09T10:00:00Z",
            "evidence": ["明天把报价单发你"],
        },
    )
    assert recorded.status_code == 200, recorded.text
    view = client.get("/api/relationships/commitments").json()
    assert view["counts"]["their_commitment"] == 1
    assert view["groups"]["their_commitment"][0]["contact_name"] == "供应商王经理"
    database = client.app.state.database
    assert database.fetch_one(
        "SELECT COUNT(*) AS total FROM people WHERE display_name = '供应商王经理'"
    )["total"] == 0
    closed = client.patch(
        f"/api/relationships/commitments/{recorded.json()['id']}",
        json={"status": "done"},
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "done"
    assert client.get("/api/relationships/commitments").json()["total"] == 0


def test_search_expands_person_aliases_and_business_synonyms(client: TestClient) -> None:
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO matters (id, title, summary, created_at, updated_at) "
            "VALUES ('m-search', '李静复核供应商付款', '等待支付资料', '2026-09-08', '2026-09-08')"
        )

    alias_result = client.get("/api/search", params={"q": "静姐"}).json()
    assert any(item["entity_id"] == "m-search" for item in alias_result["items"])
    assert "李静" in alias_result["matched_terms"]
    synonym_result = client.get("/api/search", params={"q": "打款"}).json()
    assert any(item["entity_id"] == "m-search" for item in synonym_result["items"])


def test_exact_open_title_continues_existing_matter(client: TestClient) -> None:
    client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": "第一份材料"},
        headers={"Idempotency-Key": "first"},
    )
    assert client.post("/api/analysis/run").status_code == 200
    claimed = client.post(
        "/api/jobs/claim", headers=worker_headers(), json={"worker_id": "mac"}
    ).json()["job"]
    started = client.post(
        f"/api/jobs/{claimed['id']}/start",
        headers=worker_headers(),
        json={"worker_id": "mac", "lease_token": claimed["lease_token"]},
    ).json()
    result = {
        "worker_id": "mac",
        "lease_token": started["lease_token"],
        "result": {
            "matter_title": "供应商合同复核",
            "facts": [],
            "actions": [{"kind": "task", "title": "继续核对合同"}],
        },
    }
    matter = client.post(
        f"/api/jobs/{claimed['id']}/complete", headers=worker_headers(), json=result
    ).json()

    client.post(
        "/api/intake",
        data={"source_type": "email_auto", "text_note": "后续邮件"},
        headers={"Idempotency-Key": "second"},
    )
    assert client.post("/api/analysis/run").status_code == 200
    claimed2 = client.post(
        "/api/jobs/claim", headers=worker_headers(), json={"worker_id": "mac"}
    ).json()["job"]
    started2 = client.post(
        f"/api/jobs/{claimed2['id']}/start",
        headers=worker_headers(),
        json={"worker_id": "mac", "lease_token": claimed2["lease_token"]},
    ).json()
    result2 = {
        "worker_id": "mac",
        "lease_token": started2["lease_token"],
        "result": {"matter_title": "供应商合同复核", "facts": [], "actions": []},
    }
    continued = client.post(
        f"/api/jobs/{claimed2['id']}/complete", headers=worker_headers(), json=result2
    )
    assert continued.status_code == 200, continued.text
    assert "id" in continued.json(), continued.json()
    assert continued.json()["id"] == matter["id"]
    assert client.app.state.database.fetch_one(
        "SELECT COUNT(*) AS total FROM matters"
    )["total"] == 1


def test_quality_metrics_are_derived_from_recorded_activity(client: TestClient) -> None:
    database = client.app.state.database
    database.audit("a1", "Frank", "wechat.candidate.accepted", "candidate", "c1")
    database.audit("a2", "Frank", "wechat.candidate.ignored", "candidate", "c2")
    database.audit("a3", "Frank", "wechat.candidate.restored", "candidate", "c2")
    database.audit("a4", "贾维斯", "wechat.candidate.merged", "candidate", "c3")

    response = client.get("/api/quality/metrics", params={"days": 30})
    assert response.status_code == 200, response.text
    metrics = {item["key"]: item for item in response.json()["metrics"]}
    assert metrics["useful"]["value"] == 50.0
    assert metrics["restored"]["value"] == 1
    assert metrics["merged"]["value"] == 1
    assert all(item["label"] for item in metrics.values())


def test_quality_metrics_counts_iso_utc_pending_older_than_seven_days(client: TestClient) -> None:
    database = client.app.state.database
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO review_items (id, kind, title, status, payload_json, created_at) "
            "VALUES (?, ?, ?, 'pending', '{}', ?)",
            ("old-review", "business", "旧待确认", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO review_items (id, kind, title, status, payload_json, created_at) "
            "VALUES (?, ?, ?, 'pending', '{}', ?)",
            ("new-review", "business", "新待确认", "2999-01-01T00:00:00Z"),
        )

    metrics = {
        item["key"]: item
        for item in client.get("/api/quality/metrics", params={"days": 30}).json()["metrics"]
    }
    assert metrics["old_pending"]["value"] == 1
