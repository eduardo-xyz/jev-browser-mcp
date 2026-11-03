"""Daemon configuration: data directory, config.toml loading, the repo .env and the Chrome binding."""
import hashlib
import logging
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

log = logging.getLogger("jevd")
REPO_ROOT = Path(__file__).resolve().parent.parent
# browser_harness reads these at import time to pick the Chrome it attaches to (BU_CDP_URL: a
# remote-debugging endpoint; BU_NAME: its daemon socket name). config.toml keys they come from:
CHROME_ENV = (("chrome_cdp_url", "BU_CDP_URL"), ("chrome_label", "BU_NAME"))

# A unix socket path must fit the OS limit (104 chars on macOS); a long JEV_DATA_DIR would overflow it.
_SOCK_MAX = 100


def data_dir() -> Path:
    path = Path(os.environ.get("JEV_DATA_DIR") or Path.home() / ".local/share/jev-runner")
    path.mkdir(parents=True, exist_ok=True)
    return path


def socket_path(data: Path) -> Path:
    """The jevd socket for a data dir. Client and daemon derive it the same way; when the natural
    path is too long for AF_UNIX it moves to a short /tmp name keyed to the data dir (logs and
    flows stay put). Distinct data dirs get distinct sockets, so instances don't collide."""
    natural = data / "jevd.sock"
    if len(str(natural)) <= _SOCK_MAX:
        return natural
    return Path("/tmp") / f"jevd-{hashlib.sha1(str(data).encode()).hexdigest()[:12]}.sock"


@dataclass
class Config:
    android_serial: str | None = None
    android_mdns: str | None = None
    android_ip: str | None = None
    adb_tcp_port: int = 5555
    relay_port: int = 5556
    # Interpreter for the relay helper: must be one macOS Local Network privacy lets reach
    # the LAN (system or Homebrew python; uv-managed CPython is blocked).
    relay_python: str = "/usr/bin/python3"
    money_apps: list[str] = field(default_factory=list)  # opt-in via config.toml; nothing app-specific
    # Strong labels: guarded on any app/site, always. Bank transfers and web checkout alike.
    money_labels: list[str] = field(default_factory=lambda: [
        "Transferir", "Pagar", "Pagar ahora", "Confirmar pago", "Confirmar transferencia",
        "Pay", "Pay now", "Transfer", "Send money", "Confirm transfer", "Confirm payment",
        "Place order", "Place your order", "Buy now", "Buy it now", "Complete purchase",
        "Complete order", "Comprar ahora", "Finalizar compra", "Realizar pedido", "Realizar el pedido",
    ])
    # Contextual labels: guarded only with money params or on a configured money app.
    money_labels_contextual: list[str] = field(default_factory=lambda: [
        "Confirmar", "Enviar", "Send", "Confirm",
    ])
    secret_patterns: list[str] = field(default_factory=lambda: [
        "codigo", "code", "otp", "token", "pin", "clave",
        "contrasena", "password", "verification", "verificacion",
    ])
    money_exclude_words: list[str] = field(default_factory=lambda: [
        "codigo", "code", "otp", "token", "sms", "correo", "email",
        "cancelar", "cancel",
    ])
    own_accounts: list[str] = field(default_factory=list)
    # Exact button labels you have checked only open a menu or form and never move money
    # (e.g. a bank's "Transferir o pagar" tab). Whole-label match; never a money action.
    navigation_labels: list[str] = field(default_factory=list)
    # Labels of a control that transmits a message or forward (the final "send"). Guarded
    # one-per-run and never tap-retried, so an unchanged-looking screen can't duplicate a send
    # (a WhatsApp forward returns to a similar screen). Generic verbs only, never app names.
    send_labels: list[str] = field(default_factory=lambda: [
        "Send", "Enviar", "Enviar mensaje",
    ])
    # Labels of a control that destroys records (delete/remove/trash). Every such click pauses
    # for an explicit resume(confirm=True) and is never tap-retried, so a mis-aimed click can't
    # delete. Kept tight on purpose: "Archive"/"Cancel" are not here. Generic verbs only.
    destructive_labels: list[str] = field(default_factory=lambda: [
        "Delete", "Eliminar", "Borrar", "Remove", "Quitar", "Trash", "Move to trash",
        "Delete permanently", "Eliminar permanentemente",
    ])
    # Folders local files may be uploaded/pushed from (empty = the data dir's outbox/,
    # ~/Downloads, ~/Desktop, ~/Documents). Hidden and secret-looking files are always refused.
    file_roots: list[str] = field(default_factory=list)
    downloads_dir: str = "~/Downloads"  # where Chrome saves downloads (reported per run)
    session_ttl_s: int = 900
    # "keychain": type stored passwords/authenticator codes from the macOS Keychain (see
    # jev_runner/vault.py) into their own site/app's secret fields; "none": always pause and ask.
    vault: str = "keychain"
    reconnect_window_s: int = 30
    # Which Chrome this instance drives: the remote-debugging URL of a Chrome launched with
    # --remote-debugging-port (e.g. "http://127.0.0.1:9333") and a short label for its harness
    # daemon (e.g. "da"). Kept here, in the data dir, so the binding survives whoever starts jevd:
    # a daemon autostarted without BU_CDP_URL/BU_NAME in its environment used to attach to the
    # default Chrome silently. Unset: browser_harness discovers a Chrome itself.
    chrome_cdp_url: str | None = None
    chrome_label: str | None = None
    data_dir: Path = field(default_factory=data_dir)


