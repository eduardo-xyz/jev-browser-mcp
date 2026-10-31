import json
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from jev_runner import client, daemon
from jev_runner.config import Config
from jev_runner.daemon import Daemon
from jev_runner.flows import Flow, FlowStore
from jev_runner.runner import Runner
from jev_runner.targets.base import DeviceDisconnected, TargetBlocked
from tests.fakes import FakeTarget, click, fill, page, scripted_choose

RECIPIENT = "CR05015202001026284066"


class TrackedTarget(FakeTarget):
    def __init__(self, kind, pages, raise_once=None, raise_always=None):
        super().__init__(pages)
        self.kind = kind
        self.closed = False
        self._raise_once = raise_once
        self._raise_always = raise_always

    def act(self, action, page, text=None, verify=False):
        if self._raise_always is not None:
            raise self._raise_always
        if self._raise_once is not None:
            e, self._raise_once = self._raise_once, None
            raise e
        super().act(action, page, text=text, verify=verify)

    def close(self):
        self.closed = True


def otp_pages():
    return [page(fill("n1", "OTP")), page(click("n2", "Ok")), page()]


class Script:
    """Per-test knobs for the fake target/runner factories."""

    def __init__(self):
        self.pages = otp_pages
        self.choices = [["n1", "n2", "DONE"]]
        self.raise_once = None
        self.raise_always = None
        self.targets: list[TrackedTarget] = []
        self.factory_error: BaseException | None = None
        self.factory_calls: list[tuple] = []

    def target_factory(self, target, url_or_app, device):
        self.factory_calls.append((target, url_or_app, device))
        if self.factory_error is not None:
            raise self.factory_error
        t = TrackedTarget(target, self.pages(), self.raise_once, self.raise_always)
        self.targets.append(t)
        return t

    def runner_factory(self, *a, **kw):
        choices = self.choices.pop(0) if len(self.choices) > 1 else self.choices[0]
        return Runner(*a, choose=scripted_choose(list(choices)), **kw)


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k1")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "k2")


@pytest.fixture
def script():
    return Script()


@pytest.fixture
def daemon_with_fakes(cfg, script):
    d = Daemon(cfg, target_factory=script.target_factory, runner_factory=script.runner_factory)
    d.script = script
    yield d
    d.close()


def run(d, **args):
    return d.handle({"op": "run_task", "args": args})


def test_run_then_resume_same_session(daemon_with_fakes, keys):
    r = daemon_with_fakes.handle({"op": "run_task", "args": {"target": "android", "goal": "otp"}})
    assert r["ok"] and r["result"]["status"] == "paused_needs_input"
    sid = r["result"]["session_id"]
    r2 = daemon_with_fakes.handle({"op": "resume", "args": {"session_id": sid, "input": "111222"}})
    assert r2["result"]["session_id"] == sid and r2["result"]["status"] == "done"
    assert "111222" not in json.dumps(r2)
    assert len(sid) == 8


def test_missing_keys_error_before_target(daemon_with_fakes, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False); monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    r = daemon_with_fakes.handle({"op": "run_task", "args": {"target": "chrome", "goal": "g", "url_or_app": "https://x"}})
    assert not r["ok"] and "TYPESAFE_API_KEY" in r["error"] and "TEXT_MODEL_API_KEY" in r["error"]
    assert daemon_with_fakes.targets_created == 0


def test_device_busy(daemon_with_fakes, keys):
    first = run(daemon_with_fakes, target="android", goal="otp")
    sid = first["result"]["session_id"]
    assert first["result"]["status"] == "paused_needs_input"
    second = run(daemon_with_fakes, target="android", goal="otp")
    assert second == {"ok": False, "error": f"device_busy: {sid} (status=paused_needs_input; "
                                            "pass session_id or end_session)"}
    assert daemon_with_fakes.targets_created == 1
    # every android session shares one device lock, whatever `device` alias is passed
    r = run(daemon_with_fakes, target="android", goal="otp", device="default")
    assert r["error"].startswith(f"device_busy: {sid}")
    # chrome is never locked
    assert run(daemon_with_fakes, target="chrome", goal="otp", url_or_app="https://x")["ok"]
    # ending the holder frees the device
    assert daemon_with_fakes.handle({"op": "end_session", "args": {"session_id": sid}})["ok"]
    assert run(daemon_with_fakes, target="android", goal="otp")["ok"]


