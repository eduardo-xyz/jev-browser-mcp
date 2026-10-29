"""Runner: per tick observe -> scrub -> decide -> act -> record."""
import time

from jev_ultrafast import model

from . import result
from .config import Config
from .guards import scrub
from .targets.base import DeviceDisconnected, StalePage, Target, TargetBlocked

UNREADABLE_TRIES = 3
NO_PROGRESS = 3


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


def history_entry(action: dict, page: dict, text: str | None) -> dict:
    """Replay-ready record of one executed action (Task 5 fills value_source)."""
    if "node" in action:
        locator = {k: action.get(k) for k in ("kind", "resource_id", "label", "role")}
    else:
        locator = {"control": action["id"]}
    return {
        "action": action["label"],
        "kind": action["kind"],
        "text": text,
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
        self.executed_money = False
        self.receipt: dict | None = None
        self._started = time.perf_counter()
        self._stale = 0

    def run(self) -> dict:
        self.status, self.reason = "running", None
        try:
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
        self._touch()
        return result.build(self)

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
            self._stale = 0
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

    def _act_and_record(self, decision: dict, page: dict):
        choice = decision["choice"]
        if choice == "DONE":
            raise _Stop("done")
        if choice == "BLOCKED":
            raise _Stop("blocked", "jev_blocked")
        action = next((a for a in page["actions"] if a["id"] == choice), None)
        if action is None:
            raise _Stop("error", "unknown_action")
        text = None
        if action["kind"] == "fill":
            try:
                ctx = model.field_context(self.goal, action, scrub(page, self.secrets), self.history)
                text, _ = self._field_text(ctx)
            except ValueError:
                raise _Stop("blocked", "missing_value")
        self.target.act(action, page, text=text)
        entry = history_entry(action, page, text)
        self.history.append(entry)
        self.page = after = self.target.observe()
        entry["page_changed"] = after.get("fingerprint") != page.get("fingerprint")
