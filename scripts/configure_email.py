from __future__ import annotations

import imaplib
import json
import os
import pty
import select
import ssl
import termios
import time

from scripts.email_sync import CONFIG_PATH, KEYCHAIN_SERVICE, explain_imap_login_error


HOSTS = {
    "qq.com": "imap.qq.com",
    "foxmail.com": "imap.qq.com",
    "126.com": "imap.126.com",
    "163.com": "imap.163.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "gmail.com": "imap.gmail.com",
}


def _save_keychain_secret(
    address: str,
    password: str,
    *,
    service: str = KEYCHAIN_SERVICE,
) -> None:
    arguments = [
        "/usr/bin/security",
        "add-generic-password",
        "-U",
        "-s",
        service,
        "-a",
        address,
        "-w",
    ]
    pid, terminal = pty.fork()
    if pid == 0:
        os.execv(arguments[0], arguments)

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
                prompt_count = output.lower().count(b"password data for new item:")
                prompt_count += output.lower().count(b"retype password for new item:")
                if prompt_count > sent:
                    attributes = termios.tcgetattr(terminal)
                    attributes[3] &= ~(termios.ECHO | termios.ECHONL)
                    termios.tcsetattr(terminal, termios.TCSANOW, attributes)
                    os.write(terminal, password.encode("utf-8") + b"\n")
                    sent += 1
            waited, child_status = os.waitpid(pid, os.WNOHANG)
            if waited:
                status = child_status
                break
        if status is None:
            raise RuntimeError("macOS钥匙串保存超时")
        if sent != 2 or os.waitstatus_to_exitcode(status) != 0:
            raise RuntimeError("macOS钥匙串未能保存邮箱授权码")
    finally:
        os.close(terminal)
        if status is None:
            os.kill(pid, 9)
            os.waitpid(pid, 0)


def main() -> None:
    print("财务工作台邮箱接入（只读收件，不会发送邮件）")
    address = input("邮箱地址：").strip()
    if "@" not in address:
        raise SystemExit("邮箱地址格式不正确")
    domain = address.rsplit("@", 1)[1].lower()
    suggested = HOSTS.get(domain, "")
    prompt = f"IMAP 服务器 [{suggested}]：" if suggested else "IMAP 服务器："
    host = input(prompt).strip() or suggested
    if not host:
        raise SystemExit("缺少 IMAP 服务器")
    password = input("邮箱授权码（输入时会显示）：").strip()
    if not password:
        raise SystemExit("授权码不能为空")

    mailbox = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context())
    try:
        mailbox.login(address, password)
        status, _ = mailbox.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("无法只读打开收件箱")
    except imaplib.IMAP4.error as error:
        raise SystemExit(f"连接失败：{explain_imap_login_error(address, error)}") from error
    except (OSError, RuntimeError) as error:
        raise SystemExit(f"连接失败：{error}") from error
    finally:
        try:
            mailbox.logout()
        except (imaplib.IMAP4.error, OSError):
            pass

    _save_keychain_secret(address, password)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "address": address,
                "imap_host": host,
                "imap_port": 993,
                "folder": "INBOX",
                "max_messages_per_run": 80,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(CONFIG_PATH, 0o600)
    print("配置完成。授权码已保存到 macOS 钥匙串，工作台只会读取新增邮件。")


if __name__ == "__main__":
    main()