def test_reuse_session_with_new_goal(daemon_with_fakes, keys):
    daemon_with_fakes.script.choices = [["n1"], ["DONE"]]
    sid = run(daemon_with_fakes, target="android", goal="otp")["result"]["session_id"]
    r = run(daemon_with_fakes, target="android", goal="something else", session_id=sid)
    assert r["ok"] and r["result"]["session_id"] == sid and r["result"]["status"] == "done"
    assert daemon_with_fakes.targets_created == 1  # same target reused


def test_reap_closes_idle_sessions(daemon_with_fakes, keys):
    run(daemon_with_fakes, target="android", goal="otp")
    target = daemon_with_fakes.script.targets[0]
    daemon_with_fakes.reap(time.time() + 60)
    assert not target.closed
    daemon_with_fakes.reap(time.time() + 901)
    assert target.closed
    status = daemon_with_fakes.handle({"op": "status", "args": {}})
    assert status["ok"] and status["result"]["sessions"] == []
    # the device lock is released too
    assert run(daemon_with_fakes, target="android", goal="otp")["ok"]


def test_disconnected_session_survives(daemon_with_fakes, keys):
    s = daemon_with_fakes.script
    s.pages = lambda: [page(click("n1", "Go")), page()]
    s.choices = [["n1", "n1", "DONE"]]
    s.raise_once = DeviceDisconnected("gone")
    r = run(daemon_with_fakes, target="android", goal="go")
    assert r["ok"] and r["result"]["status"] == "error"
    assert r["result"]["reason"] == "device_disconnected"
    sid = r["result"]["session_id"]
    listed = daemon_with_fakes.handle({"op": "status", "args": {}})["result"]["sessions"]
    assert [x["session_id"] for x in listed] == [sid] and listed[0]["status"] == "error"
    r2 = daemon_with_fakes.handle({"op": "resume", "args": {"session_id": sid}})
    assert r2["ok"] and r2["result"]["session_id"] == sid and r2["result"]["status"] == "done"


def test_base_exception_keeps_terminal_session(daemon_with_fakes, keys):
    s = daemon_with_fakes.script
    s.pages = lambda: [page(click("n9", "Transferir"), text="Destino ****4066 Monto ₡150.000,00"), page()]
    s.choices = [["n9", "DONE"]]
    s.raise_always = KeyboardInterrupt()
    r = run(daemon_with_fakes, target="android", goal="pay",
            params={"amount": 150000, "recipient": RECIPIENT})
    assert r["ok"] and r["result"]["status"] == "error"
    assert r["result"]["reason"] == "money_action_uncertain"
    sid = r["result"]["session_id"]
    again = daemon_with_fakes.handle({"op": "resume", "args": {"session_id": sid, "confirm": True}})
    assert again["result"]["reason"] == "money_action_uncertain"
    assert len(daemon_with_fakes.script.targets[0].acted) == 0


def test_base_exception_outside_money_is_an_error_not_a_crash(daemon_with_fakes, keys):
    s = daemon_with_fakes.script
    s.pages = lambda: [page(click("n1", "Go")), page()]
    s.choices = [["n1", "DONE"]]
    s.raise_once = KeyboardInterrupt()
    r = run(daemon_with_fakes, target="android", goal="go")
    assert not r["ok"] and "KeyboardInterrupt" in r["error"]
    listed = daemon_with_fakes.handle({"op": "status", "args": {}})["result"]["sessions"]
    sid = listed[0]["session_id"]
    assert listed[0]["status"] == "error"  # not a stale "running"
    assert daemon_with_fakes.sessions[sid].runner.reason == "interrupted"
    s.choices = [["n1", "DONE"]]
    assert daemon_with_fakes.handle({"op": "resume", "args": {"session_id": sid}})["ok"]


