"""Offline current-context catalog integration and adversarial archive checks."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import duckdb

from quantpaper.research import catalog, catalog_cli
from quantpaper.research.evidence import canonical_bytes, digest
from quantpaper.research.refresh import RefreshConfiguration
from quantpaper.sources.fred_metadata import FREDSeriesMetadataClient
from quantpaper.warehouse import DDL


UTC = timezone.utc
START = datetime(2026, 9, 7, 13, tzinfo=UTC)
OBSERVED = START + timedelta(seconds=1)
GENERATED = START + timedelta(seconds=3)
SYNTHETIC_KEY = "0123456789abcdef" * 2


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.database = self.root / "data" / "research.duckdb"
        self.database.parent.mkdir()
        with duckdb.connect(str(self.database)) as connection:
            connection.execute(DDL)
            connection.execute("INSERT INTO schema_versions(version) VALUES (1), (2), (3), (4)")
        self.run_dir = self.root / "artifacts" / "research-catalog" / "synthetic-001"
        self.configuration = RefreshConfiguration(fred_api_key=SYNTHETIC_KEY)
        self.insert_identity()

    def tearDown(self):
        self.temporary.cleanup()

    def insert_identity(self, **changes):
        row = {"instrument_id": "YF:JPM", "symbol": "JPM", "asset_class": "equity",
               "primary_exchange": None, "source": "yahoo", "valid_from": "1980-03-17T05:00:00Z",
               "valid_to": None, "available_at": "2026-09-04T12:00:00Z",
               "ingested_at": "2026-09-04T12:01:00Z", **changes}
        columns = ", ".join(row)
        with duckdb.connect(str(self.database)) as connection:
            connection.execute(f"INSERT INTO instruments ({columns}) VALUES ({', '.join('?' for _ in row)})", list(row.values()))

    @staticmethod
    def metadata_payload(series="DGS10"):
        return {"realtime_start": "2026-09-07", "realtime_end": "2026-09-07", "seriess": [{
            "id": series, "realtime_start": "2026-09-07", "realtime_end": "2026-09-07",
            "title": "Synthetic macro series", "observation_start": "1962-01-02",
            "observation_end": "2026-09-04", "frequency": "Daily", "frequency_short": "D",
            "units": "Percent", "units_short": "%", "seasonal_adjustment": "Not Seasonally Adjusted",
            "seasonal_adjustment_short": "NSA", "last_updated": "2026-09-04 15:16:03-05",
            "notes": "Synthetic private response detail that must not be archived.",
        }]}

    def batch(self, series="DGS10", *, payload=None, observed=OBSERVED):
        ticks = iter([START, observed])
        provider = Mock(return_value=self.metadata_payload(series) if payload is None else payload)
        return FREDSeriesMetadataClient(SYNTHETIC_KEY, provider, lambda: next(ticks)).fetch(series)

    def factory(self, batch=None, *, error=None):
        return Mock(return_value=SimpleNamespace(fetch=Mock(
            return_value=self.batch() if batch is None else batch, side_effect=error)))

    def build(self, **changes):
        first = True
        def clock():
            nonlocal first
            if first:
                first = False
                return START
            return GENERATED
        options = {"project_root": self.root, "instrument_id": "YF:JPM", "series": ("DGS10",),
                   "configuration": self.configuration, "client_factory": self.factory(), "now": clock,
                   **changes}
        directory = options.pop("run_dir", self.run_dir)
        return catalog.build_catalog(self.database, directory, **options)

    def read(self):
        return catalog.read_catalog(self.run_dir, project_root=self.root)

    def rewrite_artifact_hash(self, name):
        path = self.run_dir / "completion.json"
        completion = json.loads(path.read_bytes())
        completion["artifacts"][name] = hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest()
        path.write_bytes(canonical_bytes(completion))

    def rewrite_catalog_and_links(self, value):
        value["catalog_hash"] = digest({key: item for key, item in value.items() if key != "catalog_hash"})
        (self.run_dir / "catalog.json").write_bytes(canonical_bytes(value))
        manifest_path = self.run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        manifest["catalog_hash"] = value["catalog_hash"]
        manifest["manifest_hash"] = digest({key: item for key, item in manifest.items() if key != "manifest_hash"})
        manifest_path.write_bytes(canonical_bytes(manifest))
        completion_path = self.run_dir / "completion.json"
        completion = json.loads(completion_path.read_bytes())
        completion["catalog_hash"] = value["catalog_hash"]
        completion["manifest_hash"] = manifest["manifest_hash"]
        for name in ("catalog.json", "manifest.json"):
            completion["artifacts"][name] = hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest()
        completion_path.write_bytes(canonical_bytes(completion))

    def test_complete_catalog_is_verifiable_and_preserves_database_and_existing_files(self):
        names = (".env", "data/yahoo/JPM.csv", "artifacts/yahoo_walkforward_v2_model.json",
                 "artifacts/research-evidence/frozen/packet.json", "data/paper-state/synthetic.json")
        before = {"database": hashlib.sha256(self.database.read_bytes()).hexdigest()}
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic protected fixture\n")
            before[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        value = self.build()
        self.assertEqual(value["status"], "COMPLETE")
        self.assertEqual(self.read(), value)
        self.assertEqual(value["identity"]["status"], "SINGLE_ASSERTION")
        self.assertEqual(value["macro_metadata"]["DGS10"]["status"], "AVAILABLE")
        self.assertEqual(before["database"], hashlib.sha256(self.database.read_bytes()).hexdigest())
        for name in names:
            self.assertEqual(before[name], hashlib.sha256((self.root / name).read_bytes()).hexdigest())
        serialized = json.dumps(value)
        self.assertNotIn(SYNTHETIC_KEY, serialized)
        self.assertNotIn("Synthetic private response detail", serialized)
        self.assertIs(value["warehouse_modified"], False)
        self.assertIs(value["model_changed"], False)
        self.assertIs(value["execution_enabled"], False)

    def test_equivalent_assertions_preserve_both_original_validity_dates(self):
        self.insert_identity(valid_from="2024-09-06T04:00:00Z", available_at="2026-09-07T10:00:00Z")
        value = self.build()
        identity = value["identity"]
        self.assertEqual(identity["status"], "EQUIVALENT_ASSERTIONS")
        self.assertEqual((identity["assertion_count"], identity["group_count"]), (2, 1))
        self.assertEqual({item["valid_from"] for item in identity["assertions"]},
                         {"1980-03-17T05:00:00.000000+00:00", "2024-09-06T04:00:00.000000+00:00"})
        self.assertIs(identity["historical_identity_verified"], False)

    def test_conflicting_provider_descriptor_is_partial_without_alias_resolution(self):
        self.insert_identity(source="independent", primary_exchange="XNYS")
        value = self.build()
        self.assertEqual(value["status"], "PARTIAL")
        self.assertEqual(value["identity"]["status"], "AMBIGUOUS")
        self.assertIsNone(value["identity"]["resolved_attributes"])
        self.assertEqual(self.read(), value)

    def test_truncated_read_retains_501st_sentinel_and_never_resolves_subset(self):
        with duckdb.connect(str(self.database)) as connection:
            connection.executemany("""INSERT INTO instruments
                (instrument_id, symbol, asset_class, primary_exchange, source, valid_from, available_at, ingested_at)
                VALUES ('YF:JPM', 'JPM', 'equity', NULL, ?, '2024-01-01T00:00:00Z',
                        '2026-09-04T12:00:00Z', '2026-09-04T12:01:00Z')""",
                [(f"synthetic-{index:03d}",) for index in range(501)])
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        value = self.build()
        self.assertEqual(value["identity"]["status"], "TRUNCATED")
        self.assertEqual(value["identity"]["assertion_count"], 501)
        self.assertFalse(value["identity"]["complete"])
        self.assertIsNone(value["identity"]["resolved_attributes"])
        self.assertEqual(value["status"], "PARTIAL")
        self.assertEqual(self.read(), value)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_identity_selection_uses_strict_local_cutoff_before_grouping(self):
        for index, changes in enumerate(({"available_at": START.isoformat()}, {"ingested_at": START.isoformat()},
                                         {"valid_to": START.isoformat()}, {"valid_from": GENERATED.isoformat()})):
            self.insert_identity(source=f"excluded-{index}", **changes)
        value = self.build()
        self.assertEqual(value["identity"]["assertion_count"], 1)
        self.assertEqual(value["identity"]["status"], "SINGLE_ASSERTION")

    def test_missing_configuration_skips_client_and_reports_partial(self):
        provider = Mock(side_effect=AssertionError("must not construct a client"))
        value = self.build(configuration=RefreshConfiguration(), client_factory=provider)
        provider.assert_not_called()
        self.assertEqual(value["status"], "PARTIAL")
        self.assertEqual(value["macro_metadata"]["DGS10"]["status"], "MISSING_CONFIGURATION")
        self.assertEqual(self.read(), value)

    def test_factory_and_provider_failures_never_expose_private_exception_text(self):
        private = "synthetic-private-authenticated-url"
        factories = (Mock(side_effect=RuntimeError(private)), self.factory(error=RuntimeError(private)))
        for index, factory in enumerate(factories):
            directory = self.run_dir.with_name(f"failure-{index}")
            value = self.build(run_dir=directory, client_factory=factory)
            self.assertEqual(value["macro_metadata"]["DGS10"]["status"], "FAILED")
            for path in directory.iterdir():
                self.assertNotIn(private.encode(), path.read_bytes())

    def test_malformed_noncanonical_wrong_identity_and_private_request_batches_fail_closed(self):
        good = self.batch()
        bad_record = deepcopy(good.records[0])
        bad_record["observed_at"] = "2026-09-07T13:00:01+00:00"
        extra_record = {**good.records[0], "private_field": "must-not-archive"}
        wrong_record = {**good.records[0], "series_id": "DFF"}
        bad_batches = (replace(good, source="other"), replace(good, records=[]),
                       replace(good, records=[None]), replace(good, records=[bad_record]),
                       replace(good, records=[extra_record]), replace(good, records=[wrong_record]),
                       replace(good, request={**good.request, "api_key": "must-not-archive"}),
                       replace(good, content_hash="not-a-hash"))
        for index, batch in enumerate(bad_batches):
            directory = self.run_dir.with_name(f"malformed-{index}")
            value = self.build(run_dir=directory, client_factory=self.factory(batch))
            self.assertEqual(value["macro_metadata"]["DGS10"]["status"], "FAILED")
            self.assertEqual(value["status"], "PARTIAL")
            self.assertNotIn("must-not-archive", json.dumps(value))

    def test_metadata_is_unavailable_at_or_before_generation_even_if_provider_updated_earlier(self):
        value = self.build()
        for cutoff in ("2026-09-04T23:00:00Z", START.isoformat(), OBSERVED.isoformat(),
                       (GENERATED - timedelta(microseconds=1)).isoformat(), GENERATED.isoformat()):
            with self.subTest(cutoff=cutoff):
                result = catalog.metadata_as_of(value, cutoff)
                self.assertEqual(result["macro_metadata"]["DGS10"], {"status": "UNAVAILABLE_AT_CUTOFF", "record": None})
        available = catalog.metadata_as_of(value, (GENERATED + timedelta(microseconds=1)).isoformat())
        self.assertEqual(available["macro_metadata"]["DGS10"]["status"], "AVAILABLE")
        self.assertIs(available["features_generated"], False)
        self.assertIs(available["identity_reinterpreted"], False)
        with self.assertRaises(ValueError):
            catalog.metadata_as_of(value, "2026-09-07T13:00:04")

    def test_observation_outside_run_or_backward_final_clock_is_rejected(self):
        good = self.batch()
        record = deepcopy(good.records[0])
        record["observed_at"] = record["available_at"] = "2026-09-07T12:59:59.000000Z"
        value = self.build(client_factory=self.factory(replace(good, records=[record])))
        self.assertEqual(value["macro_metadata"]["DGS10"]["status"], "FAILED")
        directory = self.run_dir.with_name("backward-clock")
        clock = Mock(side_effect=[START, GENERATED, START])
        with self.assertRaises(ValueError):
            self.build(run_dir=directory, now=clock)
        self.assertFalse(directory.exists())

    def test_invalid_request_is_rejected_before_client_or_directory_creation(self):
        provider = Mock()
        for changes in ({"instrument_id": "bad id"}, {"series": ()}, {"series": ("dgs10",)},
                        {"series": "DGS10"}, {"series": ("DGS10", "DGS10")},
                        {"series": ("A", "B", "C", "D", "E")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.build(client_factory=provider, **changes)
        provider.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_existing_symlink_and_traversal_outputs_fail_before_client(self):
        self.run_dir.mkdir(parents=True)
        marker = self.run_dir / "owned.txt"
        marker.write_text("preserve", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.run_dir.with_name("linked")
        linked.symlink_to(outside, target_is_directory=True)
        provider = Mock()
        paths = (self.run_dir, linked, self.run_dir.parent, self.root / "data" / "catalog",
                 self.run_dir.parent / ".." / "escaped")
        for path in paths:
            with self.subTest(path=str(path)), self.assertRaises(ValueError):
                self.build(run_dir=path, client_factory=provider)
        provider.assert_not_called()
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual(list(outside.iterdir()), [])

    def test_schema_failure_does_not_create_or_migrate_database(self):
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("DELETE FROM schema_versions WHERE version=4")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        provider = Mock()
        with self.assertRaises(ValueError):
            self.build(client_factory=provider)
        provider.assert_not_called()
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        self.assertFalse(self.run_dir.exists())

    def test_source_fingerprint_change_prevents_archive_creation(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with patch.object(catalog, "_fingerprint", side_effect=[{"sha256": "first"}, {"sha256": "second"}]):
            with self.assertRaises(ValueError):
                self.build()
        self.assertFalse(self.run_dir.exists())
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_fingerprint_is_captured_before_identity_read_can_change_source_state(self):
        state = {"revision": 0}
        original_reader = catalog._read_identity
        def read_then_change(*args, **kwargs):
            rows = original_reader(*args, **kwargs)
            state["revision"] = 1
            return rows
        with (patch.object(catalog, "_read_identity", side_effect=read_then_change),
              patch.object(catalog, "_fingerprint", side_effect=lambda: dict(state))):
            with self.assertRaisesRegex(ValueError, "builder changed"):
                self.build()
        self.assertFalse(self.run_dir.exists())

    def test_archive_rejects_missing_changed_and_symlinked_artifacts(self):
        self.build()
        report_path = self.run_dir / "report.md"
        original = report_path.read_bytes()
        report_path.write_bytes(original + b"changed")
        with self.assertRaises(ValueError):
            self.read()
        report_path.unlink()
        with self.assertRaises(OSError):
            self.read()
        outside = self.root / "outside.md"
        outside.write_bytes(original)
        report_path.symlink_to(outside)
        with self.assertRaises(ValueError):
            self.read()

    def test_rehashed_display_text_cannot_disagree_with_validated_catalog(self):
        self.build()
        report_path = self.run_dir / "report.md"
        report_path.write_text("# Claimed trade approval\n", encoding="utf-8")
        self.rewrite_artifact_hash("report.md")
        with self.assertRaisesRegex(ValueError, "display"):
            self.read()

    def test_rehashed_manifest_cannot_omit_builder_dependencies(self):
        self.build()
        path = self.run_dir / "manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["builder"]["files"].pop(next(iter(manifest["builder"]["files"])))
        manifest["builder"]["sha256"] = digest(manifest["builder"]["files"])
        manifest["manifest_hash"] = digest({key: item for key, item in manifest.items() if key != "manifest_hash"})
        path.write_bytes(canonical_bytes(manifest))
        self.rewrite_artifact_hash("manifest.json")
        completion_path = self.run_dir / "completion.json"
        completion = json.loads(completion_path.read_bytes())
        completion["manifest_hash"] = manifest["manifest_hash"]
        completion_path.write_bytes(canonical_bytes(completion))
        with self.assertRaisesRegex(ValueError, "builder"):
            self.read()

    def test_rehashed_identity_request_flags_and_series_metadata_tampering_are_rejected(self):
        mutations = (
            lambda value: value["identity"].update(historical_identity_verified=True),
            lambda value: value["request"].update(instrument_id="YF:BAC"),
            lambda value: value.update(execution_enabled=True),
            lambda value: value["macro_metadata"]["DGS10"]["request"].update(api_key="private"),
            lambda value: value["identity"].update(assertion_count=True),
        )
        for index, mutate in enumerate(mutations):
            self.run_dir = self.run_dir.with_name(f"tamper-{index}")
            value = self.build()
            mutate(value)
            self.rewrite_catalog_and_links(value)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.read()

    def test_metadata_rendering_escapes_provider_markup(self):
        payload = self.metadata_payload()
        payload["seriess"][0]["units"] = "<script>x</script>|[open](https://example.invalid)"
        value = self.build(client_factory=self.factory(self.batch(payload=payload)))
        self.assertEqual(value["status"], "COMPLETE")
        markdown = (self.run_dir / "report.md").read_text()
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[open](", markdown)
        self.assertIn("&lt;script&gt;", markdown)
        self.assertIn("&#124;", markdown)

    def test_show_and_check_time_do_not_load_configuration_open_database_or_call_provider(self):
        self.build()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        for args, code in ((["show", "--run-dir", str(self.run_dir)], 0),
                           (["check-time", "--run-dir", str(self.run_dir), "--as-of", GENERATED.isoformat()], 3),
                           (["check-time", "--run-dir", str(self.run_dir), "--as-of", "2026-09-07T13:00:04Z"], 0)):
            output = io.StringIO()
            with (patch.object(catalog_cli, "PROJECT_ROOT", self.root),
                  patch.object(catalog, "load_configuration", side_effect=AssertionError("must not read env")),
                  patch.object(catalog, "_read_identity", side_effect=AssertionError("must not read DB")),
                  patch("quantpaper.sources.http.get_json", side_effect=AssertionError("must not fetch")),
                  patch("duckdb.connect", side_effect=AssertionError("must not connect")),
                  redirect_stdout(output)):
                self.assertEqual(catalog_cli.main(args), code)
            self.assertIs(json.loads(output.getvalue())["execution_enabled"], False)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_partial_build_and_unavailable_time_return_code_three_and_errors_are_sanitized(self):
        value = self.build(configuration=RefreshConfiguration(), client_factory=Mock())
        output = io.StringIO()
        with patch.object(catalog_cli, "build_catalog", return_value=value), redirect_stdout(output):
            self.assertEqual(catalog_cli.main(["build", "--run-dir", "artifacts/research-catalog/new"]), 3)
        error = io.StringIO()
        with (patch.object(catalog_cli, "read_catalog", side_effect=RuntimeError("private-provider-secret")),
              redirect_stderr(error)):
            self.assertEqual(catalog_cli.main(["show", "--run-dir", "artifacts/research-catalog/new"]), 2)
        self.assertNotIn("private-provider-secret", error.getvalue())


if __name__ == "__main__":
    unittest.main()
