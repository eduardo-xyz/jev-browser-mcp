import json
import time

import pytest

from jev_runner import runner as runner_mod
from jev_runner.runner import Runner
from jev_runner.targets.base import DeviceDisconnected
from tests.fakes import FakeTarget, click, fill, page, scripted_choose, unreadable


def test_runs_to_done_and_returns_compact_result(cfg):
    t = FakeTarget([page(fill("n1", "Monto")), page(click("n2", "Siguiente")), page()])
    r = Runner("s1", t, "pay", {"amount": 5}, cfg, choose=scripted_choose(["n1", "n2", "DONE"]),
               field_text=lambda ctx: ("5", {"model": "m", "latency_ms": 1, "usage": {}}))
    out = r.run()
    assert out["status"] == "done" and out["model_calls"] == 3
    assert out["last_actions"] == ["fill · Monto = 5", "click · Siguiente"]
    assert len(json.dumps(out)) < 2048 and "screenshot" not in json.dumps(out)
    h = r.history[0]
    assert h["where"] == "app://main" and h["value_source"] == "param:amount"
    assert h["locator"]["label"] == "Monto" and h["signature"]["labels"] == ["Monto"]


def test_goal_includes_param_values(cfg):
    seen = []

    def choose(page, goal, history):
        seen.append(goal)
        return scripted_choose(["DONE"])(page, goal, history)

    Runner("s", FakeTarget([page()]), "pay rent", {"amount": 5, "recipient": ["A", "B"]}, cfg,
           choose=choose).run()
    assert seen[0].endswith("Values: amount=5, recipient=A / B")


def test_send_executes_once_and_second_send_is_blocked(cfg):
    # A forward returns to a similar-looking screen; a second send in the same run must be
    # refused, not fired again (this is what triple-sent a WhatsApp forward).
    t = FakeTarget([page(click("s1", "Enviar")), page(click("s2", "Enviar")), page()])
    out = Runner("s", t, "forward it", None, cfg,
                 choose=scripted_choose(["s1", "s2"])).run()
    assert (out["status"], out["reason"]) == ("blocked", "already_sent")
    assert t.acted == [("s1", None)]  # only the first send actually ran
    assert t.verified == ["s1"]       # a send is hit-tested like a money tap


def test_single_send_then_done(cfg):
    t = FakeTarget([page(click("s1", "Send")), page()])
    out = Runner("s", t, "send it", None, cfg,
                 choose=scripted_choose(["s1", "DONE"])).run()
    assert out["status"] == "done" and t.acted == [("s1", None)]


def _osc_page(fp, cid, label):
    return {"url": "u", "title": "t", "text": "", "readable": True, "fingerprint": fp,
            "actions": [{"id": cid, "node": 1, "kind": "click", "label": label, "role": "button"}]}


def test_retrieved_lessons_are_injected_into_the_goal(cfg):
    class FakeMemory:
        def __init__(self): self.seen = {}
        def query_lessons(self, **kw):
            self.seen = kw
            return [{"body": "avoid the Quiero sheet"}, {"body": "hide keyboard before Continuar"}]

    mem = FakeMemory()
    r = Runner("s", FakeTarget([page()]), "pay the planilla", {"amount": 5}, cfg,
               choose=scripted_choose(["DONE"]), start="net.bac.sbe.android", memory=mem)
    assert "avoid the Quiero sheet" in r.goal and "hide keyboard before Continuar" in r.goal
    assert r.goal.startswith("pay the planilla")  # original goal + values preserved
    assert r._goal_raw == "pay the planilla"  # raw goal (for flow naming) is NOT polluted
    assert mem.seen["app"] == "net.bac.sbe.android" and mem.seen["target"] == "android"


def test_no_memory_leaves_goal_unchanged(cfg):
    r = Runner("s", FakeTarget([page()]), "do a thing", None, cfg, choose=scripted_choose(["DONE"]))
    assert r.goal == "do a thing"  # no memory -> no hints block