def test_unknown_session_and_flow(daemon_with_fakes, keys):
    for op in ("resume", "screenshot", "end_session"):
        r = daemon_with_fakes.handle({"op": op, "args": {"session_id": "nope"}})
        assert r == {"ok": False, "error": "unknown_session"}
    assert run(daemon_with_fakes, target="android", goal="g", session_id="nope") == \
        {"ok": False, "error": "unknown_session"}
    r = run(daemon_with_fakes, target="android", goal="g", flow="missing")
    assert r == {"ok": False, "error": "unknown_flow"}
    assert daemon_with_fakes.targets_created == 0
    r = daemon_with_fakes.handle({"op": "delete_flow", "args": {"name": "missing"}})
    assert r == {"ok": False, "error": "unknown_flow"}


def test_bad_requests(daemon_with_fakes, keys):
    assert daemon_with_fakes.handle({"op": "fly", "args": {}}) == {"ok": False, "error": "unknown_op: fly"}
    r = daemon_with_fakes.handle({"op": "run_task", "args": {"goal": "g"}})
    assert not r["ok"] and r["error"].startswith("bad_args")
    r = run(daemon_with_fakes, target="ios", goal="g")
    assert r == {"ok": False, "error": "unknown_target: ios"}
    r = run(daemon_with_fakes, target="chrome", goal="g")
    assert not r["ok"] and "url_or_app" in r["error"]


def test_target_construction_failure_is_an_error_and_releases_device(daemon_with_fakes, keys):
    s = daemon_with_fakes.script
    s.factory_error = DeviceDisconnected("no device")
    r = run(daemon_with_fakes, target="android", goal="g")
    assert not r["ok"] and r["error"].startswith("device_disconnected")
    s.factory_error = TargetBlocked("login_wall")
    r = run(daemon_with_fakes, target="android", goal="g")
    assert r == {"ok": False, "error": "blocked: login_wall"}
    s.factory_error = None
    assert run(daemon_with_fakes, target="android", goal="otp")["ok"]


def test_screenshot_writes_under_screens(daemon_with_fakes, keys, cfg):
    sid = run(daemon_with_fakes, target="android", goal="otp")["result"]["session_id"]
    r = daemon_with_fakes.handle({"op": "screenshot", "args": {"session_id": sid}})
    path = Path(r["result"]["path"])
    assert r["ok"] and path.parent == cfg.data_dir / "screens" and path.exists()


def test_read_falls_back_to_observe_text_for_plain_target(daemon_with_fakes, keys):
    sid = run(daemon_with_fakes, target="android", goal="otp")["result"]["session_id"]
    r = daemon_with_fakes.handle({"op": "read", "args": {"session_id": sid}})
    assert r["ok"] and r["result"]["session_id"] == sid
    assert "text" in r["result"] and r["result"]["frames"] == []


def test_read_uses_target_reader_when_present(daemon_with_fakes, keys):
    sid = run(daemon_with_fakes, target="android", goal="otp")["result"]["session_id"]
    daemon_with_fakes.sessions[sid].target.read = lambda: {
        "url": "u", "text": "iframe stuff", "frames": [{"url": "f", "text": "x"}], "readable": True}
    r = daemon_with_fakes.handle({"op": "read", "args": {"session_id": sid}})
    assert r["ok"] and r["result"]["text"] == "iframe stuff"
    assert r["result"]["frames"] == [{"url": "f", "text": "x"}]


def test_record_run_terminal_scrubs_and_skips_paused(daemon_with_fakes):
    d = daemon_with_fakes

    class R:
        _goal_raw = "pay 148000 to 88244852"
        verified = True
        history = [1, 2, 3]
        loop_events = 2
        goal = "g"

    class S:
        session_id = "s1"
        kind = "android"
        start = "net.bac.sbe.android"
        runner = R()

    d._record_run(S(), {"status": "paused_needs_input"})
    assert d.memory._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0  # pause not recorded
    d._record_run(S(), {"status": "done", "elapsed_ms": 4200, "flow_saved": "f"})
    row = d.memory._conn.execute("SELECT * FROM runs").fetchone()
    assert row["status"] == "done" and row["steps"] == 3 and row["verified"] == 1
    assert row["loop_events"] == 2 and row["duration_ms"] == 4200 and row["flow_saved"] == "f"
    assert "88244852" not in row["goal"] and "148000" not in row["goal"]  # PII scrubbed


