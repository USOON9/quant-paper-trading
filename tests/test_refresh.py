"""Offline orchestration tests with synthetic providers and temporary warehouses."""

from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import duckdb
import pandas as pd

from quantpaper.research import refresh
from quantpaper.source_records import FundamentalRecord, MacroRecord
from quantpaper.sources.common import FetchBatch
from quantpaper.sources.yahoo_snapshot import YahooSnapshotClient
from quantpaper.warehouse import DDL, PointInTimeWarehouse


UTC = timezone.utc
OBSERVED = datetime(2026, 9, 7, 12, tzinfo=UTC)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.database = self.root / "data" / "research.duckdb"
        self.database.parent.mkdir()
        with duckdb.connect(str(self.database)) as connection:
            connection.execute(DDL)
            connection.execute("INSERT INTO schema_versions(version) VALUES (1), (2), (3), (4)")
        self.run_dir = self.root / "artifacts" / "research-refresh" / "synthetic-001"
        self.empty_config = refresh.RefreshConfiguration()
        self.ready_config = refresh.RefreshConfiguration("QuantResearch contact@research.invalid", "0123456789abcdef" * 2)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def yahoo_snapshot():
        frame = pd.DataFrame({"Open": [100., 101.], "High": [102., 103.], "Low": [99., 100.],
                              "Close": [101., 102.], "Volume": [0., 200.]},
                             index=pd.to_datetime(["2026-09-03T04:00:00Z", "2026-09-04T04:00:00Z"]))
        return YahooSnapshotClient(lambda symbol, **kwargs: frame, now=lambda: OBSERVED).fetch("JPM")

    @staticmethod
    def sec_batch():
        record = FundamentalRecord("US:JPM", "Assets", "2024-06-30", 10., "USD", "10-Q",
                                   "synthetic-accession", "2024-08-02T04:00:00Z", "2024-08-02T04:00:00Z")
        return FetchBatch("sec-companyfacts", {"ticker": "JPM", "cik": "0000000001"}, [record], "a" * 64)

    @staticmethod
    def fred_batch(series="DFF"):
        record = MacroRecord(series, "2026-09-03", 3., "2026-09-04", "2026-09-05T05:00:00Z")
        return FetchBatch("fred-alfred", {"series_id": series}, [record], "b" * 64)

    @staticmethod
    def factory(value=None, *, error=None):
        client = SimpleNamespace(fetch=Mock(return_value=value, side_effect=error))
        return Mock(return_value=client)

    def collect(self, **changes):
        options = {"project_root": self.root, "configuration": self.empty_config,
                   "sources": ("yahoo",), "series": (),
                   "yahoo_factory": self.factory(self.yahoo_snapshot()), **changes}
        run_dir = options.pop("run_dir", self.run_dir)
        return refresh.run_refresh(self.database, run_dir, **options)

    def count(self, table):
        with duckdb.connect(str(self.database), read_only=True) as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_yahoo_success_is_observed_data_with_audit_and_verifiable_archive(self):
        started = datetime.now(UTC)
        report = self.collect()
        self.assertEqual(report["status"], "COMPLETE")
        outcome = report["outcomes"][0]
        self.assertEqual((outcome["status"], outcome["fetched"], outcome["inserted"]), ("UPDATED", 2, 2))
        self.assertEqual(self.count("market_bars"), 2)
        self.assertEqual(self.count("ingestion_runs"), 1)
        self.assertEqual(refresh.read_refresh(self.run_dir, self.root), report)
        with duckdb.connect(str(self.database), read_only=True) as connection:
            rows = connection.execute("SELECT event_time, available_at, ingested_at, data_version FROM market_bars").fetchall()
        self.assertTrue(all(available >= started and available > event for event, available, _, _ in rows))
        self.assertTrue(all(version.startswith("snapshot-v2:") for _, _, _, version in rows))
        self.assertTrue((self.run_dir / "yahoo.csv").is_file())
        self.assertFalse((self.root / "data" / "yahoo").exists())
        self.assertIs(report["execution_enabled"], False)
        self.assertIs(report["model_changed"], False)

    def test_unchanged_refresh_retains_first_observation_and_records_new_attempt(self):
        self.collect()
        with duckdb.connect(str(self.database), read_only=True) as connection:
            before = connection.execute("SELECT * FROM market_bars ORDER BY event_time").fetchall()
        report = self.collect(run_dir=self.run_dir.with_name("synthetic-002"))
        self.assertEqual(report["outcomes"][0]["status"], "NO_NEW_ROWS")
        self.assertEqual(report["outcomes"][0]["fetched"], 2)
        self.assertEqual(report["outcomes"][0]["inserted"], 0)
        self.assertEqual(self.count("ingestion_runs"), 2)
        with duckdb.connect(str(self.database), read_only=True) as connection:
            self.assertEqual(before, connection.execute("SELECT * FROM market_bars ORDER BY event_time").fetchall())

    def test_macro_no_new_rows_does_not_claim_unchanged_interval_metadata(self):
        original = self.fred_batch()
        self.collect(sources=("fred",), series=("DFF",), configuration=self.ready_config,
                     fred_factory=self.factory(original))
        revised = FetchBatch(original.source, original.request,
                             [replace(original.records[0], realtime_end="2026-09-06")], "d" * 64)
        report = self.collect(run_dir=self.run_dir.with_name("macro-002"), sources=("fred",), series=("DFF",),
                              configuration=self.ready_config, fred_factory=self.factory(revised))
        self.assertEqual(report["outcomes"][0]["status"], "NO_NEW_ROWS")
        self.assertEqual(report["outcomes"][0]["inserted"], 0)
        self.assertEqual(self.count("ingestion_runs"), 2)
        with duckdb.connect(str(self.database), read_only=True) as connection:
            end = connection.execute("SELECT realtime_end FROM macro_observations").fetchone()[0]
        self.assertEqual(end.isoformat(), "2026-09-06")
        self.assertTrue(any("not an unchanged database" in item for item in report["limitations"]))

    def test_existing_model_cache_env_and_frozen_artifacts_are_unchanged(self):
        paths = ("data/yahoo/JPM.csv", "artifacts/yahoo_walkforward_v2_model.json",
                 "artifacts/yahoo_walkforward_v2_model.joblib", ".env", "data/paper-state/synthetic.json",
                 "artifacts/marketdata/frozen/completion.json")
        before = {}
        for name in paths:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic immutable fixture\n")
            before[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.collect()
        self.assertEqual(before, {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in paths})

    def test_missing_configuration_skips_factories_and_keeps_partial_success_explicit(self):
        sec, fred = Mock(side_effect=AssertionError("must not construct SEC")), Mock(side_effect=AssertionError("must not construct FRED"))
        report = self.collect(sources=("yahoo", "sec", "fred"), series=("DFF", "DGS10"),
                              sec_factory=sec, fred_factory=fred)
        self.assertEqual(report["status"], "PARTIAL")
        self.assertEqual([row["status"] for row in report["outcomes"]],
                         ["UPDATED", "MISSING_CONFIGURATION", "MISSING_CONFIGURATION", "MISSING_CONFIGURATION"])
        sec.assert_not_called()
        fred.assert_not_called()
        self.assertEqual(self.count("ingestion_runs"), 1)

    def test_configuration_placeholders_are_invalid_without_exposing_values(self):
        config = refresh.RefreshConfiguration("QuantResearch your_email@example.com", "not-a-key")
        self.assertEqual(config.readiness(), {"yahoo": "READY", "sec": "INVALID_CONFIGURATION", "fred": "INVALID_CONFIGURATION"})
        self.assertNotIn("your_email", repr(config))
        sec, fred = Mock(), Mock()
        report = self.collect(sources=("sec", "fred"), series=("DFF",), configuration=config,
                              sec_factory=sec, fred_factory=fred)
        sec.assert_not_called()
        fred.assert_not_called()
        self.assertNotIn("not-a-key", json.dumps(report))
        self.assertNotIn("your_email", json.dumps(report))

    def test_empty_source_response_is_distinct_from_missing_configuration(self):
        empty = FetchBatch("sec-companyfacts", {"ticker": "JPM"}, [], "c" * 64)
        report = self.collect(sources=("sec",), configuration=self.ready_config, sec_factory=self.factory(empty))
        self.assertEqual(report["outcomes"][0]["status"], "EMPTY")
        self.assertEqual(self.count("fundamentals"), 0)
        self.assertEqual(self.count("ingestion_runs"), 0)

    def test_fully_filtered_macro_response_retains_exclusion_audit_without_database_writes(self):
        metadata = {"series_id": "CPIAUCSL", "provider_record_count": 3, "parsed_record_count": 3,
                    "excluded_before_observation_start": 3, "observation_start": "2024-09-07"}
        empty = FetchBatch("fred-alfred", metadata, [], "e" * 64)
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        report = self.collect(sources=("fred",), series=("CPIAUCSL",), configuration=self.ready_config,
                              fred_factory=self.factory(empty))
        outcome = report["outcomes"][0]
        self.assertEqual((outcome["status"], outcome["fetched"], outcome["inserted"]), ("EMPTY", 0, 0))
        self.assertEqual(outcome["request"], metadata)
        self.assertEqual(outcome["content_hash"], empty.content_hash)
        self.assertNotIn("ingestion_run_id", outcome)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        self.assertEqual(refresh.read_refresh(self.run_dir, self.root), report)

    def test_provider_and_factory_errors_are_sanitized_without_stale_fallback(self):
        secret = "synthetic-secret-must-never-appear"
        for index, factory in enumerate((self.factory(error=RuntimeError(f"https://host/?api_key={secret}")),
                                         Mock(side_effect=RuntimeError(secret)))):
            directory = self.run_dir.with_name(f"error-{index}")
            report = self.collect(run_dir=directory, yahoo_factory=factory)
            self.assertEqual(report["outcomes"][0]["status"], "FAILED")
            self.assertEqual(report["status"], "PARTIAL")
            self.assertEqual(self.count("market_bars"), 0)
            self.assertNotIn(secret, json.dumps(report))
            self.assertFalse((directory / "yahoo.csv").exists())
            for path in directory.iterdir():
                self.assertNotIn(secret.encode(), path.read_bytes())

    def test_successful_sec_fred_ingestion_keeps_ids_periods_and_bounded_requests(self):
        sec = self.factory(self.sec_batch())
        fred = self.factory(self.fred_batch())
        report = self.collect(sources=("sec", "fred"), series=("DFF",), configuration=self.ready_config,
                              sec_factory=sec, fred_factory=fred)
        self.assertEqual(report["status"], "COMPLETE")
        self.assertEqual(self.count("fundamentals"), 1)
        self.assertEqual(self.count("macro_observations"), 1)
        self.assertEqual(self.count("ingestion_runs"), 2)
        self.assertEqual(self.count("instrument_aliases"), 0)
        arguments = fred.return_value.fetch.call_args
        self.assertEqual(arguments.args, ("DFF",))
        self.assertEqual(set(arguments.kwargs), {"observation_start", "realtime_start", "realtime_end"})
        self.assertEqual(arguments.kwargs["realtime_start"], arguments.kwargs["observation_start"])
        start = datetime.fromisoformat(arguments.kwargs["observation_start"])
        end = datetime.fromisoformat(arguments.kwargs["realtime_end"])
        self.assertEqual((end - start).days, 730)
        self.assertNotIn(self.ready_config.fred_api_key, json.dumps(report))
        self.assertNotIn(self.ready_config.sec_user_agent, json.dumps(report))

    def test_one_year_fred_fetch_and_request_archive_bound_both_time_axes(self):
        fred = self.factory(self.fred_batch())
        self.collect(sources=("fred",), series=("DFF",), period="1y",
                     configuration=self.ready_config, fred_factory=fred)
        arguments = fred.return_value.fetch.call_args.kwargs
        self.assertEqual(arguments["observation_start"], arguments["realtime_start"])
        self.assertEqual((datetime.fromisoformat(arguments["realtime_end"]) -
                          datetime.fromisoformat(arguments["realtime_start"])).days, 365)
        archived = json.loads((self.run_dir / "request.json").read_bytes())
        self.assertEqual(archived["macro_observation_start"], arguments["observation_start"])
        self.assertEqual(archived["macro_realtime_start"], arguments["realtime_start"])
        self.assertEqual(archived["macro_realtime_end"], arguments["realtime_end"])

    def test_crypto_sec_is_unsupported_without_constructing_client(self):
        client = Mock()
        report = self.collect(symbol="BTC-USD", asset_class="crypto", sources=("sec",),
                              configuration=self.ready_config, sec_factory=client)
        self.assertEqual(report["outcomes"][0]["status"], "UNSUPPORTED")
        client.assert_not_called()

    def test_batch_and_audit_row_rollback_together(self):
        with patch.object(PointInTimeWarehouse, "record_ingestion", side_effect=RuntimeError("synthetic audit failure")):
            report = self.collect()
        self.assertEqual(report["outcomes"][0]["status"], "FAILED")
        self.assertEqual(self.count("market_bars"), 0)
        self.assertEqual(self.count("instruments"), 0)
        self.assertEqual(self.count("ingestion_runs"), 0)

    def test_later_source_failure_does_not_erase_prior_audited_success(self):
        report = self.collect(sources=("yahoo", "sec"), configuration=self.ready_config,
                              sec_factory=self.factory(error=RuntimeError("synthetic failure")))
        self.assertEqual([row["status"] for row in report["outcomes"]], ["UPDATED", "FAILED"])
        self.assertEqual(self.count("market_bars"), 2)
        self.assertEqual(self.count("ingestion_runs"), 1)
        self.assertEqual(refresh.read_refresh(self.run_dir, self.root), report)

    def test_network_factory_runs_before_writable_database_is_opened(self):
        observed = []
        def provider():
            with duckdb.connect(str(self.database), read_only=True) as connection:
                observed.append(connection.execute("SELECT count(*) FROM market_bars").fetchone()[0])
            return SimpleNamespace(fetch=lambda *args, **kwargs: self.yahoo_snapshot())
        self.collect(yahoo_factory=provider)
        self.assertEqual(observed, [0])

    def test_existing_and_unsafe_run_paths_fail_before_provider_or_writes(self):
        self.run_dir.mkdir(parents=True)
        marker = self.run_dir / "owned.txt"
        marker.write_text("preserve", encoding="utf-8")
        provider = Mock()
        paths = (self.run_dir, self.root / "artifacts" / "research-refresh",
                 self.root / "data" / "oops", self.root / "artifacts" / "research-refresh" / ".." / "oops")
        for path in paths:
            with self.subTest(path=str(path)), self.assertRaises(ValueError):
                self.collect(run_dir=path, yahoo_factory=provider)
        provider.assert_not_called()
        self.assertEqual(marker.read_text(), "preserve")
        self.assertEqual(self.count("market_bars"), 0)

    def test_symlinked_output_and_database_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.run_dir.parent.mkdir(parents=True)
        self.run_dir.symlink_to(outside, target_is_directory=True)
        provider = Mock()
        with self.assertRaises(ValueError):
            self.collect(yahoo_factory=provider)
        linked_database = self.root / "linked.duckdb"
        linked_database.symlink_to(self.database)
        with self.assertRaises(ValueError):
            refresh.run_refresh(linked_database, self.run_dir.with_name("fresh"), project_root=self.root,
                                configuration=self.empty_config, yahoo_factory=provider)
        provider.assert_not_called()

    def test_missing_or_non_v4_database_is_not_created_or_migrated(self):
        missing = self.root / "missing.duckdb"
        provider = Mock()
        with self.assertRaises(ValueError):
            refresh.run_refresh(missing, self.run_dir, project_root=self.root, yahoo_factory=provider)
        self.assertFalse(missing.exists())
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("DELETE FROM schema_versions WHERE version = 4")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            self.collect(yahoo_factory=provider)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        provider.assert_not_called()
        self.assertFalse(self.run_dir.exists())

    def test_missing_writer_tables_fail_before_provider_without_implicit_creation(self):
        for table in ("ingestion_runs", "shadow_signals"):
            with self.subTest(table=table):
                database = self.root / "data" / f"missing-{table}.duckdb"
                with duckdb.connect(str(database)) as connection:
                    connection.execute(DDL)
                    connection.execute("INSERT INTO schema_versions(version) VALUES (4)")
                    connection.execute(f"DROP TABLE {table}")
                before = hashlib.sha256(database.read_bytes()).hexdigest()
                provider = Mock()
                directory = self.run_dir.with_name(f"missing-{table}")
                with self.assertRaises(ValueError):
                    refresh.run_refresh(database, directory, project_root=self.root, sources=("yahoo",),
                                        series=(), configuration=self.empty_config, yahoo_factory=provider)
                provider.assert_not_called()
                self.assertEqual(before, hashlib.sha256(database.read_bytes()).hexdigest())
                self.assertFalse(directory.exists())

    def test_refresh_does_not_call_the_migrating_warehouse_initializer(self):
        with patch.object(PointInTimeWarehouse, "initialize", side_effect=AssertionError("DDL must not run")):
            report = self.collect()
        self.assertEqual(report["outcomes"][0]["status"], "UPDATED")
        self.assertEqual(self.count("market_bars"), 2)

    def test_code_change_preserves_receipt_without_completion_marker(self):
        with patch.object(refresh, "_code_fingerprint", side_effect=[{"sha256": "a"}, {"sha256": "b"}]):
            with self.assertRaises(ValueError):
                self.collect()
        self.assertTrue((self.run_dir / "receipt-01.json").exists())
        self.assertFalse((self.run_dir / "completion.json").exists())
        self.assertEqual(self.count("ingestion_runs"), 1)
        with self.assertRaises((ValueError, OSError)):
            refresh.read_refresh(self.run_dir, self.root)

    def test_archive_hash_tampering_and_unlisted_paths_are_rejected(self):
        self.collect()
        receipt = self.run_dir / "receipt-01.json"
        original = receipt.read_bytes()
        receipt.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "hash"):
            refresh.read_refresh(self.run_dir, self.root)
        receipt.write_bytes(original)
        path = self.run_dir / "completion.json"
        completion = json.loads(path.read_bytes())
        completion["artifacts"]["../outside.json"] = "a" * 64
        path.write_text(json.dumps(completion), encoding="utf-8")
        with self.assertRaises(ValueError):
            refresh.read_refresh(self.run_dir, self.root)

    def test_archived_symlink_is_not_followed(self):
        self.collect()
        receipt = self.run_dir / "receipt-01.json"
        outside = self.root / "outside.json"
        outside.write_bytes(receipt.read_bytes())
        receipt.unlink()
        receipt.symlink_to(outside)
        with self.assertRaises(ValueError):
            refresh.read_refresh(self.run_dir, self.root)

    def test_request_rejects_unbounded_or_ambiguous_parameters(self):
        for changes in ({"sources": ()}, {"sources": ("yahoo", "yahoo")}, {"sources": ("news",)},
                        {"symbol": "JPM/BAC"}, {"period": "max"}, {"series": ("A", "B", "C", "D", "E")},
                        {"sources": ("fred",), "series": ()}, {"sources": ("fred",), "series": "DFF"}):
            with self.subTest(changes=changes), self.assertRaises((ValueError, TypeError)):
                self.collect(**changes)
        self.assertFalse(self.run_dir.exists())

    def test_rehashed_receipt_must_match_report_outcomes(self):
        self.collect()
        receipt_path = self.run_dir / "receipt-01.json"
        receipt = json.loads(receipt_path.read_bytes())
        receipt["inserted"] = 999
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        completion_path = self.run_dir / "completion.json"
        completion = json.loads(completion_path.read_bytes())
        completion["artifacts"]["receipt-01.json"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        completion_path.write_text(json.dumps(completion), encoding="utf-8")
        with self.assertRaises(ValueError):
            refresh.read_refresh(self.run_dir, self.root)

    def test_rehashed_request_must_match_report_scope(self):
        self.collect()
        request_path = self.run_dir / "request.json"
        request = json.loads(request_path.read_bytes())
        request["request"]["symbol"] = "BAC"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        completion_path = self.run_dir / "completion.json"
        completion = json.loads(completion_path.read_bytes())
        completion["artifacts"]["request.json"] = hashlib.sha256(request_path.read_bytes()).hexdigest()
        completion_path.write_text(json.dumps(completion), encoding="utf-8")
        with self.assertRaises(ValueError):
            refresh.read_refresh(self.run_dir, self.root)

    def test_configuration_loader_does_not_mutate_environment(self):
        (self.root / ".env").write_text("SEC_USER_AGENT=Research local@research.invalid\nFRED_API_KEY=" + "a" * 32 + "\n", encoding="utf-8")
        synthetic_environment = {"FRED_API_KEY": "b" * 32, "UNRELATED": "preserve"}
        with patch.object(refresh.os, "environ", synthetic_environment):
            config = refresh.load_configuration(self.root)
        self.assertEqual(config.fred_api_key, "b" * 32)
        self.assertEqual(synthetic_environment, {"FRED_API_KEY": "b" * 32, "UNRELATED": "preserve"})
        self.assertEqual(config.readiness(), {"yahoo": "READY", "sec": "READY", "fred": "READY"})

    def test_status_does_not_open_database_or_provider_and_never_prints_configuration(self):
        from quantpaper.research import refresh_cli
        output = io.StringIO()
        with (patch.object(refresh_cli, "load_configuration", return_value=self.ready_config),
              patch.object(refresh_cli, "run_refresh", side_effect=AssertionError("must not run")),
              patch("quantpaper.research.evidence_store.read_evidence_snapshot", side_effect=AssertionError("must not open DB")),
              redirect_stdout(output)):
            self.assertEqual(refresh_cli.main(["status"]), 0)
        self.assertEqual(json.loads(output.getvalue())["sources"], self.ready_config.readiness())
        self.assertNotIn(self.ready_config.sec_user_agent, output.getvalue())
        self.assertNotIn(self.ready_config.fred_api_key, output.getvalue())

    def test_run_cli_partial_returns_nonzero_without_hiding_report(self):
        from quantpaper.research import refresh_cli
        for status, expected in (("PARTIAL", 3), ("COMPLETE", 0)):
            output = io.StringIO()
            report = {"status": status, "research_only": True, "execution_enabled": False}
            with (self.subTest(status=status), patch.object(refresh_cli, "run_refresh", return_value=report),
                  redirect_stdout(output)):
                self.assertEqual(refresh_cli.main(["run", "--run-dir", str(self.run_dir)]), expected)
            self.assertEqual(json.loads(output.getvalue()), report)

    def test_show_cli_verified_partial_archive_is_successful_read(self):
        from quantpaper.research import refresh_cli
        output = io.StringIO()
        report = {"status": "PARTIAL", "research_only": True, "execution_enabled": False}
        with (patch.object(refresh_cli, "read_refresh", return_value=report),
              patch.object(refresh_cli, "run_refresh", side_effect=AssertionError("must not fetch")),
              redirect_stdout(output)):
            self.assertEqual(refresh_cli.main(["show", "--run-dir", str(self.run_dir)]), 0)
        self.assertEqual(json.loads(output.getvalue()), report)


if __name__ == "__main__":
    unittest.main()
