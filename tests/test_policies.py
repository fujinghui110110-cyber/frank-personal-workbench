from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import utc_now
from app.main import create_app


@pytest.fixture
def policy_app(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "workbench",
        owner_passcode="owner-pass",
        session_secret="test-session-secret",
        owner_token="owner-token",
        worker_token="worker-token",
        mcp_token="mcp-token",
        email_attachment_staging_dir=tmp_path / "email-attachments",
        policy_vault_dir=tmp_path / "vault" / "公司最新规定",
    )
    return create_app(settings)


@pytest.fixture
def policy_client(policy_app):
    with TestClient(policy_app) as client:
        response = client.post("/api/auth/login", json={"passcode": "owner-pass"})
        assert response.status_code == 200
        yield client


def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-token"}


def candidate(
    source_ref: str,
    *,
    change_type: str = "new",
    matched_policy_id: str | None = None,
    attachments: list[str] | None = None,
    material_id: str | None = None,
    confidence: float = 0.82,
    is_authority: bool = True,
) -> dict:
    payload = {
        "source_type": "wecom",
        "source_ref": source_ref,
        "source_label": "集团经营管理群",
        "result": {
            "classification": "policy",
            "change_type": change_type,
            "title": "采购审批权限规定",
            "publisher": "上级公司",
            "topic": "采购管理",
            "scope": "所属企业采购事项",
            "summary": "采购达到规定金额后应按新的审批层级执行。",
            "requirements": ["达到十万元需报上级公司审批"],
            "change_summary": "审批金额门槛调整",
            "effective_date": "2026-08-21",
            "confidence": confidence,
            "is_authority": is_authority,
            "evidence": ["自本通知发布之日起执行新的采购审批权限。"],
            "attachments": attachments or [],
            "matched_policy_id": matched_policy_id,
        },
    }
    if material_id is not None:
        payload["material_id"] = material_id
    return payload


def authorized_email_material(policy_app, attachment: Path) -> str:
    material_id = "mat_policy_attachment"
    now = utc_now()
    with policy_app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO materials "
            "(id, idempotency_key, sha256, source_type, filename, content_type, size, "
            "status, received_at, updated_at, metadata_json) "
            "VALUES (?, ?, ?, 'email_auto', ?, 'application/pdf', ?, 'processed', ?, ?, ?)",
            (
                material_id,
                "email:policy-attachment",
                hashlib.sha256(attachment.read_bytes()).hexdigest(),
                attachment.name,
                attachment.stat().st_size,
                now,
                now,
                json.dumps({"attachment_paths": [str(attachment)]}),
            ),
        )
    return material_id


def authorized_uploaded_material(policy_app, content: bytes) -> tuple[str, Path]:
    material_id = "mat_uploaded_policy"
    digest = hashlib.sha256(content).hexdigest()
    attachment = policy_app.state.settings.objects_dir / digest[:2] / digest
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(content)
    now = utc_now()
    with policy_app.state.database.connect() as connection:
        connection.execute(
            "INSERT INTO materials "
            "(id, idempotency_key, sha256, source_type, filename, content_type, size, "
            "storage_key, status, received_at, updated_at, metadata_json) "
            "VALUES (?, ?, ?, 'upload', '采购审批规定.pdf', 'application/pdf', ?, ?, "
            "'processed', ?, ?, '{}')",
            (
                material_id,
                "upload:policy-attachment",
                digest,
                len(content),
                str(attachment.relative_to(policy_app.state.settings.data_dir)),
                now,
                now,
            ),
        )
    return material_id, attachment


def test_confirmed_policy_writes_current_history_index_and_attachment(
    policy_client: TestClient, policy_app, tmp_path: Path
) -> None:
    attachment = (
        policy_app.state.settings.email_attachment_staging_dir
        / "message-1"
        / "采购审批规定.pdf"
    )
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"formal-policy")
    material_id = authorized_email_material(policy_app, attachment)
    created = policy_client.post(
        "/api/policy-candidates",
        json=candidate(
            "chat-1", attachments=[str(attachment)], material_id=material_id
        ),
        headers=worker_headers(),
    ).json()
    resolved = policy_client.post(
        f"/api/policy-candidates/{created['id']}/resolve",
        json={"action": "apply"},
    )
    assert resolved.status_code == 200, resolved.text
    policy = policy_client.get("/api/policies?policy_status=active").json()[0]
    current = Path(policy["obsidian_path"])
    assert current.exists()
    assert "采购达到规定金额" in current.read_text(encoding="utf-8")
    vault = policy_app.state.settings.policy_vault_dir
    assert (vault / "00-公司最新规定总览.md").exists()
    assert list((vault / "90-历史版本").rglob("v1-*.md"))
    assert len(list((vault / "附件").rglob("*.pdf"))) == 1
    synced = policy_client.post("/api/policies/obsidian/sync")
    assert synced.status_code == 200, synced.text
    assert len(list((vault / "附件").rglob("*.pdf"))) == 1


