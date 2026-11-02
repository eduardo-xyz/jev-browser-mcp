# Which tool drives the browser: JEV Runner vs. Claude-in-Chrome

A one-pager for choosing, per task, between **JEV Runner** (goal → the Jev model drives Chrome
through jev-runner, with guards) and the **Claude-in-Chrome extension / DevTools** (Claude drives the
browser directly). They are **not** interchangeable: they have very different safety profiles. Split
work by *task type and risk*, never 50/50 by count.

## The rule in one line

**If it can move money, send a message, or change/delete a record → JEV Runner, always.
If it only reads, navigates, explores, or fills a non-persistent field → the extension is fine, and
often better.**

## Split by task type

| Task | Tool | Why |
|---|---|---|
| Bank transfer, payroll, any payment | **JEV Runner only** | Guards: amount+recipient must match the screen, one money action per run, secret masking, receipts. |
| Sending a message / forward (WhatsApp, etc.) | **JEV Runner only** | One-send-per-run guard; no double-sends. |
| Anything that saves, publishes, toggles live state, or deletes | **JEV Runner only** | It pauses on mismatch and records what happened; the extension has no such brakes. |
| Reading / listing content (incl. cross-origin iframes) | **Extension or JEV Runner** | Both can read; the extension can also read DOM/console/network and adapt. |
| Navigating, opening views, switching tabs, scrolling | **Extension (often better); JEV Runner OK** | The extension adapts when a UI is weird; JEV Runner is fine for simple navigation. |
| Debugging a page, inspecting state, gathering data | **Extension** | Direct DOM/JS/console/network access. |
| Filling a **non-persistent** field (search box, filter) | **Either** | No data is saved, so the missing guards don't matter. |

## Why the hard line on consequential actions

The extension (and raw DevTools) drive your **real Chrome profile directly with no seatbelts** — no
amount/recipient check, no secret masking, no receipts, no one-action limit, and a larger blast
radius (your actual tabs). JEV Runner exists precisely to put those brakes on consequential actions.
Using the extension for a transfer or a send bypasses every protection. Don't.

- **Never point the extension at the bank** (or any site where a stray click is costly). It needs
  site permissions and drives the live session.

## If you run both at once (recommended as an A-B)

Running **both tools on the same safe read/navigate tasks** is a good experiment: it shows where JEV
Runner still lags the extension, and it shakes out JEV Runner bugs the way live runs did during the
OOPIF work. Keep the consequential half strictly on JEV Runner.

- **Separate Chrome instances.** Don't point JEV Runner and the extension at the same Chrome — they
  fight over tabs/CDP. Give each its own Chrome, profile, and remote-debugging port, and launch a
  JEV Runner daemon bound to its own (see AGENT-GUIDE "Pointing at a specific Chrome profile":
  `BU_CDP_URL` + `BU_NAME` + `JEV_DATA_DIR`).
- Confirm which profile each is on before starting (open a known page and screenshot).

## JEV Runner gaps to watch (as of 2026-11-01)

Cross-origin iframe **read / click / scroll** are validated end to end (GHL). Still rough:

- **Fill inside an iframe** was dispatched but not yet confirmed to take effect live — verify with a
  `screenshot`/`read` after an iframe fill.
- **Navigate-only goals** ("open X") can report `blocked` even when they worked (no done-detector on
  the new screen) — **don't trust the status line alone** on view/navigate goals; confirm.
- **Non-semantic controls** (divs with no role/label) still can't be targeted by name.
- A **money button inside an iframe pauses for approval** (the amount check can't see iframe
  amounts) — that's the safe default, not a failure.

## Bottom line

Treat JEV Runner for consequential work as **"works with a human watching," not fire-and-forget**:
exact labels in goals, `screenshot`/`read` to confirm important steps, explicit approval for
anything that saves or moves money. Use the extension for reading and exploring, where it shines.
