import json

from jev_runner import tab_groups


def _write_token(tmp_path, bu="da", value="tok-abc"):
    d = tmp_path / "cli"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"claude-{bu}-cli.token").write_text(value + "\n")
    return d


def test_disabled_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_BROKER_TOKEN_DIR", str(tmp_path / "cli"))  # empty
    monkeypatch.setenv("BU_NAME", "da")
    assert tab_groups.enabled() is False


def test_disabled_without_bu_name(tmp_path, monkeypatch):
    _write_token(tmp_path, "da")
    monkeypatch.setenv("JEV_BROKER_TOKEN_DIR", str(tmp_path / "cli"))
    monkeypatch.delenv("BU_NAME", raising=False)
    assert tab_groups.enabled() is False


def test_group_name_from_bu_name(monkeypatch):
    monkeypatch.setenv("BU_NAME", "pe")
    assert tab_groups.group_name() == "JEV pe"
    monkeypatch.setenv("BU_NAME", "da/weird!$")  # sanitised to allowed charset
    assert tab_groups.group_name() == "JEV daweird"


def test_group_name_override_shows_session_name(monkeypatch):
    monkeypatch.setenv("BU_NAME", "da")  # stays the token key
    monkeypatch.setenv("JEV_GROUP_NAME", "Turbo")  # label only
    assert tab_groups.group_name() == "Turbo"
    monkeypatch.setenv("JEV_GROUP_NAME", "Jev-r (pe)")
    assert tab_groups.group_name() == "Jev-r (pe)".replace("(", "").replace(")", "")  # sanitised


def test_claim_posts_expected_rpc(tmp_path, monkeypatch):
    _write_token(tmp_path, "da", "secret-tok")
    monkeypatch.setenv("JEV_BROKER_TOKEN_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("BU_NAME", "da")
    monkeypatch.setenv("JEV_BROKER_URL", "http://127.0.0.1:18765")
    monkeypatch.setattr(tab_groups.os, "getpid", lambda: 4242)
    captured = {}

    def fake_rpc(operation, arguments, token):
        captured.update(operation=operation, arguments=arguments, token=token)

    monkeypatch.setattr(tab_groups, "_rpc", fake_rpc)
    # run the claim synchronously by capturing the thread target
    started = {}
    monkeypatch.setattr(tab_groups.threading, "Thread",
                        lambda target, **kw: type("T", (), {"start": lambda self: started.setdefault("ran", target())})())
    tab_groups.claim("ABCDEF0123456789")
    assert captured["operation"] == "browser_tab_claim"
    assert captured["arguments"] == {"target_id": "ABCDEF0123456789", "relay_pid": 4242, "name": "JEV da"}
    assert captured["token"] == "secret-tok"


def test_claim_noop_without_token(tmp_path, monkeypatch):
    monkeypatch.setenv("JEV_BROKER_TOKEN_DIR", str(tmp_path / "cli"))  # empty
    monkeypatch.setenv("BU_NAME", "da")
    called = []
    monkeypatch.setattr(tab_groups.threading, "Thread",
                        lambda target, **kw: called.append(target) or type("T", (), {"start": lambda self: None})())
    tab_groups.claim("ABCDEF0123456789")
    assert called == []  # never even spawns a thread when disabled


def test_claim_ignores_bad_target(tmp_path, monkeypatch):
    _write_token(tmp_path, "da")
    monkeypatch.setenv("JEV_BROKER_TOKEN_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("BU_NAME", "da")
    spawned = []
    monkeypatch.setattr(tab_groups.threading, "Thread",
                        lambda target, **kw: spawned.append(1) or type("T", (), {"start": lambda self: None})())
    tab_groups.claim(None)
    tab_groups.claim("")
    tab_groups.claim(1234)
    assert spawned == []


def test_activity_posts_and_swallows_errors(tmp_path, monkeypatch):
    _write_token(tmp_path, "pe", "ptok")
    monkeypatch.setenv("JEV_BROKER_TOKEN_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("BU_NAME", "pe")
    monkeypatch.setattr(tab_groups.os, "getpid", lambda: 7)
    seen = {}
    monkeypatch.setattr(tab_groups, "_rpc",
                        lambda op, args, token: seen.update(op=op, args=args))
    tab_groups.activity(["T1", "T2", None, 5])
    assert seen["op"] == "browser_activity"
    assert seen["args"] == {"target_ids": ["T1", "T2"], "relay_pid": 7}

    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(tab_groups, "_rpc", boom)
    tab_groups.activity(["T1"])  # must not raise


def test_activity_noop_on_empty(tmp_path, monkeypatch):
    _write_token(tmp_path, "pe")
    monkeypatch.setenv("JEV_BROKER_TOKEN_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("BU_NAME", "pe")
    called = []
    monkeypatch.setattr(tab_groups, "_rpc", lambda *a, **k: called.append(1))
    tab_groups.activity([])
    tab_groups.activity(None)
    assert called == []
