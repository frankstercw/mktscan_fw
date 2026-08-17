"""Alpaca Market Data REST adapter for live stock charts.

Uses Alpaca's official Market Data v2 REST endpoints directly via ``requests``.
The free/default stock feed is normally IEX; users with SIP entitlement can set
``ALPACA_DATA_FEED=sip`` in the environment.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests
from .live_market import LiveStockQuote


class AlpacaMarketDataError(RuntimeError):
    pass


class AlpacaMarketDataClient:
    BASE_URL = "https://data.alpaca.markets"

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        *,
        feed: str | None = None,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
        self.secret_key = secret_key or os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
        self.feed = (feed or os.getenv("ALPACA_DATA_FEED") or "iex").lower()
        self.timeout = timeout
        self.session = session or requests.Session()

        if not self.api_key or not self.secret_key:
            raise AlpacaMarketDataError(
                "Alpaca credentials are missing. Set ALPACA_API_KEY and "
                "ALPACA_SECRET_KEY (or APCA_API_KEY_ID/APCA_API_SECRET_KEY)."
            )
        if self.feed not in {"iex", "sip", "boats", "otc"}:
            raise AlpacaMarketDataError(f"Unsupported ALPACA_DATA_FEED={self.feed!r}")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.session.get(
                    f"{self.BASE_URL}{path}",
                    headers=self._headers,
                    params=params or {},
                    timeout=self.timeout,
                )
                if resp.status_code >= 400:
                    request_id = resp.headers.get("X-Request-ID")
                    try:
                        detail = resp.json()
                    except Exception:
                        detail = getattr(resp, "text", "")[:500]
                    suffix = f" (request id {request_id})" if request_id else ""
                    err = AlpacaMarketDataError(f"Alpaca HTTP {resp.status_code}: {detail}{suffix}")
                    # Retry rate limits and server failures; auth/permission errors
                    # should fail fast because another request will not fix them.
                    if resp.status_code not in {429, 500, 502, 503, 504}:
                        raise err
                    last_error = err
                else:
                    return resp.json()
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
        raise AlpacaMarketDataError(f"Alpaca request failed after 3 attempts: {last_error}")

    def get_quote(self, ticker: str) -> LiveStockQuote:
        ticker = ticker.strip().upper()
        payload = self._get(f"/v2/stocks/{ticker}/snapshot", {"feed": self.feed})

        quote = payload.get("latestQuote") or {}
        trade = payload.get("latestTrade") or {}
        minute = payload.get("minuteBar") or {}
        daily = payload.get("dailyBar") or {}
        previous = payload.get("prevDailyBar") or {}

        last = _float_or_none(trade.get("p"))
        if last is None:
            last = _float_or_none(minute.get("c"))
        if last is None:
            last = _float_or_none(daily.get("c"))

        ts = _parse_ts(trade.get("t") or quote.get("t") or minute.get("t"))
        return LiveStockQuote(
            ticker=ticker,
            timestamp=ts,
            last=last,
            bid=_float_or_none(quote.get("bp")),
            ask=_float_or_none(quote.get("ap")),
            day_open=_float_or_none(daily.get("o")),
            day_high=_float_or_none(daily.get("h")),
            day_low=_float_or_none(daily.get("l")),
            day_close=_float_or_none(daily.get("c")),
            prev_close=_float_or_none(previous.get("c")),
            day_volume=_int_or_none(daily.get("v")),
            source="alpaca",
            feed=self.feed,
        )

    def get_bars(
        self,
        ticker: str,
        *,
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
        limit: int = 10_000,
    ) -> pd.DataFrame:
        ticker = ticker.strip().upper()
        params: dict[str, Any] = {
            "timeframe": timeframe,
            "start": _to_rfc3339(start),
            "limit": min(max(int(limit), 1), 10_000),
            "adjustment": "all",
            "feed": self.feed,
            "sort": "asc",
        }
        if end is not None:
            params["end"] = _to_rfc3339(end)

        all_rows: list[dict[str, Any]] = []
        page_token: str | None = None
        # The requested chart windows stay under 10k bars, but pagination makes
        # the provider safe if the range changes later.
        for _ in range(20):
            if page_token:
                params["page_token"] = page_token
            payload = self._get(f"/v2/stocks/{ticker}/bars", params)
            all_rows.extend(payload.get("bars") or [])
            page_token = payload.get("next_page_token")
            if not page_token or len(all_rows) >= limit:
                break

        if not all_rows:
            return _empty_bars()

        rows = all_rows[:limit]
        df = pd.DataFrame(rows).rename(
            columns={
                "t": "timestamp",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
                "n": "trade_count",
                "vw": "provider_vwap",
            }
        )
        keep = [
            c for c in
            ["timestamp", "open", "high", "low", "close", "volume", "trade_count", "provider_vwap"]
            if c in df.columns
        ]
        df = df[keep].copy()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        for col in ["open", "high", "low", "close", "volume", "trade_count", "provider_vwap"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["timestamp", "open", "high", "low", "close"]).sort_values("timestamp")
        return df.reset_index(drop=True)


def _to_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return ts.to_pydatetime()
    except Exception:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _empty_bars() -> pd.DataFrame:
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