def test_run_through_daemon_records_metrics(daemon_with_fakes, keys, script):
    script.pages = lambda: [page(click("n1", "Next")), page()]
    script.choices = [["n1", "DONE"]]
    r = run(daemon_with_fakes, target="android", goal="navigate somewhere")
    assert r["result"]["status"] == "done"
    assert daemon_with_fakes.memory._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_flows_listed_used_and_deleted(daemon_with_fakes, keys, cfg):
    store = FlowStore(cfg.data_dir / "flows")
    store.save(Flow(name="otp-flow", target="android", start=None, goal="otp"))
    st = daemon_with_fakes.handle({"op": "status", "args": {}})["result"]
    assert st["flows"] == 1 and st["android"] == {"serial": None, "connected": False}
    flows = daemon_with_fakes.handle({"op": "list_flows", "args": {}})["result"]["flows"]
    assert [f["name"] for f in flows] == ["otp-flow"]
    r = run(daemon_with_fakes, target="android", goal="otp", flow="otp-flow")
    assert r["result"]["flow_used"] == "otp-flow"
    daemon_with_fakes.handle({"op": "end_session", "args": {"session_id": r["result"]["session_id"]}})
    # learn: a matching saved flow is found automatically; learn=False skips it
    r = run(daemon_with_fakes, target="android", goal="otp")
    assert r["result"]["flow_used"] == "otp-flow"
    daemon_with_fakes.handle({"op": "end_session", "args": {"session_id": r["result"]["session_id"]}})
    r = run(daemon_with_fakes, target="android", goal="otp", learn=False)
    assert "flow_used" not in r["result"]
    assert daemon_with_fakes.handle({"op": "delete_flow", "args": {"name": "otp-flow"}}) == \
        {"ok": True, "result": {"deleted": "otp-flow"}}
    assert daemon_with_fakes.handle({"op": "list_flows", "args": {}})["result"]["flows"] == []


def test_concurrent_calls_on_one_session_serialize(daemon_with_fakes, keys):
    sid = run(daemon_with_fakes, target="android", goal="otp")["result"]["session_id"]
    session = daemon_with_fakes.sessions[sid]
    inside, overlap = [], []

    def slow_screenshot(path, orig=session.target.screenshot):
        if inside:
            overlap.append(True)
        inside.append(True)
        time.sleep(0.05)
        inside.pop()
        return orig(path)

    session.target.screenshot = slow_screenshot
    threads = [threading.Thread(target=daemon_with_fakes.handle,
                                args=({"op": "screenshot", "args": {"session_id": sid}},))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap == []


def test_secret_input_never_logged(daemon_with_fakes, keys, caplog):
    caplog.set_level("DEBUG", logger="jevd")
    sid = run(daemon_with_fakes, target="android", goal="otp",
              params={"amount": 98765})["result"]["session_id"]
    daemon_with_fakes.handle({"op": "resume", "args": {"session_id": sid, "input": "424242"}})
    assert "424242" not in caplog.text and "98765" not in caplog.text
    assert sid in caplog.text and "resume" in caplog.text


@pytest.fixture
def short_dir(monkeypatch):
    d = tempfile.mkdtemp(dir="/tmp", prefix="jevd")
    monkeypatch.setenv("JEV_DATA_DIR", d)
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


def _start_server(started):
    holder = {}
    ready = threading.Event()

    def on_ready(server):
        holder["server"] = server
        ready.set()

    th = threading.Thread(target=daemon.serve, kwargs={"on_ready": on_ready}, daemon=True)
    th.start()
    started.append((th, holder))
    assert ready.wait(5)
    return holder["server"], th


def test_socket_roundtrip(short_dir):
    started = []
    server, th = _start_server(started)
    try:
        sock = short_dir / "jevd.sock"
        assert sock.exists() and (sock.stat().st_mode & 0o077) == 0
        result = client.call("status", {})
        assert result["sessions"] == [] and result["flows"] == 0
        with pytest.raises(RuntimeError, match="unknown_session"):
            client.call("resume", {"session_id": "nope"})
        # malformed line gets an error response, server keeps going
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(sock))
        s.sendall(b"not json\n")
        resp = json.loads(s.makefile().readline())
        s.close()
        assert resp["ok"] is False
        assert client.call("list_flows", {}) == {"flows": []}
    finally:
        server.shutdown()
        th.join(5)
    assert not th.is_alive()
    assert not (short_dir / "jevd.sock").exists()
    assert (short_dir / "jevd.log").exists()


