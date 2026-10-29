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
    label = f" {normalize(action.get('label') or '')} "
    return any(f" {p} " in label for p in map(normalize, patterns) if p)


def _host(url: str) -> str:
    return re.split(r"[/?#]", url.split("://", 1)[-1], maxsplit=1)[0]


def is_money_action(action: dict, page: dict, money_params: dict, cfg: Config) -> bool:
    if action.get("kind") != "click":
        return False
    host = _host(page.get("url") or "")
    guarded = bool(money_params) or any(
        host == app or host.endswith("." + app) for app in cfg.money_apps
    )
    if not guarded:
        return False
    label = normalize(action.get("label") or "")
    if set(label.split()) & {normalize(w) for w in cfg.money_exclude_words}:
        return False
    return any(label == m or label.startswith(m + " ") for m in map(normalize, cfg.money_labels))


def parse_amount(s: str | int | float) -> Decimal | None:
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return Decimal(str(s)) if math.isfinite(s) else None
    s = re.sub(r"[^\d.,]", "", s)
    if not re.search(r"\d", s):
        return None
    if "." in s and "," in s:
        point = max(s.rfind("."), s.rfind(","))
        s = re.sub(r"[.,]", "", s[:point]) + "." + re.sub(r"[.,]", "", s[point + 1:])
    elif re.fullmatch(r"\d*[.,]\d{2}", s):
        s = s.replace(",", ".")
    else:
        s = re.sub(r"[.,]", "", s)
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


def scrub(page: dict, secrets: list[str]) -> dict:
    def clean(v):
        if isinstance(v, str):
            for s in secrets:
                if s:
                    v = v.replace(s, "•••")
        return v

    out = dict(page)
    for key in ("url", "title", "text"):
        if key in out:
            out[key] = clean(out[key])
    if "actions" in page:
        out["actions"] = [
            {**a, **{k: clean(a[k]) for k in ("value", "label") if k in a}}
            for a in page["actions"]
        ]
    return out
