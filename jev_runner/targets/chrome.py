"""ChromeTarget: drives a Chrome tab through upstream Browser Harness, recovering blank/hung tabs."""
import base64
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

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
      if (n.getAttribute && (['dialog', 'menu', 'listbox'].includes(n.getAttribute('role')) || n.getAttribute('aria-modal') === 'true')) return true;
      const s = getComputedStyle(n);
      if ((s.position === 'fixed' || s.position === 'absolute') && (parseInt(s.zIndex) || 0) >= 1000) return true;
    }
    return false;
  };
  const out = [], base = 2000000, seen = new Set();
  // Dropdown options are often plain divs: take a pointer-cursor element in an overlay whose parent
  // isn't pointer (the option itself, not its inner span), with a short text of its own.
  const pointer = e => e && e.nodeType === 1 && getComputedStyle(e).cursor === 'pointer';
  const CONTROL = 'a[href],button,input,textarea,select,summary,' + ['button', 'link', 'checkbox', 'radio',
    'switch', 'tab', 'menuitem', 'menuitemradio', 'menuitemcheckbox', 'option', 'combobox', 'textbox']
    .map(r => '[role="' + r + '"]').join(',');
  const items = [...document.querySelectorAll('button,[role="button"],a[role="button"],[role="menuitem"]')];
  // Only scan inside overlay roots (portals land on <body>), never the whole page: heavy SPAs.
  const roots = [...document.querySelectorAll('[role="dialog"],[aria-modal="true"],[role="menu"],[role="listbox"]'),
    ...[...document.body.children].filter(n => {
      const s = getComputedStyle(n);
      return (s.position === 'fixed' || s.position === 'absolute') && (parseInt(s.zIndex) || 0) >= 1000;
    })];
  let scanned = 0;
  for (const root of roots) for (const e of root.querySelectorAll('div,span,li,a')) {
    if (++scanned > 2000) break;
    if (!pointer(e) || pointer(e.parentElement) || e.closest(CONTROL) || e.querySelector(CONTROL)) continue;
    const t = (e.innerText || '').trim();
    if (t && t.length <= 40 && !t.includes('\\n') && inOverlay(e)) items.push(e);
  }
  for (const e of items) {
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
# The row or card a control sits in, by its first line of text: the table row, list item or
# article around it, else the nearest small ancestor (a card is a <div>, not a row).
_ROW_HELPERS_JS = """
  // The row's first wordy line names it, except an avatar's initials ("AM" above the person's
  // name): a line of one to three capitals yields to the next line when there is one.
  const firstLine = (t, skip) => {
    let initials = '';
    for (const line of String(t || '').split(/[\\n\\t]/)) {
      const s = clean(line);
      if (!s || s === skip || !/[\\p{L}\\p{N}]/u.test(s)) continue;
      if (/^[A-Z]{1,3}$/.test(s)) { initials = initials || s; continue; }
      return s.slice(0, 50);
    }
    return initials;
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
"""

# Row context for same-named controls (see _disambiguate_duplicates).
_ROW_CONTEXT_FN = """((ids) => {
  const c = window.__jevFast;
  if (!c || !c.nodes) return {};
  const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
""" + _ROW_HELPERS_JS + """
  const out = {};
  for (const id of ids) {
    const e = c.nodes.get(id);
    if (!e || !e.isConnected) continue;
    const row = rowOf(e);
    if (row) out[id] = row;
  }
  return out;
})"""

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
  // An element id like "user-row-edit-shA5FPdRj77vwOG6" names the control once its record key is
  // dropped: keep the word tokens, drop any token that carries a digit or is a long opaque string.
  const idOf = e => {
    const raw = e.id || (e.firstElementChild && e.firstElementChild.id) || '';
    const words = raw.split(/[-_.:\\/\\s]+/).filter(t => t && !/\\d/.test(t) && t.length < 16);
    const name = human(words.join(' '));
    return /[a-z]{3,}/.test(name) ? name : '';
  };
""" + _ROW_HELPERS_JS + """
  const out = {};
  for (const id of ids) {
    const e = c.nodes.get(id);
    if (!e || !e.isConnected) continue;
    const hp = e.getAttribute('aria-haspopup');
    const tag = e.tagName.toLowerCase();
    const what = attrOf(e) || iconOf(e) || idOf(e) || (hp && hp !== 'false' ? 'menu' : '') ||
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


def _disambiguate_duplicates(actions: list, node_key: str, evaluate) -> int:
    """Same-named controls (one "Domain Options" kebab per card, one "Open" per row) are told
    apart by the row or card they sit in: "Domain Options — <the card's first line>". Jev sees
    labels only, so without this a goal naming a card could only pick one of them at random
    (seen: Jev clicked the card's text instead of its menu). The original stays in raw_label.
    The guards still match: a label keeps its own words first. Never raises."""
    groups: dict = {}
    for a in actions:
        if a.get("kind") in ("click", "fill") and isinstance(a.get(node_key), int) \
                and not a.get("synth_label") and (a.get("label") or "").strip():
            groups.setdefault((a.get("kind"), a["label"]), []).append(a)
    dups = [a for g in groups.values() if len(g) > 1 for a in g]
    if not dups:
        return 0
    try:
        found = evaluate(f"{_ROW_CONTEXT_FN}({json.dumps(sorted({a[node_key] for a in dups}))})")
    except Exception:
        return 0  # never let labelling break a normal observe
    if not isinstance(found, dict):
        return 0
    count = 0
    for a in dups:
        row = found.get(str(a[node_key]))
        if isinstance(row, str) and row.strip() and row.strip().lower() != a["label"].strip().lower():
            a["raw_label"], a["label"] = a["label"], f"{a['label']} — {row.strip()}"[:80]
            a["row_label"] = True
            count += 1
    # Still identical after that (several workflow nodes named alike, each its own row): number
    # them, so "the second one" is a control Jev can pick instead of the first one four times over.
    seen: dict = {}
    for a in dups:
        n = seen[(a.get("kind"), a["label"])] = seen.get((a.get("kind"), a["label"]), 0) + 1
        if n > 1:
            a.setdefault("raw_label", a["label"])
            a["label"], a["row_label"] = f"{a['label']} ({n})", True
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
  // Only control roles count as an enclosing control: a table's role=none/treegrid wrappers or a
  // role=main must not hide every icon inside them (seen: a users table whose per-row edit and
  // delete icons were never offered, so Jev wandered off to search the name elsewhere).
  const CONTROL = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],' + ['button', 'link',
    'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemradio', 'menuitemcheckbox', 'option',
    'gridcell', 'combobox', 'textbox', 'searchbox', 'spinbutton'].map(r => '[role="' + r + '"]').join(',');
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


