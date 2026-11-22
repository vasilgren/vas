#!/usr/bin/env python3
"""BTC paper simulator V3: V2 trading rules, live HTML dashboard, long-horizon testing.

No broker authentication, keys, balances, wallets, order placement, or deposits.
Python 3.10+; no external packages.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import timezone

import legacy_v1 as v1
import v2_engine as v2

ROOT = pathlib.Path(__file__).resolve().parent
OUT = pathlib.Path(os.environ.get('BOT_RESULTS_DIR', str(ROOT / 'results'))).expanduser().resolve()
CANDLE_SEC = 3600
CSV_FIELDS = ('timestamp','open','high','low','close','volume')
APP_NAME = 'BTC Paper Bot V3'


def fmt_utc(ts: int | float | None) -> str:
    if ts is None: return '—'
    return dt.datetime.fromtimestamp(int(ts), timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def validate(candles: list[v1.Candle], min_bars: int = v2.MIN_HISTORY) -> None:
    if len(candles) < min_bars:
        raise ValueError(f'Need at least {min_bars} closed hourly candles; got {len(candles)}')
    prev = None
    for c in candles:
        vals = (c.open, c.high, c.low, c.close, c.volume)
        if not all(math.isfinite(x) for x in vals) or min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
            raise ValueError(f'Invalid price/volume at {fmt_utc(c.timestamp)}')
        if c.low > min(c.open, c.close) or c.high < max(c.open, c.close):
            raise ValueError(f'Invalid OHLC range at {fmt_utc(c.timestamp)}')
        if c.timestamp % CANDLE_SEC:
            raise ValueError('Expected UTC hourly candle timestamps')
        if prev is not None and c.timestamp-prev != CANDLE_SEC:
            raise ValueError(f'Missing or duplicate hourly candle between {fmt_utc(prev)} and {fmt_utc(c.timestamp)}')
        prev = c.timestamp


def save_candles(path: pathlib.Path, candles: list[v1.Candle]) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh)
        writer.writerow(CSV_FIELDS)
        for c in candles:
            writer.writerow((c.timestamp,c.open,c.high,c.low,c.close,c.volume))


def load_candles(path: pathlib.Path) -> list[v1.Candle]:
    if not path.exists():
        raise FileNotFoundError(f'No history file at {path}. Run 02_download_history_and_test.bat first.')
    data=[]
    with path.open(newline='',encoding='utf-8-sig') as fh:
        rd=csv.DictReader(fh)
        if not rd.fieldnames or any(field not in rd.fieldnames for field in CSV_FIELDS):
            raise ValueError('History CSV must have timestamp,open,high,low,close,volume columns')
        for row in rd:
            data.append(v1.Candle(timestamp=int(row['timestamp']),open=float(row['open']),high=float(row['high']),low=float(row['low']),close=float(row['close']),volume=float(row['volume'])))
    validate(data)
    return data


def coinbase_history(days: int = 365, *, now: dt.datetime | None = None,
                     sleeper=time.sleep, opener=None) -> list[v1.Candle]:
    """Public Coinbase Exchange candles; batches <= 250 bars; no account required.

    Coinbase Exchange API: [epoch, low, high, open, close, volume].
    Partial current hour is excluded. Missing hourly bars cause a fail-closed error.
    """
    if not 30 <= days <= 730:
        raise ValueError('Days must be 30 to 730')
    if now is None: now=dt.datetime.now(timezone.utc)
    if now.tzinfo is None: raise ValueError('now must be timezone aware')
    end = int(now.timestamp()) // CANDLE_SEC * CANDLE_SEC
    start = end - days*24*CANDLE_SEC
    rows = {}
    for chunk_start in range(start,end,250*CANDLE_SEC):
        chunk_end=min(end,chunk_start+250*CANDLE_SEC)
        params=urllib.parse.urlencode({'granularity':CANDLE_SEC,
                 'start':dt.datetime.fromtimestamp(chunk_start,timezone.utc).isoformat().replace('+00:00','Z'),
                 'end':dt.datetime.fromtimestamp(chunk_end,timezone.utc).isoformat().replace('+00:00','Z')})
        url='https://api.exchange.coinbase.com/products/BTC-USD/candles?'+params
        req=urllib.request.Request(url,headers={'User-Agent':'BTCPaperBotV3/1.0','Accept':'application/json'})
        for attempt in range(3):
            try:
                with (opener or urllib.request.urlopen)(req,timeout=20) as res:
                    block=json.load(res)
                if not isinstance(block,list):
                    raise RuntimeError('Unexpected Coinbase response: not a candle list')
                for r in block:
                    if not isinstance(r,list) or len(r)<6: continue
                    ts=int(r[0]); candle=v1.Candle(ts,float(r[3]),float(r[2]),float(r[1]),float(r[4]),float(r[5]))
                    if start <= ts < end and ts+CANDLE_SEC <= end:
                        rows[ts]=candle
                break
            except (OSError, ValueError, RuntimeError) as ex:
                if attempt == 2:
                    raise RuntimeError(f'Coinbase history failed for {fmt_utc(chunk_start)}: {ex}') from ex
                sleeper((attempt+1)*1.5)
        sleeper(0.36)
    ordered=[rows[t] for t in sorted(rows)]
    validate(ordered)
    expected=days*24
    if len(ordered) != expected or ordered[0].timestamp!=start or ordered[-1].timestamp!=end-CANDLE_SEC:
        raise ValueError(f'History incomplete: found {len(ordered)} of {expected} requested hourly candles. No backtest run.')
    return ordered


def row_html(cells: list[object]) -> str:
    return '<tr>'+''.join('<td>'+html.escape(str(c))+'</td>' for c in cells)+'</tr>'


def diff_table(metrics: dict[str,dict], labels: list[str]) -> str:
    names=[('Return (%)','return_pct'),('Ending equity ($)','end_equity'),
           ('Maximum drawdown (%)','max_drawdown_pct'),('Completed trades','completed_trades'),
           ('Win rate (%)','win_rate_pct'),('Fees ($)','fees_paid_usd')]
    head='<tr><th>Metric</th>'+''.join('<th>'+html.escape(x)+'</th>' for x in labels)+'</tr>'
    body=''.join(row_html([title]+[metrics[x][field] for x in labels]) for title,field in names)
    return '<table><thead>'+head+'</thead><tbody>'+body+'</tbody></table>'


CSS = '''body{background:#0b0e14;color:#e9eef8;font:15px system-ui,-apple-system,Segoe UI,sans-serif;max-width:1100px;margin:34px auto;padding:0 20px}
h1{font-size:30px;margin-bottom:4px}.muted{color:#9ca9ba}.notice{padding:14px;border:1px solid #6b5722;background:#23231c;color:#ffe1a0;border-radius:10px;margin:16px 0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:22px 0}.card{background:#171d28;border:1px solid #293241;border-radius:12px;padding:16px}.card span{color:#9ba6b8;display:block;font-size:12px;margin-bottom:8px}.card strong{font-size:24px}table{width:100%;border-collapse:collapse;margin:20px 0}th,td{text-align:left;padding:11px;border-bottom:1px solid #30394b}th{color:#d5dbe5;background:#1d2530}.pill{display:inline-block;border-radius:20px;border:1px solid #506478;padding:4px 10px;margin-top:8px}.section{padding:15px 18px;border:1px solid #30394b;background:#151b25;border-radius:10px;margin-top:15px}a{color:#83b9ff}svg{width:100%;max-height:250px;background:#121822;border-radius:8px}.good{color:#53d7a0}.bad{color:#ff9999}small{color:#a1aab9}.scroll{overflow-x:auto}'''


def chart_svg(values: list[float]) -> str:
    if len(values)<2: return '<div class="muted">Need more price updates to display a chart.</div>'
    lo,hi=min(values),max(values)
    gap=max(0.01,hi-lo)
    points=' '.join(f'{18+i*964/(len(values)-1):.1f},{225-(v-lo)*197/gap:.1f}' for i,v in enumerate(values))
    return '<svg viewBox="0 0 1000 245" role="img" aria-label="Simulated equity over observed data"><polyline points="'+points+'" fill="none" stroke="#85b8ff" stroke-width="2"/></svg>'


def explain(state: dict, candles: list[v1.Candle], mark: float) -> list[str]:
    if state['btc']>0:
        return [f"Holding a simulated BTC position: stop ${state['stop']:,.2f}, target ${state['take']:,.2f}.",
                'The engine exits on observed stop/target prices or an exit signal from a newly closed hourly candle.']
    closes=[c.close for c in candles]
    fast,slow,trend=v1.ema(closes,v2.FAST),v1.ema(closes,v2.SLOW),v1.ema(closes,v2.TREND)
    i=len(candles)-1
    problems=[]
    if i < v2.TREND+v2.TREND_SLOPE_BARS-1 or trend[i] is None:
        problems.append('Not enough history to evaluate the 200-hour trend and its slope.')
    else:
        if closes[i] <= trend[i]: problems.append('BTC closed below its 200-hour EMA.')
        if trend[i] <= trend[i-v2.TREND_SLOPE_BARS]: problems.append('The 200-hour EMA is not rising over 24 hours.')
    if fast[i-1] is None or slow[i-1] is None or not (fast[i-1]<=slow[i-1] and fast[i]>slow[i]):
        problems.append('No new bullish 12/26 EMA crossover on the latest closed hour.')
    last_atr=v2.atr(candles)[-1]
    if last_atr and not (v2.MIN_ATR_PCT <= last_atr/closes[-1] <= v2.MAX_ATR_PCT):
        problems.append('Current ATR volatility is outside the strategy filter.')
    if state.get('cooldown_until_ts',0)>candles[-1].timestamp:
        problems.append('Loss cooldown in effect until '+fmt_utc(state['cooldown_until_ts'])+'.')
    return problems or ['Entry signal is present on latest candle; awaiting next polling/entry evaluation.']


def dashboard_html(state: dict, candles: list[v1.Candle] | None, mark: float | None,
                   equity_history: list[dict] | None, error: str | None = None) -> str:
    cash=state.get('cash',1000.0); btc=state.get('btc',0.0)
    eq=(cash+btc*mark) if mark is not None else None
    pct=100*(eq/v2.START_USD-1) if eq is not None else None
    events=state.get('events',[])[-60:]
    values=[float(x['equity']) for x in (equity_history or [])][-500:]
    if eq is not None: values.append(eq)
    status='Holding position' if btc>0 else 'Waiting for entry'
    reasons=(explain(state,candles,mark) if candles and mark is not None else ['No validated market data yet.'])
    cards=[('Account equity',f'${eq:,.2f}' if eq is not None else '—'),
           ('P/L vs $1,000',f'{pct:+.2f}%' if pct is not None else '—'),
           ('BTC last trade',f'${mark:,.2f}' if mark is not None else '—'),
           ('Virtual cash',f'${cash:,.2f}'),('Simulated BTC',f'{btc:.8f}'),('Trades logged',str(len(state.get('events',[]))))]
    tile=''.join('<div class="card"><span>'+html.escape(k)+'</span><strong>'+html.escape(v)+'</strong></div>' for k,v in cards)
    rows=''.join(row_html([e.get('time',''),e.get('side',''),e.get('reason',''),e.get('price',''),e.get('qty',''),e.get('pnl_usd','')]) for e in reversed(events))
    notes=''.join('<li>'+html.escape(s)+'</li>' for s in reasons)
    fault='<div class="notice">Data request failed: '+html.escape(error)+'</div>' if error else ''
    last=fmt_utc(candles[-1].timestamp) if candles else '—'
    updated=dt.datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    mode_notice = ('Cloud mode: dashboard remains available while the service is running.'
                   if os.environ.get('BOT_NO_BROWSER') == '1'
                   else 'Keep 03_paper_live.bat open; this page refreshes every 30 seconds while open.')
    return f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta http-equiv="refresh" content="30"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BTC Bot V3 — live paper dashboard</title><style>{CSS}</style>
<h1>Bitcoin Bot V3</h1><div class="muted">PAPER MODE ONLY · Kraken BTC/USD · Last updated {updated} · Latest completed candle {last}</div>
<div class="notice">Virtual money only. No live exchange orders. No guarantee of returns. {mode_notice}</div>{fault}
<div class="pill">{html.escape(status)}</div><div class="cards">{tile}</div>
<h2>Why is the bot waiting?</h2><div class="section"><ul>{notes}</ul></div>
<h2>Observed virtual account equity</h2>{chart_svg(values)}<p class="muted">Only sampled prices while this process is running. Not the historical backtest equity chart.</p>
<h2>Simulated trade log</h2><div class="scroll"><table><thead><tr><th>UTC</th><th>Side</th><th>Reason</th><th>Fill USD</th><th>BTC</th><th>P/L USD</th></tr></thead><tbody>{rows or '<tr><td colspan="6">No simulated trades yet</td></tr>'}</tbody></table></div>
<p><a href="comparison.html">Open historical comparison (if generated)</a></p></html>'''


def atomic_write(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(content,encoding='utf-8')
    tmp.replace(path)


def run_paper(poll: int = 60) -> None:
    if poll<15: raise ValueError('Polling must be at least 15 seconds')
    OUT.mkdir(exist_ok=True)
    state_path=OUT/'paper_state_v3.json'
    equity_path=OUT/'paper_equity_v3.csv'
    dash=OUT/'live_dashboard.html'
    state=v2.load_paper_state(state_path) if state_path.exists() else v2.new_paper_state()
    print('V3: PAPER ONLY. NO real exchange orders, accounts, API keys or deposits.')
    print('Open dashboard in browser:',dash)
    print('Press Ctrl+C to stop. Keep this terminal open.')
    last_snapshot=None
    browser_opened=False
    while True:
        try:
            candles=v1.fetch_candles(); validate(candles)
            mark=v1.fetch_price()
            state,events=v2.paper_step(state,candles,mark)
            v2.save_paper_state(state_path,state)
            if state['events']: v1.write_csv(OUT/'paper_trades_v3.csv',state['events'])
            equity=state['cash']+state['btc']*mark
            history=[]
            if equity_path.exists():
                with equity_path.open(newline='',encoding='utf-8') as file:
                    history=list(csv.DictReader(file))[-500:]
            # One history point per latest completed bar (first observed mark, not retrofilled OHLC).
            stamp=candles[-1].timestamp
            if last_snapshot is None and history:
                last_snapshot=int(history[-1]['candle_ts'])
            if stamp != last_snapshot:
                new_row={'candle_ts':stamp,'time':fmt_utc(stamp),'equity':round(equity,4),'mark':round(mark,2)}
                with equity_path.open('a',newline='',encoding='utf-8') as file:
                    writer=csv.DictWriter(file,fieldnames=list(new_row))
                    if not history and equity_path.stat().st_size==0: writer.writeheader()
                    writer.writerow(new_row)
                history.append({k:str(v) for k,v in new_row.items()})
                last_snapshot=stamp
            atomic_write(dash,dashboard_html(state,candles,mark,history))
            if not browser_opened and os.environ.get('BOT_NO_BROWSER') != '1':
                webbrowser.open(dash.as_uri())
                browser_opened=True
            print(f"{dt.datetime.now(timezone.utc).strftime('%H:%M:%S UTC')} BTC ${mark:,.2f} | Equity ${equity:,.2f} | simulated events {len(state['events'])}",flush=True)
            for ev in events: print('SIMULATED',ev,flush=True)
        except (OSError,ValueError,RuntimeError,KeyError,IndexError,json.JSONDecodeError) as ex:
            print('DATA ERROR:',ex,'-- no simulated fill for this failed poll',file=sys.stderr,flush=True)
            atomic_write(dash,dashboard_html(state,None,None,None,error=str(ex)))
            if not browser_opened and os.environ.get('BOT_NO_BROWSER') != '1':
                webbrowser.open(dash.as_uri())
                browser_opened=True
        time.sleep(poll)


def run_comparison(candles: list[v1.Candle], label: str) -> dict:
    validate(candles)
    # Fixed strategies, both see the same closed-candle history and execution assumptions.
    m1,t1,e1=v1.backtest(candles)
    m2,t2,e2=v2.backtest(candles)
    if len(candles)<v2.MIN_HISTORY*3:
        raise ValueError('Require at least 3x strategy warmup length for a reasonable evaluation')
    split=int(len(candles)*.70)
    training=candles[:split]
    testing=candles[split:]
    if len(testing)<v2.MIN_HISTORY:
        raise ValueError('Not enough independent test candles for 200EMA warmup. Use 365-730 days.')
    # Independent start and warmup on train and test: no positions carry across split.
    train1,_,_=v1.backtest(training); train2,_,_=v2.backtest(training)
    test1,_,_=v1.backtest(testing); test2,_,_=v2.backtest(testing)
    metrics={'V1':m1,'V2':m2}
    note=f'{label} | {len(candles):,} consecutive closed hours from {fmt_utc(candles[0].timestamp)} to {fmt_utc(candles[-1].timestamp)}'
    body=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BTC V3 History</title><style>{CSS}</style>
<h1>Bitcoin V3 — historical comparison</h1><p class="muted">{html.escape(note)}</p><div class="notice">HYPOTHETICAL BACKTEST ONLY. V3 deliberately keeps V2 trading rules unchanged. Fees and slippage are assumptions. 70/30 periods are independent tests with their own 200+ hour warmup and $1,000 starting account. No future prediction. The final segment is only an untouched holdout if no strategy choices were made using its results.</div>
<h2>Full data sample</h2>{diff_table(metrics,['V1','V2'])}
<h2>First 70% of time — development period</h2>{diff_table({'V1':train1,'V2':train2},['V1','V2'])}
<p class="muted">UTC range: {fmt_utc(training[0].timestamp)} – {fmt_utc(training[-1].timestamp)}</p>
<h2>Final 30% — held-out evaluation period</h2>{diff_table({'V1':test1,'V2':test2},['V1','V2'])}
<p class="muted">UTC range: {fmt_utc(testing[0].timestamp)} – {fmt_utc(testing[-1].timestamp)}</p>
<div class="section">Win rate is undefined when no trades close. A 0% return after 0 trades does not prove profitability. Independent split runs begin each with $1,000 and a separate EMA warmup; summed returns need not equal the full-period result.</div>
<h2>Full-period equity (V2)</h2>{chart_svg([r['equity'] for r in e2])}
<h2>Recent V2 simulated trades</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Side</th><th>Reason</th><th>Price</th><th>BTC</th><th>P/L</th></tr></thead><tbody>{''.join(row_html([x.get(k,'') for k in ('time','side','reason','price','qty','pnl_usd')]) for x in t2[-100:]) or '<tr><td colspan="6">No trades</td></tr>'}</tbody></table></div></html>'''
    OUT.mkdir(exist_ok=True)
    atomic_write(OUT/'comparison.html',body)
    summary={'label':label,'candles':len(candles),'full':metrics,'train_70':{'V1':train1,'V2':train2},'test_30':{'V1':test1,'V2':test2}}
    atomic_write(OUT/'long_backtest_summary.json',json.dumps(summary,indent=2))
    print(f'Data: {label}; {len(candles)} complete hourly candles')
    print('FULL   V1',m1['return_pct'],'% | V2',m2['return_pct'],'%')
    print('TEST   V1',test1['return_pct'],'% | V2',test2['return_pct'],'% | V2 completed trades',test2['completed_trades'])
    print('Report:',OUT/'comparison.html')
    return summary


def main():
    p=argparse.ArgumentParser(description='BTC V3 paper-only dashboard and long-run backtest (Python standard library only).')
    sub=p.add_subparsers(dest='command',required=True)
    a=sub.add_parser('history',help='Download 12-24 months of real historical BTC candles from Coinbase and backtest')
    a.add_argument('--days',type=int,choices=[365,730],default=365)
    a.add_argument('--csv',type=pathlib.Path,help='Use an existing vetted CSV instead of internet download')
    b=sub.add_parser('demo',help='Offline demonstration using synthetic fake prices; NOT BTC history')
    c=sub.add_parser('paper',help='Run live Kraken BTC/USD paper simulator and HTML dashboard')
    c.add_argument('--poll',type=int,default=60)
    args=p.parse_args()
    if args.command=='history':
        if args.csv:
            candles=load_candles(args.csv); label=f'Imported CSV: {args.csv.name} (source must be verified separately)'
        else:
            print(f'Downloading {args.days} days of hourly Coinbase Exchange BTC-USD candles (no API key).')
            candles=coinbase_history(args.days)
            save_candles(OUT/f'coinbase_btcusd_{args.days}d.csv',candles)
            label=f'Coinbase Exchange BTC-USD 1-hour candles, downloaded {dt.datetime.now(timezone.utc):%Y-%m-%d} UTC'
        run_comparison(candles,label)
    elif args.command=='demo':
        # Synthetic trend periods simply exercise code paths, not trading hypotheses.
        run_comparison(v1.demo_candles(24*365),'SYNTHETIC DEMO. Not historical BTC or evidence of profit.')
    else:
        if args.poll<15: p.error('--poll must be >= 15')
        try: run_paper(args.poll)
        except KeyboardInterrupt: print('\nStopped. Paper results remain saved in results/.')

if __name__=='__main__': main()
