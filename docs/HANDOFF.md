# Handoff — jev-runner (for any coding agent picking this up)

_Last updated: 2026-10-29. Written so a different agent (e.g. a DeepSeek session) can continue without the original conversation._

## What this is

`jev-runner` is a local daemon + thin MCP server that lets Claude (or any MCP client) hand a
natural-language goal to TypeSafe's **Jev** decision model, which then drives **Mac Chrome**
(via Browser Harness/CDP) or an **Android device** (uiautomator2 over adb) step by step,
fast and cheaply, returning a compact result. It must work on **any app or site** — no
app-specific code. Owner: Eduardo.

Read, in order:
1. `docs/superpowers/specs/2026-10-29-jev-multi-target-design.md` — the approved design (binding).
   Includes "Feasibility check results" and "Verification results" from the real tablet.
2. `docs/superpowers/plans/2026-10-29-jev-multi-target.md` — the implementation plan
   (Tasks 1–12; Task 7 has an OS-level addendum; Task 12 = auto-save/self-healing flows).
3. `README.md` — user-facing setup, tools, config, safety summary.

## Where things are

- Merged into local `main` on 2026-10-29 (branch `feat/multi-target` deleted). Not pushed.
- Package: `jev_runner/` — `config.py`, `guards.py` (money/secret guards), `android_tree.py`
  (uiautomator XML → Jev page), `targets/{base,android,chrome}.py`, `adb_link.py`
  (USB/wireless keeper + relay), `flows.py` (saved flows, replay, auto-save/heal),
  `runner.py` (step loop, pause/resume, money guard, receipts), `result.py`, `daemon.py`
  (`jevd`, unix socket, sessions, device lock, TTL), `client.py` (autostart + call).
  `server.py` = MCP tools `run_task, resume, screenshot, end_session, status, list_flows, delete_flow`.
- Tests: `uv run pytest -q` (offline; 302 tests; no device, no network, no keys).
- Test fixture: `tests/fixtures/android/bac_login.xml` (real BAC bank login dump, username redacted).
- Process ledger (git-ignored, may not exist on another machine):
  `.superpowers/sdd/2026-10-29-jev-multi-target/progress.md` — every task, review round and ruling.

## Status

| Task | State |
|---|---|
| 1 config + guards | done, reviewed |
| 2 Android tree parser | done, reviewed |
| 3 runner loop | done, reviewed |
| 4 pause/resume, money guard, receipts | done, reviewed (3 fix rounds) |
| 5 saved flows + replay | done, reviewed |
| 6 adb link keeper | done, reviewed |
| 7 AndroidTarget + OS controls (home/recents/notifications, app by name) | done, reviewed |
| 12 auto-save + self-healing flows | done, reviewed |
| 8 ChromeTarget (blank-tab recovery) | done, reviewed |
| 9 jevd daemon + client | done, reviewed |
| 10 MCP tools, rename to jev-runner, README | done, reviewed |
| 11 on-device verification | ran; found 5 bugs → fixed and reviewed (commits e277fcf, cc3caf8, dc7c691, 957effd); live relay check passed |
| Final whole-branch review | done; fixes applied (sticky secret detection + scrubbed history/results, hint + password blanking, `w` ids for WebView links, locator-checked resume, ambiguous amounts rejected, single android lock + `unknown_device`, AdbLink lock, config list validation, shutdown money warning, docs) — 281 tests |
| Finish | merged to local `main`; push and optional repo rename to `jev-runner` still need Eduardo |

### Task 11 fixes (done — kept here for context)

