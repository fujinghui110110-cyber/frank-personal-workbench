from __future__ import annotations

import argparse
from getpass import getpass
import json
import os
import pty
import re
import select
import subprocess
import termios
import time
from collections.abc import Sequence
from typing import Any


_SECURITY = "/usr/bin/security"
KEYCHAIN_SERVICE = "finance-workbench-personal-wechat"
_INDEX_SERVICE = f"{KEYCHAIN_SERVICE}-labels"
_ITEM_NOT_FOUND = 44
_KEY_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
_KEYCHAIN_PROMPTS = (
    b"password data for new item:",
    b"retype password for new item:",
    b"retype password new item:",
)


def validate_label(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}不能为空")
    value = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name}不能包含控制字符")
    return value


_label = validate_label


def validate_key(value: str) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("数据库密钥必须是64位十六进制字符串")
    return key.lower()


def _run_security_prompt(
    arguments: list[str], secret: str
) -> subprocess.CompletedProcess[str]:
    command = [_SECURITY, *arguments]
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(command[0], command)

    status: int | None = None
    sent = 0
    output = b""
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([terminal], [], [], 0.25)
            if ready:
                try:
                    chunk = os.read(terminal, 1024)
                except OSError:
                    _, status = os.waitpid(pid, 0)
                    break
                if not chunk:
                    _, status = os.waitpid(pid, 0)
                    break
                output = (output + chunk)[-512:]
                prompt_count = sum(
                    output.lower().count(prompt) for prompt in _KEYCHAIN_PROMPTS
                )
                while sent < min(prompt_count, 2):
                    attributes = termios.tcgetattr(terminal)
                    attributes[3] &= ~(termios.ECHO | termios.ECHONL)
                    termios.tcsetattr(terminal, termios.TCSANOW, attributes)
                    os.write(terminal, secret.encode("utf-8") + b"\n")
                    sent += 1
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = child_status
                break
        if status is None:
            raise RuntimeError("macOS钥匙串保存超时")
        exit_code = os.waitstatus_to_exitcode(status)
        returncode = exit_code if sent == 2 else 1
        return subprocess.CompletedProcess(command, returncode, "", "")
    finally:
        try:
            os.close(terminal)
        except OSError:
            pass
        if status is None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)


def _run_security(
    arguments: list[str], secret: str | None = None
) -> subprocess.CompletedProcess[str]:
    options: dict[str, Any] = {
        "text": True,
        "capture_output": True,
        "check": False,
        "timeout": 10,
    }
    try:
        if secret is not None and os.isatty(0):
            return _run_security_prompt(arguments, secret)
        if secret is not None:
            # `security` asks for the value twice when -w has no command-line value.
            options["input"] = f"{secret}\n{secret}\n"
        return subprocess.run([_SECURITY, *arguments], **options)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("无法访问 macOS 钥匙串") from error


def _item_account(account: str, database: str) -> str:
    return json.dumps([account, database], ensure_ascii=True, separators=(",", ":"))


def _find(service: str, account: str) -> subprocess.CompletedProcess[str]:
    return _run_security(["find-generic-password", "-s", service, "-a", account, "-w"])


def _load_labels(account: str) -> list[str]:
    result = _find(_INDEX_SERVICE, account)
    if result.returncode == _ITEM_NOT_FOUND:
        return []
    if result.returncode != 0:
        raise RuntimeError("无法读取 macOS 钥匙串")
    raw = result.stdout.strip() if isinstance(result.stdout, str) else ""
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError("macOS 钥匙串中的数据库标签索引格式无效") from error
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise RuntimeError("macOS 钥匙串中的数据库标签索引格式无效")
    return sorted({_label(value, "数据库标签") for value in values})


def _store_item(service: str, account: str, value: str) -> None:
    result = _run_security(
        [
            "add-generic-password",
            "-U",
            "-s",
            service,
            "-a",
            account,
            "-w",
        ],
        secret=value,
    )
    if result.returncode != 0:
        raise RuntimeError("macOS 钥匙串保存失败")


