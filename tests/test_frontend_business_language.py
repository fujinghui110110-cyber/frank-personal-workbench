from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
APP = (ROOT / "app/static/app.js").read_text(encoding="utf-8")


def test_frontend_fixed_copy_uses_business_language() -> None:
    fixed_copy = f"{INDEX}\n{APP}"
    forbidden = (
        "水位",
        "游标",
        "命中率",
        "索引",
        "候选衰减",
        "stale",
        "cursor",
        "watermark",
        "coverage",
        "数据库暂不可安全读取",
        "本机增量导出",
        "增量聊天记录导出状态",
        "运行状态",
        "执行节点",
        "工作队列",
        "读取进度",
    )

    assert not [word for word in forbidden if word.lower() in fixed_copy.lower()]


def test_today_uses_action_dashboard_and_matters_keep_follow_up_desk() -> None:
    today_branch = APP.split('if (route.name === "today")', 1)[1].split(
        '} else if (route.name === "intake")', 1
    )[0]
    matters_branch = APP.split('} else if (route.name === "matters")', 1)[1].split(
        '} else if (route.name === "matter")', 1
    )[0]

    assert "renderToday(" in today_branch
    assert "renderFollowUpDesk" not in today_branch
    assert "renderFollowUpDesk" in matters_branch
    assert "相关信息已归到一起" in APP
    assert "闭环状态" in APP
    assert "confirm(" not in APP


def test_primary_navigation_exposes_core_work_and_information_sources() -> None:
    navigation = INDEX.split('id="desktop-nav"', 1)[1].split("</nav>", 1)[0]

    assert navigation.count("data-route=") == 6
    for label in ("今天", "事项", "规定", "搜索", "聊天线索", "邮件工作"):
        assert f"<span>{label}</span>" in navigation
    assert 'id="wechat-count"' in navigation
    assert 'id="email-count"' in navigation

    attention = INDEX.split('class="work-queue-bar"', 1)[1].split("</nav>", 1)[0]
    assert 'href="#/wechat"' not in attention
    assert 'href="#/email"' not in attention


def test_processing_history_humanizes_backend_node_names() -> None:
    assert "function nodeHistoryLabel(item)" in APP
    assert "这台 Mac · 贾维斯" in APP
    assert "旧的本机记录 · 贾维斯" in APP
