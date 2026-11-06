"""Runner: per tick observe -> scrub -> decide -> act -> record."""
import hashlib
import logging
from collections import Counter
import os
import re
import time

from jev_ultrafast import model

from . import result
from . import signin
from .vault import field_kind as vault_field_kind, is_identity_field, scope_of as vault_scope
from . import verify as verify_mod
from .config import Config
from .flows import (
    Flow,
    FlowStore,
    auto_name,
    element_labels,
    find_by_locator,
    flow_from_history,
    locate,
    matches,
    slug,
    value_source,
)
from .guards import (
    _amount_seen,
    check_money,
    is_destructive_action,
    is_money_action,
    is_secret_field,
    is_send_action,
    may_retry_tap,
    scrub,
    scrub_text,
    scrub_value,
)
from .targets.base import DeviceDisconnected, StalePage, Target, TargetBlocked

_log = logging.getLogger("jevd")

UNREADABLE_TRIES = 3
NO_PROGRESS = 3
MAX_WAITS = 3  # a churny SPA never looks "settled"; after this many waits, force a real decision
LOAD_PATIENCE = 20.0  # s per run spent waiting on our own while the target reports page["loading"]
LOAD_POLL = 0.5
LOAD_MIN_TEXT = 40  # a web page showing fewer characters than this is treated as still loading
LOAD_CLEAR_READS = 2  # consecutive non-loading reads required after a loading phase
WAIT_SETTLE = 12.0  # max seconds a chosen "wait" may spend letting a page finish loading late
#                     content (a SPA that mounts a cross-origin iframe seconds after its shell
#                     renders); upstream's wait sleeps only 0.1s, far too short for that.
WAIT_STATIC = 4.0   # a screen unchanged for this long after a 'wait' isn't loading anything: stop
WAIT_POLL = 0.5     # poll interval while settling
WAIT_STABLE = 2     # consecutive unchanged polls that count as "the new content has settled"
LOOP_DROP = 2   # after the same action is taken this many times on one screen, drop it from the
#                 next view so Jev is forced to try something else (auto-recover from a cycle).
LOOP_LIMIT = 3  # hard backstop: same action on the same screen this many times = a cycle (e.g. open
#                 wrong account -> back -> open again); stop instead of churning to max_steps. A legit
#                 retry changes the screen (different fingerprint), so it is not counted.
MONEY_KEYS = ("amount", "recipient")
ESCAPABLE = ("pending_element_missing", "pending_element_ambiguous")
RESYNC_WINDOW = 3  # after a Jev fallback, look this many recorded steps ahead
REFERENCE_RE = re.compile(
    r"(?i)(referencia|comprobante|n[uú]mero de (transacci[oó]n|referencia)|reference)\D{0,20}(\d{4,})")


def goal_text(goal: str, params: dict | None) -> str:
    if not params:
        return goal
    vals = ", ".join(
        f"{k}={' / '.join(map(str, v)) if isinstance(v, (list, tuple)) else v}"
        for k, v in params.items()
    )
    return f"{goal}\nValues: {vals}"


def locator_of(action: dict) -> dict:
    if "node" in action:
        return {k: action.get(k) for k in ("kind", "resource_id", "label", "role")}
    return {"control": action["id"]}


def coarse_fingerprint(page: dict) -> str:
    """The screen by URL and its controls only, ignoring text. A page with live timestamps or a
    polling widget gets a new exact fingerprint on every read, which blinded the loop guards: the
    same click on the same screen looked like progress each time (seen: a contact page where the
    same tab was clicked five times in a row to max_steps)."""
    controls = sorted(f"{a.get('kind')}|{a.get('role') or ''}|{a.get('label') or ''}"
                      for a in page.get("actions") or [] if a.get("kind") not in ("wait", "scroll"))
    return hashlib.sha256(((page.get("url") or "") + "\n" + "\n".join(controls)).encode()).hexdigest()


def _same_screen(h: dict, fingerprint: str, coarse: str) -> bool:
    """A history entry taken on this screen: the exact fingerprint, or the same URL and controls."""
    return h.get("fingerprint") == fingerprint or (bool(h.get("coarse")) and h.get("coarse") == coarse)


def history_entry(action: dict, page: dict, text: str | None, secret_patterns: list[str] | None = None) -> dict:
    """Replay-ready record of one executed action (the runner fills value_source)."""
    locator = locator_of(action)
    return {
        "action": action["label"],
        "kind": action["kind"],
        "text": "•••" if text is not None and is_secret_field(action, secret_patterns or []) else text,
        "page_changed": None,
        "where": page["url"],
        "fingerprint": page.get("fingerprint"),  # for cycle detection (oscillation between screens)
        "signature": {"where": page["url"], "labels": element_labels(page)},
        "locator": locator,
        "value_source": None,
    }


class _NoText(Exception):
    """The text model (typed values) is unreachable or refusing: the fill is given up and the
    run goes on with the page's other controls."""


class _Stop(Exception):
    def __init__(self, status: str, reason: str | None = None):
        self.status, self.reason = status, reason


# Messages of a lost link to the driven browser or device (websocket closed, port refused).
_CONNECTION_LOST = re.compile(
    r"no close frame|connection (?:refused|reset|closed|lost|aborted)|ECONNREFUSED|ECONNRESET|"
    r"remote (?:host|end) closed|not connected to|broken pipe", re.I)

# The choice model takes at most 255 candidates per question (HTTP 422 above it) and a request
# of roughly 64 KB (HTTP 400 max_tokens_exceeded above it). A workflow canvas after "Fit to
# Screen" offered 273 click targets in a 68 KB request, so the view is trimmed to both limits:
# measured on that canvas, 220 controls (60 KB) passed and 255 (68 KB) did not.
MODEL_TARGET_LIMIT = 255
MODEL_BODY_BUDGET = 48_000     # estimated request bytes (about 8% under the real size); labels go twice
_WORD = re.compile(r"[a-z0-9]{3,}")


def _element_bytes(action: dict) -> int:
    return 2 * len(action.get("label") or "") + 160


def cap_for_model(actions: list[dict], goal: str, limit: int = MODEL_TARGET_LIMIT, text: str = "",
                  budget: int = MODEL_BODY_BUDGET) -> list[dict]:
    """The controls the model gets: at most `limit` distinct ones, within an estimated request
    size, goal-named ones first, then screen order. Wait and scroll controls are never dropped;
    a view within both limits comes back unchanged."""
    ops = [a for a in actions if a.get("kind") in ("click", "fill", "select")]
    nodes = {a.get("node", a["id"]) for a in ops}
    size = 1500 + 4 * len(goal or "") + len(text or "") + sum(
        _element_bytes(next(a for a in ops if a.get("node", a["id"]) == n)) for n in nodes)
    if len(nodes) <= limit and size <= budget:
        return actions
    words = set(_WORD.findall((goal or "").lower()))
    ranked = sorted(ops, key=lambda a: 0 if words & set(_WORD.findall((a.get("label") or "").lower())) else 1)
    kept_nodes, kept = set(), []
    per_kind = Counter()
    used = 1500 + 4 * len(goal or "") + len(text or "")
    for a in ranked:
        node = a.get("node", a["id"])
        if node not in kept_nodes:
            cost = _element_bytes(a)
            if len(kept_nodes) >= limit or per_kind[a["kind"]] >= limit or used + cost > budget:
                continue
            kept_nodes.add(node)
            per_kind[a["kind"]] += 1
            used += cost
        kept.append(a)
    keep_ids = {a["id"] for a in kept}
    return [a for a in actions if a["id"] in keep_ids or a.get("kind") not in ("click", "fill", "select")]


BRING_LIMIT = 12   # "Bring <title> into view" offers kept per decision, goal-named ones first
# "click the 'Unit: minutes' dropdown", "choose 'days'", "open 'Settings'": a quoted label after a click verb
_CLICK_QUOTED = re.compile(
    r"\b(?:click|open|choose|select|tap|press|pick|expand|toggle)\b(?:\s+(?:on|the|a|an|into|option|button|tab|menu|dropdown|item|node))*"
    r"\s*['\"\u201c\u2018]([^'\"\u201d\u2019]{1,60})['\"\u201d\u2019]", re.I)
