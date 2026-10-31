import json

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
