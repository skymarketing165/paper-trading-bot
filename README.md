# Paper Trading Bot: NASDAQ 100 Daily Pullback

Runs one high-win-rate NASDAQ 100 strategy with a simulated $100,000 account.
No broker, no real money: every trade is simulated. It runs every 15 minutes on GitHub Actions,
so no computer needs to be on.

**Dashboard:** https://skymarketing165.github.io/paper-trading-bot/

## The strategy

Buys dips while NASDAQ is in a long-term uptrend and sells on the bounce.

1. Only buy while NASDAQ's daily close is above its 200-day average
2. Buy at the next day's open when the 2-day RSI closes below 30 **and** the day closed in the bottom half of its range
3. Sell at the next open once RSI(2) rises above 70, or after 10 days
4. Safety stop at 6× the daily ATR below the entry
5. Put 25% of the starting balance into each trade, one trade at a time

### Position size and profit

The win rate doesn't depend on size; profit and drawdown do. 25-year test (2000–2026) and recent years:

| Size per trade | Per year (25 yrs) | Per year (2023–26) | Worst drawdown (25 yrs) |
|---|---|---|---|
| 8% (previous) | 0.5% | 1.2% | 2.3% |
| **25% (current)** | **1.7%** | **3.6%** | **7.0%** |
| 50% | 3.3% | 7.3% | 13.6% |
| 100% | 6.4% | 14.8% | 25.8% |

25% is the largest size whose worst historical drawdown stays inside the 8% safety switch.
To change it, edit `position_pct` in `bot.py`.

Expect about 1.5–2 trades a month, and none while NASDAQ is below its 200-day average.

## Evidence

| | Data | Trades | Win rate | Profit factor |
|---|---|---|---|---|
| Walk-forward test, older half | 2000–2013 | 186 | 66.7% | 1.40 |
| Walk-forward test, newer half | 2013–2026 | 238 | 75.6% | 1.80 |
| Bot replay | 2023–2026 | 78 | 76.9% | 2.21 |

How it got here (all scripts in this repo):
- `research.py`: the original session-breakout strategy lost money over 2.5 years of hourly data.
- `research_daily.py`: the pullback strategy held up over 25 years of daily data.
- `research_winrate.py`: buying only on closes near the day's low, exiting on RSI(2) > 70 and a 6× ATR stop
  raised the win rate in both halves of the data.
- An hourly version made ~7 trades a month but only won ~66%, so it was dropped in favour of win rate.

## Safety switch (fully automatic)

New trades pause on their own, with the reason shown on the dashboard, if:
- the paper account is down 8% from the start, or
- after 20+ live trades, the win rate drops below 50% or the profit factor below 1.0.

## Running it

- **Cloud:** `.github/workflows/paper-bot.yml` runs `bot.py --once` every 15 minutes and publishes `docs/`.
  Pause it from the repository's **Actions** tab → **Paper trading bot** → **⋯** → **Disable workflow**.
- **Locally:** `.venv/bin/python bot.py`, then open http://localhost:8765. Add `--reset` to start the account over.
