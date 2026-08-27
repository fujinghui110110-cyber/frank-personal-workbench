from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    app_env: str = "development"
    password_disabled: bool = False
    owner_passcode: str = "demo"
    session_secret: str = "local-development-secret-change-me"
    owner_token: str = "local-owner-token"
    worker_token: str = "local-worker-token"
    mcp_token: str = "local-mcp-token"
    max_upload_bytes: int = 512 * 1024 * 1024
    lease_seconds: int = 300
    policy_vault_dir: Path = Path(
        "/Users/frank/知识库/04-制度资料/公司最新规定"
    )

    @classmethod
    def from_env(cls) -> "Settings":
        settings = cls(
            data_dir=Path(os.getenv("WORKBENCH_DATA_DIR", "data")).resolve(),
            app_env=os.getenv("WORKBENCH_ENV", "development").strip().lower(),
            password_disabled=os.getenv("WORKBENCH_PASSWORD_DISABLED", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"},
            owner_passcode=os.getenv("WORKBENCH_OWNER_PASSCODE", "demo"),
            session_secret=os.getenv(
                "WORKBENCH_SESSION_SECRET", "local-development-secret-change-me"
            ),
            owner_token=os.getenv("WORKBENCH_OWNER_TOKEN", "local-owner-token"),
            worker_token=os.getenv("WORKBENCH_WORKER_TOKEN", "local-worker-token"),
            mcp_token=os.getenv("WORKBENCH_MCP_TOKEN", "local-mcp-token"),
            max_upload_bytes=int(
                os.getenv("WORKBENCH_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024))
            ),
            lease_seconds=int(os.getenv("WORKBENCH_LEASE_SECONDS", "300")),
            policy_vault_dir=Path(
                os.getenv(
                    "WORKBENCH_POLICY_VAULT_DIR",
                    "/Users/frank/知识库/04-制度资料/公司最新规定",
                )
            ).expanduser(),
        )
        settings.validate()
        return settings

    @property
    def database_path(self) -> Path:
        return self.data_dir / "workbench.sqlite3"

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)

    def validate(self) -> None:
        if self.app_env != "production":
            return
        weak_values = {
            "demo",
            "local-development-secret-change-me",
            "local-owner-token",
            "local-worker-token",
            "local-mcp-token",
        }
        values = {
            self.owner_passcode,
            self.session_secret,
            self.owner_token,
            self.worker_token,
            self.mcp_token,
        }
        if weak_values & values:
            raise RuntimeError("生产环境必须配置独立的口令、会话密钥和 API token")
