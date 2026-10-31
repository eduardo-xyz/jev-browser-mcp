import base64

import pytest

from jev_runner.targets import chrome as chrome_mod
from jev_runner.targets.base import StalePage, TargetBlocked
from jev_runner.targets.chrome import ChromeTarget


class StalePage_(Exception):  # stands in for upstream StalePage (matched by class name)
    pass


StalePage_.__name__ = "StalePage"


class FakeBrowser:
    def __init__(self, factory):
        self.f = factory
        self.closed = False
        self.acts = []
        self.fresh_result = True
        self.act_exc = None
        self.observe_exc = None
        self.calls = []

    def evaluate(self, expr):
        f = self.f
        return f.loaded[min(f.idx, len(f.loaded) - 1)]

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "Page.navigate":
            self.f.renavigations += 1
            self.f.idx += 1
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(b"PNG").decode()}
        return {}

    def observe(self, screenshot=True):
        if self.observe_exc:
            raise self.observe_exc
        assert screenshot is False
        return {"url": "https://x.test/a?b=1", "title": "T", "text": "hi", "actions": []}

    def fresh(self, page, action=None):
        if isinstance(self.fresh_result, Exception):
            raise self.fresh_result
        return self.fresh_result

    def act(self, action, page, text=None):
        self.acts.append((action, text))
        if self.act_exc:
            raise self.act_exc

    def close(self):
        self.closed = True


class FakeBrowserFactory:
    def __init__(self, loaded):
        self.loaded = loaded
        self.opened = []
        self.renavigations = 0
        self.idx = -1
        self.browsers = []

    def __call__(self, url, background):
        self.opened.append(background)
        self.idx += 1
        b = FakeBrowser(self)
        self.browsers.append(b)
        return b


