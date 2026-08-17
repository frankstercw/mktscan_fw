"""ORATS Data API adapter.

Uses the official Data API endpoints documented at https://orats.com/docs.
Authentication is read from ``ORATS_API_TOKEN`` (preferred) or
``MKTSCAN_ORATS_TOKEN``. Secrets are never written to config.yaml.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

import requests

from .base import OptionQuote


class OratsError(RuntimeError):
    pass


class OratsClient:
    BASE_URL = "https://api.orats.io/datav2"

    def __init__(self, token: str | None = None, timeout: int = 30,
                 session: requests.Session | None = None):
        self.token = (token or os.getenv("ORATS_API_TOKEN") or
                      os.getenv("MKTSCAN_ORATS_TOKEN") or "").strip()
        self.timeout = timeout
        self.http = session or requests.Session()
        if not self.token:
            raise OratsError(
                "ORATS token is not configured. Set ORATS_API_TOKEN in Railway/local env."
            )

    def _get(self, path: str, **params) -> list[dict[str, Any]]:
        query = {k: v for k, v in params.items() if v is not None}
        query["token"] = self.token
        try:
            response = self.http.get(
                f"{self.BASE_URL}/{path.lstrip('/')}", params=query,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise OratsError(f"ORATS request failed for {path}: {exc}") from exc
        if isinstance(payload, dict):
            if payload.get("error"):
                raise OratsError(str(payload["error"]))
            data = payload.get("data", [])
        else:
            data = payload
        if not isinstance(data, list):
            raise OratsError(f"Unexpected ORATS response for {path}")
        return data

    @staticmethod
    def _date(value: Any) -> date:
        if isinstance(value, date):
            return value
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()

    @staticmethod
    def _float(row: dict, key: str) -> float | None:
        value = row.get(key)
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _int(row: dict, key: str) -> int | None:
        value = OratsClient._float(row, key)
        return int(value) if value is not None else None

    def get_chain(self, ticker: str, trade_date: date, min_dte: int = 21,
                  max_dte: int = 60) -> list[OptionQuote]:
        """Historical near-EOD chain normalized to one row per call/put."""
        rows = self._get(
            "hist/strikes", ticker=ticker.upper(),
            tradeDate=trade_date.isoformat(), dte=f"{min_dte},{max_dte}",
        )
        out: list[OptionQuote] = []
        for row in rows:
            try:
                td = self._date(row["tradeDate"])
                expiry = self._date(row["expirDate"])
                strike = float(row["strike"])
            except (KeyError, TypeError, ValueError):
                continue
            common = dict(
                ticker=ticker.upper(), trade_date=td, expiration=expiry,
                strike=strike,
                underlying_price=self._float(row, "spotPrice") or self._float(row, "stockPrice"),
                gamma=self._float(row, "gamma"), theta=self._float(row, "theta"),
                vega=self._float(row, "vega"), source="ORATS_EOD",
            )
            call_delta = self._float(row, "delta")
            out.append(OptionQuote(
                **common, right="C", bid=self._float(row, "callBidPrice"),
                ask=self._float(row, "callAskPrice"), model_value=self._float(row, "callValue"),
                volume=self._int(row, "callVolume"), open_interest=self._int(row, "callOpenInterest"),
                iv=(self._float(row, "callMidIv") or self._float(row, "smvVol")),
                delta=call_delta,
            ))
            # ORATS strike rows expose call delta. Put delta follows parity: C delta - 1.
            put_delta = (call_delta - 1.0) if call_delta is not None else None
            out.append(OptionQuote(
                **common, right="P", bid=self._float(row, "putBidPrice"),
                ask=self._float(row, "putAskPrice"), model_value=self._float(row, "putValue"),
                volume=self._int(row, "putVolume"), open_interest=self._int(row, "putOpenInterest"),
                iv=(self._float(row, "putMidIv") or self._float(row, "smvVol")),
                delta=put_delta,
            ))
        return out

    def get_summary(self, ticker: str, trade_date: date | None = None) -> dict[str, Any] | None:
        path = "hist/summaries" if trade_date else "summaries"
        rows = self._get(path, ticker=ticker.upper(),
                         tradeDate=trade_date.isoformat() if trade_date else None)
        return rows[0] if rows else None

    def get_iv_rank(self, ticker: str, trade_date: date | None = None) -> dict[str, Any] | None:
        path = "hist/ivrank" if trade_date else "ivrank"
        rows = self._get(path, ticker=ticker.upper(),
                         tradeDate=trade_date.isoformat() if trade_date else None)
        return rows[0] if rows else None

    def get_implied_monies(self, ticker: str, trade_date: date | None = None) -> list[dict[str, Any]]:
        path = "hist/monies/implied" if trade_date else "monies/implied"
        return self._get(path, ticker=ticker.upper(),
                         tradeDate=trade_date.isoformat() if trade_date else None)
