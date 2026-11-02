"""ChromeTarget: drives a Chrome tab through upstream Browser Harness, recovering blank/hung tabs."""
import base64
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from .. import tab_groups
from .base import StalePage, TargetBlocked

_log = logging.getLogger("jevd")

_TAB_ACTIVITY_EVERY = 30.0  # seconds between broker keep-alive pings for a claimed tab

LOAD_TIMEOUT = 15.0
_POLL = 0.25
_LOADED_JS = (
    "document.readyState === 'complete' && "
    "!!document.body && document.body.innerText.trim().length > 0"
)

# Iframe node ids come from the frame's own __jevFast registry (small ints) and collide with the top
# frame's ids. The model's action_space dedups elements by node, so a merged iframe node must be made
# globally unique; the real in-frame node is kept as `frame_node` for act() to resolve against.
_FRAME_NODE_BASE = 10_000_000

_monotonic = time.monotonic
_sleep = time.sleep

READ_LIMIT = 20000  # cap merged read text so a huge page can't blow up a result

# Read-only text + actionable labels of one frame. Never writes, never assigns node ids: this is
# the "list/read" path, not the act path. innerText stops at iframe boundaries, which is exactly
# why a cross-origin (out-of-process) iframe must be read in its own CDP session and merged in.
_READ_TEXT_JS = """(() => {
  const vis = e => { try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); } catch (_) { return true; } };
  const seen = new Set(), labels = [], texts = [];
  // innerText stops at EVERY iframe boundary, even same-origin, so recurse into frames we can
  // reach. Cross-origin frames throw on contentDocument and are read via their own CDP target.
  const scan = (doc, depth) => {
    try {
      const t = doc.body ? doc.body.innerText.trim() : "";
      if (t) texts.push(t);
      for (const e of doc.querySelectorAll('button,[role="button"],a[href],[role="menuitem"],[role="link"],[role="tab"]')) {
        if (!vis(e)) continue;
        const l = (e.innerText || e.getAttribute("aria-label") || "").trim().replace(/\\s+/g, " ").slice(0, 80);
        if (l && !seen.has(l)) { seen.add(l); labels.push(l); }
      }
      if (depth < 5) for (const f of doc.querySelectorAll("iframe")) {
        let d = null;
        try { d = f.contentDocument; } catch (_) { d = null; }
        if (d) scan(d, depth + 1);
      }
    } catch (_) {}
  };
  scan(document, 0);
  return {text: texts.join("\\n").trim(), labels: labels.slice(0, 100)};
})()"""


# Origins of this page's own cross-origin iframe srcs. The <iframe> element lives in the top
# document even when its content runs out-of-process, so this is how we discover which iframe
# targets belong to this page (the parent frame tree omits out-of-process children).
_CHILD_ORIGINS_JS = """(() => {
  const out = new Set();
  for (const f of document.querySelectorAll('iframe')) {
    try {
      const o = new URL(f.src, location.href).origin;
      if (o && o !== 'null' && o !== location.origin) out.add(o);
    } catch (_) {}
  }
  return [...out];
})()"""


# Resolve an observed node inside its frame, returning the element's centre in that frame's own
# viewport coords, or null if it moved/vanished/is covered (so act can raise StalePage before any
# input). Mirrors upstream's pre-input hit-test, but runs in the iframe's CDP session.
_RESOLVE_IN_FRAME_FN = """(node => {
  const e = window.__jevFast && window.__jevFast.nodes.get(node);
  if (!e || !e.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]')) return null;
  try { if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return null; } catch (_) {}
  const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
  if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
  if (!e.contains(document.elementFromPoint(x, y))) return null;
  return {x, y};
})"""

# Position of this page's own <iframe> for `origin`, in the TOP viewport. An OOPIF's element lives
# in the top document, so adding this offset to the in-frame centre gives top-viewport coords the
# compositor routes into the iframe. (First match by origin; one content iframe per page in practice.)
_IFRAME_OFFSET_FN = """(origin => {
  for (const f of document.querySelectorAll('iframe')) {
    try {
      if (new URL(f.src, location.href).origin === origin) {
        const r = f.getBoundingClientRect();
        return {x: r.x, y: r.y, w: r.width, h: r.height};
      }
    } catch (_) {}
  }
  return null;
})"""


def _origin(url: str) -> str | None:
    from urllib.parse import urlsplit
    try:
        s = urlsplit(url)
    except ValueError:
        return None
    return f"{s.scheme}://{s.netloc}" if s.scheme and s.netloc else None


def _cdp(method, **params):
    """Raw CDP through Browser Harness; imported lazily and indirected here so tests can stub it."""
    from browser_harness.helpers import cdp
    return cdp(method, **params)