# Clickable text that isn't a control: list/table rows and folder names rendered as a <div>/<span>/<td>
# with a JS click handler (cursor: pointer) and no link/button/role. Upstream's selector skips them,
# so a goal like "open the folder X" finds nothing to click and the model blocks or types into the
# search box. Walk visible text nodes, keep one whose element shows a pointer cursor and isn't inside
# a real control, find where the pointer starts (cursor is inherited: climb while the parent is
# pointer), skip page-sized wrappers, keep the first text per clickable root, and register the
# text's element (its centre is inside the root, so a click there reaches the root's handler).
_POINTER_TEXT_JS = """(() => {
  const c = window.__jevFast;
  if (!c || !c.nodes || !document.body) return [];
  const CONTROL = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],' + ['button', 'link',
    'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemradio', 'menuitemcheckbox', 'option',
    'gridcell', 'combobox', 'textbox', 'searchbox', 'spinbutton'].map(r => '[role="' + r + '"]').join(',');
  const vis = e => { try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); } catch (_) { return true; } };
  const pointer = e => e && e.nodeType === 1 && getComputedStyle(e).cursor === 'pointer';
  const out = [], roots = new Set(), base = 4000000, area = innerWidth * innerHeight;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node, scanned = 0;
  while ((node = walker.nextNode()) && out.length < 60 && ++scanned < 4000) {
    const t = node.textContent.trim();
    if (!t || !/[\\p{L}\\p{N}]/u.test(t)) continue;
    const p = node.parentElement;
    if (!p || p.closest('script,style,noscript,template') || !pointer(p) || p.closest(CONTROL)) continue;
    let root = p;
    for (let i = 0; i < 12 && root.parentElement && root.parentElement !== document.body && pointer(root.parentElement); i++)
      root = root.parentElement;
    if (roots.has(root) || root === document.body || root === document.documentElement) continue;
    const rr = root.getBoundingClientRect();
    if (rr.width * rr.height > area / 2) continue;  // a page-sized pointer wrapper, not a row
    if (!vis(p)) continue;
    const r = p.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    const hit = document.elementFromPoint(x, y);
    if (!hit || !root.contains(hit)) continue;  // covered by something else
    const label = (p.innerText || t).trim().replace(/\\s+/g, ' ').slice(0, 80);
    if (!label || !/[\\p{L}\\p{N}]/u.test(label)) continue;
    roots.add(root);
    const id = base + out.length;
    c.nodes.set(id, p);
    out.push({node: id, label, guard: c.guard ? c.guard(p) : null});
  }
  return out;
})()"""


# File inputs, including hidden ones behind a styled "Upload" button (upstream skips type=file).
# Offered only when the run was given files: choosing one sets the files directly via CDP, so the
# OS file picker never opens. Label: the input's own name, else the nearest short text around it.
# Panels that scroll inside the page. Upstream offers "Scroll down" only when the window itself
# scrolls, but web apps keep the window fixed and scroll a panel (seen: an audit log whose rows
# live in a 546 px container on a page that never scrolls, so the rows below the fold were out of
# reach and Jev wandered into a date picker instead). Each visible panel that can scroll further,
# and that a wheel at its centre would actually move, is offered as a scroll action of its own.
_SCROLLERS_JS = """(() => {
  const c = window.__jevFast;
  if (!c || !c.nodes || !document.body) return [];
  const out = [], base = 6000000;
  const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
""" + _ROW_HELPERS_JS + """
  const vis = e => { try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); } catch (_) { return true; } };
  const scrolls = e => e && e.nodeType === 1 && e !== document.documentElement && e !== document.body &&
    e.scrollHeight > e.clientHeight + 20 && /(auto|scroll)/.test(getComputedStyle(e).overflowY);
  for (const e of document.querySelectorAll('*')) {
    if (out.length >= 4) break;
    if (!scrolls(e) || !vis(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.width < 120 || r.height < 120) continue;
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    let hit = document.elementFromPoint(x, y);
    while (hit && hit !== e && !scrolls(hit)) hit = hit.parentElement;
    if (hit !== e) continue;  // covered, or a panel nested there would take the wheel instead
    const id = base + out.length;
    c.nodes.set(id, e);
    const cls = (typeof e.className === 'string' ? e.className : '').split(/\\s+/)
      .filter(k => k.length >= 4 && !/\\d/.test(k) && !/^(flex|grid|relative|absolute|overflow|block|inline|hidden|w-full|h-full)/.test(k))
      .slice(0, 3).join(' ').replace(/[-_]+/g, ' ');
    out.push({node: id, down: e.scrollTop + e.clientHeight < e.scrollHeight - 2, up: e.scrollTop > 0,
              name: clean(e.getAttribute('aria-label') || cls).slice(0, 40),  // what the app calls it
              head: firstLine(e.innerText).slice(0, 30)});  // and its first line
  }
  // Pannable canvases (a flow/diagram editor): a big clipped surface whose child moves by CSS
  // transform. Nothing scrolls, but a wheel over it pans, so it is offered in all four ways.
  const area = innerWidth * innerHeight;
  for (const e of document.querySelectorAll('div,section,main')) {
    if (out.length >= 6) break;
    const cs = getComputedStyle(e);
    if (!/(hidden|clip)/.test(cs.overflow + ' ' + cs.overflowY) || !vis(e)) continue;
    const r = e.getBoundingClientRect();
    if (r.width * r.height < area * 0.4) continue;
    if (![...e.children].some(k => getComputedStyle(k).transform !== 'none')) continue;
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    const hit = document.elementFromPoint(x, y);
    if (!hit || !e.contains(hit)) continue;  // covered
    const id = base + out.length;
    c.nodes.set(id, e);
    const cls = (typeof e.className === 'string' ? e.className : '').split(/\\s+/)
      .filter(k => k.length >= 4 && !/\\d/.test(k) && !/^(flex|grid|relative|absolute|overflow|block|inline|hidden|w-full|h-full)/.test(k))
      .slice(0, 2).join(' ').replace(/[-_]+/g, ' ');
    out.push({node: id, pan: true, name: clean(e.getAttribute('aria-label') || cls || 'canvas').slice(0, 40)});
  }
  return out;
})()"""


