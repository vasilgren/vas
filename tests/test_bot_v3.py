import csv
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
import bot
import legacy_v1 as v1
import v2_engine as v2


class FakeResponse:
    def __init__(self, rows): self.rows=rows
    def __enter__(self): return self
    def __exit__(self,*args): return False
    def read(self): return json.dumps(self.rows).encode()


class TestV3(unittest.TestCase):
    def setUp(self):
        self.series=v1.demo_candles(24*365)

    def test_synthetic_history_valid(self):
        bot.validate(self.series)
        self.assertEqual(len(self.series),8760)

    def test_reject_missing_hour(self):
        with self.assertRaisesRegex(ValueError,'Missing or duplicate'):
            bot.validate(self.series[:100]+self.series[101:])

    def test_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            fp=pathlib.Path(td)/'candles.csv'
            bot.save_candles(fp,self.series)
            loaded=bot.load_candles(fp)
            self.assertEqual(loaded,self.series)

    def test_csv_injection_broken_data(self):
        changed=self.series[:]
        c=changed[10]
        changed[10]=v1.Candle(c.timestamp,100,101,100,110,1)
        with self.assertRaisesRegex(ValueError,'Invalid OHLC'):
            bot.validate(changed)

    def test_dashboard_waiting_explains_no_trades(self):
        state=v2.new_paper_state()
        page=bot.dashboard_html(state,self.series[-720:],self.series[-1].close,[])
        self.assertIn('No simulated trades yet',page)
        self.assertIn('Why is the bot waiting?',page)
        self.assertIn('PAPER MODE ONLY',page)
        self.assertIn('$1,000.00',page)

    def test_dashboard_escapes_network_error(self):
        page=bot.dashboard_html(v2.new_paper_state(),None,None,None,error='<script>alert(1)</script>')
        self.assertNotIn('<script>',page)
        self.assertIn('&lt;script&gt;',page)

    def test_history_coinbase_pagination(self):
        now=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        seconds=3600
        end=int(now.timestamp())
        start=end-30*24*seconds
        pages=[]
        def opener(req,timeout):
            qs=urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
            begin=int(dt.datetime.fromisoformat(qs['start'][0].replace('Z','+00:00')).timestamp())
            stop=int(dt.datetime.fromisoformat(qs['end'][0].replace('Z','+00:00')).timestamp())
            pages.append((begin,stop))
            # Endpoint returns descending order. Exclude active hour and use test data.
            return FakeResponse([[ts,99,102,100,101,1.0] for ts in range(stop-seconds,begin-seconds,-seconds)])
        result=bot.coinbase_history(30,now=now,sleeper=lambda _:None,opener=opener)
        self.assertEqual(len(result),720)
        self.assertEqual(len(pages),3)
        self.assertEqual(result[0].timestamp,start)
        self.assertEqual(result[-1].timestamp,end-seconds)
        self.assertTrue(all(stop-begin <= 250*seconds for begin,stop in pages))

    def test_history_gap_aborts(self):
        now=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        end=int(now.timestamp()); seconds=3600
        def opener(req,timeout):
            qs=urllib.parse.parse_qs(urllib.parse.urlparse(req.full_url).query)
            begin=int(dt.datetime.fromisoformat(qs['start'][0].replace('Z','+00:00')).timestamp())
            stop=int(dt.datetime.fromisoformat(qs['end'][0].replace('Z','+00:00')).timestamp())
            rows=[[ts,99,102,100,101,1.0] for ts in range(begin,stop,seconds) if ts != end-300*seconds]
            return FakeResponse(rows)
        with self.assertRaisesRegex(ValueError,'Missing or duplicate'):
            bot.coinbase_history(30,now=now,sleeper=lambda _:None,opener=opener)

    def test_full_and_split_comparison(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(bot,'OUT',pathlib.Path(td)):
                summary=bot.run_comparison(self.series,'SYNTHETIC UNIT TEST')
                self.assertEqual(summary['candles'],8760)
                self.assertIn('test_30',summary)
                self.assertTrue((pathlib.Path(td)/'comparison.html').exists())
                self.assertEqual(summary['full']['V2']['start_balance'],1000)

    def test_atomic_dashboard_file(self):
        with tempfile.TemporaryDirectory() as td:
            file=pathlib.Path(td)/'dashboard.html'
            bot.atomic_write(file,'one')
            bot.atomic_write(file,'two')
            self.assertEqual(file.read_text(),'two')
            self.assertFalse((pathlib.Path(td)/'dashboard.html.tmp').exists())

if __name__=='__main__': unittest.main()
