import json

import pytest

from jev_mcp.flows import Flow, FlowStore, flow_from_history, locate, matches, signature
from jev_mcp.runner import Runner
from tests.fakes import FakeTarget, click, fill, page, scripted_choose

RECIPIENT = "CR05015202001026284066"


def text5(ctx):
    return "5", {"model": "m", "latency_ms": 0, "usage": {}}


def pay_pages(amount):
    # The confirm screen shows the amount actually typed, so the money guard can match it.
    return [page(fill("n1", "Monto", rid="amt")),
            page(click("n2", "Pagar", rid="pay"), text=f"Destino ****4066 Monto ₡{amount}"),
            page(text="Pago exitoso")]


def record_agua(cfg, store):
    r = Runner("a", FakeTarget(pay_pages(5)), "pay", {"amount": 5, "recipient": RECIPIENT}, cfg,
               save_as="agua", store=store, choose=scripted_choose(["n1", "n2", "DONE"]),
               field_text=text5)
    return r.run()


def test_record_then_replay_uses_no_model_calls(tmp_path, cfg):
    store = FlowStore(tmp_path)
    out1 = record_agua(cfg, store)
    assert out1["status"] == "done" and out1["flow_saved"] == "agua"
    flow = store.load("agua")
    assert flow.steps[0]["value"] == "param:amount" and flow.steps[1]["value"] is None
    assert flow.goal == "pay" and sorted(flow.params) == ["amount", "recipient"]
    t = FakeTarget(pay_pages(7))
    out = Runner("b", t, "pay", {"amount": 7, "recipient": RECIPIENT}, cfg, flow=store.load("agua"),
                 choose=scripted_choose(["DONE"]),
                 field_text=lambda c: pytest.fail("text model must not be called on replay")).run()
    assert out["status"] == "done" and out["model_calls"] == 1  # only final DONE check
    assert t.acted[0] == ("n1", "7")
    assert t.acted[1] == ("n2", None) and t.verified == ["n2"]  # money step went through the guard


def test_replay_falls_back_to_jev_on_unexpected_screen_and_resyncs(tmp_path, cfg):
    store = FlowStore(tmp_path)
    rec = [page(click("a1", "Servicios"), url="app://home"),
           page(click("b1", "Agua"), url="app://servicios"),
           page(click("c1", "Ver recibo"), url="app://agua"),
           page(url="app://recibo")]
    out = Runner("a", FakeTarget(rec), "recibo", None, cfg, save_as="recibo", store=store,
                 choose=scripted_choose(["a1", "b1", "c1", "DONE"])).run()
    assert out["flow_saved"] == "recibo"
    # Ids differ between runs: replay must locate by label/role/kind, not by id.
    rep = [page(click("x1", "Servicios"), url="app://home"),
           page(click("l1", "Iniciar sesión"), url="app://login"),
           page(click("x2", "Agua"), url="app://servicios"),
           page(click("x3", "Ver recibo"), url="app://agua"),
           page(url="app://recibo")]
    t = FakeTarget(rep)
    calls = []

    def choose(view, goal, history):
        calls.append(view["url"])
        return scripted_choose(["l1" if view["url"] == "app://login" else "DONE"])(view, goal, history)

    out = Runner("b", t, "recibo", None, cfg, flow=store.load("recibo"), choose=choose).run()
    assert out["status"] == "done"
    assert calls == ["app://login", "app://recibo"]  # once for LOGIN, once for the final DONE
    assert out["model_calls"] == 2
    assert [a for a, _ in t.acted] == ["x1", "l1", "x2", "x3"]


def test_secret_never_stored_in_flow(tmp_path, cfg):
    store = FlowStore(tmp_path)
    pages = lambda: [page(fill("n1", "PIN")), page(click("n2", "Ingresar"), text="PIN 4829 ok"), page()]
    r = Runner("a", FakeTarget(pages()), "login", None, cfg, save_as="login", store=store,
               choose=scripted_choose(["n1", "n2", "DONE"]))
    assert r.run()["status"] == "paused_needs_input"
    out = r.resume(input="4829")
    assert out["status"] == "done" and out["flow_saved"] == "login"
    text = (tmp_path / "login.json").read_text()
    assert "4829" not in text
    assert store.load("login").steps[0]["value"] == "secret"
    # Replaying a secret step pauses for input; no model call, no stored value used.
    t = FakeTarget(pages())
    r2 = Runner("b", t, "login", None, cfg, flow=store.load("login"), choose=scripted_choose(["DONE"]))
    out = r2.run()
    assert out["status"] == "paused_needs_input" and out["field"] == "PIN" and out["model_calls"] == 0
    assert t.acted == []
    out = r2.resume(input="1111")
    assert out["status"] == "done" and t.acted == [("n1", "1111"), ("n2", None)]
    assert out["model_calls"] == 1 and "1111" not in json.dumps(out)


