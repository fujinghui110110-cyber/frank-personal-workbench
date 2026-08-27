from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import queue
import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from Crypto.Cipher import AES


APP_PATH = Path("/Applications/企业微信.app")
DATA_ROOT = Path.home() / (
    "Library/Containers/com.tencent.WeWorkMac/Data/Library/Application Support/WXWork/Data"
)
VAULT_ROOT = Path.home() / "Library/Application Support/finance-workbench/wecom"
SUPPORTED_BUILD = "99905"
PAGE_CRYPT_OFFSET = 0x278870
SQLITE_HEADER = b"SQLite format 3\x00"


def app_version() -> tuple[str, str]:
    with (APP_PATH / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    return (
        str(info.get("CFBundleShortVersionString") or ""),
        str(info.get("CFBundleVersion") or ""),
    )


def discover_dataset() -> Path:
    candidates = [
        path.parent
        for path in DATA_ROOT.glob("*/Data/message.db")
        if (path.parent / "session.db").is_file() and (path.parent / "user.db").is_file()
    ]
    if not candidates:
        raise FileNotFoundError("未找到当前企业微信账号的本地消息库")
    return max(candidates, key=lambda path: (path / "message.db").stat().st_mtime)


def snapshot_source(dataset: Path) -> Path:
    backups = [
        path
        for path in dataset.parent.parent.glob("Backup/*/Data")
        if all((path / name).is_file() for name in ("message.db", "session.db", "user.db"))
    ]
    return max(backups, key=lambda path: (path / "message.db").stat().st_mtime) if backups else dataset


def page_size(header: bytes) -> int:
    size = int.from_bytes(header[16:18], "big")
    return 65536 if size == 1 else size


def _page_iv(page: int) -> bytes:
    z = page + 1
    values: list[int] = []
    for _ in range(4):
        q = z // 52774
        z = 40692 * (z - 52774 * q) - 3791 * q
        if z < 0:
            z += 2147483399
        values.append(z)
    return hashlib.md5(b"".join(value.to_bytes(4, "little") for value in values)).digest()


def decrypt_page(raw: bytes, key: bytes, page: int) -> bytes:
    if len(key) != 16 or len(raw) % 16:
        raise ValueError("企业微信数据库密钥或页面长度无效")
    result = bytearray(raw)
    offset = 0
    clear_header = bytes(result[16:24]) if page == 1 else b""
    if page == 1:
        size = page_size(result)
        if (
            512 <= size <= 65536
            and size & (size - 1) == 0
            and clear_header[5:] == b"\x40\x20\x20"
        ):
            result[16:24] = result[8:16]
            offset = 16
    page_key = hashlib.md5(key + page.to_bytes(4, "little") + b"sAlT").digest()
    result[offset:] = AES.new(page_key, AES.MODE_CBC, _page_iv(page)).decrypt(
        bytes(result[offset:])
    )
    if page == 1 and offset and bytes(result[16:24]) == clear_header:
        result[:16] = SQLITE_HEADER
    return bytes(result)


def validates_key(key: bytes, dataset: Path) -> bool:
    for name in ("message.db", "session.db", "user.db"):
        path = dataset / name
        with path.open("rb") as stream:
            first = stream.read(65536)
        size = page_size(first)
        if size not in {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}:
            return False
        try:
            decoded = decrypt_page(first[:size], key, 1)
        except (ValueError, OSError):
            return False
        if not decoded.startswith(SQLITE_HEADER):
            return False
    return True


def _frida_agent() -> str:
    return f"""
'use strict';
const sent = new Set();
function emit(pointer, source) {{
  if (!pointer || pointer.isNull()) return;
  try {{
    const bytes = new Uint8Array(pointer.readByteArray(16));
    const hex = Array.from(bytes).map(value => ('0' + value.toString(16)).slice(-2)).join('');
    if (sent.has(hex)) return;
    sent.add(hex);
    send({{type: 'candidate', key: hex, source: source}});
  }} catch (_) {{}}
}}
function exported(name) {{
  try {{ return Module.findGlobalExportByName(name); }} catch (_) {{ return null; }}
}}
const main = Process.enumerateModules().find(module => module.path.endsWith('/Contents/MacOS/企业微信'));
if (main && main.size > {PAGE_CRYPT_OFFSET}) {{
  Interceptor.attach(main.base.add({PAGE_CRYPT_OFFSET}), {{onEnter(args) {{ emit(args[3], 'page'); }}}});
  send({{type: 'hook', name: 'page'}});
}}
const md5 = exported('CC_MD5');
if (md5) {{
  Interceptor.attach(md5, {{onEnter(args) {{
    if (args[1].toUInt32() !== 24) return;
    try {{
      const tail = new Uint8Array(args[0].add(20).readByteArray(4));
      if (tail[0] === 0x73 && tail[1] === 0x41 && tail[2] === 0x6c && tail[3] === 0x54) emit(args[0], 'md5');
    }} catch (_) {{}}
  }}}});
  send({{type: 'hook', name: 'md5'}});
}}
"""


def _wecom_pid() -> int:
    output = subprocess.check_output(
        ["pgrep", "-f", "/Applications/企业微信.app/Contents/MacOS/企业微信"],
        text=True,
    )
    return int(output.splitlines()[0])


def save_key(key: bytes, dataset: Path) -> Path:
    private = VAULT_ROOT / "private"
    private.mkdir(parents=True, exist_ok=True)
    os.chmod(private, 0o700)
    account = dataset.parent.name
    destination = private / f"key-{hashlib.sha256(account.encode()).hexdigest()[:16]}.json"
    payload = {
        "version": 1,
        "account_fingerprint": hashlib.sha256(account.encode()).hexdigest()[:24],
        "app_build": app_version()[1],
        "key": key.hex(),
        "validated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    temporary = destination.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
    return destination


def capture_key(duration: int = 60) -> Path:
    _, build = app_version()
    if build != SUPPORTED_BUILD:
        raise RuntimeError("企业微信升级后需要重新验证")
    dataset = discover_dataset()
    import frida

    events: queue.Queue[dict[str, Any]] = queue.Queue()
    session = frida.get_local_device().attach(_wecom_pid())
    script = session.create_script(_frida_agent())
    script.on(
        "message",
        lambda message, _: events.put(message.get("payload") or {})
        if message.get("type") == "send"
        else None,
    )
    script.load()
    deadline = time.monotonic() + max(5, duration)
    try:
        while time.monotonic() < deadline:
            try:
                event = events.get(timeout=min(0.5, deadline - time.monotonic()))
            except queue.Empty:
                continue
            if event.get("type") != "candidate":
                continue
            try:
                candidate = bytes.fromhex(str(event.get("key") or ""))
            except ValueError:
                continue
            if validates_key(candidate, dataset):
                return save_key(candidate, dataset)
    finally:
        script.unload()
        session.detach()
    raise RuntimeError("捕获期间没有取得可通过数据库校验的密钥")


def load_key(dataset: Path) -> bytes:
    account = dataset.parent.name
    path = VAULT_ROOT / "private" / f"key-{hashlib.sha256(account.encode()).hexdigest()[:16]}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("app_build") != app_version()[1]:
        raise RuntimeError("企业微信升级后需要重新验证")
    key = bytes.fromhex(str(payload.get("key") or ""))
    if not validates_key(key, dataset):
        raise RuntimeError("已保存的企业微信密钥未通过数据库校验")
    return key


def decrypt_database(source: Path, destination: Path, key: bytes) -> None:
    with source.open("rb") as stream:
        header = stream.read(24)
    size = page_size(header)
    if size not in {512, 1024, 2048, 4096, 8192, 16384, 32768, 65536}:
        raise RuntimeError("企业微信数据库页面格式无法识别")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        page = 1
        while chunk := incoming.read(size):
            if len(chunk) != size:
                raise RuntimeError("企业微信数据库副本页面不完整")
            outgoing.write(decrypt_page(chunk, key, page))
            page += 1
    os.chmod(destination, 0o600)


def create_snapshot() -> Path:
    dataset = discover_dataset()
    source = snapshot_source(dataset)
    key = load_key(dataset)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    snapshot = VAULT_ROOT / "snapshots" / stamp
    snapshot.mkdir(parents=True, exist_ok=False)
    os.chmod(snapshot, 0o700)
    manifest: dict[str, Any] = {"created_at": stamp, "files": {}}
    try:
        for name in ("message.db", "session.db", "user.db"):
            original = source / name
            target = snapshot / name
            before = hashlib.sha256(original.read_bytes()).hexdigest()
            decrypt_database(original, target, key)
            connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            try:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                tables = [
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    )
                ]
            finally:
                connection.close()
            after = hashlib.sha256(original.read_bytes()).hexdigest()
            if integrity != "ok" or before != after:
                raise RuntimeError("企业微信数据库只读快照校验失败")
            manifest["files"][name] = {"sha256": before, "tables": tables}
        manifest_path = snapshot / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
    except Exception:
        for path in snapshot.glob("*"):
            path.unlink(missing_ok=True)
        snapshot.rmdir()
        raise
    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    capture = subparsers.add_parser("capture")
    capture.add_argument("--duration", type=int, default=60)
    subparsers.add_parser("decrypt")
    args = parser.parse_args()
    if args.command == "status":
        version, build = app_version()
        dataset = discover_dataset()
        print(json.dumps({"version": version, "build": build, "database_count": 3, "encrypted": not (dataset / "message.db").read_bytes().startswith(SQLITE_HEADER)}, ensure_ascii=False))
        return 0
    if args.command == "capture":
        saved = capture_key(args.duration)
        print(f"已保存通过数据库校验的企业微信密钥：{saved}")
        return 0
    snapshot = create_snapshot()
    print(f"已创建企业微信只读快照：{snapshot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