def test_oscillation_between_two_screens_is_stopped(cfg):
    # open wrong account -> back -> open again -> ...  Each step changes the screen, so no_progress
    # never fires; the loop detector must stop it well before max_steps.
    pages = [_osc_page("FA", "a1", "Wrong"), _osc_page("FB", "b1", "Back")] * 5

    def choose(page, goal, history):
        aid = next(a["id"] for a in page["actions"] if a["kind"] == "click")
        return {"choice": aid, "operation": aid, "target": None, "confidence": 1.0,
                "probabilities": {aid: 1.0}, "latency_ms": 1, "usage": {}}

    r = Runner("s", FakeTarget(pages), "g", None, cfg, max_steps=40, choose=choose)
    out = r.run()
    assert (out["status"], out["reason"]) == ("blocked", "stuck_in_loop")
    assert len(r.history) < 10  # stopped at the cycle, not churned to max_steps


def test_auto_recovers_by_dropping_looped_action(cfg):
    # screen A offers Wrong + Right; Jev keeps choosing Wrong (A->B->A). Once Wrong has looped it is
    # dropped from the view, so Jev is forced to pick Right, which finishes the goal.
    def A():
        return {"url": "u", "title": "t", "text": "", "readable": True, "fingerprint": "FA",
                "actions": [{"id": "a1", "node": 1, "kind": "click", "label": "Wrong", "role": "button"},
                            {"id": "a2", "node": 2, "kind": "click", "label": "Right", "role": "button"}]}
    B = lambda: _osc_page("FB", "b1", "Back")
    D = lambda: {"url": "u", "title": "t", "text": "done", "readable": True, "fingerprint": "FD", "actions": []}
    pages = [A(), B(), A(), B(), A(), D()]

    def choose(page, goal, history):
        if page["fingerprint"] == "FD":
            c = "DONE"
        else:
            ids = {a["id"] for a in page["actions"]}
            c = next(p for p in ("a1", "a2", "b1", "wait") if p in ids or p == "wait")
        return {"choice": c, "operation": c, "target": None, "confidence": 1.0,
                "probabilities": {c: 1.0}, "latency_ms": 1, "usage": {}}

    r = Runner("s", FakeTarget(pages), "g", None, cfg, max_steps=40, choose=choose)
    out = r.run()
    assert out["status"] == "done"
    assert any(h["action"] == "Right" for h in r.history)  # recovered onto the other action


def test_looping_counts_same_action_on_same_fingerprint(cfg):
    r = Runner("s", FakeTarget([page()]), "g", None, cfg, choose=scripted_choose([]))
    act = {"id": "e1", "node": 1, "kind": "click", "label": "X", "role": "button"}
    pg = {"fingerprint": "F1", "actions": [act]}
    assert not r._looping(pg, act)  # no history yet
    r.history = [{"fingerprint": "F1", "locator": runner_mod.locator_of(act), "page_changed": True}
                 for _ in range(3)]
    assert r._looping(pg, act)  # three prior identical taps on the same screen
    assert not r._looping({"fingerprint": "OTHER", "actions": [act]}, act)  # different screen
    assert not r._looping(pg, {**act, "kind": "scroll"})  # scroll is paced elsewhere
    # a frozen screen (page_changed False) is no_progress's job, not the loop detector's
    r.history = [{"fingerprint": "F1", "locator": runner_mod.locator_of(act), "page_changed": False}
                 for _ in range(3)]
    assert not r._looping(pg, act)


def test_no_progress_blocks(cfg):
    t = FakeTarget([page(click("n1", "X"))], stuck_ids={"n1"})
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1"] * 3)).run()
    assert (out["status"], out["reason"]) == ("blocked", "no_progress")


def test_max_steps(cfg):
    t = FakeTarget([page(click("n1", "Next"))] * 10)
    out = Runner("s", t, "g", None, cfg, max_steps=2, choose=scripted_choose(["n1"] * 5)).run()
    assert out["status"] == "max_steps"