# Focusable widgets that are not controls to the snapshot: a dropdown trigger built as a
# <div tabindex="0"> with no role and no pointer cursor (seen: every property field of a
# Freshdesk ticket, Status among them, is such a trigger). Taken when the element is focusable,
# outside any real control, and either carries popup/owner ARIA attributes or is named like a
# select/dropdown/trigger. Labelled "<field label>: <current text>" ("Status: Pending") so a goal
# can name the field. Registered in upstream's node map with the real guard. Bounded.
_WIDGETS_JS = """(() => {
  const c = window.__jevFast;
  if (!c || !c.nodes || !document.body) return [];
  const CONTROL = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],' + ['button', 'link',
    'checkbox', 'radio', 'switch', 'tab', 'menuitem', 'menuitemradio', 'menuitemcheckbox', 'option',
    'gridcell', 'combobox', 'textbox', 'searchbox', 'spinbutton'].map(r => '[role="' + r + '"]').join(',');
  const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
  const vis = e => { try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); } catch (_) { return true; } };
  const widget = /select|dropdown|trigger|combobox|picker/i;
  const aria = ['aria-haspopup', 'aria-expanded', 'aria-owns', 'aria-controls', 'aria-activedescendant'];
  const fieldOf = e => {
    let n = e.parentElement;
    for (let i = 0; i < 4 && n && n !== document.body; i++, n = n.parentElement) {
      for (const l of n.querySelectorAll('label,[class*="label"]')) {
        if (e.contains(l) || l.contains(e)) continue;
        const t = clean(l.textContent);
        if (t && t.length <= 40) return t;
      }
    }
    // no label element: the short text that precedes the widget in its box ("Unit" above a select)
    n = e.parentElement;
    for (let i = 0; i < 4 && n && n !== document.body; i++, n = n.parentElement) {
      for (const leaf of n.querySelectorAll('*')) {
        if (leaf.children.length || e.contains(leaf) || leaf.contains(e)) continue;
        if (!(leaf.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
        const t = clean(leaf.textContent);
        if (t && t.length <= 24 && /[A-Za-z]/.test(t)) return t;
      }
    }
    return '';
  };
  const out = [], base = 7000000, area = innerWidth * innerHeight;
  for (const e of document.querySelectorAll('[tabindex]')) {
    if (out.length >= 40) break;
    const ti = parseInt(e.getAttribute('tabindex'), 10);
    // not a control upstream takes (its selector is CONTROL); no registry check: c.guard() registers the
    // widget in the node map, which hid it from every read after the first
    if (isNaN(ti) || ti < 0 || e.matches(CONTROL) || e.closest(CONTROL)) continue;
    if (e.getAttribute('role')) continue;  // a role upstream chose not to take
    const cls = typeof e.className === 'string' ? e.className : '';
    if (!aria.some(a => e.hasAttribute(a)) && !widget.test(cls)) continue;
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (!r.width || !r.height || r.width * r.height > area / 4 || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    const hit = document.elementFromPoint(x, y);
    if (!hit || !e.contains(hit)) continue;  // covered
    const text = clean(e.innerText).slice(0, 40), field = fieldOf(e);
    const label = field && text ? field + ': ' + text : (field || text || 'dropdown');
    const id = base + out.length;
    c.nodes.set(id, e);
    out.push({node: id, label, guard: c.guard ? c.guard(e) : null});
  }
  return out;
})()"""


_FILE_INPUTS_JS = """(() => {
  const c = window.__jevFast;
  if (!c || !c.nodes) return [];
  const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
  const out = [], base = 5000000;
  for (const e of document.querySelectorAll('input[type="file"]')) {
    if (e.disabled || out.length >= 10) continue;
    let label = clean(e.getAttribute('aria-label') || [...(e.labels || [])].map(l => l.innerText).join(' ') ||
      e.getAttribute('title') || '');
    for (let n = e.parentElement, i = 0; !label && n && n !== document.body && i < 5; n = n.parentElement, i++) {
      const t = clean(n.innerText);
      if (t && t.length <= 60) label = t;
      else if (t.length > 60) break;
    }
    label = label || clean(e.name || e.id) || 'file';
    const id = base + out.length;
    c.nodes.set(id, e);
    out.push({node: id, label: label.slice(0, 60), accept: e.accept || '', multiple: !!e.multiple});
  }
  return out;
})()"""


def _file_input_actions(found, names: list[str], prefix: str = "up") -> list:
    """'Upload <files> → <input label>' click actions for discovered file inputs."""
    out = []
    for i, f in enumerate(found if isinstance(found, list) else []):
        if not isinstance(f, dict) or not isinstance(f.get("node"), int):
            continue
        what = ", ".join(names) if f.get("multiple") else " / ".join(names)
        out.append({"id": f"{prefix}{i + 1}", "node": f["node"], "kind": "click",
                    "label": f"Upload file {what} → {f.get('label') or 'file'}"[:120],
                    "role": "button", "value": "", "file_input": True,
                    "multiple": bool(f.get("multiple")), "accept": f.get("accept", "")})
    return out


def _pointer_text_actions(found, labels: set, prefix: str = "tx") -> list:
    """Actions for discovered clickable text, skipping labels already offered (each label once)."""
    out = []
    for i, f in enumerate(found if isinstance(found, list) else []):
        label = f.get("label") if isinstance(f, dict) else None
        if not isinstance(label, str) or not label.strip() or label in labels \
                or not isinstance(f.get("node"), int):
            continue
        labels.add(label)
        out.append({"id": f"{prefix}{i + 1}", "node": f["node"], "kind": "click", "label": label,
                    "role": "button", "value": "", "_guard": f.get("guard")})
    return out


def _widget_actions(found, labels: set, prefix: str = "wd") -> list:
    """Click actions for focusable widget triggers (see _WIDGETS_JS), skipping labels already offered."""
    out = []
    for i, f in enumerate(found if isinstance(found, list) else []):
        label = f.get("label") if isinstance(f, dict) else None
        if not isinstance(label, str) or not label.strip() or label in labels \
                or not isinstance(f.get("node"), int):
            continue
        labels.add(label)
        out.append({"id": f"{prefix}{i + 1}", "node": f["node"], "kind": "click", "label": label,
                    "role": "combobox", "value": "", "_guard": f.get("guard")})
    return out


def _scroller_actions(found, prefix: str = "sc") -> list:
    """Scroll actions for panels that scroll inside the page: down and/or up, per panel. The node
    is the panel itself, so act() wheels over it rather than over the window."""
    out = []
    for i, f in enumerate(found if isinstance(found, list) else []):
        if not isinstance(f, dict) or not isinstance(f.get("node"), int):
            continue
        head = f.get("head") if isinstance(f.get("head"), str) else ""
        name = f.get("name") if isinstance(f.get("name"), str) else ""
        what = f"the {name.strip()}" if name.strip() else f"the list{f' {i + 1}' if i else ''}"
        if head.strip():
            what += f" — {head.strip()}"
        if f.get("pan"):
            # a canvas pans in every direction; a wheel of 800 moves it a few hundred px
            for way, dx, dy in (("down", 0, 800), ("up", 0, -800), ("left", -800, 0), ("right", 800, 0)):
                out.append({"id": f"{prefix}{i + 1}{way[0]}", "kind": "scroll", "node": f["node"],
                            "label": f"Pan {what} {way}", "delta": dy, "delta_x": dx,
                            "scroller": True, "pan": True})
            continue
        for way, delta in (("down", 560), ("up", -560)):
            if f.get(way):
                out.append({"id": f"{prefix}{i + 1}{way[0]}", "kind": "scroll", "node": f["node"],
                            "label": f"Scroll {what} {way}", "delta": delta, "scroller": True})
    return out


