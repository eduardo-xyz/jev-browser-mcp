# Jev Multi-Target (Mac Chrome + Android) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single `browse_web` tool with a long-lived local daemon
(`jevd`) that runs TypeSafe Jev's decision loop on Mac Chrome and Android
(uiautomator2 over adb), with sticky sessions, pause/resume for secrets and
money confirmations, and saved-flow replay, exposed to Claude through a thin
MCP server.

**Architecture:** `server.py` (MCP, stdio) forwards each tool call as one
JSON line over a unix socket to `jevd` (autostarted). `jevd` owns sessions;
each session = one `Target` (Chrome or Android) + one `Runner` that calls
`jev_ultrafast.model.choose` / `field_text` per step, applies guards, and
records/replays flows.

**Tech Stack:** Python ≥3.12, uv, `mcp[cli]` (`MCPServer`), `jev-ultrafast`
(git dep), `uiautomator2`, `zeroconf`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-29-jev-multi-target-design.md`

## Global Constraints

- Python ≥3.12; run everything via `uv run`. Add deps with `uv add`.
- Do not fork/vendor `jev_ultrafast`; import `jev_ultrafast.model.choose`,
  `field_text`, `field_context`, and `jev_ultrafast.browser.Browser`.
- Jev page shape (what `choose` consumes): `{"url", "title", "text",
  "actions": [...]}`; each element action has `id`, `kind` in
  `click|fill|select`, `node`, `label`, `role`, optional `value`,
  `checked`, `selected`, `expanded`; any other `kind` is a control action
  (`wait`, `scroll`, `back`) shown to Jev by its `label` and chosen by
  `id.upper()`.
- Required env vars: `TYPESAFE_API_KEY`, `TEXT_MODEL_API_KEY`. Missing ⇒
  `run_task` returns an error before any device/browser work.
- Secrets (OTP/password/PIN values) never reach Jev, the text model, logs,
  flow files, or tool results (shown as `•••`).
- Tool results are compact JSON (target ≤ ~2 KB); never image bytes —
  screenshots are file paths.
- Defaults: session idle TTL 15 min; adb reconnect window 30 s;
  `max_steps` 40; last_actions ≤ 5.
- Data dir: `$JEV_DATA_DIR` or `~/.local/share/jev-mcp/` containing
  `jevd.sock`, `config.toml`, `flows/`, `receipts/`, `jevd.log`.
- Tests are offline: no network, no device, no API keys. Device/key paths
  are verified only in the final manual task.

## Plan-level decisions (beyond the spec)

- Package `jev_mcp/` holds all new code; `summary.py` and `browse_web` are
  removed (superseded by `run_task`).
- `resume(..., confirm=True)` is how Claude executes a money action after
  `paused_mismatch` once Eduardo approves; it re-checks the screen is
  unchanged first.
- Money-label matching is **exact after normalization** (casefold, strip
  accents/punctuation, collapse spaces), so "Enviar código" ≠ "Enviar".
- Android `act` skips the pre-act freshness re-dump for speed **except**
  for money actions and secret fills, which always verify first.
- One open session per Android device; a second `run_task` on a busy device
  returns `error: device_busy` with the holding `session_id`.
- Flow step screen match: same `where` and Jaccard(label sets) ≥ 0.8.
- Web password inputs are invisible to Jev (upstream `snapshot.js` skips
  them); a web login that needs one ends `blocked`. Android password fields
  are visible and go through the secret guard.

## Review Focus

1. Recipient shown masked (`****1234`) while params give a full IBAN — must
   match on visible digits; a different last-4 must pause.
2. An amount formatted `₡150.000,00` vs param `150000` — must match; `₡15.000,00` must not.
3. The OTP typed via `resume(input=...)` appearing in the next screen's text
   or field value — must be scrubbed before `choose` sees it.
4. Wireless link drop mid-run — run waits up to 30 s, reconnects, and the
   session survives (`error: device_disconnected` keeps the session).
5. BAC splash/loading screen with an empty WebView — must WAIT and retry,
   not immediately report `screen_unreadable`.

(Each is pinned by a named test in Tasks 1, 3, 5 and 6.)

## File Structure

```
jev_mcp/
  __init__.py
  config.py          # data dir, config.toml load, defaults
  guards.py          # secret detection, money guard, amount/recipient matching, scrub
  android_tree.py    # uiautomator XML -> Jev page dict (pure)
  targets/
    __init__.py
    base.py          # Target protocol, StalePage, TargetBlocked
    android.py       # AndroidTarget (uiautomator2)
    chrome.py        # ChromeTarget (Browser Harness, load retry)
  adb_link.py        # TcpRelay, mDNS resolve, ensure_connected
  flows.py           # Flow model, FlowStore, recording, step matching
  result.py          # compact result builder
  runner.py          # Runner: step loop, pause/resume, replay, receipts
  daemon.py          # jevd: sessions, socket server, TTL reaper
  client.py          # socket client + autostart
server.py            # MCP tools -> client
tests/
  fixtures/android/bac_login.xml   # real uiautomator2 dump (username redacted), already committed
  fakes.py           # FakeTarget, scripted choose
  test_guards.py  test_android_tree.py  test_runner.py  test_runner_pause.py
  test_flows.py  test_adb_link.py  test_android_target.py  test_chrome_target.py
  test_daemon.py  test_server.py