def test_loading_screen_waits_before_unreadable(cfg, monkeypatch):
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    t = FakeTarget([unreadable(), unreadable(), page(click("n1", "Ingresar")), page()])
    t.advance_on_observe = 2
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "DONE"])).run()
    assert out["status"] == "done"


def test_screen_unreadable_after_three(cfg, monkeypatch):
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    out = Runner("s", FakeTarget([unreadable()]), "g", None, cfg, choose=scripted_choose([])).run()
    assert (out["status"], out["reason"]) == ("blocked", "screen_unreadable")


def test_missing_value_blocks(cfg):
    def boom(ctx):
        raise ValueError("no key")

    out = Runner("s", FakeTarget([page(fill("n1", "Monto"))]), "g", None, cfg,
                 choose=scripted_choose(["n1"]), field_text=boom).run()
    assert (out["status"], out["reason"]) == ("blocked", "missing_value")


def test_device_disconnect_reports_error_keeps_runner(cfg):
    t = FakeTarget([page(click("n1", "X"))], raise_on_act=DeviceDisconnected())
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1"]))
    out = r.run()
    assert (out["status"], out["reason"]) == ("error", "device_disconnected")


def test_secret_fill_is_masked_everywhere(cfg):
    spy = []
    t = FakeTarget([page(fill("n1", "Código BAC")), page(click("n2", "Ok")), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "n2", "DONE"], spy),
               field_text=lambda ctx: pytest.fail("secret must never reach the text model"))
    assert r.run()["status"] == "paused_needs_input"
    out = r.resume(input="482913")
    assert r.history[0]["text"] == "•••"
    assert "482913" not in json.dumps(out)
    assert all("482913" not in s for s in spy)
    assert t.acted[0] == ("n1", "482913")


def test_elapsed_excludes_idle_gap(cfg, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(runner_mod.time, "perf_counter", lambda: clock[0])
    r = Runner("s", FakeTarget([page()]), "g", None, cfg, choose=scripted_choose(["DONE", "DONE"]))
    r.run()
    clock[0] += 1000
    out = r.run()
    assert out["elapsed_ms"] == 0


def test_unexpected_exception_becomes_error(cfg):
    def choose(page, goal, history):
        raise RuntimeError("Model provider returned HTTP 401; no action executed.")

    out = Runner("s", FakeTarget([page()]), "g", None, cfg, choose=choose).run()
    assert out["status"] == "error" and "HTTP 401" in out["reason"]


def test_stale_post_act_observe_does_not_retry_act(cfg):
    from jev_runner.targets.base import StalePage
    t = FakeTarget([page(click("n1", "X")), page()])
    calls = [0]
    orig = t.observe

    def observe():
        calls[0] += 1
        if calls[0] == 2:
            raise StalePage()
        return orig()

    t.observe = observe
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "DONE"])).run()
    assert out["status"] == "done" and len(t.acted) == 1


def test_done_rejected_by_check_is_unverified_and_not_learned(cfg, tmp_path):
    from jev_runner.flows import FlowStore
    store = FlowStore(tmp_path)
    t = FakeTarget([page(click("n0", "Go")), page()])
    out = Runner("s", t, "g", None, cfg, store=store, choose=scripted_choose(["n0", "DONE"]),
                 verify=lambda g, p, h: (False, "message typed but not sent")).run()
    assert out["status"] == "unverified" and out["reason"] == "message typed but not sent"
    assert "flow_saved" not in out and not list(tmp_path.glob("*.json"))


def test_blocked_but_goal_visibly_met_is_done(cfg):
    t = FakeTarget([page(click("n0", "Send")), page()])
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n0", "BLOCKED"]),
                 verify=lambda g, p, h: (True, "")).run()
    assert out["status"] == "done" and out["reason"] == "verified_after_blocked"