def test_flow_store_list_and_delete(tmp_path):
    store = FlowStore(tmp_path)
    step = {"signature": {"where": "app://x", "labels": ["A"]},
            "locator": {"kind": "click", "resource_id": None, "label": "A", "role": "button"},
            "value": None}
    store.save(Flow("Pago Agua", "android", "com.bank", "pay", ["amount"], [], [step, step]))
    store.save(Flow("luz", "mac", None, "pay luz", [], [], [step]))
    assert (tmp_path / "pago-agua.json").exists()
    listed = sorted(store.list(), key=lambda d: d["name"])
    assert listed == [
        {"name": "Pago Agua", "target": "android", "steps": 2, "params": ["amount"]},
        {"name": "luz", "target": "mac", "steps": 1, "params": []},
    ]
    assert store.load("Pago Agua").steps == [step, step]
    store.delete("Pago Agua")
    assert [d["name"] for d in store.list()] == ["luz"]
    with pytest.raises(KeyError):
        store.load("Pago Agua")
    with pytest.raises(KeyError):
        store.delete("Pago Agua")


# --- extra coverage beyond the brief ---

def test_replayed_money_step_gets_same_guard(tmp_path, cfg):
    store = FlowStore(tmp_path)
    record_agua(cfg, store)
    # Screen still shows ₡5 but this run asks for 7: the replayed "Pagar" must pause, not tap.
    t = FakeTarget(pay_pages(5))
    out = Runner("b", t, "pay", {"amount": 7, "recipient": RECIPIENT}, cfg, flow=store.load("agua"),
                 choose=scripted_choose([])).run()
    assert out["status"] == "paused_mismatch" and out["model_calls"] == 0
    assert t.acted == [("n1", "7")]


def test_replay_ambiguous_locator_falls_back_to_jev(tmp_path, cfg):
    store = FlowStore(tmp_path)
    out = Runner("a", FakeTarget([page(click("n1", "Siguiente")), page()]), "g", None, cfg,
                 save_as="f", store=store, choose=scripted_choose(["n1", "DONE"])).run()
    assert out["flow_saved"] == "f"
    # Same signature labels set (Jaccard 1.0) but the label now appears twice: ambiguous.
    t = FakeTarget([page(click("n1", "Siguiente"), click("n2", "Siguiente")), page()])
    out = Runner("b", t, "g", None, cfg, flow=store.load("f"),
                 choose=scripted_choose(["n2", "DONE"])).run()
    assert out["status"] == "done" and out["model_calls"] == 2 and t.acted == [("n2", None)]


def test_matches_requires_same_where_and_jaccard():
    p = page(*[click(f"n{i}", f"L{i}") for i in range(5)], url="app://x")
    sig = signature(p)
    assert sig == {"where": "app://x", "labels": ["L0", "L1", "L2", "L3", "L4"]}
    assert matches(sig, p)
    assert not matches({**sig, "where": "app://y"}, p)
    assert matches({**sig, "labels": sig["labels"][:4]}, p)  # 4/5 = 0.8
    assert not matches({**sig, "labels": sig["labels"][:3]}, p)  # 3/5 = 0.6


def test_locate_by_resource_id_then_label_and_controls():
    p = page(click("n1", "Pagar", rid="pay"), click("n2", "Pagar"))
    p["actions"].append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down"})
    assert locate({"kind": "click", "resource_id": "pay", "label": "X", "role": "button"}, p)["id"] == "n1"
    assert locate({"kind": "click", "resource_id": None, "label": "Pagar", "role": "button"}, p) is None
    assert locate({"control": "scroll_down"}, p)["id"] == "scroll_down"
    assert locate({"control": "back"}, p) is None


def test_flow_from_history_values():
    def entry(kind, text, secret=False):
        return {"action": "x", "kind": kind, "text": "•••" if secret else text, "page_changed": True,
                "where": "w", "signature": {"where": "w", "labels": ["x"]},
                "locator": {"kind": kind, "resource_id": None, "label": "x", "role": "r"},
                "value_source": None}
    hist = [entry("fill", "5"), entry("fill", "hola"), entry("fill", "4829", secret=True),
            entry("click", None)]
    f = flow_from_history("n", "android", "pkg", "g", {"amount": 5}, ["amount"], hist)
    assert [s["value"] for s in f.steps] == ["param:amount", "literal:hola", "secret", None]
    assert f.params == ["amount"] and f.money_params == ["amount"]
    assert set(f.steps[0]) == {"signature", "locator", "value"}


def test_replay_retries_step_after_stale_page(tmp_path, cfg):
    from jev_mcp.targets.base import StalePage

    class StaleOnce(FakeTarget):
        def act(self, action, page, text=None, verify=False):
            if not getattr(self, "_staled", False):
                self._staled = True
                raise StalePage()
            super().act(action, page, text=text, verify=verify)

    store = FlowStore(tmp_path)
    Runner("a", FakeTarget([page(click("n1", "Siguiente")), page()]), "g", None, cfg,
           save_as="f", store=store, choose=scripted_choose(["n1", "DONE"])).run()
    t = StaleOnce([page(click("n1", "Siguiente")), page()])
    out = Runner("b", t, "g", None, cfg, flow=store.load("f"), choose=scripted_choose(["DONE"])).run()
    assert out["status"] == "done" and out["model_calls"] == 1 and t.acted == [("n1", None)]
