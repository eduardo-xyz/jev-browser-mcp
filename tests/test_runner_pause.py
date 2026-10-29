import json
from pathlib import Path

import pytest

from jev_runner.config import Config
from jev_runner.runner import Runner
from tests.fakes import FakeTarget, click, fill, page, scripted_choose


def cfg_with_data(tmp_path):
    return Config(data_dir=tmp_path)


def test_secret_field_pauses_without_text_model(cfg):
    calls = []
    t = FakeTarget([page(fill("n1", "Código BAC")), page(click("n2", "Continuar")), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "n2", "DONE"]),
               field_text=lambda ctx: calls.append(ctx))
    out = r.run()
    assert out["status"] == "paused_needs_input" and out["field"] == "Código BAC" and calls == []
    out = r.resume(input="482913")
    assert out["status"] == "done" and t.acted[0] == ("n1", "482913")
    assert "482913" not in json.dumps(out)


def test_typed_secret_scrubbed_before_next_choose(cfg):
    seen = []
    t = FakeTarget([page(fill("n1", "OTP")), page(click("n2", "Ok"), text="Código 482913 aceptado"), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "n2", "DONE"], spy=seen))
    r.run(); r.resume(input="482913")
    assert all("482913" not in s for s in seen)


def test_money_match_taps_autonomously_and_saves_receipt(tmp_path):
    confirm = page(click("n9", "Transferir"), text="Destino ****4066 Monto ₡150.000,00")
    receipt = page(text="Transferencia exitosa Referencia: 99887766")
    t = FakeTarget([confirm, receipt])
    out = Runner("s", t, "pay", {"amount": 150000, "recipient": "CR05015202001026284066"},
                 cfg_with_data(tmp_path), choose=scripted_choose(["n9", "DONE"])).run()
    assert out["status"] == "done"
    assert out["receipt"]["reference"] == "99887766" and Path(out["receipt"]["path"]).exists()
    assert t.acted[0][0] == "n9" and t.verified == ["n9"]


def test_money_mismatch_pauses_then_confirm_executes(cfg):
    confirm = page(click("n9", "Transferir"), text="Destino ****1234 Monto ₡150.000,00")
    t = FakeTarget([confirm, page()])
    r = Runner("s", t, "pay", {"amount": 150000, "recipient": "CR05015202001026284066"}, cfg,
               choose=scripted_choose(["n9", "DONE"]))
    out = r.run()
    assert out["status"] == "paused_mismatch" and t.acted == []
    assert r.resume()["status"] == "paused_mismatch"
    assert r.resume(confirm=True)["status"] == "done" and t.acted[0][0] == "n9"


def test_money_app_without_params_pauses():
    cfg = Config(money_apps=["net.bac.sbe.android"])
    t = FakeTarget([page(click("n9", "Pagar"), url="net.bac.sbe.android/x")])
    out = Runner("s", t, "check", None, cfg, choose=scripted_choose(["n9"])).run()
    assert out["status"] == "paused_mismatch"


# --- extra coverage beyond the brief ---

def test_mismatch_result_reports_expected_and_seen(cfg):
    confirm = page(click("n9", "Transferir"), text="Destino ****1234 Monto ₡150.000,00")
    r = Runner("s", FakeTarget([confirm]), "pay",
               {"amount": 150000, "recipient": "CR05015202001026284066"}, cfg,
               choose=scripted_choose(["n9"]))
    out = r.run()
    assert out["expected"] == {"recipient": ["CR05015202001026284066"]}
    assert out["seen"] == {"recipient": ["1234"]}
    assert r.pending["type"] == "money" and r.pending["locator"]["label"] == "Transferir"


def test_secret_resume_without_input_stays_paused(cfg):
    t = FakeTarget([page(fill("n1", "PIN")), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "DONE"]))
    r.run()
    out = r.resume()
    assert out["status"] == "paused_needs_input" and out["field"] == "PIN" and t.acted == []


def test_resumed_secret_masked_in_history_and_verified(cfg):
    t = FakeTarget([page(fill("n1", "Clave")), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "DONE"]))
    r.run()
    out = r.resume(input="9999")
    assert out["last_actions"] == ["fill · Clave = •••"]
    assert r.history[0]["text"] == "•••" and "9999" in r.secrets and t.verified == ["n1"]
    assert "9999" not in json.dumps(r.history)


def test_pending_located_by_resource_id_on_reobserve(cfg):
    a = {**fill("n1", "OTP"), "resource_id": "otp_field"}
    moved = {**fill("n7", "Ingrese OTP"), "resource_id": "otp_field"}
    t = FakeTarget([page(a), page(moved), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "DONE"]))
    r.run()
    t._advance()  # screen re-rendered while paused: new node id and label
    r.page = {}
    out = r.resume(input="1111")
    assert out["status"] == "done" and t.acted[0] == ("n7", "1111")


