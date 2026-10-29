"""jevd: long-lived local daemon that owns Jev sessions, device locks and the idle TTL.

Wire protocol (unix socket `data_dir/jevd.sock`): one JSON request line
`{"op": str, "args": dict}` -> one JSON response line `{"ok": true, "result": dict}`
or `{"ok": false, "error": str}`.
"""
import fcntl
import inspect
import json
import logging
import os
import signal
import socketserver
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from . import config, result
from .adb_link import AdbLink
from .config import Config
from .flows import FlowStore, slug
from .runner import Runner
from .targets.base import DeviceDisconnected, TargetBlocked

REQUIRED_ENV_VARS = ("TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY")
TARGETS = ("android", "chrome")
REAP_EVERY_S = 60
REQUEST_TIMEOUT_S = 30  # for reading the request line only, never for the run
MAX_REQUEST_BYTES = 1_000_000
CLOSE_WAIT_S = 1.0
RESUMABLE_ERRORS = ("device_disconnected", "stale_page", "interrupted")
TERMINAL_MSG = "session_terminal: money_action_uncertain; verify manually, then end_session"

log = logging.getLogger("jevd")


class OpError(Exception):
    """A request-level failure reported as `{"ok": false, "error": str(e)}`."""


@dataclass
class Session:
    session_id: str
    kind: str
    target: object
    start: str | None
    device_key: str | None  # android device lock key; None for chrome
    runner: Runner | None = None
    last_used: float = field(default_factory=time.time)
    lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False

    def idle_since(self) -> float:
        runner_used = self.runner.last_used if self.runner is not None else 0.0
        return max(self.last_used, runner_used)

    def status(self) -> str:
        return self.runner.status if self.runner is not None else "idle"

    def uncertain(self) -> bool:
        return bool(getattr(self.runner, "_money_uncertain", False))

    def settled(self) -> bool:
        """No pause awaiting the caller, no run in progress, not money-uncertain."""
        if self.runner is None:
            return True
        st = self.status()
        return not (st.startswith("paused") or st == "running" or self.uncertain())


