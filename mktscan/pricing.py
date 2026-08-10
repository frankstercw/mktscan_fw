"""
mktscan/pricing.py
──────────────────────────────────────────────────────────────────────────────
Black-Scholes pricing and greeks.

Needed because the tool has to reason about option P&L, not stock P&L. The old
trade setups reported a risk/reward ratio computed entirely on the underlying —
"target is 2 ATR away, stop is 1.5 ATR away, therefore R/R = 1.33" — for a
position whose payoff is nonlinear in that price. For a 1-week ATM call, a
2 ATR favourable move is roughly +150-300% on premium while hitting the stop is
roughly -70-85%. The reported 1.33 understated both sides by an order of
magnitude, which made every setup look far tamer than it was.

yfinance option chains give strike, bid, ask, last, volume, open interest and
implied volatility — but no greeks. Delta in particular is needed to pick
strikes: "2% out of the money" means something completely different on a 15%-vol
utility than on an 80%-vol miner, whereas a 0.30-delta call is comparable across
both.

Everything here is the standard European Black-Scholes model. American-style
equity options are worth slightly more than this (early exercise), and the model
assumes lognormal returns and constant volatility, neither of which is true.
It is used for strike selection and for approximate P&L projection, both of
which are robust to that error. It is not used to decide whether an option is
mispriced.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# Rough short-rate. Precision here barely matters: over 30-45 days the rate term
# moves an ATM option's delta by well under a percentage point.
DEFAULT_RISK_FREE_RATE = 0.042

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float      # per calendar day
    vega:  float      # per 1 volatility point (0.01)
    rho:   float


def _d1_d2(spot: float, strike: float, t: float, vol: float, r: float, q: float):
    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        return None, None
    denom = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * vol * vol) * t) / denom
    return d1, d1 - denom


def bs_price(
    spot: float, strike: float, t: float, vol: float,
    right: str = "C", r: float = DEFAULT_RISK_FREE_RATE, q: float = 0.0,
) -> float:
    """
    European option price.

    ``t`` is time to expiry in years, ``vol`` is annualised volatility as a
    decimal (0.28 = 28%), ``right`` is "C" or "P".
    """
    right = right.upper()[0]
    if t <= 0:                       # at expiry, worth intrinsic
        return max(0.0, spot - strike) if right == "C" else max(0.0, strike - spot)
    if vol <= 0:
        fwd = spot * math.exp((r - q) * t)
        disc = math.exp(-r * t)
        return (max(0.0, fwd - strike) * disc if right == "C"
                else max(0.0, strike - fwd) * disc)

    d1, d2 = _d1_d2(spot, strike, t, vol, r, q)
    if d1 is None:
        return 0.0

    if right == "C":
        return spot * math.exp(-q * t) * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * math.exp(-q * t) * _norm_cdf(-d1)


def bs_greeks(
    spot: float, strike: float, t: float, vol: float,
    right: str = "C", r: float = DEFAULT_RISK_FREE_RATE, q: float = 0.0,
) -> Greeks:
    """Price plus the greeks that matter for sizing and strike selection."""
    right = right.upper()[0]
    price = bs_price(spot, strike, t, vol, right, r, q)

    if t <= 0 or vol <= 0 or spot <= 0 or strike <= 0:
        intrinsic_delta = (
            (1.0 if spot > strike else 0.0) if right == "C"
            else (-1.0 if spot < strike else 0.0)
        )
        return Greeks(price=price, delta=intrinsic_delta, gamma=0.0,
                      theta=0.0, vega=0.0, rho=0.0)

    d1, d2 = _d1_d2(spot, strike, t, vol, r, q)
    disc_q = math.exp(-q * t)
    disc_r = math.exp(-r * t)
    sqrt_t = math.sqrt(t)

    if right == "C":
        delta = disc_q * _norm_cdf(d1)
        theta = (
            -(spot * _norm_pdf(d1) * vol * disc_q) / (2 * sqrt_t)
            - r * strike * disc_r * _norm_cdf(d2)
            + q * spot * disc_q * _norm_cdf(d1)
        )
        rho = strike * t * disc_r * _norm_cdf(d2) / 100.0
    else:
        delta = -disc_q * _norm_cdf(-d1)
        theta = (
            -(spot * _norm_pdf(d1) * vol * disc_q) / (2 * sqrt_t)
            + r * strike * disc_r * _norm_cdf(-d2)
            - q * spot * disc_q * _norm_cdf(-d1)
        )
        rho = -strike * t * disc_r * _norm_cdf(-d2) / 100.0

    gamma = disc_q * _norm_pdf(d1) / (spot * vol * sqrt_t)
    vega  = spot * disc_q * _norm_pdf(d1) * sqrt_t / 100.0   # per 1 vol point

    return Greeks(
        price=price, delta=delta, gamma=gamma,
        theta=theta / 365.0,      # per calendar day
        vega=vega, rho=rho,
    )


def implied_vol(
    market_price: float, spot: float, strike: float, t: float,
    right: str = "C", r: float = DEFAULT_RISK_FREE_RATE, q: float = 0.0,
    tol: float = 1e-6, max_iter: int = 100,
) -> float | None:
    """
    Back out implied volatility by bisection.

    Bisection rather than Newton-Raphson: it cannot diverge, and for deep ITM/OTM
    strikes vega approaches zero, which makes Newton unstable exactly where the
    chain data is least reliable. 100 iterations is microseconds.
    """
    if t <= 0 or spot <= 0 or strike <= 0 or market_price <= 0:
        return None

    intrinsic = (max(0.0, spot - strike) if right.upper()[0] == "C"
                 else max(0.0, strike - spot))
    if market_price < intrinsic - tol:
        return None                      # arbitrage / stale quote

    lo, hi = 1e-4, 5.0
    if bs_price(spot, strike, t, hi, right, r, q) < market_price:
        return None                      # beyond 500% vol — bad quote

    for _ in range(max_iter):
        mid   = 0.5 * (lo + hi)
        price = bs_price(spot, strike, t, mid, right, r, q)
        if abs(price - market_price) < tol:
            return mid
        if price > market_price:
            hi = mid
        else:
            lo = mid

    return 0.5 * (lo + hi)


def years_to_expiry(days: int | float) -> float:
    """Calendar days to year fraction, floored just above zero."""
    return max(float(days), 0.0) / 365.0


def probability_itm(
    spot: float, strike: float, t: float, vol: float,
    right: str = "C", r: float = DEFAULT_RISK_FREE_RATE, q: float = 0.0,
) -> float | None:
    """
    Risk-neutral probability of finishing in the money — i.e. N(d2) for a call.

    Worth reporting alongside any long-premium suggestion. A 0.30-delta call is
    roughly a 30% chance of expiring ITM, and "expiring ITM" is not the same as
    "profitable" — the premium still has to be recovered. Showing this next to
    the breakeven makes the distinction concrete.
    """
    if t <= 0 or vol <= 0:
        return None
    _, d2 = _d1_d2(spot, strike, t, vol, r, q)
    if d2 is None:
        return None
    return _norm_cdf(d2) if right.upper()[0] == "C" else _norm_cdf(-d2)


def probability_of_profit_long(
    spot: float, breakeven: float, t: float, vol: float,
    is_call: bool, r: float = DEFAULT_RISK_FREE_RATE, q: float = 0.0,
) -> float | None:
    """Risk-neutral probability of finishing beyond the breakeven price."""
    if t <= 0 or vol <= 0 or spot <= 0 or breakeven <= 0:
        return None
    _, d2 = _d1_d2(spot, breakeven, t, vol, r, q)
    if d2 is None:
        return None
    return _norm_cdf(d2) if is_call else _norm_cdf(-d2)
