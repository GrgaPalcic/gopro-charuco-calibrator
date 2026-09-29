const form = document.getElementById("configForm");
const previewBtn = document.getElementById("previewBtn");
const startRunBtn = document.getElementById("startRunBtn");
const stopBtn = document.getElementById("stopBtn");
const pauseResumeBtn = document.getElementById("pauseResumeBtn");
const nextCameraBtn = document.getElementById("nextCameraBtn");
const captureBtn = document.getElementById("captureBtn");
const solveBtn = document.getElementById("solveBtn");
const statusLine = document.getElementById("statusLine");
const preview = document.getElementById("preview");
const previewEmpty = document.getElementById("previewEmpty");
const results = document.getElementById("results");
const resultsPanel = document.getElementById("resultsPanel");
const verdictBox = document.getElementById("verdict");
const verdictBadge = document.getElementById("verdictBadge");
const verdictText = document.getElementById("verdictText");
const verdictReasons = document.getElementById("verdictReasons");
const recFigures = document.getElementById("recFigures");
const resultGrid = document.getElementById("resultGrid");
const resultsRaw = document.getElementById("resultsRaw");
const scatter = document.getElementById("scatter");
const guideOverlay = document.getElementById("guideOverlay");
const guidePrompt = document.getElementById("guidePrompt");
const streamStatus = document.getElementById("streamStatus");
const streamStatusText = document.getElementById("streamStatusText");
const cameraReadout = document.getElementById("cameraReadout");
const goproControls = document.getElementById("goproControls");
const deviceDetails = document.getElementById("deviceDetails");
const boardSummary = document.getElementById("boardSummary");
const steps = {
  connect: document.getElementById("stepConnect"),
  capture: document.getElementById("stepCapture"),
  solve: document.getElementById("stepSolve"),
};

const presetSelect = document.getElementById("presetSelect");
const presetLoad = document.getElementById("presetLoad");
const presetSave = document.getElementById("presetSave");
const presetMsg = document.getElementById("presetMsg");

const firewallPanel = document.getElementById("firewallPanel");
const firewallHint = document.getElementById("firewallHint");
const firewallCmd = document.getElementById("firewallCmd");
const firewallFirewalld = document.getElementById("firewallFirewalld");
const firewallIptables = document.getElementById("firewallIptables");
const firewallRaw = document.getElementById("firewallRaw");
const firewallCopy = document.getElementById("firewallCopy");
const firewallCopyResult = document.getElementById("firewallCopyResult");
const firewallDismiss = document.getElementById("firewallDismiss");

const STREAM_URL = "/api/session/stream.mjpg";
const FALLBACK_URL = "/api/session/latest.jpg";

// Canvas colours, mirroring the CSS tokens in style.css.
const COLOR = {
  field: "#0b0c0e",
  grid: "rgba(155, 154, 149, 0.14)",
  frame: "#454a54",
  signal: "#e9a23b",
  signalSoft: "rgba(233, 162, 59, 0.45)",
  ink: "rgba(232, 230, 225, 0.8)",
  inkFill: "rgba(232, 230, 225, 0.1)",
  muted: "rgba(155, 154, 149, 0.8)",
  pass: "#7fbf8e",
  fail: "#e06a5a",
  target: "#6f9fd8",
};

// Human names for the session states, for the status line.
const STATE_LABEL = {
  idle: "Idle",
  preview: "Previewing",
  capturing: "Capturing",
  paused: "Paused",
  complete: "Route complete",
  solving: "Solving",
  solved: "Solved",
  error: "Error",
};

let defaults = null;
let pollTimer = null;
let streamActive = false;
let fallbackTimer = null;
let firewallDismissed = false;
let lastFirewallCmd = "";
let autoSolved = false;
let currentState = "idle";
let lastStatus = null;

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

function optionLabel(value, label) {
  return `${label} (${value})`;
}

function populateGoProOptions(options) {
  for (const select of form.querySelectorAll("[data-gopro-options]")) {
    const key = select.dataset.goproOptions;
    const def = options[key];
    if (!def) continue;
    select.innerHTML = "";
    if (select.dataset.optional === "true") {
      select.append(new Option("Leave unchanged", ""));
    }
    for (const [value, label] of Object.entries(def.options || {})) {
      select.append(new Option(optionLabel(value, label), value));
    }
  }
}

