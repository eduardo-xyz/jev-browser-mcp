from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_runner.targets.android import AndroidTarget, adb_input_text_arg, resolve_app
from jev_runner.targets.base import DeviceDisconnected, StalePage, TargetBlocked

FIXTURE = Path("tests/fixtures/android/bac_login.xml")
SHADE_XML = (
    '<hierarchy><node package="com.android.systemui" class="android.widget.FrameLayout"'
    ' enabled="true" bounds="[0,0][1000,2000]"><node package="com.android.systemui"'
    ' class="android.widget.Button" clickable="true" enabled="true" text="Wi-Fi"'
    ' bounds="[10,100][400,300]"/></node></hierarchy>'
)


class FakeLink:
    def __init__(self, fail_after=None):
        self.ensure_calls = 0
        self.fail_after = fail_after

    def ensure_connected(self):
        self.ensure_calls += 1
        if self.fail_after is not None and self.ensure_calls > self.fail_after:
            raise DeviceDisconnected("gone")
        return "serial"

    def close(self):
        pass


class FakeDevice:
    def __init__(self):
        self.xml = FIXTURE.read_text()
        self.current = {"package": "net.bac.sbe.android", "activity": "Main"}
        self.calls = []
        self.dumps = 0
        self.fail_next_dump = None
        self.fail_next_click = None
        self.shell_out = ""
        self.labels = {}
        self.focus_override = None  # bounds dict, or "none" for no focus
        self._last_tap = (0, 0)
        self.fail_connect_after = None
        self.info_errors = {}

    def __call__(self, **kw):
        d = self
        if "resourceId" in kw:
            rid = kw["resourceId"]

            class RidSel:
                @property
                def exists(self):
                    return True

                def set_text(self, text):
                    d.calls.append(("set_text", rid, text))

            return RidSel()
        assert kw == {"focused": True}

        class Sel:
            @property
            def exists(self):
                return d.focus_override != "none"

            @property
            def info(self):
                if isinstance(d.focus_override, dict):
                    return {"bounds": d.focus_override}
                x, y = d._last_tap
                return {"bounds": {"left": x - 5, "top": y - 5, "right": x + 5, "bottom": y + 5}}

            def set_text(self, text):
                d.calls.append(("set_text", "focused", text))

        return Sel()

    def app_current(self):
        return self.current

    def dump_hierarchy(self, compressed=False):
        self.dumps += 1
        if self.fail_next_dump:
            e, self.fail_next_dump = self.fail_next_dump, None
            raise e
        return self.xml

    def click(self, x, y):
        if self.fail_next_click:
            e, self.fail_next_click = self.fail_next_click, None
            raise e
        self._last_tap = (x, y)
        self.calls.append(("click", x, y))

    def send_keys(self, text, clear=True):
        self.calls.append(("send_keys", text, clear))

    def press(self, key):
        self.calls.append(("press", key))

    def open_notification(self):
        self.calls.append(("open_notification",))

    def swipe_ext(self, direction, scale=0.9):
        self.calls.append(("swipe_ext", direction, scale))

    def app_start(self, pkg):
        self.calls.append(("app_start", pkg))

    def screenshot(self, path):
        self.calls.append(("screenshot", path))

    def shell(self, cmd):
        if str(cmd).startswith("input "):
            self.calls.append(("shell", cmd))
        return SimpleNamespace(output=self.shell_out)

    def app_info(self, pkg):
        if pkg in self.info_errors:
            e = self.info_errors[pkg]
            if isinstance(e, tuple):  # one-shot
                del self.info_errors[pkg]
                raise e[0]
            raise e
        return {"label": self.labels[pkg]}


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    monkeypatch.setattr("jev_runner.targets.android.sleep", lambda s: None)


@pytest.fixture
def fake_device():
    return FakeDevice()


@pytest.fixture
def link():
    return FakeLink()


def mk(link, d, **kw):
    return AndroidTarget(link, connect=lambda s: d, **kw)


def btn(p):
    return next(a for a in p["actions"] if a.get("resource_id", "").endswith("btnLogin"))


def test_observe_maps_fixture(fake_device, link):
    p = mk(link, fake_device).observe()
    assert any(a.get("resource_id", "").endswith("btnLogin") for a in p["actions"])


