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
    "target_unreachable": ("The driven browser or device dropped the connection mid-run (Chrome closed or "
                           "restarted, or is running without its remote-debugging port). Check `status` -> "
                           "chrome.cdp_url, make sure that Chrome is up, then run again."),
    "named_clicks_done": ("Every click the goal quoted has happened; the model then wanted to act on a "
                          "control the goal does not name (`detail.refused`), which the runner refused. "
                          "If that further step was wanted, name it in the goal; otherwise this run did "
                          "what was asked. `read` to confirm the state."),
    "text_model_unavailable": ("The text model that produces typed values (TEXT_MODEL_* in jevd's env) "
                               "failed (`detail.text_model`, e.g. HTTP 402 = that account has no balance). "
                               "Typing was switched off for the run and the goal could not be reached by "
                               "clicking alone. Put the exact text to type in the goal (\"type 'Round "
                               "Robin'\": a spelled-out value never needs the text model), or have the "
                               "text model's account fixed. Decisions use the main model and still work."),
    "control_disabled": ("The control the goal names is on screen but the app has disabled it (greyed out, "
                         "aria-disabled): a click would do nothing. Don't retry the same click; `read` the "
                         "screen (a disabled toggle shows `(disabled)` on the States line), report what the "
                         "app allows, or change the setting that governs it first."),
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
    if runner.reason == "target_unreachable" and getattr(runner, "last_error", None):
        out["detail"] = runner.last_error  # the raw connection error, for bug reports
    if runner.reason == "stale_page" and getattr(runner, "last_stale", None):
        out["detail"] = runner.last_stale  # which action was refused and why (for bug reports)
    if (runner.reason in ("jev_blocked", "no_progress", "control_disabled", "named_clicks_done",
                          "text_model_unavailable")
            or runner.status == "unverified") \
            and getattr(runner, "last_blocked", None):
        out["detail"] = runner.last_blocked  # what Jev saw when it gave up or called done
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
