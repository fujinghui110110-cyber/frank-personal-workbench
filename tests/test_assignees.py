from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from scripts.workbuddy_analysis import (
    _ASSIGNEE_GUIDANCE,
    analyze_with_workbuddy,
    classify_wechat_with_workbuddy,
)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "workbench",
        owner_passcode="owner-pass",
        session_secret="test-session-secret",
        owner_token="owner-token",
        worker_token="worker-token",
        mcp_token="mcp-token",
        max_upload_bytes=4096,
        lease_seconds=30,
    )


@pytest.fixture
def client(settings: Settings):
    with TestClient(create_app(settings)) as test_client:
        test_client.post("/api/auth/login", json={"passcode": "owner-pass"})
        yield test_client


def worker_headers() -> dict[str, str]:
    return {"Authorization": "Bearer worker-token"}


def _people_by_name(client: TestClient) -> dict[str, dict]:
    response = client.get("/api/people")
    assert response.status_code == 200, response.text
    return {item["display_name"]: item for item in response.json()}


def _complete_action(client: TestClient, action: dict, *, key: str = "assignee-flow") -> dict:
    received = client.post(
        "/api/intake",
        data={"source_type": "text", "text_note": action.get("detail") or action["title"]},
        headers={"Idempotency-Key": key},
    )
    assert received.status_code in {200, 201}, received.text
    released = client.post("/api/analysis/run")
    assert released.status_code == 200, released.text
    claimed = client.post(
        "/api/jobs/claim",
        json={"worker_id": "assignee-test"},
        headers=worker_headers(),
    ).json()["job"]
    lease = {"worker_id": "assignee-test", "lease_token": claimed["lease_token"]}
    started = client.post(
        f"/api/jobs/{claimed['id']}/start", json=lease, headers=worker_headers()
    )
    assert started.status_code == 200, started.text
    completed = client.post(
        f"/api/jobs/{claimed['id']}/complete",
        json={
            **lease,
            "result": {
                "matter_title": "人员关联测试",
                "summary": "测试负责人建议。",
                "actions": [action],
            },
        },
        headers=worker_headers(),
    )
    assert completed.status_code == 200, completed.text
    return completed.json()["actions"][0]


def test_people_seed_is_idempotent_and_contains_required_aliases(client: TestClient) -> None:
    first = client.get("/api/people")
    second = client.get("/api/people")

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()

    people = {item["display_name"]: item for item in first.json()}
    assert set(people) == {"我自己", "孙庆", "李静", "欧波", "冯李香", "陈贞婷", "潘朝荟", "朱青霞"}
    assert people["我自己"]["is_self"] is True
    assert {"Hank", "hank", "孙总"} <= set(people["孙庆"]["aliases"])
    assert {"李姐", "静姐"} <= set(people["李静"]["aliases"])
    assert {"李香", "香姐"} <= set(people["冯李香"]["aliases"])
    assert {"欧哥"} <= set(people["欧波"]["aliases"])
    assert {"阿婷"} <= set(people["陈贞婷"]["aliases"])


def test_workbench_analysis_preserves_pending_assignee_suggestions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "codebuddy"
    executable.touch()

    def fake_run(command, **kwargs):
        output = {
            "matter_id": None,
            "matter_title": "盘点表跟进",
            "summary": "需要李静明天提交盘点表。",
            "facts": [],
            "inferences": [],
            "actions": [
                {
                    "kind": "task",
                    "title": "提交盘点表",
                    "detail": "静姐明天把盘点表发过来。",
                    "owner": "待明确",
                    "due_date": "2099-01-01",
                    "assignee_suggestions": [
                        {
                            "person": "李静",
                            "detected_alias": "静姐",
                            "reason": "原话要求静姐明天发盘点表。",
                            "evidence": ["静姐明天把盘点表发过来"],
                        }
                    ],
                }
            ],
            "brief": {
                "headline": "等待盘点表",
                "what_i_did": [],
                "needs_you": "",
                "next_check_at": None,
                "next_check_reason": "",
            },
        }
        return subprocess.CompletedProcess(command, 0, json.dumps(output, ensure_ascii=False), "")

    monkeypatch.setenv("WORKBUDDY_CLI", str(executable))
    monkeypatch.setattr(
        "scripts.workbuddy_analysis.call_deepseek_json",
        lambda prompt, *_args, **_kwargs: fake_run([], input=prompt).stdout,
    )

    result = analyze_with_workbuddy(
        {"id": "mat-1", "source_type": "audio", "filename": "会议录音.m4a"},
        "静姐明天把盘点表发过来。",
        [],
    )

    assert result["actions"][0]["assignee_suggestions"][0]["person"] == "李静"
    assert result["actions"][0]["assignee_suggestions"][0]["detected_alias"] == "静姐"


