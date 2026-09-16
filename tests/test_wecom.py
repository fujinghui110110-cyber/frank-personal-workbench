from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts import wecom_crypto, wecom_sync


def test_saved_key_is_revalidated_locally_after_wecom_upgrade(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dataset = tmp_path / "account-1" / "Data"
    dataset.mkdir(parents=True)
    vault = tmp_path / "vault"
    private = vault / "private"
    private.mkdir(parents=True)
    key = bytes(range(16))
    key_path = private / f"key-{hashlib.sha256('account-1'.encode()).hexdigest()[:16]}.json"
    key_path.write_text(
        json.dumps({"version": 1, "app_build": "99905", "key": key.hex()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(wecom_crypto, "VAULT_ROOT", vault)
    monkeypatch.setattr(wecom_crypto, "app_version", lambda: ("5.0.10", "99949"))
    monkeypatch.setattr(
        wecom_crypto,
        "validates_key",
        lambda candidate, candidate_dataset: candidate == key and candidate_dataset == dataset,
    )

    assert wecom_crypto.load_key(dataset) == key
    refreshed = json.loads(key_path.read_text(encoding="utf-8"))
    assert refreshed["app_build"] == "99949"


def test_failed_upgrade_validation_does_not_capture_or_scan_processes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dataset = tmp_path / "account-1" / "Data"
    dataset.mkdir(parents=True)
    vault = tmp_path / "vault"
    private = vault / "private"
    private.mkdir(parents=True)
    key = bytes(range(16))
    key_path = private / f"key-{hashlib.sha256('account-1'.encode()).hexdigest()[:16]}.json"
    key_path.write_text(
        json.dumps({"version": 1, "app_build": "99905", "key": key.hex()}),
        encoding="utf-8",
    )
    monkeypatch.setattr(wecom_crypto, "VAULT_ROOT", vault)
    monkeypatch.setattr(wecom_crypto, "app_version", lambda: ("5.0.10", "99949"))
    monkeypatch.setattr(wecom_crypto, "validates_key", lambda *_: False)
    capture_called = False

    def forbidden_capture(*_args, **_kwargs):
        nonlocal capture_called
        capture_called = True
        raise AssertionError("不应自动操作企业微信进程")

    monkeypatch.setattr(wecom_crypto, "capture_key", forbidden_capture)

    with pytest.raises(RuntimeError, match="工作台没有操作企业微信进程"):
        wecom_crypto.load_key(dataset)
    assert capture_called is False


def _protobuf_text(value: str) -> bytes:
    encoded = value.encode("utf-8")
    assert len(encoded) < 128
    return bytes((0x0A, len(encoded))) + encoded


def _build_snapshot(root: Path) -> Path:
    snapshot = root / "snapshot"
    snapshot.mkdir()
    now = int(datetime.now(UTC).timestamp())

    with sqlite3.connect(snapshot / "message.db") as connection:
        connection.execute(
            "CREATE TABLE message_table (message_id INTEGER, sequence INTEGER, "
            "sender_id TEXT, content_type INTEGER, send_time INTEGER, "
            "content BLOB, conversation_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO message_table VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    1,
                    1,
                    "user-1",
                    2,
                    now - 8 * 24 * 60 * 60,
                    _protobuf_text("过往消息"),
                    "R:session-1",
                ),
                (
                    2,
                    2,
                    "user-1",
                    2,
                    now - 6 * 24 * 60 * 60,
                    _protobuf_text("请继续跟进合同付款"),
                    "R:session-1",
                ),
                (
                    3,
                    3,
                    "self-user",
                    2,
                    now - 60,
                    _protobuf_text("我来确认私聊事项"),
                    "S:private-session",
                ),
                (
                    4,
                    4,
                    "user-2",
                    2,
                    now - 30,
                    _protobuf_text("请继续跟进合同付款"),
                    "S:private-session",
                ),
            ],
        )

    with sqlite3.connect(snapshot / "session.db") as connection:
        connection.execute(
            "CREATE TABLE conversation_table "
            "(id TEXT, name TEXT, last_message_time INTEGER)"
        )
        connection.execute(
            "INSERT INTO conversation_table VALUES (?, ?, ?)",
            ("R:session-1", "企微财务协同群", now),
        )
        connection.execute(
            "INSERT INTO conversation_table VALUES (?, ?, ?)",
            ("S:private-session", "", 0),
        )

    with sqlite3.connect(snapshot / "user.db") as connection:
        connection.execute("CREATE TABLE user_table (id TEXT, name TEXT)")
        connection.execute(
            "INSERT INTO user_table VALUES (?, ?)", ("user-1", "财务同事")
        )
        connection.executemany(
            "INSERT INTO user_table VALUES (?, ?)",
            [("self-user", "傅京晖"), ("user-2", "陈贞婷")],
        )
    return snapshot


def test_wecom_sync_only_submits_recent_messages(tmp_path: Path, monkeypatch) -> None:
    snapshot = _build_snapshot(tmp_path)
    dataset = tmp_path / "account" / "Data"
    submitted: list[dict] = []

    class Client:
        def request_json(self, method, path, payload=None):
            if (method, path) == (
                "GET",
                "/api/wechat/conversations?source=wecom",
            ):
                return []
            if (method, path) == ("POST", "/api/wechat/windows"):
                submitted.append(payload)
                return {"created": True}
            raise AssertionError((method, path))

    monkeypatch.setattr(wecom_sync, "create_snapshot", lambda: snapshot)
    monkeypatch.setattr(wecom_sync, "discover_dataset", lambda: dataset)

    result = wecom_sync.run_wecom_sync(Client())

    assert result == {"messages": 3, "windows": 2, "skipped": 0}
    assert len(submitted) == 2
    group = next(item for item in submitted if item["session_id"] == "R:session-1")
    private = next(
        item for item in submitted if item["session_id"] == "S:private-session"
    )
    assert group["source"] == "wecom"
    assert group["kind"] == "group"
    assert group["messages"][0]["sender"]["name"] == "财务同事"
    assert private["kind"] == "friend"
    assert private["display_name"] == "陈贞婷"
    assert len(private["messages"]) == 2
    assert not snapshot.exists()


def test_real_wecom_key_rejects_wrong_and_corrupt_pages(tmp_path: Path) -> None:
    try:
        dataset = wecom_crypto.discover_dataset()
        key = wecom_crypto.load_key(dataset)
    except (FileNotFoundError, RuntimeError):
        pytest.skip("本机尚未完成企业微信安全读取验证")

    assert wecom_crypto.validates_key(key, dataset)
    wrong_key = bytes(value ^ 0xFF for value in key)
    assert not wecom_crypto.validates_key(wrong_key, dataset)

    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    for name in ("message.db", "session.db", "user.db"):
        source = dataset / name
        with source.open("rb") as stream:
            first_page = bytearray(stream.read(65536))
        (corrupt / name).write_bytes(first_page)
    message_page = bytearray((corrupt / "message.db").read_bytes())
    message_page[24] ^= 0xFF
    (corrupt / "message.db").write_bytes(message_page)
    assert not wecom_crypto.validates_key(key, corrupt)


def test_wecom_normalization_marks_self_direction_and_sender_flag(
    tmp_path: Path, monkeypatch
) -> None:
    snapshot = _build_snapshot(tmp_path)
    submitted: list[dict] = []

    class Client:
        def request_json(self, method, path, payload=None):
            if (method, path) == (
                "GET",
                "/api/wechat/conversations?source=wecom",
            ):
                return []
            if (method, path) == ("POST", "/api/wechat/windows"):
                submitted.append(payload)
                return {"created": True}
            raise AssertionError((method, path, payload))

    monkeypatch.setattr(wecom_sync, "create_snapshot", lambda: snapshot)
    monkeypatch.setattr(wecom_sync, "discover_dataset", lambda: snapshot / "account")

    result = wecom_sync.run_wecom_sync(Client())

    assert result == {"messages": 3, "windows": 2, "skipped": 0}
    private = next(item for item in submitted if item["session_id"] == "S:private-session")
    self_message = next(
        item for item in private["messages"] if item["sender"]["username"] == "self-user"
    )
    other_message = next(
        item for item in private["messages"] if item["sender"]["username"] == "user-2"
    )
    assert self_message["direction"] == "out"
    assert self_message["sender"]["isSelf"] is True
    assert other_message["direction"] == "in"
    assert other_message["sender"]["isSelf"] is False
