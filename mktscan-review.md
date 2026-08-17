# MktScan — Technical Review

The engineering is solid: clean module boundaries, graceful degradation, thoughtful docstrings, real tests. The problems are in the modeling layer, and they cluster into three themes:

1. **It recommends options but never looks at an option.** Every price, target, stop and R/R is computed on the underlying stock.
2. **Three subsystems display numbers that aren't what they claim** — IV rank, feedback calibration, and the backtest.
3. **The composite score has little cross-sectional discrimination.** Several inputs are near-constant positives for any large-cap in an uptrend.

Ordered by impact.

---

## 1. IV rank is structurally dead — and it's the "primary strategy selector"

`tradeability.py:1218` reads `iv_52w_low` / `iv_52w_high` off `PriceSnapshot` via `getattr(..., None)`. **Those columns don't exist** on the model (`database.py:95–119`). So `iv_rank` is always `None`.

The fallback is `price_data["implied_volatility"]`, sourced from `info.get("impliedVolatility")` (`yahoo.py:89`) — a field yfinance almost never populates for equities. So `calc_options_iv_signal` returns early with score 0.0, confidence 0.0.

Meanwhile the real IV pipeline exists in `iv_rank.py`, but:

- `IVSnapshot` is declared on its **own `declarative_base()`** (`iv_rank.py:37`), so `init_db()` never creates the table.
- `compute_iv_rank()` is never called from anywhere (only `update_iv_snapshot` is, from the scheduler — and it throws on every run because the table doesn't exist; swallowed at `scheduler.py:102`).
- `check_and_migrate()` uses `engine.execute()` (line 95), removed in SQLAlchemy 2.0.

**Downstream consequence:** `suggest_options_strategy` always lands in the `"unknown"` IV bucket. Every recommendation collapses to Bull Call Spread / Bear Put Spread / No Trade, and the entire strategy grid in the docstring is unreachable. The 14% weight you deliberately raised because "IV rank drives strategy selection" contributes exactly zero, and drags the composite toward zero (see §5).

**Fix:** put `IVSnapshot` on the shared `Base`, call `compute_iv_rank(session, ticker)` inside `compute_basket_tradeability`, and pass the result through. Also note your backfill stores 30-day realized vol as the historical proxy but today's value as true ATM IV — mixing the two in one min/max range is apples-to-oranges. IV is systematically above RV, so today's real IV will pin near the top of a proxy-built range and rank ~90 every day. Either backfill RV for today too, or rank IV against IV only once you have 60+ real days.

## 2. The trade setups price the stock, not the option

`options.py` produces an entry zone, target, stop and `rr_ratio` — all in underlying-price terms — for a position whose P&L is nonlinear in that price.

`generate_trade_setup` reports `rr_ratio = 2 ATR / 1.5 ATR ≈ 1.33` for, say, a 1-week ATM call. In reality: a 2 ATR favorable move in 5 days on an ATM weekly is roughly +150–300% on premium; hitting the 1.5 ATR stop is roughly −70–85%. The true R/R is nothing like 1.33, and the displayed number understates both sides by an order of magnitude. This is the most misleading output in the tool.

Worse, the strongest signals get the worst structure: `score > 0.5` → **1-week ATM long call**, i.e. maximum theta decay and the highest breakeven hurdle available. To profit you need the move *and* the timing *and* IV to not compress. A 30–45 DTE call or a debit spread has a materially better probability-weighted payoff for the same directional view.

Also missing entirely:

- **Liquidity screen.** No open interest, no bid/ask, no option volume. You already pull `option_chain` in `iv_rank.py` — `calls['bid']`, `['ask']`, `['openInterest']`, `['volume']` are right there. Reject any strike with OI < 100 or spread > 10% of mid.
- **Premium and max loss.** The tool never says what the trade costs or what you can lose. That's the first number a trader needs.
- **Strike validity.** `_round_strike` (`options.py:47`) forces $5 increments above $200. AAPL, SPY, NVDA trade $1 and $2.50 strikes. You'll suggest strikes that don't exist.
- **Expiry validity.** `_next_friday()` assumes a Friday expiry exists; ignores holidays and 0DTE/Mon-Wed weeklies.

**Fix:** make the chain the source of truth. Fetch it, filter for liquidity, pick strikes by delta (e.g. 0.30–0.40 for debit spreads) rather than by fixed OTM %, and quote entry cost / max loss / breakeven / P&L at the price target from actual bid-ask.

## 3. Two strategy engines that contradict each other

`tradeability.suggest_options_strategy` produces spreads, condors and cash-secured puts. `options._select_strategy` produces **naked calls and puts only**. Both run on every ticker; the dashboard displays the second (`app.py:973`). They disagree by construction — bullish + high IV rank gives "Cash-Secured Put" from one and "Long Call" from the other.

Pick one. Given §2, the `tradeability` version has the better logic (it at least respects IV regime) but the `options` version has the concrete strikes and levels. Merge them.

## 4. The backtest doesn't test the signal you trade

`_composite_score` (`backtest_incremental.py:150`) reconstructs **3 of 9 categories, 39% of the weight** — and uses *different* formulas than production. Compare the RSI maps:

| RSI | `tradeability.py` | `backtest_incremental.py` |
|---|---|---|
| < 25 | **+0.8** | +0.7 / +0.8 |
| 40–60 | **0.0** | **+0.3** |
| 60–70 | **+0.4** | **+0.1** |
| ≥ 80 | −0.3 | **−0.6** |

The 40–60 bucket flips from neutral to the *most common* positive contributor. Win rates and Sharpes on the Backtest page describe a signal that is not the one generating your recommendations.

Beyond that: forward returns are **stock** returns, so even a correct backtest wouldn't tell you about option P&L; no transaction costs or spread; universe is today's basket (survivorship bias); and `auto_adjust=True` on a 5-year pull means the 52-week high/low used at each date is computed from currently-known adjustment factors.

**Fix:** import the real `compute_tradeability` and drive it from historical snapshots, or accept that it's a momentum-only study and label it as such. Then add an option-level P&L layer: entry mid, exit mid at horizon, minus half-spread each way. And benchmark against "buy an ATM call on a random basket ticker" — that's the bar to clear, not zero.

## 5. Signal construction issues

**Zero-confidence categories still vote.** `effective_w = cat_weight * (0.3 + 0.7 * confidence)` (`tradeability.py:1067`). A category with no data returns score 0.0, confidence 0.0 — and still keeps 30% of its weight, pulling the composite toward zero. With `options_iv` (14%) always dead, plus frequently-missing `short_interest` (6%) and `fundamental` (8%), roughly 28% of weight is a constant zero anchor. This compresses the score distribution and is a large part of why so many tickers land in NEUTRAL. **Drop zero-confidence categories from the denominator instead.**

**No cross-sectional normalization.** Every threshold is absolute, and several are near-constant for large caps:

- `breakout_proximity` = `(pct_from_high + 0.20) / 0.25`. At the 52w high that's **+0.8**; you must be 20% below the high to score 0. Almost always positive.
- `analyst_rating`: sell-side is "buy" on most of the S&P → +0.6 for nearly everything.
- `analyst_mean_score` ≈ 2.0 typical → +0.5 for nearly everything.
- `52w_position`: positive for any stock in an uptrend.

A signal that says the same thing about every name carries no information. **Rank or z-score each sub-signal within the basket** before combining — that's the single highest-leverage change to the scoring layer.

**Non-monotonic RSI mapping.** RSI < 25 → +0.8 (mean reversion) and RSI 60–70 → +0.4 (momentum). Both tails bullish, only 40–60 neutral. You're running two incompatible theses through one variable; the composite can't distinguish "oversold bounce" from "trending up." Split them, or pick one and use RSI monotonically.

**RSI itself is unreliable here.** `calc_price_momentum_signal` seeds Wilder smoothing from a *single* observation (`gains[0]`) with only 14 data points and `alpha = 1/n` where `n = len(returns)`. Wilder's RSI needs ~100+ bars to converge. The comment claiming it "matches readings on TradingView, Bloomberg" is not true. Pull 120 days and compute it properly — it's the same yfinance call.

**Earnings signals contradict each other.** `calc_event_driven_signal` scores earnings-within-7-days as **+0.5** (bullish). `calc_earnings_proximity_signal` scores the same condition as **−0.5** (risk). The second function is **never called** — dead code. So the composite currently treats imminent earnings as a bullish signal, which for a long option is backwards (IV crush post-event). Also dead: `normalise()` (line 64).

**Sentiment labeling is asymmetric.** BULLISH at ≥ +0.3 but BEARISH at < −0.1 (`sentiment.py:20–25`, mirrored `app.py:128`). Systematic bearish-label bias with no stated rationale.

**No headline dedup.** Only URL-level dedup exists. A syndicated wire story appearing on Yahoo, MarketWatch and Reuters counts three times *and* inflates the `source_diversity` bonus that's supposed to reward independent confirmation. Hash the normalized headline.

## 6. The feedback loop is measuring noise

`scheduler.py` runs `job()` **every 15 minutes** → ~96 predictions per ticker per day, all resolving against the *same* next-day return. `n_observations` inflates ~96× faster than independent information arrives, so `confidence` (which scales to 1.0 at 30 obs) saturates within hours on a sample of essentially one day.

Compounding it, `resolve_pending_outcomes` (`feedback.py:161`) falls back to `dated[-1][1]` — the most recent return in the window — when no strictly-later trading day is found. That can resolve a prediction against a return from *before* it was made. Guaranteed spurious accuracy.

And the payoff isn't worth it: the adjustment is `±15% × confidence × multiplier`, clamped. On a score of +0.35 that's ±0.05 — rarely enough to cross a label boundary. It adds a feedback path and a failure mode for an effect you can't detect.

**Fix:** dedupe to one prediction per ticker per day; require 30+ *independent* observations before applying anything; drop the fallback branch; and move to a 5-day horizon (a 1-day horizon on a signal built from 7-day news and 14-day momentum is a mismatch). Honestly, consider deleting this and relying on a proper backtest instead.

## 7. Data-quality bugs that silently corrupt signals

**Earnings surprise is in the wrong units.** `yahoo.py:169` maps `surprise_pct = row.get("epsDifference")` — an absolute dollar difference, not a percentage. It's consumed as a percent: `surp_score = avg_surp / 10.0` (`tradeability.py:484`) and `result_score = surp / 15.0` (line 551). A $0.05 beat registers as 0.5% surprise; the fundamental and event categories are effectively pinned near zero. Divide by `epsEstimate`.

**Upcoming earnings dates never refresh.** Yahoo's calendar event is written with the literal `period="Upcoming"` (`yahoo.py:154`), and `_save_earnings` dedups on `(ticker, period)` and skips on match (`engine.py:440–447`). The first date ever fetched is frozen forever. Since `days_to_earn` drives the "Avoid — earnings in ≤3d" guardrail, **that guardrail is running on stale data.** Use the actual date as the period key, or upsert.

Also: earnings-history rows are stored with `report_date=None`, so `calc_event_driven_signal`'s "most recent past result" lookup filters them all out.

**Article writes can lose a whole batch.** `UniqueConstraint("source", "url")` plus `engine.py:402` writing `url=""` when absent → two URL-less articles from the same source in one run raise `IntegrityError` at the batch commit (line 409), discarding every article in that batch. Skip the constraint for empty URLs or generate a synthetic key.

**UTC vs market time.** `datetime.utcnow()` throughout, compared against ET trading dates. At 23:00 UTC, "earnings in 0 days" is actually tomorrow ET. Affects `days_to_earn`, the 7-day article cutoff, and the 20-hour resolve cutoff.

**Reuters isn't Reuters.** `config.yaml:56–62` `feed_urls` are all Dow Jones / WSJ / NYT / MarketWatch feeds — yet Reuters gets a 1.3× source weight for "editorial quality" and the Data Definitions page (`app.py:2782`) tells the user it's Reuters.

## 8. Engineering

- **Dashboard re-fetches ~40 yfinance calls per rerun.** `compute_basket_tradeability` (`app.py:759`) and `generate_basket_setups` (line 973) are uncached and each hit yfinance per ticker — and the weight sliders live on the same page, so *every slider drag* triggers the full round trip. Wrap both in `@st.cache_data(ttl=...)` keyed on the weight tuple. The sliders also pass both `value=` and `key=` (lines 730/733), which desyncs Streamlit state.
- **No migrations.** Only `create_all`. Every column added since the first deploy (`target_price`, `short_ratio`, `implied_volatility`, `beta`) is silently absent on existing DBs — masked by the `getattr(..., None)` pattern, which is how §1 went unnoticed. Add Alembic.
- **SQLite concurrency.** Module-global engine, `check_same_thread=False`, no WAL, no `busy_timeout`, shared between the Streamlit process and the scheduler. "Database is locked" is a matter of time. Enable WAL or move to Postgres.
- **`get_latest_scores`** (`database.py:233`) selects the entire `sentiment_scores` table and dedups in Python. At 96 runs/day this grows without bound. Use a window function or a `MAX(scored_at)` join.
- **Missing composite indexes.** The hot query shape is `WHERE ticker=? ORDER BY <ts> DESC LIMIT n`; you only have single-column indexes.
- **`_save_articles` is N+1** — one SELECT per article.
- **Secrets in `config.yaml`** in the repo — SMTP password, API keys, Slack webhook. Env vars only; the loader already supports them.
- **OpenAI scorer doesn't validate batch length** (`sentiment.py:174`). If GPT returns 18 scores for 20 headlines, `extend` misaligns every subsequent article and `all_scores[:len(texts)]` hides it. Assert length, retry on mismatch.
- **`build_scorer` catches only `ImportError`** (line 208) but `FinBERTScorer._load` raises `RuntimeError` — a model-download failure gets no VADER fallback.
- **`st.stop()` inside a try** (`app.py:346`) is caught by the `except Exception` at 517, masking the intended message.
- **Econ calendar month math** uses `timedelta(days=32 * offset)` (`app.py:1887`) — drifts ~19 days/year and skips a month after ~10 clicks. `month-2 or 11` (1755, 1774) is wrong for Jan/Feb.
- **Scheduler comment says "daily IV snapshot"** (line 89) but it runs every 15 minutes, pulling full option chains for all tickers — rate-limit bait once you fix §1.

## 9. Overfitting risk

Nine categories, ~25 sub-signals, ~40 hand-tuned thresholds, hand-set weights described as "calibrated" with no fitting procedure — against 19 tickers and a few months of live data. There is no realistic path to validating this many parameters at that sample size, and the backtest that would catch it tests a different model (§4).

Consider collapsing to 3–4 roughly orthogonal signals — momentum, IV regime, event proximity, and either sentiment or analyst revisions — with equal weights until you have out-of-sample evidence that differential weighting helps. Fewer parameters will very likely perform better out of sample, and it makes every remaining number auditable.

---

## Suggested order of work

1. Fix or delete IV rank (§1). It's the load-bearing input for strategy selection and it's returning nothing.
2. Fix the earnings unit bug and the stale-date dedup (§7). Cheap, and they're corrupting two categories plus your main safety guardrail.
3. Pull the real option chain: liquidity filter, premium, max loss, breakeven, delta-based strikes (§2). This is what turns the output from a stock opinion into a trade.
4. Replace stock-price R/R with option P&L (§2). The current `rr_ratio` is actively misleading.
5. Reconcile the two strategy engines (§3).
6. Rank signals cross-sectionally instead of by absolute thresholds (§5).
7. Make the backtest run the real composite with option-level P&L, or relabel it (§4).
8. Dedupe feedback to one prediction/ticker/day, or remove the subsystem (§6).
9. Cache the dashboard's yfinance calls; add Alembic; move secrets to env (§8).

---

*This is a code and methodology review, not investment advice, and nothing here is a judgment about whether any strategy will be profitable.*
