from pathlib import Path
from xml.etree import ElementTree


STATIC = Path(__file__).parents[1] / "app" / "static"


def test_dashboard_uses_named_icon_assets_for_each_business_state() -> None:
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    styles = (STATIC / "dashboard-reference.css").read_text(encoding="utf-8")

    for icon in (
        "hugeicons-message-square-more",
        "user-round-check-semantic",
        "hugeicons-user-multiple-03",
        "hugeicons-stamp-01",
        "hugeicons-call",
        "hugeicons-alert-02",
    ):
        assert f'icon: "{icon}"' in script or f'/static/icons/{icon}.svg' in script

    assert 'class="source-read-icon"' in script
    assert 'return "/static/icons/wechat-brand.svg"' in script
    assert 'return "/static/icons/wecom-brand.png"' in script
    assert 'return "/static/icons/mail-blue.svg"' in script
    assert 'src="/static/icons/clock.svg"' in script
    assert 'src="/static/icons/move-vertical.svg"' in script
    assert 'url("/static/icons/hugeicons-calendar-03.svg")' in styles
    assert 'url("/static/icons/hugeicons-user-round.svg")' in styles
    assert 'url("/static/icons/hugeicons-clock-05.svg")' in styles
    assert 'url("/static/icons/hugeicons-money-bag-02.svg")' in styles
    assert 'src="/static/icons/hugeicons-alert-02.svg"' in script


def test_dashboard_icon_assets_are_valid_svg_files() -> None:
    icons = (
        "hugeicons-calendar-03",
        "hugeicons-user-round",
        "hugeicons-clock-05",
        "hugeicons-money-bag-02",
        "hugeicons-download-02",
        "hugeicons-list",
        "hugeicons-user-time-02",
        "user-round-check-semantic",
        "hugeicons-home-05",
        "hugeicons-square-check",
        "hugeicons-file-text",
        "hugeicons-search-01",
        "hugeicons-message-square-more",
        "hugeicons-user-multiple-03",
        "hugeicons-stamp-01",
        "hugeicons-call",
        "hugeicons-alert-02",
        "wechat-brand",
        "mail-blue",
        "list",
        "arrow-up-down",
    )

    for icon in icons:
        path = STATIC / "icons" / f"{icon}.svg"
        assert path.is_file(), icon
        assert ElementTree.parse(path).getroot().tag.endswith("svg"), icon


def test_wecom_uses_the_official_brand_asset() -> None:
    path = STATIC / "icons" / "wecom-brand.png"
    assert path.is_file()
    assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
