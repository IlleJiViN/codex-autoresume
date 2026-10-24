# Contributing

Thanks for helping out. The project is standard-library Python (3.8+), so there is nothing to install.

```bash
python -m unittest discover -s tests -v
```

- Tests build fake Codex session logs and a fake `codex` binary; they never call the real CLI or touch `~/.codex`.
- Codex's log format is not a public API. If a Codex update changes it, a failing `DetectTests` case plus a
  sample (redacted) `task_complete` line in the issue is the most useful report.
- Keep the plugin dependency-free and cross-platform. Windows-only code must be a no-op elsewhere.