```

---

### Task 1: Package scaffold, config, guards

**Files:**
- Create: `jev_mcp/__init__.py`, `jev_mcp/config.py`, `jev_mcp/guards.py`
- Modify: `pyproject.toml` (deps; pytest `pythonpath` already `["."]`)
- Test: `tests/test_guards.py`

**Interfaces:**
- Produces:
  - `config.data_dir() -> Path` (creates it); `config.load() -> Config`
    dataclass with fields `android_serial: str | None`,
    `android_mdns: str | None`, `android_ip: str | None`,
    `adb_tcp_port: int = 5555`, `relay_port: int = 5556`,
    `money_apps: list[str]` (package names or hostnames),
    `money_labels: list[str]`, `secret_patterns: list[str]`,
    `session_ttl_s: int = 900`, `reconnect_window_s: int = 30`,
    `data_dir: Path` (default `data_dir()`).
    Reads `data_dir()/config.toml` with `tomllib`; missing file ⇒ defaults.
  - `guards.normalize(s: str) -> str` — casefold, strip accents (NFKD),
    drop punctuation, collapse whitespace.
  - `guards.is_secret_field(action: dict, patterns: list[str]) -> bool` —
    only `kind == "fill"`; true if `action.get("password")` or label matches
    any pattern (word match on normalized label).
  - `guards.is_money_action(action: dict, page: dict, money_params: dict,
    cfg: Config) -> bool` — `kind == "click"` and guarded context
    (`money_params` non-empty, or `page["url"]` package/host in
    `cfg.money_apps`) and `normalize(label)` equals a normalized entry of
    `cfg.money_labels`.
  - `guards.parse_amount(s: str | int | float) -> Decimal | None`
  - `guards.check_money(text: str, money_params: dict) -> MoneyCheck`
    (`MoneyCheck(ok: bool, expected: dict, seen: dict)`); keys: `amount`
    (exact Decimal equality with any amount in text), `recipient` (`str` or
    `list[str]`; any form matches), others (normalized substring).
  - `guards.scrub(page: dict, secrets: list[str]) -> dict` — returns copy
    with each secret replaced by `•••` in `text`, every action's `value`
    and `label`.
- Defaults: `money_labels = ["Transferir", "Pagar", "Confirmar",
  "Confirmar pago", "Enviar", "Pay", "Send", "Transfer", "Confirm"]`;
  `secret_patterns = ["codigo", "code", "otp", "token", "pin", "clave",
  "contrasena", "password", "verification", "verificacion"]`;
  `money_apps = ["net.bac.sbe.android"]`.

- [ ] **Step 1: Add deps**

Run: `uv add uiautomator2 zeroconf`
Expected: `pyproject.toml` lists both; `uv run python -c "import uiautomator2, zeroconf"` exits 0.

- [ ] **Step 2: Write failing tests** in `tests/test_guards.py`:

```python
def test_amount_formats_match():
    for text in ["Monto: ₡150.000,00", "CRC 150,000.00", "₡150 000", "150000"]:
        assert check_money(text, {"amount": 150000}).ok, text

def test_amount_off_by_factor_does_not_match():
    assert not check_money("Monto: ₡15.000,00", {"amount": 150000}).ok

def test_masked_recipient_matches_visible_digits():
    params = {"amount": "150000", "recipient": ["Juan Perez", "CR05015202001026284066"]}
    assert check_money("Destino ****4066 ₡150.000,00", params).ok

def test_masked_recipient_with_other_digits_pauses():
    params = {"amount": "150000", "recipient": "CR05015202001026284066"}
    result = check_money("Destino ****1234 ₡150.000,00", params)
    assert not result.ok and "recipient" in result.expected

def test_recipient_name_accent_and_case_insensitive():
    assert check_money("Para: JUAN PÉREZ  ₡150.000", {"amount": 150000, "recipient": "juan perez"}).ok

def test_money_label_exact_after_normalization(cfg):
    page = {"url": "net.bac.sbe.android/x", "text": ""}
    click = lambda label: {"kind": "click", "label": label}
    assert is_money_action(click("TRANSFERIR"), page, {}, cfg)
    assert not is_money_action(click("Enviar código"), page, {}, cfg)
    assert not is_money_action(click("Transferir"), {"url": "com.gohighlevel/x", "text": ""}, {}, cfg)
    assert is_money_action(click("Confirm"), {"url": "example.com/pay", "text": ""}, {"amount": 5}, cfg)

def test_secret_field_detection(cfg):
    assert is_secret_field({"kind": "fill", "label": "Código BAC"}, cfg.secret_patterns)
    assert is_secret_field({"kind": "fill", "label": "x", "password": True}, cfg.secret_patterns)
    assert not is_secret_field({"kind": "click", "label": "Ingresar token"}, cfg.secret_patterns)
    assert not is_secret_field({"kind": "fill", "label": "Usuario"}, cfg.secret_patterns)

def test_scrub_removes_secret_everywhere():
    page = {"text": "Código 482913 ok", "actions": [{"label": "482913", "value": "482913"}]}
    out = scrub(page, ["482913"])
    assert "482913" not in str(out) and page["text"] == "Código 482913 ok"
```

(`cfg` fixture = `config.Config()` defaults, in `tests/conftest.py`.)

- [ ] **Step 3: Run** `uv run pytest tests/test_guards.py -v` — Expected: FAIL (import errors).

- [ ] **Step 4: Implement** `config.py` and `guards.py` per Interfaces.
  `parse_amount`: strip everything except digits, `.`, `,`; if both
  separators present the last one is the decimal point; if only one kind
  and it is followed by exactly 2 trailing digits once, it is decimal;
  otherwise separators are thousands. Spaces between digit groups are
  thousands. Amount check scans all number-like tokens in `text`
  (`[\d][\d.,\s]*\d|\d`). Masked recipient: regex
  `[*•xX]{2,}\s?(\d{3,6})`; a recipient form matches if its digits end with
  the captured digits.

- [ ] **Step 5: Run** `uv run pytest tests/test_guards.py -v` — Expected: PASS.

- [ ] **Step 6: Commit** `git add jev_mcp tests/test_guards.py tests/conftest.py pyproject.toml uv.lock && git commit -m "Add config and money/secret guards"`

---

### Task 2: Android hierarchy → Jev page

**Files:**
- Create: `jev_mcp/android_tree.py`
- Test: `tests/test_android_tree.py` (uses `tests/fixtures/android/bac_login.xml`)

**Interfaces:**
- Produces: `parse_hierarchy(xml: str, package: str, activity: str) ->
  dict` returning `{"url": f"{package}/{activity}", "title": package,
  "text": str, "actions": list[dict], "fingerprint": str,
  "readable": bool}`.
  Element actions: `{"id": f"n{i}", "node": i, "kind": "click"|"fill",
  "label", "role", "value", "resource_id", "bounds": [l, t, r, b],
  "password": bool, "checked"?: "true"|"false"}`.
  Controls always appended: `{"id": "back", "kind": "back", "label": "Go
  back"}`, `{"id": "wait", "kind": "wait", "label": "Wait for the screen
  to update"}`; plus `scroll_down`/`scroll_up` (`kind: "scroll"`,
  `"delta": 1 | -1`) when any included node has `scrollable="true"`.