def test_client_autostarts_daemon(short_dir, monkeypatch):
    spawned, started = [], []

    def fake_popen(cmd, **kw):
        spawned.append((cmd, kw))
        _start_server(started)
        return object()

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    try:
        assert client.call("status", {})["sessions"] == []
        cmd, kw = spawned[0]
        assert cmd[1:] == ["-m", "jev_runner.daemon"] and kw["start_new_session"] is True
        assert client.call("status", {})["flows"] == 0
        assert len(spawned) == 1  # second call reuses the running daemon
    finally:
        for th, holder in started:
            holder["server"].shutdown()
            th.join(5)


def test_client_times_out_when_daemon_never_starts(short_dir, monkeypatch):
    monkeypatch.setattr(client.subprocess, "Popen", lambda cmd, **kw: object())
    monkeypatch.setattr(client, "START_TIMEOUT_S", 0.3)
    with pytest.raises(RuntimeError, match="jevd"):
        client.call("status", {})


def test_second_serve_exits_while_first_runs(short_dir):
    started = []
    server, th = _start_server(started)
    try:
        assert daemon.serve() is False  # lock held: returns immediately
        assert client.call("status", {})["sessions"] == []
    finally:
        server.shutdown()
        th.join(5)


def test_runner_construction_failure_frees_new_session(cfg, script, keys):
    def boom(*a, **kw):
        raise RuntimeError("bad runner")

    d = Daemon(cfg, target_factory=script.target_factory, runner_factory=boom)
    r = run(d, target="android", goal="g")
    assert not r["ok"] and r["error"] == "internal_error: RuntimeError"
    assert d.sessions == {} and d.devices == {} and script.targets[0].closed


def _uncertain_session(d):
    s = d.script
    s.pages = lambda: [page(click("n9", "Transferir"), text="Destino ****4066 Monto ₡150.000,00"), page()]
    s.choices = [["n9", "DONE"]]
    s.raise_always = KeyboardInterrupt()
    r = run(d, target="android", goal="pay", params={"amount": 150000, "recipient": RECIPIENT})
    assert r["result"]["reason"] == "money_action_uncertain"
    s.raise_always = None
    s.pages = otp_pages
    s.choices = [["n1", "n2", "DONE"]]
    return r["result"]["session_id"]


def test_uncertain_session_is_terminal_for_run_task_and_reap(daemon_with_fakes, keys):
    d = daemon_with_fakes
    sid = _uncertain_session(d)
    r = run(d, target="android", goal="pay again", session_id=sid)
    assert r == {"ok": False,
                 "error": "session_terminal: money_action_uncertain; verify manually, then end_session"}
    r = run(d, target="android", goal="otp")
    assert r == {"ok": False, "error": f"device_busy: {sid} (status=error; pass session_id or end_session)"}
    assert d.reap(time.time() + 10_000) == []
    assert not d.script.targets[0].closed
    assert [x["session_id"] for x in d.handle({"op": "status", "args": {}})["result"]["sessions"]] == [sid]
    assert d.handle({"op": "end_session", "args": {"session_id": sid}})["ok"]
    assert run(d, target="android", goal="otp")["ok"]


def test_settled_holder_is_reused_without_session_id(daemon_with_fakes, keys):
    d = daemon_with_fakes
    d.script.choices = [["DONE"], ["DONE"]]
    sid = run(d, target="android", goal="a", url_or_app="com.app")["result"]["session_id"]
    r = run(d, target="android", goal="b", url_or_app="com.app")
    assert r["ok"] and r["result"]["session_id"] == sid and r["result"]["status"] == "done"
    assert d.targets_created == 1
    r = run(d, target="android", goal="c")  # no url_or_app: keep the warm target
    assert r["result"]["session_id"] == sid and d.targets_created == 1


def test_settled_error_holder_is_reused(daemon_with_fakes, keys):
    d = daemon_with_fakes
    s = d.script
    s.pages = lambda: [page(click("n1", "Go")), page()]
    s.choices = [["n1"], ["DONE"]]
    s.raise_once = RuntimeError("boom")
    r = run(d, target="android", goal="go")
    sid = r["result"]["session_id"]
    assert r["result"]["status"] == "error"
    r = run(d, target="android", goal="go")
    assert r["ok"] and r["result"]["session_id"] == sid and r["result"]["status"] == "done"


