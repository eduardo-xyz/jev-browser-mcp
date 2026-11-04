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
        self.target = "FAKE-TARGET"
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


def test_construction_claims_tab_with_broker(monkeypatch):
    claimed = []
    monkeypatch.setattr(chrome_mod.tab_groups, "claim", lambda t: claimed.append(t))
    f = FakeBrowserFactory(loaded=[True])
    ChromeTarget("https://x.test", browser_factory=f)
    assert claimed == ["FAKE-TARGET"]  # the created target is registered with the broker


def test_observe_pings_activity_when_grouping(monkeypatch):
    monkeypatch.setattr(chrome_mod.tab_groups, "enabled", lambda: True)
    monkeypatch.setattr(chrome_mod.tab_groups, "claim", lambda t: None)
    pings = []
    monkeypatch.setattr(chrome_mod.tab_groups, "activity", lambda ids: pings.append(ids))
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    assert t._grouping is True
    t._last_tab_ping = -1000  # force the throttle window open
    t.observe()
    assert pings == [["FAKE-TARGET"]]


def test_observe_no_ping_when_grouping_disabled(monkeypatch):
    pings = []
    monkeypatch.setattr(chrome_mod.tab_groups, "activity", lambda ids: pings.append(ids))
    f = FakeBrowserFactory(loaded=[True])  # conftest disables tab_groups -> enabled() False
    t = ChromeTarget("https://x.test", browser_factory=f)
    assert t._grouping is False
    t._last_tab_ping = -1000
    t.observe()
    assert pings == []


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


def test_iframe_targets_scopes_to_observed_tab_by_frame_id(monkeypatch):
    # Two tabs show the same page -> two same-origin iframe targets. frameId (== OOPIF targetId)
    # picks only the observed tab's own iframe, so the other tab's actions aren't merged/misrouted.
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    b = f.browsers[0]
    b.evaluate = lambda expr: (["https://content.other"] if "new URL(f.src" in expr else True)

    def fake_call(method, **params):
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelectorAll":
            return {"nodeIds": [2]}
        if method == "DOM.describeNode":
            return {"node": {"frameId": "FID1"}}
        return {}

    b.call = fake_call
    attached = []

    def fake_cdp(method, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"type": "iframe", "url": "https://content.other/app", "targetId": "FID1"},
                {"type": "iframe", "url": "https://content.other/app", "targetId": "FID2"}]}
        if method == "Target.attachToTarget":
            attached.append(kw["targetId"])
            return {"sessionId": "S-" + kw["targetId"]}
        return {}

    monkeypatch.setattr(chrome_mod, "_cdp", fake_cdp)
    out = t._iframe_targets()
    assert attached == ["FID1"]  # only the observed tab's own iframe
    assert [u for _, u in out] == ["https://content.other/app"]


def test_iframe_targets_falls_back_to_origin_without_frame_ids(monkeypatch):
    # When the frameId lookup is unavailable (DOM calls return nothing), origin match still works.
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    b = f.browsers[0]
    b.evaluate = lambda expr: (["https://content.other"] if "new URL(f.src" in expr else True)
    # b.call returns {} for DOM.* -> _observed_frame_ids() yields empty set
    attached = []

    def fake_cdp(method, **kw):
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"type": "iframe", "url": "https://content.other/app", "targetId": "IF"},
                {"type": "iframe", "url": "https://ads.unrelated/x", "targetId": "AD"}]}
        if method == "Target.attachToTarget":
            attached.append(kw["targetId"])
            return {"sessionId": "S"}
        return {}

    monkeypatch.setattr(chrome_mod, "_cdp", fake_cdp)
    out = t._iframe_targets()
    assert attached == ["IF"]  # origin match only; unrelated ad iframe skipped
    assert [u for _, u in out] == ["https://content.other/app"]


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
        {"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 560},
        {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}]})
    page = t.observe()
    frame_actions = [a for a in page["actions"] if a.get("frame")]
    assert {a["id"] for a in frame_actions} == {"f1e1", "f1scroll_down"}  # click + scroll, not wait
    click = next(a for a in frame_actions if a["id"] == "f1e1")
    # node is made globally unique (frame base * idx + real) so it can't collide with top-frame
    # node ids in the model's per-node dedup; frame_node keeps the real in-frame id for act()
    assert click == {"id": "f1e1", "node": chrome_mod._FRAME_NODE_BASE + 5, "frame_node": 5,
                     "kind": "click", "label": "Create workflow",
                     "role": "button", "value": "", "frame": 1}
    scroll = next(a for a in frame_actions if a["id"] == "f1scroll_down")
    assert scroll == {"id": "f1scroll_down", "kind": "scroll", "label": "Scroll down",
                      "delta": 560, "frame": 1}
    assert t._frames[1] == {"session": "S1", "origin": "https://content.other"}
    # iframe actions are folded into the fingerprint so an iframe change counts as progress
    assert isinstance(page.get("fingerprint"), str) and page["fingerprint"]


