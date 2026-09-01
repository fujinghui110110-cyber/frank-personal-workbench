from __future__ import annotations

import base64
import hashlib
import sqlite3
import struct
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest
import zstandard
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from scripts.personal_wechat_crypto import (
    PersonalWechatKeyError,
    PersonalWechatUnsupportedError,
    WechatDataset,
    discover_dataset,
    decrypt_database,
    derive_database_key,
    probe_database_key,
    snapshot_dataset,
)
import scripts.personal_wechat_sync as personal_wechat_sync
from scripts.compare_personal_wechat_readers import (
    comparison_summary,
    update_window_state,
)
from scripts.personal_wechat_sync import (
    _decode_dat,
    _decode_wxgf,
    compatibility_summary,
    enrich_media,
    probe_image_keys,
    read_message_metadata,
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


def _wechat_image_dat(
    payload: bytes, aes_key: bytes, xor_key: int, signature: bytes = b"\x07\x08V2\x08\x07"
) -> bytes:
    aes_size = min(19, len(payload) - 4)
    xor_size = min(4, len(payload) - aes_size)
    encrypted = AES.new(aes_key, AES.MODE_ECB).encrypt(pad(payload[:aes_size], 16))
    middle = payload[aes_size : len(payload) - xor_size]
    tail = bytes(value ^ xor_key for value in payload[len(payload) - xor_size :])
    return struct.pack("<6sLLx", signature, aes_size, xor_size) + encrypted + middle + tail


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


def test_discovery_excludes_full_text_search_database(tmp_path: Path) -> None:
    root = tmp_path / "account/db_storage"
    for relative in (
        "message/message_0.db",
        "message/message_fts.db",
        "message/message_resource.db",
        "contact/contact.db",
        "session/session.db",
    ):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic")

    dataset = discover_dataset(tmp_path)

    assert [path.name for path in dataset.databases] == [
        "contact.db",
        "message_0.db",
        "message_resource.db",
        "session.db",
    ]


def test_reads_wechat_4_hashed_message_tables(tmp_path: Path) -> None:
    account = tmp_path / "wxid_self_abcd"
    clear = account / "db_storage"
    session_id = "group@chatroom"
    table = f"Msg_{hashlib.md5(session_id.encode()).hexdigest()}"
    _database(
        clear / "message/message_0.db",
        f'''CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER);
            INSERT INTO Name2Id(rowid, user_name, is_session) VALUES
                (1, '{session_id}', 1), (2, 'wxid_self', 0),
                (3, 'wxid_colleague', 0);
            CREATE TABLE "{table}"(
                local_id INTEGER, server_id INTEGER, local_type INTEGER,
                sort_seq INTEGER, real_sender_id INTEGER, create_time INTEGER,
                message_content BLOB, compress_content BLOB
            );
            INSERT INTO "{table}" VALUES
                (1, 101, 1, 1, 3, 1800000000, '请跟进合同', NULL),
                (2, 102, 1, 2, 2, 1800000100, '收到', NULL);''',
    )
    _database(
        clear / "contact/contact.db",
        "CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT); "
        "INSERT INTO contact VALUES ('wxid_colleague', '', '同事');",
    )
    _database(
        clear / "session/session.db", "CREATE TABLE SessionTable(username TEXT)"
    )
    dataset = WechatDataset(account.name, clear, tuple(sorted(clear.rglob("*.db"))))

    conversations = read_messages(clear, dataset, 0)
    metadata = read_message_metadata(clear, 0)

    assert conversations[session_id]["messages"][0]["text"] == "请跟进合同"
    assert conversations[session_id]["messages"][0]["sender"]["name"] == "同事"
    assert conversations[session_id]["messages"][0]["direction"] == "in"
    assert conversations[session_id]["messages"][1]["direction"] == "out"
    assert [item["session_id"] for item in metadata] == [session_id, session_id]


def test_reads_wechat_4_packed_image_filename(tmp_path: Path) -> None:
    account = tmp_path / "wxid_self_abcd"
    clear = account / "db_storage"
    session_id = "group@chatroom"
    table = f"Msg_{hashlib.md5(session_id.encode()).hexdigest()}"
    file_id = "ab" * 16
    image = account / "msg/image/2026-09" / f"{file_id}_t.dat"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"synthetic")
    filename_field = bytes([4 << 3 | 2, len(file_id)]) + file_id.encode()
    packed = bytes([3 << 3 | 2, len(filename_field)]) + filename_field
    _database(
        clear / "message/message_0.db",
        f'''CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER);
            INSERT INTO Name2Id(rowid, user_name, is_session) VALUES (1, '{session_id}', 1);
            CREATE TABLE "{table}"(
                local_id INTEGER, local_type INTEGER, create_time INTEGER,
                message_content BLOB, packed_info_data BLOB
            );''',
    )
    connection = sqlite3.connect(clear / "message/message_0.db")
    try:
        connection.execute(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?)',
            (1, 3, 1_800_000_000, b"", packed),
        )
        connection.commit()
    finally:
        connection.close()
    _database(clear / "contact/contact.db", "CREATE TABLE contact(id INTEGER)")
    _database(clear / "session/session.db", "CREATE TABLE session(id INTEGER)")
    dataset = WechatDataset(account.name, clear, tuple(sorted(clear.rglob("*.db"))))

    message = read_messages(clear, dataset, 0)[session_id]["messages"][0]

    assert message["media"]["localPath"] == str(image)


