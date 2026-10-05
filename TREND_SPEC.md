# Trend-following backtest: LOCKED SPEC (written before any price data was pulled)
Question: is a modest, durable, retail-accessible trend edge present in free data? NOT: can small capital become big.
Backtest only. No bot, no orders. Result will be reported as a Sharpe with a confidence range, never pass/fail.

Universe (13, fixed, no additions/removals after seeing results):
  SPY, EFA, EWJ, IEF, TLT, GLD, USO, CPER, CORN, UNG, EURUSD=X, GBPUSD=X, USDJPY=X
  Bunds/Gilts dropped (no clean free proxy). Prices: Yahoo, auto_adjusted (ETF dividends included). FX = spot, no carry/swap included.
  Download from 2003-01-01. A market joins the basket only once it has 312 trading days of history (252 signal + 60 vol).
  UNG, CPER, CORN are KEPT and join late (shorter history, severe contango roll decay is a real cost inside the ETF).
Signal: at the last trading day of each month, close: trailing 252-trading-day return > 0 -> +1 (long), < 0 -> -1 (short).
Size: per-market weight = signal * min(2.0, 10% / trailing-60-day realized annualized vol), then divided by the number of live markets.
Timing: signal computed on month-end close; trade executes at the CLOSE of the next trading day (no same-day fills). Old weights earn that day.
Rebalance: monthly only. No stops, no targets, no filters. Lookback 252, vol window 60, vol target 10%, cap 2x are NOT tuned.
Costs (turnover-based, one-way): ETFs 2 bps spread; FX 0.75 pip (1.5 pip round trip); shorts on ETFs 0.5%/yr borrow, charged daily on short notional.
  Expense ratios are already inside ETF prices. No cash interest earned (returns are the overlay's own P&L). Zero-cost run reported as the diagnostic.
Report: Sharpe + 95% CI (analytic SE and 12-month-block bootstrap), zero-cost Sharpe, by period (2003-09, 2010-19, 2020-26),
  contribution and standalone Sharpe by market, max drawdown, longest drawdown (months), annualized vol, return scaled to 10% vol,
  and the same stats on the window where all 13 markets are live. Sample size (years) printed next to every Sharpe.
Not tested / not claimed: whether true Sharpe is 0.2 or 0.5; live survivability; alternatives.
