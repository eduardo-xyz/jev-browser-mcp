# 2026-11-15: a bank app's Transferir tile paused as a money action

Answers to the incident report's engineering questions (Q1–Q8). The report was read-only; this
note is jev-runner's side. No account numbers, amounts or names are recorded here.

## What happened in the runner

1. **The tile.** On an account's detail screen the transfer entry is an icon tile whose name is
   the caption drawn under it: "Transferir". The review screen's commit button carries the same
   word. The money guard is label-based (`money_labels` in `config.py`: "Transferir", "Pagar",
   "Pay", …, guarded on every app), so the tile tap went through `_guard_money`. The run's
   intended amount and recipient were not on that screen (an account detail shows balances, not
   the transfer), so the check failed and the run paused `paused_mismatch` with no `reason`.
   Working as designed, and not distinguishable by label: a wrong guess at a commit is the costly
   one, so the pause stands.
2. **The operators could not proceed.** The guide allowed `resume(confirm=True)` only when the
   screenshot shows the exact intended transfer, which a launcher screen never does. Nothing in
   the result said "this is a launcher; confirming only opens the form". The owner opened it by
   hand.
3. **The source-account pencil (session c8a8ad03).** Two runs ended `jev_blocked` at step 0 with
   the model weighing BLOCKED just above CLICK; the daemon log kept counts only, not the labels on
   offer, so whether the pencil was offered (and under what name) is not in the record. Likely an
   icon with no text and no caption, offered as "Unlabelled image (WxH px)", which a goal saying
   "pencil" cannot name. The gave-up log line now records the first fifteen labels on screen, so
   the next such miss is answerable from the log.

## Changes (commit after 9b54fbb)

- The Android tree marks an action whose name came from the caption under an icon
  (`caption_label`). A money pause on such a control now carries `reason` `launcher_tile` and a
  hint: confirming opens the form; the commit button pauses again on its own
  (`money_already_attempted`), so the approval covers one tap.
- The guide's `paused_mismatch` row and money rules say when a `launcher_tile` pause may be
  confirmed, and that `navigation_labels` in jevd's `config.toml` is for labels that only ever
  open a menu ("Transferir o pagar"), never for "Transferir" itself.
- The daemon's "gave up" line for blocked, unverified and max_steps runs now includes the labels
  on offer (`sample`) besides counts, weights, what was tried and the verifier's reason.
- Earlier the same day (9b54fbb): a tap or scroll that changes nothing on a screen is not offered
  again on that screen; sixteen runs that day had re-tapped a selected tab three times.

## Q1–Q8

- **Q1** Classifier: `jev_runner/guards.py` (`is_money_action`, `check_money`), applied in
  `jev_runner/runner.py` `_dispatch` → `_guard_money`. Version on 15 Nov: the default daemon ran
  commit 21d0051 (started 10 Nov 10:15); it now runs the fix.
- **Q2** Signals: the control's label against `money_labels` (always) and
  `money_labels_contextual` (only with money params or on a configured money app), minus
  `navigation_labels` and `money_exclude_words`. The visible word "Transferir" is sufficient on
  any screen; the screen is then checked for the run's amount and recipient.
- **Q3** A pause on a screen without recipient or amount is the designed outcome, not a defect in
  the check. The defect was the missing explanation and the missing sanctioned path. Now fixed
  with `launcher_tile`.
- **Q4** Yes: the guide now states the open-form path (confirm a `launcher_tile` pause when the
  screenshot shows the launcher screen) and keeps `confirm=True` narrow elsewhere.
- **Q5** The alternate overlay's failure left no run record in the daemon log beyond the agent's
  result; the `runs` table keeps only final statuses, and paused runs are not final. The overlay
  item taps that day were "no progress" taps (three identical taps, screen unchanged); those are
  now dropped after the first.
- **Q6** Not provable from the record: the labels on offer were not logged. Same classifier is
  unlikely (a pencil carries no money word); element naming is the likely cause. The log now
  records the offered labels.
- **Q7** Per-run lines: `~/.local/share/jev-runner/jevd.log` (status, steps, model calls,
  timing, the gave-up line); final runs: `~/.local/share/jev-runner/memory.db` table `runs`;
  screenshots the agents asked for: `~/.local/share/jev-runner/screens/`. Raw `last_actions`
  exist only in the agent's result.
- **Q8** Out of the runner's hands: `screenshot` returns the current screen; a stale file is an
  operator-side mix-up. The runner's own verify reads the live screen, never a file.

## Follow-up, 16 Nov: ask once per run

Eduardo's decision after reading this: the pauses were acting as hard blockers. The money guard
now asks once per run. A caption-named tile, or any money label on a screen with no amount at
all, opens the form without asking; a commit whose screen matches the run's amount and recipient
taps on its own; a commit whose screen does not match pauses once; after that `resume(confirm=True)`
the rest of the run never asks again, including a second money tap and a screen that changed
since the screenshot (the control must still be there). The `launcher_tile` reason and the
`money_already_attempted` re-pause are gone. Deletes still pause every time.

## Same day, later: approval given once, for good

Eduardo: not once per run, once for ever by that bot; stop creating blockers. The money and
delete pauses are now off by default (`ask_before_money`, `ask_before_delete` in config.toml,
both false). A money or delete control is tapped like any other; receipts are still saved and a
money tap is still never retried. The ask path stays in the code behind those two switches.
