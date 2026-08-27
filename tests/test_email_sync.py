from __future__ import annotations

import json
from email.message import EmailMessage

import pytest

from scripts import email_sync


def test_qq_login_failure_has_actionable_chinese_guidance() -> None:
    message = email_sync.explain_imap_login_error(
        "finance@qq.com",
        email_sync.imaplib.IMAP4.error(b"Login fail. Account is abnormal, service is not open"),
    )

    assert "QQ邮箱已连通" in message
    assert "确认POP3/IMAP/SMTP服务显示为已开启" in message
    assert "等待15分钟" in message
    assert "Login fail" not in message


def raw_message(subject: str, body: str) -> bytes:
    message = EmailMessage()
    message["From"] = "业务联系人 <business@example.com>"
    message["To"] = "finance@example.com"
    message["Date"] = "Thu, 20 Aug 2026 10:00:00 +0800"
    message["Message-ID"] = f"<{abs(hash(subject))}@example.com>"
    message["Subject"] = subject
    message.set_content(body)
    return message.as_bytes()


class FakeMailbox:
    def __init__(self, messages: dict[int, bytes]):
        self.messages = messages
        self.criteria = ""

    def login(self, address: str, password: str):
        assert address == "finance@example.com"
        assert password == "secret"

    def select(self, folder: str, readonly: bool = False):
        assert folder == "INBOX" and readonly is True
        return "OK", [b""]

    def response(self, name: str):
        assert name == "UIDVALIDITY"
        return "UIDVALIDITY", [b"123"]

    def uid(self, command: str, *args):
        if command == "search":
            self.criteria = args[1]
            return "OK", [b" ".join(str(uid).encode() for uid in self.messages)]
        uid = int(args[0])
        return "OK", [(b"BODY[]", self.messages[uid])]

    def logout(self):
        return "BYE", [b""]


class FakeClient:
    def __init__(self, last_uid: int = 0, hint_error: bool = False):
        self.last_uid = last_uid
        self.hint_error = hint_error
        self.saved: list[dict] = []

    def request_json(self, method: str, path: str, payload=None):
        if path == "/api/email/accounts/register":
            return payload
        if path.endswith("/state"):
            return {"last_uid": self.last_uid, "uid_validity": "123"}
        if path == "/api/matters?limit=60":
            return []
        if path == "/api/policies?policy_status=active":
            return []
        if path.startswith("/api/policies/identity/hint"):
            if self.hint_error:
                raise RuntimeError("身份提示暂时不可用")
            return {
                "known": True,
                "publisher": "上级公司",
                "is_authority": True,
                "confidence": 0.95,
            }
        if path == "/api/policy-candidates":
            return {"stored": False}
        if path == "/api/email/messages":
            self.saved.append(payload)
            return {"status": payload["classification"]}
        raise AssertionError((method, path, payload))


def test_imap_increment_filters_obvious_nonwork_and_holds_other_mail(monkeypatch) -> None:
    mailbox = FakeMailbox(
        {
            1: raw_message("登录验证码", "验证码 123456"),
            2: raw_message("预算复核要求", "请于本周内复核预算差异并回复。"),
        }
    )
    monkeypatch.setattr(
        email_sync,
        "load_email_config",
        lambda: {
            "address": "finance@example.com",
            "imap_host": "imap.example.com",
            "imap_port": 993,
            "folder": "INBOX",
        },
    )
    monkeypatch.setattr(email_sync, "_secret", lambda _address: "secret")
    monkeypatch.setattr(email_sync.imaplib, "IMAP4_SSL", lambda *args, **kwargs: mailbox)
    client = FakeClient()
    result = email_sync.run_email_sync(client, {"id": "sync-1"})
    assert result["last_uid"] == 2
    assert result["work_count"] == 0
    assert result["ignored_count"] == 1
    assert result["pending_count"] == 1
    assert [item["classification"] for item in client.saved] == [
        "irrelevant",
        "pending",
    ]
    assert "预算复核要求" in client.saved[1]["source_text"]


def test_email_sync_saves_unclassified_mail_without_model_dependencies(monkeypatch) -> None:
    mailbox = FakeMailbox({1: raw_message("采购规定", "以后采购需按新规定执行。")})
    monkeypatch.setattr(
        email_sync,
        "load_email_config",
        lambda: {"address": "finance@example.com", "imap_host": "imap.example.com"},
    )
    monkeypatch.setattr(email_sync, "_secret", lambda _address: "secret")
    monkeypatch.setattr(email_sync.imaplib, "IMAP4_SSL", lambda *args, **kwargs: mailbox)
    client = FakeClient(hint_error=True)
    result = email_sync.run_email_sync(client, {"id": "sync-no-model"})
    assert result["processed"] == 1
    assert result["pending_count"] == 1
    assert client.saved[0]["classification"] == "pending"
    assert client.saved[0]["summary"] == ""


