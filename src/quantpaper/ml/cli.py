"""Download Yahoo daily data, train and evaluate the ML baseline."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from .features import build_features
from .training import train_and_evaluate
from .metrics import validate_cost
from .yahoo import YahooDailyData, completed_daily_rows, dataset_fingerprint


DEFAULT_SYMBOLS = ["SPY", "JPM", "XOM", "WMT", "JNJ", "BTC-USD"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Yahoo Finance daily ML research pipeline")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--period", default="max")
    parser.add_argument("--cost-bps", type=float, default=5.0, help="Total round-trip basis points per active open-to-close prediction")
    parser.add_argument("--offline", action="store_true", help="Use existing cached CSVs without downloading or changing them")
    parser.add_argument("--data-dir", type=Path, default=Path("data/yahoo"))
    parser.add_argument("--model", type=Path, default=Path("artifacts/yahoo_daily_model.joblib"))
    parser.add_argument("--metadata", type=Path, default=Path("artifacts/yahoo_daily_model.json"))
    args = parser.parse_args(argv)
    try:
        validate_cost(args.cost_bps)
    except ValueError as exc:
        parser.error(str(exc))
    args.symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in args.symbols))

    source = YahooDailyData(args.data_dir)
    results = [source.cached(symbol) if args.offline else source.download(symbol, args.period)
               for symbol in dict.fromkeys(args.symbols)]
    frames = [build_features(completed_daily_rows(source.load(result.path)), result.symbol) for result in results]
    dataset = pd.concat(frames).sort_index()
    fingerprint = dataset_fingerprint([result.path for result in results])
    evaluation = train_and_evaluate(
        dataset=dataset,
        model_path=args.model,
        metadata_path=args.metadata,
        fingerprint=fingerprint,
        cost_bps=args.cost_bps,
    )
    output = {
        "downloads": [
            {**asdict(result), "path": str(result.path)}
            for result in results
        ],
        "dataset_rows": len(dataset),
        "model_path": str(args.model),
        "metadata_path": str(args.metadata),
        "evaluation": asdict(evaluation),
    }
    print(json.dumps(output, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
