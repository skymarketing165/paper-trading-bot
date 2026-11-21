"""
Paper Trading Bot - Trend Pullback (RSI-2) on 6 markets (US indices, DAX, gold)

Simulated trades only. No broker, no real money.
- Daily candles from Yahoo Finance (index levels and gold futures)
- Strategy chosen from 25 years of walk-forward testing (see research_daily.py)
- Keeps a paper account in state.json; backfills ~4 years of trades on first run (marked "Backfill")
- Safety switch pauses new trades automatically if live results fall apart
- Serves a dashboard at http://localhost:8765 (local) or writes docs/data.json (GitHub Pages)

Usage:
    .venv/bin/python bot.py                    # run locally with dashboard
    .venv/bin/python bot.py --reset            # wipe the paper account and start over
    python bot.py --once --export docs/data.json   # one cycle (used by GitHub Actions)
"""
import argparse
import json
import os
import threading
import time
import warnings
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
DASHBOARD_FILE = os.path.join(HERE, "dashboard.html")
PORT = 8765
POLL_SECONDS = 300
STATE_VERSION = 4

STRATEGY = {
    "name": "Trend Pullback (RSI-2)",
    "summary": "Buys short dips inside a long-term uptrend, sells on the bounce",
    "rules": [
        "Only trade in an uptrend: daily close above its 200-day average (buys only)",
        "Buy signal: 2-day RSI closes below 20 (a short-term dip); buy at the next day's open",
        "Sell when the close is back above the 5-day average (sell at the next open)",
        "Time exit: sell after 10 days if it hasn't bounced",
        "Safety stop: 4x the daily ATR below the entry price",
        "Risk 0.5% of the starting balance per trade (position size set by the safety stop)",
        "Trades 6 markets (NASDAQ 100, S&P 500, Dow, Russell 2000, DAX, gold); max 4 open trades at once",
        "Safety switch: pauses new trades at -8% drawdown, or if after 20+ live trades the win rate "
        "drops below 50% or the profit factor below 1.0",
    ],
    "evidence": "25-year walk-forward test: 63-76% wins per market in both halves, about 80 trades a year across the 6 markets",
}

SETTINGS = {
    "initial_balance": 100000.0,
    "risk_pct": 0.5,
    "rsi_len": 2,
    "rsi_buy": 20,
    "trend_ma": 200,
    "exit_ma": 5,
    "max_days": 10,
    "stop_atr": 4.0,
    "max_open": 4,
    "max_drawdown_pct": 8.0,
    "check_after_trades": 20,
    "min_win_rate": 50.0,
    "min_profit_factor": 1.0,
}

MARKETS = {
    "NASDAQ": {"label": "NASDAQ 100", "ticker": "^NDX", "tz": "America/New_York", "cost_pct": 0.02},
    "SP500": {"label": "S&P 500", "ticker": "^GSPC", "tz": "America/New_York", "cost_pct": 0.02},
    "DOW": {"label": "Dow Jones (US30)", "ticker": "^DJI", "tz": "America/New_York", "cost_pct": 0.03},
    "RUSSELL": {"label": "Russell 2000", "ticker": "^RUT", "tz": "America/New_York", "cost_pct": 0.04},
    "DAX": {"label": "DAX 40 (GER40)", "ticker": "^GDAXI", "tz": "Europe/Berlin", "cost_pct": 0.03},
    "GOLD": {"label": "Gold", "ticker": "GC=F", "tz": "America/New_York", "cost_pct": 0.03},
}

LOCK = threading.Lock()
STATUS = {"last_check": None, "last_error": None, "next_check": None}