def test_uid_overlap_does_not_reclassify_old_message(monkeypatch) -> None:
    mailbox = FakeMailbox({2: raw_message("预算复核要求", "请复核。")})
    monkeypatch.setattr(
        email_sync,
        "load_email_config",
        lambda: {"address": "finance@example.com", "imap_host": "imap.example.com"},
    )
    monkeypatch.setattr(email_sync, "_secret", lambda _address: "secret")
    monkeypatch.setattr(email_sync.imaplib, "IMAP4_SSL", lambda *args, **kwargs: mailbox)
    client = FakeClient(last_uid=2)
    result = email_sync.run_email_sync(client, {"id": "sync-2"})
    assert mailbox.criteria == "UID 3:*"
    assert result["processed"] == 0
    assert client.saved == []


def test_ews_config_is_recognized_without_imap_host(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "email.json"
    config = {
        "protocol": "ews",
        "address": "finance@example.com",
        "ews_url": "https://exchange.example.com/EWS/Exchange.asmx",
        "folder": "INBOX",
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(email_sync, "CONFIG_PATH", config_path)

    assert email_sync.load_email_config() == config


class FakeEwsItem:
    def __init__(self, item_id: str, raw: bytes):
        self.id = item_id
        self.mime_content = raw


class FakeEwsInbox:
    def __init__(self, items: list[FakeEwsItem]):
        self.items = items
        self.filter_kwargs: dict = {}
        self.only_fields: tuple[str, ...] = ()
        self.limit = 0

    def filter(self, **kwargs):
        self.filter_kwargs = kwargs
        return self

    def only(self, *fields: str):
        self.only_fields = fields
        return self

    def __getitem__(self, value: slice):
        assert value.start is None and value.step is None
        self.limit = value.stop or 0
        return self.items[: self.limit]


class FakeEwsAccount:
    def __init__(self, inbox: FakeEwsInbox):
        self._inbox = inbox
        self.inbox_reads = 0

    @property
    def inbox(self):
        self.inbox_reads += 1
        return self._inbox


class FakeEwsClient:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.saved: list[dict] = []
        self.message_keys: set[tuple[str, str, str, int]] = set()

    def request_json(self, method: str, path: str, payload=None):
        self.calls.append((method, path))
        if path == "/api/email/accounts/register":
            return payload
        if path != "/api/email/messages":
            raise AssertionError((method, path, payload))
        key = (
            payload["account_id"],
            payload["folder"],
            payload["uid_validity"],
            payload["uid"],
        )
        if key not in self.message_keys:
            self.message_keys.add(key)
            self.saved.append(payload)
        return {"status": payload["classification"]}


def test_ews_reads_only_inbox_and_keeps_item_uid_stable(monkeypatch) -> None:
    inbox = FakeEwsInbox([FakeEwsItem("ews-item-1", raw_message("预算复核要求", "请复核。"))])
    account = FakeEwsAccount(inbox)
    config = {
        "protocol": "ews",
        "address": "finance@example.com",
        "ews_username": "DOMAIN\\finance",
        "ews_url": "https://exchange.example.com/EWS/Exchange.asmx",
        "folder": "INBOX",
        "sync_days": 7,
        "max_messages_per_run": 1,
    }
    credentials: list[tuple[dict, str]] = []

    def fake_ews_account(received_config, password):
        credentials.append((received_config, password))
        return account

    monkeypatch.setattr(email_sync, "load_email_config", lambda: config)
    monkeypatch.setattr(email_sync, "_secret", lambda _address: "secret")
    monkeypatch.setattr(email_sync, "_ews_account", fake_ews_account)
    client = FakeEwsClient()

    first = email_sync.run_email_sync(client, {"id": "ews-1"})
    second = email_sync.run_email_sync(client, {"id": "ews-2"})

    assert first["processed"] == second["processed"] == 1
    assert len(client.saved) == 1
    assert client.saved[0]["uid"] == email_sync._ews_uid("ews-item-1")
    assert inbox.only_fields == ("id", "mime_content")
    assert inbox.limit == 1
    assert "datetime_received__gte" in inbox.filter_kwargs
    assert account.inbox_reads == 2
    assert credentials == [(config, "secret"), (config, "secret")]
    assert client.calls == [
        ("POST", "/api/email/accounts/register"),
        ("POST", "/api/email/messages"),
        ("POST", "/api/email/accounts/register"),
        ("POST", "/api/email/messages"),
    ]


def test_ews_rejects_non_inbox_before_workbench_write(monkeypatch) -> None:
    monkeypatch.setattr(
        email_sync,
        "load_email_config",
        lambda: {
            "protocol": "ews",
            "address": "finance@example.com",
            "ews_url": "https://exchange.example.com/EWS/Exchange.asmx",
            "folder": "Sent Items",
        },
    )
    client = FakeEwsClient()

    with pytest.raises(RuntimeError, match="只支持收件箱"):
        email_sync.run_email_sync(client, {"id": "ews-invalid-folder"})

    assert client.calls == []


def test_ews_account_uses_explicit_ntlm_endpoint() -> None:
    account = email_sync._ews_account(
        {
            "address": "finance@example.com",
            "ews_username": "DOMAIN\\finance",
            "ews_url": "https://exchange.example.com/EWS/Exchange.asmx",
        },
        "secret",
    )

    assert account.primary_smtp_address == "finance@example.com"
    assert account.protocol.service_endpoint == "https://exchange.example.com/EWS/Exchange.asmx"
    assert str(account.protocol.auth_type) == "NTLM"
    assert account.access_type == "delegate"
