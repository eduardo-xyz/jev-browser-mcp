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

- Branch: `feat/multi-target` (from `main`). Not merged, not pushed.
- Package: `jev_runner/` — `config.py`, `guards.py` (money/secret guards), `android_tree.py`
  (uiautomator XML → Jev page), `targets/{base,android,chrome}.py`, `adb_link.py`
  (USB/wireless keeper + relay), `flows.py` (saved flows, replay, auto-save/heal),
  `runner.py` (step loop, pause/resume, money guard, receipts), `result.py`, `daemon.py`
  (`jevd`, unix socket, sessions, device lock, TTL), `client.py` (autostart + call).
  `server.py` = MCP tools `run_task, resume, screenshot, end_session, status, list_flows, delete_flow`.
- Tests: `uv run pytest -q` (offline; ~200 tests; no device, no network, no keys).
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
| Final whole-branch review | **in progress** (main..957effd) |
| Finish (merge/PR decision, optional repo folder rename to `jev-runner`) | **not started — ask Eduardo** |

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

Check `git log` for commits after `d00063d` to see which of these landed. Verify with
`uv run pytest -q`; for (a) a live check needs the tablet on USB + the Bash sandbox off.

## How to continue

1. `git checkout feat/multi-target && uv sync && uv run pytest -q` — must be green.
2. Task 11 fixes are done (232 tests). Synthetic WebView links are excluded from fingerprints and flow signatures.
3. Run a whole-branch review of `main..feat/multi-target` against the spec. Deferred minor
   findings to triage are listed in the ledger (`grep "minor (deferred)"`); the notable ones:
   password EditText node text not blanked in page text; tab leak if Chrome attach fails after
   createTarget; resolve_app cost; status docstring overstates fields; README lacks `jevd.stderr`
   and a restart command (`pkill -f jev_runner.daemon`).
4. Ask Eduardo how to finish (merge to main / PR / keep branch) and whether to rename the repo folder.

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
- No `TYPESAFE_API_KEY` yet → real Jev decisions are untested end to end; say so when reporting.
- Commit messages end with `Co-Authored-By:` attribution per the repo's convention; don't push or merge without Eduardo.