def _delete_item(service: str, account: str) -> None:
    result = _run_security(["delete-generic-password", "-s", service, "-a", account])
    if result.returncode not in {0, _ITEM_NOT_FOUND}:
        raise RuntimeError("macOS 钥匙串删除失败")


def load_key(account: str, database: str) -> str | None:
    account = _label(account, "微信账号")
    database = _label(database, "数据库标签")
    result = _find(KEYCHAIN_SERVICE, _item_account(account, database))
    if result.returncode == _ITEM_NOT_FOUND:
        return None
    if result.returncode != 0:
        raise RuntimeError("无法读取 macOS 钥匙串")
    try:
        return validate_key(result.stdout if isinstance(result.stdout, str) else "")
    except ValueError as error:
        raise RuntimeError("macOS 钥匙串中的数据库密钥格式无效") from error


def store_key(account: str, database: str, key: str) -> None:
    account = _label(account, "微信账号")
    database = _label(database, "数据库标签")
    key = validate_key(key)
    labels = _load_labels(account)
    _store_item(KEYCHAIN_SERVICE, _item_account(account, database), key)
    if database not in labels:
        labels.append(database)
        _store_item(
            _INDEX_SERVICE,
            account,
            json.dumps(sorted(labels), ensure_ascii=True, separators=(",", ":")),
        )


def store_keys(account: str, database_keys: Sequence[tuple[str, str]]) -> None:
    account = _label(account, "微信账号")
    items = sorted(
        {
            _label(database, "数据库标签"): validate_key(key)
            for database, key in database_keys
        }.items()
    )
    if not items:
        return
    for database, key in items:
        _store_item(KEYCHAIN_SERVICE, _item_account(account, database), key)
    _store_item(
        _INDEX_SERVICE,
        account,
        json.dumps(
            [database for database, _ in items],
            ensure_ascii=True,
            separators=(",", ":"),
        ),
    )


def delete_key(account: str, database: str) -> None:
    account = _label(account, "微信账号")
    database = _label(database, "数据库标签")
    _delete_item(KEYCHAIN_SERVICE, _item_account(account, database))
    labels = _load_labels(account)
    if database not in labels:
        return
    labels.remove(database)
    if labels:
        _store_item(
            _INDEX_SERVICE,
            account,
            json.dumps(labels, ensure_ascii=True, separators=(",", ":")),
        )
    else:
        _delete_item(_INDEX_SERVICE, account)


def list_database_labels(account: str) -> list[str]:
    return _load_labels(_label(account, "微信账号"))


def list_databases(account: str) -> list[str]:
    return list_database_labels(account)


def key_status(account: str, database: str) -> bool:
    return load_key(account, database) is not None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理个人微信 SQLCipher 密钥")
    commands = parser.add_subparsers(dest="command", required=True)

    set_command = commands.add_parser("set", help="安全输入并保存一个密钥")
    set_command.add_argument("account", help="微信账号标识")
    set_command.add_argument("database", help="数据库标签")

    list_command = commands.add_parser("list", help="列出已配置的数据库标签")
    list_command.add_argument("account", help="微信账号标识")

    status_command = commands.add_parser("status", help="检查一个密钥是否存在")
    status_command.add_argument("account", help="微信账号标识")
    status_command.add_argument("database", help="数据库标签")

    delete_command = commands.add_parser("delete", help="删除一个密钥")
    delete_command.add_argument("account", help="微信账号标识")
    delete_command.add_argument("database", help="数据库标签")
    delete_command.add_argument("--yes", action="store_true", help="确认删除，避免误删")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "set":
            store_key(
                args.account,
                args.database,
                validate_key(getpass("数据库密钥（64位十六进制，不会显示）：")),
            )
            print("数据库密钥已保存到 macOS 钥匙串。")
        elif args.command == "list":
            labels = list_database_labels(args.account)
            print("\n".join(labels) if labels else "未配置数据库密钥")
        elif args.command == "status":
            print("已配置" if key_status(args.account, args.database) else "未配置")
        elif args.command == "delete":
            if not args.yes:
                parser.error("delete 必须显式提供 --yes")
            delete_key(args.account, args.database)
            print("数据库密钥已删除。")
    except (RuntimeError, ValueError) as error:
        parser.exit(1, f"操作失败：{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
