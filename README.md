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

   ```bash
   uv sync
   ```

2. Copy `.env.example` to `.env` and fill in `TYPESAFE_API_KEY` and
   `TEXT_MODEL_API_KEY` once you have them.

3. Install and pair [Browser Harness](https://github.com/browser-use/browser-harness)
   (installed automatically by `uv sync` as a dependency of `jev-ultrafast`):

   ```bash
   uv run browser-harness --doctor
   ```

   Allow remote debugging in Chrome when prompted.

4. Run the test suite:

   ```bash
   uv run pytest
   ```

## Register with Claude Code

```bash
claude mcp add jev-browser -- uv run --directory /Users/eduardo/Projects/Personal/jev-browser-mcp --env-file .env python server.py
```

## Manual smoke test (needs real API keys)

```bash
uv run mcp dev server.py
```

Opens the MCP inspector so you can call `browse_web` by hand before wiring it
into Claude.
