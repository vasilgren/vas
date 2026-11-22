"""HTTP/cloud deployment regression tests; no live network calls."""
import base64
import http.client
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

import cloud_app

class CloudTests(unittest.TestCase):
    def test_requires_secure_password(self):
        with self.assertRaises(ValueError):
            cloud_app.make_handler('short', Path('.'))

    def test_dashboard_auth_and_no_state_leak(self):
        with tempfile.TemporaryDirectory() as temp:
            d=Path(temp)
            (d/'live_dashboard.html').write_text('<h1>paper dashboard</h1>',encoding='utf-8')
            (d/'paper_state_v3.json').write_text('{"events":[]}',encoding='utf-8')
            handler=cloud_app.make_handler('testing-long-password',d)
            server=ThreadingHTTPServer(('127.0.0.1',0), handler)
            t=threading.Thread(target=server.serve_forever,daemon=True)
            t.start()
            def req(path,auth=None):
                conn=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=2)
                headers={}
                if auth is not None:
                    headers['Authorization']='Basic '+base64.b64encode(auth.encode()).decode()
                conn.request('GET',path,headers=headers)
                r=conn.getresponse(); body=r.read(); status=r.status; hdrs=dict(r.getheaders());conn.close()
                return status,body,hdrs
            try:
                status,body,head=req('/health')
                self.assertEqual((status,body),(200,b'OK'))
                self.assertEqual(req('/')[0],401)
                self.assertTrue('WWW-Authenticate' in req('/')[2])
                self.assertEqual(req('/',auth='viewer:wrong-password')[0],401)
                status,body,head=req('/',auth='viewer:testing-long-password')
                self.assertEqual(status,200)
                self.assertIn(b'paper dashboard',body)
                self.assertEqual(head['Cache-Control'],'no-store, no-cache, must-revalidate')
                self.assertEqual(req('/paper_state_v3.json',auth='viewer:testing-long-password')[0],404)
                self.assertEqual(req('/results/../paper_state_v3.json',auth='viewer:testing-long-password')[0],404)
                self.assertEqual(req('/comparison.html',auth='viewer:testing-long-password')[0],503)
            finally:
                server.shutdown();server.server_close();t.join(timeout=2)

if __name__=='__main__': unittest.main()
