# jev-runner

A daemon (`jevd`) plus a thin MCP front end that runs [TypeSafe AI](https://typesafe.ai)'s
**Jev** agent (via [`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast))
on **Mac Chrome** and on **Android** apps, callable from Claude Code or Claude Desktop.
`server.py` only forwards tool calls to the daemon, which autostarts on first use and owns
sessions, devices, flows and safety guards.

## Status

Design and implementation are complete, but **the actual agent path is not yet verified
end-to-end with real Jev** -- it requires two things not available as of this writing:

- `TYPESAFE_API_KEY` (TypeSafe is in waitlisted early access)
- An OpenRouter (or other OpenAI-compatible) key for `TEXT_MODEL_API_KEY`

Until both are set, `run_task` fails fast with an error naming the missing variable(s),
before any device or browser work. Everything else is tested offline (no network, device or
keys). Set the keys, then restart `jevd` so it picks them up.

## Tools

| Tool | Purpose |
|---|---|
| `run_task(target, goal, url_or_app?, params?, money_params?, flow?, save_as?, max_steps=40, session_id?, device?, learn=True)` | Run one goal on `chrome` or `android`. `url_or_app` is a URL, an Android package, or an Android app name. |
| `resume(session_id, input?, instruction?, confirm=False)` | Continue a paused session. |
| `screenshot(session_id)` | Save a screenshot; returns a file path. |
| `end_session(session_id)` | Close the session and release the device. |
| `status()` | Daemon, sessions and device holders. |
| `list_flows()` / `delete_flow(name)` | Manage saved flows. |

Usage rules: one `run_task` call per goal. Statuses: `paused_needs_input` means ask the user
and call `resume(input=...)`; `paused_mismatch` means show `expected`/`seen` and call
`resume(confirm=True)` only after the user approves; `error` with reason
`money_action_uncertain` is terminal -- have the user check the app manually, then
`end_session`. A `device_busy` error names the session holding the device.

## Setup

1. Install dependencies: `uv sync`
2. Copy `.env.example` to `.env` and fill in `TYPESAFE_API_KEY` and `TEXT_MODEL_API_KEY`.
3. Chrome: install and pair [Browser Harness](https://github.com/browser-use/browser-harness)
   (installed by `uv sync` as a dependency of `jev-ultrafast`):

   ```bash
   uv run browser-harness --doctor
   ```

   Allow remote debugging in Chrome when prompted.
4. Android: enable wireless debugging and set up `config.toml` (below).
5. Run the tests: `uv run pytest`

### Data directory and config

Everything lives in `$JEV_DATA_DIR` (optional), default `~/.local/share/jev-runner/`:
`jevd.sock`, `config.toml`, `flows/`, `receipts/`, `jevd.log`.

Example `config.toml` (all keys optional):

```toml
android_serial = "zdirl7t84pqwh6ww"
android_mdns = "adb-zdirl7t84pqwh6ww"
android_ip = "192.168.1.215"
money_apps = []     # app packages/names where contextual labels (Confirm, Send) are guarded
own_accounts = []   # your own account numbers; transfers to these are recognised as own-account
# money_labels and money_labels_contextual have built-in defaults and can be overridden here
```

### Android notes

- **macOS Local Network / localhost relay:** the daemon reaches the phone over adb TCP through
  a local relay on localhost. macOS may prompt for Local Network access (or block the
  connection silently) -- allow it for your terminal / the process running `jevd` under
  System Settings > Privacy & Security > Local Network.
- **uiautomator2 first run:** it installs helper apps on the phone. On MIUI/HyperOS an
  install prompt appears on the device; approve it (and allow install via USB/adb) or the
  first run will stall.
- **OS-level controls:** besides tapping in apps, Android tasks can press home, open recents
  and the notification shade, and launch an app by name.

## Flows (learning)

By default (`learn=True`) a successful run is auto-saved as a flow, replayed on matching
later goals, and self-healed (updated) when the UI drifts. Results may include `flow_used`,
`flow_saved` and `flow_updated`. Pass `learn=False` to disable this; use `flow=` / `save_as=`
to select or name one explicitly. Secrets (OTP, passwords, PINs) are never stored, but flow
files can contain **non-secret typed text in plain JSON** under the data dir's `flows/`.

## Receipts

After an executed money action the daemon saves a screenshot and reference under
`receipts/`. The result's `receipt.path` may be `null` if the screenshot failed.

## Money safety

- Strong money labels (Transfer, Pay, Confirm payment, ...) are always guarded, on any app or site.
- Contextual labels (Confirm, Send) are guarded when money params are present or the app is in `money_apps`.
- A guarded tap happens autonomously only when the amount matches exactly and the recipient
  matches what is available on screen; otherwise the run pauses (`paused_mismatch`).
- At most one money tap per run.
- If the outcome is uncertain the session becomes terminal: check the app manually, then `end_session`.

## Register with Claude Code

```bash
claude mcp add jev-runner -- uv run --directory /Users/eduardo/Projects/Personal/jev-browser-mcp --env-file .env python server.py
```

## Manual smoke test (needs real API keys)

```bash
uv run mcp dev server.py
```
