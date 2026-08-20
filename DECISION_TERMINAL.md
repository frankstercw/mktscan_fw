# MktScan Decision Terminal v2

This release collapses the dashboard into four primary workflows:

1. **Today** — market orientation, ranked opportunities, meaningful changes, and open-position warnings.
2. **Research** — one global ticker context with summary, chart, options, trade builder, ChatGPT research handoff, and advanced diagnostics.
3. **Portfolio** — open positions, risk concentration, trade logging/management, history, and trade review.
4. **Validation** — live performance, signal calibration, backtest summaries, and attribution.

## Design principles

- Conclusion first, diagnostics second.
- Semantic states beside raw values.
- One universal ticker selector.
- Expensive network calls are lazy: live charts, live options construction, and technical downloads only run in their relevant Research view.
- Today is primarily database-backed for speed.
- ChatGPT is treated as a qualitative/adversarial research layer, not as the quantitative signal engine.
- The visual system is inspired by modern trading terminals: dark canvas, compact cards, blue interaction accent, green/red directional semantics, and low-density scanner tables.

## ChatGPT Research

The Research page generates structured prompts from the current MktScan context. It does not require an OpenAI API key. Copy the generated prompt into ChatGPT for current-source research, thesis challenge, catalysts, risk review, peer comparison, or earnings analysis.

## Deployment

No database migration is required. Deploy `dashboard/app.py` with the rest of the current repository. Existing Railway dashboard/scheduler role separation remains unchanged.
