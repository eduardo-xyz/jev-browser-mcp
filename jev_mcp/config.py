"""Daemon configuration: data directory and config.toml loading."""
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def data_dir() -> Path:
    path = Path(os.environ.get("JEV_DATA_DIR") or Path.home() / ".local/share/jev-mcp")
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Config:
    android_serial: str | None = None
    android_mdns: str | None = None
    android_ip: str | None = None
    adb_tcp_port: int = 5555
    relay_port: int = 5556
    money_apps: list[str] = field(default_factory=list)  # opt-in via config.toml; nothing app-specific
    money_labels: list[str] = field(default_factory=lambda: [
        "Transferir", "Pagar", "Confirmar", "Confirmar pago", "Enviar",
        "Pay", "Send", "Transfer", "Confirm",
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
    session_ttl_s: int = 900
    reconnect_window_s: int = 30
    data_dir: Path = field(default_factory=data_dir)


def load() -> Config:
    base = data_dir()
    path = base / "config.toml"
    values = tomllib.loads(path.read_text()) if path.exists() else {}
    known = {f.name for f in fields(Config)} - {"data_dir"}
    return Config(data_dir=base, **{k: v for k, v in values.items() if k in known})
