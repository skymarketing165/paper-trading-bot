# Paper Trading Bot

Runs **Session Breakout v2** on gold and NASDAQ with a simulated $100,000 account.
No broker and no real money: every trade is simulated.

## Start it

```bash
cd paper_bot
.venv/bin/python bot.py
```

Then open **http://localhost:8765** for the dashboard. Keep the terminal (and your Mac) running:
the bot checks for new 15-minute candles every minute.

Start the paper account over from scratch with `--reset`.

## What the dashboard shows

- **Win rate**, wins/losses, profit factor, net P/L, max drawdown, paper balance
- **Strategy in use** and its rules (every trade in the log is tagged with the strategy that took it)
- Per-market stats for gold and NASDAQ, today's opening range, and whether a trade is about to trigger
- Open positions with live P/L, and the full trade log
- A toggle between **Paper trades since start** and **All trades**, which adds a backfill
  (a backtest of the last ~60 days that runs on first start)

## Good to know

- Prices come from Yahoo Finance gold (GC=F) and Nasdaq-100 (NQ=F) **futures**. They track XAUUSD/NAS100 closely
  but aren't identical, and the data may be delayed by a few minutes.
- Trades fill at the next candle's open, with a simulated spread (gold 0.30, NASDAQ 2.0).
  If a candle touches both the stop and the target, the bot counts it as a loss.
- The paper account's state is saved in `state.json`, so you can stop and restart the bot without losing anything.

## Running 24/7 in the cloud (GitHub Actions)

`.github/workflows/paper-bot.yml` runs the bot every 15 minutes on GitHub's servers, so your Mac can be off.
Each run processes the new candles, saves the paper account to `state.json`, and publishes the dashboard
to GitHub Pages (`docs/`). GitHub may delay scheduled runs by a few minutes. That doesn't change any fills,
because trades are simulated from the candle data.

To run it manually: GitHub repo → **Actions** → **Paper trading bot** → **Run workflow**.
To pause it: **Actions** → **Paper trading bot** → **⋯** → **Disable workflow**.