class Daemon:
    def __init__(self, cfg: Config, target_factory=None, runner_factory=Runner):
        self.cfg = cfg
        self.link = AdbLink(cfg)  # one link for the daemon's lifetime: keeps the relay warm
        self.target_factory = target_factory or self._default_target
        self.runner_factory = runner_factory
        self.store = FlowStore(cfg.data_dir / "flows")
        self.sessions: dict[str, Session] = {}
        self.devices: dict[str, str] = {}  # android device key -> session_id holding it
        self.targets_created = 0
        self._lock = threading.Lock()  # guards `sessions` and `devices`

    # -- plumbing ---------------------------------------------------------

    def _default_target(self, target: str, url_or_app: str | None, device: str | None):
        if target == "android":
            from .targets.android import AndroidTarget
            return AndroidTarget(self.link, app=url_or_app)
        from .targets.chrome import ChromeTarget
        return ChromeTarget(url_or_app)

    def handle(self, request: dict) -> dict:
        op = request.get("op") if isinstance(request, dict) else None
        args = request.get("args") if isinstance(request, dict) else None
        if args is None:
            args = {}
        fn = getattr(self, f"op_{op}", None) if isinstance(op, str) else None
        if fn is None:
            log.info("op=%s error=unknown_op", op)
            return {"ok": False, "error": f"unknown_op: {op}"}
        if not isinstance(args, dict):
            return {"ok": False, "error": "bad_args: args must be an object"}
        sid = args.get("session_id")
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as e:
            log.info("op=%s session=%s error=bad_args", op, sid)
            return {"ok": False, "error": f"bad_args: {e}"}
        try:
            out = fn(**args)
        except OpError as e:
            log.info("op=%s session=%s error=%s", op, sid, str(e)[:120])
            return {"ok": False, "error": str(e)}
        except BaseException as e:  # never let one request take the server thread down
            log.error("op=%s session=%s crashed: %s", op, sid, type(e).__name__)
            return {"ok": False, "error": f"internal_error: {type(e).__name__}"}
        log.info("op=%s session=%s status=%s", op, out.get("session_id", sid), out.get("status", "ok"))
        return {"ok": True, "result": out}

    def _get(self, session_id) -> Session:
        with self._lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise OpError("unknown_session")
        return session

    def _drive(self, session: Session, fn) -> dict:
        """Run a Runner call under the session lock; BaseExceptions keep the session."""
        runner = session.runner
        try:
            return fn()
        except BaseException as e:
            # Runner already turned ordinary errors into results; this is a cancel/interrupt.
            log.warning("session=%s runner raised %s", session.session_id, type(e).__name__)
            if getattr(runner, "_money_uncertain", False):
                return result.build(runner)  # terminal: report it, resume keeps returning it
            if runner is not None:  # don't leave a stale "running" in status
                runner.status, runner.reason, runner.pending = "error", "interrupted", None
            raise OpError(f"interrupted: {type(e).__name__}") from e
        finally:
            session.last_used = time.time()

    def _close_session(self, session: Session) -> None:
        """Caller holds session.lock (or the session is otherwise idle)."""
        session.closed = True
        with self._lock:
            if self.sessions.get(session.session_id) is session:
                del self.sessions[session.session_id]
            if session.device_key and self.devices.get(session.device_key) == session.session_id:
                del self.devices[session.device_key]
        try:
            session.target.close()
        except Exception:
            log.exception("session=%s target close failed", session.session_id)

    # -- ops --------------------------------------------------------------

    def op_run_task(self, target: str, goal: str, url_or_app: str | None = None,
                    params: dict | None = None, money_params: list[str] | None = None,
                    flow: str | None = None, save_as: str | None = None, max_steps: int = 40,
                    session_id: str | None = None, device: str | None = None,
                    learn: bool = True) -> dict:
        missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
        if missing:
            raise OpError(f"Missing required environment variable(s): {', '.join(missing)} "
                          "(restart jevd after setting keys)")
        if target not in TARGETS:
            raise OpError(f"unknown_target: {target}")
        explicit = None
        if flow is not None:
            try:
                explicit = self.store.load(flow)
            except KeyError:
                raise OpError("unknown_flow") from None
        if save_as is not None:
            try:
                slug(save_as)
            except ValueError as e:
                raise OpError(f"bad_args: save_as: {e}") from None

        new = False
        if session_id is not None:
            session = self._get(session_id)
            if session.kind != target:
                raise OpError(f"target_mismatch: session {session_id} is {session.kind}")
            session.lock.acquire()
        else:
            if target == "chrome" and not url_or_app:
                raise OpError("bad_args: chrome needs url_or_app")
            session, new = self._claim(target, url_or_app, device)  # returned locked

        try:
            if session.closed:
                raise OpError("unknown_session")
            if session.uncertain():
                raise OpError(TERMINAL_MSG)
            start = url_or_app or session.start
            chosen = explicit
            if chosen is None and learn:
                chosen = self.store.find(target, start, goal)
            # A new run on an existing session gets a fresh Runner (new goal); a
            # money-uncertain session is terminal until end_session (checked above).
            try:
                session.runner = self.runner_factory(
                    session.session_id, session.target, goal, params, self.cfg,
                    max_steps=max_steps, money_params=money_params, flow=chosen,
                    save_as=save_as, store=self.store, start=start, learn=learn)
            except BaseException:
                if new:  # don't leave a runner-less session holding a device
                    self._close_session(session)
                raise
            return self._drive(session, session.runner.run)
        finally:
            session.lock.release()

    def _claim(self, target: str, url_or_app: str | None, device: str | None) -> tuple[Session, bool]:
        """A locked session for a run without session_id: (session, created).

        An android device held by a settled, idle session is reused (warm target),
        unless a different app was asked for — then that session is replaced."""
        key = (device or "default") if target == "android" else None
        while key is not None:
            with self._lock:
                holder_id = self.devices.get(key)
                if holder_id is None:
                    break
                holder = self.sessions.get(holder_id)
                if holder is None:  # still connecting
                    raise OpError(f"device_busy: {holder_id} (status=starting; "
                                  "pass session_id or end_session)")
                got = holder.lock.acquire(blocking=False)
                if not (got and holder.settled()):
                    if got:
                        holder.lock.release()
                    raise OpError(f"device_busy: {holder_id} (status={holder.status()}; "
                                  "pass session_id or end_session)")
            if not url_or_app or url_or_app == holder.start:
                return holder, False
            log.info("op=run_task session=%s replaced (other app)", holder_id)
            try:
                self._close_session(holder)
            finally:
                holder.lock.release()
        session = self._open(target, url_or_app, device)
        session.lock.acquire()  # fresh and unpublished to callers: never blocks
        return session, True

    def _open(self, target: str, url_or_app: str | None, device: str | None) -> Session:
        sid = uuid4().hex[:8]
        key = (device or "default") if target == "android" else None
        with self._lock:
            if key is not None:
                holder = self.devices.get(key)
                if holder is not None:
                    raise OpError(f"device_busy: {holder} (status=starting; "
                                  "pass session_id or end_session)")
                self.devices[key] = sid  # reserve before the (slow) connect
        try:
            t = self.target_factory(target, url_or_app, device)
        except BaseException as e:
            with self._lock:
                if key is not None and self.devices.get(key) == sid:
                    del self.devices[key]
            if isinstance(e, TargetBlocked):
                raise OpError(f"blocked: {e.reason}") from None
            if isinstance(e, DeviceDisconnected):
                raise OpError(f"device_disconnected: {e}"[:300]) from None
            if isinstance(e, Exception):
                raise OpError(f"target_error: {type(e).__name__}: {e}"[:300]) from None
            raise
        self.targets_created += 1
        session = Session(sid, target, t, url_or_app, key)
        with self._lock:
            self.sessions[sid] = session
        return session

    def op_resume(self, session_id: str, input: str | None = None,
                  instruction: str | None = None, confirm: bool = False) -> dict:
        session = self._get(session_id)
        with session.lock:
            if session.closed:
                raise OpError("unknown_session")
            if session.runner is None:
                raise OpError("no_run: call run_task with this session_id")
            runner = session.runner
            if (runner.status != "running" and not runner.status.startswith("paused")
                    and not runner._money_uncertain and not instruction
                    and not (runner.status == "error" and runner.reason in RESUMABLE_ERRORS)):
                # settled: report the last outcome (e.g. after a client disconnect), never act
                return result.build(runner)
            return self._drive(session, lambda: runner.resume(
                input=input, instruction=instruction, confirm=confirm))

    def op_screenshot(self, session_id: str) -> dict:
        session = self._get(session_id)
        with session.lock:
            if session.closed:
                raise OpError("unknown_session")
            folder = self.cfg.data_dir / "screens"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{session_id}-{int(time.time() * 1000)}.png"
            try:
                out = session.target.screenshot(path)
            except DeviceDisconnected:
                raise OpError("device_disconnected") from None
            except Exception as e:
                raise OpError(f"screenshot_failed: {type(e).__name__}") from None
            finally:
                session.last_used = time.time()
            return {"session_id": session_id, "path": str(out)}

    def op_end_session(self, session_id: str) -> dict:
        session = self._get(session_id)
        with session.lock:  # waits for an in-flight run: never close a target mid-action
            if session.closed:
                raise OpError("unknown_session")
            self._close_session(session)
        return {"session_id": session_id, "ended": True}

    def op_status(self) -> dict:
        now = time.time()
        with self._lock:
            sessions = list(self.sessions.values())
        return {
            "sessions": [{"session_id": s.session_id, "target": s.kind, "status": s.status(),
                          "idle_s": round(max(0.0, now - s.idle_since()), 1)}
                         for s in sessions],
            "android": {"serial": self.link.serial, "connected": self.link.serial is not None},
            "flows": len(self.store.list()),
        }

    def op_list_flows(self) -> dict:
        return {"flows": self.store.list()}

    def op_delete_flow(self, name: str) -> dict:
        try:
            self.store.delete(name)
        except KeyError:
            raise OpError("unknown_flow") from None
        return {"deleted": name}

    # -- lifecycle --------------------------------------------------------

    def reap(self, now: float) -> list[str]:
        """Close sessions idle longer than the TTL. Busy sessions are never idle."""
        with self._lock:
            sessions = list(self.sessions.values())
        reaped = []
        for s in sessions:
            if now - s.idle_since() <= self.cfg.session_ttl_s:
                continue
            if s.uncertain():
                continue  # money may have moved: kept until an explicit end_session
            if not s.lock.acquire(blocking=False):
                continue  # a call is in flight
            try:
                if not s.closed and now - s.idle_since() > self.cfg.session_ttl_s:
                    self._close_session(s)
                    reaped.append(s.session_id)
                    log.info("op=reap session=%s", s.session_id)
            finally:
                s.lock.release()
        return reaped

    def close(self) -> None:
        with self._lock:
            sessions = list(self.sessions.values())
        for s in sessions:
            got = s.lock.acquire(timeout=CLOSE_WAIT_S)
            if not got:
                log.warning("shutdown: session=%s busy (status=%s); closing its target anyway",
                            s.session_id, s.status())
            try:
                self._close_session(s)
            finally:
                if got:
                    s.lock.release()
        self.link.close()