# Named controls that sit outside the pane of a pannable canvas (a workflow's nodes beyond the
# visible graph). Blind pans overshoot (seen: five "Pan down" past a node near the top), so each
# such control is offered as "Bring <title> into view"; act() pans toward it, measuring as it goes.
# HTML controls only (an SVG edge is not a destination), first of each title, bounded.
_OFFSCREEN_JS = """(() => {
  const c = window.__jevFast;
  if (!c || !c.nodes || !document.body) return [];
  const clean = s => String(s || '').replace(/\\s+/g, ' ').trim();
  const vis = e => { try { return e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true}); } catch (_) { return true; } };
  const CONTROL = 'a[href],button,[role="button"],[role="link"],[role="tab"],[role="menuitem"],[role="option"]';
  const area = innerWidth * innerHeight, out = [], seen = new Set(), base = 8000000;
  let k = 0;
  for (const cv of document.querySelectorAll('div,section,main')) {
    if (k >= 2) break;
    const cs = getComputedStyle(cv);
    if (!/(hidden|clip)/.test(cs.overflow + ' ' + cs.overflowY) || !vis(cv)) continue;
    const cr = cv.getBoundingClientRect();
    if (cr.width * cr.height < area * 0.4) continue;
    if (![...cv.children].some(x => getComputedStyle(x).transform !== 'none')) continue;
    const canvas = base + 900000 + k++;
    c.nodes.set(canvas, cv);
    for (const e of cv.querySelectorAll(CONTROL)) {
      if (out.length >= 300) break;  // the runner keeps 12 for the model; a quoted title must be findable
      if (!(e instanceof HTMLElement) || !vis(e)) continue;
      const outer = e.parentElement && e.parentElement.closest(CONTROL);
      if (outer && cv.contains(outer)) continue;  // a control inside a node card: the card is the destination
      const r = e.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const inside = r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight;
      if (inside) continue;
      // the card's title: its first line with a word in it (a step-number badge may come first)
      const lines = (e.innerText || '').split('\\n').map(clean).filter(Boolean);
      const label = clean(e.getAttribute('aria-label') || lines.find(l => /[A-Za-z]{2,}/.test(l)) || lines[0] || '').slice(0, 40);
      if (!label || seen.has(label)) continue;
      seen.add(label);
      const id = base + out.length;
      c.nodes.set(id, e);
      out.push({node: id, label, canvas});
    }
  }
  return out;
})()"""

# Where a node is now, relative to its viewport (null when gone): what the pan loop measures.
_RECT_FN = """(node => {
  const e = window.__jevFast && window.__jevFast.nodes.get(node);
  if (!e || !e.isConnected) return null;
  const r = e.getBoundingClientRect();
  return {x: r.x, y: r.y, w: r.width, h: r.height, vw: innerWidth, vh: innerHeight};
})"""


def _offscreen_actions(found, prefix: str = "bv") -> list:
    """'Bring <title> into view' actions for controls beyond a canvas's pane (see _OFFSCREEN_JS)."""
    out = []
    for i, f in enumerate(found if isinstance(found, list) else []):
        if not isinstance(f, dict) or not isinstance(f.get("node"), int) or not isinstance(f.get("canvas"), int):
            continue
        label = f.get("label") if isinstance(f.get("label"), str) else ""
        if not label.strip():
            continue
        out.append({"id": f"{prefix}{i + 1}", "kind": "scroll", "node": f["node"], "canvas": f["canvas"],
                    "label": f"Bring {label.strip()} into view", "delta": 0, "scroller": True, "pan_to": True})
    return out


def _pan_into_view(rect, wheel, settle=0.25, steps=8) -> bool:
    """Wheel a canvas until the node is inside its pane. `rect(node)` measures, `wheel(dx, dy)` pans.
    The wheel-to-pixel ratio is learned from the first move (a canvas pans a fraction of the delta).
    True when the node ended inside the pane; a node that is gone raises StalePage."""
    ratio = 2.0
    for _ in range(steps):
        r = rect()
        if not isinstance(r, dict):
            raise StalePage("Node gone since this decision. Observe again.")
        cx, cy = r["x"] + r["w"] / 2, r["y"] + r["h"] / 2
        if r["x"] >= 0 and r["y"] >= 0 and r["x"] + r["w"] <= r["vw"] and r["y"] + r["h"] <= r["vh"]:
            return True
        need_x, need_y = cx - r["vw"] / 2, cy - r["vh"] / 2
        dx = max(-2400, min(2400, round(need_x * ratio)))
        dy = max(-2400, min(2400, round(need_y * ratio)))
        if not dx and not dy:
            return False
        wheel(dx, dy)
        time.sleep(settle)
        after = rect()
        if not isinstance(after, dict):
            raise StalePage("Node gone since this decision. Observe again.")
        moved_y = cy - (after["y"] + after["h"] / 2)
        moved_x = cx - (after["x"] + after["w"] / 2)
        if abs(dy) >= 50 and abs(moved_y) >= 2:
            ratio = max(0.5, min(8.0, abs(dy) / abs(moved_y)))
        elif abs(dx) >= 50 and abs(moved_x) >= 2:
            ratio = max(0.5, min(8.0, abs(dx) / abs(moved_x)))
        elif abs(moved_x) < 2 and abs(moved_y) < 2:
            return False  # the canvas does not pan under a wheel
    return False


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


def _same_page(a: str, b: str) -> bool:
    """Same document: scheme, host, path (ignoring a trailing slash) and query; fragments differ
    freely (a SPA router may add one)."""
    def key(u):
        try:
            p = urlsplit(u or "")
        except ValueError:
            return None
        return (p.scheme.lower(), p.netloc.lower(), (p.path or "/").rstrip("/") or "/", p.query)
    return key(a) is not None and key(a) == key(b)


def _wait_loaded(b, timeout: float = LOAD_TIMEOUT) -> bool:
    deadline = _monotonic() + timeout
    while True:
        if loaded(b):
            return True
        if _monotonic() >= deadline:
            return False
        _sleep(_POLL)


