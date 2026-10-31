"""jevd client: one request per connection; autostarts the daemon when it isn't running."""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import config

START_TIMEOUT_S = 5.0
POLL_S = 0.05
REPO_ROOT = Path(__file__).resolve().parent.parent


def _socket_path() -> Path:
    return config.socket_path(config.data_dir())


def _connect(path: Path) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(path))
    except BaseException:
        s.close()
        raise
    return s


def _spawn(stderr_path: Path) -> None:
    with open(stderr_path, "ab") as err:  # the child keeps its own copy of the fd
        subprocess.Popen(
            [sys.executable, "-m", "jev_runner.daemon"],
            cwd=REPO_ROOT,  # jev_runner is imported from the repo, not an installed package
            stdin=subprocess.DEVNULL, stdout=err, stderr=err,
            start_new_session=True,  # survives the MCP server; inherits env (API keys)
        )


def _connect_or_start(path: Path) -> socket.socket:
    try:
        return _connect(path)
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    stderr_path = path.parent / "jevd.stderr"
    _spawn(stderr_path)
    deadline = time.monotonic() + START_TIMEOUT_S
    while True:
        try:
            return _connect(path)
        except (FileNotFoundError, ConnectionRefusedError):
            if time.monotonic() >= deadline:
                raise RuntimeError(f"jevd did not start within {START_TIMEOUT_S:g}s "
                                   f"(see {stderr_path} and {path.parent / 'jevd.log'})") from None
            time.sleep(POLL_S)


def call(op: str, args: dict) -> dict:
    """Send one request; return its `result` or raise RuntimeError(error)."""
    path = _socket_path()
    try:
        with _connect_or_start(path) as s:
            s.sendall((json.dumps({"op": op, "args": args}) + "\n").encode())
            with s.makefile("rb") as f:
                line = f.readline()  # no timeout: a run can take minutes
    except (ConnectionResetError, BrokenPipeError) as e:
        raise RuntimeError(f"jevd connection lost: {type(e).__name__}") from None
    if not line:
        raise RuntimeError("jevd closed the connection without a response")
    try:
        response = json.loads(line)
    except ValueError:
        raise RuntimeError("jevd sent a malformed response") from None
    if not isinstance(response, dict):
        raise RuntimeError("jevd sent a malformed response")
    if not response.get("ok"):
        raise RuntimeError(response.get("error") or "jevd error")
    return response["result"]
