"""Keeps an Android device reachable: USB when present, else wireless adb through a
localhost TCP relay.

Why the relay: macOS Local Network privacy blocks the `adb` binary from LAN hosts
("No route to host"), so adb talks to 127.0.0.1 and a relay forwards to the device. The
privacy check is per binary and also blocks the uv-managed CPython running jevd, so the
relay runs as a helper process (`relay_main.py`) under `Config.relay_python`
(default /usr/bin/python3), which macOS allows.
"""
import queue
import subprocess
import threading
import time
from pathlib import Path

from jev_runner.config import Config
from jev_runner.targets.base import DeviceDisconnected

RELAY_SCRIPT = str(Path(__file__).with_name("relay_main.py"))
_SERVICES = ["_adb-tls-connect._tcp.local.", "_adb._tcp.local."]
_TCPIP_SETTLE_S = 5.0  # adbd restarts after `adb tcpip`; wait this long for USB to return
_RELAY_BUSY_EXIT = 73  # relay_main.BIND_FAILED; not 2, which CPython uses itself


class RelayBusy(OSError):
    pass


class RelayProcess:
    """Handle on a running relay_main.py helper: 127.0.0.1:local_port -> host:port."""

    def __init__(self, proc, local_port: int, host: str, port: int):
        self.proc = proc
        self.local_port = local_port
        self.host = host
        self.port = port

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self) -> None:
        proc = self.proc
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass


def _read_line(stream, timeout: float) -> str | None:
    """First line of `stream`, or None if nothing arrived within `timeout` seconds."""
    q: queue.Queue = queue.Queue()

    def reader():
        try:
            q.put(stream.readline())
        except (OSError, ValueError):
            q.put("")

    threading.Thread(target=reader, daemon=True).start()
    try:
        return q.get(timeout=timeout)
    except queue.Empty:
        return None


def resolve_ip(mdns_name: str, timeout: float = 3.0) -> str | None:
    """IPv4 of the first adb mDNS instance whose name contains `mdns_name`."""
    from zeroconf import ServiceBrowser, Zeroconf

    found: list[str] = []
    event = threading.Event()

    class Listener:
        def add_service(self, zc, type_, name):
            if mdns_name not in name:
                return
            info = zc.get_service_info(type_, name, timeout=int(timeout * 1000))
            if info:
                addrs = info.parsed_addresses()
                v4 = [a for a in addrs if ":" not in a]
                if v4:
                    found.append(v4[0])
                    event.set()

        def update_service(self, zc, type_, name):
            self.add_service(zc, type_, name)

        def remove_service(self, zc, type_, name):
            pass

    zc = Zeroconf()
    try:
        browsers = [ServiceBrowser(zc, s, Listener()) for s in _SERVICES]
        event.wait(timeout)
        for b in browsers:
            b.cancel()
    finally:
        zc.close()
    return found[0] if found else None


