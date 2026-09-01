from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .assignees import seed_people


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS matters (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '财务负责人',
    target_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS materials (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL,
    source_type TEXT NOT NULL,
    filename TEXT,
    content_type TEXT,
    size INTEGER NOT NULL DEFAULT 0,
    text_note TEXT NOT NULL DEFAULT '',
    storage_key TEXT,
    status TEXT NOT NULL,
    matter_id TEXT REFERENCES matters(id),
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_hash
ON materials(sha256, source_type);
CREATE INDEX IF NOT EXISTS idx_materials_status
ON materials(status, received_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    job_type TEXT NOT NULL DEFAULT 'analyze',
    status TEXT NOT NULL,
    requires_local INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL DEFAULT 50,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TEXT,
    next_attempt_at TEXT,
    error TEXT,
    result_version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(material_id, job_type)
);

CREATE INDEX IF NOT EXISTS idx_jobs_claim
ON jobs(status, next_attempt_at, priority DESC, created_at);

CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES matters(id),
    material_id TEXT NOT NULL REFERENCES materials(id),
    claim_type TEXT NOT NULL,
    field_type TEXT NOT NULL,
    value TEXT NOT NULL,
    source_locator TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    confidence REAL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_matter
ON evidence(matter_id, created_at DESC);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES matters(id),
    material_id TEXT REFERENCES materials(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    owner TEXT,
    due_date TEXT,
    evidence_id TEXT REFERENCES evidence(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_due
ON actions(status, due_date);

CREATE TABLE IF NOT EXISTS people (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    phonetic_aliases_json TEXT NOT NULL DEFAULT '[]',
    is_self INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS person_identities (
    id TEXT PRIMARY KEY,
    person_id TEXT NOT NULL REFERENCES people(id),
    source TEXT NOT NULL,
    stable_id TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, stable_id)
);

CREATE TABLE IF NOT EXISTS action_assignees (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(id),
    person_id TEXT NOT NULL REFERENCES people(id),
    status TEXT NOT NULL DEFAULT 'pending',
    alias TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    source_material_id TEXT REFERENCES materials(id),
    suggested_by TEXT NOT NULL DEFAULT 'jarvis',
    confirmed_by TEXT,
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(action_id, person_id)
);

CREATE INDEX IF NOT EXISTS idx_action_assignees_status
ON action_assignees(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_assignees_person
ON action_assignees(person_id, status);

CREATE TABLE IF NOT EXISTS review_items (
    id TEXT PRIMARY KEY,
    matter_id TEXT REFERENCES matters(id),
    material_id TEXT REFERENCES materials(id),
    evidence_id TEXT REFERENCES evidence(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_status
ON review_items(status, created_at DESC);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    matter_id TEXT REFERENCES matters(id),
    action_id TEXT REFERENCES actions(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    fingerprint TEXT NOT NULL UNIQUE,
    due_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reminders_status
ON reminders(status, created_at DESC);

CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS wechat_conversations (
    session_id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'personal_wechat',
    display_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    listen_status TEXT NOT NULL DEFAULT 'active',
    last_message_at TEXT,
    blocked_at TEXT,
    listen_from TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wechat_conversations_status
ON wechat_conversations(listen_status, last_message_at DESC);

CREATE TABLE IF NOT EXISTS wechat_sync_state (
    account_fingerprint TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES wechat_conversations(session_id),
    source TEXT NOT NULL DEFAULT 'personal_wechat',
    sort_seq INTEGER NOT NULL DEFAULT 0,
    create_time INTEGER NOT NULL DEFAULT 0,
    local_id INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(account_fingerprint, session_id)
);

CREATE TABLE IF NOT EXISTS wechat_seen_messages (
    session_id TEXT NOT NULL REFERENCES wechat_conversations(session_id),
    source TEXT NOT NULL DEFAULT 'personal_wechat',
    sort_seq INTEGER NOT NULL,
    create_time INTEGER NOT NULL,
    local_id INTEGER NOT NULL,
    material_id TEXT REFERENCES materials(id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(session_id, sort_seq, create_time, local_id)
);

CREATE TABLE IF NOT EXISTS wechat_sync_requests (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'personal_wechat',
    status TEXT NOT NULL DEFAULT 'pending',
    mode TEXT NOT NULL DEFAULT 'incremental',
    requested_by TEXT NOT NULL,
    worker_id TEXT,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    error TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    window_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_wechat_sync_requests_status
ON wechat_sync_requests(status, requested_at DESC);

CREATE TABLE IF NOT EXISTS wechat_candidates (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL UNIQUE REFERENCES materials(id),
    session_id TEXT NOT NULL REFERENCES wechat_conversations(session_id),
    source TEXT NOT NULL DEFAULT 'personal_wechat',
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    classification TEXT,
    summary TEXT NOT NULL DEFAULT '',
    uncertainty_reason TEXT NOT NULL DEFAULT '',
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'processing',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    extracted_json TEXT NOT NULL DEFAULT '{}',
    matter_id TEXT REFERENCES matters(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_wechat_candidates_status
ON wechat_candidates(status, created_at DESC);

CREATE TABLE IF NOT EXISTS wechat_export_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'idle',
    conversation_count INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    output_paths_json TEXT NOT NULL DEFAULT '[]',
    last_success_at TEXT,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS channel_intakes (
    id TEXT PRIMARY KEY,
    material_id TEXT NOT NULL REFERENCES materials(id),
    channel TEXT NOT NULL,
    external_message_id TEXT,
    sender TEXT,
    sent_at TEXT,
    file_type TEXT,
    retracted_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(channel, external_message_id)
);

CREATE TABLE IF NOT EXISTS email_accounts (
    account_id TEXT PRIMARY KEY,
    address_hint TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT 'INBOX',
    uid_validity TEXT NOT NULL DEFAULT '',
    last_uid INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'waiting',
    last_success_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS email_sync_requests (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    claimed_at TEXT,
    completed_at TEXT,
    worker_id TEXT,
    error TEXT NOT NULL DEFAULT '',
    scanned_count INTEGER NOT NULL DEFAULT 0,
    pending_count INTEGER NOT NULL DEFAULT 0,
    ignored_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_email_sync_requests_status
ON email_sync_requests(status, requested_at DESC);

CREATE TABLE IF NOT EXISTS email_messages (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES email_accounts(account_id),
    folder TEXT NOT NULL,
    uid_validity TEXT NOT NULL,
    uid INTEGER NOT NULL,
    message_id_hash TEXT NOT NULL DEFAULT '',
    thread_key TEXT NOT NULL,
    sender_key TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    sender_hint TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    classification TEXT NOT NULL,
    needs_follow_up INTEGER NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '[]',
    confidence REAL,
    source_text TEXT NOT NULL DEFAULT '',
    material_id TEXT REFERENCES materials(id),
    status TEXT NOT NULL,
    matter_id TEXT REFERENCES matters(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, folder, uid_validity, uid)
);

CREATE INDEX IF NOT EXISTS idx_email_messages_status
ON email_messages(status, sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_email_messages_thread
ON email_messages(account_id, thread_key, sent_at DESC);

CREATE TABLE IF NOT EXISTS company_policies (
    id TEXT PRIMARY KEY,
    canonical_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    topic TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    requirements_json TEXT NOT NULL DEFAULT '[]',
    effective_date TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    version INTEGER NOT NULL DEFAULT 1,
    obsidian_path TEXT NOT NULL DEFAULT '',
    last_verified_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_company_policies_status
ON company_policies(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS company_policy_candidates (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_label TEXT NOT NULL DEFAULT '',
    material_id TEXT REFERENCES materials(id),
    email_message_id TEXT REFERENCES email_messages(id),
    change_type TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    topic TEXT NOT NULL DEFAULT '',
    scope TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    requirements_json TEXT NOT NULL DEFAULT '[]',
    change_summary TEXT NOT NULL DEFAULT '',
  effective_date TEXT,
  confidence REAL,
  is_authority INTEGER NOT NULL DEFAULT 0,
  evidence_json TEXT NOT NULL DEFAULT '[]',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    matched_policy_id TEXT REFERENCES company_policies(id),
    status TEXT NOT NULL DEFAULT 'pending',
    obsidian_error TEXT NOT NULL DEFAULT '',
    auto_undo_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(source_type, source_ref)
);

CREATE INDEX IF NOT EXISTS idx_company_policy_candidates_status
ON company_policy_candidates(status, created_at DESC);

CREATE TABLE IF NOT EXISTS company_policy_versions (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL REFERENCES company_policies(id),
    version INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    attachments_json TEXT NOT NULL DEFAULT '[]',
    candidate_id TEXT NOT NULL REFERENCES company_policy_candidates(id),
    created_at TEXT NOT NULL,
    UNIQUE(policy_id, version)
);

CREATE TABLE IF NOT EXISTS company_policy_identity_feedback (
    source_type TEXT NOT NULL,
    source_key TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    is_authority INTEGER NOT NULL,
    confidence REAL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(source_type, source_key)
);

CREATE TABLE IF NOT EXISTS company_policy_sync_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'idle',
    written_count INTEGER NOT NULL DEFAULT 0,
    last_success_at TEXT,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    matter_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_object
ON audit_events(object_type, object_id, created_at DESC);

CREATE TABLE IF NOT EXISTS matter_events (
    id TEXT PRIMARY KEY,
    matter_id TEXT NOT NULL REFERENCES matters(id),
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    object_type TEXT,
    object_id TEXT,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_matter_events_timeline
ON matter_events(matter_id, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS daily_briefs (
    id TEXT PRIMARY KEY,
    brief_date TEXT NOT NULL UNIQUE,
    generated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS learned_rules (
    id TEXT PRIMARY KEY,
    rule_type TEXT NOT NULL,
    description TEXT NOT NULL,
    pattern_json TEXT NOT NULL DEFAULT '{}',
    source_count INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_learned_rules_enabled
ON learned_rules(enabled, updated_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
    entity_id UNINDEXED,
    entity_type UNINDEXED,
    matter_id UNINDEXED,
    title,
    body,
    created_at UNINDEXED,
    tokenize = 'unicode61'
);

CREATE TABLE IF NOT EXISTS search_index_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 0
);
"""


SEARCH_TRIGGERS = """
DROP TRIGGER IF EXISTS search_fts_matters_insert;
DROP TRIGGER IF EXISTS search_fts_matters_update;
DROP TRIGGER IF EXISTS search_fts_matters_delete;
CREATE TRIGGER search_fts_matters_insert AFTER INSERT ON matters
BEGIN
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (NEW.id, 'matter', NEW.id, NEW.title, COALESCE(NEW.summary, ''), NEW.created_at);
END;
CREATE TRIGGER search_fts_matters_update AFTER UPDATE ON matters
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'matter';
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (NEW.id, 'matter', NEW.id, NEW.title, COALESCE(NEW.summary, ''), NEW.created_at);
END;
CREATE TRIGGER search_fts_matters_delete AFTER DELETE ON matters
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'matter';
END;

DROP TRIGGER IF EXISTS search_fts_actions_insert;
DROP TRIGGER IF EXISTS search_fts_actions_update;
DROP TRIGGER IF EXISTS search_fts_actions_delete;
CREATE TRIGGER search_fts_actions_insert AFTER INSERT ON actions
BEGIN
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (
        NEW.id,
        'action',
        NEW.matter_id,
        NEW.title,
        trim(
            COALESCE(NEW.detail, '') || ' ' ||
            COALESCE(NEW.waiting_on, '') || ' ' ||
            COALESCE(NEW.blocked_reason, '') || ' ' ||
            COALESCE(NEW.completion_evidence, '')
        ),
        NEW.created_at
    );
END;
CREATE TRIGGER search_fts_actions_update AFTER UPDATE ON actions
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'action';
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (
        NEW.id,
        'action',
        NEW.matter_id,
        NEW.title,
        trim(
            COALESCE(NEW.detail, '') || ' ' ||
            COALESCE(NEW.waiting_on, '') || ' ' ||
            COALESCE(NEW.blocked_reason, '') || ' ' ||
            COALESCE(NEW.completion_evidence, '')
        ),
        NEW.created_at
    );
END;
CREATE TRIGGER search_fts_actions_delete AFTER DELETE ON actions
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'action';
END;

DROP TRIGGER IF EXISTS search_fts_materials_insert;
DROP TRIGGER IF EXISTS search_fts_materials_update;
DROP TRIGGER IF EXISTS search_fts_materials_delete;
CREATE TRIGGER search_fts_materials_insert AFTER INSERT ON materials
WHEN NEW.source_type NOT IN ('wechat_auto', 'wechat_channel', 'wecom_channel', 'workbuddy_channel')
     OR NEW.matter_id IS NOT NULL
BEGIN
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (NEW.id, 'material', NEW.matter_id, COALESCE(NEW.filename, '材料'), COALESCE(NEW.text_note, ''), NEW.received_at);
END;
CREATE TRIGGER search_fts_materials_update AFTER UPDATE ON materials
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'material';
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    SELECT NEW.id, 'material', NEW.matter_id, COALESCE(NEW.filename, '材料'), COALESCE(NEW.text_note, ''), NEW.received_at
    WHERE NEW.source_type NOT IN ('wechat_auto', 'wechat_channel', 'wecom_channel', 'workbuddy_channel')
       OR NEW.matter_id IS NOT NULL;
END;
CREATE TRIGGER search_fts_materials_delete AFTER DELETE ON materials
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'material';
END;

DROP TRIGGER IF EXISTS search_fts_evidence_insert;
DROP TRIGGER IF EXISTS search_fts_evidence_update;
DROP TRIGGER IF EXISTS search_fts_evidence_delete;
CREATE TRIGGER search_fts_evidence_insert AFTER INSERT ON evidence
WHEN NEW.status NOT IN ('rejected', 'superseded')
BEGIN
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (
        NEW.id,
        'evidence',
        NEW.matter_id,
        NEW.field_type,
        trim(COALESCE(NEW.value, '') || ' ' || COALESCE(NEW.quote, '') || ' ' || COALESCE(NEW.source_locator, '')),
        NEW.created_at
    );
END;
CREATE TRIGGER search_fts_evidence_update AFTER UPDATE ON evidence
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'evidence';
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    SELECT
        NEW.id,
        'evidence',
        NEW.matter_id,
        NEW.field_type,
        trim(COALESCE(NEW.value, '') || ' ' || COALESCE(NEW.quote, '') || ' ' || COALESCE(NEW.source_locator, '')),
        NEW.created_at
    WHERE NEW.status NOT IN ('rejected', 'superseded');
END;
CREATE TRIGGER search_fts_evidence_delete AFTER DELETE ON evidence
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'evidence';
END;

DROP TRIGGER IF EXISTS search_fts_email_messages_insert;
DROP TRIGGER IF EXISTS search_fts_email_messages_update;
DROP TRIGGER IF EXISTS search_fts_email_messages_delete;
CREATE TRIGGER search_fts_email_messages_insert AFTER INSERT ON email_messages
WHEN NEW.status = 'active' AND NEW.classification = 'work'
BEGIN
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (
        NEW.id,
        'email',
        NEW.matter_id,
        COALESCE(NEW.subject, '工作邮件'),
        trim(COALESCE(NEW.summary, '') || ' ' || COALESCE(NEW.reason, '') || ' ' || COALESCE(NEW.evidence_json, '')),
        COALESCE(NEW.sent_at, NEW.created_at)
    );
END;
CREATE TRIGGER search_fts_email_messages_update AFTER UPDATE ON email_messages
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'email';
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    SELECT
        NEW.id,
        'email',
        NEW.matter_id,
        COALESCE(NEW.subject, '工作邮件'),
        trim(COALESCE(NEW.summary, '') || ' ' || COALESCE(NEW.reason, '') || ' ' || COALESCE(NEW.evidence_json, '')),
        COALESCE(NEW.sent_at, NEW.created_at)
    WHERE NEW.status = 'active' AND NEW.classification = 'work';
END;
CREATE TRIGGER search_fts_email_messages_delete AFTER DELETE ON email_messages
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'email';
END;

DROP TRIGGER IF EXISTS search_fts_company_policies_insert;
DROP TRIGGER IF EXISTS search_fts_company_policies_update;
DROP TRIGGER IF EXISTS search_fts_company_policies_delete;
CREATE TRIGGER search_fts_company_policies_insert AFTER INSERT ON company_policies
BEGIN
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (
        NEW.id,
        'policy',
        NULL,
        NEW.title,
        trim(
            COALESCE(NEW.publisher, '') || ' ' ||
            COALESCE(NEW.topic, '') || ' ' ||
            COALESCE(NEW.scope, '') || ' ' ||
            COALESCE(NEW.summary, '') || ' ' ||
            COALESCE(NEW.requirements_json, '')
        ),
        NEW.updated_at
    );
END;
CREATE TRIGGER search_fts_company_policies_update AFTER UPDATE ON company_policies
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'policy';
    INSERT INTO search_fts(entity_id, entity_type, matter_id, title, body, created_at)
    VALUES (
        NEW.id,
        'policy',
        NULL,
        NEW.title,
        trim(
            COALESCE(NEW.publisher, '') || ' ' ||
            COALESCE(NEW.topic, '') || ' ' ||
            COALESCE(NEW.scope, '') || ' ' ||
            COALESCE(NEW.summary, '') || ' ' ||
            COALESCE(NEW.requirements_json, '')
        ),
        NEW.updated_at
    );
END;
CREATE TRIGGER search_fts_company_policies_delete AFTER DELETE ON company_policies
BEGIN
    DELETE FROM search_fts WHERE entity_id = OLD.id AND entity_type = 'policy';
END;
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            now = utc_now()
            seed_people(connection, now)
            matter_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(matters)")
            }
            if "target_date" not in matter_columns:
                connection.execute("ALTER TABLE matters ADD COLUMN target_date TEXT")
            if "status_override" not in matter_columns:
                connection.execute(
                    "ALTER TABLE matters ADD COLUMN status_override INTEGER NOT NULL DEFAULT 0"
                )
            if "contact_person_id" not in matter_columns:
                connection.execute(
                    "ALTER TABLE matters ADD COLUMN contact_person_id TEXT REFERENCES people(id)"
                )
            action_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(actions)")
            }
            if "material_id" not in action_columns:
                connection.execute(
                    "ALTER TABLE actions ADD COLUMN material_id TEXT REFERENCES materials(id)"
                )
            action_migrations = {
                "flow_state": "TEXT NOT NULL DEFAULT 'needs_action'",
                "waiting_on": "TEXT NOT NULL DEFAULT ''",
        "blocked_reason": "TEXT NOT NULL DEFAULT ''",
        "next_follow_up_at": "TEXT",
        "schedule_basis": "TEXT NOT NULL DEFAULT 'legacy'",
        "estimated_minutes": "INTEGER",
            "pinned_at": "TEXT",
            "snoozed_until": "TEXT",
            "completion_evidence": "TEXT NOT NULL DEFAULT '[]'",
            }
            for column, definition in action_migrations.items():
                if column not in action_columns:
                    connection.execute(
                        f"ALTER TABLE actions ADD COLUMN {column} {definition}"
                    )
            email_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(email_messages)")
            }
            if "source_text" not in email_columns:
                connection.execute(
                    "ALTER TABLE email_messages ADD COLUMN source_text TEXT NOT NULL DEFAULT ''"
                )
            if "material_id" not in email_columns:
                connection.execute(
                    "ALTER TABLE email_messages ADD COLUMN material_id TEXT REFERENCES materials(id)"
                )
            if "ignored_action_ids_json" not in email_columns:
                connection.execute(
                    "ALTER TABLE email_messages ADD COLUMN ignored_action_ids_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "confidence" not in email_columns:
                connection.execute("ALTER TABLE email_messages ADD COLUMN confidence REAL")
            connection.executescript(SEARCH_TRIGGERS)
            connection.execute(
                "UPDATE actions SET flow_state = CASE kind "
                "WHEN 'waiting' THEN 'waiting' "
                "WHEN 'decision' THEN 'needs_decision' "
                "ELSE COALESCE(NULLIF(flow_state, ''), 'needs_action') END "
                "WHERE flow_state IS NULL OR flow_state = ''"
            )
            policy_candidate_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(company_policy_candidates)"
                )
            }
            if "is_authority" not in policy_candidate_columns:
                connection.execute(
                    "ALTER TABLE company_policy_candidates "
                    "ADD COLUMN is_authority INTEGER NOT NULL DEFAULT 0"
                )
            for table in (
                    "wechat_conversations",
                    "wechat_sync_state",
                    "wechat_seen_messages",
                    "wechat_sync_requests",
                    "wechat_candidates",
                ):
                columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                if "source" not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN source TEXT NOT NULL "
                        "DEFAULT 'personal_wechat'"
                    )
            sync_result_migrations = {
                "wechat_sync_requests": {
                    "message_count": "INTEGER NOT NULL DEFAULT 0",
                    "window_count": "INTEGER NOT NULL DEFAULT 0",
                    "skipped_count": "INTEGER NOT NULL DEFAULT 0",
                },
                "email_sync_requests": {
                    "scanned_count": "INTEGER NOT NULL DEFAULT 0",
                    "pending_count": "INTEGER NOT NULL DEFAULT 0",
                    "ignored_count": "INTEGER NOT NULL DEFAULT 0",
                },
            }
            for table, migrations in sync_result_migrations.items():
                columns = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                for column, definition in migrations.items():
                    if column not in columns:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wechat_conversations_source "
                "ON wechat_conversations(source, listen_status, last_message_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wechat_candidates_source "
                "ON wechat_candidates(source, status, created_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_wechat_sync_requests_source "
                "ON wechat_sync_requests(source, status, requested_at DESC)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_material "
                "ON actions(material_id, status)"
            )
            connection.execute(
                "UPDATE actions SET material_id = ("
                "SELECT MIN(x.id) FROM materials x WHERE x.matter_id = actions.matter_id "
                "HAVING COUNT(*) = 1) WHERE material_id IS NULL"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return dict(row) if row else None

    def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def audit(
        self,
        event_id: str,
        actor: str,
        action: str,
        object_type: str,
        object_id: str,
        matter_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events
                (id, actor, action, object_type, object_id, matter_id, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    actor,
                    action,
                    object_type,
                    object_id,
                    matter_id,
                    json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                ),
            )
