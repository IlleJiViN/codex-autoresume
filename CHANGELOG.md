# Changelog

## 0.1.0

- Initial release.
- `watch`: detects Codex sessions whose last turn ended with `usage_limit_exceeded` and resumes them at the reset time.
- `run` / `resume`: run a task and keep resuming it across limit stops.
- `list` / `status`: inspect stalled sessions and watcher history.
- `install` / `uninstall`: start the watcher at login (Windows Startup, launchd, systemd user service).
- Codex plugin with an `autoresume` skill and a local marketplace manifest.