def test_confirm_on_changed_screen_reruns_guard(cfg):
    first = page(click("n9", "Transferir"), text="Destino ****1234 Monto ₡150.000,00")
    changed = page(click("n9", "Transferir"), text="Destino ****9999 Monto ₡999.000,00")
    t = FakeTarget([first, changed, page()])
    r = Runner("s", t, "pay", {"amount": 150000, "recipient": "CR05015202001026284066"}, cfg,
               choose=scripted_choose(["n9", "DONE"]))
    r.run()
    t._advance()
    r.page = {}
    out = r.resume(confirm=True)
    assert out["status"] == "paused_mismatch" and t.acted == []


def test_instruction_appended_to_goal(cfg):
    goals = []

    def choose(p, goal, history):
        goals.append(goal)
        return scripted_choose(["n1"] if len(goals) == 1 else ["DONE"])(p, goal, history)

    t = FakeTarget([page(fill("n1", "OTP")), page()])
    r = Runner("s", t, "g", None, cfg, choose=choose)
    r.run()
    r.resume(input="482913", instruction="use the savings account")
    assert goals[-1] == "g\nuse the savings account"


def test_no_receipt_without_money(cfg):
    out = Runner("s", FakeTarget([page(text="Referencia: 12345678")]), "g", None, cfg,
                 choose=scripted_choose(["DONE"])).run()
    assert "receipt" not in out


def test_target_error_during_secret_fill_never_leaks_value(cfg):
    class Boom(FakeTarget):
        def act(self, action, page, text=None, verify=False):
            raise RuntimeError(f"could not type {text}")

    t = Boom([page(fill("n1", "OTP"))])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1"]))
    r.run()
    out = r.resume(input="482913")
    assert out["status"] == "error" and "482913" not in json.dumps(out)
    assert r.pending["type"] == "secret"  # still resumable after the failure


# --- fix round 1 ---

def test_default_config_strong_label_without_params_pauses(cfg):
    t = FakeTarget([page(click("n9", "Transferir"), url="some.app/x")])
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n9"])).run()
    assert out["status"] == "paused_mismatch" and t.acted == []


def test_default_config_contextual_label_unconfigured_host_is_not_money(cfg):
    t = FakeTarget([page(click("n9", "Send"), url="chat.example/x"), page()])
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n9", "DONE"])).run()
    assert out["status"] == "done" and t.acted == [("n9", None)] and t.verified == []


PAY = {"amount": 150000, "recipient": "CR05015202001026284066"}


def test_confirm_refused_when_only_text_changed(cfg):
    confirm = page(click("n9", "Transferir"), text="Destino ****1234 Monto ₡150.000,00")
    t = FakeTarget([confirm, page()])
    r = Runner("s", t, "pay", PAY, cfg, choose=scripted_choose(["n9", "DONE"]))
    r.run()
    confirm["text"] = "****9999 Monto 999.000"  # Android fingerprint ignores text
    r.page = {}
    out = r.resume(confirm=True)
    assert out["status"] == "paused_mismatch" and t.acted == []
    assert "9999" in json.dumps(out["seen"])


def test_typed_secret_never_leaks_via_expected_or_seen(cfg):
    t = FakeTarget([page(fill("n1", "OTP")),
                    page(click("n2", "Transferir"),
                         text="Código 482913 aceptado Destino ****4066 Monto ₡1,00")])
    r = Runner("s", t, "pay", PAY, cfg, choose=scripted_choose(["n1", "n2"]))
    r.run()
    out = r.resume(input="482913")
    assert out["status"] == "paused_mismatch"
    assert "482913" not in json.dumps(out) and "482913" not in json.dumps(r.pending)


