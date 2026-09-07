"""Adversarial packet tests, using synthetic rows rather than a live database."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from quantpaper.research.evidence import (
    EvidenceRequest, build_evidence_packet, digest, render_markdown, validate_packet,
)


UTC = timezone.utc
CUTOFF = "2026-09-07T12:00:00.000000+00:00"
GENERATED = datetime(2026, 9, 7, 13, tzinfo=UTC)


def rows_fixture():
    timing = {"available_at": "2026-09-07T11:00:00.000000+00:00",
              "ingested_at": "2026-09-07T11:30:00.000000+00:00"}
    return {
        "identity": [{**timing, "instrument_id": "YF:JPM", "symbol": "JPM",
                      "asset_class": "equity", "primary_exchange": "XNYS", "source": "yahoo",
                      "valid_from": "2020-01-01T00:00:00.000000+00:00", "valid_to": None}],
        "market": [{**timing, "instrument_id": "YF:JPM", "source": "yahoo", "interval": "1d",
                    "event_time": "2026-09-04T00:00:00.000000+00:00", "open": 100., "high": 102.,
                    "low": 99., "close": 101., "volume": 0., "data_version": "snapshot-v2:synthetic"}],
        "fundamentals": [{**timing, "record_id": "a" * 64, "instrument_id": "US:JPM",
                          "metric": "Revenue", "period_start": "2026-04-01", "period_end": "2026-06-30",
                          "value": 0., "unit": "USD", "form": "10-Q", "accession_number": "synthetic-accession",
                          "filed_at": "2026-09-04T16:00:00.000000+00:00", "source": "sec-companyfacts",
                          "fiscal_year": 2026, "fiscal_period": "Q2", "frame": "CY2026Q2",
                          "availability_basis": "filing_date_end_america_new_york"}],
        "macro": {"DFF": [{**timing, "series_id": "DFF", "observation_date": "2026-09-03",
                           "vintage_date": "2026-09-04", "value": 0., "source": "fred-alfred",
                           "availability_basis": "vintage_date_end_america_chicago"}]},
        "news": [{**timing, "event_id": "synthetic-news-id", "source": "alpaca-news",
                  "published_at": "2026-09-06T10:00:00.000000+00:00",
                  "first_seen_at": "2026-09-06T10:00:02.000000+00:00", "title_hash": "b" * 64,
                  "entity_ids": ["JPM", "BAC"], "quality_flags": {"has_content": False}}],
    }


class EvidencePacketTests(unittest.TestCase):
    def setUp(self):
        self.request = EvidenceRequest("YF:JPM", CUTOFF, fundamental_instrument_id="US:JPM",
                                       news_entity="JPM", macro_series=("DFF",), max_records=100)
        self.rows = rows_fixture()

    def build(self, *, rows=None, request=None, generated_at=GENERATED):
        with patch("quantpaper.research.evidence_store.read_evidence_snapshot",
                   return_value=deepcopy(self.rows if rows is None else rows)) as reader:
            result = build_evidence_packet(Path("not-opened.duckdb"), request or self.request,
                                           generated_at=generated_at)
            reader.assert_called_once()
            return result

    @staticmethod
    def resign(packet):
        if "snapshot_hash" in packet:
            packet["snapshot_hash"] = digest({"request": packet["request"], "sections": packet["sections"]})
        packet["packet_hash"] = digest({key: value for key, value in packet.items() if key != "packet_hash"})
        return packet

    def test_valid_packet_is_json_safe_nonexecuting_and_preserves_explicit_ids(self):
        packet = self.build()
        self.assertEqual(validate_packet(packet), packet)
        archived = json.loads(json.dumps(packet, allow_nan=False))
        self.assertEqual(validate_packet(archived), archived)
        for field in ("execution_enabled", "model_approval", "forward_prediction", "llm_called"):
            self.assertIs(packet[field], False)
        self.assertIs(packet["research_only"], True)
        self.assertEqual(packet["request"]["instrument_id"], "YF:JPM")
        self.assertEqual(packet["request"]["fundamental_instrument_id"], "US:JPM")
        self.assertEqual(packet["sections"]["fundamentals"]["records"][0]["instrument_id"], "US:JPM")
        self.assertEqual(packet["sections"]["news"]["records"][0]["quality_flags"], {"has_content": False})
        self.assertTrue(any("caller assertions" in item for item in packet["limitations"]))

    def test_utc_normalization_and_request_validation(self):
        normalized = EvidenceRequest("YF:JPM", "2026-09-07T13:00:00+01:00", macro_series=("unrate", "DFF", "DFF"))
        self.assertEqual(normalized.as_of, CUTOFF)
        self.assertEqual(normalized.macro_series, ("DFF", "UNRATE"))
        for cutoff in ("2026-09-07", "2026-09-07T12:00:00", "2026-09-07T12:00:00.1234567Z", None):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                EvidenceRequest("YF:JPM", cutoff)
        for value in (0, -1, 501, True, 10.0, "100"):
            with self.subTest(max_records=value), self.assertRaises(ValueError):
                EvidenceRequest("YF:JPM", CUTOFF, max_records=value)
        for identifier in ("", "YF:JPM\nother", "YF:JPM`", "JPM; DROP TABLE x", None):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                EvidenceRequest(identifier, CUTOFF)
        with self.assertRaises(ValueError):
            EvidenceRequest("YF:JPM", CUTOFF, availability_mode="live")

    def test_generation_cannot_precede_cutoff_or_be_naive(self):
        for when in (datetime(2026, 9, 7, 11, 59, 59, tzinfo=UTC), datetime(2026, 9, 7, 13)):
            with self.subTest(when=when), self.assertRaises(ValueError):
                self.build(generated_at=when)

    def test_available_and_locally_ingested_equality_are_excluded(self):
        for group in ("identity", "market", "fundamentals", "news", "macro"):
            for field in ("available_at", "ingested_at"):
                rows = deepcopy(self.rows)
                row = rows[group]["DFF"][0] if group == "macro" else rows[group][0]
                row[field] = CUTOFF
                with self.subTest(group=group, field=field), self.assertRaises(ValueError):
                    self.build(rows=rows)

    def test_reconstructed_accepts_later_local_ingestion_but_never_future_availability(self):
        request = EvidenceRequest("YF:JPM", CUTOFF, fundamental_instrument_id="US:JPM", news_entity="JPM",
                                  macro_series=("DFF",), availability_mode="reconstructed")
        for values in self.rows.values():
            groups = values.values() if isinstance(values, dict) else [values]
            for group in groups:
                for row in group:
                    row["ingested_at"] = "2026-09-07T12:30:00.000000+00:00"
        packet = self.build(request=request)
        self.assertFalse(packet["forward_prediction"])
        self.assertEqual(packet["request"]["availability_mode"], "reconstructed")
        self.rows["market"][0]["available_at"] = CUTOFF
        with self.assertRaises(ValueError):
            self.build(request=request)

    def test_nonfinite_boolean_and_invalid_ohlc_are_rejected(self):
        for field, value in (("close", float("nan")), ("volume", float("inf")), ("open", True),
                             ("high", 100.), ("low", 103.), ("volume", -1), ("open", 0)):
            rows = deepcopy(self.rows)
            rows["market"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.build(rows=rows)
        for section in ("fundamentals", "macro"):
            rows = deepcopy(self.rows)
            row = rows[section]["DFF"][0] if section == "macro" else rows[section][0]
            row["value"] = float("inf")
            with self.subTest(section=section), self.assertRaises(ValueError):
                self.build(rows=rows)

    def test_packet_is_identical_after_json_round_trip(self):
        packet = self.build()
        self.assertEqual(json.loads(json.dumps(packet, allow_nan=False)), packet)

    def test_zero_is_valid_and_null_remains_missing_without_imputation(self):
        packet = self.build()
        self.assertEqual(packet["sections"]["fundamentals"]["records"][0]["value"], 0.)
        self.assertEqual(packet["sections"]["macro"]["DFF"]["records"][0]["value"], 0.)
        self.rows["fundamentals"][0]["value"] = None
        self.rows["macro"]["DFF"][0]["value"] = None
        packet = self.build()
        for section in (packet["sections"]["fundamentals"], packet["sections"]["macro"]["DFF"]):
            self.assertIsNone(section["records"][0]["value"])
            self.assertEqual(section["status"], "PARTIAL")
            self.assertTrue(any("Null" in warning or "null" in warning for warning in section["warnings"]))
        self.assertTrue(any("units/frequency are not stored" in item for item in packet["limitations"]))

    def test_optional_identifiers_are_not_inferred_and_empty_requested_section_is_missing(self):
        request = EvidenceRequest("YF:JPM", CUTOFF, macro_series=("DFF",))
        self.rows["fundamentals"] = []
        self.rows["news"] = []
        self.rows["market"] = []
        packet = self.build(request=request)
        self.assertEqual(packet["sections"]["fundamentals"]["status"], "NOT_REQUESTED")
        self.assertEqual(packet["sections"]["news"]["status"], "NOT_REQUESTED")
        self.assertEqual(packet["sections"]["market"]["status"], "MISSING")
        self.assertIsNone(packet["sections"]["market"]["availability_age_seconds"])
        self.assertEqual(packet["sections"]["market"]["count"], 0)

    def test_record_limit_exposes_truncation_and_does_not_hide_corrupt_overflow(self):
        request = EvidenceRequest("YF:JPM", CUTOFF, fundamental_instrument_id="US:JPM", news_entity="JPM",
                                  macro_series=("DFF",), max_records=1)
        extra = deepcopy(self.rows["market"][0])
        extra["event_time"] = "2026-09-03T00:00:00.000000+00:00"
        self.rows["market"].append(extra)
        packet = self.build(request=request)
        section = packet["sections"]["market"]
        self.assertEqual(section["count"], 1)
        self.assertTrue(section["truncated"])
        self.assertEqual(section["status"], "PARTIAL")
        self.rows["market"][1]["close"] = float("nan")
        with self.assertRaises(ValueError):
            self.build(request=request)

    def test_truncated_single_identity_retains_ambiguity_warning_after_archival(self):
        request = EvidenceRequest("YF:JPM", CUTOFF, fundamental_instrument_id="US:JPM", news_entity="JPM",
                                  macro_series=("DFF",), max_records=1)
        alternative = deepcopy(self.rows["identity"][0])
        alternative["source"] = "independent-synthetic-source"
        self.rows["identity"].append(alternative)
        packet = self.build(request=request)
        section = packet["sections"]["identity"]
        self.assertEqual(section["count"], 1)
        self.assertIs(section["truncated"], True)
        self.assertEqual(section["status"], "PARTIAL")
        self.assertTrue(any("Multiple eligible identity rows" in text for text in section["warnings"]))
        validate_packet(json.loads(json.dumps(packet)))

    def test_wrong_identity_market_fundamental_and_macro_ids_are_rejected(self):
        for section, field, value in (("identity", "instrument_id", "YF:BAC"),
                                      ("market", "instrument_id", "YF:BAC"),
                                      ("fundamentals", "instrument_id", "YF:JPM"),
                                      ("macro", "series_id", "UNRATE")):
            rows = deepcopy(self.rows)
            row = rows[section]["DFF"][0] if section == "macro" else rows[section][0]
            row[field] = value
            with self.subTest(section=section), self.assertRaises(ValueError):
                self.build(rows=rows)

    def test_macro_record_cannot_be_assigned_to_another_requested_series(self):
        request = EvidenceRequest("YF:JPM", CUTOFF, fundamental_instrument_id="US:JPM", news_entity="JPM",
                                  macro_series=("DFF", "UNRATE"))
        self.rows["macro"]["UNRATE"] = []
        self.rows["macro"]["DFF"][0]["series_id"] = "UNRATE"
        with self.assertRaises(ValueError):
            self.build(request=request)

    def test_legacy_or_wrong_feed_market_evidence_is_rejected(self):
        for field, value in (("data_version", "adjusted-v1"), ("source", "unknown-provider"), ("interval", "1m")):
            rows = deepcopy(self.rows)
            rows["market"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build(rows=rows)

    def test_news_requires_exact_entity_hash_and_valid_timing(self):
        for field, value in (("entity_ids", ["JPMorgan"]), ("title_hash", "not-a-hash"),
                             ("first_seen_at", "2026-09-07T11:05:00.000000+00:00"),
                             ("quality_flags", "not-an-object")):
            rows = deepcopy(self.rows)
            rows["news"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.build(rows=rows)

    def test_stable_snapshot_hash_excludes_generation_time_but_binds_stored_ingestion(self):
        first = self.build()
        second = self.build(generated_at=GENERATED + timedelta(seconds=1))
        self.assertEqual(first["snapshot_hash"], second["snapshot_hash"])
        self.assertNotEqual(first["packet_hash"], second["packet_hash"])
        self.rows["market"][0]["ingested_at"] = "2026-09-07T11:31:00.000000+00:00"
        changed = self.build()
        self.assertNotEqual(first["snapshot_hash"], changed["snapshot_hash"])

    def test_unsigned_and_record_hash_tampering_are_rejected(self):
        packet = self.build()
        packet["sections"]["market"]["records"][0]["close"] = 100.5
        with self.assertRaisesRegex(ValueError, "hash"):
            validate_packet(packet)
        self.resign(packet)
        with self.assertRaisesRegex(ValueError, "record hash"):
            validate_packet(packet)

    def test_duplicate_archived_evidence_is_rejected_even_after_rehashing(self):
        packet = self.build()
        section = packet["sections"]["market"]
        section["records"].append(deepcopy(section["records"][0]))
        section["count"] = 2
        self.resign(packet)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            validate_packet(packet)

    def test_rehashed_safety_flags_and_unsupported_versions_are_rejected(self):
        for field, value in (("execution_enabled", True), ("research_only", 1), ("model_approval", True),
                             ("forward_prediction", True), ("llm_called", True), ("schema_version", True),
                             ("schema_version", 1.0), ("warehouse_schema_version", 3),
                             ("warehouse_schema_version", 4.0), ("cutoff_policy", "less_than_or_equal")):
            packet = self.build()
            packet[field] = value
            self.resign(packet)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_packet(packet)

    def test_rehashed_section_metadata_cannot_claim_wrong_status_age_or_truncation_type(self):
        for field, value in (("status", "TRADE_APPROVED"), ("availability_age_seconds", -1),
                             ("latest_available_at", CUTOFF), ("truncated", "false"), ("count", True)):
            packet = self.build()
            packet["sections"]["market"][field] = value
            self.resign(packet)
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_packet(packet)

    def test_markdown_keeps_untrusted_record_text_inert(self):
        attack = "<script>alert(1)</script>|[open](https://example.invalid)\n`code`"
        self.rows["fundamentals"][0]["metric"] = attack
        markdown = render_markdown(self.build())
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[open](", markdown)
        self.assertIn("&lt;script&gt;", markdown)
        self.assertIn("&#124;", markdown)
        self.assertIn("&#96;", markdown)

    def test_untrusted_limitations_cannot_be_rendered_as_active_markup(self):
        packet = self.build()
        packet["limitations"].append("<script>alert(1)</script> [open](https://example.invalid)")
        self.resign(packet)
        try:
            markdown = render_markdown(packet)
        except ValueError:
            return  # Rejecting a modified fixed limitation list is also safe.
        self.assertNotIn("<script>", markdown)
        self.assertNotIn("[open](", markdown)


if __name__ == "__main__":
    unittest.main()
