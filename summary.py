"""Trims a jev_ultrafast.Agent run into the shape browse_web returns to Claude."""

from typing import Literal, TypedDict


class StepSummary(TypedDict):
    action: str
    kind: str
    text: str | None
    page_changed: bool | None
    elapsed_ms: int


class BrowseResult(TypedDict):
    status: Literal["done", "blocked", "max_steps", "error"]
    final_url: str
    elapsed_ms: int
    steps: list[StepSummary]


def summarize(state: dict, hit_step_cap: bool) -> BrowseResult:
    status = state["status"]
    if hit_step_cap and status not in ("done", "blocked"):
        status = "max_steps"
    return {
        "status": status,
        "final_url": state["page"]["url"],
        "elapsed_ms": state["elapsed_ms"],
        "steps": [
            {
                "action": step["action"],
                "kind": step["kind"],
                "text": step.get("text"),
                "page_changed": step.get("page_changed"),
                "elapsed_ms": step["elapsed_ms"],
            }
            for step in state["history"]
        ],
    }
