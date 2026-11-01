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


@pytest.fixture(autouse=True)
def _no_settle_delay(monkeypatch):
    monkeypatch.setattr("jev_runner.runner.Runner.settle_delay", 0)


@pytest.fixture(autouse=True)
def _no_wait_settle(monkeypatch):
    # the post-wait settle spends real wall-clock letting late content appear; off by default in
    # tests (a test that exercises it re-sets WAIT_SETTLE/WAIT_POLL to small values itself)
    monkeypatch.setattr("jev_runner.runner.WAIT_SETTLE", 0)
