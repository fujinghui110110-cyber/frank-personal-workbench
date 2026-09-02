from __future__ import annotations

import tempfile
from getpass import getpass
from pathlib import Path
import subprocess
import sys
from typing import Callable

from scripts.personal_wechat_crypto import (
    SQLCIPHER,
    WechatDataset,
    discover_dataset,
    probe_database_key,
    snapshot_dataset,
)
from scripts.personal_wechat_keys import load_key, store_keys, validate_key
from scripts.personal_wechat_keys import (
    load_image_keys,
    store_image_keys,
    validate_image_aes_key,
    validate_image_xor_key,
)
from scripts.personal_wechat_sync import probe_image_keys


def configure_dataset(
    dataset: WechatDataset,
    sqlcipher: Path = SQLCIPHER,
    prompt: Callable[[str], str] | None = None,
) -> int:
    prompt = prompt or getpass
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
                prompt("个人微信数据库连接密钥（不会显示）：")
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
                    f"个人微信数据库连接密钥只匹配 {total_matched}/{total} 个数据库。"
                    "这不代表输入错误；该密钥已按微信 4.x 的逐库派生方式校验，"
                    "当前微信版本或部分数据库的加密参数可能仍不兼容。"
                    "个人微信直读配置未更新，当前读取方式保持不变。"
                )
            configured.extend(matched)

        store_keys(dataset.account, configured)
    return len(configured)


def configure_image_keys(
    dataset: WechatDataset,
    prompt: Callable[[str], str] | None = None,
) -> bool:
    prompt = prompt or getpass
    candidates = sorted(
        (
            path
            for path in (dataset.root.parent / "msg").rglob("*.dat")
            if path.is_file()
        ),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not candidates:
        return False
    existing = load_image_keys(dataset.account)
    if existing and probe_image_keys(candidates, existing):
        return True
    xor_key = validate_image_xor_key(prompt("图片 XOR 密钥（例如 0x53，不会显示）："))
    aes_text = prompt("图片 AES 密钥（16 位字符，不会显示）：")
    aes_key = validate_image_aes_key(aes_text)
    if not probe_image_keys(candidates, (xor_key, aes_key)):
        raise RuntimeError(
            "图片密钥未通过只读校验。个人微信直读配置未更新，"
            "当前读取方式保持不变。"
        )
    store_image_keys(dataset.account, xor_key, aes_text)
    return True


def _macos_hidden_prompt(message: str) -> str:
    script = (
        'set response to display dialog "'
        + message.replace('"', '\\"')
        + '" default answer "" with hidden answer buttons {"取消", "确定"} '
        'default button "确定" cancel button "取消"\n'
        "return text returned of response"
    )
    completed = subprocess.run(
        ["/usr/bin/osascript", "-"],
        input=script,
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("已取消个人微信密钥配置")
    return completed.stdout.rstrip("\r\n")


def main() -> None:
    print("个人微信数据库密钥配置")
    gui = "--gui" in sys.argv[1:]
    try:
        prompt = _macos_hidden_prompt if gui else getpass
        dataset = discover_dataset()
        configured = configure_dataset(dataset, prompt=prompt)
        images_configured = configure_image_keys(dataset, prompt=prompt)
    except (RuntimeError, ValueError) as error:
        raise SystemExit(f"配置失败：{error}") from None
    print(f"配置完成，已验证 {configured} 个数据库。密钥仅保存在 macOS 钥匙串。")
    if images_configured:
        print("图片密钥已通过只读校验并保存到 macOS 钥匙串。")
    if gui:
        print("现有密钥已通过校验时不会重复弹出输入窗口。")


if __name__ == "__main__":
    main()
