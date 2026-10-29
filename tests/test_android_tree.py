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
