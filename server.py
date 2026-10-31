"""Thin MCP front end for the jevd daemon, which runs TypeSafe's Jev agent on Chrome and Android."""

from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from jev_runner import client

mcp = MCPServer("jev-runner")


def _call(op: str, args: dict) -> dict:
    try:
        return client.call(op, args)
    except RuntimeError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def run_task(
    target: Literal["chrome", "android"],
    goal: str,
    url_or_app: str | None = None,
    params: dict | None = None,
    money_params: list[str] | None = None,
    flow: str | None = None,
    save_as: str | None = None,
    max_steps: int = 40,
    session_id: str | None = None,
    device: str | None = None,
    learn: bool = True,
) -> dict:
    """Run a natural-language goal on Mac Chrome or an Android phone using TypeSafe's Jev agent.

    Make ONE call per goal. `url_or_app` may be a URL (chrome), an Android package, or an
    Android app name. For payments pass the exact `amount` and `recipient` in `params`
    (put account/phone numbers in `params`, not only in the goal text); the daemon refuses
    to tap a money action unless they match what is on screen.  `money_params` lists the
    `params` keys that are money-related (e.g. amount, recipient).

    Results have a `status`:
    - `paused_needs_input`: ask the user for the requested value, then call
      `resume(session_id, input=...)`.
    - `paused_mismatch`: show the user `expected` and `seen`; call
      `resume(session_id, confirm=True)` ONLY after the user explicitly approves.
    - `unverified`: Jev said done, but an independent check of the final screen disagreed
      (`reason` says why). Treat the goal as NOT done; check the screen (`screenshot`) before
      retrying. A `done` with reason `verified_after_blocked` means Jev gave up but the goal is
      visibly met.
    - `error` with reason `money_action_uncertain`: tell the user to check the app
      manually, then call `end_session` (the session is terminal).
    Results may include `flow_used` / `flow_saved` / `flow_updated` (learned flows replay
    cheaply; pass `learn=False` to disable auto-save/self-heal, `flow`/`save_as` to name
    one). `device_busy` errors name the session holding the device. `device` may be omitted,
    "default", or the configured android_serial (one phone; all android sessions share its
    lock); anything else fails with `unknown_device`.
    """
    return _call("run_task", {
        "target": target, "goal": goal, "url_or_app": url_or_app, "params": params,
        "money_params": money_params, "flow": flow, "save_as": save_as,
        "max_steps": max_steps, "session_id": session_id, "device": device, "learn": learn,
    })


@mcp.tool()
def resume(
    session_id: str,
    input: str | None = None,
    instruction: str | None = None,
    confirm: bool = False,
) -> dict:
    """Continue a paused session. Use `input` to answer paused_needs_input (secrets such as
    OTPs go here and are never logged), `instruction` to give extra guidance, and
    `confirm=True` to approve a paused_mismatch ONLY after the user has approved it."""
    return _call("resume", {
        "session_id": session_id, "input": input, "instruction": instruction, "confirm": confirm,
    })


@mcp.tool()
def screenshot(session_id: str) -> dict:
    """Save a screenshot of the session's current screen; returns a file path, not image data."""
    return _call("screenshot", {"session_id": session_id})


@mcp.tool()
def read(session_id: str) -> dict:
    """Read-only: return the visible text of the session's current page, INCLUDING cross-origin
    iframes the normal snapshot can't see (e.g. a white-labelled GHL automation screen). Use this
    to list or read content when a `run_task` only "waits" because the real UI is inside an iframe.
    Returns `text` (merged) and `frames` (per-iframe text). It never clicks or changes anything."""
    return _call("read", {"session_id": session_id})


@mcp.tool()
def end_session(session_id: str) -> dict:
    """Close a session and release its device/browser. Required after money_action_uncertain."""
    return _call("end_session", {"session_id": session_id})


@mcp.tool()
def status() -> dict:
    """Show daemon status: `sessions` (each with session_id, target, status and idle_s),
    `android` (`serial` last reached and whether it is `connected`) and `flows` (count of
    saved flows)."""
    return _call("status", {})


@mcp.tool()
def list_flows() -> dict:
    """List saved (learned or named) flows."""
    return _call("list_flows", {})


@mcp.tool()
def delete_flow(name: str) -> dict:
    """Delete a saved flow by name."""
    return _call("delete_flow", {"name": name})


if __name__ == "__main__":
    mcp.run()