# Upstream's snapshot hides <input type=password> (snapshot.js: safe()), so a login form can't be
# completed. We register each visible password input into upstream's own node map (at a high id, to
# avoid colliding with its small ids) and expose it as a secret fill, which upstream act() can type
# into. The value is never read back, so it never reaches the model, logs or the page fingerprint.
_PASSWORD_JS = """(() => {
  const c = window.__jevFast;
  if (!c || !c.nodes) return [];
  const out = [], base = 1000000;
  for (const e of document.querySelectorAll('input[type="password"]')) {
    const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    if (e.disabled || e.readOnly) continue;
    try { if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue; } catch (_) {}
    const id = base + out.length;
    c.nodes.set(id, e);
    const label = (e.getAttribute('aria-label') || e.getAttribute('placeholder') ||
      (e.labels && e.labels[0] && e.labels[0].innerText) || e.name || 'Password').trim();
    out.push({node: id, label});
  }
  return out;
})()"""


# Buttons inside a modal/overlay (dialogs, promo popovers, cookie banners). Heavy SPA pages blow
# past upstream's 250-action cap, so a dialog's "Not now"/"Close"/"Confirm" can be dropped and the
# agent gets stuck behind an overlay it can't dismiss. We register these into upstream's own node
# map (high ids, no collision) and expose them as clicks, so an overlay is always actionable.
_OVERLAY_JS = """(() => {
  const c = window.__jevFast;
  if (!c || !c.nodes) return [];
  const vis = e => { try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); } catch (_) { return true; } };
  const inOverlay = e => {
    for (let n = e; n && n !== document.body; n = n.parentElement) {
      if (n.getAttribute && (n.getAttribute('role') === 'dialog' || n.getAttribute('aria-modal') === 'true')) return true;
      const s = getComputedStyle(n);
      if ((s.position === 'fixed' || s.position === 'absolute') && (parseInt(s.zIndex) || 0) >= 1000) return true;
    }
    return false;
  };
  const out = [], base = 2000000, seen = new Set();
  for (const e of document.querySelectorAll('button,[role="button"],a[role="button"],[role="menuitem"]')) {
    if (!vis(e) || !inOverlay(e) || e.disabled) continue;
    const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    const label = (e.innerText || e.getAttribute('aria-label') || e.value || '').trim().replace(/\\s+/g, ' ').slice(0, 80);
    if (!label || seen.has(label)) continue;
    seen.add(label);
    const id = base + out.length;
    c.nodes.set(id, e);
    out.push({node: id, label, guard: c.guard ? c.guard(e) : null});
  }
  return out.slice(0, 20);
})()"""