- Rules:
  - Include nodes whose `package` is the foreground `package` or in
    `SYSTEM_DIALOG_PACKAGES = {"android", "com.android.permissioncontroller",
    "com.google.android.permissioncontroller"}`. Excludes status bar
    (`com.android.systemui`) and IMEs.
  - Actionable = `enabled="true"`, non-zero bounds, and (`clickable` or
    `long-clickable` or `checkable` or class ends with `EditText`).
    EditText ⇒ `fill`, else `click`.
  - Strip Unicode private-use chars (U+E000–U+F8FF) and NBSP from all text.
  - Label = own `text` ‖ `content-desc` ‖ `hint` ‖ concatenated descendant
    text (for containers) ‖ nearest preceding included text node within
    250 px above (for unlabeled fields) ‖ resource-id tail. If the result
    came from a preceding text node, append ` · <resource-id tail>` when
    an id exists.
  - Role from class tail: Button/ImageButton→button, EditText→textbox,
    CheckBox→checkbox, Switch→switch, RadioButton→radio, else button.
  - `value` = node text for EditText (empty when `password="true"`).
  - `text` = deduped visible text/desc of included nodes joined by `\n`,
    capped at 6000 chars.
  - `fingerprint` = sha256 of `url` + ordered `(label, role, bounds)`.
  - `readable` = False when `text` is empty and no element action has a
    non-empty label (an unlabeled WebView during BAC's splash counts as
    unreadable, so the runner waits).

- [ ] **Step 1: Write failing tests:**

```python
def page():
    xml = Path("tests/fixtures/android/bac_login.xml").read_text()
    return parse_hierarchy(xml, "net.bac.sbe.android", "Main")

def by_rid(rid):
    return next(a for a in page()["actions"] if a.get("resource_id", "").endswith(rid))

def test_bac_login_fields_and_button():
    assert by_rid("txtUsername")["kind"] == "fill"
    assert by_rid("txtUsername")["value"] == "testuser01"
    pw = by_rid("txtPassword")
    assert pw["kind"] == "fill" and pw["password"] and pw["value"] == ""
    assert pw["label"].startswith("Contraseña")
    assert by_rid("btnLogin")["label"] == "Ingresar"
    assert by_rid("chkBacToken")["role"] == "checkbox"

def test_status_bar_and_glyphs_excluded():
    p = page()
    assert "notification" not in p["text"] and "Battery" not in p["text"]
    assert all("" not in a["label"] for a in p["actions"])
    assert by_rid("btnClearUsername")["label"] == "btnClearUsername"

def test_controls_and_fingerprint_stable():
    a, b = page(), page()
    assert a["fingerprint"] == b["fingerprint"]
    assert {"back", "wait"} <= {x["id"] for x in a["actions"]}

def test_empty_webview_is_unreadable():
    xml = '<hierarchy><node package="net.bac.sbe.android" class="android.webkit.WebView" text="" content-desc="" clickable="true" enabled="true" bounds="[0,70][1600,2524]"/></hierarchy>'
    assert parse_hierarchy(xml, "net.bac.sbe.android", "Main")["readable"] is False  # splash: unlabeled WebView only
    assert parse_hierarchy("<hierarchy/>", "net.bac.sbe.android", "Main")["readable"] is False
```

- [ ] **Step 2: Run** `uv run pytest tests/test_android_tree.py -v` — FAIL.
- [ ] **Step 3: Implement** `parse_hierarchy` with `xml.etree.ElementTree`, bounds parsed from `[l,t][r,b]`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `"Add Android hierarchy to Jev page mapping"`.

---

### Task 3: Runner core loop and result builder

**Files:**
- Create: `jev_mcp/targets/__init__.py`, `jev_mcp/targets/base.py`, `jev_mcp/runner.py`, `jev_mcp/result.py`, `tests/fakes.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Produces (`targets/base.py`):
  - `class StalePage(Exception)`, `class TargetBlocked(Exception)` (`.reason: str`), `class DeviceDisconnected(Exception)`.
  - `class Target(Protocol)`: `kind: str` (`"chrome"|"android"`);
    `observe() -> dict`; `fresh(page: dict) -> bool`;
    `act(action: dict, page: dict, text: str | None = None, verify: bool = False) -> None`;
    `screenshot(path: Path) -> Path`; `close() -> None`.
- Produces (`runner.py`):
  - `class Runner` constructed `Runner(session_id: str, target: Target,
    goal: str, params: dict | None, cfg: Config, *, max_steps: int = 40,
    money_params: list[str] | None = None,
    flow: Flow | None = None, choose=model.choose, field_text=model.field_text)`.
  - `Runner.run() -> dict` — loops ticks until a terminal or paused status;
    returns `result.build(self)`.
  - Attributes used by later tasks: `status: str`, `reason: str | None`,
    `history: list[dict]`, `page: dict`, `model_calls: int`,
    `elapsed_ms: int`, `secrets: list[str]`, `pending: dict | None`,
    `last_used: float`, `executed_money: bool`, `receipt: dict | None`.
  - Goal text sent to Jev: `goal` plus, when `params`, a line
    `"Values: " + ", ".join(f"{k}={v}")` (recipient lists joined by " / ").
  - History entry keys: `action` (label), `kind`, `text` (`"•••"` for
    secrets), `page_changed`, `where`, `signature`, `locator`,
    `value_source` (used by Task 5).
  - Terminal statuses: `done`, `blocked` (reasons `jev_blocked`,
    `no_progress`, `screen_unreadable`, `missing_value`, or a
    `TargetBlocked.reason`), `max_steps`, `error`.
  - `no_progress`: last 3 history entries `page_changed is False` and kind ≠ `wait`.
  - `screen_unreadable`: 3 consecutive observes with `readable` False,
    sleeping 0.5 s between them.
  - `field_text` raising `ValueError` ⇒ `blocked/missing_value`.
  - `DeviceDisconnected` ⇒ `error` with `reason="device_disconnected"`
    (session kept by the daemon).
- Produces (`result.py`): `build(runner) -> dict` with keys
  `session_id, status, target, where, elapsed_ms, model_calls,
  last_actions` (≤5 strings `"{kind} · {action}"` + `" = {text}"` for fills),
  plus present-only `reason, field, expected, seen, receipt, flow_saved`.
- Produces (`tests/fakes.py`): `FakeTarget(pages: list[dict])` — `observe`
  returns the current page; `act` records `(action_id, text)` and advances
  to the next page (or stays when the action id is in `stuck_ids`);
  Knobs: `stuck_ids: set[str]`, `advance_on_observe: int` (first N
  observes advance pages without an act), `raise_on_act: Exception | None`;
  records `acted: list[tuple[str, str | None]]` and `verified: list[str]`
  (ids acted with `verify=True`).
  `scripted_choose(choices: list[str], spy: list | None = None)` (appends
  `json.dumps(page)` to `spy` on each call) returns `choose`-shaped dicts
  `{"choice", "operation", "target", "confidence", "probabilities",
  "latency_ms", "usage"}` in order and raises if exhausted.

- [ ] **Step 1: Write failing tests:**

```python
def test_runs_to_done_and_returns_compact_result():
    t = FakeTarget([page(fill("n1", "Monto")), page(click("n2", "Siguiente")), page()])
    r = Runner("s1", t, "pay", {"amount": 5}, cfg, choose=scripted_choose(["n1", "n2", "DONE"]),
               field_text=lambda ctx: ("5", {"model": "m", "latency_ms": 1, "usage": {}}))
    out = r.run()
    assert out["status"] == "done" and out["model_calls"] == 3
    assert out["last_actions"] == ["fill · Monto = 5", "click · Siguiente"]
    assert len(json.dumps(out)) < 2048 and "screenshot" not in json.dumps(out)