// The backend solver.models is a list; the form exposes it as one dropdown.
const MODEL_SETS = {
  pinhole: ["plumb_bob", "rational_polynomial"],
  fisheye: ["fisheye"],
  fisheye_ds: ["fisheye", "double_sphere"],
  double_sphere: ["double_sphere"],
  both: ["plumb_bob", "rational_polynomial", "fisheye"],
};

function modelsToKey(models) {
  const want = JSON.stringify([...(models || [])].sort());
  for (const [key, set] of Object.entries(MODEL_SETS)) {
    if (JSON.stringify([...set].sort()) === want) return key;
  }
  return "pinhole";
}

function setFormValue(name, value) {
  const input = form.elements[name];
  if (!input) return;
  if (name === "solver.models") {
    input.value = modelsToKey(value);
    return;
  }
  if (input.type === "checkbox") {
    input.checked = Boolean(value);
  } else if (input.dataset.mm === "true") {
    input.value = Number(value * 1000).toFixed(1);
  } else if (input.dataset.optional === "true" && value === null) {
    input.value = "";
  } else {
    input.value = value ?? "";
  }
}

function populateForm(config, dicts, goproOptions) {
  populateGoProOptions(goproOptions);
  const dictSelect = form.elements["board.aruco_dict"];
  dictSelect.innerHTML = "";
  for (const name of dicts) {
    dictSelect.append(new Option(name, name));
  }
  for (const input of form.elements) {
    if (!input.name) continue;
    setFormValue(input.name, getDeep(config, input.name));
  }
  syncGoproVisibility();
  updateBoardSummary();
}

function readForm() {
  const config = structuredClone(defaults.config);
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.name === "solver.models") {
      setDeep(config, input.name, MODEL_SETS[input.value] || MODEL_SETS.pinhole);
      continue;
    }
    let value = input.type === "checkbox" ? input.checked : input.value;
    if (input.dataset.optional === "true" && value === "") {
      value = null;
    } else if (input.type === "number" || input.dataset.goproOptions) {
      value = value === "" ? null : Number(value);
    }
    if (input.dataset.mm === "true") value = Number(input.value) / 1000;
    setDeep(config, input.name, value);
  }
  return config;
}

function syncGoproVisibility() {
  const enabled = form.elements["gopro.enabled"];
  if (!enabled) return;
  if (goproControls) goproControls.hidden = !enabled.checked;
  // Device and pixel format matter only for a plain camera: show them then.
  if (deviceDetails && !enabled.checked) deviceDetails.open = true;
}

