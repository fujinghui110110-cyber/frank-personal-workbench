from __future__ import annotations

import json

import app.client as client_module


def test_local_workbench_client_bypasses_system_proxy(monkeypatch) -> None:
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"ok": True}).encode()

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout))
            return Response()

    monkeypatch.setattr(client_module, "build_opener", lambda *_args: Opener())
    monkeypatch.setattr(
        client_module,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("used proxy")),
    )

    client = client_module.WorkbenchClient("http://127.0.0.1:8000", "test-token")

    assert client.request_json("GET", "/api/test") == {"ok": True}
    assert calls == [("http://127.0.0.1:8000/api/test", 60)]
