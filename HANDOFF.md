# MktScan — Handoff

Everything needed to pick this project up cold. Written for whoever (or whatever) continues the work next.

---

## What this is

A self-hosted market scanner that ingests stock news, prices and earnings; scores each ticker in a watch basket with a composite "tradeability" signal; and turns that signal into concrete options trade setups priced against the live option chain. Streamlit dashboard + CLI + background scheduler.

Stack: Python 3.11, SQLAlchemy 2.x, Alembic, Streamlit, yfinance, APScheduler. Deploys to Railway as two services plus Postgres.

---

## Where things stand

**Code: complete and tested.** A large review-and-fix pass is finished. 58 unit tests pass; all 242 intra-package imports resolve.

**Deployment: partially working, one step outstanding.** The app is on Railway with Postgres attached. The last known blocker was that both Railway services were defaulting to the `dashboard` role, so nothing ran migrations and the database stayed empty. The fix is committed — each service needs `MKTSCAN_ROLE` set explicitly (`scheduler` on one, `dashboard` on the other) and its build settings reset to defaults. See `DEPLOY.md` step 5–6.

**Not yet verified end-to-end:** the live pipeline has never been observed completing a full run against real Yahoo data. Everything below the network boundary is tested; the network path is not. Specifically unverified: option-chain fetches returning liquid strikes for the default basket, the IV backfill completing inside Railway's container start window, and the first successful scheduler run.

---

## Read these first, in order

1. **`mktscan-review.md`** — the original technical review. Explains what was broken and why it mattered. Every fix in the codebase traces back to a numbered section here.
2. **`DEPLOY.md`** — Railway deployment, verification and troubleshooting.
3. **`README.md`** — architecture, CLI reference, how to interpret the numbers honestly.

---

## What changed, and why it matters

Three problems dominated, all of which produced numbers that looked authoritative and were not:

**IV rank never worked.** `IVSnapshot` was declared on its own `declarative_base()`, so `init_db()` never created the table. The tradeability signal read `iv_52w_low` / `iv_52w_high` off `PriceSnapshot` through `getattr(..., None)` — columns that did not exist — so the rank was permanently `None`. The category carried the joint-highest weight and was described in comments as "the primary strategy selector". Every recommendation collapsed to the strategy grid's unknown-IV fallback.

**Options were never priced as options.** Entry, target, stop and risk/reward were all computed on the underlying. A reported R/R of 1.33 on a 1-week ATM call described a stock position; the actual option outcomes were roughly +200% and −75%. No liquidity screen, no premium, no max loss.

**The backtest tested a different model.** It reconstructed 3 of 9 categories using *different formulas* than production — the RSI 40–60 band was neutral in production and +0.3 in the backtest. The win rates shown did not describe the live signal.

Plus two data bugs corrupting inputs silently: `epsDifference` (dollars) stored as `surprise_pct` and consumed as a percentage, and upcoming-earnings rows deduplicated on the literal string `"Upcoming"`, freezing the first date ever fetched — which is what the "avoid within 3 days of earnings" guardrail ran on.

And one that had been mismeasuring the model's own accuracy: `tradeability_outcomes` was recording the **sentiment** score, not the composite, so the "Signal Accuracy" panel had been evaluating news sentiment alone.

---

## Module map

New modules added during the fix pass:

| File | Purpose |
|---|---|
| `mktscan/clock.py` | Market-time helpers. `as_date` for calendar dates (no TZ conversion), `as_market_date` for UTC instants. Mixing these shifts earnings dates by a day. |
| `mktscan/cross_section.py` | Ranks raw features within the basket. Fixes signals that were near-constant positives for any large cap in an uptrend. |
| `mktscan/strategy.py` | The single strategy selector. Replaced two contradictory ones. |
| `mktscan/pricing.py` | Black-Scholes pricing and greeks. Verified against put-call parity to 1e-15. |

Substantially rewritten: `tradeability.py`, `options.py`, `feedback.py`, `backtest_incremental.py`, `iv_rank.py`, `database.py`, `dashboard/app.py`.

---

## Design decisions worth preserving

These were deliberate. Reverting them reintroduces known bugs.

**Zero-confidence categories are excluded from the weighting denominator**, not counted as neutral at 30% weight. The old behaviour compressed every score toward NEUTRAL because ~28% of total weight was routinely dead.

**Cross-sectional ranking is blended 65/35 with the absolute mapping.** Pure cross-sectional loses genuine level information; pure absolute is regime-dependent and often degenerate.

**Strikes are selected by delta, not by fixed OTM percentage.** "2% OTM" is a 0.45 delta on a low-vol name and 0.30 on a high-vol one.

**Default expiry is 30–45 DTE, pushed past known earnings dates.** The old selector reached for 1-week ATM options on its *strongest* signals — maximum theta, highest breakeven hurdle.

