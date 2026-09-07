from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quantpaper.research.protocol import (
    freeze_manifest, load_protocol, protocol_hash, validate_protocol, verify_manifest,
)


PROTOCOL_PATH = Path(__file__).resolve().parents[1] / "configs" / "research_v3.toml"


class ResearchProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.data = self.directory / "prices.csv"
        self.code = self.directory / "model.py"
        self.data.write_text("session,close\n2026-09-03,10\n")
        self.code.write_text("MODEL_VERSION = 1\n")
        self.protocol = load_protocol(PROTOCOL_PATH)
        self.run = self.directory / "run_001"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def freeze(self):
        return freeze_manifest(self.run, self.protocol, [self.data], [self.code])

    def test_protocol_is_validated_and_hash_is_order_independent(self):
        self.assertEqual(self.protocol["historical_cutoff"], "2026-09-03")
        self.assertTrue(self.protocol["research_only"])
        reordered = dict(reversed(list(self.protocol.items())))
        self.assertEqual(protocol_hash(self.protocol), protocol_hash(reordered))
        detached = validate_protocol(self.protocol)
        detached["groups"]["crypto"]["costs_bps"].append(75)
        self.assertNotEqual(protocol_hash(detached), protocol_hash(self.protocol))

    def test_unknown_keys_unsafe_flags_and_bad_dates_are_rejected(self):
        invalid = [
            {"extra_setting": 1}, {"research_only": False}, {"research_only": 1},
            {"promotion_disabled": False}, {"executable_backtest": True},
            {"historical_cutoff": "2026-09-08"}, {"forward_start": "2026-09-02"},
            {"historical_cutoff": "2026-02-30"}, {"historical_cutoff": "09/03/2026"},
            {"position_policy": "long_short"}, {"target_contract_version": "daily_timing_v2"},
            {"protocol_id": "../unsafe"}, {"long_threshold": float("nan")},
            {"long_threshold": float("inf")}, {"long_threshold": True},
            {"models": ["hist_gradient_boosting"]},
        ]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_protocol({**self.protocol, **change})

    def test_group_universes_and_costs_cannot_be_silently_changed(self):
        changes = [
            ("symbols", []), ("symbols", ["SPY", "SPY"]), ("symbols", ["BTC-USD"]),
            ("costs_bps", []), ("costs_bps", [0, 5, 5]), ("costs_bps", [10, 0]),
            ("costs_bps", [0, float("inf")]), ("costs_bps", [False, 5]),
            ("selected_cost_bps", 6),
        ]
        for field, value in changes:
            protocol = deepcopy(self.protocol)
            protocol["groups"]["equities"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_protocol(protocol)
        protocol = deepcopy(self.protocol)
        protocol["validation"]["min_train_sessions"] = True
        with self.assertRaises(ValueError):
            validate_protocol(protocol)

    def test_existing_identical_snapshot_is_reused_without_overwriting(self):
        first = self.freeze()
        path = self.run / "manifest.json"
        content = path.read_bytes()
        before = path.stat().st_mtime_ns
        (self.run / "results.json").write_text("{}")
        second = self.freeze()
        self.assertEqual(first, second)
        self.assertEqual(content, path.read_bytes())
        self.assertEqual(before, path.stat().st_mtime_ns)
        self.assertEqual(first["input_files"][0]["path"], str(self.data.resolve()))
        self.assertFalse(first["executable_backtest"])
        verify_manifest(first, [self.data], [self.code])

    def test_data_mutation_rejects_reuse_and_postcompute_verification(self):
        first = self.freeze()
        original_manifest = (self.run / "manifest.json").read_bytes()
        self.data.write_text("session,close\n2026-09-03,999\n")
        with self.assertRaisesRegex(ValueError, "different research snapshot"):
            self.freeze()
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            verify_manifest(first, [self.data], [self.code])
        self.assertEqual((self.run / "manifest.json").read_bytes(), original_manifest)

    def test_code_and_protocol_mutations_reject_existing_directory(self):
        first = self.freeze()
        self.code.write_text("MODEL_VERSION = 2\n")
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            verify_manifest(first, [self.data], [self.code])
        self.code.write_text("MODEL_VERSION = 1\n")
        self.protocol["long_threshold"] = 0.56
        with self.assertRaisesRegex(ValueError, "different research snapshot"):
            self.freeze()

    def test_manifest_tampering_and_truncation_are_not_overwritten(self):
        manifest = self.freeze()
        path = self.run / "manifest.json"
        manifest["protocol"]["long_threshold"] = 0.7
        path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "integrity hash mismatch"):
            self.freeze()
        with self.assertRaisesRegex(ValueError, "integrity hash mismatch"):
            verify_manifest(manifest, [self.data], [self.code])
        path.write_text('{"partial":')
        with self.assertRaisesRegex(ValueError, "unreadable or incomplete"):
            self.freeze()
        self.assertEqual(path.read_text(), '{"partial":')

    def test_secret_names_symlinks_and_traversal_are_rejected(self):
        secret = self.directory / ".env"
        secret.write_text("DUMMY_TEST_VALUE=not-a-secret")
        for file in [secret, self.directory / "child" / ".." / "prices.csv"]:
            with self.subTest(file=file.name), self.assertRaises(ValueError):
                freeze_manifest(self.run, self.protocol, [file], [self.code])
        link = self.directory / "innocent.csv"
        link.symlink_to(secret)
        with self.assertRaises(ValueError):
            freeze_manifest(self.run, self.protocol, [link], [self.code])
        self.assertFalse(self.run.exists())

    def test_unsafe_or_unmanifested_output_paths_are_rejected(self):
        for run in [self.directory / ".." / "escape", self.directory / ".hidden"]:
            with self.subTest(run=run.name), self.assertRaises(ValueError):
                freeze_manifest(run, self.protocol, [self.data], [self.code])
        self.run.mkdir()
        (self.run / "old_results.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            self.freeze()
        link = self.directory / "linked_run"
        link.symlink_to(self.run, target_is_directory=True)
        with self.assertRaises(ValueError):
            freeze_manifest(link, self.protocol, [self.data], [self.code])

    def test_failed_atomic_publication_leaves_no_manifest_or_temp_file(self):
        with patch("quantpaper.research.protocol.os.link", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(OSError, "disk error"):
                self.freeze()
        self.assertEqual(list(self.run.iterdir()), [])
        self.assertIn("manifest_hash", self.freeze())


if __name__ == "__main__":
    unittest.main()
