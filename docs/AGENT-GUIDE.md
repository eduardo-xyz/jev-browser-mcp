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

## Tools

| Tool | Use |
|---|---|
| `run_task(target, goal, url_or_app, params, money_params, max_steps, session_id, learn, save_as)` | One goal. `target` is `"android"` or `"chrome"`. `url_or_app`: Android package (preferred, e.g. `net.bac.sbe.android`, `com.whatsapp`) or URL. Pass `session_id` to continue in the same session. |
| `resume(session_id, input, instruction, confirm)` | Continue a paused session. |
| `screenshot(session_id)` | Saves a PNG of the screen, returns its path. Use it to check before approving anything. |
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
- `learn=False` for one-off money runs; save a verified run with `save_as` if it should replay.
