"""Second-stage regime-aware expanding walk-forward research pipeline."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .features import build_features
from .regime import CONTEXT_SYMBOLS, attach_regime_features
from .walkforward import train_walk_forward
from .metrics import validate_cost
from .yahoo import YahooDailyData, completed_daily_rows, dataset_fingerprint


DEFAULT_TARGETS = ["SPY", "JPM", "XOM", "WMT", "JNJ", "BTC-USD"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Regime-aware walk-forward ML research")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_TARGETS)
    parser.add_argument("--period", default="max")
    parser.add_argument("--cost-bps", type=float, default=5.0, help="Total round-trip basis points per active open-to-close prediction")
    parser.add_argument("--offline", action="store_true", help="Use existing cached CSVs without downloading or changing them")
    parser.add_argument("--data-dir", type=Path, default=Path("data/yahoo"))
    parser.add_argument("--model", type=Path, default=Path("artifacts/yahoo_walkforward_v2_model.joblib"))
    parser.add_argument("--metadata", type=Path, default=Path("artifacts/yahoo_walkforward_v2_model.json"))
    parser.add_argument("--predictions", type=Path, default=Path("artifacts/yahoo_walkforward_v2_oos.csv"))
    args = parser.parse_args(argv)
    try:
        validate_cost(args.cost_bps)
    except ValueError as exc:
        parser.error(str(exc))

    args.symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in args.symbols))

    source = YahooDailyData(args.data_dir)
    unique_symbols = list(dict.fromkeys([*args.symbols, *CONTEXT_SYMBOLS]))
    downloads = {symbol: source.cached(symbol) if args.offline else source.download(symbol, args.period)
                 for symbol in unique_symbols}
    raw = {symbol: completed_daily_rows(source.load(result.path)) for symbol, result in downloads.items()}
    context = {symbol: raw[symbol] for symbol in CONTEXT_SYMBOLS}
    frames = [
        attach_regime_features(build_features(raw[symbol], symbol), context)
        for symbol in args.symbols
    ]
    dataset = pd.concat(frames).sort_index()
    fingerprint = dataset_fingerprint([result.path for result in downloads.values()])
    evaluation = train_walk_forward(
        dataset,
        args.model,
        args.metadata,
        args.predictions,
        fingerprint,
        args.cost_bps,
    )
    print(
        json.dumps(
            {
                "targets": args.symbols,
                "context": list(CONTEXT_SYMBOLS),
                "dataset_rows": len(dataset),
                "model_path": str(args.model),
                "metadata_path": str(args.metadata),
                "predictions_path": str(args.predictions),
                "evaluation": asdict(evaluation),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