# Icon-only controls (a row's 3-dot menu, a trash icon) have no innerText/aria name, so upstream
# labels them with their bare role ("button") and several of them in a list are indistinguishable:
# the model can't target the right one. For the node ids given, derive a label from generic DOM
# affordances only: the control's own aria-label/title/tooltip/test id, then its icon (a
# descendant's aria-label/title, SVG <title>, <use href="#id">, data-icon, or an icon-library class
# such as "fa-trash"), else aria-haspopup ("menu"), else the role; then append the nearest
# row/item text so the label names the row it acts on ("menu — Allen Brothers"). Read-only: it never
# assigns node ids or changes the page. Returns {node id: label} for nodes it could label.
_ICON_LABEL_FN = """((ids) => {
  const c = window.__jevFast;
  if (!c || !c.nodes) return {};
  const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
  const human = s => clean(String(s || '').replace(/^.*#/, '').replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[-_.:\\/]+/g, ' ')).toLowerCase().replace(/^(?:icons?|ic|svg) /, '');
  const attrs = ['aria-label', 'title', 'data-tooltip', 'data-title', 'data-original-title',
    'data-tip', 'data-testid', 'data-test', 'data-test-id', 'data-qa', 'data-cy'];
  const machine = ['data-testid', 'data-test', 'data-test-id', 'data-qa', 'data-cy'];
  const attrOf = e => {
    for (const a of attrs) {
      const v = clean(e.getAttribute(a));
      if (v) return machine.includes(a) ? human(v) : v;
    }
    return '';
  };
  const iconClass = /^(?:fa[srlbdk]?|mdi|bi|ri|ti|la|las|lar|icon|ion|gg|uil|ph|feather|lucide|tabler|heroicon|material-icons)-(.+)$/;
  const iconOf = e => {
    for (const d of e.querySelectorAll('*')) {
      const own = clean(d.getAttribute('aria-label') || d.getAttribute('title') || d.getAttribute('data-icon'));
      if (own) return human(own);
      if (d.tagName && d.tagName.toLowerCase() === 'title' && clean(d.textContent)) return clean(d.textContent);
      const href = d.getAttribute('href') || d.getAttribute('xlink:href');
      if (d.tagName && d.tagName.toLowerCase() === 'use' && href) return human(href);
      for (const k of (typeof d.className === 'string' ? d.className : (d.className && d.className.baseVal) || '').split(/\\s+/)) {
        const m = iconClass.exec(k);
        if (m && !/^(solid|regular|light|thin|duotone|brands|sharp|fw|lg|xs|sm|[0-9]x|spin)$/.test(m[1])) return human(m[1]);
      }
      const lig = d.tagName && ['i', 'span'].includes(d.tagName.toLowerCase()) && /icon/i.test(d.className || '') ? clean(d.textContent) : '';
      if (lig && /^[a-z_]{3,30}$/.test(lig)) return human(lig);  // material-icons ligature text
    }
    return '';
  };
  const firstLine = (t, skip) => {
    for (const line of String(t || '').split(/[\\n\\t]/)) {
      const s = clean(line);
      if (s && s !== skip && /[\\p{L}\\p{N}]/u.test(s)) return s.slice(0, 50);
    }
    return '';
  };
  const rowOf = e => {
    const row = e.closest('tr,[role="row"],li,[role="listitem"],[role="option"],article');
    if (row) return firstLine(row.innerText);
    let n = e.parentElement;
    for (let i = 0; n && n !== document.body && i < 5; i++, n = n.parentElement) {
      const t = n.innerText || '';
      if (t.length > 300) break;  // reached a container, not a row
      const s = firstLine(t);
      if (s) return s;
    }
    return '';
  };
  const out = {};
  for (const id of ids) {
    const e = c.nodes.get(id);
    if (!e || !e.isConnected) continue;
    const hp = e.getAttribute('aria-haspopup');
    const tag = e.tagName.toLowerCase();
    const what = attrOf(e) || iconOf(e) || (hp && hp !== 'false' ? 'menu' : '') ||
      e.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'button' ? 'button' : 'icon');
    const row = rowOf(e);
    const label = clean(row && row.toLowerCase() !== what.toLowerCase() ? what + ' — ' + row : what).slice(0, 80);
    if (label) out[id] = label;
  }
  return out;
})"""

_UPSTREAM_ROLES = {"button", "link", "checkbox", "radio", "switch", "tab", "menuitem",
                   "menuitemradio", "option", "gridcell", "combobox", "textbox", "searchbox",
                   "spinbutton"}


def _blank_label(action: dict) -> bool:
    """Upstream names an unlabeled control by its bare role; a glyph like "⋮" or "…" is no name either."""
    label = (action.get("label") or "").strip()
    return (label.casefold() in _UPSTREAM_ROLES or label.casefold() == (action.get("role") or "")
            or not any(ch.isalnum() for ch in label))


def _enrich_blank_labels(actions: list, node_key: str, evaluate) -> int:
    """Give icon-only click/fill actions a semantic, row-disambiguated label (see _ICON_LABEL_FN).
    `evaluate(expr)` runs JS in the frame that owns the nodes. Marks each synthesized label with
    `synth_label` (the original stays in `raw_label`) so it's debuggable. Never raises."""
    blank = [a for a in actions if a.get("kind") in ("click", "fill")
             and isinstance(a.get(node_key), int) and _blank_label(a)]
    if not blank:
        return 0
    try:
        found = evaluate(f"{_ICON_LABEL_FN}({json.dumps(sorted({a[node_key] for a in blank}))})")
    except Exception:
        return 0  # never let labelling break a normal observe
    if not isinstance(found, dict):
        return 0
    count = 0
    for a in blank:
        label = found.get(str(a[node_key]))
        if isinstance(label, str) and label.strip() and not _blank_label({**a, "label": label}):
            a["raw_label"], a["label"], a["synth_label"] = a.get("label", ""), label.strip()[:80], True
            count += 1
    return count


