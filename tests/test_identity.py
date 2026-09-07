"""Pure identity assertion resolution: no network, warehouse, or credentials."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest

from quantpaper.research.evidence import digest
from quantpaper.research.identity import resolve_identity, validate_resolution


AS_OF = "2026-09-07T12:00:00Z"


def assertion(**changes):
    return {
        "instrument_id": "YF:JPM", "symbol": "JPM", "asset_class": "equity",
        "primary_exchange": None, "source": "yahoo",
        "valid_from": "1980-03-17T05:00:00Z", "valid_to": None,
        "available_at": "2026-09-04T12:00:00Z", "ingested_at": "2026-09-04T12:01:00Z",
        **changes,
    }


class IdentityTests(unittest.TestCase):
    def resolve(self, rows=None, **changes):
        options = {"instrument_id": "YF:JPM", "as_of": AS_OF,
                   "availability_mode": "local_observed", **changes}
        return resolve_identity([assertion()] if rows is None else rows, **options)

    def test_single_assertion_preserves_raw_values_and_never_verifies_history(self):
        row = assertion()
        result = self.resolve([row])
        self.assertEqual(result["status"], "SINGLE_ASSERTION")
        self.assertEqual((result["assertion_count"], result["group_count"]), (1, 1))
        self.assertEqual(result["assertions"], [{"assertion_id": digest(row), **row}])
        self.assertEqual(result["resolved_attributes"], {name: row[name] for name in
                          ("source", "instrument_id", "symbol", "asset_class", "primary_exchange")})
        self.assertIs(result["historical_identity_verified"], False)
        self.assertIsNone(result["resolved_attributes"]["primary_exchange"])
        self.assertTrue(any("exchange remains null" in text for text in result["warnings"]))
        self.assertTrue(any("historical security master" in text for text in result["warnings"]))
        self.assertEqual(validate_resolution(result), result)
        self.assertEqual(validate_resolution(json.loads(json.dumps(result))), result)

    def test_matching_descriptors_retain_every_effective_assertion(self):
        first = assertion()
        second = assertion(valid_from="2024-09-06T04:00:00Z", available_at="2026-09-07T10:00:00Z",
                           ingested_at="2026-09-07T10:01:00Z")
        result = self.resolve([first, second])
        self.assertEqual(result["status"], "EQUIVALENT_ASSERTIONS")
        self.assertEqual((result["assertion_count"], result["group_count"]), (2, 1))
        self.assertEqual(result["groups"][0]["assertion_ids"], sorted([digest(first), digest(second)]))
        self.assertEqual({row["valid_from"] for row in result["assertions"]}, {first["valid_from"], second["valid_from"]})
        self.assertNotIn("valid_from", result["resolved_attributes"])
        self.assertTrue(any("no synthetic validity interval" in text for text in result["warnings"]))
        validate_resolution(result)

    def test_order_independence_and_key_order_do_not_change_hashes(self):
        rows = [assertion(), assertion(source="independent"), assertion(symbol="JPM-A")]
        original = self.resolve(rows)
        reordered = [dict(reversed(list(row.items()))) for row in reversed(rows)]
        self.assertEqual(self.resolve(reordered), original)
        self.assertEqual([row["assertion_id"] for row in original["assertions"]],
                         sorted(digest(row) for row in rows))

    def test_different_descriptor_or_source_is_never_merged(self):
        for field, value in (("symbol", "JPM-A"), ("asset_class", "option"),
                             ("primary_exchange", "XNYS"), ("source", "other-provider")):
            with self.subTest(field=field):
                result = self.resolve([assertion(), assertion(**{field: value})])
                self.assertEqual(result["status"], "AMBIGUOUS")
                self.assertEqual(result["group_count"], 2)
                self.assertIsNone(result["resolved_attributes"])
                self.assertEqual(sum(len(group["assertion_ids"]) for group in result["groups"]), 2)
                validate_resolution(result)

    def test_ambiguous_result_retains_equivalent_and_conflicting_groups(self):
        rows = [assertion(), assertion(valid_from="2024-01-01T00:00:00Z"),
                assertion(source="another-source"), assertion(symbol="JPM-A")]
        result = self.resolve(rows)
        self.assertEqual((result["status"], result["assertion_count"], result["group_count"]), ("AMBIGUOUS", 4, 3))
        self.assertEqual(sorted(len(group["assertion_ids"]) for group in result["groups"]), [1, 1, 2])

    def test_missing_is_not_a_nonexistence_claim(self):
        result = self.resolve([])
        self.assertEqual(result["status"], "MISSING")
        self.assertEqual((result["assertion_count"], result["group_count"]), (0, 0))
        self.assertEqual(result["assertions"], [])
        self.assertIsNone(result["resolved_attributes"])
        self.assertTrue(any("does not establish" in text for text in result["warnings"]))
        validate_resolution(result)

    def test_incomplete_input_never_resolves_even_when_empty_or_matching(self):
        for rows in ([], [assertion()], [assertion(), assertion(valid_from="2024-01-01T00:00:00Z")],
                     [assertion(), assertion(source="other")]):
            with self.subTest(count=len(rows)):
                result = self.resolve(rows, complete=False)
                self.assertEqual(result["status"], "TRUNCATED")
                self.assertIsNone(result["resolved_attributes"])
                self.assertEqual(result["assertion_count"], len(rows))
                validate_resolution(result)

    def test_duplicates_are_rejected_instead_of_counted_as_confirmation(self):
        row = assertion()
        for complete in (True, False):
            with self.subTest(complete=complete), self.assertRaisesRegex(ValueError, "duplicate"):
                self.resolve([row, deepcopy(row)], complete=complete)

    def test_strict_availability_and_local_ingestion_cutoffs(self):
        for field in ("available_at", "ingested_at"):
            for value in (AS_OF, "2026-09-07T13:00:00Z"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.resolve([assertion(**{field: value})])
        self.resolve([assertion(available_at="2026-09-07T11:59:59.999999Z",
                                ingested_at="2026-09-07T11:59:59.999999Z")])

    def test_reconstruction_accepts_later_ingestion_but_not_later_availability(self):
        result = self.resolve([assertion(ingested_at="2026-09-08T12:00:00Z")], availability_mode="reconstructed")
        self.assertEqual(result["status"], "SINGLE_ASSERTION")
        self.assertTrue(any("later local ingestion" in text for text in result["warnings"]))
        with self.assertRaises(ValueError):
            self.resolve([assertion(available_at=AS_OF)], availability_mode="reconstructed")

    def test_effective_interval_is_start_inclusive_and_end_exclusive(self):
        self.resolve([assertion(valid_from=AS_OF, valid_to="2026-09-08T00:00:00Z")])
        for changes in ({"valid_from": "2026-09-07T12:00:00.000001Z"}, {"valid_to": AS_OF},
                        {"valid_from": "2020-01-01T00:00:00Z", "valid_to": "2020-01-01T00:00:00Z"},
                        {"valid_from": "2020-01-02T00:00:00Z", "valid_to": "2020-01-01T00:00:00Z"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.resolve([assertion(**changes)])

    def test_offset_comparison_is_utc_but_raw_timestamps_are_not_rewritten(self):
        row = assertion(available_at="2026-09-07T12:59:59+01:00", ingested_at="2026-09-07T07:59:59-04:00")
        result = self.resolve([row], as_of="2026-09-07T13:00:00+01:00")
        self.assertEqual(result["as_of"], "2026-09-07T12:00:00.000000+00:00")
        self.assertEqual(result["assertions"][0]["available_at"], row["available_at"])
        self.assertEqual(result["assertions"][0]["assertion_id"], digest(row))
        with self.assertRaises(ValueError):
            self.resolve([assertion(available_at="2026-09-07T07:00:00-05:00")])

    def test_invalid_timestamp_types_precision_and_implicit_zones_fail_closed(self):
        invalid = (None, True, 123, "2026-09-04", "2026-09-04T12:00:00",
                   "2026-09-04T12:00:00.1234567Z", datetime(2026, 9, 4, tzinfo=timezone.utc))
        for field in ("available_at", "ingested_at", "valid_from", "valid_to"):
            for value in invalid:
                if field == "valid_to" and value is None:
                    continue
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.resolve([assertion(**{field: value})])

    def test_request_and_descriptor_identifiers_use_the_existing_evidence_rules(self):
        for field in ("instrument_id", "symbol", "source", "asset_class", "primary_exchange"):
            for value in ("", "bad value", "<script>", "JPM\nOTHER", 1, True, {}):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.resolve([assertion(**{field: value})])
        with self.assertRaises(ValueError):
            self.resolve([assertion(instrument_id="US:JPM")])
        with self.assertRaises(ValueError):
            self.resolve(instrument_id="bad identifier")
        self.resolve([assertion(symbol="BRK/B", source="provider:v2", primary_exchange="X.NYS")])

    def test_complete_and_mode_and_row_container_types_are_strict(self):
        for value in (None, 1, 0, "false", []):
            with self.subTest(complete=value), self.assertRaises(ValueError):
                self.resolve(complete=value)
        for value in (None, "LIVE", "", 1, {}):
            with self.subTest(mode=value), self.assertRaises(ValueError):
                self.resolve(availability_mode=value)
        for rows in ((), {}, "rows", [None], [1], ["row"]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                resolve_identity(rows, instrument_id="YF:JPM", as_of=AS_OF, availability_mode="local_observed")

    def test_unknown_missing_or_nested_fields_are_rejected(self):
        row = assertion()
        for field in row:
            missing = {key: value for key, value in row.items() if key != field}
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.resolve([missing])
        for extra in ({"alias": "US:JPM"}, {"evidence_id": "a" * 64}, {"raw_uri": "private"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.resolve([{**row, **extra}])

    def test_inputs_and_independent_output_views_do_not_share_mutable_containers(self):
        rows = [assertion(), assertion(valid_from="2024-01-01T00:00:00Z")]
        before = deepcopy(rows)
        result = self.resolve(rows)
        self.assertEqual(rows, before)
        result["assertions"][0]["symbol"] = "CHANGED"
        result["resolved_attributes"]["symbol"] = "DIFFERENT"
        self.assertEqual(rows, before)
        self.assertEqual(result["groups"][0]["attributes"]["symbol"], "JPM")

    def test_validation_rejects_tampered_flags_counts_warnings_and_status(self):
        for field, value in (("historical_identity_verified", True), ("schema_version", True),
                             ("schema_version", 1.0), ("assertion_count", True), ("group_count", 5),
                             ("complete", 1), ("status", "TRADE_READY"), ("warnings", [])):
            with self.subTest(field=field), self.assertRaises(ValueError):
                result = self.resolve()
                result[field] = value
                validate_resolution(result)

    def test_validation_binds_raw_assertion_ids_group_membership_and_resolution(self):
        baseline = self.resolve()
        changed_assertion = deepcopy(baseline)
        changed_assertion["assertions"][0]["valid_from"] = "2020-01-01T00:00:00Z"
        changed_id = deepcopy(baseline)
        changed_id["assertions"][0]["assertion_id"] = "0" * 64
        changed_group = deepcopy(baseline)
        changed_group["groups"][0]["assertion_ids"] = []
        changed_resolution = deepcopy(baseline)
        changed_resolution["resolved_attributes"]["primary_exchange"] = "XNYS"
        for result in (changed_assertion, changed_id, changed_group, changed_resolution):
            with self.subTest(result=result), self.assertRaises(ValueError):
                validate_resolution(result)

    def test_validation_rejects_noncanonical_order_and_added_or_missing_fields(self):
        result = self.resolve([assertion(), assertion(source="other")])
        result["assertions"].reverse()
        with self.assertRaises(ValueError):
            validate_resolution(result)
        for mutation in ("extra", "missing"):
            result = self.resolve()
            if mutation == "extra":
                result["alias_verified"] = True
            else:
                result.pop("resolved_attributes")
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                validate_resolution(result)

    def test_validation_rejects_malformed_archive_shapes_with_value_error(self):
        for result in (None, [], {}, {"schema_version": 1}, {"schema_version": 1, "assertions": None}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                validate_resolution(result)
        result = self.resolve()
        result["assertions"] = [None]
        with self.assertRaises(ValueError):
            validate_resolution(result)


if __name__ == "__main__":
    unittest.main()
