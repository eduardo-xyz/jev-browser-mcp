# jev-browser-mcp — Design

_Date: 2026-09-23_

## Purpose

Expose TypeSafe's Jev-powered browser agent (from
[`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast))
as an MCP tool that Claude Code and Claude Desktop can call to drive a real
Chrome browser toward a natural-language goal, as an alternative to the
Claude-in-Chrome extension for tasks that benefit from Jev's speed.

## Non-goals

- Not re-implementing or forking `jev_ultrafast` — it's a dependency, pulled
  from its GitHub repo, not vendored.
- Not exposing step-by-step/interactive control (start/step tools). One
  tool that runs to completion is enough for the current use case.
- Not handling authentication/session-persistence flows beyond what
  `jev_ultrafast` + Browser Harness already do.

## Architecture

```
Claude (Code/Desktop)
      │  MCP tool call: browse_web(url, goal, max_steps)
      ▼
server.py (FastMCP)
      │  jev_ultrafast.Agent(url, goal)
      ▼
jev_ultrafast (pip dep, from GitHub)
      │  Jev API (TypeSafe)      │  OpenRouter (text values)
      ▼                          ▼
Browser Harness ──► Chrome (remote debugging)
```

## Components

- **`pyproject.toml`** — `uv`-managed. Dependencies:
  - `jev-ultrafast @ git+https://github.com/browser-use/jev-ultrafast.git`
  - `mcp[cli]` (FastMCP SDK)
- **`server.py`** — single FastMCP tool:

  ```python
  browse_web(url: str, goal: str, max_steps: int = 40) -> dict
  ```

  Behavior:
  1. Validate required env vars are present (`TYPESAFE_API_KEY`,
     `TEXT_MODEL_API_KEY`); raise a clear `ToolError` if missing, before
     touching the browser.
  2. Construct `jev_ultrafast.Agent(url, goal)`.
  3. Drain `agent.run()`, capping iterations at `max_steps`. If the cap is
     hit before `done`/`blocked`, stop and report `status: "max_steps"`.
  4. Return a trimmed summary — never the raw per-step payloads (which
     include full request/response bodies sent to Jev/OpenRouter):
     ```json
     {
       "status": "done | blocked | max_steps | error",
       "final_url": "...",
       "elapsed_ms": 0,
       "steps": [
         {"action": "...", "kind": "click|fill|select|wait",
          "text": null, "page_changed": true, "elapsed_ms": 0}
       ]
     }
     ```
  5. Always call `agent.close()` (via `with Agent(...) as agent:`), even on
     error, so the browser session doesn't leak.
- **`.env.example`** — the four vars from upstream's `.env.example`
  (`TYPESAFE_API_KEY`, `TYPESAFE_MODEL`, `TEXT_MODEL_API_KEY`,
  `TEXT_MODEL_BASE_URL`, `TEXT_MODEL`, `TEXT_MODEL_REASONING`).
- **`README.md`** — setup: `uv sync`, fill in `.env`, install/pair Browser
  Harness (`uv run browser-harness --doctor`), then register with Claude
  Code (`claude mcp add jev-browser -- uv run --directory <path>
  --env-file .env python server.py` or equivalent).
- **`.gitignore`** — `.venv/`, `.env`, `__pycache__/`, `uv.lock` is kept
  (matches upstream convention of committing lockfiles).

## Error handling

| Failure | Behavior |
|---|---|
| Missing `TYPESAFE_API_KEY` / `TEXT_MODEL_API_KEY` | Tool returns an error immediately, no browser/API calls made. |
| Browser Harness not running/paired | Propagates the connection error from `jev_ultrafast.Browser` as the tool's error message. |
| Jev/OpenRouter API error (network, 4xx/5xx) | `jev_ultrafast` already retries transient errors (429/503/529) and raises `RuntimeError` on persistent failure; the tool surfaces that message as-is. |
| Agent loops without progress | `jev_ultrafast.Agent` already sets `status: "blocked"` after 3 no-op ticks; `max_steps` is a second, coarser safety cap at the MCP-tool level. |
| Unexpected exception mid-run | Caught, browser closed via context manager, tool returns `status: "error"` with the exception message — never a raw stack trace to Claude. |

## Testing

No `TYPESAFE_API_KEY` or OpenRouter key is available yet, so an actual
`browse_web` call cannot be exercised end-to-end as part of this work.
What *can* be verified today:

- `uv sync` resolves the dependency graph cleanly (confirms the GitHub
  dependency pin is valid and installable).
- `uv run mcp dev server.py` (or equivalent) starts the server and lists
  `browse_web` with the correct schema.
- Calling the tool with no env vars set returns the clean pre-flight error
  described above, not a stack trace.
- A unit test for the response-trimming logic (given a fake list of
  `jev_ultrafast` step dicts, confirm the summary only contains the
  documented fields) — this part has no external dependency and can be
  fully tested offline.

The `done`/`blocked`/`max_steps` control flow and the actual browser
automation remain unverified until real API keys are available. This will
be called out explicitly when the implementation is reported as complete.

## Repo layout

```
jev-browser-mcp/
├── pyproject.toml
├── uv.lock
├── .env.example
├── .gitignore
├── README.md
├── server.py
└── tests/
    └── test_summary.py
```
