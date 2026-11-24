"use strict";

const $ = (id) => document.getElementById(id);
let csrf = "";
let datasets = [];
let selected = null;
let history = {};
let settings = {};
let schema = false;
let busy = false;

function message(text, error = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", error);
}

async function api(path, options = {}) {
  const headers = { "X-CSRF-Token": csrf, ...options.headers };
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 204) return null;
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "The request failed.");
  return result;
}

function table(value) {
  const element = document.createElement("table");
  const head = element.createTHead().insertRow();
  for (const column of value.columns) {
    const cell = document.createElement("th");
    cell.textContent = String(column);
    head.append(cell);
  }
  const body = element.createTBody();
  for (const row of value.data) {
    const tr = body.insertRow();
    for (const value of row) {
      const cell = tr.insertCell();
      cell.textContent = value === null ? "\u2014" : String(value);
      if (value === null) cell.classList.add("null-value");
    }
  }
  return element;
}

function renderSettings() {
  $("endpoint").value = settings.endpoint;
  $("provider").value = settings.provider;
  $("api-key").value = "";
  $("key-state").textContent = settings.has_key ? "(key saved in server memory)" : "(not set)";
  const models = settings.models || [];
  $("model").replaceChildren();
  if (!models.length) $("model").append(new Option("Discover models first", ""));
  for (const model of models) $("model").append(new Option(model, model));
  $("model").value = settings.model || "";
  $("model").disabled = !models.length;
  $("verify").disabled = !models.length;
  $("connection-dot").classList.toggle("connected", settings.verified);
  $("model-label").textContent = settings.verified ? settings.model : "Connect your AI model";
}

function renderTable() {
  if (!selected) return;
  const data = schema ? {
    columns: ["Column", "Data type", "Missing values"],
    data: selected.fields.map((field) => [field.name, field.dtype, field.missing]),
  } : selected.preview;
  $("table-container").replaceChildren(table(data));
  $("preview-tab").setAttribute("aria-selected", String(!schema));
  $("schema-tab").setAttribute("aria-selected", String(schema));
  $("preview-caption").textContent = schema ? `${selected.columns} fields` : `First ${selected.preview.data.length} rows`;
  $("table-note").textContent = schema ? "Detected by pandas" : `${selected.rows.toLocaleString()} rows total`;
}

function renderChat() {
  const messages = selected ? history[selected.id] || [] : [];
  $("chat-messages").replaceChildren();
  $("chat-intro").hidden = messages.length > 0;
  for (const item of messages) {
    const bubble = document.createElement("article");
    bubble.className = `chat-message ${item.role}`;
    const label = document.createElement("strong");
    label.textContent = item.role === "user" ? "You" : "Data assistant";
    bubble.append(label);
    if (item.type === "table") {
      const wrapper = document.createElement("div");
      wrapper.className = "chat-table";
      wrapper.append(table(item.value));
      bubble.append(wrapper);
      const caption = document.createElement("small");
      caption.textContent = `Showing ${item.value.data.length} of ${item.rows} rows`;
      bubble.append(caption);
    } else if (item.type === "image") {
      const image = document.createElement("img");
      image.src = `data:image/png;base64,${item.value}`;
      image.alt = "AI-generated analysis chart";
      bubble.append(image);
    } else {
      const text = document.createElement("p");
      text.textContent = item.value;
      bubble.append(text);
    }
    $("chat-messages").append(bubble);
  }
  $("chat-messages").scrollTop = $("chat-messages").scrollHeight;
}

function render() {
  $("file-count").textContent = datasets.length;
  $("datasets").replaceChildren();
  for (const dataset of datasets) {
    const button = document.createElement("button");
    button.className = `dataset-item${selected?.id === dataset.id ? " active" : ""}`;
    const name = document.createElement("strong");
    name.textContent = dataset.name;
    const detail = document.createElement("small");
    detail.textContent = `${dataset.rows.toLocaleString()} rows \u00b7 ${dataset.columns} columns`;
    button.append(name, detail);
    button.addEventListener("click", () => { if (!busy) { selected = dataset; render(); } });
    $("datasets").append(button);
  }
  $("empty-state").hidden = Boolean(selected);
  $("data-view").hidden = !selected;
  $("remove-dataset").hidden = !selected;
  $("dataset-title").textContent = selected ? selected.name : "Make sense of your data.";
  $("dataset-subtitle").textContent = selected ? "Your data, ready to explore." : "Start with a spreadsheet. Leave with an insight.";
  $("chat-context").textContent = selected ? selected.name : "No dataset selected";
  $("send").disabled = busy || !selected || !settings.verified;
  $("question").disabled = busy;
  $("remove-dataset").disabled = busy;
  if (selected) {
    for (const key of ["rows", "columns", "missing", "duplicates"]) {
      $(`metric-${key}`).textContent = selected[key].toLocaleString();
    }
    renderTable();
  }
  renderChat();
}

