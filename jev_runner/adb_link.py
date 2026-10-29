"""Keeps an Android device reachable: USB when present, else wireless adb through a
localhost TCP relay.

Why the relay: macOS Local Network privacy blocks the `adb` binary from LAN hosts
("No route to host") while Python processes are allowed, so adb talks to 127.0.0.1
and this process forwards to the device.
"""
import socket
import subprocess
import threading
import time

from jev_runner.config import Config
from jev_runner.targets.base import DeviceDisconnected

_SERVICES = ["_adb-tls-connect._tcp.local.", "_adb._tcp.local."]


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        # closing one side closes the other
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass


class TcpRelay:
    def __init__(self, local_port: int, host: str, port: int):
        self.local_port = local_port
        self.host = host
        self.port = port
        self._listener: socket.socket | None = None
        self._lock = threading.Lock()
        self._stopped = False

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", self.local_port))
        srv.listen(16)
        self._listener = srv
        self._stopped = False
        threading.Thread(target=self._accept_loop, args=(srv,), daemon=True).start()

    def retarget(self, host: str) -> None:
        with self._lock:
            self.host = host

    def stop(self) -> None:
        self._stopped = True
        srv, self._listener = self._listener, None
        if srv is not None:
            try:
                srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                srv.close()
            except OSError:
                pass

    def _accept_loop(self, srv: socket.socket) -> None:
        while not self._stopped:
            try:
                client, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client: socket.socket) -> None:
        with self._lock:
            host = self.host
        try:
            upstream = socket.create_connection((host, self.port), timeout=5)
            upstream.settimeout(None)
        except OSError:
            client.close()
            return
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


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
                 sleep=time.sleep, clock=time.monotonic):
        self.cfg = cfg
        self._run = run
        self._resolve = resolve
        self._sleep = sleep
        self._clock = clock
        self.relay: TcpRelay | None = None
        self._tcpip_done = False
        self._relay_busy = False
        self.serial: str | None = None  # last serial reached; None after a failed connect

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

    def _ensure_relay(self, ip: str) -> None:
        """Raises OSError if the relay port cannot be bound."""
        if self.relay is None:
            relay = TcpRelay(self.cfg.relay_port, ip, self.cfg.adb_tcp_port)
            relay.start()
            self.relay = relay
        elif self.relay.host != ip:
            self.relay.retarget(ip)

    def _attempt(self, deadline: float) -> str | None:
        usb = self.cfg.android_serial
        if usb and self._device_states(deadline).get(usb) == "device":
            if not self._tcpip_done:
                self._tcpip_done = True
                try:
                    self._adb(deadline, "-s", usb, "tcpip", str(self.cfg.adb_tcp_port))
                except (OSError, subprocess.SubprocessError):
                    pass
            return usb
        if self._expired(deadline):
            return None
        ip = self._ip(deadline)
        if not ip or self._expired(deadline):
            return None
        try:
            self._ensure_relay(ip)
        except OSError:
            self._relay_busy = True
            return None
        self._relay_busy = False
        if self._expired(deadline):
            return None
        wireless = f"127.0.0.1:{self.cfg.relay_port}"
        try:
            self._adb(deadline, "connect", wireless)
        except (OSError, subprocess.SubprocessError):
            return None
        if self._expired(deadline):
            return None
        return wireless if self._device_states(deadline).get(wireless) == "device" else None

    def ensure_connected(self) -> str:
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
        relay, self.relay = self.relay, None
        if relay:
            relay.stop()
