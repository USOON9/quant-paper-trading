# Private GitHub publication

This repository publishes code and documentation only. A private repository is
not a credential store. Actual market data, audit trails, fitted models, account
state, local configuration and credentials remain on this computer.

## Allowed publication paths

- Root files: `.gitignore`, `.env.example`, `README.md`, `main.py`,
  `pyproject.toml`, `requirements-tested.txt`.
- Python files below `src/quantpaper/`, `tests/`, and `scripts/`.
- Markdown files below `docs/`; TOML files below `configs/`.

Everything else is outside the publication check's allowlist. In particular,
`artifacts/`, `data/`, virtual environments, caches, runtime databases, logs,
models, `.env` and other `.env.*` files must never be staged. `.env.example`
may contain empty credential fields or explicitly recognized placeholders only.
Symbolic links and non-regular files are rejected.

## Required checks before publication

Run the candidate check before adding files:

```sh
.venv/bin/python scripts/check_publish_secrets.py --mode candidates
```

Add only the allowed files/directories explicitly, then check the entire Git
index before committing and again before pushing:

```sh
.venv/bin/python scripts/check_publish_secrets.py --mode staged
```

Both commands must finish with `"ok": true`. The staged check examines the
actual staged blobs, so a safe working-tree copy cannot hide a secret still in
the index. Git ignore rules alone do not protect files force-added or previously
tracked. Keep the GitHub repository **private** and independently confirm its
visibility after creation.

## Credential handling and limitations

The checker reads only the project-local `.env` for credential comparison; it
does not read browser storage, operating-system credential stores, or GitHub
authentication files. Configured sensitive values of at least eight characters
are compared in memory against publication content, including staged blobs.
Reports contain only file paths, line numbers and rule names—not secret values.
Recognizable private-key material and provider tokens, suspicious high-entropy
credential literals, binary content, and unexpected paths are also blocked.

The check is a safety net, not proof that arbitrary secrets cannot exist.
Unconfigured or encoded credentials may evade heuristics. It does not audit
existing Git history; this workflow assumes a new repository with a clean
initial commit. Review the staged diff and file list. Never paste real keys into
source, tests, documentation, issues, commits, remote URLs, or `.gitignore`.
If a credential was ever committed, revoke/rotate it and clean the history;
deleting it in a later commit is not sufficient.
