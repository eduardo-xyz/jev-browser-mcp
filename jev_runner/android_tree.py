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
        self.pkg = a.get("package", "")
        self.cls = a.get("class", "")
        self.rid = a.get("resource-id", "")
        self.password = a.get("password") == "true"
        # a password field's text is the secret itself (or its mask): never surface it
        self.text = "" if self.password else _clean(a.get("text"))
        self.desc = _clean(a.get("content-desc"))
        self.hint = _clean(a.get("hint"))
        self.bounds = _bounds(a.get("bounds"))
        self.scrollable = a.get("scrollable") == "true"
        self.included = False
        self.children = []
        self.parent = None

    def flag(self, k):
        return self.el.get(k) == "true"


def _walk(el, pkg, out):
    n = _N(el)
    n.included = el.get("package") in pkg
    out.append(n)
    kids = []
    for c in el.findall("node"):
        k = _walk(c, pkg, out)
        k.parent = n
        kids.append(k)
    n.children = kids
    return n


def _is_webview(n):
    return n.cls.endswith("WebView")


def _clickable(n):
    return n.flag("clickable") or n.flag("long-clickable") or n.flag("checkable")


def _webview_link(n):
    """Plain text inside a WebView with no clickable ancestor (below the WebView).

    Web content often handles taps in JS on a non-clickable node (e.g. a footer menu
    item), so uiautomator exposes it only as a TextView; offer it as a link.
    """
    if not (n.text or n.desc) or n.el.get("visible-to-user") == "false":
        return False
    p = n.parent
    while p is not None and not _is_webview(p):
        if _clickable(p):
            return False  # its text is already part of the clickable ancestor's label
        p = p.parent
    return p is not None


def _contains(outer, inner):
    return (outer[0] <= inner[0] and outer[1] <= inner[1]
            and outer[2] >= inner[2] and outer[3] >= inner[3])


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


SYSTEMUI = "com.android.systemui"
_SHADE_FRACTION = 0.4


def _union_area(rects):
    rects = [r for r in rects if r[2] > r[0] and r[3] > r[1]]
    if not rects:
        return 0
    xs = sorted({v for r in rects for v in (r[0], r[2])})
    area = 0
    for x0, x1 in zip(xs, xs[1:]):
        spans = sorted((r[1], r[3]) for r in rects if r[0] <= x0 and r[2] >= x1)
        cur_end, covered = None, 0
        start = None
        for a, b in spans:
            if cur_end is None or a > cur_end:
                if cur_end is not None:
                    covered += cur_end - start
                start, cur_end = a, b
            else:
                cur_end = max(cur_end, b)
        if cur_end is not None:
            covered += cur_end - start
        area += covered * (x1 - x0)
    return area


def _shade_open(nodes):
    all_rects = [n.bounds for n in nodes if n.bounds[2] > n.bounds[0]]
    if not all_rects:
        return False
    screen = max(r[2] for r in all_rects) * max(r[3] for r in all_rects)
    if screen <= 0:
        return False
    sysui = [n.bounds for n in nodes if n.pkg == SYSTEMUI]
    return _union_area(sysui) > _SHADE_FRACTION * screen


