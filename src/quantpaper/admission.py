"""Pure admission checks for synthetic historical diagnostics, never orders.

The current frozen model's readiness gate is not a historical prediction. Even
an approved result is only permission for an offline simulation diagnostic; this
module has no broker, network, persistence, training, or fill interface.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from numbers import Real
import re

import pandas as pd

from .marketdata.quality import _optional_timestamp


MODEL_SCOPE = "frozen_v2_gate_only_not_historical_prediction"
STOCK_SYMBOLS = frozenset({"SPY", "JPM", "XOM", "WMT", "JNJ"})
SYMBOLS = STOCK_SYMBOLS | {"BTC/USD"}
LATENCIES_MS = (0, 250, 1000)
HARD_NOTIONAL_CAP = Decimal("25")
HARD_MAX_AGE_MS = 1000
_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]{1,8})?\Z", re.ASCII)
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


def _amount(value):
    if not isinstance(value, str) or len(value) > 64 or not _DECIMAL.fullmatch(value):
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() and result > 0 else None


def _amount_text(value):
    # String manipulation avoids Decimal context rounding during normalize().
    whole, separator, fraction = value.partition(".")
    whole = whole.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return whole + ("." + fraction if separator and fraction else "")


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _number(value, *, positive=True):
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        result = float(value)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and (result > 0 if positive else result >= 0) else None


def _intent_fields(intent):
    if not isinstance(intent, dict):
        return {}, ["INTENT_MALFORMED"]
    reasons = []
    symbol = intent.get("symbol")
    if not isinstance(symbol, str) or symbol not in SYMBOLS:
        reasons.append("INTENT_SYMBOL_UNSUPPORTED")
        symbol = None
    side = intent.get("side")
    if not isinstance(side, str) or side not in {"BUY", "SELL"}:
        reasons.append("INTENT_SIDE_INVALID")
        side = None
    amount = _amount(intent.get("notional_decimal"))
    if amount is None:
        reasons.append("INTENT_NOTIONAL_INVALID")
    decision = _optional_timestamp(intent.get("decision_at"))
    if decision is None:
        reasons.append("INTENT_DECISION_TIME_INVALID")
    latency = intent.get("latency_ms")
    if type(latency) is not int or latency not in LATENCIES_MS:
        reasons.append("INTENT_LATENCY_INVALID")
        latency = None
    source_hash = intent.get("source_completion_sha256")
    if source_hash is not None and not _hash(source_hash):
        reasons.append("INTENT_SOURCE_HASH_INVALID")
        source_hash = None
    fields = {
        "symbol": symbol, "side": side,
        "notional_decimal": _amount_text(intent["notional_decimal"]) if amount is not None else None,
        "decision_at": decision.isoformat() if decision is not None else None,
        "latency_ms": latency, "source_completion_sha256": source_hash,
    }
    return fields, reasons


def deterministic_intent_id(intent: dict) -> str:
    """Bind diagnostic identity to source, symbol, side, amount, time, latency."""
    fields, reasons = _intent_fields(intent)
    if reasons:
        raise ValueError("cannot identify malformed synthetic intent")
    encoded = json.dumps({"schema": "synthetic-admission-v1", **fields},
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "synthetic-v1-" + hashlib.sha256(encoded).hexdigest()


def _policy_checks(policy):
    policy = policy if isinstance(policy, dict) else {}
    reasons = []
    if policy.get("paper_only") is not True:
        reasons.append("POLICY_PAPER_ONLY_REQUIRED")
    if policy.get("execution_enabled") is not False:
        reasons.append("POLICY_EXECUTION_MUST_BE_DISABLED")
    limit = _amount(policy.get("max_notional_usd", "25"))
    if limit is None or limit > HARD_NOTIONAL_CAP:
        reasons.append("POLICY_NOTIONAL_LIMIT_INVALID")
        limit = HARD_NOTIONAL_CAP
    age = policy.get("max_quote_age_ms", HARD_MAX_AGE_MS)
    if type(age) is not int or not 0 <= age <= HARD_MAX_AGE_MS:
        reasons.append("POLICY_QUOTE_AGE_LIMIT_INVALID")
        age = HARD_MAX_AGE_MS
    stock_feed = policy.get("expected_stock_feed", "sip")
    crypto_feed = policy.get("expected_crypto_feed", "crypto_us")
    if not isinstance(stock_feed, str) or stock_feed not in {"sip", "iex"} or crypto_feed != "crypto_us":
        reasons.append("POLICY_EXPECTED_FEED_INVALID")
    expected_hash = policy.get("expected_source_completion_sha256")
    if expected_hash is not None and not _hash(expected_hash):
        reasons.append("POLICY_SOURCE_HASH_INVALID")
    return reasons, limit, age, stock_feed, crypto_feed


def _model_checks(model_evidence):
    evidence = model_evidence if isinstance(model_evidence, dict) else {}
    reasons = []
    if evidence.get("verified") is not True:
        reasons.append("MODEL_EVIDENCE_UNVERIFIED")
    if evidence.get("approved_for_paper") is not True:
        reasons.append("MODEL_NOT_APPROVED")
    if evidence.get("scope") != MODEL_SCOPE:
        reasons.append("MODEL_EVIDENCE_SCOPE_INVALID")
    digest = evidence.get("model_sha256")
    if digest is not None and not _hash(digest):
        reasons.append("MODEL_EVIDENCE_HASH_INVALID")
    elif evidence.get("approved_for_paper") is True and digest is None:
        reasons.append("MODEL_EVIDENCE_HASH_REQUIRED")
    return reasons


def _source_checks(replay, fields, policy, stock_feed, crypto_feed):
    reasons = []
    source = replay.get("source") if isinstance(replay.get("source"), dict) else {}
    rules = replay.get("policy") if isinstance(replay.get("policy"), dict) else {}
    symbol = fields.get("symbol")
    target = _optional_timestamp(fields.get("decision_at"))
    if replay.get("source_usable") is not True or replay.get("source_reasons") != []:
        reasons.append("SOURCE_REJECTED")
    if (replay.get("research_only") is not True or replay.get("execution_enabled") is not False
            or replay.get("fills_assumed") is not False):
        reasons.append("REPLAY_RESEARCH_ONLY_FLAGS_REQUIRED")
    if type(replay.get("schema_version")) is not int or replay["schema_version"] != 1:
        reasons.append("REPLAY_SCHEMA_UNSUPPORTED")
    if symbol is None or replay.get("symbol") != symbol or source.get("symbol") != symbol:
        reasons.append("SOURCE_SYMBOL_MISMATCH")
    expected_feed = crypto_feed if symbol == "BTC/USD" else stock_feed
    if source.get("feed") != expected_feed or replay.get("feed") != expected_feed:
        reasons.append("SOURCE_FEED_MISMATCH")
    if source.get("kind") != "quotes" or source.get("complete") is not True:
        reasons.append("SOURCE_METADATA_INVALID")
    if type(source.get("pages")) is not int or not 1 <= source["pages"] <= 3:
        reasons.append("SOURCE_PAGE_COUNT_INVALID")
    if target is None or _optional_timestamp(replay.get("target_at")) != target:
        reasons.append("SOURCE_TARGET_TIME_MISMATCH")
    if target is not None:
        start, end = target - pd.Timedelta(seconds=5), target + pd.Timedelta(seconds=2)
        if (_optional_timestamp(source.get("start")) != start or _optional_timestamp(source.get("end")) != end
                or _optional_timestamp(rules.get("required_start")) != start
                or _optional_timestamp(rules.get("required_end")) != end):
            reasons.append("SOURCE_WINDOW_COVERAGE_INVALID")
        observed = _optional_timestamp(source.get("observed_at"))
        if observed is None or observed < end:
            reasons.append("SOURCE_OBSERVATION_TIME_INVALID")
    if (rules.get("strictly_before_asof") is not True
            or rules.get("fetch_range_convention") != "start inclusive, end exclusive"
            or type(rules.get("max_age_ms")) is not int
            or not 0 <= rules.get("max_age_ms", -1) <= HARD_MAX_AGE_MS):
        reasons.append("SOURCE_CAUSAL_POLICY_INVALID")
    counts = replay.get("counts") if isinstance(replay.get("counts"), dict) else {}
    if (type(counts.get("received")) is not int or not 0 <= counts["received"] <= 30_000
            or type(counts.get("unparseable_timestamp")) is not int or counts["unparseable_timestamp"] != 0
            or type(counts.get("unprocessed_due_to_record_limit")) is not int
            or counts["unprocessed_due_to_record_limit"] != 0):
        reasons.append("SOURCE_EVENT_INTEGRITY_INVALID")
    expected_hash = policy.get("expected_source_completion_sha256")
    if expected_hash is not None and fields.get("source_completion_sha256") != expected_hash:
        reasons.append("SOURCE_COMPLETION_HASH_MISMATCH")
    return reasons


def _quote_checks(label, state, *, expected_asof, target, symbol, max_age_ms):
    prefix = f"QUOTE_{label}_"
    reasons = []
    if not isinstance(state, dict):
        return [prefix + "MISSING"], None
    status = state.get("status")
    if status != "VALID":
        suffix = status if isinstance(status, str) and status in {"STALE", "INVALID", "MISSING", "SOURCE_REJECTED"} else "STATUS_INVALID"
        reasons.append(prefix + suffix)
    if expected_asof is None or _optional_timestamp(state.get("asof")) != expected_asof:
        reasons.append(prefix + "ASOF_MISMATCH")
    if status == "VALID" and state.get("reasons") != []:
        reasons.append(prefix + "STATE_INCONSISTENT")
    event = _optional_timestamp(state.get("event_at"))
    if event is None and status == "VALID":
        reasons.append(prefix + "EVENT_TIME_INVALID")
    if event is not None and expected_asof is not None:
        if event >= expected_asof:
            reasons.append(prefix + "EVENT_NOT_STRICTLY_PRIOR")
        if target is not None and not target - pd.Timedelta(seconds=5) <= event < target + pd.Timedelta(seconds=2):
            reasons.append(prefix + "EVENT_OUTSIDE_SOURCE_WINDOW")
        age = (expected_asof.value - event.value) / 1_000_000
        if expected_asof.value - event.value > max_age_ms * 1_000_000:
            reasons.append(prefix + "STALE")
        stated_age = _number(state.get("age_ms"), positive=False)
        if stated_age is None or not math.isclose(stated_age, age, rel_tol=1e-12, abs_tol=1e-9):
            reasons.append(prefix + "AGE_INCONSISTENT")
    quote = state.get("quote")
    if not isinstance(quote, dict):
        if status == "VALID":
            reasons.append(prefix + "PAYLOAD_MISSING")
        return reasons, None
    if status != "VALID" and status != "STALE":
        reasons.append(prefix + "STATE_INCONSISTENT")
    if event is None or _optional_timestamp(quote.get("timestamp")) != event:
        reasons.append(prefix + "QUOTE_EVENT_TIME_MISMATCH")
    values = {name: _number(quote.get(name)) for name in (
        "bid_price", "ask_price", "bid_size_raw_units", "ask_size_raw_units")}
    if any(value is None for value in values.values()):
        reasons.append(prefix + "PRICE_OR_SIZE_INVALID")
    elif values["bid_price"] >= values["ask_price"]:
        reasons.append(prefix + "LOCKED_OR_CROSSED")
    expected_filter = "stock_condition_filter_not_applied_to_crypto"
    if symbol in STOCK_SYMBOLS:
        expected_filter = "exact_single_R_and_known_tape"
        if quote.get("quote_conditions") != ["R"] or quote.get("quote_conditions_shape") != "list_of_strings":
            reasons.append(prefix + "CONDITION_INVALID")
        if not isinstance(quote.get("tape"), str) or quote["tape"] not in {"A", "B", "C"}:
            reasons.append(prefix + "TAPE_INVALID")
    if quote.get("condition_filter") != expected_filter:
        reasons.append(prefix + "CONDITION_POLICY_INVALID")
    metadata = state.get("event_metadata")
    if not isinstance(metadata, list) or len(metadata) != 1 or not isinstance(metadata[0], dict):
        reasons.append(prefix + "EVENT_METADATA_AMBIGUOUS_OR_MISSING")
    elif any(metadata[0].get(field) != quote.get(field) for field in (
        "quote_conditions", "quote_conditions_shape", "bid_exchange", "ask_exchange", "tape")):
        reasons.append(prefix + "EVENT_METADATA_INCONSISTENT")
    return reasons, quote


def evaluate_intent(intent: dict, replay_window: dict, model_evidence: dict, *, policy: dict) -> dict:
    """Accumulate all blockers and produce a deterministic, terminal lifecycle.

    Hash/evidence authenticity must be established by the read-only caller; no
    boolean in this function can prove a file or model was independently vetted.
    Calling this function twice is deterministic, not durable duplicate handling.
    """
    fields, intent_reasons = _intent_fields(intent)
    intent = intent if isinstance(intent, dict) else {}
    replay = replay_window if isinstance(replay_window, dict) else {}
    policy = policy if isinstance(policy, dict) else {}
    policy_reasons, limit, max_age, stock_feed, crypto_feed = _policy_checks(policy)
    amount = _amount(fields.get("notional_decimal"))
    if amount is not None and amount > limit:
        intent_reasons.append("INTENT_NOTIONAL_LIMIT_EXCEEDED")
    expected_id = None
    if not _intent_fields(intent)[1]:
        expected_id = deterministic_intent_id(intent)
    if expected_id is None or intent.get("id") != expected_id:
        intent_reasons.append("INTENT_ID_MISMATCH")
    model_reasons = _model_checks(model_evidence)
    source_reasons = _source_checks(replay, fields, policy, stock_feed, crypto_feed)
    target = _optional_timestamp(fields.get("decision_at"))
    decision_reasons, decision_quote = _quote_checks(
        "DECISION", replay.get("decision_state"), expected_asof=target, target=target,
        symbol=fields.get("symbol"), max_age_ms=max_age)
    latency = fields.get("latency_ms")
    arrival = target + pd.Timedelta(milliseconds=latency) if target is not None and latency is not None else None
    scenarios = replay.get("scenarios")
    matches = [item for item in scenarios if isinstance(item, dict)
               and type(item.get("latency_ms")) is int and item["latency_ms"] == latency] if isinstance(scenarios, list) else []
    scenario = matches[0] if len(matches) == 1 else {}
    arrival_reasons, arrival_quote = _quote_checks(
        "ARRIVAL", scenario.get("arrival_state"), expected_asof=arrival, target=target,
        symbol=fields.get("symbol"), max_age_ms=max_age)
    if len(matches) != 1:
        arrival_reasons.append("QUOTE_ARRIVAL_SCENARIO_MISSING_OR_AMBIGUOUS")
    if arrival is None or _optional_timestamp(scenario.get("arrival_at")) != arrival:
        arrival_reasons.append("QUOTE_ARRIVAL_TIME_MISMATCH")
    decision_state = replay.get("decision_state")
    decision_state = decision_state if isinstance(decision_state, dict) else {}
    arrival_state = scenario.get("arrival_state")
    arrival_state = arrival_state if isinstance(arrival_state, dict) else {}
    decision_event = _optional_timestamp(decision_state.get("event_at"))
    arrival_event = _optional_timestamp(arrival_state.get("event_at"))
    if decision_event is not None and arrival_event is not None:
        if arrival_event < decision_event:
            arrival_reasons.append("QUOTE_ARRIVAL_EVENT_REGRESSION")
        elif arrival_event == decision_event and (
            arrival_state.get("quote") != decision_state.get("quote")
            or arrival_state.get("event_metadata") != decision_state.get("event_metadata")
        ):
            arrival_reasons.append("QUOTE_ARRIVAL_SAME_EVENT_CONTENT_MISMATCH")
    if not decision_reasons and not arrival_reasons:
        if scenario.get("status") != "QUOTE_ELIGIBLE_NO_FILL_ASSUMED":
            arrival_reasons.append("QUOTE_ARRIVAL_SCENARIO_STATUS_INCONSISTENT")
        if (arrival_quote is None or _number(scenario.get("buy_reference_price")) != _number(arrival_quote.get("ask_price"))
                or _number(scenario.get("sell_reference_price")) != _number(arrival_quote.get("bid_price"))):
            arrival_reasons.append("QUOTE_ARRIVAL_REFERENCE_PRICE_INCONSISTENT")
    if latency == 0 and scenario.get("arrival_state") != replay.get("decision_state"):
        arrival_reasons.append("QUOTE_ARRIVAL_ZERO_LATENCY_STATE_MISMATCH")
    groups = {"intent": intent_reasons, "policy": policy_reasons, "model": model_reasons,
              "source": source_reasons, "decision_quote": decision_reasons, "arrival_quote": arrival_reasons}
    checks = {name: {"passed": not reasons, "reasons": sorted(set(reasons))} for name, reasons in groups.items()}
    reasons = sorted({reason for group in groups.values() for reason in group})
    state = "REJECTED" if reasons else "APPROVED_FOR_SIMULATION_ONLY"
    normalized = {"id": expected_id, **fields}
    for name in ("session", "window_name"):
        if isinstance(intent.get(name), str) and len(intent[name]) <= 64:
            normalized[name] = intent[name]
    events = [
        {"sequence": 1, "event": "INTENT_CREATED", "intent_id": expected_id, "synthetic": True},
        {"sequence": 2, "event": "CHECKS_COMPLETED", "intent_id": expected_id, "reasons": reasons},
        {"sequence": 3, "event": state, "intent_id": expected_id, "terminal": True,
         "submitted": False, "filled": False},
    ]
    return {
        "schema_version": 1, "intent_id": expected_id, "intent": normalized,
        "state": state, "reasons": reasons, "checks": checks,
        "research_only": True, "paper_only": True, "execution_enabled": False,
        "synthetic_intent": True, "submitted": False, "filled": False,
        "model_evidence_scope": MODEL_SCOPE,
        "model_evidence_sha256": model_evidence.get("model_sha256") if isinstance(model_evidence, dict) and _hash(model_evidence.get("model_sha256")) else None,
        "events": events,
        "lifecycle_clock": "deterministic logical sequence; no claimed historical receive or broker timestamps",
        "limitations": [
            "Synthetic BUY/SELL intents are diagnostic test cases, not model predictions or trading recommendations.",
            "The current frozen model readiness gate is not evidence of historical signal validity or historical model availability.",
            "Historical event-time quotes cannot authorize live execution; receipt timing and actual fill outcomes are unknown.",
            "Approval means offline simulation diagnostics only. No order submission, fill, position, fee, or return is created.",
            "Source/model file authenticity and durable duplicate prevention remain the caller's responsibility.",
        ],
    }
