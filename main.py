#!/usr/bin/env python3
"""Dependency-free, bounded loopback HTTP testing console."""
import argparse
import http.client
import ipaddress
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

STATIC = Path(__file__).resolve().parent / 'static'
LOCK = threading.RLock()
CONTROL = threading.Lock()
STATE = dict(running=False, target='', total=0, success=0, failed=0,
             bytes_rx=0, elapsed=0, remaining=0, rps=0, peak_rps=0,
             avg_latency_ms=0, status_codes={}, errors=[], rps_history=[])
STOP = threading.Event()


def validate(payload):
    if not isinstance(payload, dict):
        raise ValueError('Expected a JSON object')
    target = str(payload.get('target', '')).strip()
    parsed = urlsplit(target)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Enter an http:// or https:// loopback URL')
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Credentials and fragments are not supported')
    host = parsed.hostname
    if host == 'localhost':
        host = '127.0.0.1'
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError()
    except ValueError:
        raise ValueError('Only literal loopback addresses or localhost are allowed')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    method = payload.get('method', 'GET')
    if method not in ('GET', 'HEAD'):
        raise ValueError('Only GET and HEAD are supported')
    values = []
    for key, default, low, high in [('threads', 2, 1, 4), ('duration', 10, 1, 60), ('rps_cap', 5, 1, 10)]:
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f'{key} must be an integer between {low} and {high}')
        values.append(value)
    return target, parsed, host, port, method, values


def snapshot():
    with LOCK:
        return json.loads(json.dumps(STATE))


def run(config):
    target, parsed, host, port, method, (workers, duration, rate) = config
    started = time.monotonic()
    deadline = started + duration
    samples = deque(maxlen=1000)
    latencies = deque(maxlen=1000)
    next_slot = started
    slots_lock = threading.Lock()

    def worker():
        nonlocal next_slot
        while not STOP.is_set():
            with slots_lock:
                slot = max(time.monotonic(), next_slot)
                next_slot = slot + 1 / rate
            if slot >= deadline or STOP.wait(max(0, slot - time.monotonic())):
                break
            conn = None
            t0 = time.monotonic()
            status, size, error = None, 0, None
            try:
                cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
                conn = cls(host, port, timeout=2)
                path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')
                conn.request(method, path, headers={'User-Agent': 'Sachi-Local-Console/1.0', 'Connection': 'close'})
                response = conn.getresponse()
                status = response.status
                size = len(response.read(65536))
                if not 200 <= status < 400:
                    error = f'HTTP {status}'
            except (OSError, http.client.HTTPException) as exc:
                error = f'{type(exc).__name__}: {exc}'
            finally:
                if conn:
                    conn.close()
            now = time.monotonic()
            with LOCK:
                samples.append(now)
                latencies.append((now - t0) * 1000)
                STATE['total'] += 1
                STATE['failed' if error else 'success'] += 1
                STATE['bytes_rx'] += size
                STATE['avg_latency_ms'] = round(sum(latencies) / len(latencies), 2)
                if status is not None:
                    key = str(status)
                    STATE['status_codes'][key] = STATE['status_codes'].get(key, 0) + 1
                if error:
                    STATE['errors'] = (STATE['errors'] + [error])[-20:]

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        now = time.monotonic()
        if now >= deadline:
            STOP.set()
        with LOCK:
            rps = sum(t > now - 1 for t in samples)
            STATE.update(rps=rps, peak_rps=max(STATE['peak_rps'], rps),
                         elapsed=round(now - started, 2), remaining=round(max(0, deadline - now), 2))
            STATE['rps_history'] = (STATE['rps_history'] + [rps])[-300:]
        time.sleep(.25)
    with LOCK:
        STATE.update(running=False, rps=0, remaining=0, elapsed=round(time.monotonic() - started, 2))


def start_test(payload):
    config = validate(payload)
    with CONTROL, LOCK:
        if STATE['running']:
            return False, 'A test is already running; stop it and wait for completion'
        STOP.clear()
        STATE.update(running=True, target=config[0], total=0, success=0, failed=0,
                     bytes_rx=0, elapsed=0, remaining=config[5][1], rps=0, peak_rps=0,
                     avg_latency_ms=0, status_codes={}, errors=[], rps_history=[])
        threading.Thread(target=run, args=(config,), daemon=True).start()
    return True, 'Local test started'


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_body(self, code, body, content_type='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/index.html': ('index.html', 'text/html; charset=utf-8'),
                  '/static/style.css': ('style.css', 'text/css'),
                  '/static/app.js': ('app.js', 'application/javascript')}
        if path in assets:
            name, mime = assets[path]
            self.send_body(200, (STATIC / name).read_bytes(), mime)
        elif path == '/api/stats':
            self.send_body(200, snapshot())
        elif path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.end_headers()
            try:
                while True:
                    self.wfile.write(('data: ' + json.dumps(snapshot()) + '\n\n').encode())
                    self.wfile.flush()
                    time.sleep(.25)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_body(404, {'ok': False, 'msg': 'Not found'})

    def do_POST(self):
        # Browser cross-origin control requests are not accepted.
        origin = self.headers.get('Origin')
        if origin and urlsplit(origin).netloc != self.headers.get('Host'):
            self.send_body(403, {'ok': False, 'msg': 'Cross-origin control is disabled'})
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 <= length <= 4096:
                raise ValueError('Request body exceeds limit')
            payload = json.loads(self.rfile.read(length) or b'{}')
            if self.path == '/api/start':
                ok, msg = start_test(payload)
                self.send_body(200 if ok else 409, {'ok': ok, 'msg': msg})
            elif self.path == '/api/stop':
                STOP.set()
                self.send_body(200, {'ok': True, 'msg': 'Stopping; waiting for in-flight requests'})
            else:
                self.send_body(404, {'ok': False, 'msg': 'Not found'})
        except (ValueError, TypeError) as exc:
            self.send_body(400, {'ok': False, 'msg': str(exc)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Sachi console on {args.host}:{args.port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STOP.set()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
