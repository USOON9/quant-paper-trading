"""Conservative historical quote-state replay, with no order or fill interface.

This is event-time reconstruction, not a complete order book and not receive-time
simulation. The latest event strictly before an observation controls state, even
when that update is unusable. Invalid or ambiguous updates must never be skipped
in favor of an older attractive quote.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import json

import pandas as pd

from .quality import _fetch_info, _number, _optional_timestamp, _timestamp


STOCK_SYMBOLS = frozenset({"SPY", "JPM", "XOM", "WMT", "JNJ"})
DEFAULT_LATENCIES_MS = (0, 250, 1000)
LOOKBACK_SECONDS = 5
LOOKAHEAD_SECONDS = 2
MAX_PAGES = 3
MAX_RECORDS = 30_000


def _validate_source(symbol, fetch, start, end):
    reasons = []
    if not isinstance(fetch, dict):
        return ["fetch_must_be_an_object"]
    if fetch.get("symbol") != symbol:
        reasons.append("fetch_symbol_missing_or_wrong")
    if fetch.get("kind") != "quotes":
        reasons.append("fetch_kind_missing_or_wrong")
    feed = fetch.get("feed")
    allowed = {"crypto_us"} if symbol == "BTC/USD" else {"sip", "iex"}
    if not isinstance(feed, str) or feed not in allowed:
        reasons.append("feed_missing_or_unsupported_for_symbol")
    if fetch.get("complete") is not True:
        reasons.append("fetch_incomplete_or_truncated")
    if type(fetch.get("pages")) is not int or not 1 <= fetch["pages"] <= MAX_PAGES:
        reasons.append("fetch_page_count_missing_or_invalid")
    if (_optional_timestamp(fetch.get("start")) != start
            or _optional_timestamp(fetch.get("end")) != end):
        reasons.append("fetch_range_not_exact_target_minus_5_to_plus_2_seconds")
    observed = _optional_timestamp(fetch.get("observed_at"))
    if observed is None or observed < end:
        reasons.append("observation_time_missing_or_before_window_end")
    if not isinstance(fetch.get("records"), list):
        reasons.append("records_list_missing_or_invalid")
    elif len(fetch["records"]) > MAX_RECORDS:
        reasons.append("record_count_exceeds_research_limit")
    return reasons


def _parse_quote(symbol, record, stamp):
    """Return sanitized quote, or the reasons the update poisons state."""
    reasons = []
    if symbol in STOCK_SYMBOLS and record.get("c") != ["R"]:
        reasons.append("stock_condition_not_exact_single_regular_R")
    if symbol in STOCK_SYMBOLS and (
        not isinstance(record.get("z"), str) or record["z"] not in {"A", "B", "C"}
    ):
        reasons.append("stock_tape_missing_or_unknown")
    quote = None
    try:
        bid, ask, bid_size, ask_size = [
            _number(record.get(key), positive=True) for key in ("bp", "ap", "bs", "as")
        ]
        if bid > ask:
            reasons.append("crossed_quote")
        elif bid == ask:
            reasons.append("locked_quote")
        else:
            midpoint = bid + (ask - bid) / 2
            quote = {
                "timestamp": stamp.isoformat(), "bid_price": bid, "ask_price": ask,
                "bid_size_raw_units": bid_size, "ask_size_raw_units": ask_size,
                "mid_price": midpoint, "full_spread_bps": (ask - bid) / midpoint * 10_000,
                "condition_filter": "exact_single_R_and_known_tape" if symbol in STOCK_SYMBOLS else "stock_condition_filter_not_applied_to_crypto",
                **_event_metadata(record),
            }
    except (ValueError, TypeError, OverflowError):
        reasons.append("quote_price_or_size_nonpositive_nonfinite_or_malformed")
    return (None if reasons else quote), reasons


def _event_metadata(record):
    conditions = record.get("c")
    conditions_valid_shape = isinstance(conditions, list) and all(isinstance(item, str) for item in conditions)
    return {
        "quote_conditions": list(conditions) if conditions_valid_shape else None,
        "quote_conditions_shape": "missing" if "c" not in record else (
            "list_of_strings" if conditions_valid_shape else "malformed"),
        "bid_exchange": record.get("bx") if isinstance(record.get("bx"), str) else None,
        "ask_exchange": record.get("ax") if isinstance(record.get("ax"), str) else None,
        "tape": record.get("z") if isinstance(record.get("z"), str) else None,
    }


def _events(symbol, records, start, end):
    buckets = {}
    unique = set()
    counts = Counter(received=len(records), in_window=0, outside=0,
                     exact_duplicates=0, unparseable_timestamp=0, unique_events=0,
                     invalid_events=0, event_timestamp_groups=0, ambiguous_groups=0,
                     state_transitions=0, invalid_state_updates=0, invalidations=0, recoveries=0)
    global_reasons = []
    invalid_reasons = Counter()
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            counts["unparseable_timestamp"] += 1
            global_reasons.append("event_timestamp_missing_or_unparseable_cannot_order")
            continue
        if any(field in record and record[field] != symbol for field in ("symbol", "S")):
            global_reasons.append("record_declares_wrong_symbol")
        try:
            stamp = _timestamp(record.get("t"))
        except ValueError:
            counts["unparseable_timestamp"] += 1
            global_reasons.append("event_timestamp_missing_or_unparseable_cannot_order")
            continue
        if not start <= stamp < end:
            counts["outside"] += 1
            continue
        counts["in_window"] += 1
        encoding_invalid = False
        try:
            # Only timestamp spelling is normalized. Distinct raw conditions,
            # venues or values at the same timestamp remain unsequenced events.
            fingerprint = json.dumps({**record, "t": stamp.isoformat()}, sort_keys=True,
                                     allow_nan=True, separators=(",", ":"))
        except (ValueError, TypeError, OverflowError):
            fingerprint = ("unserializable_event", index)
            encoding_invalid = True
        if fingerprint in unique:
            counts["exact_duplicates"] += 1
            continue
        unique.add(fingerprint)
        counts["unique_events"] += 1
        quote, reasons = _parse_quote(symbol, record, stamp)
        if encoding_invalid:
            reasons.append("quote_record_encoding_malformed")
            quote = None
        if reasons:
            counts["invalid_events"] += 1
            invalid_reasons.update(set(reasons))
        buckets.setdefault(stamp.value, []).append({"quote": quote, "reasons": reasons,
                                                  "metadata": _event_metadata(record)})
    groups = []
    previous_valid = None
    first_seed = None
    for stamp_ns, updates in sorted(buckets.items()):
        stamp = pd.Timestamp(stamp_ns, unit="ns", tz="UTC")
        ambiguous = len(updates) > 1
        reasons = sorted({reason for item in updates for reason in item["reasons"]})
        if ambiguous:
            reasons.append("distinct_same_timestamp_updates_unsequenced")
            counts["ambiguous_groups"] += 1
        quote = updates[0]["quote"] if len(updates) == 1 and not reasons else None
        valid = quote is not None
        if valid and first_seed is None:
            first_seed = stamp.isoformat()
        counts["state_transitions"] += 1
        if not valid:
            counts["invalid_state_updates"] += 1
        if previous_valid is True and not valid:
            counts["invalidations"] += 1
        if previous_valid is False and valid:
            counts["recoveries"] += 1
        previous_valid = valid
        groups.append({"event_at": stamp.isoformat(), "event_ns": stamp_ns,
                       "quote": quote, "reasons": reasons, "event_count": len(updates),
                       "event_metadata": [item["metadata"] for item in updates]})
    counts["event_timestamp_groups"] = len(groups)
    return groups, dict(counts), sorted(set(global_reasons)), dict(sorted(invalid_reasons.items())), first_seed


def _state(groups, asof, max_age_ms, source_reasons):
    state = {
        "asof": asof.isoformat(), "status": "SOURCE_REJECTED" if source_reasons else "MISSING",
        "event_at": None, "age_ms": None, "quote": None,
        "event_metadata": [], "reasons": list(source_reasons),
    }
    if source_reasons:
        return state
    prior = [group for group in groups if group["event_ns"] < asof.value]
    if not prior:
        state["reasons"] = ["no_event_strictly_before_asof"]
        return state
    latest = prior[-1]
    age_ns = asof.value - latest["event_ns"]
    state.update(event_at=latest["event_at"], age_ms=age_ns / 1_000_000,
                 quote=latest["quote"], event_metadata=latest["event_metadata"])
    if latest["quote"] is None:
        state.update(status="INVALID", reasons=list(latest["reasons"]))
    elif age_ns > max_age_ms * 1_000_000:
        state.update(status="STALE", reasons=["latest_update_older_than_max_age"])
    else:
        state.update(status="VALID", reasons=[])
    return state


def analyze_replay_window(
    symbol: str, fetch: dict, *, target: datetime, max_age_ms: int = 1000,
    latencies_ms: tuple = DEFAULT_LATENCIES_MS,
) -> dict:
    """Replay one exact seven-second historical quote window fail-closed.

    Each snapshot uses the latest event with ``timestamp < asof``; an event at
    the exact decision/arrival timestamp is excluded because event ordering is
    unknown. A blocked decision cannot recover into a hypothetical eligible
    touch merely because a usable quote appears by a later arrival.
    """
    if not isinstance(symbol, str) or symbol.strip().upper() not in STOCK_SYMBOLS | {"BTC/USD"}:
        raise ValueError("unsupported replay symbol")
    symbol = symbol.strip().upper()
    if type(max_age_ms) is not int or max_age_ms < 0:
        raise ValueError("max_age_ms must be a nonnegative integer")
    if (not isinstance(latencies_ms, tuple) or not latencies_ms
            or any(type(value) is not int or not 0 <= value <= LOOKAHEAD_SECONDS * 1000 for value in latencies_ms)
            or tuple(sorted(set(latencies_ms))) != latencies_ms):
        raise ValueError("latencies_ms must be unique sorted integer milliseconds within 0..2000")
    stamp = _timestamp(target)
    start = stamp - pd.Timedelta(seconds=LOOKBACK_SECONDS)
    end = stamp + pd.Timedelta(seconds=LOOKAHEAD_SECONDS)
    source_reasons = _validate_source(symbol, fetch, start, end)
    records = fetch.get("records", []) if isinstance(fetch, dict) else []
    records = records if isinstance(records, list) else []
    over_record_limit = len(records) > MAX_RECORDS
    groups, counts, event_reasons, invalid_reasons, first_seed = _events(
        symbol, [] if over_record_limit else records, start, end)
    counts["received"] = len(records)
    counts["unprocessed_due_to_record_limit"] = len(records) if over_record_limit else 0
    source_reasons = sorted(set(source_reasons + event_reasons))
    decision = _state(groups, stamp, max_age_ms, source_reasons)
    scenarios = []
    for latency in latencies_ms:
        arrival = stamp + pd.Timedelta(milliseconds=latency)
        arrival_state = _state(groups, arrival, max_age_ms, source_reasons)
        eligible = decision["status"] == "VALID" and arrival_state["status"] == "VALID"
        scenarios.append({
            "latency_ms": latency, "arrival_at": arrival.isoformat(),
            "arrival_state": arrival_state,
            "status": "QUOTE_ELIGIBLE_NO_FILL_ASSUMED" if eligible else (
                "DECISION_BLOCKED" if decision["status"] != "VALID" else "ARRIVAL_BLOCKED"),
            "buy_reference_price": arrival_state["quote"]["ask_price"] if eligible else None,
            "sell_reference_price": arrival_state["quote"]["bid_price"] if eligible else None,
        })
    feed = fetch.get("feed") if isinstance(fetch, dict) else None
    allowed = {"crypto_us"} if symbol == "BTC/USD" else {"sip", "iex"}
    return {
        "schema_version": 1, "symbol": symbol, "target_at": stamp.isoformat(),
        "feed": feed if isinstance(feed, str) and feed in allowed else None,
        "source": _fetch_info(fetch) if isinstance(fetch, dict) else {},
        "source_usable": not source_reasons, "source_reasons": source_reasons,
        "research_only": True, "execution_enabled": False, "fills_assumed": False,
        "policy": {
            "strictly_before_asof": True,
            "boundary_policy": "events exactly at decision or arrival are excluded; ordering is unknown",
            "required_start": start.isoformat(), "required_end": end.isoformat(),
            "fetch_range_convention": "start inclusive, end exclusive",
            "api_end_policy": "vendor response may include end timestamp; replay explicitly excludes events at or after end",
            "max_age_ms": max_age_ms,
            "max_pages": MAX_PAGES, "max_records": MAX_RECORDS,
            "staleness_policy": "age equal to max_age_ms is allowed; greater age is stale",
            "condition_policy": "stocks require exactly one R regular condition and tape A/B/C; crypto does not use stock condition or tape codes",
            "state_policy": "latest event governs, including invalidation; no fallback past invalid or unsequenced updates",
            "ordering_policy": "input is sorted by historical event timestamp; receive-time order is unknown",
            "counts_scope": "entire fetched seven-second interval, including events after decision and arrival",
            "transition_count_policy": "one state update per distinct event timestamp group; invalidations/recoveries count valid-to-invalid/invalid-to-valid changes",
        },
        "counts": counts, "invalid_event_reason_counts": invalid_reasons,
        "first_seed_at": first_seed if not source_reasons else None,
        "decision_state": decision, "scenarios": scenarios,
        "limitations": [
            "Historical event-time reconstruction is not receive-time replay; transport delay and clock uncertainty are unknown.",
            "A regular-condition/known-tape filter is a research restriction, not automated-market status, complete tape eligibility, halt-state or market-status verification.",
            "Seven seconds do not establish a complete order book or the state before the capture window.",
            "A valid displayed touch and size do not establish order acceptance, fill probability, queue priority, executable capacity, or fees.",
            "Buy/sell reference prices are diagnostic displayed quotes, never claimed fills, orders, profits, or strategy performance.",
        ],
    }
