from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from app.client import WorkbenchClient


mcp = FastMCP("财务负责人 AI 工作台")


def client() -> WorkbenchClient:
    return WorkbenchClient(
        os.getenv("WORKBENCH_BASE_URL", "http://127.0.0.1:8000"),
        os.getenv("WORKBENCH_MCP_TOKEN", "local-mcp-token"),
    )


def allowed_intake_file(file_path: str) -> Path:
    requested = Path(file_path).expanduser().resolve()
    configured = os.getenv("WORKBENCH_ALLOWED_INTAKE_DIRS", "").strip()
    roots = (
        [Path(item).expanduser().resolve() for item in configured.split(os.pathsep) if item]
        if configured
        else [Path.home() / "财务工作台投递箱", Path.home() / ".workbuddy" / "blobs"]
    )
    if not requested.is_file() or not any(requested.is_relative_to(root) for root in roots):
        raise ValueError("这个文件不在 贾维斯 附件目录或财务工作台投递箱中")
    return requested


@mcp.tool()
def intake_text(text: str, source_type: str = "text", matter_id: str | None = None) -> dict[str, Any]:
    return client().intake(source_type, text_note=text, matter_id=matter_id)


@mcp.tool()
def list_pending_materials() -> list[dict[str, Any]]:
    return client().request_json("GET", "/api/materials?material_status=queued")


@mcp.tool()
def list_matters() -> list[dict[str, Any]]:
    return client().request_json("GET", "/api/matters")


@mcp.tool()
def get_matter(matter_id: str) -> dict[str, Any]:
    return client().request_json("GET", f"/api/matters/{matter_id}")


@mcp.tool()
def fetch_material(material_id: str) -> dict[str, Any]:
    api = client()
    material = api.request_json("GET", f"/api/materials/{material_id}")
    cache_dir = Path(os.getenv("WORKBENCH_MCP_CACHE_DIR", "data/mcp-cache")).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(material.get("filename") or f"{material_id}.txt").name
    target = cache_dir / f"{material_id}-{filename}"
    target.write_bytes(api.get_bytes(f"/api/materials/{material_id}/content"))
    return {"material": material, "local_path": str(target)}


@mcp.tool()
def ask_assistant_to_analyze(material_id: str) -> dict[str, Any]:
    return client().request_json("POST", f"/api/materials/{material_id}/assistant")


@mcp.tool()
def claim_job(worker_id: str = "jarvis") -> dict[str, Any] | None:
    return client().request_json("POST", "/api/jobs/claim", {"worker_id": worker_id})["job"]


@mcp.tool()
def start_job(job_id: str, worker_id: str, lease_token: str) -> dict[str, Any]:
    return client().request_json(
        "POST",
        f"/api/jobs/{job_id}/start",
        {"worker_id": worker_id, "lease_token": lease_token},
    )


@mcp.tool()
def complete_job(
    job_id: str,
    worker_id: str,
    lease_token: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    return client().request_json(
        "POST",
        f"/api/jobs/{job_id}/complete",
        {"worker_id": worker_id, "lease_token": lease_token, "result": result},
    )


@mcp.tool()
def fail_job(job_id: str, worker_id: str, lease_token: str, error: str) -> dict[str, Any]:
    return client().request_json(
        "POST",
        f"/api/jobs/{job_id}/fail",
        {"worker_id": worker_id, "lease_token": lease_token, "error": error},
    )


@mcp.tool()
def morning_brief() -> dict[str, Any]:
    return client().request_json("GET", "/api/overview")


@mcp.tool()
def resolve_reminder(reminder_id: str, status: str = "done") -> dict[str, Any]:
    return client().request_json(
        "POST", f"/api/reminders/{reminder_id}/resolve", {"status": status}
    )


@mcp.tool()
def intake_file(
    file_path: str,
    text_note: str = "",
    source_type: str = "file",
    matter_id: str | None = None,
) -> dict[str, Any]:
    return client().intake(
        source_type,
        text_note=text_note,
        file_path=allowed_intake_file(file_path),
        matter_id=matter_id,
    )


@mcp.tool()
def intake_channel_material(
    channel: str,
    text: str = "",
    file_path: str | None = None,
    external_message_id: str | None = None,
    sender: str | None = None,
    sent_at: str | None = None,
    file_type: str | None = None,
    matter_id: str | None = None,
) -> dict[str, Any]:
    api = client()
    if file_path:
        material_type = file_type if file_type in {"image", "audio", "video", "file"} else "file"
        result = api.intake(
            material_type,
            text_note=text,
            file_path=allowed_intake_file(file_path),
            matter_id=matter_id,
            idempotency_key=f"{channel}:{external_message_id}" if external_message_id else None,
        )
        api.request_json(
            "POST",
            "/api/channel-intakes/register",
            {
                "material_id": result["material"]["id"],
                "channel": channel,
                "external_message_id": external_message_id,
                "sender": sender,
                "sent_at": sent_at,
                "file_type": file_type,
            },
        )
        return result
    return api.request_json(
        "POST",
        "/api/channel-intake",
        {
            "text": text,
            "channel": channel,
            "external_message_id": external_message_id,
            "sender": sender,
            "sent_at": sent_at,
            "file_type": file_type,
            "matter_id": matter_id,
        },
    )


@mcp.tool()
def undo_last_intake(channel: str = "assistant") -> dict[str, Any]:
    return client().request_json(
        "POST", f"/api/channel-intakes/undo-last?channel={channel}"
    )


@mcp.tool()
def list_wechat_candidates(status: str = "pending") -> list[dict[str, Any]]:
    return client().request_json(
        "GET", f"/api/wechat/candidates?candidate_status={status}"
    )


@mcp.tool()
def resolve_wechat_candidate(
    candidate_id: str,
    action: str,
    matter_id: str | None = None,
) -> dict[str, Any]:
    return client().request_json(
        "POST",
        f"/api/wechat/candidates/{candidate_id}/resolve",
        {"action": action, "matter_id": matter_id},
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
