# Handoff for the next Claude (written 2026-10-03)

Everything below was checked against the repo on this date. Anything uncertain is marked UNVERIFIED.
**This repo is PUBLIC. Never write secrets into it.** The GitHub token lives in the Claude Project "Paper Bot" instructions,
not here. Telegram token/chat id are GitHub repo secrets `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.

## Who / what
- Owner: KobOmoba (user email in Project context), Nigeria, timezone Africa/Lagos (UTC+1). Repo: https://github.com/KobOmoba/paper-bot (branch `main`).
- Goal: a family of FAKE-money (paper) bots on free GitHub Actions with Telegram monitoring. User wants a financial bot with a real edge,
  validated on paper before any real money. User dislikes being brushed off, wants plain language, and wants me to defend my analysis with evidence.
- User has authorized pushing and running workflows on this repo. Do not keep asking them to revoke/rotate the token ("Stop stressing me").
- Session notes: sandbox network is allowlisted (Yahoo blocked; run yfinance work on Actions). Git push needs the repo attached via add_repo.
  `gh workflow run` fails (GraphQL blocked) -> use `gh api -X POST repos/KobOmoba/paper-bot/actions/workflows/<file>/dispatches -f ref=main`.
  Bots commit state files concurrently: always `git pull --rebase origin main` before `git push origin main`.

## Bots (files in repo)
| Bot | Code | Workflow (schedule) | State |
|---|---|---|---|
| Polymarket paper bot, fake $50, Bitcoin-threshold model + Brier scoreboard | `bot.py` | `bot.yml` (every 30 min) | `state.json` |
| NGX (Nigerian Exchange) 6-strategy paper bot | `ngx_bot.py`, `ngx_backfill.py` | `ngx.yml` (15:00 and 17:30 UTC Mon-Fri), `backfill.yml` (manual) | `ngx_state.json`, `ngx_history.json`, `ngx_raw.json` |
| Tokenized-stock QUARTERLY momentum paper tracker (97 US stocks; every 63 sessions hold top 10 by 126-session return; 1.5%/side cost; compared with SPY) | `us_live.py` | `us_live.yml` (21:30 UTC Mon-Fri) | `us_state.json` (schema `quarterly-v1`; first buys happen at the 2026-10-05 close) |
| Meme-token paper scanner, V5-1 rules (DexScreener) | `meme_scanner.py` | `meme.yml` (every 15 min) | `meme_state.json` |
| Meme scanner, ACCEL-1 "Acceleration Gate" variant (same file, `MEME_MODE=accel`, Solana only) | `meme_scanner.py` | `meme_accel.yml` (every 15 min) | `meme_accel_state.json` |
| Backtests (manual dispatch) | `us_backtest.py`, `mom_rebalance.py` | `us.yml`, `backtest.yml`, `mom.yml` | `*_report.json` |

State snapshot on 2026-10-03: Polymarket cash $37.91, 5 open, 3 settled, 30 model_log entries, 6 forecasts scored (scoreboard needs >=30 resolved).
NGX `last_date` 2026-10-02, 6 accounts. US tracker `last_date` 2026-10-02, 1 account. Meme scanner: 0 closed trades so far.

## Findings so far (do not overstate)
- Polymarket: earning pennies on wins vs. large loss when wrong ("pennies in front of a steamroller"). Bot now uses a Bitcoin lognormal model
  (Coinbase spot + 30-day vol x1.25) and asymmetric stake rules. Three older "NO at ~92c" positions may still be open. Edge is UNPROVEN until the scoreboard has >=30 resolved forecasts.
- Slow momentum test (`mom_report.json`, test period starts 2020-03-23): at stock-level cost (0.1%/side) monthly rebalance beat SPY (+400% vs +246%);
  at token-level cost (1.5%/side) monthly collapses (+117%) and quarterly roughly ties SPY (+258%). Max drawdown ~-54% to -58%. Survivorship bias flatters results.
  User has NOT yet decided whether to switch the live tracker to quarterly rebalance.
- Real token costs on Binance bStocks / Robinhood tokens are UNVERIFIED; the result hinges on them.

## Meme scanner rules in force (version tag `V5-1`, from the user's own message, 2026-10-03)
Entry (all must pass): age 1-4h; liquidity $8k-$40k; market cap < $300k; 1h volume >= 3x the prior hour; buys >= 1.5x sells.
Exits: hard stop -25% before Tier 1; sell 50% at 2x; sell 30% at 4x (peak resets at 4x); 20% moon bag with 12% trailing stop; if 2x not hit within 12h, exit all;
rug = liquidity drops >=60% in one scan step -> 30% payout. Costs: 3% (+impact) entry, 7% (+impact) exit, 1% fee each side.
My assumptions, NOT from the user: prior-hour volume is estimated from (h6 - h1) because DexScreener has no prior-hour figure; noise floors of $3k 1h volume and >=20 trades;
a 48h backstop exit (the rules set no stop between 2x and 4x). Limits: cannot block-0 snipe; 15-minute granularity; paper cannot model unsellable tokens.
The user's real "AariNAT Scanner" Telegram bot (V5 blueprint PDF: AWS co-location, Jito bundles, JIT blockhash, Fikra API fraud check) is separate from this repo and is not reproduced here; the blueprint holds placeholder keys only.

## ACCEL-1 variant (user's redesign, 2026-10-03; runs in parallel with V5-1 to compare)
User's reasoning: edge is catching the 15-90 minute "secondary wave", not picking good tokens. Entry: age 15-90 min; liquidity >= $15k; 15-minute volume
(difference between our own scans of DexScreener 24h volume) >= 2x the previous 15 minutes; mint and freeze authority revoked (Solana RPC `getAccountInfo`).
Dropped: market-cap cap, trade-count filter, buy/sell filter. Exits unchanged except exit-all if no 2x within 45 minutes.
Honest limits: "top wallet is not the bonding curve" is NOT implemented (no holder data). The claim that any meme token makes >5x before rugging is the user's belief, NOT verified.
Public Solana RPC may rate-limit or block Actions (funnel key `rpc_error` would show it; override with env `SOLANA_RPC`). Only tokens that appear in DexScreener latest-profiles/boosts are ever seen.
A token needs two scans before it can qualify, and 15-minute scans plus 45-minute deadline give coarse fills.

### ACCEL-1 data feeds (verified from GitHub Actions, 2026-10-03)
- GeckoTerminal `new_pools` (free, no key): WORKS. Candidates per run rose from ~33 to ~97. Counters `gt_ok`/`gt_fail` in `meme_accel_state.json`.
- Pump.fun `frontend-api.pump.fun/coins`: REFUSED from Actions (`pf_fail`). Do not rely on it.
- Public Solana RPC authority check: worked on first real use (one trade, SCAT, passed it). Under heavier load it may rate-limit (`rpc_error`).
  To use a keyed RPC (e.g. Helius free tier) the user must create a key and add the full URL as repo secret `SOLANA_RPC` (already wired in `meme_accel.yml`; empty secret falls back to the public RPC). Never commit the URL.
- First ACCEL paper trade: SCAT on Solana, opened 13:47 UTC, liquidity ~$32.7k, ~25 min old. One trade proves nothing.

### Scheduling change (2026-10-03)
GitHub's `*/15` cron ran only ~once per few hours (and ~35 min late), which breaks 15-minute scanning. Both meme workflows now run ONE long job
(`LOOP_MIN=55`, scan every `SCAN_SEC=300` s) started by a `*/30` cron; the concurrency group keeps one running + one queued job so scanning is near-continuous.
State is committed only at the end of each job (`if: always()` save step), so `meme*_state.json` updates about hourly, not every scan.
ACCEL volume velocity now uses windows: recent = volume since the newest reading >=13 min old; previous = the window before that (or lifetime average).
Helius: on 2026-10-03 the user's free Helius dashboard showed 1,000,000/1,000,000 credits used and "Service halted" (resets in ~24 days). Our scanner makes very few RPC calls,
so something else used that key (UNVERIFIED what). Code now falls back to the public Solana RPC if the keyed one fails. Consider a fresh key and never reusing one key across bots.

### Research logging added 2026-10-03 (no effect on trading)
- Every position now stores its observed `path` ([unix time, price, liquidity]) so alternative exit rules can be replayed on identical entries.
- ACCEL mode appends `meme_tracks.jsonl`: price/liquidity/24h-volume of young Solana tokens (first seen <=1.5h old, liquidity >=$5k) every scan until 3h old.
  Purpose: test the user's hypothesis that entering earlier would have worked better, and test exit variants (e.g. trailing stop after 2x), BEFORE changing live rules.
  A replay script (`replay.py`) is NOT written yet; write it once a few days of tracks exist. Treat one trade (SCAT) as an anecdote.
- SCAT facts (from state): entered 13:47 UTC; next scan 14:11 UTC saw 2.6x so half sold at 2.6x; rug caught 14:24 UTC; net +15%. The scheduling gap helped that exit (sold at 2.6x not 2.0x).

### 2-minute scans, entry context, launch price (2026-10-03)
- `SCAN_SEC=120` for both meme workflows (user asked for ~2 min). Data providers (DexScreener/GeckoTerminal) have their own indexing delay of unknown size (UNVERIFIED), so the scan period is not the only latency.
- Pump.fun probe removed (refused from Actions). Discovery for ACCEL = GeckoTerminal new_pools + DexScreener profiles/boosts + our own tracked tokens.
- Buy alerts now show: observed entry price and assumed fill price, age, market cap, liquidity, LAUNCH (first-trade) price from GeckoTerminal 1-minute candles,
  entry multiple vs launch, peak multiple so far, % below peak, 5m/1h price change, 5m/1h buys vs sells, and (ACCEL) 15-minute volume now vs before. Stored in each position's `ctx`.
- `meme_tracks.jsonl` entries carry `launch_price` and `first_trade_ts`. Launch price is only reliable for tokens under ~16h old (candle cap 1000 minutes); it is the first TRADE price, not the pool-creation price.

### PumpPortal probe (verified from GitHub Actions, 2026-10-03; result in `probe_pumpportal.json`, script `probe_pumpportal.py`, workflow `probe.yml`)
- `wss://pumpportal.fun/api/data` connects from Actions with no key. `{"method":"subscribeNewToken"}` streams Pump.fun token CREATE events in real time:
  28 creates in 45 s (~37/min), first event 0.3 s after connecting. Fields: mint, name, symbol, traderPublicKey (creator), initialBuy, solAmount, bondingCurveKey,
  vTokensInBondingCurve, vSolInBondingCurve (launch price in SOL = vSol / vTokens), marketCapSol, pool, signature.
