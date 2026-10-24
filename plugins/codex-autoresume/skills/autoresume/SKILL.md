---
name: autoresume
description: Detect Codex sessions that stopped on a usage limit (5-hour or weekly) and resume them automatically once the limit resets. Use when the user mentions hitting a Codex usage limit, wants work to continue after the limit resets, asks which sessions are stalled, or wants to install, check or remove the auto-resume watcher.
---

# Codex Autoresume

Resumes Codex sessions that ended with `usage_limit_exceeded`. It reads the local session logs in
`$CODEX_HOME/sessions` (default `~/.codex/sessions`), works out the exact reset time, and then runs
`codex exec resume <session-id>` in the session's original working directory.

The CLI is `scripts/autoresume.py` in this skill's plugin (`<plugin-root>/scripts/autoresume.py`).
It needs Python 3.8+ and the `codex` CLI on PATH. Use `python` or `python3`, whichever exists.

## Workflow

1. Run `python <plugin-root>/scripts/autoresume.py list` to show stalled sessions and when each will resume.
   Add `--max-age 170` to also include sessions stalled by the weekly limit.
2. If the user wants it automatic, run `autoresume.py install`. This registers the watcher to start at login
   and starts it. Tell the user it runs in the background and resumes sessions without asking again.
3. To verify, run `autoresume.py status` (states: pending, running, done, superseded, skipped, interrupted).
4. To stop it, run `autoresume.py uninstall`.

## Other commands

- `autoresume.py watch` runs the watcher in the foreground. `--dry-run` logs without resuming.
- `autoresume.py run -C <dir> "<prompt>"` starts a new task and keeps resuming it across limit stops.
- `autoresume.py resume --last` or `resume <session-id>` does the same for an existing session.
- Pass codex options after `--`, e.g. `autoresume.py watch -- -s workspace-write`.

## Behaviour to explain to the user

- Resumed sessions run unattended and follow the user's `config.toml` sandbox and approval settings.
  Confirm they are comfortable with that before running `install`.
- A session is skipped if the user already continued it, or if its file changed in the last 2 minutes.
- Each stall is resumed once, and at most 10 times per session (`--max-resumes`).
- Subagent sessions are ignored; only the parent session is resumed.
- State and logs live in `$CODEX_HOME/autoresume/` (`state.json`, `watch.log`).