# Icon controls that aren't buttons at all: a <div>/<span> with a click handler around an SVG or
# icon font (a row's 3-dot menu often is). Upstream's selector only takes real controls/roles, so
# these never become actions. Find small pointer-cursor (or onclick/tabindex) wrappers of an icon
# with no text of their own, outside any upstream control, visible and on top at their centre;
# register them in upstream's node map (high ids, no collision) with their freshness guard. Bounded.
_ICON_CONTROLS_JS = """(() => {
  const c = window.__jevFast;
  if (!c || !c.nodes) return [];
  const CONTROL = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],[role]';
  const vis = e => { try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); } catch (_) { return true; } };
  const small = r => r.width >= 8 && r.height >= 8 && r.width <= 64 && r.height <= 64;
  const clicky = e => e.hasAttribute('onclick') || (e.tabIndex >= 0 && e.hasAttribute('tabindex')) ||
    getComputedStyle(e).cursor === 'pointer';
  const out = [], seen = new Set(), base = 3000000;
  for (const icon of document.querySelectorAll('svg,i,img,[class*="icon"]')) {
    if (out.length >= 40) break;
    let best = null;
    for (let n = icon, i = 0; n && n !== document.body && i < 4; n = n.parentElement, i++) {
      const r = n.getBoundingClientRect();
      if (!small(r)) break;
      if (clicky(n)) best = n;  // keep climbing: the outermost small clickable wrapper is the control
    }
    if (!best || seen.has(best)) continue;
    seen.add(best);
    if (best.closest(CONTROL) || best.querySelector(CONTROL) || !vis(best)) continue;
    if ((best.innerText || '').replace(/[^\\p{L}\\p{N}]/gu, '').length > 0) continue;  // has a text name: not icon-only
    const r = best.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    if (!best.contains(document.elementFromPoint(x, y))) continue;
    const id = base + out.length;
    c.nodes.set(id, best);
    out.push({node: id, guard: c.guard ? c.guard(best) : null});
  }
  return out;
})()"""


def _icon_control_actions(found, prefix: str) -> list:
    """Actions for discovered icon controls (blank label: _enrich_blank_labels names them)."""
    if not isinstance(found, list):
        return []
    return [{"id": f"{prefix}{i + 1}", "node": f["node"], "kind": "click", "label": "",
             "role": "button", "value": "", "_guard": f.get("guard"), "_icon": True}
            for i, f in enumerate(found) if isinstance(f, dict) and isinstance(f.get("node"), int)]


def _record_guard(page: dict, found: dict) -> None:
    """Upstream's click freshness check compares the element's current guard with
    page["guards"][node], which only holds upstream's own actions. A node we inject must have its
    observe-time guard recorded too, or every click on it is refused as stale (and a real change
    to it must still be caught, so we record the real guard, never a wildcard)."""
    if isinstance(found.get("guard"), list):
        page.setdefault("guards", {})[str(found["node"])] = found["guard"]


def _is_upstream_stale(exc: BaseException) -> bool:
    return any(c.__name__ == "StalePage" for c in type(exc).__mro__)


def loaded(b) -> bool:
    try:
        return bool(b.evaluate(_LOADED_JS))
    except Exception:
        return False


def _wait_loaded(b, timeout: float = LOAD_TIMEOUT) -> bool:
    deadline = _monotonic() + timeout
    while True:
        if loaded(b):
            return True
        if _monotonic() >= deadline:
            return False
        _sleep(_POLL)


def _default_factory(url: str, background: bool):
    """Open a tab via upstream Browser; imported lazily so nothing touches Chrome at import time."""
    from jev_ultrafast.browser import Browser
    from browser_harness.admin import ensure_daemon
    from browser_harness.helpers import cdp

    class _Browser(Browser):
        def __init__(self, url, background):
            ensure_daemon()
            self.target = cdp("Target.createTarget", url="about:blank", background=background)["targetId"]
            self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780,
                      deviceScaleFactor=1, mobile=False)
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.call("Page.navigate", url=url)

    return _Browser(url, background)