def test_unavailable_check_keeps_jev_verdict_but_never_learns(cfg, tmp_path):
    from jev_runner.flows import FlowStore
    t = FakeTarget([page(click("n0", "Go")), page()])
    out = Runner("s", t, "g", None, cfg, store=FlowStore(tmp_path),
                 choose=scripted_choose(["n0", "DONE"]), verify=lambda g, p, h: (None, "")).run()
    assert out["status"] == "done" and "flow_saved" not in out


def test_check_sees_scrubbed_screen(cfg):
    seen = []
    t = FakeTarget([page(text="code 482913")])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["DONE"]),
               verify=lambda g, p, h: seen.append(p) or (True, ""))
    r.secrets.append("482913")
    r.run()
    assert seen and "482913" not in json.dumps(seen)


def test_verify_check_without_key_is_unavailable(monkeypatch):
    import importlib
    from jev_runner import verify
    monkeypatch.undo()  # drop the conftest stub
    importlib.reload(verify)
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    assert verify.check("g", {"text": ""}, []) == (None, "")


class _PressTarget(FakeTarget):
    """Taps on `deaf` ids change nothing; a press advances."""
    def __init__(self, pages, deaf=()):
        super().__init__(pages, stuck_ids=deaf)
        self.pressed = []

    def press(self, action):
        self.pressed.append(action["id"])
        self._advance()


def test_tap_that_changes_nothing_is_retried_as_press(cfg):
    t = _PressTarget([page(click("n0", "Ver cuenta")), page()], deaf={"n0"})
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n0", "DONE"]), learn=False)
    out = r.run()
    assert out["status"] == "done" and t.pressed == ["n0"] and r.history[0]["pressed"] is True


def test_no_press_retry_for_send_confirm_or_checkbox(cfg):
    for act in [click("n0", "Enviar"), click("n0", "Confirmar"), {**click("n0", "Recordar"), "checked": "false"}]:
        t = _PressTarget([page(act), page()], deaf={"n0"})
        Runner("s", t, "g", None, cfg, choose=scripted_choose(["n0", "DONE"]), learn=False).run()
        assert t.pressed == [], act["label"]


def test_wait_settles_for_late_mounting_content(cfg, monkeypatch):
    # A SPA mounts a control (e.g. a cross-origin iframe's rows) seconds after the shell loads.
    # Upstream's wait sleeps only 0.1s, so three quick waits give up before it appears. The post-
    # wait settle must spend bounded wall-clock, so the late control is seen and acted on.
    monkeypatch.setattr(runner_mod, "WAIT_SETTLE", 2.0)
    monkeypatch.setattr(runner_mod, "WAIT_POLL", 0.02)
    shell = page()
    shell["actions"].append({"id": "w", "kind": "wait", "label": "Wait for the page to update"})
    ready = page(click("n1", "Stop AI when task is made"))

    class LateTarget:
        kind = "chrome"

        def __init__(self, ready_at):
            self.ready_at = ready_at
            self.acted = []

        def observe(self):
            return ready if time.monotonic() >= self.ready_at else shell

        def fresh(self, page):
            return False

        def act(self, action, page, text=None, verify=False):
            self.acted.append(action["id"])

        def close(self):
            pass

    t = LateTarget(ready_at=time.monotonic() + 0.4)

    def choose(view, goal, history):
        if any(h.get("kind") == "click" for h in history):
            return {"choice": "DONE"}
        kinds = [a["kind"] for a in view["actions"]]
        return {"choice": "n1"} if "click" in kinds else {"choice": "w"}

    r = Runner("s", t, "g", None, cfg, choose=choose, learn=False, verify=lambda g, p, h: (True, ""))
    out = r.run()
    assert out["status"] == "done"
    assert "n1" in t.acted  # acted on the late-mounted control instead of blocking on waits


