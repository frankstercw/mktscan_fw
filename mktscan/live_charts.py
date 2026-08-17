"""Live stock chart helpers: range selection and intraday technical overlays."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class ChartRange:
    label: str
    timeframe: str
    lookback: timedelta
    max_bars: int = 10_000
    intraday: bool = True


CHART_RANGES: dict[str, ChartRange] = {
    "1D": ChartRange("1D", "1Min", timedelta(days=3), intraday=True),
    "5D": ChartRange("5D", "5Min", timedelta(days=10), intraday=True),
    "1M": ChartRange("1M", "30Min", timedelta(days=40), intraday=True),
    "3M": ChartRange("3M", "1Hour", timedelta(days=110), intraday=True),
    "6M": ChartRange("6M", "1Day", timedelta(days=220), intraday=False),
    "1Y": ChartRange("1Y", "1Day", timedelta(days=380), intraday=False),
}


def chart_window(range_label: str, now: datetime | None = None) -> tuple[ChartRange, datetime, datetime]:
    cfg = CHART_RANGES.get(range_label, CHART_RANGES["1D"])
    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - cfg.lookback
    return cfg, start, end


def prepare_chart_bars(df: pd.DataFrame, range_label: str) -> pd.DataFrame:
    """Add chart indicators and trim the requested range for display."""
    if df is None or df.empty:
        return df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()

    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    out = out.dropna(subset=["timestamp"]).sort_values("timestamp")
    out["market_time"] = out["timestamp"].dt.tz_convert(MARKET_TZ)

    # Keep only the most recent session for 1D. The API request intentionally
    # looks back a few calendar days so Monday/weekend/holiday charts still work.
    if range_label == "1D" and not out.empty:
        last_session = out["market_time"].dt.date.max()
        out = out[out["market_time"].dt.date == last_session].copy()

    close = pd.to_numeric(out["close"], errors="coerce")
    volume = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)
    high = pd.to_numeric(out["high"], errors="coerce")
    low = pd.to_numeric(out["low"], errors="coerce")

    out["ema_9"] = close.ewm(span=9, adjust=False, min_periods=1).mean()
    out["ema_20"] = close.ewm(span=20, adjust=False, min_periods=1).mean()
    out["volume_sma_20"] = volume.rolling(20, min_periods=1).mean()
    denom = out["volume_sma_20"].replace(0, np.nan)
    out["bar_rvol"] = volume / denom

    # Session-reset VWAP for intraday data. For daily bars, cumulative VWAP over
    # the displayed range is less useful, so expose NaN and hide the overlay.
    if CHART_RANGES.get(range_label, CHART_RANGES["1D"]).intraday:
        typical = (high + low + close) / 3.0
        session_key = out["market_time"].dt.date
        pv = typical * volume
        out["vwap"] = pv.groupby(session_key).cumsum() / volume.groupby(session_key).cumsum().replace(0, np.nan)
    else:
        out["vwap"] = np.nan

    return out.reset_index(drop=True)


def daily_relative_volume(daily_bars: pd.DataFrame, current_volume: int | float | None) -> float | None:
    """Current daily volume divided by the prior 20 completed daily bars' mean."""
    if current_volume is None or daily_bars is None or daily_bars.empty:
        return None
    vols = pd.to_numeric(daily_bars.get("volume"), errors="coerce").dropna()
    if len(vols) < 5:
        return None
    # If today's daily bar is present, dropping the final observation avoids
    # comparing a partial session with itself. Using the last 20 prior bars also
    # behaves correctly after-hours when the current day is complete.
    baseline = vols.iloc[:-1].tail(20) if len(vols) > 1 else vols
    avg = baseline.mean()
    if not avg or np.isnan(avg):
        return None
    return float(current_volume) / float(avg)
