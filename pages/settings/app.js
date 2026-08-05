const bridge = window.AstrBotPluginPage;

const DEFAULT_SETTINGS = Object.freeze({
  server_name: "Minecraft Server",
  host: "127.0.0.1",
  port: 25565,
  protocol_version: 760,
  send_latency_ping: false,
  query_interval_seconds: 300,
  max_parallel_queries: 4,
  render_cache_seconds: 45,
  sample_reuse_seconds: 30,
  background_image_url: "https://api.imlazy.ink/img",
  background_opacity: 0.46,
  background_overlay_opacity: 0.54,
  background_cache_seconds: 3600,
  background_fetch_timeout_seconds: 5,
  background_max_bytes: 8 * 1024 * 1024,
  enable_group_whitelist: false,
  group_whitelist: "",
  allow_private_chat: true,
  use_default_server_for_unconfigured_groups: true,
  timeout_seconds: 3,
  chart_hours: 24,
  retention_days: 30,
  max_chart_points: 180,
  group_servers: [],
});

const STRING_FIELDS = Object.freeze([
  "server_name",
  "host",
  "background_image_url",
  "group_whitelist",
]);

const INTEGER_FIELDS = Object.freeze([
  "port",
  "protocol_version",
  "query_interval_seconds",
  "max_parallel_queries",
  "render_cache_seconds",
  "sample_reuse_seconds",
  "background_cache_seconds",
  "background_max_bytes",
  "chart_hours",
  "retention_days",
  "max_chart_points",
]);

const FLOAT_FIELDS = Object.freeze([
  "background_opacity",
  "background_overlay_opacity",
  "background_fetch_timeout_seconds",
  "timeout_seconds",
]);

const BOOLEAN_FIELDS = Object.freeze([
  "send_latency_ping",
  "enable_group_whitelist",
  "allow_private_chat",
  "use_default_server_for_unconfigured_groups",
]);

const form = document.getElementById("settings-form");
const settingsFields = document.getElementById("settings-fields");
const saveButton = document.getElementById("save-settings");
const reloadButton = document.getElementById("reload-settings");
const addGroupButton = document.getElementById("add-group-server");
const groupTable = document.querySelector(".group-table");
const groupRows = document.getElementById("group-server-rows");
const groupEmpty = document.getElementById("group-server-empty");
const groupTemplate = document.getElementById("group-server-row-template");
const connectionStatus = document.getElementById("connection-status");
const connectionStatusText = document.getElementById("connection-status-text");
const saveStatus = document.getElementById("save-status");

let groupServers = [];
let ready = false;
let busy = false;
let unsubscribeContext = null;

function translate(key, fallback, variables = {}) {
  let translated = fallback;
  if (bridge && typeof bridge.t === "function") {
    translated = bridge.t(`pages.settings.${key}`, fallback);
  }
  return Object.entries(variables).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
    String(translated),
  );
}

function rememberFallback(element, attribute, fallback) {
  const storageName = `fallback${attribute}`;
  if (!element.dataset[storageName]) {
    element.dataset[storageName] = fallback;
  }
  return element.dataset[storageName];
}

function applyTranslations(context = null) {
  const locale = context?.locale || bridge?.getLocale?.();
  if (typeof locale === "string" && locale.trim()) {
    document.documentElement.lang = locale;
  }

  document.querySelectorAll("[data-i18n]").forEach((element) => {
    const key = element.dataset.i18n;
    const fallback = rememberFallback(element, "Text", element.textContent.trim());
    element.textContent = translate(key, fallback);
  });

  document.querySelectorAll("[data-i18n-aria]").forEach((element) => {
    const key = element.dataset.i18nAria;
    const fallback = rememberFallback(
      element,
      "Aria",
      element.getAttribute("aria-label") || "",
    );
    element.setAttribute("aria-label", translate(key, fallback));
  });

  document.title = translate("documentTitle", "SimpMC-Motd settings");
  refreshGroupTranslations();
}

function setConnectionState(state, message) {
  connectionStatus.dataset.state = state;
  connectionStatusText.textContent = message;
}

function setSaveState(state, message) {
  saveStatus.dataset.state = state;
  saveStatus.textContent = message;
  saveStatus.setAttribute("role", state === "error" ? "alert" : "status");
}

