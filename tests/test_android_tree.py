from pathlib import Path

from jev_runner.android_tree import parse_hierarchy


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


def _clicks(p):
    return {a["label"]: a for a in p["actions"] if a.get("kind") == "click"}


def test_webview_plain_text_becomes_link_actions():
    acts = _clicks(page())
    for label in ("Tipo de Cambio", "Ubícanos", "Ayuda"):
        a = acts[label]
        assert a["role"] == "link" and a["kind"] == "click"
    assert acts["Tipo de Cambio"]["bounds"] == [168, 2455, 409, 2495]


def test_webview_text_under_clickable_ancestor_not_duplicated():
    labels = [a["label"] for a in page()["actions"]]
    assert labels.count("¿No puede ingresar?") == 1
    assert labels.count("Ingresar") == 1


def test_webview_links_have_unique_sequential_ids():
    acts = [a for a in page()["actions"] if "bounds" in a]
    real = [a for a in acts if not a.get("synthetic")]
    links = [a for a in acts if a.get("synthetic")]
    assert links and [a["id"] for a in real] == [f"n{i}" for i in range(len(real))]
    assert [a["id"] for a in links] == [f"w{i}" for i in range(len(links))]


def test_plain_text_outside_webview_not_actionable():
    xml = (
        '<hierarchy><node package="com.app" class="android.widget.FrameLayout" enabled="true"'
        ' bounds="[0,0][1000,2000]"><node package="com.app" class="android.widget.TextView"'
        ' text="Just a heading" enabled="true" bounds="[10,10][500,80]"/></node></hierarchy>'
    )
    p = parse_hierarchy(xml, "com.app", "Main")
    assert not any(a.get("label") == "Just a heading" for a in p["actions"])


def test_webview_skips_invisible_and_empty_text():
    xml = (
        '<hierarchy><node package="com.app" class="android.webkit.WebView" enabled="true"'
        ' bounds="[0,0][1000,2000]">'
        '<node package="com.app" class="android.widget.TextView" text="Hidden" enabled="true"'
        ' visible-to-user="false" bounds="[10,10][500,80]"/>'
        '<node package="com.app" class="android.widget.TextView" text="Zero" enabled="true"'
        ' bounds="[10,10][10,80]"/>'
        '<node package="com.app" class="android.widget.TextView" text="\ue001 " enabled="true"'
        ' bounds="[10,100][500,180]"/>'
        '<node package="com.app" class="android.widget.TextView" text="Shown" enabled="true"'
        ' bounds="[10,200][500,280]"/>'
        '</node></hierarchy>'
    )
    p = parse_hierarchy(xml, "com.app", "Main")
    labels = [a["label"] for a in p["actions"] if "bounds" in a]
    assert labels == ["Shown"]


def _webview_xml(countdown="Su sesión expira en 4:59", btn_label="Ingresar", btn_bounds="[10,500][500,600]"):
    return (
        '<hierarchy><node package="com.app" class="android.webkit.WebView" enabled="true"'
        ' bounds="[0,0][1000,2000]">'
        f'<node package="com.app" class="android.widget.TextView" text="{countdown}" enabled="true"'
        ' bounds="[10,10][900,80]"/>'
        f'<node package="com.app" class="android.widget.Button" text="{btn_label}" clickable="true"'
        f' enabled="true" bounds="{btn_bounds}"/>'
        '</node></hierarchy>'
    )


def test_synthetic_links_marked_and_excluded_from_fingerprint():
    a = parse_hierarchy(_webview_xml(), "com.app", "Main")
    b = parse_hierarchy(_webview_xml(countdown="Su sesión expira en 4:58"), "com.app", "Main")
    link = next(x for x in a["actions"] if x.get("role") == "link")
    assert link["synthetic"] is True
    assert not any(x.get("synthetic") for x in a["actions"] if x.get("role") != "link")
    assert a["fingerprint"] == b["fingerprint"]


def test_real_clickable_change_still_changes_fingerprint():
    base = parse_hierarchy(_webview_xml(), "com.app", "Main")["fingerprint"]
    relabeled = parse_hierarchy(_webview_xml(btn_label="Entrar"), "com.app", "Main")["fingerprint"]
    moved = parse_hierarchy(_webview_xml(btn_bounds="[10,700][500,800]"), "com.app", "Main")["fingerprint"]
    assert base != relabeled and base != moved


def test_nested_same_text_synthesizes_one_link():
    xml = (
        '<hierarchy><node package="com.app" class="android.webkit.WebView" enabled="true"'
        ' bounds="[0,0][1000,2000]">'
        '<node package="com.app" class="android.view.View" text="Ayuda" enabled="true"'
        ' bounds="[0,100][500,300]">'
        '<node package="com.app" class="android.widget.TextView" text="Ayuda" enabled="true"'
        ' bounds="[50,150][400,250]"/></node>'
        '<node package="com.app" class="android.widget.TextView" text="Ayuda" enabled="true"'
        ' bounds="[600,100][900,300]"/>'
        '</node></hierarchy>'
    )
    p = parse_hierarchy(xml, "com.app", "Main")
    links = [a["bounds"] for a in p["actions"] if a.get("role") == "link"]
    assert links == [[0, 100, 500, 300], [600, 100, 900, 300]]  # separate one elsewhere kept