- `subscribeTokenTrade` (per-token trades) is REFUSED without an API key funded with >= 0.02 SOL. We do not fund anything (paper only), so no free trade stream.
- NOT built yet: using this stream as the ACCEL discovery source. Design sketch: hold the websocket during the sleep between scans; persist births between jobs with actions/cache
  (state file would get too big); two-stage polling (cheap liquidity screen every ~5 min via DexScreener 30 mints/call, then a small hot list every scan). Awaiting user's go-ahead.

### BIRTH-1 scanner + scheduler fix (2026-10-03 evening)
- CRON FAILURE (my mistake): the `*/30` cron did not restart the loop jobs; nothing ran from 15:09 UTC until 17:50 UTC. GitHub's cron on this repo fires only every few hours
  (bot.yml, the 30-minute Polymarket cron, also runs only about every 5 hours). Fix: each meme loop job now ends by dispatching its own successor via
  `workflow_dispatch` with the built-in token (needs `permissions: actions: write`; dispatch events are exempt from the no-recursion rule). Never faster than once per 10 min. Cron stays as backup.
  Telegram now also gets an "alive:" line every ~3h per scanner.
- New mode `MEME_MODE=birth` (`meme_birth.yml`, state `meme_birth_state.json`, tag `[BIRTH]`, rules BIRTH-1): listens to PumpPortal `subscribeNewToken` for the whole job; every new Pump.fun
  token is stored (mint, birth time, launch price in SOL = vSol/vTokens, creator, creator's first buy, launch mcap) in `meme_births.json` (cached between jobs with actions/cache, NOT in git).
  Stage 1: every ~5 min look up tokens 10-100 min old on DexScreener (30 mints per call, max 1500 per scan). Stage 2: tokens with liquidity >= $8k ("hot") are polled every scan.
  Entry gates are the same as ACCEL (age 15-90 min using the TRUE birth time, liquidity >= $15k, 15-min volume >= 2x previous and >= $1k, mint+freeze authority revoked). Same exits, 45-min deadline.
  Launch price in alerts is exact (bonding curve at creation, converted to USD with DexScreener's USD/SOL). Limits: tokens missed while no job is listening (handover gaps, seconds to minutes);
  per-token trade stream not available without a funded key; most tokens never get a DexScreener pair (looked up once per 5 min until 100 min old).
- Three experiments now run in parallel: V5-1 (`meme_state.json`), ACCEL-1 (`meme_accel_state.json`), BIRTH-1 (`meme_birth_state.json`).

### Polymarket bot scheduler (2026-10-03)
`bot.yml` now runs `bot.py` three times per job (0, 25, 50 min, committing `state.json` after each run) and the job dispatches its own successor, like the meme loops.
The old `*/30` cron only ran about every 5 hours.

### Diagnostic findings (2026-10-03 ~18:08 UTC; workflow `diag.yml`, output `diag_output.txt`)
- Telegram works from Actions. All three scanner modes run. The loop jobs are healthy but only commit state when a job ends (~55 min), so the repo looks quiet in between.
- V5-1 funnel in one scan: 48 tokens seen, 0 opened (30 failed liquidity, 16 failed age, 1 volume). Entry rules are strict, so alerts are rare by design; the `alive:` Telegram line (every ~3h) is the proof of life.
- GeckoTerminal returned HTTP 429 (rate limit) on launch-price lookups: `get_url` now spaces GeckoTerminal calls >= 2.3 s apart, retries once, and new tracks are capped at 3 per scan.
- BIRTH listener received 4 PumpPortal launches in ~3 s from Actions (works).
- `diag.yml` can be re-run any time (workflow_dispatch); it uses scratch state files (`MEME_STATE_FILE`) and does not send trade alerts.

## Open items
1. DONE 2026-10-03: `us_live.py` switched to quarterly rebalance (user approved). Watch the first rebalance after the 2026-10-05 close.
2. Watch both meme funnels (`meme_state.json`, `meme_accel_state.json`) and compare V5-1 vs ACCEL-1; report win rate and the 1x/2x/3x slippage stress lines.
3. Polymarket scoreboard review once >=30 resolved forecasts.
4. Cleanup option: 8 `__pycache__` files are tracked in git; `.gitignore` now blocks new ones.
