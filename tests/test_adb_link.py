import ast
import io
import signal
import socket
import sys
import time
import subprocess
import threading
from contextlib import contextmanager

import pytest

from jev_runner.adb_link import RELAY_SCRIPT, AdbLink
from jev_runner.config import Config
from jev_runner.targets.base import DeviceDisconnected


@pytest.fixture
def cfg_android(tmp_path):
    # the relay helper runs under the test interpreter (real subprocess, localhost only)
    return Config(android_serial="zdirl7t84pqwh6ww", android_mdns="adb-zdirl7t84pqwh6ww",
                  relay_python=sys.executable, data_dir=tmp_path)


class FakeAdb:
    def __init__(self):
        self.calls = []
        self.devices = []
        self.on_connect = None
        self.on_call = None
        self.timeouts = []
        self.preamble = ""

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        self.timeouts.append(kw.get("timeout"))
        if self.on_call:
            self.on_call(argv)
        out = ""
        if argv[:2] == ["adb", "devices"]:
            out = (self.preamble + "List of devices attached\n"
                   + "".join(d + "\n" for d in self.devices))
        elif argv[:2] == ["adb", "connect"] and self.on_connect:
            self.on_connect()
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture
def fake_adb():
    return FakeAdb()


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture
def fake_clock():
    return FakeClock()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def echo_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            def echo(c=c):
                with c:
                    while d := c.recv(1024):
                        c.sendall(d)
            threading.Thread(target=echo, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield srv.getsockname()[1]
    finally:
        srv.close()


def test_prefers_usb(fake_adb, cfg_android):
    fake_adb.devices = ["zdirl7t84pqwh6ww\tdevice"]
    link = AdbLink(cfg_android, run=fake_adb)
    assert link.ensure_connected() == "zdirl7t84pqwh6ww"
    assert ["adb", "-s", "zdirl7t84pqwh6ww", "tcpip", "5555"] in fake_adb.calls
    link.ensure_connected()
    assert fake_adb.calls.count(["adb", "-s", "zdirl7t84pqwh6ww", "tcpip", "5555"]) == 1


def test_wireless_via_relay(fake_adb, cfg_android):
    # free port: the real 5556 may be bound by a live daemon on this machine
    cfg_android.relay_port = rp = free_port()
    fake_adb.devices = []
    fake_adb.on_connect = lambda: fake_adb.devices.append(f"127.0.0.1:{rp}\tdevice")
    link = AdbLink(cfg_android, run=fake_adb,
                   resolve=lambda name, timeout=3.0: "192.168.1.215")
    try:
        assert link.ensure_connected() == f"127.0.0.1:{rp}"
        assert link.relay.host == "192.168.1.215"
        assert ["adb", "connect", f"127.0.0.1:{rp}"] in fake_adb.calls
    finally:
        link.close()


def test_ip_fallback_when_mdns_fails(fake_adb, cfg_android):
    cfg_android.android_ip = "10.0.0.9"
    cfg_android.relay_port = rp = free_port()
    fake_adb.on_connect = lambda: fake_adb.devices.append(f"127.0.0.1:{rp}\tdevice")
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: None)
    try:
        link.ensure_connected()
        assert link.relay.host == "10.0.0.9"
    finally:
        link.close()


def test_gives_up_after_window_and_raises(fake_adb, fake_clock, cfg_android):
    fake_adb.devices = []
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: None,
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    assert fake_clock.now() >= 30


def _busy_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, s.getsockname()[1]


def test_busy_relay_port_raises_disconnected_then_recovers(fake_adb, fake_clock, cfg_android):
    blocker, rp = _busy_port()
    cfg_android.relay_port = rp
    fake_adb.on_connect = lambda: fake_adb.devices.append(f"127.0.0.1:{rp}\tdevice")
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: "192.168.1.215",
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected, match="busy"):
        link.ensure_connected()
    assert link.relay is None
    blocker.close()
    try:
        assert link.ensure_connected() == f"127.0.0.1:{rp}"
        assert link.relay is not None
    finally:
        link.close()


