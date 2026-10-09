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

## v2 DECISIONS (2026-10-09 22:xx Lagos, after the user's review; supersede anything above that conflicts)
- Evaluation sub-baskets reported separately: CRYPTO = {BTC-USD, ETH-USD, SOL-USD}; EQUITY = {SPY, QQQ, IWM}; plus ALL 10. (SPY/QQQ/IWM are also highly correlated; noted.)
- Timing: forecast made after the daily close of bar T. Forecast return = mean predicted close of bar T+5 / mean predicted OPEN of bar T+1 - 1 (so it is measured from the price we would actually get).
  Real entry = open of the next bar (market) or a limit during bars T+1..T+2 (FVG books). Known lag: equities are forecast ~17 hours before their next open; crypto about 2 hours.
  Realized return for scoring = actual close of bar T+5 / actual open of bar T+1 - 1 (direction hit = sign agreement).
- Extra book F: buy-and-hold SPY, same $10,000 and start date. The most important comparison: if no Kronos book beats F after fees, the pipeline adds nothing.
- Costs per side: crypto market 10 bps, crypto limit (maker) 5 bps; equities/ETFs 1 bp commission + 1 bp slippage = 2 bps. FVG limit fills are SIMULATED from the real next bars
  (filled only if the bar's low reaches the limit; gap-down open fills at the open); no assumed 50% fill rate; the actual fill rate is reported. If a gate's edge needs 100% fills it has no edge.
- Trailing stop: peak = highest DAILY CLOSE since entry (stored, only moves up). Stop level = peak x 0.92, checked on daily closes, executed at the NEXT open (market, taker cost).
  Time exit: close of bar T+5 (market on close). Max 5 positions, 18% of equity each, at least 10% cash.
- XGBoost gate: one pooled model over all assets, features dimensionless and lagged (RSI14, ATR14/price, distance to EMA20 and EMA50 in ATR units, 5- and 20-day return, all computed through bar T),
  label = close(T+5)/close(T) > 0. Training window = rolling 2 years, refit monthly (window ends at the month start minus a 5-bar embargo so overlapping labels cannot leak). Trade only if P(up) > 0.60.
  The gate's OUT-OF-SAMPLE hit rate is logged for every asset-day; if it is not clearly above 50% it is reported as noise.
- FVG: bullish gap = bar low > high two bars earlier. Limit at the top of the nearest unfilled bullish gap below the last close (searched in the last 30 bars). No gap = no order (counted separately from MISSED).
- Primary metric: annualized Sharpe of each book's daily equity curve. Secondary: max drawdown, share of exits by trailing stop vs time exit, FVG fill rate. Win rate is reported but is not the target.
  Honest limit: over ~60 trading days the standard error of an annualized Sharpe is about 2, so Sharpe alone cannot judge anything yet; the verdict also uses the direction-accuracy interval and the paired difference (book minus A, book minus F).
- Runs once a day at 22:15 UTC (cron + manual dispatch). A run processes every bar since the last run, so a missed day heals itself (that day's forecast is lost and the gap is logged).

## 7. AGENT MANAGER (advisory only; added 2026-10-09 22:xx Lagos, before any Manager code)
7.1 Purpose. The Manager watches the six books, diagnoses why a gate helps or hurts, and writes PROPOSALS for the human to approve or reject. It never trades, never edits live parameters, never starts a book by itself.
7.2 Roles (one process, four steps): Observer (computes the report), Critic (cross-checks accepted vs rejected signals against raw outcomes), Reflector (one template-filled line per finding), Evolver (writes a Proposal with a falsifiable test).
7.3 Data access. The Manager may open raw logs (kronos_forecasts.json, kronos_state.json) for AUDIT and citation. It may draw conclusions ONLY from the report (kronos_report.json / .xlsx), and only for findings whose
    status is OK. Every reflection cites the report columns it used. A finding below its minimum sample is INSUFFICIENT_DATA; the Manager says nothing narrative about it.
7.4 Minimum samples (hard states, not warnings). Gate-level: >= 30 ACCEPTED and >= 30 REJECTED Kronos signals with realized outcomes. Regime-level (trend / chop / highvol): >= 15 + 15 inside that bucket.
    An under-threshold bucket prints "INSUFFICIENT_DATA in <regime>"; a pooled number is never substituted. Overlapping 5-day windows and cross-asset correlation make the effective sample smaller than n, so intervals use a
    5-date-block bootstrap, and the report prints n next to every statistic.
    Regime (fixed, computed from data through the signal bar only): HIGHVOL if ATR14/price is above its own trailing-250-bar 75th percentile; else TREND if |close - EMA50| >= 1 ATR; else CHOP.
7.5 Backward-only cutoff. A signal enters any statistic only if its 5-day OUTCOME had already happened by the as-of date (realized_date <= as_of), not merely if the forecast was made before it.
7.6 Proposal Acceptance Rule (option B, chosen by the user). A proposal becomes a real book only if ALL hold: (1) over the same 30-day forward window its Sharpe exceeds the control book's by >= 0.15;
    (2) the window starts on the proposal date and no earlier data is used to evaluate it; (3) the control book keeps running unchanged for the full window and beyond. Otherwise status = REJECTED_BY_FORWARD_TEST (it still counts toward N).
    Cohort calendar: all proposals made in month M share one forward window, the following calendar month. A failed proposal cannot be re-run with a new start date. If two proposals touch the same gate, the later one must state
    in its hypothesis why the earlier one's forward result does not apply. A proposal needs >= 20 daily curve points in its window or it is reported as INSUFFICIENT_DATA, not passed.
7.7 Ledger (kronos_ledger.json). Every proposal, passed or failed: proposal_id, date, gate, regime, change_description, control_book_id, test_book_id, window_start, window_end, forward_sharpe_gap, status, justification_vs_earlier_same_gate.
    Any result from a book descended from a proposal is reported as "selected from N proposals", with the raw Sharpe and N side by side, never the Sharpe alone. Deflated Sharpe is NOT used (option A not chosen).
7.8 The Manager may NOT: change anything live, add a new gate, or change fees, sizing, stop rules, horizon, universe or the entry threshold. Those are changes to the system, not proposals about a gate threshold, and need a new spec version.
7.9 Reflection format. Template fill only, numbers copied verbatim from the report: "Gate {X} rejected {N} signals with mean realized 5d return {Y}; accepted {M} with mean {Z}; gap {D} (95% interval {lo} to {hi}). Largest divergence in {regime}."
    Any LLM wording may only rephrase this line, and the template line plus its numbers are logged next to it.
7.10 Report columns (one row per gate x regime): as_of_date, gate, regime, n_accepted, n_rejected, mean_ret_accepted, mean_ret_rejected, gap, ci_low, ci_high, status, reflection, proposal_count_to_date.
    Plus sections: Books (Sharpe with its standard error, max drawdown, exits by reason, fills/missed/no-gap, difference vs A and vs F), Kronos accuracy (CRYPTO, EQUITY, ALL; Wilson interval; versus always-up), XGBoost out-of-sample hit rate, Ledger.
