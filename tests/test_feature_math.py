from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
import random
import unittest

from quantpaper.research.evidence import digest, utc_timestamp
from quantpaper.research.feature_math import (
    CONTRACT_ID, CORE_SERIES, FEATURE_NAMES, compute_features, feature_contract,
)


class FeatureMathTests(unittest.TestCase):
    CUTOFF = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)

    def signed(self, kind, row):
        unsigned = {key: value for key, value in row.items() if key != "evidence_id"}
        return {"evidence_id": digest({"kind": kind, "record": unsigned}), **unsigned}

    def market(self, count=21):
        result = []
        latest = datetime(2026, 9, 7, tzinfo=timezone.utc)
        for number in range(count):
            close = float(2 ** (number // 2))
            event = latest - timedelta(days=count - number - 1)
            result.append(self.signed("market", {
                "instrument_id": "YF:JPM", "event_time": utc_timestamp(event), "interval": "1d",
                "open": close, "high": close + 1, "low": close * 0.9, "close": close,
                "volume": number + 1, "available_at": "2026-09-08T01:00:00.000000+00:00",
                "ingested_at": "2026-09-08T01:00:01.000000+00:00", "source": "yahoo",
                "data_version": "snapshot-v2:synthetic",
            }))
        return result

    def macros(self):
        output = {}
        for series, value in zip(CORE_SERIES, (3.75, 4.15, 326.5, 4.2), strict=True):
            day = "2026-09-04" if series in ("DFF", "DGS10") else "2026-08-01"
            output[series] = [self.signed("macro", {
                "series_id": series, "observation_date": day, "value": value,
                "vintage_date": "2026-09-04", "available_at": "2026-09-05T05:00:00.000000+00:00",
                "ingested_at": "2026-09-07T10:00:00.000000+00:00", "source": "fred-alfred",
                "availability_basis": "vintage_date_end_america_chicago",
            })]
        return output

    def definitions(self):
        specs = feature_contract()["quality_policies"]["macro_definitions"]
        result = {}
        for series in CORE_SERIES:
            result[series] = {
                "series_id": series, "title": f"Synthetic {series}", **specs[series],
                "frequency_short": "D" if series in ("DFF", "DGS10") else "M",
                "units_short": "%" if series != "CPIAUCSL" else "Index",
                "seasonal_adjustment_short": "NSA" if series in ("DFF", "DGS10") else "SA",
                "observation_start": "2000-01-01", "observation_end": "2026-09-04",
                "realtime_start": "2026-09-07", "realtime_end": "2026-09-07",
                "last_updated": "2026-09-04T20:00:00.000000Z",
                "observed_at": "2026-09-07T11:00:00.000000Z", "available_at": "2026-09-07T11:00:00.000000Z",
                "source": "fred-series-metadata", "source_url": f"https://fred.stlouisfed.org/series/{series}",
                "availability_basis": "local_response_observed_metadata",
            }
        return result

    def compute(self, market=None, macro=None, metadata=None, cutoff=None):
        return compute_features(
            self.market() if market is None else market,
            self.macros() if macro is None else macro,
            self.definitions() if metadata is None else metadata,
            as_of=utc_timestamp(self.CUTOFF if cutoff is None else cutoff),
        )

    def replace_row(self, rows, index, kind, **changes):
        rows[index] = self.signed(kind, {**rows[index], **changes})

    def test_exact_eleven_hand_computed_features(self):
        market = self.market()
        self.replace_row(market, -1, "market", open=1000, high=1100, low=900)
        result = self.compute(market)
        self.assertEqual(tuple(result), FEATURE_NAMES)
        self.assertEqual(len(result), 11)
        expected = {
            FEATURE_NAMES[0]: 1.0, FEATURE_NAMES[1]: 7.0, FEATURE_NAMES[2]: 1023.0,
            FEATURE_NAMES[3]: math.sqrt(5 / 19), FEATURE_NAMES[4]: 0.2,
            FEATURE_NAMES[5]: 21 / 11.5, "macro.DFF.level": 3.75,
            "macro.DGS10.level": 4.15, "macro.CPIAUCSL.level": 326.5,
            "macro.UNRATE.level": 4.2, FEATURE_NAMES[-1]: 0.4,
        }
        for name, value in expected.items():
            with self.subTest(name=name):
                self.assertEqual(result[name]["status"], "AVAILABLE")
                self.assertIsNone(result[name]["reason"])
                self.assertAlmostEqual(result[name]["value"], value)
        self.assertEqual(result["macro.CPIAUCSL.level"]["unit"], "Index 1982-1984=100")
        self.assertEqual(result[FEATURE_NAMES[-1]]["unit"], "percentage_points")
        json.dumps(result, allow_nan=False)

    def test_contract_is_complete_stable_and_caller_independent(self):
        first = feature_contract()
        self.assertEqual(first["contract_id"], CONTRACT_ID)
        self.assertEqual(CONTRACT_ID, "observed_context_v1")
        self.assertEqual(tuple(first["features"]), FEATURE_NAMES)
        self.assertEqual(digest(first), digest(feature_contract()))
        first["quality_policies"]["macro_definitions"]["DFF"]["units"] = "Wrong"
        self.assertEqual(feature_contract()["quality_policies"]["macro_definitions"]["DFF"]["units"], "Percent")

    def test_exact_result_fields_and_full_window_provenance(self):
        market = self.market()
        result = self.compute(market)
        fields = set(feature_contract()["result_fields"])
        for feature in result.values():
            self.assertEqual(set(feature), fields)
        for name, count in zip(FEATURE_NAMES[:6], (2, 6, 21, 21, 1, 20), strict=True):
            self.assertEqual(result[name]["source_evidence_ids"], sorted(row["evidence_id"] for row in market[-count:]))
            self.assertEqual(result[name]["metadata_record_hashes"], [])
            self.assertEqual(result[name]["latest_input_available_at"], utc_timestamp(market[-1]["available_at"]))

    def test_metadata_and_latest_observation_provenance(self):
        macro, metadata = self.macros(), self.definitions()
        result = self.compute(macro=macro, metadata=metadata)
        for series in CORE_SERIES:
            feature = result[f"macro.{series}.level"]
            self.assertEqual(feature["source_evidence_ids"], [macro[series][0]["evidence_id"]])
            self.assertEqual(feature["metadata_record_hashes"], [digest(metadata[series])])
            self.assertEqual(feature["latest_input_available_at"], utc_timestamp(metadata[series]["observed_at"]))
        spread = result[FEATURE_NAMES[-1]]
        self.assertEqual(spread["source_evidence_ids"], sorted(macro[series][0]["evidence_id"] for series in ("DFF", "DGS10")))
        self.assertEqual(spread["metadata_record_hashes"], sorted(digest(metadata[series]) for series in ("DFF", "DGS10")))

    def test_shuffled_inputs_are_deterministic_and_not_mutated(self):
        market, macro, metadata = self.market(), self.macros(), self.definitions()
        for series in CORE_SERIES:
            older = self.signed("macro", {**macro[series][0], "observation_date": "2025-01-01", "value": 999})
            macro[series].append(older)
        original = deepcopy((market, macro, metadata))
        expected = self.compute(market, macro, metadata)
        random.Random(42).shuffle(market)
        for rows in macro.values():
            rows.reverse()
        shuffled = deepcopy((market, macro, metadata))
        self.assertEqual(self.compute(market, macro, metadata), expected)
        self.assertEqual((market, macro, metadata), shuffled)
        self.assertNotEqual(market, original[0])

    def test_latest_null_does_not_fall_back(self):
        macro = self.macros()
        latest = self.signed("macro", {**macro["DFF"][0], "value": None})
        older = self.signed("macro", {**macro["DFF"][0], "observation_date": "2026-09-03", "value": 8})
        macro["DFF"] = [older, latest]
        result = self.compute(macro=macro)
        level = result["macro.DFF.level"]
        self.assertEqual(level["status"], "MISSING_VALUE")
        self.assertIsNone(level["value"])
        self.assertEqual(level["source_evidence_ids"], [latest["evidence_id"]])
        self.assertEqual(result[FEATURE_NAMES[-1]]["status"], "INPUT_BLOCKED")

    def test_insufficient_history_is_per_feature(self):
        result = self.compute(market=self.market(6))
        self.assertEqual(result[FEATURE_NAMES[1]]["status"], "AVAILABLE")
        for name in (FEATURE_NAMES[2], FEATURE_NAMES[3], FEATURE_NAMES[5]):
            self.assertEqual(result[name]["status"], "INSUFFICIENT_HISTORY")
            self.assertEqual(len(result[name]["source_evidence_ids"]), 6)

    def test_empty_inputs_keep_all_slots(self):
        result = self.compute(market=[], macro={series: [] for series in CORE_SERIES},
                              metadata={series: None for series in CORE_SERIES})
        self.assertEqual(tuple(result), FEATURE_NAMES)
        for feature in result.values():
            self.assertIsNone(feature["value"])
            self.assertIsNone(feature["latest_input_available_at"])
            self.assertEqual(feature["source_evidence_ids"], [])
            self.assertEqual(feature["metadata_record_hashes"], [])

    def test_seven_calendar_day_market_age_is_inclusive(self):
        market = self.market(1)
        for age, expected in ((7, "AVAILABLE"), (8, "STALE_MARKET")):
            event = self.CUTOFF.replace(hour=0) - timedelta(days=age)
            self.replace_row(market, 0, "market", event_time=utc_timestamp(event))
            self.assertEqual(self.compute(market)[FEATURE_NAMES[4]]["status"], expected)

    def test_seven_day_gap_inclusive_and_only_selected_window(self):
        for gap, expected in ((7, "AVAILABLE"), (8, "EXCESSIVE_MARKET_GAP")):
            market = self.market(2)
            last = datetime.fromisoformat(market[-1]["event_time"])
            self.replace_row(market, 0, "market", event_time=utc_timestamp(last - timedelta(days=gap)))
            result = self.compute(market)
            self.assertEqual(result[FEATURE_NAMES[0]]["status"], expected)
            self.assertEqual(result[FEATURE_NAMES[4]]["status"], "AVAILABLE")
            self.assertEqual(len(result[FEATURE_NAMES[0]]["source_evidence_ids"]), 2)
        market = self.market(22)
        self.replace_row(market, 0, "market", event_time="2025-01-01T00:00:00Z")
        self.assertEqual(self.compute(market)[FEATURE_NAMES[2]]["status"], "AVAILABLE")

    def test_zero_volume_denominator_is_missing(self):
        market = self.market()
        for index in range(len(market)):
            self.replace_row(market, index, "market", volume=0)
        feature = self.compute(market)[FEATURE_NAMES[5]]
        self.assertEqual(feature["status"], "ZERO_DENOMINATOR")
        self.assertIsNone(feature["value"])
        self.assertEqual(len(feature["source_evidence_ids"]), 20)

    def test_missing_metadata_preserves_latest_source_provenance(self):
        metadata = self.definitions()
        metadata["DFF"] = None
        feature = self.compute(metadata=metadata)["macro.DFF.level"]
        self.assertEqual(feature["status"], "MISSING_METADATA")
        self.assertEqual(feature["source_evidence_ids"], [self.macros()["DFF"][0]["evidence_id"]])
        self.assertEqual(feature["metadata_record_hashes"], [])

    def test_definition_mismatch_not_silent_unit_conversion(self):
        for field, value in (("units", "Basis Points"), ("frequency", "Daily"),
                             ("seasonal_adjustment", "Seasonally Adjusted")):
            metadata = self.definitions()
            metadata["DFF"][field] = value
            result = self.compute(metadata=metadata)
            self.assertEqual(result["macro.DFF.level"]["status"], "DEFINITION_MISMATCH")
            self.assertEqual(result[FEATURE_NAMES[-1]]["status"], "INPUT_BLOCKED")

    def test_metadata_thirty_day_age_inclusive(self):
        for excess, status in ((0, "AVAILABLE"), (1, "STALE_METADATA")):
            metadata = self.definitions()
            observed = self.CUTOFF - timedelta(days=30, microseconds=excess)
            record = metadata["DFF"]
            record.update(observed_at=observed.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                          available_at=observed.isoformat(timespec="microseconds").replace("+00:00", "Z"),
                          realtime_start="2026-08-01", realtime_end="2026-08-01",
                          last_updated="2026-08-01T00:00:00.000000Z")
            self.assertEqual(self.compute(metadata=metadata)["macro.DFF.level"]["status"], status)

    def test_macro_daily_and_monthly_available_age_limits(self):
        for series, maximum in (("DFF", 7), ("DGS10", 7), ("CPIAUCSL", 62), ("UNRATE", 62)):
            for excess, status in ((0, "AVAILABLE"), (1, "STALE_OBSERVATION")):
                macro = self.macros()
                available = self.CUTOFF - timedelta(days=maximum, microseconds=excess)
                self.replace_row(macro[series], 0, "macro", available_at=utc_timestamp(available),
                                 vintage_date=available.date().isoformat(),
                                 observation_date=available.date().isoformat())
                with self.subTest(series=series, excess=excess):
                    self.assertEqual(self.compute(macro=macro)[f"macro.{series}.level"]["status"], status)

    def test_stale_macro_latest_does_not_fall_back_to_recently_revised_older(self):
        macro = self.macros()
        self.replace_row(macro["DFF"], 0, "macro", available_at="2026-08-30T00:00:00Z",
                         vintage_date="2026-08-29", observation_date="2026-08-28")
        older = self.signed("macro", {**macro["DFF"][0], "observation_date": "2026-08-27",
                                      "available_at": "2026-09-07T00:00:00Z", "value": 8})
        macro["DFF"].append(older)
        result = self.compute(macro=macro)["macro.DFF.level"]
        self.assertEqual(result["status"], "STALE_OBSERVATION")
        self.assertNotIn(older["evidence_id"], result["source_evidence_ids"])

    def test_newly_revised_ancient_observations_are_not_current_context(self):
        macro, metadata = self.macros(), self.definitions()
        for series in CORE_SERIES:
            self.replace_row(macro[series], 0, "macro", observation_date="2000-01-01",
                             vintage_date="2026-09-07", available_at="2026-09-08T05:00:00Z")
        result = self.compute(macro=macro, metadata=metadata)
        for series in CORE_SERIES:
            with self.subTest(series=series):
                feature = result[f"macro.{series}.level"]
                self.assertEqual(feature["status"], "STALE_OBSERVATION_DATE")
                self.assertIsNone(feature["value"])
                self.assertEqual(feature["source_evidence_ids"], [macro[series][0]["evidence_id"]])
                self.assertEqual(feature["metadata_record_hashes"], [digest(metadata[series])])
                self.assertEqual(feature["latest_input_available_at"], "2026-09-08T05:00:00.000000+00:00")
        self.assertEqual(result[FEATURE_NAMES[-1]]["status"], "INPUT_BLOCKED")

    def test_macro_observation_calendar_age_exact_boundaries(self):
        for series, maximum in (("DFF", 7), ("DGS10", 7), ("CPIAUCSL", 100), ("UNRATE", 100)):
            for age, status in ((maximum, "AVAILABLE"), (maximum + 1, "STALE_OBSERVATION_DATE")):
                macro = self.macros()
                day = (self.CUTOFF.date() - timedelta(days=age)).isoformat()
                self.replace_row(macro[series], 0, "macro", observation_date=day)
                with self.subTest(series=series, age=age):
                    feature = self.compute(macro=macro)[f"macro.{series}.level"]
                    self.assertEqual(feature["status"], status)
                    later = self.CUTOFF.replace(hour=23, minute=59, second=59, microsecond=999999)
                    self.assertEqual(self.compute(macro=macro, cutoff=later)[f"macro.{series}.level"]["status"], status)

    def test_observation_age_is_separate_from_release_age(self):
        macro = self.macros()
        self.replace_row(macro["DFF"], 0, "macro", observation_date="2000-01-01",
                         available_at="2026-08-01T00:00:00Z", vintage_date="2026-07-31")
        first = self.compute(macro=macro)["macro.DFF.level"]
        self.assertEqual(first["status"], "STALE_OBSERVATION")
        self.replace_row(macro["DFF"], 0, "macro", available_at="2026-09-07T00:00:00Z",
                         vintage_date="2026-09-06")
        revised = self.compute(macro=macro)["macro.DFF.level"]
        self.assertEqual(revised["status"], "STALE_OBSERVATION_DATE")
        self.assertNotEqual(first["reason"], revised["reason"])
        self.replace_row(macro["DFF"], 0, "macro", value=None)
        self.assertEqual(self.compute(macro=macro)["macro.DFF.level"]["status"], "MISSING_VALUE")

    def test_contract_freezes_observation_age_and_packet_identity_mask(self):
        contract = feature_contract()
        policy = contract["quality_policies"]
        self.assertEqual(policy["macro_max_observation_date_age_days"],
                         {"DFF": 7, "DGS10": 7, "CPIAUCSL": 100, "UNRATE": 100})
        self.assertEqual(policy["macro_gate_order"][-2:], ["STALE_OBSERVATION", "STALE_OBSERVATION_DATE"])
        mask = policy["packet_identity_mask"]
        self.assertEqual(mask["affected_features"], list(FEATURE_NAMES[:6]))
        self.assertEqual(mask["blocked_fields"], {
            "status": "IDENTITY_BLOCKED", "value": None,
            "reason": "No eligible matching single-source equity description binds these inputs.",
        })
        self.assertEqual(mask["blocked_fields"]["reason"], contract["status_reasons"]["IDENTITY_BLOCKED"])
        self.assertEqual(mask["supported_description"], {"source": "yahoo", "asset_class": "equity",
                                                        "instrument_id": "YF:{symbol}"})
        self.assertFalse(mask["macro_features_masked"])
        self.assertFalse(mask["historical_identity_verified"])
        self.assertEqual(mask["binding_gate_order"][0], "CATALOG_UNAVAILABLE_AT_CUTOFF")

    def test_missing_observation_preserves_metadata_hash(self):
        macro = self.macros()
        macro["UNRATE"] = []
        feature = self.compute(macro=macro)["macro.UNRATE.level"]
        self.assertEqual(feature["status"], "MISSING_OBSERVATION")
        self.assertEqual(feature["metadata_record_hashes"], [digest(self.definitions()["UNRATE"])])

    def test_rate_spread_requires_equal_observation_dates(self):
        macro = self.macros()
        self.replace_row(macro["DFF"], 0, "macro", observation_date="2026-09-03")
        result = self.compute(macro=macro)
        self.assertEqual(result["macro.DFF.level"]["status"], "AVAILABLE")
        self.assertEqual(result["macro.DGS10.level"]["status"], "AVAILABLE")
        self.assertEqual(result[FEATURE_NAMES[-1]]["status"], "OBSERVATION_DATE_MISMATCH")
        self.assertIsNone(result[FEATURE_NAMES[-1]]["value"])

    def test_negative_rates_are_preserved_without_percent_conversion(self):
        macro = self.macros()
        self.replace_row(macro["DFF"], 0, "macro", value=-0.5)
        result = self.compute(macro=macro)
        self.assertEqual(result["macro.DFF.level"]["value"], -0.5)
        self.assertAlmostEqual(result[FEATURE_NAMES[-1]]["value"], 4.65)

    def test_source_timestamp_equality_and_future_rejected(self):
        for kind in ("market", "macro"):
            for field in ("available_at", "ingested_at"):
                for microseconds in (0, 1):
                    market, macro = self.market(), self.macros()
                    rows = market if kind == "market" else macro["DFF"]
                    self.replace_row(rows, 0, kind, **{field: utc_timestamp(self.CUTOFF + timedelta(microseconds=microseconds))})
                    with self.subTest(kind=kind, field=field, microseconds=microseconds), self.assertRaises(ValueError):
                        self.compute(market, macro)

    def test_metadata_equality_future_and_wrong_series_rejected(self):
        for change in ("equality", "future", "series"):
            metadata = self.definitions()
            if change == "series":
                metadata["DFF"] = metadata["DGS10"]
            else:
                stamp = self.CUTOFF + timedelta(microseconds=1 if change == "future" else 0)
                stamp = stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")
                metadata["DFF"].update(observed_at=stamp, available_at=stamp)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.compute(metadata=metadata)

    def test_bool_nan_infinity_and_invalid_old_unused_rows_rejected(self):
        for value in (True, False, float("nan"), float("inf"), -float("inf"), "1"):
            for kind in ("market", "macro"):
                market, macro = self.market(22), self.macros()
                rows = market if kind == "market" else macro["DFF"]
                key = "close" if kind == "market" else "value"
                rows[0][key] = value
                if not isinstance(value, float) or math.isfinite(value):
                    rows[0] = self.signed(kind, rows[0])
                with self.subTest(kind=kind, value=value), self.assertRaises(ValueError):
                    self.compute(market, macro)

    def test_duplicate_market_utc_day_and_macro_date_rejected(self):
        market = self.market()
        duplicate = self.signed("market", {**market[-1], "event_time": "2026-09-07T01:00:00+01:00"})
        with self.assertRaises(ValueError):
            self.compute(market + [duplicate])
        macro = self.macros()
        macro["DFF"].append(self.signed("macro", {**macro["DFF"][0], "value": 9}))
        with self.assertRaises(ValueError):
            self.compute(macro=macro)

    def test_wrong_source_interval_version_and_mixed_instrument_rejected(self):
        for change in ({"source": "other"}, {"interval": "1m"}, {"data_version": "adjusted-v1"},
                       {"instrument_id": "YF:SPY"}, {"open": 0}, {"volume": -1}, {"high": 0.1}):
            market = self.market()
            self.replace_row(market, 0, "market", **change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.compute(market)
        for change in ({"source": "other"}, {"series_id": "DGS10"}, {"availability_basis": "unknown"}):
            macro = self.macros()
            self.replace_row(macro["DFF"], 0, "macro", **change)
            with self.assertRaises(ValueError):
                self.compute(macro=macro)

    def test_future_events_and_premature_bar_availability_rejected(self):
        for change in ({"event_time": "2026-09-09T00:00:00Z"},
                       {"event_time": "2026-09-07T06:00:00Z", "available_at": "2026-09-07T12:00:00Z"}):
            market = self.market()
            self.replace_row(market, -1, "market", **change)
            with self.assertRaises(ValueError):
                self.compute(market)
        for field in ("observation_date", "vintage_date"):
            macro = self.macros()
            self.replace_row(macro["DFF"], 0, "macro", **{field: "2026-09-09"})
            with self.assertRaises(ValueError):
                self.compute(macro=macro)

    def test_tampered_evidence_identifier_rejected(self):
        market = self.market()
        market[-1]["volume"] += 1
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.compute(market)

    def test_exact_input_shapes_required(self):
        for market in (None, {}, (), self.market() * 25):
            with self.assertRaises(ValueError):
                compute_features(market, self.macros(), self.definitions(), as_of=utc_timestamp(self.CUTOFF))
        for macro in ({}, {**self.macros(), "EXTRA": []}, {**self.macros(), "DFF": None}):
            with self.assertRaises(ValueError):
                self.compute(macro=macro)
        for metadata in ({}, {**self.definitions(), "EXTRA": None}):
            with self.assertRaises(ValueError):
                self.compute(metadata=metadata)
        market = self.market()
        market[0]["unexpected"] = 0
        with self.assertRaises(ValueError):
            self.compute(market)

    def test_invalid_asof_and_naive_timestamp_rejected(self):
        for cutoff in (None, "2026-09-08", "2026-09-08T12:00:00", self.CUTOFF):
            with self.assertRaises(ValueError):
                compute_features(self.market(), self.macros(), self.definitions(), as_of=cutoff)
        market = self.market()
        self.replace_row(market, 0, "market", event_time="2026-08-18T00:00:00")
        with self.assertRaises(ValueError):
            self.compute(market)

    def test_finite_input_overflow_produces_explicit_numeric_error(self):
        market = self.market(2)
        self.replace_row(market, 0, "market", open=1e-300, high=1e-300, low=1e-300, close=1e-300)
        self.replace_row(market, 1, "market", open=1e300, high=1e300, low=1e300, close=1e300)
        feature = self.compute(market)[FEATURE_NAMES[0]]
        self.assertEqual(feature["status"], "NUMERIC_ERROR")
        self.assertIsNone(feature["value"])
        json.dumps(feature, allow_nan=False)

    def test_intermediate_volatility_overflow_is_explicit(self):
        market = self.market()
        self.replace_row(market, 0, "market", open=1e-300, high=1e-300, low=1e-300, close=1e-300)
        self.replace_row(market, 1, "market", open=1e300, high=1e300, low=1e300, close=1e300)
        feature = self.compute(market)[FEATURE_NAMES[3]]
        self.assertEqual(feature["status"], "NUMERIC_ERROR")
        self.assertIsNone(feature["value"])

    def test_non_json_input_and_overflowing_event_raise_value_error(self):
        market = self.market()
        market[0]["close"] = object()
        with self.assertRaises(ValueError):
            self.compute(market)
        market = self.market(1)
        self.replace_row(market, 0, "market", event_time="9999-12-31T00:00:00Z")
        with self.assertRaises(ValueError):
            self.compute(market)


if __name__ == "__main__":
    unittest.main()
