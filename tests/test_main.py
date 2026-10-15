import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import main


class ConsoleTests(unittest.TestCase):
    def test_validation(self):
        config = main.validate({'target': 'http://localhost:8081'})
        self.assertEqual(config[2], '127.0.0.1')
        for payload in [None, {}, {'target': 'http://example.com'},
                        {'target': 'http://192.168.1.1'},
                        {'target': 'http://127.0.0.1', 'rps_cap': 0},
                        {'target': 'http://127.0.0.1', 'threads': 5},
                        {'target': 'http://127.0.0.1', 'duration': True},
                        {'target': 'http://127.0.0.1', 'method': 'POST'}]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                main.validate(payload)

    def test_snapshot_is_read_only(self):
        before = main.snapshot()
        self.assertEqual(before, main.snapshot())
        before['status_codes']['fake'] = 42
        self.assertNotIn('fake', main.snapshot()['status_codes'])

    def test_routes(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), main.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for path, status in [('/', 200), ('/static/app.js', 200),
                                 ('/static/../main.py', 404), ('/api/stats', 200)]:
                conn = HTTPConnection(*server.server_address)
                conn.request('GET', path)
                response = conn.getresponse()
                self.assertEqual(response.status, status)
                response.read()
                conn.close()
            conn = HTTPConnection(*server.server_address)
            conn.request('POST', '/api/start', b'{bad', {'Content-Type': 'application/json'})
            response = conn.getresponse()
            self.assertEqual(response.status, 400)
            self.assertFalse(json.loads(response.read())['ok'])
            conn.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
