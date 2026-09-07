"""Local point-in-time research warehouse backed by DuckDB.

The schema separates when an event happened from when it became available to a
strategy. Every future data connector must preserve that distinction.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import uuid

import duckdb
import pandas as pd


from .source_records import FundamentalRecord, MacroRecord, NewsRecord


SCHEMA_VERSION = 4


DDL = """
CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id VARCHAR NOT NULL,
    symbol VARCHAR NOT NULL,
    asset_class VARCHAR NOT NULL,
    primary_exchange VARCHAR,
    valid_from TIMESTAMPTZ NOT NULL,
    valid_to TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    PRIMARY KEY (instrument_id, valid_from, source)
);

CREATE TABLE IF NOT EXISTS instrument_aliases (
    source VARCHAR NOT NULL,
    source_symbol VARCHAR NOT NULL,
    instrument_id VARCHAR NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (source, source_symbol)
);

CREATE TABLE IF NOT EXISTS market_bars (
    instrument_id VARCHAR NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    interval VARCHAR NOT NULL,
    open DOUBLE NOT NULL,
    high DOUBLE NOT NULL,
    low DOUBLE NOT NULL,
    close DOUBLE NOT NULL,
    volume DOUBLE NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    data_version VARCHAR NOT NULL,
    PRIMARY KEY (instrument_id, event_time, interval, source, data_version)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    record_id VARCHAR PRIMARY KEY,
    instrument_id VARCHAR NOT NULL,
    metric VARCHAR NOT NULL,
    period_start DATE,
    period_end DATE NOT NULL,
    value DOUBLE,
    unit VARCHAR,
    form VARCHAR,
    accession_number VARCHAR,
    filed_at TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    fiscal_year INTEGER,
    fiscal_period VARCHAR,
    frame VARCHAR,
    availability_basis VARCHAR NOT NULL,
    CHECK (period_start IS NULL OR period_start <= period_end),
    CHECK (available_at >= filed_at)
);

CREATE TABLE IF NOT EXISTS macro_observations (
    series_id VARCHAR NOT NULL,
    observation_date DATE NOT NULL,
    value DOUBLE,
    vintage_date DATE NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    realtime_end DATE NOT NULL,
    availability_basis VARCHAR NOT NULL,
    PRIMARY KEY (series_id, observation_date, vintage_date, source),
    CHECK (realtime_end >= vintage_date)
);

CREATE TABLE IF NOT EXISTS news_events (
    event_id VARCHAR PRIMARY KEY,
    published_at TIMESTAMPTZ NOT NULL,
    first_seen_at TIMESTAMPTZ NOT NULL,
    available_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    title_hash VARCHAR NOT NULL,
    raw_uri VARCHAR,
    entity_ids VARCHAR,
    quality_flags VARCHAR
);

