from __future__ import annotations

import hashlib
import html
import imaplib
import json
import os
import re
import ssl
import subprocess
from datetime import UTC, datetime, timedelta
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.client import WorkbenchClient


CONFIG_PATH = Path.home() / "Library/Application Support/FinanceWorkbench/email.json"
KEYCHAIN_SERVICE = "finance-workbench-email"
ATTACHMENT_DIR = Path(
    os.getenv(
        "WORKBENCH_EMAIL_ATTACHMENT_STAGING_DIR",
        "~/Library/Application Support/FinanceWorkbench/email-attachments",
    )
).expanduser()
FORMAL_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
_OBVIOUS_NONWORK_RE = re.compile(
    r"验证码|校验码|登录提醒|安全提醒|密码重置|订阅确认|退订|促销|优惠券|限时折扣|"
    r"新品推荐|每日资讯|每周简报|newsletter|verification code|one[- ]time password|"
    r"password reset|sign[- ]in alert|unsubscribe",
    re.IGNORECASE,
)
_SUBJECT_PREFIX_RE = re.compile(r"^(?:(?:re|fw|fwd|回复|转发)\s*[:：]\s*)+", re.IGNORECASE)


def explain_imap_login_error(address: str, error: BaseException) -> str:
    domain = address.rsplit("@", 1)[-1].casefold()
    if domain in {"qq.com", "foxmail.com"} and "login fail" in str(error).casefold():
        return (
            "QQ邮箱已连通，但服务器拒绝登录。请先在QQ邮箱网页版的“设置 → 账号与安全 → "
            "安全设置”确认POP3/IMAP/SMTP服务显示为已开启；若刚开启服务、刚生成授权码或"
            "连续尝试过，请等待15分钟后再试；若近期修改过QQ密码，请重新生成授权码。"
        )
    return "邮箱服务器拒绝登录，请确认已开启IMAP服务，并使用该邮箱的客户端授权码。"


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())


def load_email_config() -> dict[str, Any] | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(value, dict) or not value.get("address"):
        return None
    protocol = str(value.get("protocol") or "imap").casefold()
    if protocol == "ews":
        return value if value.get("ews_url") else None
    return value if value.get("imap_host") else None


def configured() -> bool:
    return load_email_config() is not None


def _secret(address: str) -> str:
    completed = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", address, "-w"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError("未在 macOS 钥匙串中找到邮箱密码或授权码")
    return completed.stdout.strip()


def _header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (LookupError, UnicodeDecodeError):
        return value.strip()


