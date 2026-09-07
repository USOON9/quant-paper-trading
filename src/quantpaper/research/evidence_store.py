"""Read-only, single-snapshot access to an existing schema-v4 research store.

No warehouse initializer, migration, alias inference, connector, or model is
called. The caller decides which explicitly named instruments/entities to read.
One extra row per section is returned so the packet builder can report limits.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re

import duckdb


_FIELDS = {
    "identity": ("instrument_id", "symbol", "asset_class", "primary_exchange", "valid_from", "valid_to", "available_at", "ingested_at", "source"),
    "market": ("instrument_id", "event_time", "interval", "open", "high", "low", "close", "volume", "available_at", "ingested_at", "source", "data_version"),
    "fundamentals": ("record_id", "instrument_id", "metric", "period_start", "period_end", "value", "unit", "form", "accession_number", "filed_at", "available_at", "ingested_at", "source", "fiscal_year", "fiscal_period", "frame", "availability_basis"),
    "macro": ("series_id", "observation_date", "value", "vintage_date", "available_at", "ingested_at", "source", "availability_basis"),
    "news": ("event_id", "published_at", "first_seen_at", "available_at", "ingested_at", "source", "title_hash", "entity_ids", "quality_flags"),
}
_TABLES = {"identity": "instruments", "market": "market_bars", "fundamentals": "fundamentals", "macro": "macro_observations", "news": "news_events"}
_AS_OF = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)\Z", re.ASCII)


def _cutoff(as_of):
    if not isinstance(as_of, str) or not _AS_OF.fullmatch(as_of):
        raise ValueError("Evidence cutoff must be a UTC ISO timestamp with at most microsecond precision")
    try:
        value = datetime.fromisoformat(as_of)
    except ValueError:
        raise ValueError("Evidence cutoff is invalid") from None
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("Evidence cutoff must be UTC-aware")
    return value.astimezone(timezone.utc)


def _normalize(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("Naive stored timestamp")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Nonfinite stored value")
        return value
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _normalize(item) for key, item in value.items()}
    raise ValueError("Unsupported stored value type")


def _json(value):
    if not isinstance(value, str):
        raise ValueError("Missing structured news metadata")
    return _normalize(json.loads(value))


def _columns(section, alias):
    return ", ".join(f'{alias}."{field}"' for field in _FIELDS[section])


def _rows(connection, section, sql, parameters):
    result = connection.execute(sql, parameters)
    columns = [item[0] for item in result.description]
    rows = []
    for values in result.fetchall():
        row = {name: _normalize(value) for name, value in zip(columns, values)}
        if section == "news":
            row["entity_ids"] = _json(row["entity_ids"])
            row["quality_flags"] = _json(row["quality_flags"])
            if not isinstance(row["entity_ids"], list) or not all(isinstance(item, str) for item in row["entity_ids"]):
                raise ValueError("News entities must be an array of strings")
            if not isinstance(row["quality_flags"], dict):
                raise ValueError("News quality flags must be an object")
        rows.append(row)
    return rows


def _require_schema(connection):
    required = {"schema_versions", *_TABLES.values()}
    tables = dict(connection.execute(
        "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'main'"
    ).fetchall())
    if any(tables.get(name) != "BASE TABLE" for name in required):
        raise ValueError("Required schema-v4 base tables are missing")
    versions = [row[0] for row in connection.execute("SELECT version FROM main.schema_versions").fetchall()]
    if not versions or any(type(value) is not int or value < 1 for value in versions) or max(versions) != 4:
        raise ValueError("Evidence requires schema version 4 without migration")
    for section, table in _TABLES.items():
        columns = {row[0] for row in connection.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'main' AND table_name = ?", [table]
        ).fetchall()}
        if not set(_FIELDS[section]) <= columns:
            raise ValueError("Schema-v4 provenance columns are missing")


def read_evidence_snapshot(
    database: Path, *, instrument_id: str, fundamental_instrument_id: str | None,
    news_entity: str | None, macro_series: tuple[str, ...], as_of: str,
    availability_mode: str, max_records: int,
) -> dict:
    """Read latest known records using strict knowledge-time cutoffs.

    ``local_observed`` additionally requires local ingestion before the cutoff.
    ``reconstructed`` omits only that local-ingestion restriction; it never
    removes availability/publication constraints or backdates Yahoo snapshots.
    """
    cutoff = _cutoff(as_of)
    if not isinstance(availability_mode, str) or availability_mode not in {"local_observed", "reconstructed"}:
        raise ValueError("Unsupported evidence availability mode")
    if type(max_records) is not int or not 1 <= max_records <= 500:
        raise ValueError("Evidence record limit must be between 1 and 500")
    if not isinstance(instrument_id, str) or not instrument_id.strip():
        raise ValueError("An explicit evidence instrument ID is required")
    if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in (fundamental_instrument_id, news_entity)):
        raise ValueError("Optional evidence identifiers must be explicit nonempty strings")
    if (not isinstance(macro_series, tuple) or any(not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9_]{1,64}", value, re.ASCII) for value in macro_series)
            or len(set(macro_series)) != len(macro_series)):
        raise ValueError("Macro series must be a tuple of unique canonical IDs")
    if not isinstance(database, Path) or not database.is_file():
        raise ValueError("Evidence database must already exist as a file")
    connection = None
    try:
        connection = duckdb.connect(str(database), read_only=True, config={
            "enable_external_access": False, "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        })
        connection.execute("SET TimeZone='UTC'")
        connection.execute("BEGIN TRANSACTION")
        _require_schema(connection)
        local = availability_mode == "local_observed"
        limit = max_records + 1

        def known(alias):
            clause = f"{alias}.available_at < ?::TIMESTAMPTZ"
            args = [cutoff]
            if local:
                clause += f" AND {alias}.ingested_at < ?::TIMESTAMPTZ"
                args.append(cutoff)
            return clause, args

        clause, args = known("i")
        identity = _rows(connection, "identity", f"""
            SELECT {_columns('identity', 'i')} FROM main.instruments i
            WHERE i.instrument_id = ? AND {clause}
              AND i.valid_from <= ?::TIMESTAMPTZ
              AND (i.valid_to IS NULL OR i.valid_to > ?::TIMESTAMPTZ)
            ORDER BY i.valid_from DESC, i.source ASC, i.available_at DESC, i.ingested_at DESC
            LIMIT ?
        """, [instrument_id, *args, cutoff, cutoff, limit])

        clause, args = known("b")
        market = _rows(connection, "market", f"""
            SELECT {_columns('market', 'b')} FROM main.market_bars b
            WHERE b.instrument_id = ? AND {clause}
              AND b.source = 'yahoo' AND b.interval = '1d'
              AND starts_with(b.data_version, 'snapshot-v2:')
              AND b.event_time + INTERVAL '1 day' <= ?::TIMESTAMPTZ
            QUALIFY row_number() OVER (
                PARTITION BY b.event_time, b.interval, b.source
                ORDER BY b.available_at DESC, b.ingested_at DESC, b.data_version DESC
            ) = 1
            ORDER BY b.event_time DESC, b.source ASC, b.data_version DESC LIMIT ?
        """, [instrument_id, *args, cutoff, limit])

        fundamentals = []
        if fundamental_instrument_id is not None:
            clause, args = known("f")
            fundamentals = _rows(connection, "fundamentals", f"""
                SELECT {_columns('fundamentals', 'f')} FROM main.fundamentals f
                WHERE f.instrument_id = ? AND {clause}
                  AND f.filed_at < ?::TIMESTAMPTZ AND f.period_end <= ?::DATE
                QUALIFY row_number() OVER (
                    PARTITION BY f.metric, f.period_start, f.period_end, f.unit, f.source
                    ORDER BY f.filed_at DESC, f.accession_number DESC NULLS LAST,
                             f.available_at DESC, f.ingested_at DESC, f.record_id DESC
                ) = 1
                ORDER BY f.period_end DESC, f.period_start DESC NULLS LAST, f.metric ASC,
                         f.unit ASC NULLS LAST, f.source ASC, f.record_id DESC LIMIT ?
            """, [fundamental_instrument_id, *args, cutoff, cutoff.date(), limit])

        macro = {}
        for series in macro_series:
            clause, args = known("m")
            macro[series] = _rows(connection, "macro", f"""
                SELECT {_columns('macro', 'm')} FROM main.macro_observations m
                WHERE m.series_id = ? AND {clause}
                  AND m.observation_date <= ?::DATE AND m.vintage_date <= ?::DATE
                QUALIFY row_number() OVER (
                    PARTITION BY m.observation_date, m.source
                    ORDER BY m.vintage_date DESC, m.available_at DESC, m.ingested_at DESC
                ) = 1
                ORDER BY m.observation_date DESC, m.source ASC, m.vintage_date DESC LIMIT ?
            """, [series, *args, cutoff.date(), cutoff.date(), limit])

        news = []
        if news_entity is not None:
            clause, args = known("n")
            news_filter = f"{clause} AND n.published_at < ?::TIMESTAMPTZ AND n.first_seen_at < ?::TIMESTAMPTZ"
            news_args = [*args, cutoff, cutoff]
            # Malformed entities cannot establish non-membership safely. Check
            # time-eligible metadata before using the exact array-member test.
            invalid = connection.execute(f"""
                SELECT 1 FROM main.news_events n WHERE {news_filter}
                  AND (n.entity_ids IS NULL OR NOT json_valid(n.entity_ids)) LIMIT 1
            """, news_args).fetchone()
            if invalid:
                raise ValueError("Malformed news entity JSON")
            invalid = connection.execute(f"""
                SELECT 1 FROM main.news_events n WHERE {news_filter}
                  AND (json_type(n.entity_ids) <> 'ARRAY' OR EXISTS (
                    SELECT 1 FROM json_each(n.entity_ids) e WHERE e.type <> 'VARCHAR'
                  )) LIMIT 1
            """, news_args).fetchone()
            if invalid:
                raise ValueError("News entities are not an array of strings")
            news = _rows(connection, "news", f"""
                SELECT {_columns('news', 'n')} FROM main.news_events n
                WHERE {news_filter} AND EXISTS (
                    SELECT 1 FROM json_each(n.entity_ids) e
                    WHERE e.type = 'VARCHAR' AND json_extract_string(e.value, '$') = ?
                )
                ORDER BY n.published_at DESC, n.available_at DESC, n.ingested_at DESC, n.event_id ASC
                LIMIT ?
            """, [*news_args, news_entity, limit])
        connection.execute("COMMIT")
        return {"identity": identity, "market": market, "fundamentals": fundamentals,
                "macro": macro, "news": news}
    except Exception:
        if connection is not None:
            with suppress(Exception):
                connection.execute("ROLLBACK")
        raise ValueError("Cannot read a valid schema-v4 research evidence snapshot") from None
    finally:
        if connection is not None:
            connection.close()
