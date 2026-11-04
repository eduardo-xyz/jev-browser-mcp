"""Nightly smoke run: a few read-only goals through the running jevd, so a regression or a site
change shows up before an agent hits it.

    python -m jev_runner.smoke [--config PATH] [--no-post] [--dry-run]

The goals live outside the code, in `smoke.toml` in jevd's data dir (sites and ids are
deployment facts, not runner code):

    [report]
    dir = "smoke"                       # under the data dir; one markdown file per run
    post = ["/path/to/scripts/broker", "--member", "Jev-Smoke"]   # optional: posts the summary
    room = "<conversation id>"

    [[goal]]
    name = "workflows-list"
    url = "https://app.example.com/.../workflows"
    goal = "Read only. Read the first 5 workflow names and their status. Change nothing."
    max_steps = 8
    expect = "Published"                # optional: the final read must contain it (case-insensitive)

A goal passes when the run ends `done` (any reason) and, if `expect` is set, the final read
holds that text. Every session is ended, pass or fail. The exit code is the number of failures.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import client


def data_dir() -> Path:
    return Path(os.environ.get("JEV_DATA_DIR") or Path.home() / ".local/share/jev-runner").expanduser()


def load_config(path: Path) -> dict:
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    goals = cfg.get("goal") or []
    for g in goals:
        for key in ("name", "url", "goal"):
            if not isinstance(g.get(key), str) or not g[key].strip():
                raise ValueError(f"smoke.toml: every [[goal]] needs a non-empty {key!r}")
    if not goals:
        raise ValueError("smoke.toml: no [[goal]] entries")
    return cfg


def run_goal(g: dict, call=None) -> dict:
    """One goal through the daemon: run, read, end. Never raises; a failure is a result."""
    call = call or client.call
    started = time.perf_counter()
    out = {"name": g["name"], "url": g["url"], "passed": False, "status": None, "reason": None,
           "why": None, "model_calls": None, "elapsed_ms": None, "expect": g.get("expect"), "found": None}
    sid = None
    try:
        r = call("run_task", {"target": "chrome", "url_or_app": g["url"], "goal": g["goal"],
                              "max_steps": int(g.get("max_steps") or 8)})
        sid = r.get("session_id")
        out.update(status=r.get("status"), reason=r.get("reason"), model_calls=r.get("model_calls"),
                   why=(r.get("detail") or {}).get("why") if isinstance(r.get("detail"), dict) else None)
        text = ""
        if sid:
            rd = call("read", {"session_id": sid})
            text = rd.get("text") or "" if isinstance(rd, dict) else ""
        if out["expect"]:
            out["found"] = out["expect"].casefold() in text.casefold()
        out["passed"] = out["status"] == "done" and (out["found"] is not False)
    except Exception as exc:  # the daemon down, a timeout: still a result line
        out["status"], out["reason"] = "error", f"{type(exc).__name__}: {exc}"[:200]
    finally:
        if sid:
            try:
                call("end_session", {"session_id": sid})
            except Exception:
                pass
        out["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return out


def summary(results: list[dict], when: _dt.datetime) -> str:
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    line = f"JEV smoke {when:%Y-%m-%d %H:%M}: {len(passed)}/{len(results)} passed"
    if failed:
        parts = []
        for r in failed:
            what = r["status"] or "?"
            if r["reason"]:
                what += f"/{r['reason']}"
            if r["why"]:
                what += f" ({r['why']})"
            if r["found"] is False:
                what += f", '{r['expect']}' not on the final screen"
            parts.append(f"{r['name']}: {what}")
        line += ". Failed: " + "; ".join(parts)
    return line


def report(results: list[dict], when: _dt.datetime) -> str:
    lines = [f"# JEV smoke run {when:%Y-%m-%d %H:%M}", "", summary(results, when), "",
             "| goal | result | status | reason | model calls | ms |", "|---|---|---|---|---|---|"]
    for r in results:
        verdict = "PASS" if r["passed"] else "FAIL"
        reason = r["reason"] or ""
        if r["why"]:
            reason += f" ({r['why']})"
        if r["found"] is False:
            reason += f" expected '{r['expect']}'"
        lines.append(f"| {r['name']} | {verdict} | {r['status']} | {reason} | {r['model_calls']} | {r['elapsed_ms']} |")
    lines += ["", "URLs:", *[f"- {r['name']}: {r['url']}" for r in results], ""]
    return "\n".join(lines)


def post(cfg: dict, text: str, run=subprocess.run) -> bool:
    """Send the summary line to the room with the configured command (a broker CLI). Never raises."""
    rep = cfg.get("report") or {}
    cmd, room = rep.get("post"), rep.get("room")
    if not cmd or not room:
        return False
    body = json.dumps({"conversation_id": room, "body": text[:1500],
                       "idempotency_key": f"jev-smoke-{int(time.time())}", "no_reply": True})
    try:
        done = run([*cmd, "send", body], capture_output=True, text=True, timeout=60)
        return done.returncode == 0
    except Exception:
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="JEV Runner nightly smoke run")
    ap.add_argument("--config", type=Path, default=None, help="smoke.toml (default: <data dir>/smoke.toml)")
    ap.add_argument("--no-post", action="store_true", help="write the report, do not post the summary")
    ap.add_argument("--dry-run", action="store_true", help="list the goals and exit")
    ap.add_argument("--only", default=None, help="run only the goal with this name")
    args = ap.parse_args(argv)
    cfg_path = args.config or data_dir() / "smoke.toml"
    cfg = load_config(cfg_path)
    goals = [g for g in cfg["goal"] if not args.only or g["name"] == args.only]
    if args.dry_run:
        for g in goals:
            print(f"{g['name']}: {g['url']}\n    {g['goal']}")
        return 0
    when = _dt.datetime.now()
    results = [run_goal(g) for g in goals]
    out_dir = data_dir() / ((cfg.get("report") or {}).get("dir") or "smoke")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{when:%Y-%m-%d-%H%M}.md"
    path.write_text(report(results, when))
    line = summary(results, when)
    print(line)
    print(f"report: {path}")
    if not args.no_post:
        posted = post(cfg, f"{line}\nReport: {path}")
        print("posted" if posted else "not posted")
    return sum(1 for r in results if not r["passed"])


if __name__ == "__main__":
    sys.exit(main())
