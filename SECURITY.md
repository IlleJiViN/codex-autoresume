# Security

## What this tool does

`codex-autoresume` runs `codex exec resume` **unattended** on sessions that stopped on a usage limit. A resumed
session can run commands and edit files under whatever sandbox and approval policy your Codex `config.toml`
defines. If you use `approval_policy = "never"` with a permissive sandbox, resumed work is fully autonomous.

- It only reads `$CODEX_HOME/sessions/*.jsonl` and writes to `$CODEX_HOME/autoresume/`.
- It makes no network requests of its own and has no dependencies beyond the Python standard library.
- It never touches `auth.json` or other credentials.

Recommended: run `autoresume.py list` and `watch --dry-run` first, and pass a restrictive sandbox via
`watch -- -s workspace-write` if you want it stricter than your default.

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security tab -> Report a vulnerability) rather than a public issue.
