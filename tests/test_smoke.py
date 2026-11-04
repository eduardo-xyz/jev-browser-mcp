import datetime as dt
import subprocess

import pytest

from jev_runner import smoke


def _goal(name="wf", expect=None):
    g = {"name": name, "url": f"https://a.test/{name}", "goal": "Read only. Read 5 names.", "max_steps": 6}
    if expect:
        g["expect"] = expect
    return g


class FakeCall:
    """A daemon that answers run_task/read/end_session from a script, recording every op."""

    def __init__(self, run=None, text="", read_raises=False):
        self.run = run or {"session_id": "s1", "status": "done", "model_calls": 2}
        self.text, self.read_raises = text, read_raises
        self.ops = []

    def __call__(self, op, args):
        self.ops.append((op, args))
        if op == "run_task":
            return self.run
        if op == "read":
            if self.read_raises:
                raise RuntimeError("daemon gone")
            return {"text": self.text}
        return {"status": "ok"}


def test_a_done_run_with_the_expected_text_passes_and_the_session_is_ended():
    call = FakeCall(text="Round Robin\nPublished")
    r = smoke.run_goal(_goal(expect="published"), call)
    assert r["passed"] and r["found"] is True and r["status"] == "done"
    assert [op for op, _ in call.ops] == ["run_task", "read", "end_session"]
    assert call.ops[0][1]["target"] == "chrome" and call.ops[0][1]["max_steps"] == 6


def test_blocked_runs_and_missing_text_fail_with_the_reason_kept():
    blocked = FakeCall(run={"session_id": "s2", "status": "blocked", "reason": "jev_blocked",
                            "detail": {"why": "blank_page"}, "model_calls": 1})
    r = smoke.run_goal(_goal(), blocked)
    assert not r["passed"] and r["reason"] == "jev_blocked" and r["why"] == "blank_page"
    assert ("end_session", {"session_id": "s2"}) in blocked.ops
    missing = FakeCall(text="nothing here")
    r = smoke.run_goal(_goal(expect="Published"), missing)
    assert not r["passed"] and r["status"] == "done" and r["found"] is False


def test_a_daemon_failure_is_a_failed_result_not_a_crash():
    r = smoke.run_goal(_goal(), FakeCall(read_raises=True))
    assert not r["passed"] and r["status"] == "error" and "daemon gone" in r["reason"]


def test_summary_and_report_name_the_failures():
    when = dt.datetime(2026, 11, 5, 3, 0)
    results = [
        {"name": "wf", "url": "u1", "passed": True, "status": "done", "reason": None, "why": None,
         "model_calls": 2, "elapsed_ms": 1200, "expect": "Published", "found": True},
        {"name": "editor", "url": "u2", "passed": False, "status": "blocked", "reason": "jev_blocked",
         "why": "blank_page", "model_calls": 1, "elapsed_ms": 900, "expect": None, "found": None},
        {"name": "emails", "url": "u3", "passed": False, "status": "done", "reason": None, "why": None,
         "model_calls": 3, "elapsed_ms": 4000, "expect": "car-deals", "found": False},
    ]
    line = smoke.summary(results, when)
    assert line.startswith("JEV smoke 2026-11-05 03:00: 1/3 passed. Failed: ")
    assert "editor: blocked/jev_blocked (blank_page)" in line
    assert "emails: done, 'car-deals' not on the final screen" in line
    text = smoke.report(results, when)
    assert "| wf | PASS | done |" in text and "| editor | FAIL | blocked | jev_blocked (blank_page) |" in text
    assert "- emails: u3" in text


def test_post_uses_the_configured_command_and_room(monkeypatch):
    seen = {}
    def run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")
    cfg = {"report": {"post": ["/x/broker", "--member", "Jev-Smoke"], "room": "room-1"}}
    assert smoke.post(cfg, "JEV smoke: 3/3 passed", run=run) is True
    assert seen["cmd"][:4] == ["/x/broker", "--member", "Jev-Smoke", "send"]
    import json
    body = json.loads(seen["cmd"][4])
    assert body["conversation_id"] == "room-1" and body["no_reply"] is True and body["body"].startswith("JEV smoke")
    assert smoke.post({}, "x", run=run) is False  # nothing configured: silently not posted


def test_main_runs_every_goal_writes_the_report_and_exits_with_the_failure_count(tmp_path, monkeypatch):
    (tmp_path / "smoke.toml").write_text(
        '[report]\ndir = "out"\n\n'
        '[[goal]]\nname = "ok"\nurl = "https://a.test/ok"\ngoal = "read"\nexpect = "Published"\n\n'
        '[[goal]]\nname = "bad"\nurl = "https://a.test/bad"\ngoal = "read"\nexpect = "Missing"\n')
    monkeypatch.setenv("JEV_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(smoke.client, "call", FakeCall(text="Published"))
    code = smoke.main(["--no-post"])
    assert code == 1
    files = list((tmp_path / "out").glob("*.md"))
    assert len(files) == 1 and "| bad | FAIL |" in files[0].read_text()


def test_config_rejects_a_goal_without_a_url(tmp_path):
    (tmp_path / "smoke.toml").write_text('[[goal]]\nname = "x"\ngoal = "read"\n')
    with pytest.raises(ValueError):
        smoke.load_config(tmp_path / "smoke.toml")
