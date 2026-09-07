#!/usr/bin/env python3
"""Fail-closed code-only publication check; never print credential values.

Candidate mode inspects only explicit source/documentation roots. Staged mode
inspects every blob in the Git index, not merely changed files. The local .env
is read solely to detect accidental copies of configured credential values.
This is a pre-publication safety check, not a guarantee that no secret exists.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import redirect_stderr
from dataclasses import asdict, dataclass
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = frozenset({".gitignore", ".env.example", "README.md", "main.py",
                        "pyproject.toml", "requirements-tested.txt", ".github/workflows/ci.yml"})
CODE_ROOTS = {"src/quantpaper": ".py", "tests": ".py", "scripts": ".py",
              "docs": ".md", "configs": ".toml"}
SKIP_DIRECTORIES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
MAX_FILES = 2000
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 20_000_000
SENSITIVE_NAME = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|PAT)(?:_|$)", re.I)
PLACEHOLDERS = frozenset({"", "changeme", "replace_me", "your_api_key", "your_api_key_here",
    "your_secret_key", "your_secret_key_here", "your_paper_api_key", "your_paper_secret_key",
    "your_paper_key_here", "your_paper_secret_here", "your_alpaca_api_key", "your_alpaca_secret_key",
    "your_fred_api_key", "your_fred_api_key_here"})
GENERIC_PATTERNS = (
    ("private-key-material", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{40,255})\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("alpaca-access-key", re.compile(r"\b[AP]K[A-Z0-9]{18}\b")),
)
ASSIGNMENT = re.compile(
    r"(?i)\b(?:[A-Z0-9_]*API_KEY[A-Z0-9_]*|[A-Z0-9_]*SECRET[A-Z0-9_]*|"
    r"[A-Z0-9_]*TOKEN[A-Z0-9_]*|PASSWORD|PASSWD)\b[\"']?\s*[:=]\s*"
    r"[\"'](?P<value>[A-Za-z0-9/+_.=\-]{16,})[\"']"
)


@dataclass(frozen=True, order=True)
class Issue:
    path: str
    line: int
    rule: str


def allowed_path(name: str) -> bool:
    path = PurePosixPath(name)
    if (not name or name != path.as_posix() or path.is_absolute() or ".." in path.parts or "\\" in name
            or any(part in SKIP_DIRECTORIES for part in path.parts)):
        return False
    if name in ROOT_FILES:
        return True
    return any(name.startswith(prefix + "/") and path.suffix == suffix
               and all(not part.startswith(".") for part in path.parts)
               for prefix, suffix in CODE_ROOTS.items())


def _has_symlink(root: Path, name: str) -> bool:
    # Walk from the trusted root outward: checking the leaf first would itself
    # traverse a symlinked ancestor while inspecting leaf metadata.
    path = root
    for part in PurePosixPath(name).parts:
        path = path / part
        if path.is_symlink():
            return True
    return False


def candidate_paths(root: Path) -> tuple[list[str], list[Issue]]:
    """Walk only the publication roots; never inspect data/artifacts/.git."""
    names = []
    issues = []
    for name in ROOT_FILES:
        # An exact allowlisted file may have nested ancestors (the CI workflow).
        # Check those before exists/stat so even a dangling ancestor link fails
        # closed without following it to inspect an external target.
        if _has_symlink(root, name):
            issues.append(Issue(name, 0, "symbolic-link-forbidden"))
        elif (root / name).exists():
            names.append(name)
    # Inspect this one directory, but do not grant a general workflow/YAML
    # allowance: any file other than the exact ROOT_FILES entry is rejected.
    for prefix in (*CODE_ROOTS, ".github/workflows"):
        directory = root / prefix
        if _has_symlink(root, prefix):
            issues.append(Issue(prefix, 0, "symbolic-link-forbidden"))
            continue
        if not directory.exists():
            continue
        if not directory.is_dir():
            issues.append(Issue(prefix, 0, "source-root-not-directory"))
            continue
        for parent, dirs, files in os.walk(directory, followlinks=False):
            for dirname in list(dirs):
                path = Path(parent) / dirname
                if path.is_symlink():
                    issues.append(Issue(path.relative_to(root).as_posix(), 0, "symbolic-link-forbidden"))
                    dirs.remove(dirname)
                elif dirname in SKIP_DIRECTORIES:
                    dirs.remove(dirname)
            for filename in files:
                if filename == ".DS_Store":
                    continue
                path = Path(parent) / filename
                name = path.relative_to(root).as_posix()
                if path.is_symlink():
                    issues.append(Issue(name, 0, "symbolic-link-forbidden"))
                else:
                    names.append(name)
    return sorted(set(names)), issues


def _configured_values(root: Path) -> tuple[set[str], bool, list[Issue]]:
    path = root / ".env"
    present = path.exists() or path.is_symlink()
    if path.is_symlink():
        return set(), present, [Issue(".env", 0, "credential-file-symlink-forbidden")]
    if not present:
        return set(), False, []
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        return set(), present, [Issue(".env", 0, "credential-file-unreadable-or-oversize")]
    try:
        diagnostics = io.StringIO()
        with redirect_stderr(diagnostics):
            values = dotenv_values(path, interpolate=False)
        if diagnostics.getvalue():
            return set(), present, [Issue(".env", 0, "credential-file-parse-warning")]
        secrets = {value for name, value in values.items()
                   if SENSITIVE_NAME.search(name) and isinstance(value, str) and len(value) >= 8
                   and value.lower() not in PLACEHOLDERS}
        return secrets, True, []
    except Exception:
        return set(), present, [Issue(".env", 0, "credential-file-read-failed")]


def _entropy(value: str) -> float:
    return -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())


def _fixture_or_placeholder(value: str) -> bool:
    return (value.lower() in PLACEHOLDERS
            or re.fullmatch(r"(?:synthetic|fixture|dummy|fake|test)[_-][A-Za-z0-9_-]+", value, re.I) is not None
            # Human-readable fixture phrases are not random credential literals.
            # Configured .env value matching remains unconditional, even here.
            or re.fullmatch(r"[a-z]+(?:-[a-z]+){2,}(?:-[0-9]+)?", value, re.I) is not None)


def scan_text(name: str, raw: bytes, configured_values: set[str]) -> list[Issue]:
    issues = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [Issue(name, 0, "non-utf8-content-forbidden")]
    if "\x00" in text:
        issues.append(Issue(name, 0, "binary-content-forbidden"))
    for value in configured_values:
        offset = text.find(value)
        if offset >= 0:
            issues.append(Issue(name, text.count("\n", 0, offset) + 1, "configured-credential-value"))
    for number, line in enumerate(text.splitlines(), 1):
        for rule, pattern in GENERIC_PATTERNS:
            if any(rule == "private-key-material" or _entropy(match.group()) >= 3
                   for match in pattern.finditer(line)):
                issues.append(Issue(name, number, rule))
        for match in ASSIGNMENT.finditer(line):
            value = match.group("value")
            if not _fixture_or_placeholder(value) and len(value) >= 20 and _entropy(value) >= 3.5:
                issues.append(Issue(name, number, "hardcoded-high-entropy-credential"))
    if name == ".env.example":
        try:
            diagnostics = io.StringIO()
            with redirect_stderr(diagnostics):
                values = dotenv_values(stream=io.StringIO(text), interpolate=False)
            if diagnostics.getvalue():
                issues.append(Issue(name, 0, "example-environment-parse-warning"))
            for key, value in values.items():
                if SENSITIVE_NAME.search(key) and value is not None and value.lower() not in PLACEHOLDERS:
                    number = next((i for i, line in enumerate(text.splitlines(), 1)
                                   if re.match(r"\s*(?:export\s+)?" + re.escape(key) + r"\s*=", line)), 0)
                    issues.append(Issue(name, number, "example-credential-must-be-empty-or-explicit-placeholder"))
        except Exception:
            issues.append(Issue(name, 0, "example-environment-parse-failed"))
    return sorted(set(issues))


def _git(root: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(root), "-c", "core.fsmonitor=false", *args],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=30)
    if result.returncode:
        raise ValueError("git-read-failed")
    return result.stdout


def scan(root: Path = PROJECT_ROOT, *, mode: str = "candidates") -> dict:
    root = root.resolve()
    values, env_present, issues = _configured_values(root)
    staged = {}
    if mode == "candidates":
        names, path_issues = candidate_paths(root)
        issues.extend(path_issues)
    elif mode == "staged":
        names = []
        try:
            for entry in _git(root, "ls-files", "--stage", "-z").split(b"\x00"):
                if not entry:
                    continue
                metadata, encoded_name = entry.split(b"\t", 1)
                filemode, oid, stage = metadata.decode("ascii").split()
                name = encoded_name.decode("utf-8")
                names.append(name)
                if filemode not in {"100644", "100755"} or stage != "0":
                    issues.append(Issue(name, 0, "nonregular-or-conflicted-index-entry"))
                    continue
                if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
                    raise ValueError("invalid-object-id")
                staged[name] = oid
        except (ValueError, UnicodeError, OSError, subprocess.SubprocessError):
            return {"mode": mode, "credential_file_present": env_present, "files_scanned": 0,
                    "bytes_scanned": 0, "issues": [asdict(Issue(".git/index", 0, "git-index-read-failed"))], "ok": False}
    else:
        raise ValueError("mode must be candidates or staged")
    if not names:
        issues.append(Issue(".", 0, "empty-publication-set"))
    if len(names) > MAX_FILES:
        issues.append(Issue(".", 0, "publication-file-budget-exceeded"))
        names = []
    scanned = total = 0
    for name in names:
        if any(value in name for value in values):
            issues.append(Issue(name, 0, "configured-credential-in-path"))
            continue
        if not allowed_path(name):
            issues.append(Issue(name, 0, "outside-code-publication-allowlist"))
            continue
        if _has_symlink(root, name):
            issues.append(Issue(name, 0, "symbolic-link-forbidden"))
            continue
        if mode == "staged" and name not in staged:
            continue
        try:
            if mode == "staged":
                size = int(_git(root, "cat-file", "-s", staged[name]))
            else:
                metadata = (root / name).stat()
                if not stat.S_ISREG(metadata.st_mode):
                    issues.append(Issue(name, 0, "nonregular-publication-file"))
                    continue
                size = metadata.st_size
            if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                issues.append(Issue(name, 0, "publication-byte-budget-exceeded"))
                continue
            raw = _git(root, "cat-file", "blob", staged[name]) if mode == "staged" else (root / name).read_bytes()
            if len(raw) != size:
                issues.append(Issue(name, 0, "content-changed-during-read"))
                continue
        except (ValueError, OSError, subprocess.SubprocessError):
            issues.append(Issue(name, 0, "publication-content-read-failed"))
            continue
        scanned += 1
        total += len(raw)
        issues.extend(scan_text(name, raw, values))
    sanitized = []
    for issue in sorted(set(issues)):
        name = issue.path
        for value in values:
            name = name.replace(value, "<redacted>")
        sanitized.append(asdict(Issue(name, issue.line, issue.rule)))
    return {"mode": mode, "credential_file_present": env_present, "files_scanned": scanned,
            "bytes_scanned": total, "issues": sanitized, "ok": not issues}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("candidates", "staged"), default="candidates")
    args = parser.parse_args(argv)
    report = scan(mode=args.mode)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
