from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .db import Database
from .email_work import EmailWorkService
from .policies import PolicyService
from .schemas import (
    ActionAssigneesRequest,
    ActionPlanningStateRequest,
    ActionResolveRequest,
    AssignmentRequest,
    ChannelIntakeRegisterRequest,
    ChannelIntakeRequest,
    EmailAccountRegisterRequest,
    EmailAnalysisCompleteRequest,
    EmailMessageRequest,
    EmailSyncClaimRequest,
    EmailSyncFinishRequest,
    JobClaimRequest,
    JobCompleteRequest,
    JobFailRequest,
    JobLeaseRequest,
    LearningRuleUpdateRequest,
    LoginRequest,
    MatterProgressRequest,
    MatterUpdateRequest,
    NodeHeartbeatRequest,
    PolicyCandidateIngestRequest,
    PolicyCandidateResolveRequest,
    ReminderResolveRequest,
    ReviewResolveRequest,
    ReviewQueueResolveRequest,
    TranscriptSaveRequest,
    WechatCandidateResolveRequest,
    WechatExportReportRequest,
    WechatSyncClaimRequest,
    WechatSyncFinishRequest,
    WechatSyncRunRequest,
    WechatUnblockRequest,
    WechatWindowRequest,
)
from .security import (
    AuthContext,
    auth_context,
    bearer_context,
    enforce_browser_state_change_policy,
    require_scope,
    session_value,
)
from .services import WorkbenchService
from .wechat import WechatService