function setBusyState(isBusy, operation = "") {
  busy = isBusy;
  form.setAttribute("aria-busy", String(isBusy));
  settingsFields.disabled = isBusy || !ready;
  reloadButton.disabled = isBusy;
  saveButton.disabled = isBusy || !ready;
  saveButton.dataset.busy = String(isBusy && operation === "save");
}

function asString(value, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value, fallback) {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function asBoolean(value, fallback) {
  return typeof value === "boolean" ? value : fallback;
}

function normalizeGroupServers(value) {
  if (!Array.isArray(value)) {
    return [];
  }
  return value
    .filter((entry) => entry && typeof entry === "object" && !Array.isArray(entry))
    .map((entry) => ({
      scope: asString(entry.scope).trim(),
      address: asString(entry.address).trim(),
      name: asString(entry.name).trim(),
    }));
}

function normalizeSettings(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(translate("errors.invalidSettings", "服务器返回了无效的设置数据。"));
  }

  const normalized = {};
  for (const key of STRING_FIELDS) {
    if (key === "group_whitelist" && Array.isArray(value[key])) {
      normalized[key] = value[key].map(String).join("\n");
    } else {
      normalized[key] = asString(value[key], DEFAULT_SETTINGS[key]);
    }
  }
  for (const key of INTEGER_FIELDS) {
    normalized[key] = Math.trunc(asNumber(value[key], DEFAULT_SETTINGS[key]));
  }
  for (const key of FLOAT_FIELDS) {
    normalized[key] = asNumber(value[key], DEFAULT_SETTINGS[key]);
  }
  for (const key of BOOLEAN_FIELDS) {
    normalized[key] = asBoolean(value[key], DEFAULT_SETTINGS[key]);
  }
  normalized.group_servers = normalizeGroupServers(value.group_servers);
  return normalized;
}

function extractPayload(response) {
  if (!response || typeof response !== "object" || Array.isArray(response)) {
    throw new Error(translate("errors.invalidResponse", "服务器返回了无法识别的响应。"));
  }

  const envelope =
    response.data && typeof response.data === "object" && !Array.isArray(response.data)
      ? response.data
      : response;

  if (envelope.status && envelope.status !== "ok") {
    throw new Error(
      asString(envelope.message, translate("errors.requestFailed", "请求未能完成。")),
    );
  }
  return envelope;
}

function extractSettings(response) {
  return normalizeSettings(extractPayload(response).settings);
}

function populateForm(settings) {
  for (const key of STRING_FIELDS) {
    const input = document.getElementById(key);
    input.value = settings[key];
  }
  for (const key of [...INTEGER_FIELDS, ...FLOAT_FIELDS]) {
    const input = document.getElementById(key);
    input.value = String(settings[key]);
  }
  for (const key of BOOLEAN_FIELDS) {
    const input = document.getElementById(key);
    input.checked = settings[key];
  }

  groupServers = settings.group_servers.map((entry) => ({ ...entry }));
  renderGroupServers();
}

function updateGroupEntry(index, key, value) {
  if (!groupServers[index]) {
    return;
  }
  groupServers[index][key] = value;
  markDirty();
}

function configureGroupInput(input, { id, value, placeholder, label, onInput }) {
  input.id = id;
  input.value = value;
  input.placeholder = placeholder;
  input.setAttribute("aria-label", label);
  const labelElement = input.previousElementSibling;
  labelElement.htmlFor = id;
  labelElement.textContent = label;
  input.addEventListener("input", onInput);
}