async function upload(file) {
  if (!file || busy) return;
  busy = true;
  render();
  message(`Reading ${file.name}...`);
  const body = new FormData();
  body.append("file", file);
  try {
    const result = await api("/api/upload", { method: "POST", body });
    datasets.push(...result.datasets);
    selected = result.datasets[0];
    message(`Added ${result.datasets.length} dataset${result.datasets.length === 1 ? "" : "s"}.`);
  } catch (error) {
    message(error.message, true);
  } finally {
    busy = false;
    $("file-input").value = "";
    render();
  }
}

$("file-input").addEventListener("change", (event) => upload(event.target.files[0]));
$("empty-upload").addEventListener("click", () => $("file-input").click());
$("drop-zone").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("file-input").click(); }
});
for (const name of ["dragover", "dragleave", "drop"]) {
  $("drop-zone").addEventListener(name, (event) => {
    event.preventDefault();
    $("drop-zone").classList.toggle("dragging", name === "dragover");
    if (name === "drop") upload(event.dataTransfer.files[0]);
  });
}
$("preview-tab").addEventListener("click", () => { schema = false; renderTable(); });
$("schema-tab").addEventListener("click", () => { schema = true; renderTable(); });
$("remove-dataset").addEventListener("click", async () => {
  if (!selected || busy) return;
  try {
    await api(`/api/datasets/${selected.id}`, { method: "DELETE" });
    datasets = datasets.filter((item) => item.id !== selected.id);
    delete history[selected.id];
    selected = datasets[0] || null;
    render();
    message("Dataset removed.");
  } catch (error) { message(error.message, true); }
});

$("settings-open").addEventListener("click", () => {
  renderSettings();
  $("settings-status").textContent = settings.verified ? "Model verified and ready." : "";
  $("settings-dialog").showModal();
});
$("settings-close").addEventListener("click", () => $("settings-dialog").close());
$("settings-dialog").addEventListener("close", () => { $("api-key").value = ""; });
$("provider").addEventListener("change", () => {
  $("endpoint").value = $("provider").value === "local" ? "http://host.docker.internal:1234/v1" : "https://api.openai.com/v1";
  $("api-key").value = "";
  invalidateModels();
});
function invalidateModels() {
  $("model").disabled = true;
  $("verify").disabled = true;
  $("settings-status").textContent = "List models again to use these settings.";
}
$("endpoint").addEventListener("input", invalidateModels);
$("api-key").addEventListener("input", invalidateModels);

async function connectionAction(button, action) {
  button.disabled = true;
  $("settings-status").classList.remove("error");
  $("settings-status").textContent = "Connecting...";
  try {
    const result = await action();
    settings = result.settings;
    renderSettings();
    render();
    return true;
  } catch (error) {
    $("settings-status").textContent = error.message;
    $("settings-status").classList.add("error");
    return false;
  } finally {
    button.disabled = button.id === "verify" && $("model").disabled;
  }
}
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = $("api-key").value;
  $("api-key").value = "";
  if (await connectionAction($("discover"), () => api("/api/connection/discover", {
    method: "POST", body: { endpoint: $("endpoint").value, api_key: key, provider: $("provider").value },
  }))) $("settings-status").textContent = "Models discovered. Select a model, then verify it.";
});
$("verify").addEventListener("click", async () => {
  if (await connectionAction($("verify"), () => api("/api/connection/verify", {
    method: "POST", body: { model: $("model").value },
  }))) $("settings-status").textContent = "Connection verified. Your model is ready.";
});
$("forget-key").addEventListener("click", async () => {
  if (await connectionAction($("forget-key"), () => api("/api/connection", { method: "DELETE" }))) {
    $("settings-status").textContent = "Connection and in-memory key removed from this workspace.";
  }
});
$("model").addEventListener("change", () => {
  $("settings-status").textContent = "Verify this model to use it for analysis.";
});

for (const button of document.querySelectorAll("[data-question]")) {
  button.addEventListener("click", () => { $("question").value = button.dataset.question; $("question").focus(); });
}
$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$("send").disabled) $("chat-form").requestSubmit();
  }
});
$("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || !selected || !settings.verified) return;
  const question = $("question").value.trim();
  if (!question) return;
  const datasetId = selected.id;
  history[datasetId] ||= [];
  history[datasetId].push({ role: "user", type: "text", value: question });
  busy = true;
  render();
  message("Analyzing your data...");
  try {
    const result = await api("/api/chat", { method: "POST", body: { dataset_id: datasetId, question } });
    history[datasetId].push({ role: "assistant", ...result.answer });
    history[datasetId] = history[datasetId].slice(-20);
    $("question").value = "";
    message("");
  } catch (error) {
    history[datasetId].pop();
    message(error.message, true);
  } finally {
    busy = false;
    render();
  }
});

async function init() {
  try {
    const state = await api("/api/state");
    csrf = state.csrf;
    datasets = state.datasets;
    history = state.history;
    settings = state.settings;
    selected = datasets[0] || null;
    renderSettings();
    render();
  } catch (error) { message(error.message, true); }
}
init();
