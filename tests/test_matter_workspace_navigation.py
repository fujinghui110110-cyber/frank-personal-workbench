from pathlib import Path


STATIC = Path(__file__).parents[1] / "app" / "static"


def test_matter_workspace_has_a_clear_ledger_and_detail_history() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    manifest = (STATIC / "manifest.webmanifest").read_text(encoding="utf-8")
    service_worker = (STATIC / "sw.js").read_text(encoding="utf-8")

    assert "全部推进事项" in script
    assert "当前需要推进" in script
    assert "持续跟进" in script
    assert "推进事项清单" in script
    assert "返回推进事项清单" not in script
    assert "const selected = route.id" in script
    assert "matters.find((item) => !item.is_completed) || matters[0] || null" in script
    assert '`#/matters/${encodeURIComponent(matterId)}`' in script
    assert script.count('window.history.replaceState(null, "", "#/matters")') == 1
    assert 'href="#/today" data-route="today"' not in index
    assert '["today", "intake"' not in script
    assert 'return "#/today"' not in script
    assert 'if (window.location.hash === "#/today")' in script
    assert script.index('window.location.hash === "#/today"') < script.index("const route = routeFromHash()")
    assert 'route.name === "today"' not in script
    assert "function renderToday(" not in script
    assert 'api("/api/matters?limit=100")' not in script
    assert script.count('api("/api/matters?limit=500")') >= 4
    assert 'route.name === "matters" ? "新增材料" : "交给贾维斯"' in script
    assert 'label = "修改信息"' in script
    assert '"start_url": "/#/matters"' in manifest
    assert 'frank-personal-workbench-shell-v73' in service_worker
    assert '/manifest.webmanifest?v=73' in index
    assert '/manifest.webmanifest?v=73' in service_worker


def test_intake_returns_to_the_current_workspace() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")

    assert 'const alreadyToday = window.location.hash === "#/today"' not in script
    assert 'window.location.hash = "#/today"' not in script
