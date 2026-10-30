"""Money and secret guards: decide when Jev must pause or be kept blind."""
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .config import Config

_NUMBER = re.compile(r"\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?")
_MASKED = re.compile(r"[*•xX]{2,}\s?(\d{3,6})")


@dataclass
class MoneyCheck:
    ok: bool
    expected: dict
    seen: dict


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = "".join(" " if unicodedata.category(c)[0] in "PSZC" else c for c in s)
    return " ".join(s.split())


def is_secret_field(action: dict, patterns: list[str]) -> bool:
    if action.get("kind") != "fill":
        return False
    if action.get("password"):
        return True
    tail = re.split(r"[/:]", action.get("resource_id") or "")[-1]
    tail = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", tail)  # etOtp -> et Otp
    parts = (action.get("label") or "", action.get("hint") or "", tail)
    text = f" {' '.join(normalize(p) for p in parts)} "
    return any(f" {p} " in text for p in map(normalize, patterns) if p)


def _host(url: str) -> str:
    return re.split(r"[/?#]", url.split("://", 1)[-1], maxsplit=1)[0]


def _label_matches(label: str, labels: list[str]) -> bool:
    return any(m and (label == m or label.startswith(m + " ")) for m in map(normalize, labels))


def is_money_action(action: dict, page: dict, money_params: dict, cfg: Config) -> bool:
    """Strong labels are guarded everywhere; contextual ones only in a money context."""
    if action.get("kind") != "click":
        return False
    label = normalize(action.get("label") or "")
    if label in {normalize(x) for x in cfg.navigation_labels}:
        return False
    if set(label.split()) & {normalize(w) for w in cfg.money_exclude_words}:
        return False
    if _label_matches(label, cfg.money_labels):
        return True
    host = _host(page.get("url") or "")
    in_context = bool(money_params) or any(
        host == app or host.endswith("." + app) for app in cfg.money_apps
    )
    return in_context and _label_matches(label, cfg.money_labels_contextual)


def parse_amount(s: str | int | float) -> Decimal | None:
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return Decimal(str(s)) if math.isfinite(s) else None
    s = re.sub(r"[^\d.,]", "", s)
    # Only unambiguous shapes: plain digits, thousands groups of one separator, and an
    # optional 2-digit decimal after the other separator. "150.5", ".5", "1.5000": None.
    m = (re.fullmatch(r"(\d+|\d{1,3}(?:\.\d{3})+)(?:,(\d{2}))?", s)
         or re.fullmatch(r"(\d+|\d{1,3}(?:,\d{3})+)(?:\.(\d{2}))?", s))
    if not m:
        return None
    s = re.sub(r"[.,]", "", m[1]) + (f".{m[2]}" if m[2] else "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _amount_seen(text: str) -> list[Decimal]:
    return [a for t in _NUMBER.findall(text) if (a := parse_amount(t)) is not None]


def _digits(s: str) -> str:
    return "".join(c for c in s if c.isdigit())


def _recipient_ok(text: str, forms: list[str], own_accounts: list[str]) -> bool:
    masked = _MASKED.findall(text)
    if masked:
        digits = [_digits(f) for f in forms]
        own = [_digits(a) for a in own_accounts]
        to_recipient = [any(d.endswith(m) for d in digits) for m in masked]
        to_own = [any(o.endswith(m) for o in own) for m in masked]
        return any(to_recipient) and all(r or o for r, o in zip(to_recipient, to_own))
    norm = f" {normalize(text)} "
    runs = set(re.findall(r"\d+", text))
    runs |= set(re.findall(r"\d+", re.sub(r"(?<=\d)[ -](?=\d)", "", text)))
    for form in forms:
        if re.fullmatch(r"[\d\s-]+", form) and _digits(form):
            if _digits(form) in runs:
                return True
        elif (n := normalize(form)) and f" {n} " in norm:
            return True
    return False


def check_money(
    text: str, money_params: dict, own_accounts: list[str] | None = None
) -> MoneyCheck:
    expected: dict = {}
    seen: dict = {}
    ok = True
    for key in ("amount", "recipient"):
        if money_params.get(key) in (None, "", []):
            expected[key], seen[key], ok = "<required>", None, False
    for key, value in money_params.items():
        if value in (None, "", []):
            continue
        if key == "amount":
            want = parse_amount(value)
            found = _amount_seen(text)
            good = want is not None and want in found
            expected[key], seen[key] = str(want), [str(a) for a in found]
        elif key == "recipient":
            forms = [value] if isinstance(value, str) else list(value)
            good = _recipient_ok(text, forms, own_accounts or [])
            expected[key], seen[key] = forms, _MASKED.findall(text)
        else:
            good = normalize(str(value)) in normalize(text)
            expected[key], seen[key] = value, None
        ok = ok and good
        if good:
            expected.pop(key)
            seen.pop(key)
    return MoneyCheck(ok=ok, expected=expected, seen=seen)


def _secret_re(secret: str) -> re.Pattern:
    """Digit edges must not touch other digits, so a short PIN never corrupts other numbers.

    Non-digit edges stay unconstrained so "PIN1234" or a password inside text is still masked.
    """
    head = r"(?<!\d)" if secret[0].isdigit() else ""
    tail = r"(?!\d)" if secret[-1].isdigit() else ""
    return re.compile(head + re.escape(secret) + tail)


def scrub_text(text: str, secrets: list[str]) -> str:
    for s in secrets:
        if s:
            text = _secret_re(s).sub("•••", text)
    return text


def scrub_value(value, secrets: list[str]):
    """Recursively scrub strings (and numbers) inside dicts/lists/tuples."""
    if isinstance(value, dict):
        return {k: scrub_value(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_value(v, secrets) for v in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        s = str(value)
        return "•••" if scrub_text(s, secrets) != s else value
    if isinstance(value, str):
        return scrub_text(value, secrets)
    return value


def scrub(page: dict, secrets: list[str]) -> dict:
    def clean(v):
        return scrub_text(v, secrets) if isinstance(v, str) else v

    out = dict(page)
    for key in ("url", "title", "text", "fingerprint"):  # fingerprint: opaque, but may embed text
        if key in out:
            out[key] = clean(out[key])
    if "actions" in page:
        out["actions"] = [
            {**a, **{k: clean(a[k]) for k in ("value", "label", "hint") if k in a}}
            for a in page["actions"]
        ]
    return out