LIST_FIELDS = ("money_apps", "money_labels", "money_labels_contextual",
               "money_exclude_words", "secret_patterns", "own_accounts", "navigation_labels",
               "send_labels", "destructive_labels", "file_roots")


def load() -> Config:
    base = data_dir()
    path = base / "config.toml"
    values = tomllib.loads(path.read_text()) if path.exists() else {}
    known = {f.name for f in fields(Config)} - {"data_dir"}
    for key in LIST_FIELDS:
        # fail closed: a mistyped guard list must not silently disable a guard
        v = values.get(key)
        if key in values and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise ValueError(f"config.toml: {key} must be a list of strings")
    return Config(data_dir=base, **{k: v for k, v in values.items() if k in known})


def dotenv_values(path: Path | None = None) -> dict[str, str]:
    """KEY=value lines of the repo's .env (comments and blanks skipped, simple quotes stripped)."""
    path = path or REPO_ROOT / ".env"
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("export "):
            k = k[7:].strip()
        if k:
            out[k] = v.strip().strip('"').strip("'")
    return out


def load_dotenv(environ=None, path: Path | None = None) -> list[str]:
    """Fill missing variables from the repo .env (API keys, model names). Values already in the
    environment win. Returns the names that were added. A jevd started by hand or by a client
    whose host didn't pass --env-file used to run without any model key."""
    environ = os.environ if environ is None else environ
    added = []
    for k, v in dotenv_values(path).items():
        if k not in environ:
            environ[k] = v
            added.append(k)
    return added


def apply_chrome_binding(cfg: "Config", environ=None) -> dict:
    """Export the Chrome binding from config.toml for browser_harness. The data dir's config is
    authoritative: an environment value that differs is overridden (and logged), so the Chrome a
    jevd drives depends only on its data dir, never on who started it. Returns the binding."""
    environ = os.environ if environ is None else environ
    for attr, var in CHROME_ENV:
        value = getattr(cfg, attr)
        if not value:
            continue
        current = environ.get(var)
        if current and current != value:
            log.warning("%s=%s from the environment ignored: config.toml says %s = %r",
                        var, current, attr, value)
        environ[var] = value
    return chrome_binding(environ)


def chrome_binding(environ=None) -> dict:
    environ = os.environ if environ is None else environ
    return {"cdp_url": environ.get("BU_CDP_URL") or None,
            "label": environ.get("BU_NAME") or None}
