"""Compact tool results: small JSON, never image bytes."""
from .guards import scrub_value


def _line(h: dict) -> str:
    s = f"{h['kind']} · {h['action']}"
    if h["kind"] == "fill" and h.get("text") is not None:
        s += f" = {h['text']}"
    return s


HINTS = {
    "signed_out": ("This Chrome is signed out of the site (it landed on a sign-in page). A person must "
                   "sign in in that Chrome window, including any 2FA, then run the task again. To have "
                   "the run itself sign in, say so in the goal. If the signed-in Chrome is not the one "
                   "jevd drives, check `status` -> chrome.cdp_url: the binding comes from config.toml "
                   "(chrome_cdp_url, chrome_label) in jevd's data dir."),
}


def build(runner) -> dict:
    out = {
        "session_id": runner.session_id,
        "status": runner.status,
        "target": runner.target.kind,
        "where": (runner.page or {}).get("url", ""),
        "elapsed_ms": runner.elapsed_ms,
        "model_calls": runner.model_calls,
        "last_actions": [_line(h) for h in runner.history[-5:]],
    }
    if runner.reason:
        out["reason"] = runner.reason
        if runner.reason in HINTS:
            out["hint"] = HINTS[runner.reason]
    if runner.reason == "stale_page" and getattr(runner, "last_stale", None):
        out["detail"] = runner.last_stale  # which action was refused and why (for bug reports)
    if runner.reason == "jev_blocked" and getattr(runner, "last_blocked", None):
        out["detail"] = runner.last_blocked  # what Jev saw when it gave up (for bug reports)
    for key in ("field", "expected", "seen", "control"):
        if runner.pending and key in runner.pending:
            out[key] = runner.pending[key]
    if runner.receipt:
        out["receipt"] = runner.receipt
    flow_saved = getattr(runner, "flow_saved", None)
    if flow_saved:
        out["flow_saved"] = flow_saved
    if getattr(runner, "flow_updated", None):
        out["flow_updated"] = runner.flow_updated
    flow = getattr(runner, "flow", None)
    if flow is not None:
        out["flow_used"] = flow.name
    timing = getattr(runner, "timing", None)
    if timing:  # where the run's time went, ms per phase (helps spot a slow device or model)
        out["timing"] = {k: round(v) for k, v in timing.items()}
    # belt and braces: no typed secret may reach a tool result, whatever field it hides in.
    # Identifiers are left alone: masking one would make the session or flow unreachable.
    ids = {k: out.pop(k) for k in ("session_id", "flow_saved", "flow_updated", "flow_used") if k in out}
    return {**ids, **scrub_value(out, getattr(runner, "secrets", None) or [])}
