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
| `run_task(target, goal, url_or_app?, params?, money_params?, flow?, save_as?, max_steps=40, session_id?, device?, learn=True, files?)` | Run one goal on `chrome` or `android`. `url_or_app` is a URL, an Android package, or an Android app name. |
| `resume(session_id, input?, instruction?, confirm=False)` | Continue a paused session. |
| `screenshot(session_id)` | Save a screenshot; returns a file path. |
| `end_session(session_id)` | Close the session and release the device. |
| `status()` | `sessions` (id, target, status, `idle_s`), `android` (`serial`, `connected`), `flows` (count). |
| `list_flows()` / `delete_flow(name)` | Manage saved flows. |
| `push_file(path, dest_dir?)` / `pull_file(phone_path)` | Copy a file Mac → phone (`/sdcard/Download`, indexed for pickers) or phone → Mac (`inbox/`). |
| `share_file(path, package?, mime?)` | Open Android's Share for a file (optionally straight into one app). |

Files: `run_task(target="chrome", files=[...])` uploads through the page's file inputs without the
OS picker; a chrome result lists new `downloads`. Allowed local folders: `outbox/`, `~/Downloads`,
`~/Desktop`, `~/Documents` (override with `file_roots`); hidden/secret-looking files are refused.
See [`docs/AGENT-GUIDE.md`](docs/AGENT-GUIDE.md#moving-files).

Usage rules: one `run_task` call per goal. Statuses: `paused_needs_input` means ask the user
and call `resume(input=...)`; `paused_mismatch` means show `expected`/`seen` and call
`resume(confirm=True)` only after the user approves; `paused_confirm` means a destructive
control (Delete/Remove/Eliminar, named in `control`) is about to be clicked -- check a
`screenshot` and call `resume(confirm=True)` only after the user approves; `error` with reason
`money_action_uncertain` is terminal -- have the user check the app manually, then
`end_session`. A `device_busy` error names the session holding the device.

`device`: one Android phone is supported (the one in `config.toml`). Omit it, or pass
`"default"` or the configured `android_serial`; every Android session shares that phone's
lock, so a second Android run while one is paused/running gets `device_busy`. Any other
value fails with `unknown_device: <value>`.

Agents driving jev-runner: read [`docs/AGENT-GUIDE.md`](docs/AGENT-GUIDE.md).

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
`jevd.sock`, `jevd.lock`, `config.toml`, `flows/`, `receipts/`, `screens/` (screenshots),
`outbox/` (files to upload/push), `inbox/` (files pulled from the phone),
`jevd.log`, `jevd.stderr` (daemon startup errors).

The daemon loads the repo `.env` itself (values already in its environment win), so starting it by
hand works too. `status` shows which Chrome it drives (`chrome`), whether the keys are loaded (`keys`)
and its `data_dir`.

Restart the daemon (e.g. after setting keys or editing `config.toml`; the next tool call
autostarts it): `pkill -f jev_runner.daemon`. List-typed keys must be lists of strings;
a wrong type makes the daemon refuse to start (see `jevd.stderr`).

Example `config.toml` (all keys optional):

```toml
android_serial = "zdirl7t84pqwh6ww"
android_mdns = "adb-zdirl7t84pqwh6ww"
android_ip = "192.168.1.215"
relay_python = "/usr/bin/python3"  # interpreter for the wireless relay helper (see below)
chrome_cdp_url = "http://127.0.0.1:9333"  # the Chrome to drive (its --remote-debugging-port);
chrome_label = "da"                       # unset: the harness picks a Chrome itself
money_apps = []     # app packages/names where contextual labels (Confirm, Send) are guarded
own_accounts = []   # your own account numbers; transfers to these are recognised as own-account
navigation_labels = []  # exact button labels you checked only open a menu (never money actions)
# money_labels and money_labels_contextual have built-in defaults and can be overridden here
```

### Android notes

- **macOS Local Network / localhost relay:** the daemon reaches the phone over adb TCP through
  a relay on 127.0.0.1. macOS Local Network privacy is enforced per binary and blocks both
  `adb` and the uv-managed CPython ("No route to host"), so the relay runs as a separate
  helper process (`jev_runner/relay_main.py`, stdlib only) under `relay_python`
  (default `/usr/bin/python3`; Homebrew python also works). If wireless still fails, allow
  that interpreter / your terminal under System Settings > Privacy & Security > Local Network.
- **uiautomator2 first run:** it installs helper apps on the phone. On MIUI/HyperOS an
  install prompt appears on the device; approve it (and allow install via USB/adb) or the
  first run will stall.
- **OS-level controls:** besides tapping in apps, Android tasks can press home, open recents
  and the notification shade, and launch an app by name.
- **Apps by name:** the name is matched against the package name's dot-separated parts first
  (`"BAC"` => `net.bac.sbe.android`), then against launcher labels when the device reports
  them (many uiautomator2 builds don't). Apps whose package doesn't contain their name
  (e.g. Gmail is `com.google.android.gm`) need the package name as `url_or_app`.

## Flows (learning)

Every DONE and BLOCKED verdict from Jev gets a second opinion from the text model on the final
screen. A rejected DONE returns status `unverified`; a BLOCKED run whose goal is visibly met returns
`done` (reason `verified_after_blocked`); likewise after three stale strikes that follow a landed action (`verified_after_stale`). Only runs confirmed this way are learned.

By default (`learn=True`) a successful, independently confirmed run is auto-saved as a flow, replayed on matching
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

## Passwords and authenticator codes

A password an agent passes through `resume(input=...)` has already crossed the chat and the
agent's transcript on disk. Two safer paths, both built in:

- **Stored in the macOS Keychain, typed by jevd.** Store once per site (Chrome: the page's host)
  or app (Android: the package); the value then goes from the Keychain to the device only, and an
  agent only ever sees `value_source: keychain` / `authenticator` in the history:

  ```sh
  python -m jev_runner.vault set app.example.com        # prompts; nothing on the command line
  python -m jev_runner.vault set-totp app.example.com   # base32 seed or otpauth:// URI
  python -m jev_runner.vault check app.example.com      # what is stored (never the values)
  python -m jev_runner.vault code app.example.com       # current code, to compare with your app
  ```

  Entries live in the login keychain under services `jev-runner` and `jev-runner-totp`. A stored
  value can only be typed into a secret field on its own host/package. Set `vault = "none"` in
  `config.toml` to disable.
- **Typed by you.** When a run pauses on a secret field, type it on the device or in the tab
  yourself and have the agent call `resume(session_id, typed=True)`.