class ChromeTarget:
    kind = "chrome"

    def __init__(self, url: str, browser_factory=None):
        self._factory = browser_factory or _default_factory
        self.url = url
        self.browser = self._open(url)
        # Register this tab with AI Broker so the Tab Groups extension names+colours it (best-effort;
        # a no-op without a broker token, and a failure never affects the run).
        self._tab = getattr(self.browser, "target", None)
        self._grouping = bool(self._tab) and tab_groups.enabled()
        self._last_tab_ping = _monotonic()
        if self._tab:
            tab_groups.claim(self._tab)

    def _open(self, url: str):
        b = self._factory(url, True)
        if _wait_loaded(b):
            return b
        try:
            b.call("Page.navigate", url=url)
        except Exception:
            pass
        if _wait_loaded(b):
            return b
        self._close_quiet(b)
        b = self._factory(url, False)
        if _wait_loaded(b):
            return b
        self._close_quiet(b)
        raise TargetBlocked("page_did_not_load")

    @staticmethod
    def _close_quiet(b) -> None:
        try:
            b.close()
        except Exception:
            pass

    def observe(self) -> dict:
        try:
            page = self.browser.observe(screenshot=False)
        except Exception as exc:
            if _is_upstream_stale(exc):
                raise StalePage(str(exc)) from exc
            raise
        self._add_password_fields(page)
        self._add_overlay_actions(page)
        self._add_icon_controls(page)
        n = _enrich_blank_labels(page.get("actions", []), "node", self.browser.evaluate)
        self._drop_unnamed_icons(page.get("actions", []))
        if n and os.environ.get("JEV_DEBUG_IFRAME"):
            _log.info("labels: synthesized %d top-frame label(s)", n)
        self._merge_iframe_actions(page)
        page["readable"] = True
        if self._grouping and _monotonic() - self._last_tab_ping > _TAB_ACTIVITY_EVERY:
            self._last_tab_ping = _monotonic()  # keep the claimed tab alive past the broker's sweep
            tab_groups.activity([self._tab])
        return page

    def _merge_iframe_actions(self, page: dict) -> None:
        """Surface actions from cross-origin (OOPIF) child frames so Jev can choose them. Each gets
        a frame-namespaced id and a `frame` tag; act() routes it to the owning frame's session.
        The money guard is unchanged: it classifies by label in the runner, so an iframe money
        button is guarded exactly like a top-frame one."""
        for frame in getattr(self, "_frames", {}).values():
            self._detach(frame["session"])  # release last observe's frame sessions
        self._frames: dict[int, dict] = {}
        actions = page.setdefault("actions", [])
        existing = {a.get("id") for a in actions}
        try:
            targets = self._iframe_targets()
        except Exception:
            return  # never let iframe discovery break a normal observe
        debug = bool(os.environ.get("JEV_DEBUG_IFRAME"))
        if debug:
            _log.info("iframe: %d cross-origin target(s) discovered; observed url=%s",
                      len(targets), page.get("url"))
        for idx, (session_id, url) in enumerate(targets, start=1):
            snap = self._frame_snapshot(session_id)
            if not isinstance(snap, dict):
                if debug:
                    _log.info("iframe[%d] %s: snapshot not a dict (%s)", idx, url,
                              type(snap).__name__)
                self._detach(session_id)
                continue
            self._frames[idx] = {"session": session_id, "origin": _origin(url)}
            try:
                icons = _icon_control_actions(self._frame_eval(session_id, _ICON_CONTROLS_JS), "ic")
            except Exception:
                icons = []  # never let icon discovery break a normal observe
            for a in icons:
                a.pop("_guard", None)  # in-frame act resolves by node; no upstream freshness check
            raw = list(snap.get("actions", [])) + icons
            synth = _enrich_blank_labels(raw, "node", lambda expr: self._frame_eval(session_id, expr))
            self._drop_unnamed_icons(raw)
            if debug and synth:
                _log.info("iframe[%d] synthesized %d label(s)", idx, synth)
            merged_count = 0
            for a in raw:
                kind = a.get("kind")
                if kind == "wait":
                    continue
                aid = f"f{idx}{a.get('id', '')}"
                if aid in existing:
                    continue
                if kind == "scroll":
                    # the real content scrolls inside the iframe, not the top page; offer the
                    # iframe's own scroll so Jev can reach rows below its fold (act routes it in)
                    existing.add(aid)
                    actions.append({"id": aid, "kind": "scroll", "label": a.get("label", ""),
                                    "delta": a.get("delta", 0), "frame": idx})
                    merged_count += 1
                    continue
                if not isinstance(a.get("node"), int):
                    continue
                existing.add(aid)
                # unique node for the model's per-node dedup; frame_node is the real in-frame id
                merged = {"id": aid, "node": _FRAME_NODE_BASE * idx + a["node"],
                          "frame_node": a["node"], "kind": kind,
                          "label": a.get("label", ""), "role": a.get("role", ""),
                          "value": a.get("value", ""), "frame": idx}
                for key in ("checked", "selected", "expanded", "synth_label", "raw_label"):
                    if key in a:
                        merged[key] = a[key]
                actions.append(merged)
                merged_count += 1
            if debug:
                _log.info("iframe[%d] %s: w=%s h=%s raw_actions=%d merged=%d",
                          idx, url, snap.get("w"), snap.get("h"), len(raw), merged_count)
                _log.info("iframe[%d] merged actions: %s", idx,
                          [(m["id"], m.get("kind"), (m.get("label") or "")[:50])
                           for m in actions if m.get("frame") == idx])
        if self._frames:
            # The upstream fingerprint only covers the top frame, so a change confined to an iframe
            # would read as "no progress" and stall the run. Fold the iframe actions into it so an
            # iframe change is seen as progress. (Top-only pages are left exactly as upstream made them.)
            frame_sig = json.dumps(
                [(a["id"], a.get("label"), a.get("value")) for a in actions if a.get("frame")],
                sort_keys=True)
            page["fingerprint"] = hashlib.sha256(
                (str(page.get("fingerprint", "")) + frame_sig).encode()).hexdigest()

    def _frame_snapshot(self, session_id) -> dict | None:
        from jev_ultrafast.browser import READ_STATE
        try:
            res = _cdp("Runtime.evaluate", session_id=session_id,
                       expression=READ_STATE, returnByValue=True)
        except Exception:
            return None
        if res.get("exceptionDetails"):
            return None
        value = (res.get("result") or {}).get("value")
        return value if isinstance(value, dict) else None

    @staticmethod
    def _frame_eval(session_id, expression):
        res = _cdp("Runtime.evaluate", session_id=session_id, expression=expression,
                   returnByValue=True)
        if res.get("exceptionDetails"):
            return None
        return (res.get("result") or {}).get("value")

    def _add_overlay_actions(self, page: dict) -> None:
        try:
            found = self.browser.evaluate(_OVERLAY_JS)
        except Exception:
            return  # never let overlay discovery break a normal observe
        if not isinstance(found, list) or not found:
            return
        actions = page.setdefault("actions", [])
        existing = {a.get("label") for a in actions}
        for i, f in enumerate(found):
            label = f.get("label")
            if not isinstance(f.get("node"), int) or not label or label in existing:
                continue
            actions.append({"id": f"ov{i + 1}", "node": f["node"], "kind": "click",
                            "label": label, "role": "button", "value": ""})
            existing.add(label)
            _record_guard(page, f)

    def _add_icon_controls(self, page: dict) -> None:
        try:
            found = self.browser.evaluate(_ICON_CONTROLS_JS)
        except Exception:
            return  # never let icon discovery break a normal observe
        actions = page.setdefault("actions", [])
        for a in _icon_control_actions(found, "ic"):
            _record_guard(page, {"node": a["node"], "guard": a.pop("_guard")})
            actions.append(a)

    @staticmethod
    def _drop_unnamed_icons(actions: list) -> None:
        """A discovered icon that labelling couldn't name is noise the model can't target."""
        actions[:] = [a for a in actions if not (a.get("_icon") and not a.get("synth_label"))]
        for a in actions:
            a.pop("_icon", None)

    def _add_password_fields(self, page: dict) -> None:
        try:
            found = self.browser.evaluate(_PASSWORD_JS)
        except Exception:
            return  # never let password discovery break a normal observe
        if not isinstance(found, list) or not found:
            return
        actions = page.setdefault("actions", [])
        existing = {a.get("id") for a in actions}
        for i, f in enumerate(found):
            aid = f"pw{i + 1}"
            if aid in existing or not isinstance(f.get("node"), int):
                continue
            actions.append({"id": aid, "node": f["node"], "kind": "fill",
                            "label": f.get("label") or "Password", "role": "textbox",
                            "value": "", "password": True})

    def read(self, settle_s: float = 12.0, interval_s: float = 1.0) -> dict:
        """Read, re-reading until the merged text stops growing (async iframe content loads late),
        so callers don't have to sleep before reading. Bounded by `settle_s`."""
        best = self._read_once()
        deadline = _monotonic() + settle_s
        while _monotonic() < deadline:
            _sleep(interval_s)
            nxt = self._read_once()
            if len(nxt["text"]) > len(best["text"]):
                best = nxt  # still filling in; keep waiting
            else:
                return best if len(best["text"]) >= len(nxt["text"]) else nxt
        return best

    def _read_once(self) -> dict:
        """Read-only merged text of the top frame and every cross-origin (OOPIF) child frame.

        The normal snapshot only sees the top frame, so pages that render their UI inside a
        cross-origin iframe (e.g. a white-labelled GHL automation screen) look empty. A cross-
        origin iframe is a separate CDP target that does NOT appear in the parent's frame tree,
        but its ``<iframe>`` element IS in the top document, so we match iframe targets by the
        origins of this page's own iframe srcs, read each in its own session, and merge. Content
        is deduped (two tabs showing the same page yield one block). Never clicks or fills.
        """
        parts: list[str] = []
        frames: list[dict] = []
        seen: set[str] = set()
        top = self._block(self._top_read())
        if top:
            parts.append(top)
            seen.add(top)
        for session_id, url in self._iframe_targets():
            try:
                block = self._block(self._frame_text(session_id))
            finally:
                self._detach(session_id)
            if not block or block in seen:
                continue
            seen.add(block)
            frames.append({"url": url, "text": block[:READ_LIMIT]})
            parts.append(f"[frame {url}]\n{block}")
        return {"url": self._location(), "text": "\n".join(parts)[:READ_LIMIT],
                "frames": frames, "readable": True}

    @staticmethod
    def _block(data: dict) -> str:
        text = (data.get("text") or "").strip()
        labels = data.get("labels") or []
        out = []
        if text:
            out.append(text)
        if labels:
            out.append("Actions: " + " | ".join(labels))
        return "\n".join(out)

    def _location(self) -> str:
        try:
            url = self.browser.evaluate("location.href")
        except Exception:
            url = None
        return url if isinstance(url, str) and url else self.url

    def _top_read(self) -> dict:
        try:
            value = self.browser.evaluate(_READ_TEXT_JS)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _frame_text(self, session_id) -> dict:
        try:
            res = _cdp("Runtime.evaluate", session_id=session_id,
                       expression=_READ_TEXT_JS, returnByValue=True)
        except Exception:
            return {}
        if res.get("exceptionDetails"):
            return {}
        value = (res.get("result") or {}).get("value")
        return value if isinstance(value, dict) else {}

    def _child_origins(self) -> set[str]:
        try:
            origins = self.browser.evaluate(_CHILD_ORIGINS_JS)
        except Exception:
            return set()
        return {o for o in origins if isinstance(o, str)} if isinstance(origins, list) else set()

    def _observed_frame_ids(self) -> set[str]:
        """frameIds of the observed tab's own <iframe> elements. The parent knows a child frame's id
        even when the child runs out-of-process, and an OOPIF target's targetId equals its frameId,
        so this lets us pick THIS tab's iframe targets out of the global target list — otherwise a
        different tab showing the same page is merged too, and its actions can't be routed here."""
        try:
            root = self.browser.call("DOM.getDocument", depth=0)["root"]["nodeId"]
            node_ids = self.browser.call(
                "DOM.querySelectorAll", nodeId=root, selector="iframe")["nodeIds"]
        except Exception:
            return set()
        ids = set()
        for nid in node_ids:
            try:
                fid = self.browser.call("DOM.describeNode", nodeId=nid).get("node", {}).get("frameId")
            except Exception:
                continue
            if fid:
                ids.add(fid)
        return ids

    def _iframe_targets(self) -> list[tuple]:
        """(session_id, url) for each cross-origin iframe target that belongs to THIS tab. Prefer a
        precise match by frameId (an OOPIF's targetId == its frameId), falling back to origin match
        when the frameId lookup is unavailable, so the single-tab path is never worse than before."""
        origins = self._child_origins()
        frame_ids = self._observed_frame_ids()
        debug = bool(os.environ.get("JEV_DEBUG_IFRAME"))
        if not origins and not frame_ids and not debug:
            return []
        try:
            targets = _cdp("Target.getTargets").get("targetInfos", [])
        except Exception:
            if debug:
                _log.info("iframe discovery: child_origins=%s frame_ids=%s; Target.getTargets failed",
                          sorted(origins), sorted(frame_ids))
            return []
        iframes = [i for i in targets if i.get("type") == "iframe"]
        by_frame = [i for i in iframes if i.get("targetId") in frame_ids]
        by_origin = [i for i in iframes if _origin(i.get("url") or "") in origins]
        chosen = by_frame or by_origin  # tab-scoped when we can, else origin (prior behavior)
        if debug:
            _log.info("iframe discovery: child_origins=%s frame_ids=%s; iframe targets=%s; matched=%s",
                      sorted(origins), sorted(frame_ids),
                      [(i.get("targetId"), i.get("url")) for i in iframes],
                      "by_frame" if by_frame else ("by_origin" if by_origin else "none"))
        out = []
        for info in chosen:
            try:
                session_id = _cdp("Target.attachToTarget",
                                  targetId=info["targetId"], flatten=True)["sessionId"]
            except Exception:
                continue
            out.append((session_id, info.get("url", "")))
        return out

    @staticmethod
    def _detach(session_id) -> None:
        try:
            _cdp("Target.detachFromTarget", sessionId=session_id)
        except Exception:
            pass

    def fresh(self, page: dict) -> bool:
        try:
            return bool(self.browser.fresh(page))
        except Exception as exc:
            if _is_upstream_stale(exc):
                return False
            raise

    def act(self, action: dict, page: dict, text: str | None = None, verify: bool = False) -> None:
        if action.get("frame") is not None:
            self._act_in_frame(action, text)  # OOPIF: separate path; top-frame/money path unchanged
            return
        # Freshness is checked here, before any input; our StalePage is only ever raised from this check.
        try:
            is_fresh = self.browser.fresh(page, action)
        except Exception as exc:
            if _is_upstream_stale(exc):
                raise StalePage(str(exc)) from exc
            raise
        if not is_fresh:
            raise StalePage("Page changed since this decision. Observe again.")
        try:
            self.browser.act(action, page, text=text)
        except Exception as exc:
            # Upstream raises StalePage only from its pre-input hit-test (target moved or covered),
            # before any input is dispatched: safe to re-observe and retry, not uncertain.
            if _is_upstream_stale(exc):
                raise StalePage(str(exc)) from exc
            # Anything else: input may already have been dispatched; never surface as StalePage.
            raise RuntimeError(f"action_uncertain: {exc}") from exc

    def _act_in_frame(self, action: dict, text: str | None) -> None:
        """Click/fill an element inside a cross-origin iframe by translating its in-frame centre to
        top-viewport coords and dispatching on the top session (the compositor routes by position).
        Resolution runs before any input, so a moved/covered target raises StalePage (retryable);
        once input is dispatched, any failure is action_uncertain."""
        frame = getattr(self, "_frames", {}).get(action.get("frame"))
        if not frame:
            raise StalePage("Frame gone since this decision. Observe again.")
        if action.get("kind") == "scroll":
            offset = self._iframe_offset(frame["origin"])
            if offset is None:
                raise StalePage("Iframe moved since this decision. Observe again.")
            x, y = offset["x"] + offset["w"] / 2, offset["y"] + offset["h"] / 2  # wheel over the iframe
            if os.environ.get("JEV_DEBUG_IFRAME"):
                _log.info("iframe act: kind=scroll delta=%s iframe_offset=%s -> top xy=(%.1f,%.1f)",
                          action.get("delta"), offset, x, y)
            try:
                self.browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y,
                                  deltaX=0, deltaY=action.get("delta", 0))
            except Exception as exc:
                raise RuntimeError(f"action_uncertain: {exc}") from exc
            return
        center = self._resolve_in_frame(frame["session"], action.get("frame_node", action["node"]))
        if center is None:
            raise StalePage("Target changed or is covered. Observe again.")
        offset = self._iframe_offset(frame["origin"])
        if offset is None:
            raise StalePage("Iframe moved since this decision. Observe again.")
        x, y = offset["x"] + center["x"], offset["y"] + center["y"]
        if os.environ.get("JEV_DEBUG_IFRAME"):
            _log.info("iframe act: kind=%s label=%r center=%s iframe_offset=%s -> top xy=(%.1f,%.1f)",
                      action.get("kind"), action.get("label"), center, offset, x, y)
        try:
            self._dispatch(action, x, y, text)
        except Exception as exc:  # input may already be in flight: never a retryable StalePage
            raise RuntimeError(f"action_uncertain: {exc}") from exc

    def _resolve_in_frame(self, session_id, node) -> dict | None:
        try:
            res = _cdp("Runtime.evaluate", session_id=session_id,
                       expression=f"{_RESOLVE_IN_FRAME_FN}({int(node)})", returnByValue=True)
        except Exception:
            return None
        if res.get("exceptionDetails"):
            return None
        value = (res.get("result") or {}).get("value")
        return value if isinstance(value, dict) else None

    def _iframe_offset(self, origin: str | None) -> dict | None:
        if not origin:
            return None
        try:
            value = self.browser.evaluate(f"{_IFRAME_OFFSET_FN}({json.dumps(origin)})")
        except Exception:
            return None
        return value if isinstance(value, dict) else None

    def _dispatch(self, action: dict, x: float, y: float, text: str | None) -> None:
        for event in ("mousePressed", "mouseReleased"):
            self.browser.call("Input.dispatchMouseEvent", type=event, x=x, y=y,
                              button="left", clickCount=1)
        if action.get("kind") == "fill":
            mod = 4 if sys.platform == "darwin" else 2  # Cmd on macOS, Ctrl elsewhere
            self.browser.call("Input.dispatchKeyEvent", type="keyDown", key="a", code="KeyA",
                              modifiers=mod, commands=["selectAll"])
            self.browser.call("Input.dispatchKeyEvent", type="keyUp", key="a", code="KeyA",
                              modifiers=mod)
            self.browser.call("Input.insertText", text=text or "")

    def screenshot(self, path: Path) -> Path:
        data = self.browser.call("Page.captureScreenshot", format="png")["data"]
        path = Path(path)
        path.write_bytes(base64.b64decode(data))
        return path

    def close(self) -> None:
        for frame in getattr(self, "_frames", {}).values():
            self._detach(frame["session"])
        self._close_quiet(self.browser)
