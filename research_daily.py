"""
Strategy research on DAILY data (20+ years): pullback-in-uptrend (RSI-2 mean reversion).

Rules tested:
  - Uptrend filter: close above its 200-day average (long only)
  - Entry: RSI(2) closes below a threshold -> buy at the NEXT day's open
  - Exit: close above the 5-day average (sell next open), or after max_days, or at a 3x ATR safety stop
Walk-forward: ranked on the older half of history, judged on the newer half.

    .venv/bin/python research_daily.py
"""
import itertools
import warnings

warnings.filterwarnings("ignore")
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

MARKETS = {"NASDAQ": {"ticker": "^NDX", "cost_pct": 0.02}, "GOLD": {"ticker": "GC=F", "cost_pct": 0.03}}
GRID = {"rsi_buy": [5, 10, 15], "exit_ma": [5, 10], "max_days": [5, 10], "stop_atr": [3.0, None]}


def rsi(close, n):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def load(ticker):
    df = yf.Ticker(ticker).history(period="max", interval="1d").dropna(subset=["Open", "High", "Low", "Close"])
    df = df[df.index >= "2000-01-01"]
    df["sma200"] = df["Close"].rolling(200).mean()
    df["rsi2"] = rsi(df["Close"], 2)
    for n in (5, 10):
        df[f"sma{n}"] = df["Close"].rolling(n).mean()
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df.dropna()


def simulate(df, p, cost_pct):
    rows = list(df.itertuples())
    trades, i = [], 0
    while i < len(rows) - 1:
        b = rows[i]
        if b.Close > b.sma200 and b.rsi2 < p["rsi_buy"]:
            entry_bar = rows[i + 1]
            entry = entry_bar.Open
            stop = entry - p["stop_atr"] * b.atr if p["stop_atr"] else None
            exit_px, j = None, i + 1
            while j < len(rows):
                c = rows[j]
                if stop is not None and c.Low <= stop:
                    exit_px = min(stop, c.Open)
                    break
                held = j - (i + 1) + 1
                if c.Close > getattr(c, f"sma{p['exit_ma']}") or held >= p["max_days"]:
                    if j + 1 < len(rows):
                        exit_px = rows[j + 1].Open
                        j += 1
                    else:
                        exit_px = c.Close
                    break
                j += 1
            if exit_px is None:
                break
            ret = (exit_px / entry - 1) * 100 - cost_pct
            trades.append({"date": entry_bar.Index, "ret": ret, "days": j - i})
            i = j
        i += 1
    return trades


def summarize(tr):
    n = len(tr)
    if not n:
        return {"n": 0, "win": 0, "avg": 0, "pf": 0, "dd": 0}
    rets = [t["ret"] for t in tr]
    wins = [r for r in rets if r > 0]
    gl = -sum(r for r in rets if r <= 0)
    eq = peak = 1.0
    dd = 0.0
    for r in rets:
        eq *= 1 + r / 100
        peak = max(peak, eq)
        dd = max(dd, (peak - eq) / peak * 100)
    return {"n": n, "win": round(len(wins) / n * 100, 1), "avg": round(sum(rets) / n, 3),
            "pf": round(sum(wins) / gl, 2) if gl else 99, "dd": round(dd, 1)}


def main():
    data = {k: load(m["ticker"]) for k, m in MARKETS.items()}
    for k, df in data.items():
        print(f"{k}: {df.index[0].date()} -> {df.index[-1].date()} ({len(df)} days)")
    out = []
    for combo in itertools.product(*GRID.values()):
        p = dict(zip(GRID, combo))
        row = {"p": p}
        for k, m in MARKETS.items():
            tr = simulate(data[k], p, m["cost_pct"])
            mid = data[k].index[len(data[k]) // 2]
            row[k] = (summarize([t for t in tr if t["date"] < mid]), summarize([t for t in tr if t["date"] >= mid]))
        out.append(row)

    def fmt(s):
        return f"n {s['n']:>3} win {s['win']:>5}% avg {s['avg']:>6}% pf {s['pf']:>5} dd {s['dd']:>5}%"

    for k in MARKETS:
        print(f"\n== {k}: top 6 by TRAIN profit factor -> how they did on TEST ==")
        for r in sorted(out, key=lambda r: r[k][0]["pf"], reverse=True)[:6]:
            print(f"{r['p']} | TRAIN {fmt(r[k][0])} | TEST {fmt(r[k][1])}")


if __name__ == "__main__":
    main()