APP_DIR = Path(__file__).parent
STATIC_DIR = APP_DIR / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_env()
    configured.prepare()
    database = Database(configured.database_path)
    service = WorkbenchService(configured, database)
    wechat = WechatService(database)
    email_work = EmailWorkService(database)
    policies = PolicyService(configured, database)

    async def refresh_loop() -> None:
        while True:
            await asyncio.to_thread(service.refresh_reminders)
            await asyncio.sleep(60)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        database.initialize()
        service.ensure_search_index()
        reminder_task = asyncio.create_task(refresh_loop())
        try:
            yield
        finally:
            reminder_task.cancel()
            with suppress(asyncio.CancelledError):
                await reminder_task

    app = FastAPI(
        title="Frank 的个人工作台",
        version="0.1.0",
        docs_url="/api/docs" if configured.app_env != "production" else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = configured
    app.state.database = database
    app.state.service = service
    app.state.wechat = wechat
    app.state.email_work = email_work
    app.state.policies = policies

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        try:
            authorization = request.headers.get("authorization", "")
            bearer = None
            if authorization.lower().startswith("bearer "):
                bearer = bearer_context(configured, authorization[7:].strip())
            if bearer is None:
                enforce_browser_state_change_policy(request)
            response = await call_next(request)
        except HTTPException as exc:
            response = JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(self)"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'self'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'; script-src 'self'; "
            "style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
            "connect-src 'self'; worker-src 'self' blob:; manifest-src 'self'"
        )
        if configured.app_env == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, error: ValueError):
        return JSONResponse(
            {"detail": str(error)}, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
        )

    @app.exception_handler(KeyError)
    async def key_error_handler(_: Request, error: KeyError):
        return JSONResponse(
            {"detail": str(error).strip("'")}, status_code=status.HTTP_404_NOT_FOUND
        )

    @app.exception_handler(PermissionError)
    async def permission_error_handler(_: Request, error: PermissionError):
        return JSONResponse({"detail": str(error)}, status_code=status.HTTP_403_FORBIDDEN)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/auth/login")
    def login(payload: LoginRequest, response: Response) -> dict[str, str]:
        if not secrets.compare_digest(payload.passcode, configured.owner_passcode):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "口令不正确")
        response.set_cookie(
            "workbench_session",
            session_value(configured),
            httponly=True,
            secure=configured.app_env == "production",
            samesite="strict",
            max_age=60 * 60 * 24 * 30,
            path="/",
        )
        return {"status": "authenticated"}

    @app.post("/api/auth/logout")
    def logout(response: Response) -> dict[str, str]:
        response.delete_cookie("workbench_session", path="/")
        return {"status": "signed_out"}

    @app.get("/api/auth/session")
    def session(context: AuthContext = Depends(auth_context)) -> dict[str, Any]:
        return {
            "actor": context.actor,
            "role": context.role,
            "scopes": sorted(context.scopes),
            "password_required": not configured.password_disabled,
        }

    @app.post("/api/intake")
    async def intake(
        source_type: Annotated[str, Form()],
        text_note: Annotated[str, Form()] = "",
        matter_id: Annotated[str | None, Form()] = None,
        upload: Annotated[UploadFile | None, File()] = None,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        context: AuthContext = Depends(require_scope("intake:write")),
    ) -> JSONResponse:
        material, created = await service.receive_material(
            source_type,
            idempotency_key or uuid4().hex,
            text_note,
            upload,
            context.actor,
            matter_id,
        )
        return JSONResponse(
            {"material": material, "created": created},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @app.get("/api/materials")
    def materials(
        limit: int = 100,
        material_status: str | None = None,
        _: AuthContext = Depends(require_scope("materials:read")),
    ) -> list[dict[str, Any]]:
        return service.list_materials(min(max(limit, 1), 500), material_status)

    @app.get("/api/materials/{material_id}")
    def material(
        material_id: str,
        _: AuthContext = Depends(require_scope("materials:read")),
    ) -> dict[str, Any]:
        item = service.get_material(material_id)
        if not item:
            raise KeyError("材料不存在")
        return item

    @app.get("/api/materials/{material_id}/content")
    def material_content(
        material_id: str,
        _: AuthContext = Depends(require_scope("materials:read")),
    ) -> Response:
        item = service.database.fetch_one(
            "SELECT * FROM materials WHERE id = ?", (material_id,)
        )
        if not item:
            raise KeyError("材料不存在")
        path = service.material_content_path(material_id)
        if path and path.exists():
            return FileResponse(
                path,
                media_type=item["content_type"] or "application/octet-stream",
                filename=item["filename"] or material_id,
            )
        return PlainTextResponse(item["text_note"] or "")

    @app.post("/api/materials/{material_id}/assign")
    def assign_material(
        material_id: str,
        payload: AssignmentRequest,
        context: AuthContext = Depends(auth_context),
    ) -> dict[str, Any]:
        return service.assign_material(
            material_id, context.actor, payload.matter_id, payload.title
        )

    @app.post("/api/materials/{material_id}/transcript")
    def save_transcript(
        material_id: str,
        payload: TranscriptSaveRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return service.save_transcript(
            material_id,
            payload.worker_id,
            payload.lease_token,
            payload.text,
            {
                "language": payload.language,
                "model": payload.model,
                "duration_seconds": payload.duration_seconds,
                "segment_count": payload.segment_count,
            },
        )

    @app.get("/api/materials/{material_id}/transcript")
    def get_transcript(
        material_id: str,
        _: AuthContext = Depends(require_scope("materials:read")),
    ) -> dict[str, Any]:
        return service.get_transcript(material_id)

    @app.post("/api/materials/{material_id}/assistant")
    def queue_assistant(
        material_id: str,
        context: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return service.queue_workbuddy_analysis(material_id, context.actor)

    @app.post("/api/materials/{material_id}/workbuddy", include_in_schema=False)
    def queue_workbuddy_compat(
        material_id: str,
        context: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return service.queue_workbuddy_analysis(material_id, context.actor)

    @app.get("/api/analysis/status")
    def analysis_status(
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> dict[str, Any]:
        return service.analysis_status()

    @app.post("/api/analysis/run")
    def run_analysis(
        context: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return service.release_analysis(context.actor)

    @app.get("/api/analysis/issues")
    def analysis_issues(
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> list[dict[str, Any]]:
        return service.analysis_issues()

    @app.post("/api/analysis/issues/{job_id}/retry")
    def retry_analysis_issue(
        job_id: str,
        context: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return service.retry_analysis_issue(job_id, context.actor)

    @app.post("/api/analysis/reconcile")
    def reconcile_analysis(
        context: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        result = wechat.consolidate_pending_candidates()
        database.audit(
            f"audit_{uuid4().hex}",
            context.actor,
            "analysis.reconciled",
            "analysis",
            "pending",
            metadata=result,
        )
        return result

    @app.get("/api/matters")
    def matters(
        limit: int = 100,
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> list[dict[str, Any]]:
        return service.list_matters(min(max(limit, 1), 500))

    @app.get("/api/matters/{matter_id}")
    def matter(
        matter_id: str,
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> dict[str, Any]:
        item = service.get_matter(matter_id)
        if not item:
            raise KeyError("事项不存在")
        return item

    @app.get("/api/matters/{matter_id}/timeline")
    def matter_timeline(
        matter_id: str,
        limit: int = 200,
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> list[dict[str, Any]]:
        return service.matter_timeline(matter_id, min(max(limit, 1), 500))

    @app.patch("/api/matters/{matter_id}")
    def update_matter(
        matter_id: str,
        payload: MatterUpdateRequest,
        context: AuthContext = Depends(require_scope("reminders:write")),
    ) -> dict[str, Any]:
        return service.update_matter(
            matter_id, payload.model_dump(exclude_unset=True), context.actor
        )

    @app.post("/api/matters/{matter_id}/progress")
    def add_matter_progress(
        matter_id: str,
        payload: MatterProgressRequest,
        context: AuthContext = Depends(require_scope("reminders:write")),
    ) -> dict[str, Any]:
        return service.add_matter_progress(
            matter_id, payload.summary, payload.detail, context.actor
        )

    @app.post("/api/jobs/claim")
    def claim_job(
        payload: JobClaimRequest,
        _: AuthContext = Depends(require_scope("jobs:claim")),
    ) -> dict[str, Any]:
        return {"job": service.claim_job(payload.worker_id)}

    @app.post("/api/jobs/{job_id}/start")
    def start_job(
        job_id: str,
        payload: JobLeaseRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return service.start_job(job_id, payload.worker_id, payload.lease_token)

    @app.post("/api/jobs/{job_id}/complete")
    def complete_job(
        job_id: str,
        payload: JobCompleteRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return service.complete_job(
            job_id, payload.worker_id, payload.lease_token, payload.result
        )

    @app.post("/api/jobs/{job_id}/fail")
    def fail_job(
        job_id: str,
        payload: JobFailRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return service.fail_job(job_id, payload.worker_id, payload.lease_token, payload.error)

    @app.get("/api/reviews")
    def reviews(
        review_status: str = "pending",
        _: AuthContext = Depends(auth_context),
    ) -> list[dict[str, Any]]:
        return service.list_reviews(review_status)

    @app.post("/api/reviews/{review_id}/resolve")
    def resolve_review(
        review_id: str,
        payload: ReviewResolveRequest,
        context: AuthContext = Depends(auth_context),
    ) -> dict[str, Any]:
        return service.resolve_review(review_id, payload.resolution, payload.note, context.actor)

    @app.post("/api/review-queue/resolve")
    def resolve_review_queue(
        payload: ReviewQueueResolveRequest,
        context: AuthContext = Depends(auth_context),
    ) -> dict[str, Any]:
        review_ids = payload.review_ids or [payload.review_id or payload.item_id]
        review_ids = [review_id for review_id in review_ids if review_id]
        if not review_ids:
            raise ValueError("缺少待确认项 ID")
        return service.resolve_reviews(
            review_ids, payload.resolution, payload.note, context.actor
        )

    @app.post("/api/proactive/scan")
    def proactive_scan(
        _: AuthContext = Depends(require_scope("reminders:write")),
    ) -> dict[str, int]:
        return {"created": service.refresh_reminders()}

    @app.get("/api/overview")
    def overview(
        _: AuthContext = Depends(require_scope("reminders:read")),
    ) -> dict[str, Any]:
        return service.overview()

    @app.get("/api/today/brief")
    def today_brief(
        brief_date: str | None = None,
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> dict[str, Any]:
        return service.today_brief(brief_date)

    @app.get("/api/search")
    def search(
        q: str = "",
        limit: int = 50,
        source: str = "",
        status: str = "",
        date_from: str = "",
        date_to: str = "",
        amount: str = "",
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> dict[str, Any]:
        return service.search(
            q,
            min(max(limit, 1), 200),
            source=source,
            status=status,
            date_from=date_from,
            date_to=date_to,
            amount=amount,
        )

    @app.get("/api/activity/receipts")
    def activity_receipts(
        limit: int = 100,
        matter_id: str | None = None,
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> list[dict[str, Any]]:
        return service.activity_receipts(min(max(limit, 1), 500), matter_id)

    @app.get("/api/weekly-review")
    def weekly_review(
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> dict[str, Any]:
        return service.weekly_review()

    @app.get("/api/learning-rules")
    def learning_rules(
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> list[dict[str, Any]]:
        return service.list_learning_rules()

    @app.patch("/api/learning-rules/{rule_id}")
    def update_learning_rule(
        rule_id: str,
        payload: LearningRuleUpdateRequest,
        context: AuthContext = Depends(auth_context),
    ) -> dict[str, Any]:
        return service.update_learning_rule(rule_id, payload.enabled, context.actor)

    @app.post("/api/reminders/{reminder_id}/resolve")
    def resolve_reminder(
        reminder_id: str,
        payload: ReminderResolveRequest,
        context: AuthContext = Depends(require_scope("reminders:write")),
    ) -> dict[str, Any]:
        return service.resolve_reminder(reminder_id, payload.status, context.actor)

    @app.post("/api/actions/{action_id}/resolve")
    def resolve_action(
        action_id: str,
        payload: ActionResolveRequest,
        context: AuthContext = Depends(require_scope("reminders:write")),
    ) -> dict[str, Any]:
        return service.resolve_action(action_id, payload.status, context.actor)

    @app.patch("/api/actions/{action_id}/planning-state")
    def update_action_planning(
        action_id: str,
        payload: ActionPlanningStateRequest,
        context: AuthContext = Depends(require_scope("reminders:write")),
    ) -> dict[str, Any]:
        return service.update_action_planning(
            action_id,
            payload.model_dump(exclude_unset=True),
            context.actor,
        )

    @app.get("/api/people")
    def people(_: AuthContext = Depends(require_scope("matters:read"))) -> list[dict[str, Any]]:
        return service.list_people()

    @app.get("/api/people/{person_id}/identities")
    def person_identities(
        person_id: str,
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> list[dict[str, Any]]:
        return service.list_person_identities(person_id)

    @app.get("/api/actions")
    def actions(
        status: str = "open",
        person_id: str | None = None,
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> list[dict[str, Any]]:
        return service.list_actions(status, person_id)

    @app.get("/api/assignee-reviews")
    def assignee_reviews(
        status: str = "pending",
        _: AuthContext = Depends(require_scope("matters:read")),
    ) -> list[dict[str, Any]]:
        return service.list_assignee_reviews(status)

    @app.put("/api/actions/{action_id}/assignees")
    def set_action_assignees(
        action_id: str,
        payload: ActionAssigneesRequest,
        context: AuthContext = Depends(require_scope("reminders:write")),
    ) -> dict[str, Any]:
        return service.set_action_assignees(
            action_id, payload.person_ids, payload.note, context.actor
        )

    @app.post("/api/nodes/heartbeat")
    def node_heartbeat(
        payload: NodeHeartbeatRequest,
        _: AuthContext = Depends(require_scope("nodes:write")),
    ) -> dict[str, Any]:
        return service.heartbeat(payload.node_id, payload.name, payload.metadata)

    @app.get("/api/nodes")
    def nodes(_: AuthContext = Depends(auth_context)) -> list[dict[str, Any]]:
        return service.node_status()

    @app.get("/api/audit")
    def audit(
        limit: int = 100,
        context: AuthContext = Depends(auth_context),
    ) -> list[dict[str, Any]]:
        if context.role != "owner":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "仅财务负责人可查看审计记录")
        return service.audit_events(min(max(limit, 1), 500))

    @app.post("/api/email/sync/run")
    def request_email_sync(
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return email_work.request_sync(context.actor)

    @app.post("/api/email/sync/claim")
    def claim_email_sync(
        payload: EmailSyncClaimRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return {"request": email_work.claim_sync(payload.worker_id)}

    @app.post("/api/email/sync/{request_id}/finish")
    def finish_email_sync(
        request_id: str,
        payload: EmailSyncFinishRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return email_work.finish_sync(request_id, payload.model_dump())

    @app.post("/api/email/accounts/register")
    def register_email_account(
        payload: EmailAccountRegisterRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return email_work.register_account(payload.model_dump())

    @app.get("/api/email/accounts/{account_id}/state")
    def email_account_state(
        account_id: str,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return email_work.account_state(account_id)

    @app.post("/api/email/messages")
    def ingest_email_message(
        payload: EmailMessageRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return email_work.ingest_message(payload.model_dump())

    @app.post("/api/email/jobs/{job_id}/complete")
    def complete_email_analysis(
        job_id: str,
        payload: EmailAnalysisCompleteRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        service.reserve_job_result(job_id, payload.worker_id, payload.lease_token)
        try:
            message = email_work.complete_analysis(payload.message_id, payload.result)
        except Exception:
            service.release_job_result(job_id, payload.worker_id, payload.lease_token)
            raise
        service.complete_auxiliary_job(
            job_id, payload.worker_id, payload.lease_token, reserved=True
        )
        return message

    @app.get("/api/email/status")
    def email_status(
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> dict[str, Any]:
        return email_work.status()

    @app.get("/api/email/messages")
    def email_messages(
        message_status: str = "active",
        limit: int = 100,
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> list[dict[str, Any]]:
        if message_status not in {"active", "ignored"}:
            raise ValueError("不支持的邮件状态")
        return email_work.list_messages(message_status, min(max(limit, 1), 300))

    @app.post("/api/email/messages/{message_id}/ignore")
    def ignore_email_message(
        message_id: str,
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return email_work.ignore_message(message_id, context.actor)

    @app.post("/api/email/messages/{message_id}/confirm")
    def confirm_email_message(
        message_id: str,
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return email_work.confirm_message(message_id, context.actor)

    @app.post("/api/email/messages/{message_id}/restore")
    def restore_email_message(
        message_id: str,
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return email_work.restore_message(message_id, context.actor)

    @app.get("/api/email/matters")
    def email_matters(
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> list[dict[str, Any]]:
        ids = email_work.matter_ids()
        return [item for item in service.list_matters(500) if item["id"] in ids]

    @app.post("/api/policy-candidates")
    def ingest_policy_candidate(
        payload: PolicyCandidateIngestRequest,
        _: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return policies.ingest_candidate(payload.model_dump())

    @app.get("/api/policies/status")
    def policy_status(
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> dict[str, Any]:
        return policies.status()

    @app.get("/api/policies")
    def list_policies(
        policy_status: str = "active",
        q: str = "",
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> list[dict[str, Any]]:
        return policies.list_policies(policy_status, q)

    @app.get("/api/policies/{policy_id}/versions")
    def list_policy_versions(
        policy_id: str,
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> list[dict[str, Any]]:
        return policies.versions(policy_id)

    @app.get("/api/policies/identity/hint")
    def policy_identity_hint(
        source_type: str,
        source_key: str,
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> dict[str, Any]:
        return policies.identity_hint(source_type, source_key)

    @app.get("/api/policy-candidates")
    def list_policy_candidates(
        candidate_status: str = "pending",
        limit: int = 100,
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> list[dict[str, Any]]:
        return policies.list_candidates(candidate_status, limit)

    @app.post("/api/policy-candidates/{candidate_id}/resolve")
    def resolve_policy_candidate(
        candidate_id: str,
        payload: PolicyCandidateResolveRequest,
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        try:
            candidate = policies.get_candidate(candidate_id)
            resolved = policies.resolve_candidate(
                candidate_id, payload.action, context.actor, payload.policy_id
            )
            if payload.action == "temporary" and candidate.get("material_id"):
                material = database.fetch_one(
                    "SELECT matter_id FROM materials WHERE id = ?",
                    (candidate["material_id"],),
                )
                if material and not material.get("matter_id"):
                    matter = service.assign_material(
                        candidate["material_id"],
                        context.actor,
                        title=candidate.get("title") or "临时工作事项",
                    )
                    resolved["matter_id"] = matter["id"]
            return resolved
        except OSError as error:
            policies.record_sync_error(candidate_id, error)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="规定已保留在待确认，Obsidian 暂时无法写入，请稍后重试。",
            ) from error

    @app.post("/api/policies/obsidian/sync")
    def sync_policies_to_obsidian(
        _: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        try:
            return policies.sync_obsidian()
        except OSError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Obsidian 暂时无法写入，请检查目录后重试。",
            ) from error

    @app.post("/api/wechat/sync/run")
    def run_wechat_sync(
        payload: WechatSyncRunRequest | None = None,
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return wechat.request_sync(
            context.actor,
            sources=payload.sources if payload else None,
        )

    @app.post("/api/wechat/sync/claim")
    def claim_wechat_sync(
        payload: WechatSyncClaimRequest,
        _: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return {"request": wechat.claim_sync(payload.worker_id)}

    @app.post("/api/wechat/sync/{request_id}/finish")
    def finish_wechat_sync(
        request_id: str,
        payload: WechatSyncFinishRequest,
        _: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return wechat.finish_sync(
            request_id,
            payload.worker_id,
            payload.status,
            payload.error,
            payload.message_count,
            payload.window_count,
            payload.skipped_count,
        )

    @app.get("/api/wechat/status")
    def wechat_status(
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> dict[str, Any]:
        return wechat.status()

    @app.post("/api/wechat/windows")
    def receive_wechat_window(
        payload: WechatWindowRequest,
        _: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return wechat.ingest_window(payload.model_dump())

    @app.post("/api/wechat/jobs/{job_id}/complete")
    def complete_wechat_job(
        job_id: str,
        payload: JobCompleteRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return wechat.complete_classification(
            job_id, payload.worker_id, payload.lease_token, payload.result
        )

    @app.get("/api/wechat/candidates")
    def wechat_candidates(
        candidate_status: str = "pending",
        source: str | None = None,
        limit: int = 100,
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> list[dict[str, Any]]:
        return wechat.list_candidates(candidate_status, limit, source)

    @app.get("/api/wechat/export")
    def export_listening_wechat(
        since: str | None = None,
        limit: int = 500,
        offset: int = 0,
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> list[dict[str, Any]]:
        return wechat.export_listening_windows(since, limit, offset)

    @app.post("/api/wechat/export/report")
    def report_wechat_export(
        payload: WechatExportReportRequest,
        _: AuthContext = Depends(require_scope("jobs:write")),
    ) -> dict[str, Any]:
        return wechat.report_export(payload.model_dump())

    @app.post("/api/wechat/candidates/{candidate_id}/resolve")
    def resolve_wechat_candidate(
        candidate_id: str,
        payload: WechatCandidateResolveRequest,
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return wechat.resolve_candidate(
            candidate_id, payload.action, context.actor, payload.matter_id
        )

    @app.get("/api/wechat/conversations")
    def wechat_conversations(
        q: str = "",
        source: str | None = None,
        _: AuthContext = Depends(require_scope("wechat:read")),
    ) -> list[dict[str, Any]]:
        return wechat.list_conversations(q, source)

    @app.post("/api/wechat/conversations/{session_id}/block")
    def block_wechat_conversation(
        session_id: str,
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return wechat.block_conversation(session_id, context.actor)

    @app.post("/api/wechat/conversations/{session_id}/unblock")
    def unblock_wechat_conversation(
        session_id: str,
        payload: WechatUnblockRequest,
        context: AuthContext = Depends(require_scope("wechat:write")),
    ) -> dict[str, Any]:
        return wechat.unblock_conversation(session_id, context.actor, payload.rescan_days)

    @app.post("/api/materials/{material_id}/retract")
    def retract_material(
        material_id: str,
        context: AuthContext = Depends(require_scope("intake:write")),
    ) -> dict[str, Any]:
        return wechat.retract_material(material_id, context.actor)

    @app.post("/api/channel-intake")
    async def channel_intake(
        payload: ChannelIntakeRequest,
        context: AuthContext = Depends(require_scope("intake:write")),
    ) -> JSONResponse:
        key = payload.external_message_id or uuid4().hex
        source_type = {
            "wechat": "wechat_channel",
            "wecom": "wecom_channel",
            "assistant": "workbuddy_channel",
        }[payload.channel]
        material, created = await service.receive_material(
            source_type, f"{payload.channel}:{key}", payload.text, None, context.actor,
            payload.matter_id,
        )
        if created:
            wechat.record_channel_intake(
                material["id"], payload.channel, payload.external_message_id,
                payload.sender, payload.sent_at, payload.file_type,
            )
        return JSONResponse(
            {"material": material, "created": created, "undoable": created},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    @app.post("/api/channel-intakes/register")
    def register_channel_intake(
        payload: ChannelIntakeRegisterRequest,
        _: AuthContext = Depends(require_scope("intake:write")),
    ) -> dict[str, bool]:
        wechat.record_channel_intake(
            payload.material_id, payload.channel, payload.external_message_id,
            payload.sender, payload.sent_at, payload.file_type,
        )
        return {"recorded": True}

    @app.post("/api/channel-intakes/undo-last")
    def undo_last_channel_intake(
        channel: str = "assistant",
        context: AuthContext = Depends(require_scope("intake:write")),
    ) -> dict[str, Any]:
        return wechat.undo_last_channel_intake(channel, context.actor)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest() -> FileResponse:
        return FileResponse(STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
        )

    @app.get("/{path:path}", include_in_schema=False)
    def app_shell(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(status.HTTP_404_NOT_FOUND)
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
