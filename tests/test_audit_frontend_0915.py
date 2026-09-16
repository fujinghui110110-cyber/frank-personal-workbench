from pathlib import Path


STATIC = Path(__file__).parents[1] / "app" / "static"
APP = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
REFERENCE = (STATIC / "dashboard-reference.css").read_text(encoding="utf-8")
RESPONSIVE = (STATIC / "dashboard-responsive.css").read_text(encoding="utf-8")
SW = (STATIC / "sw.js").read_text(encoding="utf-8")


def test_attention_entries_are_visible_and_named() -> None:
    queue = INDEX.split('class="work-queue-bar"', 1)[1].split("</nav>", 1)[0]
    assert queue.count("href=") == 3
    assert all(label in queue for label in ("需要拍板", "公司规定", "收件情况"))
    assert "repeat(5, 0)" not in REFERENCE
    attention_links = REFERENCE.split(".workflow-step-attention .work-queue-bar a", 1)[1].split("}", 1)[0]
    assert "font-size: 11px" in attention_links


def test_source_status_uses_latest_and_named_counts() -> None:
    assert "sourceLatestResult" in APP
    assert "sourceResultCount" in APP
    assert "personal_wechat" in APP and "wecom" in APP and "email" in APP
    assert "item?.latest?.status" in APP
    receipt_rule = REFERENCE.split(".source-sync-receipt {", 1)[1].split("}", 1)[0]
    assert "position: static" in receipt_rule
    assert "display: none" not in receipt_rule
    assert ".source-sync-receipt[hidden]" in REFERENCE
    assert 'for (const source of ["personal_wechat", "wecom"])' in APP
    assert "if (request?.id) requestIds.email = request.id" in APP
    assert "邮箱尚未配置，本次未生成读取任务" in APP
    assert "本次读取已结束" in APP
    assert "三个平台已经检查完毕" not in APP


def test_person_actions_have_individual_workflow_controls() -> None:
    assert "renderPersonActionRows" in APP
    assert 'apiOptional("/api/actions?status=open", [])' in APP
    assert 'apiOptional("/api/actions?status=done", [])' in APP
    assert 'apiOptional("/api/people", [])' in APP
    assert 'data-follow-up-mode="people"' in APP
    assert 'data-follow-up-people-panel' in APP
    assert "renderPersonGroups(openActions, state.people)" in APP
    assert 'String(openActions.length)' in APP
    assert 'String(doneActions.length)' in APP
    for label in ("我自己", "负责人待明确", "完成行动", "稍后处理", "等待反馈", "指派负责人"):
        assert label in APP
    assert 'data-person-action-status="done"' in APP
    assert 'data-person-action-status="waiting"' in APP


def test_person_action_refresh_preserves_people_mode_on_matter_route() -> None:
    assert "preserveFollowUpMode = false" in APP
    assert "preserveFollowUpMode: true" in APP
    matter_route = APP.split('} else if (route.name === "matter") {', 1)[1].split(
        '} else if (route.name === "reviews") {', 1
    )[0]
    assert "sourceEvidence,\n      preserveFollowUpMode," in matter_route
    assert "if (preferredId && !preserveMode) state.matterTab = \"all\";" in APP
    preferred_block = APP.split("if (preferredId) {", 1)[1].split("const openItems", 1)[0]
    assert "if (!preserveMode)" in preferred_block
    view_handler = APP.split("button.dataset.followUpView;", 1)[1].split(
        '$("#follow-up-list")', 1
    )[0]
    assert 'if (state.matterTab === "people")' in view_handler
    assert "String(openActions.length)" in view_handler
    assert "String(doneActions.length)" in view_handler
    desk_tail = APP.split("bindDynamic();\n    if (initial) await selectFollowUpMatter(initial.id);", 1)[1]
    count_reset = desk_tail.split("async function renderMatters", 1)[0]
    assert 'if (state.matterTab === "people")' in count_reset
    assert "String(openActions.length)" in count_reset
    assert "String(doneActions.length)" in count_reset


def test_target_and_review_dates_are_separate_and_progress_is_atomic() -> None:
    assert "data-follow-up-target-date" in APP
    assert 'name="target_date"' in APP
    assert "flex-basis: 100%" in (STATIC / "app.css").read_text(encoding="utf-8")
    assert 'name="next_review_date"' in APP
    assert "matter.next_review_date" in APP
    assert "next_review_date: form.elements.next_review_date.value" in APP
    progress_call = APP.split('/progress`, {', 1)[1].split("});", 1)[0]
    assert "summary" in progress_call and "detail" in progress_call
    assert "target_date" not in progress_call


def test_today_links_filters_and_global_metrics_are_truthful() -> None:
    assert "todayDueLabel" in APP
    assert "全局整理效果" in APP
    related_card = APP.split("function renderReferenceRelatedInfo", 1)[1].split(
        "function todayDueLabel", 1
    )[0]
    assert "已减少" not in related_card
    assert 'data-related-source="all"' in APP
    assert "commitmentMatterHref" in APP
    assert "category=" in APP


def test_rules_and_search_use_real_filter_contracts() -> None:
    assert "系统原则" in APP and "个人规则" in APP
    assert "系统固定" in APP
    assert 'data-search-filter="personId"' in APP
    assert 'data-search-filter="channel"' in APP
    assert 'data-search-filter="businessType"' in APP
    assert "待推进 ${attentionTotal} · 待确认 ${reviewTotal}" in APP
    assert "搜索工作信息" in APP
    assert "信息来源" in APP
    assert "保险目前进展到哪里" in APP
    assert "我在等谁" not in APP
    assert 'params.set("person_id"' in APP
    assert 'params.set("channel"' in APP
    assert 'params.set("business_type"' in APP
    assert 'route.name === "source"' in APP
    assert "/api/search/sources/" in APP
    assert "followUpLinkedEvidenceHtml" in APP
    assert "data-follow-up-linked-evidence" in APP
    assert '["personal_wechat", "wecom", "email"].includes(query.source_type)' in APP
    assert "这里保留原始内容和业务时间，方便核对来源。" in APP
    assert "按业务字段展示，不显示内部编号或原始 JSON" not in APP
    assert 'key === "status"' in APP
    assert "dateFields.has(key)" in APP
    assert "source_href" in APP
    assert "JSON.stringify" not in APP.split("function renderSourceDetail", 1)[1].split("function", 1)[0]


def test_v100_cache_busts_all_frontend_assets() -> None:
    for asset in ("app.css", "dashboard-reference.css", "dashboard-responsive.css", "app.js"):
        assert f"{asset}?v=100" in INDEX
    assert "frank-personal-workbench-shell-v100" in SW
    assert 'navigator.serviceWorker.register("/sw.js?v=100")' in APP


def test_persistent_handoff_does_not_cover_desktop_content() -> None:
    assert "rail-intake" in INDEX
    assert "handoff-fab" in INDEX
    assert "display: none !important" in REFERENCE
    assert "display: inline-flex !important" not in REFERENCE
    assert "display: inline-flex !important" not in RESPONSIVE
