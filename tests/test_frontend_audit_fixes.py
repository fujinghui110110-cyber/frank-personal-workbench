from pathlib import Path


STATIC = Path(__file__).parents[1] / "app" / "static"


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


def test_policy_candidate_can_be_collected_without_linking_existing_policy() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "作为新规定收录" in script
    assert "目前无需关联" in script
    assert "这属于工作台已有规定" in script
    assert "修订、废止或补充内容必须关联现行规定" not in script
