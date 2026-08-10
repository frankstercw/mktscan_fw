"""
mktscan/options.py
──────────────────────────────────────────────────────────────────────────────
Options trade setup generator — priced against the live option chain.

What this replaces
──────────────────
The previous version never looked at an option. It produced an entry zone,
profit target, stop loss and "risk/reward ratio" entirely in *underlying price*
terms, then attached a strike derived from rounding spot to the nearest $5. That
had four concrete problems:

1. **The R/R was wrong by an order of magnitude.** ``2 ATR target / 1.5 ATR stop
   = 1.33`` describes a stock position. On a 1-week ATM call, the same two price
   levels are roughly +200% and -75%. The number shown was not the number traded.

2. **No liquidity check.** No open interest, no bid/ask, no volume. A suggested
   strike could easily have a 30%-wide spread and 4 contracts of open interest,
   making the "entry price" fiction.

3. **No premium and no max loss.** The tool never said what a trade cost or what
   could be lost — the first two numbers anyone needs.

4. **Strikes that may not exist.** ``_round_strike`` forced $5 increments above
   $200; AAPL, NVDA and SPY all trade $1 and $2.50 strikes.

Now: strikes are chosen by delta from the actual chain, filtered for liquidity,
priced off real bid/ask, and every quoted number (max loss, max profit,
breakeven, probability of profit, R/R) is an option-level figure.

Everything remains educational research output, not advice — see DISCLAIMER.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .clock import market_date
from .pricing import (
    bs_greeks, bs_price, implied_vol,
    probability_of_profit_long, years_to_expiry,
)
from .strategy import StrategySpec, select_strategy

log = logging.getLogger(__name__)

DISCLAIMER = (
    "⚠️ EDUCATIONAL ONLY — NOT FINANCIAL ADVICE. "
    "Options trading involves substantial risk of loss, and the entire premium "
    "paid can be lost. These are algorithmically generated research signals "
    "priced from delayed public data. Verify every quote with your broker before "
    "trading, and consult a licensed financial adviser."
)

CONTRACT_MULTIPLIER = 100

# ── Liquidity thresholds ──────────────────────────────────────────────────────
# A contract failing these is not tradeable at anything like the displayed price.
MIN_OPEN_INTEREST   = 100     # contracts
MIN_VOLUME          = 10      # contracts traded today
MAX_SPREAD_PCT      = 0.10    # (ask - bid) / mid
MAX_SPREAD_PCT_SOFT = 0.20    # accepted with a warning if nothing better exists
MIN_BID             = 0.05    # below this the quote is noise


@dataclass
class Leg:
    """One priced contract in a structure."""
    action:      str            # BUY | SELL
    right:       str            # C | P
    strike:      float
    expiry:      str
    bid:         float
    ask:         float
    mid:         float
    open_interest: int
    volume:      int
    iv:          float | None
    delta:       float | None
    theta:       float | None
    vega:        float | None
    spread_pct:  float | None
    quantity:    int = 1

    @property
    def is_long(self) -> bool:
        return self.action.upper() == "BUY"

    @property
    def fill_price(self) -> float:
        """
        Conservative fill assumption: pay the ask, receive the bid.

        Mid-price fills are the optimistic assumption most backtests make and
        most retail traders do not achieve on multi-leg orders. Quoting the
        pessimistic side keeps the displayed max loss honest.
        """
        return self.ask if self.is_long else self.bid

    @property
    def mid_price(self) -> float:
        return self.mid

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "right": self.right, "strike": self.strike,
            "expiry": self.expiry, "bid": round(self.bid, 2), "ask": round(self.ask, 2),
            "mid": round(self.mid, 2), "fill": round(self.fill_price, 2),
            "open_interest": self.open_interest, "volume": self.volume,
            "iv": round(self.iv, 4) if self.iv is not None else None,
            "delta": round(self.delta, 3) if self.delta is not None else None,
            "theta": round(self.theta, 4) if self.theta is not None else None,
            "vega": round(self.vega, 4) if self.vega is not None else None,
            "spread_pct": round(self.spread_pct, 4) if self.spread_pct is not None else None,
            "quantity": self.quantity,
            "label": (
                f"{self.action} {self.quantity} {self.expiry} "
                f"${self.strike:g}{self.right} @ ${self.mid:.2f} "
                f"(bid {self.bid:.2f} / ask {self.ask:.2f})"
            ),
        }


# ── Chain access ──────────────────────────────────────────────────────────────

def fetch_spot(ticker: str) -> float | None:
    """Latest traded price."""
    try:
        import yfinance as yf
        tkr = yf.Ticker(ticker)
        try:
            fast = tkr.fast_info
            for key in ("last_price", "lastPrice", "regular_market_price"):
                value = fast.get(key) if hasattr(fast, "get") else getattr(fast, key, None)
                if value:
                    return float(value)
        except Exception:
            pass
        hist = tkr.history(period="5d")
        if hist is not None and not hist.empty:
            return float(hist["Close"].dropna().iloc[-1])
    except Exception as e:
        log.debug(f"[Options] spot fetch failed for {ticker}: {e}")
    return None


def fetch_chain(
    ticker: str,
    target_dte: int = 35,
    min_dte: int = 21,
    max_dte: int = 60,
) -> dict[str, Any] | None:
    """
    Fetch the option chain for the expiry closest to ``target_dte``.

    Also returns the list of available expiries so the caller can report what was
    actually available rather than assuming a Friday exists. ``_next_friday()``
    used to assume exactly that, ignoring holidays and the Monday/Wednesday
    weeklies that many of these names now list.
    """
    try:
        import yfinance as yf

        tkr         = yf.Ticker(ticker)
        expirations = list(tkr.options or [])
        if not expirations:
            return None

        today = market_date()
        candidates = []
        for exp_str in expirations:
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (exp_date - today).days
            if min_dte <= dte <= max_dte:
                candidates.append((abs(dte - target_dte), dte, exp_str))

        if not candidates:
            # Nothing in the preferred band — widen rather than fail, and report
            # the DTE so the caller can judge.
            for exp_str in expirations:
                try:
                    exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                except ValueError:
                    continue
                dte = (exp_date - today).days
                if dte >= 7:
                    candidates.append((abs(dte - target_dte), dte, exp_str))
            if not candidates:
                return None

        _, dte, expiry = min(candidates)
        chain = tkr.option_chain(expiry)

        return {
            "expiry": expiry,
            "dte": dte,
            "calls": chain.calls,
            "puts": chain.puts,
            "available_expiries": expirations,
        }
    except Exception as e:
        log.debug(f"[Options] chain fetch failed for {ticker}: {e}")
        return None


def _row_quality(row, spot: float) -> dict[str, Any]:
    """Extract and sanity-check a single chain row."""
    def _num(key, default=0.0):
        try:
            value = row.get(key)
            return default if value is None else float(value)
        except (TypeError, ValueError):
            return default

    bid = _num("bid")
    ask = _num("ask")
    last = _num("lastPrice")

    # Some rows quote 0 bid with a real ask; fall back to last where sensible.
    if bid <= 0 and ask <= 0 and last > 0:
        bid = ask = last

    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else (ask or bid or last)
    spread_pct = ((ask - bid) / mid) if mid > 0 and ask > 0 and bid > 0 else None

    return {
        "strike": _num("strike"),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "last": last,
        "volume": int(_num("volume")),
        "open_interest": int(_num("openInterest")),
        "iv": _num("impliedVolatility", 0.0) or None,
        "spread_pct": spread_pct,
    }


def _liquidity_ok(quote: dict, strict: bool = True) -> bool:
    if quote["bid"] < MIN_BID:
        return False
    if quote["open_interest"] < MIN_OPEN_INTEREST:
        return False
    if strict:
        if quote["volume"] < MIN_VOLUME:
            return False
        if quote["spread_pct"] is None or quote["spread_pct"] > MAX_SPREAD_PCT:
            return False
    else:
        if quote["spread_pct"] is not None and quote["spread_pct"] > MAX_SPREAD_PCT_SOFT:
            return False
    return True


def select_leg_by_delta(
    frame,
    right: str,
    target_delta: float,
    spot: float,
    dte: int,
    expiry: str,
    action: str,
    exclude_strikes: set[float] | None = None,
) -> tuple[Leg | None, list[str]]:
    """
    Pick the most liquid contract whose delta is closest to ``target_delta``.

    Delta rather than a fixed OTM percentage: a "2% OTM" call is a 0.45 delta on
    a low-vol name and a 0.30 delta on a high-vol one, so the old fixed-percentage
    rule produced structures with wildly different risk profiles across the
    basket while claiming they were the same trade.

    Deltas are computed from Black-Scholes using the chain's own implied
    volatility, because yfinance does not supply greeks.
    """
    warnings: list[str] = []
    if frame is None or frame.empty:
        return None, ["empty chain"]

    exclude = exclude_strikes or set()
    t       = years_to_expiry(dte)
    right   = right.upper()[0]

    candidates: list[tuple[float, dict, float]] = []
    rejected_liquidity = 0

    for _, row in frame.iterrows():
        quote = _row_quality(row, spot)
        if quote["strike"] <= 0 or quote["strike"] in exclude:
            continue

        strict_ok = _liquidity_ok(quote, strict=True)
        soft_ok   = _liquidity_ok(quote, strict=False)
        if not soft_ok:
            rejected_liquidity += 1
            continue

        iv = quote["iv"]
        if not iv or iv <= 0.01 or iv > 3.0:
            # Chain IV is unusable — back it out of the mid price instead.
            iv = implied_vol(quote["mid"], spot, quote["strike"], t, right)
        if not iv:
            continue

        greeks = bs_greeks(spot, quote["strike"], t, iv, right)
        delta  = greeks.delta
        quote["_iv"] = iv
        quote["_greeks"] = greeks

        # Penalise soft-only matches so a strict match always wins on a tie.
        penalty = 0.0 if strict_ok else 0.15
        candidates.append((abs(abs(delta) - abs(target_delta)) + penalty, quote, delta))

    if not candidates:
        return None, [f"no liquid {right} strikes (rejected {rejected_liquidity} on liquidity)"]

    _, best, delta = min(candidates, key=lambda c: c[0])
    greeks = best["_greeks"]

    if best["spread_pct"] and best["spread_pct"] > MAX_SPREAD_PCT:
        warnings.append(
            f"${best['strike']:g}{right} spread is {best['spread_pct']*100:.0f}% "
            f"of mid — expect slippage"
        )
    if best["open_interest"] < MIN_OPEN_INTEREST * 3:
        warnings.append(
            f"${best['strike']:g}{right} open interest only {best['open_interest']}"
        )

    leg = Leg(
        action=action, right=right, strike=best["strike"], expiry=expiry,
        bid=best["bid"], ask=best["ask"], mid=best["mid"],
        open_interest=best["open_interest"], volume=best["volume"],
        iv=best["_iv"], delta=delta, theta=greeks.theta, vega=greeks.vega,
        spread_pct=best["spread_pct"],
    )
    return leg, warnings


# ── Structure economics ───────────────────────────────────────────────────────

def _net_debit(legs: list[Leg], use_mid: bool = False) -> float:
    """
    Net cash flow per share. Positive = debit paid, negative = credit received.
    """
    total = 0.0
    for leg in legs:
        price = leg.mid_price if use_mid else leg.fill_price
        total += price * leg.quantity * (1 if leg.is_long else -1)
    return total


def _structure_economics(structure: str, legs: list[Leg], net_debit: float) -> dict[str, Any]:
    """
    Max profit, max loss and breakeven for the supported structures.

    All per-share; multiply by 100 for per-contract dollars.
    """
    strikes = sorted({leg.strike for leg in legs})

    if structure in ("bull_call_spread", "bear_put_spread"):
        width      = abs(strikes[-1] - strikes[0])
        max_loss   = net_debit
        max_profit = width - net_debit
        if structure == "bull_call_spread":
            long_leg  = next(l for l in legs if l.is_long)
            breakeven = long_leg.strike + net_debit
        else:
            long_leg  = next(l for l in legs if l.is_long)
            breakeven = long_leg.strike - net_debit
        return {"max_profit": max_profit, "max_loss": max_loss,
                "breakeven": breakeven, "width": width, "is_credit": False}

    if structure in ("bull_put_spread", "bear_call_spread"):
        width      = abs(strikes[-1] - strikes[0])
        credit     = -net_debit                    # net_debit is negative here
        max_profit = credit
        max_loss   = width - credit
        short_leg  = next(l for l in legs if not l.is_long)
        breakeven  = (short_leg.strike - credit if structure == "bull_put_spread"
                      else short_leg.strike + credit)
        return {"max_profit": max_profit, "max_loss": max_loss,
                "breakeven": breakeven, "width": width, "is_credit": True}

    if structure == "iron_condor":
        puts   = sorted([l for l in legs if l.right == "P"], key=lambda l: l.strike)
        calls  = sorted([l for l in legs if l.right == "C"], key=lambda l: l.strike)
        credit = -net_debit
        put_width  = abs(puts[1].strike - puts[0].strike) if len(puts) == 2 else 0.0
        call_width = abs(calls[1].strike - calls[0].strike) if len(calls) == 2 else 0.0
        width      = max(put_width, call_width)
        short_put  = next((l for l in puts if not l.is_long), None)
        short_call = next((l for l in calls if not l.is_long), None)
        return {
            "max_profit": credit,
            "max_loss": width - credit,
            "breakeven": None,
            "breakeven_lower": short_put.strike - credit if short_put else None,
            "breakeven_upper": short_call.strike + credit if short_call else None,
            "width": width, "is_credit": True,
        }

    if structure in ("long_call", "long_put"):
        leg = legs[0]
        breakeven = (leg.strike + net_debit if leg.right == "C"
                     else leg.strike - net_debit)
        return {"max_profit": None,          # unbounded for a call
                "max_loss": net_debit,
                "breakeven": breakeven, "width": None, "is_credit": False}

    return {"max_profit": None, "max_loss": abs(net_debit),
            "breakeven": None, "width": None, "is_credit": net_debit < 0}


def value_at(
    legs: list[Leg], spot_at: float, days_forward: int, dte: int,
    iv_shift: float = 0.0,
) -> float:
    """
    Reprice the whole structure at a future spot, ``days_forward`` from now.

    This is what makes an honest risk/reward possible: the position is revalued
    with time decay and an optional volatility shift, rather than assuming the
    option moves one-for-one with the stock.
    """
    remaining = max(0, dte - days_forward)
    t = years_to_expiry(remaining)

    total = 0.0
    for leg in legs:
        vol = max(0.01, (leg.iv or 0.30) + iv_shift)
        px  = bs_price(spot_at, leg.strike, t, vol, leg.right)
        total += px * leg.quantity * (1 if leg.is_long else -1)
    return total


# ── ATR (still useful for choosing target/stop levels on the underlying) ──────

def fetch_ohlc(ticker: str, bars: int = 14) -> list[tuple[float, float, float]]:
    """Recent (high, low, close) tuples, oldest first."""
    try:
        import pandas as pd
        import yfinance as yf

        end   = market_date()
        start = end - timedelta(days=int(bars * 2) + 10)
        raw   = yf.download(ticker, start=str(start), end=str(end + timedelta(days=1)),
                            progress=False, auto_adjust=True)
        if raw is None or raw.empty:
            return []

        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)

        raw = raw.dropna(subset=["High", "Low", "Close"]).tail(bars)
        return [
            (float(h), float(l), float(c))
            for h, l, c in zip(raw["High"], raw["Low"], raw["Close"])
        ]
    except Exception as e:
        log.debug(f"[Options] fetch_ohlc({ticker}) failed: {e}")
        return []


def calc_atr(ohlc: list[tuple[float, float, float]]) -> float:
    if not ohlc or len(ohlc) < 2:
        return 0.0
    true_ranges = []
    for i, (h, l, c) in enumerate(ohlc):
        if i == 0:
            true_ranges.append(h - l)
        else:
            prev_c = ohlc[i - 1][2]
            true_ranges.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(true_ranges) / len(true_ranges)


def find_support_resistance(
    ohlc: list[tuple[float, float, float]], current_price: float,
) -> tuple[float | None, float | None]:
    """Nearest swing low below and swing high above spot."""
    if len(ohlc) < 5:
        return None, None

    highs = [h for h, _, _ in ohlc]
    lows  = [l for _, l, _ in ohlc]
    swing_highs, swing_lows = [], []

    for i in range(2, len(highs) - 2):
        if highs[i] == max(highs[i - 2:i + 3]):
            swing_highs.append(highs[i])
        if lows[i] == min(lows[i - 2:i + 3]):
            swing_lows.append(lows[i])

    above = [h for h in swing_highs if h > current_price]
    below = [l for l in swing_lows if l < current_price]
    return (max(below) if below else None, min(above) if above else None)


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_trade_setup(
    ticker:       str,
    tradeability: dict[str, Any],
    spot:         float | None = None,
    chain:        dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a fully priced options trade setup for one ticker.

    Returns a dict containing the concrete legs with real bid/ask, the net
    debit/credit, max loss and max profit in dollars per contract, breakeven,
    probability of profit, and an option-level risk/reward — plus every warning
    raised while selecting strikes.
    """
    score = tradeability.get("score", 0.0)
    spec: StrategySpec | None = tradeability.get("strategy_spec")

    if spec is None:
        spec = select_strategy(
            score=score,
            iv_rank=tradeability.get("iv_rank"),
            days_to_earn=tradeability.get("days_to_earnings"),
            annual_vol=tradeability.get("annual_vol"),
            iv_basis=tradeability.get("iv_basis", "none"),
            signal_confidence=tradeability.get("coverage", 1.0),
        )

    base = {
        "ticker":       ticker,
        "strategy":     spec.name,
        "structure":    spec.structure,
        "direction":    spec.direction.upper(),
        "sizing":       spec.sizing,
        "rationale":    spec.rationale,
        "iv_note":      spec.iv_note,
        "tradeability": round(score, 4),
        "tradeability_label": tradeability.get("label", "NEUTRAL"),
        "tradeability_color": tradeability.get("color", "#fbbf24"),
        "rsi":          tradeability.get("rsi"),
        "annual_vol":   tradeability.get("annual_vol"),
        "iv_rank":      tradeability.get("iv_rank"),
        "iv_basis":     tradeability.get("iv_basis", "none"),
        "days_to_earn": tradeability.get("days_to_earnings"),
        "coverage":     tradeability.get("coverage"),
        "disclaimer":   DISCLAIMER,
        "generated_at": datetime.utcnow().isoformat(),
        "warnings":     [],
    }

    # ── No-trade / avoid paths exit before touching the chain ────────────────
    if not spec.tradeable:
        base.update({
            "tradeable": False,
            "reason": spec.avoid_reason or "no_edge",
            "legs": [],
        })
        return base

    spot = spot or fetch_spot(ticker)
    if not spot or spot <= 0:
        base.update({"tradeable": False, "reason": "no_price",
                     "error": "Could not retrieve spot price.", "legs": []})
        return base
    base["spot"] = round(spot, 2)

    chain = chain or fetch_chain(ticker, spec.target_dte, spec.min_dte, spec.max_dte)
    if not chain:
        base.update({"tradeable": False, "reason": "no_chain",
                     "error": "No option chain available in the target expiry window.",
                     "legs": []})
        return base

    expiry, dte = chain["expiry"], chain["dte"]
    base["expiry"] = expiry
    base["dte"]    = dte

    if dte < spec.min_dte or dte > spec.max_dte:
        base["warnings"].append(
            f"Nearest expiry is {dte} DTE, outside the preferred "
            f"{spec.min_dte}-{spec.max_dte} window."
        )

    # ── Build the legs ────────────────────────────────────────────────────────
    legs: list[Leg] = []
    used_strikes: set[float] = set()

    for leg_spec in spec.legs:
        right = leg_spec["right"]
        frame = chain["calls"] if right == "C" else chain["puts"]
        leg, warnings = select_leg_by_delta(
            frame=frame, right=right, target_delta=leg_spec["target_delta"],
            spot=spot, dte=dte, expiry=expiry, action=leg_spec["action"],
            exclude_strikes=used_strikes,
        )
        base["warnings"].extend(warnings)
        if leg is None:
            base.update({
                "tradeable": False, "reason": "illiquid",
                "error": (
                    f"Could not find a liquid {right} strike near "
                    f"{leg_spec['target_delta']:.2f} delta. This name's options "
                    f"are too thin to trade this structure."
                ),
                "legs": [l.as_dict() for l in legs],
            })
            return base
        used_strikes.add(leg.strike)
        legs.append(leg)

    # ── Economics ─────────────────────────────────────────────────────────────
    net_debit_fill = _net_debit(legs, use_mid=False)   # conservative
    net_debit_mid  = _net_debit(legs, use_mid=True)    # what you might get filled at
    econ           = _structure_economics(spec.structure, legs, net_debit_fill)

    max_loss   = econ["max_loss"]
    max_profit = econ["max_profit"]

    # Slippage: the gap between a mid fill and a conservative fill, which is the
    # cost the previous version implicitly assumed was zero.
    slippage = abs(net_debit_fill - net_debit_mid)

    # ── Price targets on the underlying, for context ──────────────────────────
    ohlc = fetch_ohlc(ticker, bars=14)
    atr  = calc_atr(ohlc) if ohlc else spot * 0.02
    support, resistance = find_support_resistance(ohlc, spot)

    if spec.direction == "bullish":
        price_target = spot + atr * 2.0
        if resistance and resistance < price_target:
            price_target = resistance * 0.99
        price_stop = spot - atr * 1.5
    elif spec.direction == "bearish":
        price_target = spot - atr * 2.0
        if support and support > price_target:
            price_target = support * 1.01
        price_stop = spot + atr * 1.5
    else:
        price_target, price_stop = spot, spot

    # ── Option-level P&L at those levels ─────────────────────────────────────
    # Evaluated at the halfway point of the holding period, which is a realistic
    # management horizon, and includes time decay. This is the number the old
    # "rr_ratio" was pretending to be.
    hold_days    = max(1, dte // 2)
    value_now    = net_debit_fill
    value_target = value_at(legs, price_target, hold_days, dte)
    value_stop   = value_at(legs, price_stop,   hold_days, dte)

    # One sign convention covers both debit and credit structures. `value_at`
    # signs long legs positive and short legs negative, so the mark of a credit
    # spread is negative and P&L is still (later - now):
    #   debit  opened at +2.00, now worth +3.00 →  +1.00 profit
    #   credit opened at -1.00, now worth -0.20 →  +0.80 profit (cheaper to close)
    pnl_target = value_target - value_now
    pnl_stop   = value_stop - value_now

    # Clamp to the structure's defined bounds — Black-Scholes can drift slightly
    # past them near expiry.
    if max_profit is not None:
        pnl_target = min(pnl_target, max_profit)
    if max_loss is not None:
        pnl_stop = max(pnl_stop, -abs(max_loss))

    reward = max(0.0, pnl_target)
    risk   = max(1e-9, abs(pnl_stop))
    rr_option = round(reward / risk, 2)

    # ── Probability ───────────────────────────────────────────────────────────
    priced_ivs = [l.iv for l in legs if l.iv]
    avg_iv     = sum(priced_ivs) / len(priced_ivs) if priced_ivs else 0.30
    t          = years_to_expiry(dte)

    # Risk-neutral probability of finishing on the profitable side of breakeven
    # at expiry. Bullish structures (debit call spreads and credit put spreads
    # alike) profit above their breakeven; bearish ones profit below it.
    pop = None
    if econ.get("breakeven"):
        pop = probability_of_profit_long(
            spot, econ["breakeven"], t, avg_iv,
            is_call=(spec.direction == "bullish"),
        )

    # ── Net greeks ────────────────────────────────────────────────────────────
    net_delta = sum((l.delta or 0) * l.quantity * (1 if l.is_long else -1) for l in legs)
    net_theta = sum((l.theta or 0) * l.quantity * (1 if l.is_long else -1) for l in legs)
    net_vega  = sum((l.vega  or 0) * l.quantity * (1 if l.is_long else -1) for l in legs)

    # ── Confidence tier ───────────────────────────────────────────────────────
    coverage = tradeability.get("coverage", 0.0) or 0.0
    liquidity_clean = not any("spread" in w or "open interest" in w for w in base["warnings"])
    if coverage >= 0.7 and abs(score) >= 0.4 and liquidity_clean:
        confidence_tier, conf_color = "HIGH", "#22d3a0"
    elif coverage >= 0.45 and abs(score) >= 0.2:
        confidence_tier, conf_color = "MEDIUM", "#fbbf24"
    else:
        confidence_tier, conf_color = "LOW", "#f87171"

    base.update({
        "tradeable":      True,
        "legs":           [l.as_dict() for l in legs],
        "net_debit":      round(net_debit_fill, 2),
        "net_debit_mid":  round(net_debit_mid, 2),
        "is_credit":      econ["is_credit"],
        "cost_per_contract":   round(abs(net_debit_fill) * CONTRACT_MULTIPLIER, 2),
        "max_loss_per_contract":   round(abs(max_loss) * CONTRACT_MULTIPLIER, 2) if max_loss is not None else None,
        "max_profit_per_contract": round(max_profit * CONTRACT_MULTIPLIER, 2) if max_profit is not None else None,
        "breakeven":      round(econ["breakeven"], 2) if econ.get("breakeven") else None,
        "breakeven_lower": round(econ["breakeven_lower"], 2) if econ.get("breakeven_lower") else None,
        "breakeven_upper": round(econ["breakeven_upper"], 2) if econ.get("breakeven_upper") else None,
        "breakeven_move_pct": (
            round((econ["breakeven"] - spot) / spot * 100, 2)
            if econ.get("breakeven") else None
        ),
        "spread_width":   econ.get("width"),
        "slippage_per_contract": round(slippage * CONTRACT_MULTIPLIER, 2),
        "max_return_pct": (
            round(max_profit / abs(max_loss) * 100, 1)
            if max_profit and max_loss else None
        ),
        # Option-level, not stock-level.
        "rr_ratio":       rr_option,
        "pnl_at_target_per_contract": round(pnl_target * CONTRACT_MULTIPLIER, 2),
        "pnl_at_stop_per_contract":   round(pnl_stop * CONTRACT_MULTIPLIER, 2),
        "price_target":   round(price_target, 2),
        "price_stop":     round(price_stop, 2),
        "hold_days":      hold_days,
        "atr":            round(atr, 2),
        "atr_pct":        round(atr / spot * 100, 2),
        "support":        round(support, 2) if support else None,
        "resistance":     round(resistance, 2) if resistance else None,
        "probability_of_profit": round(pop * 100, 1) if pop else None,
        "net_delta":      round(net_delta, 3),
        "net_theta_per_day_per_contract": round(net_theta * CONTRACT_MULTIPLIER, 2),
        "net_vega_per_contract":          round(net_vega * CONTRACT_MULTIPLIER, 2),
        "avg_iv":         round(avg_iv, 4),
        "confidence_tier": confidence_tier,
        "conf_color":     conf_color,
    })

    # A structure whose max profit does not clear its max loss is rarely worth
    # the execution risk, whatever the directional signal says.
    if max_profit is not None and max_loss and max_profit < abs(max_loss) * 0.5:
        base["warnings"].append(
            f"Max profit (${max_profit * CONTRACT_MULTIPLIER:.0f}) is less than half "
            f"of max loss (${abs(max_loss) * CONTRACT_MULTIPLIER:.0f}) — poor payoff "
            f"for the risk taken."
        )

    return base


def generate_basket_setups(
    basket_tradeability: dict[str, dict],
    max_workers: int = 6,
) -> dict[str, dict]:
    """
    Generate setups for every ticker, fetching chains in parallel.

    Chain fetches are network-bound and were previously serial *and* duplicated
    (options.py re-downloaded OHLC and re-fetched spot that tradeability.py had
    already pulled moments earlier).
    """
    from concurrent.futures import ThreadPoolExecutor

    tickers = list(basket_tradeability.keys())
    if not tickers:
        return {}

    def _one(ticker: str) -> tuple[str, dict]:
        try:
            return ticker, generate_trade_setup(ticker, basket_tradeability[ticker])
        except Exception as e:
            log.warning(f"[Options] setup failed for {ticker}: {e}")
            return ticker, {
                "ticker": ticker, "tradeable": False, "reason": "error",
                "error": str(e), "legs": [], "disclaimer": DISCLAIMER,
            }

    with ThreadPoolExecutor(max_workers=min(max_workers, len(tickers))) as pool:
        return dict(pool.map(_one, tickers))
