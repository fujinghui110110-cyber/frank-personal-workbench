from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from app.client import WorkbenchClient
from scripts.ciphertalk_client import open_ciphertalk
from scripts.personal_wechat_crypto import decrypted_dataset, discover_dataset
from scripts.personal_wechat_keys import load_key
from scripts.personal_wechat_sync import read_message_metadata
from scripts.wechat_sync import _epoch_ms, _messages, _sessions


def comparison_summary(
    direct: list[dict[str, Any]], legacy: list[dict[str, Any]]
) -> dict[str, Any]:
    def kind_group(value: Any) -> str:
        kind = str(value or "0")
        if kind.startswith("app_") or kind in {"file", "link", "quote"}:
            return "app"
        return kind

    def identity(item: dict[str, Any]) -> str:
        if item.get("local_id") is None or item.get("sort_seq") is None:
            return str(item.get("id"))
        return f"{item.get('sort_seq')}|{item.get('local_id')}"

    def signatures(items: list[dict[str, Any]]) -> set[str]:
        return {
            hashlib.sha256(
                (
                    f"{item.get('session_id')}|{item.get('sort_seq')}|"
                    f"{item.get('timestamp_ms')}|{identity(item)}|"
                    f"{kind_group(item.get('kind'))}"
                ).encode()
            ).hexdigest()
            for item in items
        }

    direct_signatures = signatures(direct)
    legacy_signatures = signatures(legacy)
    return {
        "direct_count": len(direct_signatures),
        "legacy_count": len(legacy_signatures),
        "direct_only": len(direct_signatures - legacy_signatures),
        "legacy_only": len(legacy_signatures - direct_signatures),
        "consistent": direct_signatures == legacy_signatures,
    }


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
    args = parser.parse_args()
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
    summary = comparison_summary(direct, legacy)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["consistent"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
