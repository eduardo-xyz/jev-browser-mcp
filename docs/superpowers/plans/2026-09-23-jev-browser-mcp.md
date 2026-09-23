# jev-browser-mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an MCP server that exposes TypeSafe's Jev browser agent (via `browser-use/jev-ultrafast`) as a single `browse_web(url, goal, max_steps)` tool callable from Claude Code/Claude Desktop.

**Architecture:** A FastMCP-style `MCPServer` instance in `server.py` registers one tool. The tool validates required env vars, constructs a `jev_ultrafast.Agent`, drains its step generator (capped by `max_steps`), and returns a trimmed summary built by a pure, independently-tested `summary.py` module. `jev_ultrafast` itself is an external dependency pulled from GitHub, never vendored.

**Tech Stack:** Python 3.12+, `uv` for dependency/env management, `mcp` SDK **v2.2.x** (package name `mcp`, server class `mcp.server.mcpserver.MCPServer` — this SDK renamed `FastMCP` to `MCPServer` in its 2.x line; do not use the old `mcp.server.fastmcp.FastMCP` import, it raises `ModuleNotFoundError` on this version), `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-23-jev-browser-mcp-design.md`

## Global Constraints

- Python `>=3.12` (matches upstream `jev-ultrafast`'s requirement).
- `mcp` dependency pinned `>=2.2,<3` — verified against the actually-installed 2.2.0 API (see Task 3 for the exact import paths and behavior; do not guess at an older FastMCP-era API).
- `jev-ultrafast` is a dependency (`jev-ultrafast @ git+https://github.com/browser-use/jev-ultrafast.git`), never vendored/copied into this repo.
- Exactly one MCP tool (`browse_web`) — no start/step tools, no interactive control.
- The tool must never return `jev_ultrafast`'s raw per-step payloads (they contain full request/response bodies sent to Jev/OpenRouter) — only the trimmed shape from `summary.py`.
- The tool must never leak a raw stack trace to the MCP client — every failure path raises `mcp.server.mcpserver.exceptions.ToolError` with a clear message.
- No `TYPESAFE_API_KEY` or OpenRouter key is available during this implementation. Nothing in this plan claims a live `browse_web` call was exercised — every test step here is either a pure-function unit test or a pre-flight-error test that never reaches `jev_ultrafast.Agent`.

---

### Task 1: Project scaffolding and dependency resolution

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.env.example`

**Interfaces:**
- Produces: an installable `uv` project with `jev_ultrafast` and `mcp` importable from `.venv`. Later tasks run `uv run pytest` / `uv run python -c ...` against this environment.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "jev-browser-mcp"
version = "0.1.0"
description = "MCP tool exposing TypeSafe's Jev browser agent (via browser-use/jev-ultrafast) to Claude Code/Desktop."
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "jev-ultrafast @ git+https://github.com/browser-use/jev-ultrafast.git",
    "mcp[cli]>=2.2,<3",
]

[dependency-groups]
dev = ["pytest>=8.4,<9"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.metadata]
allow-direct-references = true

[tool.uv]
package = false

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]
```

> **Note (added during execution):** `pythonpath = ["."]` is also required — with a flat
> `tests/` directory and no `conftest.py`, pytest's default import mode does not add the
> project root to `sys.path`, so `tests/test_summary.py`'s `from summary import summarize`
> fails with `ModuleNotFoundError` otherwise.
>
> **Note (added during execution):** `allow-direct-references = true` is required because
> hatchling refuses a git-URL dependency by default. `[tool.uv] package = false` is required
> because this project is a flat script (`server.py`, `summary.py`), not an installable
> package with a `jev_browser_mcp/` directory — without it, hatchling's wheel builder can't
> auto-detect what to ship and `uv sync` fails with "Unable to determine which files to ship
> inside the wheel." Also requires `README.md` to already exist (hatchling validates the
> `readme` field at build time even for an editable install) — Step 3 below was reordered
> ahead of Task 4 for this reason; a placeholder is fine until Task 4 fills it in.

- [ ] **Step 2: Write `.gitignore`**

```
.venv/
.env
__pycache__/
*.pyc
```

- [ ] **Step 3: Write `.env.example`**