def test_wait_action_dropped_after_cap_forces_decision(cfg):
    # Jev stalls: it would pick "wait" forever on a churny page. After MAX_WAITS the runner
    # removes the wait action, so the next decision must be a real action (here DONE).
    seen_actions = []
    def choose(view, goal, history):
        seen_actions.append([a["kind"] for a in view["actions"]])
        kinds = [a["kind"] for a in view["actions"]]
        return {"choice": "DONE"} if "wait" not in kinds else {"choice": "w"}
    pg = page(click("n0", "Open"))
    # give the page a wait action id "w"
    pg["actions"].append({"id": "w", "kind": "wait", "label": "Wait for the page to update"})
    t = FakeTarget([pg] * 10)
    r = Runner("s", t, "g", None, cfg, choose=choose, learn=False, verify=lambda g, p, h: (True, ""))
    out = r.run()
    assert out["status"] == "done"
    # first MAX_WAITS views still offered wait; the one that forced DONE had no wait action
    assert any("wait" not in ks for ks in seen_actions)
    assert sum(1 for h in r.history if h["kind"] == "wait") == 3


def test_no_press_retry_for_destructive(cfg):
    t = _PressTarget([page(click("n0", "Delete")), page()], deaf={"n0"})
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n0", "DONE"]), learn=False)
    assert r.run()["status"] == "paused_confirm"
    r.resume(confirm=True)
    assert t.pressed == []


def test_result_reports_timing_per_phase(cfg):
    t = FakeTarget([page(click("n1", "Siguiente")), page()])
    r = Runner("s1", t, "go", None, cfg, choose=scripted_choose(["n1", "DONE"]))
    out = r.run()
    assert {"observe_ms", "decide_ms", "act_ms"} <= set(out["timing"])
    assert all(isinstance(v, int) and v >= 0 for v in out["timing"].values())


def test_refresh_target_is_read_once_per_step(cfg):
    # A target whose freshness check costs a full screen read (android) offers refresh(): the
    # runner then reads once per tick instead of a check plus a re-read.
    class Refreshing(FakeTarget):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.reads = 0

        def observe(self):
            self.reads += 1
            return super().observe()

        def refresh(self, page):
            return self.observe()

        def fresh(self, page):
            raise AssertionError("fresh() must not be called when refresh() exists")

    t = Refreshing([page(click("n1", "A")), page(click("n2", "B")), page()])
    r = Runner("s1", t, "go", None, cfg, choose=scripted_choose(["n1", "n2", "DONE"]))
    assert r.run()["status"] == "done"
    assert t.reads == 5  # 2 steps x (read before deciding + read after acting) + the DONE tick


def test_wait_on_a_static_screen_stops_early(cfg, monkeypatch):
    # A 'wait' on a screen that never changes used to spend the whole WAIT_SETTLE window.
    monkeypatch.setattr(runner_mod, "WAIT_SETTLE", 5.0)
    monkeypatch.setattr(runner_mod, "WAIT_STATIC", 0.1)
    monkeypatch.setattr(runner_mod, "WAIT_POLL", 0.01)
    still = page(click("n1", "Go"))
    still["actions"].append({"id": "w", "kind": "wait", "label": "Wait for the page to update"})
    t = FakeTarget([still, still, still])
    r = Runner("s1", t, "go", None, cfg, choose=scripted_choose(["w", "DONE"]))
    t0 = time.monotonic()
    assert r.run()["status"] == "done"
    assert time.monotonic() - t0 < 1.0


# --- loop guards on a page whose text churns ---------------------------------------------------

def test_same_click_on_a_churning_screen_is_caught_as_a_loop(cfg):
    """Each read of a contact page differs (live timestamps), so the exact fingerprint never
    repeats; the same tab click five times in a row used to run to max_steps."""
    pages = [page(click("n1", "Audit logs"), click("n2", "Notes"), url="https://a/c/1", text=f"{i} min ago")
             for i in range(8)]
    t = FakeTarget(pages)
    r = Runner("s1", t, "read the audit log", None, cfg, choose=scripted_choose(["n1"] * 8), max_steps=8)
    out = r.run()
    assert out["status"] == "blocked" and out["reason"] == "stuck_in_loop"
    assert len(r.history) <= 4 and r.loop_events >= 1