_NEGATED = re.compile(r"\b(?:not|never|don't|do not|without|avoid|skip|except|instead of)\b[^.;,]*$", re.I)  # to the next punctuation
_QUOTED = re.compile(r"['\"\u201c\u2018]([^'\"\u201d\u2019]{2,60})['\"\u201d\u2019]")   # 'a title' or "a title" in a goal
_HIDE_KEYBOARD = re.compile(r"\b(?:hide|dismiss|close)\b[^.;,]{0,20}\bkeyboard\b|ocultar[^.;,]{0,10}teclado", re.I)
_PRESS_ESCAPE = re.compile(r"\bpress(?:\s+the)?\s*['\"\u201c\u2018]?(?:escape|esc)\b", re.I)  # "press Escape"
# a clause the goal rules out ("Do not click Delete or Publish"), up to the next punctuation
_NEG_CLAUSE = re.compile(r"\b(?:not|never|don't|do not|without|avoid|skip|except|instead of)\b([^.;,\n]*)", re.I)
# a value the goal spells out for a field: a typing verb, then a quoted string or a bare number
_TYPE_QUOTED = re.compile(
    r"\b(?:type|enter|write|input|put|fill(?:\s+in)?|set(?:\s+it)?|replace(?:\s+it)?|change(?:\s+it)?|search(?:\s+for)?|look\s+up)\b"
    r"(?:\s+(?:in|into|the|value|text|number|field|with|as|to|by))*"
    r"\s*(?:['\"\u201c\u2018]([^'\"\u201d\u2019]{1,200})['\"\u201d\u2019]|(-?\d(?:[\d.,:/-]{0,29}\d)?)(?![\w%]))", re.I)


def trim_bring_into_view(actions: list[dict], goal: str, limit: int = BRING_LIMIT) -> list[dict]:
    """A canvas can offer dozens of off-screen nodes; keep the ones the goal names, then the
    first few in screen order, so the model's option list stays short."""
    bring = [a for a in actions if a.get("pan_to")]
    if not bring:
        return actions
    words = set(_WORD.findall((goal or "").lower()))
    def title(a):  # the node's own words, without the "Bring … into view" template
        return _WORD.findall((a.get("label") or "").lower().removeprefix("bring ").removesuffix(" into view"))
    named = [a for a in bring if words & set(title(a))]
    keep = {a["id"] for a in named[:limit]}
    # a scrolling panel's off-screen controls (in_list) are offered only by name; a canvas's nodes
    # fill the list up to the limit so Jev can see what is out there
    for a in bring:
        if len(keep) >= limit:
            break
        if not a.get("in_list"):
            keep.add(a["id"])
    return [a for a in actions if not a.get("pan_to") or a["id"] in keep]


class _LoggingClient:
    """The model client, with a provider's error body in jevd's log (a bare status code says
    nothing about a rejected request). Never logs a successful response."""

    def __init__(self, inner):
        self._inner = inner

    def post(self, *args, **kwargs):
        url = str(args[0] if args else kwargs.get("url") or "")
        host = re.sub(r"^https?://([^/]+).*$", r"\1", url) or "?"
        try:
            response = self._inner.post(*args, **kwargs)
        except Exception as e:  # a connection failure names its host too ("Model connection failed" alone did not)
            _log.warning("model provider unreachable at %s: %s", host, (str(e) or type(e).__name__)[:200])
            raise
        if response.is_error:
            _log.warning("model provider HTTP %s at %s: %s", response.status_code, host, response.text[:300])
        return response

    def __getattr__(self, name):
        return getattr(self._inner, name)


if not isinstance(model.CLIENT, _LoggingClient):
    model.CLIENT = _LoggingClient(model.CLIENT)