def test_goal_includes_param_values():
    seen = []
    def choose(page, goal, history): seen.append(goal); return scripted_choose(["DONE"])(page, goal, history)
    Runner("s", FakeTarget([page()]), "pay rent", {"amount": 5, "recipient": ["A", "B"]}, cfg, choose=choose).run()
    assert seen[0].endswith("Values: amount=5, recipient=A / B")

def test_no_progress_blocks():
    t = FakeTarget([page(click("n1", "X"))], stuck_ids={"n1"})
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1"] * 3)).run()
    assert (out["status"], out["reason"]) == ("blocked", "no_progress")

def test_max_steps():
    t = FakeTarget([page(click("n1", "Next"))] * 10)
    out = Runner("s", t, "g", None, cfg, max_steps=2, choose=scripted_choose(["n1"] * 5)).run()
    assert out["status"] == "max_steps"

def test_loading_screen_waits_before_unreadable(monkeypatch):
    monkeypatch.setattr(runner_mod.time, "sleep", lambda s: None)
    t = FakeTarget([unreadable(), unreadable(), page(click("n1", "Ingresar")), page()])
    t.advance_on_observe = 2  # first two observes are a splash, then real screen
    out = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "DONE"])).run()
    assert out["status"] == "done"

def test_device_disconnect_reports_error_keeps_runner():
    t = FakeTarget([page(click("n1", "X"))], raise_on_act=DeviceDisconnected())
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1"]))
    out = r.run()
    assert (out["status"], out["reason"]) == ("error", "device_disconnected")
```

(`page`, `fill`, `click`, `unreadable` helpers live in `tests/fakes.py`.)

- [ ] **Step 2: Run** `uv run pytest tests/test_runner.py -v` — FAIL.
- [ ] **Step 3: Implement** `base.py`, `runner.py` (tick = observe → scrub → decide → act → record), `result.py`, `fakes.py`. For `fill`, build context with `jev_ultrafast.model.field_context(goal, action, page, history)`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `"Add runner loop and compact results"`.

---

### Task 4: Pause/resume — secrets, money guard, receipts

**Files:**
- Modify: `jev_mcp/runner.py`
- Test: `tests/test_runner_pause.py`

**Interfaces:**
- Consumes: Task 1 guards, Task 3 Runner.
- Produces:
  - When the chosen action is a secret fill: no `field_text` call;
    `status="paused_needs_input"`, `field=action["label"]`,
    `pending={"type": "secret", "locator": {...}}`.
  - When the chosen action is a money action: call `target.act(...,
    verify=True)` only if `check_money(page["text"], money_params).ok` and
    money params are non-empty; otherwise `status="paused_mismatch"` with
    `expected`/`seen` from `MoneyCheck` and `pending={"type": "money", ...}`.
    `money_params` = `params` restricted to `amount`, `recipient`, and any
    key in the run's `money_params` list.
  - `Runner.resume(input: str | None = None, instruction: str | None = None,
    confirm: bool = False) -> dict`:
    - pending secret: re-observe, locate the pending field by locator
      (resource_id, else label+role), `act(fill, text=input, verify=True)`,
      append `input` to `secrets`, continue `run()`. Missing `input` ⇒ stays
      paused.
    - pending money: `confirm=True` ⇒ re-observe, require same fingerprint
      as at pause (else re-run guard), execute the click, continue.
      `confirm=False` ⇒ stays paused.
    - `instruction` appends `"\n" + instruction` to the goal.
  - Receipts: on `done` with `executed_money`, call
    `target.screenshot(cfg.data_dir/"receipts"/f"{session_id}-{ts}.png")`
    and extract reference with regex
    `(?i)(referencia|comprobante|n[uú]mero de (transacci[oó]n|referencia)|reference)\D{0,20}(\d{4,})`;
    `receipt={"path": str, "reference": str | None}`.

- [ ] **Step 1: Write failing tests:**

```python
def test_secret_field_pauses_without_text_model():
    calls = []
    t = FakeTarget([page(fill("n1", "Código BAC")), page(click("n2", "Continuar")), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "n2", "DONE"]),
               field_text=lambda ctx: calls.append(ctx))
    out = r.run()
    assert out["status"] == "paused_needs_input" and out["field"] == "Código BAC" and calls == []
    out = r.resume(input="482913")
    assert out["status"] == "done" and t.acted[0] == ("n1", "482913")
    assert "482913" not in json.dumps(out)

