"""
Paper Trading Bot - Session Breakout v2 (gold + NASDAQ)

Simulated trades only. No broker, no real money.
- Pulls 15-minute and 1-hour candles from Yahoo Finance (GC=F gold futures, NQ=F Nasdaq-100 futures)
- Runs the same rules as SessionBreakoutBot.mq5 / .pine (improved settings)
- Keeps a paper account in state.json
- Serves a live dashboard at http://localhost:8765

Usage:
    .venv/bin/python bot.py            # start (backfills the last ~60 days on first run)
    .venv/bin/python bot.py --reset    # wipe the paper account and start over
"""
import argparse
import json
import math
import os
import threading
import time
import warnings
from datetime import datetime, time as dtime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")
import pandas as pd  # noqa: E402
import yfinance as yf  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "state.json")
DASHBOARD_FILE = os.path.join(HERE, "dashboard.html")
PORT = 8765
POLL_SECONDS = 60

STRATEGY = {
    "name": "Session Breakout v3",
    "summary": "Opening-range breakout with 1-hour 200 EMA trend filter, quick profit target",
    "rules": [
        "Mark the high/low of the session's opening range",
        "Trend filter: last 1h close above 200 EMA = buys only, below = sells only",
        "Enter when a 15-minute candle closes outside the range, in the trend direction",
        "Stop loss on the other side of the range, take profit at 0.5x the risk (quick target = high win rate)",
        "Max 1 trade per market per day; close everything at the end of the session",
        "Risk 0.5% of starting balance per trade; stop for the day at -4%, stop for good at -8%",
    ],
}

SETTINGS = {
    "initial_balance": 100000.0,
    "risk_pct": 0.5,
    "reward_risk": 0.5,
    "daily_loss_pct": 4.0,
    "max_drawdown_pct": 8.0,
    "min_range_atr": 0.5,
    "max_range_atr": 3.0,
    "ema_len": 200,
    "atr_len": 14,
    "max_trades_per_day": 1,
}

MARKETS = {
    "GOLD": {
        "label": "Gold (XAUUSD)",
        "ticker": "GC=F",
        "tz": "Europe/London",
        "session": "London open",
        "range": (dtime(8, 0), dtime(8, 30)),
        "entry_end": dtime(11, 0),
        "day_end": dtime(20, 0),
        "spread": 0.30,
    },
    "NASDAQ": {
        "label": "NASDAQ 100 (NAS100)",
        "ticker": "NQ=F",
        "tz": "America/New_York",
        "session": "New York open",
        "range": (dtime(9, 30), dtime(9, 45)),
        "entry_end": dtime(12, 0),
        "day_end": dtime(15, 45),
        "spread": 2.0,
    },
}

LOCK = threading.Lock()
STATUS = {"last_check": None, "last_error": None, "next_check": None}


# ---------------------------------------------------------------- state
def new_state():
    return {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "initial_balance": SETTINGS["initial_balance"],
        "markets": {k: {"last_bar": None, "day": None, "r_hi": None, "r_lo": None,
                        "range_done": False, "range_ok": False, "trades_today": 0,
                        "pending": None, "position": None} for k in MARKETS},
        "trades": [],
        "halted": False,
    }


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return new_state()


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- data
def fetch(ticker):
    t = yf.Ticker(ticker)
    m15 = t.history(period="60d", interval="15m").dropna(subset=["Open", "High", "Low", "Close"])
    h1 = t.history(period="1y", interval="1h").dropna(subset=["Close"])
    if m15.empty or h1.empty:
        raise RuntimeError(f"No data for {ticker}")
    m15.index = m15.index.tz_convert("UTC")
    h1.index = h1.index.tz_convert("UTC")

    prev_close = m15["Close"].shift(1)
    tr = pd.concat([m15["High"] - m15["Low"],
                    (m15["High"] - prev_close).abs(),
                    (m15["Low"] - prev_close).abs()], axis=1).max(axis=1)
    m15["atr_prev"] = tr.ewm(alpha=1 / SETTINGS["atr_len"], adjust=False).mean().shift(1)

    h1 = h1.copy()
    h1["ema"] = h1["Close"].ewm(span=SETTINGS["ema_len"], adjust=False).mean()
    h1["close_time"] = h1.index + timedelta(hours=1)
    return m15, h1