def test_merged_iframe_node_does_not_collide_with_top_frame(monkeypatch):
    # a top-frame node and an iframe node can both be the small int 5; the merge must make them
    # distinct so the model's per-node element dedup doesn't collapse two different elements.
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    b = f.browsers[0]
    b.evaluate = lambda expr: []
    orig = b.observe
    b.observe = lambda screenshot=True: {**orig(screenshot=screenshot),
        "actions": [{"id": "e5", "node": 5, "kind": "click", "label": "Top item"}]}
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {"actions": [
        {"id": "e5", "node": 5, "kind": "click", "label": "Iframe row"}]})
    page = t.observe()
    nodes = [a["node"] for a in page["actions"]]
    assert len(nodes) == len(set(nodes))  # no collision between top node 5 and iframe node 5
    iframe_a = next(a for a in page["actions"] if a.get("frame"))
    assert iframe_a["node"] != 5 and iframe_a["frame_node"] == 5


def test_act_iframe_resolves_against_frame_node(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    t._frames = {1: {"session": "S1", "origin": "https://content.other"}}
    seen = []
    monkeypatch.setattr(t, "_resolve_in_frame",
                        lambda sid, node: seen.append(node) or {"x": 1, "y": 1})
    monkeypatch.setattr(t, "_iframe_offset", lambda origin: {"x": 0, "y": 0, "w": 1, "h": 1})
    t.act({"id": "f1e5", "node": chrome_mod._FRAME_NODE_BASE + 5, "frame_node": 5,
           "kind": "click", "frame": 1}, {})
    assert seen == [5]  # resolved against the real in-frame node, not the unique model node


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


def test_act_iframe_scroll_routes_mousewheel_into_frame(monkeypatch):
    # a scroll inside the iframe becomes a mouseWheel over the iframe's centre (top-viewport coords)
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    b = f.browsers[0]
    t._frames = {1: {"session": "S1", "origin": "https://content.other"}}
    monkeypatch.setattr(t, "_iframe_offset", lambda origin: {"x": 100, "y": 50, "w": 800, "h": 600})
    t.act({"id": "f1scroll_down", "kind": "scroll", "delta": 560, "frame": 1}, {})
    wheels = [p for m, p in b.calls if m == "Input.dispatchMouseEvent"]
    assert len(wheels) == 1
    assert wheels[0] == {"type": "mouseWheel", "x": 500, "y": 350, "deltaX": 0, "deltaY": 560}
    assert b.acts == []  # never uses the upstream act path


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


def test_iframe_icon_button_gets_row_disambiguated_label(monkeypatch):
    # an icon-only row menu arrives from upstream as label == role ("button"); the merge asks the
    # owning frame for a semantic label and the action becomes targetable by row
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = lambda expr: []
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {"actions": [
        {"id": "e1", "node": 7, "kind": "click", "label": "button", "role": "button", "value": ""},
        {"id": "e2", "node": 8, "kind": "click", "label": "button", "role": "button", "value": ""},
        {"id": "e3", "node": 9, "kind": "click", "label": "Create workflow", "role": "button"}]})
    evals = []
    def frame_eval(sid, expr):
        evals.append((sid, expr))
        return {"7": "menu — Allen Brothers", "8": "menu — Comox Valley"}
    monkeypatch.setattr(t, "_frame_eval", frame_eval)
    page = t.observe()
    by_id = {a["id"]: a for a in page["actions"] if a.get("frame")}
    assert by_id["f1e1"]["label"] == "menu — Allen Brothers"
    assert by_id["f1e2"]["label"] == "menu — Comox Valley"
    assert by_id["f1e1"]["synth_label"] is True and by_id["f1e1"]["raw_label"] == "button"
    assert by_id["f1e3"]["label"] == "Create workflow" and "synth_label" not in by_id["f1e3"]
    # only blank nodes are asked about, in the frame's own session, by their in-frame node id
    label_evals = [e for e in evals if "rowOf" in e[1]]
    assert len(label_evals) == 1 and label_evals[0][0] == "S1"
    assert label_evals[0][1].endswith("([7, 8])")


