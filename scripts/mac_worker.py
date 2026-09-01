from __future__ import annotations

import argparse
import os
import platform
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from app.client import WorkbenchClient
from scripts.email_sync import configured as email_configured, run_email_sync
from scripts.export_listening_chats import export_listening_chats
from scripts.icloud_inbox import DEFAULT_SYNC_INBOX, scan_icloud_inbox
from scripts.transcription import is_audio_video, transcribe_material
from scripts.wechat_sync import run_personal_wechat_sync
from scripts.wecom_sync import run_wecom_sync
from scripts.workbuddy_analysis import (
    analyze_with_workbuddy,
    classify_email_with_workbuddy,
    classify_policy_with_workbuddy,
    classify_wechat_with_workbuddy,
    material_text,
)


SYNC_INTERVAL_SECONDS = 2 * 60 * 60
WAKE_GAP_SECONDS = 60
DEFAULT_ANALYSIS_CONCURRENCY = 300
MAX_ANALYSIS_CONCURRENCY = 500


def error_text(error: BaseException) -> str:
    return str(error).strip()[:1000] or "本机处理未完成"


def scheduled_sync_reason(
    now_epoch: float,
    last_loop_epoch: float | None,
    last_sync_epoch: float | None,
    weekday: int,
) -> str | None:
    if weekday >= 5:
        return None
    if last_sync_epoch is None:
        return "startup"
    if last_loop_epoch is not None and now_epoch - last_loop_epoch > WAKE_GAP_SECONDS:
        return "wake"
    if now_epoch - last_sync_epoch >= SYNC_INTERVAL_SECONDS:
        return "interval"
    return None


def request_incremental_sync(client: WorkbenchClient) -> None:
    client.request_json(
        "POST", "/api/wechat/sync/run", {"sources": ["personal_wechat", "wecom"]}
    )
    if email_configured():
        client.request_json("POST", "/api/email/sync/run", {})


def _classify_chat_policy(
    client: WorkbenchClient, material: dict, source_text: str
) -> dict:
    source_type = (
        "wecom" if material.get("source_type") == "wecom_auto" else "personal_wechat"
    )
    source_label = str(material.get("filename") or "聊天会话")
    policies = client.request_json("GET", "/api/policies?policy_status=active") or []
    try:
        identity_hint = (
            client.request_json(
                "GET",
                f"/api/policies/identity/hint?source_type={source_type}&source_key={quote(source_label)}",
            )
            or {}
        )
    except (OSError, RuntimeError):
        identity_hint = {}
    result = classify_policy_with_workbuddy(
        source_text,
        source_type,
        source_label,
        policies,
        identity_hint=identity_hint,
    )
    return client.request_json(
        "POST",
        "/api/policy-candidates",
        {
            "source_type": source_type,
            "source_ref": str(material["id"]),
            "source_label": source_label,
            "material_id": str(material["id"]),
            "result": result,
        },
    )


