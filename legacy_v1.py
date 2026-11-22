#!/usr/bin/env python3
"""Educational, long-only, paper-only trading bot. No brokerage orders or API keys."""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import pathlib
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / "results"
INTERVAL_MIN = 60
CANDLE_SEC = INTERVAL_MIN * 60
PAIR = "XBTUSD"
FEE = 0.0025     # Hypothetical 0.25% each side; change for your venue
SLIPPAGE = 0.001  # Hypothetical 0.10% each fill
POSITION_FRACTION = 0.20
STOP_PCT = 0.03
TAKE_PCT = 0.06
FAST = 12
SLOW = 26
START_USD = 1000.0

@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def utc(t: int) -> str:
    return dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def api(endpoint: str, params: dict[str, str | int]) -> dict:
    url = "https://api.kraken.com/0/public/" + endpoint + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "EducationalPaperTradingBot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"Market data request failed: {exc}") from exc
    if payload.get("error"):
        raise RuntimeError("Kraken API: " + ", ".join(payload["error"]))
    if not isinstance(payload.get("result"), dict):
        raise RuntimeError("Kraken API returned an unexpected payload")
    return payload["result"]


def fetch_candles() -> list[Candle]:
    """Return only *fully closed* hourly Kraken candles (max approx 719)."""
    result = api("OHLC", {"pair": PAIR, "interval": INTERVAL_MIN})
    pair_key = next((k for k in result if k != "last"), None)
    if pair_key is None:
        raise RuntimeError("Kraken returned no candle series")
    raw = result[pair_key]
    # Kraken explicitly includes the in-progress candle as the last record.
    rows = raw[:-1]
    candles = [Candle(timestamp=int(r[0]), open=float(r[1]), high=float(r[2]),
                      low=float(r[3]), close=float(r[4]), volume=float(r[6]))
               for r in rows]
    if len(candles) < SLOW + 2:
        raise RuntimeError("Too few closed candles to calculate EMA signals")
    return sorted({c.timestamp: c for c in candles}.values(), key=lambda c: c.timestamp)


def fetch_price() -> float:
    result = api("Ticker", {"pair": PAIR})
    ticker = next(iter(result.values()))
    price = float(ticker["c"][0])  # last trade; hypothetical mark, not executable quote
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError("Invalid price")
    return price


def demo_candles(n: int = 500) -> list[Candle]:
    """Deterministic SYNTHETIC candles: for software demonstration, not historical BTC."""
    rng = random.Random(20261122)
    start = 1700000000 // CANDLE_SEC * CANDLE_SEC
    price = 40_000.0
    output = []
    for i in range(n):
        drift = 0.0010 if i % 180 < 65 else (-0.0009 if i % 180 < 112 else 0.0001)
        change = drift + rng.gauss(0, 0.008)
        op = price
        cl = max(100.0, op * (1 + change))
        hi = max(op, cl) * (1 + rng.random() * 0.003)
        lo = min(op, cl) * (1 - rng.random() * 0.003)
        output.append(Candle(start + i * CANDLE_SEC, op, hi, lo, cl, 1.0))
        price = cl
    return output


def ema(values: list[float], span: int) -> list[float | None]:
    if span < 1:
        raise ValueError("EMA span must be >= 1")
    result: list[float | None] = [None] * len(values)
    if len(values) < span:
        return result
    initial = sum(values[:span]) / span
    result[span-1] = initial
    alpha = 2.0 / (span + 1)
    last = initial
    for i in range(span, len(values)):
        last = alpha * values[i] + (1 - alpha) * last
        result[i] = last
    return result


def signals(candles: list[Candle]) -> list[str]:
    closes = [c.close for c in candles]
    a, b = ema(closes, FAST), ema(closes, SLOW)
    result = ["hold"] * len(candles)
    for i in range(1, len(candles)):
        if any(v is None for v in (a[i-1], b[i-1], a[i], b[i])):
            continue
        if a[i-1] <= b[i-1] and a[i] > b[i]:
            result[i] = "buy"
        elif a[i-1] >= b[i-1] and a[i] < b[i]:
            result[i] = "sell"
    return result


def buy(cash: float, price: float) -> tuple[float, float, float, float]:
    """Commit 20% of available cash, inclusive of fees."""
    commitment = cash * POSITION_FRACTION
    execution_price = price * (1 + SLIPPAGE)
    qty = commitment / (execution_price * (1 + FEE))
    fee_paid = qty * execution_price * FEE
    return cash - commitment, qty, execution_price, fee_paid