def test_confirm_taps_the_exact_element_jev_chose(cfg):
    t = FakeTarget([page(click("n1", "Pagar"), click("n2", "Pagar")), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n2", "DONE"]))
    assert r.run()["status"] == "paused_mismatch"
    assert r.resume(confirm=True)["status"] == "done" and t.acted == [("n2", None)]


def test_confirm_on_changed_screen_with_two_matches_stays_paused(cfg):
    t = FakeTarget([page(click("n1", "Pagar"), click("n2", "Pagar")),
                    page(click("n5", "Pagar"), click("n6", "Pagar"))])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n2"]))
    r.run()
    t._advance()
    r.page = {}
    out = r.resume(confirm=True)
    assert (out["status"], out["reason"]) == ("paused_mismatch", "pending_element_ambiguous")
    assert t.acted == []


def test_money_pending_requires_same_label(cfg):
    a = {**click("n9", "Transferir"), "resource_id": "btn_main"}
    b = {**click("n3", "Cancelar"), "resource_id": "btn_main"}
    t = FakeTarget([page(a), page(b)])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n9"]))
    r.run()
    t._advance()
    r.page = {}
    out = r.resume(confirm=True)
    assert out["reason"] == "pending_element_missing" and t.acted == []


def test_failed_confirmed_money_act_is_uncertain_and_not_retryable(cfg):
    t = FakeTarget([page(click("n9", "Transferir"))])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n9"]))
    r.run()
    t.raise_on_act = RuntimeError("tap verification failed")
    out = r.resume(confirm=True)
    assert (out["status"], out["reason"]) == ("error", "money_action_uncertain")
    assert r.pending is None
    t.raise_on_act = None
    r.resume(confirm=True)
    assert t.acted == []  # a second confirm can never re-tap


def test_failed_autonomous_money_act_is_uncertain(cfg):
    confirm = page(click("n9", "Transferir"), text="Destino ****4066 Monto ₡150.000,00")
    t = FakeTarget([confirm], raise_on_act=RuntimeError("boom"))
    out = Runner("s", t, "pay", PAY, cfg, choose=scripted_choose(["n9"])).run()
    assert (out["status"], out["reason"]) == ("error", "money_action_uncertain")


def test_instruction_escapes_missing_pending_element(cfg):
    a = {**fill("n1", "OTP"), "resource_id": "otp"}
    t = FakeTarget([page(a), page(click("n2", "Volver")), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "n2", "DONE"]))
    r.run()
    t._advance()
    r.page = {}
    assert r.resume(input="482913")["reason"] == "pending_element_missing"
    out = r.resume(instruction="start over from the menu")
    assert out["status"] == "done" and r.pending is None
    assert r.goal.endswith("\nstart over from the menu")


def test_empty_input_treated_as_missing(cfg):
    t = FakeTarget([page(fill("n1", "PIN")), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "DONE"]))
    r.run()
    assert r.resume(input="")["status"] == "paused_needs_input" and t.acted == []


# --- fix round 2: one money action per run ---

class RaiseOnce(FakeTarget):
    def __init__(self, pages, exc, **kw):
        super().__init__(pages, **kw)
        self.exc, self.attempts = exc, 0

    def act(self, action, page, text=None, verify=False):
        self.attempts += 1
        if self.exc is not None:
            exc, self.exc = self.exc, None
            raise exc
        return super().act(action, page, text, verify)


class ObserveCrashOnceAfterAct(FakeTarget):
    crashed = False

    def observe(self):
        if self.acted and not self.crashed:
            self.crashed = True
            raise RuntimeError("observe died")
        return super().observe()


MATCH = "Destino ****4066 Monto ₡150.000,00"


def test_uncertain_autonomous_tap_never_retried_by_resume(cfg):
    t = RaiseOnce([page(click("n9", "Transferir"), text=MATCH), page()],
                  RuntimeError("timeout after tap"))
    r = Runner("s", t, "pay", PAY, cfg, choose=scripted_choose(["n9", "n9", "DONE"]))
    assert r.run()["reason"] == "money_action_uncertain"
    out = r.resume()
    assert (out["status"], out["reason"]) == ("error", "money_action_uncertain")
    assert t.attempts == 1


def test_post_act_crash_then_resume_taps_once_and_keeps_receipt_flag(cfg):
    confirm = page(click("n9", "Transferir"), text=MATCH)
    t = ObserveCrashOnceAfterAct([confirm], stuck_ids={"n9"})
    r = Runner("s", t, "pay", PAY, cfg, choose=scripted_choose(["n9", "n9", "DONE"]))
    assert r.run()["status"] == "error"
    out = r.resume()
    assert (out["status"], out["reason"]) == ("paused_mismatch", "money_already_attempted")
    assert t.acted == [("n9", None)] and r.executed_money is True


def test_second_money_tap_in_same_run_pauses_until_confirmed(cfg):
    confirm = page(click("n9", "Transferir"), text=MATCH)
    t = FakeTarget([confirm], stuck_ids={"n9"})
    r = Runner("s", t, "pay", PAY, cfg, choose=scripted_choose(["n9", "n9", "DONE"]))
    out = r.run()
    assert (out["status"], out["reason"]) == ("paused_mismatch", "money_already_attempted")
    assert t.acted == [("n9", None)]
    assert r.resume(confirm=True)["status"] == "done" and len(t.acted) == 2


