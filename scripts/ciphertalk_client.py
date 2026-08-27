from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx


DEFAULT_CONFIG_DB = Path.home() / "Library/Application Support/ciphertalk/ciphertalk-config.db"


def _config_value(database: Path, key: str) -> Any:
    if not database.exists():
        raise RuntimeError("未找到 CipherTalk 配置，请先打开 CipherTalk")
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    if not row:
        raise RuntimeError("CipherTalk MCP 尚未完成初始化")
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return row[0]


class CipherTalkClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def call(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        response = await self.http.post(f"/tool/{name}", json={"args": arguments or {}})
        try:
            payload = response.json()
        except ValueError as error:
            raise RuntimeError("CipherTalk 返回了无法读取的响应") from error
        if response.is_error or not payload.get("success"):
            detail = payload.get("error") if isinstance(payload, dict) else None
            message = detail.get("message") if isinstance(detail, dict) else None
            raise RuntimeError(str(message or "CipherTalk 暂时不可用"))
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("CipherTalk 没有返回可读取的数据")
        return data


@asynccontextmanager
async def open_ciphertalk() -> AsyncIterator[CipherTalkClient]:
    database = Path(os.getenv("CIPHERTALK_CONFIG_DB", str(DEFAULT_CONFIG_DB)))
    port = int(os.getenv("CIPHERTALK_MCP_PORT") or _config_value(database, "mcpProxyPort"))
    token = os.getenv("CIPHERTALK_MCP_TOKEN") or str(
        _config_value(database, "mcpProxyToken") or ""
    )
    if not token:
        raise RuntimeError("CipherTalk MCP 尚未生成访问凭据，请重启 CipherTalk")
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    ) as http:
        try:
            response = await http.get("/status")
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise RuntimeError("CipherTalk 尚未准备好，请保持应用打开") from error
        yield CipherTalkClient(http)
