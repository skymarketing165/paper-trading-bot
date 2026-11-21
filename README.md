# Paper Trading Bot: NASDAQ 100

Runs two NASDAQ 100 pullback strategies side by side with a simulated $100,000 account.
No broker, no real money: every trade is simulated. It runs every 15 minutes on GitHub Actions,
so no computer needs to be on.

**Dashboard:** https://skymarketing165.github.io/paper-trading-bot/

## The strategies

Both buy dips while NASDAQ is in a long-term uptrend (last daily close above the 200-day average) and sell on the bounce.

| | Daily Pullback | Hourly Pullback |
|---|---|---|
| Candles | Daily | Hourly |
| Buy when | 2-day RSI < 30 and the day closed in the bottom half of its range | 2-hour RSI < 10 |
| Sell when | RSI(2) rises above 70, or after 10 days | close > 5-hour average, or after 14 hours |
| Safety stop | 6× daily ATR | 6× hourly ATR |
| Expected trades | ~1.5–2 a month | ~7 a month |

Risk is 0.75% of the starting balance per trade, with one open trade per strategy. Orders fill at the next candle's open
with simulated costs.

## Evidence

| | Data tested | Win rate | Profit factor |
|---|---|---|---|
| Daily Pullback | 25 years (research_winrate.py) | 66.7% older half / 75.6% newer half | 1.40 / 1.80 |
| Daily Pullback, bot replay | 2023–2026, 78 trades | 76.9% | 2.21 |
| Hourly Pullback | ~3 years of hourly data | 67.0% / 66.4% | 1.20 / 1.51 |
| Hourly Pullback, bot replay | 2024–2026, 248 trades | 66.5% | 1.14 |

`research_winrate.py` tested entry filters, exits and stop widths. The daily strategy's win rate improved by buying only
when the day closed near its low and selling once RSI(2) rises above 70. A 6× ATR stop beat 4× for both strategies
(the tighter stop often sold right before the bounce). Nothing reliably raised the hourly strategy's win rate.

The hourly strategy has a much thinner edge and far less history. Watch its live paper results closely.
Earlier strategies that failed: the session breakout (`research.py`) lost money over 2.5 years, and short-selling
versions of the pullback lost money over the last 4 years.

## Safety switch (fully automatic)

Each strategy stops taking new trades on its own, with the reason shown on the dashboard, if:
- the paper account is down 8% from the start, or
- after 20+ live trades, that strategy's win rate drops below 50% or its profit factor below 1.0.

## Running it

- **Cloud:** `.github/workflows/paper-bot.yml` runs `bot.py --once` every 15 minutes and publishes `docs/`.
  Pause it from the repository's **Actions** tab → **Paper trading bot** → **⋯** → **Disable workflow**.
- **Locally:** `.venv/bin/python bot.py`, then open http://localhost:8765. Add `--reset` to start the account over.