def process_once(client: WorkbenchClient, worker_id: str) -> bool:
    client.request_json(
        "POST",
        "/api/nodes/heartbeat",
        {
            "node_id": worker_id,
            "name": f"{platform.node()} · 贾维斯 执行节点",
            "metadata": {
                "platform": platform.platform(),
                "mode": "workbuddy-finance-chief-of-staff",
                "agent": "贾维斯",
            },
        },
    )
    if email_configured():
        email_response = client.request_json(
            "POST", "/api/email/sync/claim", {"worker_id": worker_id}
        )
        email_request = (
            email_response.get("request") if isinstance(email_response, dict) else None
        )
        if email_request:
            result: dict = {}
            try:
                result = run_email_sync(client, email_request)
                client.request_json(
                    "POST",
                    f"/api/email/sync/{email_request['id']}/finish",
                    {
                        "worker_id": worker_id,
                        "status": "completed",
                        "error": "",
                        **result,
                        "scanned_count": int(result.get("processed") or 0),
                    },
                )
                print(
                    f"邮箱检查完成：新收取 {result['pending_count']} 封待整理邮件，"
                    f"本地规则过滤 {result['ignored_count']} 封",
                    flush=True,
                )
            except Exception as error:
                client.request_json(
                    "POST",
                    f"/api/email/sync/{email_request['id']}/finish",
                    {
                        "worker_id": worker_id,
                        "status": "failed",
                        "error": error_text(error),
                        "account_id": result.get("account_id", ""),
                        "uid_validity": result.get("uid_validity", ""),
                        "last_uid": result.get("last_uid", 0),
                    },
                )
                print(f"邮箱检查未完成：{error}", flush=True)
            return True

    sync_response = client.request_json(
        "POST", "/api/wechat/sync/claim", {"worker_id": worker_id}
    )
    sync_request = (
        sync_response.get("request") if isinstance(sync_response, dict) else None
    )
    if sync_request:
        source = str(sync_request.get("source") or "personal_wechat")
        try:
            sync = run_wecom_sync if source == "wecom" else run_personal_wechat_sync
            result = sync(client, sync_request.get("mode") or "incremental")
            skipped = int(result.get("skipped") or 0)
            client.request_json(
                "POST",
                f"/api/wechat/sync/{sync_request['id']}/finish",
                {
                    "worker_id": worker_id,
                    "status": "completed",
                    "error": f"已跳过 {skipped} 个已失效会话" if skipped else "",
                    "message_count": int(result.get("messages") or 0),
                    "window_count": int(result.get("windows") or 0),
                    "skipped_count": skipped,
                },
            )
            try:
                exported = export_listening_chats(client.base_url)
                client.request_json(
                    "POST",
                    "/api/wechat/export/report",
                    {"status": "completed", "error": "", **exported},
                )
            except Exception as export_error:
                try:
                    client.request_json(
                        "POST",
                        "/api/wechat/export/report",
                        {"status": "failed", "error": error_text(export_error)},
                    )
                except Exception:
                    pass
                print(f"聊天已检查，但本机导出未完成：{export_error}", flush=True)
            print(
                f"{'企业微信' if source == 'wecom' else '个人微信'}检查完成："
                f"读取 {result['messages']} 条，新增 {result['windows']} 个线索窗口"
                + (f"，跳过 {skipped} 个已失效会话" if skipped else ""),
                flush=True,
            )
        except Exception as error:
            client.request_json(
                "POST",
                f"/api/wechat/sync/{sync_request['id']}/finish",
                {
                    "worker_id": worker_id,
                    "status": "failed",
                    "error": error_text(error),
                },
            )
            print(
                f"{'企业微信' if source == 'wecom' else '个人微信'}检查暂未完成：{error}",
                flush=True,
            )
        return True
    claimed = client.request_json("POST", "/api/jobs/claim", {"worker_id": worker_id})[
        "job"
    ]
    if not claimed:
        return False
    lease = {
        "worker_id": worker_id,
        "lease_token": claimed["lease_token"],
    }
    try:
        client.request_json("POST", f"/api/jobs/{claimed['id']}/start", lease)
        material = claimed["material"]
        if claimed.get("job_type") == "email_classify":
            source_text = material_text(
                material,
                client.get_bytes(f"/api/materials/{material['id']}/content"),
            )
            matters = client.request_json("GET", "/api/matters?limit=60") or []
            metadata = material.get("metadata") or {}
            attachment_paths = list(metadata.get("attachment_paths") or [])
            result = (
                classify_email_with_workbuddy(
                    source_text,
                    matters,
                    image_paths=attachment_paths,
                )
                if attachment_paths
                else classify_email_with_workbuddy(source_text, matters)
            )
            message_id = str(metadata.get("email_message_id") or "")
            if not message_id:
                raise RuntimeError("邮件分类任务缺少原始邮件定位")
            policies = (
                client.request_json("GET", "/api/policies?policy_status=active") or []
            )
            sender_key = str(metadata.get("sender_key") or "")
            try:
                identity_hint = (
                    client.request_json(
                        "GET",
                        "/api/policies/identity/hint"
                        f"?source_type=email&source_key={quote(sender_key)}",
                    )
                    or {}
                )
            except (OSError, RuntimeError):
                identity_hint = {}
            policy_result = classify_policy_with_workbuddy(
                source_text,
                "email",
                sender_key,
                policies,
                identity_hint=identity_hint,
            )
            policy_relevant = policy_result.get("classification") in {
                "policy",
                "uncertain",
            }
            result["_policy_relevant"] = policy_relevant
            if policy_relevant:
                policy_result["attachments"] = metadata.get("attachment_paths") or []
                client.request_json(
                    "POST",
                    "/api/policy-candidates",
                    {
                        "source_type": "email",
                        "source_ref": f"email:{message_id}",
                        "source_label": sender_key,
                        "material_id": material["id"],
                        "email_message_id": message_id,
                        "result": policy_result,
                    },
                )
            client.request_json(
                "POST",
                f"/api/email/jobs/{claimed['id']}/complete",
                {**lease, "message_id": message_id, "result": result},
            )
            print(f"贾维斯已整理邮件：{material['id']}", flush=True)
            return True
        if claimed.get("job_type") == "wechat_classify":
            source_text = material_text(
                material,
                client.get_bytes(f"/api/materials/{material['id']}/content"),
            )
            result = classify_wechat_with_workbuddy(material, source_text)
            _classify_chat_policy(client, material, source_text)
            client.request_json(
                "POST",
                f"/api/wechat/jobs/{claimed['id']}/complete",
                {**lease, "result": result},
            )
            print(f"微信线索已由 贾维斯 判断：{material['id']}", flush=True)
            return True
        image_bytes: bytes | None = None
        if is_audio_video(material):
            transcription = client.request_json(
                "GET", f"/api/materials/{material['id']}/transcript"
            )
            if not transcription.get("text") or not transcription.get("metadata"):
                content = client.get_bytes(f"/api/materials/{material['id']}/content")
                transcription = transcribe_material(material, content)
                client.request_json(
                    "POST",
                    f"/api/materials/{material['id']}/transcript",
                    {
                        **lease,
                        "text": transcription["text"],
                        "language": transcription["language"],
                        "model": transcription["model"],
                        "duration_seconds": transcription["duration_seconds"],
                        "segment_count": transcription["segment_count"],
                    },
                )
            analysis_material = {
                **material,
                "filename": f"{material.get('filename') or '会议录音'}.md",
                "content_type": "text/markdown",
                "text_note": transcription["text"],
            }
            source_text = transcription["text"]
        else:
            content = client.get_bytes(f"/api/materials/{material['id']}/content")
            analysis_material = material
            source_text = material_text(material, content)
            if material.get("source_type") == "image":
                image_bytes = content
            if not source_text.strip():
                if image_bytes:
                    source_text = "请直接理解图片中的工作内容，并提取事项、行动、日期、金额、人员和风险。"
                else:
                    raise RuntimeError("材料尚未提取出可供 贾维斯 理解的文字")
        matters = client.request_json("GET", "/api/matters?limit=60")
        open_actions = client.request_json("GET", "/api/actions?status=open")
        actions_by_matter: dict[str, list[dict]] = {}
        for action in open_actions:
            actions_by_matter.setdefault(str(action.get("matter_id") or ""), []).append(
                {
                    "id": action.get("id"),
                    "title": action.get("title"),
                    "detail": action.get("detail"),
                    "flow_state": action.get("flow_state"),
                    "owner": action.get("owner"),
                    "due_date": action.get("due_date"),
                }
            )
        for matter in matters:
            matter["open_actions"] = actions_by_matter.get(
                str(matter.get("id") or ""), []
            )
        result = (
            analyze_with_workbuddy(
                analysis_material,
                source_text,
                matters,
                image_bytes=image_bytes,
            )
            if image_bytes
            else analyze_with_workbuddy(analysis_material, source_text, matters)
        )
        if material.get("matter_id") and not result.get("matter_id"):
            result["matter_id"] = material["matter_id"]
        client.request_json(
            "POST",
            f"/api/jobs/{claimed['id']}/complete",
            {**lease, "result": result},
        )
        print(f"贾维斯 已整理 {material['id']} → {result['matter_title']}", flush=True)
        return True
    except Exception as error:
        client.request_json(
            "POST",
            f"/api/jobs/{claimed['id']}/fail",
            {**lease, "error": error_text(error)},
        )
        print(f"处理失败 {claimed['id']}: {error}", flush=True)
        return True