# --- shadow DOM --------------------------------------------------------------------------------
# Upstream's snapshot reads document.querySelectorAll and a TreeWalker over document.body: both
# stop at shadow roots, so a page built from web components (a GHL calendar: 2 shadow hosts,
# thousands of characters, body.innerText empty) observes as blank with no controls, and Jev
# reports BLOCKED or clicks a tab whose panel it cannot see. The snapshot is patched by string
# substitution at the anchors below; if upstream changes and an anchor is missing, the original
# snapshot is kept and a warning logged. Controls found inside a shadow root carry `shadow: true`
# and are acted on by _act_shadow (upstream's hit test, document.elementFromPoint, returns the
# host for them, so it would refuse every such click as "covered").
_SHADOW_HELPERS = """
  const deepAll=(root,sel,out=[])=>{for (const e of root.querySelectorAll(sel)) out.push(e);
    for (const h of root.querySelectorAll('*')) if (h.shadowRoot) deepAll(h.shadowRoot,sel,out); return out;};
  const deepText=(root,visit)=>{const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT|NodeFilter.SHOW_ELEMENT);
    let n; while ((n=w.nextNode())) { if (n.nodeType===3) { if (visit(n)===false) return false; }
      else if (n.shadowRoot && deepText(n.shadowRoot,visit)===false) return false; } return true;};
"""
_SHADOW_ANCHORS = (
    ("  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);\n",
     "  for (const [id,e] of cache.nodes) if (!e.isConnected) cache.nodes.delete(id);\n" + _SHADOW_HELPERS),
    ("[...document.querySelectorAll('input,textarea,select')]", "deepAll(document,'input,textarea,select')"),
    ("for (const e of document.querySelectorAll(selector)) {", "for (const e of deepAll(document,selector)) {"),
    ("      rect:{x:r.x,y:r.y,w:r.width,h:r.height}};",
     "      rect:{x:r.x,y:r.y,w:r.width,h:r.height},...(e.getRootNode()!==document?{shadow:true}:{})};"),
    ("""  const words=[], walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
  const range=document.createRange(); let node,length=0;
  while ((node=walker.nextNode()) && length<6000) {
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) continue;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  }
""", """  const words=[], range=document.createRange(); let length=0;
  deepText(document.body, node => {
    if (length>=6000) return false;
    const value=node.textContent.trim(), parent=node.parentElement;
    if (!value || !parent || parent.closest('script,style,noscript,template') || !visible(parent)) return;
    range.selectNodeContents(node); const r=range.getBoundingClientRect();
    if (r.width>0 && r.height>0 && r.bottom>0 && r.top<innerHeight && r.right>0 && r.left<innerWidth) {
      words.push(value); length+=value.length;
    }
  });
"""),
)


def shadow_snapshot_js(src: str) -> str | None:
    """Upstream's snapshot with shadow-root traversal, or None when an anchor is not found exactly
    once (upstream changed: keep theirs rather than ship a broken script)."""
    out = src
    for old, new in _SHADOW_ANCHORS:
        if out.count(old) != 1:
            return None
        out = out.replace(old, new)
    return out


def _install_shadow_snapshot() -> bool:
    from jev_ultrafast import browser as ub
    if getattr(ub, "_jev_runner_shadow", None) is not None:
        return ub._jev_runner_shadow
    patched = shadow_snapshot_js(ub.READ_STATE)
    if patched is None:
        _log.warning("shadow DOM: upstream snapshot.js changed; keeping the original (no shadow support)")
        ub._jev_runner_shadow = False
        return False
    ub.READ_STATE = patched
    ub.MARKER = f"(() => {{ const state={patched}; return state?.marker ?? null; }})()"
    ub._jev_runner_shadow = True
    return True


# Like upstream's pre-input resolution, with a composed hit test that descends through open shadow
# roots (document.elementFromPoint stops at the host). Runs before any input: null = StalePage.
_RESOLVE_SHADOW_FN = """((action) => {
  const e = window.__jevFast && window.__jevFast.nodes.get(action.node);
  if (!e || !e.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]')) return null;
  try { if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return null; } catch (_) {}
  if (action.kind === 'fill' && (e.readOnly || e.getAttribute('aria-readonly') === 'true')) return null;
  const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
  if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return null;
  let hit = document.elementFromPoint(x, y);
  while (hit && hit.shadowRoot) { const d = hit.shadowRoot.elementFromPoint(x, y); if (!d || d === hit) break; hit = d; }
  if (!hit || !(e === hit || e.contains(hit))) return null;
  if (action.kind === 'select') {
    if (e.tagName !== 'SELECT' || ![...e.options].some(o => o.value === action.value && !o.disabled)) return null;
    e.value = action.value;
    e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true}));
    return {x, y, selected: true};
  }
  return {x, y};
})"""


# After upstream's observe: which observed controls are occluded (a click at their centre would land
# on an unrelated element, which is exactly what the pre-input hit test refuses later), and whether
# the page is still loading (visible spinner/skeleton/loader elements, or the document not complete).
# Seen: a panel opened by a mis-labelled icon covered a search field; Jev tried to fill it three times
# and the run died as stale_page. And a contact page shows loaders for ~9 s while its text is only the
# navigation shell; Jev answered BLOCKED after one wait.
# Only indicators with some area count (>= 400 px²): a 14 px "loading"-styled input decoration that
# never goes away held a run for the whole 20 s patience (seen on an audit log page).
_LOADING_SELECTOR = ("[aria-busy='true'],[role='progressbar'],[class*='skeleton'],[class*='spinner'],"
                     "[class*='loader'],[class*='loading']")
_POST_OBSERVE_FN = """((nodes, loadingSel) => {
  const c = window.__jevFast; const covered = [];
  if (c) for (const n of nodes) {
    const e = c.nodes.get(n); if (!e || !e.isConnected) { covered.push(n); continue; }
    const r = e.getBoundingClientRect(), x = r.x + r.width / 2, y = r.y + r.height / 2;
    if (!r.width || !r.height || x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) continue;
    let hit = document.elementFromPoint(x, y);
    while (hit && hit.shadowRoot) { const d = hit.shadowRoot.elementFromPoint(x, y); if (!d || d === hit) break; hit = d; }
    if (hit && !(e === hit || e.contains(hit))) covered.push(n);
  }
  let loading = document.readyState !== 'complete';
  if (!loading) for (const e of document.querySelectorAll(loadingSel)) {
    try {
      if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
      if (e.matches('input,textarea,select') || e.querySelector('input,textarea,select')) continue;
      const r = e.getBoundingClientRect();
      if (r.width * r.height >= 400) { loading = true; break; }  // a spinner or skeleton, not a decoration
    } catch (_) {}
  }
  return {covered, loading};
})"""


# A cross-origin iframe's own loaders. The top frame's check cannot see inside an OOPIF, so a
# white-labelled screen whose canvas was still a skeleton looked loaded (seen: Jev judged a
# workflow editor at 5 s, on 20 of its 77 controls, and said BLOCKED). Only indicators with some
# area count: a 14 px "loading"-styled input decoration that never goes away must not hold a run.
_FRAME_LOADING_FN = """((loadingSel) => {
  if (document.readyState !== 'complete') return true;
  for (const e of document.querySelectorAll(loadingSel)) {
    try {
      if (!e.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
      // a field whose class carries a loading modifier (a number input's wrapper) is a field, not a loader
      if (e.matches('input,textarea,select') || e.querySelector('input,textarea,select')) continue;
      const r = e.getBoundingClientRect();
      if (r.width * r.height >= 400) return true;  // a spinner or skeleton, not a decoration
    } catch (_) {}
  }
  return false;
})"""
FRAME_SETTLE = 1.0  # s a frame's text must hold still before the frame counts as loaded


_NEW_TAB_GRACE = 0.3  # s after a click for a window.open to register as a target


_NODE_FRESH = ("(() => { const c=window.__jevFast; "
               "return c ? [c.pageKey(), c.guard(c.nodes.get(%d))] : null; })()")


