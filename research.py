"""
Strategy research: tests opening-range-breakout variations on ~2.5 years of HOURLY data.

Walk-forward check: settings are ranked on the first half of the history ("train")
and then judged on the second half they never saw ("test").

    .venv/bin/python research.py
"""
import itertools
import math
import warnings
from datetime import time as dtime
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

MARKETS = {
    # hourly bars: gold trades ~24h (bars on the hour), ^NDX is cash-session only (bars at :30)
    "GOLD":   {"ticker": "GC=F", "tz": "Europe/London",   "range": (dtime(8, 0), dtime(9, 0)),
               "entry_end": dtime(12, 0), "day_end": dtime(20, 0), "spread": 0.30},
    "NASDAQ": {"ticker": "^NDX", "tz": "America/New_York", "range": (dtime(9, 30), dtime(10, 30)),
               "entry_end": dtime(13, 30), "day_end": dtime(15, 30), "spread": 2.0},
}

GRID = {
    "rr":        [0.5, 0.75, 1.0, 1.5, 2.0],
    "stop":      ["range", "mid"],          # other side of range, or range midpoint
    "trend":     ["ema200", "ema50", "none"],
    "max_rng":   [3.0, 1.5],                # skip days whose range > x * ATR
}


def load(ticker):
    df = yf.Ticker(ticker).history(period="730d", interval="1h").dropna(subset=["Open", "High", "Low", "Close"])
    df.index = df.index.tz_convert("UTC")
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["ema200"] = df["Close"].ewm(span=200, adjust=False).mean()
    df["ema50"] = df["Close"].ewm(span=50, adjust=False).mean()
    return df


def simulate(df, mk, p):
    """Returns list of trades (dicts with day, r)."""
    tz = ZoneInfo(mk["tz"])
    loc = df.index.tz_convert(tz)
    df = df.assign(day=loc.date, t=loc.time)
    rs, re_ = mk["range"]
    half = mk["spread"] / 2
    trades = []
    for day, g in df.groupby("day", sort=True):
        rng = g[(g["t"] >= rs) & (g["t"] < re_)]
        if rng.empty:
            continue
        hi, lo = rng["High"].max(), rng["Low"].min()
        atr = rng["atr"].iloc[-1]
        if not (atr > 0) or not (0.3 * atr <= hi - lo <= p["max_rng"] * atr):
            continue
        after = g[g["t"] >= re_]
        rows = list(after.itertuples())
        for i, b in enumerate(rows[:-1]):
            if b.t >= mk["entry_end"]:
                break
            if p["trend"] == "none":
                up = dn = True
            else:
                ema = getattr(b, p["trend"])
                up, dn = b.Close > ema, b.Close < ema
            side = 1 if (up and b.Close > hi) else -1 if (dn and b.Close < lo) else 0
            if not side:
                continue
            nxt = rows[i + 1]
            entry = nxt.Open + side * half
            stop = (lo if side > 0 else hi) if p["stop"] == "range" else (hi + lo) / 2
            dist = (entry - stop) * side
            if dist <= 0:
                break
            target = entry + side * dist * p["rr"]
            exit_px = None
            for c in rows[i + 1:]:
                if c.t >= mk["day_end"]:
                    exit_px = c.Open
                    break
                if (side > 0 and c.Low <= stop) or (side < 0 and c.High >= stop):
                    exit_px = min(stop, c.Open) if side > 0 else max(stop, c.Open)
                    break
                if (side > 0 and c.High >= target) or (side < 0 and c.Low <= target):
                    exit_px = max(target, c.Open) if side > 0 else min(target, c.Open)
                    break
            if exit_px is None:
                exit_px = rows[-1].Close
            r = ((exit_px - side * half) - entry) * side / dist
            trades.append({"day": day, "r": r})
            break  # one trade per day
    return trades


def summarize(trades):
    n = len(trades)
    if not n:
        return {"n": 0, "win": 0, "exp": 0, "pf": 0, "dd": 0, "total": 0}
    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    gl = -sum(r for r in rs if r <= 0)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return {"n": n, "win": round(len(wins) / n * 100, 1), "exp": round(sum(rs) / n, 3),
            "pf": round(sum(wins) / gl, 2) if gl else math.inf, "dd": round(dd, 1), "total": round(sum(rs), 1)}


def main():
    data = {k: load(m["ticker"]) for k, m in MARKETS.items()}
    rows = []
    keys = list(GRID)
    for combo in itertools.product(*GRID.values()):
        p = dict(zip(keys, combo))
        row = dict(p)
        all_train, all_test = [], []
        for k, mk in MARKETS.items():
            tr = simulate(data[k], mk, p)
            tr.sort(key=lambda x: x["day"])
            mid = data[k].index[len(data[k]) // 2].tz_convert(mk["tz"]).date()
            train = [t for t in tr if t["day"] < mid]
            test = [t for t in tr if t["day"] >= mid]
            row[k + "_train"] = summarize(train)
            row[k + "_test"] = summarize(test)
            all_train += train
            all_test += test
        row["train"] = summarize(all_train)
        row["test"] = summarize(all_test)
        rows.append(row)

    def line(r):
        tr, te = r["train"], r["test"]
        return (f"rr {r['rr']:<4} stop {r['stop']:<5} trend {r['trend']:<6} maxR {r['max_rng']:<3} | "
                f"TRAIN n {tr['n']:>3} win {tr['win']:>5}% exp {tr['exp']:>6}R pf {tr['pf']:>5} | "
                f"TEST n {te['n']:>3} win {te['win']:>5}% exp {te['exp']:>6}R pf {te['pf']:>5} dd {te['dd']:>4}R | "
                f"gold test {r['GOLD_test']['win']}%/{r['GOLD_test']['exp']}R  nas test {r['NASDAQ_test']['win']}%/{r['NASDAQ_test']['exp']}R")

    print("== Current live settings (rr 0.5, range stop, ema200, maxR 3) ==")
    for r in rows:
        if r["rr"] == 0.5 and r["stop"] == "range" and r["trend"] == "ema200" and r["max_rng"] == 3.0:
            print(line(r))
    print("\n== Top 10 by TRAIN expectancy (picked without looking at test) ==")
    for r in sorted(rows, key=lambda r: r["train"]["exp"], reverse=True)[:10]:
        print(line(r))
    print("\n== Settings with >=60% win rate in BOTH train and test, positive expectancy in both ==")
    good = [r for r in rows if r["train"]["win"] >= 60 and r["test"]["win"] >= 60 and r["train"]["exp"] > 0 and r["test"]["exp"] > 0]
    for r in sorted(good, key=lambda r: r["test"]["exp"], reverse=True):
        print(line(r))
    if not good:
        print("none")


if __name__ == "__main__":
    main()
