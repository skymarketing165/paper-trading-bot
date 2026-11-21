"""
Paper Trading Bot - NASDAQ 100 Daily Pullback

Simulated trades only. No broker, no real money.
- Data: Yahoo Finance ^NDX (Nasdaq-100 index), daily and hourly candles
- Daily Pullback: ~1.5-2 trades/month, tested on 25 years (research_winrate.py)
  (an hourly version exists in STRATS history but was dropped: lower win rate, ~66%)
- Backfills history on first run (marked "Backfill"); later trades are "Paper"
- Safety switch pauses a strategy automatically if its live results fall apart

Usage:
    .venv/bin/python bot.py                        # run locally with dashboard on :8765
    .venv/bin/python bot.py --reset                # wipe the paper account and start over
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
STATE_VERSION = 8
TZ = ZoneInfo("America/New_York")
TICKER = "^NDX"

STRATEGY = {
    "name": "NASDAQ Daily Pullback",
    "summary": "Buys short dips in the NASDAQ 100 while it's in a long-term uptrend, sells on the bounce",
    "rules": [
        "Only buy while NASDAQ's last daily close is above its 200-day average (no trades in downtrends)",
        "Daily Pullback: buy at the next day's open when the 2-day RSI closes below 30 AND the day closed in the "
        "bottom half of its range; sell at the next open once RSI(2) rises above 70, or after 10 days",
        "Position size: 25% of the starting balance per trade (sized so the worst 25-year drawdown, 7%, stays "
        "inside the 8% safety limit); safety stop 6x ATR below entry; one trade at a time",
        "Safety switch: pauses new trades at -8% account drawdown, or if after 20+ live trades the win rate "
        "drops below 50% or profit factor below 1.0",
    ],
    "evidence": "25-year test: 67% / 76% wins (older / newer half); 77% wins over the last ~4 years",
}

SETTINGS = {
    "initial_balance": 100000.0,
    "position_pct": 25.0,
    "trend_ma": 200,
    "exit_ma": 5,
    "stop_atr": 6.0,
    "max_drawdown_pct": 8.0,
    "check_after_trades": 20,
    "min_win_rate": 50.0,
    "min_profit_factor": 1.0,
    "cost_pct": 0.02,
}

# key -> strategy settings
STRATS = {
    "DAILY": {"label": "Daily Pullback", "interval": "1d", "period": "5y", "rsi_buy": 30, "max_bars": 10,
              "ibs_max": 0.5, "exit": "rsi70", "unit": "days", "freq": "~1.5 trades/month"},
}

LOCK = threading.Lock()
STATUS = {"last_check": None, "last_error": None, "next_check": None}


# ---------------------------------------------------------------- state
def new_state():
    return {
        "version": STATE_VERSION,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "backfilled": False,
        "initial_balance": SETTINGS["initial_balance"],
        "strats": {k: {"last_done": None, "position": None, "pending_entry": None, "pending_exit": None,
                       "last_exit_bar": None, "halted": None, "info": {}} for k in STRATS},
        "trades": [],
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            s = json.load(f)
        if s.get("version") == STATE_VERSION:
            return s
        print("Older strategy state found: starting a new paper account.", flush=True)
    return new_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- data
def rsi(close, n=2):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def add_indicators(df):
    df["rsi"] = rsi(df["Close"])
    rng = (df["High"] - df["Low"]).replace(0, float("nan"))
    df["ibs"] = ((df["Close"] - df["Low"]) / rng).fillna(0.5)   # 0 = closed at the low, 1 = at the high
    df["exit_ma"] = df["Close"].rolling(SETTINGS["exit_ma"]).mean()
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()
    return df


def fetch_all():
    t = yf.Ticker(TICKER)
    daily = t.history(period="5y", interval="1d").dropna(subset=["Open", "High", "Low", "Close"])
    hourly = t.history(period="730d", interval="1h").dropna(subset=["Open", "High", "Low", "Close"])
    if len(daily) < 300 or len(hourly) < 300:
        raise RuntimeError("Not enough NASDAQ data from Yahoo Finance")

    daily = add_indicators(daily)
    daily["trend_ma"] = daily["Close"].rolling(SETTINGS["trend_ma"]).mean()
    daily["up"] = daily["Close"] > daily["trend_ma"]
    daily.index = [d.tz_convert(TZ).strftime("%Y-%m-%d") for d in daily.index]
    # trend as of the previous completed day, for each date
    prev_up = daily["up"].shift(1)

    hourly = add_indicators(hourly)
    hourly["start"] = [d.tz_convert(timezone.utc) for d in hourly.index]
    hourly.index = [d.tz_convert(TZ).strftime("%Y-%m-%d %H:%M") for d in hourly.index]
    hourly["up"] = [bool(prev_up.get(i[:10], False)) if pd.notna(prev_up.get(i[:10])) else False for i in hourly.index]
    daily["up"] = daily["up"].where(daily["trend_ma"].notna(), False)
    return {"DAILY": daily, "HOURLY": hourly}, daily


def completed(key, bar_id, bar, now):
    """True if the candle has closed."""
    today = now.astimezone(TZ).strftime("%Y-%m-%d")
    if bar_id[:10] < today:
        return True
    if key == "HOURLY":
        return bar["start"] + timedelta(hours=1) <= now
    return False


# ---------------------------------------------------------------- engine
def balance(state, mode="paper"):
    return state["initial_balance"] + sum(t["pnl"] for t in state["trades"] if t["mode"] == mode)


def safety_check(state, key, mode):
    init = state["initial_balance"]
    if balance(state, mode) <= init * (1 - SETTINGS["max_drawdown_pct"] / 100):
        return f"Account down {SETTINGS['max_drawdown_pct']}% from start"
    if mode != "paper":
        return None
    live = [t for t in state["trades"] if t["mode"] == "paper" and t["market"] == key]
    if len(live) >= SETTINGS["check_after_trades"]:
        s = stats(live, init)
        if s["win_rate"] < SETTINGS["min_win_rate"]:
            return f"Live win rate {s['win_rate']}% is below {SETTINGS['min_win_rate']}%"
        if s["profit_factor"] is not None and s["profit_factor"] < SETTINGS["min_profit_factor"]:
            return f"Live profit factor {s['profit_factor']} is below {SETTINGS['min_profit_factor']}"
    return None


def open_position(state, key, ss, bar_id, open_px):
    p = ss["pending_entry"]
    ss["pending_entry"] = None
    entry = open_px * (1 + SETTINGS["cost_pct"] / 200)
    stop_dist = SETTINGS["stop_atr"] * p["atr"]
    qty = state["initial_balance"] * SETTINGS["position_pct"] / 100 / entry
    ss["position"] = {"side": "LONG", "entry": entry, "entry_date": bar_id, "stop": entry - stop_dist,
                      "qty": qty, "risk_amt": qty * stop_dist, "held": 0, "rsi_at_signal": p["rsi"],
                      "mode": "paper" if state["backfilled"] else "backtest"}


def close_position(state, key, ss, bar_id, px, reason):
    pos, st = ss["position"], STRATS[key]
    exit_px = px * (1 - SETTINGS["cost_pct"] / 200)
    pnl = (exit_px - pos["entry"]) * pos["qty"]
    risk = pos["risk_amt"]
    state["trades"].append({
        "id": f"{key}-{pos['entry_date']}", "market": key, "label": f"NASDAQ 100 · {st['label']}",
        "strategy": st["label"], "mode": pos["mode"], "side": "LONG",
        "signal": f"RSI(2) {pos['rsi_at_signal']:.1f} < {st['rsi_buy']} in uptrend"
                  + (", closed near low" if st["ibs_max"] < 1 else ""),
        "entry_time": pos["entry_date"], "entry": round(pos["entry"], 2), "stop": round(pos["stop"], 2),
        "qty": round(pos["qty"], 4), "exit_time": bar_id, "exit": round(exit_px, 2), "reason": reason,
        "days": pos["held"], "pnl": round(pnl, 2), "r": round(pnl / risk, 2),
        "ret_pct": round((exit_px / pos["entry"] - 1) * 100, 2),
    })
    ss["position"] = None
    ss["pending_exit"] = None
    ss["last_exit_bar"] = bar_id


def process_bar(state, key, ss, bar_id, bar):
    """One COMPLETED candle."""
    st = STRATS[key]
    if ss["pending_entry"] and not ss["position"]:
        open_position(state, key, ss, bar_id, bar["Open"])
    if ss["pending_exit"] and ss["position"] and ss["position"]["entry_date"] < bar_id:
        close_position(state, key, ss, bar_id, bar["Open"], ss["pending_exit"])

    pos = ss["position"]
    if pos:
        pos["held"] += 1
        if bar["Low"] <= pos["stop"]:
            close_position(state, key, ss, bar_id, min(pos["stop"], bar["Open"]), "Safety stop")
        elif not ss["pending_exit"]:
            if st["exit"] == "rsi70" and bar["rsi"] > 70:
                ss["pending_exit"] = "Bounce (RSI(2) > 70)"
            elif st["exit"] == "ma5" and bar["Close"] > bar["exit_ma"]:
                ss["pending_exit"] = "Bounce (close > 5-candle avg)"
            elif pos["held"] >= st["max_bars"]:
                ss["pending_exit"] = f"Time exit ({st['max_bars']} {st['unit']})"
        return

    if ss["pending_entry"] or ss["last_exit_bar"] == bar_id:
        return
    mode = "paper" if state["backfilled"] else "backtest"
    pause = safety_check(state, key, mode)
    if mode == "paper":
        ss["halted"] = pause
    if pause:
        return
    if bar["up"] and bar["rsi"] < st["rsi_buy"] and bar["ibs"] <= st["ibs_max"]:
        ss["pending_entry"] = {"bar": bar_id, "atr": float(bar["atr"]), "rsi": float(bar["rsi"])}


def run_cycle(state):
    frames, daily = fetch_all()
    now = datetime.now(timezone.utc)
    with LOCK:
        for key in STRATS:
            df, ss = frames[key], state["strats"][key]
            start = df.index[SETTINGS["trend_ma"] + 5] if key == "DAILY" else df.index[30]
            last_bar = None
            for bar_id, bar in df.iterrows():
                if bar_id < start or (ss["last_done"] and bar_id <= ss["last_done"]):
                    continue
                if not completed(key, bar_id, bar, now):
                    last_bar = (bar_id, bar)
                    break
                process_bar(state, key, ss, bar_id, bar)
                ss["last_done"] = bar_id
            # The forming candle's open is already known: fill pending orders at it right away
            if last_bar:
                bar_id, bar = last_bar
                if ss["pending_entry"] and not ss["position"]:
                    open_position(state, key, ss, bar_id, bar["Open"])
                elif ss["pending_exit"] and ss["position"] and ss["position"]["entry_date"] < bar_id:
                    close_position(state, key, ss, bar_id, bar["Open"], ss["pending_exit"])
            done = df.loc[ss["last_done"]] if ss["last_done"] in df.index else df.iloc[-1]
            ss["info"] = {"rsi": round(float(done["rsi"]), 1), "exit_ma": round(float(done["exit_ma"]), 2),
                          "as_of": ss["last_done"]}
        d_done = daily[daily.index < now.astimezone(TZ).strftime("%Y-%m-%d")].iloc[-1]
        state["market"] = {"price": round(float(frames["HOURLY"]["Close"].iloc[-1]), 2),
                           "price_time": frames["HOURLY"].index[-1],
                           "trend_ma": round(float(d_done["trend_ma"]), 2),
                           "daily_close": round(float(d_done["Close"]), 2),
                           "uptrend": bool(d_done["up"])}
        state["backfilled"] = True
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
    mkt = state.get("market", {})
    halted = "; ".join(f"{STRATS[k]['label']}: {s['halted']}" for k, s in state["strats"].items() if s.get("halted"))
    out = {"strategy": STRATEGY, "settings": SETTINGS, "started_at": state["started_at"],
           "halted": halted or None, "status": STATUS, "balance": round(balance(state), 2),
           "initial_balance": init, "views": {}, "positions": [], "markets": {}}
    for view, sel in (("paper", lambda t: t["mode"] == "paper"), ("all", lambda t: True)):
        ts = [t for t in trades if sel(t)]
        out["views"][view] = {"overall": stats(ts, init),
                              "by_market": {k: stats([t for t in ts if t["market"] == k], init) for k in STRATS}}
    for key, ss in state["strats"].items():
        st, info = STRATS[key], ss.get("info") or {}
        if ss.get("halted"):
            status = "Paused by safety switch"
        elif ss["position"]:
            status = "In a trade"
        elif ss["pending_entry"]:
            status = "Buy signal: buying at next open"
        elif not mkt:
            status = "Waiting for data"
        elif not mkt.get("uptrend"):
            status = "No trades: NASDAQ below 200-day average"
        else:
            status = f"Waiting for a dip (RSI below {st['rsi_buy']})"
        out["markets"][key] = {
            "label": f"NASDAQ 100 · {st['label']}", "status": status,
            "details": [
                ["Candles", "Daily" if key == "DAILY" else "Hourly"],
                ["Expected frequency", st["freq"]],
                ["Last price", f"{mkt.get('price', '—')} ({mkt.get('price_time', '—')} NY)"],
                ["Trend filter", ("Uptrend" if mkt.get("uptrend") else "Downtrend")
                 + f": close {mkt.get('daily_close', '—')} vs 200-day avg {mkt.get('trend_ma', '—')}"],
                ["RSI(2)", f"{info.get('rsi', '—')} (buy below {st['rsi_buy']})"],
                ["Last candle checked", f"{info.get('as_of', '—')} NY"],
            ]}
        if ss["position"]:
            p = dict(ss["position"], market=key, label=f"NASDAQ 100 · {st['label']}", strategy=st["label"],
                     pending_exit=ss["pending_exit"], unit=st["unit"])
            if mkt.get("price"):
                p["open_pnl"] = round((mkt["price"] - p["entry"]) * p["qty"], 2)
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
        s = snapshot()
        v = s["views"]
        print(f"[{STATUS['last_check'][:19]}] balance {balance(STATE):,.2f} | paper trades "
              f"{v['paper']['overall']['trades']} | all trades {v['all']['overall']['trades']} "
              f"(win rate {v['all']['overall']['win_rate']}%) | paused: {s['halted']}", flush=True)
        if args.once:
            break
        time.sleep(args.interval)


STATE = None
if __name__ == "__main__":
    main()
