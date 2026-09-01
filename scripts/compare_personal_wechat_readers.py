from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.client import WorkbenchClient
from scripts.ciphertalk_client import open_ciphertalk
from scripts.personal_wechat_crypto import decrypted_dataset, discover_dataset
from scripts.personal_wechat_keys import load_key
from scripts.personal_wechat_sync import read_message_metadata
from scripts.wechat_sync import _epoch_ms, _messages, _sessions


def _kind_group(value: Any) -> str:
    kind = str(value or "0")
    if kind.startswith("app_") or kind in {"file", "link", "quote"}:
        return "app"
    return kind


def message_signatures(items: list[dict[str, Any]]) -> set[str]:
    def identity(item: dict[str, Any]) -> str:
        if item.get("local_id") is None or item.get("sort_seq") is None:
            return str(item.get("id"))
        return f"{item.get('sort_seq')}|{item.get('local_id')}"

    return {
        hashlib.sha256(
            (
                f"{item.get('session_id')}|{item.get('sort_seq')}|"
                f"{item.get('timestamp_ms')}|{identity(item)}|"
                f"{_kind_group(item.get('kind'))}"
            ).encode()
        ).hexdigest()
        for item in items
    }


def comparison_summary(
    direct: list[dict[str, Any]], legacy: list[dict[str, Any]]
) -> dict[str, Any]:
    direct_signatures = message_signatures(direct)
    legacy_signatures = message_signatures(legacy)
    return {
        "direct_count": len(direct_signatures),
        "legacy_count": len(legacy_signatures),
        "direct_only": len(direct_signatures - legacy_signatures),
        "legacy_only": len(legacy_signatures - direct_signatures),
        "consistent": direct_signatures == legacy_signatures,
    }


def update_window_state(
    state: dict[str, Any],
    direct: list[dict[str, Any]],
    legacy: list[dict[str, Any]],
    *,
    checked_at: str,
    required_windows: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    direct_signatures = message_signatures(direct)
    legacy_signatures = message_signatures(legacy)
    if "seen_direct" not in state or "seen_legacy" not in state:
        state = {
            "version": 1,
            "started_at": checked_at,
            "required_windows": required_windows,
            "passed_windows": 0,
            "failed_windows": 0,
            "seen_direct": sorted(direct_signatures),
            "seen_legacy": sorted(legacy_signatures),
            "results": [],
        }
        return state, {
            "status": "baseline",
            "passed_windows": 0,
            "required_windows": required_windows,
            "direct_new": 0,
            "legacy_new": 0,
        }

    seen_direct = set(state.get("seen_direct", []))
    seen_legacy = set(state.get("seen_legacy", []))
    direct_new = direct_signatures - seen_direct
    legacy_new = legacy_signatures - seen_legacy
    state["seen_direct"] = sorted(seen_direct | direct_signatures)
    state["seen_legacy"] = sorted(seen_legacy | legacy_signatures)
    state["required_windows"] = required_windows

    if int(state.get("passed_windows", 0)) >= required_windows:
        status = "passed"
    elif not direct_new and not legacy_new:
        status = "waiting"
    elif direct_new == legacy_new:
        state["passed_windows"] = int(state.get("passed_windows", 0)) + 1
        status = (
            "passed"
            if int(state["passed_windows"]) >= required_windows
            else "window_passed"
        )
    else:
        state["failed_windows"] = int(state.get("failed_windows", 0)) + 1
        status = "mismatch"

    if direct_new or legacy_new:
        results = list(state.get("results", []))
        results.append(
            {
                "checked_at": checked_at,
                "status": status,
                "direct_new": len(direct_new),
                "legacy_new": len(legacy_new),
            }
        )
        state["results"] = results[-20:]
    return state, {
        "status": status,
        "passed_windows": int(state.get("passed_windows", 0)),
        "required_windows": required_windows,
        "direct_new": len(direct_new),
        "legacy_new": len(legacy_new),
    }


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("个人微信一致性验证状态无法读取") from error
    if not isinstance(state, dict):
        raise RuntimeError("个人微信一致性验证状态格式无效")
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=True, sort_keys=True)
            output.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            raise


async def _legacy_metadata(
    session_ids: set[str], start_ms: int
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    async with open_ciphertalk() as ciphertalk:
        for session in await _sessions(ciphertalk):
            session_id = str(session.get("sessionId") or "")
            if session_id not in session_ids:
                continue
            for message in await _messages(ciphertalk, session_id, start_ms):
                cursor = (
                    message.get("cursor")
                    if isinstance(message.get("cursor"), dict)
                    else {}
                )
                result.append(
                    {
                        "id": str(message.get("messageId") or ""),
                        "timestamp_ms": _epoch_ms(
                            message.get("timestampMs") or message.get("timestamp")
                        ),
                        "kind": str(message.get("kind") or "0"),
                        "session_id": session_id,
                        "local_id": int(cursor.get("localId") or 0),
                        "sort_seq": int(cursor.get("sortSeq") or 0),
                    }
                )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--required-windows", type=int, default=3)
    args = parser.parse_args()
    if args.required_windows < 1:
        raise SystemExit("验证窗口数量必须大于零")
    token = os.getenv("WORKBENCH_WORKER_TOKEN", "")
    if not token:
        raise SystemExit("缺少工作台本机访问凭据")
    client = WorkbenchClient(
        os.getenv("WORKBENCH_BASE_URL", "http://127.0.0.1:8000"), token
    )
    conversations = client.request_json(
        "GET", "/api/wechat/conversations?source=personal_wechat"
    )
    session_ids = {
        str(item.get("session_id") or "")
        for item in conversations
        if item.get("listen_status") == "active"
    }
    start_ms = int(
        (datetime.now(UTC) - timedelta(hours=max(1, args.hours))).timestamp() * 1000
    )
    dataset = discover_dataset()
    with decrypted_dataset(dataset, load_key) as clear:
        direct = [
            item
            for item in read_message_metadata(clear, start_ms)
            if item["session_id"] in session_ids
        ]
    legacy = asyncio.run(_legacy_metadata(session_ids, start_ms))
    if args.state_file:
        checked_at = datetime.now(UTC).isoformat()
        state, summary = update_window_state(
            _load_state(args.state_file),
            direct,
            legacy,
            checked_at=checked_at,
            required_windows=args.required_windows,
        )
        _save_state(args.state_file, state)
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 1 if summary["status"] == "mismatch" else 0
    summary = comparison_summary(direct, legacy)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
