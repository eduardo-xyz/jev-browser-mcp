# jev-browser-mcp v2 — Jev on Mac Chrome and Android (adb) — Design

_Date: 2026-10-29_
_Supersedes the tool surface in `2026-09-23-jev-browser-mcp-design.md`._

## Intent and the why

Today, when Eduardo asks Claude to do something in a browser or on an
Android device, Claude itself is the agent loop: every tap costs a
screenshot or page dump in context, a reasoning turn, and a tool call.
Twenty taps = twenty heavy round-trips. It is slow and wastes tokens.

On top of that, long jobs die in the gaps. Real example: paying rent
(BAC transfer) and the water bill (CRC) on a Redmi Pad over wireless adb.
The taps were not the slow part — the bank session expired mid-confirm,
kicked him back to login, demanded a fresh 6-digit code (which arrives on
his phone), and the Mac↔tablet adb link was flaky in between. With a
sticky device session that could take the live code without the
round-trip dying, the whole job would have been one pass.

Another real example (from a different Claude session): GHL tabs in Mac
Chrome loading blank or hanging, blocking work.

**The fix:** Claude delegates instead of driving. Claude sends one goal;
TypeSafe's **Jev** model makes every per-step decision locally and fast;
Claude gets back a short result. Repeat jobs replay saved steps and skip
the model entirely when the screens match.

**Guiding rule from Eduardo:** do everything that saves time and money.

## Goals

1. **One decision model, three targets.** Jev (`jev_ultrafast.model.choose`
   and `field_text`) makes every choice on:
   - **Mac Chrome** (desktop browser) — via Browser Harness / CDP, as today.
   - **Android native apps** — primary new target (BAC app, water utility
     app, etc.).
   - **Chrome on Android** — covered by the Android path (Chrome exposes
     page content through the accessibility tree). If the tree proves too
     thin for real pages, fall back to driving the device's Chrome over CDP
     via `adb forward` (builder verifies which is needed).
2. **Fast.** Warm connections, no per-call setup, no per-step subprocess,
   cheap screen reads. Every result reports timing so speed is measurable.
3. **Cheap in Claude tokens.** One tool call per goal; compact results; no
   images in the result unless explicitly requested.
4. **Sticky sessions.** A device/browser session survives pauses (waiting
   for a code), flaky wireless adb, and Claude-side delays. Resume continues
   on the same screen, no re-login.
5. **Pause-and-resume for live input.** When the job needs something only
   Eduardo has (e.g. Código BAC from his phone), it pauses and returns;
   Claude asks Eduardo and resumes in seconds.
6. **Autonomous money confirmation, guarded by code.** Claude/Jev may tap
   Transferir/Pagar on its own *when the amount and recipient on the confirm
   screen match what was requested* (Eduardo's decision). The match check is
   deterministic code, not model judgement (see Money guard).
7. **Saved flows + replay (in v1).** After a successful run, the steps can be
   saved as a named, parameterized flow and replayed near-instantly, with Jev
   called only for steps whose screen doesn't match.

## Non-goals

- Not forking/vendoring `jev_ultrafast`; it stays a git dependency. We reuse
  its `model` module (decision + text helper) and write our own loop and
  device layer, because `Agent` hard-wires `Browser(url)`.
- No vision/pixel model. Jev decides from a text element list; we keep it
  that way on Android too.
- No iOS, no non-Chrome desktop browsers.
- No Claude-in-the-loop per step (that is the thing being replaced).

## Anti-goals (failure even if it "works")

- A money button tapped when amount or recipient don't match the request.
- An OTP, password, or PIN ever sent to Jev or the text model.
- Results that dump step-by-step page text or screenshots into Claude's
  context by default (defeats the token goal).
- Sessions that silently restart from scratch (re-login) after a pause.
- Android screen reads that take seconds each (`uiautomator dump` via
  `adb shell` per step is too slow to be the main path).

## Constraints

- Python ≥3.12, `uv`, existing repo and MCP SDK (`mcp[cli]` `MCPServer`).
- Jev API: `POST https://api.typesafe.ai/v1/systemone` with
  `TYPESAFE_API_KEY` (waitlisted; not yet in hand). Text helper:
  OpenAI-compatible endpoint via `TEXT_MODEL_*` vars.
- Jev's input contract (from `jev_ultrafast/model.py`): page `{url, title,
  text}`, a list of elements each with label/role/value/checked/selected/
  expanded, and per-element operations `click` / `fill` / `select`, plus
  optional control actions (e.g. wait/back). Output: one typed choice, or
  DONE / BLOCKED.
- Android device: Redmi Pad, wireless adb from the Mac. Android 11+ wireless
  debugging ports change on reboot/re-pair.
- Banking apps may set `FLAG_SECURE` and/or hide parts of their
  accessibility tree. Unknown for BAC until tested — this is the main
  feasibility risk for the motivating use case.

## Architecture

```
Claude ──MCP stdio──► server.py (thin MCP front end)
                          │ local socket (unix domain)
                          ▼
                     jevd (long-lived local daemon)
                     ├─ session manager (sessions by id, idle TTL)
                     ├─ runner loop  ── jev_ultrafast.model.choose / field_text
                     ├─ flow store (saved flows, replay)
                     ├─ money guard / secret-field guard
                     └─ targets:
                          ChromeTarget  ── Browser Harness / CDP (Mac Chrome)
                          AndroidTarget ── uiautomator2 server on device, over adb