function updateBoardSummary() {
  const value = (name) => form.elements[name]?.value;
  const dict = (value("board.aruco_dict") || "").replace(/^DICT_/, "");
  boardSummary.textContent =
    `${value("board.cols")}×${value("board.rows")} · ${dict} · `
    + `${Number(value("board.square_m"))}/${Number(value("board.marker_m"))} mm`;
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

function num(value, digits = 2) {
  return value == null || Number.isNaN(value) ? "n/a" : Number(value).toFixed(digits);
}

// ---- Stream ----

function setStreamStatus(state, text) {
  streamStatus.dataset.state = state;
  streamStatusText.textContent = text;
}

function startStream() {
  if (streamActive) return;
  streamActive = true;
  clearInterval(fallbackTimer);
  fallbackTimer = null;
  setStreamStatus("connecting", "connecting…");
  preview.src = STREAM_URL;
}

function stopStream() {
  if (!streamActive && !fallbackTimer) {
    setStreamStatus("none", "no stream");
    return;
  }
  streamActive = false;
  clearInterval(fallbackTimer);
  fallbackTimer = null;
  preview.removeAttribute("src");
  setStreamStatus("none", "no stream");
}

function startFallback() {
  if (fallbackTimer) return;
  setStreamStatus("error", "stream error, using snapshots");
  fallbackTimer = setInterval(() => {
    preview.src = `${FALLBACK_URL}?t=${Date.now()}`;
  }, 500);
}

function syncStream(state) {
  const previewOpen = !["idle", "error"].includes(state);
  if (previewOpen) startStream();
  else stopStream();
}

preview.addEventListener("load", () => {
  if (!streamActive) return;
  if (fallbackTimer) setStreamStatus("error", "stream blocked, snapshots");
  else setStreamStatus("streaming", "streaming");
});

preview.addEventListener("error", () => {
  if (streamActive && !fallbackTimer) startFallback();
});

// ---- Firewall ----

function extractUfwFromError(error) {
  const match = /sudo ufw allow[^\n]*/.exec(error || "");
  return match ? match[0] : "";
}

function renderFirewall(bridge, fwCmd) {
  const blocked = bridge && bridge.enabled && bridge.ok === false && fwCmd && !firewallDismissed;
  if (!blocked) {
    firewallPanel.hidden = true;
    return;
  }
  firewallPanel.hidden = false;
  firewallHint.textContent =
    bridge.firewall_hint ||
    "HTTP control works but incoming GoPro UDP video is blocked. Allow it once with:";
  firewallCmd.textContent = fwCmd;
  lastFirewallCmd = fwCmd;
  firewallFirewalld.textContent = bridge.firewalld_command || "(unavailable)";
  firewallIptables.textContent = bridge.iptables_command || "(unavailable)";
  firewallRaw.textContent = JSON.stringify(bridge, null, 2);
}

// ---- Camera readout ----

const READOUT_KEYS = {
  webcam_digital_lens: "Lens",
  max_lens_mod: "Mod",
  hypersmooth: "HyperSmooth",
};

function chip(text, {key = "", kind = ""} = {}) {
  const el = document.createElement("span");
  el.className = `chip${kind ? ` chip-${kind}` : ""}`;
  if (key) {
    const k = document.createElement("span");
    k.className = "chip-key";
    k.textContent = key;
    el.append(k);
  }
  el.append(document.createTextNode(text));
  return el;
}

// What the camera itself reported after the webcam started (read back, never
// assumed), plus any mismatch with what was requested.
function renderReadout(gopro, config) {
  const chips = [];
  const labels = gopro?.camera_state?.labels;
  if (labels && Object.keys(labels).length) {
    for (const [field, label] of Object.entries(labels)) {
      chips.push(chip(label, {key: READOUT_KEYS[field] || field}));
    }
  } else if (gopro?.enabled) {
    chips.push(chip("camera did not report its settings", {kind: "muted"}));
  } else if (config?.gopro?.enabled) {
    chips.push(chip("camera settings appear after Open preview", {kind: "muted"}));
  }
  for (const warning of gopro?.warnings || []) {
    chips.push(chip(warning, {key: "Check", kind: "warn"}));
  }
  cameraReadout.replaceChildren(...chips);
}

// ---- Steps ----

function stepStates(state, captures, minFrames) {
  const live = !["idle", "error"].includes(state);
  const capturing = ["capturing", "paused"].includes(state);
  const captured = ["complete", "solving", "solved"].includes(state);
  let connect = live ? "done" : "current";
  let capture = "pending";
  let solve = "pending";
  if (live) capture = captured ? "done" : "current";
  if (state === "solved") solve = "done";
  else if (state === "solving" || state === "complete") solve = "current";
  else if (capturing && captures >= minFrames) solve = "current";
  if (state === "error") connect = "current";
  return {connect, capture, solve};
}

function renderSteps(state, captures, minFrames) {
  for (const [name, value] of Object.entries(stepStates(state, captures, minFrames))) {
    steps[name].dataset.stepState = value;
  }
}

// ---- Canvases ----

function fitCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(1, rect.width);
  const cssH = Math.max(1, rect.height);
  const pixelW = Math.round(cssW * dpr);
  const pixelH = Math.round(cssH * dpr);
  if (canvas.width !== pixelW || canvas.height !== pixelH) {
    canvas.width = pixelW;
    canvas.height = pixelH;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  return {ctx, cssW, cssH};
}

function updateBars(coverage) {
  document.getElementById("barX").value = coverage?.x?.progress || 0;
  document.getElementById("barY").value = coverage?.y?.progress || 0;
  document.getElementById("barSize").value = coverage?.size?.progress || 0;
  document.getElementById("barSkew").value = coverage?.skew?.progress || 0;
}

// The coverage map is the image field itself (same aspect as the stream).
// Each captured view is drawn as its board footprint, so it is obvious whether
// the board reached the edges, which is what decides wide-lens model quality.
function drawCoverageMap(points, rejected, imageSize) {
  const {ctx, cssW, cssH} = fitCanvas(scatter);
  const [natW, natH] = imageSize?.length === 2 ? imageSize : [16, 9];
  const scale = Math.min(cssW / natW, cssH / natH);
  const w = natW * scale;
  const h = natH * scale;
  const ox = (cssW - w) / 2;
  const oy = (cssH - h) / 2;
  const px = (x) => ox + x * w;
  const py = (y) => oy + y * h;

  ctx.fillStyle = COLOR.field;
  ctx.fillRect(ox, oy, w, h);
  ctx.strokeStyle = COLOR.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 1; i < 10; i++) {
    ctx.moveTo(px(i / 10), oy);
    ctx.lineTo(px(i / 10), oy + h);
    ctx.moveTo(ox, py(i / 10));
    ctx.lineTo(ox + w, py(i / 10));
  }
  ctx.stroke();
  ctx.strokeStyle = COLOR.frame;
  ctx.strokeRect(ox + 0.5, oy + 0.5, w - 1, h - 1);

  // The spread the capture route asks for (0.2 to 0.8 of the frame).
  ctx.setLineDash([6, 5]);
  ctx.strokeStyle = COLOR.signalSoft;
  ctx.lineWidth = 1.5;
  ctx.strokeRect(px(0.2), py(0.2), 0.6 * w, 0.6 * h);
  ctx.setLineDash([]);

  function footprint(point) {
    const size = Math.max(point.size || 0, 0.03);
    return [px(point.x) - (size * w) / 2, py(point.y) - (size * h) / 2, size * w, size * h];
  }

  for (const point of points || []) {
    const box = footprint(point);
    ctx.fillStyle = COLOR.inkFill;
    ctx.fillRect(...box);
    ctx.strokeStyle = COLOR.ink;
    ctx.lineWidth = 1;
    ctx.strokeRect(...box);
  }
  for (const point of rejected || []) {
    const box = footprint(point);
    if (point.kind === "surplus") {
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = COLOR.muted;
      ctx.lineWidth = 1;
      ctx.strokeRect(...box);
      ctx.setLineDash([]);
    } else {
      ctx.fillStyle = "rgba(224, 106, 90, 0.35)";
      ctx.fillRect(...box);
      ctx.strokeStyle = COLOR.fail;
      ctx.lineWidth = 2;
      ctx.strokeRect(...box);
    }
  }
}