def test_top_frame_blank_label_enriched_and_labelled_kept():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    orig = b.observe
    b.observe = lambda screenshot=True: {**orig(screenshot=screenshot), "actions": [
        {"id": "e1", "node": 1, "kind": "click", "label": "button", "role": "button"},
        {"id": "e2", "node": 2, "kind": "click", "label": "⋮", "role": "button"},
        {"id": "e3", "node": 3, "kind": "click", "label": "Save", "role": "button"}]}
    def ev(expr):
        if "__jevFast" in expr and "rowOf" in expr:
            return {"1": "delete — Allen Brothers"}  # node 2: nothing found, stays as-is
        return []
    b.evaluate = ev
    acts = {a["id"]: a for a in t.observe()["actions"]}
    assert acts["e1"]["label"] == "delete — Allen Brothers" and acts["e1"]["synth_label"]
    assert acts["e2"]["label"] == "⋮" and "synth_label" not in acts["e2"]
    assert acts["e3"]["label"] == "Save"


def test_label_enrichment_failure_never_breaks_observe():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    orig = b.observe
    b.observe = lambda screenshot=True: {**orig(screenshot=screenshot), "actions": [
        {"id": "e1", "node": 1, "kind": "click", "label": "button", "role": "button"}]}
    def ev(expr):
        if "rowOf" in expr:
            raise RuntimeError("frame gone")
        return []
    b.evaluate = ev
    assert t.observe()["actions"][0]["label"] == "button"


def test_synthesized_label_never_turns_row_text_into_money_or_send():
    # a synthesized "<icon> — <row>" label never starts with row text, so a row named "Pay" or
    # "Send" can't turn an icon button into a money/send action
    from jev_runner.config import Config
    from jev_runner.guards import is_money_action, is_send_action
    a = {"kind": "click", "label": "menu — Send", "node": 1}
    assert not is_send_action(a, Config())
    assert not is_money_action({**a, "label": "menu — Pay now"}, {}, {}, Config())


def test_overlay_action_records_guard_so_upstream_fresh_accepts_it():
    # upstream fresh(): current == [page_key, page["guards"].get(str(node))]. An injected overlay
    # node with no recorded guard compares None against a list and every click is refused stale.
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    g = [2000000, "button", "Not now", None]
    b.evaluate = lambda expr: ([{"node": 2000000, "label": "Not now", "guard": g}]
                               if "aria-modal" in expr else [])
    page = t.observe()
    assert page["guards"]["2000000"] == g
    ov = next(a for a in page["actions"] if a["id"] == "ov1")
    current = [page.get("page_key"), g]  # what upstream reads back for an unchanged element
    assert current == [page.get("page_key"), page["guards"].get(str(ov["node"]))]


def test_div_icon_controls_become_labelled_guarded_actions():
    # a <div onclick><svg/></div> row menu isn't in upstream's selector at all: discovery
    # registers it (high id + guard), labelling names it, an unnameable one is dropped
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    g = [3000000, None, "", None]
    def ev(expr):
        if "svg,i,img" in expr:  # icon discovery
            return [{"node": 3000000, "guard": g}, {"node": 3000001, "guard": g}]
        if "rowOf" in expr:
            return {"3000000": "dots vertical — Allen Brothers"}
        return []
    f.browsers[0].evaluate = ev
    page = t.observe()
    icons = [a for a in page["actions"] if a["id"].startswith("ic")]
    assert [(a["id"], a["label"]) for a in icons] == [("ic1", "dots vertical — Allen Brothers")]
    assert page["guards"]["3000000"] == g
    assert all("_icon" not in a and "_guard" not in a for a in page["actions"])


def test_iframe_div_icon_controls_merged(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = lambda expr: []
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {"actions": []})
    def frame_eval(sid, expr):
        if "svg,i,img" in expr:
            return [{"node": 3000000, "guard": None}]
        return {"3000000": "dots vertical — Comox Valley"}
    monkeypatch.setattr(t, "_frame_eval", frame_eval)
    acts = [a for a in t.observe()["actions"] if a.get("frame")]
    assert [(a["id"], a["label"], a["frame_node"]) for a in acts] == [
        ("f1ic1", "dots vertical — Comox Valley", 3000000)]


def test_iframe_overlay_menu_items_merged_and_deduped(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = lambda expr: []
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {"actions": [
        {"id": "e1", "node": 4, "kind": "click", "label": "Edit", "role": "menuitem"}]})
    def frame_eval(sid, expr):
        if "aria-modal" in expr:  # overlay pass, run inside the frame
            return [{"node": 2000000, "label": "Edit"}, {"node": 2000001, "label": "Delete"}]
        return []
    monkeypatch.setattr(t, "_frame_eval", frame_eval)
    acts = {a["id"]: a for a in t.observe()["actions"] if a.get("frame")}
    assert set(acts) == {"f1e1", "f1ov2"}  # "Edit" already offered by upstream: not duplicated
    assert acts["f1ov2"]["label"] == "Delete" and acts["f1ov2"]["frame_node"] == 2000001


