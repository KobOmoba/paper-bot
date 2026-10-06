# Long-only comparison + two cuts: LOCKED READING (committed before any long-only number was computed)
Known before this run: trend overlay (same 13 markets, realistic costs) Sharpe 0.60, 95% CI [0.16, 1.04]. Nothing else about the comparisons has been seen.

Runs (same markets, join rules, start dates, month-end signal dates, next-day execution, costs, vol window 60, cap 2x as in TREND_SPEC.md):
  A  overlay (trend signal, long/short, vol-targeted)                         = already run
  B  long-only, vol-targeted (sign forced to +1; same sizing)                 = PRIMARY comparator (differs from A only by the signal)
  C  long-only, equal weight (1/k each live market, monthly rebalance)        = secondary comparator
  Cut 1: GLD removed from the universe (A, B, C all re-run, full period)
  Cut 2: only markets with first price on or before 2006-06-30 (SPY, EFA, EWJ, IEF, TLT, GLD, USO, EUR, GBP, USDJPY), window from 2005-01-01 (A, B, C re-run)
Delta = Sharpe(A) - Sharpe(B), same window. Also reported vs C. Paired 12-month-block bootstrap 95% CI on the delta (same resampled months for both).

Interpretation of the PRIMARY delta (A - B), point estimate, fixed now:
  delta < 0           overlay worse than buy-and-hold: not a hedge, not an edge. Walk away.
  0 <= delta < 0.15   trend signal adds nothing; 0.60 is beta. Dead for the purpose set.
  0.15 <= delta < 0.30  small genuine overlay value; modest, probably not worth the complexity vs passive.
  delta >= 0.30       real value added by the trend signal; still modest in absolute terms.
  If the delta's bootstrap CI includes 0, the sentence must say "not distinguishable from zero" whatever the point estimate says.
GLD cut (overlay Sharpe, full period vs 0.60): drop <= 0.05 minor; 0.05-0.15 moderate; >= 0.15 GLD was carrying a meaningful chunk.
No further runs, markets, filters, or tuning after seeing these. Closing the chapter after this.
