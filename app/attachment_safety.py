from __future__ import annotations

from pathlib import Path


def resolve_owned_staged_attachment(
    attachment_path: str | Path, staging_root: Path
) -> Path | None:
    return resolve_attachment_in_owned_roots(attachment_path, (staging_root,))


def resolve_attachment_in_owned_roots(
    attachment_path: str | Path, owned_roots: tuple[Path, ...]
) -> Path | None:
    candidate = Path(attachment_path).expanduser()
    if not candidate.is_absolute():
        return None
    for owned_root in owned_roots:
        resolved_attachment = _resolve_owned_regular_file(candidate, owned_root)
        if resolved_attachment is not None:
            return resolved_attachment
    return None


def resolve_authorized_attachment(
    attachment_path: str | Path,
    authorized_paths: tuple[str, ...],
    owned_roots: tuple[Path, ...],
) -> Path | None:
    candidate = resolve_attachment_in_owned_roots(attachment_path, owned_roots)
    if candidate is None:
        return None
    for authorized_path in authorized_paths:
        authorized = resolve_attachment_in_owned_roots(authorized_path, owned_roots)
        if candidate == authorized:
            return candidate
    return None


def remove_owned_staged_attachment(attachment_path: str | Path, staging_root: Path) -> bool:
    candidate = resolve_owned_staged_attachment(attachment_path, staging_root)
    if candidate is None:
        return False
    candidate.unlink()
    return True


def _is_symlink_free(candidate: Path, configured_root: Path) -> bool:
    relative_path = candidate.relative_to(configured_root)
    current = configured_root
    for component in relative_path.parts:
        current = current / component
        if current.is_symlink():
            return False
    return True


def _resolve_owned_regular_file(candidate: Path, owned_root: Path) -> Path | None:
    configured_root = owned_root.expanduser().absolute()
    if configured_root.is_symlink():
        return None
    try:
        relative_path = candidate.relative_to(configured_root)
        resolved_root = configured_root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if ".." in relative_path.parts or not _is_symlink_free(candidate, configured_root):
        return None
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return None
    if not resolved_candidate.is_file():
        return None
    return resolved_candidate
