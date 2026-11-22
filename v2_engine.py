#!/usr/bin/env python3
"""V2 long-only BTC trading simulator. Public data, no orders, no credentials."""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import pathlib
import sys
import time
from dataclasses import asdict

import legacy_v1 as v1
from legacy_v1 import Candle, demo_candles, ema, fetch_candles, fetch_price, make_html, utc, write_csv

ROOT = pathlib.Path(__file__).resolve().parent
OUT = ROOT / 'results'
START_USD = 1000.0
FAST, SLOW, TREND = 12, 26, 200
TREND_SLOPE_BARS = 24
ATR_PERIOD = 14
ATR_STOP = 2.5
ATR_TAKE = 4.5
COOLDOWN_BARS = 12
MAX_POSITION_PCT = 0.20
RISK_PCT = 0.005
MIN_ATR_PCT = 0.001
MAX_ATR_PCT = 0.08
FEE, SLIPPAGE = v1.FEE, v1.SLIPPAGE
CANDLE_SEC = v1.CANDLE_SEC
MIN_HISTORY = max(TREND + TREND_SLOPE_BARS, SLOW, ATR_PERIOD + 1) + 2


def atr(candles: list[Candle], period: int = ATR_PERIOD) -> list[float | None]:
    """ATR computed from current/past bars only, with Wilder smoothing."""
    result: list[float | None] = [None] * len(candles)
    if len(candles) <= period:
        return result
    true_ranges = [None]
    for i in range(1, len(candles)):
        c, previous = candles[i], candles[i-1]
        true_ranges.append(max(c.high-c.low, abs(c.high-previous.close), abs(c.low-previous.close)))
    last = sum(true_ranges[1:period+1]) / period
    result[period] = last
    for i in range(period+1, len(candles)):
        last = (last * (period-1) + true_ranges[i]) / period
        result[i] = last
    return result


def signals(candles: list[Candle]) -> list[str]:
    """EMA cross only in rising 200-EMA market; exits on bearish cross or loss of trend."""
    closes = [c.close for c in candles]
    fast, slow, trend = ema(closes, FAST), ema(closes, SLOW), ema(closes, TREND)
    volatility = atr(candles)
    result = ['hold'] * len(candles)
    for i in range(1, len(candles)):
        if fast[i-1] is None or slow[i-1] is None or fast[i] is None or slow[i] is None:
            continue
        bear_cross = fast[i-1] >= slow[i-1] and fast[i] < slow[i]
        if bear_cross or (trend[i] is not None and closes[i] < trend[i]):
            result[i] = 'sell'
            continue
        if i < TREND+TREND_SLOPE_BARS-1:
            continue
        t, past = trend[i], trend[i-TREND_SLOPE_BARS]
        a = volatility[i]
        if t is None or past is None or a is None:
            continue
        atr_fraction = a/closes[i]
        up_cross = fast[i-1] <= slow[i-1] and fast[i] > slow[i]
        if (up_cross and closes[i] > t and t > past and
                MIN_ATR_PCT <= atr_fraction <= MAX_ATR_PCT):
            result[i] = 'buy'
    return result


def size_buy(cash: float, equity: float, raw_price: float, last_atr: float):
    """Size position to at most 20% of cash and ~0.5% equity planned stop risk.

    Returns cash, qty, fill_price, fees, stop_price, take_price, total_cost.
    A gap can produce a realized loss greater than the planned risk budget.
    """
    if not all(math.isfinite(x) for x in (cash, equity, raw_price, last_atr)):
        raise ValueError('Non-finite account, price, or ATR')
    if min(cash, equity, raw_price, last_atr) <= 0:
        raise ValueError('Account, price, and ATR must be positive')
    fill = raw_price * (1+SLIPPAGE)
    stop_gap = ATR_STOP * last_atr
    stop = max(0.01, fill-stop_gap)
    take = fill + ATR_TAKE * last_atr
    fee_risk_per_coin = fill * FEE + stop * (FEE+SLIPPAGE)
    risk_qty = equity * RISK_PCT / (stop_gap + fee_risk_per_coin)
    cash_qty = cash * MAX_POSITION_PCT / (fill*(1+FEE))
    qty = min(risk_qty, cash_qty)
    if qty <= 0:
        raise ValueError('Position size too small')
    paid = qty * fill * (1+FEE)
    fee = qty * fill * FEE
    return cash-paid, qty, fill, fee, stop, take, paid