def sell(cash: float, qty: float, price: float) -> tuple[float, float, float]:
    execution_price = price * (1 - SLIPPAGE)
    proceeds = qty * execution_price
    fee_paid = proceeds * FEE
    return cash + proceeds - fee_paid, execution_price, fee_paid


def backtest(candles: list[Candle]) -> tuple[dict, list[dict], list[dict]]:
    """Signals at candle close, execute at NEXT candle open. Stops pessimistically first."""
    if len(candles) < SLOW + 3:
        raise ValueError("Need at least 29 closed candles")
    sig = signals(candles)
    cash, qty, entry, cost, fees = START_USD, 0.0, 0.0, 0.0, 0.0
    trades, equity = [], []
    entries = 0
    exits = 0
    realized_total = 0.0
    wins = 0
    for i, candle in enumerate(candles):
        if i > 0:
            # Entry/exit decisions from the immediately preceding *completed* candle.
            prev_signal = sig[i-1]
            if qty > 0 and prev_signal == "sell":
                cash, px, fee = sell(cash, qty, candle.open)
                pnl = qty * px * (1-FEE) - cost
                realized_total += pnl
                wins += pnl > 0
                fees += fee
                trades.append({"time": utc(candle.timestamp), "side": "SELL", "reason": "EMA down", "price": round(px,2), "qty": round(qty,8), "pnl_usd": round(pnl,2)})
                qty, entry, cost = 0.0, 0.0, 0.0
                exits += 1
            elif qty == 0 and prev_signal == "buy":
                cash, qty, entry, fee = buy(cash, candle.open)
                cost = qty * entry * (1+FEE)
                fees += fee
                entries += 1
                trades.append({"time": utc(candle.timestamp), "side": "BUY", "reason": "EMA up", "price": round(entry,2), "qty": round(qty,8), "pnl_usd": ""})
        if qty > 0:
            # An open gap past a stop is filled at the open, not magically at the stop.
            stop = entry * (1 - STOP_PCT)
            take = entry * (1 + TAKE_PCT)
            reason, raw_exit = None, None
            if candle.open <= stop:
                reason, raw_exit = "Stop gap", candle.open
            elif candle.open >= take:
                reason, raw_exit = "Take-profit gap", candle.open
            elif candle.low <= stop:
                reason, raw_exit = "Stop-loss", stop  # pessimistic if TP also touched
            elif candle.high >= take:
                reason, raw_exit = "Take-profit", take
            if reason:
                cash, px, fee = sell(cash, qty, raw_exit)
                pnl = qty * px * (1-FEE) - cost
                realized_total += pnl
                wins += pnl > 0
                fees += fee
                trades.append({"time": utc(candle.timestamp), "side": "SELL", "reason": reason, "price": round(px,2), "qty": round(qty,8), "pnl_usd": round(pnl,2)})
                qty, entry, cost = 0.0, 0.0, 0.0
                exits += 1
        equity.append({"time": utc(candle.timestamp), "equity": round(cash + qty * candle.close, 4), "price": round(candle.close, 2)})
    total = cash + qty * candles[-1].close
    peak, drawdown = START_USD, 0.0
    for item in equity:
        peak = max(peak, item["equity"])
        drawdown = min(drawdown, item["equity"] / peak - 1)
    start_price, end_price = candles[0].close, candles[-1].close
    metrics = {
        "start_balance": round(START_USD, 2), "end_equity": round(total, 2),
        "return_pct": round((total/START_USD-1)*100, 2),
        "buy_hold_return_pct": round((end_price/start_price-1)*100, 2),
        "max_drawdown_pct": round(drawdown*100, 2), "entries": entries,
        "completed_trades": exits, "win_rate_pct": round(100*wins/exits, 2) if exits else None,
        "fees_paid_usd": round(fees, 2), "open_position_btc": round(qty, 8),
        "from": utc(candles[0].timestamp), "to": utc(candles[-1].timestamp),
    }
    return metrics, trades, equity


