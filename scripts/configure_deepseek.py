from __future__ import annotations

import json

from scripts.configure_email import _save_keychain_secret
from scripts.deepseek_client import (
    KEYCHAIN_ACCOUNT,
    KEYCHAIN_SERVICE,
    call_deepseek_json,
)


def main() -> None:
    print("Frank 的个人工作台 DeepSeek 接入")
    secret = input("DeepSeek API 密钥（输入时显示）：").strip()
    if not secret:
        raise SystemExit("未输入 API 密钥")
    print("正在验证密钥……")
    try:
        output = call_deepseek_json(
            '只返回 {"ok": true}',
            60,
            {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            api_key=secret,
        )
        if json.loads(output).get("ok") is not True:
            raise RuntimeError("DeepSeek 验证结果不正确，请重试")
        _save_keychain_secret(
            KEYCHAIN_ACCOUNT,
            secret,
            service=KEYCHAIN_SERVICE,
        )
    except (RuntimeError, json.JSONDecodeError) as error:
        raise SystemExit(f"配置失败：{error}") from None
    print("配置完成。DeepSeek 密钥已保存到 macOS 钥匙串。")
    print("现在可以回到工作台点击“让贾维斯整理”。")


if __name__ == "__main__":
    main()