def test_missing_authorized_attachment_keeps_candidate_pending_until_retry(
    policy_client: TestClient, policy_app
) -> None:
    attachment = (
        policy_app.state.settings.email_attachment_staging_dir
        / "message-retry"
        / "采购审批规定.pdf"
    )
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"retry-policy")
    material_id = authorized_email_material(policy_app, attachment)
    created = policy_client.post(
        "/api/policy-candidates",
        json=candidate(
            "chat-retry", attachments=[str(attachment)], material_id=material_id
        ),
        headers=worker_headers(),
    ).json()
    attachment.unlink()

    failed = policy_client.post(
        f"/api/policy-candidates/{created['id']}/resolve", json={"action": "apply"}
    )

    assert failed.status_code == 503, failed.text
    assert "待确认" in failed.json()["detail"]
    pending = policy_client.get("/api/policy-candidates?candidate_status=pending").json()
    assert [row["id"] for row in pending] == [created["id"]]
    status = policy_client.get("/api/policies/status").json()
    assert status["obsidian"]["status"] == "failed"
    assert "正式附件" in status["obsidian"]["error"]

    attachment.write_bytes(b"retry-policy")
    retried = policy_client.post(
        f"/api/policy-candidates/{created['id']}/resolve", json={"action": "apply"}
    )

    assert retried.status_code == 200, retried.text
    assert len(list((policy_app.state.settings.policy_vault_dir / "附件").rglob("*.pdf"))) == 1


def test_policy_copies_authorized_uploaded_material_attachment(
    policy_client: TestClient, policy_app
) -> None:
    material_id, attachment = authorized_uploaded_material(policy_app, b"uploaded-policy")
    created = policy_client.post(
        "/api/policy-candidates",
        json=candidate(
            "upload-1", attachments=[str(attachment)], material_id=material_id
        ),
        headers=worker_headers(),
    )

    assert created.status_code == 200, created.text
    assert created.json()["attachments"] == [str(attachment.resolve())]
    resolved = policy_client.post(
        f"/api/policy-candidates/{created.json()['id']}/resolve",
        json={"action": "apply"},
    )
    assert resolved.status_code == 200, resolved.text
    assert len(list((policy_app.state.settings.policy_vault_dir / "附件").rglob("*.pdf"))) == 1


def test_policy_candidate_ignores_unlinked_attachment(
    policy_client: TestClient, policy_app, tmp_path: Path
) -> None:
    outside_attachment = tmp_path / "unrelated.pdf"
    outside_attachment.write_bytes(b"must not copy")

    created = policy_client.post(
        "/api/policy-candidates",
        json=candidate("chat-unlinked", attachments=[str(outside_attachment)]),
        headers=worker_headers(),
    )

    assert created.status_code == 200, created.text
    assert created.json()["attachments"] == []
    resolved = policy_client.post(
        f"/api/policy-candidates/{created.json()['id']}/resolve",
        json={"action": "apply"},
    )
    assert resolved.status_code == 200, resolved.text
    assert list((policy_app.state.settings.policy_vault_dir / "附件").rglob("*.pdf")) == []