def test_pointer_text_rows_become_guarded_click_actions():
    # a folder row is a <div style="cursor:pointer">Old Leads</div>: not in upstream's selector,
    # so discovery offers it as a named click (guard recorded, labels already offered skipped)
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    g = [4000000, None, "Old Leads", None]
    def ev(expr):
        if "createTreeWalker" in expr and "4000000" in expr:
            return [{"node": 4000000, "label": "Old Leads", "guard": g},
                    {"node": 4000001, "label": "hi", "guard": None}]
        return []
    f.browsers[0].evaluate = ev
    f.browsers[0].observe = lambda screenshot=False: {"url": "u", "text": "", "actions": [
        {"id": "e1", "node": 1, "kind": "click", "label": "hi", "role": "link"}]}
    page = t.observe()
    tx = [a for a in page["actions"] if a["id"].startswith("tx")]
    assert [(a["id"], a["label"], a["kind"]) for a in tx] == [("tx1", "Old Leads", "click")]
    assert page["guards"]["4000000"] == g
    assert all("_guard" not in a for a in page["actions"])


def test_iframe_pointer_text_rows_merged(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = lambda expr: []
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {"actions": [
        {"id": "e1", "node": 4, "kind": "click", "label": "Home", "role": "link"}]})
    def frame_eval(sid, expr):
        if "createTreeWalker" in expr and "4000000" in expr:
            return [{"node": 4000000, "label": "Home"}, {"node": 4000001, "label": "Status Tiggers"}]
        return []
    monkeypatch.setattr(t, "_frame_eval", frame_eval)
    acts = {a["id"]: a for a in t.observe()["actions"] if a.get("frame")}
    assert set(acts) == {"f1e1", "f1tx2"}
    assert acts["f1tx2"]["label"] == "Status Tiggers" and acts["f1tx2"]["frame_node"] == 4000001


def test_pointer_text_discovery_failure_never_breaks_observe():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    def ev(expr):
        if "createTreeWalker" in expr and "4000000" in expr:
            raise RuntimeError("boom")
        return []
    f.browsers[0].evaluate = ev
    assert t.observe()["readable"] is True


def test_file_inputs_offered_only_when_run_has_files():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    b.evaluate = lambda expr: ([{"node": 5000000, "label": "Attach", "accept": ".pdf", "multiple": False}]
                               if 'input[type="file"]' in expr and "5000000" in expr else [])
    assert not [a for a in t.observe()["actions"] if a.get("file_input")]
    t.set_files(["/u/Downloads/invoice.pdf"])
    assert ("Page.setInterceptFileChooserDialog", {"enabled": True}) in b.calls
    up = [a for a in t.observe()["actions"] if a.get("file_input")]
    assert [(a["id"], a["label"], a["kind"]) for a in up] == [
        ("up1", "Upload file invoice.pdf → Attach", "click")]
    assert "/u/Downloads" not in up[0]["label"]  # names only, never the local path


def test_upload_sets_files_via_cdp_without_clicking():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    t.set_files(["/u/a.pdf", "/u/b.pdf"])
    def call(method, **params):
        b.calls.append((method, params))
        if method == "Runtime.evaluate":
            return {"result": {"type": "object", "objectId": "OBJ1"}}
        return {}
    b.call = call
    action = {"id": "up1", "node": 5000000, "kind": "click", "label": "Upload file", "file_input": True}
    t.act(action, {})
    t.act(action, {})  # single-file input: the next run file
    sets = [p for m, p in b.calls if m == "DOM.setFileInputFiles"]
    assert sets == [{"files": ["/u/a.pdf"], "objectId": "OBJ1"}, {"files": ["/u/b.pdf"], "objectId": "OBJ1"}]
    assert not b.acts and not any(m == "Input.dispatchMouseEvent" for m, _ in b.calls)


def test_upload_gone_input_is_retryable_stale():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    t.set_files(["/u/a.pdf"])
    f.browsers[0].call = lambda method, **p: {"result": {"type": "object", "subtype": "null"}}
    with pytest.raises(StalePage):
        t.act({"id": "up1", "node": 5000000, "kind": "click", "file_input": True}, {})


# --- reusing an idle tab for a new run on the same URL ---

def test_same_page_ignores_trailing_slash_case_and_fragment():
    assert chrome_mod._same_page("https://X.test/a/", "https://x.test/a")
    assert chrome_mod._same_page("https://x.test/a?f=1#x", "https://x.test/a?f=1")
    assert not chrome_mod._same_page("https://x.test/a?f=1", "https://x.test/a?f=2")
    assert not chrome_mod._same_page("https://x.test/a", "https://x.test/b")
    assert not chrome_mod._same_page("", "https://x.test/b")