def test_resume_after_uncertain_never_acts_with_any_args(cfg):
    t = RaiseOnce([page(click("n9", "Transferir"), text=MATCH), page()], RuntimeError("x"))
    r = Runner("s", t, "pay", PAY, cfg, choose=scripted_choose(["n9"] + ["n9", "DONE"] * 5))
    r.run()
    for kw in ({}, {"confirm": True}, {"input": "123456"}, {"instruction": "retry"}):
        out = r.resume(**kw)
        assert (out["status"], out["reason"]) == ("error", "money_action_uncertain"), kw
    assert r.run()["reason"] == "money_action_uncertain"
    assert t.attempts == 1 and t.acted == []


def test_stale_before_tap_is_not_uncertain_autonomous(cfg):
    from jev_runner.targets.base import StalePage
    t = RaiseOnce([page(click("n9", "Transferir"), text=MATCH), page()], StalePage())
    r = Runner("s", t, "pay", PAY, cfg, choose=scripted_choose(["n9", "n9", "DONE"]))
    out = r.run()
    assert out["status"] == "done" and t.acted == [("n9", None)]


def test_stale_before_tap_is_not_uncertain_confirmed(cfg):
    from jev_runner.targets.base import StalePage
    t = RaiseOnce([page(click("n9", "Transferir")), page()], None)
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n9", "DONE"]))
    r.run()
    t.exc = StalePage()
    out = r.resume(confirm=True)
    assert out["status"] == "paused_mismatch" and out["reason"] != "money_action_uncertain"
    assert r.money_attempted is False and t.acted == []
    assert r.resume(confirm=True)["status"] == "done" and t.acted == [("n9", None)]


def test_confirm_transfer_label_without_params_pauses(cfg):
    t = FakeTarget([page(click("n9", "Confirm transfer"), url="any.site/x")])
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n9"])).run()
    assert out["status"] == "paused_mismatch" and t.acted == []


# --- fix round 3 ---

def test_cancelled_during_confirmed_money_act_is_terminal(cfg):
    import asyncio
    t = RaiseOnce([page(click("n9", "Transferir")), page()], None)
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n9", "DONE", "DONE"]))
    r.run()
    t.exc = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        r.resume(confirm=True)
    assert (r.status, r.reason) == ("error", "money_action_uncertain") and r.pending is None
    out = r.resume(confirm=True)
    assert (out["status"], out["reason"]) == ("error", "money_action_uncertain")
    assert t.attempts == 1 and t.acted == []


def test_receipt_not_overwritten_by_later_done(tmp_path):
    t = FakeTarget([page(click("n9", "Transferir"), text=MATCH),
                    page(click("n1", "Saldo"), text="Referencia: 99887766"),
                    page(text="Saldo 5 Referencia: 11112222")])
    r = Runner("s", t, "pay", PAY, cfg_with_data(tmp_path),
               choose=scripted_choose(["n9", "DONE", "n1", "DONE"]))
    first = r.run()["receipt"]
    assert first["reference"] == "99887766"
    out = r.resume(instruction="check balance")
    assert out["status"] == "done" and out["receipt"] == first


# --- final review fixes -------------------------------------------------------

def _spying(choices):
    """A choose that records everything it is shown (page, goal and history)."""
    seen, it = [], iter(choices)

    def choose(view, goal, history):
        seen.append(json.dumps([view, goal, history], ensure_ascii=False))
        return {"choice": next(it)}

    return choose, seen


def test_retyping_into_field_whose_label_is_now_the_otp_pauses_and_never_leaks(cfg):
    # Android: an EditText's label is its current text. After the OTP is typed into a
    # non-password field, the field's label becomes the OTP itself.
    p1 = page(fill("n0", "Código", rid="com.app:id/etOtp"), click("n1", "Continuar"))
    p2 = page(fill("n0", "482913", rid="com.app:id/etOtp"), click("n1", "Continuar"),
              text="482913\nCódigo incorrecto")
    p3 = page()
    t = FakeTarget([p1, p2, p3])
    choose, seen = _spying(["n0", "n0", "DONE"])
    ctxs = []

    def field_text(ctx):
        ctxs.append(json.dumps(ctx, ensure_ascii=False))
        return "000000", None

    r = Runner("s", t, "log in", None, cfg, choose=choose, field_text=field_text, learn=False)
    assert r.run()["status"] == "paused_needs_input"
    out = r.resume(input="482913")
    assert out["status"] == "paused_needs_input"
    assert ctxs == []  # the text model was never asked
    assert t.acted == [("n0", "482913")]
    assert "482913" not in json.dumps(out, ensure_ascii=False)
    assert all("482913" not in s for s in seen)
    out = r.resume(input="777111")
    assert out["status"] == "done" and t.acted[1] == ("n0", "777111")
    assert all(s not in json.dumps(out, ensure_ascii=False) for s in ("482913", "777111"))
    assert all("482913" not in s and "777111" not in s for s in seen)


