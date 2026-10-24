import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-autoresume" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from codex_autoresume import autostart, core, watch as cw  # noqa: E402

CLI = SCRIPTS / "autoresume.py"


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def write_rollout(root: Path, sid: str, events: list[dict], subagent=False) -> Path:
    d = root / "2026" / "10" / "24"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-x-{sid}.jsonl"
    meta = {"type": "session_meta", "timestamp": events[0]["timestamp"],
            "payload": {"id": sid, "cwd": str(root), "thread_source": "subagent" if subagent else "user"}}
    p.write_text("\n".join(json.dumps(e) for e in [meta] + events) + "\n", encoding="utf-8")
    return p


def ev(t, typ, **payload):
    return {"timestamp": iso(t), "type": "event_msg", "payload": {"type": typ, **payload}}


def limit_event(t, msg):
    return ev(t, "task_complete", turn_id="t1", error={"message": msg, "codex_error_info": "usage_limit_exceeded"})


def msg_for(t: dt.datetime) -> str:
    return f"You've hit your usage limit. Upgrade to Pro, or try again at {t:%I:%M %p}."


class ParseTests(unittest.TestCase):
    now = dt.datetime(2026, 10, 24, 12, 0, 0)

    def test_today_pm(self):
        self.assertEqual(core.parse_try_again("try again at 7:55 PM.", self.now), dt.datetime(2026, 10, 24, 19, 55))

    def test_rolls_to_tomorrow(self):
        self.assertEqual(core.parse_try_again("try again at 6:25 AM.", self.now), dt.datetime(2026, 10, 25, 6, 25))

    def test_with_date(self):
        self.assertEqual(core.parse_try_again("try again at Oct 29th, 2026 3:12 PM.", self.now),
                         dt.datetime(2026, 10, 29, 15, 12))

    def test_12am_12pm(self):
        self.assertEqual(core.parse_try_again("try again at 12:30 AM", self.now).hour, 0)
        self.assertEqual(core.parse_try_again("try again at 12:30 PM", self.now).hour, 12)

    def test_none(self):
        self.assertIsNone(core.parse_try_again("nothing here", self.now))


class DetectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = dt.datetime.now()

    def tearDown(self):
        self.tmp.cleanup()

    def scan(self, hours=6):
        return cw.Scanner(self.root).scan(dt.timedelta(hours=hours), dt.datetime.now())

    def test_detects_limit_stall(self):
        t = self.now - dt.timedelta(minutes=30)
        write_rollout(self.root, "s1", [ev(t - dt.timedelta(minutes=5), "task_started"), limit_event(t, msg_for(t))])
        self.assertEqual([f["sid"] for f in self.scan()], ["s1"])

    def test_ignores_completed_after_limit(self):
        t = self.now - dt.timedelta(hours=1)
        write_rollout(self.root, "s2", [limit_event(t, msg_for(t)), ev(t + dt.timedelta(minutes=10), "task_started"),
                                        ev(t + dt.timedelta(minutes=20), "task_complete", turn_id="t2")])
        self.assertEqual(self.scan(), [])

    def test_ignores_running_after_limit(self):
        t = self.now - dt.timedelta(hours=1)
        write_rollout(self.root, "s3", [limit_event(t, msg_for(t)), ev(t + dt.timedelta(minutes=10), "task_started")])
        self.assertEqual(self.scan(), [])

    def test_ignores_subagent_and_old(self):
        t = self.now - dt.timedelta(minutes=30)
        write_rollout(self.root, "sub", [limit_event(t, msg_for(t))], subagent=True)
        old = self.now - dt.timedelta(hours=20)
        write_rollout(self.root, "old", [limit_event(old, msg_for(old))])
        self.assertEqual(self.scan(), [])

    def test_wake_prefers_resets_at(self):
        t = self.now - dt.timedelta(minutes=30)
        reset = t + dt.timedelta(hours=2)
        rl = {"timestamp": iso(t), "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": int(reset.timestamp())},
            "secondary": {"used_percent": 40.0, "window_minutes": 10080,
                          "resets_at": int((reset + dt.timedelta(days=3)).timestamp())}}}}
        write_rollout(self.root, "s4", [ev(t - dt.timedelta(minutes=1), "task_started"), rl,
                                        limit_event(t, msg_for(t + dt.timedelta(hours=1)))])
        wake, how = cw.compute_wake(self.scan()[0], 0)
        self.assertEqual(int(wake.timestamp()), int(reset.timestamp()))
        self.assertIn("resets_at", how)


