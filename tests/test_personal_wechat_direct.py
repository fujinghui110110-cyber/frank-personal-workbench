from __future__ import annotations

import hashlib
import sqlite3
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import zstandard

from scripts.personal_wechat_crypto import (
    PersonalWechatKeyError,
    PersonalWechatUnsupportedError,
    WechatDataset,
    decrypt_database,
    derive_database_key,
    probe_database_key,
    snapshot_dataset,
)
import scripts.personal_wechat_sync as personal_wechat_sync
from scripts.compare_personal_wechat_readers import comparison_summary
from scripts.personal_wechat_sync import (
    _decode_dat,
    compatibility_summary,
    enrich_media,
    read_messages,
)


def _database(path: Path, sql: str, rows: list[tuple] = []) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(sql)
        if rows:
            connection.executemany(
                "INSERT INTO messages(localId, createTime, talker, sender, isSend, type, content, localPath) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        connection.commit()
    finally:
        connection.close()


def _wechat_encrypted_database(path: Path, account_key: str, sqlcipher: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    salt = hashlib.sha256(path.name.encode()).digest()[:16]
    database_key = hashlib.pbkdf2_hmac(
        "sha512", bytes.fromhex(account_key), salt, 256_000, dklen=32
    ).hex()
    created = subprocess.run(
        [str(sqlcipher), str(path)],
        input=(
            f"PRAGMA key = \"x'{database_key}{salt.hex()}'\";\n"
            "PRAGMA kdf_iter = 1;\n"
            "PRAGMA cipher_compatibility = 4;\n"
            "PRAGMA cipher_page_size = 4096;\n"
            "CREATE TABLE messages(id INTEGER, content TEXT);\n"
            "INSERT INTO messages VALUES (1, 'synthetic');\n.quit\n"
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert created.returncode == 0
    assert path.read_bytes()[:16] == salt


def test_snapshot_keeps_database_wal_and_shm(tmp_path: Path) -> None:
    root = tmp_path / "account" / "db_storage"
    message = root / "message" / "message_0.db"
    _database(message, "CREATE TABLE messages(id INTEGER)")
    for suffix in ("-wal", "-shm"):
        Path(f"{message}{suffix}").write_bytes(suffix.encode())
    _database(root / "contact" / "contact.db", "CREATE TABLE contacts(id INTEGER)")
    _database(root / "session" / "session.db", "CREATE TABLE sessions(id INTEGER)")
    dataset = WechatDataset(
        "account",
        root,
        (message, root / "contact/contact.db", root / "session/session.db"),
    )
    snapshot = snapshot_dataset(dataset, tmp_path / "snapshot")
    assert (snapshot / "message/message_0.db-wal").read_bytes() == b"-wal"
    assert (snapshot / "message/message_0.db-shm").read_bytes() == b"-shm"


def test_reads_split_messages_contacts_group_sender_and_media(tmp_path: Path) -> None:
    account = tmp_path / "account"
    root = account / "db_storage"
    schema = "CREATE TABLE messages(localId INTEGER, createTime INTEGER, talker TEXT, sender TEXT, isSend INTEGER, type INTEGER, content BLOB, localPath TEXT);"
    _database(
        root / "message/message_0.db",
        schema,
        [(1, 1_800_000_000, "team@chatroom", "alice", 0, 1, "请跟进付款", "")],
    )
    image = account / "msg/attach/image.dat"
    image.parent.mkdir(parents=True)
    jpeg = b"\xff\xd8\xffpayload"
    image.write_bytes(bytes(value ^ 0x66 for value in jpeg))
    _database(
        root / "message/message_1.db",
        schema,
        [(2, 1_800_000_100, "bob", "", 0, 3, b"", "msg/attach/image.dat")],
    )
    _database(
        root / "contact/contact.db",
        "CREATE TABLE contacts(username TEXT, nickname TEXT); INSERT INTO contacts VALUES ('alice', '艾丽丝'), ('team@chatroom', '财务群'), ('bob', '鲍勃');",
    )
    _database(
        root / "session/session.db",
        "CREATE TABLE sessions(username TEXT, display_name TEXT);",
    )
    dataset = WechatDataset("account", root, tuple(sorted(root.rglob("*.db"))))
    result = read_messages(root, dataset, 0)
    assert result["team@chatroom"]["display_name"] == "财务群"
    assert result["team@chatroom"]["messages"][0]["sender"]["name"] == "艾丽丝"
    assert result["bob"]["messages"][0]["kind"] == "image"
    decoded = tmp_path / "decoded.jpg"
    assert _decode_dat(image, decoded)
    assert decoded.read_bytes() == jpeg


def test_reads_compressed_text_voice_video_file_link_and_quote(tmp_path: Path) -> None:
    account = tmp_path / "account"
    root = account / "db_storage"
    schema = "CREATE TABLE messages(localId INTEGER, createTime INTEGER, talker TEXT, sender TEXT, isSend INTEGER, type INTEGER, content BLOB, localPath TEXT);"
    compressed = zstandard.ZstdCompressor().compress("压缩后的工作安排".encode())
    rows = [
        (1, 1_800_000_000, "alice", "", 0, 1, compressed, ""),
        (2, 1_800_000_001, "alice", "", 0, 34, b"", "msg/voice.silk"),
        (3, 1_800_000_002, "alice", "", 0, 43, b"", "msg/video.mp4"),
        (4, 1_800_000_003, "alice", "", 0, 49, "报告", "msg/report.pdf"),
        (
            5,
            1_800_000_004,
            "alice",
            "",
            0,
            49,
            "<msg><title>链接</title><url>https://example.test</url></msg>",
            "",
        ),
        (
            6,
            1_800_000_005,
            "alice",
            "",
            0,
            49,
            "<msg><title>引用回复</title><content>原始内容</content></msg>",
            "",
        ),
    ]
    for relative in ("msg/voice.silk", "msg/video.mp4", "msg/report.pdf"):
        path = account / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    _database(root / "message/message_0.db", schema, rows)
    _database(
        root / "contact/contact.db",
        "CREATE TABLE contacts(username TEXT, nickname TEXT); INSERT INTO contacts VALUES ('alice', '艾丽丝');",
    )
    _database(
        root / "session/session.db",
        "CREATE TABLE sessions(username TEXT, display_name TEXT);",
    )
    dataset = WechatDataset("account", root, tuple(sorted(root.rglob("*.db"))))
    messages = read_messages(root, dataset, 0)["alice"]["messages"]
    assert [item["kind"] for item in messages] == [
        "text",
        "voice",
        "video",
        "file",
        "link",
        "quote",
    ]
    assert messages[0]["text"] == "压缩后的工作安排"
    assert messages[1]["text"] == messages[2]["text"] == messages[3]["text"] == ""
    assert messages[4]["text"] == "链接\nhttps://example.test"
    assert messages[5]["text"] == "引用回复\n原始内容"


def test_ambiguous_media_index_never_attaches_the_wrong_file(tmp_path: Path) -> None:
    account = tmp_path / "account"
    root = account / "db_storage"
    _database(
        root / "message/message_0.db",
        "CREATE TABLE messages(localId INTEGER, createTime INTEGER, talker TEXT, type INTEGER, content BLOB); INSERT INTO messages VALUES (7, 1800000000, 'alice', 3, X'');",
    )
    _database(
        root / "message/media_0.db",
        "CREATE TABLE media(localId INTEGER, localPath TEXT); INSERT INTO media VALUES (7, 'msg/a.dat'), (7, 'msg/b.dat');",
    )
    _database(root / "contact/contact.db", "CREATE TABLE contacts(id INTEGER)")
    _database(root / "session/session.db", "CREATE TABLE sessions(id INTEGER)")
    for name in ("a.dat", "b.dat"):
        path = account / "msg" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")
    dataset = WechatDataset("account", root, tuple(sorted(root.rglob("*.db"))))
    message = read_messages(root, dataset, 0)["alice"]["messages"][0]
    assert "localPath" not in message["media"]


def test_unknown_message_schema_stops_safely(tmp_path: Path) -> None:
    root = tmp_path / "account/db_storage"
    _database(root / "message/message_0.db", "CREATE TABLE unknown(payload BLOB)")
    _database(root / "contact/contact.db", "CREATE TABLE contacts(id INTEGER)")
    _database(root / "session/session.db", "CREATE TABLE sessions(id INTEGER)")
    dataset = WechatDataset("account", root, tuple(sorted(root.rglob("*.db"))))
    with pytest.raises(PersonalWechatUnsupportedError, match="当前微信版本暂不支持"):
        read_messages(root, dataset, 0)


def test_compatibility_check_reads_only_counts_and_types(tmp_path: Path) -> None:
    clear = tmp_path / "clear"
    _database(
        clear / "message/message_0.db",
        "CREATE TABLE messages(localId INTEGER, createTime INTEGER, talker TEXT, type INTEGER, content BLOB); INSERT INTO messages VALUES (1, 1800000000, 'alice', 1, 'private text'), (2, 1800000100, 'alice', 3, X'0102');",
    )
    summary = compatibility_summary(clear)
    assert summary == {
        "supported": True,
        "database_count": 1,
        "message_shards": 1,
        "message_count": 2,
        "latest_timestamp": 1_800_000_100,
        "type_counts": {"1": 1, "3": 1},
    }
    assert "private text" not in str(summary)


def test_parallel_comparison_reports_only_counts() -> None:
    direct = [{"session_id": "alice", "id": "1", "timestamp_ms": 1000, "kind": "1"}]
    same = [
        {
            "session_id": "alice",
            "id": "1",
            "timestamp_ms": 1000,
            "kind": "1",
            "text": "private",
        }
    ]
    different = [{"session_id": "alice", "id": "2", "timestamp_ms": 1000, "kind": "1"}]
    assert comparison_summary(direct, same) == {
        "direct_count": 1,
        "legacy_count": 1,
        "direct_only": 0,
        "legacy_only": 0,
        "consistent": True,
    }
    summary = comparison_summary(direct, different)
    assert summary["consistent"] is False
    assert "private" not in str(summary)

    other_session = [
        {"session_id": "bob", "id": "1", "timestamp_ms": 1000, "kind": "1"}
    ]
    assert comparison_summary(direct, other_session)["consistent"] is False


def test_sqlcipher_decryption_rejects_wrong_key(tmp_path: Path) -> None:
    sqlcipher = Path("/opt/homebrew/bin/sqlcipher")
    if not sqlcipher.is_file():
        pytest.skip("本机没有 SQLCipher")
    encrypted = tmp_path / "encrypted.db"
    key = "12" * 32
    _wechat_encrypted_database(encrypted, key, sqlcipher)
    assert derive_database_key(encrypted, key) == hashlib.pbkdf2_hmac(
        "sha512", bytes.fromhex(key), encrypted.read_bytes()[:16], 256_000, dklen=32
    ).hex()
    assert probe_database_key(encrypted, key, sqlcipher) is True
    second = tmp_path / "second.db"
    _wechat_encrypted_database(second, key, sqlcipher)
    assert derive_database_key(second, key) != derive_database_key(encrypted, key)
    assert probe_database_key(second, key, sqlcipher) is True
    assert probe_database_key(encrypted, "34" * 32, sqlcipher) is False
    clear = tmp_path / "clear.db"
    decrypt_database(encrypted, clear, key, sqlcipher)
    connection = sqlite3.connect(clear)
    try:
        assert (
            connection.execute("SELECT content FROM messages").fetchone()[0]
            == "synthetic"
        )
    finally:
        connection.close()
    with pytest.raises(PersonalWechatKeyError):
        decrypt_database(encrypted, tmp_path / "wrong.db", "34" * 32, sqlcipher)


def test_failed_media_decode_never_creates_placeholder_task_text(
    tmp_path: Path,
) -> None:
    unreadable = tmp_path / "image.dat"
    unreadable.write_bytes(b"not-a-wechat-image")
    messages = [
        {
            "kind": "image",
            "text": "",
            "media": {"type": "image", "localPath": str(unreadable)},
        }
    ]
    result = enrich_media(messages)[0]
    assert result["text"] == ""
    assert result["media"]["extractionStatus"] == "failed"


def test_failed_media_stops_before_any_message_write(
    tmp_path: Path, monkeypatch
) -> None:
    dataset = WechatDataset("account", tmp_path / "account/db_storage", ())
    unreadable = tmp_path / "image.dat"
    unreadable.write_bytes(b"not-a-wechat-image")

    @contextmanager
    def clear_dataset(*_args, **_kwargs):
        yield tmp_path

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request_json(self, method: str, path: str, payload=None):
            self.calls.append((method, path))
            if (method, path) == (
                "GET",
                "/api/wechat/conversations?source=personal_wechat",
            ):
                return []
            raise AssertionError("媒体读取失败时不应写入工作台")

    client = Client()
    monkeypatch.setenv("PERSONAL_WECHAT_DIRECT_ENABLED_AT", "2026-09-01T00:00:00+08:00")
    monkeypatch.setattr(personal_wechat_sync, "discover_dataset", lambda: dataset)
    monkeypatch.setattr(personal_wechat_sync, "decrypted_dataset", clear_dataset)
    monkeypatch.setattr(
        personal_wechat_sync,
        "read_messages",
        lambda *_args, **_kwargs: {
            "alice": {
                "display_name": "合成会话",
                "kind": "dm",
                "messages": [
                    {
                        "id": "1",
                        "timestampMs": 1_800_000_000_000,
                        "kind": "image",
                        "text": "",
                        "media": {
                            "type": "image",
                            "localPath": str(unreadable),
                        },
                    }
                ],
            }
        },
    )

    with pytest.raises(RuntimeError, match="将在下次检查时重试"):
        personal_wechat_sync.run_direct_wechat_sync(client)
    assert client.calls == [("GET", "/api/wechat/conversations?source=personal_wechat")]


def test_direct_failure_happens_before_any_message_write(
    tmp_path: Path, monkeypatch
) -> None:
    dataset = WechatDataset("account", tmp_path / "account/db_storage", ())

    @contextmanager
    def clear_dataset(*_args, **_kwargs):
        yield tmp_path

    class Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def request_json(self, method: str, path: str, payload=None):
            self.calls.append((method, path))
            if (method, path) == (
                "GET",
                "/api/wechat/conversations?source=personal_wechat",
            ):
                return []
            raise AssertionError("失败前不应写入工作台")

    client = Client()
    monkeypatch.setenv("PERSONAL_WECHAT_DIRECT_ENABLED_AT", "2026-09-01T00:00:00+08:00")
    monkeypatch.setattr(personal_wechat_sync, "discover_dataset", lambda: dataset)
    monkeypatch.setattr(personal_wechat_sync, "decrypted_dataset", clear_dataset)
    monkeypatch.setattr(
        personal_wechat_sync,
        "read_messages",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PersonalWechatUnsupportedError("当前微信版本暂不支持")
        ),
    )
    with pytest.raises(PersonalWechatUnsupportedError):
        personal_wechat_sync.run_direct_wechat_sync(client)
    assert client.calls == [("GET", "/api/wechat/conversations?source=personal_wechat")]
