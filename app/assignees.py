from __future__ import annotations

import json
import re
from sqlite3 import Connection
from typing import Any


PEOPLE_SEED = [
    {
        "id": "person_self",
        "display_name": "我自己",
        "role": "Frank",
        "aliases": ["Frank", "傅京晖", "我自己"],
        "phonetic_aliases": [],
        "is_self": 1,
    },
    {
        "id": "person_sun_qing",
        "display_name": "孙庆",
        "role": "片区财务领导",
        "aliases": ["孙庆", "Hank", "hank", "孙总"],
        "phonetic_aliases": [],
        "is_self": 0,
    },
    {
        "id": "person_li_jing",
        "display_name": "李静",
        "role": "仓管员",
        "aliases": ["李静", "李姐", "静姐"],
        "phonetic_aliases": [],
        "is_self": 0,
    },
    {
        "id": "person_ou_bo",
        "display_name": "欧波",
        "role": "仓管员",
        "aliases": ["欧波", "欧哥"],
        "phonetic_aliases": [],
        "is_self": 0,
    },
    {
        "id": "person_feng_lixiang",
        "display_name": "冯李香",
        "role": "采购",
        "aliases": ["冯李香", "李香", "香姐"],
        "phonetic_aliases": [],
        "is_self": 0,
    },
    {
        "id": "person_chen_zhenting",
        "display_name": "陈贞婷",
        "role": "出纳、兼职文员",
        "aliases": ["陈贞婷", "阿婷"],
        "phonetic_aliases": [],
        "is_self": 0,
    },
    {
        "id": "person_pan_chaohui",
        "display_name": "潘朝荟",
        "role": "应收、收入、资产管理、收入审计",
        "aliases": ["潘朝荟"],
        "phonetic_aliases": [],
        "is_self": 0,
    },
    {
        "id": "person_zhu_qingxia",
        "display_name": "朱青霞",
        "role": "总账主管",
        "aliases": ["朱青霞", "朱青霞-球会总账"],
        "phonetic_aliases": [],
        "is_self": 0,
    },
]


def _norm(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).casefold()


def seed_people(connection: Connection, now: str) -> None:
    for person in PEOPLE_SEED:
        connection.execute(
            "INSERT INTO people "
            "(id, display_name, role, aliases_json, phonetic_aliases_json, is_self, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET display_name = excluded.display_name, role = excluded.role, "
            "aliases_json = excluded.aliases_json, phonetic_aliases_json = excluded.phonetic_aliases_json, "
            "is_self = excluded.is_self, enabled = 1, updated_at = excluded.updated_at",
            (
                person["id"],
                person["display_name"],
                person["role"],
                json.dumps(person["aliases"], ensure_ascii=False, separators=(",", ":")),
                json.dumps(person["phonetic_aliases"], ensure_ascii=False, separators=(",", ":")),
                person["is_self"],
                now,
                now,
            ),
        )


def people_map() -> dict[str, dict[str, Any]]:
    return {person["id"]: person for person in PEOPLE_SEED}


def resolve_person_alias(text: str) -> dict[str, str] | None:
    needle = _norm(text)
    if not needle:
        return None
    for person in PEOPLE_SEED:
        for alias in person["aliases"]:
            if _norm(alias) == needle:
                return {"person_id": person["id"], "display_name": person["display_name"], "alias": alias}
    return None


_RESPONSIBILITY_RE = re.compile(
    r"负责|跟进|处理|核对|确认|整理|发给我|发过来|问供应商|沟通|提交|准备|回复|落实|对接|继续"
)
_MENTION_ONLY_RE = re.compile(r"好|说过|提到|表示|认为|意见|转发|抄送")