```
TYPESAFE_API_KEY=
TYPESAFE_MODEL=jev-latest
# Required for TYPE_TEXT. OpenAI-compatible helper; credentials stay server-side.
TEXT_MODEL_API_KEY=
TEXT_MODEL_BASE_URL=https://openrouter.ai/api/v1
TEXT_MODEL=inception/mercury-2.5
TEXT_MODEL_REASONING=none
```

- [ ] **Step 4: Resolve dependencies**

Run: `uv sync`
Expected: completes without error; `.venv/` now contains both `jev_ultrafast` and `mcp` packages. Verify with:

Run: `uv run python -c "import jev_ultrafast, mcp; print('ok')"`
Expected: prints `ok`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore .env.example uv.lock
git commit -m "Scaffold jev-browser-mcp project"
```

---

### Task 2: Step summarization (pure function, TDD)

**Files:**
- Create: `summary.py`
- Test: `tests/test_summary.py`

**Interfaces:**
- Consumes: nothing from other tasks (pure function, no `jev_ultrafast` import needed — it only shapes plain dicts).
- Produces: `summarize(state: dict, hit_step_cap: bool) -> BrowseResult` and the `BrowseResult` / `StepSummary` `TypedDict`s, both imported by `server.py` in Task 3.

  `state` is shaped like a `jev_ultrafast.Agent.run()` yield (a snapshot dict): it has `status` (`"ready"|"predicted"|"done"|"blocked"`), `page` (dict with a `"url"` key), `elapsed_ms` (int), and `history` (list of step dicts, each with at least `action`, `kind`, `text`, `page_changed`, `elapsed_ms`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_summary.py`:

```python
from summary import summarize


def _fake_state(status="done", history=None):
    return {
        "status": status,
        "elapsed_ms": 7073,
        "page": {"url": "https://www.google.com/travel/flights?hl=en&results"},
        "history": history if history is not None else [],
    }


def test_summarize_trims_history_to_documented_fields():
    state = _fake_state(
        history=[
            {
                "action": "Where from? · San Francisco",
                "kind": "fill",
                "text": "Zurich",
                "page_changed": True,
                "elapsed_ms": 1200,
                "probability": 0.94,
                "confidence": 0.91,
                "raw_answers": {"operation": {"choice": "TYPE_TEXT"}},
                "usage": {"input_tokens": 512},
            }
        ]
    )

    result = summarize(state, hit_step_cap=False)

    assert result == {
        "status": "done",
        "final_url": "https://www.google.com/travel/flights?hl=en&results",
        "elapsed_ms": 7073,
        "steps": [
            {
                "action": "Where from? · San Francisco",
                "kind": "fill",
                "text": "Zurich",
                "page_changed": True,
                "elapsed_ms": 1200,
            }
        ],
    }


def test_summarize_reports_max_steps_when_cap_hit_before_done_or_blocked():
    state = _fake_state(status="ready", history=[])

    result = summarize(state, hit_step_cap=True)

    assert result["status"] == "max_steps"


def test_summarize_keeps_done_status_even_if_cap_flag_is_true():
    # The loop can hit done/blocked on the exact same tick the cap would have
    # triggered; a real status always wins over the cap label.
    state = _fake_state(status="done", history=[])

    result = summarize(state, hit_step_cap=True)

    assert result["status"] == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_summary.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'summary'`

- [ ] **Step 3: Write `summary.py`**

```python
"""Trims a jev_ultrafast.Agent run into the shape browse_web returns to Claude."""

from typing import Literal, TypedDict


class StepSummary(TypedDict):
    action: str
    kind: str
    text: str | None
    page_changed: bool | None
    elapsed_ms: int


class BrowseResult(TypedDict):
    status: Literal["done", "blocked", "max_steps", "error"]
    final_url: str
    elapsed_ms: int
    steps: list[StepSummary]


def summarize(state: dict, hit_step_cap: bool) -> BrowseResult:
    status = state["status"]
    if hit_step_cap and status not in ("done", "blocked"):
        status = "max_steps"
    return {
        "status": status,
        "final_url": state["page"]["url"],
        "elapsed_ms": state["elapsed_ms"],
        "steps": [
            {
                "action": step["action"],
                "kind": step["kind"],
                "text": step.get("text"),
                "page_changed": step.get("page_changed"),
                "elapsed_ms": step["elapsed_ms"],
            }
            for step in state["history"]
        ],
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_summary.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add summary.py tests/test_summary.py
git commit -m "Add pure step-summarization function for browse_web"
```