```

**Why a separate daemon (my call):** the MCP server process lives and dies
with each Claude session. Warm browser targets, the on-device uiautomator
server, the adb link, and paused sessions must outlive that. `server.py`
autostarts `jevd` if it isn't running.

### Target interface

Each target implements the same small interface the runner uses, modeled on
`jev_ultrafast.browser.Browser`:

- `observe(screenshot=False) -> page` — returns the dict shape Jev's
  `action_space`/`choose` expect: `url` (Android: `package/activity`),
  `title`, `text` (visible text, trimmed), `actions` (list of
  `{id, kind, node, label, role, value, ...}`), `fingerprint`.
- `fresh(page) -> bool` — is the screen still the one observed.
- `act(action, page, text=None)` — perform click / fill / select; raise
  `StalePage` if the screen changed since observation.
- `screenshot() -> bytes` — on demand only (receipts, debugging).
- `close()`.

### AndroidTarget

- Screen reads via a persistent on-device uiautomator server (the
  `uiautomator2` Python library / its agent), not `adb shell uiautomator
  dump` per step. Target: ~100–300 ms per observe (to be measured).
- Mapping accessibility nodes → Jev elements:
  - `clickable`/`long-clickable` → `click`; `EditText`/editable → `fill`;
    spinners/pickers → `select` where feasible, else `click`.
  - label = `text` ‖ `content-desc` ‖ resource-id tail ‖ hint.
  - role from class name (Button, EditText, CheckBox, Switch, ...).
  - `checked`/`selected` carried through.
  - `password=true` nodes are never exposed for `fill` to the text model
    (same policy as `snapshot.js` skipping password inputs).
- Click = tap at node bounds center (or uiautomator2's element click);
  fill = focus + set text. Control actions exposed to Jev: `BACK`, `WAIT`,
  `SCROLL_DOWN`/`SCROLL_UP` (needed because off-screen nodes aren't in the
  tree).
- Fingerprint = hash of package/activity + ordered (label, role, bounds)
  of actionable nodes.
- If the tree is empty/obviously hidden (FLAG_SECURE / secure app),
  return `blocked` with reason `screen_unreadable` rather than guessing.

### ChromeTarget

- Reuse `jev_ultrafast.browser.Browser` as-is where possible (one CDP
  session via Browser Harness, background tab).
- Add blank/hung-tab handling: if `readyState` never completes or body is
  empty after the load deadline, retry navigation once, then try in a
  foreground tab, then return `blocked` with reason `page_did_not_load`.

### Connection keeper (Android)

- `jevd` holds the device by serial or by mDNS name. On disconnect, it
  reconnects automatically (`adb connect`, and `adb mdns services` to
  rediscover a changed wireless-debugging port), then re-attaches the
  uiautomator server, then re-observes. A run that hits a disconnect
  waits/retries for a bounded time (default 30 s) before reporting
  `error: device_disconnected`; the session is **not** discarded.

## MCP tool surface

All results are compact JSON (target ≤ ~1–2 KB). Each includes
`session_id`, `status`, `target`, `where` (URL or package/activity),
`elapsed_ms`, `model_calls`, `last_actions` (last ≤5, one line each), and
`reason` when not `done`.

- `run_task(target, goal, url_or_app=None, params=None, flow=None,
  save_as=None, max_steps=40, session_id=None)`
  - `target`: `"chrome"` | `"android"` (device chosen from config; optional
    `device` param if more than one).
  - `goal`: natural language. `params`: exact values (e.g. `amount`,
    `recipient`) used by the text helper *and* the money guard.
  - `flow`: name of a saved flow to replay (Jev only on mismatched steps).
  - `save_as`: on `done`, save this run as a flow.
  - `session_id`: continue in an existing session instead of opening one.
- `resume(session_id, input=None, instruction=None)` — continue a paused
  session; `input` fills the pending secret field directly (never passes
  through any model); `instruction` optionally amends the goal.
- `screenshot(session_id)` — returns a local file path (not image bytes).
- `end_session(session_id)`.
- `status()` — targets/devices connected, sessions open, flows available.
- `list_flows()` / `delete_flow(name)`.

### Statuses

`done` · `paused_needs_input` (with `field` label) · `paused_mismatch`
(money guard; with `expected` vs `seen`) · `blocked` (with `reason`) ·
`max_steps` · `error` (with `reason`, session kept if recoverable).

## Safety

### Secret-field guard

A field is treated as secret if: Android `password=true`, HTML
`type=password`, or its label/hint matches a code pattern (código, code,
OTP, token, PIN, clave, contraseña, verification) — pattern list is
configurable. When Jev chooses to fill a secret field: do **not** call the
text helper; pause with `paused_needs_input`. On `resume(input=...)` type
the value directly. Secret values are never logged, stored in flows, or
returned. After typing a secret, every later observation in that session
scrubs that exact value (and the field's value) from page text and element
values before anything goes to Jev or the text model.

### Money guard

- **When the guard is on (my call):** only in a *guarded context* — the
  task supplies money params (`amount`, `recipient`, or any key listed in an
  optional `money_params: [...]` arg), **or** the current app/site is in a
  configured money-app allowlist (BAC, water utility, ...). Outside a
  guarded context, Confirmar/Send/etc. are ordinary buttons (so GHL flows,
  "Enviar código" on logins, and dialogs never pause).
- A "money action" is a click, inside a guarded context, on an element
  whose label matches a configurable list (Transferir, Pagar, Confirmar,
  Confirmar pago, Enviar, Pay, Send, Transfer, Confirm). "Continuar" /
  "Siguiente" are not on the list.
- Every money action gets the full check (my call: safer than trying to
  detect "the final" button). An intermediate screen that lacks the values
  pauses; if that proves annoying in practice, tune the label list per app.
- Before executing a money action, the runner checks that **every**
  provided money param (`amount`, `recipient`, and any other params marked
  money-critical) appears in the current screen's visible text, using
  normalized comparison. Amounts: strip currency symbols/separators,
  compare numeric value exactly. Recipient (Eduardo: "only what's
  available"): match against whatever identifying info the screen shows —
  if the screen shows a masked account (`****1234`), the visible digits must
  equal the corresponding digits of the requested account; if it shows a
  nickname/name or service number, case/whitespace-insensitive match
  against the requested recipient (the caller may pass several known forms,
  e.g. `recipient: ["Juan Pérez", "CR0501...1234"]`). Amount must match
  exactly in all cases.
- Match → tap autonomously. No match, or no money params supplied →
  `paused_mismatch` with what was seen; Claude asks Eduardo.
- Applies identically in live runs and flow replays.
- On `done` after a money action: save a screenshot of the resulting
  screen (comprobante) to a local receipts dir and extract a reference
  number if visible; return path + reference.

## Saved flows and replay

- A flow = name, target, app/url, and ordered steps. Each step records the
  expected screen signature (package/activity or URL path + set of
  actionable labels), the chosen element's stable locator (resource-id /
  label / role; CSS-ish locator for web), the action kind, and the value
  source for fills (`param:<name>`, `secret`, or literal non-sensitive
  text).
- Replay: observe → if the screen matches the step's signature and the
  locator resolves to exactly one element, act **without** a Jev call;
  otherwise call Jev for that step with the flow's goal, then try to
  re-sync with the next recorded step. Secret and money guards apply.
- Flows stored as JSON files in a local data dir (not in git). Format is
  the builder's call.
- Login/session expiry mid-replay: handled naturally — screens won't match,
  Jev takes over (and pauses for codes).

## Speed

- Daemon keeps: Browser Harness daemon + CDP session, adb connection,
  uiautomator2 server, HTTP/2 client to TypeSafe (already in
  `jev_ultrafast.model.CLIENT`).
- No screenshots during runs unless recording or receipts.
- Per-step timings (observe, model, act) recorded internally;
  `status()` or a debug flag can show them. Results carry totals only.
- Possible later optimizations, builder's discretion if cheap: prefetch the
  next observation while the model call is in flight is **not** safe (screen
  changes after act) — skip; but parallel text-helper call when Jev picks
  `TYPE_TEXT` is already how upstream works.

## Error handling

| Failure | Behavior |
|---|---|
| Missing `TYPESAFE_API_KEY` / `TEXT_MODEL_API_KEY` | `error` immediately, before any device/browser work (as v1). |
| adb device unreachable | Auto-reconnect loop (bounded); then `error: device_disconnected`, session kept. |
| Screen unreadable (secure app) | `blocked: screen_unreadable`. |
| Chrome tab blank/hung | Retry, foreground fallback, then `blocked: page_did_not_load`. |
| Jev/text API errors | Surface upstream message as `error`; session kept. |
| No progress 3 ticks | `blocked: no_progress` (upstream rule). |
| Idle session | Closed after TTL (default 15 min, configurable). |

## Testing

- Offline unit tests (no keys, no device): Android node→Jev element mapping
  from captured hierarchy XML fixtures; secret-field detection; money guard
  normalization/matching; flow record/replay matching logic with a fake
  target; result summarization.
- Integration with a fake target implementing the target interface, and a
  stubbed `choose` — exercises pause/resume, money guard, replay fallback.
- Manual, needs real keys + device: BAC app screen readability check
  (first thing to verify), a non-money flow on the Redmi Pad, a Mac Chrome
  flow on a GHL page.
- Real end-to-end remains unverified until `TYPESAFE_API_KEY` exists; this
  must be stated when reporting completion.

## Decided by Eduardo

- Targets: Mac Chrome, Android native apps (mostly), Chrome on Android.
- Jev is the decision model for all targets.
- Claude may tap money-confirm buttons autonomously when amount and
  recipient match the request.
- Saved flows/replay are in scope; prioritize anything that saves time and
  money.
- Recipient check matches "only what's available" on the confirm screen
  (masked digits / nickname / service number); amount always exact.
- Sending bank screen content (balances, account numbers, names) to
  TypeSafe and the text model is acceptable. No redaction layer beyond the
  secret scrub.
- Run an on-device feasibility check (Redmi Pad, BAC app readability)
  before building.

## Feasibility check results

Spike run 2026-10-29 on the Redmi Pad (model 2505DRP06G, Android 16,
HyperOS 3.0), BAC app `net.bac.sbe.android`, login screen only (no login
performed, nothing tapped).

- **BAC is readable — go.** The app is a single WebView. Plain
  `adb shell uiautomator dump` sees an empty WebView (6 nodes). Through
  `uiautomator2` the WebView content is fully exposed (102 nodes) with
  stable resource-ids: `txtUsername` (EditText), `txtPassword`
  (EditText, `password=true`), `chkBacToken`, `chbTouchId` (CheckBox),
  `btnLogin` ("Ingresar"). So `uiautomator2` is required, not optional.
- **No FLAG_SECURE** on the login screen: `screencap` returns the real
  screen, so comprobante screenshots should work (verify on the receipt
  screen itself).
- **No WebView CDP socket** for BAC (debugging disabled), so the
  accessibility tree is the only structured path.
- **Speed (USB):** `uiautomator2` connect ≈1.1 s once (daemon keeps it);
  dumps 186–367 ms, getting faster when warm. Plain `uiautomator dump`
  ≈3.3 s. `adb shell` round-trip ≈64 ms both over USB and over wireless.
- **The dump includes the status bar and notification icons.** The
  Android target must filter to the foreground app's package
  (plus its system dialogs, such as the IME/permission prompts).
- **macOS blocks the `adb` binary from the LAN.** `adb connect
  192.168.1.215:<port>` fails with "No route to host" while `nc` to the same
  port succeeds (macOS Local Network privacy). Workaround proven: a tiny
  localhost TCP relay (`127.0.0.1:5556 → tablet:5555`) run by a process that
  has LAN permission; `adb connect 127.0.0.1:5556` then works. The
  connection keeper in `jevd` should own this relay (and re-resolve the
  tablet's IP via mDNS `_adb-tls-connect._tcp` / `Android.local`). Granting
  `adb` Local Network permission in System Settings is the alternative.
- Tablet set to classic `adb tcpip 5555` during the spike (resets on
  reboot; the keeper should re-issue it over USB when available, or use
  wireless-debugging pairing).
- Tablet LAN ping 110–180 ms (likely Wi-Fi power saving); worth checking
  the Redmi's Wi-Fi power settings for speed.

## My calls (Claude's, open to change)

- Separate long-lived `jevd` daemon + thin MCP front end.
- `uiautomator2` as the Android screen-read/act layer.
- The match check is deterministic code over screen text; no money params
  ⇒ always pause.
- Secret fields pause for input and bypass all models.
- Results never include images; screenshots are file paths.
- Session idle TTL 15 min; adb reconnect window 30 s.

## Left to the builder

- Module layout, IPC protocol details, flow file format.
- Exact label pattern lists (start with the ones above).
- Whether Chrome-on-Android needs the CDP-over-`adb forward` fallback.
- How to package/launch `jevd` (autostart from `server.py` is required;
  launchd optional).
