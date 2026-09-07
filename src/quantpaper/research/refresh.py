"""Bounded research-data refresh, isolated from models and order interfaces."""

from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import platform
import re

from .evidence import canonical_bytes, digest
from .evidence_cli import _create_bytes, _read_bytes


SOURCES = ("yahoo", "sec", "fred")


@dataclass(frozen=True, slots=True, repr=False)
class RefreshConfiguration:
    sec_user_agent: str = ""
    fred_api_key: str = ""

    def readiness(self) -> dict[str, str]:
        agent = self.sec_user_agent.strip()
        sec_ok = (re.search(r"[^\s@]+@[^\s@]+\.[^\s@]+", agent) is not None
                  and not re.search(r"example\.(com|org|net)|your_email|[\r\n]", agent, re.I))
        fred_ok = re.fullmatch(r"[a-z0-9]{32}", self.fred_api_key.strip()) is not None
        return {"yahoo": "READY", "sec": "READY" if sec_ok else "INVALID_CONFIGURATION" if agent else "MISSING_CONFIGURATION",
                "fred": "READY" if fred_ok else "INVALID_CONFIGURATION" if self.fred_api_key.strip() else "MISSING_CONFIGURATION"}


def load_configuration(project_root: Path) -> RefreshConfiguration:
    """Read only these two settings without mutating the process environment."""
    from dotenv import dotenv_values

    path = project_root.resolve() / ".env"
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size > 100_000)):
        raise ValueError("local configuration must be a bounded regular file")
    diagnostics = io.StringIO()
    with redirect_stderr(diagnostics):
        values = dotenv_values(path, interpolate=False) if path.exists() else {}
    if diagnostics.getvalue():
        raise ValueError("local configuration could not be parsed")
    return RefreshConfiguration(*(os.environ.get(name, values.get(name) or "").strip()
                                  for name in ("SEC_USER_AGENT", "FRED_API_KEY")))


