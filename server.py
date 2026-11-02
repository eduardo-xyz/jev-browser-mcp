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
    files: list[str] | None = None,
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
    - `blocked` with reason `signed_out` (chrome): the browser is signed out of the site; a
      person must sign in in that Chrome (2FA included), then retry. Not retried automatically.
    - `error` with reason `money_action_uncertain`: tell the user to check the app
      manually, then call `end_session` (the session is terminal).
    Results may include `flow_used` / `flow_saved` / `flow_updated` (learned flows replay
    cheaply; pass `learn=False` to disable auto-save/self-heal, `flow`/`save_as` to name
    one). `device_busy` errors name the session holding the device. `device` may be omitted,
    "default", or the configured android_serial (one phone; all android sessions share its
    lock); anything else fails with `unknown_device`.

    Files: on chrome, pass `files` (absolute paths, or bare names in the data dir's outbox/) to
    upload them: the page's file inputs then appear as "Upload file ..." actions and the OS file
    picker never opens. Allowed folders: outbox/, ~/Downloads, ~/Desktop, ~/Documents (config
    `file_roots`); hidden or key/secret-looking files are refused. Files a chrome run downloads are
    listed in the result's `downloads`. For the phone use push_file / pull_file / share_file.
    """
    return _call("run_task", {
        "target": target, "goal": goal, "url_or_app": url_or_app, "params": params,
        "money_params": money_params, "flow": flow, "save_as": save_as,
        "max_steps": max_steps, "session_id": session_id, "device": device, "learn": learn,
        "files": files,
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
def push_file(path: str, dest_dir: str = "/sdcard/Download") -> dict:
    """Copy a Mac file to the Android phone (default its Download folder) and index it so the
    phone's pickers (gallery, documents, attach) list it. Returns `phone_path`. Same allowed
    folders as run_task `files`."""
    return _call("push_file", {"path": path, "dest_dir": dest_dir})


@mcp.tool()
def pull_file(phone_path: str) -> dict:
    """Copy a file from the phone (a path under /sdcard/, e.g. /sdcard/Download/x.pdf) to the Mac,
    into the data dir's inbox/. Returns the `local` path."""
    return _call("pull_file", {"phone_path": phone_path})


@mcp.tool()
def share_file(path: str, package: str | None = None, mime: str | None = None) -> dict:
    """Open Android's Share for a file: a Mac path (pushed first) or a phone path under /sdcard/.
    With `package` (e.g. com.whatsapp) it goes straight into that app's share screen; without it
    Android shows its app picker. Then call run_task on android to pick the chat/recipient and tap
    Send (the send guard applies). More reliable than an app's own in-app file picker. Refused with
    device_busy while a run is driving the phone."""
    return _call("share_file", {"path": path, "package": package, "mime": mime})


@mcp.tool()
def end_session(session_id: str) -> dict:
    """Close a session and release its device/browser. Required after money_action_uncertain."""
    return _call("end_session", {"session_id": session_id})


@mcp.tool()
def add_lesson(body: str, target: str | None = None, app: str | None = None,
               goal: str = "", screen_tags: str = "") -> dict:
    """Save a durable lesson to the experience memory (replaces hand-editing lessons.md). `body` is
    the knowledge ("avoid the Quiero sheet", "hide keyboard before Continuar"); `target`/`app`/`goal`
    scope it so future runs on the same app/goal retrieve it. Account/PII digit runs are stripped
    before storing. Use this for durable playbook knowledge, not one-off notes."""
    return _call("add_lesson", {"body": body, "target": target, "app": app,
                                "goal": goal, "screen_tags": screen_tags})


@mcp.tool()
def status() -> dict:
    """Show daemon status: `sessions` (each with session_id, target, status and idle_s),
    `android` (`serial` last reached and whether it is `connected`), `flows` (count of saved
    flows) and `memory` (counts of learned `lessons` and recorded `runs`, with `done`/`verified`)."""
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
