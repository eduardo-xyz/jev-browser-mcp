"""Stored passwords and authenticator codes, typed by jevd without ever reaching an agent.

Why: a password an agent passes through `resume(input=...)` has already crossed the chat and the
agent's own transcript on disk. With the vault, the value travels from the Mac's Keychain to the
device only. Entries are scoped to the site (Chrome: the page's host) or the app (Android: the
package), so a stored value can only ever be typed into a field on that site or app.

Keychain layout (macOS `security` tool, the login keychain):
  service "jev-runner"       account <scope>   -> the password
  service "jev-runner-totp"  account <scope>   -> a base32 seed or an otpauth:// URI
  service "jev-runner-user"  account <scope>   -> the sign-in identifier (email / username)

CLI (values are prompted, never put on a command line):
  python -m jev_runner.vault set app.example.com        # store a password for that site
  python -m jev_runner.vault set-totp app.example.com   # store its authenticator seed
  python -m jev_runner.vault set-user app.example.com   # store the email/username to sign in with
  python -m jev_runner.vault check app.example.com      # what is stored (never the values)
  python -m jev_runner.vault code app.example.com       # the current 6-digit code, to compare
  python -m jev_runner.vault remove[-totp|-user] <scope>
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit

SERVICE = "jev-runner"
TOTP_SERVICE = "jev-runner-totp"
USER_SERVICE = "jev-runner-user"
_ID_WORDS = re.compile(r"\b(e-?mail|correo|user ?name|usuario|username|user id|login id|identifier|"
                       r"account|phone|telefono|user)\b", re.I)
_OTP_WORDS = re.compile(r"\b(otp|code|codigo|token|verification|verificacion|2fa|one.?time|"
                        r"authenticator|autenticador|totp)\b", re.I)
_NOT_FOUND = 44  # security: "The specified item could not be found in the keychain."


class VaultError(Exception):
    pass


# --- which entry a field maps to -------------------------------------------------------------

def scope_of(kind: str | None, url: str | None) -> str:
    """Chrome: the page's host. Android: the package (the part of `pkg/activity` before '/')."""
    url = url or ""
    if kind == "chrome":
        try:
            return (urlsplit(url).hostname or "").lower()
        except ValueError:
            return ""
    return url.split("/", 1)[0].strip().lower()


def field_kind(action: dict) -> str:
    """'totp' for a one-time-code field (label/hint/id mention a code), else 'password'."""
    from .guards import normalize  # accent/case folding, as the secret-field detection uses
    tail = re.split(r"[/:]", action.get("resource_id") or "")[-1]
    tail = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tail)
    text = " ".join(str(action.get(k) or "") for k in ("label", "hint")) + " " + tail
    return "totp" if _OTP_WORDS.search(normalize(text)) else "password"


def is_identity_field(action: dict) -> bool:
    """A sign-in identifier field (email / username / phone): a plain text field whose label, hint
    or id says so. Never a password or code field."""
    if action.get("kind") != "fill" or action.get("password"):
        return False
    from .guards import normalize
    tail = re.split(r"[/:]", action.get("resource_id") or "")[-1]
    tail = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tail)
    text = " ".join(str(action.get(k) or "") for k in ("label", "hint")) + " " + tail
    text = normalize(text)
    return bool(_ID_WORDS.search(text)) and not _OTP_WORDS.search(text) and "password" not in text


# --- TOTP (RFC 6238), no third-party dependency -------------------------------------------------

def parse_seed(seed: str) -> tuple[bytes, int, int, str]:
    """(key, digits, period, algorithm) from a base32 seed or an otpauth://totp/... URI."""
    seed = (seed or "").strip()
    digits, period, algo = 6, 30, "SHA1"
    if seed.lower().startswith("otpauth://"):
        u = urlsplit(seed)
        q = {k.lower(): v[0] for k, v in parse_qs(u.query).items()}
        seed = q.get("secret", "")
        digits = int(q.get("digits", digits))
        period = int(q.get("period", period))
        algo = q.get("algorithm", algo).upper()
    raw = re.sub(r"[\s-]", "", seed).upper()
    if not raw:
        raise VaultError("empty seed")
    try:
        key = base64.b32decode(raw + "=" * (-len(raw) % 8), casefold=True)
    except Exception:
        raise VaultError("seed is not base32") from None
    if algo not in ("SHA1", "SHA256", "SHA512") or not 6 <= digits <= 8 or period <= 0:
        raise VaultError("unsupported otpauth parameters")
    return key, digits, period, algo


