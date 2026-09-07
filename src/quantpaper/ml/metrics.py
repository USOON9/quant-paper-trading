"""An explicit capital convention shared by the daily research evaluators.

Each symbol owns one fixed, equal capital sleeve for the whole evaluation.
An absent row or FLAT signal holds that sleeve in cash; it does not redistribute
the capital to another asset. Equity and crypto are aggregated on calendar days.
Costs are total round-trip bps for an active open-to-close prediction, not bps
per side. These diagnostics assume fills at bar prices, not executable fills.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


PORTFOLIO_DEFINITION = "fixed_equal_symbol_sleeves_cash_on_missing_calendar_days"
COST_DEFINITION = "round_trip_bps_per_active_prediction"
BENCHMARK_DEFINITION = "equal_sleeve_intraday_long_flat_overnight_not_buy_and_hold"


def validate_cost(cost_bps: float) -> None:
    if not math.isfinite(cost_bps) or cost_bps < 0:
        raise ValueError("round-trip cost_bps must be finite and nonnegative")


def portfolio_daily_returns(predictions: pd.DataFrame, row_returns: np.ndarray) -> pd.Series:
    if predictions.empty:
        raise ValueError("cannot evaluate an empty prediction set")
    dates = pd.DatetimeIndex(pd.to_datetime(predictions.index, utc=True)).normalize()
    symbols = predictions["symbol"].to_numpy()
    values = np.asarray(row_returns, dtype=float)
    if values.shape != (len(predictions),) or not np.isfinite(values).all():
        raise ValueError("one finite return is required per prediction")
    if dates.hasnans or pd.isna(symbols).any():
        raise ValueError("each prediction requires a valid date and symbol")
    panel = pd.DataFrame({"date": dates, "symbol": symbols, "return": values})
    if panel.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate symbol/session predictions would double-count capital")
    daily = panel.groupby("date")["return"].sum() / panel["symbol"].nunique()
    calendar = pd.date_range(dates.min(), dates.max(), freq="D")
    return daily.reindex(calendar, fill_value=0.0).rename("return")


def return_metrics(daily: pd.Series) -> dict[str, float]:
    if daily.empty or not np.isfinite(daily.to_numpy()).all() or (daily <= -1).any():
        raise ValueError("portfolio daily returns must be finite and strictly greater than -100%")
    equity = (1.0 + daily).cumprod()
    # Include the original capital of 1.0, otherwise the first day's loss vanishes.
    high_water_mark = equity.cummax().clip(lower=1.0)
    drawdown = equity / high_water_mark - 1.0
    deviation = float(daily.std(ddof=1))
    sharpe = (
        float(daily.mean()) / deviation * math.sqrt(365.25)
        if math.isfinite(deviation) and deviation > 0 else 0.0
    )
    return {
        "net_return_pct": float((equity.iloc[-1] - 1.0) * 100.0),
        "annualized_sharpe": sharpe,
        "max_drawdown_pct": float(drawdown.min() * 100.0),
    }


def strategy_metrics(predictions: pd.DataFrame, cost_bps: float) -> dict[str, float]:
    validate_cost(cost_bps)
    probability = predictions["probability"].to_numpy(dtype=float)
    if not np.isfinite(probability).all() or ((probability < 0) | (probability > 1)).any():
        raise ValueError("probabilities must be finite and in [0, 1]")
    signal = np.where(probability >= 0.55, 1.0, np.where(probability <= 0.45, -1.0, 0.0))
    returns = signal * predictions["target_return"].to_numpy(dtype=float)
    returns -= (signal != 0.0) * cost_bps / 10_000.0
    return return_metrics(portfolio_daily_returns(predictions, returns))