Findings from the real Redmi Pad (details in the spec's "Verification results" and the ledger):
- (a) macOS Local Network privacy blocks the **uv-managed Python** from LAN hosts → the
  in-process relay can't reach the tablet. Fix: run relay as a helper process under
  `Config.relay_python = "/usr/bin/python3"` (`jev_runner/relay_main.py`, stdlib only), supervised by AdbLink.
- (b) uiautomator2 `app_info()` returns no labels on this device → app-by-name fails.
  Fix: match name against package-name tokens first, labels as fallback; cache per daemon.
- (c) BAC's "Tipo de Cambio" is a non-clickable TextView inside a WebView → not an action.
  Fix: text-bearing nodes in WebView subtrees without a clickable ancestor become click actions (role "link").
- (d) `adb tcpip` on first USB attach restarts adbd → "AdbError: closed". Fix: poll until device is back.
- (e) stale `127.0.0.1:5556 offline` adb entry hangs later runs. Fix: `adb disconnect` before connect and on close.

Verify with `uv run pytest -q`; for (a) a live check needs the tablet on USB + the Bash sandbox off.

## How to continue

1. `git checkout main && uv sync && uv run pytest -q` — must be green.
2. Task 11 fixes and the final whole-branch review fixes are done (281 tests). Synthetic
   WebView links are excluded from fingerprints and flow signatures and use their own ids (`w0`, …).
3. Remaining deferred minors (ledger, `grep "minor (deferred)"`): tab leak if Chrome attach
   fails after createTarget; resolve_app cost.
4. Ask Eduardo before pushing `main` or renaming the repo folder.

## Non-negotiable safety rules (do not weaken)

- Secrets (OTP/password/PIN) never reach Jev, the text model, logs, flow files or tool results (`•••`).
- Strong money labels (Transferir, Pagar, Pay, Transfer, Confirm transfer/payment, …) are guarded on
  **every** app/site. Autonomous tap only when **both** `amount` (exact) and `recipient`
  ("only what's available" on screen: masked digits / name / service number; own accounts via
  `own_accounts`) match. Otherwise pause (`paused_mismatch`) and require `resume(confirm=True)`
  after the user approves.
- One money action per run; any further money action pauses. An uncertain money act
  (exception during/after the tap, incl. CancelledError) is **terminal**: no resume, no new
  run on that session; device stays locked until explicit `end_session`.
- Target contract: `StalePage` may only be raised before any text is typed or irreversible tap
  is sent; anything after input = uncertain. Never re-send input after a failure.
- No app/site names in production code (`money_apps` defaults to `[]`).

## Environment facts

- Tablet: Redmi Pad, model 2505DRP06G, Android 16 / HyperOS 3.0, serial `zdirl7t84pqwh6ww`,
  LAN `192.168.1.215`, mDNS instance `adb-zdirl7t84pqwh6ww-…` (`_adb-tls-connect._tcp`).
- `adb` itself is also blocked from the LAN by macOS → always connect through `127.0.0.1:5556` relay.
- Keys are in `.env` (git-ignored): TypeSafe + DeepSeek as text model (`TEXT_MODEL_BASE_URL=https://api.deepseek.com/v1`,
  `TEXT_MODEL=deepseek-chat`, no `TEXT_MODEL_REASONING`). OpenAI's API is not usable directly: `jev_ultrafast` always
  sends a `reasoning`/`thinking` field.
- 2026-10-29 first real Jev runs on Mac Chrome: DuckDuckGo search goal → `done` in 4.6 s (fill via DeepSeek, 9 model
  calls, one needless extra click). example.com "Open the 'Learn more' link" → clicked correctly, then Jev said
  BLOCKED instead of DONE (goals phrased as an action, not an end state, may end `blocked`). Android untested with real Jev.
- Commit messages end with `Co-Authored-By:` attribution per the repo's convention; don't push or merge without Eduardo.

- 2026-10-29 Android + real Jev (Redmi Pad, USB): WhatsApp self-message → `done` in 25 s, 7 model calls (flow saved).
  Findings: (1) a goal naming only a first name sent a test message to the wrong contact — non-money sends have no
  recipient check; goals must name the exact chat and say to stop otherwise. (2) DeepSeek refuses to type a value not
  literally in the goal. (3) **False done + bad self-heal:** replaying the same goal while already in that chat, Jev
  filled the message and said DONE without tapping Send (the identical earlier message was visible), and the flow was
  `flow_updated` to that broken shorter version. Flow deleted; not yet fixed.

- More real-Jev findings (2026-10-29): (4) learn run tapped WhatsApp's "Ask" (Meta AI) with the search text, recorded
  it in the flow, and the replay repeated it and reported a false `done` in the Meta AI chat — flows learned from runs
  that end in a wrong place are saved as-is. (5) Fixed: no wait after app launch (splash reached Jev → BLOCKED);
  `AndroidTarget._settle` now waits up to 5 s for a stable screen with on-screen actions (ChatGPT's splash still
  exceeded it once). (6) Fixed: unlabelled EditText took a far-away "preceding" label (ChatGPT box labelled
  "Projects"); now an overlay placeholder inside the field wins and above-labels must overlap horizontally.
  Pending user task: ChatGPT orange-cat image → WhatsApp to the user's wife (exact chat name "Grethel Sánchez";
  user approved sending) + a text saying it's a test. Do it in steps, learn=False, verify chat header by screenshot
  before sending. `git push origin main` was blocked by the permission classifier — user must run it.