function drawGuideOverlay(status) {
  const {ctx, cssW, cssH} = fitCanvas(guideOverlay);
  const checkpoints = status.guide?.checkpoints || [];
  if (!checkpoints.length) return;

  const naturalW = preview.naturalWidth || status.image_size?.[0] || cssW;
  const naturalH = preview.naturalHeight || status.image_size?.[1] || cssH;
  const scale = Math.min(cssW / naturalW, cssH / naturalH);
  const imageW = naturalW * scale;
  const imageH = naturalH * scale;
  const offsetX = (cssW - imageW) / 2;
  const offsetY = (cssH - imageH) / 2;
  const px = (x) => offsetX + x * imageW;
  const py = (y) => offsetY + y * imageH;

  function dot(point, color, radius) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px(point.x), py(point.y), radius, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.save();
  ctx.strokeStyle = COLOR.signal;
  ctx.globalAlpha = 0.9;
  ctx.lineWidth = 3;
  ctx.setLineDash([14, 10]);
  ctx.strokeRect(px(0.2), py(0.2), imageW * 0.6, imageH * 0.6);
  ctx.setLineDash([]);
  ctx.globalAlpha = 0.35;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(px(0.2), py(0.5));
  ctx.lineTo(px(0.8), py(0.5));
  ctx.moveTo(px(0.5), py(0.2));
  ctx.lineTo(px(0.5), py(0.8));
  ctx.stroke();
  ctx.restore();

  for (const point of checkpoints) {
    const radius = point.current ? 12 : 7;
    const color = point.complete ? COLOR.pass : point.current ? COLOR.signal : COLOR.target;
    dot(point, color, radius);
  }

  const current = status.guide?.current;
  if (current && !status.guide?.complete) {
    const boxW = Math.max(80, current.size * imageW);
    const boxH = Math.max(50, current.size * imageH);
    ctx.strokeStyle = current.live_match ? COLOR.pass : COLOR.signal;
    ctx.lineWidth = 4;
    ctx.strokeRect(px(current.x) - boxW / 2, py(current.y) - boxH / 2, boxW, boxH);
  }
  const pose = status.pose;
  if (pose) dot(pose, COLOR.fail, 9);
}

