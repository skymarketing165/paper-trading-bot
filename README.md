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
| Buy when | 2-day RSI < 30 | 2-hour RSI < 10 |
| Sell when | close > 5-day average, or after 10 days | close > 5-hour average, or after 14 hours |
| Safety stop | 4× daily ATR | 4× hourly ATR |
| Expected trades | ~1.5–2 a month | ~7 a month |

Risk is 0.5% of the starting balance per trade, with one open trade per strategy. Orders fill at the next candle's open
with simulated costs.

## Evidence

| | Data tested | Win rate | Profit factor |
|---|---|---|---|
| Daily Pullback | 25 years (research_daily.py) | 65% older half / 75% newer half | 1.19 / 1.91 |
| Daily Pullback, bot replay | 2023–2026, 97 trades | 74.2% | 2.20 |
| Hourly Pullback | ~3 years of hourly data | 66–67% in both halves | 1.2–1.3 |
| Hourly Pullback, bot replay | 2024–2026, 253 trades | 66.0% | 1.09 |

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