def write_csv(path: pathlib.Path, rows: list[dict]) -> None:
    if not rows: return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def make_html(metrics: dict, trades: list[dict], equity: list[dict], data_label: str) -> str:
    nums = [r["equity"] for r in equity]
    min_y, max_y = min(nums), max(nums)
    spread = max(0.1, max_y-min_y)
    pts = " ".join(f"{30 + i * 940 / max(1,len(nums)-1):.1f},{270 - (val-min_y)/spread*240:.1f}"
                   for i, val in enumerate(nums))
    summary_rows = "".join(f"<div><span>{html.escape(str(k).replace('_',' ').title())}</span><strong>{html.escape(str(v))}</strong></div>" for k,v in metrics.items())
    table_rows = "".join("<tr>"+"".join(f"<td>{html.escape(str(r.get(k,'')))}</td>" for k in ("time","side","reason","price","qty","pnl_usd"))+"</tr>" for r in trades)
    return f'''<!DOCTYPE html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Bot Backtest</title><style>body{{font:15px system-ui;background:#0d1117;color:#e6edf3;margin:0;padding:36px;max-width:1050px;margin:auto}}h1{{font-size:30px}}p{{color:#8b949e}}.note{{background:#242b35;padding:16px;border-radius:8px;color:#f2cc60}}.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(205px,1fr));gap:10px;margin:24px 0}}.stats div{{background:#161b22;padding:14px;border-radius:8px;display:flex;flex-direction:column;gap:8px}}.stats span{{color:#8b949e;font-size:13px}}strong{{font-size:19px}}svg{{width:100%;height:auto;background:#161b22;border-radius:8px}}table{{border-collapse:collapse;width:100%;font-size:13px}}td,th{{padding:9px;text-align:left;border-bottom:1px solid #30363d}}.scroll{{overflow:auto}}small{{color:#8b949e}}</style>
<h1>Trading bot — backtest report</h1><p>Source: {html.escape(data_label)} • Generated {html.escape(dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}</p>
<div class="note">SIMULATION ONLY. No real money, no executed orders. Fees and slippage are assumptions. Results do not predict future returns. Synthetic demo prices are NOT actual Bitcoin history.</div>
<div class="stats">{summary_rows}</div><h2>Portfolio equity (USD)</h2><svg viewBox="0 0 1000 300" role="img" aria-label="Backtest equity line chart"><line x1="30" y1="270" x2="970" y2="270" stroke="#4b5563"/><polyline points="{pts}" stroke="#5fa8ff" fill="none" stroke-width="2"/></svg><small>Backtest equity at each closed bar. Open position marked at candle close.</small>
<h2>Simulated trades</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Side</th><th>Reason</th><th>Execution price</th><th>BTC</th><th>Realized P&amp;L (USD)</th></tr></thead><tbody>{table_rows}</tbody></table></div>
</html>'''


def run_backtest(data: str):
    OUT.mkdir(exist_ok=True)
    candles = demo_candles() if data == "demo" else fetch_candles()
    metrics, trades, equity = backtest(candles)
    label = "SYNTHETIC DEMO DATA (not actual BTC)" if data == "demo" else "Kraken XBT/USD hourly closed candles"
    (OUT / "report.html").write_text(make_html(metrics, trades, equity, label), encoding="utf-8")
    (OUT / "summary.json").write_text(json.dumps({"data":label, "metrics":metrics}, indent=2), encoding="utf-8")
    write_csv(OUT / "trades.csv", trades)
    write_csv(OUT / "equity.csv", equity)
    print("\n"+label)
    print("Range:",metrics["from"],"to",metrics["to"])
    print("Starting USD:",metrics["start_balance"],"Final simulated equity:",metrics["end_equity"])
    print("Strategy return:",str(metrics["return_pct"])+"%", "Buy/hold:",str(metrics["buy_hold_return_pct"])+"%")
    print("Max drawdown:",str(metrics["max_drawdown_pct"])+"%", "Completed trades:",metrics["completed_trades"])
    print("Report:",OUT / "report.html")


def load_paper_state(path: pathlib.Path) -> dict:
    if path.exists():
        s = json.loads(path.read_text(encoding="utf-8"))
        if s.get("version") != 1:
            raise RuntimeError("Unsupported paper state version")
        return s
    return {"version":1, "cash":START_USD, "btc":0.0, "entry":0.0,
            "cost":0.0, "last_closed_candle":None, "events":[]}


