"""Shared fixtures: isolate the data dir and provide default config."""
import pytest

from jev_mcp import config


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_DATA_DIR", str(tmp_path / "jev-data"))


@pytest.fixture
def cfg():
    return config.Config()
