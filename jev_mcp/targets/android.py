"""AndroidTarget: reads the screen and drives input through a persistent uiautomator2 session."""
import re
from pathlib import Path
from time import sleep

from ..android_tree import parse_hierarchy
from ..guards import normalize
from .base import DeviceDisconnected, StalePage, TargetBlocked

_LAUNCHER_CMD = (
    "cmd package query-activities --brief -a android.intent.action.MAIN "
    "-c android.intent.category.LAUNCHER"
)
_COMPONENT = re.compile(r"^(\S+?)/\S+$")


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


def resolve_app(d, name: str, cache: dict | None = None) -> str:
    """Resolve a human app name to a package among launchable apps."""
    cache = cache if cache is not None else {}
    pkgs = []
    for line in _shell_text(d.shell(_LAUNCHER_CMD)).splitlines():
        m = _COMPONENT.match(line.strip())
        if m and m.group(1) not in pkgs:
            pkgs.append(m.group(1))
    labels = {}
    for pkg in pkgs:
        if pkg not in cache:
            try:
                cache[pkg] = (d.app_info(pkg) or {}).get("label") or ""
            except Exception:
                cache[pkg] = ""
        labels[pkg] = cache[pkg]
    want = normalize(name)
    exact = [p for p, l in labels.items() if l and normalize(l) == want]
    if not exact:
        exact = [p for p, l in labels.items() if l and normalize(l).startswith(want)]
    if not exact:
        exact = [p for p, l in labels.items() if l and want in normalize(l)]
    if not exact:
        raise TargetBlocked(f"app_not_found: {name}")
    if len(exact) > 1:
        raise TargetBlocked("app_ambiguous: " + ", ".join(sorted(labels[p] for p in exact)))
    return exact[0]


def _default_connect(serial):
    import uiautomator2

    return uiautomator2.connect(serial)


class AndroidTarget:
    kind = "android"

    def __init__(self, link, app: str | None = None, connect=None):
        self.link = link
        self._connect = connect or _default_connect
        self._labels: dict = {}
        self.d = None
        self._dev()
        if app:
            pkg = app if "." in app else resolve_app(self.d, app, self._labels)
            if (self._call(lambda d: d.app_current()) or {}).get("package") != pkg:
                self._call(lambda d: d.app_start(pkg))

    def _dev(self):
        if self.d is None:
            self.d = self._connect(self.link.ensure_connected())
        return self.d

    def _call(self, fn):
        """Run fn(device); on a connection error reconnect once and retry."""
        try:
            return fn(self._dev())
        except _CONN_ERRORS:
            self.d = None
        return fn(self._dev())  # ensure_connected raises DeviceDisconnected if it can't

    def observe(self) -> dict:
        def read(d):
            info = d.app_current()
            xml = d.dump_hierarchy(compressed=False)
            p = parse_hierarchy(xml, info["package"], info["activity"])
            if p["shade_open"]:
                p = parse_hierarchy(xml, info["package"], info["activity"], include_system_ui=True)
            return p

        return self._call(read)

    def fresh(self, page: dict) -> bool:
        return self.observe()["fingerprint"] == page["fingerprint"]

    def _input(self, fn):
        """Send input once. A connection error here is never retried: input may have landed."""
        d = self._dev()
        try:
            fn(d)
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
                    d.send_keys(text or "", clear=True)

            self._input(tap)
        elif kind == "back":
            self._input(lambda d: d.press("back"))
        elif kind == "home":
            self._input(lambda d: d.press("home"))
        elif kind == "recents":
            self._input(lambda d: d.press("recent"))
        elif kind == "notifications":
            self._input(lambda d: d.open_notification())
        elif kind == "scroll":
            direction = "up" if action.get("delta", 1) > 0 else "down"
            self._input(lambda d: d.swipe_ext(direction, scale=0.6))
        elif kind == "wait":
            sleep(0.3)
        sleep(0.15)

    def screenshot(self, path: Path) -> Path:
        self._call(lambda d: d.screenshot(str(path)))
        return path

    def close(self) -> None:
        self.d = None