def test_typed_secret_scrubbed_before_next_choose():
    seen = []
    t = FakeTarget([page(fill("n1", "OTP")), page(click("n2", "Ok"), text="Código 482913 aceptado"), page()])
    r = Runner("s", t, "g", None, cfg, choose=scripted_choose(["n1", "n2", "DONE"], spy=seen))
    r.run(); r.resume(input="482913")
    assert all("482913" not in s for s in seen)

def test_money_match_taps_autonomously_and_saves_receipt(tmp_path):
    confirm = page(click("n9", "Transferir"), text="Destino ****4066 Monto ₡150.000,00")
    receipt = page(text="Transferencia exitosa Referencia: 99887766")
    t = FakeTarget([confirm, receipt])
    out = Runner("s", t, "pay", {"amount": 150000, "recipient": "CR05015202001026284066"},
                 cfg_with_data(tmp_path), choose=scripted_choose(["n9", "DONE"])).run()
    assert out["status"] == "done"
    assert out["receipt"]["reference"] == "99887766" and Path(out["receipt"]["path"]).exists()
    assert t.acted[0][0] == "n9" and t.verified == ["n9"]

def test_money_mismatch_pauses_then_confirm_executes():
    confirm = page(click("n9", "Transferir"), text="Destino ****1234 Monto ₡150.000,00")
    t = FakeTarget([confirm, page()])
    r = Runner("s", t, "pay", {"amount": 150000, "recipient": "CR05015202001026284066"}, cfg,
               choose=scripted_choose(["n9", "DONE"]))
    out = r.run()
    assert out["status"] == "paused_mismatch" and t.acted == []
    assert r.resume()["status"] == "paused_mismatch"
    assert r.resume(confirm=True)["status"] == "done" and t.acted[0][0] == "n9"

def test_money_app_without_params_pauses():
    t = FakeTarget([page(click("n9", "Pagar"), url="net.bac.sbe.android/x")])
    out = Runner("s", t, "check", None, cfg, choose=scripted_choose(["n9"])).run()
    assert out["status"] == "paused_mismatch"
```

- [ ] **Step 2: Run** `uv run pytest tests/test_runner_pause.py -v` — FAIL.
- [ ] **Step 3: Implement** in `runner.py` per Interfaces (extend `FakeTarget` with `verified`, `text=`, `url=` helpers as needed).
- [ ] **Step 4: Run** `uv run pytest -v` — all PASS.
- [ ] **Step 5: Commit** `"Add pause/resume, money guard and receipts to runner"`.

---

### Task 5: Saved flows and replay

**Files:**
- Create: `jev_mcp/flows.py`
- Modify: `jev_mcp/runner.py`
- Test: `tests/test_flows.py`

**Interfaces:**
- Produces:
  - `@dataclass Flow`: `name: str`, `target: str`, `start: str | None`
    (url or package), `goal: str`, `params: list[str]`,
    `money_params: list[str]`, `steps: list[dict]`. Step:
    `{"signature": {"where": str, "labels": list[str]}, "locator":
    {"kind", "resource_id", "label", "role"} | {"control": id},
    "value": "param:<name>" | "secret" | "literal:<text>" | None}`.
  - `class FlowStore(root: Path)`: `save(flow)`, `load(name) -> Flow`
    (`KeyError` if absent), `list() -> list[dict]` (name, target, steps
    count, params), `delete(name)`. One JSON file per flow,
    `root/<slug>.json`.
  - `signature(page) -> dict`; `matches(sig: dict, page: dict) -> bool`
    (same `where` and Jaccard ≥ 0.8); `locate(locator, page) -> dict | None`
    (exactly one match by resource_id when non-empty, else label+role+kind;
    controls by id).
  - `flow_from_history(name, target, start, goal, params: dict,
    money_params: list[str], history: list[dict]) -> Flow` — `value` is
    `param:<k>` when the typed text equals `str(params[k])`, `secret` for
    secret fills, `literal:<text>` otherwise.
  - Runner: when `flow` is set, at each tick if `flow.steps[i]` matches the
    page and `locate` resolves, use it **without** `choose` (counts no
    model call); fill values from params (`param:`) / literal; `secret`
    ⇒ pause for input. Otherwise call `choose`, then on following ticks
    look for a match in `steps[i:i+3]` to resync. Guards apply identically.
  - `Runner.run()` on `done` with `save_as` set ⇒ `FlowStore.save(...)`
    and `flow_saved=<name>` in the result. `Runner(..., save_as: str | None = None, store: FlowStore | None = None)`.

- [ ] **Step 1: Write failing tests:**

```python
def test_record_then_replay_uses_no_model_calls(tmp_path):
    pages = lambda: [page(fill("n1", "Monto", rid="amt")), page(click("n2", "Pagar", rid="pay"), text="₡5"), page()]
    store = FlowStore(tmp_path)
    r1 = Runner("a", FakeTarget(pages()), "pay", {"amount": 5}, cfg, save_as="agua", store=store,
                choose=scripted_choose(["n1", "n2", "DONE"]), field_text=lambda c: ("5", {"model": "m", "latency_ms": 0, "usage": {}}))
    assert r1.run()["flow_saved"] == "agua"
    assert store.load("agua").steps[0]["value"] == "param:amount"
    t = FakeTarget(pages())
    out = Runner("b", t, "pay", {"amount": 7}, cfg, flow=store.load("agua"),
                 choose=scripted_choose(["DONE"])).run()
    assert out["status"] == "done" and out["model_calls"] == 1  # only final DONE check
    assert t.acted[0] == ("n1", "7")

def test_replay_falls_back_to_jev_on_unexpected_screen_and_resyncs(tmp_path): ...
    # flow recorded on [A, B, C]; replay sees [A, LOGIN, B, C]; expect choose called once for LOGIN,
    # then steps for B and C replayed without model calls.