def size_sell(cash: float, qty: float, raw_price: float):
    fill = raw_price*(1-SLIPPAGE)
    proceeds = qty*fill
    fees = proceeds*FEE
    return cash+proceeds-fees, fill, fees


def backtest(candles: list[Candle]):
    """Fill next open after a completed-candle signal; risk exits modelled on OHLC.

    Stops are conservatively checked before takes on an ambiguous candle.
    """
    if len(candles) < MIN_HISTORY:
        raise ValueError(f'Need at least {MIN_HISTORY} fully closed candles')
    sig, values_atr = signals(candles), atr(candles)
    cash, qty, entry, stop, take, cost, fees = START_USD, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    cooldown_through = -1
    trades, equity_curve = [], []
    entries = exits = wins = 0
    for i, candle in enumerate(candles):
        if i > 0:
            previous_signal = sig[i-1]
            if qty > 0 and previous_signal == 'sell':
                cash, fill, fee = size_sell(cash, qty, candle.open)
                pnl = qty*fill*(1-FEE)-cost
                trades.append(dict(time=utc(candle.timestamp), side='SELL', reason='Trend/EMA exit', price=round(fill,2), qty=round(qty,8), pnl_usd=round(pnl,2)))
                fees += fee; wins += int(pnl>0); exits += 1
                if pnl <= 0: cooldown_through = i+COOLDOWN_BARS
                qty = entry = stop = take = cost = 0.0
            elif qty == 0 and previous_signal == 'buy' and i >= cooldown_through:
                previous_atr = values_atr[i-1]
                if previous_atr is not None and previous_atr > 0:
                    cash, qty, entry, fee, stop, take, cost = size_buy(cash, cash, candle.open, previous_atr)
                    fees += fee; entries += 1
                    trades.append(dict(time=utc(candle.timestamp), side='BUY', reason='EMA up + 200EMA trend', price=round(entry,2), qty=round(qty,8), pnl_usd=''))
        if qty > 0:
            reason = raw_exit = None
            if candle.open <= stop: reason, raw_exit = 'Stop gap', candle.open
            elif candle.open >= take: reason, raw_exit = 'Take gap', candle.open
            elif candle.low <= stop: reason, raw_exit = 'ATR stop-loss', stop
            elif candle.high >= take: reason, raw_exit = 'ATR take-profit', take
            if reason is not None:
                cash, fill, fee = size_sell(cash, qty, raw_exit)
                pnl = qty*fill*(1-FEE)-cost
                trades.append(dict(time=utc(candle.timestamp), side='SELL', reason=reason, price=round(fill,2), qty=round(qty,8), pnl_usd=round(pnl,2)))
                fees += fee; wins += int(pnl>0); exits += 1
                if pnl <= 0: cooldown_through = i+COOLDOWN_BARS
                qty = entry = stop = take = cost = 0.0
        equity_curve.append(dict(time=utc(candle.timestamp), equity=round(cash+qty*candle.close,4), price=round(candle.close,2)))
    end_equity = cash+qty*candles[-1].close
    peak, dd = START_USD, 0.0
    for p in equity_curve:
        peak = max(peak, p['equity'])
        dd = min(dd, p['equity']/peak-1)
    metrics = dict(start_balance=round(START_USD,2), end_equity=round(end_equity,2),
                   return_pct=round(100*(end_equity/START_USD-1),2),
                   buy_hold_return_pct=round(100*(candles[-1].close/candles[0].close-1),2),
                   max_drawdown_pct=round(100*dd,2), entries=entries,
                   completed_trades=exits, win_rate_pct=round(100*wins/exits,2) if exits else None,
                   fees_paid_usd=round(fees,2), open_position_btc=round(qty,8),
                   **{'from':utc(candles[0].timestamp),'to':utc(candles[-1].timestamp)})
    return metrics, trades, equity_curve


