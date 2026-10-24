const $ = (id) => document.getElementById(id);
const term = $("term");
const codes = $("codes");
const chart = $("chart");
const ctx = chart.getContext("2d");

let running = false;
let rpsHist = [];
let eventSource = null;
let reconnectTimer = null;
let requestInFlight = false;

function log(message, cls = "") {
  const line = document.createElement("div");
  line.className = `line ${cls}`.trim();
  const timestamp = new Date().toLocaleTimeString();
  line.textContent = `[${timestamp}] ${String(message)}`;
  term.appendChild(line);
  term.scrollTop = term.scrollHeight;
  while (term.childElementCount > 500) term.removeChild(term.firstChild);
}

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function fmt(value) {
  const number = finiteNumber(value);
  if (number >= 1e6) return `${(number / 1e6).toFixed(2)}M`;
  if (number >= 1e3) return `${(number / 1e3).toFixed(1)}k`;
  return String(Math.round(number));
}

function setConnectionState(state) {
  const dot = $("connDot");
  const text = $("connText");
  dot.className = "dot";
  if (state === "connected") {
    dot.classList.add(running ? "run" : "on");
    text.textContent = running ? "RUNNING" : "IDLE";
  } else if (state === "connecting") {
    text.textContent = "CONNECTING";
  } else {
    text.textContent = "RECONNECTING";
  }
}

function updateStats(stats) {
  if (!stats || typeof stats !== "object") return;

  const wasRunning = running;
  running = Boolean(stats.running);
  if (wasRunning && !running) {
    log(
      `Test complete: ${fmt(stats.total)} requests, ${fmt(stats.failed)} failed`,
      "info",
    );
  }

  $("sRps").textContent = fmt(stats.rps);
  $("sPeak").textContent = fmt(stats.peak_rps);
  $("sTotal").textContent = fmt(stats.total);
  $("sOk").textContent = fmt(stats.success);
  $("sFail").textContent = fmt(stats.failed);
  $("sLat").textContent = finiteNumber(stats.avg_latency_ms).toFixed(2);
  $("sBytes").textContent = fmt(stats.bytes_rx);
  $("sElapsed").textContent = finiteNumber(stats.elapsed).toFixed(2);

  codes.replaceChildren();
  const statusCodes = stats.status_codes && typeof stats.status_codes === "object"
    ? stats.status_codes
    : {};
  Object.entries(statusCodes)
    .sort(([a], [b]) => Number(a) - Number(b))
    .forEach(([code, count]) => {
      const pill = document.createElement("span");
      pill.className = "code-pill";
      pill.textContent = `${code}: ${fmt(count)}`;
      codes.appendChild(pill);
    });

  rpsHist = Array.isArray(stats.rps_history)
    ? stats.rps_history.map((value) => Math.max(0, finiteNumber(value)))
    : [];
  drawChart();

  $("startBtn").disabled = running || requestInFlight;
  $("stopBtn").disabled = !running || requestInFlight;
  if (eventSource) setConnectionState("connected");
}

function drawChart() {
  const width = chart.clientWidth || 900;
  const height = chart.clientHeight || 220;
  if (chart.width !== width) chart.width = width;
  if (chart.height !== height) chart.height = height;

  const W = chart.width;
  const H = chart.height;
  ctx.clearRect(0, 0, W, H);

  ctx.strokeStyle = "#00ff4115";
  ctx.lineWidth = 1;
  for (let row = 0; row <= 5; row += 1) {
    const y = (H / 5) * row;
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(W, y);
    ctx.stroke();
  }
  for (let column = 0; column <= 10; column += 1) {
    const x = (W / 10) * column;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, H);
    ctx.stroke();
  }

  if (rpsHist.length < 2) return;
  const maxValue = Math.max(10, ...rpsHist);
  ctx.strokeStyle = "#00ff41";
  ctx.lineWidth = 2;
  ctx.shadowColor = "#00ff41";
  ctx.shadowBlur = 6;
  ctx.beginPath();
  rpsHist.forEach((value, index) => {
    const x = (index / (rpsHist.length - 1)) * W;
    const y = H - (value / maxValue) * (H - 10) - 5;
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0;

  ctx.fillStyle = "#00ff41";
  ctx.font = "11px Consolas, monospace";
  ctx.fillText(`max ${maxValue} rps`, 8, 14);
}

function scheduleReconnect() {
  if (reconnectTimer !== null) return;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    connectEvents();
  }, 1500);
}

function connectEvents() {
  if (eventSource) eventSource.close();
  setConnectionState("connecting");

  const source = new EventSource("/events");
  eventSource = source;
  source.onopen = () => setConnectionState("connected");
  source.onmessage = (event) => {
    try {
      updateStats(JSON.parse(event.data));
    } catch (error) {
      log(`telemetry parse error: ${error.message}`, "err");
    }
  };
  source.onerror = () => {
    if (eventSource !== source) return;
    source.close();
    eventSource = null;
    setConnectionState("reconnecting");
    scheduleReconnect();
  };
}

async function postJson(path, payload = {}) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data;
  try {
    data = await response.json();
  } catch (_error) {
    throw new Error(`server returned HTTP ${response.status}`);
  }
  if (!response.ok) throw new Error(data.msg || `server returned HTTP ${response.status}`);
  return data;
}

$("startBtn").addEventListener("click", async () => {
  if (requestInFlight || running) return;
  const payload = {
    target: $("target").value.trim(),
    method: $("method").value,
    threads: Number.parseInt($("threads").value, 10),
    duration: Number.parseInt($("duration").value, 10),
    rps_cap: Number.parseInt($("rps").value, 10),
  };

  requestInFlight = true;
  $("startBtn").disabled = true;
  $("msg").textContent = "";
  log(`starting test -> ${payload.target}`, "info");
  try {
    const data = await postJson("/api/start", payload);
    $("msg").textContent = data.msg;
    log(`started: ${JSON.stringify(payload)}`, "ok");
  } catch (error) {
    $("msg").textContent = error.message;
    log(`start failed: ${error.message}`, "err");
  } finally {
    requestInFlight = false;
    $("startBtn").disabled = running;
    $("stopBtn").disabled = !running;
  }
});

$("stopBtn").addEventListener("click", async () => {
  if (requestInFlight || !running) return;
  requestInFlight = true;
  $("stopBtn").disabled = true;
  try {
    const data = await postJson("/api/stop");
    $("msg").textContent = data.msg;
    log(data.msg, "dim");
  } catch (error) {
    $("msg").textContent = error.message;
    log(`stop failed: ${error.message}`, "err");
  } finally {
    requestInFlight = false;
    $("stopBtn").disabled = !running;
  }
});

window.addEventListener("resize", drawChart);
window.addEventListener("beforeunload", () => {
  if (eventSource) eventSource.close();
});

log("SACHI-DDOS console ready. localhost targets allowed by default.", "dim");
connectEvents();
