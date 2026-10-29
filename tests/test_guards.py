from jev_mcp.guards import check_money, is_money_action, is_secret_field, scrub


def test_amount_formats_match():
    for text in ["Monto: ₡150.000,00", "CRC 150,000.00", "₡150 000", "150000"]:
        assert check_money(text, {"amount": 150000}).ok, text


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


def test_money_label_exact_after_normalization(cfg):
    page = {"url": "net.bac.sbe.android/x", "text": ""}
    click = lambda label: {"kind": "click", "label": label}
    assert is_money_action(click("TRANSFERIR"), page, {}, cfg)
    assert not is_money_action(click("Enviar código"), page, {}, cfg)
    assert not is_money_action(click("Transferir"), {"url": "com.gohighlevel/x", "text": ""}, {}, cfg)
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
