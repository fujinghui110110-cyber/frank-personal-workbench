from pathlib import Path


APP_JS = Path(__file__).resolve().parents[1] / "app" / "static" / "app.js"
STEP_FIELDS = (
    "kind",
    "owner",
    "due_date",
    "flow_state",
    "waiting_on",
    "blocked_reason",
    "next_follow_up_at",
    "estimated_minutes",
)


def app_script() -> str:
    return APP_JS.read_text(encoding="utf-8")


def region(script: str, start: str, end: str) -> str:
    start_at = script.index(start)
    end_at = script.index(end, start_at + len(start))
    return script[start_at:end_at]


def test_stale_work_package_has_formal_recovery_prompt_and_disabled_apply() -> None:
    script = app_script()
    renderer = region(script, "function renderWorkPackage(matter", "function renderMatter(")
    binder = region(script, "function bindWorkPackage(", "function closePreviewHtml(")

    assert 'const stale = workPackage.status === "stale";' in renderer
    assert "事项内容已有变化，当前草稿已保留" in renderer
    assert "可以继续保存草稿；重新核对事项内容后，再生成新的办理方案。" in renderer
    assert "data-work-package-regenerate" in renderer
    assert "重新生成办理方案" in renderer
    assert 'data-work-package-apply disabled${stale ? " aria-disabled=\\"true\\"' in renderer
    assert "applyButton.disabled = stale ||" in binder


def test_legacy_today_renderer_is_removed_from_the_active_frontend() -> None:
    script = app_script()

    assert 'route.name === "today"' not in script
    assert "function renderToday(" not in script
    assert "data-today-complete" not in script


def test_work_package_step_exposes_and_serializes_all_business_fields() -> None:
    script = app_script()
    renderer = region(script, "function renderWorkPackageStep(", "function renderWorkPackage(")
    serializer = region(script, "function workPackageDraft(", "function bindWorkPackage(")

    for field in STEP_FIELDS:
        control_name = f'name="step_{field}"'
        assert control_name in renderer, field
        assert f'[name="step_{field}"]' in serializer, field
        assert f"{field}:" in serializer, field


def test_http_409_uses_the_unified_chinese_conflict_prompt() -> None:
    friendly_error = region(app_script(), "function friendlyError(", "function businessPersonLabel(")

    assert 'if (Number(error?.status) === 409)' in friendly_error
    assert 'return "内容已被后台更新，请核对后再保存。";' in friendly_error


def test_close_preview_gives_each_blocker_type_a_processing_entry() -> None:
    script = app_script()
    renderer = region(script, "function closePreviewHtml(", "function bindCloseCheck(")
    binder = region(script, "function bindCloseCheck(", "function renderAssigneeReview(")
    labels = {
        "actions": "未完成行动",
        "reminders": "未处理提醒",
        "reviews": "待确认事项",
        "assignee_reviews": "待确认负责人",
        "active_jobs": "后台处理中",
    }

    for kind, label in labels.items():
        assert f'{kind}: "{label}"' in renderer
    assert 'data-close-target-kind="${escapeHtml(kind)}"' in renderer
    assert 'data-close-target-id="${escapeHtml(item.action_id || item.id || "")}"' in renderer
    assert ">去处理</button>" in renderer
    assert "$$('[data-close-target-kind]', result)" in binder
    assert 'window.location.hash = "#/nodes"' in binder
    assert '[data-action-row="${id}"]' in binder
    assert '[data-review-row="${id}"]' in binder
    assert '[data-reminder-id="${id}"]' in binder


def test_search_results_offer_edit_and_ownership_correction_entries() -> None:
    search_renderer = region(
        app_script(),
        "function renderSearchResults()",
        "async function performSearch(",
    )

    assert 'matter: "修改事项"' in search_renderer
    assert 'action: "修改行动"' in search_renderer
    assert 'material: "纠正归属"' in search_renderer
    assert 'button.className = "button button-quiet search-result-correct";' in search_renderer
    assert "button.textContent = label;" in search_renderer
    assert 'resultLink.insertAdjacentElement("afterend", button);' in search_renderer
    assert 'api(`/api/materials/${encodeURIComponent(item.entity_id)}`)' in search_renderer
    assert "openManualEditor(type, record, button);" in search_renderer


def test_manual_edits_offer_one_cas_protected_undo() -> None:
    script = app_script()
    undo = region(script, "function manualEditUndo(", "async function saveManualEdit(")
    save = region(script, "async function saveManualEdit(", "function addManualEditButton(")

    assert 'const reason = "撤销上次人工修改"' in undo
    assert "expected_updated_at: expected" in undo
    assert "expected_created_at: saved?.created_at" in undo
    assert "saved?._material_updated_at" in undo
    assert "saved?.changed === false" in undo
    assert 'label: "撤销本次修改"' in save
    assert 'toast("已撤销本次修改", "success")' in save
    assert 'toast(friendlyError(error), "error")' in save
