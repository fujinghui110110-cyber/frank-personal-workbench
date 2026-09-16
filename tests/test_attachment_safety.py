from __future__ import annotations

from pathlib import Path

from app.attachment_safety import (
    remove_owned_staged_attachment,
    resolve_authorized_attachment,
)


def test_remove_owned_staged_attachment_removes_regular_file_inside_staging_root(
    tmp_path: Path,
) -> None:
    # Given
    staging_root = tmp_path / "email-attachments"
    attachment = staging_root / "message-1" / "notice.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"formal attachment")

    # When
    removed = remove_owned_staged_attachment(attachment, staging_root)

    # Then
    assert removed is True
    assert attachment.exists() is False


def test_remove_owned_staged_attachment_preserves_file_outside_staging_root(
    tmp_path: Path,
) -> None:
    # Given
    staging_root = tmp_path / "email-attachments"
    outside_file = tmp_path / "unrelated.pdf"
    outside_file.write_bytes(b"must remain")

    # When
    removed = remove_owned_staged_attachment(outside_file, staging_root)

    # Then
    assert removed is False
    assert outside_file.read_bytes() == b"must remain"


def test_remove_owned_staged_attachment_preserves_symlink_target(
    tmp_path: Path,
) -> None:
    # Given
    staging_root = tmp_path / "email-attachments"
    staging_root.mkdir()
    outside_file = tmp_path / "unrelated.pdf"
    outside_file.write_bytes(b"must remain")
    linked_attachment = staging_root / "message-1" / "notice.pdf"
    linked_attachment.parent.mkdir()
    linked_attachment.symlink_to(outside_file)

    # When
    removed = remove_owned_staged_attachment(linked_attachment, staging_root)

    # Then
    assert removed is False
    assert linked_attachment.is_symlink() is True
    assert outside_file.read_bytes() == b"must remain"


def test_resolve_authorized_attachment_returns_only_authorized_owned_file(
    tmp_path: Path,
) -> None:
    # Given
    staging_root = tmp_path / "email-attachments"
    attachment = staging_root / "message-1" / "notice.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"formal attachment")

    # When
    resolved = resolve_authorized_attachment(
        attachment,
        (str(attachment),),
        (staging_root,),
    )

    # Then
    assert resolved == attachment.resolve()


def test_resolve_authorized_attachment_rejects_unlisted_owned_file(tmp_path: Path) -> None:
    # Given
    staging_root = tmp_path / "email-attachments"
    attachment = staging_root / "message-1" / "notice.pdf"
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"formal attachment")

    # When
    resolved = resolve_authorized_attachment(attachment, (), (staging_root,))

    # Then
    assert resolved is None
