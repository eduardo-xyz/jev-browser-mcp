"""Saved flows: record a finished run's steps, replay them without calling Jev."""
import json
import os
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

MATCH_THRESHOLD = 0.8
SECRET_MARK = "•••"


@dataclass
class Flow:
    name: str
    target: str
    start: str | None
    goal: str
    params: list[str] = field(default_factory=list)
    money_params: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    if not s:
        raise ValueError("flow name must contain letters or digits")
    return s


class FlowStore:
    """One JSON file per flow at `root/<slug>.json`."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, name: str) -> Path:
        return self.root / f"{_slug(name)}.json"

    def save(self, flow: Flow) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(flow.name)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(flow), ensure_ascii=False, indent=2))
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written flow
        return path

    def load(self, name: str) -> Flow:
        try:
            path = self._path(name)
        except ValueError:
            raise KeyError(name)
        if not path.exists():
            raise KeyError(name)
        return Flow(**json.loads(path.read_text()))

    def list(self) -> list[dict]:
        if not self.root.exists():
            return []
        out = []
        for path in sorted(self.root.glob("*.json")):
            try:
                d = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            out.append({"name": d.get("name"), "target": d.get("target"),
                        "steps": len(d.get("steps") or []), "params": d.get("params") or []})
        return out

    def delete(self, name: str):
        try:
            path = self._path(name)
        except ValueError:
            raise KeyError(name)
        if not path.exists():
            raise KeyError(name)
        path.unlink()


def element_labels(page: dict) -> list[str]:
    return sorted(a["label"] for a in page.get("actions", []) if "node" in a)


def signature(page: dict) -> dict:
    return {"where": page.get("url", ""), "labels": element_labels(page)}


def matches(sig: dict, page: dict) -> bool:
    """Same `where` and Jaccard similarity of element labels >= 0.8."""
    if sig.get("where") != page.get("url", ""):
        return False
    a, b = set(sig.get("labels") or []), set(element_labels(page))
    if not a and not b:
        return True
    return len(a & b) / len(a | b) >= MATCH_THRESHOLD


def find_by_locator(page: dict, locator: dict) -> list[dict]:
    """All matches: resource_id when non-empty, else label+role+kind; controls by id."""
    actions = page.get("actions", [])
    if "control" in locator:
        return [a for a in actions if a["id"] == locator["control"]]
    if locator.get("resource_id"):
        return [a for a in actions if "node" in a and a.get("resource_id") == locator["resource_id"]]
    return [a for a in actions if "node" in a and all(
        a.get(k) == locator.get(k) for k in ("label", "role", "kind"))]


def locate(locator: dict, page: dict) -> dict | None:
    """The single matching action, or None when absent or ambiguous."""
    found = find_by_locator(page, locator)
    return found[0] if len(found) == 1 else None


def value_source(text: str | None, params: dict | None, secret: bool = False) -> str | None:
    if text is None:
        return None
    if secret:
        return "secret"
    for k, v in (params or {}).items():
        if text == str(v):
            return f"param:{k}"
    return f"literal:{text}"


def flow_from_history(name: str, target: str, start: str | None, goal: str, params: dict,
                      money_params: list[str], history: list[dict]) -> Flow:
    steps = []
    for h in history:
        text = h.get("text")
        if h.get("value_source") == "secret" or text == SECRET_MARK:
            value = "secret"
        else:
            value = value_source(text, params)
        steps.append({"signature": h["signature"], "locator": h["locator"], "value": value})
    return Flow(name=name, target=target, start=start, goal=goal, params=list(params or {}),
                money_params=list(money_params or []), steps=steps)
