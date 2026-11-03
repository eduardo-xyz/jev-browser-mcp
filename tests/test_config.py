import os
from pathlib import Path

import pytest

from jev_runner import config

LIST_KEYS = ["money_apps", "money_labels", "money_labels_contextual",
             "money_exclude_words", "secret_patterns", "own_accounts", "destructive_labels", "file_roots"]


def _write(text):
    path = Path(os.environ["JEV_DATA_DIR"])
    path.mkdir(parents=True, exist_ok=True)
    (path / "config.toml").write_text(text)


@pytest.mark.parametrize("key", LIST_KEYS)
@pytest.mark.parametrize("bad", ['"Transferir"', "5", "[1, 2]", '["ok", 3]', "true"])
def test_list_fields_must_be_lists_of_str(key, bad):
    _write(f"{key} = {bad}\n")
    with pytest.raises(ValueError, match=key):
        config.load()


def test_valid_lists_load():
    _write('money_apps = ["com.bank"]\nsecret_patterns = []\nown_accounts = ["123"]\n')
    cfg = config.load()
    assert cfg.money_apps == ["com.bank"] and cfg.secret_patterns == [] and cfg.own_accounts == ["123"]


def test_socket_path_short_data_dir_is_natural():
    from pathlib import Path
    from jev_runner.config import socket_path
    short = Path("/tmp/jd")  # a short data dir keeps the socket beside it
    assert socket_path(short) == short / "jevd.sock"


def test_socket_path_long_data_dir_moves_to_tmp():
    from pathlib import Path
    from jev_runner.config import socket_path
    longd = Path("/private/var/folders/xx/" + "a" * 90 + "/jev-data")
    sp = socket_path(longd)
    assert str(sp).startswith("/tmp/jevd-") and sp.suffix == ".sock" and len(str(sp)) <= 100
    # stable and unique per data dir
    assert socket_path(longd) == sp
    assert socket_path(Path("/private/var/folders/yy/" + "b" * 90 + "/jev-data")) != sp


# --- the repo .env and the Chrome binding ---------------------------------------------------

def test_dotenv_values_and_load(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('# keys\nTYPESAFE_API_KEY="k1"\nexport TEXT_MODEL_API_KEY=k2\n\nbad line\nEMPTY=\n')
    assert config.dotenv_values(env_file) == {"TYPESAFE_API_KEY": "k1", "TEXT_MODEL_API_KEY": "k2", "EMPTY": ""}
    environ = {"TEXT_MODEL_API_KEY": "already"}
    added = config.load_dotenv(environ, env_file)
    assert added == ["TYPESAFE_API_KEY", "EMPTY"]
    assert environ["TEXT_MODEL_API_KEY"] == "already" and environ["TYPESAFE_API_KEY"] == "k1"
    assert config.dotenv_values(tmp_path / "missing.env") == {}


def test_chrome_binding_from_config_wins_over_environment(caplog):
    cfg = config.Config(chrome_cdp_url="http://127.0.0.1:9333", chrome_label="da")
    environ = {"BU_CDP_URL": "http://127.0.0.1:9222", "OTHER": "x"}
    with caplog.at_level("WARNING", logger="jevd"):
        binding = config.apply_chrome_binding(cfg, environ)
    assert binding == {"cdp_url": "http://127.0.0.1:9333", "label": "da"}
    assert environ["BU_CDP_URL"] == "http://127.0.0.1:9333" and environ["BU_NAME"] == "da"
    assert "config.toml says chrome_cdp_url" in caplog.text


def test_chrome_binding_unset_leaves_environment_alone():
    environ = {"BU_CDP_URL": "http://127.0.0.1:9222"}
    assert config.apply_chrome_binding(config.Config(), environ) == {"cdp_url": "http://127.0.0.1:9222",
                                                                      "label": None}
    assert config.chrome_binding({}) == {"cdp_url": None, "label": None}


def test_chrome_binding_loads_from_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_DATA_DIR", str(tmp_path))
    (tmp_path / "config.toml").write_text('chrome_cdp_url = "http://127.0.0.1:9333"\nchrome_label = "da"\n')
    cfg = config.load()
    assert (cfg.chrome_cdp_url, cfg.chrome_label) == ("http://127.0.0.1:9333", "da")


def test_binding_repoints_an_already_imported_harness(monkeypatch, caplog):
    import sys
    from types import SimpleNamespace
    ipc = SimpleNamespace(sock_addr=lambda name: f"/tmp/bu-{name}.sock")
    helpers = SimpleNamespace(NAME="default", SOCK="/tmp/bu-default.sock", ipc=ipc)
    admin = SimpleNamespace(NAME="default")
    monkeypatch.setitem(sys.modules, "browser_harness.helpers", helpers)
    monkeypatch.setitem(sys.modules, "browser_harness.admin", admin)
    monkeypatch.delenv("BU_NAME", raising=False)
    with caplog.at_level("WARNING", logger="jevd"):
        config.apply_chrome_binding(config.Config(chrome_label="da"))
    assert helpers.NAME == "da" and helpers.SOCK == "/tmp/bu-da.sock" and admin.NAME == "da"
    assert config.harness_label() == "da" and "re-pointing" in caplog.text


def test_daemon_module_binds_the_harness_before_importing_it(tmp_path):
    """Regression: importing jev_runner.daemon used to import browser_harness (via the runner)
    with BU_NAME unset, freezing the harness name to 'default' = a harness on whatever Chrome it
    finds, whatever config.toml said. Checked in a fresh interpreter, as the daemon starts."""
    import subprocess, sys
    (tmp_path / "config.toml").write_text('chrome_cdp_url = "http://127.0.0.1:9555"\nchrome_label = "zz"\n')
    env = {k: v for k, v in os.environ.items() if k not in ("BU_NAME", "BU_CDP_URL")}
    env["JEV_DATA_DIR"] = str(tmp_path)
    code = ("import jev_runner.daemon, browser_harness.helpers as h, browser_harness.admin as a, os; "
            "print(h.NAME, a.NAME, os.environ.get('BU_CDP_URL'))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                         cwd=str(config.REPO_ROOT), timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.split() == ["zz", "zz", "http://127.0.0.1:9555"]
