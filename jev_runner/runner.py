"""Runner: per tick observe -> scrub -> decide -> act -> record."""
import hashlib
import re
import time

from jev_ultrafast import model

from . import result
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
    is_money_action,
    is_secret_field,
    is_send_action,
    may_retry_tap,
    scrub,
    scrub_text,
    scrub_value,
)
from .targets.base import DeviceDisconnected, StalePage, Target, TargetBlocked

UNREADABLE_TRIES = 3
NO_PROGRESS = 3
MAX_WAITS = 3  # a churny SPA never looks "settled"; after this many waits, force a real decision
LOOP_LIMIT = 3  # same action on the same screen this many times = a cycle (e.g. open wrong account
#                 -> back -> open again); stop instead of churning to max_steps. A legit retry
#                 changes the screen (different fingerprint), so it is not counted.
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
                 learn: bool = True, choose=model.choose, field_text=model.field_text, verify=None):
        self.session_id, self.target, self.params, self.cfg = session_id, target, params, cfg
        self.goal = goal_text(goal, params)
        self._goal_raw = goal  # saved flows keep the goal without this run's values
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
        self.verified: bool | None = None  # independent check of the last DONE/BLOCKED
        self._verify_reason = ""
        self.status, self.reason = "running", None
        self.history: list[dict] = []
        self.page: dict = {}
        self.model_calls = 0
        self.elapsed_ms = 0
        self.secrets: list[str] = []
        self._secret_fields: set = set()  # locators of fields a secret was typed into
        self.pending: dict | None = None
        self.last_used = time.time()
        self.executed_money = False  # never reset once True: the receipt must survive
        self.money_attempted = False  # one autonomous money action per run; never reset
        self.sent_attempted = False  # one message/forward send per run; never reset
        self._money_uncertain = False  # terminal: the session must be ended
        self.receipt: dict | None = None
        self._started = time.perf_counter()
        self._stale = 0
        self._obs_log: list[tuple[str, str]] = []  # recent (where, text): a field can scroll off

    def run(self) -> dict:
        if self._money_uncertain:
            return result.build(self)
        if not self.status.startswith("paused"):
            self.pending = None
        return self._drive()

    def resume(self, input: str | None = None, instruction: str | None = None,
               confirm: bool = False) -> dict:
        """Continue a paused run. Never lets a secret value reach a result."""
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
        if pending["type"] == "secret" and not input:
            return result.build(self)
        if pending["type"] == "money" and not confirm:
            return result.build(self)
        return self._drive(lambda: self._resume_pending(pending, input))

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
        except StalePage:
            self.status, self.reason = "error", "stale_page"
        except Exception as e:  # provider/target failures end the run, never escape
            msg = scrub({"text": str(e) or type(e).__name__}, self.secrets)["text"]
            self.status, self.reason = "error", msg[:200]
        finally:
            self._touch()
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

    def _resume_pending(self, pending: dict, input: str | None):
        status = "paused_needs_input" if pending["type"] == "secret" else "paused_mismatch"
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
        if pending["type"] == "money":
            found = [a for a in found if a.get("label") == pending["label"]]
        if not found:
            raise _Stop(status, "pending_element_missing")
        if len(found) > 1:
            raise _Stop(status, "pending_element_ambiguous")
        action = found[0]
        if pending["type"] == "secret":
            self._execute(action, page, input, verify=True, secret=True)
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

    def _pending_for(self, kind: str, action: dict, page: dict, **extra) -> dict:
        return {"type": kind, "locator": locator_of(action), "action_id": action["id"],
                "label": action.get("label"), "fingerprint": page.get("fingerprint"), **extra}

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
        replay = self._match_flow(page)
        if replay is None and self.flow is not None:
            self._resyncing = True
        if replay:
            decision = None
        else:
            view = scrub(page, self.secrets)
            if self._consecutive_waits() >= MAX_WAITS:
                # Jev keeps stalling on a never-settling page: drop "wait" so it must act or stop.
                view = {**view, "actions": [a for a in view["actions"] if a.get("kind") != "wait"]}
            decision = self._decide(view)
        try:
            if replay:
                self._replay(*replay, page)
            else:
                self._act_and_record(decision, page)
        except StalePage:
            self._stale += 1
            self.page = {}
            if self._stale >= 3:
                raise

    def _consecutive_waits(self) -> int:
        n = 0
        for h in reversed(self.history):
            if h.get("kind") == "wait":
                n += 1
            else:
                break
        return n

    def _no_progress(self) -> bool:
        last = self.history[-NO_PROGRESS:]
        return len(last) == NO_PROGRESS and all(
            h["page_changed"] is False and h["kind"] not in ("wait", "fill") for h in last)
        # typing is progress even when the screen can't show it (password fields are blanked)

    def _observe(self) -> dict:
        page = self.page
        if not page or not self.target.fresh(page):
            page = self.target.observe()
        for i in range(UNREADABLE_TRIES - 1):
            if page.get("readable", True):
                break
            time.sleep(0.5)
            page = self.target.observe()
        if not page.get("readable", True):
            self.page = page
            raise _Stop("blocked", "screen_unreadable")
        self.page = page
        self._obs_log.append((page.get("url", ""), page.get("text") or ""))
        del self._obs_log[:-4]
        return page

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
        return self._choose(view, self.goal, self.history)

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

    def _changed(self, before: dict, after: dict) -> bool:
        return (after.get("fingerprint") != before.get("fingerprint")
                or self._text_hash(after) != self._text_hash(before))

    def _settled(self, page: dict, action: dict, tries: int = 4) -> tuple[dict, dict]:
        """Re-read until two reads agree, so a pause never records a screen still animating."""
        for _ in range(tries):
            time.sleep(self.settle_delay)
            again = self.target.observe()
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
            raise _Stop("blocked", "stuck_in_loop")
        self._jev_steps += 1
        self._dispatch(action, page, lambda: self._model_text(action, page))

    def _looping(self, page: dict, action: dict) -> bool:
        """True when this exact action has already been taken LOOP_LIMIT times on this exact screen
        (same fingerprint + same locator) — an oscillation the no-progress check can't see, because
        each step does change the screen. Scroll/wait are paced by their own caps, not here."""
        fp = page.get("fingerprint")
        if fp is None or action.get("kind") in ("scroll", "wait"):
            return False
        key = tuple(sorted(locator_of(action).items()))
        seen = sum(1 for h in self.history
                   if h.get("fingerprint") == fp
                   and tuple(sorted((h.get("locator") or {}).items())) == key)
        return seen >= LOOP_LIMIT

    def _check(self, page: dict) -> bool | None:
        """Second opinion on Jev's verdict from the text model; None when unavailable."""
        check = self._verify or verify_mod.check
        ok, why = check(self.goal, scrub(page, self.secrets), self.history)  # text model: not in model_calls
        self.verified = ok
        self._verify_reason = scrub({"text": why}, self.secrets)["text"][:200] if why else ""
        return ok

    def _dispatch(self, action: dict, page: dict, text_for):
        """Shared by Jev-chosen and replayed actions: secret and money guards first."""
        if self._is_secret(action):
            # Secrets never go to the text model: ask the human instead.
            self.pending = self._pending_for("secret", action, page,
                                             field=scrub_text(action["label"], self.secrets))
            raise _Stop("paused_needs_input")
        money_params = self._money_params()
        if is_money_action(action, page, money_params, self.cfg):
            self._guard_money(action, page, money_params)
            return
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
            text, _ = self._field_text(ctx)
        except ValueError:
            raise _Stop("blocked", "missing_value")
        return text

    def _execute(self, action: dict, page: dict, text: str | None, *,
                 verify: bool = False, secret: bool = False, money: bool = False,
                 send: bool = False):
        if send:
            self.sent_attempted = True  # mark before acting: a duplicate must never slip in
        if secret and text:
            self.secrets.append(text)  # before acting, so even a target error is scrubbed
        if money:
            before, self.money_attempted = self.money_attempted, True
            try:
                self.target.act(action, page, text=text, verify=True)
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
            self.target.act(action, page, text=text, verify=True)
        else:
            self.target.act(action, page, text=text)
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
        try:
            self.page = after = self.target.observe()
        except StalePage:
            self.page = {}
            return
        if not money and not send and not self._changed(page, after) \
                and may_retry_tap(action, self.cfg) and hasattr(self.target, "press"):
            # Some web views ignore a quick tap; look again, then press and hold briefly once.
            time.sleep(self.settle_delay * 2)
            after = self.page = self.target.observe()
            if not self._changed(page, after):
                self.target.press(action)
                entry["pressed"] = True
                after = self.page = self.target.observe()
        entry["page_changed"] = after.get("fingerprint") != page.get("fingerprint")
