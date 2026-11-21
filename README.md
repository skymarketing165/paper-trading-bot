# Paper Trading Bot

Runs **Trend Pullback (RSI-2)** on NASDAQ 100 and gold with a simulated $100,000 account.
No broker, no real money: every trade is simulated. It runs every 15 minutes on GitHub Actions,
so no computer needs to be on.

**Dashboard:** https://skymarketing165.github.io/paper-trading-bot/

## The strategy

Buys short, sharp dips inside a long-term uptrend and sells on the bounce.

1. Only trade in an uptrend: daily close above the 200-day average (buys only)
2. Buy when the 2-day RSI closes below 10; buy at the next day's open
3. Sell when the close is back above the 5-day average (at the next open), or after 10 days
4. Safety stop at 4× the daily ATR below the entry
5. Risk 0.5% of the starting balance per trade

### Why this strategy

`research.py` tested the earlier session-breakout strategy on ~2.5 years of hourly data: it **lost money**
(the 70% win rate from 60 days of data didn't hold up). `research_daily.py` tested this pullback strategy on
25 years of daily data, choosing settings on the older half and checking them on the newer half:

| Market | Trades | Win rate (older / newer half) | Profit factor (all) |
|---|---|---|---|
| NASDAQ 100 | 203 | 67.8% / 68.1% | 1.42 |
| Gold | 173 | 63.3% / 61.3% | 1.43 |

It trades rarely (about 7–8 times a year per market) and only when the market is in an uptrend.

## Safety switch (fully automatic)

New trades are paused automatically, with the reason shown on the dashboard, if:
- the paper account is down 8% from the start, or
- after 20+ live paper trades, the win rate drops below 50% or the profit factor below 1.0.

## Running it

- **Cloud:** `.github/workflows/paper-bot.yml` runs `bot.py --once` every 15 minutes and publishes `docs/`.
  Pause it from the repository's **Actions** tab → **Paper trading bot** → **⋯** → **Disable workflow**.
- **Locally:** `.venv/bin/python bot.py`, then open http://localhost:8765. Add `--reset` to start the account over.

## Good to know

- Data: Yahoo Finance daily candles for ^NDX (Nasdaq-100 index) and GC=F (gold futures).
- Fills at the next day's open with simulated costs (0.02% NASDAQ, 0.03% gold per side pair).
- The "Backfill" trades are a backtest of the last ~4 years; they don't count toward the paper balance.
