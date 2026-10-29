import socket
import subprocess
import threading
from contextlib import contextmanager

import pytest

from jev_mcp.adb_link import AdbLink, TcpRelay
from jev_mcp.config import Config
from jev_mcp.targets.base import DeviceDisconnected


@pytest.fixture
def cfg_android(tmp_path):
    return Config(android_serial="zdirl7t84pqwh6ww", android_mdns="adb-zdirl7t84pqwh6ww",
                  data_dir=tmp_path)


class FakeAdb:
    def __init__(self):
        self.calls = []
        self.devices = []
        self.on_connect = None

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = ""
        if argv[:2] == ["adb", "devices"]:
            out = "List of devices attached\n" + "".join(d + "\n" for d in self.devices)
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


def test_relay_pipes_bytes_both_ways():
    with echo_server() as port:
        relay = TcpRelay(free_port(), "127.0.0.1", port)
        relay.start()
        with socket.create_connection(("127.0.0.1", relay.local_port)) as s:
            s.sendall(b"hello")
            assert s.recv(5) == b"hello"
        relay.stop()


def test_relay_stop_idempotent_and_closes_listener():
    with echo_server() as port:
        relay = TcpRelay(free_port(), "127.0.0.1", port)
        relay.start()
        relay.stop()
        relay.stop()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", relay.local_port), timeout=1)


def test_relay_retarget_affects_new_connections():
    with echo_server() as port:
        relay = TcpRelay(free_port(), "203.0.113.1", port)  # unroutable
        relay.start()
        relay.retarget("127.0.0.1")
        assert relay.host == "127.0.0.1"
        with socket.create_connection(("127.0.0.1", relay.local_port)) as s:
            s.sendall(b"hi")
            assert s.recv(2) == b"hi"
        relay.stop()


def test_relay_upstream_close_closes_client():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    port = srv.getsockname()[1]
    relay = TcpRelay(free_port(), "127.0.0.1", port)
    relay.start()
    with socket.create_connection(("127.0.0.1", relay.local_port)) as s:
        s.settimeout(3)
        up, _ = srv.accept()
        up.close()
        assert s.recv(1) == b""
    relay.stop()
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
        link.relay.stop()


def test_ip_fallback_when_mdns_fails(fake_adb, cfg_android):
    cfg_android.android_ip = "10.0.0.9"
    cfg_android.relay_port = rp = free_port()
    fake_adb.on_connect = lambda: fake_adb.devices.append(f"127.0.0.1:{rp}\tdevice")
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: None)
    try:
        link.ensure_connected()
        assert link.relay.host == "10.0.0.9"
    finally:
        link.relay.stop()


def test_gives_up_after_window_and_raises(fake_adb, fake_clock, cfg_android):
    fake_adb.devices = []
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: None,
                   sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    assert fake_clock.now() >= 30
