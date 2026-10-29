"""Money and secret guards: decide when Jev must pause or be kept blind."""
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .config import Config

_NUMBER = re.compile(r"\d[\d.,\s]*\d|\d")
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
    labels = {normalize(label) for label in cfg.money_labels}
    return guarded and normalize(action.get("label") or "") in labels


def parse_amount(s: str | int | float) -> Decimal | None:
    if isinstance(s, (int, float)):
        return Decimal(str(s))
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


def _recipient_ok(text: str, forms: list[str]) -> bool:
    masked = _MASKED.findall(text)
    if masked:
        digits = ["".join(c for c in f if c.isdigit()) for f in forms]
        return any(d.endswith(m) for d in digits for m in masked)
    norm = normalize(text)
    return any(n and n in norm for n in map(normalize, forms))


def check_money(text: str, money_params: dict) -> MoneyCheck:
    expected: dict = {}
    seen: dict = {}
    ok = True
    for key, value in money_params.items():
        if key == "amount":
            want = parse_amount(value)
            found = _amount_seen(text)
            good = want is not None and want in found
            expected[key], seen[key] = str(want), [str(a) for a in found]
        elif key == "recipient":
            forms = [value] if isinstance(value, str) else list(value)
            good = _recipient_ok(text, forms)
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
    if "text" in out:
        out["text"] = clean(out["text"])
    out["actions"] = [
        {**a, **{k: clean(a[k]) for k in ("value", "label") if k in a}}
        for a in page.get("actions", [])
    ]
    return out
