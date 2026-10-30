# jev-runner: reusable skills, app notes and agent hand-off — design

_2026-10-30. Status: proposed (not built). Builds on `2026-10-29-jev-multi-target-design.md`._

## Why

Real runs on 2026-10-29/30 (WhatsApp, ChatGPT, BAC) showed:

- A saved flow is one long recording of a whole goal. One screen that differs between runs
  (WhatsApp's recent-searches list) sends every later step back to Jev, so replays were only
  30–50% cheaper, and a single drift relearns the whole flow.
- Knowledge that would have saved minutes was learned but thrown away: "in this app
  'Transferir dinero' only opens a menu", "this bank logs out after ~8 minutes",
  "search the chat by full name, never tap Ask".
- Eduardo wants jev-runner to be a product another agent (Grok Bot / Smart-Bot) drives on its
  own, including approving transfers.

## Goals

1. **Skills**: small reusable flows, one per screen-level task ("open chat X", "log in",
   "fill transfer form"), composed into goals. A drifted skill heals alone.
2. **App notes**: per-app facts stored as data (never code), applied automatically.
3. **Agent hand-off**: any agent on the Mac (or a cloud agent through it) can use jev-runner
   with one written guide; nothing depends on this Claude session.

## Non-goals

- No vector database now. Lookup stays exact (target + app + skill name/goal key). Fuzzy
  matching may later *suggest* a skill, never auto-replay one that can move money.
- No app-specific production code (unchanged rule).

## Design

### Skills (replaces "one flow per goal")

- A skill = today's `Flow` with a short name, an app, a start-screen signature and an end-screen
  signature (what "done" looks like, from the verified end screen).
- `run_task(goal, skills=[...])` or `run_skill(name, params)`: the runner replays skill by skill;
  each skill boundary is a checkpoint verified by the second-opinion check (`verify.py`).
- Learning: a verified run is cut into skills at screen changes that the agent names, or
  automatically at app/activity changes. Healing touches only the skill that drifted.
- Storage: `~/.local/share/jev-runner/skills/<app>/<name>.json` (plain JSON, secrets never stored,
  same scrubbing as flows). Old flows keep working.

### App notes

- File per app: `~/.local/share/jev-runner/apps/<package-or-host>.toml`, merged into the run:
  - `navigation_labels` (exists globally today, commit 8de14c2; moves here per app)
  - `session_timeout_s` — runner warns/re-logs in before it lapses
  - `never_tap` — labels to hide from Jev (e.g. "Ask", "Meta AI")
  - `hints` — short lines appended to the goal for Jev and the text model
- Who writes notes: the human, or an agent through a `add_app_note` tool. Notes that weaken a
  guard (`navigation_labels`) need `approved_by` and are listed by `status` so they stay visible.

### Agent hand-off

- Local agents (Grok Bot on the Mac): stdio MCP, exactly like Claude Code today
  (`docs/AGENT-GUIDE.md`).
- Cloud agents: a later, separate front door (authenticated HTTP on localhost behind a tunnel);
  not built until needed.
- **Approval (Eduardo's decision, 2026-10-30): Smart-Bot may approve money pauses itself**
  (`resume(confirm=True)`). Consequence: jev-runner's code guard (amount + recipient must match
  the screen) is then the only automatic protection; a bot error in `params` is not caught.
  Mitigations kept: one money action per run, exact-match guard, receipts, `unverified` status.

## Open questions (builder's call unless marked)

- How the agent names skill boundaries (explicit `checkpoint` hints vs automatic cuts).
- Whether app notes may be learned automatically (proposal: only `never_tap` and
  `session_timeout_s`; guard-weakening notes always need approval).