function guideText(status) {
  const guide = status.guide;
  const state = status.state || "idle";
  if (state === "idle" || state === "error") return "Open preview to start.";
  if (state === "preview") return "Start a new run, then move the board centre along the guide.";
  if (state === "solving") return "Solving…";
  if (state === "solved") {
    return "Solved. Check the result below; Resume to add views where the coverage map has gaps, then Solve again.";
  }
  if (guide?.complete || state === "complete") {
    return `Route complete (${guide?.complete_count}/${guide?.total_count}). Check the result below; `
      + "Resume to add or repeat poses, then Solve again.";
  }
  const current = guide?.current;
  if (!current) return "Move the board centre along the guide.";
  const base = `Target ${guide.complete_count + 1}/${guide.total_count}: ${current.label}`;
  if (state === "paused") return `Paused at ${base.toLowerCase()}. Resume to keep capturing.`;
  if (current.live_match) return `${base}. Hold still.`;
  if ((current.skew || 0) > 0) {
    return `${base}. Move to the point, then tilt the board until the box turns green.`;
  }
  return `${base}. Put the board centre on the highlighted point and match the box size.`;
}

// ---- Results ----

function pickRecommended(results) {
  const solved = results.filter(
    (result) => result && result.ok !== false && (result.median_view_error_px != null || result.rms != null),
  );
  if (!solved.length) return null;
  // The solver marks the recommendation (double_sphere when it solved: the cv2
  // medians are biased by dropping edge views). Older summaries lack the flag:
  // lower median reprojection error wins, falling back to RMS.
  const marked = solved.find((result) => result.recommended === true);
  if (marked) return marked;
  return solved.slice().sort((a, b) => {
    const am = a.median_view_error_px ?? a.rms ?? 1e9;
    const bm = b.median_view_error_px ?? b.rms ?? 1e9;
    return am - bm;
  })[0];
}

function coverageReasons(cov) {
  const reasons = [];
  if (!cov) return reasons;
  if ((cov.overall ?? 0) < 0.8) {
    reasons.push(`Coverage is ${Math.round((cov.overall || 0) * 100)}%; aim for 80% or more.`);
  }
  if (cov.x && !(cov.x.low_hit && cov.x.high_hit)) reasons.push("Push the board to the left and right edges.");
  if (cov.y && !(cov.y.low_hit && cov.y.high_hit)) reasons.push("Push the board to the top and bottom edges.");
  if (cov.size && !(cov.size.low_hit && cov.size.high_hit)) {
    reasons.push("Add both near (large) and far (small) board views.");
  }
  if (cov.skew && !cov.skew.hit) reasons.push("Add more tilted views.");
  return reasons;
}

function modeSummary(mode) {
  if (!mode) return "";
  const parts = [];
  if (mode.lens_fov) parts.push(`lens ${mode.lens_fov}`);
  if (mode.max_lens_mod) parts.push(`mod ${mode.max_lens_mod}`);
  if (mode.webcam_resolution) parts.push(`res ${mode.webcam_resolution}`);
  if (mode.frame_size) parts.push(mode.frame_size);
  if (mode.fps) parts.push(`${mode.fps} fps`);
  return parts.join(", ");
}

function figure(key, value, primary = false) {
  const el = document.createElement("div");
  el.className = `figure${primary ? " figure-primary" : ""}`;
  const k = document.createElement("span");
  k.className = "label";
  k.textContent = key;
  const v = document.createElement("span");
  v.className = "figure-value";
  v.textContent = value;
  el.append(k, v);
  return el;
}

