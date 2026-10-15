const $ = (id) => document.getElementById(id);
const term = $("term");
const codes = $("codes");
const chart = $("chart");
const ctx = chart.getContext("2d");

let running = false;
let rpsHist = [];

function log(msg, cls = "") {
  const div = document.createElement("div");
  div.className = "line " + cls;
  const ts = new Date().toLocaleTimeString();
  div.textContent = `[${ts}] ${msg}`;
  term.appendChild(div);
  term.scrollTop = term.scrollHeight;
  // cap
  while (term.childElementCount > 500) term.removeChild(term.firstChild);
}

function fmt(n) {
  if (n >= 1e6) return (n/1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n/1e3).toFixed(1) + "k";
  return String(n);
}

function updateStats(s) {
  if (running && !s.running) log("Test complete: " + s.total + " requests, " + s.failed + " failed", "info");
  running = s.running;
  $("sRps").textContent = s.rps;
  $("sPeak").textContent = s.peak_rps;
  $("sTotal").textContent = fmt(s.total);
  $("sOk").textContent = fmt(s.success);
  $("sFail").textContent = fmt(s.failed);
  $("sLat").textContent = s.avg_latency_ms;
  $("sBytes").textContent = fmt(s.bytes_rx);
  $("sElapsed").textContent = s.elapsed.toFixed(2);

  // status codes
  codes.innerHTML = "";
  for (const [code, count] of Object.entries(s.status_codes)) {
    const pill = document.createElement("span");
    pill.className = "code-pill";
    pill.textContent = `${code}: ${count}`;
    codes.appendChild(pill);
  }

  // chart
  rpsHist = s.rps_history || [];
  drawChart();

  // buttons
  $("startBtn").disabled = s.running;
  $("stopBtn").disabled = !s.running;

  // conn dot
  const dot = $("connDot");
  const txt = $("connText");
  dot.className = "dot on";
  txt.textContent = s.running ? "RUNNING" : "IDLE";
  if (s.running) dot.classList.add("run");
}

function drawChart() {
  const w = chart.clientWidth || 900;
  const h = chart.clientHeight || 220;
  if (chart.width !== w) chart.width = w;
  if (chart.height !== h) chart.height = h;
  const W = chart.width, H = chart.height;
  ctx.clearRect(0, 0, W, H);

  // grid
  ctx.strokeStyle = "#00ff4115";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 5; i++) {
    const y = (H / 5) * i;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }
  for (let i = 0; i <= 10; i++) {
    const x = (W / 10) * i;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }

  if (rpsHist.length < 2) return;
  const maxv = Math.max(10, ...rpsHist);
  ctx.strokeStyle = "#00ff41";
  ctx.lineWidth = 2;
  ctx.shadowColor = "#00ff41";
  ctx.shadowBlur = 6;
  ctx.beginPath();
  rpsHist.forEach((v, i) => {
    const x = (i / (rpsHist.length - 1)) * W;
    const y = H - (v / maxv) * (H - 10) - 5;
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.shadowBlur = 0;

  // label
  ctx.fillStyle = "#00ff41";
  ctx.font = "11px Consolas, monospace";
  ctx.fillText("max " + maxv + " rps", 8, 14);
}

// SSE
let es = null;
function connect() {
  es = new EventSource("/events");
  es.onmessage = (e) => {
    try { updateStats(JSON.parse(e.data)); } catch (err) {}
  };
  es.onerror = () => {
    $("connDot").className = "dot";
    $("connText").textContent = "RECONNECTING";
    setTimeout(connect, 1000);
    try { es.close(); } catch (e) {}
  };
}
connect();

// buttons
$("startBtn").addEventListener("click", async () => {
  const payload = {
    target: $("target").value.trim(),
    method: $("method").value,
    threads: parseInt($("threads").value, 10),
    duration: parseInt($("duration").value, 10),
    rps_cap: parseInt($("rps").value, 10),
  };
  $("msg").textContent = "";
  log("starting test -> " + payload.target, "info");
  try {
    const res = await fetch("/api/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await res.json();
    $("msg").textContent = data.msg;
    if (data.ok) log("started: " + JSON.stringify(payload), "ok");
    else log("start failed: " + data.msg, "err");
  } catch (e) {
    log("request error: " + e, "err");
  }
});

$("stopBtn").addEventListener("click", async () => {
  try {
    const res = await fetch("/api/stop", { method: "POST" });
    const data = await res.json();
    $("msg").textContent = data.msg;
    log("stopped by user", "dim");
  } catch (e) {
    log("stop error: " + e, "err");
  }
});

window.addEventListener("resize", drawChart);
log("SACHI-DDOS console ready. localhost targets allowed by default.", "dim");
