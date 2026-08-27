from __future__ import annotations

import json

from app.analyzer import analyze_material


def analyze(payload: dict, filename: str) -> dict:
    return analyze_material(
        {
            "filename": filename,
            "content_type": "application/json",
            "source_type": "wechat_markdown",
            "text_note": "",
        },
        json.dumps(payload, ensure_ascii=False).encode(),
    )


def test_weflow_export_uses_messages_instead_of_raw_json() -> None:
    result = analyze(
        {
            "weflow": {"version": "1.0.3"},
            "session": {"displayName": "预算对齐群", "wxid": "secret@chatroom"},
            "avatars": {"secret": "https://example.invalid/avatar"},
            "messages": [
                {"type": "系统消息", "content": "邀请成员加入", "senderDisplayName": "系统"},
                {
                    "type": "文本消息",
                    "content": "需要明天完成核对，涉及金额10万元",
                    "senderDisplayName": "财务同事",
                },
                {"type": "图片消息", "content": "[图片]", "senderDisplayName": "财务同事"},
                {"type": "文本消息", "content": "收到", "senderDisplayName": "负责人"},
            ],
        },
        "群聊_预算对齐群.json",
    )

    assert result["matter_title"] == "预算对齐群"
    assert "需要明天完成核对" in result["summary"]
    assert "weflow" not in result["summary"]
    assert "wxid" not in result["summary"]
    assert result["actions"][0]["title"] == "需要明天完成核对，涉及金额10万元"
    assert all(len(item["title"]) <= 72 for item in result["actions"])
    assert result["facts"][0]["source_locator"] == "聊天消息 2"


def test_ciphertalk_export_keeps_text_and_quote_messages_only() -> None:
    result = analyze(
        {
            "chatlab": {"version": "0.0.2"},
            "meta": {"name": "预测更新", "groupAvatar": "https://example.invalid/avatar"},
            "members": [{"wxid": "secret"}],
            "messages": [
                {"type": 80, "content": "secret@chatroom:", "accountName": "系统"},
                {"type": 0, "content": "本周预测需要调整", "accountName": "运营"},
                {"type": 25, "content": "请确认主营收入口径", "accountName": "财务"},
                {"type": 1, "content": "[图片]", "accountName": "财务"},
            ],
        },
        "预测更新.json",
    )

    assert result["matter_title"] == "预测更新"
    assert "本周预测需要调整" in result["summary"]
    assert "请确认主营收入口径" in result["summary"]
    assert "chatroom" not in result["summary"]
    assert all('"content"' not in item["title"] for item in result["actions"])
