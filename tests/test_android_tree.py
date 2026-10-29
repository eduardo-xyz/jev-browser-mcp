from pathlib import Path

from jev_mcp.android_tree import parse_hierarchy


def page():
    xml = Path("tests/fixtures/android/bac_login.xml").read_text()
    return parse_hierarchy(xml, "net.bac.sbe.android", "Main")


def by_rid(rid):
    return next(a for a in page()["actions"] if a.get("resource_id", "").endswith(rid))


def test_bac_login_fields_and_button():
    assert by_rid("txtUsername")["kind"] == "fill"
    assert by_rid("txtUsername")["value"] == "testuser01"
    pw = by_rid("txtPassword")
    assert pw["kind"] == "fill" and pw["password"] and pw["value"] == ""
    assert pw["label"].startswith("Contraseña")
    assert by_rid("btnLogin")["label"] == "Ingresar"
    assert by_rid("chkBacToken")["role"] == "checkbox"


def test_status_bar_and_glyphs_excluded():
    p = page()
    assert "notification" not in p["text"] and "Battery" not in p["text"]
    assert all("" not in a["label"] and "" not in a["label"] for a in p["actions"])
    assert not any("" <= c <= "" for c in p["text"])
    assert by_rid("btnClearUsername")["label"] == "btnClearUsername"


def test_controls_and_fingerprint_stable():
    a, b = page(), page()
    assert a["fingerprint"] == b["fingerprint"]
    assert {"back", "wait"} <= {x["id"] for x in a["actions"]}


def test_empty_webview_is_unreadable():
    xml = '<hierarchy><node package="net.bac.sbe.android" class="android.webkit.WebView" text="" content-desc="" clickable="true" enabled="true" bounds="[0,70][1600,2524]"/></hierarchy>'
    assert parse_hierarchy(xml, "net.bac.sbe.android", "Main")["readable"] is False
    assert parse_hierarchy("<hierarchy/>", "net.bac.sbe.android", "Main")["readable"] is False


def _shade_xml(width=1000, height=2000):
    return (
        '<hierarchy>'
        f'<node package="com.android.systemui" class="android.widget.FrameLayout" enabled="true" bounds="[0,0][{width},{height}]">'
        '<node package="com.android.systemui" class="android.widget.Button" clickable="true" enabled="true"'
        ' text="Wi-Fi" bounds="[10,100][400,300]"/></node>'
        '<node package="com.app" class="android.widget.Button" clickable="true" enabled="true"'
        ' text="Pay" bounds="[10,1500][400,1600]"/>'
        '</hierarchy>'
    )


def test_system_controls_present():
    ids = {a["id"]: a for a in page()["actions"]}
    assert ids["home"]["kind"] == "home" and ids["home"]["label"] == "Go to the home screen"
    assert ids["recents"]["kind"] == "recents" and ids["recents"]["label"] == "Show recent apps"
    assert ids["notifications"]["kind"] == "notifications"
    assert ids["notifications"]["label"] == "Open the notification shade"


def test_shade_open_false_for_status_bar_only():
    assert page()["shade_open"] is False


def test_shade_open_true_when_systemui_covers_screen():
    assert parse_hierarchy(_shade_xml(), "com.app", "Main")["shade_open"] is True


def test_notification_nodes_only_when_shade_open():
    xml = _shade_xml()
    hidden = parse_hierarchy(xml, "com.app", "Main")
    shown = parse_hierarchy(xml, "com.app", "Main", include_system_ui=True)
    assert not any(a.get("label") == "Wi-Fi" for a in hidden["actions"])
    assert any(a.get("label") == "Wi-Fi" for a in shown["actions"])