def test_ensure_at_keeps_the_page_or_navigates():
    t, b = make()
    b.evaluate = lambda expr: "https://x.test/a?b=1#frag" if expr == "location.href" else True
    assert t.ensure_at("https://x.test/a/?b=1") is True
    assert not any(m == "Page.navigate" for m, _ in b.calls)  # still on the page: no reload
    b.evaluate = lambda expr: "https://x.test/elsewhere" if expr == "location.href" else True
    assert t.ensure_at("https://x.test/a?b=1") is True
    assert ("Page.navigate", {"url": "https://x.test/a?b=1"}) in b.calls


def test_ensure_at_false_when_the_tab_is_gone():
    t, b = make()

    def gone(expr):
        raise RuntimeError("No target with given id found")
    b.evaluate = gone
    assert t.ensure_at("https://x.test") is False


# --- freshness: only the action's own target must be unchanged ------------------------------

from jev_runner.targets.chrome import fresh_check  # noqa: E402


def _page():
    return {"marker": ["m"], "page_key": ["k"], "guards": {"7": ["btn", "button", "Save"]}}


def test_wait_and_scroll_are_never_stale_even_on_a_churning_page():
    calls = []
    evaluate = lambda expr: calls.append(expr) or None  # noqa: E731
    assert fresh_check(evaluate, _page(), {"kind": "wait"}) is True
    assert fresh_check(evaluate, _page(), {"kind": "scroll", "delta": 560}) is True
    assert calls == []  # no round trip at all


def test_node_actions_compare_page_key_and_the_node_guard_only():
    seen = []

    def evaluate(expr):
        seen.append(expr)
        return [["k"], ["btn", "button", "Save"]]
    for kind in ("click", "select", "fill", "type"):
        assert fresh_check(evaluate, _page(), {"kind": kind, "node": 7}) is True
    assert all("c.nodes.get(7)" in e for e in seen)
    changed = lambda expr: [["k"], ["btn", "button", "Saved"]]  # noqa: E731
    assert fresh_check(changed, _page(), {"kind": "click", "node": 7}) is False
    assert fresh_check(evaluate, _page(), {"kind": "click", "node": 9}) is False  # not in guards
    assert fresh_check(evaluate, _page(), {"kind": "click", "node": "e7"}) is False  # not a node id
    gone = lambda expr: None  # noqa: E731  # __jevFast not installed: page replaced
    assert fresh_check(gone, _page(), {"kind": "click", "node": 7}) is False


def test_no_action_compares_the_whole_page_marker():
    assert fresh_check(lambda expr: ["m"], _page(), None) is True
    assert fresh_check(lambda expr: ["other"], _page(), None) is False


# --- shadow DOM --------------------------------------------------------------------------------

from jev_runner.targets.chrome import shadow_snapshot_js, _install_shadow_snapshot  # noqa: E402


def test_shadow_snapshot_patches_upstream_or_keeps_it():
    from jev_ultrafast import browser as ub
    src = ub.READ_STATE if not getattr(ub, "_jev_runner_shadow", False) else None
    if src is not None:
        patched = shadow_snapshot_js(src)
        assert patched and "deepAll(document,selector)" in patched and "deepText(document.body" in patched
        assert "shadow:true" in patched and "document.querySelectorAll(selector)" not in patched
    assert shadow_snapshot_js("(() => { return 1; })()") is None  # unknown upstream: untouched


def test_install_shadow_snapshot_once_and_marker_follows(monkeypatch):
    from jev_ultrafast import browser as ub
    monkeypatch.setattr(ub, "READ_STATE", ub.READ_STATE if not getattr(ub, "_jev_runner_shadow", False)
                        else shadow_snapshot_js.__globals__["_SHADOW_ANCHORS"] and ub.READ_STATE)
    monkeypatch.setattr(ub, "_jev_runner_shadow", None, raising=False)
    monkeypatch.setattr(ub, "MARKER", "old")
    ok = _install_shadow_snapshot()
    assert ok is True and ub.MARKER != "old" and "deepText" in ub.MARKER
    assert _install_shadow_snapshot() is True  # idempotent


def _shadow_target():
    f = FakeBrowserFactory(["complete"])
    t = ChromeTarget("https://x.test/a", browser_factory=f)
    return t, t.browser