# ---------------------------------------------------------------- state
def new_state():
    return {
        "version": STATE_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "initial_balance": SETTINGS["initial_balance"],
        "markets": {k: {"last_done": None, "position": None, "pending_entry": None,
                        "pending_exit": None, "last_exit_date": None, "info": {}} for k in MARKETS},
        "trades": [],
        "halted": None,
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get("version") == STATE_VERSION:
            return s
        print("Old strategy state found: starting a new paper account for the new strategy.", flush=True)
    return new_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- data
def rsi(close, n):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def fetch(ticker):
    df = yf.Ticker(ticker).history(period="5y", interval="1d").dropna(subset=["Open", "High", "Low", "Close"])
    if len(df) < 300:
        raise RuntimeError(f"Not enough data for {ticker}")
    df["trend_ma"] = df["Close"].rolling(SETTINGS["trend_ma"]).mean()
    df["exit_ma"] = df["Close"].rolling(SETTINGS["exit_ma"]).mean()
    df["rsi"] = rsi(df["Close"], SETTINGS["rsi_len"])
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    df.index = [d.date().isoformat() for d in df.index]
    return df


# ---------------------------------------------------------------- engine
def mode_for(state, date_iso):
    return "paper" if date_iso >= state["started_at"][:10] else "backtest"


def balance(state, mode="paper"):
    return state["initial_balance"] + sum(t["pnl"] for t in state["trades"] if t["mode"] == mode)


def safety_check(state, mode):
    """Returns a reason string if new trades should be paused, else None."""
    init = state["initial_balance"]
    if balance(state, mode) <= init * (1 - SETTINGS["max_drawdown_pct"] / 100):
        return f"Account down {SETTINGS['max_drawdown_pct']}% from start"
    if mode != "paper":
        return None
    live = [t for t in state["trades"] if t["mode"] == "paper"]
    if len(live) >= SETTINGS["check_after_trades"]:
        s = stats(live, init)
        if s["win_rate"] < SETTINGS["min_win_rate"]:
            return f"Live win rate {s['win_rate']}% is below {SETTINGS['min_win_rate']}%"
        if s["profit_factor"] is not None and s["profit_factor"] < SETTINGS["min_profit_factor"]:
            return f"Live profit factor {s['profit_factor']} is below {SETTINGS['min_profit_factor']}"
    return None


def open_position(state, key, mk, ms, date_iso, open_px):
    p = ms.pop("pending_entry")
    ms["pending_entry"] = None
    entry = open_px * (1 + mk["cost_pct"] / 200)
    stop_dist = SETTINGS["stop_atr"] * p["atr"]
    risk = state["initial_balance"] * SETTINGS["risk_pct"] / 100
    ms["position"] = {"side": "LONG", "entry": entry, "entry_date": date_iso, "signal_date": p["date"],
                      "stop": entry - stop_dist, "qty": risk / stop_dist, "held": 0,
                      "rsi_at_signal": p["rsi"]}


def close_position(state, key, mk, ms, date_iso, px, reason):
    pos = ms["position"]
    exit_px = px * (1 - mk["cost_pct"] / 200)
    pnl = (exit_px - pos["entry"]) * pos["qty"]
    risk = state["initial_balance"] * SETTINGS["risk_pct"] / 100
    state["trades"].append({
        "id": f"{key}-{pos['entry_date']}", "market": key, "label": mk["label"],
        "strategy": STRATEGY["name"], "mode": mode_for(state, pos["entry_date"]), "side": pos["side"],
        "signal": f"RSI(2) {pos['rsi_at_signal']:.1f} < {SETTINGS['rsi_buy']} in uptrend",
        "entry_time": pos["entry_date"], "entry": round(pos["entry"], 2), "stop": round(pos["stop"], 2),
        "qty": round(pos["qty"], 3), "exit_time": date_iso, "exit": round(exit_px, 2), "reason": reason,
        "days": pos["held"], "pnl": round(pnl, 2), "r": round(pnl / risk, 2),
        "ret_pct": round((exit_px / pos["entry"] - 1) * 100, 2),
    })
    ms["position"] = None
    ms["pending_exit"] = None
    ms["last_exit_date"] = date_iso


def process_day(state, key, mk, ms, d, bar):
    """One COMPLETED daily candle."""
    if ms["pending_entry"] and not ms["position"]:
        open_position(state, key, mk, ms, d, bar["Open"])
    if ms["pending_exit"] and ms["position"] and ms["position"]["entry_date"] < d:
        close_position(state, key, mk, ms, d, bar["Open"], ms["pending_exit"])

    pos = ms["position"]
    if pos:
        pos["held"] += 1
        if bar["Low"] <= pos["stop"]:
            close_position(state, key, mk, ms, d, min(pos["stop"], bar["Open"]), "Safety stop")
        elif not ms["pending_exit"]:
            if bar["Close"] > bar["exit_ma"]:
                ms["pending_exit"] = "Bounce (close > 5-day avg)"
            elif pos["held"] >= SETTINGS["max_days"]:
                ms["pending_exit"] = f"Time exit ({SETTINGS['max_days']} days)"
        return

    if ms["pending_entry"] or ms["last_exit_date"] == d:
        return
    mode = mode_for(state, d)
    pause = safety_check(state, mode)
    if mode == "paper":
        state["halted"] = pause
    if pause:
        return
    busy = sum(1 for m in state["markets"].values() if m["position"] or m["pending_entry"])
    if busy >= SETTINGS["max_open"]:
        return
    if bar["Close"] > bar["trend_ma"] and bar["rsi"] < SETTINGS["rsi_buy"]:
        ms["pending_entry"] = {"date": d, "atr": float(bar["atr"]), "rsi": float(bar["rsi"])}


def run_cycle(state):
    frames = {key: fetch(mk["ticker"]) for key, mk in MARKETS.items()}
    today = {key: datetime.now(ZoneInfo(mk["tz"])).date().isoformat() for key, mk in MARKETS.items()}
    with LOCK:
        # Walk all markets day by day together, so the max-open-trades cap works across markets
        dates = sorted(set().union(*[set(df.index) for df in frames.values()]))
        for d in dates:
            for key, mk in MARKETS.items():
                df, ms = frames[key], state["markets"][key]
                if d not in df.index or d < df.index[SETTINGS["trend_ma"] + 5]:
                    continue
                if (ms["last_done"] and d <= ms["last_done"]) or d >= today[key]:
                    continue    # already processed, or today's candle is still forming
                process_day(state, key, mk, ms, d, df.loc[d])
                ms["last_done"] = d
        for key, mk in MARKETS.items():
            df, ms = frames[key], state["markets"][key]
            # Today's open is already known: fill pending orders at it right away
            if df.index[-1] >= today[key]:
                d, bar = df.index[-1], df.iloc[-1]
                if ms["pending_entry"] and not ms["position"]:
                    open_position(state, key, mk, ms, d, bar["Open"])
                elif ms["pending_exit"] and ms["position"] and ms["position"]["entry_date"] < d:
                    close_position(state, key, mk, ms, d, bar["Open"], ms["pending_exit"])
            last = df.iloc[-1]
            done = df[df.index < today[key]].iloc[-1]
            ms["info"] = {
                "price": round(float(last["Close"]), 2), "price_date": df.index[-1],
                "rsi": round(float(done["rsi"]), 1), "trend_ma": round(float(done["trend_ma"]), 2),
                "uptrend": bool(done["Close"] > done["trend_ma"]), "exit_ma": round(float(done["exit_ma"]), 2),
                "as_of": df[df.index < today[key]].index[-1],
            }
        save_state(state)


# ---------------------------------------------------------------- stats
def stats(trades, initial):
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    eq, peak, max_dd = initial, initial, 0.0
    for t in sorted(trades, key=lambda x: x["exit_time"]):
        eq += t["pnl"]
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak * 100)
    return {
        "trades": n, "wins": len(wins), "losses": n - len(wins),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "net_pnl": round(gross_win - gross_loss, 2),
        "avg_r": round(sum(t["r"] for t in trades) / n, 2) if n else None,
        "max_dd_pct": round(max_dd, 2),
    }


