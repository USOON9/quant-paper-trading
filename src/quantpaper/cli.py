"""Command-line entry point for the deterministic multi-asset demo."""

from __future__ import annotations

import argparse
import json
import tomllib
from decimal import Decimal
from pathlib import Path

from .audit import AuditTrail
from .data import synthetic_quotes
from .domain import AssetClass, Instrument, Portfolio
from .engine import TradingEngine
from .execution import ExecutionConfig, PaperExchange
from .risk import RiskEngine, RiskLimits
from .strategy import ImbalanceStrategy, StrategyConfig


def build_engine(config_path: Path) -> tuple[TradingEngine, list[Instrument], int]:
    with config_path.open("rb") as file:
        raw = tomllib.load(file)

    instruments = [
        Instrument("AAPL", AssetClass.EQUITY, Decimal("0.01"), Decimal("1")),
        Instrument("BTC/USD", AssetClass.CRYPTO, Decimal("0.01"), Decimal("0.0001")),
        Instrument(
            "AAPL260918C00225000",
            AssetClass.OPTION,
            Decimal("0.01"),
            Decimal("1"),
            Decimal("100"),
        ),
    ]
    engine_cfg = raw["engine"]
    strategy_cfg = raw["strategy"]
    risk_cfg = raw["risk"]
    starting_cash = Decimal(engine_cfg["starting_cash"])
    portfolio = Portfolio(starting_cash)
    strategy = ImbalanceStrategy(
        StrategyConfig(
            imbalance_threshold=Decimal(strategy_cfg["imbalance_threshold"]),
            momentum_weight=Decimal(strategy_cfg["momentum_weight"]),
            cooldown_ns=int(strategy_cfg["cooldown_ms"]) * 1_000_000,
        ),
        max_positions={"AAPL": Decimal("100"), "BTC/USD": Decimal("0.10"), "AAPL260918C00225000": Decimal("5")},
    )
    risk = RiskEngine(
        RiskLimits(
            max_order_notional=Decimal(risk_cfg["max_order_notional"]),
            max_symbol_notional=Decimal(risk_cfg["max_symbol_notional"]),
            max_gross_notional=Decimal(risk_cfg["max_gross_notional"]),
            max_daily_loss=Decimal(risk_cfg["max_daily_loss"]),
            max_orders_per_second=int(risk_cfg["max_orders_per_second"]),
        ),
        starting_equity=starting_cash,
    )
    exchange = PaperExchange(
        ExecutionConfig(
            latency_ns=int(engine_cfg["latency_ms"]) * 1_000_000,
            slippage_bps=Decimal(engine_cfg["slippage_bps"]),
        )
    )
    engine = TradingEngine(
        instruments={instrument.symbol: instrument for instrument in instruments},
        portfolio=portfolio,
        strategy=strategy,
        risk=risk,
        exchange=exchange,
    )
    return engine, instruments, int(engine_cfg["seed"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe multi-asset paper-trading research engine")
    parser.add_argument("command", choices=["demo"])
    parser.add_argument("--events", type=int, default=1500, help="events per instrument")
    parser.add_argument("--config", type=Path, default=Path("configs/paper.toml"))
    parser.add_argument("--audit", type=Path, help="append hash-chained JSONL audit events")
    args = parser.parse_args(argv)
    if args.events <= 0:
        parser.error("--events must be positive")

    engine, instruments, seed = build_engine(args.config)
    if args.audit:
        engine.audit = AuditTrail(args.audit)
    report = engine.run(synthetic_quotes(instruments, args.events, seed))
    print(json.dumps(report.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
