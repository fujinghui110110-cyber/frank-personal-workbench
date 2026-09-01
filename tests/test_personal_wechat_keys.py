from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import configure_personal_wechat
from scripts import personal_wechat_keys


class _FakeKeychain:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, args: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((args, kwargs))
        command = args[1]
        service = args[args.index("-s") + 1]
        account = args[args.index("-a") + 1]
        item = (service, account)
        if command == "add-generic-password":
            value = str(kwargs["input"]).splitlines()[0]
            self.items[item] = value
            return subprocess.CompletedProcess(args, 0, "", "")
        if command == "find-generic-password":
            if item not in self.items:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            return subprocess.CompletedProcess(args, 0, self.items[item] + "\n", "")
        if command == "delete-generic-password":
            if item not in self.items:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            del self.items[item]
            return subprocess.CompletedProcess(args, 0, "", "")
        raise AssertionError(command)


def test_store_load_list_and_delete_use_keychain_without_secret_arguments(
    monkeypatch,
) -> None:
    fake = _FakeKeychain()
    monkeypatch.setattr(personal_wechat_keys.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(personal_wechat_keys.subprocess, "run", fake)
    key = "ab" * 32

    personal_wechat_keys.store_key("wxid-frank", "message.db", key)
    assert personal_wechat_keys.load_key("wxid-frank", "message.db") == key
    assert personal_wechat_keys.list_database_labels("wxid-frank") == ["message.db"]

    personal_wechat_keys.delete_key("wxid-frank", "message.db")
    assert personal_wechat_keys.load_key("wxid-frank", "message.db") is None
    assert personal_wechat_keys.list_databases("wxid-frank") == []
    assert all(key not in args for args, _ in fake.calls)
    assert any(key in str(kwargs.get("input", "")) for _, kwargs in fake.calls)


def test_store_and_load_image_keys_without_secret_arguments(monkeypatch) -> None:
    fake = _FakeKeychain()
    monkeypatch.setattr(personal_wechat_keys.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(personal_wechat_keys.subprocess, "run", fake)
    aes_key = "0123456789abcdef"

    personal_wechat_keys.store_image_keys("wxid-frank", "0x53", aes_key)

    assert personal_wechat_keys.load_image_keys("wxid-frank") == (0x53, aes_key.encode())
    assert all(aes_key not in args for args, _ in fake.calls)


def test_store_keys_replaces_invalid_legacy_label_index(monkeypatch) -> None:
    fake = _FakeKeychain()
    account = "wxid-frank"
    fake.items[(personal_wechat_keys._INDEX_SERVICE, account)] = "legacy-index"
    monkeypatch.setattr(personal_wechat_keys.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(personal_wechat_keys.subprocess, "run", fake)
    first = "12" * 32
    second = "34" * 32

    personal_wechat_keys.store_keys(
        account,
        [("session/session.db", second), ("contact/contact.db", first)],
    )

    assert personal_wechat_keys.list_database_labels(account) == [
        "contact/contact.db",
        "session/session.db",
    ]
    assert personal_wechat_keys.load_key(account, "contact/contact.db") == first
    assert personal_wechat_keys.load_key(account, "session/session.db") == second
    assert all(first not in args and second not in args for args, _ in fake.calls)


def test_key_validation_happens_before_keychain_access(monkeypatch) -> None:
    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Keychain must not be called")

    monkeypatch.setattr(personal_wechat_keys.subprocess, "run", fail_if_called)
    with pytest.raises(ValueError, match="64位十六进制"):
        personal_wechat_keys.store_key("wxid-frank", "message.db", "not-a-key")

    with pytest.raises(ValueError, match="64位十六进制"):
        personal_wechat_keys.store_keys(
            "wxid-frank",
            [("contact.db", "ab" * 32), ("message.db", "not-a-key")],
        )


def test_non_not_found_keychain_errors_are_not_reported_as_missing(monkeypatch) -> None:
    def denied(*args, **kwargs):
        return subprocess.CompletedProcess(args, 36, "", "access denied")

    monkeypatch.setattr(personal_wechat_keys.subprocess, "run", denied)
    with pytest.raises(RuntimeError, match="钥匙串"):
        personal_wechat_keys.load_key("wxid-frank", "message.db")
    with pytest.raises(RuntimeError, match="钥匙串"):
        personal_wechat_keys.list_database_labels("wxid-frank")


def test_tty_writer_sends_secret_only_after_hidden_prompts(monkeypatch) -> None:
    key = "ef" * 32
    script = (
        "printf 'password data for new item:'; "
        "IFS= read -r first; "
        "printf 'retype password new item:'; "
        "IFS= read -r second; "
        '[ "$first" = "$second" ]'
    )
    monkeypatch.setattr(personal_wechat_keys, "_SECURITY", "/bin/sh")

    result = personal_wechat_keys._run_security_prompt(["-c", script], key)

    assert result.returncode == 0
    assert key not in result.args


def test_cli_commands_never_print_key_and_delete_requires_confirmation(
    monkeypatch, capsys
) -> None:
    fake = _FakeKeychain()
    monkeypatch.setattr(personal_wechat_keys.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(personal_wechat_keys.subprocess, "run", fake)
    key = "cd" * 32
    monkeypatch.setattr(personal_wechat_keys, "getpass", lambda _prompt: key)

    assert personal_wechat_keys.main(["set", "wxid-frank", "message.db"]) == 0
    assert key not in capsys.readouterr().out

    assert personal_wechat_keys.main(["list", "wxid-frank"]) == 0
    output = capsys.readouterr().out
    assert output.strip() == "message.db"
    assert key not in output

    assert personal_wechat_keys.main(["status", "wxid-frank", "message.db"]) == 0
    output = capsys.readouterr().out
    assert output.strip() == "已配置"
    assert key not in output

    with pytest.raises(SystemExit):
        personal_wechat_keys.main(["delete", "wxid-frank", "message.db"])
    assert personal_wechat_keys.load_key("wxid-frank", "message.db") == key

    assert (
        personal_wechat_keys.main(["delete", "wxid-frank", "message.db", "--yes"]) == 0
    )
    assert key not in capsys.readouterr().out
    assert personal_wechat_keys.key_status("wxid-frank", "message.db") is False


def test_interactive_configuration_uses_hidden_input_and_does_not_print_key(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    key = "CD" * 32
    saved: list[tuple[str, str, str]] = []
    root = tmp_path / "db_storage"
    source = root / "message/message_0.db"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"encrypted")
    dataset = configure_personal_wechat.WechatDataset("wxid-frank", root, (source,))
    monkeypatch.setattr(configure_personal_wechat, "getpass", lambda _prompt: key)
    monkeypatch.setattr(configure_personal_wechat, "discover_dataset", lambda: dataset)
    monkeypatch.setattr(configure_personal_wechat, "load_key", lambda *_: None)
    monkeypatch.setattr(
        configure_personal_wechat,
        "snapshot_dataset",
        lambda _dataset, destination: destination,
    )
    monkeypatch.setattr(
        configure_personal_wechat, "probe_database_key", lambda *_: True
    )
    monkeypatch.setattr(
        configure_personal_wechat,
        "store_keys",
        lambda account, values: saved.extend(
            (account, database, value) for database, value in values
        ),
    )

    configure_personal_wechat.main()

    assert saved == [("wxid-frank", "message/message_0.db", key.lower())]
    assert key not in capsys.readouterr().out


def test_interactive_configuration_rejects_invalid_key(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "db_storage"
    source = root / "message/message_0.db"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"encrypted")
    dataset = configure_personal_wechat.WechatDataset("wxid-frank", root, (source,))
    monkeypatch.setattr(configure_personal_wechat, "discover_dataset", lambda: dataset)
    monkeypatch.setattr(configure_personal_wechat, "load_key", lambda *_: None)
    monkeypatch.setattr(
        configure_personal_wechat,
        "snapshot_dataset",
        lambda _dataset, destination: destination,
    )
    monkeypatch.setattr(configure_personal_wechat, "getpass", lambda _prompt: "short")
    with pytest.raises(SystemExit, match="64位十六进制"):
        configure_personal_wechat.main()


def test_configuration_probes_one_ciphertalk_key_for_all_databases(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    key = "ab" * 32
    root = tmp_path / "db_storage"
    sources = tuple(
        root / label
        for label in (
            "contact/contact.db",
            "session/session.db",
            "message/message_0.db",
        )
    )
    for source in sources:
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"encrypted")
    dataset = configure_personal_wechat.WechatDataset("wxid-frank", root, sources)
    saved: list[tuple[str, str, str]] = []
    prompts: list[str] = []
    monkeypatch.setattr(configure_personal_wechat, "load_key", lambda *_: None)
    monkeypatch.setattr(
        configure_personal_wechat,
        "snapshot_dataset",
        lambda _dataset, destination: destination,
    )
    monkeypatch.setattr(
        configure_personal_wechat,
        "probe_database_key",
        lambda _source, value, _sqlcipher: value == key,
    )
    monkeypatch.setattr(
        configure_personal_wechat,
        "getpass",
        lambda prompt: prompts.append(prompt) or key,
    )
    monkeypatch.setattr(
        configure_personal_wechat,
        "store_keys",
        lambda account, values: saved.extend(
            (account, database, value) for database, value in values
        ),
    )

    configured = configure_personal_wechat.configure_dataset(dataset, Path("sqlcipher"))

    assert configured == 3
    assert len(prompts) == 1
    assert "CipherTalk" in prompts[0]
    assert [database for _, database, _ in saved] == [
        "contact/contact.db",
        "session/session.db",
        "message/message_0.db",
    ]
    output = capsys.readouterr().out
    assert "已匹配 3/3 个数据库" in output
    assert key not in output


def test_configuration_explains_partial_ciphertalk_key_coverage_without_saving(
    tmp_path: Path, monkeypatch
) -> None:
    key = "cd" * 32
    root = tmp_path / "db_storage"
    contact = root / "contact/contact.db"
    session = root / "session/session.db"
    for source in (contact, session):
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"encrypted")
    dataset = configure_personal_wechat.WechatDataset(
        "wxid-frank", root, (contact, session)
    )
    saved: list[tuple[str, str, str]] = []
    monkeypatch.setattr(configure_personal_wechat, "load_key", lambda *_: None)
    monkeypatch.setattr(
        configure_personal_wechat,
        "snapshot_dataset",
        lambda _dataset, destination: destination,
    )
    monkeypatch.setattr(configure_personal_wechat, "getpass", lambda _prompt: key)
    monkeypatch.setattr(
        configure_personal_wechat,
        "probe_database_key",
        lambda source, _value, _sqlcipher: source.as_posix().endswith(
            "session/session.db"
        ),
    )
    monkeypatch.setattr(
        configure_personal_wechat,
        "store_keys",
        lambda account, values: saved.extend(
            (account, database, value) for database, value in values
        ),
    )

    with pytest.raises(RuntimeError, match="只匹配 1/2 个数据库") as error:
        configure_personal_wechat.configure_dataset(dataset, Path("sqlcipher"))

    assert "不代表输入错误" in str(error.value)
    assert "逐库派生方式" in str(error.value)
    assert saved == []