def test_wechat_classifier_preserves_personal_chat_self_and_counterparty_suggestions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "codebuddy"
    executable.touch()

    def fake_call(_executable, prompt, _timeout, _schema):
        assert "对方：我来跟进" in prompt
        return {
            "classification": "relevant",
            "summary": "对方承诺继续跟进付款资料。",
            "uncertainty_reason": "",
            "confidence": 0.95,
            "evidence": ["我来跟进付款资料"],
            "extracted": {
                "matter_title": "付款资料跟进",
                "amounts": [],
                "dates": [],
                "people": ["李静"],
                "approval": "",
                "risks": [],
                "actions": [
                    {
                        "kind": "task",
                        "title": "跟进付款资料",
                        "detail": "对方说我来跟进付款资料。",
                        "owner": "李静",
                        "due_date": None,
                        "assignee_suggestions": [
                            {
                                "person": "李静",
                                "detected_alias": "私聊对方",
                                "reason": "个人微信私聊中对方说我来跟进。",
                                "evidence": ["对方：我来跟进付款资料"],
                            }
                        ],
                    }
                ],
            },
        }

    monkeypatch.setenv("WORKBUDDY_CLI", str(executable))
    monkeypatch.setattr("scripts.workbuddy_analysis._call_workbuddy_json", fake_call)

    result = classify_wechat_with_workbuddy({}, "对方：我来跟进付款资料")

    assert result["extracted"]["actions"][0]["assignee_suggestions"][0]["person"] == "李静"


def test_no_action_greeting_does_not_create_assignee_suggestion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "codebuddy"
    executable.touch()

    def fake_call(_executable, _prompt, _timeout, _schema):
        return {
            "classification": "irrelevant",
            "summary": "只是寒暄。",
            "uncertainty_reason": "",
            "confidence": 0.99,
            "evidence": ["李姐好"],
            "extracted": {
                "matter_title": "",
                "amounts": [],
                "dates": [],
                "people": ["李静"],
                "approval": "",
                "risks": [],
                "actions": [],
            },
        }

    monkeypatch.setenv("WORKBUDDY_CLI", str(executable))
    monkeypatch.setattr("scripts.workbuddy_analysis._call_workbuddy_json", fake_call)

    result = classify_wechat_with_workbuddy({}, "李姐好")

    assert result["classification"] == "irrelevant"
    assert result["extracted"]["actions"] == []


def test_action_completion_creates_pending_assignee_reviews(client: TestClient) -> None:
    action = _complete_action(
        client,
        {
            "kind": "task",
            "title": "核对入库数量",
            "detail": "香姐和欧哥一起核对入库数量。",
            "owner": "待明确",
            "due_date": None,
            "assignee_suggestions": [
                {"person": "冯李香", "detected_alias": "香姐", "reason": "原话要求香姐一起核对", "evidence": ["香姐和欧哥一起核对"]},
                {"person": "欧波", "detected_alias": "欧哥", "reason": "原话要求欧哥一起核对", "evidence": ["香姐和欧哥一起核对"]},
            ],
        },
    )

    response = client.get("/api/assignee-reviews?status=pending")

    assert response.status_code == 200, response.text
    review = response.json()[0]
    assert review["action_id"] == action["id"]
    assert [item["display_name"] for item in review["suggested_people"]] == ["冯李香", "欧波"]
    assert review["status"] == "pending"