def trend_at(h1, when):
    """+1 / -1 / 0 using the last fully closed 1h candle at time `when`."""
    idx = h1["close_time"].searchsorted(when, side="right") - 1
    if idx < SETTINGS["ema_len"]:
        return 0
    row = h1.iloc[idx]
    if row["Close"] > row["ema"]:
        return 1
    if row["Close"] < row["ema"]:
        return -1
    return 0


# ---------------------------------------------------------------- engine
def balance(state, mode="paper"):
    """Paper account balance. Backfilled (backtest) trades are kept separate."""
    return state["initial_balance"] + sum(t["pnl"] for t in state["trades"] if t["mode"] == mode)


def day_pnl(state, day_iso, mode):
    return sum(t["pnl"] for t in state["trades"] if t["exit_time"][:10] == day_iso and t["mode"] == mode)


def close_position(state, key, mk, ms, when, price, reason):
    pos = ms["position"]
    d = 1 if pos["side"] == "LONG" else -1
    exit_px = price - d * mk["spread"] / 2
    pnl = (exit_px - pos["entry"]) * d * pos["qty"]
    risk = state["initial_balance"] * SETTINGS["risk_pct"] / 100
    mode = "paper" if pos["entry_time"] >= state["started_at"] else "backtest"
    state["trades"].append({
        "id": f"{key}-{pos['entry_time']}",
        "market": key,
        "label": mk["label"],
        "strategy": STRATEGY["name"],
        "session": mk["session"],
        "mode": mode,
        "side": pos["side"],
        "entry_time": pos["entry_time"],
        "entry": round(pos["entry"], 2),
        "stop": round(pos["stop"], 2),
        "target": round(pos["target"], 2),
        "qty": round(pos["qty"], 2),
        "exit_time": when.isoformat(),
        "exit": round(exit_px, 2),
        "reason": reason,
        "pnl": round(pnl, 2),
        "r": round(pnl / risk, 2),
    })
    ms["position"] = None


def process_bar(state, key, mk, ms, ts, bar, h1):
    tz = ZoneInfo(mk["tz"])
    local = ts.astimezone(tz)
    t = local.time()
    r_start, r_end = mk["range"]
    bar_close_time = ts + timedelta(minutes=15)

    # new trading day
    day = local.date().isoformat()
    if ms["day"] != day:
        ms.update(day=day, r_hi=None, r_lo=None, range_done=False, range_ok=False, trades_today=0)

    # 1) fill a pending entry at this bar's open
    if ms["pending"] and ms["position"] is None:
        p = ms["pending"]
        ms["pending"] = None
        d = 1 if p["side"] == "LONG" else -1
        entry = bar["Open"] + d * mk["spread"] / 2
        dist = (entry - p["stop"]) * d
        if dist > 0:
            risk = state["initial_balance"] * SETTINGS["risk_pct"] / 100
            ms["position"] = {
                "side": p["side"], "entry": entry, "stop": p["stop"],
                "target": entry + d * dist * SETTINGS["reward_risk"],
                "qty": risk / dist, "entry_time": ts.isoformat(), "day": day,
            }
            ms["trades_today"] += 1

    # 2) manage open position
    pos = ms["position"]
    if pos:
        if pos["day"] != day or t >= mk["day_end"]:
            close_position(state, key, mk, ms, ts, bar["Open"], "End of session")
        elif pos["side"] == "LONG":
            if bar["Low"] <= pos["stop"]:
                close_position(state, key, mk, ms, bar_close_time, min(pos["stop"], bar["Open"]), "Stop loss")
            elif bar["High"] >= pos["target"]:
                close_position(state, key, mk, ms, bar_close_time, max(pos["target"], bar["Open"]), "Take profit")
        else:
            if bar["High"] >= pos["stop"]:
                close_position(state, key, mk, ms, bar_close_time, max(pos["stop"], bar["Open"]), "Stop loss")
            elif bar["Low"] <= pos["target"]:
                close_position(state, key, mk, ms, bar_close_time, min(pos["target"], bar["Open"]), "Take profit")

    # 3) build the opening range
    if r_start <= t < r_end:
        ms["r_hi"] = bar["High"] if ms["r_hi"] is None else max(ms["r_hi"], bar["High"])
        ms["r_lo"] = bar["Low"] if ms["r_lo"] is None else min(ms["r_lo"], bar["Low"])
    elif t >= r_end and not ms["range_done"] and ms["r_hi"] is not None:
        ms["range_done"] = True
        atr = bar["atr_prev"]
        width = ms["r_hi"] - ms["r_lo"]
        ms["range_ok"] = bool(atr and not math.isnan(atr) and
                              SETTINGS["min_range_atr"] * atr <= width <= SETTINGS["max_range_atr"] * atr)

    # 4) look for a breakout signal (filled on the next bar's open)
    mode = "paper" if ts.isoformat() >= state["started_at"] else "backtest"
    halted = balance(state, mode) <= state["initial_balance"] * (1 - SETTINGS["max_drawdown_pct"] / 100)
    if mode == "paper" and halted:
        state["halted"] = True
    daily_limit_hit = day_pnl(state, ts.date().isoformat(), mode) <= -state["initial_balance"] * SETTINGS["daily_loss_pct"] / 100
    can_trade = (ms["range_ok"] and r_end <= t < mk["entry_end"] and ms["position"] is None
                 and ms["pending"] is None and ms["trades_today"] < SETTINGS["max_trades_per_day"]
                 and not halted and not daily_limit_hit)
    if can_trade:
        trend = trend_at(h1, bar_close_time)
        if trend > 0 and bar["Close"] > ms["r_hi"]:
            ms["pending"] = {"side": "LONG", "stop": ms["r_lo"]}
        elif trend < 0 and bar["Close"] < ms["r_lo"]:
            ms["pending"] = {"side": "SHORT", "stop": ms["r_hi"]}


