"""Register the watcher to start automatically at login (Windows, macOS, Linux)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
LABEL = "codex-autoresume"
LAUNCHD_ID = "io.github.codex-autoresume"


def _python() -> str:
    exe = Path(sys.executable)
    if os.name == "nt":
        pyw = exe.with_name("pythonw.exe")
        if pyw.exists():
            return str(pyw)
    return str(exe)


def _cmd() -> list[str]:
    return [_python(), str(SCRIPTS / "autoresume.py"), "watch"]


def target_path() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise SystemExit("APPDATA is not set")
        return Path(appdata) / "Microsoft/Windows/Start Menu/Programs/Startup" / f"{LABEL}.vbs"
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents" / f"{LAUNCHD_ID}.plist"
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd/user" / f"{LABEL}.service"


def render() -> str:
    cmd = _cmd()
    if os.name == "nt":
        quoted = " ".join(f'""{c}""' for c in cmd)
        return ('Set sh = CreateObject("WScript.Shell")\r\n'
                f'sh.CurrentDirectory = "{SCRIPTS}"\r\n'
                f'sh.Run "{quoted}", 0, False\r\n')
    if sys.platform == "darwin":
        args = "".join(f"    <string>{c}</string>\n" for c in cmd)
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                f'<plist version="1.0"><dict>\n  <key>Label</key><string>{LAUNCHD_ID}</string>\n'
                f'  <key>ProgramArguments</key><array>\n{args}  </array>\n'
                '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n</dict></plist>\n')
    quoted = " ".join(f'"{c}"' for c in cmd)
    return ("[Unit]\nDescription=Resume Codex sessions stopped by usage limits\n\n"
            f"[Service]\nExecStart={quoted}\nRestart=on-failure\nRestartSec=30\n\n"
            "[Install]\nWantedBy=default.target\n")


def install(path: Path | None = None, activate: bool = True) -> Path:
    f = path or target_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(render(), encoding="utf-8")
    if activate and path is None:
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "load", "-w", str(f)], check=False)
        elif os.name != "nt":
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
            subprocess.run(["systemctl", "--user", "enable", "--now", f"{LABEL}.service"], check=False)
        elif os.name == "nt":
            subprocess.Popen(["wscript.exe", str(f)])
    return f


def uninstall(path: Path | None = None, activate: bool = True) -> bool:
    f = path or target_path()
    if not f.exists():
        return False
    if activate and path is None:
        if sys.platform == "darwin":
            subprocess.run(["launchctl", "unload", str(f)], check=False)
        elif os.name != "nt":
            subprocess.run(["systemctl", "--user", "disable", "--now", f"{LABEL}.service"], check=False)
    f.unlink()
    return True