def test_revision_and_authoritative_evidence_merge_then_undo(
    policy_client: TestClient,
) -> None:
    first = policy_client.post(
        "/api/policy-candidates",
        json=candidate("chat-1"),
        headers=worker_headers(),
    ).json()
    applied = policy_client.post(
        f"/api/policy-candidates/{first['id']}/resolve", json={"action": "apply"}
    ).json()
    policy_id = applied["matched_policy_id"]

    revision = policy_client.post(
        "/api/policy-candidates",
        json=candidate("mail-2", change_type="revision", matched_policy_id=policy_id),
        headers=worker_headers(),
    ).json()
    policy_client.post(
        f"/api/policy-candidates/{revision['id']}/resolve",
        json={"action": "merge", "policy_id": policy_id},
    )
    assert len(policy_client.get(f"/api/policies/{policy_id}/versions").json()) == 2

    evidence = policy_client.post(
        "/api/policy-candidates",
        json=candidate(
            "chat-3",
            change_type="evidence",
            matched_policy_id=policy_id,
            confidence=0.95,
        ),
        headers=worker_headers(),
    ).json()
    assert evidence["status"] == "auto_applied"
    undone = policy_client.post(
        f"/api/policy-candidates/{evidence['id']}/resolve", json={"action": "undo"}
    )
    assert undone.status_code == 200
    assert undone.json()["status"] == "undone"
    assert len(policy_client.get(f"/api/policies/{policy_id}/versions").json()) == 2


def test_non_policy_is_not_stored(policy_client: TestClient) -> None:
    payload = candidate("chat-social")
    payload["result"] = {"classification": "not_policy"}
    response = policy_client.post(
        "/api/policy-candidates", json=payload, headers=worker_headers()
    )
    assert response.status_code == 200
    assert response.json() == {"stored": False, "classification": "not_policy"}
    assert policy_client.get("/api/policy-candidates?candidate_status=all").json() == []


@pytest.mark.parametrize(
    ("change_type", "policy_status"),
    (("revision", "active"), ("interpretation", "active"), ("repeal", "retired")),
)
def test_policy_change_can_be_collected_without_existing_policy(
    policy_client: TestClient, change_type: str, policy_status: str
) -> None:
    created = policy_client.post(
        "/api/policy-candidates",
        json=candidate(f"chat-{change_type}", change_type=change_type),
        headers=worker_headers(),
    ).json()

    resolved = policy_client.post(
        f"/api/policy-candidates/{created['id']}/resolve",
        json={"action": "apply"},
    )

    assert resolved.status_code == 200, resolved.text
    policy_id = resolved.json()["matched_policy_id"]
    policies = policy_client.get(f"/api/policies?policy_status={policy_status}").json()
    assert [item["id"] for item in policies] == [policy_id]
    assert policy_client.get(f"/api/policies/{policy_id}/versions").json()[0]["change_type"] == change_type


def test_empty_vault_sync_creates_overview(policy_client: TestClient, policy_app) -> None:
    response = policy_client.post("/api/policies/obsidian/sync")
    assert response.status_code == 200
    overview = policy_app.state.settings.policy_vault_dir / "00-公司最新规定总览.md"
    assert overview.exists()
    assert "当前有效规定" in overview.read_text(encoding="utf-8")


def test_identity_hint_normalizes_source_key_and_preserves_authority(
    policy_client: TestClient,
) -> None:
    payload = candidate("chat-identity", is_authority=False)
    payload["source_label"] = "  Group   A  "
    created = policy_client.post(
        "/api/policy-candidates", json=payload, headers=worker_headers()
    ).json()
    resolved = policy_client.post(
        f"/api/policy-candidates/{created['id']}/resolve",
        json={"action": "apply"},
    )
    assert resolved.status_code == 200, resolved.text
    hint = policy_client.get(
        "/api/policies/identity/hint",
        params={"source_type": "wecom", "source_key": "Group A"},
    ).json()
    assert hint["publisher"] == "上级公司"
    assert hint["is_authority"] == 0


def test_failed_obsidian_write_keeps_candidate_pending(
    policy_client: TestClient, policy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = policy_client.post(
        "/api/policy-candidates",
        json=candidate("chat-failure"),
        headers=worker_headers(),
    ).json()

    def fail_write(_operations):
        raise OSError("vault unavailable")

    monkeypatch.setattr(policy_app.state.policies, "_write_batch", fail_write)
    response = policy_client.post(
        f"/api/policy-candidates/{created['id']}/resolve", json={"action": "apply"}
    )
    assert response.status_code == 503
    rows = policy_client.get("/api/policy-candidates?candidate_status=pending").json()
    assert [row["id"] for row in rows] == [created["id"]]
    status = policy_client.get("/api/policies/status").json()
    assert status["obsidian"]["status"] == "failed"
    assert "vault unavailable" in status["obsidian"]["error"]
