from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class LoginRequest(BaseModel):
    passcode: str = Field(min_length=1, max_length=256)


class AssignmentRequest(BaseModel):
    matter_id: str | None = None
    title: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def require_target(self) -> "AssignmentRequest":
        if not self.matter_id and not self.title:
            raise ValueError("必须选择已有事项或填写新事项标题")
        return self


class MatterUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    summary: str | None = Field(default=None, max_length=4000)
    contact_name: str | None = Field(default=None, max_length=80)
    target_date: str | None = Field(default=None, max_length=10)
    next_review_date: str | None = Field(default=None, max_length=10)
    status: Literal["active", "completed", "dismissed"] | None = None


class MatterProgressRequest(BaseModel):
    summary: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=2000)
    next_review_date: str | None = Field(default=None, max_length=10)


class ContactIdentityRequest(BaseModel):
    source: Literal["personal_wechat", "wecom", "email"]
    stable_id: str = Field(min_length=1, max_length=300)
    display_name: str = Field(min_length=1, max_length=160)
    organization: str = Field(default="", max_length=200)
    role: str = Field(default="", max_length=160)


class CommitmentRequest(BaseModel):
    source: Literal["personal_wechat", "wecom", "email", "meeting", "manual"]
    source_ref: str = Field(min_length=1, max_length=300)
    category: Literal[
        "waiting_reply",
        "my_commitment",
        "their_commitment",
        "waiting_approval",
        "need_follow_up",
    ]
    summary: str = Field(min_length=1, max_length=500)
    contact_id: str | None = Field(default=None, max_length=160)
    matter_id: str | None = Field(default=None, max_length=160)
    action_id: str | None = Field(default=None, max_length=160)
    due_at: str | None = Field(default=None, max_length=80)
    next_follow_up_at: str | None = Field(default=None, max_length=80)
    evidence: list[str] = Field(default_factory=list, max_length=4)


class CommitmentStatusRequest(BaseModel):
    status: Literal["open", "done", "dismissed"]


class JobClaimRequest(BaseModel):
    worker_id: str = Field(min_length=2, max_length=120)


class JobLeaseRequest(BaseModel):
    worker_id: str = Field(min_length=2, max_length=120)
    lease_token: str = Field(min_length=16, max_length=256)


class JobFailRequest(JobLeaseRequest):
    error: str = Field(min_length=1, max_length=1000)


class JobCompleteRequest(JobLeaseRequest):
    result: dict[str, Any]


class TranscriptSaveRequest(JobLeaseRequest):
    text: str = Field(min_length=1, max_length=500_000)
    language: str = Field(default="zh", min_length=2, max_length=20)
    model: str = Field(min_length=1, max_length=200)
    duration_seconds: float = Field(default=0, ge=0)
    segment_count: int = Field(default=0, ge=0)


class ReviewResolveRequest(BaseModel):
    resolution: Literal["accepted", "rejected", "edited"]
    note: str = Field(default="", max_length=500)


class ReminderResolveRequest(BaseModel):
    status: Literal["done", "dismissed", "snoozed"]


class ActionResolveRequest(BaseModel):
    status: Literal["open", "done", "dismissed"]


class ActionPlanningStateRequest(BaseModel):
    flow_state: Literal["needs_action", "waiting", "blocked", "needs_decision"] | None = None
    waiting_on: str | None = Field(default=None, max_length=160)
    blocked_reason: str | None = Field(default=None, max_length=500)
    next_follow_up_at: str | None = Field(default=None, max_length=80)
    estimated_minutes: int | None = Field(default=None, ge=1, le=100_000)
    pinned: bool | None = None
    snoozed_until: str | None = Field(default=None, max_length=80)
    completion_evidence: list[str] | None = Field(default=None, max_length=12)


class ReviewQueueResolveRequest(BaseModel):
    review_id: str | None = Field(default=None, max_length=160)
    item_id: str | None = Field(default=None, max_length=160)
    review_ids: list[str] = Field(default_factory=list, max_length=100)
    resolution: Literal["accepted", "rejected", "edited"]
    note: str = Field(default="", max_length=500)


class LearningRuleUpdateRequest(BaseModel):
    enabled: bool


class ActionAssigneesRequest(BaseModel):
    person_ids: list[str] = Field(default_factory=list, max_length=12)
    note: str = Field(default="", max_length=500)


class EmailSyncClaimRequest(BaseModel):
    worker_id: str = Field(min_length=2, max_length=120)


class EmailAccountRegisterRequest(BaseModel):
    account_id: str = Field(min_length=16, max_length=128)
    address_hint: str = Field(min_length=3, max_length=160)
    imap_host: str = Field(min_length=3, max_length=255)
    folder: str = Field(default="INBOX", min_length=1, max_length=160)


class EmailSyncFinishRequest(BaseModel):
    worker_id: str = Field(min_length=2, max_length=120)
    status: Literal["completed", "failed"]
    error: str = Field(default="", max_length=1000)
    account_id: str = Field(default="", max_length=128)
    uid_validity: str = Field(default="", max_length=160)
    last_uid: int = Field(default=0, ge=0)
    scanned_count: int = Field(default=0, ge=0)
    pending_count: int = Field(default=0, ge=0)
    ignored_count: int = Field(default=0, ge=0)


