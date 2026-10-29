import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server
from server import mcp


def _tools():
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_tools_registered():
    assert set(_tools()) == {
        "run_task", "resume", "screenshot", "end_session", "status", "list_flows", "delete_flow",
    }


def test_run_task_schema():
    tool = _tools()["run_task"]
    assert tool.input_schema["required"] == ["target", "goal"]
    assert "learn" in tool.input_schema["properties"]


def test_server_name():
    assert mcp.name == "jev-runner"


def test_run_task_forwards_all_args(monkeypatch):
    seen = {}

    def fake(op, args):
        seen["op"], seen["args"] = op, args
        return {"status": "done"}

    monkeypatch.setattr(server.client, "call", fake)
    asyncio.run(mcp.call_tool("run_task", {"target": "chrome", "goal": "g", "learn": False}))
    assert seen["op"] == "run_task"
    assert seen["args"]["learn"] is False and seen["args"]["max_steps"] == 40
    assert seen["args"]["target"] == "chrome" and seen["args"]["goal"] == "g"


@pytest.mark.parametrize("tool,args,op", [
    ("resume", {"session_id": "s", "confirm": True}, "resume"),
    ("screenshot", {"session_id": "s"}, "screenshot"),
    ("end_session", {"session_id": "s"}, "end_session"),
    ("status", {}, "status"),
    ("list_flows", {}, "list_flows"),
    ("delete_flow", {"name": "n"}, "delete_flow"),
])
def test_thin_tools_forward(monkeypatch, tool, args, op):
    calls = []
    monkeypatch.setattr(server.client, "call", lambda o, a: calls.append((o, a)) or {"ok": True})
    asyncio.run(mcp.call_tool(tool, args))
    assert calls[0][0] == op
    for k, v in args.items():
        assert calls[0][1][k] == v


def test_daemon_error_becomes_tool_error(monkeypatch):
    monkeypatch.setattr(server.client, "call",
                        lambda op, args: (_ for _ in ()).throw(RuntimeError("device_busy: ab12")))
    with pytest.raises(ToolError, match="device_busy"):
        asyncio.run(mcp.call_tool("run_task", {"target": "android", "goal": "g"}))
