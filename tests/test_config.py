import os
from pathlib import Path

import pytest

from jev_runner import config

LIST_KEYS = ["money_apps", "money_labels", "money_labels_contextual",
             "money_exclude_words", "secret_patterns", "own_accounts"]


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
