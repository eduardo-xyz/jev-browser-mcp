"""Moving files between the Mac, web pages (Chrome) and the Android phone.

Options (pick per task):
- Mac -> web page: run_task(files=[...]) on chrome exposes the page's file inputs as "Upload file"
  actions; choosing one sets the files directly (no OS file picker).
- web page -> Mac: files a chrome run downloads are reported in the result's `downloads`.
- Mac -> phone: push_file copies into the phone's Download folder (and indexes it for pickers).
- phone -> Mac: pull_file copies a phone file into the data dir's inbox/.
- Mac/phone -> an app's share sheet: share_file opens Android's Share for the file (optionally
  straight into one app); a run_task then picks the chat and taps Send under the usual guards.

Every local path must be a regular file under an allowed root (config `file_roots`, default: the
data dir's outbox/, ~/Downloads, ~/Desktop, ~/Documents); hidden paths and key/secret-looking files
are refused so a run can't upload credentials.
"""
from __future__ import annotations

import mimetypes
import re
import shlex
import subprocess
import time
from pathlib import Path

PHONE_ROOTS = ("/sdcard/", "/storage/emulated/0/")
PHONE_DEFAULT_DIR = "/sdcard/Download"
_SECRET_NAME = re.compile(
    r"(^id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key|p12|pfx|keychain|kdbx|ovpn)$|^\.env|credential|secret"
    r"|token|password|passwd)", re.I)


class FileError(Exception):
    pass


def outbox(data_dir: Path) -> Path:
    p = data_dir / "outbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def inbox(data_dir: Path) -> Path:
    p = data_dir / "inbox"
    p.mkdir(parents=True, exist_ok=True)
    return p


def roots(cfg) -> list[Path]:
    configured = [Path(r).expanduser() for r in (getattr(cfg, "file_roots", None) or [])]
    if configured:
        return [r.resolve() for r in configured]
    home = Path.home()
    return [outbox(cfg.data_dir).resolve(), *(home / d for d in ("Downloads", "Desktop", "Documents"))]


def check_local(path: str, cfg) -> Path:
    """A readable regular file under an allowed root, not hidden, not secret-looking."""
    if not isinstance(path, str) or not path.strip():
        raise FileError("bad_path: empty")
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = outbox(cfg.data_dir) / p  # a bare name means the outbox
    p = p.resolve()
    allowed = [r.resolve() for r in roots(cfg)]
    under = next((r for r in allowed if p == r or r in p.parents), None)
    if under is None:
        raise FileError(f"path_not_allowed: {p} (allowed roots: {', '.join(map(str, allowed))})")
    if any(part.startswith(".") for part in p.relative_to(under).parts):
        raise FileError(f"path_not_allowed: hidden path {p}")
    if _SECRET_NAME.search(p.name):
        raise FileError(f"path_not_allowed: secret-looking file name {p.name}")
    if not p.is_file():
        raise FileError(f"not_found: {p}")
    return p


def check_phone(path: str) -> str:
    if not isinstance(path, str) or not path.startswith(PHONE_ROOTS) or ".." in path.split("/"):
        raise FileError(f"bad_phone_path: {path} (must be under /sdcard/)")
    return path


def mime_of(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


class Adb:
    """Minimal adb file operations on one serial."""

    def __init__(self, serial: str, run=subprocess.run, timeout: float = 120):
        self.serial, self._run, self.timeout = serial, run, timeout

    def _adb(self, *args: str) -> str:
        try:
            res = self._run(["adb", "-s", self.serial, *args], capture_output=True, text=True,
                            timeout=self.timeout)
        except (OSError, subprocess.SubprocessError) as e:
            raise FileError(f"adb_failed: {type(e).__name__}") from None
        if res.returncode != 0:
            raise FileError(f"adb_failed: {(res.stderr or res.stdout or '').strip()[:200]}")
        return res.stdout or ""

    def shell(self, cmd: str) -> str:
        return self._adb("shell", cmd)

    def push(self, local: Path, dest_dir: str = PHONE_DEFAULT_DIR) -> str:
        dest = f"{check_phone(dest_dir.rstrip('/') + '/')}{local.name}"
        self._adb("push", str(local), dest)
        self.scan(dest)
        return dest

    def scan(self, phone_path: str) -> None:
        """Index the file so gallery/document pickers and content URIs see it. Best-effort."""
        try:
            self.shell("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d "
                       + shlex.quote("file://" + phone_path))
        except FileError:
            pass

    def pull(self, phone_path: str, dest: Path) -> Path:
        check_phone(phone_path)
        target = dest / Path(phone_path).name
        if target.exists():  # never overwrite an earlier pull
            target = dest / f"{target.stem}-{int(time.time())}{target.suffix}"
        self._adb("pull", phone_path, str(target))
        return target

    def content_uri(self, phone_path: str) -> str | None:
        """content://media/... for an indexed file (what share targets accept), else None."""
        for p in {phone_path, phone_path.replace("/sdcard/", "/storage/emulated/0/", 1)}:
            where = shlex.quote(f"_data='{p}'")
            try:
                out = self.shell(f"content query --uri content://media/external/file --projection _id "
                                 f"--where {where}")
            except FileError:
                continue
            m = re.search(r"_id=(\d+)", out)
            if m:
                return f"content://media/external/file/{m.group(1)}"
        return None

    def share(self, phone_path: str, package: str | None = None, mime: str | None = None) -> dict:
        check_phone(phone_path)
        uri = None
        for _ in range(5):  # the media scanner indexes asynchronously
            uri = self.content_uri(phone_path)
            if uri:
                break
            time.sleep(0.6)
        if uri is None:
            raise FileError(f"not_indexed: {phone_path} (the phone has not indexed it yet; retry)")
        mime = mime or mime_of(phone_path)
        cmd = (f"am start -a android.intent.action.SEND -t {shlex.quote(mime)} "
               f"--eu android.intent.extra.STREAM {shlex.quote(uri)} --grant-read-uri-permission")
        if package:  # straight into that app; without it Android shows its app picker
            cmd += f" -p {shlex.quote(package)}"
        out = self.shell(cmd)
        if "Error" in out:
            raise FileError(f"share_failed: {out.strip()[:200]}")
        return {"uri": uri, "mime": mime, "package": package}


def new_downloads(folder: Path, since: float, limit: int = 20) -> list[str]:
    """Finished files that appeared in `folder` since `since` (epoch seconds), newest first."""
    try:
        items = [p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")
                 and p.suffix not in (".crdownload", ".download", ".part", ".tmp")
                 and p.stat().st_mtime >= since]
    except OSError:
        return []
    items.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [str(p) for p in items[:limit]]
