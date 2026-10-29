import json
from pathlib import Path

from jev_mcp.config import Config
from jev_mcp.runner import Runner
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
    r.resume(input="1", instruction="use the savings account")
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