def test_subprocess_timeouts_bounded_by_remaining(fake_adb, fake_clock, cfg_android):
    cfg_android.reconnect_window_s = 2
    seen = []
    link = AdbLink(cfg_android, run=fake_adb,
                   resolve=lambda name, timeout=3.0: seen.append(timeout),
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    assert fake_adb.timeouts[0] <= 2
    assert all(t is not None and 1 <= t <= 15 for t in fake_adb.timeouts)
    assert seen and seen[0] <= 2


def test_deadline_checked_between_substeps(fake_adb, fake_clock, cfg_android):
    cfg_android.reconnect_window_s = 5
    fake_adb.on_call = lambda argv: setattr(fake_clock, "t", fake_clock.t + 20)  # slow adb
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: "192.168.1.215",
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    assert not any(c[:2] == ["adb", "connect"] for c in fake_adb.calls)
    assert link.relay is None


def test_close_resets_relay_and_restart_works(fake_adb, cfg_android):
    cfg_android.relay_port = rp = free_port()
    fake_adb.on_connect = lambda: fake_adb.devices.append(f"127.0.0.1:{rp}\tdevice")
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: "192.168.1.215")
    link.ensure_connected()
    link.close()
    assert link.relay is None
    link.ensure_connected()
    try:
        assert link.relay is not None
        socket.create_connection(("127.0.0.1", rp), timeout=1).close()
    finally:
        link.close()


@pytest.mark.parametrize("state", ["unauthorized", "offline"])
def test_non_device_states_not_connected(fake_adb, fake_clock, cfg_android, state):
    fake_adb.devices = [f"zdirl7t84pqwh6ww\t{state}"]
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: None,
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    assert not any("tcpip" in c for c in fake_adb.calls)


def test_devices_parsing_skips_daemon_preamble(fake_adb, cfg_android):
    fake_adb.preamble = "* daemon not running; starting now at tcp:5037\n* daemon started successfully\n"
    fake_adb.devices = ["zdirl7t84pqwh6ww\tdevice"]
    assert AdbLink(cfg_android, run=fake_adb).ensure_connected() == "zdirl7t84pqwh6ww"


def test_serial_tracks_last_connect(fake_adb, fake_clock, cfg_android):
    fake_adb.devices = ["zdirl7t84pqwh6ww\tdevice"]
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: None,
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    assert link.serial is None
    link.ensure_connected()
    assert link.serial == "zdirl7t84pqwh6ww"
    fake_adb.devices = []
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    assert link.serial is None


# --- relay helper process (relay_main.py) ---------------------------------------------


def test_default_relay_python_is_system_python():
    assert Config().relay_python == "/usr/bin/python3"


def test_relay_script_is_stdlib_only_and_py39_compatible():
    src = open(RELAY_SCRIPT).read()
    tree = ast.parse(src, feature_version=(3, 9))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "no relative imports"
            mods.add(node.module.split(".")[0])
    assert mods <= set(sys.stdlib_module_names), mods - set(sys.stdlib_module_names)
    assert "jev_runner" not in mods


