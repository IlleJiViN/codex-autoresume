"""Shared helpers: logging, reset-time parsing, sleeping and running the codex CLI."""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import os
import re
import subprocess
import time
from pathlib import Path

DEFAULT_PROMPT = (
    "The previous turn was interrupted by a usage limit. Continue the work from where "
    "it stopped. Do not redo finished steps; complete only what remains and summarize the result."
)
FALLBACK_WAIT = 5 * 3600 + 300  # 5h window + 5 minutes
LIMIT_CODE = "usage_limit_exceeded"
LIMIT_RE = re.compile(r"usage[ _]limit", re.I)
TRY_AT_RE = re.compile(r"try again at\s+(?P<when>[^.\n\"]*?\d{1,2}:\d{2}\s*(?:AM|PM)?)", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


_log_file = None


def set_log_file(path: str | os.PathLike | None) -> None:
    global _log_file
    if _log_file:
        _log_file.close()
    _log_file = open(path, "a", encoding="utf-8") if path else None


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if _log_file:
        _log_file.write(line + "\n")
        _log_file.flush()


def parse_try_again(message: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """Parse 'try again at 7:55 PM' / 'try again at Oct 29th, 2026 3:12 PM' into a local datetime."""
    now = now or dt.datetime.now()
    m = TRY_AT_RE.search(message or "")
    if not m:
        return None
    when = m.group("when").strip()
    t = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", when, re.I)
    if not t:
        return None
    hour, minute = int(t.group(1)), int(t.group(2))
    ampm = (t.group(3) or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0

    date_part = when[: t.start()]
    md = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?", date_part)
    year_m = re.search(r"\b(20\d{2})\b", date_part)
    try:
        if md and md.group(1).lower() in MONTHS:
            year = int(year_m.group(1)) if year_m else now.year
            target = dt.datetime(year, MONTHS[md.group(1).lower()], int(md.group(2)), hour, minute)
            if not year_m and target < now - dt.timedelta(days=1):
                target = target.replace(year=year + 1)
            return target
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        return None
    # The message time has minute precision, so a time a minute or two in the past means "now".
    if target <= now - dt.timedelta(minutes=2):
        target += dt.timedelta(days=1)
    return target


def full_window_reset(rate_limits: dict | None, after: dt.datetime | None = None) -> dt.datetime | None:
    """Latest reset time among rate-limit windows that are exhausted (>= 99.5% used)."""
    full = []
    for key in ("primary", "secondary"):
        w = (rate_limits or {}).get(key) or {}
        if w.get("resets_at") and (w.get("used_percent") or 0) >= 99.5:
            full.append(dt.datetime.fromtimestamp(w["resets_at"]))
    if after:
        full = [t for t in full if t > after]
    return max(full) if full else None


def keep_awake(enable: bool) -> None:
    """Prevent Windows from sleeping while we wait. No-op elsewhere."""
    if os.name != "nt":
        return
    flags = 0x80000000 | (0x00000001 if enable else 0)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    ctypes.windll.kernel32.SetThreadExecutionState(flags)


def sleep_until(target: dt.datetime) -> None:
    """Wall-clock sleep, so waking from system sleep still resumes on time."""
    last_report = -1e9
    while True:
        remain = (target - dt.datetime.now()).total_seconds()
        if remain <= 0:
            return
        if time.monotonic() - last_report >= 1800:
            h, rem = divmod(int(remain), 3600)
            log(f"Waiting... {h}h {rem // 60}m left (resume at {target:%m-%d %H:%M:%S})")
            last_report = time.monotonic()
        time.sleep(min(remain, 20))


class Result:
    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.limit_message: str | None = None
        self.completed = False
        self.returncode = 0


def run_codex(cmd: list[str], cwd: str | None) -> Result:
    """Run a codex command, stream its JSONL output, and report thread id / limit state."""
    res = Result()
    log("Running: " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.stdout
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if not line:
            continue
        print(line, flush=True)
        try:
            ev = json.loads(line)
        except ValueError:
            ev = None
        if isinstance(ev, dict):
            etype = ev.get("type")
            if etype == "thread.started":
                res.thread_id = ev.get("thread_id") or res.thread_id
            elif etype == "turn.completed":
                res.completed = True
            elif etype in ("error", "turn.failed"):
                err = ev.get("message") or (ev.get("error") or {}).get("message") or ""
                if LIMIT_RE.search(err) or LIMIT_CODE in line:
                    new = err or line
                    # Keep the message that carries a reset time (turn.failed may follow error).
                    if not res.limit_message or (TRY_AT_RE.search(new) and not TRY_AT_RE.search(res.limit_message)):
                        res.limit_message = new
        elif LIMIT_RE.search(line) and "hit your" in line.lower():
            res.limit_message = line
    res.returncode = proc.wait()
    if res.limit_message:
        res.completed = False
    return res


def resume_compatible(extra: list[str]) -> list[str]:
    """`codex exec resume` rejects -s/--sandbox; express it as a config override instead."""
    out: list[str] = []
    i = 0
    while i < len(extra):
        a = extra[i]
        if a in ("-s", "--sandbox") and i + 1 < len(extra):
            out += ["-c", f'sandbox_mode="{extra[i + 1]}"']
            i += 2
        elif a.startswith("--sandbox="):
            out += ["-c", f'sandbox_mode="{a.split("=", 1)[1]}"']
            i += 1
        else:
            out.append(a)
            i += 1
    return out


def build_cmd(codex: str, mode: str, session: str | None, last: bool, prompt: str,
              cwd: str | None, extra: list[str]) -> list[str]:
    cmd = [codex, "exec"]
    if mode == "run":
        cmd += ["--json", "--skip-git-repo-check"]
        if cwd:
            cmd += ["-C", cwd]
        cmd += extra + [prompt]
    else:
        cmd += ["resume", "--json", "--skip-git-repo-check"] + resume_compatible(extra)
        cmd += ["--last"] if (last and not session) else [session] if session else []
        cmd += [prompt]
    return cmd