CREATE TABLE IF NOT EXISTS index_membership (
    index_id VARCHAR NOT NULL,
    instrument_id VARCHAR NOT NULL,
    effective_from TIMESTAMPTZ NOT NULL,
    effective_to TIMESTAMPTZ,
    announced_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    PRIMARY KEY (index_id, instrument_id, effective_from, source)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    instrument_id VARCHAR NOT NULL,
    action_type VARCHAR NOT NULL,
    ex_date DATE NOT NULL,
    value DOUBLE,
    currency VARCHAR,
    announced_at TIMESTAMPTZ,
    available_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp,
    source VARCHAR NOT NULL,
    PRIMARY KEY (instrument_id, action_type, ex_date, source)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id VARCHAR PRIMARY KEY,
    source VARCHAR NOT NULL,
    request_json VARCHAR NOT NULL,
    record_count BIGINT NOT NULL,
    content_hash VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS shadow_signals (
    signal_id VARCHAR PRIMARY KEY,
    generated_at TIMESTAMPTZ NOT NULL,
    generation_date DATE NOT NULL,
    symbol VARCHAR NOT NULL,
    asset_class VARCHAR NOT NULL,
    feature_as_of DATE NOT NULL,
    probability_up DOUBLE NOT NULL,
    direction SMALLINT NOT NULL,
    model_hash VARCHAR NOT NULL,
    model_trained_through DATE NOT NULL,
    model_approved BOOLEAN NOT NULL,
    feature_hash VARCHAR NOT NULL,
    cost_bps DOUBLE NOT NULL,
    status VARCHAR NOT NULL,
    target_session DATE,
    realized_return DOUBLE,
    strategy_return DOUBLE,
    evaluated_at TIMESTAMPTZ,
    UNIQUE (model_hash, symbol, generation_date, feature_as_of)
);
"""


@dataclass(frozen=True, slots=True)
class WarehouseStats:
    instruments: int
    instrument_aliases: int
    market_bars: int
    fundamentals: int
    macro_observations: int
    news_events: int
    index_membership: int
    corporate_actions: int
    ingestion_runs: int
    shadow_signals: int


class PointInTimeWarehouse:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(path))
        self.connection.execute("SET TimeZone='UTC'")
        self._initialized = False
        self._transaction_depth = 0

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        self.connection.execute("BEGIN TRANSACTION")
        try:
            for table, required_column in (("fundamentals", "record_id"),
                                           ("macro_observations", "availability_basis")):
                columns = self.connection.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
                    [table],
                ).fetchall()
                if columns and required_column not in {row[0] for row in columns}:
                    # Old facts lost period/unit context; old macro payload/time
                    # semantics cannot be certified. Preserve every row for audit
                    # and require fresh ingestion into the corrected schema.
                    self.connection.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy_v3")
            self.connection.execute(DDL)
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_versions(version) VALUES (?)", [SCHEMA_VERSION]
            )
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        self._initialized = True

    @contextmanager
    def transaction(self):
        """Make each source batch, its aliases, and its audit row atomic."""
        self.initialize()
        outer = self._transaction_depth == 0
        if outer:
            self.connection.execute("BEGIN TRANSACTION")
        self._transaction_depth += 1
        try:
            yield
        except Exception:
            if outer:
                self.connection.execute("ROLLBACK")
            raise
        else:
            if outer:
                self.connection.execute("COMMIT")
        finally:
            self._transaction_depth -= 1

    def ingest_yahoo_csv(self, symbol: str, path: Path, asset_class: str) -> int:
        self.initialize()
        frame = pd.read_csv(path, parse_dates=["timestamp"])
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        observed_at = datetime.now(timezone.utc)
        if frame["timestamp"].isna().any() or frame["timestamp"].duplicated().any():
            raise ValueError("Yahoo bars contain missing or duplicate timestamps")
        prices = frame[["open", "high", "low", "close", "volume"]]
        if not prices.map(lambda value: isinstance(value, (int, float)) and math.isfinite(value)).all().all():
            raise ValueError("Yahoo bars contain missing or nonfinite values")
        if (prices[["open", "high", "low", "close"]] <= 0).any().any() or (prices["volume"] < 0).any():
            raise ValueError("Yahoo bars contain invalid prices or volume")
        if ((prices["high"] < prices[["open", "close", "low"]].max(axis=1)) |
                (prices["low"] > prices[["open", "close", "high"]].min(axis=1))).any():
            raise ValueError("Yahoo bars violate OHLC bounds")
        frame = frame[frame["timestamp"] + timedelta(days=1) <= observed_at].copy()
        if frame.empty:
            return 0
        instrument_id = f"YF:{symbol.upper()}"
        first_time = frame["timestamp"].min()

        normalized = pd.DataFrame(
            {
                "instrument_id": instrument_id,
                "event_time": frame["timestamp"],
                "interval": "1d",
                "open": frame["open"],
                "high": frame["high"],
                "low": frame["low"],
                "close": frame["close"],
                "volume": frame["volume"],
                # Yahoo's adjusted history is a snapshot observed now; splits,
                # dividends and provider corrections may alter old prices.
                "available_at": observed_at,
                "source": "yahoo",
                "data_version": [
                    "snapshot-v2:" + observed_at.isoformat() + ":"
                    + hashlib.sha256(json.dumps(tuple(row)).encode()).hexdigest()
                    for row in frame[["open", "high", "low", "close", "volume"]].itertuples(index=False, name=None)
                ],
            }
        )
        self.connection.register("incoming_bars", normalized)
        try:
            with self.transaction():
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO instruments
                      (instrument_id, symbol, asset_class, primary_exchange, valid_from,
                       available_at, source)
                    VALUES (?, ?, ?, NULL, ?, ?, 'yahoo')
                    """,
                    [instrument_id, symbol.upper(), asset_class, first_time, observed_at],
                )
                before = self.connection.execute("SELECT count(*) FROM market_bars").fetchone()[0]
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO market_bars
                      (instrument_id, event_time, interval, open, high, low, close, volume,
                       available_at, source, data_version)
                    WITH latest AS (
                        SELECT * FROM market_bars
                        WHERE source = 'yahoo' AND data_version LIKE 'snapshot-v2:%'
                        QUALIFY row_number() OVER (
                            PARTITION BY instrument_id, event_time, interval, source
                            ORDER BY available_at DESC, ingested_at DESC, data_version DESC
                        ) = 1
                    )
                    SELECT i.instrument_id, i.event_time, i.interval, i.open, i.high, i.low,
                           i.close, i.volume, i.available_at, i.source, i.data_version
                    FROM incoming_bars i LEFT JOIN latest b
                      ON i.instrument_id = b.instrument_id AND i.event_time = b.event_time
                      AND i.interval = b.interval AND i.source = b.source
                    WHERE b.instrument_id IS NULL OR i.open IS DISTINCT FROM b.open
                      OR i.high IS DISTINCT FROM b.high OR i.low IS DISTINCT FROM b.low
                      OR i.close IS DISTINCT FROM b.close OR i.volume IS DISTINCT FROM b.volume
                    """
                )
                after = self.connection.execute("SELECT count(*) FROM market_bars").fetchone()[0]
        finally:
            self.connection.unregister("incoming_bars")
        return int(after - before)

    def upsert_instrument_alias(
        self, source: str, source_symbol: str, instrument_id: str, available_at: str
    ) -> None:
        self.initialize()
        self.connection.execute(
            """
            INSERT OR REPLACE INTO instrument_aliases
              (source, source_symbol, instrument_id, available_at)
            VALUES (?, ?, ?, ?::TIMESTAMPTZ)
            """,
            [source, source_symbol, instrument_id, available_at],
        )

    def ingest_fundamentals(self, records: list[FundamentalRecord]) -> int:
        self.initialize()
        if not records:
            return 0
        with self.transaction():
            before = self.connection.execute("SELECT count(*) FROM fundamentals").fetchone()[0]
            for record in records:
                if record.value is not None and not math.isfinite(record.value):
                    raise ValueError("Fundamental value must be finite")
                prior = self.connection.execute(
                    "SELECT value FROM fundamentals WHERE record_id = ?", [record.record_id]
                ).fetchone()
                if prior is not None and prior[0] != record.value:
                    raise ValueError("A fundamental fact changed within one accession; revalidation is required")
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO fundamentals
                      (record_id, instrument_id, metric, period_start, period_end, value, unit, form,
                       accession_number, filed_at, available_at, source, fiscal_year, fiscal_period,
                       frame, availability_basis)
                    VALUES (?, ?, ?, ?::DATE, ?::DATE, ?, ?, ?, ?, ?::TIMESTAMPTZ,
                            ?::TIMESTAMPTZ, ?, ?, ?, ?, ?)
                    """, record.as_tuple(),
                )
            after = self.connection.execute("SELECT count(*) FROM fundamentals").fetchone()[0]
        return int(after - before)

    def ingest_macro(self, records: list[MacroRecord]) -> int:
        self.initialize()
        if not records:
            return 0
        with self.transaction():
            before = self.connection.execute("SELECT count(*) FROM macro_observations").fetchone()[0]
            for record in records:
                if record.value is not None and not math.isfinite(record.value):
                    raise ValueError("Macro value must be finite")
                prior = self.connection.execute(
                    """SELECT value FROM macro_observations
                       WHERE series_id = ? AND observation_date = ?::DATE
                       AND vintage_date = ?::DATE AND source = ?""",
                    [record.series_id, record.observation_date, record.vintage_date, record.source],
                ).fetchone()
                if prior is not None and prior[0] != record.value:
                    raise ValueError("A macro value changed within one vintage; revalidation is required")
                self.connection.execute(
                    """
                    INSERT INTO macro_observations
                      (series_id, observation_date, value, vintage_date, available_at, source,
                       realtime_end, availability_basis)
                    VALUES (?, ?::DATE, ?, ?::DATE, ?::TIMESTAMPTZ, ?, ?::DATE, ?)
                    ON CONFLICT (series_id, observation_date, vintage_date, source)
                    DO UPDATE SET realtime_end = excluded.realtime_end
                    """, record.as_tuple(),
                )
            after = self.connection.execute("SELECT count(*) FROM macro_observations").fetchone()[0]
        return int(after - before)

    def ingest_news(self, records: list[NewsRecord]) -> int:
        self.initialize()
        if not records:
            return 0
        with self.transaction():
            before = self.connection.execute("SELECT count(*) FROM news_events").fetchone()[0]
            for record in records:
                available = pd.Timestamp(record.available_at)
                if available < max(pd.Timestamp(record.published_at), pd.Timestamp(record.first_seen_at)):
                    raise ValueError("News availability precedes publication or first observation")
            self.connection.executemany(
                """
                INSERT OR IGNORE INTO news_events
                  (event_id, published_at, first_seen_at, available_at, source, title_hash,
                   raw_uri, entity_ids, quality_flags)
                VALUES (?, ?::TIMESTAMPTZ, ?::TIMESTAMPTZ, ?::TIMESTAMPTZ, ?, ?, ?, ?, ?)
                """,
                [record.as_tuple() for record in records],
            )
            after = self.connection.execute("SELECT count(*) FROM news_events").fetchone()[0]
        return int(after - before)

    def record_ingestion(
        self, source: str, request: dict[str, object], record_count: int, content_hash: str
    ) -> str:
        self.initialize()
        run_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO ingestion_runs
              (run_id, source, request_json, record_count, content_hash, status)
            VALUES (?, ?, ?, ?, ?, 'completed')
            """,
            [run_id, source, json.dumps(request, sort_keys=True), record_count, content_hash],
        )
        return run_id

    def bars_as_of(self, instrument_id: str, as_of: str) -> pd.DataFrame:
        """Return one observed Yahoo snapshot per day, excluding legacy backfills."""
        self.initialize()
        return self.connection.execute(
            """
            SELECT event_time, open, high, low, close, volume, available_at
            FROM market_bars
            WHERE instrument_id = ? AND available_at <= ?::TIMESTAMPTZ
              AND source = 'yahoo' AND interval = '1d'
              AND data_version LIKE 'snapshot-v2:%'
            QUALIFY row_number() OVER (
                PARTITION BY event_time ORDER BY available_at DESC, ingested_at DESC, data_version DESC
            ) = 1
            ORDER BY event_time
            """,
            [instrument_id, as_of],
        ).fetchdf()

    def fundamentals_as_of(self, instrument_id: str, as_of: str) -> pd.DataFrame:
        """Latest available filing for each period/unit, retaining quarter vs YTD."""
        self.initialize()
        return self.connection.execute(
            """
            SELECT * FROM fundamentals
            WHERE instrument_id = ? AND available_at <= ?::TIMESTAMPTZ
            QUALIFY row_number() OVER (
                PARTITION BY metric, period_start, period_end, unit, source
                ORDER BY filed_at DESC, accession_number DESC
            ) = 1
            ORDER BY period_end, period_start, metric, unit
            """, [instrument_id, as_of],
        ).fetchdf()

    def macro_as_of(self, series_id: str, as_of: str) -> pd.DataFrame:
        """Select by release availability, never by observation date alone.

        Do not filter by the latest fetched realtime_end: that end can itself
        reveal a later revision, and our date-only availability uses a delay.
        """
        self.initialize()
        return self.connection.execute(
            """
            SELECT * FROM macro_observations
            WHERE series_id = ? AND available_at <= ?::TIMESTAMPTZ
            QUALIFY row_number() OVER (
                PARTITION BY observation_date, source ORDER BY vintage_date DESC
            ) = 1
            ORDER BY observation_date
            """, [series_id.upper(), as_of],
        ).fetchdf()

    def quality_report(self) -> dict[str, object]:
        self.initialize()
        coverage: dict[str, list[dict[str, object]]] = {}
        specifications = {
            "fundamentals": ("source", "available_at"),
            "macro_observations": ("source", "available_at"),
            "news_events": ("source", "available_at"),
            "market_bars": ("source", "available_at"),
        }
        for table, (source_column, time_column) in specifications.items():
            rows = self.connection.execute(
                f"""
                SELECT {source_column}, count(*), min({time_column}), max({time_column})
                FROM {table}
                GROUP BY {source_column}
                ORDER BY {source_column}
                """
            ).fetchall()
            coverage[table] = [
                {
                    "source": row[0],
                    "rows": int(row[1]),
                    "first_available": str(row[2]),
                    "last_available": str(row[3]),
                }
                for row in rows
            ]
        violations = {
            "fundamental_available_before_filed": int(
                self.connection.execute(
                    "SELECT count(*) FROM fundamentals WHERE available_at < filed_at"
                ).fetchone()[0]
            ),
            "macro_available_before_vintage": int(
                self.connection.execute(
                    "SELECT count(*) FROM macro_observations WHERE available_at::DATE < vintage_date"
                ).fetchone()[0]
            ),
            "news_available_before_published": int(
                self.connection.execute(
                    "SELECT count(*) FROM news_events WHERE available_at < published_at"
                ).fetchone()[0]
            ),
            "news_available_before_first_seen": int(
                self.connection.execute(
                    "SELECT count(*) FROM news_events WHERE available_at < first_seen_at"
                ).fetchone()[0]
            ),
        }
        existing = {row[0] for row in self.connection.execute("SHOW TABLES").fetchall()}
        excluded = {
            table: int(self.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("fundamentals_legacy_v3", "macro_observations_legacy_v3")
            if table in existing
        }
        excluded["market_bars_adjusted_v1"] = int(self.connection.execute(
            "SELECT count(*) FROM market_bars WHERE data_version = 'adjusted-v1'"
        ).fetchone()[0])
        return {
            "schema_version": SCHEMA_VERSION, "coverage": coverage, "violations": violations,
            "excluded_from_as_of": excluded,
            "limitations": [
                "Legacy v3 facts and macro rows are preserved but require reingestion before as-of use.",
                "Adjusted Yahoo snapshots are usable only from first observation; historical adjusted-v1 rows are excluded.",
                "SEC filing dates and FRED vintages use conservative local end-of-day availability, not verified intraday release times.",
                "Historical SEC/ALFRED records reconstruct provider history; ingestion timestamps separately record local observation.",
                "Instrument aliases are current mappings, not historical ticker membership or a survivorship-free universe.",
                "News is first-seen metadata only; later article revisions are not yet a versioned text dataset.",
            ],
        }

    def stats(self) -> WarehouseStats:
        self.initialize()
        names = (
            "instruments",
            "instrument_aliases",
            "market_bars",
            "fundamentals",
            "macro_observations",
            "news_events",
            "index_membership",
            "corporate_actions",
            "ingestion_runs",
            "shadow_signals",
        )
        counts = {
            name: int(self.connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
            for name in names
        }
        return WarehouseStats(**counts)