function gridRow(key, value, cls = "") {
  const dt = document.createElement("dt");
  dt.textContent = key;
  const dd = document.createElement("dd");
  if (cls) dd.className = cls;
  dd.textContent = value;
  resultGrid.append(dt, dd);
}

function renderResults(modelResults, coverage, mode) {
  if (!modelResults || !modelResults.length) {
    resultsPanel.hidden = true;
    return;
  }
  resultsPanel.hidden = false;
  resultGrid.replaceChildren();
  recFigures.replaceChildren();
  verdictReasons.replaceChildren();
  resultsRaw.textContent = JSON.stringify(modelResults, null, 2);
  const failed = modelResults.filter((r) => r.ok === false);
  const rec = pickRecommended(modelResults);

  if (!rec) {
    verdictBox.className = "verdict retake";
    verdictBadge.textContent = "FAILED";
    verdictText.textContent = "No calibration model solved.";
    for (const result of failed) {
      gridRow(`${result.model || "model"} failed`, result.error || result.error_type || "solver failed", "failed");
    }
    return;
  }

  const isDS = rec.model === "double_sphere";
  const cm = rec.camera_matrix || [[null, null, null], [null, null, null]];
  const selected = rec.selected || rec;
  const allFrames = rec.all_frames || {};
  const used = selected.frame_count ?? allFrames.frame_count;
  const total = allFrames.frame_count ?? used;

  const reasons = coverageReasons(coverage);
  if (rec.worst_view_error_px != null && rec.worst_view_error_px > 2.5) {
    reasons.push(`The worst view is off by ${num(rec.worst_view_error_px)} px.`);
  }
  const alpha = isDS ? rec.distortion?.[1] : null;
  if (alpha != null && alpha >= 0.98) {
    reasons.push(`alpha ${num(alpha, 3)} is at its limit: the board missed the edge of the lens circle. Add edge views.`);
  }
  const pass = reasons.length === 0;
  verdictBox.className = `verdict ${pass ? "pass" : "retake"}`;
  verdictBadge.textContent = pass ? "PASS" : "RETAKE";
  verdictText.textContent = pass
    ? `${rec.model}: coverage and error look good.`
    : `${rec.model} solved. Another pass would improve it:`;
  for (const reason of reasons) {
    const li = document.createElement("li");
    li.textContent = reason;
    verdictReasons.append(li);
  }

  recFigures.append(
    figure(isDS ? "Error (OpenICC)" : "Median error", `${num(isDS ? rec.rms : rec.median_view_error_px)} px`, true),
  );
  if (!isDS) recFigures.append(figure("Worst view", `${num(rec.worst_view_error_px)} px`));
  if (isDS) {
    recFigures.append(figure("xi", num(rec.distortion?.[0], 4)), figure("alpha", num(alpha, 3)));
  }
  recFigures.append(
    figure("Focal", `${num(cm[0]?.[0], 1)}${isDS ? "" : ` / ${num(cm[1]?.[1], 1)}`}`),
    figure("Centre", `${num(cm[0]?.[2], 1)}, ${num(cm[1]?.[2], 1)}`),
    figure("Frames used", `${used ?? "n/a"} / ${total ?? "n/a"}`),
  );

  const modeStr = modeSummary(mode);
  if (modeStr) gridRow("Captured in", `${modeStr}. Record your data in exactly this mode.`);
  for (const other of modelResults.filter((r) => r !== rec && r.ok !== false)) {
    const error = other.model === "double_sphere"
      ? `${num(other.rms)} px (OpenICC)`
      : `median ${num(other.median_view_error_px)} px, worst ${num(other.worst_view_error_px)} px`;
    gridRow(`Also solved: ${other.model}`, error);
  }
  for (const result of failed) {
    gridRow(`${result.model || "model"} failed`, result.error || result.error_type || "solver failed", "failed");
  }
  if (rec.yaml) gridRow("Camera info YAML", rec.yaml, "mono");
  if (rec.json) gridRow("Intrinsics JSON", rec.json, "mono");
  if (isDS) gridRow("Note", "Compare Double Sphere models by projecting rays, not by focal length: f, xi and alpha trade off.");
}

// ---- Status ----