function renderGroupServers(focusIndex = null) {
  groupRows.replaceChildren();
  const isEmpty = groupServers.length === 0;
  groupTable.hidden = isEmpty;
  groupEmpty.hidden = !isEmpty;

  const labels = {
    scope: translate("groups.scope", "会话范围"),
    address: translate("groups.address", "服务器地址"),
    name: translate("groups.name", "显示名称"),
    actions: translate("groups.actions", "操作"),
  };

  groupServers.forEach((entry, index) => {
    const fragment = groupTemplate.content.cloneNode(true);
    const row = fragment.querySelector("tr");
    const scopeInput = fragment.querySelector(".group-scope");
    const addressInput = fragment.querySelector(".group-address");
    const nameInput = fragment.querySelector(".group-name");
    const removeButton = fragment.querySelector(".remove-group");

    row.querySelectorAll("td[data-column]").forEach((cell) => {
      cell.dataset.label = labels[cell.dataset.column];
    });

    configureGroupInput(scopeInput, {
      id: `group-scope-${index}`,
      value: entry.scope,
      placeholder: "qq:group:123456789",
      label: labels.scope,
      onInput: (event) => {
        event.currentTarget.setCustomValidity("");
        updateGroupEntry(index, "scope", event.currentTarget.value);
      },
    });
    configureGroupInput(addressInput, {
      id: `group-address-${index}`,
      value: entry.address,
      placeholder: "mc.example.com:25565",
      label: labels.address,
      onInput: (event) => updateGroupEntry(index, "address", event.currentTarget.value),
    });
    configureGroupInput(nameInput, {
      id: `group-name-${index}`,
      value: entry.name,
      placeholder: translate("groups.namePlaceholder", "可选显示名称"),
      label: labels.name,
      onInput: (event) => updateGroupEntry(index, "name", event.currentTarget.value),
    });

    removeButton.setAttribute(
      "aria-label",
      translate("groups.removeAria", "删除第 {index} 条群服映射", { index: index + 1 }),
    );
    removeButton.title = translate("groups.remove", "删除");
    removeButton.addEventListener("click", () => {
      groupServers.splice(index, 1);
      renderGroupServers();
      markDirty();
      addGroupButton.focus();
    });

    groupRows.append(fragment);
  });

  if (Number.isInteger(focusIndex)) {
    document.getElementById(`group-scope-${focusIndex}`)?.focus();
  }
}

function refreshGroupTranslations() {
  const labels = {
    scope: translate("groups.scope", "会话范围"),
    address: translate("groups.address", "服务器地址"),
    name: translate("groups.name", "显示名称"),
    actions: translate("groups.actions", "操作"),
  };

  Array.from(groupRows.children).forEach((row, index) => {
    row.querySelectorAll("td[data-column]").forEach((cell) => {
      cell.dataset.label = labels[cell.dataset.column];
    });
    for (const [key, selector] of [
      ["scope", ".group-scope"],
      ["address", ".group-address"],
      ["name", ".group-name"],
    ]) {
      const input = row.querySelector(selector);
      input.setAttribute("aria-label", labels[key]);
      input.previousElementSibling.textContent = labels[key];
    }
    row.querySelector(".group-name").placeholder = translate(
      "groups.namePlaceholder",
      "可选显示名称",
    );
    const removeButton = row.querySelector(".remove-group");
    removeButton.setAttribute(
      "aria-label",
      translate("groups.removeAria", "删除第 {index} 条群服映射", { index: index + 1 }),
    );
    removeButton.title = translate("groups.remove", "删除");
  });
}

function markDirty() {
  if (!ready || busy) {
    return;
  }
  setSaveState("dirty", translate("status.unsaved", "有尚未保存的更改。"));
}

function clearGroupValidity() {
  groupRows.querySelectorAll(".group-scope").forEach((input) => input.setCustomValidity(""));
}

function validateGroupServers() {
  clearGroupValidity();
  const seenScopes = new Map();
  for (let index = 0; index < groupServers.length; index += 1) {
    const scope = groupServers[index].scope.trim();
    const normalizedScope = scope.toLocaleLowerCase("en-US");
    if (seenScopes.has(normalizedScope)) {
      const input = document.getElementById(`group-scope-${index}`);
      input.setCustomValidity(translate("errors.duplicateScope", "会话范围不能重复。"));
      input.reportValidity();
      input.focus();
      return false;
    }
    seenScopes.set(normalizedScope, index);
  }
  return true;
}

function readNumericField(key, integer = false) {
  const input = document.getElementById(key);
  const value = input.valueAsNumber;
  if (!Number.isFinite(value) || (integer && !Number.isInteger(value))) {
    throw new Error(
      translate("errors.invalidField", "“{field}”的值无效。", {
        field: input.labels?.[0]?.textContent?.trim() || key,
      }),
    );
  }
  return value;
}

