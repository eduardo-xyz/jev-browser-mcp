"""AndroidTarget: reads the screen and drives input through a persistent uiautomator2 session."""
import re
import shlex
from pathlib import Path
from time import monotonic, sleep

from ..android_tree import parse_hierarchy, screen_package
from ..guards import normalize
from .base import DeviceDisconnected, StalePage, TargetBlocked

_LAUNCHER_CMD = (
    "cmd package query-activities --brief -a android.intent.action.MAIN "
    "-c android.intent.category.LAUNCHER"
)
_COMPONENT = re.compile(r"^(\S+?)/\S+$")
# Foreground window + keyboard state in ONE shell round trip (~0.2 s over Wi-Fi). adbutils'
# app_current() tries `dumpsys window windows` (no match on recent Android), then falls through to
# `dumpsys activity top`, which dumps every activity's view tree: ~10 s on a Pixel, per screen read.
_FOCUS_CMD = ("dumpsys window 2>/dev/null | grep -m1 mCurrentFocus; "
              "dumpsys input_method 2>/dev/null | grep -m1 mInputShown")
_RESUMED_CMD = ("dumpsys activity activities 2>/dev/null | grep -m1 -E "
                "'topResumedActivity|ResumedActivity:'")
_COMPONENT_IN_RECORD = re.compile(r"\bu\d+ (\S+?)/(\S+?)[}\s]")


