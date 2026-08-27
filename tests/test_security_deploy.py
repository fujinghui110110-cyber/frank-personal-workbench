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
        password_disabled=True,
        owner_passcode="owner-pass",
        session_secret="test-session-secret",
        owner_token="owner-token",
        worker_token="worker-token",
        mcp_token="mcp-token",
        max_upload_bytes=4096,
        lease_seconds=1,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_passwordless_browser_state_change_rejects_cross_site_origin(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": "跨站请求"},
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403


def test_passwordless_browser_state_change_rejects_cross_site_fetch(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": "跨站请求"},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/api/auth/login", "/api/auth/logout"])
def test_auth_state_changes_also_reject_cross_site_origin(
    client: TestClient, path: str
) -> None:
    response = client.post(
        path,
        json={"passcode": "owner-pass"} if path.endswith("login") else None,
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 403


def test_passwordless_localhost_state_change_remains_available(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": "本机请求"},
        headers={"Origin": "http://localhost:8000"},
    )

    assert response.status_code == 201, response.text


@pytest.mark.parametrize("token", ["worker-token", "mcp-token"])
def test_bearer_workers_keep_cross_site_api_access(
    client: TestClient, token: str
) -> None:
    response = client.post(
        "/api/nodes/heartbeat",
        json={"node_id": "security-test", "name": "security-test", "metadata": {}},
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 200, response.text


def test_deploy_scripts_secure_and_retire_legacy_schedule() -> None:
    root = Path(__file__).resolve().parents[1]
    worker_script = (root / "deploy/install-mac-worker.sh").read_text(encoding="utf-8")
    legacy_script = (root / "deploy/install-wechat-sync.sh").read_text(encoding="utf-8")

    assert "umask 077" in worker_script
    assert 'chmod 600 "$target"' in worker_script
    assert (
        'legacy_sync_target="$HOME/Library/LaunchAgents/com.finance-workbench.wechat-sync.plist"'
        in worker_script
    )
    assert 'launchctl bootout "gui/$(id -u)/$legacy_sync_label"' in worker_script
    assert 'rm -f "$legacy_sync_target"' in worker_script
    assert 'launchctl bootout "gui/$(id -u)/$label"' in legacy_script
    assert 'rm -f "$target"' in legacy_script
    assert "WORKBENCH_WORKER_TOKEN" not in legacy_script
    assert "PlistBuddy" not in legacy_script
    assert "bootstrap" not in legacy_script
