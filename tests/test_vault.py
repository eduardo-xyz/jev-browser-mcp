import json
from types import SimpleNamespace

import pytest

from jev_runner import vault
from jev_runner.runner import Runner
from tests.fakes import FakeTarget, click, fill, page, scripted_choose

SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # RFC 6238 test key "12345678901234567890"


def test_scope_is_host_for_chrome_and_package_for_android():
    assert vault.scope_of("chrome", "https://App.Example.com/login?x=1") == "app.example.com"
    assert vault.scope_of("android", "net.bac.sbe.android/.Main") == "net.bac.sbe.android"
    assert vault.scope_of("chrome", "") == "" and vault.scope_of("android", None) == ""


def test_field_kind():
    assert vault.field_kind({"label": "Password", "password": True}) == "password"
    assert vault.field_kind({"label": "Contraseña"}) == "password"
    assert vault.field_kind({"label": "Código de verificación"}) == "totp"
    assert vault.field_kind({"label": "", "resource_id": "com.app:id/etOtp"}) == "totp"
    assert vault.field_kind({"label": "", "hint": "One-time code"}) == "totp"


def test_totp_matches_rfc_6238_vectors():
    assert vault.totp_code(SEED, at=59) == "287082"
    assert vault.totp_code(SEED, at=1111111109) == "081804"
    assert vault.totp_code(f"otpauth://totp/x?secret={SEED}&digits=8", at=59) == "94287082"
    sha256 = vault.totp_code(f"otpauth://totp/x?secret={SEED.lower()}&algorithm=SHA256", at=59)
    assert len(sha256) == 6 and sha256.isdigit() and sha256 != "287082"
    assert vault.totp_code("gezd gnbv-gy3t qojq gezd gnbv gy3t qojq", at=59) == "287082"
    with pytest.raises(vault.VaultError):
        vault.totp_code("not base32!!")
    with pytest.raises(vault.VaultError):
        vault.totp_code("")


class FakeSecurity:
    def __init__(self, entries):
        self.entries, self.calls = entries, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw.get("input")))
        if argv[1] == "find-generic-password":
            key = (argv[3], argv[5])
            if key in self.entries:
                return SimpleNamespace(returncode=0, stdout=self.entries[key] + "\n", stderr="")
            return SimpleNamespace(returncode=44, stdout="", stderr="could not be found")
        if argv[1] == "-i":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "delete-generic-password":
            return SimpleNamespace(returncode=0 if (argv[3], argv[5]) in self.entries else 44,
                                   stdout="", stderr="")
        raise AssertionError(argv)


def test_keychain_reads_password_and_generates_code():
    sec = FakeSecurity({("jev-runner", "app.x.com"): "pw!", ("jev-runner-totp", "app.x.com"): SEED})
    kc = vault.Keychain(run=sec, platform="darwin")
    assert kc.password("app.x.com") == "pw!"
    assert kc.password("other.com") is None
    assert kc.totp("app.x.com").isdigit() and len(kc.totp("app.x.com")) == 6
    assert kc.totp("other.com") is None
    assert kc.has("app.x.com") == {"password": True, "totp": True, "username": False}
    sec.entries[("jev-runner-user", "app.x.com")] = "eo@x.com"
    assert kc.username("app.x.com") == "eo@x.com" and kc.has("app.x.com")["username"] is True


def test_keychain_is_inert_off_macos_or_when_disabled():
    sec = FakeSecurity({("jev-runner", "a"): "pw"})
    assert vault.Keychain(run=sec, platform="linux").password("a") is None
    assert vault.Keychain(enabled=False, run=sec, platform="darwin").password("a") is None
    assert sec.calls == []


def test_keychain_put_keeps_the_value_off_the_command_line():
    sec = FakeSecurity({})
    kc = vault.Keychain(run=sec, platform="darwin")
    kc.put(vault.SERVICE, "app.x.com", 'p"w\\d')
    argv, stdin = sec.calls[-1]
    assert argv == ["security", "-i"] and 'p"w' not in " ".join(argv)
    assert stdin == 'add-generic-password -U -s "jev-runner" -a "app.x.com" -w "p\\"w\\\\d"\n'
    with pytest.raises(vault.VaultError):
        kc.put(vault.SERVICE, "app.x.com", "")
    assert kc.remove(vault.SERVICE, "nope") is False


# --- the runner: stored secrets are typed, never seen ---

class FakeVault:
    def __init__(self, password=None, totp=None, username=None):
        self._p, self._t, self._u, self.asked = password, totp, username, []

    def username(self, scope):
        self.asked.append(("username", scope))
        return self._u.get(scope) if isinstance(self._u, dict) else self._u

    def password(self, scope):
        self.asked.append(("password", scope))
        return self._p

    def totp(self, scope):
        self.asked.append(("totp", scope))
        return self._t


def _pw_field():
    f = fill("n1", "Password")
    f["password"] = True
    return f


