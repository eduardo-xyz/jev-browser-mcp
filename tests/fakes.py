"""Offline fakes: a scripted Target and a scripted Jev `choose`."""
import itertools
import json
from pathlib import Path

from jev_mcp.targets.base import StalePage  # noqa: F401

_seq = itertools.count()


def fill(id, label, rid=None):
    a = {"id": id, "node": id, "kind": "fill", "label": label, "role": "textbox", "value": ""}
    if rid is not None:
        a["resource_id"] = rid
    return a


def click(id, label, rid=None):
    a = {"id": id, "node": id, "kind": "click", "label": label, "role": "button"}
    if rid is not None:
        a["resource_id"] = rid
    return a


def page(*actions, url="app://main", readable=True, text=""):
    labels = ",".join(a["label"] for a in actions)
    return {"url": url, "title": "t", "text": text, "actions": list(actions),
            "fingerprint": f"{url}|{labels}|{next(_seq)}", "readable": readable}


def unreadable():
    return page(readable=False)


class FakeTarget:
    kind = "android"

    def __init__(self, pages, stuck_ids=None, advance_on_observe=0, raise_on_act=None):
        self.pages, self.i = pages, 0
        self.stuck_ids = set(stuck_ids or ())
        self.advance_on_observe = advance_on_observe
        self.raise_on_act = raise_on_act
        self.acted, self.verified = [], []

    def observe(self):
        p = self.pages[self.i]
        if self.advance_on_observe > 0:
            self.advance_on_observe -= 1
            self._advance()
        return p

    def _advance(self):
        self.i = min(self.i + 1, len(self.pages) - 1)

    def fresh(self, page):
        return True

    def act(self, action, page, text=None, verify=False):
        if self.raise_on_act:
            raise self.raise_on_act
        self.acted.append((action["id"], text))
        if verify:
            self.verified.append(action["id"])
        if action["id"] not in self.stuck_ids:
            self._advance()

    def screenshot(self, path):
        Path(path).write_bytes(b"")
        return Path(path)

    def close(self):
        pass


def scripted_choose(choices, spy=None):
    it = iter(choices)

    def choose(page, goal, history):
        if spy is not None:
            spy.append(json.dumps(page))
        try:
            c = next(it)
        except StopIteration:
            raise AssertionError("scripted_choose exhausted")
        return {"choice": c, "operation": c, "target": None, "confidence": 1.0,
                "probabilities": {c: 1.0}, "latency_ms": 1, "usage": {}}

    return choose
