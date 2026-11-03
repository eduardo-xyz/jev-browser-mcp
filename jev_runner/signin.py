"""Spotting a sign-in wall, so a run on a signed-out browser stops at once with a clear reason.

A signed-out Chrome redirects the requested page to a login screen. Without this check Jev either
wanders on the login form or, for "just wait for the page" goals, reports done on the wrong page.
Signing in (account chooser, password, 2FA) is for a person in that Chrome, unless the goal itself
asks to sign in. Generic signals only: identity-provider hosts, login-shaped paths, and a password
field next to sign-in wording.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_IDP_HOSTS = ("accounts.google.com", "login.microsoftonline.com", "login.live.com",
              "appleid.apple.com", "okta.com", "auth0.com", "onelogin.com")
_LOGIN_PATH = re.compile(r"/(log-?in|sign-?in|sign_in|signin|sso|oauth2?|authorize)(/|$|\.)", re.I)
_SIGNIN_WORDS = re.compile(
    r"\b(sign|log)\s?in\b|iniciar sesi[oó]n|forgot (your )?password|olvid[oó] (su|tu) contrase|"
    r"choose an account|elige una cuenta", re.I)
_GOAL_WANTS = re.compile(
    r"\b(sign|log)[\s-]?(in|on)\b|\blogin\b|\bsignin\b|iniciar sesi[oó]n|\bingres(ar|e)\b|"
    r"password|contrase[nñ]a|\b2fa\b|\botp\b|account chooser|authenticat", re.I)


def _host_path(url: str) -> tuple[str, str]:
    try:
        u = urlsplit(url or "")
    except ValueError:
        return "", ""
    return (u.hostname or "").lower(), u.path or ""


def is_login_url(url: str | None) -> bool:
    """An identity-provider host or a login-shaped path: a URL that is a sign-in page by itself."""
    host, path = _host_path(url or "")
    if any(host == h or host.endswith("." + h) for h in _IDP_HOSTS):
        return True
    return bool(_LOGIN_PATH.search(path))


def is_signin_page(page: dict) -> bool:
    if is_login_url(page.get("url", "")):
        return True
    has_password = any(a.get("password") for a in page.get("actions", []))
    return has_password and bool(_SIGNIN_WORDS.search(page.get("text") or ""))


def goal_wants_signin(goal: str, params: dict | None) -> bool:
    if _GOAL_WANTS.search(goal or ""):
        return True
    return any(_GOAL_WANTS.search(str(k)) for k in (params or {}))
