"""Convert a uiautomator2 XML hierarchy dump into the page dict Jev consumes."""
import hashlib
import re
import xml.etree.ElementTree as ET

SYSTEM_DIALOG_PACKAGES = {
    "android",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
}
_PUA_NBSP = re.compile("[- ]")
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_ROLES = {
    "Button": "button",
    "ImageButton": "button",
    "EditText": "textbox",
    "CheckBox": "checkbox",
    "Switch": "switch",
    "RadioButton": "radio",
}
_TEXT_CAP = 6000
_ABOVE_PX = 250


def _clean(s):
    return " ".join(_PUA_NBSP.sub(" ", s or "").split())


def _bounds(s):
    m = _BOUNDS.fullmatch((s or "").strip())
    return [int(g) for g in m.groups()] if m else [0, 0, 0, 0]


def _rid_tail(rid):
    return re.split(r"[/:]", rid)[-1] if rid else ""


class _N:
    def __init__(self, el):
        a = el.attrib
        self.el = el
        self.cls = a.get("class", "")
        self.rid = a.get("resource-id", "")
        self.text = _clean(a.get("text"))
        self.desc = _clean(a.get("content-desc"))
        self.hint = _clean(a.get("hint"))
        self.bounds = _bounds(a.get("bounds"))
        self.password = a.get("password") == "true"
        self.scrollable = a.get("scrollable") == "true"
        self.included = False
        self.children = []

    def flag(self, k):
        return self.el.get(k) == "true"


def _walk(el, pkg, out):
    n = _N(el)
    n.included = el.get("package") in pkg
    out.append(n)
    kids = []
    for c in el.findall("node"):
        kids.append(_walk(c, pkg, out))
    n.children = kids
    return n


def _desc_text(n):
    parts = []
    for c in n.children:
        if c.included:
            if c.text:
                parts.append(c.text)
            elif c.desc:
                parts.append(c.desc)
        t = _desc_text(c)
        if t:
            parts.append(t)
    return " ".join(parts)


def parse_hierarchy(xml: str, package: str, activity: str) -> dict:
    url = f"{package}/{activity}"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        root = ET.Element("hierarchy")
    pkgs = {package} | SYSTEM_DIALOG_PACKAGES
    nodes: list[_N] = []
    for top in root.findall("node"):
        _walk(top, pkgs, nodes)
    inc = [n for n in nodes if n.included]

    # page text
    seen, lines = set(), []
    for n in inc:
        for t in (n.text, n.desc):
            if t and t not in seen:
                seen.add(t)
                lines.append(t)
    text = "\n".join(lines)[:_TEXT_CAP]

    actions = []
    text_nodes = []  # (bottom, text) of preceding included text nodes
    scroll = False
    for i, n in enumerate(nodes):
        if not n.included:
            continue
        scroll = scroll or n.scrollable
        l, t, r, b = n.bounds
        is_edit = n.cls.endswith("EditText")
        actionable = (
            n.flag("enabled")
            and r > l
            and b > t
            and (n.flag("clickable") or n.flag("long-clickable") or n.flag("checkable") or is_edit)
        )
        if actionable:
            tail = _rid_tail(n.rid)
            label = n.text or n.desc or n.hint or _desc_text(n)
            if not label and is_edit:
                cands = [
                    (t - bb, tx)
                    for bb, tx, idx in text_nodes
                    if idx < i and 0 <= t - bb <= _ABOVE_PX
                ]
                if cands:
                    label = min(cands, key=lambda c: c[0])[1]
                    if tail:
                        label = f"{label} · {tail}"
            if not label:
                label = tail
            role = _ROLES.get(n.cls.rsplit(".", 1)[-1], "button")
            act = {
                "id": f"n{len(actions)}",
                "node": len(actions),
                "kind": "fill" if is_edit else "click",
                "label": label,
                "role": role,
                "value": "" if (not is_edit or n.password) else n.text,
                "resource_id": n.rid,
                "bounds": n.bounds,
                "password": n.password,
            }
            if n.flag("checkable"):
                act["checked"] = "true" if n.flag("checked") else "false"
            actions.append(act)
        own = n.text or n.desc
        if own and not (actionable and is_edit):
            text_nodes.append((b, own, i))

    actions.append({"id": "back", "kind": "back", "label": "Go back"})
    actions.append({"id": "wait", "kind": "wait", "label": "Wait for the screen to update"})
    if scroll:
        actions.append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 1})
        actions.append({"id": "scroll_up", "kind": "scroll", "label": "Scroll up", "delta": -1})

    h = hashlib.sha256(url.encode())
    for a in actions:
        if "bounds" in a:
            h.update(repr((a["label"], a["role"], a["bounds"])).encode())
    readable = bool(text) or any(a["label"] for a in actions if "bounds" in a)
    return {
        "url": url,
        "title": package,
        "text": text,
        "actions": actions,
        "fingerprint": h.hexdigest(),
        "readable": readable,
    }
