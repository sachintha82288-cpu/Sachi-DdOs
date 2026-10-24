import argparse
import json
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import main


class TargetHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = b"local target is alive\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class ConsoleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.console = main.ConsoleServer(("127.0.0.1", 0), main.Handler)
        cls.console_thread = threading.Thread(target=cls.console.serve_forever, daemon=True)
        cls.console_thread.start()

        cls.target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
        cls.target_thread = threading.Thread(target=cls.target.serve_forever, daemon=True)
        cls.target_thread.start()

    @classmethod
    def tearDownClass(cls):
        main.stop_test()
        cls.console.shutdown()
        cls.target.shutdown()
        cls.console.server_close()
        cls.target.server_close()

    def setUp(self):
        self.wait_until_idle()
        with main.LOCK:
            main.STOP.clear()

    @classmethod
    def console_address(cls):
        return cls.console.server_address

    @classmethod
    def target_url(cls, path="/"):
        return f"http://127.0.0.1:{cls.target.server_address[1]}{path}"

    @staticmethod
    def wait_until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("condition did not become true before timeout")

    def wait_until_idle(self, timeout=5):
        self.wait_until(lambda: not main.snapshot()["running"], timeout=timeout)

    @classmethod
    def request(cls, method, path, body=None, headers=None):
        connection = HTTPConnection(*cls.console_address(), timeout=3)
        request_headers = headers or {}
        if body is not None and isinstance(body, dict):
            body = json.dumps(body).encode()
            request_headers = {"Content-Type": "application/json", **request_headers}
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
        connection.close()
        return status, response_headers, content

    def test_validation_accepts_loopback_and_rejects_other_hosts(self):
        config = main.validate({"target": "http://localhost:8081"})
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8081)

        valid_ipv6 = main.validate({"target": "http://[::1]:8081", "method": "HEAD"})
        self.assertEqual(valid_ipv6.host, "::1")
        self.assertEqual(valid_ipv6.method, "HEAD")

        invalid_payloads = [
            None,
            {},
            {"target": "https://example.com"},
            {"target": "http://192.168.1.1"},
            {"target": "http://127.0.0.1:bad"},
            {"target": "http://127.0.0.1:0"},
            {"target": "http://127.0.0.1", "rps_cap": 0},
            {"target": "http://127.0.0.1", "threads": 5},
            {"target": "http://127.0.0.1", "duration": True},
            {"target": "http://127.0.0.1", "method": "POST"},
            {"target": "http://name@127.0.0.1"},
            {"target": "http://127.0.0.1/#fragment"},
            {"target": "http://" + "1" * (main.MAX_TARGET_LENGTH + 1)},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                main.validate(payload)

    def test_port_argument_validation(self):
        self.assertEqual(main.valid_port("8080"), 8080)
        for value in ("0", "65536", "not-a-port"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                main.valid_port(value)

    def test_snapshot_is_read_only(self):
        before = main.snapshot()
        self.assertEqual(before, main.snapshot())
        before["status_codes"]["fake"] = 42
        before["rps_history"].append(99)
        current = main.snapshot()
        self.assertNotIn("fake", current["status_codes"])
        self.assertNotIn(99, current["rps_history"])

    def test_routes_and_security_headers(self):
        for path, status in [
            ("/", 200),
            ("/static/app.js", 200),
            ("/static/../main.py", 404),
            ("/api/stats", 200),
            ("/health", 200),
        ]:
            with self.subTest(path=path):
                response_status, headers, body = self.request("GET", path)
                self.assertEqual(response_status, status)
                self.assertTrue(body)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertIn("Content-Security-Policy", headers)

        status, headers, body = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["Content-Length"]), 0)

        status, _, body = self.request("POST", "/api/start", b"{bad", {"Content-Type": "application/json"})
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

        status, _, body = self.request("POST", "/api/unknown", {})
        self.assertEqual(status, 404)
        self.assertFalse(json.loads(body)["ok"])

        status, _, body = self.request(
            "POST",
            "/api/stop",
            {},
            {"Origin": "https://not-the-console.example"},
        )
        self.assertEqual(status, 403)
        self.assertFalse(json.loads(body)["ok"])

        same_origin = f"http://127.0.0.1:{self.console_address()[1]}"
        status, _, body = self.request("POST", "/api/stop", {}, {"Origin": same_origin})
        self.assertEqual(status, 409)
        self.assertFalse(json.loads(body)["ok"])

        status, _, body = self.request(
            "POST",
            "/api/stop",
            b"x" * (main.MAX_REQUEST_BODY + 1),
            {"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(body)["ok"])

    def test_sse_starts_with_a_snapshot(self):
        connection = HTTPConnection(*self.console_address(), timeout=3)
        connection.request("GET", "/events")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        first_line = response.readline()
        self.assertTrue(first_line.startswith(b"data: {"))
        connection.close()

    def test_start_run_publishes_real_metrics(self):
        status, _, body = self.request(
            "POST",
            "/api/start",
            {
                "target": self.target_url(),
                "method": "GET",
                "threads": 1,
                "duration": 1,
                "rps_cap": 3,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.wait_until_idle(timeout=4)

        stats = main.snapshot()
        self.assertGreaterEqual(stats["total"], 1)
        self.assertEqual(stats["total"], stats["success"] + stats["failed"])
        self.assertGreaterEqual(stats["success"], 1)
        self.assertEqual(stats["failed"], 0)
        self.assertEqual(stats["status_codes"].get("200"), stats["success"])
        self.assertGreater(stats["bytes_rx"], 0)
        self.assertTrue(stats["rps_history"])

    def test_second_start_is_rejected_and_stop_is_idempotent(self):
        payload = {
            "target": self.target_url(),
            "threads": 1,
            "duration": 10,
            "rps_cap": 1,
        }
        first_status, _, first_body = self.request("POST", "/api/start", payload)
        self.assertEqual(first_status, 200)
        self.assertTrue(json.loads(first_body)["ok"])
        self.wait_until(lambda: main.snapshot()["running"])

        second_status, _, second_body = self.request("POST", "/api/start", payload)
        self.assertEqual(second_status, 409)
        self.assertFalse(json.loads(second_body)["ok"])

        stop_status, _, stop_body = self.request("POST", "/api/stop", {})
        self.assertEqual(stop_status, 200)
        self.assertTrue(json.loads(stop_body)["ok"])
        self.wait_until_idle(timeout=4)

        idle_status, _, idle_body = self.request("POST", "/api/stop", {})
        self.assertEqual(idle_status, 409)
        self.assertFalse(json.loads(idle_body)["ok"])


if __name__ == "__main__":
    unittest.main()
