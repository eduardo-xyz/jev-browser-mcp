"""Runner: per tick observe -> scrub -> decide -> act -> record."""
import hashlib
import re
import time

from jev_ultrafast import model

from . import result
from .config import Config
from .guards import (
    check_money,
    is_money_action,
    is_secret_field,
    scrub,
    scrub_text,
    scrub_value,
)
from .targets.base import DeviceDisconnected, StalePage, Target, TargetBlocked

UNREADABLE_TRIES = 3
NO_PROGRESS = 3
MONEY_KEYS = ("amount", "recipient")
ESCAPABLE = ("pending_element_missing", "pending_element_ambiguous")
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


def element_labels(page: dict) -> list[str]:
    return sorted(a["label"] for a in page.get("actions", []) if "node" in a)


def locator_of(action: dict) -> dict:
    if "node" in action:
        return {k: action.get(k) for k in ("kind", "resource_id", "label", "role")}
    return {"control": action["id"]}


def find_by_locator(page: dict, locator: dict) -> list[dict]:
    """All matches: resource_id when non-empty, else label+role+kind."""
    actions = page.get("actions", [])
    if "control" in locator:
        return [a for a in actions if a["id"] == locator["control"]]
    if locator.get("resource_id"):
        return [a for a in actions if "node" in a and a.get("resource_id") == locator["resource_id"]]
    return [a for a in actions if "node" in a and all(
        a.get(k) == locator.get(k) for k in ("label", "role", "kind"))]


def history_entry(action: dict, page: dict, text: str | None, secret_patterns: list[str] | None = None) -> dict:
    """Replay-ready record of one executed action (Task 5 fills value_source)."""
    locator = locator_of(action)
    return {
        "action": action["label"],
        "kind": action["kind"],
        "text": "•••" if text is not None and is_secret_field(action, secret_patterns or []) else text,
        "page_changed": None,
        "where": page["url"],
        "signature": {"where": page["url"], "labels": element_labels(page)},
        "locator": locator,
        "value_source": None,
    }


class _Stop(Exception):
    def __init__(self, status: str, reason: str | None = None):
        self.status, self.reason = status, reason


class Runner:
    def __init__(self, session_id: str, target: Target, goal: str, params: dict | None,
                 cfg: Config, *, max_steps: int = 40, money_params: list[str] | None = None,
                 flow=None, choose=model.choose, field_text=model.field_text):
        self.session_id, self.target, self.params, self.cfg = session_id, target, params, cfg
        self.goal = goal_text(goal, params)
        self.max_steps, self.money_params, self.flow = max_steps, money_params or [], flow
        self._choose, self._field_text = choose, field_text
        self.status, self.reason = "running", None
        self.history: list[dict] = []
        self.page: dict = {}
        self.model_calls = 0
        self.elapsed_ms = 0
        self.secrets: list[str] = []
        self.pending: dict | None = None
        self.last_used = time.time()
        self.executed_money = False  # never reset once True: the receipt must survive
        self.money_attempted = False  # one autonomous money action per run; never reset
        self._money_uncertain = False  # terminal: the session must be ended
        self.receipt: dict | None = None
        self._started = time.perf_counter()
        self._stale = 0

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
        return result.build(self)

    def _resume_pending(self, pending: dict, input: str | None):
        status = "paused_needs_input" if pending["type"] == "secret" else "paused_mismatch"
        self.page = {}  # force a real re-observe: the screen may have moved while paused
        page = self._observe()
        same_screen = page.get("fingerprint") == pending["fingerprint"]
        if same_screen:
            found = [a for a in page.get("actions", []) if a["id"] == pending["action_id"]]
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
        view = scrub(page, self.secrets)
        decision = self._decide(view)
        try:
            self._act_and_record(decision, page)
        except StalePage:
            self._stale += 1
            self.page = {}
            if self._stale >= 3:
                raise

    def _no_progress(self) -> bool:
        last = self.history[-NO_PROGRESS:]
        return len(last) == NO_PROGRESS and all(
            h["page_changed"] is False and h["kind"] != "wait" for h in last)

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
        return page

    def _decide(self, view: dict) -> dict:
        self.model_calls += 1
        return self._choose(view, self.goal, self.history)

    def _money_params(self) -> dict:
        keys = set(MONEY_KEYS) | set(self.money_params)
        return {k: v for k, v in (self.params or {}).items() if k in keys}

    def _guard_money(self, action: dict, page: dict, money_params: dict):
        """Tap autonomously only on an exact on-screen match; otherwise pause."""
        text = scrub_text(page.get("text") or "", self.secrets)
        check = check_money(text, money_params, self.cfg.own_accounts)
        if money_params and check.ok and not self.money_attempted:
            self._execute(action, page, None, verify=True, money=True)
            return
        self.pending = self._pending_for(
            "money", action, page, text_hash=self._text_hash(page),
            expected=scrub_value(check.expected, self.secrets),
            seen=scrub_value(check.seen, self.secrets))
        raise _Stop("paused_mismatch", "money_already_attempted" if self.money_attempted else None)

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
            if self.executed_money:
                self._save_receipt(page)
            raise _Stop("done")
        if choice == "BLOCKED":
            raise _Stop("blocked", "jev_blocked")
        action = next((a for a in page["actions"] if a["id"] == choice), None)
        if action is None:
            raise _Stop("error", "unknown_action")
        if is_secret_field(action, self.cfg.secret_patterns):
            # Secrets never go to the text model: ask the human instead.
            self.pending = self._pending_for("secret", action, page, field=action["label"])
            raise _Stop("paused_needs_input")
        money_params = self._money_params()
        if is_money_action(action, page, money_params, self.cfg):
            self._guard_money(action, page, money_params)
            return
        text = None
        if action["kind"] == "fill":
            ctx = model.field_context(self.goal, action, scrub(page, self.secrets), self.history)
            try:
                text, _ = self._field_text(ctx)
            except ValueError:
                raise _Stop("blocked", "missing_value")
        self._execute(action, page, text)

    def _execute(self, action: dict, page: dict, text: str | None, *,
                 verify: bool = False, secret: bool = False, money: bool = False):
        if secret and text:
            self.secrets.append(text)  # before acting, so even a target error is scrubbed
        if money:
            before, self.money_attempted = self.money_attempted, True
            try:
                self.target.act(action, page, text=text, verify=True)
            except StalePage:
                self.money_attempted = before  # refused before tapping: nothing moved
                raise
            except Exception:  # noqa: BLE001 - outcome unknown: the money may have moved
                self.pending, self._money_uncertain = None, True
                raise _Stop("error", "money_action_uncertain")
            self.executed_money = True
            self.pending = None  # tapped: a crash below must not leave a confirmable pending
        elif verify:
            self.target.act(action, page, text=text, verify=True)
        else:
            self.target.act(action, page, text=text)
        # From here the action has executed: nothing below may trigger the pre-act retry path.
        self._stale = 0
        entry = history_entry(action, page, text, self.cfg.secret_patterns)
        if text is not None and (secret or entry["text"] == "•••"):
            entry["text"] = "•••"
            if text not in self.secrets:
                self.secrets.append(text)
        self.history.append(entry)
        try:
            self.page = after = self.target.observe()
        except StalePage:
            self.page = {}
            return
        entry["page_changed"] = after.get("fingerprint") != page.get("fingerprint")
