from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
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
    confidence: float = 0.82,
    is_authority: bool = True,
) -> dict:
    return {
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


def test_confirmed_policy_writes_current_history_index_and_attachment(
    policy_client: TestClient, policy_app, tmp_path: Path
) -> None:
    attachment = tmp_path / "采购审批规定.pdf"
    attachment.write_bytes(b"formal-policy")
    created = policy_client.post(
        "/api/policy-candidates",
        json=candidate("chat-1", attachments=[str(attachment)]),
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
    assert list((vault / "附件").rglob("*.pdf"))


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
    assert evidence["status"] == "pending"
    resolved_evidence = policy_client.post(
        f"/api/policy-candidates/{evidence['id']}/resolve",
        json={"action": "merge", "policy_id": policy_id},
    )
    assert resolved_evidence.status_code == 200, resolved_evidence.text
    assert resolved_evidence.json()["status"] == "applied"


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