def _edit(text, rid="com.app:id/f", password=False, hint="", bounds="[10,300][900,400]"):
    return (f'<node package="com.app" class="android.widget.EditText" text="{text}" hint="{hint}"'
            f' resource-id="{rid}" password="{"true" if password else "false"}" clickable="true"'
            f' enabled="true" bounds="{bounds}"/>')


def test_password_edittext_text_blanked_everywhere():
    xml = ('<hierarchy><node package="com.app" class="android.widget.FrameLayout" enabled="true"'
           ' bounds="[0,0][1000,2000]">'
           + _edit("hunter2secret", rid="com.app:id/pw", password=True)
           # a disabled password node is not an action but must not leak as page text
           + '<node package="com.app" class="android.widget.EditText" text="disabledSecret"'
           ' password="true" enabled="false" bounds="[10,500][900,600]"/>'
           # an unlabeled field just below: the password text must not become its label
           + _edit("", rid="", bounds="[10,620][900,700]")
           + '</node></hierarchy>')
    p = parse_hierarchy(xml, "com.app", "Main")
    blob = repr(p)
    assert "hunter2secret" not in blob and "disabledSecret" not in blob
    pw = next(a for a in p["actions"] if a.get("resource_id") == "com.app:id/pw")
    assert pw["password"] and pw["value"] == ""


def test_edittext_emits_hint():
    xml = ('<hierarchy><node package="com.app" class="android.widget.FrameLayout" enabled="true"'
           ' bounds="[0,0][1000,2000]">' + _edit("482913", rid="com.app:id/etOtp", hint="Código")
           + '</node></hierarchy>')
    a = next(a for a in parse_hierarchy(xml, "com.app", "Main")["actions"] if a.get("kind") == "fill")
    assert a["label"] == "482913" and a["hint"] == "Código"


def test_synthetic_links_have_own_id_namespace():
    plain = parse_hierarchy(_webview_xml(), "com.app", "Main")
    ids = {a["label"]: a["id"] for a in plain["actions"] if "bounds" in a}
    assert ids == {"Su sesión expira en 4:59": "w0", "Ingresar": "n0"}
    # a link appearing above a real element never shifts the real element's id
    xml = _webview_xml().replace(
        '<node package="com.app" class="android.widget.TextView"',
        '<node package="com.app" class="android.widget.TextView" text="Nuevo" enabled="true"'
        ' bounds="[10,5][900,9]"/><node package="com.app" class="android.widget.TextView"', 1)
    more = parse_hierarchy(xml, "com.app", "Main")
    real = [a for a in more["actions"] if "bounds" in a and not a.get("synthetic")]
    assert [(a["id"], a["label"]) for a in real] == [("n0", "Ingresar")]
    assert [a["id"] for a in more["actions"] if a.get("synthetic")] == ["w0", "w1"]


def test_unlabelled_field_takes_overlay_placeholder_not_far_text():
    xml = ('<hierarchy><node package="a.b" class="android.widget.FrameLayout" enabled="true" bounds="[0,0][2560,1600]">'
           '<node package="a.b" class="android.widget.TextView" text="Projects" enabled="true" bounds="[153,474][291,522]"/>'
           '<node package="a.b" class="android.view.View" content-desc="Ask ChatGPT" enabled="true" bounds="[1056,583][2333,637]"/>'
           '<node package="a.b" class="android.widget.EditText" text="" clickable="true" enabled="true" bounds="[1056,556][2333,664]"/>'
           '</node></hierarchy>')
    fills = [a for a in parse_hierarchy(xml, "a.b", "M")["actions"] if a["kind"] == "fill"]
    assert [a["label"] for a in fills] == ["Ask ChatGPT"]


def test_unlabelled_clickable_gets_descriptive_label():
    xml = ('<hierarchy><node package="a.b" class="android.widget.FrameLayout" enabled="true" bounds="[0,0][1000,2000]">'
           '<node package="a.b" class="android.view.View" clickable="true" enabled="true" bounds="[36,414][981,1202]"/>'
           '<node package="a.b" class="android.widget.ImageView" clickable="true" enabled="true" bounds="[0,0][100,100]"/>'
           '</node></hierarchy>')
    labels = [a["label"] for a in parse_hierarchy(xml, "a.b", "M")["actions"] if "bounds" in a]
    assert labels == ["Unlabelled element (945x788 px)", "Unlabelled image (100x100 px)"]
