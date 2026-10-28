# Crypto trend test: LOCKED SPEC (written 2026-10-28 23:xx Lagos, before any new data was pulled)
Paper only. No exchange, no keys, no real orders.

## Why this test
`crypto_backtest_full_report.json` showed the "trend" rule at +255% (test 2024-02..2026-10) vs BTC hold +64%. Two problems make that number unsafe:
1. Survivorship: the 45 coins were picked TODAY (PEPE, BONK, FLOKI are in the list only because they survived and grew).
2. Sizing bug (found 2026-10-28): the engine buys whole units only (`int(...)`). With $100k and 10% per slot, one slot is $10k,
   so BTC could not be bought at all whenever its price was above the slot size (BTC was above $10k for most of 2020-2026).
   The result leaned on cheaper alt-coins.
This test removes both: BTC and ETH only (no survivor choice), fractional units.

## Rule (copied unchanged from ngx_bot.py e_trend / x_trend; NOT tuned)
- Daily closes (Yahoo BTC-USD / ETH-USD, UTC days). Today's unfinished bar is dropped.
- Entry: 20-day average crosses above the 50-day average TODAY (yesterday 20d <= 50d, today 20d > 50d) and close > 50-day average.
- Exit (checked each close while held): close <= entry price x 0.90 (stop) | close < 20-day average (trend) | held 120 days (time).
- After any exit, re-entry needs a NEW cross.
- Each coin has its own sub-wallet (equal split of the bank), all-in or all-cash. No leverage, no shorts.

## Runs (all fixed now)
- PRIMARY: BTC+ETH, two 50% sub-wallets, window 2018-01-01 to the last complete day. Fill at the signal-day close (= next open in 24/7 crypto),
  cost 0.4% per side (0.1% fee + 0.3% slippage, same as the old report).
- STRESS A: costs doubled to 0.8% per side.
- STRESS B: fill one full day late (close of the next day), base costs.
- Also reported (not used for the verdict): BTC alone from 2014-09-17; ETH alone from 2017-11-09; periods 2018-19, 2020-21, 2022, 2023-26.
- Comparator for every run: buy-and-hold of the same coins in the same sub-wallets, one entry cost, never rebalanced.

## Metrics
Total return, CAGR, Sharpe (daily returns x sqrt(365), zero risk-free rate), max drawdown, % of days invested, trades, win rate.
Paired 30-day-block bootstrap (2,000 resamples, seed 11) 95% interval on Sharpe(strategy) - Sharpe(hold).
Rolling 2-year windows (730 days, step 30): % of windows the strategy beats hold on return, and on Sharpe.

## Verdict (fixed now, applied to PRIMARY)
- PASS: Sharpe(strategy) >= Sharpe(hold) AND max drawdown at least 15 points smaller AND under BOTH stresses Sharpe(strategy) >= 0.9 x Sharpe(hold).
- WEAK: max drawdown at least 15 points smaller but a Sharpe condition fails. Meaning: it is mostly "holding less crypto", not an edge.
- FAIL: anything else.
- If the bootstrap interval on the Sharpe difference includes 0, the report must say "not distinguishable from holding" whatever the verdict.
- PASS or WEAK -> run the live paper bot (BTC+ETH, $1,000 fake) for at least 3 months before any real-money discussion. FAIL -> no live bot.
No further variants, coins, or parameter changes after seeing these numbers.