def _focused_component(out: str) -> tuple[str | None, str | None]:
    """(package, activity) from a Window{… u0 pkg/act} or ActivityRecord{… u0 pkg/act t1} line;
    (None, None) when the focused window is not an app (null, the shade, a popup)."""
    m = _COMPONENT_IN_RECORD.search(out or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def _short_activity(pkg: str, act: str) -> str:
    """`com.whatsapp/com.whatsapp.Conversation` -> `.Conversation`: the form `dumpsys activity top`
    reports and every saved flow's `where` was recorded in."""
    return act[len(pkg):] if pkg and act.startswith(pkg + ".") else act


def adb_input_text_arg(text: str) -> str | None:
    """Shell-safe argument for ``adb shell input text``, or ``None`` when the value can't be typed
    exactly this way and the caller should fall back to ``send_keys``.

    This path types secure-field values (passwords/codes), so correctness matters. Spaces are
    encoded as ``%s`` (``input text``'s own convention), and the whole argument is shell-quoted so
    no metacharacter can break out of it — a password like ``a&b; rm -rf /`` is typed verbatim,
    never executed or word-split. A literal ``%`` is ambiguous with the ``%s`` space encoding, so
    such text returns ``None`` to force the clipboard-free ``send_keys`` fallback rather than risk
    typing the wrong secret."""
    text = text or ""
    if "%" in text:
        return None
    return shlex.quote(text.replace(" ", "%s"))


def _connection_errors() -> tuple[type[BaseException], ...]:
    errs: list[type[BaseException]] = [ConnectionError, OSError, TimeoutError]
    try:
        import uiautomator2.exceptions as ex

        for name in ("ConnectError", "RPCUnavailableError", "HTTPTimeoutError",
                     "HTTPError", "AdbShellError"):
            c = getattr(ex, name, None)
            if isinstance(c, type) and issubclass(c, BaseException):
                errs.append(c)
    except Exception:  # uiautomator2 missing or shaped differently
        pass
    return tuple(errs)


_CONN_ERRORS = _connection_errors()


def _shell_text(res) -> str:
    return res if isinstance(res, str) else (getattr(res, "output", "") or "")


def _by_package_tokens(pkgs: list[str], want: str) -> list[str]:
    """Packages with a dot-separated token equal to (else starting with) the name."""
    want = want.replace(" ", "")
    tokens = {p: [normalize(t).replace(" ", "") for t in p.split(".")] for p in pkgs}
    exact = [p for p in pkgs if want in tokens[p]]
    return exact or [p for p in pkgs if any(t.startswith(want) for t in tokens[p])]


def _by_labels(d, pkgs: list[str], want: str, cache: dict) -> tuple[list[str], dict]:
    labels = {}
    for pkg in pkgs:
        if pkg not in cache:
            try:
                cache[pkg] = (d.app_info(pkg) or {}).get("label") or ""
            except _CONN_ERRORS:
                raise
            except Exception:
                continue  # unreadable label: skip, and don't cache the failure
        if cache[pkg]:
            labels[pkg] = cache[pkg]
    exact = [p for p, l in labels.items() if normalize(l) == want]
    if not exact:
        exact = [p for p, l in labels.items() if normalize(l).startswith(want)]
    if not exact:
        exact = [p for p, l in labels.items() if want in normalize(l)]
    return exact, labels


def resolve_app(d, name: str, cache: dict | None = None) -> str:
    """Resolve a human app name to a package among launchable apps.

    Package-name tokens first (uiautomator2's app_info often has no label: "bac" =>
    net.bac.sbe.android); launcher labels only when no token matches. `cache` maps
    package -> label and should live as long as the daemon (AdbLink.app_labels).
    """
    cache = cache if cache is not None else {}
    want = normalize(name)
    if not want:
        raise TargetBlocked(f"app_not_found: {name.strip()}")
    pkgs = []
    for line in _shell_text(d.shell(_LAUNCHER_CMD)).splitlines():
        m = _COMPONENT.match(line.strip())
        if m and m.group(1) not in pkgs:
            pkgs.append(m.group(1))
    found = _by_package_tokens(pkgs, want)
    names = {p: p for p in found}
    if not found:
        found, labels = _by_labels(d, pkgs, want, cache)
        names = {p: labels[p] for p in found}
    if not found:
        raise TargetBlocked(f"app_not_found: {name}")
    if len(found) > 1:
        raise TargetBlocked("app_ambiguous: " + ", ".join(sorted(set(names.values()))))
    return found[0]


def _default_connect(serial):
    import uiautomator2

    return uiautomator2.connect(serial)


# Foreground packages we must not launch an app over: doing so aborts an in-progress share.
_NO_RELAUNCH_OVER = {"com.android.intentresolver", "com.android.sharesheet"}


def _focus_ok(d, action: dict, cx: int, cy: int) -> bool:
    """The tap landed on the intended field: trust resource_id when present, else the bounds
    (which may have moved if the keyboard scrolled the field, hence the caller's one retry)."""
    sel = d(focused=True)
    if not sel.exists:
        return False
    info = sel.info
    rid = action.get("resource_id")
    if rid and info.get("resourceName") == rid:
        return True
    fb = info["bounds"]
    return fb["left"] <= cx <= fb["right"] and fb["top"] <= cy <= fb["bottom"]


class AndroidTarget:
    kind = "android"

    def __init__(self, link, app: str | None = None, connect=None):
        self.link = link
        self._connect = connect or _default_connect
        # label cache lives on the daemon's AdbLink so it outlives this target
        labels = getattr(link, "app_labels", None)
        self._labels: dict = labels if isinstance(labels, dict) else {}
        self.d = None
        self._dev()
        if app:
            pkg = app if "." in app else self._call(lambda d: resolve_app(d, app, self._labels))
            cur = self._call(self._foreground).get("package")
            # Never yank an app to the front over a system share sheet: it aborts the share
            # (launching over a share chooser aborts the share).
            if cur != pkg and cur not in _NO_RELAUNCH_OVER:
                self._call(lambda d: d.app_start(pkg))
                self._settle()

    settle_timeout = 5.0

    def _settle(self):
        """After a launch, wait until the screen has actions and two dumps agree (splash screens
        and half-drawn first frames otherwise reach Jev, which reports BLOCKED)."""
        deadline, prev = monotonic() + self.settle_timeout, None
        while monotonic() < deadline:
            p = self.observe()
            on_screen = any("bounds" in a for a in p["actions"])  # not just back/home/wait
            if on_screen and p["fingerprint"] == prev:
                return
            prev = p["fingerprint"] if on_screen else None
            sleep(0.25)

    def _dev(self):
        if self.d is None:
            try:
                self.d = self._connect(self.link.ensure_connected())
            except _CONN_ERRORS as e:
                raise DeviceDisconnected(f"cannot connect: {e}") from e
        return self.d

    def _call(self, fn):
        """Run fn(device); on a connection error reconnect once and retry."""
        try:
            return fn(self._dev())
        except _CONN_ERRORS:
            self.d = None
        try:
            return fn(self._dev())  # ensure_connected raises DeviceDisconnected if it can't
        except _CONN_ERRORS as e:
            self.d = None
            raise DeviceDisconnected(f"connection lost: {e}") from e

    def observe(self) -> dict:
        def read(d):
            info = self._foreground(d)
            xml = d.dump_hierarchy(compressed=False)
            p = parse_hierarchy(xml, info["package"], info["activity"])
            if not p["readable"] and not p["shade_open"]:
                # the focused window (keyboard bubble, another app's overlay) is not what's on
                # screen: read the app that actually fills the display instead of giving up
                shown = screen_package(xml)
                if shown and shown != info["package"]:
                    alt = parse_hierarchy(xml, shown, "")  # its activity is unknown
                    if alt["readable"]:
                        alt["focused_package"] = info["package"]
                        p = alt
            if p["shade_open"]:
                p = parse_hierarchy(xml, info["package"], info["activity"], include_system_ui=True)
            if info.get("keyboard"):
                # The IME can cover the button a step needs; give Jev an explicit way to dismiss it.
                p["actions"].append({"id": "hide_keyboard", "kind": "hide_keyboard",
                                     "label": "Hide the on-screen keyboard"})
            return p

        return self._call(read)

    def _foreground(self, d) -> dict:
        """{package, activity, keyboard} of the screen, in one shell round trip (see _FOCUS_CMD)."""
        out = _shell_text(d.shell(_FOCUS_CMD))
        pkg, act = _focused_component(out)
        if pkg is None:  # focus is on the shade/a popup/nothing: the app resumed under it
            pkg, act = _focused_component(_shell_text(d.shell(_RESUMED_CMD)))
        if pkg is None:  # mid-transition: adbutils' slow but thorough path
            info = d.app_current() or {}
            pkg, act = info.get("package") or "", info.get("activity") or ""
        return {"package": pkg, "activity": _short_activity(pkg, act),
                "keyboard": "mInputShown=true" in out}

    def fresh(self, page: dict) -> bool:
        return self.observe()["fingerprint"] == page["fingerprint"]

    def refresh(self, page: dict) -> dict:
        """The current screen in one read: a freshness check costs a full read here anyway."""
        return self.observe()

    def _input(self, fn):
        """Send input once. A connection error here is never retried: input may have landed."""
        try:
            fn(self._dev())
        except _CONN_ERRORS as e:
            self.d = None
            raise DeviceDisconnected(f"connection lost during input: {e}") from e

    def act(self, action: dict, page: dict, text: str | None = None, verify: bool = False) -> None:
        if verify and not self.fresh(page):
            raise StalePage()
        kind = action["kind"]
        if kind in ("click", "fill"):
            l, t, r, b = action["bounds"]
            cx, cy = (l + r) // 2, (t + b) // 2

            def tap(d):
                d.click(cx, cy)
                if kind == "fill":
                    sleep(0.1)
                    if not _focus_ok(d, action, cx, cy):
                        sleep(0.3)  # opening the keyboard can scroll the field; look once more
                        if not _focus_ok(d, action, cx, cy):
                            raise StalePage()  # focus landed elsewhere; nothing typed yet
                    # Prefer set_text (no clipboard). send_keys pastes via clipboard and BAC
                    # often leaves password/Código fields empty or toasts "pasted from clipboard".
                    typed = False
                    rid = action.get("resource_id") or ""

                    def _try_set(sel):
                        try:
                            if sel is not None and getattr(sel, "exists", False):
                                sel.set_text(text or "")
                                return True
                        except Exception:
                            return False
                        return False

                    if rid and _try_set(d(resourceId=rid)):
                        typed = True
                    elif _try_set(d(focused=True)):
                        typed = True
                    if not typed:
                        # Shell input text avoids the clipboard path entirely. Skip it when the
                        # value can't be transported exactly this way (see adb_input_text_arg) and
                        # let send_keys handle it instead of typing a wrong secret.
                        arg = adb_input_text_arg(text or "")
                        if arg is not None:
                            try:
                                d.shell(f"input text {arg}")
                                typed = True
                            except Exception:
                                typed = False
                    if not typed:
                        d.send_keys(text or "", clear=True)
                    # uiautomator2 may still use the clipboard for send_keys: never leave the value
                    # (often a password or code) there for other apps or keyboard history
                    try:
                        d.clipboard = ""
                    except Exception:
                        pass

            self._input(tap)
        elif kind == "back":
            self._input(lambda d: d.press("back"))
        elif kind == "home":
            self._input(lambda d: d.press("home"))
        elif kind == "recents":
            self._input(lambda d: d.press("recent"))
        elif kind == "notifications":
            self._input(lambda d: d.open_notification())
        elif kind == "hide_keyboard":
            self._input(lambda d: d.press("back"))  # back dismisses the IME without navigating
        elif kind == "scroll":
            direction = "up" if action.get("delta", 1) > 0 else "down"
            self._input(lambda d: d.swipe_ext(direction, scale=0.6))
        elif kind == "wait":
            sleep(0.3)
        sleep(0.15)

    def press(self, action: dict) -> None:
        """Touch down, hold briefly, release: for web-view elements that ignore a plain click."""
        l, t, r, b = action["bounds"]
        cx, cy = (l + r) // 2, (t + b) // 2

        def hold(d):
            d.touch.down(cx, cy)
            sleep(0.12)
            d.touch.up(cx, cy)

        self._input(hold)
        sleep(0.3)

    def screenshot(self, path: Path) -> Path:
        self._call(lambda d: d.screenshot(str(path)))
        return path

    def close(self) -> None:
        self.d = None
