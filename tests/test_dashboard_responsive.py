from pathlib import Path


STATIC = Path(__file__).parents[1] / "app" / "static"


def test_header_uses_content_sized_layout():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "dashboard-reference.css").read_text(encoding="utf-8")
    responsive = (STATIC / "dashboard-responsive.css").read_text(encoding="utf-8")
    assert '<div class="workspace-header">' in index
    header = css.split(".workflow-band {", 1)[1].split("}", 1)[0]
    assert "position: static" in header
    assert "align-content: start" in css
    assert ".workspace-header" in responsive


def test_desktop_workflow_keeps_reference_composition_when_space_is_tight() -> None:
    css = (STATIC / "dashboard-responsive.css").read_text(encoding="utf-8")

    reference = (STATIC / "dashboard-reference.css").read_text(encoding="utf-8")
    assert "@media (min-width: 721px)" in reference
    assert "@media (min-width: 721px) and (max-width: 1299px)" in css
    assert "@media (min-width: 721px) and (max-width: 1040px)" in css
    assert "zoom:" not in css
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css
    assert "grid-column: auto" in css
    assert "grid-template-columns: minmax(0, 1fr)" in css


def test_responsive_stylesheet_is_versioned_with_the_shell() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    service_worker = (STATIC / "sw.js").read_text(encoding="utf-8")

    assert '/static/dashboard-responsive.css?v=100' in index
    assert '/static/dashboard-responsive.css?v=100' in service_worker
    assert 'frank-personal-workbench-shell-v100' in service_worker
