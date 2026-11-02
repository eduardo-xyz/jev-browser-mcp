# Handoff — GHL writes: kebab-menu targeting + destructive-click guard

_Written 2026-11-02 so another session can pick up two fixes without the originating conversation._
_Owner: Eduardo. Scope: generic (no app-specific code) — GHL is only the place the gap showed up._

## Why this exists

Three independent read-only shakedowns on 2026-11-02 (Turbo, Drivy, and the Soft15 group) all
reached the **same verdict**: jev-runner is a good **read** tool on GHL/DA Chrome — `read()`
reaches the cross-origin automation iframe that chrome-devtools-mcp can't, and confirmed live
values (e.g. custom value #9395 = "Yes") — but it is **not ready for GHL row-menu writes**.

Two concrete failures, both reproduced more than once, nothing written or deleted:

1. **Kebab / 3-dot row menus can't be targeted.** The per-row action menu is an **unlabeled icon
   button**. Jev either can't find it (runs hit `max_steps`) or picks the wrong element.
2. **A coordinate hint produced a mis-click next to Delete.** On one run a coordinate hint sent
   the click onto a **row checkbox adjacent to the Delete control**. Harmless this time (checkbox
   only), but row-select + Delete is exactly the sequence that destroys records. Logged as
   **lesson 20** in the experience memory.

These are the two fixes to assign. The OOPIF act path itself already works for *labeled* iframe
controls (click/fill/scroll validated end to end 2026-11-01 — see
`memory/oopif-act-path.md`); this is about making *icon* controls targetable and making
destructive clicks safe.

## Before you start

```bash
cd /Users/eduardo/Projects/Personal/jev-browser-mcp
git checkout main && git pull
/opt/homebrew/bin/uv run --env-file .env python -m pytest -q   # must be green (368 tests)
```

Read `docs/AGENT-GUIDE.md` (how the tool is used) and `memory/oopif-act-path.md` (how the iframe
act path was built). Non-negotiable safety rules are in `docs/HANDOFF.md` ("Non-negotiable safety
rules") — **do not weaken any guard**.

---

## Fix 1 — Semantic labels for unlabeled icon actions (the kebab menu)

**Problem.** Actions come from upstream's snapshot (`jev_ultrafast` `READ_STATE`), which labels an
element from its `innerText` / `aria-label`. A 3-dot icon button has neither, so it arrives with
`label: ""`. In `_merge_iframe_actions` the empty label is kept as-is, and the model has nothing to
select on — several blank icon buttons in a virtualized row list are indistinguishable.

**Where.** `jev_runner/targets/chrome.py`:
- `_merge_iframe_actions` (line ~272) merges iframe actions; labels are copied at line ~322
  (`"label": a.get("label", "")`).
- Pattern to copy: `_add_overlay_actions` (~357) and the `_READ_TEXT_JS` / `_PASSWORD_JS` evals —
  a small JS evaluated in the frame session that enriches nodes upstream left blank.

**Approach (generic, no GHL strings).** For actions whose `label` is empty, synthesize a label from,
in priority order: `aria-label` → `title` → `data-testid`/`data-tooltip` (humanized) → the control's
accessible name from an SVG `<title>` / `<use href>` icon id → **the nearest row/cell text** (so an
unlabeled menu button becomes e.g. `"menu — <row name>"`). Keep it ≤80 chars, mark synthesized
labels so they're debuggable under `JEV_DEBUG_IFRAME`. Apply the same enrichment to top-frame
actions (same class of bug exists outside iframes). Do **not** hard-code "kebab"/GHL selectors —
derive from generic DOM affordances only.

**Acceptance.**
- Unit: given a frame snapshot with a `label:""` icon button sitting in a row with text "Allen
  Brothers", the merged action gets a non-empty, row-disambiguated label. Add to
  `tests/` alongside the existing chrome/iframe tests.
- Live (Eduardo or a delegated read/act session, on DA Chrome with `JEV_DEBUG_IFRAME=1`): on the
  GHL workflows or custom-values list, the per-row menu appears as a distinct labeled action and a
  `run_task` to "open the row menu for <exact row name>" clicks it. Confirm with `screenshot`.

---

## Fix 2 — Destructive-action guard (Delete / Remove / Eliminar)

**Problem.** There is a money guard and a send guard, but **no guard on destructive controls**. A
mis-aimed click (here, from a coordinate hint) near Delete has nothing stopping it from selecting
rows and deleting them. The safety net must be symmetric with the money/send guards: a destructive
click **pauses for explicit human confirmation**, it is never auto-tapped, and never tap-retried.

**Where.**
- `jev_runner/config.py`: add a `destructive_labels` field next to `send_labels` (~line 65) and add
  it to `LIST_FIELDS` (~line 73). Suggested generic defaults:
  `["Delete", "Eliminar", "Borrar", "Remove", "Quitar", "Trash", "Move to trash", "Delete permanently", "Eliminar permanentemente"]`.
  (Not "Archive"/"Cancel" — those aren't destructive enough to always block; keep the list tight.)
- `jev_runner/guards.py`: add `is_destructive_action(action, cfg)` mirroring `is_send_action`
  (~line 63) — `kind == "click"`, not a `navigation_labels` match, label matches
  `destructive_labels`. Also ensure `may_retry_tap` (~line 48) returns `False` for a destructive
  label (today it already blocks checkboxes and money/send; add destructive to that set).
- `jev_runner/runner.py`: in `_dispatch` (~line 583), add a branch after the money/send guards:
  if `is_destructive_action(action, self.cfg)`, **pause** (reuse the `paused_mismatch` →
  `resume(confirm=True)` mechanism, or add a `paused_destructive` status — decide and note it) and
  require explicit confirmation before `_execute`. One destructive action per run, like money.

**Note on the checkbox case.** The guard above stops the *Delete press*, which is the record-losing
step — that is the fix to ship. Preventing the *adjacent checkbox tick itself* is a targeting
problem, not a guard problem, and is addressed by Fix 1 (a correctly-labeled menu means Jev stops
aiming by coordinate). Don't try to infer "checkbox is near a Delete button" geometrically; it's
brittle. Guard the consequential control, improve the targeting.

**Acceptance.**
- Unit: `is_destructive_action` true for "Delete"/"Eliminar", false for a `navigation_labels`
  entry and for non-click kinds; `may_retry_tap` false for a destructive label; a runner test that
  a Delete click pauses instead of executing and resumes only with `confirm=True`. Mirror the
  existing money/send guard tests.
- Live: a `run_task` that would click Delete pauses and returns the pause status; `resume(confirm=True)`
  is required to proceed. (Do **not** run a real delete in testing — assert the pause and stop.)

---

## Repro context

- Target: DA Chrome (separate jevd, `BU_CDP_URL=http://127.0.0.1:9333`), GHL white-labelled
  automation UI, iframe `client-app-automation-workflows.leadconnectorhq.com`.
- Exercise: the **#9395** custom-value edit (the one the Soft15 group had to finish with
  chrome-devtools after the kebab menu blocked). Row menus live in the workflows / custom-values
  lists.
- `lesson 20` in `~/.local/share/jev-runner/lessons.md` records the kebab-block + checkbox mis-tap.

## Constraints (do not break)

- **No app/site-specific code.** GHL names stay out of `jev_runner/`. Both fixes are generic.
- **Don't weaken money/secret/send guards.** Fix 2 *adds* a guard; it must not relax the others.
- **PII rule:** every Jev step screenshots to TypeSafe's model — never point a run at pages with
  real customer PII (Contacts/Opportunities). Validate on workflows/custom-values lists only.
- **Through jev-runner only** for any consequential tap; no raw adb/CDP clicks for writes.
- Tests: `/opt/homebrew/bin/uv run --env-file .env python -m pytest`. Keep the suite green.
- Commit messages end with: `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
  `git push origin main` must be its own separate command (the compound add+commit+push is blocked).
- Ask Eduardo before pushing `main`.

## Suggested order

Fix 1 first (it removes the root cause of the mis-click by making the menu targetable), then Fix 2
(the safety net for everything else). They're independent and can be two separate commits / two
separate task assignments.