def test_reads_wechat_4_voice_transcript_and_media_blob(
    tmp_path: Path, monkeypatch
) -> None:
    account = tmp_path / "wxid_self_abcd"
    clear = account / "db_storage"
    session_id = "group@chatroom"
    table = f"Msg_{hashlib.md5(session_id.encode()).hexdigest()}"
    transcript = "请复核本月报表"
    transcript_bytes = transcript.encode()
    inner = bytes([2 << 3 | 2, len(transcript_bytes)]) + transcript_bytes
    packed = bytes([5 << 3 | 2, len(inner)]) + inner
    _database(
        clear / "message/message_0.db",
        f'''CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY, is_session INTEGER);
            INSERT INTO Name2Id(rowid, user_name, is_session) VALUES (1, '{session_id}', 1);
            CREATE TABLE "{table}"(
                local_id INTEGER, server_id INTEGER, local_type INTEGER,
                create_time INTEGER, message_content BLOB, packed_info_data BLOB
            );''',
    )
    connection = sqlite3.connect(clear / "message/message_0.db")
    try:
        connection.executemany(
            f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?)',
            [
                (1, 101, 34, 1_800_000_000, b"voice", packed),
                (2, 102, 34, 1_800_000_001, b"voice", b""),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    _database(clear / "contact/contact.db", "CREATE TABLE contact(id INTEGER)")
    _database(clear / "session/session.db", "CREATE TABLE session(id INTEGER)")
    _database(
        clear / "message/media_0.db",
        "CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY); "
        "INSERT INTO Name2Id(rowid, user_name) VALUES (1, 'group@chatroom'); "
        "CREATE TABLE VoiceInfo("
        "chat_name_id INTEGER, create_time INTEGER, local_id INTEGER, "
        "svr_id INTEGER, voice_data BLOB, data_index INTEGER);",
    )
    connection = sqlite3.connect(clear / "message/media_0.db")
    try:
        connection.executemany(
            "INSERT INTO VoiceInfo VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, 1_800_000_000, 1, 101, b"silk-one", 0),
                (1, 1_800_000_001, 2, 102, b"silk-two", 0),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    dataset = WechatDataset(account.name, clear, tuple(sorted(clear.rglob("*.db"))))

    messages = read_messages(clear, dataset, 0)[session_id]["messages"]

    assert messages[0]["text"] == f"[语音转写] {transcript}"
    assert messages[0]["media"]["extractionStatus"] == "completed"
    assert "_voiceData" not in messages[0]["media"]
    assert messages[1]["media"]["_voiceData"] == b"silk-two"
    monkeypatch.setattr(personal_wechat_sync, "_transcribe_voice", lambda _path: "补充转写")
    enriched = enrich_media([messages[1]])[0]
    assert enriched["text"] == "[语音转写] 补充转写"
    assert "_voiceData" not in enriched["media"]


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


def test_parity_windows_only_count_distinct_matching_additions() -> None:
    first = [{"session_id": "a", "id": "1", "timestamp_ms": 1, "kind": "text"}]
    second = first + [
        {"session_id": "a", "id": "2", "timestamp_ms": 2, "kind": "text"}
    ]
    third = second + [
        {"session_id": "a", "id": "3", "timestamp_ms": 3, "kind": "image"}
    ]
    fourth = third + [
        {"session_id": "a", "id": "4", "timestamp_ms": 4, "kind": "voice"}
    ]

    state, baseline = update_window_state({}, first, first, checked_at="t0")
    state, waiting = update_window_state(state, first, first, checked_at="t1")
    state, window_one = update_window_state(state, second, second, checked_at="t2")
    state, window_two = update_window_state(state, third, third, checked_at="t3")
    state, passed = update_window_state(state, fourth, fourth, checked_at="t4")

    assert baseline["status"] == "baseline"
    assert waiting["status"] == "waiting"
    assert window_one["passed_windows"] == 1
    assert window_two["passed_windows"] == 2
    assert passed["status"] == "passed"
    assert passed["passed_windows"] == 3


def test_parity_window_rejects_mismatched_additions() -> None:
    first = [{"session_id": "a", "id": "1", "timestamp_ms": 1, "kind": "text"}]
    state, _ = update_window_state({}, first, first, checked_at="t0")
    direct = first + [
        {"session_id": "a", "id": "2", "timestamp_ms": 2, "kind": "text"}
    ]

    state, result = update_window_state(state, direct, first, checked_at="t1")

    assert result["status"] == "mismatch"
    assert result["passed_windows"] == 0
    assert state["failed_windows"] == 1


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


@pytest.mark.parametrize("signature", [b"\x07\x08V1\x08\x07", b"\x07\x08V2\x08\x07"])
def test_decrypts_wechat_4_image_with_manual_keys(
    tmp_path: Path, signature: bytes
) -> None:
    aes_key = b"0123456789abcdef"
    xor_key = 0x53
    payload = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    source = tmp_path / "image.dat"
    destination = tmp_path / "image.jpg"
    source.write_bytes(_wechat_image_dat(payload, aes_key, xor_key, signature))

    assert _decode_dat(source, destination, (xor_key, aes_key))
    assert destination.read_bytes() == payload
    assert probe_image_keys([source], (xor_key, aes_key))
    assert not _decode_dat(source, destination, (xor_key, b"fedcba9876543210"))


def test_wxgf_conversion_passes_only_hevc_bitstream_to_ffmpeg(monkeypatch) -> None:
    seen: dict[str, bytes] = {}

    def run(_command, **kwargs):
        seen["input"] = kwargs["input"]
        return subprocess.CompletedProcess([], 0, b"\x89PNGsynthetic", b"")

    monkeypatch.setattr(personal_wechat_sync.subprocess, "run", run)
    result = _decode_wxgf(b"wxgf-container\x00\x00\x00\x01hevc")

    assert result == b"\x89PNGsynthetic"
    assert seen["input"] == b"\x00\x00\x00\x01hevc"


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
