from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from getpass import getpass

from scripts.configure_email import _save_keychain_secret
from scripts.email_sync import CONFIG_PATH, KEYCHAIN_SERVICE, _ews_account


DEFAULT_EWS_URL = "https://owacn.cohl.com/EWS/Exchange.asmx"


def main() -> None:
    print("财务工作台 EWS 邮箱接入（只读收件，不会发送邮件）")
    address = input("集团邮箱地址：").strip()
    if "@" not in address:
        raise SystemExit("邮箱地址格式不正确")
    username = input(f"NTLM 用户名 [{address}]：").strip() or address
    endpoint = input(f"EWS 地址 [{DEFAULT_EWS_URL}]：").strip() or DEFAULT_EWS_URL
    password = getpass("集团邮箱密码（不会显示）：").strip()
    if not password:
        raise SystemExit("邮箱密码不能为空")

    config = {
        "protocol": "ews",
        "address": address,
        "ews_username": username,
        "ews_url": endpoint,
        "folder": "INBOX",
        "sync_days": 7,
        "max_messages_per_run": 80,
    }
    try:
        account = _ews_account(config, password)
        next(iter(account.inbox.all().only("id")[:1]), None)
    except Exception as error:
        raise SystemExit(
            "EWS 只读验证失败，请检查邮箱地址、NTLM 用户名、密码、EWS 地址和权限。"
        ) from error

    if CONFIG_PATH.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_path = CONFIG_PATH.with_name(f"email.json.before-ews-{stamp}")
        shutil.copy2(CONFIG_PATH, backup_path)
        print(f"原邮箱配置已保留：{backup_path}")
    _save_keychain_secret(address, password, service=KEYCHAIN_SERVICE)
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.chmod(CONFIG_PATH, 0o600)
    print(
        "EWS 配置完成。密码已保存到 macOS 钥匙串；本次未启动同步。"
        "确认后请重启现有 Worker，加载 EWS 适配。"
    )


if __name__ == "__main__":
    main()
