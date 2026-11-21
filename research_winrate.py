"""
Win-rate research for the NASDAQ pullback strategies.

Tests well-known changes, each judged on the older and newer half of the data:
  Entry filter  - IBS (where the candle closed inside its range): only buy if it closed near the low
  Exit rule     - close > 5-candle avg (current) | RSI(2) > 70 | first up-close | either of the first two
  Safety stop   - 4x ATR (current) | none (time exit only)

    .venv/bin/python research_winrate.py
"""
import itertools
import warnings

warnings.filterwarnings("ignore")
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

import research_daily as R  # noqa: E402

GRID = {
    "ibs_max": [1.0, 0.5, 0.3],
    "exit": ["ma5", "rsi70", "upclose", "ma5_or_rsi70"],
    "stop_atr": [4.0, None],
}


def prep(df):
    df["rsi2"] = R.rsi(df["Close"], 2)
    df["sma5"] = df["Close"].rolling(5).mean()
    rng = (df["High"] - df["Low"]).replace(0, float("nan"))
    df["ibs"] = ((df["Close"] - df["Low"]) / rng).fillna(0.5)
    pc = df["Close"].shift(1)
    df["prev_close"] = pc
    df["atr"] = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()],
                          axis=1).max(axis=1).rolling(14).mean()
    return df


def load():
    t = yf.Ticker("^NDX")
    d = prep(t.history(period="max", interval="1d").dropna(subset=["Open", "High", "Low", "Close"]))
    d["sma200"] = d["Close"].rolling(200).mean()
    d["up"] = d["Close"] > d["sma200"]
    d = d[d.index >= "2000-01-01"].dropna()
    h = prep(t.history(period="730d", interval="1h").dropna(subset=["Open", "High", "Low", "Close"]))
    prev_up = pd.Series(d["up"].shift(1).values, index=[x.date() for x in d.index])
    h["up"] = [bool(prev_up.get(x.date(), False)) for x in h.index]
    return d, h.dropna()


def simulate(df, rsi_buy, max_bars, p, cost=0.02):
    rows = list(df.itertuples())
    trades, i = [], 0
    while i < len(rows) - 1:
        b = rows[i]
        if b.up and b.rsi2 < rsi_buy and b.ibs <= p["ibs_max"]:
            entry = rows[i + 1].Open
            stop = entry - p["stop_atr"] * b.atr if p["stop_atr"] else None
            exit_px, j = None, i + 1
            while j < len(rows):
                c = rows[j]
                if stop is not None and c.Low <= stop:
                    exit_px = min(stop, c.Open)
                    break
                held = j - i
                hit = {"ma5": c.Close > c.sma5, "rsi70": c.rsi2 > 70, "upclose": c.Close > c.prev_close,
                       "ma5_or_rsi70": c.Close > c.sma5 or c.rsi2 > 70}[p["exit"]]
                if hit or held >= max_bars:
                    if j + 1 < len(rows):
                        exit_px, j = rows[j + 1].Open, j + 1
                    else:
                        exit_px = c.Close
                    break
                j += 1
            if exit_px is None:
                break
            trades.append({"date": rows[i + 1].Index, "ret": (exit_px / entry - 1) * 100 - cost})
            i = j
        i += 1
    return trades


def main():
    d, h = load()
    print(f"daily {d.index[0].date()} -> {d.index[-1].date()}, hourly {h.index[0].date()} -> {h.index[-1].date()}")
    f = lambda s: f"n {s['n']:>3} win {s['win']:>5}% avg {s['avg']:>6}% pf {s['pf']:>5}"
    for name, df, rsi_buy, max_bars in (("DAILY (RSI<30)", d, 30, 10), ("HOURLY (RSI<10)", h, 10, 14)):
        mid = df.index[len(df) // 2]
        print(f"\n== {name} ==")
        rows = []
        for combo in itertools.product(*GRID.values()):
            p = dict(zip(GRID, combo))
            tr = simulate(df, rsi_buy, max_bars, p)
            a = R.summarize([t for t in tr if t["date"] < mid])
            b = R.summarize([t for t in tr if t["date"] >= mid])
            rows.append((p, a, b))
        rows.sort(key=lambda r: min(r[1]["win"], r[2]["win"]), reverse=True)
        for p, a, b in rows:
            cur = " <- current" if p == {"ibs_max": 1.0, "exit": "ma5", "stop_atr": 4.0} else ""
            print(f"ibs<={p['ibs_max']:<3} exit {p['exit']:<12} stop {str(p['stop_atr']):<4} | OLD {f(a)} | NEW {f(b)}{cur}")


if __name__ == "__main__":
    main()
