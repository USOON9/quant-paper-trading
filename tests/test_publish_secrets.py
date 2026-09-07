"""Publication checks inspect synthetic fixtures only; no Git writes or network."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_publish_secrets.py"
SPEC = importlib.util.spec_from_file_location("publish_security_checks", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
import sys
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


class PublishSecretTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "src/quantpaper").mkdir(parents=True)
        (self.root / "tests").mkdir()
        (self.root / "README.md").write_text("# Synthetic code project\n")
        self.main = self.root / "src/quantpaper/main.py"
        self.main.write_text("print('safe code')\n")
        self.secret = "Ab9Q" + "m2Xr7Zp4Ks8Vn6Cw3Tj5Ld0H"
        (self.root / ".env").write_text("APCA_API_SECRET_KEY=" + self.secret + "\nENABLE_ALPACA_PAPER=NO\n")

    def rules(self, result):
        return {item["rule"] for item in result["issues"]}

    def index(self, files):
        """Mock read-only Git plumbing without creating or modifying repositories."""
        blobs = {str(i + 1).zfill(40): raw for i, (_, raw, _) in enumerate(files)}
        listing = b"".join(f"{mode} {oid} 0\t{name}".encode() + b"\0"
                           for (name, _, mode), oid in zip(files, blobs))
        def git(root, *args):
            self.assertEqual(root, self.root)
            if args == ("ls-files", "--stage", "-z"):
                return listing
            if args[:2] == ("cat-file", "-s"):
                return str(len(blobs[args[2]])).encode()
            if args[:2] == ("cat-file", "blob"):
                return blobs[args[2]]
            raise AssertionError("unexpected Git command")
        return patch.object(checker, "_git", side_effect=git)

    def test_code_only_allowlist_and_runtime_files_stay_unread(self):
        for directory in ("artifacts", "data", ".venv"):
            (self.root / directory).mkdir()
            (self.root / directory / "local-only.txt").write_text(self.secret)
        (self.root / "src/quantpaper/__pycache__").mkdir()
        (self.root / "src/quantpaper/__pycache__/main.pyc").write_bytes(b"\0" + self.secret.encode())
        (self.root / ".env.example").write_text("APCA_API_KEY_ID=\nAPCA_API_SECRET_KEY=your_secret_key_here\n")
        result = checker.scan(self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["files_scanned"], 3)
        self.assertTrue(result["credential_file_present"])
        for name in (".env", "data/a.py", "artifacts/a.py", "../secret.py", "/tmp/a.py",
                     "src/quantpaper/key.pem", "docs/secret.json", ".github/workflows/publish.yaml"):
            self.assertFalse(checker.allowed_path(name), name)

    def test_actual_configured_value_detected_without_outputting_value(self):
        self.main.write_text("# harmless header\nvalue = " + repr(self.secret) + "\n")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            result = checker.scan(self.root)
        self.assertIn("configured-credential-value", self.rules(result))
        issue = next(x for x in result["issues"] if x["rule"] == "configured-credential-value")
        self.assertEqual(issue["path"], "src/quantpaper/main.py")
        self.assertEqual(issue["line"], 2)
        self.assertNotIn(self.secret, json.dumps(result) + output.getvalue())

    def test_synthetic_looking_configured_value_is_still_detected(self):
        value = "synthetic-" + "known-private-credential"
        (self.root / ".env").write_text("GITHUB_TOKEN=" + value)
        self.main.write_text("value = " + repr(value))
        self.assertIn("configured-credential-value", self.rules(checker.scan(self.root)))

    def test_known_multiline_value_is_not_missed(self):
        value = "line-one-value\nline-two-value"
        issues = checker.scan_text("README.md", ("prefix\n" + value).encode(), {value})
        self.assertIn(checker.Issue("README.md", 2, "configured-credential-value"), issues)

    def test_credential_in_filename_is_detected_and_redacted(self):
        (self.root / "tests" / (self.secret + ".py")).write_text("safe content")
        result = checker.scan(self.root)
        self.assertIn("configured-credential-in-path", self.rules(result))
        self.assertNotIn(self.secret, json.dumps(result))

    def test_env_example_only_accepts_empty_or_exact_placeholders(self):
        path = self.root / ".env.example"
        for value in (self.secret, "not-a-approved-example-value", "your_" + self.secret):
            with self.subTest(case=value[:3]):
                path.write_text("APCA_API_SECRET_KEY=" + value)
                self.assertIn("example-credential-must-be-empty-or-explicit-placeholder",
                              self.rules(checker.scan(self.root)))
        path.write_text("APCA_API_SECRET_KEY=replace_me")
        self.assertTrue(checker.scan(self.root)["ok"])

    def test_generic_private_tokens_and_random_credential_literals_block(self):
        random = self.secret
        cases = [("github-token", "ghp_" + random),
                 ("github-token", "github_pat_" + random * 2),
                 ("aws-access-key", "AKIA" + "B2C3D4E5F6G7H8J9"),
                 ("alpaca-access-key", "PK" + "B2C3D4E5F6G7H8J9L0"),
                 ("private-key-material", "-----BEGIN " + "OPENSSH PRIVATE KEY-----"),
                 ("hardcoded-high-entropy-credential", "API_SECRET=" + repr(random))]
        for rule, text in cases:
            with self.subTest(rule=rule):
                found = checker.scan_text("src/quantpaper/a.py", text.encode(), set())
                self.assertIn(rule, {issue.rule for issue in found})

    def test_normal_synthetic_fixture_strings_do_not_trigger_generic_scan(self):
        phrases = ["synthetic-private-secret-sentinel", "dummy-key-for-offline-unit-tests",
                   "human-readable-fixture-secret-12345",
                   "a-human-readable-secret-fixture-phrase", "test-private-key-sentinel"]
        for phrase in phrases:
            content = ("API_SECRET=" + repr(phrase)).encode()
            self.assertEqual(checker.scan_text("tests/test_safe.py", content, set()), [])

    def test_symlink_files_and_ancestors_fail_closed_without_reading_target(self):
        outside = self.root / "outside-secret.txt"
        outside.write_text(self.secret)
        link = self.root / "tests/test_link.py"
        link.symlink_to(outside)
        result = checker.scan(self.root)
        self.assertIn("symbolic-link-forbidden", self.rules(result))
        self.assertNotIn("configured-credential-value", self.rules(result))
        link.unlink()
        directory_link = self.root / "tests/linked"
        directory_link.symlink_to(self.root / "artifacts", target_is_directory=True)
        self.assertIn("symbolic-link-forbidden", self.rules(checker.scan(self.root)))

    def test_env_symlink_is_never_followed(self):
        target = self.root / "local-private.txt"
        target.write_text("API_SECRET=" + self.secret)
        (self.root / ".env").unlink()
        (self.root / ".env").symlink_to(target)
        result = checker.scan(self.root)
        self.assertIn("credential-file-symlink-forbidden", self.rules(result))

    def test_malformed_env_is_not_silently_treated_as_no_known_credentials(self):
        (self.root / ".env").write_text("API_SECRET=\"unterminated\n")
        result = checker.scan(self.root)
        self.assertIn("credential-file-parse-warning", self.rules(result))

    def test_unexpected_source_files_binary_and_fifo_are_rejected(self):
        (self.root / "tests/private.pem").write_text("never publish")
        self.assertIn("outside-code-publication-allowlist", self.rules(checker.scan(self.root)))
        self.main.write_bytes(b"\0binary")
        self.assertIn("binary-content-forbidden", self.rules(checker.scan(self.root)))
        self.main.unlink()
        os.mkfifo(self.main)
        self.assertIn("nonregular-publication-file", self.rules(checker.scan(self.root)))

    def test_staged_checks_blob_not_safe_working_tree_content(self):
        blob = ("secret = " + repr(self.secret)).encode()
        with self.index([("src/quantpaper/main.py", blob, "100644")]):
            result = checker.scan(self.root, mode="staged")
        self.assertIn("configured-credential-value", self.rules(result))
        self.assertNotIn(self.secret, json.dumps(result))
        self.assertEqual(self.main.read_text(), "print('safe code')\n")

    def test_staged_forced_artifacts_env_and_symlink_entries_are_rejected(self):
        files = [("artifacts/private.json", b"{}", "100644"),
                 (".env", b"secret", "100644"),
                 ("src/quantpaper/linked.py", b"outside-target", "120000")]
        with self.index(files):
            result = checker.scan(self.root, mode="staged")
        self.assertIn("outside-code-publication-allowlist", self.rules(result))
        self.assertIn("nonregular-or-conflicted-index-entry", self.rules(result))
        self.assertEqual(result["files_scanned"], 0)

    def test_staged_safe_all_files_pass_and_git_errors_stay_sanitized(self):
        with self.index([("main.py", b"print('safe')\n", "100644"), ("README.md", b"safe docs", "100644")]):
            result = checker.scan(self.root, mode="staged")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["files_scanned"], 2)
        with patch.object(checker, "_git", side_effect=ValueError(self.secret)):
            result = checker.scan(self.root, mode="staged")
        self.assertFalse(result["ok"])
        self.assertNotIn(self.secret, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