def _plain_body(message: Message) -> str:
    plain: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.get_content_disposition() == "attachment":
            continue
        content_type = part.get_content_type()
        if content_type not in {"text/plain", "text/html"}:
            continue
        try:
            content = part.get_content()
        except (LookupError, UnicodeDecodeError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if content_type == "text/plain":
            plain.append(str(content))
        else:
            html_parts.append(str(content))
    text = "\n".join(plain).strip()
    if not text and html_parts:
        parser = _HtmlText()
        parser.feed("\n".join(html_parts))
        text = "\n".join(parser.parts)
    text = html.unescape(text).replace("\r", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:100000]


def _attachments(message: Message) -> list[str]:
    names = []
    for part in message.walk():
        filename = _header(part.get_filename())
        if filename:
            names.append(filename[:160])
    return names[:20]


def _save_formal_attachments(message: Message, message_key: str) -> list[str]:
    saved: list[str] = []
    directory = ATTACHMENT_DIR / message_key
    for part in message.walk():
        filename = _header(part.get_filename())
        if not filename or Path(filename).suffix.lower() not in FORMAL_EXTENSIONS:
            continue
        content = part.get_payload(decode=True)
        if not content:
            continue
        safe_name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", Path(filename).name)[:160]
        digest = hashlib.sha256(content).hexdigest()[:12]
        target = directory / f"{digest}-{safe_name}"
        if not target.exists():
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = target.with_name(f".{target.name}.tmp")
            temporary.write_bytes(content)
            os.chmod(temporary, 0o600)
            temporary.replace(target)
        saved.append(str(target))
    return saved[:20]


def _hint(address: str) -> str:
    local, _, domain = address.partition("@")
    if not domain:
        return "未显示邮箱"
    return f"{local[:1]}***@{domain}"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _ews_uid(item_id: str) -> int:
    value = int.from_bytes(hashlib.sha256(item_id.encode("utf-8")).digest()[:8], "big")
    return value & ((1 << 63) - 1) or 1


def _thread_key(message: Message, subject: str) -> str:
    references = str(message.get("References") or "").split()
    anchor = references[0] if references else str(message.get("In-Reply-To") or "")
    if not anchor:
        anchor = _SUBJECT_PREFIX_RE.sub("", subject).strip().casefold()
    return _hash(anchor or "无主题")


def _sent_at(message: Message) -> str | None:
    try:
        value = parsedate_to_datetime(str(message.get("Date") or ""))
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        return None


def _obvious_nonwork(message: Message, subject: str) -> bool:
    if _OBVIOUS_NONWORK_RE.search(subject):
        return True
    if message.get("List-Unsubscribe") or str(message.get("Precedence") or "").lower() in {"bulk", "list", "junk"}:
        return True
    auto = str(message.get("Auto-Submitted") or "").lower()
    return bool(auto and auto != "no")


def _payload_from_raw(
    raw: bytes,
    *,
    account_id: str,
    folder: str,
    uid_validity: str,
    uid: int,
) -> dict[str, Any]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    subject = _header(message.get("Subject")) or "无主题邮件"
    work_subject = _SUBJECT_PREFIX_RE.sub("", subject).strip() or subject
    sender_name, sender_address = parseaddr(_header(message.get("From")))
    sender_address = sender_address.casefold()
    body = _plain_body(message)
    attachments = _attachments(message)
    source_text = "\n".join(
        [
            f"主题：{work_subject}",
            f"发件人线索：{sender_name or '未识别名称'} "
            f"<{sender_address or '未识别邮箱'}>",
            f"时间：{_sent_at(message) or '未识别'}",
            f"附件：{'、'.join(attachments) if attachments else '无'}",
            f"正文：{body}",
        ]
    )
    if _obvious_nonwork(message, subject):
        result = {
            "classification": "irrelevant",
            "needs_follow_up": False,
            "summary": "已自动过滤非工作通知或订阅邮件",
            "reason": "邮件特征显示为验证码、安全提醒、订阅或群发内容",
            "matter_id": None,
            "matter_title": "",
            "evidence": [],
            "actions": [],
        }
        attachment_paths: list[str] = []
    else:
        result = {
            "classification": "pending",
            "needs_follow_up": False,
            "summary": "",
            "reason": "",
            "matter_id": None,
            "matter_title": "",
            "evidence": [],
            "actions": [],
        }
        attachment_paths = _save_formal_attachments(
            message, _hash(f"email:{account_id}:{uid_validity}:{uid}")
        )
    return {
        "account_id": account_id,
        "folder": folder,
        "uid_validity": uid_validity,
        "uid": uid,
        "message_id_hash": _hash(str(message.get("Message-ID") or "")),
        "thread_key": _thread_key(message, subject),
        "sender_key": _hash(sender_address or str(message.get("From") or "")),
        "sender_name": sender_name[:160],
        "sender_hint": _hint(sender_address),
        "subject": work_subject,
        "sent_at": _sent_at(message),
        "source_text": source_text if result["classification"] == "pending" else "",
        "attachment_paths": attachment_paths,
        **result,
    }


def _ews_account(config: dict[str, Any], password: str) -> Any:
    try:
        from exchangelib import Account, Configuration, Credentials, DELEGATE, NTLM
    except ImportError as error:
        raise RuntimeError("当前 Python 环境缺少 EWS 依赖，请先安装项目的 mac 依赖") from error

    endpoint = str(config.get("ews_url") or "").strip()
    if not endpoint.startswith("https://"):
        raise RuntimeError("EWS 地址必须使用 HTTPS")
    address = str(config["address"]).strip()
    username = str(config.get("ews_username") or address).strip()
    credentials = Credentials(username=username, password=password)
    exchange_config = Configuration(
        service_endpoint=endpoint,
        credentials=credentials,
        auth_type=NTLM,
    )
    return Account(
        primary_smtp_address=address,
        config=exchange_config,
        autodiscover=False,
        access_type=DELEGATE,
    )


def _run_ews_sync(
    client: WorkbenchClient,
    request: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    address = str(config["address"]).strip()
    account_id = _hash(address.casefold())
    folder = str(config.get("folder") or "INBOX")
    endpoint = str(config["ews_url"]).strip()
    if folder.casefold() != "inbox":
        raise RuntimeError("EWS 只读适配目前只支持收件箱")
    endpoint_host = urlsplit(endpoint).hostname or endpoint
    client.request_json(
        "POST",
        "/api/email/accounts/register",
        {"account_id": account_id, "address_hint": _hint(address), "imap_host": endpoint_host, "folder": folder},
    )
    password = _secret(address)
    account = _ews_account(config, password)

    try:
        sync_days = max(1, min(int(config.get("sync_days") or 7), 30))
        limit = max(1, min(int(config.get("max_messages_per_run") or 80), 80))
        since = datetime.now(UTC) - timedelta(days=sync_days)
        uid_validity = _hash(f"ews:{endpoint}:{folder}")
        processed = work_count = ignored_count = pending_count = 0
        # ponytail: EWS IDs are opaque, so bounded rereads rely on DB uniqueness; add an ID checkpoint if volume warrants it.
        items = account.inbox.filter(datetime_received__gte=since).only("id", "mime_content")[:limit]
        for item in items:
            if isinstance(item, BaseException):
                raise RuntimeError("EWS 收件箱读取失败") from item
            raw = getattr(item, "mime_content", None)
            if not raw:
                raise RuntimeError("EWS 邮件内容暂时无法读取")
            if not isinstance(raw, bytes):
                raw = bytes(raw)
            item_id = str(getattr(item, "id", "") or _hash(raw.decode("utf-8", errors="replace")))
            payload = _payload_from_raw(
                raw,
                account_id=account_id,
                folder=folder,
                uid_validity=uid_validity,
                uid=_ews_uid(item_id),
            )
            saved = client.request_json("POST", "/api/email/messages", payload)
            processed += 1
            if saved.get("status") == "pending":
                pending_count += 1
            elif saved.get("status") == "active":
                work_count += 1
            else:
                ignored_count += 1
        return {
            "account_id": account_id,
            "uid_validity": uid_validity,
            "last_uid": 0,
            "processed": processed,
            "work_count": work_count,
            "ignored_count": ignored_count,
            "pending_count": pending_count,
        }
    except Exception as error:
        if isinstance(error, RuntimeError):
            raise
        raise RuntimeError("EWS 收件箱读取失败") from error


def run_email_sync(client: WorkbenchClient, request: dict[str, Any]) -> dict[str, Any]:
    config = load_email_config()
    if not config:
        raise RuntimeError("邮箱尚未在本机完成配置")
    if str(config.get("protocol") or "imap").casefold() == "ews":
        return _run_ews_sync(client, request, config)
    address = str(config["address"]).strip()
    account_id = _hash(address.casefold())
    folder = str(config.get("folder") or "INBOX")
    host = str(config["imap_host"])
    port = int(config.get("imap_port") or 993)
    address_hint = _hint(address)
    client.request_json(
        "POST",
        "/api/email/accounts/register",
        {"account_id": account_id, "address_hint": address_hint, "imap_host": host, "folder": folder},
    )
    state = client.request_json("GET", f"/api/email/accounts/{account_id}/state") or {}
    password = _secret(address)
    mailbox = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context())
    last_uid = int(state.get("last_uid") or 0)
    processed = work_count = ignored_count = pending_count = 0
    try:
        try:
            mailbox.login(address, password)
        except imaplib.IMAP4.error as error:
            raise RuntimeError(explain_imap_login_error(address, error)) from error
        status, _ = mailbox.select(folder, readonly=True)
        if status != "OK":
            raise RuntimeError("邮箱收件箱暂时无法读取")
        response = mailbox.response("UIDVALIDITY")[1]
        uid_validity = response[0].decode(errors="replace") if response else "unknown"
        if state.get("uid_validity") and state.get("uid_validity") != uid_validity:
            last_uid = 0
        if last_uid:
            criteria = f"UID {last_uid + 1}:*"
        else:
            since = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
            criteria = f'SINCE "{since}"'
        status, data = mailbox.uid("search", None, criteria)
        if status != "OK":
            raise RuntimeError("邮箱新增邮件检查失败")
        uids = sorted(
            int(value)
            for value in set((data[0] or b"").split())
            if int(value) > last_uid
        )
        limit = int(config.get("max_messages_per_run") or 80)
        for uid in uids[:limit]:
            status, rows = mailbox.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                raise RuntimeError("有一封新增邮件暂时无法读取")
            raw = next((item[1] for item in rows if isinstance(item, tuple)), None)
            if not raw:
                continue
            payload = _payload_from_raw(
                raw,
                account_id=account_id,
                folder=folder,
                uid_validity=uid_validity,
                uid=uid,
            )
            saved = client.request_json("POST", "/api/email/messages", payload)
            processed += 1
            if saved.get("status") == "pending":
                pending_count += 1
            elif saved.get("status") == "active":
                work_count += 1
            else:
                ignored_count += 1
            last_uid = max(last_uid, uid)
        return {
            "account_id": account_id,
            "uid_validity": uid_validity,
            "last_uid": last_uid,
            "processed": processed,
            "work_count": work_count,
            "ignored_count": ignored_count,
            "pending_count": pending_count,
        }
    finally:
        try:
            mailbox.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