def compare_html(old: dict, new: dict, label: str) -> str:
    rows = [
        ('Ending equity (USD)', 'end_equity'),
        ('Total return (%)', 'return_pct'),
        ('Max drawdown (%)', 'max_drawdown_pct'),
        ('Trades closed', 'completed_trades'),
        ('Win rate (%)', 'win_rate_pct'),
        ('Fees paid (USD)', 'fees_paid_usd'),
        ('Open BTC', 'open_position_btc'),
        ('Buy and hold (%)', 'buy_hold_return_pct'),
    ]
    body = ''.join('<tr><td>'+html.escape(title)+'</td><td>'+html.escape(str(old[key]))+'</td><td>'+html.escape(str(new[key]))+'</td></tr>' for title,key in rows)
    caution = 'If V2 made no trades, that is not evidence of strategy profitability. ' if new['completed_trades']==0 else ''
    return f"""<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>BTC bot: V1 vs V2</title><style>body{{background:#0d1117;color:#e6edf3;font:16px system-ui;max-width:850px;margin:40px auto;padding:0 18px}}h1{{font-size:29px}}p{{color:#a0a9b6}}.notice{{border:1px solid #5f522a;background:#25251f;color:#ffe29c;padding:15px;border-radius:8px}}table{{width:100%;border-collapse:collapse;margin:24px 0}}th,td{{padding:13px;text-align:left;border-bottom:1px solid #333a44}}th{{background:#1c232d}}a{{color:#82b9ff}}b{{color:#fff}}</style>
<h1>Bitcoin bot — comparison</h1><p>{html.escape(label)}</p><div class='notice'>PAPER-ONLY BACKTEST. Both strategies use exactly the same price candles, hypothetical fees and slippage. {caution}One month of candles is insufficient to judge profitability. Synthetic data is not real BTC history.</div>
<table><thead><tr><th>Metric</th><th>Original V1</th><th>Filtered V2</th></tr></thead><tbody>{body}</tbody></table>
<p>V2 adds 200-hour rising-trend requirement, 12-bar loss cooldown, ATR stops/targets, and risk-based position sizing. The rules may reduce trades without increasing returns.</p>
<p><a href='original_v1_report.html'>Open original V1 chart</a> &nbsp; | &nbsp; <a href='report.html'>Open V2 chart</a></p></html>"""


def run_backtest(source: str, compare: bool = True):
    OUT.mkdir(exist_ok=True)
    candles = demo_candles(720) if source == 'demo' else fetch_candles()
    label = ('SYNTHETIC DEMO (not historical BTC)' if source == 'demo'
             else 'Kraken BTC/USD closed hourly candles')
    metrics, trades, equity = backtest(candles)
    report = make_html(metrics, trades, equity, label+' — V2 trend-filtered + ATR risk')
    (OUT/'report.html').write_text(report, encoding='utf-8')
    write_csv(OUT/'trades.csv',trades); write_csv(OUT/'equity.csv',equity)
    summary = {'data':label, 'strategy':'V2', 'metrics':metrics}
    if compare:
        old_m, old_t, old_e = v1.backtest(candles)
        summary['original_v1_metrics'] = old_m
        (OUT/'original_v1_report.html').write_text(make_html(old_m,old_t,old_e,label+' — original V1'), encoding='utf-8')
        (OUT/'comparison.html').write_text(compare_html(old_m, metrics, label), encoding='utf-8')
    (OUT/'summary.json').write_text(json.dumps(summary, indent=2),encoding='utf-8')
    print('\n',label)
    print('V2 return:',metrics['return_pct'],'% | V2 trades:',metrics['completed_trades'])
    print('V2 end equity: $',metrics['end_equity'], ' | V2 fees: $',metrics['fees_paid_usd'],sep='')
    if compare:
        print('Original V1 return:',old_m['return_pct'],'% | V1 trades:',old_m['completed_trades'])
        print('Both versions tested on the EXACT same candles and fee/slippage assumptions.')
    if metrics['completed_trades'] == 0:
        print('NOTICE: V2 made NO completed trades in this sample. 0% does not establish an edge.')
    print('V2 report:',OUT/'report.html')
    print('Original report:',OUT/'original_v1_report.html')
    print('Side-by-side comparison:', OUT/'comparison.html')


def new_paper_state():
    return dict(version=2, cash=START_USD, btc=0.0, entry=0.0, stop=0.0, take=0.0,
                cost=0.0, cooldown_until_ts=0, last_closed_candle=None, events=[])


def load_paper_state(path: pathlib.Path):
    if not path.exists(): return new_paper_state()
    state = json.loads(path.read_text(encoding='utf-8'))
    if state.get('version') != 2:
        raise RuntimeError('Paper state mismatch. V2 requires V2 state; old V1 is not imported.')
    return state