def test_stale_page_result_says_which_action_was_refused(cfg):
    from jev_runner.targets.base import StalePage
    t = FakeTarget([page(click("n1", "Save")), page()], raise_on_act=StalePage("Target changed or is covered."))
    r = Runner("s1", t, "x", None, cfg, choose=scripted_choose(["n1"] * 4))
    out = r.run()
    assert out["status"] == "error" and out["reason"] == "stale_page"
    assert out["detail"] == {"action": "click · Save", "why": "Target changed or is covered.", "strike": 3}


# --- a page that says it is loading is waited for, not judged ------------------------------------

def test_loading_page_is_waited_for_without_asking_the_model(cfg, monkeypatch):
    monkeypatch.setattr("jev_runner.runner.LOAD_PATIENCE", 20.0)
    monkeypatch.setattr("jev_runner.runner.LOAD_POLL", 0.0)
    shell = page(url="https://a/c", text="Nav only"); shell["loading"] = True
    body = page(click("n1", "Audit logs"), url="https://a/c", text="the contact")
    t = FakeTarget([shell, shell, shell, body], advance_on_observe=3)
    calls = []
    choose = scripted_choose(["DONE"])
    r = Runner("s1", t, "read it", None, cfg,
               choose=lambda page, goal, history: calls.append(page) or choose(page, goal, history))
    out = r.run()
    assert out["status"] == "done" and len(calls) == 1  # Jev never saw the shell
    assert not calls[0].get("loading") and "the contact" in calls[0]["text"]


def test_loading_patience_is_bounded(cfg, monkeypatch):
    monkeypatch.setattr("jev_runner.runner.LOAD_PATIENCE", 20.0)
    monkeypatch.setattr("jev_runner.runner.LOAD_POLL", 10.0)  # two polls exhaust LOAD_PATIENCE
    monkeypatch.setattr("jev_runner.runner.time.sleep", lambda s: None)
    shell = page(click("n1", "X"), url="https://a/c", text="Nav only"); shell["loading"] = True
    r = Runner("s1", FakeTarget([shell]), "read it", None, cfg, choose=scripted_choose(["BLOCKED"]))
    out = r.run()
    assert out["status"] == "blocked" and out["reason"] == "jev_blocked"  # still loading after patience: Jev decides


def test_loading_gap_between_loader_and_skeleton_is_bridged(cfg, monkeypatch):
    monkeypatch.setattr("jev_runner.runner.LOAD_PATIENCE", 20.0)
    monkeypatch.setattr("jev_runner.runner.LOAD_POLL", 0.0)
    class Chrome(FakeTarget):
        kind = "chrome"
    loader = page(url="https://a/c", text="Nav only shell text here"); loader["loading"] = True
    gap = page(click("n1", "Menu"), url="https://a/c", text="Nav only shell text here, nothing more yet")
    skeleton = page(click("n1", "Menu"), url="https://a/c", text="Nav only shell text here"); skeleton["loading"] = True
    body = page(click("n2", "Audit logs"), url="https://a/c", text="the contact, fully rendered now, with all of its text")
    t = Chrome([loader, gap, skeleton, skeleton, body], advance_on_observe=4)
    seen = []
    choose = scripted_choose(["DONE"])
    r = Runner("s1", t, "read it", None, cfg,
               choose=lambda page, goal, history: seen.append(page["text"]) or choose(page, goal, history))
    assert r.run()["status"] == "done" and seen == [body["text"]]


def test_near_empty_web_page_counts_as_loading(cfg, monkeypatch):
    monkeypatch.setattr("jev_runner.runner.LOAD_PATIENCE", 20.0)
    monkeypatch.setattr("jev_runner.runner.LOAD_POLL", 0.0)
    class Chrome(FakeTarget):
        kind = "chrome"
    blank = page(click("n1", "X"), url="https://a/c", text="Loading")
    full = page(click("n1", "X"), url="https://a/c", text="A whole page worth of text is visible here now.")
    seen = []
    choose = scripted_choose(["DONE"])
    r = Runner("s1", Chrome([blank, blank, full], advance_on_observe=2), "read", None, cfg,
               choose=lambda page, goal, history: seen.append(page["text"]) or choose(page, goal, history))
    assert r.run()["status"] == "done" and seen == [full["text"]]


