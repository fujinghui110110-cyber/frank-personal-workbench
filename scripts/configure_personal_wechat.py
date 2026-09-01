from __future__ import annotations

import tempfile
from getpass import getpass
from pathlib import Path

from scripts.personal_wechat_crypto import (
    SQLCIPHER,
    WechatDataset,
    discover_dataset,
    probe_database_key,
    snapshot_dataset,
)
from scripts.personal_wechat_keys import load_key, store_keys, validate_key


def configure_dataset(dataset: WechatDataset, sqlcipher: Path = SQLCIPHER) -> int:
    with tempfile.TemporaryDirectory(
        prefix="finance-workbench-wechat-config-"
    ) as temporary:
        private = Path(temporary)
        encrypted = snapshot_dataset(dataset, private / "encrypted")
        configured: list[tuple[str, str]] = []
        missing: list[tuple[str, Path]] = []
        for source in dataset.databases:
            label = source.relative_to(dataset.root).as_posix()
            existing = load_key(dataset.account, label)
            if existing and probe_database_key(
                encrypted / label, existing, sqlcipher
            ):
                configured.append((label, existing))
            else:
                missing.append((label, encrypted / label))

        if missing:
            key = validate_key(
                getpass("CipherTalk 数据库连接密钥（不会显示）：")
            )
            matched = [
                (label, key)
                for label, source in missing
                if probe_database_key(source, key, sqlcipher)
            ]
            total_matched = len(configured) + len(matched)
            total = len(dataset.databases)
            print(f"只读校验结果：已匹配 {total_matched}/{total} 个数据库。")
            if total_matched != total:
                raise RuntimeError(
                    f"CipherTalk 连接密钥只匹配 {total_matched}/{total} 个数据库。"
                    "这不代表输入错误；该密钥已按微信 4.x 的逐库派生方式校验，"
                    "当前微信版本或部分数据库的加密参数可能仍不兼容。"
                    "工作台未启用个人微信直读，现有 CipherTalk 读取方式保持不变。"
                )
            configured.extend(matched)

        store_keys(dataset.account, configured)
    return len(configured)


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
