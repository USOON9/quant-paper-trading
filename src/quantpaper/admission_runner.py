"""Offline synthetic-intent admission audit; never broker submission or fills."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform

from .admission import deterministic_intent_id, evaluate_intent
from .marketdata.replay_book import analyze_replay_window
from .marketdata.replay_protocol import build_replay_plan
from .marketdata.storage import read_report, scoped_directory, validate_run_directory, write_json
from .scheduler import require_paper_gates_closed


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTRACT = "offline_intent_admission_v1"
_REPLAY_CONTRACT = "intraday_asof_replay_v1"
_MODEL_META = "artifacts/yahoo_walkforward_v2_model.json"
_MODEL_BINARY = "artifacts/yahoo_walkforward_v2_model.joblib"


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _aware(value, name: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError:
        raise ValueError(f"{name} must be a timezone-aware timestamp") from None
    if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    return stamp.astimezone(timezone.utc)


def _safe_file(root: Path, relative: str) -> Path:
    root = root.resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root):
        raise ValueError("admission input cannot leave the project")
    for node in (path, *path.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError("admission input cannot use symbolic links")
    return path


def _file_hash(path: Path) -> str:
    # Hashing the model bytes does not deserialize or execute a pickle/joblib.
    if path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("admission model input exceeds the bounded file budget")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_snapshot() -> dict:
    names = ["main.py", "pyproject.toml", "src/quantpaper/scheduler.py", "src/quantpaper/sessions.py",
             "src/quantpaper/admission.py", "src/quantpaper/admission_runner.py"]
    names.extend(f"src/quantpaper/marketdata/{name}.py" for name in (
        "__init__", "cli", "storage", "windows", "quality", "study_protocol", "replay_protocol", "replay_book",
    ))
    files = {}
    for name in names:
        raw = _safe_file(PROJECT_ROOT, name).read_bytes()
        files[name] = {"sha256": _sha(raw), "text": raw.decode("utf-8")}
    return {"files": files, "python": platform.python_version(),
            "scope": "explicit offline admission/replay source; no model deserialization or account data"}


def _model_evidence(project_root: Path, now: datetime) -> dict:
    metadata_path = _safe_file(project_root, _MODEL_META)
    model_path = _safe_file(project_root, _MODEL_BINARY)
    if metadata_path.stat().st_size > 1024 * 1024:
        raise ValueError("model metadata exceeds the bounded JSON budget")
    raw = metadata_path.read_bytes()
    metadata = json.loads(raw)
    if not isinstance(metadata, dict) or not isinstance(metadata.get("evaluation"), dict):
        raise ValueError("model metadata lacks a structured evaluation gate")
    evaluation = metadata["evaluation"]
    approved = evaluation.get("approved_for_paper_signals")
    reasons = evaluation.get("rejection_reasons")
    if type(approved) is not bool or not isinstance(reasons, list) or any(type(item) is not str for item in reasons):
        raise ValueError("model metadata approval gate and rejection reasons are malformed")
    if approved and reasons:
        raise ValueError("model metadata approval conflicts with its rejection reasons")
    if metadata.get("feature_contract_version") != "daily_pit_v2":
        raise ValueError("model metadata is not the expected frozen daily_pit_v2 contract")
    created = _aware(metadata.get("created_at"), "model metadata created_at")
    if created > now:
        raise ValueError("model metadata cannot be created in the future")
    binary_hash = _file_hash(model_path)
    if metadata.get("model_sha256") != binary_hash:
        raise ValueError("model metadata hash does not match the frozen model bytes")
    return {
        "verified": True, "approved_for_paper": approved,
        "scope": "frozen_v2_gate_only_not_historical_prediction",
        "model_sha256": binary_hash, "metadata_sha256": _sha(raw),
        "feature_contract_version": "daily_pit_v2",
        "gate_field": "evaluation.approved_for_paper_signals",
        "rejection_reasons": list(reasons), "metadata_created_at": created.isoformat(),
    }


class _Evidence:
    """Read only indexed local evidence and enforce its internal hash links."""

    def __init__(self, directory: Path, project_root: Path):
        self.root = project_root
        self.directory = scoped_directory(directory, project_root)
        self.report = read_report(self.directory, project_root)
        self.index_raw = (self.directory / "completion.json").read_bytes()
        self.index = json.loads(self.index_raw)["artifacts"]
        if len(self.index) > 1000:
            raise ValueError("replay source artifact budget exceeded")
        if _canonical(self.read("report.json")) != _canonical(self.report):
            raise ValueError("replay source changed during verification")
        self.plan = self.read("plan.json")
        self.study_plan = self.read("source-plan.json")
        if self.report.get("plan_sha256") != self.index.get("plan.json"):
            raise ValueError("replay source report is not paired with its plan")
        if (not isinstance(self.index.get("source.json"), str)
                or self.report.get("source_snapshot_sha256") != self.index["source.json"]
                or self.plan.get("source_snapshot_sha256") != self.index["source.json"]
                or self.plan.get("source_study_plan_sha256") != self.index.get("source-plan.json")):
            raise ValueError("replay source hash cross-links are inconsistent")
        if (self.report.get("contract") != _REPLAY_CONTRACT
                or self.report.get("status") not in {"captured", "replayed"}
                or self.report.get("source_unchanged_during_run") is not True
                or self.report.get("input_index_unchanged_during_run") is not True
                or self.report.get("execution_enabled") is not False):
            raise ValueError("admission requires a complete unchanged offline replay source")

    def read(self, name: str):
        if Path(name).name != name or name not in self.index or not name.endswith(".json"):
            raise ValueError("required replay source file is not indexed")
        raw = _safe_file(self.directory, name).read_bytes()
        if _sha(raw) != self.index[name]:
            raise ValueError("replay source file integrity mismatch")
        return json.loads(raw)

    def unchanged(self):
        try:
            return ((self.directory / "completion.json").read_bytes() == self.index_raw
                    and _canonical(read_report(self.directory, self.root)) == _canonical(self.report))
        except (ValueError, OSError, TypeError, KeyError):
            return False

    def identity(self):
        return {"directory": str(self.directory), "contract": _REPLAY_CONTRACT,
                "completion_sha256": _sha(self.index_raw), "plan_sha256": self.index["plan.json"],
                "report_sha256": self.index["report.json"]}


def _verified_rows(evidence: _Evidence, now: datetime) -> tuple[dict, list[dict]]:
    rebuilt = build_replay_plan(evidence.study_plan, now=now)
    for key in ("contract", "research_only", "execution_enabled", "source_study", "windows", "policy", "limits", "summary"):
        if _canonical(evidence.plan.get(key)) != _canonical(rebuilt[key]):
            raise ValueError("source replay plan does not match the frozen study protocol")
    if _aware(evidence.plan.get("created_at"), "source replay created_at") > now:
        raise ValueError("source replay creation time is in the future")
    frozen_rows = evidence.report.get("windows")
    if not isinstance(frozen_rows, list) or len(frozen_rows) != len(rebuilt["windows"]):
        raise ValueError("source replay rows do not cover the exact planned windows")
    rows = []
    for window, frozen in zip(rebuilt["windows"], frozen_rows, strict=True):
        filename = f"{window['symbol'].replace('/', '-')}-{window['session']}-{window['window_name']}-quotes.json"
        payload = evidence.read(filename)
        records = payload.get("records") if isinstance(payload, dict) else None
        if (not isinstance(records, list) or len(records) > 30000
                or payload.get("symbol") != window["symbol"] or payload.get("kind") != "quotes"
                or payload.get("feed") != window["feed"] or payload.get("complete") is not True
                or type(payload.get("pages")) is not int or not 1 <= payload["pages"] <= 3
                or payload.get("content_hash") != _sha(_canonical(records))):
            raise ValueError("raw replay source identity, pagination or content hash mismatch")
        if (_aware(payload.get("start"), "quote start") != _aware(window["start"], "planned start")
                or _aware(payload.get("end"), "quote end") != _aware(window["end"], "planned end")
                or _aware(payload.get("observed_at"), "quote observed_at") > now):
            raise ValueError("raw replay source range or observation time mismatch")
        row = analyze_replay_window(window["symbol"], payload, target=_aware(window["target"], "target"),
                                    max_age_ms=rebuilt["policy"]["max_age_ms"],
                                    latencies_ms=tuple(rebuilt["policy"]["latency_scenarios_ms"]))
        row.update(session=window["session"], window_name=window["window_name"], raw_file=filename)
        if _canonical(row) != _canonical(frozen):
            raise ValueError("source replay row differs from fresh raw-quote reanalysis")
        rows.append(row)
    return rebuilt, rows


def _chain_events(results: list[dict], observed_at: str) -> dict:
    events, previous = [], "0" * 64
    for row in results:
        for logical_event in row["evaluation"]["events"]:
            event = {
                "sequence": len(events) + 1, "previous_sha256": previous,
                "payload": {"intent_id": row["intent"]["id"], "logical_event": logical_event,
                            "run_observed_at": observed_at,
                            "time_scope": "synthetic diagnostic created now; not historical receive or broker time"},
            }
            event["sha256"] = _sha(_canonical(event))
            previous = event["sha256"]
            events.append(event)
    return {"contract": "offline_admission_event_chain_v1", "events": events,
            "count": len(events), "head_sha256": previous,
            "scope": "local tamper-evident logical events, not authenticated broker order events"}


def run_admission(
    run_dir: Path, source_dir: Path, *, project_root: Path = PROJECT_ROOT, now: datetime | None = None,
) -> dict:
    """Evaluate synthetic $5 intents offline; no client or network option exists."""
    directory = validate_run_directory(run_dir, project_root)
    require_paper_gates_closed(project_root / ".env")
    current = _aware(now if now is not None else datetime.now(timezone.utc), "admission now")
    environment_path = _safe_file(project_root, ".env")
    environment_hash = _file_hash(environment_path) if environment_path.exists() else None
    evidence = _Evidence(source_dir, project_root)
    model = _model_evidence(project_root, current)
    snapshot = source_snapshot()
    replay_plan, replay_rows = _verified_rows(evidence, current)
    policy = {
        "paper_only": True, "execution_enabled": False, "max_notional_usd": "25", "max_quote_age_ms": 1000,
        "expected_stock_feed": replay_plan["source_study"]["stock_feed"], "expected_crypto_feed": "crypto_us",
        "expected_source_completion_sha256": evidence.identity()["completion_sha256"],
    }
    plan = {
        "contract": CONTRACT, "created_at": current.isoformat(), "research_only": True,
        "execution_enabled": False, "policy": policy, "synthetic_notional_usd": "5",
        "sides": ["BUY", "SELL"], "latency_scenarios_ms": [0, 250, 1000],
        "input_evidence": evidence.identity(), "model_evidence": model,
        "expected_windows": len(replay_rows), "expected_intents": len(replay_rows) * 6,
        "max_intents": 1080,
        "intent_scope": "synthetic diagnostics using the current frozen v2 gate, not model predictions at historical targets",
        "id_scope": "deterministic audit IDs bound to source completion and intent parameters; not broker IDs or submission idempotency",
        "source_verification": "all raw quote rows freshly reanalyzed and compared; source aggregate summaries are not used for admission",
    }
    require_paper_gates_closed(project_root / ".env")
    directory.mkdir(parents=True, exist_ok=False)
    artifacts = {"source.json": write_json(directory / "source.json", snapshot),
                 "source-plan.json": write_json(directory / "source-plan.json", evidence.plan),
                 "source-study-plan.json": write_json(directory / "source-study-plan.json", evidence.study_plan),
                 "model-evidence.json": write_json(directory / "model-evidence.json", model)}
    plan["source_snapshot_sha256"] = artifacts["source.json"]
    artifacts["plan.json"] = write_json(directory / "plan.json", plan)
    results, ids = [], set()
    for row in replay_rows:
        require_paper_gates_closed(project_root / ".env")
        for latency in (0, 250, 1000):
            for side in ("BUY", "SELL"):
                intent = {"symbol": row["symbol"], "side": side, "notional_decimal": "5",
                          "decision_at": row["target_at"], "latency_ms": latency,
                          "session": row["session"], "window_name": row["window_name"],
                          "source_completion_sha256": evidence.identity()["completion_sha256"]}
                intent["id"] = deterministic_intent_id(intent)
                if intent["id"] in ids:
                    raise ValueError("duplicate synthetic intent ID; no completed admission audit")
                ids.add(intent["id"])
                result = evaluate_intent(intent, row, model, policy=policy)
                if result.get("submitted") is not False or result.get("filled") is not False:
                    raise ValueError("admission kernel violated the offline no-submission contract")
                results.append({"intent": intent, "evaluation": result})
    if (_canonical(source_snapshot()) != _canonical(snapshot) or not evidence.unchanged()
            or _canonical(_model_evidence(project_root, current)) != _canonical(model)
            or (_file_hash(environment_path) if environment_path.exists() else None) != environment_hash):
        raise ValueError("admission inputs changed during the run; no completed audit was published")
    require_paper_gates_closed(project_root / ".env")
    chain = _chain_events(results, current.isoformat())
    artifacts["events.json"] = write_json(directory / "events.json", chain)
    artifacts["intents.json"] = write_json(directory / "intents.json", {"intents": results})
    artifacts["replay-verified.json"] = write_json(directory / "replay-verified.json", {"windows": replay_rows})
    state_counts = dict(sorted(Counter(row["evaluation"]["state"] for row in results).items()))
    reasons = Counter(reason for row in results for reason in set(row["evaluation"]["reasons"]))
    by_symbol = defaultdict(list)
    for row in results:
        by_symbol[row["intent"]["symbol"]].append(row)
    report = {
        "contract": CONTRACT, "status": "audited", "research_only": True, "execution_enabled": False,
        "model_updated": False, "orders_submitted": 0, "fills_simulated": 0, "network_requests": 0,
        "started_at": current.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
        "plan_sha256": artifacts["plan.json"], "source_snapshot_sha256": artifacts["source.json"],
        "inputs_unchanged_during_run": True, "input_evidence": evidence.identity(), "model_evidence": model,
        "counts": {"source_windows": len(replay_rows), "synthetic_intents": len(results),
                   "unique_intent_ids": len(ids), "audit_events": chain["count"], "states": state_counts},
        "rejection_reason_counts": dict(sorted(reasons.items())),
        "by_symbol": {symbol: {
            "synthetic_intents": len(rows),
            "states": dict(sorted(Counter(row["evaluation"]["state"] for row in rows).items())),
            "reason_counts": dict(sorted(Counter(reason for row in rows for reason in set(row["evaluation"]["reasons"])).items())),
        } for symbol, rows in sorted(by_symbol.items())},
        "event_chain_head_sha256": chain["head_sha256"],
        "limitations": [
            "These are synthetic $5 BUY/SELL admission probes, not historical model signals, executed orders or trading recommendations.",
            "The current frozen v2 approval gate is checked now; its presence does not establish approval or predictions at historical targets.",
            "Approval, if any, is simulation-only and never triggers submission, fills, account or position changes.",
            "Quote state is vendor event-time reconstruction; fees, receive latency, halts, impact, queue priority and capacity remain unknown.",
            "Hash links and event chains are tamper-evident local evidence, not signatures or authenticated broker records.",
            "No model deserialization, training, strategy PnL, network access, or existing data/automation mutation occurs.",
        ],
    }
    artifacts["report.json"] = write_json(directory / "report.json", report)
    write_json(directory / "completion.json", {"status": "complete", "artifacts": artifacts})
    return report


def admission_summary(report: dict) -> dict:
    return {key: report[key] for key in (
        "contract", "status", "execution_enabled", "model_updated", "orders_submitted", "fills_simulated",
        "network_requests", "inputs_unchanged_during_run", "model_evidence", "counts",
        "rejection_reason_counts", "by_symbol", "event_chain_head_sha256",
    )}
