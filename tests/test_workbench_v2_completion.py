from __future__ import annotations

from fastapi.testclient import TestClient

from test_workbench_v2_acceptance import (
    client,
    login,
    package_body,
    seed_matter,
    work_package_draft,
)


def test_work_package_becomes_stale_and_cannot_be_applied_after_source_change(
    client: TestClient,
) -> None:
    login(client)
    matter = seed_matter(client, matter_id="package-stale", action_statuses=("open",))
    generated = client.post(f"/api/matters/{matter['id']}/work-package/generate")
    assert generated.status_code == 200, generated.text
    package = package_body(generated)

    changed = client.patch(
        f"/api/matters/{matter['id']}",
        json={
            "summary": "人工修正后的事项摘要",
            "expected_updated_at": matter["updated_at"],
        },
    )
    assert changed.status_code == 200, changed.text

    latest = client.get(f"/api/matters/{matter['id']}/work-package")
    assert latest.status_code == 200, latest.text
    stale = package_body(latest)
    assert stale["status"] == "stale"

    saved = client.patch(
        f"/api/matters/{matter['id']}/work-package",
        json={
            "draft": work_package_draft(),
            "expected_updated_at": stale["updated_at"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert package_body(saved)["status"] == "stale"

    applied = client.post(
        f"/api/matters/{matter['id']}/work-package/apply",
        json={
            "step_indexes": [0],
            "expected_updated_at": package_body(saved)["updated_at"],
        },
    )
    assert applied.status_code == 409, applied.text
    assert "重新生成" in applied.json()["detail"]


def test_clearing_one_action_date_keeps_user_schedule_basis_when_another_remains(
    client: TestClient,
) -> None:
    login(client)
    matter = seed_matter(client, matter_id="schedule-basis", action_statuses=("open",))
    action_id = matter["action_ids"][0]
    action = client.get(f"/api/matters/{matter['id']}").json()["actions"][0]

    scheduled = client.patch(
        f"/api/actions/{action_id}/planning-state",
        json={
            "due_date": "2099-01-02",
            "next_follow_up_at": "2099-01-01T09:00:00Z",
            "expected_updated_at": action["updated_at"],
        },
    )
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()["schedule_basis"] == "user_entered"

    cleared_due_date = client.patch(
        f"/api/actions/{action_id}/planning-state",
        json={
            "due_date": None,
            "expected_updated_at": scheduled.json()["updated_at"],
        },
    )
    assert cleared_due_date.status_code == 200, cleared_due_date.text
    assert cleared_due_date.json()["schedule_basis"] == "user_entered"

    cleared_follow_up = client.patch(
        f"/api/actions/{action_id}/planning-state",
        json={
            "next_follow_up_at": None,
            "expected_updated_at": cleared_due_date.json()["updated_at"],
        },
    )
    assert cleared_follow_up.status_code == 200, cleared_follow_up.text
    assert cleared_follow_up.json()["schedule_basis"] == "legacy"