function renderDiagnostics(status, fwCmd) {
  const gopro = status.gopro;
  const bridge = status.video_bridge;
  let text = "";
  if (gopro && gopro.enabled && gopro.ok === false) text = JSON.stringify(gopro, null, 2);
  else if (bridge && bridge.enabled && bridge.ok === false && !fwCmd) text = JSON.stringify(bridge, null, 2);
  results.textContent = text;
  results.hidden = !text;
}

function updateStatus(status) {
  lastStatus = status;
  const state = status.state || "idle";
  statusLine.textContent = `${STATE_LABEL[state] || state}: ${status.message || ""}`;
  const captures = status.captures || 0;
  const target = status.target_samples || 0;
  const minFrames = Math.max(3, Number(form.elements["solver.min_frames"].value || 25));
  document.getElementById("captureCount").textContent = `${captures}/${target}`;
  document.getElementById("markerCount").textContent = status.markers || 0;
  document.getElementById("coverageOverall").textContent = percent(status.coverage?.overall);
  document.getElementById("guideCount").textContent =
    `${status.guide?.complete_count || 0}/${status.guide?.total_count || 0}`;
  guidePrompt.textContent = guideText(status);
  updateBars(status.coverage || {});
  drawCoverageMap(status.coverage?.points || [], status.rejected_points || [], status.image_size);
  drawGuideOverlay(status);
  renderReadout(status.gopro, defaults?.config);
  renderSteps(state, captures, minFrames);

  const capturing = state === "capturing";
  const paused = state === "paused";
  const solving = state === "solving";
  const live = !["idle", "error"].includes(state);
  currentState = state;
  const done = state === "complete" || state === "solved";
  previewBtn.disabled = live;                 // open only when nothing is live
  stopBtn.disabled = !live;                    // stop only when something is live
  startRunBtn.disabled = capturing || solving; // start/restart a run otherwise
  nextCameraBtn.disabled = !live || solving;   // switch only from a live session
  // Resume re-enables capturing from paused or after the route completed/solved,
  // so the operator can add or repeat poses on the same run.
  pauseResumeBtn.disabled = !(capturing || paused || done);
  pauseResumeBtn.textContent = paused || done ? "Resume" : "Pause";
  pauseResumeBtn.setAttribute("aria-pressed", String(paused));
  captureBtn.disabled = !capturing;
  solveBtn.disabled = captures < minFrames;

  // When the route first completes, run a checkpoint solve automatically so the
  // operator gets an early quality readout (and can then Resume to fix weak areas).
  if (state === "complete" && !autoSolved) {
    autoSolved = true;
    runSolve();
  }

  syncStream(state);
  previewEmpty.hidden = live;
  if (state === "error") {
    previewEmpty.firstElementChild.textContent = status.message || "The camera could not be opened.";
  }

  const bridge = status.video_bridge;
  const fwCmd = bridge ? bridge.ufw_command || extractUfwFromError(bridge.error) : "";
  renderFirewall(bridge, fwCmd);

  if (status.results && status.results.length) {
    renderResults(status.results, status.coverage, status.acquisition_mode);
    results.hidden = true;
  } else {
    resultsPanel.hidden = true;
    renderDiagnostics(status, fwCmd);
  }
}

async function poll() {
  try {
    updateStatus(await api("/api/session/status"));
  } catch {
    statusLine.textContent = "The server is not reachable. Is it still running?";
    setStreamStatus("error", "server offline");
  }
}

function startPolling() {
  clearInterval(pollTimer);
  // Poll fast so the guide overlay (target box, dots, pose dot, green match)
  // tracks the board closely. The video itself is a separate MJPEG stream; this
  // only fetches the lightweight status JSON.
  pollTimer = setInterval(poll, 150);
}

// ---- Presets ----

async function loadPresetList() {
  try {
    const data = await api("/api/presets");
    presetSelect.innerHTML = '<option value="">none</option>';
    for (const preset of data.presets || []) {
      const label = preset.title ? `${preset.title}` : preset.name;
      presetSelect.append(new Option(label, preset.name));
    }
  } catch {
    // Presets endpoint unavailable; leave the bar inert.
  }
}