def snapshot():
    with LOCK:
        state = json.loads(json.dumps(STATE))
    trades, init = state["trades"], state["initial_balance"]
    out = {"strategy": STRATEGY, "settings": SETTINGS, "started_at": state["started_at"],
           "halted": state["halted"], "status": STATUS, "balance": round(balance(state), 2),
           "initial_balance": init, "views": {}, "positions": [], "markets": {}}
    for view, sel in (("paper", lambda t: t["mode"] == "paper"), ("all", lambda t: True)):
        ts = [t for t in trades if sel(t)]
        out["views"][view] = {"overall": stats(ts, init),
                              "by_market": {k: stats([t for t in ts if t["market"] == k], init) for k in MARKETS}}
    for key, ms in state["markets"].items():
        mk, info = MARKETS[key], ms.get("info") or {}
        if ms["position"]:
            status = "In a trade"
        elif ms["pending_entry"]:
            status = "Buy signal: buying at next open"
        elif not info:
            status = "Waiting for data"
        elif not info["uptrend"]:
            status = "No trades: below 200-day average"
        else:
            status = f"Waiting for a dip (RSI below {SETTINGS['rsi_buy']})"
        out["markets"][key] = {
            "label": mk["label"], "status": status,
            "details": [
                ["Last price", f"{info.get('price', '—')} ({info.get('price_date', '—')})"],
                ["Trend", ("Uptrend" if info.get("uptrend") else "Downtrend") + f" vs 200-day avg {info.get('trend_ma', '—')}"],
                ["RSI(2)", f"{info.get('rsi', '—')} (buy below {SETTINGS['rsi_buy']})"],
                ["5-day avg (exit level)", info.get("exit_ma", "—")],
                ["Data as of", info.get("as_of", "—")],
            ]}
        if ms["position"]:
            p = dict(ms["position"], market=key, label=mk["label"], strategy=STRATEGY["name"],
                     pending_exit=ms["pending_exit"])
            if info.get("price"):
                p["open_pnl"] = round((info["price"] - p["entry"]) * p["qty"], 2)
            out["positions"].append(p)
    out["trades"] = sorted(trades, key=lambda t: t["entry_time"], reverse=True)
    return out