class Runner:
    settle_delay = 0.4  # seconds between reads when checking a paused screen holds still

    def __init__(self, session_id: str, target: Target, goal: str, params: dict | None,
                 cfg: Config, *, max_steps: int = 40, money_params: list[str] | None = None,
                 flow: Flow | None = None, save_as: str | None = None,
                 store: FlowStore | None = None, start: str | None = None,
                 learn: bool = True, choose=model.choose, field_text=model.field_text, verify=None,
                 memory=None, vault=None):
        self.session_id, self.target, self.params, self.cfg = session_id, target, params, cfg
        self.goal = goal_text(goal, params)
        self._goal_raw = goal  # saved flows keep the goal without this run's values
        self._signin_ok = signin.goal_wants_signin(goal, params)
        self._add_hints(memory, target, goal, start)  # fold retrieved lessons into the goal context
        self.max_steps, self.money_params, self.flow = max_steps, money_params or [], flow
        if flow is not None:  # the flow's money-critical keys guard every replay
            self.money_params = sorted({*self.money_params, *flow.money_params})
        if save_as is not None:
            slug(save_as)  # reject an unusable name now, not after the run
        self.save_as, self.store, self.start = save_as, store, start
        self.flow_i = 0  # next recorded step expected when replaying `flow`
        self._resyncing = False  # Jev decided during a flow: look ahead to resync
        self.learn = learn
        self.flow_saved: str | None = None
        self.flow_updated: str | None = None
        self._jev_steps = 0  # actions decided by Jev (not replayed)
        self._choose, self._field_text = choose, field_text
        self._verify = verify  # None: verify.check, looked up per call
        self._vault = vault  # stored passwords/codes typed straight to the device (vault.Keychain)
        self.verified: bool | None = None  # independent check of the last DONE/BLOCKED
        self._verify_reason = ""
        self.status, self.reason = "running", None
        self.history: list[dict] = []
        self.page: dict = {}
        self.model_calls = 0
        self.loop_events = 0  # times a cycle was detected (auto-recover drop or hard stop)
        self.elapsed_ms = 0
        self.timing: dict[str, float] = {}  # ms per phase: observe, decide, text, act, verify
        self.secrets: list[str] = []
        self._secret_fields: set = set()  # locators of fields a secret was typed into
        self.pending: dict | None = None
        self.last_used = time.time()
        self.executed_money = False  # never reset once True: the receipt must survive
        self.money_attempted = False  # one autonomous money action per run; never reset
        self.sent_attempted = False  # one message/forward send per run; never reset
        self.destructive_attempted = False  # a confirmed delete happened in this run; never reset
        self._money_uncertain = False  # terminal: the session must be ended
        self.receipt: dict | None = None
        self._started = time.perf_counter()
        self._stale = 0
        self._signin_reloaded = False  # one reload of the start URL per run on a sign-in bounce
        self.last_stale: dict | None = None  # why the last action was refused as stale (result detail)
        self.last_blocked: dict | None = None  # what Jev was looking at when it gave up (result detail)
        self.last_error: dict | None = None  # the raw error behind target_unreachable (result detail)
        self._goal_typed: set = set()  # values spelled out in the goal that this run already typed
        self._text_down: str | None = None  # the text model's failure, once it has failed this run
        self._quoted_as: dict = {}  # a short quoted name -> the full label it was clicked under
        self._load_waited = 0.0  # seconds spent waiting on a loading page without asking Jev
        self._load_clear = LOAD_CLEAR_READS  # clear reads since the last loading one (none seen yet)
        self._obs_log: list[tuple[str, str]] = []  # recent (where, text): a field can scroll off

    def run(self) -> dict:
        if self._money_uncertain:
            return result.build(self)
        if not self.status.startswith("paused"):
            self.pending = None
        return self._drive()

    def resume(self, input: str | None = None, instruction: str | None = None,
               confirm: bool = False, typed: bool = False) -> dict:
        """Continue a paused run. Never lets a secret value reach a result. `typed`: the person
        typed the secret on the device themselves; continue without a value."""
        if self._money_uncertain:
            return result.build(self)
        if instruction:
            self.goal += "\n" + instruction
        pending = self.pending
        if pending is None:
            return self.run()
        if instruction and self.reason in ESCAPABLE:
            # Escape hatch: the paused element is gone; let Jev re-plan (guards still apply).
            self.pending = None
            return self.run()
        if pending["type"] == "secret" and not input and not typed:
            return result.build(self)
        if pending["type"] in ("money", "destructive") and not confirm:
            return result.build(self)
        return self._drive(lambda: self._resume_pending(pending, input, typed))

    def _drive(self, first=None) -> dict:
        self.status, self.reason = "running", None
        self._started = time.perf_counter()
        try:
            if first is not None:
                first()
            while self.status == "running":
                self._tick()
        except _Stop as s:
            self.status, self.reason = s.status, s.reason
        except TargetBlocked as e:
            self.status, self.reason = "blocked", e.reason
        except DeviceDisconnected:
            self.status, self.reason = "error", "device_disconnected"
        except StalePage as e:
            self.status, self.reason = "error", "stale_page"
            if self._stale < 3:  # raised by observe itself, not by three refused actions
                self.last_stale = {"action": "observe", "why": (str(e) or "page changed")[:160]}
            if self._met_after_all():
                # The last action landed and the page churned after it (a Save closed its modal,
                # a toast came and went): the goal is visibly met, so this is a success, not an
                # error (seen: a From Name save that persisted but was reported as stale_page).
                self.status, self.reason = "done", "verified_after_stale"
        except Exception as e:  # provider/target failures end the run, never escape
            msg = scrub({"text": str(e) or type(e).__name__}, self.secrets)["text"]
            if _CONNECTION_LOST.search(msg):
                # The driven browser or device dropped the link mid-run (Chrome closed, restarted,
                # or started without its debugging port): a plain name beats a websocket message.
                self.status, self.reason = "error", "target_unreachable"
                self.last_error = {"error": msg[:160]}
            else:
                self.status, self.reason = "error", msg[:200]
        finally:
            self._touch()
        _log.info("session=%s status=%s steps=%d model_calls=%d elapsed_ms=%d timing=%s",
                  self.session_id, self.status, len(self.history), self.model_calls,
                  self.elapsed_ms, {k: round(v) for k, v in self.timing.items()})
        if self.status == "done":
            self._save_flow()
        return result.build(self)

    def _save_flow(self):
        if not self.store or self.flow_saved or self.flow_updated:
            return
        start = self.start or (self.history[0]["where"] if self.history else None)
        if self.save_as:
            name, start, goal, healing = self.save_as, start, self._goal_raw, False
        elif not self.learn or not self._jev_steps or self.verified is not True:
            return  # learn only from runs whose result was independently confirmed
        elif self.flow is None:
            name, goal, healing = auto_name(self.target.kind, start, self._goal_raw), self._goal_raw, False
        else:
            name, start, goal, healing = self.flow.name, self.flow.start, self.flow.goal, True
        try:
            flow = flow_from_history(name, self.target.kind, start, goal,
                                     self.params or {}, self.money_params, self.history)
            self.store.save(self._scrub_flow(flow))
            if healing:
                self.flow_updated = name
            else:
                self.flow_saved = name
        except Exception:  # a failed save must not undo a finished run
            self.reason = "flow_save_failed"

    def _scrub_flow(self, flow: Flow) -> Flow:
        """Belt and braces: no typed secret may reach a flow file, even inside a literal."""
        for step in flow.steps:
            step["signature"] = scrub_value(step["signature"], self.secrets)
            step["locator"] = scrub_value(step["locator"], self.secrets)
            v = step["value"]
            if isinstance(v, str) and scrub_text(v, self.secrets) != v:
                step["value"] = "secret"
        flow.start = scrub_value(flow.start, self.secrets)
        flow.goal = scrub_text(flow.goal, self.secrets)
        return flow

    def _resume_pending(self, pending: dict, input: str | None, typed: bool = False):
        status = {"secret": "paused_needs_input",
                  "destructive": "paused_confirm"}.get(pending["type"], "paused_mismatch")
        self.page = {}  # force a real re-observe: the screen may have moved while paused
        page = self._observe()
        same_screen = page.get("fingerprint") == pending["fingerprint"]
        if same_screen:
            # ids alone can shift under an unchanged fingerprint: the locator must agree too
            found = [a for a in page.get("actions", []) if a["id"] == pending["action_id"]
                     and locator_of(a) == pending["locator"]]
            if not found:
                found = find_by_locator(page, pending["locator"])
        else:
            found = find_by_locator(page, pending["locator"])
        if pending["type"] in ("money", "destructive"):
            found = [a for a in found if a.get("label") == pending["label"]]
        if pending["type"] == "secret" and typed:
            # The person typed it; the app has usually moved on, so the field may be gone by now.
            self._typed_by_human(found[0] if len(found) == 1 else None, page, pending)
            self.pending = None
            return
        if not found:
            raise _Stop(status, "pending_element_missing")
        if len(found) > 1:
            raise _Stop(status, "pending_element_ambiguous")
        action = found[0]
        if pending["type"] == "secret":
            self._execute(action, page, input, verify=True, secret=True)
            self.pending = None
            return
        if pending["type"] == "destructive":
            if not (same_screen and self._text_hash(page) == pending["text_hash"]):
                # Screen changed since the human looked at it: their confirm no longer applies.
                self._guard_destructive(action, page)
            try:
                self._execute(action, page, None, verify=True, destructive=True)
            except StalePage:
                self.page = {}  # refused before tapping: nothing deleted; keep waiting
                raise _Stop("paused_confirm", "screen_changed")
            self.pending = None
            return
        try:
            if not _amount_seen(self._money_text(page)):
                # A strong-label tap on a screen with no amount only navigates (e.g. a bank's
                # account-page "Transferir" that opens the form): not a money action, so it must
                # not spend the one-money-action budget or save a receipt.
                self._execute(action, page, None, verify=True)
                self.pending = None
                return
            if not (same_screen and self._text_hash(page) == pending["text_hash"]):
                # Screen changed since the human looked at it: their confirm no longer applies.
                self._guard_money(action, page, self._money_params())
                return
            self._execute(action, page, None, verify=True, money=True)
        except StalePage:
            # The target refused before tapping: nothing moved; keep waiting for a confirm.
            self.page = {}
            raise _Stop("paused_mismatch", "screen_changed")

    def _text_hash(self, page: dict) -> str:
        return hashlib.sha256(scrub_text(page.get("text") or "", self.secrets).encode()).hexdigest()

    def _typed_by_human(self, action: dict | None, page: dict, pending: dict) -> None:
        """The person typed the secret on the device: record the step with no value, and keep
        whatever the field now shows (a plain-text code field echoes it) out of every model
        payload and result. `action` is None when the field has already left the screen."""
        if action is None:
            loc = pending.get("locator") or {}
            action = {"kind": "fill", "id": pending["action_id"], "node": pending["action_id"],
                      "label": pending.get("label") or "",
                      **{k: v for k, v in loc.items() if k in ("resource_id", "role")}}
        shown = action.get("value")
        if shown and shown not in self.secrets:
            self.secrets.append(shown)
        self._secret_fields.update(self._field_keys(action))
        entry = history_entry(scrub_value(action, self.secrets), scrub(page, self.secrets), None,
                              self.cfg.secret_patterns)
        entry["text"], entry["value_source"], entry["page_changed"] = "•••", "human", True
        self.history.append(entry)
        self.page = {}

    def _fill_from_vault(self, action: dict, page: dict) -> bool:
        """Type the stored password, or a fresh authenticator code, for this site/app if the vault
        has one. The value goes from the Keychain to the device only: never to a model, a result
        or a log. Entries are keyed by the page's host / the app's package, so a stored value can
        only land on its own site or app."""
        if self._vault is None:
            return False
        scope = vault_scope(getattr(self.target, "kind", None), page.get("url"))
        kind = vault_field_kind(action)
        try:
            value = self._vault.totp(scope) if kind == "totp" else self._vault.password(scope)
        except Exception:
            value = None
        if not value:
            return False
        n = len(self.history)
        self._execute(action, page, value, verify=True, secret=True)
        if len(self.history) > n:
            self.history[-1]["value_source"] = "authenticator" if kind == "totp" else "keychain"
        return True

    def _fill_username_from_vault(self, action: dict, page: dict) -> bool:
        """Type the stored sign-in identifier (email / username) into an identity field of a
        sign-in page, when the vault has one for the site. Looked up by the page's host, then —
        on an identity provider's page (accounts.google.com) or any login URL — by the host the
        run started on, so one entry covers a site and the provider it signs in through. Only on
        sign-in pages: an email field elsewhere (a contact form) is the goal's or the model's."""
        if self._vault is None or getattr(self.target, "kind", None) != "chrome":
            return False
        if not is_identity_field(action) or not signin.is_signin_page(page):
            return False
        url = page.get("url") or ""
        scopes = [vault_scope("chrome", url)]
        if signin.is_login_url(url) and self.start:
            scopes.append(vault_scope("chrome", self.start))
        value = None
        for scope in scopes:
            if not scope:
                continue
            try:
                value = self._vault.username(scope)
            except Exception:
                value = None
            if value:
                break
        if not value:
            return False
        n = len(self.history)
        self._execute(action, page, value, verify=True, secret=True)  # kept out of results and logs
        if len(self.history) > n:
            self.history[-1]["value_source"] = "keychain"
        return True

    def _pending_for(self, kind: str, action: dict, page: dict, **extra) -> dict:
        return {"type": kind, "locator": locator_of(action), "action_id": action["id"],
                "label": action.get("label"), "fingerprint": page.get("fingerprint"), **extra}

    def _add_hints(self, memory, target, goal, start) -> None:
        """Query the experience memory for lessons relevant to this goal/app and append them to the
        goal as advisory hints. Advisory only — the model may ignore them and the guards are
        unchanged. Lessons are already PII-scrubbed in the store, so nothing sensitive is added."""
        if memory is None:
            return
        try:
            hints = memory.query_lessons(target=getattr(target, "kind", None), app=start,
                                         goal=goal, k=3)
        except Exception:
            return  # retrieval must never break a run
        if hints:
            self.goal += ("\n\nWhat we've learned before (hints — may not all apply):\n"
                          + "\n".join(f"- {h['body']}" for h in hints if h.get("body")))

    def _touch(self):
        self.last_used = time.time()
        self.elapsed_ms += round((time.perf_counter() - self._started) * 1000)
        self._started = time.perf_counter()

    # --- tick: observe -> scrub -> decide -> act -> record ---
    def _tick(self):
        if len(self.history) >= self.max_steps:
            raise _Stop("max_steps")
        if self._no_progress():
            # the same screen after the last few actions: say what was on it and what was tried
            self.last_blocked = self._blocked_detail({}, self.page or {})
            self.last_blocked["tried"] = [h.get("action") for h in self.history[-NO_PROGRESS:]]
            raise _Stop("blocked", "no_progress")
        page = self._observe()
        if self._still_loading(page):
            # Spinners/skeletons still visible (or a near-empty page): the page is arriving. Asking
            # Jev now gets a "wait" at best and a BLOCKED on the empty shell at worst; look again.
            self._load_waited += LOAD_POLL
            self._timed("observe_ms", time.sleep, LOAD_POLL)
            self.page = {}
            return
        if (getattr(self.target, "kind", None) == "chrome" and not self._signin_ok
                and signin.is_signin_page(page)):
            if self._reload_once_on_signin(page):
                return  # back on the requested page's load: observe again
            raise _Stop("blocked", "signed_out")  # a person must sign in in that Chrome
        replay = self._match_flow(page)
        if replay is None and self.flow is not None:
            self._resyncing = True
        if replay:
            decision = None
        else:
            view = scrub(page, self.secrets)
            looped = self._looped_locators(page)
            if looped:
                self.loop_events += 1
                kept = [a for a in view["actions"]
                        if tuple(sorted(locator_of(a).items())) not in looped]
                if not any(a.get("kind") not in ("wait", "scroll") for a in kept):
                    raise _Stop("blocked", "stuck_in_loop")  # nothing else to try on this screen
                view = {**view, "actions": kept}  # force a different choice (auto-recover)
            if self._consecutive_waits() >= MAX_WAITS:
                # Jev keeps stalling on a never-settling page: drop "wait" so it must act or stop.
                view = {**view, "actions": [a for a in view["actions"] if a.get("kind") != "wait"]}
            decision = self._decide(view)
        try:
            if replay:
                self._replay(*replay, page)
            else:
                self._act_and_record(decision, page)
        except _NoText:
            self.page = {}  # nothing happened; the next read decides without the fill
        except StalePage as e:
            self._stale += 1
            self.page = {}
            label = "replay" if replay else self._choice_label(decision, page)
            self.last_stale = {"action": label, "why": (str(e) or "page changed")[:160],
                               "strike": self._stale}
            _log.info("session=%s stale %d/3 before acting on %r: %s", self.session_id,
                      self._stale, label, self.last_stale["why"])
            if self._stale >= 3:
                raise

    def _still_loading(self, page: dict) -> bool:
        """Wait on our own while the target flags loading, while a web page has almost no text yet,
        and for two more reads after a loading phase ends (a loader is often followed by a skeleton
        a second or two later). Bounded by LOAD_PATIENCE per run, then Jev decides."""
        if self._load_waited >= LOAD_PATIENCE:
            return False
        chrome = getattr(self.target, "kind", None) == "chrome"
        if page.get("loading") or (chrome and len((page.get("text") or "").strip()) < LOAD_MIN_TEXT):
            self._load_clear = 0  # a loading phase was seen: demand clear reads after it
            return True
        if self._load_clear < LOAD_CLEAR_READS:
            self._load_clear += 1
            return True
        return False

    def _blocked_detail(self, decision: dict, page: dict) -> dict:
        """What Jev was looking at when it gave up: the screen's size and controls, and how BLOCKED
        weighed against acting. Jev's answer is a bare choice, so this is the only record of the
        cause: "no_controls" means the content never reached Jev (a canvas, an unreadable frame,
        a page still arriving), while controls with a low BLOCKED weight mean the goal did not
        name a target Jev could match."""
        actions = page.get("actions") or []
        controls = Counter(a.get("kind") for a in actions if a.get("kind") not in ("wait", "scroll", "key"))
        probs = (decision or {}).get("operation_probabilities") or {}
        top = sorted(probs.items(), key=lambda kv: -kv[1])[:3]
        detail = {"url": page.get("url", ""), "text_chars": len(page.get("text") or ""),
                  "controls": dict(controls), "weighed": {k: round(float(v), 2) for k, v in top}}
        in_frames = sum(1 for a in actions if a.get("frame"))
        if in_frames:
            detail["in_iframes"] = in_frames
        # what was on offer, so a report can say whether the goal's control was there at all
        detail["sample"] = [(a.get("label") or "")[:40] for a in actions
                            if a.get("kind") not in ("wait", "scroll", "key")][:15]
        if page.get("covered"):
            detail["covered"] = page["covered"]
        if page.get("disabled"):  # named controls the app has greyed out: not on offer by design
            detail["disabled"] = [d[:60] for d in page["disabled"][:10]]
        # titles a canvas offers to bring into view: the names as the app shows them, so a goal
        # can quote one exactly (a node's settings text, "4 hours", is not on its card)
        canvas = [(a.get("label") or "")[len("Bring "):-len(" into view")] for a in actions if a.get("pan_to")]
        if canvas:
            detail["canvas_nodes"] = canvas[:20]
        if self._load_waited:
            detail["waited_loading_ms"] = round(self._load_waited * 1000)
        if detail["text_chars"] == 0 and sum(controls.values()) <= 2:
            # Nothing rendered at all (an injected widget or two at most): the app showed an empty
            # shell, usually because the URL names a route it does not know.
            detail["why"] = "blank_page"
        elif not controls:
            detail["why"] = "no_controls"
        return detail

    @staticmethod
    def _choice_label(decision, page: dict) -> str:
        choice = (decision or {}).get("choice") if isinstance(decision, dict) else None
        a = next((a for a in page.get("actions") or [] if a.get("id") == choice), None)
        return f"{a.get('kind')} · {a.get('label')}" if a else str(choice)

    def _reload_once_on_signin(self, page: dict) -> bool:
        """A signed-in web app can still bounce one fresh load of a deep link to its login page
        (seen while its session token was being refreshed: sibling tabs stayed signed in and the
        same link loaded fine seconds later). Before blocking the run, reload the start URL once;
        a truly signed-out browser lands on the login page again and then blocks as before."""
        start = getattr(self.target, "url", None)
        ensure_at = getattr(self.target, "ensure_at", None)
        if self._signin_reloaded or not start or ensure_at is None or signin.is_login_url(start):
            return False
        self._signin_reloaded = True
        _log.info("session=%s sign-in page at %s on a fresh load: reloading %s once",
                  self.session_id, page.get("url", ""), start)
        if not self._timed("act_ms", ensure_at, start):
            return False
        self.page = {}
        return True

    def _consecutive_waits(self) -> int:
        n = 0
        for h in reversed(self.history):
            if h.get("kind") == "wait":
                n += 1
            else:
                break
        return n

    def _settle_after_wait(self, page: dict) -> None:
        """A 'wait' choice means the screen reported itself unsettled. Upstream's wait sleeps only
        0.1s, too short for a page that mounts content seconds after its initial load (e.g. a SPA
        that injects a cross-origin iframe once the shell has rendered). Poll within a bounded
        window and return once NEW content has appeared and settled — i.e. the fingerprint has
        changed from the wait-moment and then held still for WAIT_STABLE polls. Returning on the
        first change was too eager: a churny shell changes every frame while the real content has
        not arrived yet, so we wait for stability; a static screen waits the full window so a late
        mount still has time, rather than the run giving up after MAX_WAITS consecutive waits."""
        start = time.monotonic()
        deadline = start + WAIT_SETTLE
        origin = page.get("fingerprint")
        prev, stable = origin, 0
        while time.monotonic() < deadline:
            time.sleep(WAIT_POLL)
            try:
                fp = self._read().get("fingerprint")
            except StalePage:
                return
            if fp == prev:
                stable += 1
                if stable >= WAIT_STABLE and fp != origin:
                    return  # new content appeared and has settled
                if fp == origin and time.monotonic() - start >= WAIT_STATIC and not self.page.get("loading"):
                    return  # nothing has changed at all: don't spend the whole window on it
            else:
                prev, stable = fp, 0

    def _no_progress(self) -> bool:
        last = self.history[-NO_PROGRESS:]
        return len(last) == NO_PROGRESS and all(
            h["page_changed"] is False and h["kind"] not in ("wait", "fill") for h in last)
        # typing is progress even when the screen can't show it (password fields are blanked)

    def _observe(self) -> dict:
        page = self.page
        refresh = getattr(self.target, "refresh", None)
        if page and refresh is not None:
            page = self._timed("observe_ms", refresh, page)  # one read: no separate freshness check
        elif not page or not self._timed("observe_ms", self.target.fresh, page):
            page = self._read()
        for i in range(UNREADABLE_TRIES - 1):
            if page.get("readable", True):
                break
            time.sleep(0.5)
            page = self._read()
        if not page.get("readable", True):
            self.page = page
            raise _Stop("blocked", "screen_unreadable")
        self.page = page
        self._obs_log.append((page.get("url", ""), page.get("text") or ""))
        del self._obs_log[:-4]
        return page

    def _read(self) -> dict:
        return self._timed("observe_ms", self.target.observe)

    def _timed(self, key: str, fn, *args, **kwargs):
        """Run fn, adding its wall-clock to self.timing[key] (reported as `timing` in results)."""
        t0 = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            self.timing[key] = self.timing.get(key, 0.0) + (time.perf_counter() - t0) * 1000

    def _money_text(self, page: dict) -> str:
        """Scrubbed text of the current screen plus any just-seen text of the same screen: a
        confirmation field (e.g. the recipient IBAN) can scroll out of view when the keyboard opens."""
        where = page.get("url", "")
        parts = [page.get("text") or ""]
        for w, t in self._obs_log:
            if w == where and t not in parts:
                parts.append(t)
        return scrub_text("\n".join(parts), self.secrets)

    def _bring_named(self, view: dict) -> dict | None:
        """The off-screen offer whose title the goal quotes exactly, while nothing on screen carries
        that label. A quoted title is a fact, not a preference: a workflow with a "Go To 4 Hour Wait"
        node kept outscoring the quoted "WAIT 4 hours" at the model, so this is decided here."""
        offers = [a for a in view.get("actions") or [] if a.get("pan_to")]
        if not offers:
            return None
        norm = lambda text: " ".join((text or "").split()).casefold()
        on_screen = set()
        for a in view["actions"]:
            if a.get("kind") in ("click", "fill", "select"):
                on_screen.add(norm(a.get("raw_label") or (a.get("label") or "").split(" — ")[0]))
                on_screen.add(norm(a.get("label")))  # a toggle's synthetic "switch — <row>" is its name too
        for quoted in _QUOTED.findall(self.goal or ""):
            want = norm(quoted)
            if not want or want in on_screen:
                continue
            for a in offers:
                title = (a.get("label") or "")[len("Bring "):-len(" into view")]
                if norm(title) == want:
                    return a
        return None

    def _quoted_click(self, view: dict) -> dict | None:
        """The control whose label the goal quotes after a click verb ("click the 'Unit: minutes'
        dropdown"), the first such phrase not yet clicked in this run, when exactly one control on
        screen carries that label. A negated verb ("do not click 'Save'") never matches. Seen: a
        select offered as "Unit: minutes" lost four times to the kebab beside it, "button — minutes"."""
        phrases = []
        for m in _CLICK_QUOTED.finditer(self.goal or ""):
            before = (self.goal or "")[max(0, m.start() - 30):m.start()]
            if _NEGATED.search(before):
                continue
            phrases.append(" ".join(m.group(1).split()).casefold())
        if not phrases:
            return None
        norm = lambda text: " ".join((text or "").split()).casefold()
        done = {norm(h.get("action")) for h in self.history}
        done |= {norm(h.get("raw_label")) for h in self.history if h.get("raw_label")}
        done |= {short for short, full in self._quoted_as.items() if full in done}
        for want in phrases:
            if want in done:
                continue
            hits = [a for a in view.get("actions") or [] if a.get("kind") == "click"
                    and want in (norm(a.get("label")), norm(a.get("raw_label")))]
            if len(hits) == 1:
                return hits[0]
            if not hits:
                # The goal names the control in full elsewhere ("Bring 'WAIT 24 hour — 1205' into
                # view, then click 'WAIT 24 hour'"): on screen next to its same-titled neighbours
                # it carries that full name, and the short one matches none of them. Seen: three
                # such goals each opened the top card.
                full = set()
                for m in _QUOTED.finditer(self.goal or ""):
                    q = norm(m.group(1))
                    if q != want and q.startswith(want) and not _NEGATED.search(
                            (self.goal or "")[max(0, m.start() - 30):m.start()]):
                        full.add(q)
                hits = [a for a in view.get("actions") or [] if a.get("kind") == "click"
                        and norm(a.get("label")) in full]
                if len(hits) == 1:
                    self._quoted_as[want] = norm(hits[0].get("label"))
                    return hits[0]
            return None  # not on screen yet, or ambiguous: the model's turn
        return None

    def _once_done(self, decision: dict, view: dict) -> bool:
        """True when the model wants to click a quoted label again that the goal says to click
        "once" and this run already clicked. Seen: a group switch clicked three times in one run,
        the model re-clicking because the group still read partly on after the first click."""
        goal = self.goal or ""
        if not re.search(r"\bonce\b", goal, re.I):
            return False
        choice = decision.get("choice")
        norm = lambda text: " ".join((text or "").split()).casefold()
        done = {norm(h.get("action")) for h in self.history} | {norm(h.get("raw_label")) for h in self.history}
        done |= {short for short, full in self._quoted_as.items() if full in done}
        quoted = set()
        for m in _CLICK_QUOTED.finditer(goal):
            if not _NEGATED.search(goal[max(0, m.start() - 30):m.start()]):
                quoted.add(norm(m.group(1)))
        if choice == "BLOCKED":
            # every click the goal asked for once has happened: there is nothing left to do, so the
            # model's "no supported operation can progress" is the end, not a failure (seen: a card
            # clicked and its panel open, reported blocked)
            return bool(quoted) and quoted <= done
        act = next((a for a in view.get("actions") or []
                    if a.get("id") == choice and a.get("kind") == "click"), None)
        if act is None:
            return False
        labels = {norm(act.get("label")), norm(act.get("raw_label"))} - {""}
        labels |= {short for short, full in self._quoted_as.items() if full in labels}  # its short quoted name too
        if not (labels & quoted):
            return False
        return bool(labels & done)

    def _quoted_disabled(self, view: dict) -> str | None:
        """A control the goal says to click that the app has disabled on this screen (greyed out,
        aria-disabled): its name as the target reports it under page["disabled"], when no control
        on screen carries that label. Seen: a permission checkbox the app greys out cost three runs
        that each ended BLOCKED with nothing said about why."""
        names = {" ".join(d.split()).casefold(): d for d in view.get("disabled") or [] if isinstance(d, str)}
        if not names:
            return None
        norm = lambda text: " ".join((text or "").split()).casefold()
        offered = {norm(a.get("label")) for a in view.get("actions") or [] if a.get("kind") == "click"}
        offered |= {norm(a.get("raw_label")) for a in view.get("actions") or [] if a.get("raw_label")}
        for m in _CLICK_QUOTED.finditer(self.goal or ""):
            if _NEGATED.search((self.goal or "")[max(0, m.start() - 30):m.start()]):
                continue
            want = norm(m.group(1))
            if want in names and want not in offered:
                return names[want]
        return None

    def _negated_labels(self) -> set:
        """Control names the goal rules out: every word sequence inside a negated clause ("Do not
        click Delete or Publish" rules out "delete" and "publish")."""
        norm = lambda text: " ".join((text or "").split()).casefold()
        clauses = [norm(m.group(1)) for m in _NEG_CLAUSE.finditer(self.goal or "")]
        return {c for c in clauses if c}

    def _ruled_out(self, label: str, clauses: set) -> bool:
        want = " ".join((label or "").split()).casefold()
        if len(want) < 3:
            return False
        return any(re.search(r"(?<![\w])" + re.escape(want) + r"(?![\w])", c) for c in clauses)

    def _drop_ruled_out(self, view: dict) -> dict:
        """The model never sees a control the goal rules out ("Do not click Delete or Publish"):
        a wrong pick there is the costly kind. Seen: a goal that named its one click was followed
        by a model click on the workflow's Save, which only an issues dialog stopped."""
        clauses = self._negated_labels()
        if not clauses:
            return view
        kept, dropped = [], []
        for a in view.get("actions") or []:
            if a.get("kind") in ("click", "fill", "select") and (
                    self._ruled_out(a.get("label"), clauses) or self._ruled_out(a.get("raw_label"), clauses)):
                dropped.append(a.get("label"))
            else:
                kept.append(a)
        if dropped:
            _log.info("session=%s %d control(s) the goal rules out kept from the model: %s",
                      self.session_id, len(dropped), dropped[:5])
            return {**view, "actions": kept}
        return view

    def _named_done(self, decision: dict, view: dict) -> str | None:
        """The label of a control the model wants to act on that the goal never names, after every
        click the goal quoted has happened: the goal's work is done and the model is reaching
        beyond it. Seen: "Click the 'Save action' button" followed by a model click on the
        workflow's own Save. None when the goal quotes no click, or one is still pending."""
        goal = self.goal or ""
        norm = lambda text: " ".join((text or "").split()).casefold()
        quoted = set()
        for m in _CLICK_QUOTED.finditer(goal):
            if not _NEGATED.search(goal[max(0, m.start() - 30):m.start()]):
                quoted.add(norm(m.group(1)))
        if not quoted:
            return None
        done = {norm(h.get("action")) for h in self.history} | {norm(h.get("raw_label")) for h in self.history}
        done |= {short for short, full in self._quoted_as.items() if full in done}
        if not quoted <= done:
            return None
        act = next((a for a in view.get("actions") or [] if a.get("id") == decision.get("choice")
                    and a.get("kind") in ("click", "fill", "select")), None)
        if act is None:
            return None
        clean = norm(_NEG_CLAUSE.sub(" ", goal))  # mentions inside a ruled-out clause do not count
        for label in (act.get("label"), act.get("raw_label")):
            want = norm(label)
            if want and len(want) >= 3 and re.search(r"(?<![\w])" + re.escape(want) + r"(?![\w])", clean):
                return None  # the goal names it: allowed
        return act.get("label") or act.get("raw_label") or act.get("id")

    def _keyboard_in_the_way(self, view: dict) -> dict | None:
        """The on-screen keyboard's own dismiss step, without the model: when the goal says to hide
        the keyboard and it is still up, or when the goal names a control to click that is not on
        screen while the keyboard is (the IME covers the bottom of the form, where Continuar sits).
        Seen: five "Hide the keyboard, then tap Continuar" goals ended BLOCKED at step 0; split
        into "hide the keyboard" and "tap Continuar" each ran fine."""
        hide = next((a for a in view.get("actions") or [] if a.get("kind") == "hide_keyboard"), None)
        if hide is None:
            return None
        if self.history and self.history[-1].get("kind") == "hide_keyboard":
            return None  # just done: the keyboard that is still up is not this step's problem
        if _HIDE_KEYBOARD.search(self.goal or ""):
            return hide
        norm = lambda text: " ".join((text or "").split()).casefold()
        on_screen = {norm(a.get("label")) for a in view["actions"] if a.get("kind") == "click"}
        on_screen |= {norm(a.get("raw_label")) for a in view["actions"] if a.get("raw_label")}
        done = {norm(h.get("action")) for h in self.history}
        for m in _CLICK_QUOTED.finditer(self.goal or ""):
            if _NEGATED.search((self.goal or "")[max(0, m.start() - 30):m.start()]):
                continue
            want = norm(m.group(1))
            if want and want not in done and want not in on_screen:
                return hide  # the named control is somewhere under the keyboard
        return None

    def _key_named(self, view: dict) -> dict | None:
        """The Escape press when the goal says to press Escape and this run has not yet: a modal
        that offers no close control is dismissed by the runner itself, no model involved."""
        m = _PRESS_ESCAPE.search(self.goal or "")
        if m is None or _NEGATED.search((self.goal or "")[max(0, m.start() - 30):m.start()]):
            return None
        if any(h.get("action") == "Press Escape" for h in self.history):
            return None
        return next((a for a in view.get("actions") or [] if a.get("kind") == "key"
                     and a.get("key") == "Escape"), None)

    def _decide(self, view: dict) -> dict:
        view = self._drop_ruled_out(view)
        if self._text_down and self._goal_value() is None:
            kept = [a for a in view["actions"] if a.get("kind") != "fill"]
            if len(kept) < len(view["actions"]):
                _log.info("session=%s text model down: %d fill(s) withheld from the model",
                          self.session_id, len(view["actions"]) - len(kept))
            view = {**view, "actions": kept}
        named = (self._bring_named(view) or self._quoted_click(view) or self._key_named(view)
                 or self._keyboard_in_the_way(view))
        if named is not None:  # no model call: the goal names it outright
            _log.info("session=%s goal names the control: %s", self.session_id, named["label"])
            op = named["id"].upper()
            return {"choice": named["id"], "operation": op, "target": None, "confidence": 1.0,
                    "probabilities": {named["id"]: 1.0}, "operation_probabilities": {op: 1.0},
                    "shortcut": "goal_named"}
        off = self._quoted_disabled(view)
        if off is not None:  # the app will not take the click: say so instead of asking the model
            _log.info("session=%s goal names a control the app has disabled: %s", self.session_id, off)
            self.last_blocked = {**self._blocked_detail({}, view), "control": off, "why": "control_disabled"}
            raise _Stop("blocked", "control_disabled")
        self.model_calls += 1
        view = {**view, "actions": [a for a in view["actions"] if a.get("kind") != "key"]}  # runner-only
        capped = cap_for_model(trim_bring_into_view(view["actions"], self.goal), self.goal,
                               text=view.get("text") or "")
        if len(capped) < len(view["actions"]):
            _log.info("session=%s view trimmed for the model: %d of %d controls kept",
                      self.session_id, len(capped), len(view["actions"]))
            view = {**view, "actions": capped}
        decision = self._timed("decide_ms", self._choose, view, self.goal, self.history)
        beyond = self._named_done(decision, view)
        if beyond is not None:
            _log.info("session=%s goal's named clicks are done; refusing a further act on %r",
                      self.session_id, beyond)
            self.last_blocked = {**self._blocked_detail(decision, view), "refused": beyond,
                                 "why": "named_clicks_done"}
            decision = {"choice": "DONE", "operation": "DONE", "target": None, "confidence": 1.0,
                        "probabilities": {"DONE": 1.0}, "operation_probabilities": {"DONE": 1.0},
                        "shortcut": "named_done", "reason": "named_clicks_done"}
        if self._once_done(decision, view):
            _log.info("session=%s goal says once and it was clicked: done", self.session_id)
            decision = {"choice": "DONE", "operation": "DONE", "target": None, "confidence": 1.0,
                        "probabilities": {"DONE": 1.0}, "operation_probabilities": {"DONE": 1.0},
                        "shortcut": "once"}
        if os.environ.get("JEV_DEBUG_IFRAME"):
            probs = decision.get("probabilities", {}) or {}
            top = dict(sorted(probs.items(), key=lambda kv: -kv[1])[:4])
            _log.info("decide: op=%s choice=%s target=%s top_probs=%s",
                      decision.get("operation"), decision.get("choice"),
                      decision.get("target"), top)
        return decision

    # --- replay: recorded steps stand in for `choose`; every guard still applies ---
    def _match_flow(self, page: dict) -> tuple[int, dict, dict] | None:
        """First recorded step in the window whose screen matches, if it can be replayed."""
        if self.flow is None:
            return None
        steps = self.flow.steps
        window = RESYNC_WINDOW if self._resyncing else 1
        for j in range(self.flow_i, min(self.flow_i + window, len(steps))):
            step = steps[j]
            if not matches(step["signature"], page):
                continue
            action = locate(step["locator"], page)
            if action is None or not self._replayable(step, action):
                return None  # this screen is the expected one but differs: let Jev decide
            self._resyncing = False
            return j, step, action
        return None

    def _replayable(self, step: dict, action: dict) -> bool:
        kind = step["locator"].get("kind")
        if kind is not None and action.get("kind") != kind:
            return False
        if action.get("kind") != "fill":
            return True
        value = step.get("value") or ""
        if value.startswith("param:"):
            return value[len("param:"):] in (self.params or {})
        if value.startswith("literal:"):
            return self._literal_current(value[len("literal:"):])
        return value == "secret"

    def _literal_current(self, text: str) -> bool:
        """A recorded literal with a digit run absent from this run's goal/params is stale."""
        known = self.goal + " " + " ".join(str(v) for v in (self.params or {}).values())
        return all(run in known for run in re.findall(r"\d{3,}", text))

    def _replay(self, j: int, step: dict, action: dict, page: dict):
        self.flow_i = j + 1  # a pause (secret/money) consumes the step; resume executes it
        if step.get("value") == "secret" and action.get("kind") == "fill":
            try:
                if self._fill_from_vault(action, page):
                    return
            except StalePage:
                self.flow_i = j  # refused before typing: retry this step on the fresh screen
                raise
            self.pending = self._pending_for("secret", action, page,
                                             field=scrub_text(action["label"], self.secrets))
            raise _Stop("paused_needs_input")
        try:
            self._dispatch(action, page, lambda: self._step_text(step))
        except StalePage:
            self.flow_i = j  # refused before acting: retry this step on the fresh screen
            raise

    def _step_text(self, step: dict) -> str:
        value = step["value"]
        if value.startswith("param:"):
            return str(self.params[value[len("param:"):]])
        return value[len("literal:"):]

    def _money_params(self) -> dict:
        keys = set(MONEY_KEYS) | set(self.money_params)
        return {k: v for k, v in (self.params or {}).items() if k in keys}

    def _guard_money(self, action: dict, page: dict, money_params: dict):
        """Tap autonomously only on an exact on-screen match; otherwise pause."""
        text = self._money_text(page)
        check = check_money(text, money_params, self.cfg.own_accounts)
        if money_params and check.ok and not self.money_attempted:
            self._execute(action, page, None, verify=True, money=True)
            return
        page, action = self._settled(page, action)  # the human must confirm a screen that holds still
        text = self._money_text(page)
        check = check_money(text, money_params, self.cfg.own_accounts)
        self.pending = self._pending_for(
            "money", action, page, text_hash=self._text_hash(page),
            expected=scrub_value(check.expected, self.secrets),
            seen=scrub_value(check.seen, self.secrets))
        raise _Stop("paused_mismatch", "money_already_attempted" if self.money_attempted else None)

    def _guard_destructive(self, action: dict, page: dict):
        """Never delete on Jev's say-so: pause on a settled screen for resume(confirm=True).
        Every destructive click pauses (a delete plus its confirm dialog is two confirms).
        Its own status, not paused_mismatch, so a standing money approval never covers a delete."""
        page, action = self._settled(page, action)
        self.pending = self._pending_for("destructive", action, page,
                                         text_hash=self._text_hash(page),
                                         control=scrub_text(action.get("label") or "", self.secrets))
        raise _Stop("paused_confirm", "destructive_already_attempted"
                    if self.destructive_attempted else "destructive_action")

    def _changed(self, before: dict, after: dict) -> bool:
        return (after.get("fingerprint") != before.get("fingerprint")
                or self._text_hash(after) != self._text_hash(before))

    def _settled(self, page: dict, action: dict, tries: int = 4) -> tuple[dict, dict]:
        """Re-read until two reads agree, so a pause never records a screen still animating."""
        for _ in range(tries):
            time.sleep(self.settle_delay)
            again = self._read()
            if (again.get("fingerprint") == page.get("fingerprint")
                    and self._text_hash(again) == self._text_hash(page)):
                break
            found = [a for a in find_by_locator(again, locator_of(action))
                     if a.get("label") == action.get("label")]
            if len(found) != 1:
                break  # the button moved or multiplied: keep the screen the decision was made on
            page, action = again, found[0]
        self.page = page
        return page, action

    def _save_receipt(self, page: dict):
        text = scrub(page, self.secrets).get("text") or ""
        m = REFERENCE_RE.search(text)
        ref = m.group(3) if m else None
        path = None
        try:
            folder = self.cfg.data_dir / "receipts"
            folder.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            path = str(self.target.screenshot(folder / f"{self.session_id}-{ts}.png"))
        except Exception:  # a failed screenshot must not turn a completed transfer into an error
            path = None
        self.receipt = {"path": path, "reference": ref}

    def _act_and_record(self, decision: dict, page: dict):
        choice = decision["choice"]
        if choice == "DONE":
            if self.executed_money and self.receipt is None:
                self._save_receipt(page)
            if decision.get("shortcut") in ("once", "named_done"):
                # The runner itself did every click the goal named and ended the run: that is the
                # goal, by construction. Seen: "Click 'Transferir' once. Nothing else." came back
                # "unverified: not a completed transfer" from the text model and was retried.
                self.verified, self._verify_reason = True, "runner_named"
                raise _Stop("done", decision.get("reason"))
            if self._check(page) is False:
                self.last_blocked = self._blocked_detail(decision, page)  # what Jev called done
                raise _Stop("unverified", self._verify_reason or "goal_not_visible")
            raise _Stop("done", decision.get("reason"))
        if choice == "BLOCKED":
            if self._check(page) is True:  # Jev gave up, but the goal is visibly met
                raise _Stop("done", "verified_after_blocked")
            self.last_blocked = self._blocked_detail(decision, page)
            if self._text_down:  # nothing to click towards the goal once typing was taken away
                self.last_blocked["text_model"] = self._text_down
                raise _Stop("blocked", "text_model_unavailable")
            raise _Stop("blocked", "jev_blocked")
        action = next((a for a in page["actions"] if a["id"] == choice), None)
        if action is None:
            raise _Stop("error", "unknown_action")
        if self._looping(page, action):
            self.loop_events += 1
            raise _Stop("blocked", "stuck_in_loop")
        self._jev_steps += 1
        self._dispatch(action, page, lambda: self._model_text(action, page))

    def _looped_locators(self, page: dict) -> set:
        """Locators of real actions already taken LOOP_DROP times on this exact screen — dropped
        from the next view so Jev tries something else before the hard stop trips."""
        fp = page.get("fingerprint")
        if fp is None:
            return set()
        coarse = coarse_fingerprint(page)
        counts: dict = {}
        for h in self.history:
            # only true oscillation (the action changed the screen, yet we're back on it); a frozen
            # screen is page_changed False and belongs to _no_progress, not here.
            if (not _same_screen(h, fp, coarse) or h.get("kind") in ("wait", "scroll")
                    or h.get("page_changed") is not True):
                continue
            key = tuple(sorted((h.get("locator") or {}).items()))
            counts[key] = counts.get(key, 0) + 1
        return {k for k, n in counts.items() if n >= LOOP_DROP}

    def _looping(self, page: dict, action: dict) -> bool:
        """True when this exact action has already been taken LOOP_LIMIT times on this exact screen
        (same fingerprint + same locator) — an oscillation the no-progress check can't see, because
        each step does change the screen. Scroll/wait are paced by their own caps, not here."""
        fp = page.get("fingerprint")
        if fp is None or action.get("kind") in ("scroll", "wait"):
            return False
        coarse = coarse_fingerprint(page)
        key = tuple(sorted(locator_of(action).items()))
        seen = sum(1 for h in self.history
                   if _same_screen(h, fp, coarse) and h.get("page_changed") is True
                   and tuple(sorted((h.get("locator") or {}).items())) == key)
        return seen >= LOOP_LIMIT

    def _met_after_all(self) -> bool:
        """After a run died on stale strikes, whether the goal is visibly met on a fresh read of
        the screen (only when something was acted on; a run that never acted cannot have won)."""
        if not self.history:
            return False
        try:
            page = self.target.observe()
        except Exception:
            return False
        try:
            return self._check(page) is True
        except Exception:
            return False

    def _check(self, page: dict) -> bool | None:
        """Second opinion on Jev's verdict from the text model; None when unavailable."""
        check = self._verify or verify_mod.check
        ok, why = self._timed("verify_ms", check, self.goal, scrub(page, self.secrets),
                              self.history)  # text model: not in model_calls
        self.verified = ok
        self._verify_reason = scrub({"text": why}, self.secrets)["text"][:200] if why else ""
        return ok

    def _dispatch(self, action: dict, page: dict, text_for):
        """Shared by Jev-chosen and replayed actions: secret, money and destructive guards first."""
        if self._fill_username_from_vault(action, page):
            return
        if self._is_secret(action):
            if self._fill_from_vault(action, page):
                return
            # Secrets never go to the text model: ask the human instead.
            self.pending = self._pending_for("secret", action, page,
                                             field=scrub_text(action["label"], self.secrets))
            raise _Stop("paused_needs_input")
        money_params = self._money_params()
        if is_money_action(action, page, money_params, self.cfg):
            self._guard_money(action, page, money_params)
            return
        if is_destructive_action(action, self.cfg):
            self._guard_destructive(action, page)
        text = text_for() if action["kind"] == "fill" else None
        if is_send_action(action, self.cfg):
            # One send per run, and never the tap-retry path: a forward returns to a
            # similar-looking screen, so re-pressing an "unchanged" send duplicates it.
            if self.sent_attempted:
                raise _Stop("blocked", "already_sent")
            self._execute(action, page, text, send=True, verify=True)
            return
        self._execute(action, page, text)

    @staticmethod
    def _field_keys(action: dict) -> list:
        rid = action.get("resource_id")
        keys = [("rid", rid)] if rid else []
        return keys + [("loc", repr(sorted(locator_of(action).items())))]

    def _is_secret(self, action: dict) -> bool:
        """Sticky secret detection: once a field took a secret it stays secret.

        Android shows an EditText's current text as its label, so after a secret is typed
        into a plain field the label *is* the secret and label patterns no longer match."""
        if action.get("kind") != "fill":
            return False
        if is_secret_field(action, self.cfg.secret_patterns):
            return True
        if any(scrub_value(action.get(k), self.secrets) != action.get(k)
               for k in ("label", "hint", "value")):
            return True
        return any(k in self._secret_fields for k in self._field_keys(action))

    def _goal_value(self) -> str | None:
        """The value the goal spells out for the next field ("select all and type 3", "enter
        'Follow-up'"): the first such phrase not yet typed in this run, when it is the only one
        left. The text model is for values the goal leaves to be worked out; a value written in
        the goal needs no model (seen: a typing step stopped on a text-model HTTP 402 although the
        goal said exactly what to type). Reached only after the secret and money guards."""
        values = []
        for m in _TYPE_QUOTED.finditer(self.goal or ""):
            if _NEGATED.search((self.goal or "")[max(0, m.start() - 30):m.start()]):
                continue
            value = (m.group(1) if m.group(1) is not None else m.group(2) or "").strip()
            if value and value not in self._goal_typed and value not in values:
                values.append(value)
        return values[0] if len(values) == 1 else None

    def _model_text(self, action: dict, page: dict) -> str:
        spelled = self._goal_value()
        if spelled is not None:
            _log.info("session=%s goal spells out the value to type: %d chars", self.session_id, len(spelled))
            self._goal_typed.add(spelled)
            return spelled
        ctx = model.field_context(self.goal, scrub_value(action, self.secrets),
                                  scrub(page, self.secrets), self.history)
        try:
            text, _ = self._timed("text_ms", self._field_text, ctx)
        except ValueError:
            raise _Stop("blocked", "missing_value")
        except RuntimeError as e:
            # The text provider is down or refusing (a connection failure, an HTTP 402 on its
            # account): a value nobody can produce must not end the run while the page has other
            # controls. The fill is dropped from the next view; the run goes on by clicking.
            self._text_down = (str(e) or type(e).__name__)[:160]
            _log.warning("session=%s text model unavailable (%s): giving up the fill %r, "
                         "typing is off for the rest of this run", self.session_id,
                         self._text_down, action.get("label"))
            raise _NoText() from None
        return text

    def _execute(self, action: dict, page: dict, text: str | None, *,
                 verify: bool = False, secret: bool = False, money: bool = False,
                 send: bool = False, destructive: bool = False):
        if send:
            self.sent_attempted = True  # mark before acting: a duplicate must never slip in
        if destructive:
            self.destructive_attempted = True
        if secret and text:
            self.secrets.append(text)  # before acting, so even a target error is scrubbed
        if money:
            before, self.money_attempted = self.money_attempted, True
            try:
                self._timed("act_ms", self.target.act, action, page, text=text, verify=True)
            except StalePage:
                self.money_attempted = before  # refused before tapping: nothing moved
                raise
            except BaseException as e:  # outcome unknown: the money may have moved
                self.pending, self._money_uncertain = None, True
                self.status, self.reason = "error", "money_action_uncertain"
                if not isinstance(e, Exception):
                    raise  # cancellation/interrupt must propagate, but the session stays terminal
                raise _Stop("error", "money_action_uncertain") from e
            self.executed_money = True
            self.pending = None  # tapped: a crash below must not leave a confirmable pending
        elif verify:
            self._timed("act_ms", self.target.act, action, page, text=text, verify=True)
        else:
            self._timed("act_ms", self.target.act, action, page, text=text)
        # From here the action has executed: nothing below may trigger the pre-act retry path.
        self._stale = 0
        entry = history_entry(scrub_value(action, self.secrets), scrub(page, self.secrets), text,
                              self.cfg.secret_patterns)
        if text is not None and (secret or entry["text"] == "•••"):
            entry["text"] = "•••"
            if text not in self.secrets:
                self.secrets.append(text)
            self._secret_fields.update(self._field_keys(action))
        entry["value_source"] = value_source(text, self.params, secret=entry["text"] == "•••")
        self.history.append(entry)
        if action.get("kind") == "wait":
            self._settle_after_wait(page)  # let late async content (e.g. a mounting iframe) appear
        try:
            self.page = after = self._read()
        except StalePage:
            self.page = {}
            return
        if not money and not send and not destructive and not self._changed(page, after) \
                and may_retry_tap(action, self.cfg) and hasattr(self.target, "press"):
            # Some web views ignore a quick tap; look again, then press and hold briefly once.
            time.sleep(self.settle_delay * 2)
            after = self.page = self._read()
            if not self._changed(page, after):
                self._timed("act_ms", self.target.press, action)
                entry["pressed"] = True
                after = self.page = self._read()
        entry["page_changed"] = after.get("fingerprint") != page.get("fingerprint")
        entry["coarse"] = coarse_fingerprint(page)  # loop guards: same screen even when text churns