def socket_path(cfg: Config) -> Path:
    return cfg.data_dir / "jevd.sock"


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(REQUEST_TIMEOUT_S)
        try:
            line = self.rfile.readline(MAX_REQUEST_BYTES)
        except TimeoutError:
            response = {"ok": False, "error": "request_timeout"}
        except OSError:
            return
        else:
            self.connection.settimeout(None)  # the run itself may take minutes
            if not line:
                return
            if len(line) >= MAX_REQUEST_BYTES and not line.endswith(b"\n"):
                response = {"ok": False, "error": "request_too_large"}
            else:
                try:
                    request = json.loads(line)
                except ValueError:
                    response = {"ok": False, "error": "bad_request: not JSON"}
                else:
                    response = self.server.daemon.handle(request)
        try:
            self.connection.settimeout(REQUEST_TIMEOUT_S)
            self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass  # client went away


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


def serve(on_ready=None) -> bool:
    """Run jevd until shutdown. Returns False at once if another jevd holds the lock."""
    cfg = config.load()
    lock_file = open(cfg.data_dir / "jevd.lock", "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return False
    handler = logging.FileHandler(cfg.data_dir / "jevd.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    path = socket_path(cfg)
    daemon = Daemon(cfg)
    stop = threading.Event()
    server = None
    try:
        path.unlink(missing_ok=True)  # we hold the lock: any socket file is stale
        old = os.umask(0o177)  # socket is owner-only: it can drive money actions
        try:
            server = _Server(str(path), _Handler)
        finally:
            os.umask(old)
        server.daemon = daemon

        def reaper():
            while not stop.wait(REAP_EVERY_S):
                try:
                    daemon.reap(time.time())
                except Exception:
                    log.exception("reaper failed")

        threading.Thread(target=reaper, daemon=True, name="jevd-reaper").start()
        log.info("jevd listening on %s (pid %d)", path, os.getpid())
        if threading.current_thread() is threading.main_thread():
            # SIGTERM: shut down cleanly (close targets, remove the socket)
            signal.signal(signal.SIGTERM,
                          lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        if on_ready is not None:
            on_ready(server)
        server.serve_forever()
    finally:
        stop.set()
        if server is not None:
            server.server_close()
        path.unlink(missing_ok=True)
        daemon.close()
        log.info("jevd stopped")
        log.removeHandler(handler)
        handler.close()
        lock_file.close()
    return True


if __name__ == "__main__":
    serve()