async function applyPreset(name) {
  if (!name) return;
  try {
    const preset = await api(`/api/presets/${encodeURIComponent(name)}`);
    const cfg = preset.config || preset;
    populateForm(cfg, defaults.aruco_dictionaries, defaults.gopro_options);
    presetMsg.textContent = `Loaded "${preset.title || name}".`;
  } catch (err) {
    presetMsg.textContent = `Could not load the preset: ${err}`;
  }
}

async function savePreset() {
  const name = prompt("Save the current settings as a preset named:");
  if (!name) return;
  try {
    await api(`/api/presets/${encodeURIComponent(name)}`, {
      method: "POST",
      body: JSON.stringify({config: readForm()}),
    });
    presetMsg.textContent = `Saved "${name}".`;
    await loadPresetList();
    presetSelect.value = name;
  } catch (err) {
    presetMsg.textContent = `Could not save the preset: ${err}`;
  }
}

// ---- Actions ----

previewBtn.addEventListener("click", async () => {
  firewallDismissed = false;
  autoSolved = false;
  startStream();
  updateStatus(await api("/api/session/preview", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
});

startRunBtn.addEventListener("click", async () => {
  firewallDismissed = false;
  autoSolved = false;
  results.hidden = true;
  resultsPanel.hidden = true;
  startStream();
  updateStatus(await api("/api/session/run", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
});

pauseResumeBtn.addEventListener("click", async () => {
  const action = pauseResumeBtn.textContent === "Resume" ? "resume" : "pause";
  updateStatus(await api(`/api/session/${action}`, {method: "POST"}));
});

stopBtn.addEventListener("click", async () => {
  // Tear the video down first so a final stream "load" cannot flip the dot back
  // to green while the request is in flight.
  stopStream();
  clearInterval(pollTimer);
  updateStatus(await api("/api/session/stop", {method: "POST"}));
});

captureBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/capture", {method: "POST"}));
});

async function runSolve() {
  statusLine.textContent = "Solving…";
  try {
    // Pause capture first so solving from an active run is a clean, explicit stop.
    if (currentState === "capturing") {
      await api("/api/session/pause", {method: "POST"});
    }
    await api("/api/session/solve", {method: "POST"});
  } catch (err) {
    statusLine.textContent = `Solve failed: ${err}`;
    return;
  }
  await poll(); // status now carries results -> updateStatus renders the panel
}

solveBtn.addEventListener("click", runSolve);

nextCameraBtn.addEventListener("click", async () => {
  // Cleanly stop the current camera and go idle; the operator swaps the camera,
  // edits the camera name if needed, then clicks Open preview for the new one.
  firewallDismissed = false;
  autoSolved = false;
  results.hidden = true;
  resultsPanel.hidden = true;
  // Tear the video down first (before awaiting) so the dot does not flip back to
  // green on the stream's final load while the camera is being stopped.
  stopStream();
  clearInterval(pollTimer);
  updateStatus(await api("/api/session/next-camera", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
});

presetLoad.addEventListener("click", () => applyPreset(presetSelect.value));
presetSave.addEventListener("click", savePreset);

form.elements["gopro.enabled"].addEventListener("change", syncGoproVisibility);
form.addEventListener("input", (event) => {
  if (event.target.name?.startsWith("board.")) updateBoardSummary();
});

firewallCopy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(lastFirewallCmd);
    firewallCopyResult.textContent = "Copied";
  } catch {
    const range = document.createRange();
    range.selectNodeContents(firewallCmd);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    firewallCopyResult.textContent = "Press Ctrl+C to copy";
  }
});

firewallDismiss.addEventListener("click", () => {
  firewallDismissed = true;
  firewallPanel.hidden = true;
});

// Canvases are sized to their CSS box; redraw them when the layout changes.
window.addEventListener("resize", () => {
  if (lastStatus) {
    drawCoverageMap(lastStatus.coverage?.points || [], lastStatus.rejected_points || [], lastStatus.image_size);
    drawGuideOverlay(lastStatus);
  }
});

async function init() {
  defaults = await api("/api/defaults");
  populateForm(defaults.config, defaults.aruco_dictionaries, defaults.gopro_options);
  await loadPresetList();
  statusLine.textContent = "Ready.";
  drawCoverageMap([], [], null);
  await poll();
}

init().catch((err) => {
  statusLine.textContent = String(err);
});
