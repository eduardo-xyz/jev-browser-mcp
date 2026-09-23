import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from server import mcp


def test_browse_web_is_registered_with_expected_schema():
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name: tool for tool in tools}

    assert "browse_web" in names
    schema = names["browse_web"].input_schema
    assert schema["required"] == ["url", "goal"]
    assert set(schema["properties"]) == {"url", "goal", "max_steps"}


def test_browse_web_fails_cleanly_without_touching_the_browser_when_keys_are_missing(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)

    with pytest.raises(ToolError, match="TYPESAFE_API_KEY"):
        asyncio.run(
            mcp.call_tool("browse_web", {"url": "https://example.com", "goal": "test"})
        )


def test_browse_web_reports_all_missing_keys_at_once(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "present")

    with pytest.raises(ToolError) as exc_info:
        asyncio.run(
            mcp.call_tool("browse_web", {"url": "https://example.com", "goal": "test"})
        )

    assert "TYPESAFE_API_KEY" in str(exc_info.value)
    assert "TEXT_MODEL_API_KEY" not in str(exc_info.value)
