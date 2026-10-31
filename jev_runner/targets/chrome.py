"""ChromeTarget: drives a Chrome tab through upstream Browser Harness, recovering blank/hung tabs."""
import base64
import time
from pathlib import Path

from .base import StalePage, TargetBlocked

LOAD_TIMEOUT = 15.0
_POLL = 0.25
_LOADED_JS = (
    "document.readyState === 'complete' && "
    "!!document.body && document.body.innerText.trim().length > 0"
)

_monotonic = time.monotonic
_sleep = time.sleep

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
        page["readable"] = True
        return page

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

    def fresh(self, page: dict) -> bool:
        try:
            return bool(self.browser.fresh(page))
        except Exception as exc:
            if _is_upstream_stale(exc):
                return False
            raise

    def act(self, action: dict, page: dict, text: str | None = None, verify: bool = False) -> None:
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

    def screenshot(self, path: Path) -> Path:
        data = self.browser.call("Page.captureScreenshot", format="png")["data"]
        path = Path(path)
        path.write_bytes(base64.b64decode(data))
        return path

    def close(self) -> None:
        self._close_quiet(self.browser)
