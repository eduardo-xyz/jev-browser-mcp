from decimal import Decimal

from jev_runner.config import Config
from jev_runner.guards import (
    check_money,
    is_money_action,
    is_secret_field,
    parse_amount,
    scrub,
)


def test_amount_formats_match():
    for text in ["Monto: ₡150.000,00", "CRC 150,000.00", "₡150 000", "150000"]:
        assert check_money(f"Para Juan Perez {text}", {"amount": 150000, "recipient": "Juan Perez"}).ok, text


def test_amount_off_by_factor_does_not_match():
    assert not check_money("Monto: ₡15.000,00", {"amount": 150000}).ok


def test_masked_recipient_matches_visible_digits():
    params = {"amount": "150000", "recipient": ["Juan Perez", "CR05015202001026284066"]}
    assert check_money("Destino ****4066 ₡150.000,00", params).ok


def test_masked_recipient_with_other_digits_pauses():
    params = {"amount": "150000", "recipient": "CR05015202001026284066"}
    result = check_money("Destino ****1234 ₡150.000,00", params)
    assert not result.ok and "recipient" in result.expected


def test_recipient_name_accent_and_case_insensitive():
    assert check_money("Para: JUAN PÉREZ  ₡150.000", {"amount": 150000, "recipient": "juan perez"}).ok


def test_money_label_exact_after_normalization():
    cfg = Config(money_apps=["net.bac.sbe.android"])
    page = {"url": "net.bac.sbe.android/x", "text": ""}
    click = lambda label: {"kind": "click", "label": label}
    assert is_money_action(click("TRANSFERIR"), page, {}, cfg)
    assert not is_money_action(click("Enviar código"), page, {}, cfg)
    assert is_money_action(click("Transferir"), {"url": "com.gohighlevel/x", "text": ""}, {}, cfg)
    assert not is_money_action(click("Confirmar"), {"url": "com.gohighlevel/x", "text": ""}, {}, cfg)
    assert is_money_action(click("Confirm"), {"url": "example.com/pay", "text": ""}, {"amount": 5}, cfg)


def test_secret_field_detection(cfg):
    assert is_secret_field({"kind": "fill", "label": "Código BAC"}, cfg.secret_patterns)
    assert is_secret_field({"kind": "fill", "label": "x", "password": True}, cfg.secret_patterns)
    assert not is_secret_field({"kind": "click", "label": "Ingresar token"}, cfg.secret_patterns)
    assert not is_secret_field({"kind": "fill", "label": "Usuario"}, cfg.secret_patterns)


def test_scrub_removes_secret_everywhere():
    page = {"text": "Código 482913 ok", "actions": [{"label": "482913", "value": "482913"}]}
    out = scrub(page, ["482913"])
    assert "482913" not in str(out) and page["text"] == "Código 482913 ok"


def test_recipient_name_needs_token_boundaries():
    p = {"amount": 150000, "recipient": "Ana Perez"}
    assert not check_money("Para: Juana Perezoso 150000", p).ok
    assert check_money("Para: Ana Perez 150000", p).ok


def test_numeric_recipient_only_standalone_digit_run():
    p = {"amount": 150000, "recipient": "5000"}
    assert not check_money("Servicio 9999 Monto 150000", p).ok
    assert not check_money("Servicio 15000 Monto 150000", p).ok
    assert check_money("Servicio 5000 Monto 150000", p).ok


def test_adjacent_numbers_do_not_fuse():
    p = {"amount": 150000, "recipient": "Juan Perez"}
    assert not check_money("Juan Perez Cuotas 1\n50.000,00", p).ok
    assert check_money("Juan Perez Destino ****4066 150.000,00", {**p, "recipient": ["Juan Perez", "4066"]}).ok


def test_check_money_fails_closed_on_missing_keys():
    r = check_money("Juan Perez 150000", {"amount": 150000})
    assert not r.ok and "recipient" in r.expected
    r = check_money("Juan Perez 150000", {"recipient": "Juan Perez"})
    assert not r.ok and "amount" in r.expected
    assert not check_money("Juan Perez 150000", {}).ok


def test_money_label_prefix_and_exclusions():
    cfg = Config(money_apps=["net.bac.sbe.android"])
    page = {"url": "net.bac.sbe.android/x", "text": ""}
    click = lambda label: {"kind": "click", "label": label}
    for label in ["Confirmar transferencia", "Transferir ahora", "Pagar ₡150.000"]:
        assert is_money_action(click(label), page, {}, cfg), label
    for label in ["Enviar código", "Cancelar pago", "Enviar por correo", "Pagar con token"]:
        assert not is_money_action(click(label), page, {}, cfg), label


