"""Feature packet integration uses only synthetic catalogs and temporary data."""

from contextlib import redirect_stdout
from copy import deepcopy
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

from quantpaper.research import catalog, feature_cli, feature_packet
from quantpaper.research.evidence import canonical_bytes, digest
from quantpaper.research.feature_math import CORE_SERIES, FEATURE_NAMES
from quantpaper.research.refresh import RefreshConfiguration
from quantpaper.sources.fred_metadata import FREDSeriesMetadataClient
from quantpaper.warehouse import DDL


UTC = timezone.utc
CATALOG_START = datetime(2026, 9, 7, 13, tzinfo=UTC)
CATALOG_OBSERVED = CATALOG_START + timedelta(seconds=1)
CATALOG_GENERATED = CATALOG_START + timedelta(seconds=3)
CUTOFF = "2026-09-07T14:00:00Z"
DEFINITIONS = {
    "DFF": ("Percent", "Daily, 7-Day", "Not Seasonally Adjusted"),
    "DGS10": ("Percent", "Daily", "Not Seasonally Adjusted"),
    "CPIAUCSL": ("Index 1982-1984=100", "Monthly", "Seasonally Adjusted"),
    "UNRATE": ("Percent", "Monthly", "Seasonally Adjusted"),
}


class FeaturePacketTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.database = self.root / "data" / "research.duckdb"
        self.database.parent.mkdir()
        with duckdb.connect(str(self.database)) as connection:
            connection.execute(DDL)
            connection.execute("INSERT INTO schema_versions(version) VALUES (1), (2), (3), (4)")
            connection.execute("""INSERT INTO instruments
                (instrument_id,symbol,asset_class,primary_exchange,valid_from,available_at,ingested_at,source)
                VALUES ('YF:JPM','JPM','equity',NULL,'1980-03-17T05:00:00Z',
                        '2026-09-04T12:00:00Z','2026-09-04T12:01:00Z','yahoo')""")
            rows = []
            for index in range(21):
                event = datetime(2026, 8, 15, tzinfo=UTC) + timedelta(days=index)
                close = 100. + index
                rows.append((event, close - .5, close + 1., close - 1., close, 100. + index, f"snapshot-v2:synthetic-{index}"))
            connection.executemany("""INSERT INTO market_bars
                (instrument_id,event_time,interval,open,high,low,close,volume,available_at,ingested_at,source,data_version)
                VALUES ('YF:JPM',?,'1d',?,?,?,?,?,'2026-09-05T01:00:00Z','2026-09-06T00:00:00Z','yahoo',?)""", rows)
            connection.executemany("""INSERT INTO macro_observations
                (series_id,observation_date,value,vintage_date,available_at,ingested_at,source,realtime_end,availability_basis)
                VALUES (?,'2026-09-04',?,'2026-09-04','2026-09-05T05:00:00Z',
                        '2026-09-06T00:00:00Z','fred-alfred','9999-12-31','vintage_date_end_america_chicago')""",
                [("DFF", 3.5), ("DGS10", 4.25), ("CPIAUCSL", 320.), ("UNRATE", 4.2)])
        self.catalog_dir = self.root / "artifacts" / "research-catalog" / "synthetic"
        self.run_dir = self.root / "artifacts" / "research-features" / "synthetic"
        self.catalog_value = self.make_catalog(self.catalog_dir)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def batch(series, *, units_override=None):
        units, frequency, adjustment = DEFINITIONS[series]
        payload = {"realtime_start": "2026-09-07", "realtime_end": "2026-09-07", "seriess": [{
            "id": series, "realtime_start": "2026-09-07", "realtime_end": "2026-09-07",
            "title": "Synthetic research metadata", "observation_start": "1960-01-01",
            "observation_end": "2026-09-04", "frequency": frequency,
            "frequency_short": "M" if frequency == "Monthly" else "D", "units": units_override or units,
            "units_short": "Index" if series == "CPIAUCSL" else "%", "seasonal_adjustment": adjustment,
            "seasonal_adjustment_short": "SA" if adjustment == "Seasonally Adjusted" else "NSA",
            "last_updated": "2026-09-04T20:00:00Z",
        }]}
        ticks = iter([CATALOG_START, CATALOG_OBSERVED])
        return FREDSeriesMetadataClient("0123456789abcdef" * 2, lambda url: payload, lambda: next(ticks)).fetch(series)

    def make_catalog(self, directory, *, unit_mismatch=None, series=CORE_SERIES):
        ticks = iter([CATALOG_START, *([CATALOG_GENERATED] * (len(series) + 1))])
        client = SimpleNamespace(fetch=lambda value: self.batch(value, units_override="Dollars" if value == unit_mismatch else None))
        return catalog.build_catalog(self.database, directory, project_root=self.root, instrument_id="YF:JPM",
                                     series=series, configuration=RefreshConfiguration(fred_api_key="0123456789abcdef" * 2),
                                     client_factory=lambda key: client, now=lambda: next(ticks))

    def build(self, **changes):
        options = {"project_root": self.root, "instrument_id": "YF:JPM", "as_of": CUTOFF, **changes}
        directory = options.pop("run_dir", self.run_dir)
        catalog_dir = options.pop("catalog_run_dir", self.catalog_dir)
        database = options.pop("database", self.database)
        return feature_packet.build_run(database, catalog_dir, directory, **options)

    def read(self):
        return feature_packet.read_run(self.run_dir, project_root=self.root)

    def inputs(self):
        return tuple(json.loads((self.run_dir / name).read_bytes()) for name in ("evidence.json", "catalog.json"))

    def rewrite_hash(self, name):
        path = self.run_dir / "completion.json"
        value = json.loads(path.read_bytes())
        value["artifacts"][name] = hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest()
        path.write_bytes(canonical_bytes(value))

    def fully_rehash_packet_links(self, packet):
        packet["feature_hash"] = digest({key: packet[key] for key in
            ("request", "contract_hash", "input_hashes", "identity_binding", "features")})
        packet["packet_hash"] = digest({key: value for key, value in packet.items() if key != "packet_hash"})
        (self.run_dir / "features.json").write_bytes(canonical_bytes(packet))
        (self.run_dir / "report.md").write_text(feature_packet.render_markdown(packet), encoding="utf-8")
        manifest_path = self.run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_bytes())
        for key in ("packet_hash", "feature_hash", "contract_hash", "input_hashes"):
            manifest[key] = packet[key]
        manifest["manifest_hash"] = digest({key: value for key, value in manifest.items() if key != "manifest_hash"})
        manifest_path.write_bytes(canonical_bytes(manifest))
        completion_path = self.run_dir / "completion.json"
        completion = json.loads(completion_path.read_bytes())
        completion.update(packet_hash=packet["packet_hash"], manifest_hash=manifest["manifest_hash"])
        for name in feature_packet.ARTIFACT_NAMES:
            completion["artifacts"][name] = hashlib.sha256((self.run_dir / name).read_bytes()).hexdigest()
        completion_path.write_bytes(canonical_bytes(completion))

    def test_complete_eleven_feature_packet_is_recomputed_and_preserves_all_inputs(self):
        protected = [self.database, *self.catalog_dir.iterdir()]
        for name in (".env", "data/yahoo/JPM.csv", "artifacts/yahoo_walkforward_v2_model.joblib",
                     "artifacts/research-evidence/previous/packet.json", "data/paper-state/synthetic.json"):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic frozen input\n")
            protected.append(path)
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected}
        value = self.build()
        self.assertEqual((value["status"], value["feature_count"], value["available_count"]), ("COMPLETE", 11, 11))
        self.assertEqual(set(value["features"]), set(FEATURE_NAMES))
        self.assertAlmostEqual(value["features"]["market.return_20_observed_bars"]["value"], .2)
        self.assertAlmostEqual(value["features"]["macro.DGS10_minus_DFF.spread_pp"]["value"], .75)
        self.assertEqual(value["identity_binding"]["status"], "MATCHED_SOURCE_DESCRIPTION")
        self.assertEqual(self.read(), value)
        evidence, copied_catalog = self.inputs()
        self.assertEqual(feature_packet.validate_packet(value, evidence, copied_catalog), value)
        self.assertIsNone(evidence["request"]["fundamental_instrument_id"])
        self.assertIsNone(evidence["request"]["news_entity"])
        self.assertEqual(evidence["request"]["max_records"], 500)
        for name in ("execution_enabled", "model_changed", "training_ready", "prediction_generated",
                     "historical_forward_sample", "warehouse_modified"):
            self.assertIs(value[name], False)
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in protected})

    def test_same_archived_inputs_preserve_feature_hash_across_later_generation(self):
        self.build()
        evidence, copied_catalog = self.inputs()
        first = feature_packet.build_packet(evidence, copied_catalog, generated_at="2027-01-01T00:00:00Z")
        later = feature_packet.build_packet(evidence, copied_catalog, generated_at="2027-01-01T00:00:01Z")
        self.assertEqual(first["feature_hash"], later["feature_hash"])
        self.assertNotEqual(first["packet_hash"], later["packet_hash"])

    def test_before_or_equal_catalog_generation_blocks_metadata_and_all_market_slots(self):
        for index, cutoff in enumerate((CATALOG_START.isoformat(), CATALOG_GENERATED.isoformat())):
            value = self.build(run_dir=self.run_dir.with_name(f"early-{index}"), as_of=cutoff)
            self.assertEqual(value["status"], "PARTIAL")
            self.assertEqual(value["identity_binding"]["status"], "CATALOG_UNAVAILABLE_AT_CUTOFF")
            self.assertTrue(all(row["status"] == "IDENTITY_BLOCKED" and row["value"] is None
                                for name, row in value["features"].items() if name.startswith("market.")))
            self.assertEqual(value["features"]["macro.DFF.level"]["status"], "MISSING_METADATA")
            self.assertTrue(all(status == "UNAVAILABLE_AT_CUTOFF" for status in value["metadata_eligibility"].values()))

    def test_latest_null_is_retained_and_does_not_use_older_macro_value(self):
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("UPDATE macro_observations SET value=NULL WHERE series_id='UNRATE'")
            connection.execute("""INSERT INTO macro_observations VALUES
                ('UNRATE','2026-08-01',4.1,'2026-08-02','2026-08-03T05:00:00Z',
                 '2026-08-04T00:00:00Z','fred-alfred','9999-12-31','vintage_date_end_america_chicago')""")
        value = self.build()
        result = value["features"]["macro.UNRATE.level"]
        self.assertEqual(result["status"], "MISSING_VALUE")
        self.assertIsNone(result["value"])
        self.assertEqual(len(result["source_evidence_ids"]), 1)
        self.assertEqual(value["available_count"], 10)

    def test_current_unit_mismatch_blocks_only_relevant_level_and_spread_dependency(self):
        directory = self.catalog_dir.with_name("wrong-unit")
        self.make_catalog(directory, unit_mismatch="DFF")
        value = self.build(catalog_run_dir=directory)
        self.assertEqual(value["features"]["macro.DFF.level"]["status"], "DEFINITION_MISMATCH")
        self.assertEqual(value["features"]["macro.DGS10_minus_DFF.spread_pp"]["status"], "INPUT_BLOCKED")
        self.assertEqual(value["features"]["macro.CPIAUCSL.level"]["status"], "AVAILABLE")
        self.assertEqual(value["available_count"], 9)

    def test_strict_source_availability_and_ingestion_exclude_future_revisions(self):
        with duckdb.connect(str(self.database)) as connection:
            for field in ("available_at", "ingested_at"):
                connection.execute(f"""INSERT INTO market_bars
                    SELECT instrument_id,event_time,interval,open,high,low,close,volume,
                    {"?::TIMESTAMPTZ" if field == "available_at" else "available_at"},
                    {"?::TIMESTAMPTZ" if field == "ingested_at" else "ingested_at"},source,?
                    FROM market_bars WHERE event_time='2026-09-04T00:00:00Z' LIMIT 1""",
                    [CUTOFF, f"snapshot-v2:future-{field}"])
        value = self.build()
        evidence, _ = self.inputs()
        self.assertEqual(value["available_count"], 11)
        self.assertEqual(evidence["sections"]["market"]["count"], 21)
        self.assertFalse(any("future-" in row["data_version"] for row in evidence["sections"]["market"]["records"]))

    def test_new_current_identity_conflict_blocks_market_but_retains_unmasked_quality(self):
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("""INSERT INTO instruments VALUES ('YF:JPM','JPM','equity','XNYS',
                '2020-01-01T00:00:00Z',NULL,'2026-09-07T13:30:00Z','2026-09-07T13:31:00Z','another-source')""")
        value = self.build()
        self.assertEqual(value["identity_binding"]["status"], "CURRENT_IDENTITY_UNRESOLVED")
        self.assertEqual(value["available_count"], 5)
        self.assertTrue(all(status == "AVAILABLE" for status in value["unmasked_market_quality"].values()))
        self.assertTrue(all(row["status"] == "IDENTITY_BLOCKED" for name, row in value["features"].items() if name.startswith("market.")))

    def test_bounded_market_history_keeps_latest_window_without_imputation(self):
        with duckdb.connect(str(self.database)) as connection:
            rows = [(datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index), f"snapshot-v2:old-{index}") for index in range(510)]
            connection.executemany("""INSERT INTO market_bars VALUES ('YF:JPM',?,'1d',90,91,89,90,100,
                '2026-09-05T01:00:00Z','2026-09-06T00:00:00Z','yahoo',?)""", rows)
        value = self.build()
        self.assertEqual(value["source_coverage"]["market"]["count"], 500)
        self.assertIs(value["source_coverage"]["market"]["truncated"], True)
        self.assertEqual(value["status"], "COMPLETE")
        self.assertAlmostEqual(value["features"]["market.return_20_observed_bars"]["value"], .2)

    def test_truncated_identity_is_never_resolved_from_retained_subset(self):
        with duckdb.connect(str(self.database)) as connection:
            rows = [(datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=index),) for index in range(501)]
            connection.executemany("""INSERT INTO instruments VALUES ('YF:JPM','JPM','equity',NULL,?,NULL,
                '2026-09-07T13:30:00Z','2026-09-07T13:31:00Z','yahoo')""", rows)
        value = self.build()
        current = value["identity_binding"]["current_resolution"]
        self.assertEqual(current["status"], "TRUNCATED")
        self.assertEqual(current["assertion_count"], 500)
        self.assertEqual(value["identity_binding"]["status"], "CURRENT_IDENTITY_UNRESOLVED")
        self.assertIsNone(current["resolved_attributes"])

    def test_wrong_catalog_instrument_or_missing_core_series_is_rejected(self):
        provider = Mock(side_effect=AssertionError("must not read warehouse evidence"))
        directory = self.catalog_dir.with_name("subset")
        self.make_catalog(directory, series=("DFF",))
        with patch.object(feature_packet, "build_evidence_packet", provider):
            with self.assertRaises(ValueError):
                self.build(catalog_run_dir=directory)
            with self.assertRaises(ValueError):
                self.build(instrument_id="YF:BAC")
        provider.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_existing_or_unsafe_output_paths_fail_before_catalog_or_database_read(self):
        self.run_dir.mkdir(parents=True)
        (self.run_dir / "preserve.txt").write_text("owned", encoding="utf-8")
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.run_dir.with_name("linked")
        linked.symlink_to(outside, target_is_directory=True)
        for path in (self.run_dir, self.run_dir.parent, linked, self.root / "data" / "features",
                     self.run_dir.parent / ".." / "escape"):
            with (self.subTest(path=str(path)), patch.object(feature_packet, "read_catalog") as reader,
                  patch.object(feature_packet, "build_evidence_packet") as evidence_reader):
                with self.assertRaises(ValueError):
                    self.build(run_dir=path)
                reader.assert_not_called()
                evidence_reader.assert_not_called()
        self.assertEqual((self.run_dir / "preserve.txt").read_text(), "owned")

    def test_missing_or_wrong_schema_database_does_not_get_created_or_migrated(self):
        missing = self.root / "data" / "missing.duckdb"
        with self.assertRaises(ValueError):
            self.build(database=missing)
        self.assertFalse(missing.exists())
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("DELETE FROM schema_versions WHERE version=4")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        self.assertFalse(self.run_dir.exists())

    def test_builder_fingerprint_precedes_input_reads_and_blocks_changed_source(self):
        revision = {"value": 0}
        original = feature_packet.read_catalog
        def read_and_change(*args, **kwargs):
            result = original(*args, **kwargs)
            revision["value"] = 1
            return result
        with (patch.object(feature_packet, "read_catalog", side_effect=read_and_change),
              patch.object(feature_packet, "_fingerprint", side_effect=lambda: dict(revision))):
            with self.assertRaisesRegex(ValueError, "builder changed"):
                self.build()
        self.assertFalse(self.run_dir.exists())

    def test_show_uses_copied_inputs_without_original_database_catalog_env_or_provider(self):
        packet = self.build()
        with (patch("duckdb.connect", side_effect=AssertionError("no database")),
              patch.object(feature_packet, "read_catalog", side_effect=AssertionError("no original catalog")),
              patch.object(feature_packet, "build_evidence_packet", side_effect=AssertionError("no evidence rebuild")),
              patch("quantpaper.research.refresh.load_configuration", side_effect=AssertionError("no env")),
              patch("quantpaper.sources.http.get_json", side_effect=AssertionError("no provider"))):
            self.assertEqual(self.read(), packet)
            output = io.StringIO()
            with patch.object(feature_cli, "PROJECT_ROOT", self.root), redirect_stdout(output):
                self.assertEqual(feature_cli.main(["show", "--run-dir", str(self.run_dir)]), 0)
            self.assertEqual(json.loads(output.getvalue())["packet_hash"], packet["packet_hash"])

    def test_generation_before_input_or_cutoff_is_rejected(self):
        self.build()
        evidence, copied_catalog = self.inputs()
        for generated in (CATALOG_START, "2026-09-07T13:59:59Z", datetime(2026, 9, 8)):
            with self.subTest(generated=generated), self.assertRaises(ValueError):
                feature_packet.build_packet(evidence, copied_catalog, generated_at=generated)

    def test_nonlocal_or_expanded_evidence_scope_is_rejected(self):
        self.build()
        evidence, copied_catalog = self.inputs()
        for field, value in (("availability_mode", "reconstructed"), ("max_records", 100),
                             ("fundamental_instrument_id", "US:JPM"), ("news_entity", "JPM")):
            changed = deepcopy(evidence)
            changed["request"][field] = value
            changed["snapshot_hash"] = digest({"request": changed["request"], "sections": changed["sections"]})
            changed["packet_hash"] = digest({key: item for key, item in changed.items() if key != "packet_hash"})
            with self.subTest(field=field), self.assertRaises(ValueError):
                feature_packet.build_packet(changed, copied_catalog)

    def test_missing_changed_or_symlinked_artifacts_fail_verification(self):
        self.build()
        path = self.run_dir / "evidence.json"
        original = path.read_bytes()
        path.write_bytes(original + b" ")
        with self.assertRaises(ValueError):
            self.read()
        path.unlink()
        with self.assertRaises(OSError):
            self.read()
        other = self.root / "outside-evidence.json"
        other.write_bytes(original)
        path.symlink_to(other)
        with self.assertRaises(ValueError):
            self.read()

    def test_rehashed_display_or_manifest_changes_are_rejected(self):
        self.build()
        report = self.run_dir / "report.md"
        original_report = report.read_bytes()
        report.write_text("# Not validated data\n", encoding="utf-8")
        self.rewrite_hash("report.md")
        with self.assertRaisesRegex(ValueError, "report"):
            self.read()
        report.write_bytes(original_report)
        self.rewrite_hash("report.md")
        path = self.run_dir / "manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["builder"]["files"].pop(next(iter(manifest["builder"]["files"])))
        manifest["builder"]["sha256"] = digest(manifest["builder"]["files"])
        manifest["manifest_hash"] = digest({key: item for key, item in manifest.items() if key != "manifest_hash"})
        path.write_bytes(canonical_bytes(manifest))
        self.rewrite_hash("manifest.json")
        completion_path = self.run_dir / "completion.json"
        completion = json.loads(completion_path.read_bytes())
        completion["manifest_hash"] = manifest["manifest_hash"]
        completion_path.write_bytes(canonical_bytes(completion))
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.read()

    def test_fully_rehashed_numeric_provenance_flags_and_contract_tampering_fail_recomputation(self):
        mutations = (
            lambda value: value["features"]["macro.DFF.level"].update(value=999.),
            lambda value: value["features"]["market.return_1_observed_bar"].update(source_evidence_ids=[]),
            lambda value: value.update(training_ready=True),
            lambda value: value["identity_binding"].update(historical_identity_verified=True),
            lambda value: value["contract"]["quality_policies"].update(metadata_max_observation_age_days=900),
        )
        for index, mutate in enumerate(mutations):
            self.run_dir = self.run_dir.with_name(f"tampered-{index}")
            packet = self.build()
            mutate(packet)
            if index == 4:
                packet["contract_hash"] = digest(packet["contract"])
            self.fully_rehash_packet_links(packet)
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "recomputed"):
                self.read()

    def test_copied_catalog_and_evidence_mutations_cannot_be_hidden_by_file_hash_updates(self):
        self.build()
        for name, mutate in (("catalog.json", lambda value: value.update(warehouse_modified=True)),
                             ("evidence.json", lambda value: value["sections"]["market"]["records"][0].update(close=999.))):
            path = self.run_dir / name
            original = path.read_bytes()
            value = json.loads(original)
            mutate(value)
            path.write_bytes(canonical_bytes(value))
            self.rewrite_hash(name)
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.read()
            path.write_bytes(original)
            self.rewrite_hash(name)


if __name__ == "__main__":
    unittest.main()