def fresh_check(evaluate, page: dict, action: dict | None) -> bool:
    """Whether `action` may still act on `page`. Upstream compares the whole-page marker (title,
    text, every control) for anything but click/select, so on a page that keeps re-rendering
    (live timestamps, polling widgets) every wait/fill came back stale and the run died after
    three strikes without ever acting. Here only the action's own target has to be unchanged
    (identity, role, name, value, enclosing form/row text), in the same document (same load,
    same URL). The rest of upstream's page key (scroll position, viewport, every other field's
    value) describes the page around the target, not the target: a list that scrolls itself or a
    clock in another field must not refuse a click on an unchanged link (seen: an audit log where
    "Open in new tab" was refused three times in 400 ms while the row never changed). Waiting and
    scrolling send no input to any element, so they are never stale. No action: the whole-page
    marker, as before."""
    if action is None:
        from jev_ultrafast import browser as ub  # lazy: nothing touches the harness at import
        return evaluate(ub.MARKER) == page.get("marker")  # ub.MARKER: patched for shadow DOM
    if action.get("kind") in ("wait", "scroll"):
        return True
    node = action.get("node")
    if type(node) is not int:
        return False
    current = evaluate(_NODE_FRESH % node)
    if not isinstance(current, list) or len(current) != 2:
        return False
    key, guard = current
    return (_document_of(key) == _document_of(page.get("page_key"))
            and guard == (page.get("guards") or {}).get(str(node)))


def _document_of(page_key):
    """The document part of upstream's page key: its load time and URL (the first two entries)."""
    return page_key[:2] if isinstance(page_key, list) else page_key


def _default_factory(url: str, background: bool):
    """Open a tab via upstream Browser; imported lazily so nothing touches Chrome at import time."""
    from jev_ultrafast.browser import Browser
    from browser_harness.admin import ensure_daemon
    from browser_harness.helpers import cdp
    _install_shadow_snapshot()

    class _Browser(Browser):
        def __init__(self, url, background):
            ensure_daemon()
            self.target = cdp("Target.createTarget", url="about:blank", background=background)["targetId"]
            self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780,
                      deviceScaleFactor=1, mobile=False)
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            self.call("Page.navigate", url=url)

        def fresh(self, page, action=None):
            return fresh_check(self.evaluate, page, action)

        def alive(self) -> bool:
            """Whether the tab still exists (ms): a CDP call on a closed tab only times out (5 s)."""
            ids = {t.get("targetId") for t in cdp("Target.getTargets").get("targetInfos", [])}
            return self.target in ids

    return _Browser(url, background)


