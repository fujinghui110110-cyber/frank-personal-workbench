from __future__ import annotations

import json
import urllib.error

import pytest

from scripts import configure_deepseek
from scripts.deepseek_client import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    TEXT_MODEL,
    VISION_MODEL,
    call_deepseek_json,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }


def test_text_request_uses_flash_and_json_mode(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    output = call_deepseek_json("判断这段文字", 23, _schema(), api_key="secret-key")

    payload = json.loads(captured["request"].data)
    assert output == '{"ok":true}'
    assert payload["model"] == TEXT_MODEL
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["thinking"] == {"type": "disabled"}
    assert captured["request"].get_header("Authorization") == "Bearer secret-key"
    assert captured["timeout"] == 23


def test_image_request_uses_vision_model_and_base64(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    call_deepseek_json(
        "理解图片",
        23,
        _schema(),
        image_bytes=b"\x89PNG\r\n\x1a\nexample",
        api_key="secret-key",
    )

    payload = captured["payload"]
    assert payload["model"] == VISION_MODEL
    assert payload["messages"][1]["content"][1]["type"] == "image_url"
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )


def test_unsupported_file_is_not_sent_as_an_image(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        return _Response({"choices": [{"message": {"content": '{"ok":true}'}}]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    call_deepseek_json(
        "理解附件",
        23,
        _schema(),
        image_bytes=b"not-an-image",
        api_key="secret-key",
    )

    assert captured["payload"]["model"] == TEXT_MODEL


def test_invalid_key_error_never_contains_the_key(monkeypatch) -> None:
    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "bad", None, None)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="密钥无效") as captured:
        call_deepseek_json("测试", 23, _schema(), api_key="must-not-leak")
    assert "must-not-leak" not in str(captured.value)


def test_configure_validates_then_saves_to_the_expected_keychain_item(
    monkeypatch, capsys
) -> None:
    saved = {}
    monkeypatch.setattr("builtins.input", lambda _prompt: "test-secret")
    monkeypatch.setattr(
        configure_deepseek,
        "call_deepseek_json",
        lambda *_args, **_kwargs: '{"ok":true}',
    )

    def fake_save(account, secret, *, service):
        saved.update(account=account, secret=secret, service=service)

    monkeypatch.setattr(configure_deepseek, "_save_keychain_secret", fake_save)
    configure_deepseek.main()

    assert saved == {
        "account": KEYCHAIN_ACCOUNT,
        "secret": "test-secret",
        "service": KEYCHAIN_SERVICE,
    }
    output = capsys.readouterr().out
    assert "配置完成" in output
    assert "test-secret" not in output
