# Direction Engine v2

Implements the first three decision-engine recommendations:

1. **Directional Conviction Engine**
   - Independent signal families rather than indicator-count voting.
   - MktScan model, Macro, Trend, Momentum, Participation, Catalyst, Options.
   - 0–100 conviction score; coverage affects confidence rather than direction.

2. **Signal Agreement**
   - Counts non-neutral signal families aligned/opposed to the resolved direction.
   - HIGH / MODERATE / LOW agreement state.

3. **Divergence Engine**
   - Relative-strength divergence
   - Momentum divergence
   - Weak-volume / participation divergence
   - RSI extension / oversold risk
   - Analyst divergence
   - High-IV pricing risk
   - Elevated downside-skew warning
   - Cross-family disagreement

These outputs are displayed in Research → Summary above the existing setup scorecard.
No database migration is required.
