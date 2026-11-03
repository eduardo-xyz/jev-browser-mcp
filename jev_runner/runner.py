"""Runner: per tick observe -> scrub -> decide -> act -> record."""
import hashlib
import logging
import os
import re
import time

from jev_ultrafast import model

from . import result
from . import signin
from .vault import field_kind as vault_field_kind, scope_of as vault_scope
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


class _Stop(Exception):
    def __init__(self, status: str, reason: str | None = None):
        self.status, self.reason = status, reason


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
        except Exception as e:  # provider/target failures end the run, never escape
            msg = scrub({"text": str(e) or type(e).__name__}, self.secrets)["text"]
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
            raise _Stop("blocked", "no_progress")
        page = self._observe()
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
                if fp == origin and time.monotonic() - start >= WAIT_STATIC:
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

    def _decide(self, view: dict) -> dict:
        self.model_calls += 1
        decision = self._timed("decide_ms", self._choose, view, self.goal, self.history)
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
            if self._check(page) is False:
                raise _Stop("unverified", self._verify_reason or "goal_not_visible")
            raise _Stop("done")
        if choice == "BLOCKED":
            if self._check(page) is True:  # Jev gave up, but the goal is visibly met
                raise _Stop("done", "verified_after_blocked")
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

    def _model_text(self, action: dict, page: dict) -> str:
        ctx = model.field_context(self.goal, scrub_value(action, self.secrets),
                                  scrub(page, self.secrets), self.history)
        try:
            text, _ = self._timed("text_ms", self._field_text, ctx)
        except ValueError:
            raise _Stop("blocked", "missing_value")
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