def test_settled_holder_with_other_app_gets_a_fresh_target(daemon_with_fakes, keys):
    d = daemon_with_fakes
    d.script.choices = [["DONE"], ["DONE"]]
    sid = run(d, target="android", goal="a", url_or_app="com.one")["result"]["session_id"]
    r = run(d, target="android", goal="a", url_or_app="com.two")
    assert r["ok"] and r["result"]["session_id"] != sid and d.targets_created == 2
    assert d.script.targets[0].closed
    assert sid not in d.sessions


def test_settled_holder_without_runner_is_reused(daemon_with_fakes, keys):
    d = daemon_with_fakes
    sid = run(d, target="android", goal="otp")["result"]["session_id"]
    d.sessions[sid].runner = None
    r = run(d, target="android", goal="otp")
    assert r["ok"] and r["result"]["session_id"] == sid


def test_in_flight_holder_is_busy(daemon_with_fakes, keys):
    d = daemon_with_fakes
    d.script.choices = [["DONE"]]
    sid = run(d, target="android", goal="a")["result"]["session_id"]
    with d.sessions[sid].lock:  # simulate a call in flight
        r = run(d, target="android", goal="b")
    assert r == {"ok": False, "error": f"device_busy: {sid} (status=done; pass session_id or end_session)"}


def test_resume_on_settled_runner_returns_last_result_without_acting(daemon_with_fakes, keys):
    d = daemon_with_fakes
    s = d.script
    s.pages = lambda: [page(click("n9", "Transferir"), text="Destino ****4066 Monto ₡150.000,00"),
                       page(text="Transferencia exitosa Referencia: 99887766")]
    s.choices = [["n9", "DONE"]]
    r = run(d, target="android", goal="pay", params={"amount": 150000, "recipient": RECIPIENT})
    assert r["result"]["status"] == "done"
    acted = list(s.targets[0].acted)
    for args in ({}, {"confirm": True}, {"input": "123"}):
        again = d.handle({"op": "resume", "args": {"session_id": r["result"]["session_id"], **args}})
        assert again["ok"] and again["result"] == r["result"]
    assert s.targets[0].acted == acted


def test_missing_keys_message_mentions_restart(daemon_with_fakes, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    r = run(daemon_with_fakes, target="android", goal="g")
    assert "restart jevd after setting keys" in r["error"]


def test_crash_logs_type_only(daemon_with_fakes, keys, caplog, monkeypatch):
    caplog.set_level("DEBUG", logger="jevd")

    def boom():
        raise RuntimeError("secret-xyz")

    monkeypatch.setattr(daemon_with_fakes.store, "list", boom)
    r = daemon_with_fakes.handle({"op": "list_flows", "args": {}})
    assert r == {"ok": False, "error": "internal_error: RuntimeError"}
    assert "secret-xyz" not in caplog.text and "RuntimeError" in caplog.text
    assert "Traceback" not in caplog.text


def test_close_logs_busy_sessions(daemon_with_fakes, keys, caplog):
    caplog.set_level("DEBUG", logger="jevd")
    d = daemon_with_fakes
    sid = run(d, target="android", goal="otp")["result"]["session_id"]
    lock = d.sessions[sid].lock
    lock.acquire()
    try:
        t0 = time.monotonic()
        d.close()
        assert time.monotonic() - t0 < 3
    finally:
        lock.release()
    assert "busy" in caplog.text and sid in caplog.text
    assert d.script.targets[0].closed


def test_request_too_large_and_timeout(short_dir, monkeypatch):
    monkeypatch.setattr(daemon, "REQUEST_TIMEOUT_S", 0.3)
    monkeypatch.setattr(daemon, "MAX_REQUEST_BYTES", 1000)
    started = []
    server, th = _start_server(started)
    try:
        path = str(short_dir / "jevd.sock")
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        s.sendall(b"x" * 2000 + b"\n")
        assert json.loads(s.makefile().readline()) == {"ok": False, "error": "request_too_large"}
        s.close()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)
        s.settimeout(3)
        resp = s.makefile().readline()  # sends nothing: the daemon gives up after the timeout
        s.close()
        assert resp == "" or json.loads(resp)["error"] == "request_timeout"
        assert client.call("status", {})["sessions"] == []  # server still healthy
    finally:
        server.shutdown()
        th.join(5)