def save_paper_state(path: pathlib.Path, state: dict):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state, indent=2), encoding='utf-8')
    temp.replace(path)


def paper_step(state: dict, candles: list[Candle], mark_price: float):
    """Check observed price for stops on every poll; closed bar signals once per hour.

    No exchange orders, no intrabar fill reconstruction, no wall-clock assumptions.
    """
    if len(candles) < MIN_HISTORY or mark_price <= 0 or not math.isfinite(mark_price):
        raise ValueError('Invalid paper trading data')
    if state.get('version') != 2:
        raise ValueError('State is not version 2')
    last = candles[-1]
    previous = state['last_closed_candle']
    if previous is not None and last.timestamp < previous:
        raise RuntimeError('Market data moved backwards')
    if previous is not None and last.timestamp-previous > CANDLE_SEC:
        print('WARNING: missed bars; actual intrabar stop prices are unknown.')
    new_bar = previous != last.timestamp
    event_time = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    events = []
    sig = signals(candles)[-1] if new_bar else 'hold'
    if state['btc'] > 0:
        if mark_price <= state['stop']: reason = 'ATR stop observed'
        elif mark_price >= state['take']: reason = 'ATR take observed'
        elif sig == 'sell': reason = 'Trend/EMA exit'
        else: reason = None
        if reason:
            qty = state['btc']
            state['cash'], fill, fee = size_sell(state['cash'], qty, mark_price)
            pnl = qty*fill*(1-FEE)-state['cost']
            events.append(dict(time=event_time, side='SELL', reason=reason,
                               price=round(fill,2), qty=round(qty,8), pnl_usd=round(pnl,2)))
            if pnl <= 0: state['cooldown_until_ts'] = last.timestamp+COOLDOWN_BARS*CANDLE_SEC
            for key in ('btc','entry','stop','take','cost'): state[key] = 0.0
    elif new_bar and sig == 'buy' and last.timestamp >= state['cooldown_until_ts']:
        last_atr = atr(candles)[-1]
        if last_atr is not None and last_atr > 0:
            state['cash'], qty, entry, fee, stop, take, cost = size_buy(state['cash'],state['cash'],mark_price,last_atr)
            state.update(btc=qty,entry=entry,stop=stop,take=take,cost=cost)
            events.append(dict(time=event_time, side='BUY', reason='EMA up + 200EMA trend',
                               price=round(entry,2), qty=round(qty,8), pnl_usd=''))
    if new_bar:
        state['last_closed_candle'] = last.timestamp
    state['events'].extend(events)
    return state, events


def run_paper(poll: int):
    OUT.mkdir(exist_ok=True)
    state_path = OUT/'paper_state_v2.json'
    print('V2 PAPER ONLY. No orders, accounts, keys or deposits. Ctrl+C to stop.')
    print('Independent V2 simulated state:',state_path)
    while True:
        try:
            candles, price = fetch_candles(), fetch_price()
            state = load_paper_state(state_path)
            state, events = paper_step(state,candles,price)
            save_paper_state(state_path,state)
            if state['events']: write_csv(OUT/'paper_trades_v2.csv',state['events'])
            equity = state['cash']+state['btc']*price
            print(f"{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S UTC')} BTC ${price:,.2f} | Cash ${state['cash']:,.2f} | BTC {state['btc']:.8f} | Equity ${equity:,.2f}")
            for ev in events: print('SIMULATED:',ev)
        except (RuntimeError, OSError, ValueError, KeyError, IndexError, json.JSONDecodeError) as e:
            print('ERROR:',e,'-- no real order sent; retrying',file=sys.stderr)
        time.sleep(poll)


def main():
    parser = argparse.ArgumentParser(description='BTC V2: paper-only, rising trend filter, cooldown and ATR sizing')
    sub = parser.add_subparsers(dest='command', required=True)
    b = sub.add_parser('backtest'); b.add_argument('--data',choices=['demo','kraken'],default='demo')
    p = sub.add_parser('paper'); p.add_argument('--poll',type=int,default=60)
    args = parser.parse_args()
    if args.command == 'backtest': run_backtest(args.data)
    else:
        if args.poll < 15: parser.error('--poll must be >=15')
        try: run_paper(args.poll)
        except KeyboardInterrupt: print('\nStopped. V2 paper state saved.')


if __name__ == '__main__': main()