class AdbLink:
    def __init__(self, cfg: Config, run=subprocess.run, resolve=resolve_ip,
                 sleep=time.sleep, clock=time.monotonic, popen=subprocess.Popen):
        self.cfg = cfg
        self._run = run
        self._popen = popen
        self._resolve = resolve
        self._sleep = sleep
        self._clock = clock
        self.relay: RelayProcess | None = None
        self.app_labels: dict[str, str] = {}  # package -> launcher label, per daemon
        self._tcpip_done = False
        self._relay_busy = False
        self.serial: str | None = None  # last serial reached; None after a failed connect
        self._lock = threading.Lock()  # sessions on different threads share this link

    def _timeout(self, deadline: float, cap: float = 15) -> float:
        return min(cap, max(1.0, deadline - self._clock()))

    def _expired(self, deadline: float) -> bool:
        return self._clock() >= deadline

    def _adb(self, deadline: float, *args: str):
        return self._run(["adb", *args], capture_output=True, text=True,
                         timeout=self._timeout(deadline))

    def _device_states(self, deadline: float) -> dict[str, str]:
        try:
            out = self._adb(deadline, "devices").stdout or ""
        except (OSError, subprocess.SubprocessError):
            return {}
        states = {}
        seen_header = False
        for line in out.splitlines():
            if not seen_header:
                seen_header = line.strip().startswith("List of devices attached")
                continue
            parts = line.split()
            if len(parts) >= 2:
                states[parts[0]] = parts[1]
        return states

    def _ip(self, deadline: float) -> str | None:
        ip = None
        if self.cfg.android_mdns:
            try:
                ip = self._resolve(self.cfg.android_mdns, timeout=self._timeout(deadline, 3.0))
            except Exception:
                ip = None
        return ip or self.cfg.android_ip

    @property
    def _wireless(self) -> str:
        return f"127.0.0.1:{self.cfg.relay_port}"

    def _stop_relay(self) -> None:
        relay, self.relay = self.relay, None
        if relay:
            relay.stop()

    def _start_relay(self, ip: str, deadline: float) -> None:
        """Raises RelayBusy if the port cannot be bound, OSError on any other failure."""
        argv = [self.cfg.relay_python, "-I", RELAY_SCRIPT,
                str(self.cfg.relay_port), ip, str(self.cfg.adb_tcp_port)]
        proc = self._popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        relay = RelayProcess(proc, self.cfg.relay_port, ip, self.cfg.adb_tcp_port)
        line = _read_line(proc.stdout, self._timeout(deadline, 5.0))
        if line is not None and line.strip() == "ready":
            self.relay = relay
            return
        if line == "":  # stdout closed: the helper exited (or is about to)
            try:
                code = proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                code = None
            if code == _RELAY_BUSY_EXIT:
                raise RelayBusy(f"relay port {self.cfg.relay_port} busy")
        relay.stop()
        raise OSError("relay helper did not become ready")

    def _ensure_relay(self, ip: str, deadline: float) -> None:
        """(Re)start the helper if missing, dead, or pointed at another host."""
        if self.relay is not None and (not self.relay.alive() or self.relay.host != ip):
            self._stop_relay()
        if self.relay is None:
            self._start_relay(ip, deadline)

    def _wait_usb(self, usb: str, deadline: float) -> bool:
        """After `adb tcpip`, adbd restarts: wait (bounded) for USB to be back as `device`."""
        limit = min(deadline, self._clock() + _TCPIP_SETTLE_S)
        while True:
            self._sleep(0.5)
            if self._device_states(deadline).get(usb) == "device":
                return True
            if self._clock() >= limit:
                return False

    def _attempt(self, deadline: float) -> str | None:
        usb = self.cfg.android_serial
        if usb and self._device_states(deadline).get(usb) == "device":
            if not self._tcpip_done:
                self._tcpip_done = True
                try:
                    self._adb(deadline, "-s", usb, "tcpip", str(self.cfg.adb_tcp_port))
                except (OSError, subprocess.SubprocessError):
                    pass
                if not self._wait_usb(usb, deadline):
                    return None
            return usb
        if self._expired(deadline):
            return None
        ip = self._ip(deadline)
        if not ip or self._expired(deadline):
            return None
        try:
            self._ensure_relay(ip, deadline)
        except RelayBusy:
            self._relay_busy = True
            return None
        except OSError:
            self._relay_busy = False
            return None
        self._relay_busy = False
        if self._expired(deadline):
            return None
        wireless = self._wireless
        try:
            # a stale "offline" entry from a dead relay hangs later adb commands
            self._adb(deadline, "disconnect", wireless)
            self._adb(deadline, "connect", wireless)
        except (OSError, subprocess.SubprocessError):
            return None
        if self._expired(deadline):
            return None
        return wireless if self._device_states(deadline).get(wireless) == "device" else None

    def ensure_connected(self) -> str:
        with self._lock:
            return self._ensure_connected()

    def _ensure_connected(self) -> str:
        deadline = self._clock() + self.cfg.reconnect_window_s
        while True:
            serial = self._attempt(deadline)
            if serial:
                self.serial = serial
                return serial
            if self._expired(deadline):
                self.serial = None
                msg = f"Android device not reachable within {self.cfg.reconnect_window_s}s"
                if self._relay_busy:
                    msg += f" (relay port {self.cfg.relay_port} is busy)"
                raise DeviceDisconnected(msg)
            self._sleep(1)

    def close(self) -> None:
        with self._lock:
            self._close()

    def _close(self) -> None:
        had_relay = self.relay is not None
        self._stop_relay()
        if had_relay:
            try:
                self._run(["adb", "disconnect", self._wireless], capture_output=True,
                          text=True, timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
