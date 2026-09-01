from pathlib import Path


STATIC = Path(__file__).parents[1] / "app" / "static"


def test_simple_workbench_keeps_policy_entry_and_explains_auto_merge() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    desktop_nav = index.split('id="desktop-nav"', 1)[1].split("</nav>", 1)[0]
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'href="#/policies"' in desktop_nav
    assert "公司规定" in desktop_nav
    assert "同一事项会自动归并" in script


def test_workbench_restores_source_monitoring_and_manual_rejection() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    desktop_nav = index.split('id="desktop-nav"', 1)[1].split("</nav>", 1)[0]
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "聊天监控" in desktop_nav
    assert "邮件工作" in desktop_nav
    assert "data-follow-up-dismiss" in script
    assert 'id="follow-up-source"' in script
    assert 'status: "dismissed"' in script


def test_assistant_copy_matches_manual_analysis_contract() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "在线，自动读取；整理需手工启动" in script
    assert "在线，自动接手新材料" not in script


def test_matter_status_is_editable_without_a_dialog() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'data-matter-status-form' in script
    assert 'body: JSON.stringify({ status: form.elements.status.value })' in script
    assert 'window.confirm(' not in script


def test_desktop_intake_button_uses_reserved_sidebar_space() -> None:
    css = (STATIC / "app.css").read_text(encoding="utf-8")

    assert "padding: 18px 7px 66px" in css
    assert "left: 18px" in css
    assert "width: 228px" in css


def test_import_material_button_is_bound_on_every_route() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    setup_intake = script.split("function setupIntake()", 1)[1].split(
        "async function init()", 1
    )[0]

    assert 'event.target.closest("[data-open-intake]")' in setup_intake, (
        "导入材料必须使用全局事件委托，不能依赖某个页面单独绑定"
    )


def test_completing_matter_keeps_the_unfinished_list_open() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    complete_handler = script.split('$("[data-follow-up-complete]"', 1)[1].split(
        '$("[data-follow-up-reopen]"', 1
    )[0]

    assert "待复盘" not in script
    assert "待完成" in script
    assert 'state.followUpView = "open";' in complete_handler
    assert 'state.followUpView = "completed";' not in complete_handler


def test_jarvis_refreshes_open_matters_after_background_analysis_finishes() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    handler = script.split("async function runPendingAnalysis()", 1)[1].split(
        "async function runSourceSyncLegacy()", 1
    )[0]

    assert "async function waitForAnalysisCompletion()" in script
    assert "await waitForAnalysisCompletion()" in handler
    assert "await refreshRouteWithoutJump()" in handler


def test_policy_candidate_can_be_collected_without_linking_existing_policy() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "作为新规定收录" in script
    assert "目前无需关联" in script
    assert "这属于工作台已有规定" in script
    assert "修订、废止或补充内容必须关联现行规定" not in script
