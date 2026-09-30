const form = document.getElementById("configForm");
const previewBtn = document.getElementById("previewBtn");
const startRunBtn = document.getElementById("startRunBtn");
const stopBtn = document.getElementById("stopBtn");
const pauseResumeBtn = document.getElementById("pauseResumeBtn");
const nextCameraBtn = document.getElementById("nextCameraBtn");
const captureBtn = document.getElementById("captureBtn");
const solveBtn = document.getElementById("solveBtn");
const statusMain = document.getElementById("statusMain");
const statusDetail = document.getElementById("statusDetail");
const preview = document.getElementById("preview");
const previewEmpty = document.getElementById("previewEmpty");
const results = document.getElementById("results");
const resultsPanel = document.getElementById("resultsPanel");
const verdictBox = document.getElementById("verdict");
const verdictBadge = document.getElementById("verdictBadge");
const verdictText = document.getElementById("verdictText");
const verdictDetail = document.getElementById("verdictDetail");
const verdictReasons = document.getElementById("verdictReasons");
const recFigures = document.getElementById("recFigures");
const resultGrid = document.getElementById("resultGrid");
const filesGrid = document.getElementById("filesGrid");
const filesTitle = document.getElementById("filesTitle");
const resultsRaw = document.getElementById("resultsRaw");
const scatter = document.getElementById("scatter");
const guideOverlay = document.getElementById("guideOverlay");
const guidePrompt = document.getElementById("guidePrompt");
const guideLegend = document.getElementById("guideLegend");
const legendTodo = document.getElementById("legendTodo");
const solveDesc = document.getElementById("solveDesc");
const streamStatus = document.getElementById("streamStatus");
const streamStatusText = document.getElementById("streamStatusText");
const cameraReadout = document.getElementById("cameraReadout");
const goproControls = document.getElementById("goproControls");
const deviceDetails = document.getElementById("deviceDetails");
const boardSummary = document.getElementById("boardSummary");
const steps = {
  setup: document.getElementById("stepSetup"),
  connect: document.getElementById("stepConnect"),
  capture: document.getElementById("stepCapture"),
  solve: document.getElementById("stepSolve"),
  // The From a recording route's steps 2-4 (step 1 is shared).
  settings: document.getElementById("stepSettings"),
  record: document.getElementById("stepRecord"),
  drop: document.getElementById("stepDrop"),
};

const presetSelect = document.getElementById("presetSelect");
const presetCustom = document.getElementById("presetCustom");
const presetSave = document.getElementById("presetSave");
const presetReset = document.getElementById("presetReset");
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

// The status line: one plain sentence per session state. The server's own
// message follows it as a quieter detail.
function stateSentence(state, captures) {
  const views = `${captures} view${captures === 1 ? "" : "s"} saved`;
  switch (state) {
    case "idle": return "Not connected.";
    case "preview": return "Connected. Start a new run when the board is ready.";
    case "capturing": return `Capturing: ${views}.`;
    case "paused": return `Paused: ${views}.`;
    case "complete": return `Capture finished: ${views}.`;
    case "solving": return "Solving… this can take a minute.";
    case "solved": return "Calibration done.";
    default: return `${state}.`;
  }
}

// Server messages that only repeat the sentence in front of them.
const QUIET_MESSAGES = new Set(["no active preview", "preview closed", "calibration solve complete"]);

function setStatusLine(main, detail = "") {
  if (statusMain.textContent === main && statusDetail.textContent === detail) return;
  statusMain.textContent = main;
  statusDetail.textContent = detail;
}

let defaults = null;
// The config the form edits: the server defaults, or the last loaded preset. Keys
// with no form field (for example gopro.webcam_digital_lens) pass through from it.
let baseConfig = null;
let pollTimer = null;
let streamActive = false;
let fallbackTimer = null;
let firewallDismissed = false;
let lastFirewallCmd = "";
let autoSolved = false;
let currentState = "idle";
let currentLive = false;
let lastStatus = null;
// Shipped and saved camera setups, name -> config, so the dropdown can show which
// one the form holds. presetChoice is the name shown ("" = custom settings).
let presetConfigs = {};
// Camera names the recording job replaces with one from the serial: the model's default
// (CameraConfig.camera_name) and the shipped setups' names, as recording.py counts them.
let shippedCameraNames = new Set(["gopro_camera"]);
let presetChoice = "";
// Whether the operator has made a settings choice on purpose (picked a setup,
// edited a field, saved or reset). Custom settings chosen that way finish step 1.
let settingsChosen = false;
// The empty-preview copy, restored once an error message has replaced it.
const previewEmptyHTML = previewEmpty.innerHTML;

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
  fisheye_ds: ["double_sphere", "kannala_brandt", "fisheye"],
  double_sphere: ["double_sphere"],
  both: ["plumb_bob", "rational_polynomial", "fisheye"],
};

// The smallest set that holds every requested model, so an older preset
// (for example [fisheye, double_sphere]) lands on the set that grew from it.
function modelsToKey(models) {
  const want = [...(models || [])];
  if (!want.length) return "pinhole";
  let best = null;
  for (const [key, set] of Object.entries(MODEL_SETS)) {
    if (!want.every((model) => set.includes(model))) continue;
    if (best === null || set.length < MODEL_SETS[best].length) best = key;
  }
  return best || "pinhole";
}

// What each model is called on the page.
const MODEL_NAME = {
  double_sphere: "Double Sphere",
  kannala_brandt: "Kannala–Brandt for UMI",
  fisheye: "OpenCV fisheye",
  plumb_bob: "Pinhole (plumb_bob)",
  rational_polynomial: "Pinhole (rational)",
};

function modelName(model) {
  return MODEL_NAME[model] || model || "model";
}

// Solved by the OpenICC backend: one aggregate error, no per-view errors.
const OPENICC_MODELS = new Set(["double_sphere", "kannala_brandt"]);

// What a form field shows for a config value, and the config value it reads
// back. setFormValue, readForm and the camera-setup match all go through these
// two, so a setup matches exactly when the form would show it.
function formString(input, value) {
  if (input.name === "solver.models") return modelsToKey(value);
  if (input.type === "checkbox") return Boolean(value);
  if (input.dataset.mm === "true") return Number(value * 1000).toFixed(1);
  if (input.dataset.optional === "true" && value === null) return "";
  const text = String(value ?? "");
  // A select cannot show a value it has no option for; it shows nothing instead.
  if (input.tagName === "SELECT" && ![...input.options].some((o) => o.value === text)) return "";
  return text;
}

function parseFormValue(input, raw) {
  if (input.name === "solver.models") return MODEL_SETS[raw] || MODEL_SETS.pinhole;
  if (input.dataset.mm === "true") return Number(raw) / 1000;
  if (input.dataset.optional === "true" && raw === "") return null;
  if (input.type === "number" || input.dataset.goproOptions) return raw === "" ? null : Number(raw);
  return raw;
}

function setFormValue(name, value) {
  const input = form.elements[name];
  if (!input) return;
  const shown = formString(input, value);
  if (input.type === "checkbox") input.checked = shown;
  else input.value = shown;
}

// What each model choice writes, shown under the Model dropdown with the full
// name of the choice (the narrow settings column cuts the dropdown text short).
const MODEL_WRITES = {
  fisheye_ds: "writes the Double Sphere fit, the UMI file with its ORB-SLAM3 camera block, "
    + "and a ROS YAML from OpenCV fisheye.",
  double_sphere: "writes the Double Sphere JSON only.",
  fisheye: "writes a ROS YAML.",
  pinhole: "writes a ROS YAML for each of the two models.",
  both: "writes a ROS YAML for each model, to compare them.",
};
const modelNote = document.getElementById("modelNote");

// A narrow dropdown cuts its label short; the tooltip carries the whole choice.
function showFullChoice(select) {
  const text = select.selectedOptions[0]?.text || "";
  if (select.title !== text) select.title = text;
  if (select.name === "solver.models" && modelNote) {
    const writes = MODEL_WRITES[select.value];
    modelNote.textContent = writes ? `${text}: ${writes}` : text;
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
  showFullChoice(form.elements["solver.models"]);
}

function readForm() {
  const config = structuredClone(baseConfig || defaults.config);
  for (const input of form.elements) {
    if (!input.name) continue;
    setDeep(config, input.name, parseFormValue(input, input.type === "checkbox" ? input.checked : input.value));
  }
  return config;
}

function syncGoproVisibility() {
  const enabled = form.elements["gopro.enabled"];
  if (!enabled) return;
  if (goproControls) goproControls.hidden = !enabled.checked;
  for (const id of ["connectionDetails", "recordingDetails"]) {
    const el = document.getElementById(id);
    if (el) el.hidden = !enabled.checked; // GoPro-only settings
  }
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
    let message = text || response.statusText;
    try {
      const detail = JSON.parse(text).detail; // FastAPI's HTTPException body
      if (detail) message = typeof detail === "string" ? detail : JSON.stringify(detail);
    } catch {
      // not JSON: keep the raw text
    }
    throw new Error(message);
  }
  return response.json();
}

function percent(value) {
  return `${Math.round((value || 0) * 100)} %`;
}

function num(value, digits = 2) {
  return value == null || Number.isNaN(value) ? "n/a" : Number(value).toFixed(digits);
}

// ---- Stream ----

// Whether the camera is streaming. The state names describe the run, and a run
// can be solved after Stop, so the server reports this separately.
function isLive(status) {
  const state = status.state || "idle";
  if (state === "error") return false;
  return status.preview_open ?? state !== "idle";
}

// The first seconds after Open preview, before the webcam sends a picture.
function isConnecting(status) {
  const state = status.state || "idle";
  return isLive(status) && (state === "idle" || /^waiting for/.test(status.message || ""));
}

