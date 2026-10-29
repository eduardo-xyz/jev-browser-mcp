"""Compact tool results: small JSON, never image bytes."""


def _line(h: dict) -> str:
    s = f"{h['kind']} · {h['action']}"
    if h["kind"] == "fill" and h.get("text") is not None:
        s += f" = {h['text']}"
    return s


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
    for key in ("field", "expected", "seen"):
        if runner.pending and key in runner.pending:
            out[key] = runner.pending[key]
    if runner.receipt:
        out["receipt"] = runner.receipt
    flow_saved = getattr(runner, "flow_saved", None)
    if flow_saved:
        out["flow_saved"] = flow_saved
    return out