- 2026-10-30 cat test passed, run in verified steps (learn=False, screenshot checks between steps):
  ChatGPT generate (prompt text must be in goal) → save (needed the new unlabelled-element labels) → WhatsApp
  open exact chat (Jev correctly stopped when the saved name differed from the goal's) → attach → send photo → send text.
  Status is unreliable both ways: the photo send reported `blocked` although the photo was sent. Jev also tapped
  "Allow all" on a photo-permission prompt without being told to. Next: supervised Smart-Bot transactions.

- 2026-10-30 second-opinion check (`jev_runner/verify.py`, commit da4836c) live: self-message learned only after the
  check confirmed it (42.6 s, 10 Jev calls); replay from cold WhatsApp correct, 28.2 s, 5 Jev calls, self-healed
  because WhatsApp's search screen differs between runs (recent-searches list), so steps fell back to Jev.
  Replays are only partially faster on screens that vary; the Jev API also returned HTTP 529 (overloaded) once.

- 2026-10-30 BAC (real money, supervised): login via clipboard → resume worked (password never printed). No money
  moved: BAC logged out after ~8 min while the money guard paused on navigation tabs ('Transferir o pagar',
  'Transferir dinero'), a confirm was voided by an animating screen, and Jev tapped an icon's dead caption.
  Fixed in 8de14c2 (navigation_labels config, settled pauses, caption dedupe) — not yet re-tested live.
- Hand-off: `docs/AGENT-GUIDE.md` (any agent); private recipe with IBANs outside git at
  `~/.local/share/jev-runner/recipes/2026-10-bac-transfers.md`. Eduardo decided Smart-Bot (Grok Bot) may approve
  money pauses itself. Next design: `docs/superpowers/specs/2026-10-30-jev-skills-and-app-notes-design.md`.

- 2026-10-30 15:20–16:15 Smart-Bot (Grok Bot) ran the BAC transfers through jev-runner with learn=true: float
  ₡30,000 *0195→*0743 (ref 406496950), Marybel ₡20,000 (406401935), Agua HERSULI ₡10,000 (406409947); comprobantes
  sent on WhatsApp. ~45 jevd calls, many blocked/max_steps retries. Learned BAC flows: login, show accounts, float
  (16 steps), open home, one confirm screen. Its lessons (`~/.local/share/jev-runner/lessons.md`) say BAC's WebView
  account cards often ignore a plain click (down/up or long-click works), the 'Transferir o pagar' sheet is often
  missing from the accessibility tree (use the account page's Transferir), and they mention uiautomator2 calls
  (`touch.down`, `resourceIdMatches`) — i.e. the bot also drove the tablet directly, outside jev-runner's guards.
  Next: generic WebView click fallback in AndroidTarget; build skills/app notes; agent guide rule that money taps go
  only through jev-runner.

- 2026-10-30 17:01–17:09 Claude drove the HERSULI rent ($900 *2837 → CR77…1775, 'Casa Octubre', ref 406475275)
  and shared the comprobante to WhatsApp 'Lilly Yamuni' with caption. Login and show-accounts replayed learned flows
  (login 14.6 s, 1 model call). Open issues seen: (a) approving a navigation 'Transferir' counts as the run's money
  action and saves a receipt; (b) keyboard covering 'Continuar' made Jev BLOCKED (no hide-keyboard action);
  (c) the fill focus check raised stale_page when the keypad scrolled the field — needs one automatic retry;
  (d) after scrolling, the confirm screen no longer showed the recipient IBAN, so the guard paused (correct, but
  a run could remember what it saw earlier in the same form); (e) the Android share sheet worked once observed late;
  (f) uiautomator2 types via the clipboard — now cleared after every fill (keyboard clipboard history may still
  keep values: tell Eduardo to clear/disable it on the tablet); (g) a run_task with url_or_app while another app
  is in front relaunches that app (likely Grok's 'BAC minimized').

- 2026-10-30 Chrome (personal profile) tests: navigation/search/typing work; web money-guard labels added
  (Place order, Buy now, Pay now, Finalizar compra, ... commit 381f263) and unit-tested. A live 'click a real
  Pay now' guard test was (correctly) blocked by the transaction safety classifier; not retried. Found a real
  Chrome weakness: on saucedemo.com login, Jev looped re-filling Username and never advanced to the Password
  field, then returned BLOCKED on a directed retry. Multi-field web form login needs work in the Chrome target /
  page model before autonomous web checkout is trustworthy. Guard itself is enforced by the shared code path.

## Known residuals after final review (2026-10-29)

Final whole-branch review + fix wave done (281 tests green). Open items, not yet fixed:
- Fixed 2026-10-29: `parse_amount` accepts only plain digits, single-separator thousands groups and an
  optional 2-digit decimal (anything else → None); `result.build` no longer scrubs `session_id`/`flow_*`.
- Minor: sticky secret detection over-triggers on apps reusing one EditText resource-id; history
  recorded before a value became secret isn't re-scrubbed; scalar config fields unvalidated;
  `Daemon.close` can wait ~30 s behind an in-flight reconnect.