FAKE = r'''
import json, sys, pathlib
with pathlib.Path(__file__).with_name("calls.txt").open("a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
print(json.dumps({"type": "thread.started", "thread_id": "s9"}))
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''

FAKE_LIMIT_ONCE = r'''
import json, sys, pathlib, datetime as dt
state = pathlib.Path(__file__).with_suffix(".state")
n = int(state.read_text()) if state.exists() else 0
state.write_text(str(n + 1))
with pathlib.Path(__file__).with_name("calls.txt").open("a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
print(json.dumps({"type": "thread.started", "thread_id": "abc-123"}), flush=True)
if n == 0:
    t = (dt.datetime.now() + dt.timedelta(seconds=2)).strftime("%I:%M %p")
    print(json.dumps({"type": "error", "message": "You've hit your usage limit. try again at " + t + "."}), flush=True)
    print(json.dumps({"type": "turn.failed", "error": {"message": "usage limit"}}), flush=True)
    sys.exit(1)
print(json.dumps({"type": "turn.completed", "usage": {}}))
'''


def make_fake(d: Path, body: str) -> Path:
    (d / "fake.py").write_text(body, encoding="utf-8")
    if os.name == "nt":
        w = d / "fake.cmd"
        w.write_text(f'@echo off\r\n"{sys.executable}" "{d / "fake.py"}" %*\r\n')
    else:
        w = d / "fake.sh"
        w.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{d / "fake.py"}" "$@"\n')
        w.chmod(0o755)
    return w


def cli(*args, timeout=90):
    env = dict(os.environ, PYTHONUTF8="1")
    return subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True,
                          encoding="utf-8", timeout=timeout, env=env)


class E2E(unittest.TestCase):
    def test_watch_resumes_once(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            sessions = d / "sessions"
            t = dt.datetime.now() - dt.timedelta(minutes=20)
            write_rollout(sessions, "s9", [ev(t - dt.timedelta(minutes=1), "task_started"),
                                           limit_event(t, msg_for(t + dt.timedelta(minutes=1)))])
            w = make_fake(d, FAKE)
            args = ["watch", "--once", "--buffer", "0", "--idle-guard", "0", "--sessions-dir", str(sessions),
                    "--codex-bin", str(w), "--state", str(d / "state.json"), "--log", str(d / "w.log"),
                    "--no-keep-awake", "--prompt", "go"]
            for _ in range(2):  # a second pass must not resume again
                p = cli(*args)
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            calls = (d / "calls.txt").read_text().strip().splitlines()
            self.assertEqual(len(calls), 1, calls)
            self.assertIn("resume", calls[0])
            self.assertIn("s9", calls[0])
            state = json.loads((d / "state.json").read_text(encoding="utf-8"))
            self.assertEqual([e["status"] for e in state["events"].values()], ["done"])

    def test_list_json(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            t = dt.datetime.now() - dt.timedelta(minutes=20)
            write_rollout(d / "sessions", "s7", [limit_event(t, msg_for(t + dt.timedelta(hours=1)))])
            p = cli("list", "--json", "--sessions-dir", str(d / "sessions"))
            rows = json.loads(p.stdout)
            self.assertEqual([r["sid"] for r in rows], ["s7"])
            self.assertFalse(rows[0]["ready"])

    def test_run_limit_then_resume(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            w = make_fake(d, FAKE_LIMIT_ONCE)
            p = cli("run", "do it", "--codex-bin", str(w), "--buffer", "0", "--log", str(d / "log.txt"),
                    "--no-keep-awake", "--resume-prompt", "continue", timeout=180)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            calls = (d / "calls.txt").read_text().strip().splitlines()
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[1].startswith("exec resume --json --skip-git-repo-check abc-123 continue"), calls[1])


class AutostartTests(unittest.TestCase):
    def test_install_uninstall_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x" / "entry"
            for cmd in ("install", "uninstall"):
                p = cli(cmd, "--target-file", str(f))
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(f.exists(), cmd == "install")

    def test_rendered_entry_points_to_cli(self):
        self.assertIn("autoresume.py", autostart.render())


class PluginLayoutTests(unittest.TestCase):
    def test_manifests_valid(self):
        plugin = ROOT / "plugins" / "codex-autoresume"
        manifest = json.loads((plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "codex-autoresume")
        self.assertTrue((plugin / manifest["skills"]).is_dir())
        market = json.loads((ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
        for entry in market["plugins"]:
            self.assertTrue((ROOT / entry["source"]["path"] / ".codex-plugin" / "plugin.json").exists())
        skill = (plugin / "skills" / "autoresume" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: autoresume\n"))


if __name__ == "__main__":
    unittest.main()