def test_fill_with_secret_in_value_is_treated_as_secret(cfg):
    p1 = page(fill("n0", "Código"), click("n1", "Ok"))
    p2 = page({**fill("n0", "Campo"), "value": "482913"}, click("n1", "Ok"))
    t = FakeTarget([p1, p2, page()])
    ctxs = []
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n0", "n0", "DONE"]),
               field_text=lambda ctx: ctxs.append(ctx) or ("x", None), learn=False)
    r.run()
    assert r.resume(input="482913")["status"] == "paused_needs_input" and ctxs == []


def test_field_reused_by_resource_id_is_sticky_secret(cfg):
    # same resource id, innocuous label, no secret on screen: still a secret field
    p1 = page(fill("n0", "Código", rid="com.app:id/f1"), click("n1", "Ok"))
    p2 = page(fill("n0", "Campo", rid="com.app:id/f1"), click("n1", "Ok"))
    t = FakeTarget([p1, p2, page()])
    ctxs = []
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n0", "n0", "DONE"]),
               field_text=lambda ctx: ctxs.append(ctx) or ("x", None), learn=False)
    r.run()
    assert r.resume(input="482913")["status"] == "paused_needs_input" and ctxs == []


def test_click_label_with_secret_scrubbed_in_history_and_results(cfg):
    p1 = page(fill("n0", "OTP"), click("n1", "Ok"))
    p2 = page(click("n5", "Reenviar 482913"), click("n6", "Ok"))
    t = FakeTarget([p1, p2, page(), page()])
    choose, seen = _spying(["n0", "n5", "DONE"])
    r = Runner("s", t, "g", None, cfg, choose=choose, learn=False)
    r.run()
    out = r.resume(input="482913")
    assert out["status"] == "done" and t.acted[1][0] == "n5"
    assert "482913" not in json.dumps(out, ensure_ascii=False)
    assert all("482913" not in s for s in seen)
    assert "482913" not in json.dumps(r.history, ensure_ascii=False)


def test_field_context_gets_scrubbed_action(cfg, monkeypatch):
    from jev_runner import runner as runner_mod
    got = []
    monkeypatch.setattr(runner_mod.model, "field_context",
                        lambda goal, action, page, history: got.append(action) or {})
    monkeypatch.setattr(runner_mod.Runner, "_is_secret", lambda self, a: False)
    t = FakeTarget([page({**fill("n0", "Nota 482913"), "value": "482913"}), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n0", "DONE"]),
               field_text=lambda ctx: ("hola", None), learn=False)
    r.secrets.append("482913")
    r.run()
    assert got and "482913" not in json.dumps(got, ensure_ascii=False)
    assert "482913" not in json.dumps(r.history, ensure_ascii=False)


def test_result_build_scrubs_every_string(cfg):
    from jev_runner import result
    t = FakeTarget([page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["DONE"]), learn=False)
    r.secrets.append("482913")
    r.page = {"url": "app://x?otp=482913"}
    r.reason = "boom 482913"
    r.pending = {"field": "482913", "expected": {"a": "482913"}, "seen": ["482913"]}
    r.history.append({"kind": "click", "action": "Reenviar 482913", "text": None})
    assert "482913" not in json.dumps(result.build(r), ensure_ascii=False)


def test_same_screen_resume_requires_matching_locator(cfg):
    # A synthetic link appears above the field while paused; the fingerprint ignores it,
    # so the old ids shift under an unchanged fingerprint.
    before = page(fill("n0", "Usuario", rid="com.app:id/user"),
                  fill("n1", "Código", rid="com.app:id/otp"))
    after = page({"id": "n0", "node": "n0", "kind": "click", "label": "Tipo de Cambio",
                  "role": "link", "synthetic": True},
                 fill("n1", "Usuario", rid="com.app:id/user"),
                 fill("n2", "Código", rid="com.app:id/otp"))
    after["fingerprint"] = before["fingerprint"]
    t = FakeTarget([before, after, page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "DONE"]), learn=False)
    assert r.run()["status"] == "paused_needs_input"
    t._advance()
    out = r.resume(input="482913")
    assert out["status"] == "done" and t.acted[0] == ("n2", "482913")