def test_shadow_action_is_resolved_with_the_composed_hit_test_and_dispatched():
    t, b = _shadow_target()
    seen = []
    b.evaluate = lambda expr: seen.append(expr) or {"x": 10.0, "y": 20.0}
    page = {"marker": 1, "page_key": ["k"], "guards": {"5": ["g"]}}
    t.act({"id": "e1", "kind": "click", "node": 5, "shadow": True, "label": "Today"}, page)
    assert b.acts == []  # upstream act (document.elementFromPoint) is bypassed
    assert "shadowRoot.elementFromPoint" in seen[-1] and '"node": 5' in seen[-1]
    presses = [(m, p["x"], p["y"]) for m, p in b.calls if m == "Input.dispatchMouseEvent"]
    assert presses == [("Input.dispatchMouseEvent", 10.0, 20.0)] * 2


def test_shadow_fill_types_and_select_sets_value_in_page():
    t, b = _shadow_target()
    b.evaluate = lambda expr: {"x": 1.0, "y": 2.0}
    t.act({"id": "e1", "kind": "fill", "node": 5, "shadow": True, "label": "Search"}, {}, text="abc")
    assert ("Input.insertText", {"text": "abc"}) in b.calls
    b.calls.clear()
    b.evaluate = lambda expr: {"x": 1.0, "y": 2.0, "selected": True}
    t.act({"id": "e2", "kind": "select", "node": 6, "shadow": True, "value": "v"}, {})
    assert not any(m.startswith("Input.") for m, _ in b.calls)  # select is applied in the page


def test_shadow_action_refused_before_input_is_a_stale_page():
    t, b = _shadow_target()
    b.evaluate = lambda expr: None  # moved, covered or gone
    with pytest.raises(StalePage):
        t.act({"id": "e1", "kind": "click", "node": 5, "shadow": True}, {})
    assert not any(m.startswith("Input.") for m, _ in b.calls)


# --- covered controls are dropped; loading is flagged -------------------------------------------

def _observe_with(post_result, actions):
    f = FakeBrowserFactory(["complete"])
    t = ChromeTarget("https://x.test/a", browser_factory=f)
    b = t.browser
    b.observe = lambda screenshot=True: {"url": "https://x.test/a", "title": "T", "text": "hi",
                                         "actions": [dict(a) for a in actions]}
    orig = b.evaluate
    b.evaluate = lambda expr: post_result if "loadingSel" in expr else orig(expr)
    return t.observe()


def test_covered_controls_are_dropped_from_the_actions():
    acts = [{"id": "e1", "node": 1, "kind": "fill", "label": "Search by title", "role": "textbox"},
            {"id": "e2", "node": 2, "kind": "click", "label": "Close", "role": "button"},
            {"id": "wait", "kind": "wait", "label": "Wait"}]
    page = _observe_with({"covered": [1], "loading": False}, acts)
    assert [a["id"] for a in page["actions"]] == ["e2", "wait"] and page["covered"] == 1
    assert page["loading"] is False


def test_loading_flag_and_tolerance_of_a_failed_probe():
    acts = [{"id": "e1", "node": 1, "kind": "click", "label": "X", "role": "button"}]
    assert _observe_with({"covered": [], "loading": True}, acts)["loading"] is True
    page = _observe_with("not a dict", acts)  # probe failed: nothing dropped, no flag
    assert [a["id"] for a in page["actions"]] == ["e1"] and "loading" not in page


# --- a click that opens a new tab is followed ------------------------------------------------

def _tab_target(monkeypatch, targets_after):
    f = FakeBrowserFactory(["complete"])
    t = ChromeTarget("https://x.test/a", browser_factory=f)
    b = t.browser
    b.target, b.session = "T1", "S1"
    calls = []
    state = {"phase": 0}

    def fake_cdp(method, **params):
        calls.append((method, params))
        if method == "Target.getTargets":
            infos = [{"targetId": "T1", "type": "page", "url": "https://x.test/a"}]
            if state["phase"]:
                infos += targets_after
            return {"targetInfos": infos}
        if method == "Target.attachToTarget":
            return {"sessionId": "S2"}
        return {}
    monkeypatch.setattr("jev_runner.targets.chrome._cdp", fake_cdp)
    monkeypatch.setattr("jev_runner.targets.chrome._sleep", lambda s: None)
    orig_act = b.act
    def act(action, page, text=None):
        state["phase"] = 1  # the click opened something
        return orig_act(action, page, text)
    b.act = act
    return t, b, calls


def test_click_that_opens_a_tab_moves_the_session_there_and_closes_the_old_tab_later(monkeypatch):
    t, b, calls = _tab_target(monkeypatch, [{"targetId": "T2", "type": "page", "openerId": "T1",
                                             "url": "https://x.test/audit"}])
    t.act({"id": "e1", "kind": "click", "node": 3, "label": "Audit logs"}, {"marker": 1})
    assert (b.target, b.session) == ("T2", "S2")
    assert ("Target.attachToTarget", {"targetId": "T2", "flatten": True}) in calls
    assert any(m == "Emulation.setDeviceMetricsOverride" for m, _ in b.calls)
    t.close()
    assert ("Target.closeTarget", {"targetId": "T1"}) in calls  # the tab it came from