def _fake_server(path, reply):
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve_one():
        conn, _ = srv.accept()
        conn.recv(65536)
        if reply is None:
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
        else:
            conn.sendall(reply)
        conn.close()
        srv.close()

    th = threading.Thread(target=serve_one, daemon=True)
    th.start()
    return th


@pytest.mark.parametrize("reply", [b"garbage\n", b"", None])
def test_client_bad_replies_raise_runtime_error(short_dir, reply):
    th = _fake_server(short_dir / "jevd.sock", reply)
    with pytest.raises(RuntimeError):
        client.call("status", {})
    th.join(2)


def test_client_spawn_logs_stderr_and_timeout_mentions_it(short_dir, monkeypatch):
    seen = {}

    def fake_popen(cmd, **kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(client.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(client, "START_TIMEOUT_S", 0.2)
    with pytest.raises(RuntimeError, match="jevd.stderr"):
        client.call("status", {})
    assert Path(seen["stderr"].name) == short_dir / "jevd.stderr"


def test_unknown_device_rejected(script, keys):
    d = Daemon(Config(android_serial="abc123"), target_factory=script.target_factory,
               runner_factory=script.runner_factory)
    try:
        assert run(d, target="android", goal="otp", device="other") == {
            "ok": False, "error": "unknown_device: other"}
        assert d.targets_created == 0
        sid = run(d, target="android", goal="otp", device="abc123")["result"]["session_id"]
        # the configured serial and "default" are the same phone: one lock
        r = run(d, target="android", goal="otp", device="default")
        assert r["error"].startswith(f"device_busy: {sid}")
        r = run(d, target="android", goal="otp")
        assert r["error"].startswith(f"device_busy: {sid}")
    finally:
        d.close()


def test_close_warns_when_money_may_be_in_flight(daemon_with_fakes, keys, caplog, monkeypatch):
    monkeypatch.setattr(daemon, "CLOSE_WAIT_S", 0.05)
    caplog.set_level("DEBUG", logger="jevd")
    d = daemon_with_fakes
    sid = run(d, target="android", goal="otp")["result"]["session_id"]
    d.sessions[sid].runner.money_attempted = True
    lock = d.sessions[sid].lock
    lock.acquire()
    try:
        d.close()
    finally:
        lock.release()
    warn = [r for r in caplog.records if r.levelname == "WARNING" and "money action may be in flight" in r.getMessage()]
    assert warn and sid in warn[0].getMessage()


def test_close_no_money_warning_without_money_attempt(daemon_with_fakes, keys, caplog, monkeypatch):
    monkeypatch.setattr(daemon, "CLOSE_WAIT_S", 0.05)
    caplog.set_level("DEBUG", logger="jevd")
    d = daemon_with_fakes
    sid = run(d, target="android", goal="otp")["result"]["session_id"]
    lock = d.sessions[sid].lock
    lock.acquire()
    try:
        d.close()
    finally:
        lock.release()
    assert "money action may be in flight" not in caplog.text


def test_status_probes_configured_phone(cfg, script, monkeypatch):
    from dataclasses import replace
    from jev_runner.targets.base import DeviceDisconnected
    d = Daemon(replace(cfg, android_serial="abc"), target_factory=script.target_factory,
               runner_factory=script.runner_factory)
    try:
        def ok():
            d.link.serial = "abc"
            return "abc"
        monkeypatch.setattr(d.link, "ensure_connected", ok)
        assert d.handle({"op": "status", "args": {}})["result"]["android"] == {"serial": "abc", "connected": True}

        def down():
            d.link.serial = None
            raise DeviceDisconnected("no device")
        monkeypatch.setattr(d.link, "ensure_connected", down)
        st = d.handle({"op": "status", "args": {}})["result"]["android"]
        assert st == {"serial": None, "connected": False, "error": "no device"}
    finally:
        d.close()
