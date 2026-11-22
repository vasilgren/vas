#!/usr/bin/env python3
"""Railway / container entrypoint. Password-protected read-only browser dashboard.

Never places real orders and does not require crypto credentials.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

# Must be set before importing bot; bot resolves its data directory during import.
os.environ.setdefault('BOT_RESULTS_DIR', '/data')
os.environ['BOT_NO_BROWSER'] = '1'
import bot


def make_handler(password: str, output_dir: Path):
    """Allowlisted read-only files; do not expose account state or arbitrary paths."""
    if len(password) < 12:
        raise ValueError('Set DASHBOARD_PASSWORD to at least 12 characters (Railway Variables tab).')
    output_dir = Path(output_dir)

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = 'BTCPaperDashboard/1.0'
        sys_version = ''

        def _reply(self, code: int, body: bytes, *, content_type='text/plain; charset=utf-8', challenge=False):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            if challenge:
                self.send_header('WWW-Authenticate', 'Basic realm="BTC Paper Dashboard", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            header = self.headers.get('Authorization', '')
            scheme, _, token = header.partition(' ')
            if scheme.lower() != 'basic':
                return False
            try:
                raw = base64.b64decode(token, validate=True)
                return hmac.compare_digest(raw, ('viewer:' + password).encode('utf-8'))
            except (ValueError, binascii.Error):
                return False

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/health':
                # Liveness of the web service only, not proof Kraken polling works.
                self._reply(200, b'OK')
                return
            if not self._authorized():
                self._reply(401, b'Authentication required', challenge=True)
                return
            if path in ('/', '/live_dashboard.html'):
                file_path = output_dir / 'live_dashboard.html'
            elif path == '/comparison.html':
                file_path = output_dir / 'comparison.html'
            else:
                self._reply(404, b'Not found')
                return
            try:
                body = file_path.read_bytes()
            except FileNotFoundError:
                self._reply(503, b'Dashboard not generated yet. Check Railway deployment logs.')
                return
            except OSError:
                self._reply(503, b'Dashboard temporarily unavailable')
                return
            self._reply(200, body, content_type='text/html; charset=utf-8')

        def do_HEAD(self):
            self._reply(405, b'GET only')

        def do_POST(self):
            self._reply(405, b'GET only')

        def log_message(self, fmt: str, *args):
            # Do not record password or authorization headers.
            print('HTTP', fmt % args, flush=True)

    return DashboardHandler


def main():
    password = os.environ.get('DASHBOARD_PASSWORD', '')
    port = int(os.environ.get('PORT', '8080'))
    handler = make_handler(password, bot.OUT)
    bot.OUT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(('0.0.0.0', port), handler)
    thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.5}, daemon=True)
    thread.start()
    print(f'Cloud dashboard server listening on port {port}; state stored in {bot.OUT}', flush=True)
    print('PAPER MODE: no exchange authentication or real orders.', flush=True)
    try:
        bot.run_paper(poll=60)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