def parse_hierarchy(xml: str, package: str, activity: str, include_system_ui: bool = False) -> dict:
    url = f"{package}/{activity}"
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        root = ET.Element("hierarchy")
    pkgs = {package} | SYSTEM_DIALOG_PACKAGES
    if include_system_ui:
        pkgs.add(SYSTEMUI)
    nodes: list[_N] = []
    for top in root.findall("node"):
        _walk(top, pkgs, nodes)
    inc = [n for n in nodes if n.included]
    shade_open = _shade_open(nodes)

    # page text
    seen, lines = set(), []
    for n in inc:
        for t in (n.text, n.desc):
            if t and t not in seen:
                seen.add(t)
                lines.append(t)
    text = "\n".join(lines)[:_TEXT_CAP]

    actions = []
    text_nodes = []  # (bottom, text, index, left, right) of preceding included text nodes
    synthetic = []  # WebView text links emitted so far (own id namespace: w0, w1, ...)
    real = 0  # element actions emitted so far (ids n0, n1, ...)
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
            and (_clickable(n) or is_edit)
        )
        link = (not actionable and n.flag("enabled") and r > l and b > t
                and not _is_webview(n) and _webview_link(n))
        if actionable:
            tail = _rid_tail(n.rid)
            label = n.text or n.desc or n.hint or _desc_text(n)
            if not label and is_edit:
                # a placeholder drawn on top of the field (e.g. Compose hint text) names it best
                label = next((o.text or o.desc for o in inc
                              if o is not n and (o.text or o.desc) and _contains(n.bounds, o.bounds)), "")
            if not label and is_edit:
                cands = [
                    (t - bb, tx)
                    for bb, tx, idx, tl, tr in text_nodes
                    if idx < i and 0 <= t - bb <= _ABOVE_PX and tl < r and tr > l  # above and overlapping
                ]
                if cands:
                    label = min(cands, key=lambda c: c[0])[1]
                    if tail:
                        label = f"{label} · {tail}"
            if not label:
                label = tail
            unlabelled = not label
            if unlabelled:
                # nothing names it (common for Compose images/cards); say what and how big it is
                kind = "image" if "Image" in n.cls else "element"
                label = f"Unlabelled {kind} ({r - l}x{b - t} px)"
            role = _ROLES.get(n.cls.rsplit(".", 1)[-1], "button")
            act = {
                "id": f"n{real}",
                "node": real,
                "kind": "fill" if is_edit else "click",
                "label": label,
                "role": role,
                "value": "" if (not is_edit or n.password) else n.text,
                "resource_id": n.rid,
                "bounds": n.bounds,
                "password": n.password,
            }
            if n.hint:
                act["hint"] = n.hint
            if unlabelled:
                act["unlabelled"] = True
            real += 1
            if n.flag("checkable"):
                act["checked"] = "true" if n.flag("checked") else "false"
            actions.append(act)
        elif link and not any(
            a["label"] == (n.text or n.desc) and _contains(a["bounds"], n.bounds)
            for a in synthetic
        ):
            # separate ids: a link appearing never shifts the ids of real elements
            synthetic.append({
                "id": f"w{len(synthetic)}",
                "node": f"w{len(synthetic)}",
                "kind": "click",
                "label": n.text or n.desc,
                "role": "link",
                "value": "",
                "resource_id": n.rid,
                "bounds": n.bounds,
                "password": False,
                # synthesized from plain text: may be dynamic (countdowns, balances),
                # so it never feeds the fingerprint
                "synthetic": True,
            })
            actions.append(synthetic[-1])
        own = n.text or n.desc
        if own and not (actionable and is_edit):
            text_nodes.append((b, own, i, l, r))

    actions.append({"id": "back", "kind": "back", "label": "Go back"})
    actions.append({"id": "wait", "kind": "wait", "label": "Wait for the screen to update"})
    actions.append({"id": "home", "kind": "home", "label": "Go to the home screen"})
    actions.append({"id": "recents", "kind": "recents", "label": "Show recent apps"})
    actions.append({"id": "notifications", "kind": "notifications",
                    "label": "Open the notification shade"})
    if scroll:
        actions.append({"id": "scroll_down", "kind": "scroll", "label": "Scroll down", "delta": 1})
        actions.append({"id": "scroll_up", "kind": "scroll", "label": "Scroll up", "delta": -1})

    h = hashlib.sha256(url.encode())
    for a in actions:
        if "bounds" in a and not a.get("synthetic"):
            h.update(repr((a["label"], a["role"], a["bounds"])).encode())
    readable = bool(text) or any(a["label"] for a in actions if "bounds" in a and not a.get("unlabelled"))
    return {
        "url": url,
        "title": package,
        "text": text,
        "actions": actions,
        "fingerprint": h.hexdigest(),
        "readable": readable,
        "shade_open": shade_open,
    }