def run_cycle(state):
    now = datetime.now(timezone.utc)
    for key, mk in MARKETS.items():
        m15, h1 = fetch(mk["ticker"])
        ms = state["markets"][key]
        last = pd.Timestamp(ms["last_bar"]) if ms["last_bar"] else None
        for ts, bar in m15.iterrows():
            if ts + timedelta(minutes=15) > now:   # candle still forming
                break
            if last is not None and ts <= last:
                continue
            with LOCK:
                process_bar(state, key, mk, ms, ts.to_pydatetime(), bar, h1)
                ms["last_bar"] = ts.isoformat()
        ms["last_price"] = round(float(m15["Close"].iloc[-1]), 2)
        ms["last_price_time"] = m15.index[-1].isoformat()
    with LOCK:
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
        "trades": n,
        "wins": len(wins),
        "losses": n - len(wins),
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "net_pnl": round(gross_win - gross_loss, 2),
        "avg_r": round(sum(t["r"] for t in trades) / n, 2) if n else None,
        "max_dd_pct": round(max_dd, 2),
    }


def snapshot():
    with LOCK:
        state = json.loads(json.dumps(STATE))
    trades = state["trades"]
    init = state["initial_balance"]
    out = {"strategy": STRATEGY, "settings": SETTINGS, "started_at": state["started_at"],
           "halted": state["halted"], "status": STATUS, "balance": round(balance(state), 2),
           "initial_balance": init, "views": {}, "positions": [], "markets": {}}
    for view, sel in (("paper", lambda t: t["mode"] == "paper"), ("all", lambda t: True)):
        ts = [t for t in trades if sel(t)]
        out["views"][view] = {
            "overall": stats(ts, init),
            "by_market": {k: stats([t for t in ts if t["market"] == k], init) for k in MARKETS},
        }
    for key, ms in state["markets"].items():
        mk = MARKETS[key]
        out["markets"][key] = {"label": mk["label"], "session": mk["session"],
                               "last_price": ms.get("last_price"), "last_price_time": ms.get("last_price_time"),
                               "range_high": ms["r_hi"], "range_low": ms["r_lo"], "range_ok": ms["range_ok"],
                               "pending": ms["pending"]}
        if ms["position"]:
            p = dict(ms["position"], market=key, label=mk["label"], strategy=STRATEGY["name"])
            if ms.get("last_price"):
                d = 1 if p["side"] == "LONG" else -1
                p["open_pnl"] = round((ms["last_price"] - p["entry"]) * d * p["qty"], 2)
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
        print(f"[{STATUS['last_check'][:19]}] balance {balance(STATE):,.2f} | "
              f"paper trades {s['paper']['overall']['trades']} | all trades {s['all']['overall']['trades']} "
              f"(win rate {s['all']['overall']['win_rate']}%)", flush=True)
        if args.once:
            break
        time.sleep(POLL_SECONDS)


STATE = None
if __name__ == "__main__":
    main()
