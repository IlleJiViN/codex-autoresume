# codex-autoresume

[![tests](https://github.com/IlleJiViN/codex-autoresume/actions/workflows/test.yml/badge.svg)](https://github.com/IlleJiViN/codex-autoresume/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A [Codex](https://github.com/openai/codex) plugin that finds sessions stopped by a **usage limit** (the 5-hour or weekly
window) and **resumes them automatically** when the limit resets, so long tasks finish while you are away.

- Works with sessions started anywhere: Codex desktop app, CLI, or IDE. It reads the local session logs.
- Resumes the *same* session with `codex exec resume`, in its original working directory.
- Zero dependencies. Python 3.8+ standard library and the `codex` CLI.
- Windows, macOS and Linux (start-at-login via Startup folder, launchd or systemd).

## Install

```bash
codex plugin marketplace add IlleJiViN/codex-autoresume
codex plugin add codex-autoresume@codex-autoresume
```

Then ask Codex: *"Set up auto-resume for my usage limit"*. The bundled `autoresume` skill runs the steps below for you.

You can also use the CLI directly without installing the plugin: clone the repo and run
`python plugins/codex-autoresume/scripts/autoresume.py <command>`.

## Usage

```bash
autoresume.py list                 # sessions stalled on a limit, and when each will resume (nothing is run)
autoresume.py watch --dry-run      # log what the watcher would do
autoresume.py install              # start the watcher in the background now and at every login
autoresume.py status               # what the watcher detected / resumed
autoresume.py uninstall            # stop and remove it
```

Run a task directly and have it survive limit stops:

```bash
autoresume.py run -C ~/proj "implement the importer and make the tests pass"
autoresume.py resume --last
autoresume.py run --start-at 02:00 "large refactor"     # delay the start
autoresume.py watch -- -s workspace-write               # options after -- go to codex
```

## How it works

1. Every 60 s the watcher scans `$CODEX_HOME/sessions/**/*.jsonl` (cached by mtime and size).
2. A session counts as stalled when its **last turn event** is a `task_complete` with
   `codex_error_info: "usage_limit_exceeded"`. Subagent sessions are ignored.
3. The wake time is the later of the exhausted window's `rate_limits.resets_at` and the message's `try again at ...`,
   plus a 90 s buffer. If neither exists it assumes 5 h 05 min.
4. At that time it runs `codex exec resume <id> "<continue prompt>"` in the session's `cwd`.

Safeguards: each stall is resumed once (recorded in `state.json`), at most 10 times per session; a session you already
continued is skipped; a session file touched in the last 2 minutes is treated as in use and retried later;
Windows sleep is blocked while a resume is pending.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--max-age` | 6 | Ignore stalls older than this many hours. Use `170` to include weekly-limit stalls. |
| `--buffer` | 90 | Extra seconds after the reset time. |
| `--interval` | 60 | Seconds between scans. |
| `--prompt` | built-in | Prompt sent on resume. |
| `--max-resumes` | 10 | Per-session cap. |
| `--idle-guard` | 120 | Seconds of file inactivity required before resuming. |
| `--once` / `--dry-run` | off | Single pass / log only. |

State and logs: `$CODEX_HOME/autoresume/` (`state.json`, `watch.log`, `run.log`). Override with `CODEX_AUTORESUME_HOME`.

## Safety

Resumed sessions run **unattended** with your `config.toml` sandbox and approval policy. See [SECURITY.md](SECURITY.md).
Try `list` and `watch --dry-run` first.

## Limitations

- Depends on Codex's session-log format, which is not a public API and may change.
- The watcher must be running at the reset time (the computer on and logged in).
- `resume` continues the conversation with a generic prompt; it cannot restore uncommitted in-memory state beyond what
  the session log holds.
- Verified against Codex CLI 0.163 session logs. Tests use synthetic logs and a fake `codex`.

## Development

```bash
python -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Released under the [MIT License](LICENSE).