def test_multiple_masks_need_own_accounts():
    text = "Origen ****4066 Destino ****1234 150000"
    assert not check_money(text, {"amount": 150000, "recipient": "CR05015202001026284066"}).ok
    p = {"amount": 150000, "recipient": "CR05015202001026284066"}
    assert not check_money("Origen ****4066 Destino ****1234 150000", {**p, "recipient": "CR0500001234"}).ok
    assert check_money(text, {"amount": 150000, "recipient": "CR0500001234"}, ["4066"]).ok
    assert not check_money(text, {"amount": 150000, "recipient": "CR0500009999"}, ["4066"]).ok


def test_parse_amount_rejects_bool_and_nonfinite():
    for v in [True, float("nan"), float("inf")]:
        assert parse_amount(v) is None


def test_scrub_title_url_and_no_actions_key():
    out = scrub({"url": "x/482913", "title": "t 482913", "text": "a"}, ["482913"])
    assert "482913" not in str(out) and "actions" not in out


def test_strong_labels_guarded_everywhere_contextual_only_in_context():
    cfg = Config()
    assert cfg.money_apps == []
    anywhere = {"url": "example.org/x", "text": ""}
    click = lambda label: {"kind": "click", "label": label}
    for label in ["Transferir", "Pagar ahora", "Confirmar transferencia", "Send money", "Pay now"]:
        assert is_money_action(click(label), anywhere, {}, cfg), label
    for label in ["Send", "Confirmar", "Enviar", "Confirm"]:
        assert not is_money_action(click(label), anywhere, {}, cfg), label
        assert is_money_action(click(label), anywhere, {"amount": 5}, cfg), label
        assert is_money_action(click(label), {"url": "bank.app/x"}, {},
                               Config(money_apps=["bank.app"])), label
    assert not is_money_action(click("Enviar código"), anywhere, {"amount": 5}, cfg)


def test_scrub_respects_digit_boundaries():
    out = scrub({"text": "PIN 1234 Monto 12345 x1234y PIN1234"}, ["1234"])
    assert out["text"] == "PIN ••• Monto 12345 x•••y PIN•••"


def test_scrub_value_recurses():
    from jev_runner.guards import scrub_value
    v = {"a": ["482913", ("x 482913",)], "b": 482913, "c": "4829130"}
    assert scrub_value(v, ["482913"]) == {"a": ["•••", ("x •••",)], "b": "•••", "c": "4829130"}


def test_ambiguous_single_decimal_digit_amount_is_rejected():
    assert parse_amount("150.5") is None and parse_amount("150,5") is None
    assert not check_money("Monto ₡1.505,00 Destino ****4066",
                           {"amount": "150.5", "recipient": "4066"}).ok
    assert not check_money("Monto ₡1.505,00 Destino ****4066",
                           {"amount": "150,5", "recipient": "4066"}).ok
    # unchanged rules
    assert parse_amount("150.50") == parse_amount("150,50") == Decimal("150.50")
    assert parse_amount("1.505,00") == Decimal("1505.00")
    assert parse_amount("150.000") == Decimal("150000")
    assert parse_amount(150.5) == Decimal("150.5")


def test_secret_field_checks_hint_and_resource_id_tail(cfg):
    p = cfg.secret_patterns
    assert is_secret_field({"kind": "fill", "label": "482913", "hint": "Código"}, p)
    assert is_secret_field({"kind": "fill", "label": "482913", "resource_id": "com.app:id/etOtp"}, p)
    assert is_secret_field({"kind": "fill", "label": "x", "resource_id": "com.app:id/pin_input"}, p)
    assert not is_secret_field({"kind": "fill", "label": "x", "resource_id": "com.app:id/etName"}, p)
    assert not is_secret_field({"kind": "fill", "label": "x", "resource_id": "com.app:id/spinner"}, p)


def test_scrub_cleans_action_hint():
    out = scrub({"actions": [{"label": "x", "hint": "Clave 482913"}]}, ["482913"])
    assert out["actions"][0]["hint"] == "Clave •••"


def test_malformed_amounts_fail_closed():
    for s in [".5", ",5", ".50", "1.5000", "1,5000", "1.23.4", "12.34.567", "1.000,5",
              "1,234.567", "1.234,56,78", "1.234,567", "1,000.000,00"]:
        assert parse_amount(s) is None, s
    assert not check_money("Monto ₡15.000 Destino ****4066",
                           {"amount": "1.5000", "recipient": "4066"}).ok
    # well-formed amounts still parse
    assert parse_amount("150000") == Decimal("150000")
    assert parse_amount("1.234.567") == parse_amount("1,234,567") == Decimal("1234567")
    assert parse_amount("1,234.56") == parse_amount("1.234,56") == Decimal("1234.56")
    assert parse_amount("₡ 150 000,00") == Decimal("150000.00")