---

### Task 3: MCP server with the `browse_web` tool

**Files:**
- Create: `server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `summarize`, `BrowseResult` from `summary.py` (Task 2); `jev_ultrafast.Agent` from the dependency installed in Task 1.
- Produces: a module-level `mcp` object (`mcp.server.mcpserver.MCPServer` instance) with one registered tool, `browse_web(url: str, goal: str, max_steps: int = 40) -> BrowseResult`. Running `python server.py` starts it over stdio.

**Verified SDK behavior this task relies on** (checked directly against the installed `mcp==2.2.0` package before writing this plan — see the design spec's "Testing" section for why keys aren't available to test the browser path itself):
- `from mcp.server.mcpserver import MCPServer` and `from mcp.server.mcpserver.exceptions import ToolError` are the correct import paths (`mcp.server.fastmcp.FastMCP` is gone in this version and raises `ModuleNotFoundError`).
- `MCPServer(name: str)` takes the server name as its first positional/keyword arg.
- `@mcp.tool()` decorates a plain (sync) function; a `TypedDict` return annotation produces both a correct JSON input schema and populated `structured_content` on results — confirmed with a throwaway `TypedDict`-returning tool.
- Calling `await mcp.call_tool(name, arguments_dict)` directly (no client/transport needed) is the right way to unit test a tool in-process: on success it returns a `CallToolResult` (`.is_error`, `.structured_content`, `.content`); on a `ToolError` raised inside the tool, `call_tool` re-raises `ToolError` (message prefixed `"Error executing tool <name>: ..."`) rather than swallowing it into an `is_error` result — that swallowing only happens at the outer JSON-RPC handler used when a real client calls the server. So tests should use `pytest.raises(ToolError)`, not check `.is_error`.
- `await mcp.list_tools()` returns `Tool` objects with a `.input_schema` (snake_case) dict.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_server.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'server'`

- [ ] **Step 3: Write `server.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -v`
Expected: 3 passed

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: 6 passed (3 from Task 2, 3 from this task)

- [ ] **Step 6: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "Add browse_web MCP tool wrapping jev_ultrafast.Agent"
```

---

### Task 4: README and registration instructions

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: nothing (documentation only).
- Produces: nothing consumed by other tasks — this is the last task.

- [ ] **Step 1: Write `README.md`**

```markdown
# jev-browser-mcp

An MCP server exposing [TypeSafe AI](https://typesafe.ai)'s **Jev** browser agent
(via [`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast))
as a single tool, `browse_web(url, goal, max_steps)`, callable from Claude Code
or Claude Desktop.

## Status

Design and implementation are complete, but **the actual browser-automation
path is not yet verified end-to-end** — it requires two things not available
as of this writing:

- `TYPESAFE_API_KEY` (TypeSafe is in waitlisted early access)
- An OpenRouter (or other OpenAI-compatible) key for `TEXT_MODEL_API_KEY`

Until both are set, `browse_web` fails fast with a clear error naming the
missing variable(s) — this path *is* tested (see `tests/test_server.py`).

## Setup

1. Install dependencies:

   \`\`\`bash
   uv sync
   \`\`\`

2. Copy `.env.example` to `.env` and fill in `TYPESAFE_API_KEY` and
   `TEXT_MODEL_API_KEY` once you have them.

3. Install and pair [Browser Harness](https://github.com/browser-use/browser-harness)
   (installed automatically by `uv sync` as a dependency of `jev-ultrafast`):

   \`\`\`bash
   uv run browser-harness --doctor
   \`\`\`

   Allow remote debugging in Chrome when prompted.

4. Run the test suite:

   \`\`\`bash
   uv run pytest
   \`\`\`

## Register with Claude Code

\`\`\`bash
claude mcp add jev-browser -- uv run --directory /Users/eduardo/Projects/Personal/jev-browser-mcp --env-file .env python server.py
\`\`\`

## Manual smoke test (needs real API keys)

\`\`\`bash
uv run mcp dev server.py
\`\`\`

Opens the MCP inspector so you can call `browse_web` by hand before wiring it
into Claude.
```

- [ ] **Step 2: Verify the full test suite still passes**

Run: `uv run pytest -v`
Expected: 6 passed

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "Add setup and registration instructions"
```
