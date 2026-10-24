#!/usr/bin/env python3
"""Dependency-free, bounded loopback HTTP testing console."""

from __future__ import annotations

import argparse
import copy
import http.client
import ipaddress
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple, Optional
from urllib.parse import SplitResult, urlsplit

STATIC = Path(__file__).resolve().parent / "static"
MAX_REQUEST_BODY = 4096
MAX_TARGET_LENGTH = 2048
MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_TIMEOUT_SECONDS = 2
METRICS_INTERVAL_SECONDS = 0.25
SSE_INTERVAL_SECONDS = 0.25

LOCK = threading.RLock()
CONTROL = threading.Lock()
STOP = threading.Event()
STATE = {
    "running": False,
    "target": "",
    "total": 0,
    "success": 0,
    "failed": 0,
    "bytes_rx": 0,
    "elapsed": 0,
    "remaining": 0,
    "rps": 0,
    "peak_rps": 0,
    "avg_latency_ms": 0,
    "status_codes": {},
    "errors": [],
    "rps_history": [],
}


class TestConfig(NamedTuple):
    """Validated settings passed to a test run.

    The tuple shape is kept deliberately small and index-compatible with the
    original public ``validate`` helper while giving callers named fields.
    """

    target: str
    parsed: SplitResult
    host: str
    port: int
    method: str
    limits: tuple[int, int, int]


def validate(payload: object) -> TestConfig:
    """Validate and normalize a test request.

    Only literal loopback addresses and ``localhost`` are accepted.  Literal
    address validation avoids DNS rebinding and keeps the console local-only.
    """

    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object")

    raw_target = payload.get("target", "")
    if not isinstance(raw_target, str):
        raise ValueError("Target must be a URL string")
    target = raw_target.strip()
    if not target:
        raise ValueError("Enter an http:// or https:// loopback URL")
    if len(target) > MAX_TARGET_LENGTH:
        raise ValueError(f"Target URL must be {MAX_TARGET_LENGTH} characters or fewer")

    try:
        parsed = urlsplit(target)
        hostname = parsed.hostname
        explicit_port = parsed.port
    except ValueError as exc:
        raise ValueError("Enter a valid loopback URL") from exc

    if parsed.scheme not in ("http", "https") or not hostname:
        raise ValueError("Enter an http:// or https:// loopback URL")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Credentials and fragments are not supported")

    host = hostname.lower()
    if host == "localhost":
        connect_host = "127.0.0.1"
    else:
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("Only literal loopback addresses or localhost are allowed") from exc
        if not address.is_loopback:
            raise ValueError("Only literal loopback addresses or localhost are allowed")
        connect_host = host

    port = explicit_port if explicit_port is not None else (443 if parsed.scheme == "https" else 80)
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")

    method = payload.get("method", "GET")
    if method not in ("GET", "HEAD"):
        raise ValueError("Only GET and HEAD are supported")

    limits: list[int] = []
    for key, default, low, high in (
        ("threads", 2, 1, 4),
        ("duration", 10, 1, 60),
        ("rps_cap", 5, 1, 10),
    ):
        value = payload.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{key} must be an integer between {low} and {high}")
        limits.append(value)

    return TestConfig(target, parsed, connect_host, port, method, tuple(limits))


def snapshot() -> dict:
    """Return an isolated copy of the current metrics."""

    with LOCK:
        return copy.deepcopy(STATE)


def _record_error(message: str) -> None:
    with LOCK:
        STATE["errors"] = (STATE["errors"] + [message[:200]])[-20:]


