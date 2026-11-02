from jev_runner.runner import Runner
from jev_runner.signin import goal_wants_signin, is_signin_page
from tests.fakes import FakeTarget, click, fill, page, scripted_choose


def _login_form(url="https://app.example.com/"):
    pw = fill("p1", "Password")
    pw["password"] = True
    return page(fill("e1", "Email"), pw, click("b1", "Sign in with Google"), url=url,
                text="Sign into your account\nForgot password?")


def test_signin_pages_detected():
    assert is_signin_page(page(url="https://accounts.google.com/v3/signin/accountchooser"))
    assert is_signin_page(page(url="https://login.microsoftonline.com/common/oauth2/authorize"))
    assert is_signin_page(page(url="https://crm.example.com/login?next=/settings"))
    assert is_signin_page(_login_form())  # login at the site root: password field + wording


def test_ordinary_pages_not_signin():
    assert not is_signin_page(page(click("n1", "Users"), url="https://app.example.com/settings/users"))
    pw = fill("p1", "New password")
    pw["password"] = True  # a change-password form is not a sign-in wall
    assert not is_signin_page(page(pw, url="https://app.example.com/profile", text="Update profile"))
    assert not is_signin_page(page(url="https://blog.example.com/catalog-blogin"))


def test_goal_wants_signin():
    assert goal_wants_signin("Click the Sign in with Google button", None)
    assert goal_wants_signin("log in and open Users", None)
    assert goal_wants_signin("abrir", {"password": "x"})
    assert not goal_wants_signin("READ ONLY. Wait for the page to render and stop.", None)


class ChromeTarget(FakeTarget):
    kind = "chrome"


def test_signed_out_chrome_run_stops_without_a_model_call(cfg):
    t = ChromeTarget([_login_form()])
    r = Runner("s1", t, "READ ONLY. Wait for the page to render and stop.", None, cfg,
               choose=scripted_choose(["DONE"]))
    out = r.run()
    assert out["status"] == "blocked" and out["reason"] == "signed_out"
    assert out["model_calls"] == 0 and "sign in" in out["hint"]


def test_goal_that_asks_to_sign_in_proceeds(cfg):
    t = ChromeTarget([_login_form(), page(click("n1", "Users"))])
    r = Runner("s1", t, "Click Sign in with Google, then stop", None, cfg,
               choose=scripted_choose(["b1", "DONE"]))
    assert r.run()["status"] != "blocked"


def test_android_login_screens_unaffected(cfg):
    t = FakeTarget([_login_form(url="net.bank.app/Login"), page()])
    r = Runner("s1", t, "check balance", None, cfg, choose=scripted_choose(["DONE"]))
    assert r.run().get("reason") != "signed_out"