class EmailMessageRequest(BaseModel):
    account_id: str = Field(min_length=16, max_length=128)
    folder: str = Field(default="INBOX", min_length=1, max_length=160)
    uid_validity: str = Field(min_length=1, max_length=160)
    uid: int = Field(gt=0)
    message_id_hash: str = Field(default="", max_length=128)
    thread_key: str = Field(min_length=8, max_length=160)
    sender_key: str = Field(min_length=8, max_length=160)
    sender_name: str = Field(default="", max_length=160)
    sender_hint: str = Field(default="", max_length=160)
    subject: str = Field(default="", max_length=300)
    sent_at: str | None = Field(default=None, max_length=80)
    classification: Literal["pending", "work", "irrelevant"]
    needs_follow_up: bool
    summary: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=500)
    evidence: list[str] = Field(default_factory=list, max_length=4)
    matter_id: str | None = Field(default=None, max_length=160)
    matter_title: str = Field(default="", max_length=120)
    actions: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    source_text: str = Field(default="", max_length=100_000)
    attachment_paths: list[str] = Field(default_factory=list, max_length=20)


class EmailAnalysisCompleteRequest(JobLeaseRequest):
    message_id: str = Field(min_length=4, max_length=160)
    result: dict[str, Any]


class PolicyCandidateIngestRequest(BaseModel):
    source_type: Literal["personal_wechat", "wecom", "email"]
    source_ref: str = Field(min_length=1, max_length=300)
    source_label: str = Field(default="", max_length=300)
    material_id: str | None = Field(default=None, max_length=160)
    email_message_id: str | None = Field(default=None, max_length=160)
    result: dict[str, Any]


class PolicyCandidateResolveRequest(BaseModel):
    action: Literal["apply", "ignore", "merge", "temporary", "undo"]
    policy_id: str | None = Field(default=None, max_length=160)


class NodeHeartbeatRequest(BaseModel):
    node_id: str = Field(min_length=2, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    metadata: dict[str, Any] = Field(default_factory=dict)


class WechatSyncClaimRequest(BaseModel):
    worker_id: str = Field(min_length=2, max_length=120)


class WechatSyncRunRequest(BaseModel):
    sources: list[Literal["personal_wechat", "wecom"]] = Field(
        default_factory=lambda: ["personal_wechat", "wecom"],
        min_length=1,
        max_length=2,
    )


class WechatSyncFinishRequest(BaseModel):
    worker_id: str = Field(min_length=2, max_length=120)
    status: Literal["completed", "failed", "unsupported"]
    error: str = Field(default="", max_length=1000)
    message_count: int = Field(default=0, ge=0)
    window_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)


class WechatExportReportRequest(BaseModel):
    status: Literal["completed", "failed"]
    conversation_count: int = Field(default=0, ge=0)
    message_count: int = Field(default=0, ge=0)
    file_count: int = Field(default=0, ge=0)
    output_paths: list[str] = Field(default_factory=list, max_length=4)
    error: str = Field(default="", max_length=1000)


class WechatCursor(BaseModel):
    model_config = ConfigDict(extra="allow")

    sortSeq: int
    createTime: int = Field(gt=0)
    localId: int


class WechatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    timestamp: int = Field(gt=0)
    timestampMs: int | None = Field(default=None, gt=0)
    direction: Literal["in", "out"]
    kind: str = Field(min_length=1, max_length=80)
    text: str = Field(default="", max_length=100_000)
    cursor: WechatCursor
    media: dict[str, Any] | None = None


class WechatWindowRequest(BaseModel):
    source: Literal["personal_wechat", "wecom"] = "personal_wechat"
    account_fingerprint: str = Field(min_length=4, max_length=128)
    session_id: str = Field(min_length=1, max_length=256)
    display_name: str = Field(min_length=1, max_length=160)
    kind: Literal["friend", "group", "official", "other"]
    messages: list[WechatMessage] = Field(min_length=1, max_length=200)


class WechatCandidateResolveRequest(BaseModel):
    action: Literal["accept", "ignore", "merge", "restore", "undo"]
    matter_id: str | None = Field(default=None, max_length=160)


class WechatUnblockRequest(BaseModel):
    rescan_days: int | None = Field(default=None, ge=1, le=30)


class ChannelIntakeRequest(BaseModel):
    text: str = Field(default="", max_length=500_000)
    channel: Literal["wechat", "wecom", "assistant"]
    external_message_id: str | None = Field(default=None, max_length=256)
    sender: str | None = Field(default=None, max_length=160)
    sent_at: str | None = Field(default=None, max_length=64)
    file_type: str | None = Field(default=None, max_length=80)
    matter_id: str | None = Field(default=None, max_length=160)


class ChannelIntakeRegisterRequest(BaseModel):
    material_id: str = Field(min_length=4, max_length=160)
    channel: Literal["wechat", "wecom", "assistant"]
    external_message_id: str | None = Field(default=None, max_length=256)
    sender: str | None = Field(default=None, max_length=160)
    sent_at: str | None = Field(default=None, max_length=64)
    file_type: str | None = Field(default=None, max_length=80)
