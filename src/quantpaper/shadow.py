"""Journal of predeclared, forward session predictions; no order capability."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import uuid

import joblib
import numpy as np
import pandas as pd

from .ml.features import FEATURE_COLUMNS, FEATURE_CONTRACT_VERSION, latest_feature_snapshot
from .ml.regime import REGIME_FEATURE_COLUMNS, latest_regime_snapshot
from .sessions import completed_bars, is_crypto, next_session, session_bounds, utc_timestamp, validate_bar
from .sources.common import canonical_hash
from .warehouse import PointInTimeWarehouse


@dataclass(frozen=True, slots=True)
class ShadowSignal:
    signal_id: str
    generated_at: str
    generation_date: str
    symbol: str
    asset_class: str
    feature_as_of: str
    probability_up: float
    direction: int
    model_hash: str
    model_trained_through: str
    model_approved: bool
    feature_hash: str
    cost_bps: float
    target_session: str
    target_open: str
    target_close: str
    feature_payload_json: str
    feature_contract_version: str = FEATURE_CONTRACT_VERSION
    status: str = "PENDING"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_shadow_model(model_path: Path, metadata: dict):
    # Only load local trusted joblib files. Verify before deserializing.
    digest = file_sha256(model_path)
    if metadata.get("feature_contract_version") != FEATURE_CONTRACT_VERSION:
        raise RuntimeError("model feature contract is obsolete; train a new v2 artifact")
    if metadata.get("model_sha256") != digest:
        raise RuntimeError("model/metadata SHA-256 mismatch")
    artifact = joblib.load(model_path)
    if artifact.get("feature_contract_version") != FEATURE_CONTRACT_VERSION:
        raise RuntimeError("model feature contract does not match the runner")
    if artifact.get("features") != FEATURE_COLUMNS + REGIME_FEATURE_COLUMNS:
        raise RuntimeError("model feature columns do not match the runner")
    return artifact, digest


def generate_shadow_signals(
    model_path: Path,
    metadata: dict,
    target_frames: dict[str, pd.DataFrame],
    context_frames: dict[str, pd.DataFrame],
    cost_bps: float,
    generated_at: datetime | None = None,
) -> list[ShadowSignal]:
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("cost_bps must be finite and nonnegative (round-trip)")
    artifact, model_hash = load_shadow_model(model_path, metadata)
    now = utc_timestamp(generated_at)
    if artifact.get("created_at") and utc_timestamp(artifact["created_at"]) > now:
        raise RuntimeError("model was created after the requested signal time")
    approved = bool(metadata.get("evaluation", {}).get("approved_for_paper_signals", False))
    context = {s: completed_bars(f, s, now) for s, f in context_frames.items()}
    signals = []
    for symbol, frame in target_frames.items():
        if symbol not in artifact.get("symbols", []):
            raise ValueError(f"{symbol} is outside the model's training universe")
        if is_crypto(symbol):
            raise ValueError(
                "crypto daily open-to-close target needs a delayed-entry model: the next UTC "
                "bar opens before the previous bar can be finalized; shadow generation skipped"
            )
        own = latest_feature_snapshot(completed_bars(frame, symbol, now), symbol)
        feature_as_of = own.index[-1]
        target = next_session(symbol, feature_as_of)
        opening, closing = session_bounds(symbol, target)
        if now >= opening - pd.Timedelta(minutes=1):
            raise ValueError(f"{symbol} data are stale or target {target} has already opened")
        trained_through = pd.Timestamp(str(artifact["trained_through"]), tz="UTC")
        if trained_through > feature_as_of:
            raise RuntimeError(f"model training ends after {symbol} feature snapshot")
        regime = latest_regime_snapshot(feature_as_of, context)
        row = pd.concat([own.iloc[-1][FEATURE_COLUMNS], regime.iloc[-1][REGIME_FEATURE_COLUMNS]])
        row = row.reindex(artifact["features"]).astype(float)
        if not np.isfinite(row.to_numpy()).all():
            raise ValueError(f"invalid features for {symbol}")
        probability = float(artifact["model"].predict_proba(pd.DataFrame([row]))[0, 1])
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("invalid predicted probability")
        payload = {name: float(row[name]) for name in artifact["features"]}
        signals.append(ShadowSignal(
            signal_id=str(uuid.uuid4()), generated_at=now.isoformat(),
            generation_date=now.date().isoformat(), symbol=symbol, asset_class="equity",
            feature_as_of=feature_as_of.date().isoformat(), probability_up=probability,
            direction=1 if probability >= 0.55 else -1 if probability <= 0.45 else 0,
            model_hash=model_hash, model_trained_through=str(artifact["trained_through"]),
            model_approved=approved, feature_hash=canonical_hash(payload), cost_bps=cost_bps,
            target_session=target, target_open=opening.isoformat(), target_close=closing.isoformat(),
            feature_payload_json=json.dumps(payload, sort_keys=True),
        ))
    return signals


class ShadowJournal:
    def __init__(self, database: Path) -> None:
        warehouse = PointInTimeWarehouse(database)
        try:
            warehouse.initialize()
            self.connection = warehouse.connection
            self.connection.execute("BEGIN TRANSACTION")
            for name, dtype in (
                ("target_open", "TIMESTAMPTZ"), ("target_close", "TIMESTAMPTZ"),
                ("feature_payload_json", "VARCHAR"), ("feature_contract_version", "VARCHAR"),
                ("legacy_status", "VARCHAR"), ("settlement_bar_json", "VARCHAR"),
                ("settlement_bar_hash", "VARCHAR"),
            ):
                self.connection.execute(f"ALTER TABLE shadow_signals ADD COLUMN IF NOT EXISTS {name} {dtype}")
            # Keep the original evidence; it has no declared future target or feature contract.
            self.connection.execute("""
                UPDATE shadow_signals SET legacy_status=status, status='INVALIDATED_LEGACY'
                WHERE feature_contract_version IS NULL AND status != 'INVALIDATED_LEGACY'
            """)
            self.connection.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS shadow_target_once
                ON shadow_signals(model_hash, symbol, target_session)
            """)
            self.connection.execute("COMMIT")
            self._in_transaction = False
        except Exception:
            warehouse.close()
            raise

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self):
        if self._in_transaction:
            yield
            return
        self.connection.execute("BEGIN TRANSACTION")
        self._in_transaction = True
        try:
            yield
            self.connection.execute("COMMIT")
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

    def append(self, signals: list[ShadowSignal]) -> int:
        if not signals:
            return 0
        columns = list(asdict(signals[0]))
        for signal in signals:
            opening, closing = session_bounds(signal.symbol, signal.target_session)
            if utc_timestamp(signal.generated_at) >= opening:
                raise ValueError("signal must be generated before target open")
            if next_session(signal.symbol, signal.feature_as_of) != signal.target_session:
                raise ValueError("target is not the next session after feature date")
            if utc_timestamp(signal.target_open) != opening or utc_timestamp(signal.target_close) != closing:
                raise ValueError("declared target bounds disagree with exchange calendar")
            if not math.isfinite(signal.cost_bps) or signal.cost_bps < 0:
                raise ValueError("invalid round-trip cost")
            if signal.feature_contract_version != FEATURE_CONTRACT_VERSION:
                raise ValueError("obsolete signal contract")
            if not math.isfinite(signal.probability_up) or not 0 <= signal.probability_up <= 1:
                raise ValueError("invalid probability")
            expected_direction = 1 if signal.probability_up >= 0.55 else -1 if signal.probability_up <= 0.45 else 0
            if signal.direction != expected_direction:
                raise ValueError("direction disagrees with frozen probability thresholds")
            if canonical_hash(json.loads(signal.feature_payload_json)) != signal.feature_hash:
                raise ValueError("feature payload hash mismatch")
            if pd.Timestamp(signal.model_trained_through) > pd.Timestamp(signal.feature_as_of):
                raise ValueError("model training exceeds feature cutoff")
        with self.transaction():
            before = self.connection.execute("SELECT count(*) FROM shadow_signals").fetchone()[0]
            self.connection.executemany(
                f"INSERT OR IGNORE INTO shadow_signals ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [list(asdict(signal).values()) for signal in signals],
            )
            after = self.connection.execute("SELECT count(*) FROM shadow_signals").fetchone()[0]
            return int(after - before)

    def pending_symbols(self) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT DISTINCT symbol FROM shadow_signals WHERE status='PENDING' ORDER BY symbol"
        ).fetchall()]

    def settle(self, frames: dict[str, pd.DataFrame], now=None) -> int:
        current = utc_timestamp(now)
        pending = self.connection.execute("""
            SELECT signal_id,symbol,target_session,target_open,target_close,direction,cost_bps,generated_at
            FROM shadow_signals WHERE status='PENDING'
        """).fetchall()
        updates = []
        for signal_id, symbol, target_date, opening, closing, direction, cost_bps, generated in pending:
            if target_date is None or opening is None or closing is None:
                raise RuntimeError("pending signal lacks declared session boundaries")
            if current < utc_timestamp(closing) + pd.Timedelta(minutes=30):
                continue
            if utc_timestamp(generated) >= utc_timestamp(opening):
                raise RuntimeError("pending signal was generated after target open")
            if symbol not in frames:
                continue
            data = completed_bars(frames[symbol], symbol, current)
            target = pd.Timestamp(target_date, tz="UTC")
            # Missing target never silently substitutes a later day.
            if target not in data.index:
                continue
            row = data.loc[target]
            validate_bar(row)
            realized = float(row["close"] / row["open"] - 1)
            strategy = direction * realized - (cost_bps / 10_000 if direction else 0)
            payload = {"session": target.date().isoformat(), **{
                field: float(row[field]) for field in ("open", "high", "low", "close")
            }}
            updates.append([realized, strategy, current.isoformat(), json.dumps(payload, sort_keys=True),
                            canonical_hash(payload), signal_id])
        if updates:
            with self.transaction():
                self.connection.executemany("""
                    UPDATE shadow_signals SET status='EVALUATED',realized_return=?,strategy_return=?,
                        evaluated_at=?::TIMESTAMPTZ,settlement_bar_json=?,settlement_bar_hash=?
                    WHERE signal_id=? AND status='PENDING'
                """, updates)
        return len(updates)

    def report(self) -> dict:
        counts = dict(self.connection.execute(
            "SELECT status,count(*) FROM shadow_signals GROUP BY status ORDER BY status").fetchall())
        evaluated = self.connection.execute("""
            SELECT model_hash,symbol,target_session,probability_up,realized_return,strategy_return,cost_bps
            FROM shadow_signals WHERE status='EVALUATED' ORDER BY model_hash,target_session,symbol
        """).fetchdf()
        cohorts = []
        # Do not add independent symbol returns or combine different model versions.
        if not evaluated.empty:
            for (model_hash,cost), group in evaluated.groupby(["model_hash", "cost_bps"]):
                probabilities = group["probability_up"].to_numpy()
                targets = (group["realized_return"].to_numpy() > 0).astype(int)
                cohorts.append({
                    "model_hash": model_hash, "cost_bps": float(cost), "observations": len(group),
                    "target_sessions": int(group["target_session"].nunique()),
                    "brier_score": float(np.mean((probabilities-targets)**2)),
                    "mean_hypothetical_signal_return": float(group["strategy_return"].mean()),
                    "portfolio_return": None,
                    "note": "signal diagnostics only; no capital allocation or actual execution",
                })
        recent = self.connection.execute("""
            SELECT generated_at,symbol,feature_as_of,probability_up,direction,model_approved,
                status,target_session,strategy_return,model_hash,feature_contract_version
            FROM shadow_signals ORDER BY generated_at DESC,symbol LIMIT 30
        """).fetchall()
        return {
            "execution_enabled": False, "counts": counts,
            "evaluated": {"observations": len(evaluated), "cohorts": cohorts,
                          "portfolio_return": None},
            "recent": [{
                "generated_at": str(r[0]), "symbol": r[1], "feature_as_of": str(r[2]),
                "probability_up": r[3], "direction": {1:"LONG",-1:"SHORT",0:"FLAT"}[r[4]],
                "model_approved": r[5], "status": r[6],
                "target_session": str(r[7]) if r[7] else None,
                "strategy_return": r[8], "model_hash": r[9], "feature_contract_version": r[10],
            } for r in recent],
        }
