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
**separate** jevd pointed at it, with its own data dir so it doesn't touch the shared instance. Put
the binding in that data dir's `config.toml`, so it holds no matter who starts the daemon:

```toml
# $JEV_DATA_DIR/config.toml
chrome_cdp_url = "http://127.0.0.1:9333"   # the Chrome's --remote-debugging-port endpoint
chrome_label = "<label>"                    # short name for its harness daemon (e.g. "da")
```

```bash
JEV_DATA_DIR=~/.local/share/jev-<label> \
  uv run --directory /Users/eduardo/Projects/Personal/jev-browser-mcp --env-file .env python server.py
```

`JEV_DATA_DIR` is the only variable the launch needs: it picks the data dir, and the data dir's config
picks the Chrome. (`BU_CDP_URL`/`BU_NAME` in the environment still work for a one-off, but
`config.toml` wins when both are set, and the daemon logs the override.) The daemon also loads the
repo `.env` itself, so a jevd started without `--env-file` still has its model keys.

A signed-in web app can still bounce one fresh load of a deep link to its login page (seen with a
session token mid-refresh). Jev reloads the start URL once before reporting `signed_out`; a truly
signed-out Chrome lands on the login page again and blocks as before.

A click that opens a new tab (window.open, target=_blank) is followed: the run continues in the new
tab and closes the one it came from when the session ends. While a page shows loaders or skeletons (or almost no text), the runner looks again on its own, up to
20 s per run, before asking Jev anything: no more BLOCKED on a navigation shell. Loaders inside a
cross-origin iframe count too, and a frame whose text is still changing is waited for as well. Controls that another
element is sitting on (an open panel, a sticky bar) are left out of the observed actions, since a click
there would land on the covering element. Pages built from web components (shadow DOM) are read through
their shadow roots, and their controls are clicked there too. Panels that scroll inside the page (a
log or table in its own scroll box while the window never moves) get `Scroll the list down/up` actions,
so rows below the fold are reachable without a date picker or a search. A flow or diagram canvas (a workflow
editor's node graph) gets `Pan the <canvas> down/up/left/right` actions: its nodes sit beyond the visible pane
in both directions, and every titled node beyond the pane is offered as `Bring <title> into view`, which pans
to it in one step, measuring as it goes (blind pans overshoot). Quote the title exactly as the card shows it
(a `jev_blocked` detail lists them under `canvas_nodes`); a node's settings text ("4 hours") is not on its card:
"bring the node 'WAIT' into view, then click it". Do not ask for "Fit to Screen" or a
zoom-out on such a canvas: it shrinks node titles below what Jev can read (3 px) and puts hundreds of
unlabelled controls on screen, more than the model takes. The text of a cross-origin
iframe is part of what Jev and the verifier read, not only of `read()`. Dropdown triggers built as plain focusable boxes (a ticket's property fields: `Status: Pending`) are offered as
clicks named by their field. Same-named controls (one `Domain Options` per card, one `Open` per row) are told apart by their row:
`Domain Options — <the card's first line>`, so name the row or card in the goal. A click is refused as stale only
when its own control or the document changed, not when the page scrolled or another field ticked.
A page whose observe comes back with empty `text` after it has settled really
shows nothing in the viewport (one such GHL route renders only a single-button overlay): check the URL
in a browser before blaming the tool.

**Check before the first Chrome run:** `status` → `chrome.cdp_url` must be the Chrome you expect and
`keys` must all be true. A jevd attached to the wrong Chrome (one that is not signed in) shows every
run as `blocked / signed_out`; a missing key fails every run.

Each data dir gets its own socket automatically, so instances stay isolated (a long `JEV_DATA_DIR` is
fine now — the socket is relocated to a short path when needed).

### Identifying jev-runner's tabs (named + coloured tab groups)

jev-runner tags each Chrome tab it opens with AI Broker so the **AI Broker Tab Groups** extension puts
it in a named, coloured group — the same mechanism `relay.mjs` uses for chrome-devtools-mcp. It's
best-effort and off unless three things are present, and a failure never affects a run:

1. `BU_NAME` set on the jevd launch, and a broker profile token at
   `~/Projects/Personal/AI-Broker/.broker/cli/claude-<BU_NAME>-cli.token` (override the dir with
   `JEV_BROKER_TOKEN_DIR`; broker URL defaults to `http://127.0.0.1:18765`, override with
   `JEV_BROKER_URL`). The group label defaults to `JEV <BU_NAME>`; set **`JEV_GROUP_NAME`** to show the
   session's own name instead (e.g. `JEV_GROUP_NAME=Turbo`) — `BU_NAME` stays the token key, so it
   can't double as the label. Chrome renders group titles in uppercase; that's display-only.
2. The broker running, and the **AI Broker Tab Groups** extension loaded in the Chrome that jevd
   drives (`BU_CDP_URL`).
3. The group **colour** is derived from the name; pin a specific one in
   `.broker/cli/browser-groups.json` under `colors` (e.g. `{"JEV da": "orange"}`) — that's Eduardo's
   file, not jevd's.

jevd claims each tab on creation and sends a keep-alive while a session is active, so the broker's
idle sweep (10 min) won't close an in-use tab. With no token/`BU_NAME` it is a silent no-op.

## Tools

| Tool | Use |
|---|---|
| `run_task(target, goal, url_or_app, params, money_params, max_steps, session_id, learn, save_as)` | One goal. `target` is `"android"` or `"chrome"`. `url_or_app`: Android package (preferred, e.g. `net.bac.sbe.android`, `com.whatsapp`) or URL. Pass `session_id` to continue in the same session. On chrome, a run for a URL that an idle jev-runner tab already shows **reuses that tab** (the same `session_id` comes back): no new tab and no page load. Call `end_session` first if you need a fresh tab. |
| `resume(session_id, input, instruction, confirm)` | Continue a paused session. |
| `screenshot(session_id)` | Saves a PNG of the screen, returns its path. Use it to check before approving anything. |
| `read(session_id)` | Read-only. Returns the page's visible `text` and per-iframe `frames`, **including cross-origin iframes** the normal snapshot can't see (e.g. a white-labelled GHL automation screen). Use it to list/read content when a `run_task` only "waits" because the real UI is in an iframe. It **self-settles** (re-reads until the text stops growing, up to ~12 s), so no manual sleep is needed. Only visible rows of a virtualised table are captured. Never clicks or changes anything. |
| `end_session(session_id)` | Always call when finished, and after `money_action_uncertain`. |
| `add_lesson(body, target, app, goal, screen_tags)` | Save durable playbook knowledge to the experience memory (replaces hand-editing `lessons.md`), e.g. "avoid the Quiero sheet". Scope it with `target`/`app`/`goal` so future runs on the same app/goal retrieve it automatically. Account/PII digit runs are stripped. |
| `status`, `list_flows`, `delete_flow` | Housekeeping. |
| `push_file(path, dest_dir)` | Mac → phone: copies into `/sdcard/Download` (default) and indexes it so the phone's gallery/document/attach pickers list it. |
| `pull_file(phone_path)` | Phone → Mac: copies a `/sdcard/...` file into the data dir's `inbox/`. |
| `share_file(path, package, mime)` | Opens Android's Share for a Mac file (pushed first) or a `/sdcard/...` file; with `package` (e.g. `com.whatsapp`) straight into that app. Then `run_task` to pick the chat and tap Send. Prefer this over an app's in-app file picker (WhatsApp's document picker often stalls). Refused while a run is driving the phone. |

Every run also records scrubbed metrics, and each run automatically retrieves the lessons relevant
to its goal/app and feeds them to Jev as hints — so writing a good lesson once helps future runs.

## Moving files

Several ways, pick the one that fits:

| From → to | How |
|---|---|
| Mac → web page | `run_task(target="chrome", files=["/Users/…/Downloads/invoice.pdf"], goal="Attach the invoice and save")`. The page's file inputs (even hidden ones behind an "Upload" button) appear as `Upload file <name> → <field>` actions; choosing one sets the file directly. The macOS file picker never opens. A single-file input takes the next file in the list. |
| web page → Mac | Anything a chrome run downloads is listed in the result's `downloads` (finished files new in `~/Downloads`). |
| Mac → phone | `push_file`, then a `run_task` that attaches it from the app's picker (it's in Downloads / recent files). |
| Mac or phone → app on the phone | `share_file(path, package=...)`, then `run_task` to choose the recipient and Send. |
| phone → Mac | `pull_file("/sdcard/Download/x.pdf")`. |
| phone → web page | `pull_file`, then `run_task(chrome, files=[<local path>])`. |

Local paths must be under the allowed folders: the data dir's `outbox/` (a bare file name means
outbox), `~/Downloads`, `~/Desktop`, `~/Documents`, or what `file_roots` in `config.toml` lists.
Hidden files and key/secret-looking names (`.env`, `id_rsa`, `*.pem`, `*token*`, …) are refused.
Uploading is not sending: the page's Send/Save button afterwards still goes through every guard.

## Results and what to do

Every result carries `timing` (ms per phase: `setup_ms` connect/launch or tab open, `observe_ms` screen reads, `decide_ms` TypeSafe choices, `text_ms` field values, `act_ms` taps/typing, `verify_ms` the final check). If a run felt slow, look there before changing the goal.

| `status` | Meaning | Do |
|---|---|---|
| `done` | Goal done and confirmed by an independent check of the final screen. | Next step. |
| `unverified` | Jev said done, the check disagreed (`reason`). | Treat as NOT done. `screenshot`, then retry or fix the goal. |
| `blocked` | Could not continue (`reason`, e.g. `jev_blocked`, `screen_unreadable`, `missing_value`, `stuck_in_loop`, `signed_out`). `jev_blocked` carries `detail`: the URL, `text_chars`, `controls` by kind (plus `in_iframes`, `covered`, `waited_loading_ms` when relevant) `sample` (the first control labels on offer) and `weighed` (how BLOCKED scored against acting). `why: blank_page` means nothing rendered at all after the loading wait (`text_chars` 0, at most an injected widget button): the URL is almost always wrong, a route the app does not know shows an empty shell (e.g. `/automation/workflows/<id>` instead of `/automation/workflow/<id>`); fix the URL before reporting. `why: no_controls` means nothing clickable reached Jev (a canvas, a frame it cannot read, a page still arriving): `screenshot` and report it here. Controls present but a high BLOCKED weight: the goal did not name a control on that screen; name it. |
| `error` / `stale_page` | Three times in a row the chosen control had changed or was covered between the read and the act. `detail` says which action was refused and why; include it when reporting. If an earlier action had landed and the goal is visibly met on a fresh read, the run ends `done` with reason `verified_after_stale` instead (a Save whose modal closed and page churned). | `signed_out` (Chrome): the page redirected to a sign-in screen (account chooser, password, 2FA). Don't retry and don't try another tool: ask Eduardo to sign in in that Chrome window, then run again. A goal that itself asks to sign in is not stopped. `screen_unreadable` right after opening an app: wait 3 s and run again with the same `session_id`. `missing_value`: put the exact text in the goal. `stuck_in_loop`: Jev kept repeating the same tap on the same screen (e.g. opening the wrong account, going back, opening it again) — don't just retry; make the goal name the exact target ("open the account ending 0743", "tap exactly …") or `screenshot` to see where it's stuck. |
| `paused_needs_input` | A password/PIN/code field (`field`) with nothing stored for that site/app. | **Preferred:** ask Eduardo to type it on the device / in the tab himself, then `resume(session_id, typed=True)`: the value never passes through the chat or your transcript. Better still, have it stored once (`python -m jev_runner.vault set <host-or-package>`, `set-totp` for authenticator codes): jevd then types it before ever pausing, and you never see it. Last resort: `resume(session_id, input=<value>)`; never write the value anywhere else. |
| `paused_mismatch` | A money button, and the amount/recipient did not both match the screen (`expected` vs `seen`), or a second money action in one run. | Check `expected`/`seen` and a `screenshot`. Approve with `resume(session_id, confirm=True)` only if it is exactly the intended payment. |
| `paused_confirm` | A destructive control (Delete/Remove/Eliminar/Trash…, named in `control`). Every such click pauses; `reason` is `destructive_action`, or `destructive_already_attempted` if this run already deleted something (a row-menu Delete plus its dialog's Delete is two confirms). | `screenshot` and check it is exactly the record to delete. Approve with `resume(session_id, confirm=True)` only with the human's OK. If the screen changed meanwhile it re-pauses instead of clicking. |
| `error` `money_action_uncertain` | A money tap may or may not have gone through. | Do NOT retry. Check the bank app's movements, then `end_session`. |
| `error` `target_unreachable` | The driven Chrome or device dropped the connection mid-run (Chrome closed, restarted, or running without its remote-debugging port); `detail.error` has the raw message. | Check `status` -> `chrome.cdp_url`, make sure that Chrome is up, then run again with a new session. |
| `error` other | Provider/device problem (e.g. HTTP 529 = Jev overloaded; HTTP 422 = the request was rejected, jevd's log has the provider's reason). A screen with more than 255 controls (a whole workflow canvas after Fit to Screen) is trimmed before it reaches Jev, controls named in the goal first: name the control you want. | Retry once after a few seconds. |

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
  That authorisation does **not** cover `paused_confirm` (deletes): those need the human.

## Writing goals that work (learned on the real tablet)

- Put every value to type **in the goal text, exactly** (the text model never invents values):
  "type exactly: float marybel".
- Name people and chats **exactly as saved in the app** and add "if no chat with exactly that
  name appears, stop". (A goal saying only "Eduardo" once messaged the wrong person.)
- Say what not to tap when an app has traps: "Never tap Ask or Meta AI".
- Small goals, one screen-level task each, reusing the `session_id`: log in → open transfer →
  fill → confirm → share receipt. Check `screenshot` between them when money is involved.
- Banking sessions expire fast (BAC: ~8 minutes). Log in and go straight to the transfer.
- **Bank login is entered by the human on the device, by design** — jev-runner does not auto-log-in
  (the password is never delivered to the daemon). Expect: human types Contraseña on the Pad and taps
  Ingresar, then you run the transfer. Don't try to fill the login password or look for a "login skill".
- **Leave learning ON (it is the default).** Money runs are safe to learn: a replayed fill pulls
  this run's value fresh from `params` (`param:amount`), a stale recorded number is auto-rejected,
  secrets are never stored (always re-prompted), and every guard (amount+recipient match, the
  one-money-action limit) runs again on replay. Don't pass `learn=False`. Use `save_as` to name a
  flow you want to replay by name, and pass `flow=<name>` next time to reuse it.
- **Controls inside a cross-origin iframe are actionable** (e.g. GHL's automation UI): they appear
  as normal actions and `run_task` can click, fill, and scroll them — no special syntax. The runner
  waits for the iframe to finish mounting on its own (a "wait" now settles until the page stops
  changing), and scrolling is routed into the iframe, so rows below its fold are reachable. Only the
  tab whose page is being driven is used, even if other tabs show the same page. Validated end to end
  on GHL (open a workflow from the list). A *money* button inside an iframe will **pause for approval**
  (the amount/recipient check reads the top-frame text, which can't see iframe amounts) — that's the
  safe default, not a failure. Note: a goal that only *navigates* ("open X") can still finish
  `blocked` even though it worked, because Jev doesn't always recognise the new screen as "done";
  `screenshot`/`read` to confirm, or phrase the goal around a visible end state.
- **A modal or promo popover blocks everything** — Jev returns `blocked` because it can't read or
  scroll under an overlay. Even on a read-only tour, allow dismissing it: add "first dismiss any
  popover with Not now / Close / Skip / Dismiss / Got it / the X, then …". Dismissing a promo or
  cookie banner is not a data change. (Don't accept anything that submits a form or changes records.)