def totp_code(seed: str, at: float | None = None) -> str:
    key, digits, period, algo = parse_seed(seed)
    counter = int((time.time() if at is None else at) // period)
    mac = hmac.new(key, struct.pack(">Q", counter), getattr(hashlib, algo.lower())).digest()
    offset = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


# --- the Keychain ---------------------------------------------------------------------------

class Keychain:
    """Reads (and, for the CLI, writes) jev-runner entries in the macOS login keychain."""

    def __init__(self, enabled: bool = True, run=subprocess.run, platform: str = sys.platform):
        self.enabled = enabled and platform == "darwin"
        self._run = run

    def _get(self, service: str, account: str) -> str | None:
        if not self.enabled or not account:
            return None
        try:
            res = self._run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
                            capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        if res.returncode != 0:
            return None
        value = (res.stdout or "").rstrip("\n")
        return value or None

    def password(self, scope: str) -> str | None:
        return self._get(SERVICE, scope)

    def totp(self, scope: str) -> str | None:
        seed = self._get(TOTP_SERVICE, scope)
        return totp_code(seed) if seed else None

    def username(self, scope: str) -> str | None:
        return self._get(USER_SERVICE, scope)

    def has(self, scope: str) -> dict:
        return {"password": self._get(SERVICE, scope) is not None,
                "totp": self._get(TOTP_SERVICE, scope) is not None,
                "username": self._get(USER_SERVICE, scope) is not None}

    def put(self, service: str, account: str, value: str) -> None:
        """Store through `security -i` (commands on stdin): the value never appears in a process
        argument list. -U updates an existing entry."""
        if not self.enabled:
            raise VaultError("the vault needs macOS (Keychain)")
        if not account or "\n" in account:
            raise VaultError("bad scope")
        if not value or "\n" in value:
            raise VaultError("bad value")
        q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'  # noqa: E731
        cmd = f"add-generic-password -U -s {q(service)} -a {q(account)} -w {q(value)}\n"
        res = self._run(["security", "-i"], input=cmd, capture_output=True, text=True, timeout=30)
        if res.returncode != 0 or "error" in (res.stderr or "").lower():
            raise VaultError(f"keychain write failed: {(res.stderr or '').strip()[:120]}")

    def remove(self, service: str, account: str) -> bool:
        if not self.enabled:
            raise VaultError("the vault needs macOS (Keychain)")
        res = self._run(["security", "delete-generic-password", "-s", service, "-a", account],
                        capture_output=True, text=True, timeout=15)
        if res.returncode == _NOT_FOUND:
            return False
        if res.returncode != 0:
            raise VaultError(f"keychain delete failed: {(res.stderr or '').strip()[:120]}")
        return True


# --- CLI ------------------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    import argparse
    import getpass

    p = argparse.ArgumentParser(prog="python -m jev_runner.vault", description=__doc__.split("\n\n")[0])
    p.add_argument("command", choices=["set", "set-totp", "set-user", "check", "code", "remove",
                                       "remove-totp", "remove-user"])
    p.add_argument("scope", help="site host (app.example.com) or Android package (com.whatsapp)")
    a = p.parse_args(argv)
    kc = Keychain()
    scope = a.scope.strip().lower()
    try:
        if a.command == "set":
            v1 = getpass.getpass(f"Password for {scope}: ")
            if v1 != getpass.getpass("Again: "):
                print("They differ; nothing stored.", file=sys.stderr)
                return 2
            kc.put(SERVICE, scope, v1)
            print(f"Stored a password for {scope}.")
        elif a.command == "set-totp":
            seed = getpass.getpass(f"Authenticator seed or otpauth:// URI for {scope}: ")
            parse_seed(seed)  # validate before storing
            kc.put(TOTP_SERVICE, scope, seed.strip())
            print(f"Stored an authenticator seed for {scope}. Current code: {totp_code(seed)} "
                  "(compare it with your authenticator app).")
        elif a.command == "set-user":
            user = input(f"Email or username to sign in to {scope} with: ").strip()
            kc.put(USER_SERVICE, scope, user)
            print(f"Stored a sign-in identifier for {scope}.")
        elif a.command == "check":
            h = kc.has(scope)
            print(f"{scope}: username {'yes' if h['username'] else 'no'}, "
                  f"password {'yes' if h['password'] else 'no'}, "
                  f"authenticator {'yes' if h['totp'] else 'no'}")
        elif a.command == "code":
            code = kc.totp(scope)
            print(code if code else f"No authenticator seed stored for {scope}.")
        elif a.command == "remove":
            print("Removed." if kc.remove(SERVICE, scope) else "Nothing stored.")
        elif a.command == "remove-totp":
            print("Removed." if kc.remove(TOTP_SERVICE, scope) else "Nothing stored.")
        elif a.command == "remove-user":
            print("Removed." if kc.remove(USER_SERVICE, scope) else "Nothing stored.")
    except VaultError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
