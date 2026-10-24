"""Run a new codex task and auto-resume it across usage-limit stops."""
from __future__ import annotations

import datetime as dt
import json
import re

from . import core


def run_task(opts, extra: list[str]) -> int:
    cwd = opts.cd
    core.keep_awake(not opts.no_keep_awake)
    try:
        if opts.start_at:
            t = opts.start_at.strip()
            now = dt.datetime.now()
            if re.fullmatch(r"\d{1,2}:\d{2}", t):
                h, m = map(int, t.split(":"))
                target = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if target <= now:
                    target += dt.timedelta(days=1)
            else:
                target = dt.datetime.fromisoformat(t)
            core.log(f"Scheduled start: waiting until {target:%Y-%m-%d %H:%M:%S}")
            core.sleep_until(target)

        mode = opts.mode
        session = getattr(opts, "session", None)
        last = getattr(opts, "last", False)
        prompt = opts.prompt if mode == "run" else opts.resume_prompt
        resumes = 0
        while True:
            res = core.run_codex(core.build_cmd(opts.codex_bin, mode, session, last, prompt, cwd, extra), cwd)
            if res.thread_id:
                session, last = res.thread_id, False
                if mode == "run":
                    core.log(f"Session id: {session}")
            if not res.limit_message:
                if res.completed and res.returncode == 0:
                    core.log("Task completed.")
                    return 0
                core.log(f"Stopped for a reason other than a usage limit (exit={res.returncode}); not resuming.")
                return res.returncode or 1
            if resumes >= opts.max_resumes:
                core.log(f"Exceeded max resumes ({opts.max_resumes}); giving up.")
                return 2
            if not session:
                core.log("Session id unknown; cannot resume.")
                return 3
            wake, how = _wake_time(res.limit_message, session, opts.fallback_wait)
            wake += dt.timedelta(seconds=opts.buffer)
            core.log(f"Usage limit hit: {res.limit_message[:200]}")
            core.log(f"Resume at {wake:%Y-%m-%d %H:%M:%S} (basis: {how}, buffer {opts.buffer}s)")
            core.sleep_until(wake)
            resumes += 1
            mode, prompt = "resume", opts.resume_prompt
            core.log(f"Auto-resume #{resumes} (session {session})")
    except KeyboardInterrupt:
        core.log("Stopped by user")
        return 130
    finally:
        core.keep_awake(False)


def _wake_time(message: str, thread_id: str | None, fallback_wait: int) -> tuple[dt.datetime, str]:
    from .watch import inspect_tail, read_tail_lines  # local import avoids a cycle at module load
    now = dt.datetime.now()
    cands = []
    if thread_id:
        files = list((core.codex_home() / "sessions").rglob(f"*{thread_id}*.jsonl"))
        if files:
            path = max(files, key=lambda p: p.stat().st_mtime)
            info = inspect_tail(read_tail_lines(path))
            if info and info.get("rate_limits_line"):
                try:
                    rl = json.loads(info["rate_limits_line"])["payload"]["rate_limits"]
                    r = core.full_window_reset(rl, now)
                    if r:
                        cands.append((r, "session log resets_at"))
                except (ValueError, KeyError, TypeError):
                    pass
    p = core.parse_try_again(message, now)
    if p and p > now:
        cands.append((p, "error message"))
    if cands:
        return max(cands, key=lambda c: c[0])
    if p:
        return now, "message time already passed"
    return now + dt.timedelta(seconds=fallback_wait), f"no reset time found, default {fallback_wait}s"
