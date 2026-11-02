"""Register jev-runner's Chrome tabs with AI Broker so its Tab Groups extension names and colours
them (one named, coloured group per jev-runner instance).

Fully optional and best-effort: with no broker token or ``BU_NAME`` every call is a silent no-op and
a run is never affected. Mirrors the contract that ``relay.mjs`` uses for chrome-devtools-mcp:
POST ``/rpc`` with a Bearer profile token and operation ``browser_tab_claim`` right after a CDP
target is created, and periodic ``browser_activity`` so the broker's sweep doesn't close an in-use
tab. jev-runner drives Chrome directly (not through relay.mjs), so it must claim its own targets.

Config (all via env, matching the BU_* launch convention):
- ``BU_NAME``            instance label (e.g. ``da``/``pe``); maps to identity ``claude-<BU_NAME>-cli``
- ``JEV_BROKER_URL``     broker base URL (default ``http://127.0.0.1:18765``)
- ``JEV_BROKER_TOKEN_DIR`` dir holding ``<identity>.token`` (default the AI-Broker repo's .broker/cli)

The token is read from disk at call time and never logged or returned.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path

_log = logging.getLogger("jevd")

_DEFAULT_TOKEN_DIR = Path.home() / "Projects" / "Personal" / "AI-Broker" / ".broker" / "cli"
_NAME_OK = re.compile(r"[^A-Za-z0-9 ._-]")


def _broker_url() -> str:
    return (os.environ.get("JEV_BROKER_URL") or "http://127.0.0.1:18765").rstrip("/")


def _token_dir() -> Path:
    return Path(os.environ.get("JEV_BROKER_TOKEN_DIR") or _DEFAULT_TOKEN_DIR)


def _bu_name() -> str:
    return (os.environ.get("BU_NAME") or "").strip()


def group_name() -> str:
    """The tab-group label. Defaults to 'JEV <BU_NAME>'; set JEV_GROUP_NAME to show the session's own
    name instead (BU_NAME stays the token-lookup key, so it can't double as the label). Chrome renders
    group titles in uppercase — that's display-only. The extension derives the colour from this name."""
    raw = (os.environ.get("JEV_GROUP_NAME") or f"JEV {_bu_name()}").strip()
    return _NAME_OK.sub("", raw)[:40] or "JEV"


def _token() -> str:
    bu = _bu_name()
    if not bu:
        return ""
    try:
        return (_token_dir() / f"claude-{bu}-cli.token").read_text().strip()
    except OSError:
        return ""


def enabled() -> bool:
    """True when a claim could actually be made (a token exists for this instance)."""
    return bool(_token())


def _rpc(operation: str, arguments: dict, token: str) -> None:
    body = json.dumps({"operation": operation, "arguments": arguments}).encode()
    req = urllib.request.Request(
        f"{_broker_url()}/rpc", data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 (fixed localhost broker)
        resp.read()


def claim(target_id, *, attempts: int = 30, delay: float = 2.0) -> None:
    """Claim a CDP target so the extension groups its tab. Runs in a background thread and never
    raises: a failed or disabled claim must not affect the run (mirrors relay.mjs's retry)."""
    if not isinstance(target_id, str) or not target_id:
        return
    token = _token()
    if not token:
        return
    args = {"target_id": target_id, "relay_pid": os.getpid(), "name": group_name()}

    def run():
        for attempt in range(attempts):
            try:
                _rpc("browser_tab_claim", args, token)
                return
            except Exception as exc:  # broker down / 409 / network — keep retrying, never crash
                if attempt == 0:
                    _log.info("tab_groups: claim failed, retrying: %s", exc)
                time.sleep(delay)

    threading.Thread(target=run, name="jev-tab-claim", daemon=True).start()


def activity(target_ids) -> None:
    """Heartbeat so the broker's idle sweep doesn't close an in-use tab. Best-effort, never raises."""
    ids = [t for t in (target_ids or []) if isinstance(t, str) and t]
    if not ids:
        return
    token = _token()
    if not token:
        return
    try:
        _rpc("browser_activity", {"target_ids": ids[:100], "relay_pid": os.getpid()}, token)
    except Exception:
        pass