# ---------------------------------------------------------------- web
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/state"):
            self._send(200, json.dumps(snapshot()).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            with open(DASHBOARD_FILE, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        else:
            self._send(404, b"not found", "text/plain")


def main():
    global STATE
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="wipe the paper account")
    ap.add_argument("--once", action="store_true", help="run one update and exit")
    ap.add_argument("--export", help="also write the dashboard data to this JSON file (for GitHub Pages)")
    ap.add_argument("--interval", type=int, default=POLL_SECONDS, help="seconds between checks (shown on dashboard)")
    args = ap.parse_args()
    if args.reset and os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)
    STATE = load_state()

    if not args.once:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"Dashboard: http://localhost:{PORT}", flush=True)

    while True:
        try:
            run_cycle(STATE)
            STATUS["last_error"] = None
        except Exception as e:  # keep running on network hiccups
            STATUS["last_error"] = str(e)
            print("Update failed:", e, flush=True)
        STATUS["last_check"] = datetime.now(timezone.utc).isoformat()
        STATUS["next_check"] = (datetime.now(timezone.utc) + timedelta(seconds=args.interval)).isoformat()
        if args.export:
            os.makedirs(os.path.dirname(os.path.abspath(args.export)), exist_ok=True)
            with open(args.export, "w") as f:
                json.dump(snapshot(), f)
        s = snapshot()["views"]
        print(f"[{STATUS['last_check'][:19]}] balance {balance(STATE):,.2f} | paper trades "
              f"{s['paper']['overall']['trades']} | all trades {s['all']['overall']['trades']} "
              f"(win rate {s['all']['overall']['win_rate']}%) | paused: {STATE['halted']}", flush=True)
        if args.once:
            break
        time.sleep(args.interval)


STATE = None
if __name__ == "__main__":
    main()