def test_click_taps_bounds_center(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    t.act(btn(p), p)
    assert fake_device.calls[-1] == ("click", 799, 1265)


def test_fill_clicks_then_types(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    f = next(a for a in p["actions"] if a.get("resource_id", "").endswith("txtUsername"))
    t.act(f, p, text="bob")
    assert fake_device.calls[-1] == ("set_text", f["resource_id"], "bob")
    assert fake_device.calls[-2][0] == "click"


def test_back_scroll_wait(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    t.act({"kind": "back", "id": "back"}, p)
    t.act({"kind": "scroll", "id": "s", "delta": 1}, p)
    t.act({"kind": "scroll", "id": "s", "delta": -1}, p)
    t.act({"kind": "wait", "id": "wait"}, p)
    assert fake_device.calls == [("press", "back"), ("swipe_ext", "up", 0.6), ("swipe_ext", "down", 0.6)]


def test_verify_raises_stale_when_screen_changed(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    fake_device.xml = "<hierarchy/>"
    with pytest.raises(StalePage):
        t.act(p["actions"][0], p, verify=True)
    assert not any(c[0] == "click" for c in fake_device.calls)


def test_verify_passes_when_unchanged(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    t.act(btn(p), p, verify=True)
    assert fake_device.calls[-1] == ("click", 799, 1265)


def test_reconnects_once_on_connection_error(fake_device, link):
    fake_device.fail_next_dump = ConnectionError("adb gone")
    mk(link, fake_device).observe()
    assert link.ensure_calls == 2


def test_observe_disconnect_propagates(fake_device):
    link = FakeLink(fail_after=1)
    fake_device.fail_next_dump = ConnectionError("adb gone")
    with pytest.raises(DeviceDisconnected):
        mk(link, fake_device).observe()


def test_act_does_not_resend_input_after_click_error(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    fake_device.fail_next_click = ConnectionError("dropped")
    with pytest.raises(DeviceDisconnected):
        t.act(btn(p), p, verify=True)
    assert not any(c[0] == "click" for c in fake_device.calls)


def test_act_retries_when_verify_dump_fails(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    fake_device.fail_next_dump = ConnectionError("x")
    t.act(btn(p), p, verify=True)
    assert fake_device.calls[-1] == ("click", 799, 1265)
    assert link.ensure_calls == 2


def test_observe_reparses_with_systemui_when_shade_open(fake_device, link):
    fake_device.xml = SHADE_XML
    fake_device.current = {"package": "com.app", "activity": "Main"}
    p = mk(link, fake_device).observe()
    assert p["shade_open"] and any(a.get("label") == "Wi-Fi" for a in p["actions"])
    assert fake_device.dumps == 1


def test_system_controls_dispatch(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    for kind in ("home", "recents", "notifications"):
        t.act(next(a for a in p["actions"] if a["id"] == kind), p)
    assert fake_device.calls == [("press", "home"), ("press", "recent"), ("open_notification",)]


def test_screenshot_and_close(fake_device, link, tmp_path):
    t = mk(link, fake_device)
    assert t.screenshot(tmp_path / "a.png") == tmp_path / "a.png"
    assert fake_device.calls[-1] == ("screenshot", str(tmp_path / "a.png"))
    t.close()


def test_package_app_started_when_not_foreground(fake_device, link):
    mk(link, fake_device, app="com.other.app")
    assert ("app_start", "com.other.app") in fake_device.calls


def test_package_app_foreground_not_started(fake_device, link):
    mk(link, fake_device, app="net.bac.sbe.android")
    assert not any(c[0] == "app_start" for c in fake_device.calls)


def _dev_with_apps(labels):
    d = FakeDevice()
    d.labels = labels
    d.shell_out = "\n".join(f"{p}/.Main" for p in labels)
    return d


def test_resolve_app_by_name():
    d = _dev_with_apps({"com.whatsapp": "WhatsApp", "com.x.cam": "Camera"})
    assert resolve_app(d, "whatsapp") == "com.whatsapp"


def test_resolve_app_prefix_unique():
    d = _dev_with_apps({"com.whatsapp": "WhatsApp", "com.x.cam": "Camera"})
    assert resolve_app(d, "what") == "com.whatsapp"


def test_resolve_app_ambiguous():
    d = _dev_with_apps({"a.b": "Maps Go", "c.d": "Maps Lite"})
    with pytest.raises(TargetBlocked) as e:
        resolve_app(d, "maps")
    assert e.value.reason.startswith("app_ambiguous:")


def test_resolve_app_missing():
    d = _dev_with_apps({"a.b": "Maps"})
    with pytest.raises(TargetBlocked) as e:
        resolve_app(d, "zzz")
    assert e.value.reason == "app_not_found: zzz"


def test_app_name_resolved_and_started(link):
    d = _dev_with_apps({"com.whatsapp": "WhatsApp"})
    mk(link, d, app="WhatsApp")
    assert ("app_start", "com.whatsapp") in d.calls


def _fill_target(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    f = next(a for a in p["actions"] if a.get("resource_id", "").endswith("txtUsername"))
    return t, p, f


def test_fill_focus_elsewhere_raises_stale_without_typing(fake_device, link):
    t, p, f = _fill_target(fake_device, link)
    fake_device.focus_override = {"left": 0, "top": 0, "right": 10, "bottom": 10}
    with pytest.raises(StalePage):
        t.act(f, p, text="secret")
    assert not any(c[0] == "send_keys" for c in fake_device.calls)


def test_fill_no_focus_raises_stale(fake_device, link):
    t, p, f = _fill_target(fake_device, link)
    fake_device.focus_override = "none"
    with pytest.raises(StalePage):
        t.act(f, p, text="secret")
    assert not any(c[0] == "send_keys" for c in fake_device.calls)


def test_click_does_not_check_focus(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    fake_device.focus_override = "none"
    t.act(btn(p), p)
    assert fake_device.calls[-1] == ("click", 799, 1265)


def test_connection_error_after_retry_maps_to_disconnected(fake_device, link):
    t = mk(link, fake_device)
    orig = fake_device.dump_hierarchy
    def boom(compressed=False):
        raise ConnectionError("down")
    fake_device.dump_hierarchy = boom
    with pytest.raises(DeviceDisconnected):
        t.observe()


def test_connect_error_inside_input_maps_to_disconnected(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    t.d = None
    def bad(serial):
        raise ConnectionError("no")
    t._connect = bad
    with pytest.raises(DeviceDisconnected):
        t.act(btn(p), p)


def test_resolve_app_survives_connection_error_via_reconnect(link):
    d = _dev_with_apps({"com.wa.messenger": "WhatsApp"})
    d.info_errors["com.wa.messenger"] = (ConnectionError("x"),)
    t = mk(link, d, app="whatsapp")
    assert ("app_start", "com.wa.messenger") in d.calls
    assert link.ensure_calls == 2


def test_resolve_app_does_not_cache_failures():
    d = _dev_with_apps({"com.wa.messenger": "WhatsApp", "b.c": "Other"})
    d.info_errors["com.wa.messenger"] = ValueError("flaky")
    cache = {}
    with pytest.raises(TargetBlocked):
        resolve_app(d, "whatsapp", cache)
    assert "com.wa.messenger" not in cache
    del d.info_errors["com.wa.messenger"]
    assert resolve_app(d, "whatsapp", cache) == "com.wa.messenger"


def test_resolve_app_blank_name():
    d = _dev_with_apps({"a.b": "Maps"})
    with pytest.raises(TargetBlocked) as e:
        resolve_app(d, "  ")
    assert e.value.reason == "app_not_found: "


def test_resolve_app_ambiguous_dedupes_labels():
    d = _dev_with_apps({"a.b": "Maps", "c.d": "Maps"})
    with pytest.raises(TargetBlocked) as e:
        resolve_app(d, "maps")
    assert e.value.reason == "app_ambiguous: Maps"


class NoLabelDevice(FakeDevice):
    """uiautomator2 on some devices returns app_info without a label."""

    def __init__(self, pkgs):
        super().__init__()
        self.shell_out = "\n".join(f"{p}/.Main" for p in pkgs)
        self.info_calls = 0

    def app_info(self, pkg):
        self.info_calls += 1
        return {"versionName": "5.53.0", "versionCode": 553}


def test_resolve_app_by_package_token_exact():
    d = NoLabelDevice(["net.bac.sbe.android", "com.google.android.gm", "com.bacon.app"])
    assert resolve_app(d, "BAC") == "net.bac.sbe.android"
    assert d.info_calls == 0  # token match never reads labels


def test_resolve_app_by_package_token_prefix():
    d = NoLabelDevice(["net.bac.sbe.android", "com.whatsapp"])
    assert resolve_app(d, "whats") == "com.whatsapp"


def test_resolve_app_token_ambiguous():
    d = NoLabelDevice(["net.bac.sbe.android", "cr.bac.empresas"])
    with pytest.raises(TargetBlocked) as e:
        resolve_app(d, "bac")
    assert e.value.reason.startswith("app_ambiguous:")
    assert "net.bac.sbe.android" in e.value.reason and "cr.bac.empresas" in e.value.reason


def test_resolve_app_no_token_no_label_not_found():
    d = NoLabelDevice(["com.google.android.gm"])
    with pytest.raises(TargetBlocked) as e:
        resolve_app(d, "Gmail")
    assert e.value.reason == "app_not_found: Gmail"


def test_resolve_app_falls_back_to_labels_when_no_token():
    d = _dev_with_apps({"com.google.android.gm": "Gmail", "a.b": "Maps"})
    assert resolve_app(d, "gmail") == "com.google.android.gm"


def test_label_cache_shared_via_link(link):
    link.app_labels = {}
    d = _dev_with_apps({"com.google.android.gm": "Gmail"})
    calls = {"n": 0}
    orig = d.app_info

    def counting(pkg):
        calls["n"] += 1
        return orig(pkg)

    d.app_info = counting
    mk(link, d, app="Gmail")
    mk(link, d, app="Gmail")  # second target on the same daemon link: cached
    assert calls["n"] == 1
    assert link.app_labels == {"com.google.android.gm": "Gmail"}


def test_launch_waits_for_screen_to_settle(fake_device, link):
    # cold start: splash with nothing to act on, then the real screen
    splash = '<hierarchy><node package="com.other.app" class="android.widget.FrameLayout" enabled="true" bounds="[0,0][1000,2000]"/></hierarchy>'
    seq = [splash, splash, fake_device.xml, fake_device.xml]
    fake_device.dump_hierarchy = lambda compressed=False: seq.pop(0) if len(seq) > 1 else seq[0]
    t = mk(link, fake_device, app="com.other.app")
    assert len(seq) == 1 and t.observe()["actions"]


def test_launch_settle_gives_up_after_timeout(fake_device, link, monkeypatch):
    monkeypatch.setattr(AndroidTarget, "settle_timeout", 0.3)
    splash = '<hierarchy><node package="com.other.app" class="android.widget.FrameLayout" enabled="true" bounds="[0,0][1000,2000]"/></hierarchy>'
    fake_device.dump_hierarchy = lambda compressed=False: splash
    mk(link, fake_device, app="com.other.app")  # returns instead of hanging


def test_fill_clears_clipboard_after_typing(fake_device, link):
    fake_device.clipboard = None
    t = mk(link, fake_device)
    p = t.observe()
    f = next(a for a in p["actions"] if a["kind"] == "fill")
    t.act(f, p, text="s3cret")
    assert fake_device.calls[-1][0] == "set_text" and fake_device.clipboard == ""


def test_observe_reads_the_shown_app_when_focus_is_elsewhere(fake_device, link):
    # A keyboard/overlay window holds focus while WhatsApp fills the screen: reading only the
    # focused package used to come back empty and stop the run as screen_unreadable.
    from tests.test_android_tree import _OVERLAY_XML
    fake_device.xml = _OVERLAY_XML
    fake_device.current = {"package": "com.grammarly.android.keyboard", "activity": "Bubble"}
    p = mk(link, fake_device).observe()
    assert p["readable"] and "El costo es 30 mil" in p["text"]
    assert p["url"].startswith("com.whatsapp/") and p["focused_package"] == "com.grammarly.android.keyboard"


def test_observe_keeps_focused_app_when_it_is_readable(fake_device, link):
    p = mk(link, fake_device).observe()
    assert p["url"].startswith("net.bac.sbe.android/") and "focused_package" not in p


# --- adb `input text` secure-field escaping (hardening: only spaces were escaped before) ---

def _shell_tokens(arg):
    """How a POSIX device shell would tokenise `input text <arg>` — proves no metachar escapes
    the single argument (no injection, no word-split)."""
    import shlex
    return shlex.split("input text " + arg)


def test_input_text_arg_plain_text_is_passed_through():
    assert adb_input_text_arg("hunter2") == "hunter2"
    assert _shell_tokens(adb_input_text_arg("hunter2")) == ["input", "text", "hunter2"]


def test_input_text_arg_encodes_space_as_percent_s():
    # a space must survive as a space on-device; the established transport encodes it as %s
    assert adb_input_text_arg("my pass") == "my%spass"
    assert _shell_tokens(adb_input_text_arg("my pass")) == ["input", "text", "my%spass"]


def test_input_text_arg_shell_metacharacters_stay_one_argument():
    # &, ;, |, $, (, ), <, >, `, ", ' must not break out of the single argument
    secret = "a&b;c|d$e(f)g<h>i`j\"k'l"
    tokens = _shell_tokens(adb_input_text_arg(secret))
    assert tokens == ["input", "text", secret]


def test_input_text_arg_blocks_command_injection():
    tokens = _shell_tokens(adb_input_text_arg("x; rm -rf /"))
    # exactly three tokens: the malicious `;` and the rest are inside the one arg, not a command
    assert tokens[:2] == ["input", "text"]
    assert len(tokens) == 3
    assert "rm" in tokens[2] and ";" in tokens[2]


def test_input_text_arg_refuses_literal_percent():
    # %s is our space encoding, so a literal % is ambiguous over this transport: signal fallback
    assert adb_input_text_arg("50%off") is None
    assert adb_input_text_arg("abc") is not None


# --- foreground app + keyboard in one shell round trip (adbutils' app_current takes ~10 s) ---

def test_focused_component_parsing():
    from jev_runner.targets import android as mod
    assert mod._focused_component(
        "mCurrentFocus=Window{93ac883 u0 com.whatsapp/com.whatsapp.Conversation}"
    ) == ("com.whatsapp", "com.whatsapp.Conversation")
    assert mod._focused_component(
        "topResumedActivity=ActivityRecord{2 u0 com.app/.Main t12}") == ("com.app", ".Main")
    assert mod._focused_component("mCurrentFocus=null") == (None, None)
    assert mod._focused_component("mCurrentFocus=Window{1 u0 NotificationShade}") == (None, None)
    assert mod._short_activity("com.whatsapp", "com.whatsapp.Conversation") == ".Conversation"
    assert mod._short_activity("com.whatsapp", "com.other.Activity") == "com.other.Activity"


def _never():
    raise AssertionError("slow app_current path used")


def test_observe_reads_foreground_in_one_shell_call(fake_device, link):
    fake_device.shell_out = ("mCurrentFocus=Window{93ac883 u0 net.bac.sbe.android/net.bac.sbe.android.ui.Main}\n"
                             "      mInputShown=true\n")
    fake_device.app_current = _never
    p = mk(link, fake_device).observe()
    assert p["url"] == "net.bac.sbe.android/.ui.Main"  # short form, as saved flows record it
    assert any(a["id"] == "hide_keyboard" for a in p["actions"])


def test_observe_falls_back_to_resumed_activity_when_focus_is_not_an_app(fake_device, link):
    answers = {"mCurrentFocus": "mCurrentFocus=Window{1 u0 NotificationShade}\n mInputShown=false",
               "topResumedActivity": "topResumedActivity=ActivityRecord{2 u0 net.bac.sbe.android/.Home t12}"}
    fake_device.shell = lambda cmd: SimpleNamespace(
        output=next((v for k, v in answers.items() if k in cmd), ""))
    fake_device.app_current = _never
    p = mk(link, fake_device).observe()
    assert p["url"] == "net.bac.sbe.android/.Home"
    assert not any(a["id"] == "hide_keyboard" for a in p["actions"])


def test_observe_uses_app_current_only_when_nothing_is_focused(fake_device, link):
    fake_device.shell_out = "mCurrentFocus=null\n"
    p = mk(link, fake_device).observe()
    assert p["url"] == "net.bac.sbe.android/Main"  # from the fake's app_current


def test_refresh_is_one_read(fake_device, link):
    t = mk(link, fake_device)
    p = t.observe()
    before = fake_device.dumps
    assert t.refresh(p)["fingerprint"] == p["fingerprint"]
    assert fake_device.dumps == before + 1
