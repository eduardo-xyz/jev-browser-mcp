import subprocess
from pathlib import Path

import pytest

from jev_runner import files
from jev_runner.config import Config


@pytest.fixture
def fcfg(tmp_path):
    root = tmp_path / "share"
    root.mkdir()
    return Config(data_dir=tmp_path / "data", file_roots=[str(root)]), root


def test_check_local_accepts_file_under_root(fcfg):
    cfg, root = fcfg
    (root / "invoice.pdf").write_bytes(b"%PDF")
    assert files.check_local(str(root / "invoice.pdf"), cfg) == (root / "invoice.pdf").resolve()


@pytest.mark.parametrize("name", ["id_rsa", "server.pem", ".env", "my-token.txt", "credentials.json"])
def test_check_local_refuses_secret_looking(fcfg, name):
    cfg, root = fcfg
    (root / name).write_text("x")
    with pytest.raises(files.FileError, match="path_not_allowed"):
        files.check_local(str(root / name), cfg)


def test_check_local_refuses_outside_roots_hidden_dirs_and_traversal(fcfg, tmp_path):
    cfg, root = fcfg
    (tmp_path / "outside.pdf").write_text("x")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "a.pdf").write_text("x")
    for p in (tmp_path / "outside.pdf", root / ".hidden" / "a.pdf", root / ".." / "outside.pdf"):
        with pytest.raises(files.FileError, match="path_not_allowed"):
            files.check_local(str(p), cfg)
    with pytest.raises(files.FileError, match="not_found"):
        files.check_local(str(root / "missing.pdf"), cfg)


def test_bare_name_means_outbox(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    (files.outbox(cfg.data_dir) / "a.pdf").write_text("x")
    assert files.check_local("a.pdf", cfg).name == "a.pdf"


def test_check_phone_paths():
    assert files.check_phone("/sdcard/Download/a.pdf")
    for bad in ("/data/data/x/db", "/sdcard/../data/x", "relative.pdf"):
        with pytest.raises(files.FileError):
            files.check_phone(bad)


class FakeRun:
    def __init__(self, outputs=None):
        self.calls, self.outputs = [], list(outputs or [])

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out = self.outputs.pop(0) if self.outputs else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def test_push_copies_then_indexes(tmp_path):
    run = FakeRun()
    f = tmp_path / "a.pdf"; f.write_text("x")
    dest = files.Adb("SER", run=run).push(f)
    assert dest == "/sdcard/Download/a.pdf"
    assert run.calls[0] == ["adb", "-s", "SER", "push", str(f), "/sdcard/Download/a.pdf"]
    assert "MEDIA_SCANNER_SCAN_FILE" in run.calls[1][-1]


def test_pull_never_overwrites(tmp_path):
    (tmp_path / "a.pdf").write_text("old")
    run = FakeRun()
    out = files.Adb("SER", run=run).pull("/sdcard/Download/a.pdf", tmp_path)
    assert out != tmp_path / "a.pdf" and out.suffix == ".pdf"
    assert run.calls[0][:4] == ["adb", "-s", "SER", "pull"]


def test_share_uses_content_uri_and_package():
    run = FakeRun(["Row: 0 _id=42\n", "Starting: Intent { act=android.intent.action.SEND }"])
    out = files.Adb("SER", run=run).share("/sdcard/Download/a.pdf", package="com.whatsapp")
    assert out["uri"] == "content://media/external/file/42" and out["mime"] == "application/pdf"
    cmd = run.calls[1][-1]
    assert "android.intent.action.SEND" in cmd and "content://media/external/file/42" in cmd
    assert "-p com.whatsapp" in cmd and "--grant-read-uri-permission" in cmd


def test_adb_failure_is_file_error():
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="error: no devices")
    with pytest.raises(files.FileError, match="adb_failed"):
        files.Adb("SER", run=run).shell("ls")


def test_new_downloads_skips_partial_and_old(tmp_path):
    import os, time
    (tmp_path / "old.pdf").write_text("x"); os.utime(tmp_path / "old.pdf", (1, 1))
    (tmp_path / "new.pdf").write_text("x")
    (tmp_path / "busy.pdf.crdownload").write_text("x")
    assert files.new_downloads(tmp_path, time.time() - 60) == [str(tmp_path / "new.pdf")]
