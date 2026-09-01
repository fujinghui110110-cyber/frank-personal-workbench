from __future__ import annotations

import shutil
import tempfile
from getpass import getpass
from pathlib import Path

from scripts.personal_wechat_crypto import (
    SQLCIPHER,
    WechatDataset,
    decrypt_database,
    discover_dataset,
    snapshot_dataset,
)
from scripts.personal_wechat_keys import load_key, store_key, validate_key


def configure_dataset(dataset: WechatDataset, sqlcipher: Path = SQLCIPHER) -> int:
    configured = 0
    with tempfile.TemporaryDirectory(
        prefix="finance-workbench-wechat-config-"
    ) as temporary:
        private = Path(temporary)
        encrypted = snapshot_dataset(dataset, private / "encrypted")
        for source in dataset.databases:
            label = source.relative_to(dataset.root).as_posix()
            clear = private / "clear" / label
            existing = load_key(dataset.account, label)
            if existing:
                try:
                    decrypt_database(encrypted / label, clear, existing, sqlcipher)
                    clear.unlink(missing_ok=True)
                    configured += 1
                    print(f"已验证：{label}")
                    continue
                except RuntimeError:
                    clear.unlink(missing_ok=True)
            key = validate_key(getpass(f"{label} 的数据库密钥（不会显示）："))
            decrypt_database(encrypted / label, clear, key, sqlcipher)
            clear.unlink(missing_ok=True)
            store_key(dataset.account, label, key)
            configured += 1
            print(f"已验证并保存：{label}")
        shutil.rmtree(private / "clear", ignore_errors=True)
    return configured


def main() -> None:
    print("个人微信数据库密钥配置")
    try:
        dataset = discover_dataset()
        configured = configure_dataset(dataset)
    except (RuntimeError, ValueError) as error:
        raise SystemExit(f"配置失败：{error}") from None
    print(f"配置完成，已验证 {configured} 个数据库。密钥仅保存在 macOS 钥匙串。")


if __name__ == "__main__":
    main()
