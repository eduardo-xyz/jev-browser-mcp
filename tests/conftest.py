"""Shared fixtures: isolate the data dir and provide default config."""
import pytest

from jev_runner import config


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_DATA_DIR", str(tmp_path / "jev-data"))


@pytest.fixture
def cfg():
    return config.Config()


@pytest.fixture(autouse=True)
def _verify_ok(monkeypatch):
    # no network in tests: the independent DONE/BLOCKED check is unavailable unless a test overrides it
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    monkeypatch.setattr("jev_runner.verify.check", lambda goal, page, history: (None, ""))
