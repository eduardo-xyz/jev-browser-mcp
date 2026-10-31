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


def _top_eval(mapping):
    """Browser.evaluate stub: child-origins list, location.href, and the top-frame read dict."""
    def ev(expr):
        if "new URL(f.src" in expr:
            return mapping.get("origins", [])
        if "location.href" in expr:
            return mapping.get("url", "https://app.example/x")
        if "querySelectorAll('button" in expr:
            return mapping.get("top", {})
        return True
    return ev


def test_read_merges_top_and_cross_origin_iframe(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = _top_eval({"origins": ["https://content.other"],
                                        "top": {"text": "Workflows", "labels": ["Create workflow"]}})

    def fake_cdp(method, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"type": "page", "url": "https://app.example/x", "targetId": "P"},
                {"type": "iframe", "url": "https://content.other/app?loc=1", "targetId": "IF"},
                {"type": "iframe", "url": "https://ads.unrelated/banner", "targetId": "OTHER"}]}
        if method == "Target.attachToTarget":
            assert kw["targetId"] == "IF"  # origin match only; the unrelated iframe is skipped
            return {"sessionId": "IFSESS"}
        if method == "Runtime.evaluate" and kw.get("session_id") == "IFSESS":
            return {"result": {"value": {"text": "Round Robin\nStop AI when task is made",
                                         "labels": ["Publish"]}}}
        return {}

    monkeypatch.setattr(chrome_mod, "_cdp", fake_cdp)
    out = t._read_once()
    assert out["readable"] is True and out["url"] == "https://app.example/x"
    assert "Round Robin" in out["text"] and "Stop AI when task is made" in out["text"]
    assert "Create workflow" in out["text"] and "Publish" in out["text"]
    assert [fr["url"] for fr in out["frames"]] == ["https://content.other/app?loc=1"]


def test_read_dedupes_identical_iframes_from_two_tabs(monkeypatch):
    # Two tabs open the same page -> two iframe targets with identical content; keep one block.
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = _top_eval({"origins": ["https://content.other"], "top": {"text": "Top"}})
    attached = []

    def fake_cdp(method, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"type": "iframe", "url": "https://content.other/app", "targetId": "IF1"},
                {"type": "iframe", "url": "https://content.other/app", "targetId": "IF2"}]}
        if method == "Target.attachToTarget":
            attached.append(kw["targetId"])
            return {"sessionId": "S-" + kw["targetId"]}
        if method == "Runtime.evaluate":
            return {"result": {"value": {"text": "Same workflows", "labels": []}}}
        return {}

    monkeypatch.setattr(chrome_mod, "_cdp", fake_cdp)
    out = t._read_once()
    assert attached == ["IF1", "IF2"]  # both attached (and detached)
    assert len(out["frames"]) == 1 and out["text"].count("Same workflows") == 1


