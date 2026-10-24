"""Watch Codex session logs and resume sessions that stopped on a usage limit."""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path

from . import core

TURN_TYPES = ("task_started", "task_complete", "turn_aborted")


def to_local(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def read_tail_lines(path: Path, max_bytes: int = 2_000_000) -> list[str]:
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    if start > 0:
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    return data.decode("utf-8", "replace").splitlines()


def read_meta(path: Path) -> dict | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            ev = json.loads(f.readline())
    except (OSError, ValueError):
        return None
    if ev.get("type") != "session_meta":
        return None
    return ev.get("payload") or {}


def is_subagent(meta: dict) -> bool:
    src = meta.get("source")
    return meta.get("thread_source") == "subagent" or (isinstance(src, dict) and "subagent" in src)


def inspect_tail(lines: list[str]) -> dict | None:
    """Return limit info if the session's last turn event is a task_complete caused by a usage limit."""
    last = None
    window = None
    last_window = None
    for line in lines:
        if '"event_msg"' not in line:
            continue
        if '"token_count"' in line and '"rate_limits"' in line:
            window = line
            continue
        if not any(f'"{t}"' in line for t in TURN_TYPES):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if (ev.get("payload") or {}).get("type") in TURN_TYPES:
            last, last_window = ev, window
    if not last:
        return None
    p = last["payload"]
    err = p.get("error") or {}
    if p.get("type") != "task_complete" or err.get("codex_error_info") != core.LIMIT_CODE:
        return None
    return {
        "ts": to_local(last["timestamp"]),
        "message": err.get("message") or "",
        "turn_id": p.get("turn_id"),
        "rate_limits_line": last_window,
    }


def compute_wake(info: dict, buffer: int) -> tuple[dt.datetime, str]:
    ev_ts = info["ts"]
    cands = []
    msg_t = core.parse_try_again(info["message"], ev_ts)  # interpret relative to when the limit hit
    if msg_t and msg_t > ev_ts - dt.timedelta(minutes=2):
        cands.append((msg_t, "error message"))
    rl = None
    if info.get("rate_limits_line"):
        try:
            rl = json.loads(info["rate_limits_line"])["payload"]["rate_limits"]
        except (ValueError, KeyError, TypeError):
            rl = None
    win_t = core.full_window_reset(rl, ev_ts)
    if win_t:
        cands.append((win_t, "rate_limits.resets_at"))
    if cands:
        t, how = max(cands, key=lambda c: c[0])
    else:
        t, how = ev_ts + dt.timedelta(seconds=core.FALLBACK_WAIT), "no reset time found, assuming 5h05m"
    return t + dt.timedelta(seconds=buffer), how


class State:
    """Persistent record of detected stalls so each one is resumed at most once."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict = {"events": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
        self.events: dict = self.data.setdefault("events", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def resumes_of(self, sid: str) -> int:
        return sum(1 for e in self.events.values() if e["sid"] == sid and e["status"] in ("done", "running"))


class Scanner:
    def __init__(self, sessions_dir: Path) -> None:
        self.dir = sessions_dir
        self.cache: dict[Path, tuple] = {}

    def _files(self, since: float):
        for root, _dirs, files in os.walk(self.dir):
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                p = Path(root) / name
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime >= since:
                    yield p, st

    def scan(self, max_age: dt.timedelta, now: dt.datetime) -> list[dict]:
        found = []
        since = (now - max_age - dt.timedelta(hours=1)).timestamp()
        for p, st in self._files(since):
            sig = (st.st_mtime, st.st_size)
            cached = self.cache.get(p)
            if cached and cached[0] == sig:
                res = cached[1]
            else:
                res = self._inspect(p)
                self.cache[p] = (sig, res)
            if res and now - res["ts"] <= max_age:
                found.append(res)
        return found

    @staticmethod
    def _inspect(p: Path) -> dict | None:
        meta = read_meta(p)
        if not meta or is_subagent(meta):
            return None
        sid = meta.get("id") or meta.get("session_id")
        if not sid:
            return None
        try:
            info = inspect_tail(read_tail_lines(p))
        except OSError:
            return None
        if not info:
            return None
        info.update(sid=sid, cwd=meta.get("cwd"), path=str(p))
        return info


def register(state: State, found: list[dict], buffer: int, max_resumes: int) -> bool:
    changed = False
    for info in found:
        key = f"{info['sid']}@{info['ts']:%Y%m%dT%H%M%S}"
        if key in state.events:
            continue
        base = dict(sid=info["sid"], cwd=info["cwd"], path=info["path"],
                    limit_at=f"{info['ts']:%Y-%m-%d %H:%M:%S}")
        if state.resumes_of(info["sid"]) >= max_resumes:
            core.log(f"Session {info['sid']}: exceeded max auto-resumes ({max_resumes}), skipping")
            state.events[key] = dict(base, status="skipped", wake_at=None)
        else:
            wake, how = compute_wake(info, buffer)
            state.events[key] = dict(base, status="pending", wake_at=f"{wake:%Y-%m-%d %H:%M:%S}", how=how)
            core.log(f"Limit stall detected: session {info['sid']} ({info['cwd']}) "
                     f"-> resume at {wake:%m-%d %H:%M:%S} (basis: {how})")
        changed = True
    return changed


def still_stalled(entry: dict) -> bool:
    try:
        info = inspect_tail(read_tail_lines(Path(entry["path"])))
    except OSError:
        return False
    return bool(info) and f"{info['ts']:%Y-%m-%d %H:%M:%S}" == entry["limit_at"]


def resume_one(entry: dict, state: State, opts, extra: list[str]) -> None:
    cwd = entry["cwd"] if entry.get("cwd") and Path(entry["cwd"]).is_dir() else None
    entry["status"] = "running"
    entry["started_at"] = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"
    state.save()
    cmd = core.build_cmd(opts.codex_bin, "resume", entry["sid"], False, opts.prompt, cwd, extra)
    core.log(f"Auto-resume start: session {entry['sid']} (cwd={cwd})")
    try:
        res = core.run_codex(cmd, cwd)
        entry["status"] = "done"
        entry["result"] = "limit" if res.limit_message else "ok" if res.completed else f"exit={res.returncode}"
    except Exception as e:  # noqa: BLE001
        entry["status"] = "failed"
        entry["result"] = repr(e)
    entry["finished_at"] = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}"
    core.log(f"Auto-resume end: session {entry['sid']} -> {entry['result']}")
    state.save()


def watch(opts, extra: list[str]) -> int:
    state = State(Path(opts.state))
    scanner = Scanner(Path(opts.sessions_dir))
    max_age = dt.timedelta(hours=opts.max_age)
    core.log(f"Watching (interval {opts.interval}s, window {opts.max_age}h, dir {opts.sessions_dir})")
    for e in state.events.values():  # left over from a crashed run: never re-run blindly
        if e["status"] == "running":
            e["status"] = "interrupted"
    state.save()
    awake = False
    try:
        while True:
            now = dt.datetime.now()
            if register(state, scanner.scan(max_age, now), opts.buffer, opts.max_resumes):
                state.save()

            pending = [(k, e) for k, e in state.events.items() if e["status"] == "pending"]
            if not opts.no_keep_awake and bool(pending) != awake:
                awake = bool(pending)
                core.keep_awake(awake)

            due = sorted(
                ((k, e) for k, e in pending
                 if dt.datetime.strptime(e["wake_at"], "%Y-%m-%d %H:%M:%S") <= now),
                key=lambda x: x[1]["wake_at"])
            if due:
                _key, entry = due[0]
                p = Path(entry["path"])
                if not still_stalled(entry):
                    core.log(f"Session {entry['sid']}: already progressed, nothing to resume")
                    entry["status"] = "superseded"
                    state.save()
                elif time.time() - p.stat().st_mtime < opts.idle_guard:
                    core.log(f"Session {entry['sid']}: recently active, re-checking in {opts.idle_guard}s")
                    entry["wake_at"] = f"{now + dt.timedelta(seconds=opts.idle_guard):%Y-%m-%d %H:%M:%S}"
                    state.save()
                elif opts.dry_run:
                    core.log(f"[dry-run] would resume {entry['sid']}")
                    entry["status"] = "dry-run"
                    state.save()
                else:
                    resume_one(entry, state, opts, extra)
                continue
            if opts.once:
                return 0
            time.sleep(opts.interval)
    except KeyboardInterrupt:
        core.log("Stopped by user")
        return 130
    finally:
        core.keep_awake(False)


def list_stalled(opts) -> list[dict]:
    now = dt.datetime.now()
    found = Scanner(Path(opts.sessions_dir)).scan(dt.timedelta(hours=opts.max_age), now)
    out = []
    for info in sorted(found, key=lambda i: i["ts"]):
        wake, how = compute_wake(info, opts.buffer)
        out.append(dict(sid=info["sid"], cwd=info["cwd"], stalled_at=f"{info['ts']:%Y-%m-%d %H:%M:%S}",
                        resume_at=f"{wake:%Y-%m-%d %H:%M:%S}", basis=how,
                        ready=wake <= now, minutes_left=max(0, int((wake - now).total_seconds() // 60))))
    return out
