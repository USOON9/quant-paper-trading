"""Synthetic schema-v4 evidence reads: time causality, isolation, and bounds."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import duckdb

from quantpaper.research.evidence_store import read_evidence_snapshot
from quantpaper.warehouse import DDL


class EvidenceStoreTests(unittest.TestCase):
    cutoff = "2024-02-10T12:00:00.000000+00:00"
    early = "2024-02-02T00:00:00Z"

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "synthetic.duckdb"
        with duckdb.connect(str(self.database)) as connection:
            connection.execute(DDL)
            connection.execute("INSERT INTO schema_versions(version) VALUES (1), (2), (3), (4)")

    def tearDown(self):
        self.directory.cleanup()

    def insert(self, table, row):
        with duckdb.connect(str(self.database)) as connection:
            columns = ", ".join(f'"{column}"' for column in row)
            values = ", ".join("?" for _ in row)
            connection.execute(f"INSERT INTO {table} ({columns}) VALUES ({values})", list(row.values()))

    def identity(self, **changes):
        return {"instrument_id": "YF:SPY", "symbol": "SPY", "asset_class": "equity",
                "primary_exchange": "ARCX", "valid_from": "2024-01-01T00:00:00Z", "valid_to": None,
                "available_at": self.early, "ingested_at": self.early, "source": "yahoo", **changes}

    def market(self, **changes):
        return {"instrument_id": "YF:SPY", "event_time": "2024-02-08T00:00:00Z", "interval": "1d",
                "open": 100, "high": 102, "low": 99, "close": 101, "volume": 100,
                "available_at": "2024-02-09T01:00:00Z", "ingested_at": "2024-02-09T02:00:00Z",
                "source": "yahoo", "data_version": "snapshot-v2:one", **changes}

    def fundamental(self, **changes):
        return {"record_id": "one", "instrument_id": "US:SPY", "metric": "Revenue",
                "period_start": "2024-01-01", "period_end": "2024-01-31", "value": 10,
                "unit": "USD", "form": "10-Q", "accession_number": "001", "filed_at": self.early,
                "available_at": self.early, "ingested_at": self.early, "source": "sec-companyfacts",
                "fiscal_year": 2024, "fiscal_period": "Q1", "frame": None,
                "availability_basis": "filing_date_end_america_new_york", **changes}

    def macro(self, **changes):
        return {"series_id": "CPI", "observation_date": "2024-01-01", "value": 3,
                "vintage_date": "2024-02-01", "available_at": self.early, "ingested_at": self.early,
                "source": "fred-alfred", "realtime_end": "9999-12-31",
                "availability_basis": "vintage_date_end_america_chicago", **changes}

    def news(self, **changes):
        return {"event_id": "news-one", "published_at": self.early, "first_seen_at": self.early,
                "available_at": self.early, "ingested_at": self.early, "source": "alpaca-news:test",
                "title_hash": "a" * 64, "raw_uri": "https://private.example/?token=DO_NOT_EXPOSE",
                "entity_ids": '["SPY"]', "quality_flags": '{"historical_backfill": false}', **changes}

    def populate(self):
        for table, row in (("instruments", self.identity()), ("market_bars", self.market()),
                           ("fundamentals", self.fundamental()), ("macro_observations", self.macro()),
                           ("news_events", self.news())):
            self.insert(table, row)

    def read(self, **changes):
        args = {"instrument_id": "YF:SPY", "fundamental_instrument_id": "US:SPY", "news_entity": "SPY",
                "macro_series": ("CPI",), "as_of": self.cutoff, "availability_mode": "local_observed",
                "max_records": 100, **changes}
        return read_evidence_snapshot(self.database, **args)

    def test_normalized_provenance_without_database_path_uri_or_mutable_macro_end(self):
        self.populate()
        result = self.read()
        self.assertEqual(set(result), {"identity", "market", "fundamentals", "macro", "news"})
        self.assertEqual(result["market"][0]["event_time"], "2024-02-08T00:00:00.000000+00:00")
        self.assertEqual(result["fundamentals"][0]["period_end"], "2024-01-31")
        self.assertEqual(result["news"][0]["entity_ids"], ["SPY"])
        self.assertEqual(result["news"][0]["quality_flags"], {"historical_backfill": False})
        self.assertNotIn("raw_uri", result["news"][0])
        self.assertNotIn("realtime_end", result["macro"]["CPI"][0])
        for section in ("identity", "market", "fundamentals", "news"):
            self.assertIn("ingested_at", result[section][0])
        serialized = json.dumps(result, allow_nan=False)
        self.assertNotIn(str(self.database), serialized)
        self.assertNotIn("DO_NOT_EXPOSE", serialized)

    def test_available_at_equal_cutoff_excluded_in_every_section_and_both_modes(self):
        for table, row in (("instruments", self.identity(available_at=self.cutoff)),
                           ("market_bars", self.market(available_at=self.cutoff)),
                           ("fundamentals", self.fundamental(available_at=self.cutoff)),
                           ("macro_observations", self.macro(available_at=self.cutoff)),
                           ("news_events", self.news(available_at=self.cutoff))):
            self.insert(table, row)
        for mode in ("local_observed", "reconstructed"):
            result = self.read(availability_mode=mode)
            self.assertTrue(all(result[key] == [] for key in ("identity", "market", "fundamentals", "news")))
            self.assertEqual(result["macro"], {"CPI": []})

    def test_local_ingestion_equal_cutoff_excluded_but_reconstructed_retains_provenance(self):
        for table, row in (("instruments", self.identity(ingested_at=self.cutoff)),
                           ("market_bars", self.market(ingested_at=self.cutoff)),
                           ("fundamentals", self.fundamental(ingested_at=self.cutoff)),
                           ("macro_observations", self.macro(ingested_at=self.cutoff)),
                           ("news_events", self.news(ingested_at=self.cutoff))):
            self.insert(table, row)
        local = self.read()
        self.assertTrue(all(local[key] == [] for key in ("identity", "market", "fundamentals", "news")))
        self.assertEqual(local["macro"]["CPI"], [])
        reconstructed = self.read(availability_mode="reconstructed")
        self.assertTrue(all(len(reconstructed[key]) == 1 for key in ("identity", "market", "fundamentals", "news")))
        self.assertEqual(reconstructed["macro"]["CPI"][0]["ingested_at"], self.cutoff)

    def test_identity_validity_interval_and_ambiguity_preserved_without_alias_guessing(self):
        self.insert("instruments", self.identity(valid_to=self.cutoff))
        self.insert("instruments", self.identity(source="alternate", valid_from=self.cutoff))
        self.insert("instruments", self.identity(source="third"))
        self.insert("instrument_aliases", {"source": "sec", "source_symbol": "SPY", "instrument_id": "US:SPY", "available_at": self.early})
        result = self.read(fundamental_instrument_id=None, news_entity=None)
        self.assertEqual(len(result["identity"]), 2)
        self.assertEqual(result["identity"][0]["source"], "alternate")
        self.assertEqual(result["fundamentals"], [])
        self.assertEqual(result["news"], [])

    def test_yahoo_only_complete_daily_snapshot_v2_rows(self):
        rows = [self.market(), self.market(data_version="adjusted-v1"),
                self.market(source="other", data_version="snapshot-v2:other"),
                self.market(interval="1m", data_version="snapshot-v2:minute"),
                self.market(event_time="2024-02-10T00:00:00Z", data_version="snapshot-v2:incomplete"),
                self.market(event_time="2024-02-09T12:00:00Z", data_version="snapshot-v2:complete-boundary")]
        for row in rows:
            self.insert("market_bars", row)
        result = self.read()["market"]
        self.assertEqual([row["data_version"] for row in result], ["snapshot-v2:complete-boundary", "snapshot-v2:one"])

    def test_current_yahoo_snapshot_not_backdated_in_reconstructed_mode(self):
        self.insert("market_bars", self.market(available_at="2024-03-01T00:00:00Z"))
        self.assertEqual(self.read(availability_mode="reconstructed")["market"], [])

    def test_latest_bar_known_version_and_deterministic_equal_time_tie(self):
        self.insert("market_bars", self.market())
        self.insert("market_bars", self.market(data_version="snapshot-v2:z", close=102))
        self.insert("market_bars", self.market(data_version="snapshot-v2:future", close=500, available_at="2024-03-01T00:00:00Z"))
        self.assertEqual(self.read()["market"][0]["data_version"], "snapshot-v2:z")
        self.insert("market_bars", self.market(data_version="snapshot-v2:observed-later", close=100,
                    available_at="2024-02-09T03:00:00Z", ingested_at="2024-03-01T00:00:00Z"))
        self.assertEqual(self.read()["market"][0]["data_version"], "snapshot-v2:z")
        self.assertEqual(self.read(availability_mode="reconstructed")["market"][0]["data_version"], "snapshot-v2:observed-later")

    def test_fundamental_latest_filing_preserves_period_unit_source_groups(self):
        rows = [self.fundamental(), self.fundamental(record_id="revision", accession_number="002", value=11,
                    filed_at="2024-02-03T00:00:00Z", available_at="2024-02-03T00:00:00Z"),
                self.fundamental(record_id="future", accession_number="003", value=99,
                    filed_at=self.cutoff, available_at=self.cutoff),
                self.fundamental(record_id="otherperiod", period_start="2023-10-01", value=20),
                self.fundamental(record_id="otherunit", unit="EUR", value=9),
                self.fundamental(record_id="othersource", source="other", value=8)]
        for row in rows:
            self.insert("fundamentals", row)
        result = self.read()["fundamentals"]
        self.assertEqual(len(result), 4)
        self.assertEqual(sorted(row["value"] for row in result), [8, 9, 11, 20])

    def test_fundamentals_period_end_after_cutoff_excluded(self):
        self.insert("fundamentals", self.fundamental(period_end="2024-02-11"))
        self.assertEqual(self.read()["fundamentals"], [])

    def test_fundamental_ties_are_ordered_by_accession_then_record_id(self):
        self.insert("fundamentals", self.fundamental())
        self.insert("fundamentals", self.fundamental(record_id="two", accession_number="002", value=20))
        self.insert("fundamentals", self.fundamental(record_id="zzz", accession_number="002", value=30))
        self.assertEqual(self.read()["fundamentals"][0]["record_id"], "zzz")

    def test_macro_latest_known_vintage_and_no_realtime_end_filter(self):
        self.insert("macro_observations", self.macro(realtime_end="2024-02-03"))
        self.insert("macro_observations", self.macro(vintage_date="2024-02-04", value=4, available_at=self.cutoff))
        self.assertEqual(self.read()["macro"]["CPI"][0]["value"], 3)
        self.insert("macro_observations", self.macro(vintage_date="2024-02-05", value=5,
                    available_at="2024-02-06T00:00:00Z", ingested_at="2024-03-01T00:00:00Z"))
        self.assertEqual(self.read()["macro"]["CPI"][0]["value"], 3)
        self.assertEqual(self.read(availability_mode="reconstructed")["macro"]["CPI"][0]["value"], 5)

    def test_macro_future_observation_or_vintage_excluded_and_sources_separate(self):
        self.insert("macro_observations", self.macro())
        self.insert("macro_observations", self.macro(source="other", value=4))
        self.insert("macro_observations", self.macro(observation_date="2024-02-11"))
        self.insert("macro_observations", self.macro(vintage_date="2024-02-11"))
        result = self.read()["macro"]["CPI"]
        self.assertEqual(len(result), 2)
        self.assertEqual({row["source"] for row in result}, {"fred-alfred", "other"})

    def test_news_exact_entity_membership_no_substring_or_case_guessing(self):
        for index, entities in enumerate(('["SPY"]', '["SPYI"]', '["spy"]', '["XSPY"]', '["SPY", "JPM"]')):
            self.insert("news_events", self.news(event_id=f"news-{index}", entity_ids=entities))
        result = self.read()["news"]
        self.assertEqual([row["event_id"] for row in result], ["news-0", "news-4"])

    def test_news_published_and_first_seen_equal_cutoff_excluded(self):
        self.insert("news_events", self.news(published_at=self.cutoff))
        self.insert("news_events", self.news(event_id="two", first_seen_at=self.cutoff))
        self.assertEqual(self.read(availability_mode="reconstructed")["news"], [])

    def test_news_malformed_entities_fail_closed_not_silent_empty(self):
        for value in ('not json', '"SPY"', '[{"ticker":"SPY"}]', '[1]', None):
            with self.subTest(value=value):
                with duckdb.connect(str(self.database)) as connection:
                    connection.execute("DELETE FROM news_events")
                self.insert("news_events", self.news(entity_ids=value))
                with self.assertRaisesRegex(ValueError, "valid schema-v4"):
                    self.read()

    def test_news_quality_json_rejected_only_for_selected_entity(self):
        self.insert("news_events", self.news(entity_ids='["JPM"]', quality_flags="invalid"))
        self.assertEqual(self.read()["news"], [])
        self.insert("news_events", self.news(event_id="selected", quality_flags="invalid"))
        with self.assertRaisesRegex(ValueError, "valid schema-v4"):
            self.read()

    def test_future_or_not_locally_seen_malformed_news_does_not_affect_past_snapshot(self):
        self.insert("news_events", self.news())
        self.insert("news_events", self.news(event_id="future", entity_ids="bad-json", available_at=self.cutoff))
        self.insert("news_events", self.news(event_id="not-local", entity_ids="bad-json", ingested_at=self.cutoff))
        result = self.read()
        self.assertEqual([row["event_id"] for row in result["news"]], ["news-one"])

    def test_optional_sections_do_not_query_unrequested_bad_data(self):
        self.insert("news_events", self.news(entity_ids="invalid"))
        self.insert("fundamentals", self.fundamental(value=float("nan")))
        self.insert("macro_observations", self.macro(value=float("inf")))
        result = self.read(fundamental_instrument_id=None, news_entity=None, macro_series=())
        self.assertEqual(result["news"], [])
        self.assertEqual(result["fundamentals"], [])
        self.assertEqual(result["macro"], {})

    def test_max_records_plus_one_per_section_and_per_macro_series(self):
        for index in range(4):
            self.insert("instruments", self.identity(source=f"source-{index}"))
            self.insert("market_bars", self.market(event_time=f"2024-02-0{index + 1}T00:00:00Z"))
            self.insert("fundamentals", self.fundamental(record_id=f"r{index}", metric=f"metric-{index}"))
            self.insert("news_events", self.news(event_id=f"news-{index}"))
            for series in ("CPI", "GDP"):
                self.insert("macro_observations", self.macro(series_id=series, observation_date=f"2024-01-0{index + 1}"))
        result = self.read(max_records=2, macro_series=("CPI", "GDP"))
        for section in ("identity", "market", "fundamentals", "news"):
            self.assertEqual(len(result[section]), 3)
        self.assertEqual([len(rows) for rows in result["macro"].values()], [3, 3])
        self.assertEqual(result["market"][0]["event_time"], "2024-02-04T00:00:00.000000+00:00")

    def test_nonfinite_numbers_rejected_null_missing_preserved(self):
        for table, field, factory in (("market_bars", "close", self.market), ("fundamentals", "value", self.fundamental),
                                      ("macro_observations", "value", self.macro)):
            for value in (float("nan"), float("inf"), -float("inf")):
                with self.subTest(table=table, value=value):
                    self.insert(table, factory(**{field: value}))
                    with self.assertRaisesRegex(ValueError, "valid schema-v4"):
                        self.read()
                    with duckdb.connect(str(self.database)) as connection:
                        connection.execute(f"DELETE FROM {table}")
        self.insert("fundamentals", self.fundamental(value=None))
        self.insert("macro_observations", self.macro(value=None))
        result = self.read()
        self.assertIsNone(result["fundamentals"][0]["value"])
        self.assertIsNone(result["macro"]["CPI"][0]["value"])

    def test_finite_bad_ohlc_is_preserved_for_packet_validator_not_repaired(self):
        self.insert("market_bars", self.market(open=-1, high=1, close=100, volume=-5))
        row = self.read()["market"][0]
        self.assertEqual(row["open"], -1)
        self.assertEqual(row["high"], 1)
        self.assertEqual(row["volume"], -5)

    def test_read_only_single_transaction_and_database_bytes_unchanged(self):
        self.populate()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        commands = []
        real_connect = duckdb.connect

        class TracedConnection:
            def __init__(self, wrapped):
                self.wrapped = wrapped
            def execute(self, sql, *args):
                commands.append(sql.strip())
                return self.wrapped.execute(sql, *args)
            def close(self):
                self.wrapped.close()

        def connect(*args, **kwargs):
            self.assertTrue(kwargs["read_only"])
            self.assertFalse(kwargs["config"]["enable_external_access"])
            self.assertFalse(kwargs["config"]["autoinstall_known_extensions"])
            self.assertFalse(kwargs["config"]["autoload_known_extensions"])
            return TracedConnection(real_connect(*args, **kwargs))

        with patch("quantpaper.research.evidence_store.duckdb.connect", side_effect=connect):
            self.read()
        self.assertEqual(sum(command == "BEGIN TRANSACTION" for command in commands), 1)
        self.assertEqual(commands[-1], "COMMIT")
        self.assertLess(commands.index("BEGIN TRANSACTION"), next(index for index, command in enumerate(commands) if command.startswith("SELECT")))
        self.assertFalse(any(command.startswith(("CREATE", "INSERT", "UPDATE", "DELETE", "ALTER")) for command in commands))
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_missing_database_not_created_and_missing_schema_not_migrated(self):
        missing = Path(self.directory.name) / "missing" / "missing.duckdb"
        with self.assertRaisesRegex(ValueError, "already exist"):
            read_evidence_snapshot(missing, instrument_id="YF:SPY", fundamental_instrument_id=None,
                news_entity=None, macro_series=(), as_of=self.cutoff, availability_mode="local_observed", max_records=1)
        self.assertFalse(missing.parent.exists())
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("DROP TABLE schema_versions")
        before = self.database.read_bytes()
        with self.assertRaisesRegex(ValueError, "valid schema-v4"):
            self.read()
        self.assertEqual(self.database.read_bytes(), before)

    def test_wrong_schema_version_or_view_rejected_without_querying_view(self):
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("DELETE FROM schema_versions WHERE version = 4")
        with self.assertRaisesRegex(ValueError, "valid schema-v4"):
            self.read()
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("INSERT INTO schema_versions(version) VALUES (4), (5)")
        with self.assertRaisesRegex(ValueError, "valid schema-v4"):
            self.read()
        with duckdb.connect(str(self.database)) as connection:
            connection.execute("DELETE FROM schema_versions WHERE version = 5")
            connection.execute("DROP TABLE news_events")
            connection.execute("CREATE VIEW news_events AS SELECT 'no external access' AS event_id")
        with self.assertRaisesRegex(ValueError, "valid schema-v4"):
            self.read()

    def test_identifiers_are_parameterized_and_never_guessed(self):
        self.populate()
        result = self.read(instrument_id="YF:SPY' OR 1=1 --", fundamental_instrument_id=None, news_entity="SPY' OR 1=1 --")
        self.assertEqual(result["identity"], [])
        self.assertEqual(result["market"], [])
        self.assertEqual(result["news"], [])

    def test_invalid_arguments_and_sanitized_database_failures(self):
        for changes in ({"as_of": "2024-02-10"}, {"as_of": "2024-02-10T12:00:00.000000001Z"},
                        {"availability_mode": "live"}, {"max_records": True}, {"max_records": 0},
                        {"max_records": 501}, {"instrument_id": ""}, {"macro_series": ("CPI", "CPI")},
                        {"macro_series": ("bad series",)}, {"news_entity": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(**changes)
        with patch("quantpaper.research.evidence_store.duckdb.connect", side_effect=RuntimeError("SECRET /private/db")):
            with self.assertRaises(ValueError) as captured:
                self.read()
        self.assertNotIn("SECRET", str(captured.exception))
        self.assertNotIn("/private", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