def test_pending_suggestions_are_excluded_from_person_action_lists(client: TestClient) -> None:
    people = _people_by_name(client)
    _complete_action(
        client,
        {
            "kind": "task",
            "title": "让李香继续问供应商",
            "detail": "让李香继续问供应商。",
            "owner": "待明确",
            "assignee_suggestions": [
                {"person": "冯李香", "detected_alias": "李香", "reason": "原话要求李香继续问供应商", "evidence": ["让李香继续问供应商"]}
            ],
        },
        key="pending-not-classified",
    )

    response = client.get(
        "/api/actions", params={"status": "open", "person_id": people["冯李香"]["id"]}
    )

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_confirming_multiple_assignees_classifies_one_action_for_each_person(
    client: TestClient,
) -> None:
    people = _people_by_name(client)
    action = _complete_action(
        client,
        {
            "kind": "task",
            "title": "共同核对入库",
            "detail": "香姐和欧哥一起核对。",
            "owner": "待明确",
            "assignee_suggestions": [
                {"person": "冯李香", "detected_alias": "香姐", "reason": "原话要求香姐核对", "evidence": ["香姐和欧哥一起核对"]},
                {"person": "欧波", "detected_alias": "欧哥", "reason": "原话要求欧哥核对", "evidence": ["香姐和欧哥一起核对"]},
            ],
        },
        key="confirm-multiple",
    )

    response = client.put(
        f"/api/actions/{action['id']}/assignees",
        json={"person_ids": [people["冯李香"]["id"], people["欧波"]["id"]]},
    )
    assert response.status_code == 200, response.text

    for person in ("冯李香", "欧波"):
        listed = client.get(
            "/api/actions", params={"status": "open", "person_id": people[person]["id"]}
        )
        assert listed.status_code == 200, listed.text
        assert [item["id"] for item in listed.json()] == [action["id"]]


def test_replacing_assignees_rejects_unselected_suggestions(client: TestClient) -> None:
    people = _people_by_name(client)
    action = _complete_action(
        client,
        {
            "kind": "task",
            "title": "确认付款安排",
            "detail": "这个需要 Hank 确认。",
            "owner": "待明确",
            "assignee_suggestions": [
                {"person": "孙庆", "detected_alias": "Hank", "reason": "原话要求 Hank 确认", "evidence": ["这个需要 Hank 确认"]}
            ],
        },
        key="replace-assignee",
    )

    changed = client.put(
        f"/api/actions/{action['id']}/assignees",
        json={"person_ids": [people["我自己"]["id"]]},
    )

    assert changed.status_code == 200, changed.text
    assert [item["display_name"] for item in changed.json()["assignees"]] == ["我自己"]
    pending = client.get("/api/assignee-reviews?status=pending").json()
    assert all(item["action_id"] != action["id"] for item in pending)


def test_empty_assignee_list_marks_all_suggestions_rejected(client: TestClient) -> None:
    action = _complete_action(
        client,
        {
            "kind": "task",
            "title": "近音误判",
            "detail": "会议转写疑似静姐，但不确定。",
            "owner": "待明确",
            "assignee_suggestions": [
                {"person": "李静", "detected_alias": "近音：静姐", "reason": "会议近音可能指李静", "evidence": ["近音：静姐"]}
            ],
        },
        key="clear-assignee",
    )

    response = client.put(f"/api/actions/{action['id']}/assignees", json={"person_ids": []})

    assert response.status_code == 200, response.text
    assert response.json()["assignees"] == []
    pending = client.get("/api/assignee-reviews?status=pending").json()
    assert all(item["action_id"] != action["id"] for item in pending)


def test_done_action_closes_pending_assignee_reviews(client: TestClient) -> None:
    action = _complete_action(
        client,
        {
            "kind": "task",
            "title": "整理资料",
            "detail": "阿婷整理好后发给我。",
            "owner": "待明确",
            "assignee_suggestions": [
                {"person": "陈贞婷", "detected_alias": "阿婷", "reason": "原话要求阿婷整理资料", "evidence": ["阿婷整理好后发给我"]}
            ],
        },
        key="done-closes-review",
    )

    resolved = client.post(f"/api/actions/{action['id']}/resolve", json={"status": "done"})
    pending = client.get("/api/assignee-reviews?status=pending")

    assert resolved.status_code == 200, resolved.text
    assert pending.status_code == 200, pending.text
    assert all(item["action_id"] != action["id"] for item in pending.json())


def test_legacy_owner_does_not_backfill_people_classification(client: TestClient) -> None:
    people = _people_by_name(client)
    _complete_action(
        client,
        {
            "kind": "task",
            "title": "历史 owner 测试",
            "detail": "旧字段 owner 不代表已确认人员。",
            "owner": "李静",
        },
        key="legacy-owner",
    )

    response = client.get(
        "/api/actions", params={"status": "open", "person_id": people["李静"]["id"]}
    )

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_legacy_actions_do_not_enter_the_new_people_view(
    client: TestClient, settings: Settings
) -> None:
    action = _complete_action(
        client,
        {
            "kind": "task",
            "title": "历史行动",
            "detail": "上线前已经存在的行动。",
            "owner": "李静",
            "due_date": None,
        },
        key="legacy-people-view",
    )
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "UPDATE actions SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (action["id"],),
        )

    actions = client.get("/api/actions", params={"status": "open"})

    assert actions.status_code == 200, actions.text
    assert all(item["id"] != action["id"] for item in actions.json())


