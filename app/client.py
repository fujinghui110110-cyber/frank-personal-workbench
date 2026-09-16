from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen
from uuid import uuid4


class WorkbenchClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._open = (
            build_opener(ProxyHandler({})).open
            if urlparse(self.base_url).hostname in {"127.0.0.1", "localhost", "::1"}
            else urlopen
        )

    def request_json(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> Any:
        body = None
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with self._open(request, timeout=60) as response:
                content = response.read()
        except HTTPError as error:
            content = error.read()
            try:
                detail = json.loads(content.decode("utf-8")).get("detail", str(error))
            except (json.JSONDecodeError, UnicodeDecodeError):
                detail = str(error)
            raise RuntimeError(detail) from error
        return json.loads(content.decode("utf-8")) if content else None

    def get_bytes(self, path: str) -> bytes:
        request = Request(
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {self.token}"},
            method="GET",
        )
        try:
            with self._open(request, timeout=120) as response:
                return response.read()
        except HTTPError as error:
            raise RuntimeError(error.read().decode("utf-8", errors="replace")) from error

    def intake(
        self,
        source_type: str,
        text_note: str = "",
        file_path: Path | None = None,
        matter_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        boundary = f"----workbench{uuid4().hex}"
        parts: list[bytes] = []

        def field(name: str, value: str) -> None:
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                    value.encode("utf-8"),
                    b"\r\n",
                ]
            )

        field("source_type", source_type)
        field("text_note", text_note)
        if matter_id:
            field("matter_id", matter_id)
        if file_path:
            filename = file_path.name
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            parts.extend(
                [
                    f"--{boundary}\r\n".encode(),
                    (
                        f'Content-Disposition: form-data; name="upload"; filename="{filename}"\r\n'
                    ).encode("utf-8"),
                    f"Content-Type: {content_type}\r\n\r\n".encode(),
                    file_path.read_bytes(),
                    b"\r\n",
                ]
            )
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Idempotency-Key": idempotency_key or uuid4().hex,
            "Accept": "application/json",
        }
        request = Request(
            f"{self.base_url}/api/intake", data=body, headers=headers, method="POST"
        )
        try:
            with self._open(request, timeout=180) as response:
                content = response.read()
        except HTTPError as error:
            raise RuntimeError(error.read().decode("utf-8", errors="replace")) from error
        return json.loads(content.decode("utf-8"))
