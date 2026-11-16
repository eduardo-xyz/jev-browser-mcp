# jev-runner — guide for agents

For any AI agent that drives apps on Eduardo's Android tablet or Mac Chrome through jev-runner.
Setup, Chrome binding and the long reference of what the runner does on its own live in
`docs/SETUP.md`; you do not need them to run tasks.

## What it does

You send one natural-language goal; jev-runner drives the app step by step and returns a short
JSON result with the screen it left. You never tap, read screens or handle images yourself.

## Tools

| Tool | Use |
|---|---|
| `run_task(target, goal, url_or_app, params, money_params, max_steps, session_id, learn, save_as, files)` | One goal. `target` is `"android"` or `"chrome"`. `url_or_app`: Android package (e.g. `net.bac.sbe.android`) or URL. Pass `session_id` to continue in the same session; with a URL too, the tab is taken there first (`tab_gone` = the tab was closed: run again without `session_id`). On chrome a URL an idle jev-runner tab already shows reuses that tab. |
| `resume(session_id, input, instruction, typed)` | Continue a paused session. |
| `read(session_id)` | Read-only: the page's visible `text`, per-iframe `frames` (cross-origin included) and a `States:` line for every switch/checkbox/radio. Self-settles. Never clicks. |
| `screenshot(session_id)` | Saves a PNG, returns its path. Only when you need the image. |
| `end_session(session_id)` | Always call when finished, and after `money_action_uncertain`. |
| `add_lesson(body, target, app, goal, screen_tags)` | Save durable playbook knowledge (e.g. "avoid the Quiero sheet"); future runs on that app/goal get it as a hint. |
| `push_file`, `pull_file`, `share_file` | Mac ↔ phone files; `share_file(path, package)` opens Android's Share into that app. `run_task(chrome, files=[...])` attaches files to a web page's upload fields. Paths must be under the data dir's `outbox/`, `~/Downloads`, `~/Desktop`, `~/Documents`. |
| `status`, `list_flows`, `delete_flow` | Housekeeping. |

## Every result

- `status` and `reason` (below), `last_actions` (what was done), `timing` (ms per phase).
- **`screen` = `{text, controls}`: the screen as the runner last read it, after its final action.
  Use it to decide the next step. Do not call `read` or `screenshot` after a run unless you need
  the image.**
- `detail` on blocked, unverified and max_steps: `controls` by kind, `sample` (labels on offer),
  `tried` (what was tapped), `weighed` (how BLOCKED scored), the verifier's reason. Read it
  before re-sending a goal.
- `downloads` (chrome): files the run saved to `~/Downloads`. `receipt` after a money tap.

## Statuses

| `status` / `reason` | Meaning | Do |
|---|---|---|
| `done` | Goal done; confirmed by an independent check of the final screen, or by the runner itself when it made every click the goal named. Read-only goals are never second-guessed. | Next step. |
| `unverified` | The check disagreed (`reason`). `tap_changed_nothing`: the named control was tapped and the screen stayed the same (re-read once). | Not done. On `tap_changed_nothing` name a different control of that card or row (its text, its icon) or scroll it fully into view; `detail.sample` lists what was on offer. |
| `blocked` / `jev_blocked` | The model found nothing to do towards the goal. `detail.why: blank_page` = nothing rendered (the URL is almost always wrong); `no_controls` = nothing clickable reached the model (a canvas or unreadable frame). Controls present but a high BLOCKED weight = the goal did not name a control on that screen. | Name the control as the app shows it (quote it), fix the URL, or report with `detail`. |
| `blocked` / `no_progress` | Three taps left the screen unchanged. | Name a different control; the ones in `detail.tried` do nothing there. |
| `blocked` / `control_disabled` | The named control is greyed out (`detail.control`). | Don't retry; change what governs it or report it. |
| `blocked` / `signed_out` (chrome) | The page redirected to a sign-in screen. | Ask Eduardo to sign in in that Chrome window, then run again. |
| `blocked` / `screen_unreadable` | Right after opening an app, usually. | Wait 3 s, run again with the same `session_id`. |
| `blocked` / `stuck_in_loop` | The same tap on the same screen, repeatedly. | Make the goal name the exact target. |
| `blocked` / `text_model_unavailable` | The provider that produces typed values failed (`detail.text_model`). | Put the exact text in the goal (`type 'Round Robin'`). |
| `blocked` / `already_sent` | A second send in one run. | One message per run. |
| `max_steps` | Step limit hit. | `detail.steps` shows what happened; shorten or sharpen the goal. |
| `paused_needs_input` | A password/PIN/code field with nothing stored. | Ask Eduardo to type it on the device, then `resume(typed=True)`. Better: store it once with `python -m jev_runner.vault set <host-or-package>` (`set-totp`, `set-user`), then it is typed without any model. Never write the value anywhere. |
| `error` / `money_action_uncertain` | A money tap may or may not have gone through. | Do NOT retry. Check the app's movements, then `end_session`. |
| `error` / `target_unreachable` | Chrome or the device dropped mid-run. | `status`, make sure it is up, run again in a new session. |
| `error` / `stale_page` | Three refused acts in a row: the control kept changing under the read. `verified_after_stale` as `done` means it worked anyway. | Retry once. |
| `error` other | Provider or device problem (HTTP 529 = overloaded). | Retry once after a few seconds. |