def save_paper_state(path: pathlib.Path, state: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(path)


def paper_step(state: dict, candles: list[Candle], mark_price: float) -> tuple[dict, list[dict]]:
    """Simulates fills at CURRENT last-traded price, never submits an order.

    One closed candle is evaluated per loop. First run may act on latest signal.
    Large polling gaps cannot be replayed at historical executable prices.
    """
    if len(candles) < SLOW+2 or mark_price <= 0:
        raise ValueError("Invalid paper data")
    last_bar = candles[-1]
    if state["last_closed_candle"] == last_bar.timestamp:
        return state, []
    if state["last_closed_candle"] is not None and last_bar.timestamp < state["last_closed_candle"]:
        raise RuntimeError("Market candles moved backwards")
    if state["last_closed_candle"] is not None and last_bar.timestamp - state["last_closed_candle"] > CANDLE_SEC:
        print("WARNING: Missed closed bars. Paper accounting cannot reconstruct intrabar stop fills.")
    sig = signals(candles)[-1]
    events = []
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    # We use a single observed last-trade price; real fills might differ substantially.
    if state["btc"] > 0:
        reason = None
        if mark_price <= state["entry"] * (1-STOP_PCT): reason = "Stop-loss at observed price"
        elif mark_price >= state["entry"] * (1+TAKE_PCT): reason = "Take-profit at observed price"
        elif sig == "sell": reason = "EMA down"
        if reason:
            qty = state["btc"]
            state["cash"], px, fee = sell(state["cash"], qty, mark_price)
            pnl = qty*px*(1-FEE)-state["cost"]
            events.append({"time":now, "side":"SELL", "reason":reason, "price":round(px,2), "qty":round(qty,8), "pnl_usd":round(pnl,2)})
            state["btc"] = state["entry"] = state["cost"] = 0.0
    elif sig == "buy":
        state["cash"], qty, px, fee = buy(state["cash"], mark_price)
        state["btc"] = qty
        state["entry"] = px
        state["cost"] = qty*px*(1+FEE)
        events.append({"time":now, "side":"BUY", "reason":"EMA up", "price":round(px,2), "qty":round(qty,8), "pnl_usd":""})
    state["last_closed_candle"] = last_bar.timestamp
    state["events"].extend(events)
    return state, events


def run_paper_loop(poll_seconds: int):
    OUT.mkdir(exist_ok=True)
    path = OUT / "paper_state.json"
    print("PAPER MODE ONLY. No orders or credentials. Press Ctrl+C to stop.")
    print("State:",path)
    while True:
        try:
            candles = fetch_candles()
            price = fetch_price()
            state = load_paper_state(path)
            # Update stop at each poll even without a new closed candle.
            state, events = paper_step(state, candles, price)
            if not events and state["btc"] > 0:
                # Note: this stop check has no intrabar reconstruction; current-price only.
                if price <= state["entry"]*(1-STOP_PCT) or price >= state["entry"]*(1+TAKE_PCT):
                    reason = "Stop-loss observed" if price <= state["entry"]*(1-STOP_PCT) else "Take-profit observed"
                    qty = state["btc"]
                    state["cash"], px, fee = sell(state["cash"], qty, price)
                    pnl = qty*px*(1-FEE)-state["cost"]
                    state["btc"] = state["entry"] = state["cost"] = 0.0
                    event = {"time":dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                             "side":"SELL", "reason":reason, "price":round(px,2), "qty":round(qty,8), "pnl_usd":round(pnl,2)}
                    events = [event]
                    state["events"].extend(events)
            save_paper_state(path, state)
            write_csv(OUT / "paper_trades.csv", state["events"])
            equity = state["cash"] + state["btc"]*price
            print(f"{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S UTC')} BTC ${price:,.2f} | Cash ${state['cash']:,.2f} | BTC {state['btc']:.8f} | Equity ${equity:,.2f}")
            for ev in events:
                print("SIMULATED", ev)
        except (RuntimeError, urllib.error.URLError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
            print("ERROR:",exc, "— no trade placed; retry at next poll", file=sys.stderr)
        time.sleep(poll_seconds)


def main():
    parser = argparse.ArgumentParser(description="Free, paper-only crypto bot. No account or API key required.")
    commands = parser.add_subparsers(dest="command", required=True)
    back = commands.add_parser("backtest", help="Backtest EMA crossover + stop-loss on fully closed candles")
    back.add_argument("--data", choices=["demo", "kraken"], default="demo", help="demo is SYNTHETIC, kraken is real historical candles")
    paper = commands.add_parser("paper", help="Simulate ongoing hourly trading on live Kraken prices")
    paper.add_argument("--poll", type=int, default=60, help="seconds between price polls, minimum 15")
    args = parser.parse_args()
    if args.command == "backtest":
        run_backtest(args.data)
    else:
        if args.poll < 15:
            parser.error("--poll must be >= 15 seconds")
        try:
            run_paper_loop(args.poll)
        except KeyboardInterrupt:
            print("\nStopped. Paper state preserved.")

if __name__ == "__main__":
    main()