@pytest.fixture(autouse=True)
def fake_clock(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(chrome_mod, "_monotonic", lambda: t[0])
    monkeypatch.setattr(chrome_mod, "_sleep", lambda s: t.__setitem__(0, t[0] + s))


def test_retries_then_foreground_then_blocks():
    f = FakeBrowserFactory(loaded=[False, False, False])
    with pytest.raises(TargetBlocked) as e:
        ChromeTarget("https://app.gohighlevel.com", browser_factory=f)
    assert e.value.reason == "page_did_not_load"
    assert f.opened == [True, False] and f.renavigations == 1
    assert all(b.closed for b in f.browsers)


def test_second_navigation_recovers():
    f = FakeBrowserFactory(loaded=[False, True])
    ChromeTarget("https://x.test", browser_factory=f)
    assert f.opened == [True]


def test_foreground_reopen_recovers():
    f = FakeBrowserFactory(loaded=[False, False, True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    assert f.opened == [True, False]
    assert f.browsers[0].closed and not f.browsers[1].closed
    t.close()
    assert f.browsers[1].closed


def test_first_load_ok_no_renavigate():
    f = FakeBrowserFactory(loaded=[True])
    ChromeTarget("https://x.test", browser_factory=f)
    assert f.renavigations == 0


def make():
    f = FakeBrowserFactory(loaded=[True])
    return ChromeTarget("https://x.test", browser_factory=f), f.browsers[0]


def test_observe_adds_readable_keeps_url():
    t, _ = make()
    p = t.observe()
    assert p["readable"] is True and p["url"] == "https://x.test/a?b=1" and p["text"] == "hi"


def test_observe_translates_upstream_stale():
    t, b = make()
    b.observe_exc = StalePage_("x")
    with pytest.raises(StalePage):
        t.observe()


def test_act_stale_before_input_raises_ours():
    t, b = make()
    b.fresh_result = False
    with pytest.raises(StalePage):
        t.act({"kind": "click", "node": 1}, {})
    assert b.acts == []


def test_fresh_check_upstream_stale_translated():
    t, b = make()
    b.fresh_result = StalePage_("x")
    with pytest.raises(StalePage):
        t.act({"kind": "click", "node": 1}, {})
    assert b.acts == []


def test_act_upstream_stale_is_retryable_not_uncertain():
    # Upstream raises StalePage only from its pre-input hit-test, so nothing was dispatched:
    # the Chrome target surfaces our StalePage (retryable), never action_uncertain.
    t, b = make()
    b.act_exc = StalePage_("Target changed or is covered. Observe again.")
    with pytest.raises(StalePage):
        t.act({"kind": "fill", "node": 1}, {}, text="abc")


def test_act_other_error_is_uncertain():
    t, b = make()
    b.act_exc = ValueError("bad")
    with pytest.raises(RuntimeError, match="action_uncertain: bad"):
        t.act({"kind": "click", "node": 1}, {})


def test_act_ok_delegates():
    t, b = make()
    t.act({"kind": "fill", "node": 1}, {}, text="abc", verify=True)
    assert b.acts == [({"kind": "fill", "node": 1}, "abc")]


def test_screenshot_writes_file(tmp_path):
    t, _ = make()
    out = t.screenshot(tmp_path / "s.png")
    assert out.read_bytes() == b"PNG"


def test_observe_exposes_password_field_as_secret_fill():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    b.evaluate = lambda expr: ([{"node": 1000000, "label": "Password"}]
                               if "password" in expr else True)
    p = t.observe()
    pw = [a for a in p["actions"] if a.get("password")]
    assert len(pw) == 1
    assert pw[0]["kind"] == "fill" and pw[0]["node"] == 1000000 and pw[0]["id"] == "pw1"
    # the runner must see it as secret
    from jev_runner.guards import is_secret_field
    from jev_runner.config import Config
    assert is_secret_field(pw[0], Config().secret_patterns)


def test_observe_without_password_fields_is_unchanged():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    f.browsers[0].evaluate = lambda expr: [] if "password" in expr else True
    p = t.observe()
    assert not any(a.get("password") for a in p["actions"]) and p["readable"] is True


def test_observe_exposes_overlay_buttons_not_already_present():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    def ev(expr):
        if "password" in expr:
            return []
        if "aria-modal" in expr:  # the overlay JS
            return [{"node": 2000000, "label": "Not now"}, {"node": 2000001, "label": "Try it now"}]
        return True
    b.evaluate = ev
    p = t.observe()
    ov = [a for a in p["actions"] if a.get("id", "").startswith("ov")]
    assert [a["label"] for a in ov] == ["Not now", "Try it now"]
    assert all(a["kind"] == "click" and isinstance(a["node"], int) for a in ov)


def test_read_merges_top_and_cross_origin_iframe(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].session = "TOP"

    def fake_cdp(method, **kw):
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"url": "https://app.example/x"},
                                  "childFrames": [{"frame": {"url": "https://content.other/app"}}]}}
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"type": "page", "url": "https://app.example/x", "targetId": "P"},
                {"type": "iframe", "url": "https://content.other/app", "targetId": "IF"},
                {"type": "iframe", "url": "https://unrelated.tab/zzz", "targetId": "OTHER"}]}
        if method == "Target.attachToTarget":
            assert kw["targetId"] == "IF"  # only this page's child, never the unrelated iframe
            return {"sessionId": "IFSESS"}
        if method == "Runtime.evaluate":
            if kw.get("session_id") == "TOP":
                return {"result": {"value": {"text": "Workflows", "labels": ["Create workflow"]}}}
            if kw.get("session_id") == "IFSESS":
                return {"result": {"value": {"text": "Round Robin\nStop AI when task is made",
                                             "labels": ["Publish"]}}}
        return {}

    monkeypatch.setattr(chrome_mod, "_cdp", fake_cdp)
    out = t.read()
    assert out["readable"] is True and out["url"] == "https://app.example/x"
    assert "Round Robin" in out["text"] and "Stop AI when task is made" in out["text"]
    assert "Create workflow" in out["text"] and "Publish" in out["text"]
    assert [fr["url"] for fr in out["frames"]] == ["https://content.other/app"]
    assert "Round Robin" in out["frames"][0]["text"]


def test_read_without_iframes_returns_top_only(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].session = "TOP"

    def fake_cdp(method, **kw):
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"url": "https://app.example/x"}, "childFrames": []}}
        if method == "Runtime.evaluate" and kw.get("session_id") == "TOP":
            return {"result": {"value": {"text": "Just the top", "labels": []}}}
        return {}

    monkeypatch.setattr(chrome_mod, "_cdp", fake_cdp)
    out = t.read()
    assert out["frames"] == [] and out["text"] == "Just the top" and out["readable"] is True


def test_read_truncates_to_limit(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].session = "TOP"
    big = "x" * (chrome_mod.READ_LIMIT + 5000)

    def fake_cdp(method, **kw):
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"url": "https://app.example/x"}, "childFrames": []}}
        if method == "Runtime.evaluate":
            return {"result": {"value": {"text": big, "labels": []}}}
        return {}

    monkeypatch.setattr(chrome_mod, "_cdp", fake_cdp)
    assert len(t.read()["text"]) == chrome_mod.READ_LIMIT


def test_overlay_button_already_in_actions_is_not_duplicated():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    orig = b.observe
    b.observe = lambda screenshot=True: {**orig(screenshot=screenshot),
                                         "actions": [{"id": "e1", "label": "Not now", "kind": "click", "node": 5}]}
    b.evaluate = lambda expr: [] if "password" in expr else (
        [{"node": 2000000, "label": "Not now"}] if "aria-modal" in expr else True)
    p = t.observe()
    assert [a for a in p["actions"] if a.get("id", "").startswith("ov")] == []
