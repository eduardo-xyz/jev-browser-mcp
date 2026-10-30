import pytest
import json

from jev_runner.flows import FlowStore, auto_name, goal_key
from jev_runner.runner import Runner
from tests.fakes import FakeTarget, click, fill, page, scripted_choose


@pytest.fixture(autouse=True)
def _verified(monkeypatch):
    # learning needs an independently confirmed DONE
    monkeypatch.setattr("jev_runner.verify.check", lambda goal, page, history: (True, ""))


RECIPIENT = "CR05015202001026284066"
START = "com.example.water"


def text_amt(ctx):
    return "15000", {"model": "m", "latency_ms": 0, "usage": {}}


def pay_pages(amount):
    return [page(fill("n1", "Monto", rid="amt")),
            page(click("n2", "Pagar", rid="pay"), text=f"Destino ****4066 Monto {amount}"),
            page(text="Pago exitoso")]


def run_pay(store, cfg, amount, choices, flow=None, **kw):
    kw.setdefault("start", START)
    return Runner("s", FakeTarget(pay_pages(amount)), f"pay water bill {amount}",
                  {"amount": amount, "recipient": RECIPIENT}, cfg, flow=flow, store=store,
                  choose=scripted_choose(choices),
                  field_text=lambda c: (str(amount), {"model": "m", "latency_ms": 0, "usage": {}}),
                  **kw).run()


def test_first_run_autosaves_and_second_run_replays(tmp_path, cfg):
    store = FlowStore(tmp_path)
    r1 = run_pay(store, cfg, 15000, ["n1", "n2", "DONE"])
    assert r1["status"] == "done" and r1["flow_saved"].startswith("auto-")
    assert "flow_used" not in r1
    flow = store.find("android", START, "pay water bill 16000")
    assert flow is not None and flow.name == r1["flow_saved"] and flow.updated_at > 0
    r2 = run_pay(store, cfg, 16000, ["DONE"], flow=flow)
    assert r2["status"] == "done"
    assert r2["model_calls"] == 1 and r2["flow_used"] == flow.name
    assert "flow_updated" not in r2 and "flow_saved" not in r2


def test_find_filters_target_start_goal(tmp_path, cfg):
    store = FlowStore(tmp_path)
    run_pay(store, cfg, 15000, ["n1", "n2", "DONE"])
    assert store.find("chrome", START, "pay water bill 1") is None
    assert store.find("android", "com.other", "pay water bill 1") is None
    assert store.find("android", START, "pay light bill 1") is None
    assert store.find("android", None, "pay water bill 1") is not None
    assert store.find("android", START + "/.Main", "pay water bill 1") is not None


def heal_pages(mid):
    return [page(click("a1", "A"), url="app://a"), page(click("b1", mid), url="app://b"),
            page(click("c1", "C"), url="app://c"), page(url="app://end")]


def test_fallback_then_success_heals_flow(tmp_path, cfg):
    store = FlowStore(tmp_path)
    out = Runner("a", FakeTarget(heal_pages("B")), "go", None, cfg, store=store, start=START,
                 choose=scripted_choose(["a1", "b1", "c1", "DONE"])).run()
    name = out["flow_saved"]
    flow = store.load(name)
    # The app now shows X instead of B on the same screen.
    t = FakeTarget(heal_pages("X"))
    out2 = Runner("b", t, "go", None, cfg, flow=flow, store=store,
                  choose=scripted_choose(["b1", "DONE"])).run()
    assert out2["status"] == "done" and out2["flow_updated"] == name and out2["flow_used"] == name
    assert [a for a, _ in t.acted] == ["a1", "b1", "c1"]
    healed = store.load(name)
    assert [s["locator"]["label"] for s in healed.steps] == ["A", "X", "C"]
    assert len(list(tmp_path.glob("*.json"))) == 1
    t3 = FakeTarget(heal_pages("X"))
    out3 = Runner("c", t3, "go", None, cfg, flow=healed, store=store,
                  choose=scripted_choose(["DONE"])).run()
    assert out3["model_calls"] == 1 and "flow_updated" not in out3