def test_secret_never_stored_in_flow(tmp_path):
    # record a run that typed a secret via resume; flow file text must not contain it and value == "secret"

def test_flow_store_list_and_delete(tmp_path): ...
```

(Write the `...` tests concretely with the same helpers; assertions as in the comments.)

- [ ] **Step 2: Run** `uv run pytest tests/test_flows.py -v` — FAIL.
- [ ] **Step 3: Implement** `flows.py` and runner replay. Ending a replayed flow still requires one `choose` call returning `DONE` (visible evidence rule), unless the last recorded step was followed by DONE on a page whose signature matches — then finish without a model call.
- [ ] **Step 4: Run** `uv run pytest -v` — PASS.
- [ ] **Step 5: Commit** `"Add saved flows with replay and Jev fallback"`.

---

### Task 6: adb link keeper (relay, mDNS, reconnect)

**Files:**
- Create: `jev_mcp/adb_link.py`
- Test: `tests/test_adb_link.py`

**Interfaces:**
- Produces:
  - `class TcpRelay(local_port: int, host: str, port: int)`: `start()`
    (daemon thread, `127.0.0.1` only), `retarget(host: str)`, `stop()`.
    Threaded socket relay; each accepted connection pipes both ways.
  - `resolve_ip(mdns_name: str, timeout: float = 3.0) -> str | None` —
    `zeroconf` browse `_adb-tls-connect._tcp.local.` and `_adb._tcp.local.`,
    return IPv4 of the first instance whose name contains `mdns_name`.
  - `class AdbLink(cfg: Config, run=subprocess.run, resolve=resolve_ip,
    sleep=time.sleep, clock=time.monotonic)`:
    `ensure_connected() -> str` returns the adb serial to use:
    1. USB serial `cfg.android_serial` listed as `device` in `adb devices` ⇒ return it.
    2. Else resolve IP (`cfg.android_ip` fallback), ensure relay
       `127.0.0.1:{relay_port} → ip:{adb_tcp_port}`, run
       `adb connect 127.0.0.1:{relay_port}`; listed as `device` ⇒ return
       `"127.0.0.1:{relay_port}"`.
    3. Retry (1 s apart) until `cfg.reconnect_window_s` elapses ⇒ raise
       `DeviceDisconnected`.
    - Whenever USB is present, run `adb -s <usb> tcpip {adb_tcp_port}` once
      per daemon lifetime so wireless keeps working after unplugging.
- Why the relay: macOS Local Network privacy blocks the `adb` binary from
  LAN hosts ("No route to host") while Python processes are allowed
  (spec, Feasibility check results).

- [ ] **Step 1: Write failing tests:**

```python
def test_relay_pipes_bytes_both_ways():
    with echo_server() as port:
        relay = TcpRelay(free_port(), "127.0.0.1", port); relay.start()
        with socket.create_connection(("127.0.0.1", relay.local_port)) as s:
            s.sendall(b"hello"); assert s.recv(5) == b"hello"
        relay.stop()

def test_prefers_usb(fake_adb):
    fake_adb.devices = ["zdirl7t84pqwh6ww\tdevice"]
    assert AdbLink(cfg_android, run=fake_adb).ensure_connected() == "zdirl7t84pqwh6ww"
    assert ["adb", "-s", "zdirl7t84pqwh6ww", "tcpip", "5555"] in fake_adb.calls

def test_wireless_via_relay(fake_adb):
    fake_adb.devices = []; fake_adb.on_connect = lambda: fake_adb.devices.append("127.0.0.1:5556\tdevice")
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda name, timeout=3.0: "192.168.1.215")
    assert link.ensure_connected() == "127.0.0.1:5556"
    assert link.relay.host == "192.168.1.215"

def test_gives_up_after_window_and_raises(fake_adb, fake_clock):
    fake_adb.devices = []
    link = AdbLink(cfg_android, run=fake_adb, resolve=lambda *a, **k: None, sleep=fake_clock.sleep, clock=fake_clock.now)
    with pytest.raises(DeviceDisconnected):
        link.ensure_connected()
    assert fake_clock.now() >= 30
```

- [ ] **Step 2: Run** `uv run pytest tests/test_adb_link.py -v` — FAIL.
- [ ] **Step 3: Implement** `adb_link.py`.
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `"Add adb link keeper with localhost relay and mDNS"`.

---

### Task 7: AndroidTarget (uiautomator2)

**Files:**
- Create: `jev_mcp/targets/android.py`
- Test: `tests/test_android_target.py`

**Interfaces:**
- Consumes: `AdbLink.ensure_connected()`, `parse_hierarchy`, `Target` protocol.
- Produces: `AndroidTarget(link: AdbLink, app: str | None = None,
  connect=uiautomator2.connect)`; `kind = "android"`.
  - Construct: `serial = link.ensure_connected()`, `d = connect(serial)`;
    if `app` given and not foreground, `d.app_start(app)`.
  - `observe()`: `info = d.app_current()`; `parse_hierarchy(d.dump_hierarchy(compressed=False), info["package"], info["activity"])`.
  - `fresh(page)`: `observe()["fingerprint"] == page["fingerprint"]`.
  - `act(action, page, text=None, verify=False)`: if `verify` and not
    `fresh(page)` ⇒ `StalePage`. `click` ⇒ `d.click(cx, cy)` at bounds
    center; `fill` ⇒ click center, then `d.send_keys(text, clear=True)`;
    `back` ⇒ `d.press("back")`; `scroll` ⇒ `d.swipe_ext("up" if delta > 0 else "down", scale=0.6)`;
    `wait` ⇒ `sleep(0.3)`. Then `sleep(0.15)` settle.
  - Any uiautomator2/adb connection error during observe/act ⇒ call
    `link.ensure_connected()` and reconnect once; if that raises,
    propagate `DeviceDisconnected`.
  - `screenshot(path)`: `d.screenshot(str(path))`.
  - `close()`: no-op for the device (keep warm); drops the handle.

- [ ] **Step 1: Write failing tests** with a `FakeDevice` (records `click`, `send_keys`, `press`, `swipe_ext`; serves the fixture XML; `app_current()` returns BAC):

```python
def test_observe_maps_fixture(fake_device, link):
    p = AndroidTarget(link, connect=lambda s: fake_device).observe()
    assert any(a.get("resource_id", "").endswith("btnLogin") for a in p["actions"])

