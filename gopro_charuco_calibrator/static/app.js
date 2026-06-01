const form = document.getElementById("configForm");
const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const captureBtn = document.getElementById("captureBtn");
const solveBtn = document.getElementById("solveBtn");
const statusLine = document.getElementById("statusLine");
const preview = document.getElementById("preview");
const results = document.getElementById("results");
const scatter = document.getElementById("scatter");

let defaults = null;
let pollTimer = null;

function setDeep(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (const part of parts.slice(0, -1)) {
    cur[part] = cur[part] || {};
    cur = cur[part];
  }
  cur[parts[parts.length - 1]] = value;
}

function getDeep(obj, path) {
  return path.split(".").reduce((cur, part) => (cur ? cur[part] : undefined), obj);
}

function setFormValue(name, value) {
  const input = form.elements[name];
  if (!input) return;
  if (input.type === "checkbox") {
    input.checked = Boolean(value);
  } else if (input.dataset.mm === "true") {
    input.value = Number(value * 1000).toFixed(1);
  } else {
    input.value = value;
  }
}

function populateForm(config, dicts) {
  const dictSelect = form.elements["board.aruco_dict"];
  dictSelect.innerHTML = "";
  for (const name of dicts) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    dictSelect.append(option);
  }
  for (const input of form.elements) {
    if (!input.name) continue;
    setFormValue(input.name, getDeep(config, input.name));
  }
}

function readForm() {
  const config = structuredClone(defaults.config);
  for (const input of form.elements) {
    if (!input.name) continue;
    let value = input.type === "checkbox" ? input.checked : input.value;
    if (input.type === "number") value = Number(value);
    if (input.dataset.mm === "true") value = Number(value) / 1000;
    setDeep(config, input.name, value);
  }
  return config;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function percent(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

function updateBars(coverage) {
  document.getElementById("barX").value = coverage?.x?.progress || 0;
  document.getElementById("barY").value = coverage?.y?.progress || 0;
  document.getElementById("barSize").value = coverage?.size?.progress || 0;
  document.getElementById("barSkew").value = coverage?.skew?.progress || 0;
}

function drawScatter(points) {
  const ctx = scatter.getContext("2d");
  const w = scatter.width;
  const h = scatter.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#080a0d";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "#46505c";
  ctx.lineWidth = 1;
  for (let i = 1; i < 5; i++) {
    const x = (i / 5) * w;
    const y = (i / 5) * h;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  ctx.strokeStyle = "#d49a37";
  ctx.lineWidth = 2;
  ctx.strokeRect(0.2 * w, 0.2 * h, 0.6 * w, 0.6 * h);
  for (const point of points || []) {
    const x = point.x * w;
    const y = point.y * h;
    const r = 3 + 8 * Math.min(point.size || 0, 0.7);
    ctx.fillStyle = `rgba(74, 166, 255, ${0.45 + 0.45 * Math.min(point.skew || 0, 1)})`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }
}

function updateStatus(status) {
  statusLine.textContent = `${status.state || "idle"}: ${status.message || ""}`;
  const captures = status.captures || 0;
  const target = status.target_samples || 0;
  document.getElementById("captureCount").textContent = `${captures}/${target}`;
  document.getElementById("markerCount").textContent = status.markers || 0;
  document.getElementById("coverageOverall").textContent = percent(status.coverage?.overall);
  const p = status.pose;
  document.getElementById("poseText").textContent = p
    ? `${p.x.toFixed(2)} ${p.y.toFixed(2)} ${p.size.toFixed(2)} ${p.skew.toFixed(2)}`
    : "-";
  updateBars(status.coverage || {});
  drawScatter(status.coverage?.points || []);
  const active = status.state === "capturing";
  startBtn.disabled = active;
  stopBtn.disabled = !active;
  captureBtn.disabled = !active;
  solveBtn.disabled = captures < Math.max(3, Number(form.elements["solver.min_frames"]?.value || 25));
  if (active || captures > 0) {
    preview.src = `/api/session/latest.jpg?t=${Date.now()}`;
  }
  if (status.results) {
    results.textContent = JSON.stringify(status.results, null, 2);
  }
}

async function poll() {
  try {
    updateStatus(await api("/api/session/status"));
  } catch (err) {
    statusLine.textContent = String(err);
  }
}

startBtn.addEventListener("click", async () => {
  results.textContent = "";
  const status = await api("/api/session/start", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  });
  updateStatus(status);
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 500);
});

stopBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/stop", {method: "POST"}));
  clearInterval(pollTimer);
});

captureBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/capture", {method: "POST"}));
});

solveBtn.addEventListener("click", async () => {
  statusLine.textContent = "solving...";
  const summary = await api("/api/session/solve", {method: "POST"});
  results.textContent = JSON.stringify(summary.results, null, 2);
  await poll();
});

async function init() {
  defaults = await api("/api/defaults");
  populateForm(defaults.config, defaults.aruco_dictionaries);
  statusLine.textContent = "Ready";
  drawScatter([]);
  await poll();
}

init().catch((err) => {
  statusLine.textContent = String(err);
});
