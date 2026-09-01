from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


WECHAT_ROOT = Path.home() / (
    "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
)
SQLCIPHER = Path("/opt/homebrew/bin/sqlcipher")


class PersonalWechatError(RuntimeError):
    pass


class PersonalWechatKeyError(PersonalWechatError):
    pass


class PersonalWechatUnsupportedError(PersonalWechatError):
    pass


@dataclass(frozen=True)
class WechatDataset:
    account: str
    root: Path
    databases: tuple[Path, ...]

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.account.encode("utf-8")).hexdigest()[:24]


def discover_dataset(root: Path = WECHAT_ROOT) -> WechatDataset:
    candidates: list[WechatDataset] = []
    for storage in root.glob("*/db_storage"):
        databases = _database_paths(storage)
        if databases:
            candidates.append(WechatDataset(storage.parent.name, storage, databases))
    if not candidates:
        raise PersonalWechatUnsupportedError("未找到当前个人微信的本地消息数据")
    return max(
        candidates,
        key=lambda item: max(path.stat().st_mtime_ns for path in item.databases),
    )


def _database_paths(storage: Path) -> tuple[Path, ...]:
    patterns = (
        "message/message_*.db",
        "message/media_*.db",
        "message/message_resource.db",
        "contact/contact.db",
        "session/session.db",
    )
    paths = sorted({
        path
        for pattern in patterns
        for path in storage.glob(pattern)
        if not path.name.startswith("message_fts")
    })
    required = (
        any(
            path.parent.name == "message" and path.name.startswith("message_")
            for path in paths
        ),
        any(path.name == "contact.db" for path in paths),
        any(path.name == "session.db" for path in paths),
    )
    return tuple(paths) if all(required) else ()


def _signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def snapshot_dataset(
    dataset: WechatDataset, destination: Path, retries: int = 3
) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    try:
        for database in dataset.databases:
            relative = database.relative_to(dataset.root)
            copied = False
            for _ in range(max(1, retries)):
                sources = [database]
                sources.extend(
                    path
                    for path in (Path(f"{database}-wal"), Path(f"{database}-shm"))
                    if path.exists()
                )
                before = {path: _signature(path) for path in sources}
                for source in sources:
                    target = destination / source.relative_to(dataset.root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    os.chmod(target, 0o600)
                after_sources = [database]
                after_sources.extend(
                    path
                    for path in (Path(f"{database}-wal"), Path(f"{database}-shm"))
                    if path.exists()
                )
                if set(after_sources) == set(sources) and all(
                    path.exists() and _signature(path) == before[path]
                    for path in sources
                ):
                    copied = True
                    break
            if not copied:
                raise PersonalWechatError(
                    f"个人微信正在写入，未能形成稳定只读副本：{relative}"
                )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


def _sql_literal(value: str) -> str:
    return value.replace("'", "''")


def derive_database_key(source: Path, account_key_hex: str) -> str:
    if len(account_key_hex) != 64 or any(
        character not in "0123456789abcdefABCDEF" for character in account_key_hex
    ):
        raise PersonalWechatKeyError("个人微信数据库密钥格式无效")
    with source.open("rb") as database:
        salt = database.read(16)
    if len(salt) != 16:
        raise PersonalWechatUnsupportedError("个人微信数据库文件不完整")
    return hashlib.pbkdf2_hmac(
        "sha512",
        bytes.fromhex(account_key_hex),
        salt,
        256_000,
        dklen=32,
    ).hex()


def probe_database_key(
    source: Path,
    key_hex: str,
    sqlcipher: Path = SQLCIPHER,
) -> bool:
    if not sqlcipher.is_file():
        raise PersonalWechatUnsupportedError("本机尚未安装个人微信只读组件")
    database_key = derive_database_key(source, key_hex)
    commands = "\n".join(
        (
            ".bail on",
            f"PRAGMA key = \"x'{database_key}'\";",
            "PRAGMA kdf_iter = 1;",
            "PRAGMA cipher_compatibility = 4;",
            "PRAGMA cipher_page_size = 4096;",
            "SELECT 'WORKBENCH_KEY_OK:' || count(*) FROM sqlite_master;",
            ".quit",
        )
    )
    completed = subprocess.run(
        [str(sqlcipher), str(source)],
        input=commands,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.returncode == 0 and "WORKBENCH_KEY_OK:" in completed.stdout


def decrypt_database(
    source: Path,
    destination: Path,
    key_hex: str,
    sqlcipher: Path = SQLCIPHER,
) -> None:
    if not sqlcipher.is_file():
        raise PersonalWechatUnsupportedError("本机尚未安装个人微信只读组件")
    database_key = derive_database_key(source, key_hex)
    destination.parent.mkdir(parents=True, exist_ok=True)
    commands = "\n".join(
        (
            f"PRAGMA key = \"x'{database_key}'\";",
            "PRAGMA kdf_iter = 1;",
            "PRAGMA cipher_compatibility = 4;",
            "PRAGMA cipher_page_size = 4096;",
            "SELECT count(*) FROM sqlite_master;",
            f"ATTACH DATABASE '{_sql_literal(str(destination))}' AS plaintext KEY '';",
            "SELECT sqlcipher_export('plaintext');",
            "DETACH DATABASE plaintext;",
            ".quit",
        )
    )
    completed = subprocess.run(
        [str(sqlcipher), str(source)],
        input=commands,
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file():
        destination.unlink(missing_ok=True)
        raise PersonalWechatKeyError("个人微信数据库密钥未通过校验")
    try:
        connection = sqlite3.connect(f"file:{destination}?mode=ro", uri=True)
        try:
            ok = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        ok = False
    if not ok:
        destination.unlink(missing_ok=True)
        raise PersonalWechatKeyError("个人微信数据库密钥未通过校验")
    os.chmod(destination, 0o600)


@contextmanager
def decrypted_dataset(
    dataset: WechatDataset,
    key_loader: Callable[[str, str], str],
    sqlcipher: Path = SQLCIPHER,
) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="finance-workbench-wechat-") as temporary:
        private = Path(temporary)
        os.chmod(private, 0o700)
        encrypted = snapshot_dataset(dataset, private / "encrypted")
        clear = private / "clear"
        for source in dataset.databases:
            relative = source.relative_to(dataset.root)
            try:
                key = key_loader(dataset.account, relative.as_posix())
            except (KeyError, FileNotFoundError) as error:
                raise PersonalWechatKeyError(
                    "个人微信密钥不完整，需要重新配置"
                ) from error
            if not key:
                raise PersonalWechatKeyError("个人微信密钥不完整，需要重新配置")
            decrypt_database(encrypted / relative, clear / relative, key, sqlcipher)
        yield clear
