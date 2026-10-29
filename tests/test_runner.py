import json

import pytest

from jev_mcp import runner as runner_mod
from jev_mcp.runner import Runner
from jev_mcp.targets.base import DeviceDisconnected
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
    assert h["where"] == "app://main" and h["value_source"] is None
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
    from jev_mcp.targets.base import StalePage
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