# --- a BLOCKED verdict says what Jev was looking at ---------------------------------------------

def test_blocked_result_says_what_jev_saw(cfg):
    t = FakeTarget([page(click("n1", "Save"), url="https://a/b", text="some words here")])
    def choose(p, goal, history):
        return {"choice": "BLOCKED", "operation": "BLOCKED", "target": None, "confidence": 0.7,
                "probabilities": {"BLOCKED": 0.7}, "latency_ms": 1, "usage": {},
                "operation_probabilities": {"BLOCKED": 0.7, "CLICK": 0.3}}
    out = Runner("s1", t, "x", None, cfg, choose=choose).run()
    assert out["status"] == "blocked" and out["reason"] == "jev_blocked"
    assert out["detail"] == {"url": "https://a/b", "text_chars": 15, "controls": {"click": 1},
                             "weighed": {"BLOCKED": 0.7, "CLICK": 0.3}, "sample": ["Save"]}


def test_blocked_with_nothing_to_click_says_no_controls(cfg):
    t = FakeTarget([page(url="https://a/b", text="x" * 50)])
    out = Runner("s1", t, "x", None, cfg, choose=scripted_choose(["BLOCKED"])).run()
    assert out["detail"]["why"] == "no_controls" and out["detail"]["controls"] == {}
    assert out["detail"]["weighed"] == {}


def test_blocked_on_a_page_that_rendered_nothing_says_blank_page(cfg):
    # an app's unknown route: no text at all, only an injected widget button (seen on a workflow
    # URL with the wrong path segment: a blank shell for the whole loading wait)
    t = FakeTarget([page(click("w1", "Open DAOS widget"), url="https://a/x/wrong/1", text="")])
    out = Runner("s1", t, "x", None, cfg, choose=scripted_choose(["BLOCKED"])).run()
    assert out["detail"]["why"] == "blank_page"
    assert out["detail"]["text_chars"] == 0 and out["detail"]["controls"] == {"click": 1}


def test_blocked_with_no_text_but_real_controls_is_not_blank(cfg):
    actions = [click(f"n{i}", f"Button {i}") for i in range(3)]
    t = FakeTarget([page(*actions, url="https://a/b", text="")])
    out = Runner("s1", t, "x", None, cfg, choose=scripted_choose(["BLOCKED"])).run()
    assert "why" not in out["detail"]


def test_lost_browser_link_is_reported_as_target_unreachable(cfg):
    class Dropped(FakeTarget):
        def observe(self):
            raise RuntimeError("no close frame received or sent")

    out = Runner("s1", Dropped([page()]), "x", None, cfg, choose=scripted_choose([])).run()
    assert (out["status"], out["reason"]) == ("error", "target_unreachable")
    assert out["detail"] == {"error": "no close frame received or sent"}
    assert "remote-debugging port" in out["hint"]


def test_other_target_errors_keep_their_message(cfg):
    class Broken(FakeTarget):
        def observe(self):
            raise RuntimeError("HTTP 529 overloaded")

    out = Runner("s1", Broken([page()]), "x", None, cfg, choose=scripted_choose([])).run()
    assert (out["status"], out["reason"]) == ("error", "HTTP 529 overloaded") and "detail" not in out


# --- a screen with more controls than the model accepts per question -------------------------

