# jev-runner — guide for agents

For any AI agent (Grok Bot, Claude, …) that will drive apps on Eduardo's Android tablet or Mac
Chrome through jev-runner. Read this whole page before the first call.

## What it does

You send one natural-language goal; jev-runner drives the app step by step with TypeSafe's Jev
model and returns a short JSON result. You never tap, read screens or handle images yourself.

## Connect (on Eduardo's Mac)

MCP server over stdio:

```bash
uv run --directory /Users/eduardo/Projects/Personal/jev-browser-mcp --env-file .env python server.py
```

Prerequisites (already set up on this Mac): `.env` with `TYPESAFE_API_KEY` and the DeepSeek
text-model key; `~/.local/share/jev-runner/config.toml` with the tablet serial, `own_accounts`
and `navigation_labels`; the tablet on USB or the same Wi-Fi with wireless debugging on.
Check with the `status` tool: `android.connected` must be `true`.

If the `jev-runner` tools aren't in your session, register it once:
`claude mcp add jev-runner -- uv run --directory /Users/eduardo/Projects/Personal/jev-browser-mcp --env-file .env python server.py`

### Pointing at a specific Chrome profile

For `target: "chrome"`, jev-runner drives whichever Chrome the Browser Harness daemon is attached to —
which may not be the profile you want (this has picked the wrong profile repeatedly). **Always confirm
the profile first**: open a known page and `screenshot`, before anything else.

To bind an instance to one profile, launch that Chrome with its own remote-debugging port and run a
**separate** jevd pointed at it, with its own data dir so it doesn't touch the shared instance:

```bash
BU_CDP_URL=http://127.0.0.1:9333 BU_NAME=<label> JEV_DATA_DIR=~/.local/share/jev-<label> \
  uv run --directory /Users/eduardo/Projects/Personal/jev-browser-mcp --env-file .env python server.py
```

Each data dir gets its own socket automatically, so instances stay isolated (a long `JEV_DATA_DIR` is
fine now — the socket is relocated to a short path when needed).

## Tools

| Tool | Use |
|---|---|
| `run_task(target, goal, url_or_app, params, money_params, max_steps, session_id, learn, save_as)` | One goal. `target` is `"android"` or `"chrome"`. `url_or_app`: Android package (preferred, e.g. `net.bac.sbe.android`, `com.whatsapp`) or URL. Pass `session_id` to continue in the same session. |
| `resume(session_id, input, instruction, confirm)` | Continue a paused session. |
| `screenshot(session_id)` | Saves a PNG of the screen, returns its path. Use it to check before approving anything. |
| `read(session_id)` | Read-only. Returns the page's visible `text` and per-iframe `frames`, **including cross-origin iframes** the normal snapshot can't see (e.g. a white-labelled GHL automation screen). Use it to list/read content when a `run_task` only "waits" because the real UI is in an iframe. It **self-settles** (re-reads until the text stops growing, up to ~12 s), so no manual sleep is needed. Only visible rows of a virtualised table are captured. Never clicks or changes anything. |
| `end_session(session_id)` | Always call when finished, and after `money_action_uncertain`. |
| `status`, `list_flows`, `delete_flow` | Housekeeping. |

## Results and what to do

| `status` | Meaning | Do |
|---|---|---|
| `done` | Goal done and confirmed by an independent check of the final screen. | Next step. |
| `unverified` | Jev said done, the check disagreed (`reason`). | Treat as NOT done. `screenshot`, then retry or fix the goal. |
| `blocked` | Could not continue (`reason`, e.g. `jev_blocked`, `screen_unreadable`, `missing_value`). | `screen_unreadable` right after opening an app: wait 3 s and run again with the same `session_id`. `missing_value`: put the exact text in the goal. |
| `paused_needs_input` | A password/PIN/code field (`field`). | `resume(session_id, input=<value>)`. Never write the value anywhere else. |
| `paused_mismatch` | A money button, and the amount/recipient did not both match the screen (`expected` vs `seen`), or a second money action in one run. | Check `expected`/`seen` and a `screenshot`. Approve with `resume(session_id, confirm=True)` only if it is exactly the intended payment. |
| `error` `money_action_uncertain` | A money tap may or may not have gone through. | Do NOT retry. Check the bank app's movements, then `end_session`. |
| `error` other | Provider/device problem (e.g. HTTP 529 = Jev overloaded). | Retry once after a few seconds. |

## Only through jev-runner (hard rule)

Never drive the device yourself — no `adb shell input`, no uiautomator2 / `u2` calls
(`click`, `touch.down`, `resourceIdMatches`, …), no scripts that tap the phone — for anything in
a banking or payment app, and never for a tap that can move money or send a message.
Those taps skip every protection: amount/recipient check, one-money-action limit, secret masking,
receipts and the final-screen check. If jev-runner can't get past a screen, stop and report the
`session_id`, `reason` and a `screenshot` path; the fix belongs in jev-runner.
(jev-runner already retries a tap that changed nothing with a short press, which is what web-view
cards in some bank apps need.)

## Message rules (enforced in code)

- **One send per run.** The final send/forward control ("Send", "Enviar") is tapped at most
  once per run and is never auto-retried, so a forward that returns to a similar-looking screen
  can't be sent twice. A second send in the same run returns `blocked` / `already_sent`.
- So: do each message or forward as its own `run_task`. If a send comes back `unverified` or
  `blocked`, do NOT re-run blindly — `screenshot`, confirm whether it actually went, then decide.

## Money rules (enforced in code — do not try to bypass)

- For any payment pass `params={"amount": <number>, "recipient": "<IBAN or account>"}` and
  `money_params=["amount","recipient"]`. jev-runner taps the final Transferir/Pagar on its own
  **only** if both match what the screen shows; otherwise it pauses.
- One money action per run. Do each transfer as its own `run_task`.
- Eduardo has authorised Smart-Bot to approve `paused_mismatch` itself. Approve only when
  `expected` matches the intended transfer and the screenshot shows the right accounts.

## Writing goals that work (learned on the real tablet)

- Put every value to type **in the goal text, exactly** (the text model never invents values):
  "type exactly: float marybel".
- Name people and chats **exactly as saved in the app** and add "if no chat with exactly that
  name appears, stop". (A goal saying only "Eduardo" once messaged the wrong person.)
- Say what not to tap when an app has traps: "Never tap Ask or Meta AI".
- Small goals, one screen-level task each, reusing the `session_id`: log in → open transfer →
  fill → confirm → share receipt. Check `screenshot` between them when money is involved.
- Banking sessions expire fast (BAC: ~8 minutes). Log in and go straight to the transfer.
- **Leave learning ON (it is the default).** Money runs are safe to learn: a replayed fill pulls
  this run's value fresh from `params` (`param:amount`), a stale recorded number is auto-rejected,
  secrets are never stored (always re-prompted), and every guard (amount+recipient match, the
  one-money-action limit) runs again on replay. Don't pass `learn=False`. Use `save_as` to name a
  flow you want to replay by name, and pass `flow=<name>` next time to reuse it.
- **A modal or promo popover blocks everything** — Jev returns `blocked` because it can't read or
  scroll under an overlay. Even on a read-only tour, allow dismissing it: add "first dismiss any
  popover with Not now / Close / Skip / Dismiss / Got it / the X, then …". Dismissing a promo or
  cookie banner is not a data change. (Don't accept anything that submits a form or changes records.)