function collectSettings() {
  const settings = {};
  for (const key of STRING_FIELDS) {
    settings[key] = document.getElementById(key).value.trim();
  }
  for (const key of INTEGER_FIELDS) {
    settings[key] = readNumericField(key, true);
  }
  for (const key of FLOAT_FIELDS) {
    settings[key] = readNumericField(key);
  }
  for (const key of BOOLEAN_FIELDS) {
    settings[key] = document.getElementById(key).checked;
  }
  settings.group_servers = groupServers.map((entry) => ({
    scope: entry.scope.trim(),
    address: entry.address.trim(),
    name: entry.name.trim(),
  }));
  return settings;
}

function formatError(error) {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  return translate("errors.unknown", "发生未知错误，请检查 AstrBot 控制台日志。");
}

async function loadSettings() {
  setBusyState(true, "load");
  setConnectionState("loading", translate("status.loading", "正在读取设置"));
  setSaveState("", translate("status.loadingDetail", "正在从插件后端读取当前设置…"));

  try {
    const response = await bridge.apiGet("settings");
    const settings = extractSettings(response);
    populateForm(settings);
    ready = true;
    setConnectionState("ready", translate("status.connected", "已连接"));
    setSaveState("", translate("status.loaded", "设置已载入。"));
  } catch (error) {
    ready = false;
    setConnectionState("error", translate("status.connectionFailed", "连接失败"));
    setSaveState("error", formatError(error));
  } finally {
    setBusyState(false);
    reloadButton.disabled = false;
  }
}

async function saveSettings() {
  if (!form.reportValidity() || !validateGroupServers()) {
    setSaveState("error", translate("errors.fixFields", "请先修正标记出的配置项。"));
    return;
  }

  let settings;
  try {
    settings = collectSettings();
  } catch (error) {
    setSaveState("error", formatError(error));
    return;
  }

  setBusyState(true, "save");
  setSaveState("", translate("status.saving", "正在保存设置…"));
  setConnectionState("loading", translate("status.savingShort", "正在保存"));

  try {
    const response = await bridge.apiPost("settings/save", { settings });
    const payload = extractPayload(response);
    const saved = normalizeSettings(payload.settings);
    populateForm(saved);
    setConnectionState("ready", translate("status.connected", "已连接"));
    if (payload.applied === false) {
      setSaveState(
        "warning",
        translate(
          "status.savedNeedsReload",
          "设置已保存，但需要重载插件后才能完全生效。",
        ),
      );
    } else {
      setSaveState("success", translate("status.saved", "设置已保存并立即生效。"));
    }
  } catch (error) {
    setConnectionState("error", translate("status.saveFailed", "保存失败"));
    setSaveState("error", formatError(error));
  } finally {
    setBusyState(false);
  }
}

function bindEvents() {
  form.addEventListener("input", (event) => {
    if (!event.target.closest("#group-server-rows")) {
      markDirty();
    }
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    void saveSettings();
  });

  reloadButton.addEventListener("click", () => {
    void loadSettings();
  });

  addGroupButton.addEventListener("click", () => {
    groupServers.push({ scope: "", address: "", name: "" });
    const newIndex = groupServers.length - 1;
    renderGroupServers(newIndex);
    markDirty();
  });
}

async function initialize() {
  bindEvents();

  if (
    !bridge ||
    typeof bridge.ready !== "function" ||
    typeof bridge.apiGet !== "function" ||
    typeof bridge.apiPost !== "function"
  ) {
    setConnectionState("error", "Bridge unavailable");
    setSaveState(
      "error",
      "AstrBotPluginPage bridge 不可用。请从 AstrBot 插件详情页打开此页面。",
    );
    reloadButton.disabled = true;
    return;
  }

  try {
    const context = await bridge.ready();
    applyTranslations(context);
    if (typeof bridge.onContext === "function") {
      unsubscribeContext = bridge.onContext((nextContext) => applyTranslations(nextContext));
    }
    await loadSettings();
  } catch (error) {
    setConnectionState("error", translate("status.connectionFailed", "连接失败"));
    setSaveState("error", formatError(error));
    reloadButton.disabled = false;
  }
}

window.addEventListener("beforeunload", () => {
  if (typeof unsubscribeContext === "function") {
    unsubscribeContext();
  }
});

void initialize();