**Fill prices are conservative** (pay the ask, receive the bid). Mid-price fills are the optimistic assumption most backtests make and most retail traders do not get on multi-leg orders.

**Proxy-based IV rank is treated as `unknown` by the strategy layer.** Realised volatility is not implied volatility; ranking one against the other pins every reading near the top.

**Feedback score adjustment is off by default** (`FEEDBACK_ADJUSTMENT_ENABLED = False`). The effect size was smaller than the uncertainty in the statistic driving it, and it silently altered a displayed number.

**Weights are not fitted and should not be presented as if they were.** Nine weights cannot be estimated on nineteen tickers without overfitting.

---

## Known limitations

- **Historical option chains are unavailable** from yfinance. IV rank needs ~60 days of live daily snapshots before it measures implied rather than realised volatility. Until then `basis` reads `proxy`.
- **The backtest cannot reconstruct** news sentiment, analyst targets or short interest historically. Each observation records `coverage` — the fraction of model weight that was actually live.
- **Option P&L in the backtest** uses realised vol as the IV estimate, ignoring the variance risk premium and IV changes over the holding period. It will tend to flatter debit spreads.
- **The basket is survivorship-biased** — it is today's list.
- **Black-Scholes assumes** European exercise, lognormal returns, constant volatility. Fine for strike selection and approximate P&L; not for judging whether an option is mispriced.

---

## Running locally

```bash
pip install -r requirements.txt          # or requirements-railway.txt to skip torch
alembic upgrade head
python -m mktscan doctor                 # database, schema, row counts, IV status
python -m mktscan run --mode all
python -m mktscan iv --backfill && python -m mktscan iv --update
python -m mktscan setups
streamlit run dashboard/app.py
```

Tests:

```bash
pytest tests/ -v
```

`tests/test_options_pipeline.py` is the regression suite for this work — each test names the bug it guards against. `tests/test_scrapers.py` covers scrapers and sentiment.

---

## Suggested next steps

**Immediate — finish the deploy.** Set `MKTSCAN_ROLE` per service, reset build settings to defaults, redeploy, and read the `doctor` block in the logs. `DEPLOY.md` has the expected output and a symptom-to-cause table.

**Once running:**

1. **Watch for the first successful scrape.** Confirm articles land, prices populate, and a scheduler run completes without errors.
2. **Confirm the IV backfill finished.** `python -m mktscan iv` should show `basis=proxy` initially, then `chain` after ~60 daily snapshots. Until then the strategy layer is running uninformed about the volatility regime.
3. **Sanity-check a live setup against a broker.** Pick one ticker, compare the suggested strikes, bid/ask and max loss against a real chain. This is the single most valuable validation and has not been done.

**Later:**

4. **Run the backtest and read `excess_return_pct`**, not the raw win rate. If the excess is not positive and stable, the signal is not adding anything and the weights are the first thing to revisit.
5. **Consider collapsing to 3–4 signals.** Section 9 of the review argues nine categories with ~40 hand-tuned thresholds is heavily overparameterised for a 19-ticker basket. The cross-sectional layer mitigates but does not solve this. Fewer parameters will likely perform better out of sample.
6. **Only then consider enabling feedback adjustment**, and only if the pooled accuracy panel shows a statistically significant edge.

---

## Things that will bite you

- **`as_date` vs `as_market_date`.** Calendar dates (earnings, expiries) must not be timezone-converted; UTC instants (`predicted_at`) must be. Getting this backwards shifts dates by a day and silently breaks the earnings blackout.
- **Component dicts are heterogeneous** — floats, ints, bools, strings, `None`. Never format them with a bare float format code. Use `fmt_component` / `format_component_value`.
- **Postgres vs SQLite booleans.** `= 1` works on SQLite and aborts the transaction on Postgres. Migration `0001` handles this; new migrations must too.
- **`create_all()` never adds columns** to an existing table. Schema changes go through Alembic. `ensure_schema()` is the safety net, not the mechanism.
- **The scheduler must stay at 1 replica.** Unique constraints prevent duplicate rows, but two replicas still double the Yahoo request rate.

---

## 2026-08-17 — Market regime context added

A separate, non-intervening market context layer now exists in `mktscan/regime.py`.
It combines SPY/QQQ trend (45%), active-basket breadth (25%), VIX regime (20%), and
2Y/10Y rates context (10%). Macro-event proximity is stored as a caution flag but
is intentionally excluded from the directional score. `mktscan/macro.py` persists
live MarketWatch calendar events. Migration `0002_market_regime.py` creates
`macro_events` and `market_regime_snapshots`, and adds regime-at-prediction fields
to `tradeability_outcomes` so later validation can segment results by the regime
that actually existed when the prediction was made.

Commands: `python -m mktscan regime --refresh` and `python -m mktscan regime`.
The dashboard now shows a Market Regime panel. Do not wire this score into
tradeability or sizing until regime-conditioned forward results demonstrate edge.
