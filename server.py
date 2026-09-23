"""MCP server exposing TypeSafe's Jev browser agent as a single browse_web tool."""

import os

from jev_ultrafast import Agent
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from summary import BrowseResult, summarize

REQUIRED_ENV_VARS = ("TYPESAFE_API_KEY", "TEXT_MODEL_API_KEY")

mcp = MCPServer("jev-browser-mcp")


@mcp.tool()
def browse_web(url: str, goal: str, max_steps: int = 40) -> BrowseResult:
    """Drive a real Chrome browser toward a natural-language goal using TypeSafe's Jev agent."""
    missing = [name for name in REQUIRED_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise ToolError(f"Missing required environment variable(s): {', '.join(missing)}")

    try:
        with Agent(url, goal) as agent:
            state = None
            hit_step_cap = False
            for state in agent.run():
                if len(state["history"]) >= max_steps:
                    hit_step_cap = True
                    break
            return summarize(state, hit_step_cap)
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    mcp.run()