def _spawn_relay(local_port, host, port):
    proc = subprocess.Popen([sys.executable, "-I", RELAY_SCRIPT, str(local_port), host, str(port)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
    return proc.wait(timeout=5)


def test_relay_main_pipes_bytes_both_ways():
    with echo_server() as port:
        lp = free_port()
        proc = _spawn_relay(lp, "127.0.0.1", port)
        try:
            with socket.create_connection(("127.0.0.1", lp), timeout=3) as s:
                s.sendall(b"hello")
                assert s.recv(5) == b"hello"
        finally:
            _stop(proc)


def test_relay_main_upstream_close_closes_client():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    lp = free_port()
    proc = _spawn_relay(lp, "127.0.0.1", srv.getsockname()[1])
    try:
        with socket.create_connection(("127.0.0.1", lp), timeout=3) as s:
            up, _ = srv.accept()
            up.close()
            assert s.recv(1) == b""
    finally:
        _stop(proc)
        srv.close()


def test_relay_main_unreachable_upstream_closes_client():
    lp = free_port()
    proc = _spawn_relay(lp, "127.0.0.1", free_port())  # nothing listening upstream
    try:
        with socket.create_connection(("127.0.0.1", lp), timeout=3) as s:
            s.settimeout(5)
            assert s.recv(1) == b""
    finally:
        _stop(proc)


def test_relay_main_sigterm_exits_cleanly_and_frees_port():
    lp = free_port()
    proc = _spawn_relay(lp, "127.0.0.1", 1)
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=5) == 0
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", lp), timeout=1)


def test_relay_main_exits_when_parent_stdin_closes():
    proc = _spawn_relay(free_port(), "127.0.0.1", 1)
    proc.stdin.close()
    assert proc.wait(timeout=5) == 0


def test_relay_main_busy_port_exits_nonzero_without_ready():
    blocker, rp = _busy_port()
    try:
        proc = subprocess.Popen([sys.executable, "-I", RELAY_SCRIPT, str(rp), "127.0.0.1", "1"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        assert proc.stdout.read() == ""
        assert proc.wait(timeout=5) == 2
    finally:
        blocker.close()


def test_relay_main_binds_loopback_only():
    src = open(RELAY_SCRIPT).read()
    assert '"127.0.0.1"' in src and '"0.0.0.0"' not in src and "(\"\", " not in src


class FakeProc:
    def __init__(self, argv, ready=True, code=None):
        self.argv = argv
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("ready\n" if ready else "")
        self.returncode = code
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class FakePopen:
    def __init__(self):
        self.procs = []
        self.ready = True
        self.code = None

    def __call__(self, argv, **kw):
        p = FakeProc(list(argv), ready=self.ready, code=self.code)
        self.procs.append(p)
        return p


def _wireless_link(fake_adb, cfg_android, popen, ips, **kw):
    rp = cfg_android.relay_port
    def on_connect():
        if f"127.0.0.1:{rp}\tdevice" not in fake_adb.devices:
            fake_adb.devices.append(f"127.0.0.1:{rp}\tdevice")
    fake_adb.on_connect = on_connect
    it = iter(ips)
    return AdbLink(cfg_android, run=fake_adb, popen=popen,
                   resolve=lambda *a, **k: next(it), **kw)


def test_relay_launched_as_helper_process(fake_adb, cfg_android):
    cfg_android.relay_python = "/usr/bin/python3"
    popen = FakePopen()
    link = _wireless_link(fake_adb, cfg_android, popen, ["192.168.1.215"])
    assert link.ensure_connected() == "127.0.0.1:5556"
    (proc,) = popen.procs
    assert proc.argv[0] == "/usr/bin/python3"
    assert RELAY_SCRIPT in proc.argv
    assert proc.argv[-3:] == ["5556", "192.168.1.215", "5555"]
    assert link.relay.host == "192.168.1.215"


def test_relay_restarted_when_helper_died(fake_adb, cfg_android):
    popen = FakePopen()
    link = _wireless_link(fake_adb, cfg_android, popen, ["192.168.1.215"] * 2)
    link.ensure_connected()
    popen.procs[0].returncode = 1  # helper crashed
    link.ensure_connected()
    assert len(popen.procs) == 2 and link.relay.proc is popen.procs[1]


def test_relay_retarget_restarts_with_new_host(fake_adb, cfg_android):
    popen = FakePopen()
    link = _wireless_link(fake_adb, cfg_android, popen, ["192.168.1.215", "192.168.1.99"])
    link.ensure_connected()
    link.ensure_connected()
    assert popen.procs[0].terminated
    assert popen.procs[1].argv[-2] == "192.168.1.99"
    assert link.relay.host == "192.168.1.99"


def test_relay_same_host_reuses_helper(fake_adb, cfg_android):
    popen = FakePopen()
    link = _wireless_link(fake_adb, cfg_android, popen, ["192.168.1.215"] * 2)
    link.ensure_connected()
    link.ensure_connected()
    assert len(popen.procs) == 1


def test_close_terminates_helper(fake_adb, cfg_android):
    popen = FakePopen()
    link = _wireless_link(fake_adb, cfg_android, popen, ["192.168.1.215"])
    link.ensure_connected()
    link.close()
    assert popen.procs[0].terminated and link.relay is None


def test_helper_bind_failure_reports_busy(fake_adb, fake_clock, cfg_android):
    popen = FakePopen()
    popen.ready, popen.code = False, 2
    link = _wireless_link(fake_adb, cfg_android, popen, iter(lambda: "192.168.1.215", None),
                          sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected, match="busy"):
        link.ensure_connected()
    assert link.relay is None


def test_helper_never_ready_is_killed_and_retried(fake_adb, fake_clock, cfg_android):
    popen = FakePopen()
    popen.ready = False  # stdout closes without "ready", process still "running"
    link = _wireless_link(fake_adb, cfg_android, popen, iter(lambda: "192.168.1.215", None),
                          sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    assert popen.procs and all(p.terminated for p in popen.procs)
    assert link.relay is None
    assert not any(c[:2] == ["adb", "connect"] for c in fake_adb.calls)


# --- stale adb entry (e) --------------------------------------------------------------


def test_disconnects_stale_entry_before_connect(fake_adb, cfg_android):
    link = _wireless_link(fake_adb, cfg_android, FakePopen(), ["192.168.1.215"])
    link.ensure_connected()
    disc = fake_adb.calls.index(["adb", "disconnect", "127.0.0.1:5556"])
    conn = fake_adb.calls.index(["adb", "connect", "127.0.0.1:5556"])
    assert disc < conn


def test_close_disconnects_relay_entry(fake_adb, cfg_android):
    link = _wireless_link(fake_adb, cfg_android, FakePopen(), ["192.168.1.215"])
    link.ensure_connected()
    fake_adb.calls.clear()
    link.close()
    assert ["adb", "disconnect", "127.0.0.1:5556"] in fake_adb.calls


# --- tcpip adbd restart race (d) ------------------------------------------------------


def test_waits_for_usb_device_after_tcpip(fake_adb, fake_clock, cfg_android):
    usb = "zdirl7t84pqwh6ww"
    fake_adb.devices = [f"{usb}\tdevice"]
    polls = {"n": 0}

    def on_call(argv):
        if "tcpip" in argv:
            fake_adb.devices = []  # adbd restarting
        elif argv[:2] == ["adb", "devices"] and not fake_adb.devices:
            polls["n"] += 1
            if polls["n"] >= 3:
                fake_adb.devices = [f"{usb}\tdevice"]

    fake_adb.on_call = on_call
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: None,
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    assert link.ensure_connected() == usb
    i = fake_adb.calls.index(["adb", "-s", usb, "tcpip", "5555"])
    assert fake_adb.calls[i + 1:].count(["adb", "devices"]) >= 3
    assert fake_clock.now() <= 5


def test_tcpip_wait_is_bounded(fake_adb, fake_clock, cfg_android):
    usb = "zdirl7t84pqwh6ww"
    fake_adb.devices = [f"{usb}\tdevice"]
    fake_adb.on_call = lambda argv: ("tcpip" in argv) and setattr(fake_adb, "devices", [])
    resolved_at = []
    link = AdbLink(cfg_android, run=fake_adb,
                   resolve=lambda *a, **k: resolved_at.append(fake_clock.now()),
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    # the post-tcpip poll gave up after <=5 s; the next attempt went on to wireless
    assert resolved_at and resolved_at[0] <= 6.5
