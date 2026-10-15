<div align="center">

# ◢ SACHI-DDOS ◣
### LOCAL LAB / LIVE TELEMETRY

**Cyber-themed HTTP testing console · Zero dependencies · Real measurements**

`PYTHON 3.9+` &nbsp; / &nbsp; `VANILLA JS` &nbsp; / &nbsp; `STDLIB ONLY`

</div>

---

## A small console. A clear signal.

Sachi is a browser-based, **bounded loopback HTTP test app**. Configure a local
request run, watch its throughput and latency live, and inspect the results in
a terminal-style output panel. Despite the repository name, this implementation
is not a distributed denial-of-service tool and cannot target remote hosts.

```text
┌─ INPUT / CONFIG ──────┬─ REAL-TIME MONITOR ─────────────────┐
│ Target URL           │ RPS    PEAK    TOTAL    OK          │
│ Method · Workers     │ FAIL   AVG ms  BYTES    ELAPSED     │
│ Duration · Rate      │ ▂▃▅▆▄▅▇▆▃▅  rolling throughput     │
│                      ├─ OUTPUT / LOG ─────────────────────┤
│ [ START ] [ STOP ]   │ [12:30:00] Local test started       │
└──────────────────────┴────────────────────────────────────┘
```

## Launch in two terminals

No package manager, build step, CDN, fonts, or third-party libraries required.

```bash
# Terminal 1: a local target service
python3 -m http.server 8081 --bind 127.0.0.1
```

```bash
# Terminal 2: the console
python3 main.py
```

Open **http://127.0.0.1:8080**, leave the default target as
`http://127.0.0.1:8081`, and select **START**. On Windows, use `py` if `python3`
is unavailable. **STOP** cancels queued requests; in-flight requests finish or
time out before the run becomes idle.

### What you get

- **Cyber dashboard:** neon green/cyan, scanlines, responsive panels and visible focus states.
- **Input / output workflow:** local URL, GET/HEAD, workers, duration and rate controls.
- **Live telemetry:** SSE snapshots every 250 ms; current and peak completion rate,
  total/OK/failed counts, latency, bytes and elapsed time.
- **Canvas chart:** recent throughput history without a charting library.
- **Final results:** counters and elapsed time remain visible after completion.
- **Bounded execution:** 1–4 workers, 1–10 requests/second globally, 1–60 seconds.
- **Local only:** literal loopback IP addresses and `localhost`; redirects are not followed.

## Run options

| Option | Default | Purpose |
| --- | --- | --- |
| `--host` | `127.0.0.1` | Console bind address |
| `--port` | `8080` | Console listen port |

For a hosted development preview:

```bash
python3 main.py --host 0.0.0.0 --port 8099
```

The console has no authentication. Keep it on a trusted development network.
Loopback always refers to the machine running Python, not the browser's machine.
There is no external-target mode.

## How measurements work

Workers share a paced request scheduler. RPS counts **completed attempts over the
last second**, including errors; request starts are limited to the configured rate.
HTTP 200–399 responses count as OK; other responses and connection errors count as
failed. Average latency covers the latest 1,000 attempts. Each response is read up
to **64 KiB**, so bytes reflect sampled response bodies, not full network traffic.
Requests use a two-second socket timeout. SSE clients read snapshots without
changing counters or chart history. Results are in memory and reset on the next run.

## Project map

```text
main.py               HTTP server, validation, paced workers, SSE
static/index.html     Dashboard shell and configuration
static/style.css      Cyber theme and mobile layout
static/app.js         Controls, live metrics, canvas and log
tests/test_main.py    Validation, snapshot and HTTP route tests
```

### API

| Route | Action |
| --- | --- |
| `GET /api/stats` | Read a JSON snapshot |
| `GET /events` | Subscribe to SSE snapshots |
| `POST /api/start` | Start a validated local test |
| `POST /api/stop` | Request cancellation |

Start body example:

```json
{"target":"http://127.0.0.1:8081","method":"GET","threads":2,"duration":10,"rps_cap":5}
```

## Verify

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile main.py
```

## Limitations

This is an HTTP lab console, not a packet generator, distributed runner or
production observability system. It does not simulate browser rendering or measure
server CPU/memory. HTTPS uses Python's default certificate validation. Closing the
browser does not cancel a run; its configured duration still applies.