def test_failed_or_paused_runs_never_save(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr("jev_runner.verify.check", lambda goal, page, history: (False, ""))
    store = FlowStore(tmp_path)
    out = Runner("a", FakeTarget(pay_pages(5)), "x", None, cfg, store=store,
                 choose=scripted_choose(["n1", "BLOCKED"]), field_text=text_amt).run()
    assert out["status"] == "blocked"
    # money mismatch: confirm screen shows another amount => paused_mismatch
    pages = [page(fill("n1", "Monto", rid="amt")),
             page(click("n2", "Pagar", rid="pay"), text="Destino ****4066 Monto 999"), page()]
    out = Runner("b", FakeTarget(pages), "pay", {"amount": 5, "recipient": RECIPIENT}, cfg,
                 store=store, choose=scripted_choose(["n1", "n2"]),
                 field_text=lambda c: ("5", {})).run()
    assert out["status"] == "paused_mismatch"
    out = Runner("c", FakeTarget(pay_pages(5)), "x", None, cfg, store=store, max_steps=1,
                 choose=scripted_choose(["n1"]), field_text=text_amt).run()
    assert out["status"] == "max_steps"
    assert list(tmp_path.glob("*.json")) == []


def test_learn_false_disables_both(tmp_path, cfg):
    store = FlowStore(tmp_path)
    r1 = run_pay(store, cfg, 15000, ["n1", "n2", "DONE"], learn=False)
    assert r1["status"] == "done" and "flow_saved" not in r1
    assert list(tmp_path.glob("*.json")) == []
    run_pay(store, cfg, 15000, ["n1", "n2", "DONE"])
    flow = store.find("android", START, "pay water bill 1")
    before = (tmp_path / f"{flow.name}.json").read_text()
    # learn=False must not rewrite the stored flow
    r = Runner("z", FakeTarget(pay_pages(15000)), "pay water bill 15000",
               {"amount": 15000, "recipient": RECIPIENT}, cfg, flow=flow, store=store, learn=False,
               choose=scripted_choose(["DONE"])).run()
    assert "flow_updated" not in r and (tmp_path / f"{flow.name}.json").read_text() == before


def test_explicit_save_as_wins_over_autosave(tmp_path, cfg):
    store = FlowStore(tmp_path)
    r = run_pay(store, cfg, 15000, ["n1", "n2", "DONE"], save_as="agua")
    assert r["flow_saved"] == "agua"
    assert [p.name for p in tmp_path.glob("*.json")] == ["agua.json"]


def test_autosave_never_stores_secret(tmp_path, cfg):
    store = FlowStore(tmp_path)
    pages = [page(fill("n1", "PIN")), page(click("n2", "Ingresar"), text="PIN 4829 ok"), page()]
    r = Runner("a", FakeTarget(pages), "login", None, cfg, store=store, start=START,
               choose=scripted_choose(["n1", "n2", "DONE"]))
    assert r.run()["status"] == "paused_needs_input"
    out = r.resume(input="4829")
    assert out["flow_saved"].startswith("auto-")
    assert all("4829" not in p.read_text() for p in tmp_path.glob("*.json"))


def test_goal_key_ignores_numbers():
    assert goal_key("Pay water 15000") == goal_key("pay  water 16.000")
    assert auto_name("android", "com.x/.A", "pay 1") == auto_name("android", "com.x", "pay 22")
    assert auto_name("android", "com.x", "pay").startswith("auto-android-com-x-")


def msg_pages():
    return [page(fill("n1", "Numero", rid="num")), page(click("n2", "Enviar", rid="send")),
            page(text="ok")]


def run_msg(store, cfg, number, choices, flow=None, typed=None):
    return Runner("m", FakeTarget(msg_pages()), f"send message to {number}", None, cfg, flow=flow,
                  store=store, start=START, choose=scripted_choose(choices),
                  field_text=lambda c: (typed or str(number), {})).run()


def test_literal_number_not_in_new_goal_falls_back_to_jev_and_heals(tmp_path, cfg):
    store = FlowStore(tmp_path)
    r1 = run_msg(store, cfg, 88881111, ["n1", "n2", "DONE"])
    flow = store.find("android", START, "send message to 99992222")
    assert flow is not None and flow.steps[0]["value"] == "literal:88881111"
    r2 = run_msg(store, cfg, 99992222, ["n1", "DONE"], flow=flow)
    assert r2["status"] == "done" and r2["model_calls"] == 2
    assert r2["flow_updated"] == r1["flow_saved"]
    assert store.load(flow.name).steps[0]["value"] == "literal:99992222"


def test_literal_number_present_in_new_goal_still_replays(tmp_path, cfg):
    store = FlowStore(tmp_path)
    run_msg(store, cfg, 88881111, ["n1", "n2", "DONE"])
    flow = store.find("android", START, "send message to 88881111")
    r = run_msg(store, cfg, 88881111, ["DONE"], flow=flow)
    assert r["model_calls"] == 1 and "flow_updated" not in r