def test_oversized_view_is_capped_with_goal_named_controls_first(cfg):
    from jev_runner.runner import cap_for_model
    actions = [click(f"n{i}", f"Step {i}") for i in range(300)] + [click("save", "Save changes")]
    actions.append({"id": "wait", "kind": "wait", "label": "Wait for the page to update"})
    actions.append({"id": "sc0d", "kind": "scroll", "node": 6000001, "label": "Scroll the canvas down"})
    kept = cap_for_model(actions, "click Save changes on the workflow")
    ops = [a for a in kept if a["kind"] == "click"]
    ids = [a["id"] for a in ops]
    assert len(ops) == 255 and "save" in ids                    # the goal's control survives
    assert ids[:3] == ["n0", "n1", "n2"] and "n299" not in ids  # the rest in screen order, tail dropped
    assert [a["id"] for a in kept if a["kind"] != "click"] == ["wait", "sc0d"]


def test_long_labels_are_capped_by_request_size_before_count(cfg):
    from jev_runner.runner import cap_for_model, MODEL_BODY_BUDGET
    actions = [click(f"n{i}", f"Step {i} " + "x" * 400) for i in range(200)]   # 200 < 255, but ~190 KB
    kept = cap_for_model(actions, "open Step 7", text="t" * 3000)
    assert 40 < len(kept) < 200 and any(a["id"] == "n7" for a in kept)
    est = 1500 + 4 * len("open Step 7") + 3000 + sum(2 * len(a["label"]) + 160 for a in kept)
    assert est <= MODEL_BODY_BUDGET


def test_view_under_the_limit_is_untouched(cfg):
    from jev_runner.runner import cap_for_model
    actions = [click(f"n{i}", f"Step {i}") for i in range(255)]
    assert cap_for_model(actions, "anything") is actions


def test_runner_sends_the_model_a_capped_view(cfg):
    seen = []

    def choose(view, goal, history):
        seen.append(len([a for a in view["actions"] if a["kind"] == "click"]))
        return {"choice": "DONE", "operation": "DONE"}

    actions = [click(f"n{i}", f"Step {i}") for i in range(300)]
    Runner("s1", FakeTarget([page(*actions)]), "x", None, cfg, choose=choose).run()
    assert seen == [255]


def test_unverified_result_says_what_jev_called_done(cfg, monkeypatch):
    t = FakeTarget([page(click("n1", "Status: Pending"), url="https://a/b", text="ticket")])
    r = Runner("s1", t, "x", None, cfg, choose=scripted_choose(["DONE"]))
    monkeypatch.setattr(r, "_check", lambda page: False)
    out = r.run()
    assert out["status"] == "unverified"
    assert out["detail"]["sample"] == ["Status: Pending"] and out["detail"]["controls"] == {"click": 1}


# --- a save that landed, then stale strikes on the churn after it ------------------------------

def test_stale_strikes_after_a_landed_action_are_a_verified_done(cfg, monkeypatch):
    from jev_runner.targets.base import StalePage

    class SaveThenChurn(FakeTarget):
        def act(self, action, page, text=None, verify=False):
            if self.acted:  # everything after the first (landed) click is refused as stale
                raise StalePage("Target changed or is covered.")
            super().act(action, page, text, verify)

    t = SaveThenChurn([page(click("n1", "Save")), page(click("n2", "Close"), text="Saved")])
    r = Runner("s1", t, "save the headers", None, cfg, choose=scripted_choose(["n1"] + ["n2"] * 4))
    monkeypatch.setattr(r, "_check", lambda page: True)
    out = r.run()
    assert out["status"] == "done" and out["reason"] == "verified_after_stale"
    assert [a for a, _ in t.acted] == ["n1"] and "detail" not in out


def test_stale_strikes_with_nothing_landed_stay_an_error(cfg, monkeypatch):
    from jev_runner.targets.base import StalePage
    t = FakeTarget([page(click("n1", "Save")), page()], raise_on_act=StalePage("Target changed."))
    r = Runner("s1", t, "x", None, cfg, choose=scripted_choose(["n1"] * 4))
    monkeypatch.setattr(r, "_check", lambda page: pytest.fail("no action landed: nothing to verify"))
    out = r.run()
    assert out["status"] == "error" and out["reason"] == "stale_page"