def drain_pending_work(
    client: WorkbenchClient, worker_id: str, max_workers: int
) -> int:
    status = client.request_json("GET", "/api/analysis/status") or {}
    pending = max(1, int(status.get("pending") or 0))
    workers = min(pending, max(1, max_workers), MAX_ANALYSIS_CONCURRENCY)

    def drain_lane() -> int:
        completed = 0
        while process_once(client, worker_id):
            completed += 1
        return completed

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="jarvis") as pool:
        completed = sum(pool.map(lambda _: drain_lane(), range(workers)))
    if completed:
        result = client.request_json("POST", "/api/analysis/reconcile", {}) or {}
        print(
            "本批内容已统一比对："
            f"合并 {int(result.get('merged') or 0)} 条，"
            f"过滤 {int(result.get('ignored') or 0)} 条，"
            f"续接现有事项 {int(result.get('continued') or 0)} 条",
            flush=True,
        )
    return completed


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="mac_worker", description="财务工作台 Mac 执行节点"
    )
    parser.add_argument(
        "--base-url", default=os.getenv("WORKBENCH_BASE_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument(
        "--token", default=os.getenv("WORKBENCH_WORKER_TOKEN", "local-worker-token")
    )
    parser.add_argument(
        "--worker-id",
        default=os.getenv("WORKBENCH_WORKER_ID", f"mac-{socket.gethostname()}"),
    )
    parser.add_argument(
        "--sync-inbox",
        default=os.getenv("WORKBENCH_SYNC_INBOX", str(DEFAULT_SYNC_INBOX)),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument(
        "--analysis-concurrency",
        type=int,
        default=int(
            os.getenv(
                "WORKBENCH_ANALYSIS_CONCURRENCY", str(DEFAULT_ANALYSIS_CONCURRENCY)
            )
        ),
    )
    args = parser.parse_args()
    client = WorkbenchClient(args.base_url, args.token)
    sync_inbox = Path(args.sync_inbox).expanduser()
    last_loop_epoch: float | None = None
    last_sync_epoch: float | None = None
    while True:
        now_epoch = time.time()
        reason = scheduled_sync_reason(
            now_epoch,
            last_loop_epoch,
            last_sync_epoch,
            datetime.now().weekday(),
        )
        service_error = False
        if reason:
            try:
                request_incremental_sync(client)
            except Exception as error:
                service_error = True
                print(f"工作台暂未就绪：{error_text(error)}；等待重试。", flush=True)
            else:
                last_sync_epoch = now_epoch
                print(
                    "已在 Mac 启动、唤醒或两小时节点读取新内容；等待手工让贾维斯整理。",
                    flush=True,
                )
        imported = False
        worked = False
        if not service_error:
            try:
                imported = scan_icloud_inbox(client, sync_inbox)
            except Exception as error:
                service_error = True
                print(f"工作台暂未就绪：{error_text(error)}；等待重试。", flush=True)
        if not service_error:
            try:
                worked = bool(
                    drain_pending_work(
                        client, args.worker_id, args.analysis_concurrency
                    )
                )
            except Exception as error:
                service_error = True
                print(f"工作台暂未就绪：{error_text(error)}；等待重试。", flush=True)
        last_loop_epoch = time.time()
        if args.once and not service_error:
            return
        if not imported and not worked:
            time.sleep(max(2, args.poll_seconds))


if __name__ == "__main__":
    main()