def suggest_assignees_from_action(action: dict[str, Any]) -> list[dict[str, Any]]:
    text = " ".join(str(action.get(key) or "") for key in ("title", "detail")).strip()
    if not text:
        return []
    suggestions: list[dict[str, Any]] = []
    for person in PEOPLE_SEED:
        for alias in person["aliases"]:
            start = text.casefold().find(str(alias).casefold())
            if start < 0:
                continue
            after = text[start + len(alias) : start + len(alias) + 18]
            before = text[max(0, start - 8) : start]
            if _MENTION_ONLY_RE.search(after[:8]):
                continue
            if not (_RESPONSIBILITY_RE.search(after) or _RESPONSIBILITY_RE.search(before)):
                continue
            suggestions.append(
                {
                    "person_id": person["id"],
                    "alias": alias,
                    "reason": f"行动内容把“{alias}”与明确的跟进动作关联",
                    "evidence": [text[:240]],
                }
            )
            break
    return suggestions


def normalize_suggestions(action: dict[str, Any]) -> list[dict[str, Any]]:
    raw = action.get("assignee_suggestions")
    suggestions = raw if isinstance(raw, list) else []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in suggestions:
        if not isinstance(item, dict):
            continue
        match = resolve_person_alias(str(item.get("person") or item.get("name") or item.get("alias") or ""))
        person_id = str(item.get("person_id") or (match or {}).get("person_id") or "")
        if person_id not in people_map() or person_id in seen:
            continue
        seen.add(person_id)
        normalized.append(
            {
                "person_id": person_id,
                "alias": str(
                    item.get("detected_alias")
                    or item.get("alias")
                    or (match or {}).get("alias")
                    or ""
                )[:80],
                "reason": str(item.get("reason") or "贾维斯建议确认该跟进人")[:500],
                "evidence": item.get("evidence") if isinstance(item.get("evidence"), list) else [],
            }
        )
    for item in suggest_assignees_from_action(action):
        if item["person_id"] not in seen:
            normalized.append(item)
            seen.add(item["person_id"])
    return normalized


def upsert_action_suggestions(
    connection: Connection,
    action_id: str,
    material_id: str | None,
    suggestions: list[dict[str, Any]],
    now: str,
) -> int:
    created = 0
    for item in suggestions:
        person_id = str(item.get("person_id") or "")
        if person_id not in people_map():
            continue
        evidence = item.get("evidence") if isinstance(item.get("evidence"), list) else []
        existing = connection.execute(
            "SELECT status, evidence_json FROM action_assignees "
            "WHERE action_id = ? AND person_id = ?",
            (action_id, person_id),
        ).fetchone()
        if existing:
            previous = json.loads(existing["evidence_json"] or "[]")
            merged = list(dict.fromkeys(str(part) for part in [*previous, *evidence] if part))[:4]
            connection.execute(
                "UPDATE action_assignees SET "
                "alias = CASE WHEN status = 'pending' AND ? != '' THEN ? ELSE alias END, "
                "reason = CASE WHEN status = 'pending' AND ? != '' THEN ? ELSE reason END, "
                "evidence_json = ?, updated_at = ? WHERE action_id = ? AND person_id = ?",
                (
                    str(item.get("alias") or "")[:80],
                    str(item.get("alias") or "")[:80],
                    str(item.get("reason") or "")[:500],
                    str(item.get("reason") or "")[:500],
                    json.dumps(merged, ensure_ascii=False, separators=(",", ":")),
                    now,
                    action_id,
                    person_id,
                ),
            )
            continue
        connection.execute(
            "INSERT INTO action_assignees "
            "(id, action_id, person_id, status, alias, reason, evidence_json, source_material_id, "
            "suggested_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, 'jarvis', ?, ?)",
            (
                f"assignee_{action_id}_{person_id}",
                action_id,
                person_id,
                str(item.get("alias") or "")[:80],
                str(item.get("reason") or "")[:500],
                json.dumps(evidence[:4], ensure_ascii=False, separators=(",", ":")),
                material_id,
                now,
                now,
            ),
        )
        created += 1
    return created
