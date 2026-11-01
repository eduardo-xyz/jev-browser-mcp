"""ChromeTarget: drives a Chrome tab through upstream Browser Harness, recovering blank/hung tabs."""
import base64
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from .base import StalePage, TargetBlocked

_log = logging.getLogger("jevd")

LOAD_TIMEOUT = 15.0
_POLL = 0.25
_LOADED_JS = (
    "document.readyState === 'complete' && "
    "!!document.body && document.body.innerText.trim().length > 0"
)

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
    out.push({node: id, label});
  }
  return out.slice(0, 20);
})()"""


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
        self._merge_iframe_actions(page)
        page["readable"] = True
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
            raw = snap.get("actions", [])
            merged_count = 0
            for a in raw:
                if a.get("kind") in ("scroll", "wait") or not isinstance(a.get("node"), int):
                    continue
                aid = f"f{idx}{a.get('id', '')}"
                if aid in existing:
                    continue
                existing.add(aid)
                merged = {"id": aid, "node": a["node"], "kind": a.get("kind"),
                          "label": a.get("label", ""), "role": a.get("role", ""),
                          "value": a.get("value", ""), "frame": idx}
                for key in ("checked", "selected", "expanded"):
                    if key in a:
                        merged[key] = a[key]
                actions.append(merged)
                merged_count += 1
            if debug:
                _log.info("iframe[%d] %s: w=%s h=%s raw_actions=%d merged=%d",
                          idx, url, snap.get("w"), snap.get("h"), len(raw), merged_count)
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

    def _iframe_targets(self) -> list[tuple]:
        """(session_id, url) for each cross-origin iframe target whose origin this page embeds."""
        origins = self._child_origins()
        debug = bool(os.environ.get("JEV_DEBUG_IFRAME"))
        if not origins and not debug:
            return []
        try:
            targets = _cdp("Target.getTargets").get("targetInfos", [])
        except Exception:
            if debug:
                _log.info("iframe discovery: child_origins=%s; Target.getTargets failed",
                          sorted(origins))
            return []
        if debug:
            _log.info("iframe discovery: child_origins=%s; iframe targets=%s",
                      sorted(origins),
                      [i.get("url") for i in targets if i.get("type") == "iframe"])
        if not origins:
            return []
        out = []
        for info in targets:
            if info.get("type") != "iframe" or _origin(info.get("url") or "") not in origins:
                continue
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
        center = self._resolve_in_frame(frame["session"], action["node"])
        if center is None:
            raise StalePage("Target changed or is covered. Observe again.")
        offset = self._iframe_offset(frame["origin"])
        if offset is None:
            raise StalePage("Iframe moved since this decision. Observe again.")
        x, y = offset["x"] + center["x"], offset["y"] + center["y"]
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