function setStreamStatus(state, text) {
  // Both this and the readout are live regions: touch them only on a change,
  // or a screen reader re-announces them on every poll.
  if (streamStatus.dataset.state === state && streamStatusText.textContent === text) return;
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

function syncStream(live) {
  if (live) startStream();
  else stopStream();
}

// Until a frame arrives, hide the image: a failed load would show the browser's
// broken-image icon and alt text over the black frame.
preview.addEventListener("load", () => {
  delete preview.dataset.noFrame;
  if (!streamActive) return;
  if (fallbackTimer) setStreamStatus("error", "stream blocked, snapshots");
  else setStreamStatus("streaming", "streaming");
});

preview.addEventListener("error", () => {
  preview.dataset.noFrame = "";
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
  // role="alert": rewriting it on every poll would make screen readers repeat it.
  const signature = JSON.stringify(bridge) + fwCmd;
  if (!firewallPanel.hidden && firewallPanel.dataset.signature === signature) return;
  firewallPanel.dataset.signature = signature;
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
// assumed), plus any mismatch with what was requested. Only a live session has
// a report worth showing; otherwise say when one will appear.
function renderReadout(gopro, live) {
  const chips = [];
  const autoSetup = form.elements["gopro.enabled"]?.checked
    && form.elements["gopro.apply_on_preview"]?.checked;
  const labels = gopro?.camera_state?.labels;
  if (gopro?.enabled && gopro.ok === false) {
    chips.push(chip("GoPro setup failed: details at the bottom of the page", {kind: "warn"}));
  } else if (live && labels && Object.keys(labels).length) {
    for (const [field, label] of Object.entries(labels)) {
      chips.push(chip(label, {key: READOUT_KEYS[field] || field}));
    }
  } else if (live && gopro?.enabled) {
    chips.push(chip("camera did not report its settings", {kind: "muted"}));
  } else if (!live && autoSetup) {
    chips.push(chip("camera settings appear after Open preview", {kind: "muted"}));
  }
  if (live) {
    for (const warning of gopro?.warnings || []) {
      chips.push(chip(warning, {key: "Check", kind: "warn"}));
    }
  }
  const signature = chips.map((el) => el.textContent).join("|");
  if (cameraReadout.dataset.signature === signature) return;
  cameraReadout.dataset.signature = signature;
  cameraReadout.replaceChildren(...chips);
}

// ---- Steps ----

// What the page knows about the run, gathered once per status so the step bar,
// the amber button, the prompt and the Solve line all say the same thing.
// `solved`: at least one model solved; `pass`: the verdict on the card is PASS.
function runFacts(status, minFrames) {
  const state = status.state || "idle";
  const captures = status.captures || 0;
  const live = isLive(status);
  // A fresh preview (after Stop, then Open preview) belongs to the next run: the
  // server still holds the last run's result, but it no longer applies.
  const stale = live && state === "preview";
  const results = stale ? [] : status.results || [];
  const verdict = judge(results, status.coverage);
  const atCap = status.max_samples != null && captures >= status.max_samples;
  const done = state === "complete" || state === "solved";
  const solved = verdict.rec !== null;
  return {
    state, live, captures, minFrames, atCap,
    enough: captures >= minFrames,
    hasResults: results.length > 0,
    solved,
    pass: verdict.pass,
    stale,
    // The only thing wrong is that the two fisheye solves disagree: OpenICC does not
    // always settle the same way, so the solver's own advice is to solve again first
    // and add edge views only if the gap stays.
    solveFirst: solved && !verdict.pass && verdict.reasons.length > 0
      && verdict.reasons.every((reason) => reason.startsWith(DISAGREE)),
    // Views were added (Resume) since the result shown: it does not include them.
    moreViews: live && results.length > 0 && ["capturing", "paused", "complete"].includes(state),
    // Something solved, but an OpenICC model the run asked for did not: its files
    // are missing (for the gripper preset, possibly the UMI file), and the usual
    // cause (setup, or a solve that did not settle) is fixed by solving again. A
    // failed OpenCV model next to them is not: OpenCV's fisheye cannot fit every
    // lens, so that row is only a note.
    missing: solved ? verdict.failed.filter((r) => OPENICC_MODELS.has(r.model)) : [],
    incomplete: solved && verdict.failed.some((r) => OPENICC_MODELS.has(r.model)),
    missingText: solved
      ? missingSentence(verdict.failed.filter((r) => OPENICC_MODELS.has(r.model)), cameraOf(status))
      : "",
    // Resume carries on the same run: from paused, or after the route or a solve.
    canResume: live && (state === "paused" || done) && !atCap,
    connecting: isConnecting(status),
    setupChosen: Boolean(presetChoice) || settingsChosen,
  };
}

// The camera name a run's files start with: the run id minus its timestamp.
function cameraOf(status) {
  const fromRun = (status.run_id || "").replace(/_\d{8}_\d{6}$/, "");
  return fromRun || form.elements["camera.camera_name"]?.value || "camera";
}

function listText(items) {
  if (items.length < 2) return items.join("");
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

// What a failed model would have written, in the words the Files list uses.
function missingFile(model, camera) {
  if (model === "kannala_brandt") return `the UMI file (${camera}_kannala_brandt.json)`;
  if (model === "double_sphere") return "the Double Sphere JSON";
  return `the ${modelName(model)} ROS YAML`;
}

// "Kannala–Brandt for UMI did not solve, so the UMI file (…) was not written."
function missingSentence(failed, camera) {
  if (!failed.length) return "";
  const names = listText(failed.map((r) => modelName(r.model)));
  const files = failed.map((r) => missingFile(r.model, camera));
  return `${names} did not solve, so ${listText(files)} ${files.length === 1 ? "was" : "were"} not written.`;
}

function fixMissing(f) {
  const rows = f.missing.length === 1 ? "row" : "rows";
  const again = f.route === "recording" ? "drop the clip again to solve again" : "Solve again";
  return `${f.missingText} Fix the reason on the red ${rows}, then ${again}.`;
}

function stepStates(f) {
  const {state, live, captures, enough, solved, pass, incomplete} = f;
  const started = live || captures > 0 || f.hasResults;
  // Custom settings are allowed once chosen on purpose; untouched start-up
  // settings that match no setup point a newcomer at step 1 first.
  const setup = f.setupChosen || started ? "done" : "current";
  let connect = setup === "current" ? "pending" : "current";
  let capture = "pending";
  let solve = "pending";
  if (f.connecting) {
    // Still connecting: the webcam has not sent a picture yet.
    connect = "current";
  } else if (live) {
    connect = "done";
    capture = "current";
    if (state === "solving" || state === "complete" || (state === "paused" && enough)) {
      capture = "done";
      solve = "current";
    } else if (state === "solved") {
      // A failed or incomplete solve is solved again; a retake goes back to capturing.
      const again = !solved || incomplete || f.solveFirst;
      capture = !again && !pass ? "current" : "done";
      solve = again ? "current" : "done";
    }
  } else {
    if (state === "solving" || (enough && (!solved || incomplete || f.solveFirst))) {
      // Stopped (or the stream failed) with enough views: Solve is the next
      // step, not reconnecting. A stopped run cannot be resumed.
      connect = "pending";
      capture = "done";
      solve = "current";
    } else if (solved) {
      // Open preview again for the next camera (or for a retake).
      capture = "done";
      solve = "done";
    }
  }
  return {setup, connect, capture, solve};
}

// The one obvious next action (the amber button), or null while the operator
// is busy moving the board, waiting for the camera or a solve, or has still to
// pick the camera setup (then the step 1 dropdown carries the highlight).
function primaryButton(f, setupCurrent) {
  const {state, live, enough, solved, pass, canResume, incomplete} = f;
  if (setupCurrent) return null;
  if (state === "capturing" || state === "solving" || f.connecting) return null;
  if (!live) return enough && (!solved || incomplete || f.solveFirst) ? solveBtn : previewBtn;
  if (state === "preview") return startRunBtn;
  if (state === "paused") return enough ? solveBtn : pauseResumeBtn;
  if (state === "complete") return solveBtn;
  if (state === "solved") {
    // Fix the reason on the red rows, then solve again.
    if (!solved || incomplete || f.solveFirst) return solveBtn;
    if (pass) return nextCameraBtn;
    // Retake: add views to this run, or, at the frame cap, start a fresh one.
    return canResume ? pauseResumeBtn : startRunBtn;
  }
  return null;
}

// Enable or disable a button, with a title that says what it does or why it
// cannot be used right now.
function setButton(button, enabled, title, whyNot) {
  button.disabled = !enabled;
  const text = enabled ? title : whyNot;
  if (button.title !== text) button.title = text;
}

const STEP_NOTE = {current: ", current step", done: ", done", pending: ""};

function renderSteps(states) {
  for (const [name, value] of Object.entries(states)) {
    const step = steps[name];
    step.dataset.stepState = value;
    if (value === "current") step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
    step.querySelector("[data-step-note]").textContent = STEP_NOTE[value] || "";
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
const DEFAULT_TARGETS = {x_min: 0.2, x_max: 0.8, y_min: 0.2, y_max: 0.8};

// The box the board centre must reach, from the run's coverage targets.
function targetBox(coverage) {
  const t = {...DEFAULT_TARGETS, ...(coverage?.targets || {})};
  return {x: t.x_min, y: t.y_min, w: t.x_max - t.x_min, h: t.y_max - t.y_min};
}

function drawCoverageMap(points, imageSize, target = targetBox(null)) {
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

  // The spread the coverage targets ask for (dashed, as on the live preview).
  ctx.setLineDash([6, 5]);
  ctx.strokeStyle = COLOR.muted;
  ctx.lineWidth = 1.5;
  ctx.strokeRect(px(target.x), py(target.y), target.w * w, target.h * h);
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
}

function drawGuideOverlay(status) {
  const {ctx, cssW, cssH} = fitCanvas(guideOverlay);
  const checkpoints = status.guide?.checkpoints || [];
  // Only over a live picture: on the empty preview it would cover the how-to,
  // and while connecting there is no picture yet to hold the board in.
  const draw = isLive(status) && !isConnecting(status) && checkpoints.length > 0;
  // Once the capture is finished (route done, frame cap, a solve) there is no
  // "next" pose: the orange box, the orange dot and their legend lines go.
  const state = status.state || "idle";
  const current = status.guide?.current;
  const showNext = Boolean(current) && !status.guide?.complete
    && !["complete", "solving", "solved"].includes(state);
  guideLegend.hidden = !draw;
  for (const line of guideLegend.querySelectorAll("[data-legend-next]")) line.hidden = !showNext;
  if (!draw) return;

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

  // The reach line in white, not orange: orange means "the board goes here next".
  ctx.save();
  ctx.strokeStyle = COLOR.ink;
  ctx.globalAlpha = 0.75;
  ctx.lineWidth = 2;
  const target = targetBox(status.coverage);
  const midX = target.x + target.w / 2;
  const midY = target.y + target.h / 2;
  ctx.setLineDash([14, 10]);
  ctx.strokeRect(px(target.x), py(target.y), imageW * target.w, imageH * target.h);
  ctx.setLineDash([]);
  ctx.globalAlpha = 0.25;
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.moveTo(px(target.x), py(midY));
  ctx.lineTo(px(target.x + target.w), py(midY));
  ctx.moveTo(px(midX), py(target.y));
  ctx.lineTo(px(midX), py(target.y + target.h));
  ctx.stroke();
  ctx.restore();

  for (const point of checkpoints) {
    const next = point.current && showNext;
    const color = point.complete ? COLOR.pass : next ? COLOR.signal : COLOR.target;
    dot(point, color, next ? 12 : 7);
  }

  if (showNext) {
    const boxW = Math.max(80, current.size * imageW);
    const boxH = Math.max(50, current.size * imageH);
    ctx.strokeStyle = current.live_match ? COLOR.pass : COLOR.signal;
    ctx.lineWidth = 4;
    ctx.strokeRect(px(current.x) - boxW / 2, py(current.y) - boxH / 2, boxW, boxH);
  }
  const pose = status.pose;
  if (pose) dot(pose, COLOR.fail, 9);
}

const IDLE_PROMPT = "Open preview to start.";
const SOLVE_FIRST = "Retake: the two fisheye solves disagree. Click Solve again; if the gap "
  + "stays, add views near the edge of the circle.";
const OTHER_CAMERA = "To do another camera, plug it in, give it its own Camera name in Settings "
  + "(it names the files), then click Open preview.";
const NEXT_CAMERA_PROMPT = "Plug in the next GoPro, give it its own Camera name in Settings "
  + "(it names the files), then click Open preview.";

// What the result card asks for next, in the same words as the amber button.
function resultPrompt(f) {
  if (!f.solved) {
    return f.enough
      ? "The solve failed. Fix the first reason in the result below, then Solve again."
      : "The solve failed. Open preview and start a new run.";
  }
  if (f.moreViews) {
    return "Adding views to this run. Solve again to include the views added since the last solve.";
  }
  if (f.incomplete) return fixMissing(f);
  if (f.solveFirst) return SOLVE_FIRST;
  if (!f.live) {
    return f.pass
      ? `Solved. Check the result below. ${OTHER_CAMERA}`
      : "Retake: Open preview and start a new run with the views listed below.";
  }
  if (f.pass) {
    return "Solved. Check the result below, then click Next camera. "
      + "To improve it first, Resume and add views where the coverage map has gaps.";
  }
  return f.canResume
    ? "Retake: click Resume and add the views listed in the result below, then Solve again."
    : "Retake: this run is at its frame cap. Start a new run and add the views listed below.";
}

function guideText(status, f) {
  const guide = status.guide || {};
  const {state, live, minFrames} = f;
  const done = guide.complete_count || 0;
  const total = guide.total_count || 0;
  if (state === "solving") return "Solving…";
  if (state === "error") {
    return f.enough && (!f.solved || f.incomplete)
      ? `The camera stopped with ${f.captures} views saved. Solve them now, `
        + "or fix the problem on the status line and Open preview again."
      : "Fix the problem on the status line, then Open preview again.";
  }
  // A finished solve, live or after Stop: the card below is what matters now.
  if (state === "solved" || (!live && f.hasResults)) return resultPrompt(f);
  if (!live) {
    const captures = status.captures || 0;
    if (status.message === "stopped; ready for next camera") return NEXT_CAMERA_PROMPT;
    if (!captures || !status.run_id) return IDLE_PROMPT;
    return captures >= minFrames
      ? `Preview stopped. Solve this run's ${captures} views, or Open preview to start again.`
      : `Preview stopped with ${captures} views; solving needs ${minFrames}. Open preview and start a new run.`;
  }
  if (f.connecting) return "Connecting to the camera. This can take up to 25 seconds.";
  if (state === "preview") return "Hold the board in front of the camera, then click Start new run.";
  if (state === "complete") {
    if (guide.complete) {
      return `Route complete (${done}/${total}). Check the result below; `
        + "Resume to add or repeat poses, then Solve again.";
    }
    return `Frame cap reached (${status.captures} views) with the route at ${done}/${total}. `
      + "Solve with these views, or raise Frame cap under Capture and solver and start a new run.";
  }
  const paused = state === "paused";
  // Enough views to solve: Solve is the amber button, so say so first.
  if (paused && f.enough) {
    const where = guide.complete || !guide.current ? "" : ` (guide at ${done}/${total})`;
    return `Paused with ${f.captures} views${where}. Solve now, or Resume to keep capturing.`;
  }
  if (guide.complete) {
    // Resumed after the route was done: free capture of extra views.
    return paused
      ? "Paused. Resume to add more views, then Solve again."
      : "Adding extra views: fill the gaps in the coverage map, then Solve again.";
  }
  const current = guide.current;
  if (!current) return paused ? "Paused. Resume to keep capturing." : "Move the board centre along the guide.";
  const base = `target ${done + 1}/${total}, ${current.label}`;
  if (paused) return `Paused at ${base}. Resume to keep capturing.`;
  const Base = `Target ${done + 1}/${total}: ${current.label}`;
  if (current.live_match) return `${Base}. Hold still.`;
  if ((current.skew || 0) > 0) {
    return `${Base}. Put the board centre on the orange dot, then tilt the board until the box turns green.`;
  }
  return `${Base}. Put the board centre on the orange dot and match the size of the orange box.`;
}

// ---- Results ----

// A model counts as solved only when it reported an error figure; ok:false
// rows and rows without numbers are failures.
function solvedOk(result) {
  return Boolean(result) && result.ok !== false
    && (result.median_view_error_px != null || result.rms != null);
}

function pickRecommended(results) {
  const solved = results.filter(solvedOk);
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
    reasons.push(`Spread is ${percent(cov.overall)}; aim for 80 % or more.`);
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
  if (mode.route === "recording") {
    // The file carries the size and frame rate, not the lens or the mod.
    const set = [mode.lens_fov && `lens ${mode.lens_fov}`, mode.max_lens_mod].filter(Boolean);
    if (set.length) parts.push(`${set.join(" and ")} as set on the camera (not in the file)`);
    if (mode.frame_size) parts.push(mode.frame_size);
    if (mode.fps) parts.push(`${mode.fps} fps`);
    return parts.join(", ");
  }
  // What the camera reported beats what was requested.
  const lens = mode.reported_webcam_digital_lens || mode.lens_fov;
  const mod = mode.reported_max_lens_mod || mode.max_lens_mod;
  if (lens) parts.push(`lens ${lens}`);
  if (mod) parts.push(`mod ${mod}`);
  if (mode.webcam_resolution) parts.push(`res ${mode.webcam_resolution}`);
  if (mode.frame_size) parts.push(mode.frame_size);
  if (mode.fps) parts.push(`${mode.fps} fps`);
  if (mode.protocol) parts.push(`${mode.protocol} stream`);
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

// One row of a result list. `notes` are extra lines under the value (warnings,
// or what a file is for), each with its own class.
function gridRow(grid, key, value, {cls = "", notes = []} = {}) {
  const dt = document.createElement("dt");
  dt.textContent = key;
  const dd = document.createElement("dd");
  if (cls) dd.className = cls;
  const main = document.createElement("span");
  main.className = "row-value";
  main.textContent = value;
  dd.append(main);
  for (const note of notes) {
    const line = document.createElement("span");
    line.className = note.cls;
    line.textContent = note.text;
    dd.append(line);
  }
  grid.append(dt, dd);
}

// Plain-language warnings a model row carries: the solver's own, plus the UMI
// check on the Kannala–Brandt row.
// The start of the solver's Kannala-Brandt vs Double Sphere warning (solver.umi_check).
const DISAGREE = "The two fisheye solves disagree";

function resultWarnings(result) {
  const all = [...(result.warnings || []), ...(result.umi_check?.warnings || [])];
  return [...new Set(all)];
}

function warningNotes(result) {
  return resultWarnings(result).map((text) => ({cls: "row-warning", text}));
}

function umiSummary(result) {
  const check = result.umi_check || {};
  const diff = check.max_diff_vs_double_sphere_px;
  const reach = check.board_reach_deg;
  const fit = `Reprojection RMS ${num(result.rms)} px (OpenICC).`;
  if (diff != null && reach != null) {
    return `Kannala–Brandt for UMI: matches Double Sphere within ${num(diff)} px `
      + `out to ${Math.round(reach)}°. ${fit}`;
  }
  return `Kannala–Brandt for UMI: solved. ${fit} No Double Sphere solve to compare it with.`;
}

// Every file a solved model wrote, with what it is for.
function fileRows(result) {
  const rows = [];
  const name = modelName(result.model);
  if (result.json && result.model === "kannala_brandt") {
    rows.push(["UMI intrinsics JSON", result.json, "Use this for: UMI. Load it with parse_fisheye_intrinsics."]);
  } else if (result.json && result.model === "double_sphere") {
    rows.push(["Double Sphere JSON", result.json, "Use this for: tools that take the Double Sphere model (the reference fit for this lens)."]);
  } else if (result.json) {
    rows.push([`${name} JSON`, result.json, "Use this for: tools that read OpenICC's JSON."]);
  }
  if (result.orbslam3_yaml) {
    // The recording route writes this block at UMI's SLAM size and says so.
    const size = result.orbslam3_note ? ` ${result.orbslam3_note}` : "";
    rows.push([
      "ORB-SLAM3 camera YAML",
      result.orbslam3_yaml,
      "Use this for: the camera block of ORB-SLAM3 settings. Merge in the IMU block from your existing settings file."
        + size,
    ]);
  }
  if (result.yaml) {
    rows.push([`ROS camera_info YAML (${name})`, result.yaml, "Use this for: ROS, as the camera_info file."]);
  }
  return rows;
}

// The verdict on a set of results: the recommended model, what is wrong with
// it, and whether that adds up to PASS. The step bar and the amber button read
// it too, so the card and the controls never disagree.
function judge(results, coverage) {
  const rec = results.length ? pickRecommended(results) : null;
  // Models the run asked for that did not solve (their files were not written).
  const failed = results.filter((r) => !solvedOk(r));
  if (!rec) return {rec: null, reasons: [], pass: false, alpha: null, failed};
  const reasons = coverageReasons(coverage);
  if (rec.worst_view_error_px != null && rec.worst_view_error_px > 2.5) {
    reasons.push(`The worst view is off by ${num(rec.worst_view_error_px)} px.`);
  }
  const alpha = rec.model === "double_sphere" ? rec.distortion?.[1] : null;
  if (alpha != null && alpha >= 0.98) {
    reasons.push(`alpha ${num(alpha, 3)} is at its limit: the board missed the edge of the lens circle. Add edge views.`);
  }
  reasons.push(...resultWarnings(rec));
  return {rec, reasons, pass: reasons.length === 0, alpha, failed};
}

let resultsSignature = "";

// The result headline; a clip-check warning (recording route) is repeated in front of it.
function setHeadline(text, warning = "") {
  if (!warning) {
    verdictText.textContent = text;
    return;
  }
  const lead = Object.assign(document.createElement("span"), {
    className: "verdict-warning",
    textContent: warning,
  });
  verdictText.replaceChildren(lead, document.createTextNode(text));
}

// `f` is runFacts for the same status, so the headline names the same next
// action as the amber button.
function renderResults(modelResults, coverage, mode, f) {
  if (!modelResults || !modelResults.length) {
    resultsPanel.hidden = true;
    resultsSignature = "";
    return;
  }
  resultsPanel.hidden = false;
  // Rebuild only on a change: the status polls every 150 ms, and a rebuilt card
  // would drop a text selection (copying a file path) on every poll.
  const signature = JSON.stringify([
    modelResults, coverage, mode, f.live, f.canResume, f.missingText, f.moreViews, f.solveFirst,
    f.route, f.clipWarning,
  ]);
  if (signature === resultsSignature) return;
  resultsSignature = signature;
  resultGrid.replaceChildren();
  filesGrid.replaceChildren();
  recFigures.replaceChildren();
  verdictReasons.replaceChildren();
  resultsRaw.textContent = JSON.stringify(modelResults, null, 2);
  const failed = modelResults.filter((r) => !solvedOk(r));
  const {rec, reasons, pass, alpha} = judge(modelResults, coverage);
  const failedRows = () => {
    for (const result of failed) {
      // Beside a solved result, an OpenCV model that failed is a note, not a to-do:
      // OpenCV's fisheye cannot fit every lens, and solving again will not change that.
      const notes = rec && !OPENICC_MODELS.has(result.model)
        ? [`No ${modelName(result.model)} files from this run. The other files are complete; `
          + "solving again will not change this."]
        : [];
      gridRow(resultGrid, `${modelName(result.model)} failed`, result.error || result.error_type || "solver failed", {cls: "failed", notes});
    }
  };

  const recording = f.route === "recording";
  if (!rec) {
    verdictBox.className = "verdict retake";
    verdictBadge.textContent = "FAILED";
    setHeadline("No calibration model solved. The reasons are below.", f.clipWarning);
    verdictDetail.textContent = recording
      ? "Fix the first reason, then drop the clip again to solve again."
      : "Fix the first reason, then Solve again.";
    failedRows();
    filesTitle.hidden = true;
    return;
  }

  const isDS = rec.model === "double_sphere";
  const isOpenICC = OPENICC_MODELS.has(rec.model);
  const cm = rec.camera_matrix || [[null, null, null], [null, null, null]];
  const selected = rec.selected || rec;
  const allFrames = rec.all_frames || {};
  const used = selected.frame_count ?? allFrames.frame_count;
  const total = allFrames.frame_count ?? used;

  const complete = pass && !f.incomplete;
  verdictBox.className = `verdict ${complete ? "pass" : "retake"}`;
  verdictBadge.textContent = f.incomplete ? "INCOMPLETE" : pass ? "PASS" : "RETAKE";
  const next = f.moreViews
    ? "Solve again to include the views added since this result."
    : f.live || recording
      ? "Click Next camera to do another camera."
      : OTHER_CAMERA;
  let headline;
  if (f.incomplete) {
    const lead = pass
      ? `Calibration passed for ${modelName(rec.model)}, but`
      : `${modelName(rec.model)} solved but needs a retake, and`;
    headline = `${lead} ${fixMissing(f)}`;
  } else if (pass) {
    headline = f.settingsDiffer
      ? `Calibration passed, but it is only valid for footage recorded exactly like ${f.clipsWord}.`
      // A clip read short: the prompt above says what to do next, not Next camera.
      : `Calibration passed. The files are listed below.${f.warning ? "" : ` ${next}`}`;
  } else if (f.solveFirst) {
    headline = recording ? REC_SOLVE_FIRST : SOLVE_FIRST;
  } else if (recording && f.warning && f.canRedo) {
    // The clip is best recorded again in a fresh run: the prompt above says how.
    headline = "Retake: record the clip again in a fresh run, as the prompt above says "
      + "(Start this camera again).";
  } else if (recording) {
    // The missing positions are listed above the card; with none missing, the reasons
    // below are what the next clip must fix.
    headline = f.missingPositions.length
      ? "Retake: record another clip that covers the missing positions listed above, "
        + "then click Add another clip."
      : "Retake: record another clip that fixes the reasons listed below, then click Add "
        + "another clip.";
  } else if (f.canResume) {
    headline = "Retake: Resume and add the views listed below, then Solve again.";
  } else {
    // At the frame cap, or after Stop: a stopped run cannot be resumed.
    headline = f.live
      ? "Retake: start a new run and add the views listed below."
      : "Retake: Open preview, start a new run and add the views listed below.";
  }
  setHeadline(headline, f.clipWarning);
  // Say only what was checked: coverage, plus worst view (cv2) or alpha (Double Sphere).
  const checked = isDS
    ? " and alpha is clear of its limit"
    : isOpenICC ? "" : " and no kept view is off by more than 2.5 px";
  // A PASS covers the recommended model only; point at a warning on another row.
  const solved = modelResults.filter(solvedOk);
  const warned = solved.filter((r) => r !== rec && resultWarnings(r).length);
  let seeAlso = "";
  if (warned.some((r) => r.model === "kannala_brandt")) {
    seeAlso = " See the note on the For UMI row before using the UMI file.";
  } else if (warned.length) {
    seeAlso = " See the notes on the other models below before using their files.";
  }
  verdictDetail.textContent = pass
    ? `${f.incomplete ? "Best of the models that solved" : "Recommended model"}: `
      + `${modelName(rec.model)}. Coverage targets met${checked}.${seeAlso}`
    : `The recommended model, ${modelName(rec.model)}, solved and wrote its files, but:`;
  for (const reason of reasons) {
    const li = document.createElement("li");
    li.textContent = reason;
    verdictReasons.append(li);
  }

  recFigures.append(
    figure(isOpenICC ? "Reprojection RMS (OpenICC)" : "Median error", `${num(isOpenICC ? rec.rms : rec.median_view_error_px)} px`, true),
  );
  if (!isOpenICC) {
    recFigures.append(
      figure("Worst view", `${num(rec.worst_view_error_px)} px`),
      figure("RMS", `${num(rec.rms)} px`),
    );
  }
  if (isDS) {
    recFigures.append(figure("xi", num(rec.distortion?.[0], 4)), figure("alpha", num(alpha, 3)));
  }
  recFigures.append(
    figure("Focal", `${num(cm[0]?.[0], 1)}${isDS ? "" : ` / ${num(cm[1]?.[1], 1)}`}`),
    figure("Centre (cx, cy)", `${num(cm[0]?.[2], 1)}, ${num(cm[1]?.[2], 1)}`),
    figure("Frames used", `${used ?? "n/a"} / ${total ?? "n/a"}`),
  );

  const modeStr = modeSummary(mode);
  if (modeStr) {
    const advice = f.settingsDiffer
      ? "The clip check found settings that differ from the camera setup: see the red box."
      : "Record your data in exactly this mode.";
    gridRow(resultGrid, "Captured in", `${modeStr}. ${advice}`);
  }
  for (const result of solved) {
    // The recommended row's warnings are already in the verdict above.
    const notes = result === rec ? [] : warningNotes(result);
    if (result.model === "kannala_brandt") {
      gridRow(resultGrid, "For UMI", umiSummary(result), {notes});
    } else if (result !== rec) {
      const error = OPENICC_MODELS.has(result.model)
        ? `Reprojection RMS ${num(result.rms)} px (OpenICC)`
        : `median ${num(result.median_view_error_px)} px, worst ${num(result.worst_view_error_px)} px`;
      gridRow(resultGrid, `Also solved: ${modelName(result.model)}`, error, {notes});
    }
  }
  failedRows();
  if (isDS) gridRow(resultGrid, "Note", "Compare Double Sphere models by projecting rays, not by focal length: f, xi and alpha trade off.");

  // Files: the recommended model's first, then the others in solve order.
  const files = [rec, ...solved.filter((r) => r !== rec)].flatMap(fileRows);
  for (const [label, path, use] of files) {
    gridRow(filesGrid, label, path, {cls: "file", notes: [{cls: "row-use", text: use}]});
  }
  filesTitle.hidden = files.length === 0;
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

// A message with no closing punctuation gets a full stop before the next sentence.
function sentence(text) {
  const trimmed = String(text).trim();
  return /[.!?…]$/.test(trimmed) ? trimmed : `${trimmed}.`;
}

function updateStatus(status) {
  lastStatus = status;
  // The live session keeps its status while the From a recording route is shown.
  if (calibrationRoute !== "live") return;
  const state = status.state || "idle";
  const captures = status.captures || 0;
  const message = status.message || "";
  const target = status.target_samples || 0;
  const minFrames = Math.max(3, Number(form.elements["solver.min_frames"].value || 25));
  const facts = runFacts(status, minFrames);
  const {live, solved, atCap} = facts;
  if (state === "error") {
    setStatusLine(`Problem: ${sentence(message || "the camera could not be opened")}`);
  } else if (facts.connecting) {
    // The first seconds after Open preview: the webcam is starting (up to 25 s).
    setStatusLine("Connecting to the camera…", QUIET_MESSAGES.has(message) ? "" : message);
  } else {
    let main = stateSentence(state, captures);
    // "Calibration done." only when the card below agrees.
    if (state === "solved" && !solved) main = "The solve failed: no model solved.";
    else if (state === "solved" && facts.incomplete) {
      main = `Calibration incomplete: ${listText(facts.missing.map((r) => modelName(r.model)))} did not solve.`;
    }
    else if (state === "solved" && !facts.pass) main = "Calibration done, but it needs a retake.";
    setStatusLine(main, QUIET_MESSAGES.has(message) ? "" : message);
  }
  document.getElementById("captureCount").textContent = captures;
  document.getElementById("captureAim").textContent = `· aim ${target}`;
  document.getElementById("markerCount").textContent = status.markers || 0;
  document.getElementById("coverageOverall").textContent = percent(status.coverage?.overall);
  document.getElementById("guideCount").textContent =
    `${status.guide?.complete_count || 0} / ${status.guide?.total_count || 0}`;
  const capturing = state === "capturing";
  const paused = state === "paused";
  const solving = state === "solving";
  guidePrompt.textContent = guideText(status, facts);
  // Before anything has started, the how-to on the empty preview says it all.
  guidePrompt.hidden = guidePrompt.textContent === IDLE_PROMPT;
  updateBars(status.coverage || {});
  drawCoverageMap(status.coverage?.points || [], status.image_size, targetBox(status.coverage));
  drawGuideOverlay(status);
  renderReadout(status.gopro, live);
  // Once the result passes, the poses the guide did not reach are not needed.
  const todo = facts.pass && !facts.incomplete && !facts.moreViews
    ? "Blue: not reached (not needed, the result passed)"
    : "Blue: still to do";
  if (legendTodo.textContent !== todo) legendTodo.textContent = todo;
  // With the route complete there are no blue dots to explain.
  legendTodo.parentElement.hidden = Boolean(status.guide?.complete);
  const stepValues = stepStates(facts);
  renderSteps(stepValues);
  // Until a camera setup is chosen, the step 1 dropdown is the next thing to do.
  const setupCurrent = stepValues.setup === "current";
  presetSelect.classList.toggle("select-primary", setupCurrent);

  currentState = state;
  currentLive = live;
  const done = state === "complete" || state === "solved";
  const waitSolve = "Wait for the solve to finish";
  // Open only when nothing is live; stop only when something is.
  setButton(previewBtn, !live, "Start the GoPro webcam and show its picture", "The preview is already open");
  setButton(stopBtn, live, "Stop the webcam stream", "The preview is not open");
  // Start or restart a run whenever it is not capturing or solving.
  setButton(
    startRunBtn, !(capturing || solving), "Start saving views into a new run folder",
    solving ? waitSolve : "A run is capturing. Pause it first to start over",
  );
  // Shown once a model has solved (the plan's "after a solve"); switch only
  // from a live session.
  nextCameraBtn.hidden = !solved;
  setButton(
    nextCameraBtn, live && !solving,
    "End this run and stop the webcam, so you can plug in the next camera",
    solving ? waitSolve : "Only needed while the preview is open. Otherwise plug in the next camera, give it its own Camera name in Settings, and click Open preview",
  );
  // Resume re-enables capturing from paused or after the route completed/solved,
  // so the operator can add or repeat poses on the same run.
  const resumeLabel = paused || done;
  pauseResumeBtn.textContent = resumeLabel ? "Resume" : "Pause";
  let pauseWhy = "Start a new run first";
  if (!live) pauseWhy = "Open the preview first";
  else if (solving) pauseWhy = waitSolve;
  else if (resumeLabel && atCap) pauseWhy = `The run has reached its frame cap (${status.max_samples} views)`;
  setButton(
    pauseResumeBtn, live && (capturing || paused || done) && !(resumeLabel && atCap),
    resumeLabel ? "Carry on saving views into this run" : "Stop saving views for now; the preview keeps running",
    pauseWhy,
  );
  let captureWhy = "Start a new run first";
  if (!live) captureWhy = "Open the preview first";
  else if ((paused || done) && atCap) captureWhy = "The run has reached its frame cap. Start a new run to capture more";
  else if (paused || done) captureWhy = "Resume first";
  else if (solving) captureWhy = waitSolve;
  setButton(captureBtn, capturing, "Save the current view immediately (views also save automatically)", captureWhy);
  // A fresh preview's saved views are the last run's (maybe another camera's):
  // solving them from here would mix runs, so wait for Start new run.
  setButton(
    solveBtn, captures >= minFrames && !solving && !facts.stale,
    "Compute the lens calibration from the saved views",
    solving ? "Solving now…"
      : facts.stale ? "Start a new run first: the saved views belong to the last run"
        : `Needs at least ${minFrames} views (${captures} saved so far)`,
  );
  const primary = primaryButton(facts, setupCurrent);
  for (const button of [previewBtn, stopBtn, startRunBtn, captureBtn, pauseResumeBtn, solveBtn, nextCameraBtn]) {
    button.classList.toggle("btn-primary", button === primary && !button.disabled);
  }
  let solveLine = "Computes the lens calibration from the saved views.";
  if (facts.moreViews) {
    solveLine = "Solve again to include the views added since the last solve.";
  } else if (facts.hasResults && !solved) {
    solveLine = "The last solve failed. Fix the first reason in the result below, then Solve again.";
  } else if (facts.incomplete) {
    const rows = facts.missing.length === 1 ? "row" : "rows";
    solveLine = `${listText(facts.missing.map((r) => modelName(r.model)))} did not solve. `
      + `Fix the reason on the red ${rows} in the result below, then Solve again.`;
  } else if (solved && !facts.pass) {
    solveLine = facts.solveFirst
      ? "The two fisheye solves disagree. Solve again; add edge views only if the gap stays."
      : "Solved, but the result below asks for a retake.";
  } else if (solved) {
    solveLine = live
      ? "Done. Next camera ends this run so you can plug in the next GoPro."
      : `Done. ${OTHER_CAMERA}`;
  }
  solveDesc.textContent = solveLine;

  // When the route first completes, run a checkpoint solve automatically so the
  // operator gets an early quality readout (and can then Resume to fix weak areas).
  if (state === "complete" && !autoSolved) {
    autoSolved = true;
    runSolve();
  }

  syncStream(live);
  previewEmpty.hidden = live;
  if (state === "error") {
    const views = facts.enough && (!solved || facts.incomplete)
      ? ` Or Solve the ${captures} views already saved.`
      : "";
    previewEmpty.replaceChildren(Object.assign(document.createElement("p"), {
      textContent: `${sentence(status.message || "The camera could not be opened")} Fix it, then Open preview again.${views}`,
    }));
  } else if (previewEmpty.innerHTML !== previewEmptyHTML) {
    previewEmpty.innerHTML = previewEmptyHTML;
  }

  const bridge = status.video_bridge;
  const fwCmd = bridge ? bridge.ufw_command || extractUfwFromError(bridge.error) : "";
  renderFirewall(bridge, fwCmd);

  if (status.results && status.results.length && !facts.stale) {
    renderResults(status.results, status.coverage, status.acquisition_mode, facts);
    results.hidden = true;
  } else {
    resultsPanel.hidden = true;
    renderDiagnostics(status, fwCmd);
  }
  // From a recording cannot be picked while the preview is open.
  renderRouteChoice();
}

async function poll() {
  try {
    updateStatus(await api("/api/session/status"));
  } catch {
    setStatusLine("Problem: the server is not reachable. Is it still running?");
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
    const list = data.presets || [];
    presetSelect.replaceChildren(presetCustom);
    for (const preset of list) {
      presetSelect.append(new Option(preset.title || preset.name, preset.name));
    }
    const configs = await Promise.all(list.map((preset) =>
      api(`/api/presets/${encodeURIComponent(preset.name)}`)
        .then((data) => [preset.name, data.config])
        .catch(() => [preset.name, null])));
    presetConfigs = Object.fromEntries(configs);
    shippedCameraNames = new Set(["gopro_camera", ...list
      .filter((preset) => preset.source === "shipped")
      .map((preset) => presetConfigs[preset.name]?.camera?.camera_name)
      .filter(Boolean)]);
  } catch {
    // Presets endpoint unavailable; the dropdown keeps only "Custom settings".
  }
}

// JSON with sorted keys, so two configs compare equal whatever their key order.
function stableJson(value) {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${stableJson(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

// A config as the form would read it back, minus the camera name: renaming the
// camera for the next GoPro keeps the same camera setup.
function setupKey(config) {
  const copy = structuredClone(config);
  for (const input of form.elements) {
    if (!input.name) continue;
    setDeep(copy, input.name, parseFormValue(input, formString(input, getDeep(config, input.name))));
  }
  if (copy.camera) delete copy.camera.camera_name;
  return stableJson(copy);
}

// The camera setup the form holds, or "" for custom settings.
function matchingPreset(config) {
  const want = setupKey(config);
  for (const [name, cfg] of Object.entries(presetConfigs)) {
    if (cfg && setupKey(cfg) === want) return name;
  }
  return "";
}

function showPresetChoice(name) {
  presetChoice = name;
  presetSelect.value = name;
  presetCustom.hidden = Boolean(name); // offered only while it is the truth
  showFullChoice(presetSelect);
  if (lastStatus) updateStatus(lastStatus); // step 1 follows the choice
  refreshRecordingConfig();
}

function appliedNote(what) {
  return currentLive
    ? `${what} They reach the camera when you click Start new run.`
    : `${what}`;
}

// A choice in step 1 fills in the settings at once (there is no Load button).
async function applyPreset(name) {
  try {
    const preset = await api(`/api/presets/${encodeURIComponent(name)}`);
    const cfg = preset.config || preset;
    baseConfig = structuredClone(cfg);
    presetConfigs[name] = cfg;
    settingsChosen = true;
    populateForm(cfg, defaults.aruco_dictionaries, defaults.gopro_options);
    showPresetChoice(name);
    presetMsg.textContent = appliedNote(`Settings filled in for "${preset.title || name}".`);
  } catch (err) {
    showPresetChoice(presetChoice);
    presetMsg.textContent = `Could not load that camera setup: ${err.message || err}`;
  }
}

function resetSettings() {
  // Back to the settings the server started with.
  settingsChosen = true;
  baseConfig = null;
  populateForm(defaults.config, defaults.aruco_dictionaries, defaults.gopro_options);
  showPresetChoice(matchingPreset(defaults.config));
  presetMsg.textContent = appliedNote("Put back the settings the server started with.");
}

async function savePreset() {
  const name = prompt("Save the current settings as a camera setup named:");
  if (!name) return;
  try {
    const saved = await api(`/api/presets/${encodeURIComponent(name)}`, {
      method: "POST",
      body: JSON.stringify({config: readForm()}),
    });
    settingsChosen = true;
    await loadPresetList();
    // The server may tidy the name; the file it wrote is the one to select.
    const stem = (saved.path || "").split(/[\\/]/).pop().replace(/\.ya?ml$/, "") || name;
    showPresetChoice(stem in presetConfigs ? stem : "");
    presetMsg.textContent = `Saved "${name}". It is now listed under Camera setup.`;
  } catch (err) {
    presetMsg.textContent = `Could not save the camera setup: ${err.message || err}`;
  }
}

// ---- Actions ----

// Every button runs through here, so a failed request says what failed on the
// status line instead of dying silently in the console.
function act(label, fn) {
  return async () => {
    const name = typeof label === "function" ? label() : label;
    try {
      await fn();
    } catch (err) {
      setStatusLine(`Problem: ${name} failed: ${err.message || err}`);
      syncStream(currentLive);
    }
  };
}

previewBtn.addEventListener("click", act("Open preview", async () => {
  firewallDismissed = false;
  autoSolved = false;
  startStream();
  updateStatus(await api("/api/session/preview", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
}));

startRunBtn.addEventListener("click", act("Start new run", async () => {
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
}));

pauseResumeBtn.addEventListener("click", act(() => pauseResumeBtn.textContent, async () => {
  const action = pauseResumeBtn.textContent === "Resume" ? "resume" : "pause";
  updateStatus(await api(`/api/session/${action}`, {method: "POST"}));
}));

stopBtn.addEventListener("click", act("Stop", async () => {
  // Tear the video down first so a final stream "load" cannot flip the dot back
  // to green while the request is in flight.
  stopStream();
  clearInterval(pollTimer);
  updateStatus(await api("/api/session/stop", {method: "POST"}));
}));

captureBtn.addEventListener("click", act("Capture now", async () => {
  updateStatus(await api("/api/session/capture", {method: "POST"}));
}));

async function runSolve() {
  // After Stop nothing polls, so no "solving" status would disable the button.
  solveBtn.disabled = true;
  setStatusLine(stateSentence("solving", 0));
  try {
    // Pause capture first so solving from an active run is a clean, explicit stop.
    if (currentState === "capturing") {
      await api("/api/session/pause", {method: "POST"});
    }
    await api("/api/session/solve", {method: "POST"});
  } catch (err) {
    await poll(); // the session parks itself paused on a failed solve
    setStatusLine(`Problem: Solve failed: ${err.message || err}`);
    return;
  }
  await poll(); // status now carries results -> updateStatus renders the panel
}

solveBtn.addEventListener("click", runSolve);

nextCameraBtn.addEventListener("click", act("Next camera", async () => {
  const captures = lastStatus?.captures || 0;
  // The button is hidden until something solved; if a model the run asked for
  // failed (its files are missing), ask before ending the run.
  const failed = (lastStatus?.results || []).filter(
    (r) => !solvedOk(r) && OPENICC_MODELS.has(r.model),
  );
  // Views added since the last solve are not in its files either.
  const unsolvedViews = lastStatus?.state !== "solved";
  const why = failed.length ? missingSentence(failed, cameraOf(lastStatus))
    : unsolvedViews ? "The views added since the last solve are not in its files." : "";
  if (captures > 0 && why && !confirm(
    `${why} Next camera ends this run. Its ${captures} views stay on disk but can then be `
    + "solved only with the solve-frames command. Continue?",
  )) return;
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
}));

presetSelect.addEventListener("change", () => applyPreset(presetSelect.value));
presetSave.addEventListener("click", savePreset);
presetReset.addEventListener("click", resetSettings);

for (const name of ["gopro.enabled", "gopro.apply_on_preview"]) {
  form.elements[name].addEventListener("change", () => {
    syncGoproVisibility();
    if (lastStatus) updateStatus(lastStatus); // the idle readout depends on both
  });
}
form.addEventListener("input", (event) => {
  if (event.target.name?.startsWith("board.")) updateBoardSummary();
  if (event.target.name === "solver.models") showFullChoice(event.target);
  refreshRecordingConfig();
  // An edit is a choice: custom settings made on purpose finish step 1.
  const firstChoice = !settingsChosen;
  settingsChosen = true;
  // Step 1 names the setup only while the form still holds it.
  const name = matchingPreset(readForm());
  if (name === presetChoice) {
    if (firstChoice && !name) {
      presetMsg.textContent =
        "Custom settings. Click Open preview when ready; Save as… keeps them as a camera setup.";
    }
    if (firstChoice && lastStatus) updateStatus(lastStatus);
    return;
  }
  const was = presetChoice;
  showPresetChoice(name);
  presetMsg.textContent = name
    ? `These settings match "${presetSelect.selectedOptions[0]?.text || name}".`
    : was
      ? "You changed a setting, so these are now custom settings. Save as… keeps them as a camera setup."
      : "";
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

// Canvases are sized to their CSS box; redraw them whenever that box changes
// (window resize, a scrollbar appearing, the results panel opening).
function redrawCanvases() {
  const status = lastStatus || {};
  drawCoverageMap(status.coverage?.points || [], status.image_size, targetBox(status.coverage));
  if (lastStatus) drawGuideOverlay(lastStatus);
}
new ResizeObserver(redrawCanvases).observe(scatter);
new ResizeObserver(redrawCanvases).observe(guideOverlay);

// ---- Route: live over USB, or from a recording ----

// The From a recording route: the operator sets the camera (checklist and GoPro Labs
// QR codes), records a clip on the camera while following an animation of the board
// positions, then drops the mp4 here. The server's recording job (/api/recording/*)
// checks the clip, picks the views and solves, independent of the live session.

const appEl = document.getElementById("app");
const routeRadios = [...document.querySelectorAll('input[name="calibrationRoute"]')];
const routeLiveOption = document.getElementById("routeLiveOption");
const routeRecordingOption = document.getElementById("routeRecordingOption");
const recSettingsBtn = document.getElementById("recSettingsBtn");
const recShowSettingsBtn = document.getElementById("recShowSettingsBtn");
const recRecordedBtn = document.getElementById("recRecordedBtn");
const recShowRecordBtn = document.getElementById("recShowRecordBtn");
const recChooseBtn = document.getElementById("recChooseBtn");
const recNextBtn = document.getElementById("recNextBtn");
const recRedoBtn = document.getElementById("recRedoBtn");
const recDropDesc = document.getElementById("recDropDesc");
const recPrompt = document.getElementById("recPrompt");
const recHint = document.getElementById("recHint");
const recReminder = document.getElementById("recReminder");
const qrDatasetSmall = document.getElementById("qrDatasetSmall");
const recBack = document.getElementById("recBack");
const recBackBtn = document.getElementById("recBackBtn");
const recPanels = {
  settings: document.getElementById("recSettingsPanel"),
  record: document.getElementById("recRecordPanel"),
  drop: document.getElementById("recDropPanel"),
};
const labsError = document.getElementById("labsError");
const labsBody = document.getElementById("labsBody");
const qrCalibration = document.getElementById("qrCalibration");
const qrDataset = document.getElementById("qrDataset");
const qrCalibrationCode = document.getElementById("qrCalibrationCode");
const qrDatasetCode = document.getElementById("qrDatasetCode");
const shutterWarningText = document.getElementById("shutterWarningText");
const labsUnverified = document.getElementById("labsUnverified");
const labsUnverifiedList = document.getElementById("labsUnverifiedList");
const labsChecklist = document.getElementById("labsChecklist");
const labsAlternatives = document.getElementById("labsAlternatives");
const labsAlternativesList = document.getElementById("labsAlternativesList");
const recAnim = document.getElementById("recAnim");
const animToggle = document.getElementById("animToggle");
const animCaption = document.getElementById("animCaption");
const animLegend = document.getElementById("animLegend");
const animTodo = document.getElementById("animTodo");
const positionsList = document.getElementById("positionsList");
const positionsOl = document.getElementById("positionsOl");
const dropZone = document.getElementById("dropZone");
const dropZoneTitle = document.getElementById("dropZoneTitle");
const dropZoneNote = document.getElementById("dropZoneNote");
const recFile = document.getElementById("recFile");
const recProgress = document.getElementById("recProgress");
const recProgressLabel = document.getElementById("recProgressLabel");
const recProgressPct = document.getElementById("recProgressPct");
const recProgressBar = document.getElementById("recProgressBar");
const recProgressFill = document.getElementById("recProgressFill");
const recProgressNote = document.getElementById("recProgressNote");
const recAlert = document.getElementById("recAlert");
const recAlertText = document.getElementById("recAlertText");
const recSwitchNote = document.getElementById("recSwitchNote");
const recNotesList = document.getElementById("recNotes");
const recCamera = document.getElementById("recCamera");
const recStats = document.getElementById("recStats");
const recCounts = document.getElementById("recCounts");
const recMissing = document.getElementById("recMissing");
const recMissingHead = document.getElementById("recMissingHead");
const recMissingList = document.getElementById("recMissingList");
const recMapWrap = document.getElementById("recMapWrap");
const recMap = document.getElementById("recMap");
const recMapTodo = document.getElementById("recMapTodo");
const recClips = document.getElementById("recClips");
const clipCheck = document.getElementById("clipCheck");
const clipBanner = document.getElementById("clipBanner");
const clipUnknown = document.getElementById("clipUnknown");
const clipUnknownList = document.getElementById("clipUnknownList");
const clipOther = document.getElementById("clipOther");
const clipOtherList = document.getElementById("clipOtherList");

const ROUTE_KEY = "gopro-charuco.route";
const REC_BUSY = new Set(["uploading", "analysing", "solving"]);
const NO_RECORDING = "This camera setup has no settings for recording on the camera. "
  + "Pick a HERO13 lens-mod setup in step 1 for the From a recording route.";
const REC_SOLVE_FIRST = "Retake: the two fisheye solves disagree. Add another clip with views "
  + "near the edge of the circle; the run is solved again with it.";
const RECORD_TEXT = "Record 60–90 s. Move slowly and hold each position for about a second. "
  + "Push the board right to the edges of the frame.";
// Server messages that only repeat what the page already says.
const REC_QUIET = new Set([
  "no recording run yet", "Ready for the clip.", "Ready for the next camera.", "Checking the clip.",
  "Solving.", "Calibration solved.",
]);
const DROP_WORDS = {
  no_board: "no board found",
  blurred: "blurred",
  moving: "moving too fast",
  duplicate: "too like a view already kept",
  over_cap: "over the view limit",
};

// The route is a per-viewer convenience: remembered in this browser only.
function loadRoute() {
  try {
    return localStorage.getItem(ROUTE_KEY) === "recording" ? "recording" : "live";
  } catch {
    return "live";
  }
}

function saveRoute(value) {
  try {
    localStorage.setItem(ROUTE_KEY, value);
  } catch {
    // Storage blocked (private window): the choice lasts for this page only.
  }
}

let calibrationRoute = loadRoute();
appEl.dataset.route = calibrationRoute;
for (const radio of routeRadios) radio.checked = radio.value === calibrationRoute;

let recStatus = null;
// What the Labs endpoint returned for the recording settings in the form.
let recLabs = null;
let recLabsKey = null;
let recLabsError = "";
// The 23 guide positions, with plain words for the animation.
let recGuide = null;
let recGuideKey = null;
// The operator's own progress through steps 2 and 3 (the server knows nothing of it).
const recConfirmed = {settings: false, recorded: false};
// A finished step the operator asked to see again, or null for the current step.
let recView = null;
let recQueue = [];
// The upload in flight: {name, loaded, total, left}.
let recUpload = null;
// A request that failed before the job could say so (start refused, upload cut), and
// the job's state and run when it failed: once either moves on, the error is stale.
let recLocalError = "";
let recLocalErrorKey = null;
// A file dropped where it cannot be used yet (steps 2 and 3, or while a clip is busy).
let recHintText = "";
// The run left behind by Start this camera again, named in step 2 until a clip is in.
let recRestartedFrom = "";
// Files left out of a multi-file drop, and clips refused on the way through a queue.
let recNotes = [];
let recPollTimer = null;
let recConfigTimer = null;

function recordingConfig() {
  return readForm().recording || null;
}

function humanBytes(bytes) {
  if (bytes == null) return "";
  if (bytes <= 0) return "0 kB";
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`;
  return `${Math.max(1, Math.round(bytes / 1e3))} kB`;
}

function plural(n, word, many = `${word}s`) {
  return `${n} ${n === 1 ? word : many}`;
}

// ---- Clip check ----

// Every mismatch over every clip: settings as "Frame rate is 30 fps, expected 60 fps",
// and a clip that could not be read cleanly in the check's own words (its advice
// names what went wrong: cut short, or an ffmpeg error).
function mismatchItems(status) {
  const clips = status.clips || [];
  const several = clips.length > 1;
  const settings = [];
  const reads = [];
  for (const clip of clips) {
    for (const row of clip.check || []) {
      if (row.status !== "mismatch") continue;
      if (row.field === "complete") {
        const advice = row.advice || "Copy the file from the card again.";
        reads.push(`${several ? clip.name : "The clip"} could not be read cleanly: ${advice}`);
      } else {
        const where = several ? `in ${clip.name}, ` : "";
        settings.push(`${where}${row.label} is ${row.found ?? "missing"}, expected ${row.expected ?? "nothing"}`);
      }
    }
  }
  return {settings, reads};
}

// The loud warning, and the first sentence of it that the result headline repeats.
// `settings` is true when a setting differs (not only a clip read short).
function clipWarning(status) {
  const {settings, reads} = mismatchItems(status);
  if (!settings.length && !reads.length) return null;
  const clips = status.clips || [];
  const one = clips.length <= 1;
  const clipsWord = one ? "this clip" : "these clips";
  if (!settings.length) return {first: reads.join(" "), rest: "", clipsWord, settings: false};
  const lead = one
    ? "This clip was not recorded with the preset's settings"
    : "Not every clip was recorded with the preset's settings";
  const first = `${lead}: ${settings.join("; ")}.`;
  const rest = [
    "Check every setting on the camera again, and record the clip again if any differ. "
      + `The calibration below is only valid for footage recorded exactly like ${clipsWord}.`,
    ...reads,
  ].join(" ");
  return {first, rest, clipsWord, settings: true};
}

// The check's own advice, once each: what the file could not confirm, and the other
// notes on rows that passed (a short clip).
function clipAdvice(status) {
  const seen = new Set();
  const unknown = [];
  const other = [];
  for (const clip of status.clips || []) {
    for (const row of clip.check || []) {
      if (row.status === "mismatch" || !row.advice || seen.has(row.advice)) continue;
      seen.add(row.advice);
      (row.status === "unknown" ? unknown : other).push(row.advice);
    }
  }
  return {unknown, other};
}

// ---- Facts, steps and buttons ----

function recFacts(status) {
  const s = status || {};
  const state = s.state || "idle";
  const busy = Boolean(recUpload) || REC_BUSY.has(state);
  const clips = s.clips || [];
  const hasRun = Boolean(s.run_id);
  // While a retake is picked and solved, the result on screen no longer applies.
  const results = busy ? [] : s.results || [];
  const verdict = judge(results, s.coverage);
  const solved = verdict.rec !== null;
  const failedOpenicc = solved ? verdict.failed.filter((r) => OPENICC_MODELS.has(r.model)) : [];
  const supported = Boolean(recordingConfig());
  const started = hasRun || busy || clips.length > 0;
  const setupChosen = Boolean(presetChoice) || settingsChosen;
  const settingsDone = started || recConfirmed.settings;
  const recordedDone = started || recConfirmed.recorded;
  let step = "drop";
  if (!supported) step = "setup";
  else if (!settingsDone) step = "settings";
  else if (!recordedDone) step = "record";
  const warning = busy ? null : clipWarning(s);
  // A clip still being processed is listed without counts; only finished clips count.
  const finishedClips = clips.filter((clip) => clip.counts).length;
  const f = {
    route: "recording",
    state, busy, clips, hasRun, started, supported, setupChosen, step,
    results,
    hasResults: results.length > 0,
    solved,
    pass: verdict.pass,
    live: false,
    canResume: false,
    moreViews: false,
    missing: failedOpenicc,
    incomplete: failedOpenicc.length > 0,
    missingText: failedOpenicc.length ? missingSentence(failedOpenicc, cameraOf(s)) : "",
    solveFirst: solved && !verdict.pass && verdict.reasons.length > 0
      && verdict.reasons.every((reason) => reason.startsWith(DISAGREE)),
    clipWarning: warning ? warning.first : "",
    clipsWord: warning ? warning.clipsWord : "this clip",
    warning,
    settingsDiffer: Boolean(warning?.settings),
    finishedClips,
    missingPositions: missingPositions(s),
    // The server clears both only once the next clip gets that far: stale while busy.
    refused: busy ? null : s.refused_clip || null,
    needsMore: !busy && Boolean(s.needs_more_views),
    error: recLocalError || (state === "error" ? s.message || "" : ""),
  };
  // The result on show passed. A clip refused after it (or a failed retake) leaves it
  // standing, so Next camera stays the next action; the step is done only without one,
  // and never while the clip check found a problem.
  f.passed = f.solved && f.pass && !f.incomplete && !busy;
  f.done = f.passed && !f.refused && !f.error && !f.warning;
  // Record this camera again in a fresh run: offered when a clip's settings differ or
  // a clip was refused, and the next action when the clip check found a problem.
  // A clip refused for its size gets a run of its own only when it is the one in the
  // camera setup's mode; one in another mode is recorded again (the refusal says so).
  f.canRedo = !busy && hasRun
    && (Boolean(f.warning)
      || (f.refused?.reason === "different_size" && f.refused.matches_setup !== false));
  return f;
}

function recStepStates(f) {
  const setup = !f.supported || (!f.setupChosen && !f.started) ? "current" : "done";
  if (setup === "current") return {setup, settings: "pending", record: "pending", drop: "pending"};
  const order = ["settings", "record", "drop"];
  const at = order.indexOf(f.step);
  const states = {setup};
  order.forEach((name, i) => {
    states[name] = i < at ? "done" : i === at ? "current" : "pending";
  });
  if (f.done) states.drop = "done";
  return states;
}

// The one obvious next action, or null while a clip is being copied or processed.
function recPrimary(f, states) {
  if (states.setup === "current") return null;
  if (f.step === "settings") return recSettingsBtn;
  if (f.step === "record") return recRecordedBtn;
  if (f.busy) return null;
  // The banner asks for the clip again: a fresh run keeps this clip's views out.
  if (f.warning && f.canRedo) return recRedoBtn;
  if (f.passed) return recNextBtn;
  return recChooseBtn;
}

// The panel on show: the current step, or a finished one the operator went back to.
function currentRecView(f) {
  const step = f.step === "setup" ? "settings" : f.step;
  return recView || step;
}

function recStatusLine(f, s) {
  if (!f.supported) return ["Pick a camera setup for recording in step 1.", ""];
  const message = s.message || "";
  const detail = REC_QUIET.has(message) ? "" : message;
  if (recUpload) {
    const pct = recUpload.total ? ` ${percent(recUpload.loaded / recUpload.total)}` : "";
    const left = recUpload.left ? `${plural(recUpload.left, "more clip")} after this one.` : "";
    return [`Copying ${recUpload.name} into the run folder:${pct}.`, left];
  }
  // A refused clip's reason is in the red box of the drop step, in full.
  if (f.refused) return ["Problem: the last clip was not used.", ""];
  if (f.needsMore && !recLocalError) {
    const need = s.min_frames || Number(form.elements["solver.min_frames"].value || 25);
    return [`Not enough views to solve yet: ${plural(s.captures || 0, "view")} kept, ${need} needed.`, ""];
  }
  if (f.error) return [`Problem: ${sentence(f.error)}`, ""];
  switch (f.state) {
    case "uploading": return [`Copying ${s.upload?.name || "the clip"} into the run folder.`, ""];
    case "analysing":
      // The job's own "Picking views from X." only repeats this line; a camera-switch
      // note in front of it does not.
      return s.stage === "extract"
        ? [`Picking views from the clip: ${percent(s.progress)}.`,
          detail.startsWith("Picking views") ? "" : detail]
        : ["Checking the clip…", detail];
    case "solving": return ["Solving… this can take a minute.", ""];
    case "solved": {
      let main = "Calibration done.";
      if (!f.solved) main = "The solve failed: no model solved.";
      else if (f.incomplete) main = `Calibration incomplete: ${listText(f.missing.map((r) => modelName(r.model)))} did not solve.`;
      else if (f.settingsDiffer) main = "Calibration done, but the clip was not recorded with the preset's settings.";
      else if (f.warning) main = "Calibration done, but a clip could not be read cleanly.";
      else if (!f.pass) main = "Calibration done, but it needs a retake.";
      // The job's message repeats "Calibration solved." and the clip check's list,
      // which the page already shows; only a camera-switch note in front is new.
      const switchNote = s.camera_switch?.message || "";
      return [main, switchNote && message.startsWith(switchNote) ? switchNote : ""];
    }
    default:
      if (f.step === "settings") return ["Set the camera up for the calibration clip.", ""];
      if (f.step === "record") return ["Record the calibration clip on the camera.", ""];
      return ["Ready for the clip.", detail];
  }
}

// The guide positions the kept views do not cover yet, in route order.
function missingPositions(s) {
  const points = s.guide?.checkpoints || [];
  const words = recGuide?.checkpoints || [];
  const missing = [];
  points.forEach((point, index) => {
    if (!point.complete) {
      missing.push({number: index + 1, caption: words[index]?.caption || point.label});
    }
  });
  return missing;
}

// How to record the next clip when one reason left out most of the frames.
const DROP_ADVICE = {
  moving: "Most frames were left out because the board was moving: hold it still for about "
    + "a second at each position.",
  no_board: "Most frames were left out because no board was found: keep the whole board in "
    + "view.",
  blurred: "Most frames were left out because they were blurred: move slowly, and keep the "
    + "board lit and in focus.",
};

function dropAdvice(s) {
  const counts = s.counts || {};
  const left = Object.keys(DROP_WORDS).reduce((sum, key) => sum + (counts[key] || 0), 0);
  const top = Object.keys(DROP_ADVICE).find((key) => (counts[key] || 0) * 2 > left);
  return top && counts.samples ? ` ${DROP_ADVICE[top]}` : "";
}

// What the next clip must fix: the missing positions when some are listed, else the
// reasons in the result (a solved RETAKE with every position covered), or more views.
function retakeText(f, s) {
  const count = f.missingPositions.length;
  const then = "then click Add another clip. Its views are added to this run.";
  const advice = dropAdvice(s);
  if (count) {
    return `Retake: record another clip that covers the ${plural(count, "missing position")} `
      + `listed below, ${then}${advice}`;
  }
  if (f.needsMore) {
    return `Retake: record another clip that holds the board still at more positions, ${then}${advice}`;
  }
  return `Retake: record another clip that fixes the reasons in the result below, ${then}${advice}`;
}

// A clip whose settings differ (or that was read short) is recorded again in a fresh
// run, so its views never mix with the new clip's.
function redoText(f, s) {
  const run = s.run_dir || "its run folder";
  const keep = f.passed ? " To keep this result anyway, click Next camera." : "";
  if (f.settingsDiffer) {
    return "The clip's settings differ from the camera setup: read the red box. To record it "
      + "again: fix the settings on the camera, click Start this camera again (this run stays "
      + `in ${run}), then follow steps 2 to 4 with a new clip. Do not add the new clip to this `
      + `run.${keep}`;
  }
  return "A clip could not be read cleanly: read the red box. To use all of it: copy the clip "
    + `from the card again, click Start this camera again (this run stays in ${run}), then drop `
    + `the new copy in step 4.${keep}`;
}

function recPromptText(f, s) {
  if (!f.supported) {
    return "Pick a HERO13 lens-mod camera setup in step 1 (Max Lens Mod 2.0 or Ultra Wide Lens "
      + "Mod), or choose Live over USB.";
  }
  const view = currentRecView(f);
  if (view !== f.step && view === "settings") {
    return "The settings for this camera, again. Click Back to return to the current step.";
  }
  if (view !== f.step && view === "record") {
    return `${RECORD_TEXT} Click Back to return to the current step.`;
  }
  if (f.step === "settings") {
    const again = recRestartedFrom
      ? `Starting this camera again; the earlier run stays in ${recRestartedFrom}. `
      : "";
    return `${again}Scan the first QR code with the camera, then check each setting below on `
      + "the camera screen. Click I've set the camera when they all match.";
  }
  if (f.step === "record") return `${RECORD_TEXT} Then click I've recorded the clip.`;
  if (f.busy) {
    if (recUpload) return "Copying the clip. Keep this page open until it is done.";
    if (f.state === "uploading") return "Copying the clip into the run folder.";
    return "Working on the clip. A 90 s clip can take a few minutes.";
  }
  const standing = "The calibration below still passes: click Next camera when this camera is done";
  // The clip check's problem comes first: Start this camera again is the next action,
  // even when a later clip was refused as well.
  if (f.warning && f.canRedo) {
    return `${f.refused ? "The last clip was not used either; read why below. " : ""}${redoText(f, s)}`;
  }
  if (f.refused) {
    // The reason below names Start this camera again where it applies.
    return f.passed
      ? `That clip was not used; read why below. ${standing}, or add another clip.`
      : "That clip was not used. Read why below, then add another clip.";
  }
  if (f.error && f.passed) {
    return `The last clip could not be used; the problem is below. ${standing}, or drop the clip again.`;
  }
  if (f.needsMore) return retakeText(f, s);
  if (f.error) {
    return f.hasRun
      ? "Fix the problem on the status line, then drop the clip again."
      : "Fix the problem on the status line, then choose the clip again.";
  }
  if (f.hasResults && !f.solved) {
    return "No model solved. Read the reasons in the result below, then drop the clip again, "
      + "or add another clip.";
  }
  if (f.incomplete) return fixMissing(f);
  if (f.solveFirst) return REC_SOLVE_FIRST;
  if (f.solved && !f.pass) return retakeText(f, s);
  if (f.done) {
    return "Solved. Before you record the dataset, scan QR code 2 below to put the shutter back "
      + "on Auto. Then click Next camera for the next GoPro.";
  }
  return "Copy the clip (GX01xxxx.MP4) from the camera's card, then drop it below or click "
    + "Choose clip…";
}

function renderRecButtons(f, states) {
  const view = currentRecView(f);
  const inFlight = f.busy ? "Wait for this clip to finish" : "";
  // Step 2: confirm, or look at the settings again once past it.
  recSettingsBtn.hidden = states.settings === "done";
  recShowSettingsBtn.hidden = states.settings !== "done";
  setButton(
    recSettingsBtn, f.step === "settings" && Boolean(recLabs),
    "Go on to recording the clip",
    !f.supported ? "Pick a camera setup with recording settings in step 1 first"
      : recLabsError ? "The settings could not be loaded; see the message below"
        : "Loading the settings…",
  );
  setButton(
    recShowSettingsBtn, view !== "settings", "Show the checklist and the QR codes again",
    "The settings are on show below",
  );
  recRecordedBtn.hidden = states.record === "done";
  recShowRecordBtn.hidden = states.record !== "done";
  setButton(
    recRecordedBtn, f.step === "record", "Go on to dropping the clip",
    f.supported ? "Set up the camera first (step 2)" : "Pick a camera setup with recording settings in step 1 first",
  );
  setButton(
    recShowRecordBtn, view !== "record", "Show the animation of the board positions again",
    "The animation is on show below",
  );
  // "Add another clip" once a clip has finished; the first one still in work is not one.
  const another = f.finishedClips > 0;
  setText(recChooseBtn, another ? "Add another clip" : "Choose clip…");
  let chooseWhy = "Finish the steps before this one first";
  if (!f.supported) chooseWhy = "Pick a camera setup with recording settings in step 1 first";
  else if (f.busy) chooseWhy = inFlight;
  setButton(
    recChooseBtn, f.step === "drop" && !f.busy,
    another ? "Pick another clip of this camera; its views are added to this run"
      : "Pick the clip recorded on the camera (.mp4)",
    chooseWhy,
  );
  recRedoBtn.hidden = !f.canRedo;
  setButton(
    recRedoBtn, f.canRedo,
    `End this run (its files stay in ${recStatus?.run_dir || "its run folder"}) and start a `
      + "fresh run for the same camera at step 2",
    inFlight,
  );
  recNextBtn.hidden = !(f.clips.length || f.solved);
  setButton(
    recNextBtn, !f.busy, "End this camera's run and start again at step 2 for the next camera",
    inFlight,
  );
  const primary = recPrimary(f, states);
  for (const button of [recSettingsBtn, recShowSettingsBtn, recRecordedBtn, recShowRecordBtn, recChooseBtn, recRedoBtn, recNextBtn]) {
    button.classList.toggle("btn-primary", button === primary && !button.disabled);
  }
  let desc = "Drop the clip here. The app picks the views and solves.";
  if (f.busy) desc = "Working on the clip. Wait for the result.";
  else if (f.done) desc = "Done. Scan QR code 2 before you record the dataset. Next camera starts again at step 2.";
  else if (f.warning && f.canRedo) desc = "The clip check found a problem: see the red box. Start this camera again, or click Next camera.";
  else if (another) desc = "Add another clip of this camera, or click Next camera.";
  setText(recDropDesc, desc);
}

// The route choice cannot change while the other route is busy.
function renderRouteChoice() {
  const liveOpen = Boolean(lastStatus) && isLive(lastStatus);
  const recBusy = Boolean(recUpload) || REC_BUSY.has(recStatus?.state);
  for (const radio of routeRadios) {
    const other = radio.value !== calibrationRoute;
    radio.disabled = other && (radio.value === "recording" ? liveOpen : recBusy);
  }
  routeRecordingOption.title = liveOpen && calibrationRoute === "live"
    ? "Stop the live preview first" : "";
  routeLiveOption.title = recBusy && calibrationRoute === "recording"
    ? "Wait for the clip to finish" : "";
}

function renderRecReadout(s) {
  const chips = [];
  if (s.camera_name && s.run_id) chips.push(chip(s.camera_name, {key: "Camera"}));
  if (s.serial) chips.push(chip(s.serial, {key: "Serial"}));
  const model = (s.clips || []).map((c) => c.metadata?.model).find(Boolean);
  if (model) chips.push(chip(model, {key: "Model"}));
  const signature = `rec|${chips.map((el) => el.textContent).join("|")}`;
  if (cameraReadout.dataset.signature === signature) return;
  cameraReadout.dataset.signature = signature;
  cameraReadout.replaceChildren(...chips);
}

// ---- Settings panel ----

let labsSignature = "";

function renderLabs(f) {
  const error = !f.supported ? NO_RECORDING : recLabsError;
  labsError.hidden = !error;
  labsError.textContent = error;
  labsBody.hidden = Boolean(error) || !recLabs;
  if (!recLabs || error) return;
  const signature = JSON.stringify(recLabs);
  if (signature === labsSignature) return;
  labsSignature = signature;
  qrCalibration.src = recLabs.calibration.png;
  qrDataset.src = recLabs.dataset.png;
  qrDatasetSmall.src = recLabs.dataset.png;
  qrCalibrationCode.textContent = recLabs.calibration.code;
  qrDatasetCode.textContent = recLabs.dataset.code;
  const rec = recordingConfig() || {};
  const shutter = rec.calibration_shutter || "1/480";
  shutterWarningText.textContent = `The calibration clip uses a fast shutter (${shutter} s); `
    + "the dataset is recorded with the shutter on Auto. Before you record the dataset, scan "
    + "the second QR code, or set Shutter back to Auto in Protune.";
  // Codes the Labs docs do not confirm for a HERO13, once each: what to check and do
  // first, then the code itself for reference; a code only QR code 1 carries says so.
  const dataset = new Set(recLabs.dataset.unverified.map((u) => u.code));
  const seen = new Set();
  const items = [];
  for (const entry of [...recLabs.calibration.unverified, ...recLabs.dataset.unverified]) {
    if (seen.has(entry.code)) continue;
    seen.add(entry.code);
    const li = document.createElement("li");
    const code = Object.assign(document.createElement("code"), {textContent: entry.code});
    li.append(`${entry.note} (code `, code, dataset.has(entry.code) ? ")" : ", QR code 1 only)");
    items.push(li);
  }
  labsUnverifiedList.replaceChildren(...items);
  labsUnverified.hidden = items.length === 0;
  // Codes for whoever confirms them on a camera, kept out of the operator's list.
  const alternatives = recLabs.alternatives || [];
  labsAlternativesList.replaceChildren(...alternatives.map((entry) => {
    const li = document.createElement("li");
    li.append(Object.assign(document.createElement("code"), {textContent: entry.code}), `: ${entry.note}`);
    return li;
  }));
  labsAlternatives.hidden = alternatives.length === 0;
  labsChecklist.replaceChildren(...recLabs.checklist.map((row) => {
    const tr = document.createElement("tr");
    for (const text of [row.setting, row.value, row.how]) {
      tr.append(Object.assign(document.createElement("td"), {textContent: text}));
    }
    return tr;
  }));
}

// Fetch the QR codes and the guide for the settings in the form, when they change.
function refreshRecordingConfig() {
  clearTimeout(recConfigTimer);
  recConfigTimer = setTimeout(loadRecordingConfig, 150);
}

async function loadRecordingConfig() {
  if (!defaults || calibrationRoute !== "recording") return;
  const config = readForm();
  const labsKey = stableJson(config.recording || null);
  if (labsKey !== recLabsKey) {
    // Another camera setup before any clip is in: its codes differ, so step 2 again.
    if (recLabsKey !== null && !recStatus?.run_id && !recUpload) {
      recConfirmed.settings = false;
      recConfirmed.recorded = false;
      recView = null;
    }
    recLabsKey = labsKey;
    recLabs = null;
    recLabsError = "";
    if (config.recording) {
      try {
        const labs = await api("/api/recording/labs", {method: "POST", body: JSON.stringify({config})});
        if (labsKey === recLabsKey) recLabs = labs;
      } catch (err) {
        if (labsKey === recLabsKey) {
          recLabsError = `Could not make the QR codes: ${sentence(err.message || err)} `
            + "Reload the page to try again.";
          recLabsKey = null; // the next change of settings (or a reload) asks again
        }
      }
    }
  }
  const guideKey = stableJson(config.coverage_targets || null);
  if (guideKey !== recGuideKey) {
    recGuideKey = guideKey;
    try {
      const guide = await api("/api/recording/guide", {method: "POST", body: JSON.stringify({config})});
      if (guideKey === recGuideKey) {
        recGuide = guide;
        positionsOl.replaceChildren(...guide.checkpoints.map((cp) =>
          Object.assign(document.createElement("li"), {textContent: cp.caption})));
      }
    } catch {
      if (guideKey === recGuideKey) {
        recGuide = null; // the animation says it has nothing to show
        recGuideKey = null;
      }
    }
  }
  renderRecording();
}

// ---- Animation: the camera's-eye view of the board positions ----

// About 64 s for the 23 positions: the pace the clip should be recorded at.
const ANIM_MOVE_MS = 1600;
const ANIM_HOLD_MS = 1200;
const ANIM_STEP_MS = ANIM_MOVE_MS + ANIM_HOLD_MS;
const anim = {playing: true, t: 0, last: 0, raf: 0, listOpened: false};
const reducedMotion = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;

function prefersReducedMotion() {
  return Boolean(reducedMotion?.matches);
}

function ease(u) {
  return u < 0.5 ? 4 * u * u * u : 1 - ((-2 * u + 2) ** 3) / 2;
}

// A tilted position turns the board about both axes, so it reads as a trapezoid.
function boardPose(cp) {
  const tilt = cp.tilted ? 1 : 0;
  return {
    x: cp.x,
    y: cp.y,
    size: cp.size,
    yaw: tilt * (cp.x < 0.5 ? 0.75 : -0.75),
    pitch: tilt * (cp.y < 0.5 ? -0.55 : 0.55),
  };
}

function animAt(t) {
  const cps = recGuide?.checkpoints || [];
  const n = cps.length;
  const total = n * ANIM_STEP_MS;
  const time = ((t % total) + total) % total;
  const index = Math.floor(time / ANIM_STEP_MS);
  const into = time - index * ANIM_STEP_MS;
  const from = boardPose(cps[(index - 1 + n) % n]);
  const to = boardPose(cps[index]);
  const u = into < ANIM_MOVE_MS ? ease(into / ANIM_MOVE_MS) : 1;
  const mix = (key) => from[key] + (to[key] - from[key]) * u;
  return {
    index,
    holding: into >= ANIM_MOVE_MS,
    pose: {x: mix("x"), y: mix("y"), size: mix("size"), yaw: mix("yaw"), pitch: mix("pitch")},
  };
}

function boardShape() {
  const cols = Number(form.elements["board.cols"]?.value) || 11;
  const rows = Number(form.elements["board.rows"]?.value) || 8;
  return {cols, rows};
}

// Board point (u, v in 0..1) -> canvas point, for a board whose bounding box is `size`
// (sqrt of its width x height fraction of the frame, as coverage.py measures it).
function boardProjector(pose, frame, shape) {
  const aspect = shape.cols / shape.rows;
  const area = pose.size * pose.size * frame.w * frame.h;
  const h = Math.sqrt(area / aspect);
  const w = h * aspect;
  const focal = 2.2 * Math.max(w, h);
  const cy = Math.cos(pose.yaw);
  const sy = Math.sin(pose.yaw);
  const cp = Math.cos(pose.pitch);
  const sp = Math.sin(pose.pitch);
  return (u, v) => {
    const x0 = (u - 0.5) * w;
    const y0 = (v - 0.5) * h;
    const x = x0 * cy;
    const z0 = x0 * sy;
    const y = y0 * cp - z0 * sp;
    const z = y0 * sp + z0 * cp;
    const k = focal / (focal + z);
    return [frame.x + pose.x * frame.w + x * k, frame.y + pose.y * frame.h + y * k];
  };
}

function drawBoard(ctx, pose, frame, shape) {
  const at = boardProjector(pose, frame, shape);
  const quad = (u0, v0, u1, v1) => {
    ctx.beginPath();
    ctx.moveTo(...at(u0, v0));
    ctx.lineTo(...at(u1, v0));
    ctx.lineTo(...at(u1, v1));
    ctx.lineTo(...at(u0, v1));
    ctx.closePath();
  };
  quad(0, 0, 1, 1);
  ctx.fillStyle = COLOR.inkFill;
  ctx.fill();
  ctx.fillStyle = "rgba(232, 230, 225, 0.42)";
  for (let r = 0; r < shape.rows; r++) {
    for (let c = 0; c < shape.cols; c++) {
      if ((r + c) % 2) continue;
      quad(c / shape.cols, r / shape.rows, (c + 1) / shape.cols, (r + 1) / shape.rows);
      ctx.fill();
    }
  }
  quad(0, 0, 1, 1);
  ctx.strokeStyle = COLOR.ink;
  ctx.lineWidth = 2;
  ctx.stroke();
}

// The 4:3 camera frame inside a canvas: dark field, grid, and the reach line.
function drawFrame(canvas, aspect = 4 / 3) {
  const {ctx, cssW, cssH} = fitCanvas(canvas);
  const scale = Math.min(cssW / aspect, cssH);
  const frame = {w: scale * aspect, h: scale};
  frame.x = (cssW - frame.w) / 2;
  frame.y = (cssH - frame.h) / 2;
  ctx.fillStyle = COLOR.field;
  ctx.fillRect(frame.x, frame.y, frame.w, frame.h);
  ctx.strokeStyle = COLOR.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 1; i < 10; i++) {
    ctx.moveTo(frame.x + (frame.w * i) / 10, frame.y);
    ctx.lineTo(frame.x + (frame.w * i) / 10, frame.y + frame.h);
    ctx.moveTo(frame.x, frame.y + (frame.h * i) / 10);
    ctx.lineTo(frame.x + frame.w, frame.y + (frame.h * i) / 10);
  }
  ctx.stroke();
  ctx.strokeStyle = COLOR.frame;
  ctx.strokeRect(frame.x + 0.5, frame.y + 0.5, frame.w - 1, frame.h - 1);
  const target = targetBox(recStatus?.coverage);
  ctx.save();
  ctx.strokeStyle = COLOR.ink;
  ctx.globalAlpha = 0.6;
  ctx.lineWidth = 1.5;
  ctx.setLineDash([10, 8]);
  ctx.strokeRect(
    frame.x + target.x * frame.w, frame.y + target.y * frame.h, target.w * frame.w, target.h * frame.h,
  );
  ctx.restore();
  return {ctx, frame};
}

function dotAt(ctx, frame, point, color, radius, dx = 0) {
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(frame.x + point.x * frame.w + dx, frame.y + point.y * frame.h, radius, 0, Math.PI * 2);
  ctx.fill();
}

// Positions that share a spot (the centre is visited three times) sit side by side:
// each position's place in its row, in dot spacings from the spot (0 when alone).
function spotOffsets(cps) {
  const groups = new Map();
  cps.forEach((cp, i) => {
    const key = `${cp.x.toFixed(3)},${cp.y.toFixed(3)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(i);
  });
  const offsets = cps.map(() => 0);
  for (const indices of groups.values()) {
    indices.forEach((i, k) => {
      offsets[i] = k - (indices.length - 1) / 2;
    });
  }
  return offsets;
}

function drawAnimation() {
  const cps = recGuide?.checkpoints || [];
  const {ctx, frame} = drawFrame(recAnim);
  if (!cps.length) {
    setText(animCaption, "The positions could not be loaded. Reload the page.");
    return;
  }
  if (prefersReducedMotion()) {
    drawNumbered(ctx, frame, cps, null);
    setText(animCaption, `Hold the board at each numbered position in turn, 1 to ${cps.length}. `
      + "The list below says how far away and where.");
    return;
  }
  const {index, pose} = animAt(anim.t);
  // Shared spots side by side, as on the numbered map, so a position still to come
  // is never hidden under one already shown.
  const offsets = spotOffsets(cps);
  const spacing = 18;
  cps.forEach((cp, i) => {
    if (i !== index) {
      dotAt(ctx, frame, cp, i < index ? COLOR.pass : COLOR.target, 6, offsets[i] * spacing);
    }
  });
  drawBoard(ctx, pose, frame, boardShape());
  dotAt(ctx, frame, cps[index], COLOR.signal, 10, offsets[index] * spacing);
  setText(animCaption, `position ${index + 1}/${cps.length} · ${cps[index].caption}`);
}

// Numbered positions (reduced motion, and the drop step's coverage map). Positions
// that share a spot (the centre is visited three times) sit side by side.
function drawNumbered(ctx, frame, cps, complete) {
  const offsets = spotOffsets(cps);
  const radius = Math.max(9, Math.min(13, frame.w / 50));
  ctx.font = `600 ${Math.round(radius * 1.05)}px ${getComputedStyle(document.body).fontFamily}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  cps.forEach((cp, i) => {
    const cx = frame.x + cp.x * frame.w + offsets[i] * (radius * 2 + 9);
    const cy = frame.y + cp.y * frame.h;
    const done = complete ? complete[i] : false;
    ctx.fillStyle = done ? COLOR.pass : COLOR.target;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fill();
    if (cp.tilted ?? cp.skew > 0) {
      // A tilted position: a ring around the number.
      ctx.strokeStyle = ctx.fillStyle;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, radius + 3.5, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.fillStyle = COLOR.field;
    ctx.fillText(String(i + 1), cx, cy + 0.5);
  });
}

function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function animFrame(now) {
  anim.raf = 0;
  if (!animShouldRun()) return;
  if (anim.last) anim.t += now - anim.last;
  anim.last = now;
  drawAnimation();
  anim.raf = requestAnimationFrame(animFrame);
}

function animVisible() {
  return calibrationRoute === "recording" && !recPanels.record.hidden;
}

function animShouldRun() {
  return animVisible() && anim.playing && !prefersReducedMotion() && Boolean(recGuide?.checkpoints?.length);
}

function syncAnimation() {
  const reduced = prefersReducedMotion();
  animToggle.hidden = reduced;
  animToggle.textContent = anim.playing ? "Pause" : "Play";
  animToggle.title = anim.playing ? "Stop the animation at this position" : "Carry on with the animation";
  for (const line of animLegend.querySelectorAll("[data-anim-moving]")) line.hidden = reduced;
  for (const line of animLegend.querySelectorAll("[data-anim-still]")) line.hidden = !reduced;
  setText(animTodo, reduced ? "Blue, numbered: the positions, in the order to hold them" : "Blue: positions still to come");
  // Opened once for reduced motion; after that the operator may close it.
  if (reduced && !anim.listOpened) {
    positionsList.open = true;
    anim.listOpened = true;
  }
  if (animShouldRun() && !anim.raf) {
    anim.last = 0;
    anim.raf = requestAnimationFrame(animFrame);
  } else if (!animShouldRun() && anim.raf) {
    cancelAnimationFrame(anim.raf);
    anim.raf = 0;
  }
  if (animVisible()) drawAnimation();
}

// Show one moment of the animation and stop there (the screenshot script uses it).
function seekAnimation(index, fraction = 0.9) {
  anim.playing = false;
  anim.t = (index + fraction) * ANIM_STEP_MS;
  syncAnimation();
}

animToggle.addEventListener("click", () => {
  anim.playing = !anim.playing;
  syncAnimation();
});
reducedMotion?.addEventListener?.("change", syncAnimation);

// ---- Drop step ----

let mapSignature = "";

function drawRecMap() {
  const s = recStatus || {};
  if (recMapWrap.hidden) return;
  const cps = recGuide?.checkpoints || s.guide?.checkpoints || [];
  const size = s.image_size?.length === 2 ? s.image_size : [4, 3];
  const {ctx, frame} = drawFrame(recMap, size[0] / size[1]);
  for (const point of s.coverage?.points || []) {
    const side = Math.max(point.size || 0, 0.03);
    const box = [
      frame.x + (point.x - side / 2) * frame.w, frame.y + (point.y - side / 2) * frame.h,
      side * frame.w, side * frame.h,
    ];
    ctx.fillStyle = COLOR.inkFill;
    ctx.fillRect(...box);
    ctx.strokeStyle = "rgba(232, 230, 225, 0.35)";
    ctx.lineWidth = 1;
    ctx.strokeRect(...box);
  }
  drawNumbered(ctx, frame, cps, (s.guide?.checkpoints || []).map((c) => c.complete));
}

function drawRecordingCanvases() {
  if (calibrationRoute !== "recording") return;
  if (animVisible()) drawAnimation();
  drawRecMap();
}
new ResizeObserver(drawRecordingCanvases).observe(recAnim);
new ResizeObserver(drawRecordingCanvases).observe(recMap);

function renderProgress(f, s) {
  let label = "";
  let fraction = null;
  let note = "";
  if (recUpload) {
    label = `Copying ${recUpload.name} into the run folder`;
    fraction = recUpload.total ? recUpload.loaded / recUpload.total : null;
    note = `${humanBytes(recUpload.loaded)} of ${humanBytes(recUpload.total)} copied. Keep this page open.`;
    if (recUpload.left) note += ` ${plural(recUpload.left, "more clip")} after this one.`;
  } else if (f.state === "uploading") {
    const upload = s.upload || {};
    label = `Copying ${upload.name || "the clip"} into the run folder`;
    fraction = upload.total_bytes ? upload.received_bytes / upload.total_bytes : null;
    note = upload.total_bytes
      ? `${humanBytes(upload.received_bytes)} of ${humanBytes(upload.total_bytes)} copied.` : "";
  } else if (f.state === "analysing") {
    if (s.stage === "extract") {
      label = "Picking views from the clip";
      fraction = s.progress || 0;
      note = "Reads the clip and keeps sharp, still views of the board at new positions.";
    } else {
      label = "Checking the clip";
      note = "Reads the clip's settings and compares them with the camera setup.";
    }
  } else if (f.state === "solving") {
    label = "Solving";
    note = "Computes the lens calibration from the kept views. This can take a minute.";
  }
  recProgress.hidden = !label;
  if (!label) return;
  setText(recProgressLabel, label);
  setText(recProgressPct, fraction == null ? "" : percent(fraction));
  setText(recProgressNote, note);
  recProgressFill.style.width = fraction == null ? "100%" : `${Math.round(fraction * 100)}%`;
  recProgressFill.style.opacity = fraction == null ? "0.35" : "1";
  if (fraction == null) recProgressBar.removeAttribute("aria-valuenow");
  else recProgressBar.setAttribute("aria-valuenow", String(Math.round(fraction * 100)));
}

function countsText(s) {
  const counts = s.counts || {};
  if (!counts.samples) return "";
  const rate = recordingConfig()?.sample_hz;
  const left = Object.entries(DROP_WORDS)
    .filter(([key]) => counts[key])
    .map(([key, words]) => `${counts[key]} ${words}`);
  const read = `Read ${plural(counts.samples, "frame")}${rate ? ` (${rate} a second)` : ""}`;
  return `${read} and kept ${plural(counts.kept || 0, "view")}.`
    + (left.length ? ` Left out: ${listText(left)}.` : "");
}

let clipsSignature = "";

function renderClips(s) {
  const clips = s.clips || [];
  const signature = JSON.stringify(clips);
  if (signature === clipsSignature) return;
  clipsSignature = signature;
  recClips.replaceChildren(...clips.map((clip) => {
    const box = document.createElement("div");
    const row = document.createElement("div");
    row.className = "clip-row";
    row.append(Object.assign(document.createElement("span"), {className: "clip-name", textContent: clip.name}));
    const facts = [humanBytes(clip.size_bytes)];
    if (clip.counts) facts.push(`${plural(clip.counts.kept || 0, "view")} kept`);
    row.append(Object.assign(document.createElement("span"), {className: "note", textContent: facts.join(" · ")}));
    if (clip.mismatch_count) {
      row.append(Object.assign(document.createElement("span"), {
        className: "clip-flag",
        textContent: `${clip.mismatch_count} DIFFER${clip.mismatch_count === 1 ? "S" : ""}`,
      }));
    }
    const details = document.createElement("details");
    details.className = "disclosure";
    details.append(Object.assign(document.createElement("summary"), {textContent: "What the clip check read"}));
    const body = document.createElement("div");
    body.className = "disclosure-body";
    const table = document.createElement("table");
    table.className = "clip-table";
    const head = document.createElement("tr");
    for (const text of ["Setting", "Expected", "In the file", ""]) {
      head.append(Object.assign(document.createElement("th"), {scope: "col", textContent: text}));
    }
    table.append(head);
    for (const check of clip.check || []) {
      const tr = document.createElement("tr");
      const status = {ok: "ok", mismatch: "differs", unknown: "not in the file"}[check.status] || check.status;
      for (const [text, cls] of [
        [check.label, ""], [check.expected ?? "", ""], [check.found ?? "", ""], [status, check.status],
      ]) {
        tr.append(Object.assign(document.createElement("td"), {textContent: text, className: cls}));
      }
      table.append(tr);
    }
    body.append(table);
    details.append(body);
    box.append(row, details);
    return box;
  }));
}

function renderDrop(f, s) {
  const disabled = f.busy || f.step !== "drop";
  if (disabled) dropZone.dataset.disabled = "";
  else delete dropZone.dataset.disabled;
  // While a clip is copied or processed, the progress bar takes its place.
  dropZone.hidden = f.busy;
  if (f.clips.length) dropZone.dataset.compact = "";
  else delete dropZone.dataset.compact;
  const redo = f.warning && f.canRedo;
  setText(dropZoneTitle, f.busy ? "Wait for this clip to finish"
    : redo ? "Add a clip to this run (not a re-recorded one)"
      : f.clips.length ? "Drop another clip of this camera here" : "Drop the clip from the camera's card here");
  setText(dropZoneNote, redo
    ? "A clip dropped here is solved together with the one above. For a clip recorded with the settings fixed, click Start this camera again first."
    : f.clips.length
    ? "Its views are added to this run and everything is solved again. A clip from another camera starts its own run."
    : "An .mp4 file (GX01xxxx.MP4, for example GX010042.MP4), or several. A 90 s clip is about 1.4 GB and takes a moment to copy.");
  renderProgress(f, s);
  // What went wrong, in the server's words: a refused clip first, then any other problem.
  // Too few views is not a failure: the prompt and the stats say what to add.
  const alert = f.refused?.message || (!f.busy && !f.needsMore && f.error) || "";
  recAlert.hidden = !alert;
  setText(recAlertText, alert);
  const switchNote = s.camera_switch?.message
    || (s.previous_run_id ? `This run is for another camera than the one before. The previous camera's calibration is in ${s.previous_run_id}.` : "");
  recSwitchNote.hidden = !switchNote || alert.startsWith(switchNote);
  setText(recSwitchNote, switchNote);
  // Once the calibration passes, QR code 2 again: the dataset needs the shutter on Auto.
  // Not while the calibration clip is still to be recorded again.
  recReminder.hidden = !f.passed || Boolean(f.warning);
  qrDatasetSmall.hidden = !recLabs;
  recNotesList.hidden = recNotes.length === 0;
  const notesSig = recNotes.join("\n");
  if (recNotesList.dataset.signature !== notesSig) {
    recNotesList.dataset.signature = notesSig;
    recNotesList.replaceChildren(...recNotes.map((text) => Object.assign(document.createElement("li"), {textContent: text})));
  }
  // The camera this run is for, named from its serial where the clip carries one.
  // Before the first clip is read, a default name is only a placeholder.
  const renamed = !f.clips.length && !s.serial && !s.camera_named_from_serial
    && shippedCameraNames.has(s.camera_name);
  let cameraText = "";
  if (s.run_id && s.camera_name) {
    cameraText = s.camera_name_note || (renamed
      ? `Camera: ${s.camera_name} for now. The run is named after the camera's serial number `
        + "once the clip is read."
      : `Camera: ${s.camera_name}. Its files are named after it.`);
  }
  recCamera.hidden = !cameraText;
  if (recCamera.dataset.text !== cameraText) {
    recCamera.dataset.text = cameraText;
    const name = s.camera_name || "";
    const at = cameraText.indexOf(name);
    recCamera.replaceChildren();
    if (name && at >= 0) {
      recCamera.append(cameraText.slice(0, at), Object.assign(document.createElement("strong"), {textContent: name}),
        cameraText.slice(at + name.length));
    } else {
      recCamera.textContent = cameraText;
    }
  }
  const hasViews = (s.captures || 0) > 0 || f.clips.length > 0;
  recStats.hidden = !hasViews || (f.busy && !s.captures);
  const need = s.min_frames || Number(form.elements["solver.min_frames"].value || 25);
  setText(document.getElementById("recViews"), String(s.captures || 0));
  setText(document.getElementById("recViewsNeed"), `· need ${need}`);
  setText(document.getElementById("recPositions"), `${s.guide?.complete_count || 0} / ${s.guide?.total_count || 0}`);
  setText(document.getElementById("recSpread"), percent(s.coverage?.overall));
  setText(document.getElementById("recClipCount"), String(f.clips.length));
  const counts = countsText(s);
  recCounts.hidden = !counts || f.busy;
  setText(recCounts, counts);
  // The retake list: the positions the next clip must add.
  const missing = hasViews && !f.busy && !f.passed ? missingPositions(s) : [];
  recMissing.hidden = missing.length === 0;
  setText(recMissingHead, `Missing positions (${missing.length} of ${s.guide?.total_count || 0}), `
    + "numbered as in the animation:");
  const missingSig = JSON.stringify(missing);
  if (recMissingList.dataset.signature !== missingSig) {
    recMissingList.dataset.signature = missingSig;
    recMissingList.replaceChildren(...missing.map((m) => {
      const li = Object.assign(document.createElement("li"), {textContent: m.caption, value: m.number});
      return li;
    }));
  }
  recMapWrap.hidden = !hasViews || f.busy;
  setText(recMapTodo, f.passed ? "Blue: not covered (not needed, the result passed)" : "Blue: still missing");
  const signature = JSON.stringify([s.coverage?.points, s.guide?.checkpoints, s.image_size, recMapWrap.hidden, recGuideKey]);
  if (signature !== mapSignature) {
    mapSignature = signature;
    drawRecMap();
  }
  renderClips(s);
}

function renderClipCheck(f, s) {
  const show = !f.busy && f.clips.length > 0;
  clipCheck.hidden = !show;
  // The banner sits in the drop step, outside #clipCheck: it is cleared here too, so a
  // fresh run (or a retake in work) never shows the last one's warning.
  const warning = show ? f.warning : null;
  const text = warning ? `${warning.first} ${warning.rest}` : "";
  clipBanner.hidden = !warning;
  if (clipBanner.dataset.text !== text) {
    clipBanner.dataset.text = text;
    clipBanner.replaceChildren();
    if (warning) {
      clipBanner.append(Object.assign(document.createElement("strong"), {textContent: warning.first}),
        ` ${warning.rest}`);
    }
  }
  if (!show) return;
  const advice = clipAdvice(s);
  clipUnknown.hidden = advice.unknown.length === 0;
  clipOther.hidden = advice.other.length === 0;
  for (const [list, lines] of [[clipUnknownList, advice.unknown], [clipOtherList, advice.other]]) {
    const signature = lines.join("\n");
    if (list.dataset.signature === signature) continue;
    list.dataset.signature = signature;
    list.replaceChildren(...lines.map((line) => Object.assign(document.createElement("li"), {textContent: line})));
  }
}

// ---- Render ----

// A local error stands until the job moves on: another state, or another run.
function recJobKey(s) {
  return `${s?.state || "idle"}|${s?.run_id || ""}`;
}

function renderRecording(status) {
  if (status) recStatus = status;
  if (recLocalError && recLocalErrorKey !== null && recJobKey(recStatus) !== recLocalErrorKey) {
    recLocalError = "";
    recLocalErrorKey = null;
  }
  renderRouteChoice();
  if (calibrationRoute !== "recording") return;
  const s = recStatus || {};
  const f = recFacts(s);
  const states = recStepStates(f);
  renderSteps(states);
  presetSelect.classList.toggle("select-primary", states.setup === "current");
  renderRecButtons(f, states);
  const view = currentRecView(f);
  for (const [name, panel] of Object.entries(recPanels)) panel.hidden = name !== view;
  recBack.hidden = view === (f.step === "setup" ? "settings" : f.step);
  recBackBtn.textContent = f.step === "drop" ? "Back to Drop clip & solve"
    : f.step === "record" ? "Back to Record" : "Back";
  setStatusLine(...recStatusLine(f, s));
  setText(recPrompt, recPromptText(f, s));
  recHint.hidden = !recHintText;
  setText(recHint, recHintText);
  renderLabs(f);
  syncAnimation();
  renderDrop(f, s);
  renderClipCheck(f, s);
  renderRecReadout(s);
  results.hidden = true;
  if (f.hasResults) renderResults(f.results, s.coverage, s.acquisition_mode, f);
  else {
    resultsPanel.hidden = true;
    resultsSignature = "";
  }
}

// ---- Polling and actions ----

async function recPoll() {
  try {
    renderRecording(await api("/api/recording/status"));
  } catch {
    if (calibrationRoute === "recording") {
      setStatusLine("Problem: the server is not reachable. Is it still running?");
    }
  }
  recSyncPolling();
}

// Poll while the job is working on a clip; nothing changes on the server otherwise.
// While recWaitIdle polls for this client's own clip, the interval stands down.
let recWaiting = false;

function recSyncPolling() {
  const busy = !recWaiting && REC_BUSY.has(recStatus?.state);
  if (busy && !recPollTimer) recPollTimer = setInterval(recPoll, 400);
  if (!busy && recPollTimer) {
    clearInterval(recPollTimer);
    recPollTimer = null;
  }
}

function recWaitIdle() {
  recWaiting = true;
  recSyncPolling();
  return new Promise((resolve) => {
    const check = async () => {
      await recPoll();
      if (REC_BUSY.has(recStatus?.state)) {
        setTimeout(check, 400);
        return;
      }
      recWaiting = false;
      recSyncPolling();
      resolve(recStatus);
    };
    check();
  });
}

// The clip is the raw request body (no form encoding): a 1.4 GB file streams to disk.
function uploadClip(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/recording/clips?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (!recUpload) return;
      recUpload.loaded = event.loaded;
      recUpload.total = event.lengthComputable ? event.total : file.size;
      renderRecording();
    };
    xhr.onload = () => {
      let body = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // not JSON: keep the status text
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body);
      else {
        const detail = body?.detail;
        reject(new Error(typeof detail === "string" ? detail : xhr.statusText || `HTTP ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error("The upload stopped: the server is not reachable. Drop the clip again."));
    xhr.send(file);
  });
}

async function recAddFiles(fileList) {
  const files = [...(fileList || [])];
  if (!files.length) return;
  const clips = files.filter((file) => /\.mp4$/i.test(file.name));
  recNotes = files
    .filter((file) => !clips.includes(file))
    .map((file) => `${file.name} was left out: only .mp4 files from the camera's card can be used.`);
  recLocalError = "";
  recLocalErrorKey = null;
  recHintText = "";
  recView = null;
  if (!clips.length) {
    renderRecording();
    return;
  }
  // The banner asks for the clip to be recorded again in a fresh run: adding the new
  // clip here would solve it together with the flagged one.
  const f = recFacts(recStatus || {});
  if (f.warning && f.canRedo) {
    const why = f.settingsDiffer
      ? "A clip in this run was not recorded with the preset's settings."
      : "A clip in this run could not be read cleanly.";
    const add = clips.length === 1 ? "this clip" : "these clips";
    if (!confirm(`${why} Add ${add} to the run anyway? To solve a re-recorded clip on its own, `
      + "click Cancel, then Start this camera again.")) {
      recHintText = `Not added: ${listText(clips.map((file) => file.name))}. To solve a `
        + "re-recorded clip on its own, click Start this camera again, then drop it in step 4.";
      renderRecording();
      return;
    }
  }
  recQueue.push(...clips);
  if (!recRunning) await recRunQueue();
  else renderRecording();
}

// One clip at a time: the job refuses a clip while it works on the previous one.
let recRunning = false;

async function recRunQueue() {
  recRunning = true;
  // Nothing polls while idle: a server restart or another client's Next camera may have
  // ended the run this page last saw.
  await recPoll();
  if (REC_BUSY.has(recStatus?.state)) await recWaitIdle();
  while (recQueue.length) {
    const file = recQueue.shift();
    try {
      if (!recStatus?.run_id) {
        renderRecording(await api("/api/recording/start", {method: "POST", body: JSON.stringify({config: readForm()})}));
      }
      recUpload = {name: file.name, loaded: 0, total: file.size, left: recQueue.length};
      renderRecording();
      const status = await uploadClip(file);
      recUpload = null;
      renderRecording(status);
      const done = await recWaitIdle();
      if (recQueue.length && done?.refused_clip) recNotes.push(done.refused_clip.message);
    } catch (err) {
      recUpload = null;
      recLocalError = err.message || String(err);
      recLocalErrorKey = null;
      if (recQueue.length) {
        recNotes.push(`Not copied: ${listText(recQueue.map((f) => f.name))}. Drop ${recQueue.length === 1 ? "it" : "them"} again.`);
      }
      recQueue = [];
      await recPoll();
      // Stale from the job's next change on (another client's clip finishing, say).
      if (recLocalError) recLocalErrorKey = recJobKey(recStatus);
    }
  }
  recRunning = false;
  renderRecording();
}

function recChoose() {
  recFile.value = "";
  recFile.click();
}

recFile.addEventListener("change", () => recAddFiles(recFile.files));
recChooseBtn.addEventListener("click", recChoose);
dropZone.addEventListener("click", () => {
  if (!recChooseBtn.disabled) recChoose();
});
dropZone.addEventListener("dragover", (event) => {
  event.preventDefault();
  if (dropZone.dataset.disabled == null) dropZone.dataset.over = "";
});
dropZone.addEventListener("dragleave", () => delete dropZone.dataset.over);
dropZone.addEventListener("drop", (event) => {
  event.preventDefault();
  delete dropZone.dataset.over;
  recDropAnywhere(event.dataTransfer?.files);
});

// A file dropped anywhere on the page counts as dropped on the zone in step 4; before
// that, or while a clip is busy, the page says why it was not taken.
function recDropAnywhere(files) {
  if (!files?.length) return;
  const f = recFacts(recStatus || {});
  let hint = "";
  if (!f.supported) hint = "That file was not used: pick a camera setup with recording settings in step 1 first.";
  else if (f.busy) hint = "That file was not used: wait for this clip to finish, then drop the next one.";
  else if (f.step !== "drop") hint = "That file was not used: finish steps 2 and 3 first, then drop the clip in step 4.";
  if (hint) {
    recHintText = hint;
    renderRecording();
    return;
  }
  recAddFiles(files);
}

// A clip dropped next to the drop zone must not make the browser open the video.
for (const type of ["dragover", "drop"]) {
  window.addEventListener(type, (event) => {
    if (calibrationRoute !== "recording" || dropZone.contains(event.target)) return;
    event.preventDefault();
    if (type === "drop") recDropAnywhere(event.dataTransfer?.files);
  });
}
window.addEventListener("beforeunload", (event) => {
  if (recUpload) event.preventDefault(); // leaving would cut the copy short
});

recSettingsBtn.addEventListener("click", () => {
  recConfirmed.settings = true;
  recView = null;
  recHintText = "";
  renderRecording();
});
recRecordedBtn.addEventListener("click", () => {
  recConfirmed.recorded = true;
  recView = null;
  recHintText = "";
  renderRecording();
});
recShowSettingsBtn.addEventListener("click", () => {
  recView = "settings";
  renderRecording();
});
recShowRecordBtn.addEventListener("click", () => {
  recView = "record";
  renderRecording();
});
recBackBtn.addEventListener("click", () => {
  recView = null;
  renderRecording();
});

// Next camera and Start this camera again both end this run (its files stay) and
// start a fresh one at step 2; only the words differ.
async function recNewRun(button) {
  const s = recStatus || {};
  const f = recFacts(s);
  if (!f.solved && f.clips.length && !confirm(
    `This camera's run is not solved yet. Its clips stay in ${s.run_dir || "its run folder"}, `
    + `but ${button} ends the run, so no more clips can be added to it. Continue?`,
  )) return;
  recConfirmed.settings = false;
  recConfirmed.recorded = false;
  recView = null;
  recNotes = [];
  recLocalError = "";
  recLocalErrorKey = null;
  recHintText = "";
  recRestartedFrom = button === "Next camera" ? "" : s.run_dir || "";
  renderRecording(await api("/api/recording/new", {method: "POST", body: JSON.stringify({config: readForm()})}));
}

recNextBtn.addEventListener("click", act("Next camera", () => recNewRun("Next camera")));
recRedoBtn.addEventListener("click", act("Start this camera again", () => recNewRun("Start this camera again")));

function setRoute(value, {remember = true} = {}) {
  calibrationRoute = value === "recording" ? "recording" : "live";
  if (remember) saveRoute(calibrationRoute);
  appEl.dataset.route = calibrationRoute;
  for (const radio of routeRadios) radio.checked = radio.value === calibrationRoute;
  // Each route marks only its own steps.
  const own = calibrationRoute === "recording"
    ? ["setup", "settings", "record", "drop"] : ["setup", "connect", "capture", "solve"];
  for (const [name, step] of Object.entries(steps)) {
    if (!own.includes(name)) {
      step.removeAttribute("aria-current");
      step.dataset.stepState = "pending";
    }
  }
  delete cameraReadout.dataset.signature;
  resultsSignature = "";
  if (calibrationRoute === "recording") {
    refreshRecordingConfig();
    renderRecording();
    recSyncPolling();
  } else {
    syncAnimation();
    // No live status yet means the last poll failed: ask again, and let it say so.
    if (lastStatus) updateStatus(lastStatus);
    else poll();
    renderRouteChoice();
  }
}

for (const radio of routeRadios) {
  radio.addEventListener("change", () => {
    if (radio.checked) setRoute(radio.value);
  });
}

async function init() {
  defaults = await api("/api/defaults");
  populateForm(defaults.config, defaults.aruco_dictionaries, defaults.gopro_options);
  await loadPresetList();
  showPresetChoice(matchingPreset(defaults.config));
  if (!presetChoice) {
    presetMsg.textContent =
      "These settings match no camera setup. Pick one here, or keep them and click Open preview.";
  }
  setStatusLine("Ready.");
  drawCoverageMap([], null);
  await poll();
  if (lastStatus && isLive(lastStatus)) startPolling();
  await recPoll();
  // A session that is already running decides the route, whatever was remembered.
  let start = calibrationRoute;
  if (lastStatus && isLive(lastStatus)) start = "live";
  else if (recFacts(recStatus || {}).busy) start = "recording";
  setRoute(start, {remember: false});
}

init().catch((err) => {
  setStatusLine(`Problem: could not load the app settings: ${err.message || err}. Is the server running?`);
});
