from pathlib import Path
from types import SimpleNamespace

import pytest

from jev_mcp.targets.android import AndroidTarget, resolve_app
from jev_mcp.targets.base import DeviceDisconnected, StalePage, TargetBlocked

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
        return SimpleNamespace(output=self.shell_out)

    def app_info(self, pkg):
        return {"label": self.labels[pkg]}


@pytest.fixture(autouse=True)
def _nosleep(monkeypatch):
    monkeypatch.setattr("jev_mcp.targets.android.sleep", lambda s: None)


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
    assert fake_device.calls[-1] == ("send_keys", "bob", True)
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