def run(config: TestConfig) -> None:
    """Run one bounded test and publish metrics until every worker exits."""

    _target, parsed, host, port, method, (workers, duration, rate) = config
    started = time.monotonic()
    deadline = started + duration
    samples: deque[float] = deque(maxlen=1000)
    latencies: deque[float] = deque(maxlen=1000)
    next_slot = started
    slots_lock = threading.Lock()

    def worker() -> None:
        nonlocal next_slot
        while not STOP.is_set():
            with slots_lock:
                slot = max(time.monotonic(), next_slot)
                next_slot = slot + 1 / rate
            if slot >= deadline or STOP.wait(max(0, slot - time.monotonic())):
                break

            connection = None
            started_request = time.monotonic()
            status = None
            response_size = 0
            error = None
            try:
                connection_type = (
                    http.client.HTTPSConnection
                    if parsed.scheme == "https"
                    else http.client.HTTPConnection
                )
                connection = connection_type(host, port, timeout=REQUEST_TIMEOUT_SECONDS)
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                connection.request(
                    method,
                    path,
                    headers={
                        "User-Agent": "Sachi-Local-Console/1.0",
                        "Connection": "close",
                    },
                )
                response = connection.getresponse()
                status = response.status
                response_size = len(response.read(MAX_RESPONSE_BYTES))
                if not 200 <= status < 400:
                    error = f"HTTP {status}"
            except (OSError, http.client.HTTPException) as exc:
                detail = str(exc).strip() or "request failed"
                error = f"{type(exc).__name__}: {detail}"
            except Exception as exc:  # Keep one malformed response from orphaning a run.
                error = f"{type(exc).__name__}: request failed"
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass

            completed = time.monotonic()
            with LOCK:
                samples.append(completed)
                latencies.append((completed - started_request) * 1000)
                STATE["total"] += 1
                STATE["failed" if error else "success"] += 1
                STATE["bytes_rx"] += response_size
                STATE["avg_latency_ms"] = round(sum(latencies) / len(latencies), 2)
                if status is not None:
                    code = str(status)
                    STATE["status_codes"][code] = STATE["status_codes"].get(code, 0) + 1
                if error:
                    STATE["errors"] = (STATE["errors"] + [error])[-20:]

    threads = [
        threading.Thread(target=worker, name=f"sachi-worker-{index + 1}", daemon=True)
        for index in range(workers)
    ]

    started_threads: list[threading.Thread] = []
    try:
        for thread in threads:
            thread.start()
            started_threads.append(thread)

        while any(thread.is_alive() for thread in started_threads):
            now = time.monotonic()
            if now >= deadline:
                STOP.set()
            with LOCK:
                current_rps = sum(timestamp > now - 1 for timestamp in samples)
                STATE.update(
                    rps=current_rps,
                    peak_rps=max(STATE["peak_rps"], current_rps),
                    elapsed=round(now - started, 2),
                    remaining=round(max(0, deadline - now), 2),
                )
                STATE["rps_history"] = (STATE["rps_history"] + [current_rps])[-300:]
            time.sleep(METRICS_INTERVAL_SECONDS)
    except Exception as exc:
        _record_error(f"{type(exc).__name__}: test runner failed")
        STOP.set()
    finally:
        for thread in started_threads:
            thread.join(timeout=REQUEST_TIMEOUT_SECONDS + 1)
        with LOCK:
            STATE.update(
                running=False,
                rps=0,
                remaining=0,
                elapsed=round(time.monotonic() - started, 2),
            )


def start_test(payload: object) -> tuple[bool, str]:
    """Validate and start a test unless another run is active."""

    config = validate(payload)
    with CONTROL, LOCK:
        if STATE["running"]:
            return False, "A test is already running; stop it and wait for completion"
        STOP.clear()
        STATE.update(
            running=True,
            target=config.target,
            total=0,
            success=0,
            failed=0,
            bytes_rx=0,
            elapsed=0,
            remaining=config.limits[1],
            rps=0,
            peak_rps=0,
            avg_latency_ms=0,
            status_codes={},
            errors=[],
            rps_history=[],
        )
        threading.Thread(target=run, args=(config,), name="sachi-test-run", daemon=True).start()
    return True, "Local test started"