def test_click_taps_bounds_center(fake_device, link):
    t = AndroidTarget(link, connect=lambda s: fake_device); p = t.observe()
    btn = next(a for a in p["actions"] if a.get("resource_id", "").endswith("btnLogin"))
    t.act(btn, p)
    assert fake_device.calls[-1] == ("click", 799, 1265)  # center of [76,1210][1523,1321]

def test_verify_raises_stale_when_screen_changed(fake_device, link):
    t = AndroidTarget(link, connect=lambda s: fake_device); p = t.observe()
    fake_device.xml = "<hierarchy/>"
    with pytest.raises(StalePage):
        t.act(p["actions"][0], p, verify=True)

def test_reconnects_once_on_connection_error(fake_device, link):
    fake_device.fail_next_dump = ConnectionError("adb gone")
    t = AndroidTarget(link, connect=lambda s: fake_device)
    t.observe()
    assert link.ensure_calls == 2
```

(Integer center: `((l + r) // 2, (t + b) // 2)`; adjust the expected tuple to the fixture's actual btnLogin bounds.)

- [ ] **Step 2: Run** `uv run pytest tests/test_android_target.py -v` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `"Add AndroidTarget on uiautomator2"`.

---

### Task 8: ChromeTarget with blank/hung tab handling

**Files:**
- Create: `jev_mcp/targets/chrome.py`
- Test: `tests/test_chrome_target.py`

**Interfaces:**
- Consumes: `jev_ultrafast.browser.Browser`, `StalePage` (translate
  upstream `jev_ultrafast.browser.StalePage` to ours).
- Produces: `ChromeTarget(url: str, browser_factory=None)`; `kind = "chrome"`.
  - Opens via `browser_factory(url, background)` (default: a `Browser`
    subclass whose `__init__` mirrors upstream but takes `background`).
  - `loaded(b) -> bool`: `document.readyState == "complete"` and
    `document.body.innerText.trim().length > 0`.
  - Load policy: open background → not loaded ⇒ `Page.navigate` again and
    wait 15 s → not loaded ⇒ close, reopen with `background=False` → not
    loaded ⇒ raise `TargetBlocked("page_did_not_load")`.
  - `observe()` = `browser.observe(screenshot=False)` plus `readable=True`;
    `act(..., verify)` delegates (upstream always verifies); `screenshot`
    via CDP `Page.captureScreenshot` written to `path`; `close()` closes the tab.

- [ ] **Step 1: Write failing tests** with a fake browser factory whose `loaded` result is scripted:

```python
def test_retries_then_foreground_then_blocks():
    f = FakeBrowserFactory(loaded=[False, False, False])
    with pytest.raises(TargetBlocked) as e:
        ChromeTarget("https://app.gohighlevel.com", browser_factory=f)
    assert e.value.reason == "page_did_not_load"
    assert f.opened == [True, False] and f.renavigations == 1

def test_second_navigation_recovers():
    f = FakeBrowserFactory(loaded=[False, True])
    ChromeTarget("https://x.test", browser_factory=f)
    assert f.opened == [True]
```

- [ ] **Step 2: Run** `uv run pytest tests/test_chrome_target.py -v` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `"Add ChromeTarget with blank-tab recovery"`.

---

### Task 9: jevd daemon and client

**Files:**
- Create: `jev_mcp/daemon.py`, `jev_mcp/client.py`
- Test: `tests/test_daemon.py`

**Interfaces:**
- Consumes: Runner, FlowStore, AndroidTarget, ChromeTarget, AdbLink, config.
- Produces:
  - Wire protocol: one request line `{"op": str, "args": dict}` ⇒ one
    response line `{"ok": true, "result": dict}` or `{"ok": false,
    "error": str}`. Ops: `run_task`, `resume`, `screenshot`,
    `end_session`, `status`, `list_flows`, `delete_flow`.
  - `class Daemon(cfg, target_factory=None, runner_factory=Runner)`:
    `handle(request: dict) -> dict`. `target_factory(target: str,
    url_or_app: str | None, device: str | None) -> Target`.
    - `run_task` args: `target, goal, url_or_app=None, params=None,
      money_params=None, flow=None, save_as=None, max_steps=40,
      session_id=None, device=None`. Checks env keys first (message lists
      every missing var, like v1). New `session_id` = `uuid4().hex[:8]`.
      Keeps the session after any status except when `end_session` is
      called or TTL expires. `session_id` given ⇒ reuse that session's
      target with a new goal/runner.
    - Android device lock ⇒ `{"ok": false, "error": "device_busy: <session_id>"}`.
    - Unknown `session_id` ⇒ `error: unknown_session`. Unknown flow ⇒ `error: unknown_flow`.
    - `screenshot` ⇒ `{"path": ...}` under `data_dir/"screens"`.
    - `status` ⇒ `{"sessions": [{session_id, target, status, idle_s}],
      "android": {"serial": str | None, "connected": bool}, "flows": int}`.
    - Each session has a lock; concurrent calls on one session serialize.
  - `reap(now: float)` closes sessions idle > `cfg.session_ttl_s`;
    `serve()` runs `socketserver.ThreadingUnixStreamServer` on
    `data_dir/jevd.sock` plus a reaper thread (every 60 s); logs to
    `data_dir/jevd.log`. `python -m jev_mcp.daemon` calls `serve()`.
  - `client.call(op: str, args: dict) -> dict`: connect to the socket; on
    missing/refused, spawn `[sys.executable, "-m", "jev_mcp.daemon"]`
    detached (`start_new_session=True`, inherits env), poll up to 5 s for
    the socket, then send. Returns `result` or raises `RuntimeError(error)`.

- [ ] **Step 1: Write failing tests** (daemon `handle` with fake targets and scripted choose; one test through a real socket in `tmp_path`):

```python
def test_run_then_resume_same_session(daemon_with_fakes, keys):
    r = daemon_with_fakes.handle({"op": "run_task", "args": {"target": "android", "goal": "otp"}})
    assert r["ok"] and r["result"]["status"] == "paused_needs_input"
    sid = r["result"]["session_id"]
    r2 = daemon_with_fakes.handle({"op": "resume", "args": {"session_id": sid, "input": "111222"}})
    assert r2["result"]["session_id"] == sid and r2["result"]["status"] == "done"

def test_missing_keys_error_before_target(daemon_with_fakes, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False); monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    r = daemon_with_fakes.handle({"op": "run_task", "args": {"target": "chrome", "goal": "g", "url_or_app": "https://x"}})
    assert not r["ok"] and "TYPESAFE_API_KEY" in r["error"] and "TEXT_MODEL_API_KEY" in r["error"]
    assert daemon_with_fakes.targets_created == 0

def test_device_busy(daemon_with_fakes, keys): ...   # second android run while first paused -> error device_busy:<sid>
def test_reap_closes_idle_sessions(daemon_with_fakes, keys): ...  # reap(now + 901) closes target, status lists none
def test_disconnected_session_survives(daemon_with_fakes, keys): ...  # error/device_disconnected then resume works
def test_socket_roundtrip(tmp_path, monkeypatch): ...  # serve() in a thread with JEV_DATA_DIR=tmp_path; client.call("status", {}) returns dict
```

(Write the `...` tests concretely; assertions as in the comments.)

- [ ] **Step 2: Run** `uv run pytest tests/test_daemon.py -v` — FAIL.
- [ ] **Step 3: Implement** `daemon.py` and `client.py`.
- [ ] **Step 4: Run** `uv run pytest -v` — PASS.
- [ ] **Step 5: Commit** `"Add jevd daemon with sessions and autostarting client"`.

---

### Task 10: MCP tools, cleanup, docs

**Files:**
- Modify: `server.py`, `README.md`, `.env.example`, `tests/test_server.py`
- Delete: `summary.py`, `tests/test_summary.py`

**Interfaces:**
- Consumes: `client.call`.
- Produces MCP tools (thin; each returns `client.call(op, args)`;
  `RuntimeError` ⇒ `ToolError`):
  - `run_task(target: Literal["chrome", "android"], goal: str, url_or_app: str | None = None, params: dict | None = None, money_params: list[str] | None = None, flow: str | None = None, save_as: str | None = None, max_steps: int = 40, session_id: str | None = None, device: str | None = None) -> dict`
  - `resume(session_id: str, input: str | None = None, instruction: str | None = None, confirm: bool = False) -> dict`
  - `screenshot(session_id: str) -> dict`, `end_session(session_id: str) -> dict`,
    `status() -> dict`, `list_flows() -> dict`, `delete_flow(name: str) -> dict`
  - Tool docstrings tell Claude: one call per goal; pass exact `amount`/
    `recipient` in `params` for payments; on `paused_needs_input` ask the
    user and call `resume(input=...)`; on `paused_mismatch` show
    `expected`/`seen` and call `resume(confirm=True)` only after the user approves.

- [ ] **Step 1: Write failing tests** in `tests/test_server.py` (replace old ones):

```python
def test_tools_registered():
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"run_task", "resume", "screenshot", "end_session", "status", "list_flows", "delete_flow"}

def test_run_task_schema():
    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "run_task")
    assert tool.input_schema["required"] == ["target", "goal"]

def test_daemon_error_becomes_tool_error(monkeypatch):
    monkeypatch.setattr(server.client, "call", lambda op, args: (_ for _ in ()).throw(RuntimeError("device_busy: ab12")))
    with pytest.raises(ToolError, match="device_busy"):
        asyncio.run(mcp.call_tool("run_task", {"target": "android", "goal": "g"}))
```

- [ ] **Step 2: Run** `uv run pytest tests/test_server.py -v` — FAIL.
- [ ] **Step 3: Implement** `server.py`; delete `summary.py` and its test.
- [ ] **Step 4: Update docs:** README — new tools, `config.toml` example
  (`android_serial = "zdirl7t84pqwh6ww"`, `android_mdns = "adb-zdirl7t84pqwh6ww"`,
  `android_ip = "192.168.1.215"`, `money_apps`), the macOS Local Network /
  relay note, `uiautomator2` first-run install prompt on MIUI, and the
  registration command (unchanged: `claude mcp add jev-browser -- uv run
  --directory <repo> --env-file .env python server.py`). `.env.example`
  unchanged except a comment that `JEV_DATA_DIR` is optional.
- [ ] **Step 5: Run** `uv run pytest -v` — all PASS.
- [ ] **Step 6: Commit** `"Expose run_task/resume tools via jevd; drop browse_web"`.

---

### Task 11: Manual on-device verification (needs the tablet; keys optional)

No code unless a bug is found (then fix with a test first).

- [ ] **Step 1:** Tablet on USB, `config.toml` filled. Run
  `uv run python -c "from jev_mcp.config import load; from jev_mcp.adb_link import AdbLink; print(AdbLink(load()).ensure_connected())"`
  Expected: prints the USB serial.
- [ ] **Step 2:** Unplug USB. Run the same. Expected: `127.0.0.1:5556`
  within 30 s.
- [ ] **Step 3:** Open BAC on the tablet. Run a script that builds
  `AndroidTarget`, calls `observe()` 5 times, prints per-call ms and
  `btnLogin` label. Expected: label `Ingresar`; record timings over
  wireless in the spec's Feasibility section.
- [ ] **Step 4:** Hand-write a flow `bac-tipo-cambio` (one step: click
  label `Tipo de Cambio`) into `data_dir/flows/` and replay it through
  `client.call("run_task", {"target": "android", "goal": "open exchange
  rate", "flow": "bac-tipo-cambio"})` with dummy key env vars. Expected:
  the tablet opens Tipo de Cambio; the final DONE check fails with an
  HTTP error from TypeSafe (proves the full path up to Jev).
- [ ] **Step 5:** With real keys (when available): a non-money Chrome goal
  on a GHL page and a non-money Android goal. Report results; state
  plainly anything still unverified (money flows, receipts).
- [ ] **Step 6: Commit** spec timing notes: `"Record on-device verification results"`.
