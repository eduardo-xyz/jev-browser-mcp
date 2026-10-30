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