def test_read_without_iframes_returns_top_only(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = _top_eval({"origins": [], "top": {"text": "Just the top", "labels": []}})
    monkeypatch.setattr(chrome_mod, "_cdp", lambda method, **kw: {})
    out = t._read_once()
    assert out["frames"] == [] and out["text"] == "Just the top" and out["readable"] is True


def test_read_truncates_to_limit(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    big = "x" * (chrome_mod.READ_LIMIT + 5000)
    f.browsers[0].evaluate = _top_eval({"origins": [], "top": {"text": big, "labels": []}})
    monkeypatch.setattr(chrome_mod, "_cdp", lambda method, **kw: {})
    assert len(t._read_once()["text"]) == chrome_mod.READ_LIMIT


def test_read_polls_until_text_stops_growing(monkeypatch):
    # fake_clock makes _sleep instant; read() should keep re-reading while text grows, then stop.
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    seq = iter([
        {"url": "u", "text": "a" * 10, "frames": [], "readable": True},
        {"url": "u", "text": "a" * 50, "frames": [], "readable": True},
        {"url": "u", "text": "a" * 50, "frames": [], "readable": True},  # stable -> return
    ])
    calls = []
    monkeypatch.setattr(t, "_read_once", lambda: calls.append(1) or next(seq))
    out = t.read()
    assert len(out["text"]) == 50 and len(calls) == 3


def test_read_stops_at_settle_deadline(monkeypatch):
    # text that never stops growing must still return by the deadline, not loop forever.
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    n = [0]

    def grow():
        n[0] += 1
        return {"url": "u", "text": "a" * n[0], "frames": [], "readable": True}

    monkeypatch.setattr(t, "_read_once", grow)
    out = t.read(settle_s=3, interval_s=1)
    assert out["text"]  # returned without hanging


def test_observe_merges_iframe_actions_tagged_with_frame(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = lambda expr: []  # no password / overlay actions
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {"actions": [
        {"id": "e1", "node": 5, "kind": "click", "label": "Create workflow", "role": "button", "value": ""},
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down"}]})
    page = t.observe()
    frame_actions = [a for a in page["actions"] if a.get("frame")]
    assert len(frame_actions) == 1
    a = frame_actions[0]
    assert a == {"id": "f1e1", "node": 5, "kind": "click", "label": "Create workflow",
                 "role": "button", "value": "", "frame": 1}
    assert t._frames[1] == {"session": "S1", "origin": "https://content.other"}
    # iframe actions are folded into the fingerprint so an iframe change counts as progress
    assert isinstance(page.get("fingerprint"), str) and page["fingerprint"]


def test_observe_iframe_change_changes_fingerprint(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = lambda expr: []
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    snaps = iter([
        {"actions": [{"id": "e1", "node": 1, "kind": "click", "label": "Round Robin"}]},
        {"actions": [{"id": "e1", "node": 1, "kind": "click", "label": "Round Robin (editing)"}]},
    ])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: next(snaps))
    fp1 = t.observe()["fingerprint"]
    fp2 = t.observe()["fingerprint"]
    assert fp1 != fp2  # a label change inside the iframe is visible as progress


def test_act_routes_iframe_action_with_translated_coords(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    b = f.browsers[0]
    t._frames = {1: {"session": "S1", "origin": "https://content.other"}}
    monkeypatch.setattr(t, "_resolve_in_frame", lambda sid, node: {"x": 30, "y": 20})
    monkeypatch.setattr(t, "_iframe_offset", lambda origin: {"x": 100, "y": 50, "w": 1, "h": 1})
    t.act({"id": "f1e1", "node": 5, "kind": "click", "frame": 1}, {})
    mouse = [p for m, p in b.calls if m == "Input.dispatchMouseEvent"]
    assert [p["type"] for p in mouse] == ["mousePressed", "mouseReleased"]
    assert all(p["x"] == 130 and p["y"] == 70 for p in mouse)  # 100+30, 50+20
    assert b.acts == []  # upstream act path is never used for an iframe action


def test_act_iframe_fill_selects_all_then_inserts(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    b = f.browsers[0]
    t._frames = {1: {"session": "S1", "origin": "https://content.other"}}
    monkeypatch.setattr(t, "_resolve_in_frame", lambda sid, node: {"x": 5, "y": 5})
    monkeypatch.setattr(t, "_iframe_offset", lambda origin: {"x": 0, "y": 0, "w": 1, "h": 1})
    t.act({"id": "f1e2", "node": 9, "kind": "fill", "frame": 1}, {}, text="hello")
    methods = [m for m, _ in b.calls]
    assert methods == ["Input.dispatchMouseEvent", "Input.dispatchMouseEvent",
                       "Input.dispatchKeyEvent", "Input.dispatchKeyEvent", "Input.insertText"]
    assert b.calls[-1][1]["text"] == "hello"


def test_act_iframe_stale_before_dispatch_raises_stalepage(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    b = f.browsers[0]
    t._frames = {1: {"session": "S1", "origin": "https://content.other"}}
    monkeypatch.setattr(t, "_resolve_in_frame", lambda sid, node: None)  # target gone
    with pytest.raises(StalePage):
        t.act({"id": "f1e1", "node": 5, "kind": "click", "frame": 1}, {})
    assert not any(m.startswith("Input.") for m, _ in b.calls)  # nothing dispatched


def test_act_iframe_unknown_frame_raises_stalepage():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    t._frames = {}
    with pytest.raises(StalePage):
        t.act({"id": "f9e1", "node": 5, "kind": "click", "frame": 9}, {})


def test_act_top_frame_action_unchanged_by_oopif_branch():
    # an action with no `frame` still goes through the upstream (money-safe) path verbatim
    t, b = make()
    t.act({"id": "e1", "node": 2, "kind": "click"}, {}, verify=True)
    assert b.acts == [({"id": "e1", "node": 2, "kind": "click"}, None)]


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
