# Kronos system: DRAFT SPEC (paper only, nothing trades real money). Written before any code or data.
Built from the user's write-up: Kronos forecaster + TradingView-style confirmation gates + hard risk rules. NOTHING from the write-up is dropped;
every piece is built, and every piece is measured separately by running parallel paper books.

## Layer 1: Kronos forecaster (github.com/shiyu-coder/Kronos, MIT; weights NeoQuasar/Kronos-small on Hugging Face)
- Kronos-small (24.7M params), context 512 bars, tokenizer Kronos-Tokenizer-base. Input: open/high/low/close (+volume), timestamps.
- Note from the README: predict(sample_count=N) AVERAGES the N paths. To keep a distribution, call predict N=20 times with sample_count=1 and keep every path.
  Signal = mean and median of the 20 paths' forecast return over the horizon, plus the spread (std) as a confidence read.
- Use predict_batch() across the universe (all assets share lookback length and pred_len).
- Entry threshold: forecast mean return >= +1.0% over the horizon (user's example value). Long only at first (no shorting in paper v1).

## Layer 2: confirmation gates (each is a switch; none is tuned after seeing results)
- G1 FTFC (TheStrat full timeframe continuity): long allowed only if the current price is above the open of the current month, current week and current 3-day bar.
- G2 XGBoost meta-label: XGBoost classifier on the asset's own features (RSI14, ATR14/price, distance to EMA20/EMA50, 5- and 20-day return), trained walk-forward
  (retrained monthly on past data only) to predict whether the 5-day forward return is positive. Trade only if probability > 0.60.
- G3 Fair-value-gap entry: instead of buying at once, rest a limit order at the nearest bullish FVG (bar low > high two bars earlier). If price never trades down to it
  within 2 bars the order is cancelled and counted as MISSED (missed fills are reported, never hidden).

## Layer 3: execution and risk (paper)
- Max 5 positions, max 18% of equity per position, minimum 10% cash. Start $10,000 fake.
- Trailing stop 8% from the HIGH-WATER MARK; the peak price is saved in state and only ever moves up.
- Time exit: close at the forecast horizon end. Costs: 2 bps per side ETFs, 10 bps per side crypto.
- Run once per day after the US close on GitHub Actions; state committed with the robust save pattern (fetch+reset, retry 5x, verify the commit).

## Defaults I chose (change any of these before the build)
- Universe (fixed): BTC-USD, ETH-USD, SOL-USD, SPY, QQQ, IWM, GLD, TLT, IEF, EEM. Daily bars. Horizon 5 trading days.
- Five paper books run in parallel from the same Kronos signal: A Kronos only | B +G1 | C +G2 | D +G3 | E all three gates.
  Every forecast and every gate verdict is logged for every asset every day, so any combination can be re-tested later.

## How it is judged (fixed now)
- Direction accuracy of Kronos with a 95% Wilson interval, versus always-up and versus no-change, per asset class and overall. Overlapping 5-day horizons are
  correlated, so the effective sample is about days/5; no verdict before 60 trading days (about 12 independent 5-day periods per asset), and the report says plainly how wide the interval is.
- Paper P&L per book after costs; the gate's value = book minus book A, with a bootstrap interval. A gate that does not beat book A is reported as adding nothing.
- OPTIONAL quick look (flagged): a rolling historical run of Kronos on the same universe for 2024-2026. Kronos may have seen this data in training, so it can only embarrass the model, never validate it.