def test_names_without_a_responsibility_relationship_do_not_create_suggestions(
    client: TestClient,
) -> None:
    action = _complete_action(
        client,
        {
            "kind": "task",
            "title": "核对资料",
            "detail": "李姐好，孙总刚才说过，需要我核对资料。",
            "owner": "待明确",
            "due_date": None,
        },
        key="mentioned-not-assignee",
    )

    reviews = client.get("/api/assignee-reviews?status=pending")

    assert reviews.status_code == 200, reviews.text
    assert all(item["action"]["id"] != action["id"] for item in reviews.json())


def test_assignee_guidance_contains_the_fixed_disambiguation_rules() -> None:
    assert "李姐、静姐只能指李静" in _ASSIGNEE_GUIDANCE
    assert "李香、香姐只能指冯李香" in _ASSIGNEE_GUIDANCE
    assert "绝不能把群名称当负责人" in _ASSIGNEE_GUIDANCE


def test_wecom_sender_identity_is_recorded_without_using_group_as_assignee(
    client: TestClient,
) -> None:
    now = 1_800_000_000
    created = client.post(
        "/api/wechat/windows",
        json={
            "source": "wecom",
            "account_fingerprint": "acct",
            "session_id": "group-1",
            "display_name": "财务部群",
            "kind": "group",
            "messages": [
                {
                    "timestamp": now,
                    "timestampMs": now * 1000,
                    "direction": "in",
                    "kind": "text",
                    "text": "阿婷整理好后发给我。",
                    "cursor": {"sortSeq": 1, "createTime": now, "localId": 1},
                    "sender": {
                        "username": "wecom-user-at",
                        "displayName": "陈贞婷",
                        "isSelf": False,
                    },
                },
                {
                    "timestamp": now + 1,
                    "timestampMs": (now + 1) * 1000,
                    "direction": "out",
                    "kind": "text",
                    "text": "我来跟进。",
                    "cursor": {"sortSeq": 2, "createTime": now + 1, "localId": 2},
                    "sender": {
                        "username": "wecom-user-frank",
                        "displayName": "傅京晖",
                        "isSelf": True,
                    },
                }
            ],
        },
        headers=worker_headers(),
    )

    assert created.status_code == 200, created.text
    people = _people_by_name(client)
    identities = client.get(f"/api/people/{people['陈贞婷']['id']}/identities")
    assert identities.status_code == 200, identities.text
    assert {"source": "wecom", "stable_id": "wecom-user-at"} in identities.json()
    assert all(item["stable_id"] != "wecom:group-1" for item in identities.json())
    self_identities = client.get(f"/api/people/{people['我自己']['id']}/identities")
    assert {"source": "wecom", "stable_id": "wecom-user-frank"} in self_identities.json()


def test_personal_wechat_private_session_is_recorded_as_a_stable_identity(
    client: TestClient,
) -> None:
    now = 1_800_000_100
    created = client.post(
        "/api/wechat/windows",
        json={
            "source": "personal_wechat",
            "account_fingerprint": "acct",
            "session_id": "wxid-hank-stable",
            "display_name": "Hank",
            "kind": "friend",
            "messages": [
                {
                    "timestamp": now,
                    "timestampMs": now * 1000,
                    "direction": "in",
                    "kind": "text",
                    "text": "我来确认。",
                    "cursor": {"sortSeq": 1, "createTime": now, "localId": 1},
                    "sender": {"username": "wxid-hank-stable", "displayName": "Hank"},
                }
            ],
        },
        headers=worker_headers(),
    )

    assert created.status_code == 200, created.text
    people = _people_by_name(client)
    identities = client.get(f"/api/people/{people['孙庆']['id']}/identities")
    assert {"source": "personal_wechat", "stable_id": "wxid-hank-stable"} in identities.json()


def test_frontend_exposes_assignee_tabs_without_dialog_confirmation(client: TestClient) -> None:
    script = client.get("/static/app.js").text

    assert "按负责人" in script
    assert "负责人待明确" in script
    assert "业务判断" in script
    assert "跟进人" in script
    assert "item.action_title" in script
    assert "suggestion.evidence" in script
    assert "/api/assignee-reviews" in script
    assert "/assignees" in script
    assert "confirm(" not in script
