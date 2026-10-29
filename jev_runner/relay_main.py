"""Standalone localhost TCP relay: 127.0.0.1:<local_port> -> <host>:<port>.

Usage: python3 relay_main.py LOCAL_PORT HOST PORT

Runs as a separate helper process under an interpreter that macOS Local Network privacy
allows to reach the LAN (e.g. /usr/bin/python3); the uv-managed CPython running jevd gets
"No route to host". Stdlib only, Python >= 3.9, no project imports.

Prints "ready" on stdout once listening. Exits 0 on SIGTERM/SIGINT or when stdin reaches
EOF (the parent died or closed it), 2 if the port cannot be bound.
"""
import os
import signal
import socket
import sys
import threading


def _close(s):
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        s.close()
    except OSError:
        pass


def _pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        # closing one side closes the other
        _close(src)
        _close(dst)


def _handle(client, host, port):
    try:
        upstream = socket.create_connection((host, port), timeout=5)
        upstream.settimeout(None)
    except OSError:
        _close(client)
        return
    threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
    threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


def _accept_loop(srv, host, port):
    while True:
        try:
            client, _ = srv.accept()
        except OSError:
            return
        threading.Thread(target=_handle, args=(client, host, port), daemon=True).start()


def _watch_stdin(stop):
    try:
        while sys.stdin.buffer.read(1024):
            pass
    except (OSError, ValueError):
        pass
    stop.set()


def main(argv):
    if len(argv) != 3:
        sys.stderr.write("usage: relay_main.py LOCAL_PORT HOST PORT\n")
        return 64
    local_port, host, port = int(argv[0]), argv[1], int(argv[2])
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", local_port))
        srv.listen(16)
    except OSError as e:
        sys.stderr.write("relay: cannot bind 127.0.0.1:%d: %s\n" % (local_port, e))
        srv.close()
        return 2

    threading.Thread(target=_accept_loop, args=(srv, host, port), daemon=True).start()
    threading.Thread(target=_watch_stdin, args=(stop,), daemon=True).start()
    sys.stdout.write("ready\n")
    sys.stdout.flush()

    while not stop.wait(0.5):
        pass
    _close(srv)
    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    sys.stdout.flush()
    os._exit(code)  # don't wait on daemon threads blocked in recv/accept