class ChromeTarget:
    kind = "chrome"

    def __init__(self, url: str, browser_factory=None):
        self._factory = browser_factory or _default_factory
        self.url = url
        self.browser = self._open(url)
        self._files: list[str] = []  # local paths this run may upload (set by the daemon)
        self._frame_seen: dict = {}  # frame origin -> (text digest, when it last changed)
        self._next_file = 0
        # Register this tab with AI Broker so the Tab Groups extension names+colours it (best-effort;
        # a no-op without a broker token, and a failure never affects the run).
        self._tab = getattr(self.browser, "target", None)
        self._grouping = bool(self._tab) and tab_groups.enabled()
        self._last_tab_ping = _monotonic()
        if self._tab:
            tab_groups.claim(self._tab)

    def ensure_at(self, url: str) -> bool:
        """Make this tab show `url` for a new run: True when it already does, or after navigating
        there; False when the tab is gone. Reusing an idle tab skips opening one and, when it is
        still on the page, the page load (the biggest fixed cost of a run)."""
        alive = getattr(self.browser, "alive", None)
        try:
            if alive is not None and not alive():
                return False
            here = self.browser.evaluate("location.href")
        except Exception:
            return False
        if _same_page(here, url):
            return True
        try:
            self.browser.call("Page.navigate", url=url)
            return _wait_loaded(self.browser)
        except Exception:
            return False

    def set_files(self, paths: list[str]) -> None:
        """Files this run may upload. With files, the OS file picker is intercepted (never opens)
        and the page's file inputs are offered as "Upload file" actions."""
        self._files, self._next_file = list(paths or []), 0
        try:
            self.browser.call("Page.setInterceptFileChooserDialog", enabled=bool(self._files))
        except Exception:
            pass  # best-effort: the upload action itself never needs the picker

    def _upload_names(self) -> list[str]:
        return [Path(p).name for p in self._files]

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
        self._add_pointer_text(page)
        self._add_widgets(page)
        self._add_scrollers(page)
        if self._files:
            try:
                page.setdefault("actions", []).extend(_file_input_actions(
                    self.browser.evaluate(_FILE_INPUTS_JS), self._upload_names()))
            except Exception:
                pass  # never let discovery break a normal observe
        n = _enrich_blank_labels(page.get("actions", []), "node", self.browser.evaluate)
        _disambiguate_duplicates(page.get("actions", []), "node", self.browser.evaluate)
        self._drop_unnamed_icons(page.get("actions", []))
        if n and os.environ.get("JEV_DEBUG_IFRAME"):
            _log.info("labels: synthesized %d top-frame label(s)", n)
        self._merge_iframe_actions(page)
        self._drop_covered_and_flag_loading(page)
        if page.get("frame_loading"):
            page["loading"] = True  # an iframe still shows loaders, or its text is still changing
        page["readable"] = True
        if self._grouping and _monotonic() - self._last_tab_ping > _TAB_ACTIVITY_EVERY:
            self._last_tab_ping = _monotonic()  # keep the claimed tab alive past the broker's sweep
            tab_groups.activity([self._tab])
        return page

    def _drop_covered_and_flag_loading(self, page: dict) -> None:
        """Remove controls an element outside them is sitting on (a panel, a sticky bar, a dialog
        backdrop): act() would refuse them anyway, so offering them only buys stale_page strikes.
        Sets page["loading"] while spinners/skeletons are visible, so the runner waits on its own
        instead of asking Jev about a page that is still arriving."""
        actions = page.get("actions") or []
        nodes = [a["node"] for a in actions
                 if type(a.get("node")) is int and a.get("frame") is None and not a.get("file_input")]
        try:
            res = self.browser.evaluate(f"{_POST_OBSERVE_FN}({json.dumps(nodes)}, "
                                        f"{json.dumps(_LOADING_SELECTOR)})")
        except Exception:
            return  # never let this break a normal observe
        if not isinstance(res, dict):
            return
        covered = set(res.get("covered") or [])
        if covered:
            page["actions"] = [a for a in actions if a.get("node") not in covered or a.get("frame") is not None]
            page["covered"] = len(covered)
        page["loading"] = bool(res.get("loading"))

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
            frame_text = snap.get("text") if isinstance(snap.get("text"), str) else ""
            if self._frame_still_loading(session_id, _origin(url) or url, frame_text):
                page["frame_loading"] = True
            if frame_text.strip():
                # Jev (and the verifier) decide on page["text"], which innerText stops at the
                # iframe boundary: without this a white-labelled screen looked empty to Jev even
                # though its controls were offered (BLOCKED at step 0; DONE then "unverified").
                page["text"] = ((page.get("text") or "") + "\n" + frame_text)[:READ_LIMIT]
            try:
                icons = _icon_control_actions(self._frame_eval(session_id, _ICON_CONTROLS_JS), "ic")
            except Exception:
                icons = []  # never let icon discovery break a normal observe
            for a in icons:
                a.pop("_guard", None)  # in-frame act resolves by node; no upstream freshness check
            labels = {a.get("label") for a in snap.get("actions", [])}
            raw = list(snap.get("actions", [])) + icons + self._frame_overlay_actions(
                session_id, labels)
            try:
                texts = _pointer_text_actions(self._frame_eval(session_id, _POINTER_TEXT_JS), labels)
            except Exception:
                texts = []  # never let discovery break a normal observe
            for a in texts:
                a.pop("_guard", None)  # in-frame act resolves by node; no upstream freshness check
            raw += texts
            try:
                widgets = _widget_actions(self._frame_eval(session_id, _WIDGETS_JS),
                                          {a.get("label") for a in raw})
            except Exception:
                widgets = []  # never let discovery break a normal observe
            for a in widgets:
                a.pop("_guard", None)  # in-frame act resolves by node; no upstream freshness check
            raw += widgets
            try:
                raw += _scroller_actions(self._frame_eval(session_id, _SCROLLERS_JS))
                raw += _offscreen_actions(self._frame_eval(session_id, _OFFSCREEN_JS))
            except Exception:
                pass  # never let discovery break a normal observe
            if self._files:
                try:
                    raw += _file_input_actions(self._frame_eval(session_id, _FILE_INPUTS_JS),
                                               self._upload_names())
                except Exception:
                    pass
            synth = _enrich_blank_labels(raw, "node", lambda expr: self._frame_eval(session_id, expr))
            _disambiguate_duplicates(raw, "node", lambda expr: self._frame_eval(session_id, expr))
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
                    merged = {"id": aid, "kind": "scroll", "label": a.get("label", ""),
                              "delta": a.get("delta", 0), "frame": idx}
                    if a.get("delta_x"):
                        merged["delta_x"] = a["delta_x"]
                    if a.get("pan"):
                        merged["pan"] = True
                    if a.get("pan_to") and isinstance(a.get("canvas"), int):
                        merged["pan_to"], merged["canvas"] = True, a["canvas"]
                    if a.get("scroller") and isinstance(a.get("node"), int):
                        merged["frame_node"], merged["scroller"] = a["node"], True
                    actions.append(merged)
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
                for key in ("checked", "selected", "expanded", "synth_label", "raw_label",
                            "row_label", "file_input", "multiple"):
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
                sort_keys=True) + (page.get("text") or "")
            page["fingerprint"] = hashlib.sha256(
                (str(page.get("fingerprint", "")) + frame_sig).encode()).hexdigest()

    def _frame_still_loading(self, session_id, key: str, text: str) -> bool:
        """Loaders visible inside the frame, or its text changed within the last FRAME_SETTLE s
        (a canvas or table that is still filling in shows no loader at all)."""
        try:
            loading = bool(self._frame_eval(
                session_id, f"{_FRAME_LOADING_FN}({json.dumps(_LOADING_SELECTOR)})"))
        except Exception:
            loading = False
        now = _monotonic()
        seen = self._frame_seen.get(key)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if seen is None or seen[0] != digest:
            self._frame_seen[key] = (digest, now)
            return True
        return loading or now - seen[1] < FRAME_SETTLE

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

    def _add_pointer_text(self, page: dict) -> None:
        try:
            found = self.browser.evaluate(_POINTER_TEXT_JS)
        except Exception:
            return  # never let discovery break a normal observe
        actions = page.setdefault("actions", [])
        for a in _pointer_text_actions(found, {x.get("label") for x in actions}):
            _record_guard(page, {"node": a["node"], "guard": a.pop("_guard")})
            actions.append(a)

    def _add_widgets(self, page: dict) -> None:
        try:
            found = self.browser.evaluate(_WIDGETS_JS)
        except Exception:
            return  # never let discovery break a normal observe
        actions = page.setdefault("actions", [])
        for a in _widget_actions(found, {x.get("label") for x in actions}):
            _record_guard(page, {"node": a["node"], "guard": a.pop("_guard")})
            actions.append(a)

    def _add_scrollers(self, page: dict) -> None:
        try:
            found = self.browser.evaluate(_SCROLLERS_JS)
        except Exception:
            return  # never let discovery break a normal observe
        page.setdefault("actions", []).extend(_scroller_actions(found))
        try:
            page["actions"].extend(_offscreen_actions(self.browser.evaluate(_OFFSCREEN_JS)))
        except Exception:
            pass  # never let discovery break a normal observe

    def _frame_overlay_actions(self, session_id, labels: set) -> list:
        """Overlay/menu options inside an OOPIF (a row menu opened in the iframe renders there)."""
        try:
            found = self._frame_eval(session_id, _OVERLAY_JS)
        except Exception:
            return []  # never let overlay discovery break a normal observe
        out = []
        for i, f in enumerate(found if isinstance(found, list) else []):
            label = f.get("label") if isinstance(f, dict) else None
            if not isinstance(f.get("node") if label else None, int) or label in labels:
                continue
            labels.add(label)
            out.append({"id": f"ov{i + 1}", "node": f["node"], "kind": "click", "label": label,
                        "role": "button", "value": ""})
        return out

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
        if action.get("file_input"):
            self._upload(action)
            return
        if action.get("frame") is not None:
            self._act_in_frame(action, text)  # OOPIF: separate path; top-frame/money path unchanged
            return
        if action.get("scroller"):
            self._act_scroller(action)  # wheel over a panel, not over the window
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
        before = self._page_targets() if action.get("kind") == "click" else None
        if action.get("shadow"):
            self._act_shadow(action, text)  # inside a shadow root: upstream's hit test can't see it
        else:
            try:
                self.browser.act(action, page, text=text)
            except Exception as exc:
                # Upstream raises StalePage only from its pre-input hit-test (target moved or
                # covered), before any input is dispatched: safe to re-observe and retry.
                if _is_upstream_stale(exc):
                    raise StalePage(str(exc)) from exc
                # Anything else: input may already have been dispatched; never a StalePage.
                raise RuntimeError(f"action_uncertain: {exc}") from exc
        if before is not None:
            self._follow_new_tab(before)

    def _act_scroller(self, action: dict) -> None:
        """Wheel over a panel that scrolls inside the page, resolved by node like a click: a panel
        that is gone or covered is a retryable StalePage, before any input."""
        if action.get("pan_to"):
            try:
                center = self.browser.evaluate(f"{_RESOLVE_IN_FRAME_FN}({int(action['canvas'])})")
            except Exception as exc:
                raise StalePage("Canvas gone since this decision. Observe again.") from exc
            if not isinstance(center, dict):
                raise StalePage("Canvas changed or is covered. Observe again.")
            node = int(action["node"])
            _pan_into_view(lambda: self.browser.evaluate(f"{_RECT_FN}({node})"),
                           lambda dx, dy: self.browser.call("Input.dispatchMouseEvent", type="mouseWheel",
                                                            x=center["x"], y=center["y"], deltaX=dx, deltaY=dy))
            return
        try:
            center = self.browser.evaluate(f"{_RESOLVE_IN_FRAME_FN}({int(action['node'])})")
        except Exception as exc:
            raise StalePage("Panel gone since this decision. Observe again.") from exc
        if not isinstance(center, dict):
            raise StalePage("Panel changed or is covered. Observe again.")
        try:
            self.browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=center["x"],
                              y=center["y"], deltaX=action.get("delta_x", 0), deltaY=action.get("delta", 0))
        except Exception as exc:
            raise RuntimeError(f"action_uncertain: {exc}") from exc

    # --- a click that opens a new tab ---------------------------------------------------------
    # Seen: a contact page's "Audit logs" button is window.open to a page of the same site. Jev's
    # session is bound to one tab, so the click looked like a no-op (three identical clicks, then
    # no_progress) and every try left another tab open in the user's Chrome. The run now follows
    # a tab its own click opened; the tab it came from is closed with the session.

    def _page_targets(self) -> set | None:
        try:
            return {t.get("targetId") for t in _cdp("Target.getTargets").get("targetInfos", [])
                    if t.get("type") == "page"}
        except Exception:
            return None

    def _follow_new_tab(self, before: set | None) -> None:
        if before is None or not getattr(self.browser, "target", None):
            return
        _sleep(_NEW_TAB_GRACE)
        try:
            targets = _cdp("Target.getTargets").get("targetInfos", [])
        except Exception:
            return
        opened = [t for t in targets if t.get("type") == "page" and t.get("targetId") not in before
                  and t.get("openerId") == self.browser.target]
        if not opened:
            return
        new_id = opened[-1]["targetId"]
        try:
            session = _cdp("Target.attachToTarget", targetId=new_id, flatten=True)["sessionId"]
        except Exception as exc:
            _log.info("new tab %s opened by a click but could not be attached: %s", new_id, exc)
            return
        old = self.browser.target
        self.browser.target, self.browser.session = new_id, session
        if hasattr(self.browser, "after_input"):
            self.browser.after_input = None
        for method, params in (("Emulation.setDeviceMetricsOverride",
                                {"width": 1120, "height": 780, "deviceScaleFactor": 1, "mobile": False}),
                               ("Emulation.setFocusEmulationEnabled", {"enabled": True})):
            try:
                self.browser.call(method, **params)
            except Exception:
                pass
        self._orphans = getattr(self, "_orphans", []) + [old]
        _wait_loaded(self.browser)
        _log.info("followed the tab a click opened: %s -> %s (%s)", old, new_id,
                  (opened[-1].get("url") or "")[:80])
        if self._grouping:
            self._tab = new_id
            tab_groups.claim(new_id)

    def _act_shadow(self, action: dict, text: str | None) -> None:
        """Click/fill/select a control inside a shadow root: resolve with a composed hit test (no
        input yet: a moved/covered target is a retryable StalePage), then dispatch as for iframes."""
        args = {k: action.get(k) for k in ("node", "kind", "value")}
        try:
            res = self.browser.evaluate(f"{_RESOLVE_SHADOW_FN}({json.dumps(args)})")
        except Exception as exc:
            if _is_upstream_stale(exc):
                raise StalePage(str(exc)) from exc
            raise
        if not isinstance(res, dict):
            raise StalePage("Target changed or is covered. Observe again.")
        if res.get("selected"):
            return
        try:
            self._dispatch(action, res["x"], res["y"], text)
        except Exception as exc:  # input may already be in flight: never a retryable StalePage
            raise RuntimeError(f"action_uncertain: {exc}") from exc

    def _upload(self, action: dict) -> None:
        """Set the run's files on a file input via CDP (no OS picker). A single-file input takes
        the next file not yet uploaded. Resolving the input is retryable (StalePage); once the
        files are set, a failure is action_uncertain."""
        if not self._files:
            raise StalePage("No files were given for this run (run_task files=[...]).")
        if action.get("frame") is not None:
            frame = getattr(self, "_frames", {}).get(action["frame"])
            if not frame:
                raise StalePage("Frame gone since this decision. Observe again.")
            call = lambda method, **kw: _cdp(method, session_id=frame["session"], **kw)
            node = action.get("frame_node", action["node"])
        else:
            call, node = self.browser.call, action["node"]
        try:
            res = call("Runtime.evaluate", returnByValue=False,
                       expression=f"(window.__jevFast && window.__jevFast.nodes.get({int(node)})) || null")
        except Exception as exc:
            raise StalePage(f"File input not reachable: {exc}") from exc
        obj = (res or {}).get("result") or {}
        if not obj.get("objectId") or obj.get("subtype") == "null":
            raise StalePage("File input gone since this decision. Observe again.")
        if action.get("multiple"):
            chosen = list(self._files)
        else:
            chosen = [self._files[self._next_file % len(self._files)]]
            self._next_file += 1
        try:
            call("DOM.setFileInputFiles", files=chosen, objectId=obj["objectId"])
        except Exception as exc:
            raise RuntimeError(f"action_uncertain: {exc}") from exc
        _log.info("upload: %d file(s) set on %r", len(chosen), action.get("label"))

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
            if action.get("pan_to"):
                center = self._resolve_in_frame(frame["session"], action["canvas"])
                if center is None:
                    raise StalePage("Canvas changed or is covered. Observe again.")
                cx, cy = offset["x"] + center["x"], offset["y"] + center["y"]
                node = int(action["frame_node"])
                _pan_into_view(lambda: self._frame_eval(frame["session"], f"{_RECT_FN}({node})"),
                               lambda dx, dy: self.browser.call("Input.dispatchMouseEvent", type="mouseWheel",
                                                                x=cx, y=cy, deltaX=dx, deltaY=dy))
                return
            if action.get("frame_node") is not None:  # a panel inside the iframe: wheel over it
                center = self._resolve_in_frame(frame["session"], action["frame_node"])
                if center is None:
                    raise StalePage("Panel changed or is covered. Observe again.")
                x, y = offset["x"] + center["x"], offset["y"] + center["y"]
            if os.environ.get("JEV_DEBUG_IFRAME"):
                _log.info("iframe act: kind=scroll delta=%s iframe_offset=%s -> top xy=(%.1f,%.1f)",
                          action.get("delta"), offset, x, y)
            try:
                self.browser.call("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y,
                                  deltaX=action.get("delta_x", 0), deltaY=action.get("delta", 0))
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
        for old in getattr(self, "_orphans", []):  # tabs this run moved on from
            try:
                _cdp("Target.closeTarget", targetId=old)
            except Exception:
                pass
        self._orphans = []
        self._close_quiet(self.browser)