def test_tabs_not_opened_by_this_tab_are_ignored(monkeypatch):
    t, b, calls = _tab_target(monkeypatch, [{"targetId": "T9", "type": "page", "openerId": "OTHER",
                                             "url": "https://x.test/else"}])
    t.act({"id": "e1", "kind": "click", "node": 3, "label": "X"}, {"marker": 1})
    assert (b.target, b.session) == ("T1", "S1")
    assert not any(m == "Target.attachToTarget" for m, _ in calls)


def test_fill_and_scroll_do_not_look_for_new_tabs(monkeypatch):
    t, b, calls = _tab_target(monkeypatch, [{"targetId": "T2", "type": "page", "openerId": "T1"}])
    t.act({"id": "e1", "kind": "fill", "node": 3, "label": "Search"}, {"marker": 1}, text="a")
    assert not any(m == "Target.getTargets" for m, _ in calls)


# --- freshness is about the target and its document, not the page around it ------------------

def test_node_freshness_ignores_scroll_and_other_fields_but_not_the_document():
    page = {"page_key": ["t0", "https://x/a", 0, 0, 1120, 780, [["q", "", False]]],
            "guards": {"7": ["lnk", "link", "Open in new tab"]}}
    guard = ["lnk", "link", "Open in new tab"]
    # the list scrolled itself and another field changed: the row link is still the same link
    moved = lambda e: [["t0", "https://x/a", 0, 560, 1120, 780, [["q", "abc", False]]], guard]  # noqa: E731
    assert fresh_check(moved, page, {"kind": "click", "node": 7}) is True
    reloaded = lambda e: [["t1", "https://x/a", 0, 0, 1120, 780, [["q", "", False]]], guard]  # noqa: E731
    assert fresh_check(reloaded, page, {"kind": "click", "node": 7}) is False
    elsewhere = lambda e: [["t0", "https://x/b", 0, 0, 1120, 780, [["q", "", False]]], guard]  # noqa: E731
    assert fresh_check(elsewhere, page, {"kind": "click", "node": 7}) is False
    assert fresh_check(lambda e: "garbage", page, {"kind": "click", "node": 7}) is False


# --- panels that scroll inside the page --------------------------------------------------------

def _scroller_target(found):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    def ev(expr):
        if "scrollHeight > e.clientHeight + 20" in expr:
            return found
        if expr.endswith("(6000001)"):  # the resolver applied to that node
            return {"x": 300, "y": 420}
        if expr.endswith("(6000000)"):
            return None  # gone or covered
        return []
    b.evaluate = ev
    b.observe = lambda screenshot=False: {"url": "u", "text": "", "actions": []}
    return t, b


def test_panels_that_scroll_inside_the_page_are_offered_and_wheeled_over():
    t, b = _scroller_target([{"node": 6000000, "down": True, "up": False},
                             {"node": 6000001, "down": True, "up": True}])
    page = t.observe()
    sc = [(a["id"], a["label"], a["delta"]) for a in page["actions"] if a.get("scroller")]
    assert sc == [("sc1d", "Scroll the list down", 560), ("sc2d", "Scroll the list 2 down", 560),
                  ("sc2u", "Scroll the list 2 up", -560)]
    t.act(next(a for a in page["actions"] if a["id"] == "sc2u"), page)
    assert b.acts == []  # not upstream's wheel over the window
    assert ("Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": 300, "y": 420, "deltaX": 0, "deltaY": -560}) in b.calls


def test_a_panel_gone_or_covered_is_a_retryable_stale_page():
    t, b = _scroller_target([{"node": 6000000, "down": True, "up": False}])
    page = t.observe()
    with pytest.raises(StalePage):
        t.act(next(a for a in page["actions"] if a["id"] == "sc1d"), page)
    assert not any(m == "Input.dispatchMouseEvent" for m, _ in b.calls)


def test_scroller_discovery_failure_never_breaks_observe():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    def ev(expr):
        if "scrollHeight > e.clientHeight + 20" in expr:
            raise RuntimeError("boom")
        return []
    f.browsers[0].evaluate = ev
    assert [a["id"] for a in t.observe()["actions"]] == []


# --- an iframe's text reaches Jev, and its panels scroll in place --------------------------------

