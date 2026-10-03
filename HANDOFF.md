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
| Tokenized-stock momentum paper tracker (97 US stocks) | `us_live.py` | `us_live.yml` (21:30 UTC Mon-Fri) | `us_state.json` |
| Meme-token paper scanner (DexScreener) | `meme_scanner.py` | `meme.yml` (every 15 min) | `meme_state.json` |
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

## Open items
1. Ask whether to switch `us_live.py` to quarterly rebalance.
2. Watch the meme funnel (`funnel` in `meme_state.json`) to see if V5 filters produce trades; report win rate and the 1x/2x/3x slippage stress lines.
3. Polymarket scoreboard review once >=30 resolved forecasts.
4. Cleanup option: 8 `__pycache__` files are tracked in git; `.gitignore` now blocks new ones.
