#!/usr/bin/env python3
"""codex-autoresume CLI.

  autoresume.py list                 show Codex sessions currently stalled on a usage limit
  autoresume.py watch                watch session logs and auto-resume stalled sessions
  autoresume.py run "<prompt>"       run a new task and auto-resume it across limit stops
  autoresume.py resume --last        resume an existing session, then keep resuming on limits
  autoresume.py install|uninstall    start the watcher automatically at login
  autoresume.py status               show what the watcher has detected/resumed

Extra codex options go after `--`, e.g.  autoresume.py watch -- -s workspace-write
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from codex_autoresume import __version__, autostart, core, run, watch  # noqa: E402


def data_dir() -> Path:
    d = Path(os.environ.get("CODEX_AUTORESUME_HOME") or core.codex_home() / "autoresume")
    d.mkdir(parents=True, exist_ok=True)
    return d


def build_parser() -> argparse.ArgumentParser:
    dd = data_dir()
    codex_bin = os.environ.get("CODEX_BIN") or shutil.which("codex") or "codex"

    ap = argparse.ArgumentParser(prog="autoresume", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    scan = argparse.ArgumentParser(add_help=False)
    scan.add_argument("--sessions-dir", default=str(core.codex_home() / "sessions"))
    scan.add_argument("--max-age", type=float, default=6.0,
                      help="ignore sessions stalled longer ago than this many hours (default 6)")
    scan.add_argument("--buffer", type=int, default=90, help="extra seconds to wait after the reset time")
    scan.add_argument("--json", action="store_true", help="machine-readable output")

    w = sub.add_parser("watch", parents=[scan], help="watch and auto-resume stalled sessions")
    w.add_argument("--interval", type=int, default=60, help="seconds between scans")
    w.add_argument("--prompt", default=core.DEFAULT_PROMPT, help="prompt sent when resuming")
    w.add_argument("--max-resumes", type=int, default=10, help="max auto-resumes per session")
    w.add_argument("--idle-guard", type=int, default=120,
                   help="skip resuming if the session file changed within this many seconds")
    w.add_argument("--state", default=str(dd / "state.json"))
    w.add_argument("--log", default=str(dd / "watch.log"))
    w.add_argument("--codex-bin", default=codex_bin)
    w.add_argument("--once", action="store_true", help="scan once, handle due resumes, then exit")
    w.add_argument("--dry-run", action="store_true", help="log what would be resumed without running codex")
    w.add_argument("--no-keep-awake", action="store_true")

    sub.add_parser("list", parents=[scan], help="list stalled sessions and their resume times")

    st = sub.add_parser("status", help="show the watcher's state file")
    st.add_argument("--state", default=str(dd / "state.json"))

    task = argparse.ArgumentParser(add_help=False)
    task.add_argument("-C", "--cd", help="working directory")
    task.add_argument("--resume-prompt", default=core.DEFAULT_PROMPT)
    task.add_argument("--buffer", type=int, default=90)
    task.add_argument("--fallback-wait", type=int, default=core.FALLBACK_WAIT)
    task.add_argument("--max-resumes", type=int, default=20)
    task.add_argument("--start-at", help="delay the first run until HH:MM or an ISO datetime")
    task.add_argument("--codex-bin", default=codex_bin)
    task.add_argument("--log", default=str(dd / "run.log"))
    task.add_argument("--no-keep-awake", action="store_true")
    r = sub.add_parser("run", parents=[task], help="run a new task with auto-resume")
    r.add_argument("prompt")
    rs = sub.add_parser("resume", parents=[task], help="resume an existing session with auto-resume")
    rs.add_argument("session", nargs="?")
    rs.add_argument("--last", action="store_true")

    for name in ("install", "uninstall"):
        s = sub.add_parser(name, help=f"{name} start-at-login for the watcher")
        s.add_argument("--target-file", help=argparse.SUPPRESS)
    return ap


def main(argv: list[str] | None = None) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    a = build_parser().parse_args(argv)

    if a.cmd == "list":
        rows = watch.list_stalled(a)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if not rows:
            print(f"No session stalled on a usage limit in the last {a.max_age:g}h.")
        for r in rows:
            when = "ready now" if r["ready"] else f"in {r['minutes_left']} min"
            print(f"- {r['sid']}\n    cwd    : {r['cwd']}\n    stalled: {r['stalled_at']}\n"
                  f"    resume : {r['resume_at']} ({when}; basis: {r['basis']})")
        return 0

    if a.cmd == "status":
        p = Path(a.state)
        if not p.exists():
            print("No state yet (the watcher has not detected anything).")
            return 0
        for k, e in json.loads(p.read_text(encoding="utf-8")).get("events", {}).items():
            print(f"{e['status']:<11} {e['sid']}  limit@{e['limit_at']}  wake@{e.get('wake_at')}  {e.get('result', '')}")
        return 0

    if a.cmd == "install":
        f = autostart.install(Path(a.target_file) if a.target_file else None)
        print(f"Installed: {f}")
        return 0
    if a.cmd == "uninstall":
        ok = autostart.uninstall(Path(a.target_file) if a.target_file else None)
        print("Removed." if ok else "Nothing was installed.")
        return 0

    if a.cmd == "resume" and not (a.session or a.last):
        print("resume needs a session id or --last", file=sys.stderr)
        return 2
    core.set_log_file(a.log)
    try:
        if a.cmd == "watch":
            return watch.watch(a, extra)
        a.mode = a.cmd
        return run.run_task(a, extra)
    finally:
        core.set_log_file(None)


if __name__ == "__main__":
    sys.exit(main())