def stop_test() -> tuple[bool, str]:
    """Request cancellation and report whether a run was active."""

    with LOCK:
        if not STATE["running"]:
            return False, "No test is running"
        STOP.set()
    return True, "Stopping; waiting for in-flight requests"


class ConsoleServer(ThreadingHTTPServer):
    """HTTP server configured to let disconnected clients cleanly exit."""

    allow_reuse_address = True
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    """Small same-origin API and static-file handler."""

    server_version = "SachiConsole/1.1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    ASSETS = {
        "/": ("index.html", "text/html; charset=utf-8"),
        "/index.html": ("index.html", "text/html; charset=utf-8"),
        "/static/style.css": ("style.css", "text/css; charset=utf-8"),
        "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    }

    def log_message(self, *_args: object) -> None:
        pass

    def _send_headers(self, code: int, content_type: str, length: Optional[int] = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def send_body(
        self,
        code: int,
        body: object,
        content_type: str = "application/json; charset=utf-8",
        *,
        write: bool = True,
    ) -> None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self._send_headers(code, content_type, len(data))
        if write:
            self.wfile.write(data)

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _serve_asset(self, path: str, *, head_only: bool = False) -> None:
        asset = self.ASSETS.get(path)
        if asset is None:
            self.send_body(404, {"ok": False, "msg": "Not found"}, write=not head_only)
            return
        name, content_type = asset
        try:
            data = (STATIC / name).read_bytes()
        except OSError:
            self.send_body(500, {"ok": False, "msg": "Static asset unavailable"}, write=not head_only)
            return
        self.send_body(200, data, content_type, write=not head_only)

    def _send_events_headers(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.end_headers()

    def _serve_events(self) -> None:
        self._send_events_headers()
        try:
            while True:
                payload = json.dumps(snapshot(), separators=(",", ":"))
                self.wfile.write(("data: " + payload + "\n\n").encode("utf-8"))
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_SECONDS)
        except OSError:
            pass

    def _serve_get(self, *, head_only: bool = False) -> None:
        path = self._path()
        if path in self.ASSETS:
            self._serve_asset(path, head_only=head_only)
        elif path == "/api/stats":
            self.send_body(200, snapshot(), write=not head_only)
        elif path == "/health":
            self.send_body(200, {"ok": True, "service": "sachi-console"}, write=not head_only)
        elif path == "/events":
            if not head_only:
                self._serve_events()
            else:
                self._send_events_headers()
        else:
            self.send_body(404, {"ok": False, "msg": "Not found"}, write=not head_only)

    def do_GET(self) -> None:
        self._serve_get()

    def do_HEAD(self) -> None:
        self._serve_get(head_only=True)

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            origin_parts = urlsplit(origin)
        except ValueError:
            return False
        host = self.headers.get("Host", "")
        return bool(origin_parts.scheme and origin_parts.hostname and origin_parts.netloc) and (
            origin_parts.netloc.lower() == host.lower()
        )

    def do_POST(self) -> None:
        if not self._same_origin():
            self.send_body(403, {"ok": False, "msg": "Cross-origin control is disabled"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= MAX_REQUEST_BODY:
                self.close_connection = True
                raise ValueError("Request body exceeds limit")
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = self._path()
            if path == "/api/start":
                ok, message = start_test(payload)
                self.send_body(200 if ok else 409, {"ok": ok, "msg": message})
            elif path == "/api/stop":
                ok, message = stop_test()
                self.send_body(200 if ok else 409, {"ok": ok, "msg": message})
            else:
                self.send_body(404, {"ok": False, "msg": "Not found"})
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_body(400, {"ok": False, "msg": str(exc)})


def valid_port(value: str) -> int:
    """Argparse type for a TCP port."""

    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="console bind address")
    parser.add_argument("--port", type=valid_port, default=8080, help="console listen port")
    args = parser.parse_args()

    server = ConsoleServer((args.host, args.port), Handler)
    print(f"Sachi console on {args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STOP.set()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
