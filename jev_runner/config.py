"""Daemon configuration: data directory and config.toml loading."""
import hashlib
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

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
    session_ttl_s: int = 900
    reconnect_window_s: int = 30
    data_dir: Path = field(default_factory=data_dir)


LIST_FIELDS = ("money_apps", "money_labels", "money_labels_contextual",
               "money_exclude_words", "secret_patterns", "own_accounts", "navigation_labels",
               "send_labels", "destructive_labels")


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
