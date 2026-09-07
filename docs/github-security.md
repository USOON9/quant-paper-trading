# Private GitHub publication

This repository publishes code and documentation only. A private repository is
not a credential store. Actual market data, audit trails, fitted models, account
state, local configuration and credentials remain on this computer.

## Allowed publication paths

- Root files: `.gitignore`, `.env.example`, `README.md`, `main.py`,
  `pyproject.toml`, `requirements-tested.txt`.
- Exactly `.github/workflows/ci.yml`; no general YAML or workflow allowance.
- Python files below `src/quantpaper/`, `tests/`, and `scripts/`.
- Markdown files below `docs/`; TOML files below `configs/`.

Everything else is outside the publication check's allowlist. In particular,
`artifacts/`, `data/`, virtual environments, caches, runtime databases, logs,
models, `.env` and other `.env.*` files must never be staged. `.env.example`
may contain empty credential fields or explicitly recognized placeholders only.
Symbolic links and non-regular files are rejected.
The candidate check also inspects `.github/workflows/` to reject unexpected
workflow files. The allowlisted CI file and every ancestor are checked for
symbolic links, including dangling links, before reading file content.

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

## Offline continuous integration

The CI workflow runs on pushes to `main` and pull requests, on Ubuntu 24.04
with Python 3.11 and 3.14. Its token has `contents: read` only, checkout does
not persist credentials, and newer runs cancel older runs for the same ref.
It installs `.[ml,paper,data]`, checks publication candidates and every blob in
the checkout index, runs `python -m unittest discover -s tests -v`, and checks
`python main.py --help`. No strategy, collector, model-training CLI, or broker
command is launched. Test cases use synthetic data and mocked clients; no
market-data, broker, or LLM API credentials are supplied. Both paper order
gates default to `NO` in the job; gate-positive unit tests use mocked brokers.

The two official actions are pinned to immutable commits verified against
their official GitHub tag references on 2026-09-07:

- [actions/checkout v7.0.1](https://github.com/actions/checkout/releases/tag/v7.0.1):
  `3d3c42e5aac5ba805825da76410c181273ba90b1`.
- [actions/setup-python v7.0.0](https://github.com/actions/setup-python/releases/tag/v7.0.0):
  `5fda3b95a4ea91299a34e894583c3862153e4b97`.

"Offline" describes the application tests, not a runner network sandbox:
checkout, Python setup, and dependency installation need network access.
Package dependencies use the project's version ranges, not a hash-locked
dependency snapshot, so a green run is not a reproducible-build or
dependency-security attestation. No local `.env` is uploaded; consequently,
CI can apply generic secret rules but cannot compare against private values
known only on this computer. Local candidate and staged checks remain required.
The workflow does not deploy, submit orders, promote models, open gates, or
prove trading profitability. Review workflow changes as executable code; do
not switch this job to `pull_request_target` or add trading secrets.

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
