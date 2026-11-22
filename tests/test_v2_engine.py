import math
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import v2_engine as bot
from v2_engine import Candle, ATR_PERIOD, CANDLE_SEC, MIN_HISTORY, START_USD, atr, backtest, demo_candles, new_paper_state, paper_step, signals, size_buy, size_sell


def fixed_bars(n=300):
    t = 1700000000 // CANDLE_SEC * CANDLE_SEC
    return [Candle(t+i*CANDLE_SEC,100,101,99,100,1) for i in range(n)]

class TestsV2(unittest.TestCase):
    def test_atr_wilder_and_past_only(self):
        series = fixed_bars()
        vals = atr(series)
        self.assertEqual(vals[:ATR_PERIOD], [None]*ATR_PERIOD)
        self.assertAlmostEqual(vals[ATR_PERIOD],2)
        modified = list(series)
        last = modified[-1]
        modified[-1] = Candle(last.timestamp,last.open,last.high*2,last.low/2,last.close*2,last.volume)
        self.assertEqual(vals[:-1], atr(modified)[:-1])

    def test_signals_use_only_known_prices(self):
        series = demo_candles(720)
        before = signals(series)
        last = series[-1]
        modified = series[:-1]+[Candle(last.timestamp,last.open,last.high*3,last.low/3,last.close*2,last.volume)]
        self.assertEqual(before[:-1],signals(modified)[:-1])

    def test_buy_requires_rising_long_term_trend(self):
        series = demo_candles(720)
        sig = signals(series)
        trend = bot.ema([c.close for c in series],bot.TREND)
        self.assertGreater(sum(x=='buy' for x in sig),0)
        for i,s in enumerate(sig):
            if s=='buy':
                self.assertGreaterEqual(i,bot.TREND+bot.TREND_SLOPE_BARS-1)
                self.assertGreater(series[i].close,trend[i])
                self.assertGreater(trend[i],trend[i-bot.TREND_SLOPE_BARS])

    def test_sizing_caps_commitment_and_risk(self):
        cash, qty, entry, fee, stop, take, cost = size_buy(1000,1000,100,2)
        self.assertGreater(qty,0)
        self.assertGreater(stop,0)
        self.assertGreater(take,entry)
        self.assertLessEqual(cost,200.000001)
        self.assertLessEqual(qty*(entry-stop)+qty*(entry*bot.FEE+stop*(bot.FEE+bot.SLIPPAGE)),5.000001)
        recovered, _, _ = size_sell(cash,qty,100)
        self.assertLess(recovered,1000) # fees + hypothetical fill slippage

    def test_backtest_consistent_and_nonnegative(self):
        series = demo_candles(720)
        metrics, trades, curve = backtest(series)
        self.assertEqual(len(curve), len(series))
        self.assertGreater(metrics['end_equity'],0)
        self.assertGreaterEqual(metrics['open_position_btc'],0)
        self.assertTrue(all(t['qty']>0 for t in trades))
        self.assertTrue(all(trades[i]['side'] != trades[i+1]['side'] for i in range(len(trades)-1)))

    def test_next_candle_open_and_loss_cooldown(self):
        series = fixed_bars()
        series[252] = Candle(series[252].timestamp,100,101,95,100,1)  # stop triggered
        fake_sig = ['hold']*len(series)
        fake_sig[250]=fake_sig[253]=fake_sig[264]='buy'
        fake_atr = [2.0]*len(series)
        with patch.object(bot,'signals',return_value=fake_sig), patch.object(bot,'atr',return_value=fake_atr):
            m,trades,curve=backtest(series)
        buys = [t for t in trades if t['side']=='BUY']
        self.assertEqual(len(buys),2)
        self.assertEqual(buys[0]['time'],bot.utc(series[251].timestamp))
        self.assertEqual(buys[1]['time'],bot.utc(series[265].timestamp))
        self.assertEqual(m['completed_trades'],1)

    def test_gap_stop_does_not_assume_ideal_fill(self):
        series = fixed_bars()
        series[252] = Candle(series[252].timestamp,90,90,87,88,1)
        fake_sig=['hold']*len(series)
        fake_sig[250]='buy'
        with patch.object(bot,'signals',return_value=fake_sig), patch.object(bot,'atr',return_value=[2.0]*len(series)):
            _,trades,_=backtest(series)
        self.assertEqual(trades[1]['reason'],'Stop gap')
        self.assertAlmostEqual(trades[1]['price'],90*(1-bot.SLIPPAGE), places=2)

    def test_paper_no_duplicate_and_intrabar_stop(self):
        candles=demo_candles(720)
        state=new_paper_state()
        state, first=paper_step(state,candles,candles[-1].close)
        state, repeated=paper_step(state,candles,candles[-1].close)
        self.assertEqual(repeated, [])
        self.assertEqual(len(state['events']),len(first))
        state=new_paper_state()
        state.update(cash=800, btc=0.002, entry=100000, cost=200.5, stop=95000,
                     take=110000,last_closed_candle=candles[-1].timestamp)
        state,events=paper_step(state,candles,94000)
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['side'],'SELL')
        self.assertEqual(state['btc'],0)
        self.assertGreater(state['cooldown_until_ts'],candles[-1].timestamp)
        state,again=paper_step(state,candles,94000)
        self.assertEqual(again,[])

    def test_reject_short_history_and_wrong_state(self):
        with self.assertRaises(ValueError): backtest(fixed_bars(MIN_HISTORY-1))
        s=new_paper_state(); s['version']=1
        with self.assertRaises(ValueError): paper_step(s,demo_candles(720),40000)

if __name__=='__main__': unittest.main()
