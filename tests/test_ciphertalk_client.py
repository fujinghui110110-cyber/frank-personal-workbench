from __future__ import annotations

import asyncio
import json

import httpx

from scripts.ciphertalk_client import CipherTalkClient


def test_ciphertalk_client_uses_local_proxy_shape() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/tool/list_sessions"
            assert json.loads(request.content) == {"args": {"offset": 0, "limit": 1}}
            return httpx.Response(200, json={"success": True, "data": {"items": []}})

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:5032",
            transport=httpx.MockTransport(handler),
        ) as http:
            result = await CipherTalkClient(http).call(
                "list_sessions", {"offset": 0, "limit": 1}
            )
        assert result == {"items": []}

    asyncio.run(scenario())