`paused_mismatch` and `paused_confirm` exist only with `ask_before_money` / `ask_before_delete`
set true in jevd's config.toml. They are off: see Money rules.

## Rules

1. **Only through jev-runner.** Never drive the device yourself (`adb shell input`, uiautomator2
   calls, scripts that tap) for anything in a banking or payment app, or any tap that can move
   money or send a message. If jev-runner can't get past a screen, stop and report the
   `session_id`, `reason`, `detail` and the `screen` labels: the fix belongs in jev-runner.
2. **Money and delete taps never pause.** Eduardo's approval is given once, in jevd's config
   (`ask_before_money` / `ask_before_delete`, both off). Transferir/Pagar/Delete are tapped like
   any other control: **do not ask him per step.** Still pass `params={"amount": ..., "recipient":
   ...}` and `money_params=["amount","recipient"]` for a payment so the receipt records them. A
   money tap is never retried. One transfer per `run_task` keeps one receipt per transfer.
3. **One send per run.** The final Send/Enviar is tapped at most once per run and never
   auto-retried. One message or forward per `run_task`; if it comes back `unverified`, look at
   `screen` before deciding whether it went.
4. **Bank login is entered by the human on the device**, by design. Expect: Eduardo types the
   password and taps Ingresar, then you run the task. Don't try to fill it.
5. **Leave learning on** (default). Secrets are never stored, values come fresh from `params`.

## Writing goals that work

- **Several steps per goal, not one tap per run.** The runner handles on its own: a quoted label
  after a click verb (`click 'Unit: minutes'`, in order, once each), a value spelled out for a
  named field (`type '500' into the Monto field`), `hide the keyboard`, `press Escape`, a
  canvas node named in the goal (`bring 'WAIT 4 hours' into view, then click it`), a tap that
  changed nothing (not offered again). One `run_task` per task, several taps inside it.
- **Quote labels exactly as the app shows them.** Same-named controls carry their row:
  `Domain Options — <card's first line>`, `switch — Dashboard`, `user row edit — <person>`. Two
  cards with the same title are numbered: `... — 20` and `... — 20 (2)`.
- **Say what not to tap** (`Do not click Delete or Publish`): those controls are never shown to
  the model. After every quoted click has happened, a model act on an unnamed control ends the
  run `done` with `reason` `named_clicks_done`; if that step was wanted, name it.
- Put every value to type in the goal, exactly. Name people and chats exactly as saved, and add
  "if no chat with exactly that name appears, stop".
- A popover blocks everything: allow "first dismiss any popover with Not now / Close / Skip /
  Got it / the X, then …". Dismissing a promo is not a data change.
- Banking sessions expire fast (BAC: ~8 minutes): go straight to the task.
- Controls inside cross-origin iframes (GHL's automation UI) are normal actions. A list that
  scrolls inside the page gets `Scroll the list down/up`; a canvas gets `Pan the canvas ...` and
  `Bring <title> into view`. Don't ask for Fit to Screen on a canvas.
- Known GHL routes that render blank: `/settings/team` (use `/settings/users`) and the plural
  `/automation/workflows/<id>` (the editor is `/automation/workflow/<id>`).
