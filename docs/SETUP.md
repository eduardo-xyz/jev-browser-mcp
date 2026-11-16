# jev-runner — setup and runner reference

For whoever installs or binds jevd. Bots read `AGENT-GUIDE.md` instead.

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

## How the runner reads a screen (reference)

What follows is the accumulated detail of what the runner does on its own. Bots do not need it; it is here for whoever changes the runner or writes unusual goals.

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
so rows below the fold are reachable without a date picker or a search. Jev and the verifier see the text
in the viewport, and the verifier judges the final screen: a list that fits three tall rows per screen cannot
verify "read the first 5" (seen on a ticket list: 30 rows a page, three in view, `unverified`). Ask for what
the screen shows and page through with `read()` after each scroll, or name the row you want. A flow or diagram canvas (a workflow
editor's node graph) gets `Pan the <canvas> down/up/left/right` actions: its nodes sit beyond the visible pane
in both directions, and every titled node beyond the pane is offered as `Bring <title> into view`, which pans
to it in one step, measuring as it goes (blind pans overshoot). Quote the title exactly as the card shows it
(a `jev_blocked` detail lists them under `canvas_nodes`); a node's settings text ("4 hours") is not on its card:
"bring the node 'WAIT' into view, then click it". A title quoted in the goal that matches an offer exactly
(case and spacing aside) is brought in by the runner itself, without asking Jev, so a look-alike node
("Go To 4 Hour Wait" next to "WAIT 4 hours") cannot win; quote the full title. The same holds for any
control: a label quoted after a click verb ("click the 'Unit: minutes' dropdown and choose 'days'") is clicked
by the runner itself, once, when exactly one control on screen carries it; "do not click 'Save'" is honoured. Do not ask for "Fit to Screen" or a
zoom-out on such a canvas: it shrinks node titles below what Jev can read (3 px) and puts hundreds of
unlabelled controls on screen, more than the model takes. The text of a cross-origin
iframe is part of what Jev and the verifier read, not only of `read()`. Dropdown triggers built as plain focusable boxes (a ticket's property fields: `Status: Pending`) are offered as
clicks named by their field. Same-named controls (one `Domain Options` per card, one `Open` per row) are told apart by their row:
`Domain Options — <the card's first line>`, so name the row or card in the goal. An icon-only row control with no
name of its own is named by the words of its element id and its row (`user row edit — <person>`,
`user row delete — <person>` on Settings > Users): quote that label after the click verb to open the row's panel.
A control beyond the visible box of a panel that scrolls inside the page (a permission group header deep
in a settings drawer, a row far down a list) is offered as `Bring <label> into view` only when the goal
names it, so quote the label as the app shows it (`click the 'Dashboard 9/9' group header`): the runner
wheels the panel until it is in view, then the quoted-click rule clicks it. Controls covered at their
centre by something else (the panel's own `Delete` under its confirm dialog) are not offered, so `click
'Delete'` on a confirm dialog hits the dialog's button.
`read()` ends with a `States:` line naming every visible switch, checkbox and radio with `=on`, `=off` or
`=mixed` (named by its row), so a partial permission group can be recorded before anything is toggled.
A toggle the app has greyed out reads `=on (disabled)`: it is never offered, and a goal that says to click it
ends `blocked` with reason `control_disabled` and `detail.control` (no model call). Change whatever governs it
first, or report what the app allows; retrying the same click gets the same answer.
A switch, checkbox or radio with no name of its own (a component's stock aria-label, or only an id) is offered
as `switch — <row>` / `checkbox — <row>` with the same row name the States line uses, so `click 'switch —
Dashboard'` is unambiguous next to the `Dashboard` nav item and the `Dashboard 9/9` header.
A value the goal spells out for a field (`select all and type 3`, `enter 'Follow-up call' in the Name field`, `search for 'Round Robin'`) is
typed by the runner itself, no text model involved, as long as it is the only value left to type. Several values
in one goal each find their field when the goal names it: `type '500' into the Monto field. Type 'Consulta' into
the 'Descripción' box` (`type exactly '…'` counts too); values with no field named go to the text model, which must
work out which is which. Secrets never come from the goal: a password field takes the Keychain entry or pauses.
A goal that says to click a label `once` ends `done` after that click, even if the model then answers BLOCKED,
and that `done` is **verified by construction** (the verifier is not asked to imagine an end state); `read`
afterwards to confirm the state. On Android the **on-screen keyboard is the runner's own step**: `hide the
keyboard` in the goal dismisses it without the model, and a quoted control to click that is not on screen while
the keyboard is up gets the keyboard dismissed first (so `Hide the keyboard, then tap 'Continuar'` is one run).
Same-titled workflow cards are told apart by their badge (the contact count on the card): off screen they are
offered as `Bring WAIT 24 hour (change this if you need) — 29 into view`, and on screen the card reads
`... — 29` too, so quote the title with its badge for both the bring and the click (a short title in the click
still resolves to the full name the goal quoted for the bring). Two cards with the same title and the same badge
are numbered in canvas order, top to bottom: `... — 20` and `... — 20 (2)`, on screen and off.
A control the goal rules out (`Do not click Delete or Publish`, up to the next comma or period) is never shown
to the model. Once every click the goal quotes has happened, a model act on a control the goal does not name
ends the run `done` with reason `named_clicks_done` and `detail.refused`; if that further step was wanted, name
it in the goal ("then click 'Next'").
`press Escape` in a goal is done by the runner itself (one key press, no model): the way to dismiss a modal,
dropdown or popover that offers no close control you can name.
A goal that says to click a quoted label `once` is honoured: after the runner's own click, a repeat of that
label is refused and the run ends, so a toggle is never flipped back (give such a goal `max_steps` 3 or so).
A click is refused as stale only
when its own control or the document changed, not when the page scrolled or another field ticked.
A page whose observe comes back with empty `text` after it has settled really
shows nothing in the viewport (`why: blank_page`): check the URL in a browser before blaming the tool. Known
GHL routes that render blank on every location: `/settings/team` (the team page is `/settings/users`) and the
plural `/automation/workflows/<id>` (the editor is `/automation/workflow/<id>`).