def test_stored_password_is_typed_without_pausing_or_leaking(cfg):
    class Chrome(FakeTarget):
        kind = "chrome"
    t = Chrome([page(_pw_field(), url="https://app.x.com/login"), page(click("n2", "Sign in")), page()])
    v = FakeVault(password="s3cret!")
    r = Runner("s1", t, "sign in", None, cfg, vault=v, choose=scripted_choose(["n1", "n2", "DONE"]),
               field_text=lambda ctx: pytest.fail("secret must never reach the text model"))
    out = r.run()
    assert out["status"] == "done" and v.asked == [("password", "app.x.com")]
    assert any(text == "s3cret!" for _, text in t.acted)  # typed on the device...
    assert "s3cret!" not in json.dumps(out) and "s3cret!" not in json.dumps(r.history)  # ...only
    assert r.history[0]["text"] == "•••" and r.history[0]["value_source"] == "keychain"


def test_code_field_gets_an_authenticator_code(cfg):
    t = FakeTarget([page(fill("n1", "Código"), url="net.bank.app/.Login"), page()])
    v = FakeVault(totp="123456")
    r = Runner("s1", t, "log in", None, cfg, vault=v, choose=scripted_choose(["n1", "DONE"]))
    assert r.run()["status"] == "done" and v.asked == [("totp", "net.bank.app")]
    assert r.history[0]["value_source"] == "authenticator"


def test_without_a_stored_entry_the_run_pauses_as_before(cfg):
    t = FakeTarget([page(_pw_field()), page()])
    r = Runner("s1", t, "x", None, cfg, vault=FakeVault(), choose=scripted_choose(["n1", "DONE"]))
    assert r.run()["status"] == "paused_needs_input"


def test_resume_typed_by_human_continues_and_hides_what_the_field_shows(cfg):
    code = fill("n1", "Código")  # plain-text code field: echoes what was typed
    after = fill("n1", "Código")
    after["value"] = "987654"
    t = FakeTarget([page(code), page(after, click("n2", "Ok")), page()])
    r = Runner("s1", t, "x", None, cfg, choose=scripted_choose(["n1", "n2", "DONE"]))
    assert r.run()["status"] == "paused_needs_input"
    assert r.resume().get("status") == "paused_needs_input"  # no value, not typed: still waiting
    t._advance()
    out = r.resume(typed=True)
    assert out["status"] == "done"
    assert r.history[0]["text"] == "•••" and r.history[0]["value_source"] == "human"
    assert "987654" not in json.dumps(out) and "987654" not in json.dumps(r.history)


def test_identity_fields():
    assert vault.is_identity_field(fill("n1", "Email"))
    assert vault.is_identity_field(fill("n1", "Correo electrónico"))
    assert vault.is_identity_field({"id": "n1", "kind": "fill", "label": "", "resource_id": "com.x:id/userName"})
    assert not vault.is_identity_field(_pw_field())
    assert not vault.is_identity_field(fill("n1", "Verification code"))
    assert not vault.is_identity_field(fill("n1", "Subject"))
    assert not vault.is_identity_field(click("n1", "Email"))


def test_stored_username_is_typed_on_a_signin_page_without_the_text_model(cfg):
    class Chrome(FakeTarget):
        kind = "chrome"
    login = page(fill("e1", "Email"), _pw_field(), url="https://app.x.com/login", text="Sign in\nForgot password?")
    t = Chrome([login, page(_pw_field(), url="https://app.x.com/login", text="Sign in"), page(click("b1", "Continue")), page()])
    v = FakeVault(password="s3cret!", username="eo@x.com")
    r = Runner("s1", t, "sign in", None, cfg, vault=v, choose=scripted_choose(["e1", "n1", "b1", "DONE"]),
               field_text=lambda ctx: pytest.fail("a stored identifier never needs the text model"))
    out = r.run()
    assert out["status"] == "done" and t.acted[0] == ("e1", "eo@x.com") and t.acted[1] == ("n1", "s3cret!")
    assert v.asked[0] == ("username", "app.x.com")
    assert "eo@x.com" not in json.dumps(out) and "eo@x.com" not in json.dumps(r.history)
    assert r.history[0]["value_source"] == "keychain"


def test_identity_provider_page_uses_the_start_hosts_username(cfg):
    class Chrome(FakeTarget):
        kind = "chrome"
    chooser = page(fill("e1", "Email or phone"), url="https://accounts.google.com/v3/signin/identifier", text="Sign in")
    t = Chrome([chooser, page()])
    v = FakeVault(username={"app.x.com": "eo@x.com"})
    r = Runner("s1", t, "sign in with Google", None, cfg, vault=v, start="https://app.x.com/settings",
               choose=scripted_choose(["e1", "DONE"]), field_text=lambda ctx: pytest.fail("no model"))
    assert r.run()["status"] == "done" and t.acted[0] == ("e1", "eo@x.com")
    assert v.asked == [("username", "accounts.google.com"), ("username", "app.x.com")]


def test_an_email_field_off_a_signin_page_is_not_filled_from_the_vault(cfg):
    class Chrome(FakeTarget):
        kind = "chrome"
    t = Chrome([page(fill("e1", "Email"), url="https://app.x.com/contacts/new", text="New contact"), page()])
    v = FakeVault(username="eo@x.com")
    r = Runner("s1", t, "enter 'lead@y.com' in the Email field", None, cfg, vault=v,
               choose=scripted_choose(["e1", "DONE"]), field_text=lambda ctx: pytest.fail("spelled out"))
    assert r.run()["status"] == "done" and t.acted[0] == ("e1", "lead@y.com") and v.asked == []