def test_iframe_text_is_merged_for_the_model_and_its_panels_wheeled_in_the_frame(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    b = f.browsers[0]
    b.evaluate = lambda expr: []
    b.observe = lambda screenshot=False: {"url": "u", "text": "Nav only", "actions": [],
                                          "fingerprint": "fp0"}
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {
        "actions": [{"id": "e1", "node": 4, "kind": "click", "label": "View Configuration",
                     "role": "button"}], "text": "Email Services\nView Configuration"})
    def frame_eval(sid, expr):
        if "scrollHeight > e.clientHeight + 20" in expr:
            return [{"node": 6000000, "down": True, "up": False}]
        return []
    monkeypatch.setattr(t, "_frame_eval", frame_eval)
    page = t.observe()
    assert page["text"] == "Nav only\nEmail Services\nView Configuration"
    sc = next(a for a in page["actions"] if a.get("frame") and a.get("scroller"))
    assert sc["id"] == "f1sc1d" and sc["frame_node"] == 6000000 and sc["kind"] == "scroll"
    fp_with_text = page["fingerprint"]
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {"actions": [
        {"id": "e1", "node": 4, "kind": "click", "label": "View Configuration", "role": "button"}],
        "text": "Email Services\nView Configuration\nnew row"})
    assert t.observe()["fingerprint"] != fp_with_text  # a change confined to the frame is progress
    monkeypatch.setattr(t, "_iframe_offset", lambda origin: {"x": 224, "y": 0, "w": 896, "h": 780})
    monkeypatch.setattr(t, "_resolve_in_frame", lambda sid, node: {"x": 100, "y": 200} if node == 6000000 else None)
    t.act(sc, page)
    assert ("Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": 324, "y": 200, "deltaX": 0, "deltaY": 560}) in b.calls


# --- same-named controls are told apart by their row ------------------------------------------

def test_same_named_controls_are_labelled_with_their_row():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    def ev(expr):
        if expr.startswith(chrome_mod._ROW_CONTEXT_FN):
            assert expr.endswith("([1, 2])")  # only the duplicates are looked up
            return {"1": "first.example.net", "2": "second.example.net"}
        return []
    b.evaluate = ev
    b.observe = lambda screenshot=False: {"url": "u", "text": "", "actions": [
        {"id": "e1", "node": 1, "kind": "click", "label": "Domain Options", "role": "button"},
        {"id": "e2", "node": 2, "kind": "click", "label": "Domain Options", "role": "button"},
        {"id": "e3", "node": 3, "kind": "click", "label": "Add Domain", "role": "button"}]}
    acts = {a["id"]: a for a in t.observe()["actions"]}
    assert acts["e1"]["label"] == "Domain Options — first.example.net"
    assert acts["e2"]["label"] == "Domain Options — second.example.net"
    assert acts["e1"]["raw_label"] == "Domain Options" and acts["e1"]["row_label"] is True
    assert acts["e3"]["label"] == "Add Domain" and "row_label" not in acts["e3"]
    # the guards keep matching: the control's own words still come first
    from jev_runner.guards import _label_matches, normalize
    assert _label_matches(normalize("Send — Ana López"), ["Send"]) is True


def test_same_named_controls_inside_an_iframe_are_labelled_too(monkeypatch):
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://app.example/x", browser_factory=f)
    f.browsers[0].evaluate = lambda expr: []
    monkeypatch.setattr(t, "_iframe_targets", lambda: [("S1", "https://content.other/app")])
    monkeypatch.setattr(t, "_frame_snapshot", lambda sid: {"actions": [
        {"id": "e1", "node": 4, "kind": "click", "label": "Domain Options", "role": "button"},
        {"id": "e2", "node": 5, "kind": "click", "label": "Domain Options", "role": "button"}]})
    def frame_eval(sid, expr):
        if expr.startswith(chrome_mod._ROW_CONTEXT_FN):
            return {"4": "first.example.net", "5": "second.example.net"}
        return []
    monkeypatch.setattr(t, "_frame_eval", frame_eval)
    acts = {a["id"]: a for a in t.observe()["actions"] if a.get("frame")}
    assert acts["f1e1"]["label"] == "Domain Options — first.example.net" and acts["f1e1"]["row_label"]
    assert acts["f1e2"]["label"] == "Domain Options — second.example.net"


def test_row_lookup_failure_leaves_labels_alone():
    f = FakeBrowserFactory(loaded=[True])
    t = ChromeTarget("https://x.test", browser_factory=f)
    b = f.browsers[0]
    def ev(expr):
        if expr.startswith(chrome_mod._ROW_CONTEXT_FN):
            raise RuntimeError("boom")
        return []
    b.evaluate = ev
    b.observe = lambda screenshot=False: {"url": "u", "text": "", "actions": [
        {"id": "e1", "node": 1, "kind": "click", "label": "Open", "role": "link"},
        {"id": "e2", "node": 2, "kind": "click", "label": "Open", "role": "link"}]}
    assert [a["label"] for a in t.observe()["actions"]] == ["Open", "Open"]