def scoped_refresh_directory(run_dir: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    raw = run_dir if run_dir.is_absolute() else root / run_dir
    base = root / "artifacts" / "research-refresh"
    if ".." in raw.parts or raw == base or not raw.is_relative_to(base):
        raise ValueError("refresh run must be a new child of artifacts/research-refresh")
    for node in (raw, *raw.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError("refresh paths must not contain symlinks")
    if not raw.resolve().is_relative_to(base.resolve()):
        raise ValueError("refresh path is outside its isolated directory")
    return raw


def scoped_database(database: Path, project_root: Path) -> Path:
    root = project_root.resolve()
    raw = database if database.is_absolute() else root / database
    base = root / "data"
    if ".." in raw.parts or not raw.is_relative_to(base) or raw.suffix != ".duckdb":
        raise ValueError("refresh database must be a research DuckDB file under project data")
    for node in (raw, *raw.parents):
        if node == root:
            break
        if node.is_symlink():
            raise ValueError("refresh database paths must not contain symlinks")
    if not raw.is_file() or not raw.resolve().is_relative_to(base.resolve()):
        raise ValueError("refresh database must already exist inside project data")
    return raw


def _request(symbol: str, asset_class: str, period: str, sources: tuple[str, ...], series: tuple[str, ...]) -> dict:
    if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,14}", symbol) or ".." in symbol:
        raise ValueError("provide one explicit uppercase Yahoo symbol")
    if asset_class not in {"equity", "crypto"} or period not in {"1y", "2y"}:
        raise ValueError("unsupported asset class or research window")
    if not isinstance(sources, (tuple, list)) or not sources or any(not isinstance(source, str) for source in sources) or len(set(sources)) != len(sources) or any(source not in SOURCES for source in sources):
        raise ValueError("select unique explicit research sources")
    if not isinstance(series, (tuple, list)) or any(not isinstance(item, str) for item in series) or len(series) > 4 or len(set(series)) != len(series) or any(not re.fullmatch(r"[A-Z0-9_]{1,64}", item) for item in series):
        raise ValueError("select at most four unique explicit macro series")
    if "fred" in sources and not series:
        raise ValueError("FRED refresh requires explicit series")
    return {"symbol": symbol, "asset_class": asset_class, "period": period,
            "sources": list(sources), "series": list(series)}


def _code_fingerprint() -> dict:
    base = Path(__file__).resolve().parents[1]
    names = ("research/refresh.py", "research/refresh_cli.py", "sources/yahoo_snapshot.py",
             "sources/sec.py", "sources/fred.py", "sources/http.py", "warehouse.py")
    files = {name: hashlib.sha256((base / name).read_bytes()).hexdigest() for name in names}
    return {"files": files, "sha256": digest(files)}


def _require_existing_schema(connection) -> None:
    from .evidence_store import _require_schema

    _require_schema(connection)
    existing = dict(connection.execute(
        "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall())
    required = {"instruments", "instrument_aliases", "market_bars", "fundamentals", "macro_observations",
                "news_events", "index_membership", "corporate_actions", "ingestion_runs", "shadow_signals"}
    if any(existing.get(name) != "BASE TABLE" for name in required):
        raise ValueError("refresh requires all existing warehouse-v4 tables; no migration is allowed")
    columns = {row[0] for row in connection.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' AND table_name = 'ingestion_runs'"
    ).fetchall()}
    if not {"run_id", "source", "request_json", "record_count", "content_hash", "status", "completed_at"} <= columns:
        raise ValueError("existing source audit schema is incomplete")


def _existing_writer(database: Path):
    from ..warehouse import PointInTimeWarehouse

    class ExistingResearchWarehouse(PointInTimeWarehouse):
        def initialize(self) -> None:
            # Do not call the normal initializer: it can create/migrate tables
            # and commits that DDL outside the later source batch transaction.
            if not self._initialized:
                _require_existing_schema(self.connection)
                self._initialized = True

    return ExistingResearchWarehouse(database)


def run_refresh(database: Path, run_dir: Path, *, project_root: Path, symbol: str = "JPM",
                asset_class: str = "equity", period: str = "2y",
                sources: tuple[str, ...] = SOURCES,
                series: tuple[str, ...] = ("DFF", "DGS10", "CPIAUCSL", "UNRATE"),
                configuration: RefreshConfiguration | None = None,
                yahoo_factory=None, sec_factory=None, fred_factory=None) -> dict:
    """Fetch before opening a writer; commit each valid source and audit together.

    Injected factories are for offline tests. Failed or unconfigured sources are
    never silently replaced. A completed report may truthfully be PARTIAL.
    """
    request = _request(symbol, asset_class, period, sources, series)
    directory = scoped_refresh_directory(run_dir, project_root)
    if directory.exists():
        raise ValueError("refresh run already exists; preserve it and use a new name")
    database = scoped_database(database, project_root)
    # Verify current provenance schema before any provider call or database write.
    import duckdb
    connection = duckdb.connect(str(database), read_only=True, config={"enable_external_access": False,
                                 "autoinstall_known_extensions": False, "autoload_known_extensions": False})
    try:
        _require_existing_schema(connection)
    finally:
        connection.close()
    config = configuration if configuration is not None else load_configuration(project_root)
    readiness = config.readiness()
    code = _code_fingerprint()
    directory.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc)
    lookback_start = (started.date() - timedelta(days=365 if period == "1y" else 730)).isoformat()
    cutoff_date = started.date().isoformat()
    artifacts = {}
    outcomes = []

    def publish(name: str, value: dict) -> None:
        payload = canonical_bytes(value) + b"\n"
        _create_bytes(directory / name, payload)
        artifacts[name] = hashlib.sha256(payload).hexdigest()

    publish("request.json", {"request": request, "started_at": started.isoformat(),
                             "macro_observation_start": lookback_start, "macro_realtime_start": lookback_start,
                             "macro_realtime_end": cutoff_date,
                             "runtime": {"python": platform.python_version(),
                                         **{name: version(name) for name in ("duckdb", "pandas", "yfinance", "certifi")}},
                             "builder": code, "research_only": True, "execution_enabled": False})

    for source in sources:
        identifiers = series if source == "fred" else (symbol,)
        for identifier in identifiers:
            outcome = {"source": source, "identifier": identifier, "fetched": 0, "inserted": 0}
            if readiness[source] != "READY":
                outcome.update(status=readiness[source], reason="Configure the requested source locally; no request was sent.")
            elif source == "sec" and asset_class != "equity":
                outcome.update(status="UNSUPPORTED", reason="SEC company facts require an explicit equity issuer.")
            else:
                stage = "fetch"
                try:
                    if source == "yahoo":
                        if yahoo_factory is None:
                            from ..sources.yahoo_snapshot import YahooSnapshotClient
                            yahoo_factory = YahooSnapshotClient
                        snapshot = yahoo_factory().fetch(symbol, period=period)
                        payload = snapshot.frame.to_csv(index_label="timestamp").encode()
                        csv_path = directory / "yahoo.csv"
                        _create_bytes(csv_path, payload)
                        artifacts["yahoo.csv"] = hashlib.sha256(payload).hexdigest()
                        count = len(snapshot.frame)
                        safe_request = {**snapshot.request, "response_observed_at": snapshot.observed_at,
                                        "excluded_incomplete_rows": snapshot.excluded_incomplete_rows,
                                        "normalized_csv_sha256": artifacts["yahoo.csv"]}
                        batch_hash = snapshot.content_hash
                        batch_source = "yahoo"
                    elif source == "sec":
                        if sec_factory is None:
                            from ..sources.sec import SECCompanyFactsClient
                            sec_factory = SECCompanyFactsClient
                        batch = sec_factory(config.sec_user_agent).fetch(symbol)
                        count, safe_request, batch_hash, batch_source = len(batch.records), batch.request, batch.content_hash, batch.source
                    else:
                        if fred_factory is None:
                            from ..sources.fred import FREDVintageClient
                            fred_factory = FREDVintageClient
                        batch = fred_factory(config.fred_api_key).fetch(identifier, observation_start=lookback_start,
                                                                      realtime_start=lookback_start, realtime_end=cutoff_date)
                        count, safe_request, batch_hash, batch_source = len(batch.records), batch.request, batch.content_hash, batch.source
                    outcome["fetched"] = count
                    if not count:
                        outcome.update(status="EMPTY", request=safe_request, content_hash=batch_hash,
                                       reason="Source returned no eligible records; no database write occurred.")
                    else:
                        stage = "ingestion"
                        # Recheck the path/schema after the potentially slow fetch.
                        database = scoped_database(database, project_root)
                        warehouse = _existing_writer(database)
                        try:
                            with warehouse.transaction():
                                if source == "yahoo":
                                    if hashlib.sha256(csv_path.read_bytes()).hexdigest() != artifacts["yahoo.csv"]:
                                        raise ValueError("normalized source changed before ingestion")
                                    inserted = warehouse.ingest_yahoo_csv(symbol, csv_path, asset_class)
                                elif source == "sec":
                                    inserted = warehouse.ingest_fundamentals(batch.records)
                                else:
                                    inserted = warehouse.ingest_macro(batch.records)
                                ingestion_id = warehouse.record_ingestion(batch_source, safe_request, count, batch_hash)
                        finally:
                            warehouse.close()
                        outcome.update(status="UPDATED" if inserted else "NO_NEW_ROWS", inserted=inserted,
                                       content_hash=batch_hash, ingestion_run_id=ingestion_id,
                                       request=safe_request)
                except Exception:
                    # Never include provider exceptions, authenticated URLs, contact details, or keys.
                    outcome.update(status="FAILED", reason=f"Source {stage} failed; no fallback was used.")
            outcomes.append(outcome)
            # Incremental receipts survive interruption; never overwrite a previous source outcome.
            publish(f"receipt-{len(outcomes):02d}.json", outcome)

    if code != _code_fingerprint():
        raise ValueError("refresh builder changed; run evidence is incomplete and must not be resumed")
    report = {"schema_version": 1, "request": request, "started_at": started.isoformat(),
              "completed_at": datetime.now(timezone.utc).isoformat(),
              "status": "COMPLETE" if all(row["status"] in {"UPDATED", "NO_NEW_ROWS"} for row in outcomes) else "PARTIAL",
              "research_only": True, "execution_enabled": False, "model_changed": False,
              "outcomes": outcomes,
              "limitations": ["Source commits are independent; a later failure does not undo prior successful commits.",
                              "NO_NEW_ROWS means no inserted rows, not an unchanged database; FRED interval metadata may update.",
                              "Yahoo adjusted rows are newly observed snapshots, not proof of historical availability.",
                              "SEC/FRED history uses conservative provider-date availability and separate local ingestion.",
                              "This refresh changes research data only; it is not model training, an order, or a prediction.",
                              "Interrupted runs may have committed audited batches; inspect receipts and ingestion_runs before retrying."]}
    report["report_hash"] = digest(report)
    publish("report.json", report)
    _create_bytes(directory / "completion.json", canonical_bytes({"schema_version": 1,
                  "status": "complete", "report_hash": report["report_hash"], "artifacts": artifacts}) + b"\n")
    return report


def read_refresh(run_dir: Path, project_root: Path) -> dict:
    directory = scoped_refresh_directory(run_dir, project_root)
    completion = json.loads(_read_bytes(directory / "completion.json"))
    if not isinstance(completion, dict) or completion.get("status") != "complete" or type(completion.get("schema_version")) is not int or completion["schema_version"] != 1:
        raise ValueError("refresh completion is invalid")
    artifacts = completion.get("artifacts")
    if not isinstance(artifacts, dict) or not {"request.json", "report.json"} <= set(artifacts) or any(
        name not in {"request.json", "report.json", "yahoo.csv"} and not re.fullmatch(r"receipt-0[1-6]\.json", name)
        for name in artifacts
    ):
        raise ValueError("refresh artifact list is invalid")
    for name, expected in artifacts.items():
        if hashlib.sha256(_read_bytes(directory / name)).hexdigest() != expected:
            raise ValueError("refresh artifact hash mismatch")
    report = json.loads(_read_bytes(directory / "report.json"))
    if not isinstance(report, dict) or report.get("report_hash") != completion.get("report_hash") or report.get("report_hash") != digest({k: v for k, v in report.items() if k != "report_hash"}):
        raise ValueError("refresh report hash mismatch")
    if report.get("research_only") is not True or report.get("execution_enabled") is not False or report.get("model_changed") is not False:
        raise ValueError("refresh safety flags are invalid")
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("unsupported refresh report schema")
    request = report["request"]
    if not isinstance(request, dict) or _request(**request) != request:
        raise ValueError("refresh request is invalid")
    recorded_request = json.loads(_read_bytes(directory / "request.json"))
    if not isinstance(recorded_request, dict) or recorded_request.get("request") != request or recorded_request.get("started_at") != report["started_at"]:
        raise ValueError("refresh request artifact does not match the report")
    if recorded_request.get("research_only") is not True or recorded_request.get("execution_enabled") is not False:
        raise ValueError("refresh request safety flags are invalid")
    builder = recorded_request.get("builder")
    if not isinstance(builder, dict) or not isinstance(builder.get("files"), dict) or builder.get("sha256") != digest(builder["files"]):
        raise ValueError("refresh builder fingerprint is invalid")
    outcomes = report["outcomes"]
    expected_pairs = [(source, identifier) for source in request["sources"]
                      for identifier in (request["series"] if source == "fred" else [request["symbol"]])]
    if not isinstance(outcomes, list) or len(outcomes) != len(expected_pairs):
        raise ValueError("refresh outcome list is invalid")
    expected_receipts = {f"receipt-{index:02d}.json" for index in range(1, len(outcomes) + 1)}
    if {name for name in artifacts if name.startswith("receipt-")} != expected_receipts:
        raise ValueError("refresh receipt list is incomplete")
    successful = {"UPDATED", "NO_NEW_ROWS"}
    statuses = successful | {"MISSING_CONFIGURATION", "INVALID_CONFIGURATION", "UNSUPPORTED", "EMPTY", "FAILED"}
    for index, (outcome, pair) in enumerate(zip(outcomes, expected_pairs), 1):
        if not isinstance(outcome, dict) or (outcome.get("source"), outcome.get("identifier")) != pair or outcome.get("status") not in statuses:
            raise ValueError("refresh outcome identity or status is invalid")
        if any(type(outcome.get(key)) is not int or outcome[key] < 0 for key in ("fetched", "inserted")) or outcome["inserted"] > outcome["fetched"]:
            raise ValueError("refresh outcome counts are invalid")
        if outcome["status"] == "UPDATED" and outcome["inserted"] == 0:
            raise ValueError("updated source must have inserted records")
        if outcome["status"] == "NO_NEW_ROWS" and (outcome["inserted"] != 0 or outcome["fetched"] == 0):
            raise ValueError("unchanged source must have fetched but not inserted records")
        if outcome["status"] not in successful and outcome["inserted"] != 0:
            raise ValueError("unsuccessful source cannot claim inserted records")
        if json.loads(_read_bytes(directory / f"receipt-{index:02d}.json")) != outcome:
            raise ValueError("refresh receipt does not match the report")
    expected_status = "COMPLETE" if all(row["status"] in successful for row in outcomes) else "PARTIAL"
    if report.get("status") != expected_status:
        raise ValueError("refresh report status is inconsistent")
    return report
