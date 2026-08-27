from __future__ import annotations

from pathlib import Path

from scripts import icloud_inbox


class FakeClient:
    def __init__(self, *, created: bool = True):
        self.created = created
        self.calls: list[dict[str, object]] = []

    def intake(self, **kwargs):
        self.calls.append(kwargs)
        return {"created": self.created, "material": {"id": "mat_test"}}


def test_file_changed_during_hash_waits_for_next_scan(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "投递箱"
    pending = root / icloud_inbox.PENDING_DIR
    pending.mkdir(parents=True)
    source = pending / "仍在同步.md"
    source.write_text("第一段", encoding="utf-8")

    def changing_hash(path: Path) -> str:
        path.write_text("第一段和第二段", encoding="utf-8")
        return "temporary"

    monkeypatch.setattr(icloud_inbox, "file_sha256", changing_hash)
    client = FakeClient()
    assert icloud_inbox.scan_icloud_inbox(client, root, min_age_seconds=0) == []
    assert source.exists()
    assert client.calls == []


def test_source_type_for_user_files() -> None:
    assert icloud_inbox.source_type_for(Path("会议录音.m4a")) == "audio"
    assert icloud_inbox.source_type_for(Path("现场视频.mov")) == "video"
    assert icloud_inbox.source_type_for(Path("发票照片.heic")) == "image"
    assert icloud_inbox.source_type_for(Path("付款审批.pdf")) == "wecom_approval"
    assert icloud_inbox.source_type_for(Path("群聊_预算.json")) == "wechat_markdown"
    assert icloud_inbox.source_type_for(Path("预算说明.md")) == "file"


def test_successful_or_duplicate_intake_moves_file_to_received(tmp_path: Path) -> None:
    pending = tmp_path / icloud_inbox.PENDING_DIR
    pending.mkdir()
    first = pending / "群聊_预算.json"
    first.write_text("第一份聊天", encoding="utf-8")
    client = FakeClient()

    imported = icloud_inbox.scan_icloud_inbox(
        client, tmp_path, min_age_seconds=0
    )

    assert len(imported) == 1
    assert not first.exists()
    assert (tmp_path / icloud_inbox.RECEIVED_DIR / first.name).read_text() == "第一份聊天"
    assert client.calls[0]["source_type"] == "wechat_markdown"
    assert str(client.calls[0]["idempotency_key"]).startswith(
        "icloud:wechat_markdown:"
    )

    duplicate = pending / first.name
    duplicate.write_text("第一份聊天", encoding="utf-8")
    duplicate_client = FakeClient(created=False)
    icloud_inbox.scan_icloud_inbox(
        duplicate_client, tmp_path, min_age_seconds=0
    )
    assert not duplicate.exists()
    assert (tmp_path / icloud_inbox.RECEIVED_DIR / "群聊_预算 (2).json").exists()


def test_root_drop_is_also_received(tmp_path: Path) -> None:
    source = tmp_path / "会议录音.wav"
    source.write_bytes(b"audio")
    client = FakeClient()

    imported = icloud_inbox.scan_icloud_inbox(
        client,
        tmp_path,
        min_age_seconds=0,
    )

    assert len(imported) == 1
    assert client.calls[0]["source_type"] == "audio"
    assert (tmp_path / icloud_inbox.RECEIVED_DIR / source.name).exists()


def test_read_failure_keeps_file_waiting(tmp_path: Path, monkeypatch) -> None:
    pending = tmp_path / icloud_inbox.PENDING_DIR
    pending.mkdir()
    waiting = pending / "会议录音.m4a"
    waiting.write_bytes(b"audio")

    def fail_read(_: Path) -> str:
        raise OSError("尚未从 iCloud 下载完成")

    monkeypatch.setattr(icloud_inbox, "file_sha256", fail_read)
    imported = icloud_inbox.scan_icloud_inbox(
        FakeClient(), tmp_path, min_age_seconds=0
    )

    assert imported == []
    assert waiting.exists()
