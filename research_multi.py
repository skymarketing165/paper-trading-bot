"""
More trades per month: test the SAME pullback rules on more markets, plus two variations.
Each market is judged separately on the older and newer half of its history (since 2000).

    .venv/bin/python research_multi.py
"""
import warnings

warnings.filterwarnings("ignore")
import research_daily as R  # noqa: E402

MARKETS = {
    # name: (ticker, cost % per round trip)
    "NASDAQ 100": ("^NDX", 0.02), "S&P 500": ("^GSPC", 0.02), "Dow Jones": ("^DJI", 0.03),
    "Russell 2000": ("^RUT", 0.04), "DAX 40": ("^GDAXI", 0.03), "FTSE 100": ("^FTSE", 0.03),
    "Nikkei 225": ("^N225", 0.04), "Gold": ("GC=F", 0.03), "Silver": ("SI=F", 0.06),
    "Crude oil": ("CL=F", 0.06), "EUR/USD": ("EURUSD=X", 0.01),
}

VARIANTS = {
    "base (RSI<10)": {"rsi_buy": 10, "exit_ma": 5, "max_days": 10, "stop_atr": 4.0},
    "looser (RSI<20)": {"rsi_buy": 20, "exit_ma": 5, "max_days": 10, "stop_atr": 4.0},
}


def short_simulate(df, p, cost):
    """Mirror image: sell short a spike (RSI>100-x) in a downtrend, cover on close below 5-day avg."""
    flipped = df.copy()
    for c in ("Open", "High", "Low", "Close", "sma200", "sma5", "sma10"):
        flipped[c] = 1e6 / df[c]   # invert prices: a downtrend becomes an uptrend
    flipped["High"], flipped["Low"] = 1e6 / df["Low"], 1e6 / df["High"]
    flipped["rsi2"] = 100 - df["rsi2"]
    flipped["atr"] = df["atr"] * flipped["Close"] / df["Close"]
    return R.simulate(flipped, p, cost)


def main():
    rows = []
    for name, (tk, cost) in MARKETS.items():
        try:
            df = R.load(tk)
        except Exception as e:
            print(name, "no data", e)
            continue
        mid = df.index[len(df) // 2]
        years = (df.index[-1] - df.index[0]).days / 365.25
        for vname, p in VARIANTS.items():
            for side, fn in (("long", R.simulate), ("short", short_simulate)):
                tr = fn(df, p, cost)
                a = R.summarize([t for t in tr if t["date"] < mid])
                b = R.summarize([t for t in tr if t["date"] >= mid])
                ok = a["n"] >= 20 and b["n"] >= 20 and a["pf"] > 1.1 and b["pf"] > 1.1 and a["win"] >= 60 and b["win"] >= 60
                rows.append((name, vname, side, round(len(tr) / years, 1), a, b, ok))
    print(f"{'market':<13} {'variant':<16} {'side':<5} {'/yr':>4} | {'OLDER HALF':<32} | {'NEWER HALF':<32} | pass")
    for name, vname, side, per_year, a, b, ok in rows:
        f = lambda s: f"n {s['n']:>3} win {s['win']:>5}% pf {s['pf']:>5} dd {s['dd']:>5}%"
        print(f"{name:<13} {vname:<16} {side:<5} {per_year:>4} | {f(a)} | {f(b)} | {'PASS' if ok else ''}")
    for vname in VARIANTS:
        for side in ("long", "short"):
            passed = [r for r in rows if r[1] == vname and r[2] == side and r[6]]
            print(f"\n{vname} {side}: {len(passed)} markets pass, about {sum(r[3] for r in passed):.0f} trades/year: "
                  + ", ".join(r[0] for r in passed))


if __name__ == "__main__":
    main()
