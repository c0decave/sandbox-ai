// sandbox-ai web: one page, plain JavaScript, no build step. Everything shown from the server goes through
// textContent (never innerHTML); every change sends the session's CSRF token.
"use strict";

const state = {
  me: null, host: localStorage.getItem("sandbox-ai-host") || "local", hosts: [], page: "starts",
  themes: {}, theme: localStorage.getItem("sandbox-ai-theme") || "system", terminals: [], current: null,
  approvals: 0, timer: null, asked: new Set(),
};
const AGENTS = ["claude", "codex", "opencode"];
const PAGES = [
  ["starts", "Starts and sessions"], ["approvals", "Approvals"], ["terminals", "Terminals"],
  ["doctor", "Doctor"], ["logs", "Logs"], ["llm", "LLM servers"], ["vpn", "VPN profiles"], ["settings", "Defaults"], ["hosts", "Hosts"],
];

// --- small helpers ---------------------------------------------------------------------------------------

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const headers = {};
  if (method !== "GET") {
    headers["Content-Type"] = "application/json";
    if (state.me) headers["X-CSRF-Token"] = state.me.csrf;
  }
  const response = await fetch(path, {
    method, headers, credentials: "same-origin", cache: "no-store",
    body: method === "GET" ? undefined : JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await response.json(); } catch (e) { data = null; }
  if (response.status === 401 && path !== "/api/login") { state.me = null; render(); }
  if (!response.ok) throw new ApiError(response.status, (data && data.error) || response.statusText);
  return data;
}

function hostPath(suffix) { return `/api/hosts/${encodeURIComponent(state.host)}${suffix}`; }

function notice(text, kind) { return el("div", { class: `notice ${kind || ""}`, role: kind === "error" ? "alert" : "status" }, text); }

// --- notifications: toasts in the theme's colours, top right ---------------------------------------------
// Errors stay until they are closed (role "alert"); the others (role "status") close after TOAST_SECONDS, not
// while the pointer or the focus is on them. Title and text are text nodes: what a sandbox chose is never markup.

const TOAST_SECONDS = 8;
const TOAST_MAX = 4;
const TOAST_LEVELS = { info: ["Information", "i"], success: ["Done", "✓"], warning: ["Warning", "!"], error: ["Error", "×"] };

function toast(level, title, text, options) {
  if (!Object.prototype.hasOwnProperty.call(TOAST_LEVELS, level)) level = "info";
  const opts = options || {};
  let box = document.getElementById("toasts");
  if (!box) { box = el("div", { id: "toasts", class: "toasts" }); document.body.append(box); }
  const [name, sign] = TOAST_LEVELS[level];
  const seconds = level === "error" ? 0 : (opts.seconds ?? TOAST_SECONDS);
  let timer = null;
  const item = el("div", { class: `toast ${level}`, role: level === "error" ? "alert" : "status" });
  const dismiss = () => { clearTimeout(timer); item.remove(); };
  const start = () => { clearTimeout(timer); if (seconds > 0) timer = setTimeout(dismiss, seconds * 1000); };
  const body = String(text ?? "");
  item.append(
    el("span", { class: "glyph", "aria-hidden": "true" }, sign),
    el("div", { class: "body" },
      el("span", { class: "sr-only" }, `${name}: `),
      el("strong", { class: "title" }, String(title ?? "")),
      body ? el("div", { class: "text" }, body.length > 2000 ? `${body.slice(0, 1999)}…` : body) : null,
      opts.action ? el("div", { class: "actions" },
        el("button", { onclick: () => { dismiss(); opts.action[1](); } }, opts.action[0])) : null),
    el("button", { class: "close", "aria-label": "Close", title: "Close", onclick: dismiss }, "×"));
  for (const type of ["mouseenter", "focusin"]) item.addEventListener(type, () => clearTimeout(timer));
  for (const type of ["mouseleave", "focusout"]) item.addEventListener(type, start);
  box.prepend(item);  // the newest on top
  while (box.children.length > TOAST_MAX) box.lastElementChild.remove();
  start();
  item.dismiss = dismiss;
  return item;
}

// What failed, as an error toast: catch (e) { failed("Could not …")(e); }
function failed(title) { return (e) => toast("error", title, e && e.message ? e.message : String(e)); }

function statusCell(value) {
  const kind = { ok: "ok", warn: "warn", fail: "fail", skip: "skip", running: "ok", paused: "warn", stopped: "muted" }[value] || "muted";
  return el("td", { class: `status-${kind}` }, value === undefined || value === null ? "" : String(value));
}

function table(columns, rows, empty) {
  if (!rows.length) return el("p", { class: "hint" }, empty || "Nothing here.");
  return el("table", {}, el("thead", {}, el("tr", {}, columns.map(c => el("th", {}, c)))),
    el("tbody", {}, rows));
}

async function busy(button, work) {
  button.disabled = true;
  try { return await work(); } finally { button.disabled = false; }
}

// --- themes: the desktop app's ---------------------------------------------------------------------------

async function loadThemes() {
  try {
    const data = await (await fetch("/static/themes.json", { cache: "no-cache" })).json();
    state.themes = data.themes || {};
  } catch (e) { state.themes = {}; }
  applyTheme(state.theme);
  if (window.matchMedia) window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(state.theme));
}

// WCAG 2.1 contrast, for the sign on a toast's badge (the theme's warn, bad, ok or accent colour behind it).
function luminance(color) {
  let hex = String(color || "").replace("#", "");
  if (hex.length === 3) hex = hex.split("").map(c => c + c).join("");
  const channel = (i) => { const v = parseInt(hex.slice(i, i + 2), 16) / 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
}

function contrast(a, b) {
  const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

function readableOn(background, candidates) {
  return candidates.filter(Boolean).reduce((best, c) => (contrast(c, background) > contrast(best, background) ? c : best));
}

function applyTheme(name) {
  state.theme = name;
  localStorage.setItem("sandbox-ai-theme", name);
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = state.themes[name] || state.themes[dark ? "dark" : "light"];
  if (!theme) return;
  const root = document.documentElement.style;
  for (const [key, value] of Object.entries(theme.colors || {})) root.setProperty(`--${key}`, value);
  for (const [key, value] of Object.entries(theme.status || {})) root.setProperty(`--${key}`, value);
  const colors = theme.colors || {}, status = theme.status || {};
  for (const [key, value] of [["accent", colors.accent], ["ok", status.ok], ["warn", status.warn], ["bad", status.bad]]) {
    if (value) root.setProperty(`--on-${key}`, readableOn(value, [colors.accent_text, colors.base, colors.text, "#000000", "#ffffff"]));
  }
  root.setProperty("--radius", `${theme.radius ?? 8}px`);
  const families = (theme.font && theme.font.families) || [];
  if (families.length) root.setProperty("--font", families.map(f => (/\s/.test(f) ? `"${f}"` : f)).join(", "));
  if (theme.font && theme.font.size) root.setProperty("--size", `${Math.round(theme.font.size * 1.4)}px`);
  document.body.classList.toggle("bevel", Boolean(theme.bevel));
  document.documentElement.style.colorScheme = theme.dark ? "dark" : "light";
  for (const t of state.terminals) t.term.options.theme = terminalTheme();
}

function terminalTheme() {
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = state.themes[state.theme] || state.themes[dark ? "dark" : "light"] || {};
  return theme.terminal || {};
}

// --- login -----------------------------------------------------------------------------------------------

function loginView(message) {
  const user = el("input", { id: "user", autocomplete: "username", required: true, "aria-label": "User" });
  const password = el("input", { id: "password", type: "password", autocomplete: "current-password", required: true, "aria-label": "Password" });
  const code = el("input", { id: "code", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}", maxlength: 6, required: true, "aria-label": "Code" });
  const status = el("div");
  if (message) status.append(notice(message, "error"));
  const form = el("form", {
    class: "card login", onsubmit: async (event) => {
      event.preventDefault();
      clear(status);
      try {
        state.me = await api("POST", "/api/login", { user: user.value, password: password.value, code: code.value });
        state.me = await api("GET", "/api/me");
        password.value = "";
        render();
      } catch (e) {
        password.value = ""; code.value = "";
        status.append(notice(e.message, "error"));
      }
    },
  },
  el("h1", {}, el("img", { src: "/static/icon.svg", alt: "", width: 28, height: 28 }), " sandbox-ai"),
  status,
  el("div", { class: "form" },
    el("label", { for: "user" }, "User"), user,
    el("label", { for: "password" }, "Password"), password,
    el("label", { for: "code" }, "Code from your TOTP app"), code,
    el("button", { class: "primary", type: "submit" }, "Log in")),
  el("p", { class: "hint" }, "A login is made on the server: sandbox-ai web user add NAME"));
  return form;
}

// --- the frame -------------------------------------------------------------------------------------------

async function render() {
  const root = clear(document.getElementById("app"));
  if (!state.me) {
    try { state.me = await api("GET", "/api/me"); } catch (e) { state.me = null; }
  }
  if (!state.me) { root.append(loginView()); return; }
  try { state.hosts = await api("GET", "/api/hosts"); } catch (e) { state.hosts = [{ name: "local", kind: "local" }]; }
  if (!state.hosts.some(h => h.name === state.host)) state.host = "local";
  const hostSelect = el("select", { "aria-label": "Host", onchange: (e) => { state.host = e.target.value; localStorage.setItem("sandbox-ai-host", state.host); show(state.page); } },
    state.hosts.map(h => el("option", { value: h.name, selected: h.name === state.host }, h.kind === "local" ? `${h.name} (this machine)` : `${h.name} (${h.ssh})`)));
  const themeSelect = el("select", { "aria-label": "Theme", onchange: (e) => applyTheme(e.target.value) },
    el("option", { value: "system", selected: state.theme === "system" }, "as the system"),
    Object.entries(state.themes).map(([key, t]) => el("option", { value: key, selected: state.theme === key }, t.label || key)));
  const nav = el("nav", { "aria-label": "Pages" }, PAGES.map(([key, label]) =>
    el("button", { "data-page": key, class: state.page === key ? "selected" : "", onclick: () => show(key) }, label)));
  root.append(el("div", { class: "layout" },
    el("header", {},
      el("span", { class: "brand" }, el("img", { src: "/static/icon.svg", alt: "" }), "sandbox-ai"),
      el("label", {}, "Host ", hostSelect),
      el("span", { class: "spacer" }),
      themeSelect,
      el("span", { class: "hint" }, state.me.user),
      el("button", { onclick: logout }, "Log out")),
    nav, el("main", { id: "main" })));
  show(state.page);
  clearInterval(state.timer);
  state.timer = setInterval(poll, 5000);
  poll();
}

async function logout() {
  try { await api("POST", "/api/logout"); } catch (e) { /* the session is gone either way */ }
  for (const t of state.terminals) t.ws.close();
  state.terminals = []; state.me = null; clearInterval(state.timer); state.asked.clear();
  for (const item of document.querySelectorAll("#toasts .toast")) item.remove();
  render();
}

async function poll() {
  if (!state.me) return;
  try {
    const open = await api("GET", hostPath("/approvals"));
    state.approvals = open.length;
    const fresh = open.filter(q => !state.asked.has(`${state.host} ${q.id}`));
    for (const q of fresh) state.asked.add(`${state.host} ${q.id}`);
    if (fresh.length && state.page !== "approvals") announce(fresh);
  } catch (e) { state.approvals = 0; }
  const button = document.querySelector('nav button[data-page="approvals"]');
  if (button) {
    for (const badge of button.querySelectorAll(".badge")) badge.remove();
    if (state.approvals) button.append(el("span", { class: "badge", "aria-label": `${state.approvals} open` }, state.approvals));
  }
  if (state.page === "approvals") pages.approvals(document.getElementById("main"), true);
  if (state.page === "terminals") refreshTerminalStarts(document.getElementById("main"));
}

// New questions: a warning toast (a canary that went off: an error, which stays), with the way to answer them.
function announce(questions) {
  const canary = questions.find(q => q.kind === "canary");
  const first = canary || questions[0];
  const more = questions.length > 1 ? ` and ${questions.length - 1} more` : "";
  toast(canary ? "error" : "warning", `sandbox-ai asks: ${first.sandbox || first.run || state.host}`, `${first.question || first.kind || ""}${more}`,
    { action: ["Open approvals", () => show("approvals")] });
}

function show(page) {
  state.page = page;
  for (const button of document.querySelectorAll("nav button")) button.classList.toggle("selected", button.dataset.page === page);
  const main = document.getElementById("main");
  if (!main) return;
  for (const t of state.terminals) t.box.classList.add("hidden");
  if (page !== "terminals") clear(main);
  pages[page](main).catch(e => { clear(main); main.append(notice(e.message, "error")); });
}

// --- pages -----------------------------------------------------------------------------------------------

const pages = {};

pages.starts = async (main) => {
  main.append(el("h1", {}, `Starts and sessions on ${state.host}`), el("p", { class: "hint" }, "Loading…"));
  const starts = await api("GET", hostPath("/starts"));
  clear(main).append(el("h1", {}, `Starts and sessions on ${state.host}`));
  const rows = starts.map(s => {
    const actions = el("td", { class: "actions" });
    const act = (label, action, extra) => el("button", Object.assign({
      onclick: async (event) => {
        if (action === "delete" && !confirm(`Delete ${s.name} and its kept HOME?`)) return;
        await busy(event.target, async () => {
          try { await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/${action}`), action === "delete" ? { confirm: true } : {}); show("starts"); }
          catch (e) { failed(`Could not ${label.toLowerCase()} ${s.name}`)(e); }
        });
      },
    }, extra || {}), label);
    if (s.attachable) actions.append(el("button", { class: "primary", onclick: () => openTerminal({ kind: "attach", name: s.name }, `${s.name}`) }, "Attach"));
    if (s.state === "running") actions.append(act("Pause", "pause"), act("Stop", "stop"),
      el("button", { onclick: () => networkCard(main, s.name) }, "Network…"));
    if (s.state === "paused") actions.append(act("Resume", "resume"), act("Stop", "stop"));
    if (s.state === "stopped" && s.keep) actions.append(act("Resume", "resume"), act("Delete", "delete", { class: "danger" }));
    return el("tr", {}, el("td", { class: "mono" }, s.name), el("td", {}, s.agent || s.kind || ""), statusCell(s.state),
      el("td", { class: "mono" }, s.workspace || ""), el("td", {}, s.home || ""), el("td", {}, (s.started || "").replace("T", " ").replace("Z", "")), actions);
  });
  main.append(table(["Name", "Agent", "State", "Workspace", "HOME", "Started", ""], rows, "Nothing runs or is kept on this host."));
  // a new start: in a terminal of the page, detachable like every start (Ctrl-b d)
  const agent = el("select", { id: "agent" }, AGENTS.map(a => el("option", { value: a }, a)));
  const name = el("input", { id: "name", placeholder: "automatic", pattern: "[A-Za-z0-9][A-Za-z0-9_.-]{0,47}" });
  const workspace = el("input", { id: "workspace", placeholder: "/home/you/src/project", class: "mono" });
  const options = el("textarea", { id: "options", rows: 3, class: "mono", placeholder: "--mode high\n--allow-pypi" });
  // a preset to start from (sandbox-ai presets on that host; an older sandbox-ai has none)
  const preset = el("select", { id: "preset" }, el("option", { value: "" }, "none"));
  const presetHint = el("span", { class: "hint" }, "");
  let presets = [];
  try { presets = await api("GET", hostPath("/presets")); } catch (e) { presetHint.textContent = "this host's sandbox-ai has no presets yet"; }
  for (const p of presets) preset.append(el("option", { value: p.id }, `${p.id}: ${p.title}`));
  preset.addEventListener("change", () => {
    const chosen = presets.find(p => p.id === preset.value);
    const lines = options.value.split("\n").filter(line => line.trim() && !line.trim().startsWith("--preset"));
    if (chosen) {
      lines.unshift(`--preset ${chosen.id}`);
      for (const [key, spec] of Object.entries(chosen.values || {}))
        if (!spec.optional && spec.default === undefined) lines.push(`--preset-value ${key}=`);
      presetHint.textContent = `${chosen.description}${(chosen.notes || []).length ? " " + chosen.notes.join(" ") : ""}`;
    } else presetHint.textContent = "";
    options.value = lines.join("\n");
  });
  main.append(el("h2", {}, "New start"), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "agent" }, "Agent"), agent,
    el("label", { for: "name" }, "Name"), name,
    el("label", { for: "workspace" }, "Workspace"), workspace,
    el("label", { for: "preset" }, "Preset"), el("div", {}, preset, " ", presetHint),
    el("label", { for: "options" }, "Options (one per line)"), options,
    el("span"), el("div", { class: "row" },
      el("button", { class: "primary", onclick: () => {
        const list = options.value.split("\n").map(v => v.trim()).filter(Boolean).flatMap(line => line.startsWith("--") && line.includes(" ") ? [line.slice(0, line.indexOf(" ")), line.slice(line.indexOf(" ") + 1).trim()] : [line]);
        openTerminal({ kind: "start", agent: agent.value, name: name.value.trim(), workspace: workspace.value.trim(), options: JSON.stringify(list) }, name.value.trim() || `new ${agent.value}`);
      } }, "Start in a terminal"),
      el("span", { class: "hint" }, "Detach with Ctrl-b d: the start runs on.")))));
};

// the network of a running start (sandbox-ai net allow|revoke|show on its host)
function networkCard(main, name) {
  const old = document.getElementById("network-card");
  if (old) old.remove();
  const field = (id, placeholder) => el("input", { id, placeholder, class: "mono" });
  const hosts = field("net-hosts", "pypi.org api.example.org"), tcp = field("net-tcp", "192.0.2.10:5432");
  const ports = field("net-ports", "3000 11434"), dns = el("input", { id: "net-dns", type: "checkbox" });
  const gitUrl = field("net-git-url", "https://git.example.org"), gitRef = field("net-git-ref", "GITEA_TOKEN");
  const gitKind = el("select", { id: "net-git-kind" }, el("option", { value: "env" }, "variable"), el("option", { value: "secret" }, "secret"));
  const shown = el("pre", { class: "mono" }, "");
  const words = (input) => input.value.split(/\s+/).filter(Boolean);
  const body = (op) => {
    const request = { op, allow_host: words(hosts), allow_tcp: words(tcp), host_ports: words(ports).map(Number), dns: dns.checked };
    if (gitUrl.value.trim() && gitRef.value.trim()) request.git_login = { url: gitUrl.value.trim(), kind: gitKind.value, ref: gitRef.value.trim() };
    return request;
  };
  const refresh = async () => { try { shown.textContent = JSON.stringify(await api("GET", hostPath(`/net/${encodeURIComponent(name)}`)), null, 2); } catch (e) { shown.textContent = e.message; } };
  const change = (op) => async (event) => busy(event.target, async () => {
    try { await api("POST", hostPath(`/net/${encodeURIComponent(name)}`), body(op)); await refresh(); } catch (e) { failed(`Could not change the network of ${name}`)(e); }
  });
  main.append(el("div", { class: "card", id: "network-card" }, el("h2", {}, `Network of ${name}`),
    el("p", { class: "hint" }, "Until the sandbox ends. Ports on the host go through a relay only you start; a Git login names where its token is, never the token."),
    el("div", { class: "form" },
      el("label", { for: "net-hosts" }, "Host names (TCP/443)"), hosts,
      el("label", { for: "net-tcp" }, "IP:PORT (TCP)"), tcp,
      el("label", { for: "net-ports" }, "Ports on the host"), ports,
      el("label", { for: "net-dns" }, "Real DNS"), dns,
      el("label", { for: "net-git-url" }, "Git login: server"), gitUrl,
      el("label", { for: "net-git-kind" }, "Git login: token in"), el("div", {}, gitKind, " ", gitRef),
      el("span"), el("div", { class: "row" },
        el("button", { class: "primary", onclick: change("allow") }, "Allow"),
        el("button", { onclick: change("revoke") }, "Revoke"),
        el("button", { onclick: refresh }, "Show"))),
    shown));
  refresh();
}

pages.approvals = async (main, refresh) => {
  const questions = await api("GET", hostPath("/approvals"));
  if (state.page !== "approvals") return;
  clear(main).append(el("h1", {}, `Approvals on ${state.host}`),
    el("p", { class: "hint" }, "What the sandboxes ask. Without an answer within 60 seconds a question is refused."));
  const rows = questions.map(q => {
    const scope = el("select", { "aria-label": "Scope" }, ["once", "run", "always"].map(s => el("option", { value: s }, s)));
    const answer = (decision) => async (event) => busy(event.target, async () => {
      try { await api("POST", hostPath(`/approvals/${encodeURIComponent(q.id)}`), { decision, scope: scope.value }); pages.approvals(main); poll(); }
      catch (e) { failed(`Could not ${decision} the question`)(e); }
    });
    return el("tr", {}, el("td", {}, q.question, q.always ? el("div", { class: "hint" }, q.always) : null), el("td", { class: "mono" }, q.sandbox || q.run),
      el("td", { class: "actions" }, scope, el("button", { class: "primary", onclick: answer("allow") }, "Allow"), el("button", { class: "danger", onclick: answer("deny") }, "Deny")));
  });
  main.append(table(["Question", "Sandbox", ""], rows, "No open questions."));
};

pages.doctor = async (main) => {
  main.append(el("h1", {}, `Doctor on ${state.host}`), el("p", { class: "hint" }, "Checking…"));
  const results = await api("GET", hostPath("/doctor"));
  const problems = results.filter(r => r.status === "fail").length, warnings = results.filter(r => r.status === "warn").length;
  clear(main).append(el("h1", {}, `Doctor on ${state.host}`),
    notice(problems ? `${problems} problem(s), ${warnings} warning(s)` : warnings ? `no problems, ${warnings} warning(s)` : "no problems", problems ? "error" : "ok"),
    table(["", "Check", "Detail", "What to do"], results.map(r => el("tr", {}, statusCell(r.status), el("td", {}, r.check), el("td", {}, r.detail || ""), el("td", { class: "mono" }, r.hint || "")))));
};

pages.logs = async (main) => {
  main.append(el("h1", {}, `Logs on ${state.host}`), el("p", { class: "hint" }, "Loading…"));
  const data = await api("GET", hostPath("/logs"));
  clear(main).append(el("h1", {}, `Logs on ${state.host}`));
  const summaries = Array.isArray(data) ? data : [data];
  for (const s of summaries) {
    main.append(el("div", { class: "card" },
      el("div", { class: "mono hint" }, s.directory || ""),
      el("p", {}, `${s.denied ?? 0} refused connection(s), ${s.file_events ?? 0} file event(s)`),
      (s.targets || []).length ? table(["Refused destination", "Count"], s.targets.slice(0, 20).map(([t, n]) => el("tr", {}, el("td", { class: "mono" }, t), el("td", {}, n)))) : null,
      (s.suggestions || []).length ? el("div", {}, el("h2", {}, "To allow them"), table(["Option", "Why", "Count"], s.suggestions.map(x => el("tr", {}, el("td", { class: "mono" }, x.option), el("td", {}, x.why), el("td", {}, x.count))))) : null,
      el("details", {}, el("summary", {}, "All of it (JSON)"), el("pre", {}, JSON.stringify(s, null, 2)))));
  }
};

pages.vpn = async (main) => {
  const profiles = await api("GET", hostPath("/vpn"));
  clear(main).append(el("h1", {}, `VPN profiles on ${state.host}`),
    el("p", { class: "hint" }, "A profile names the secret that holds its key or password (sandbox-ai secrets set NAME on that host): no key passes through this page."));
  const rows = profiles.map(p => el("tr", {}, el("td", { class: "mono" }, p.profile), el("td", {}, p.type || ""), el("td", {}, p.provider || ""),
    el("td", { class: "actions" },
      el("button", { onclick: async (event) => busy(event.target, async () => {
        try { toast("success", `${p.profile} works`, (await api("POST", hostPath(`/vpn/${encodeURIComponent(p.profile)}/check`), {})).output || "ok"); }
        catch (e) { failed(`The check of ${p.profile} failed`)(e); } }) }, "Check"),
      el("button", { class: "danger", onclick: async (event) => {
        if (!confirm(`Remove the VPN profile ${p.profile}?`)) return;
        await busy(event.target, async () => { try { await api("DELETE", hostPath(`/vpn/${encodeURIComponent(p.profile)}`)); show("vpn"); } catch (e) { failed(`Could not remove ${p.profile}`)(e); } });
      } }, "Remove"))));
  main.append(table(["Profile", "Kind", "Provider", ""], rows, "No VPN profiles on this host."));
  const name = el("input", { id: "vpn-name", placeholder: "home", pattern: "[a-z0-9][a-z0-9_-]{0,31}" });
  const kind = el("select", { id: "vpn-kind" }, [["wireguard", "WireGuard (a wg-quick file)"], ["openvpn", "OpenVPN (an .ovpn file)"],
    ["mullvad", "Mullvad (account number in a secret)"], ["tailscale", "Tailscale or Headscale (auth key in a secret)"]].map(([v, t]) => el("option", { value: v }, t)));
  const file = el("input", { id: "vpn-file", class: "mono", placeholder: "/home/you/vpn/home.conf (on that host)" });
  const secret = el("input", { id: "vpn-secret", class: "mono", placeholder: "VPN_HOME" });
  const location = el("input", { id: "vpn-location", placeholder: "se-sto" });
  const exitNode = el("input", { id: "vpn-exit", placeholder: "exit-node-1" });
  const loginServer = el("input", { id: "vpn-login", placeholder: "https://headscale.example.org" });
  const rows2 = { file: [file, "Configuration file"], secret: [secret, "Secret (its name)"], location: [location, "Location"],
    exit_node: [exitNode, "Exit node"], login_server: [loginServer, "Login server"] };
  const wanted = { wireguard: ["file"], openvpn: ["file", "secret"], mullvad: ["secret", "location"], tailscale: ["secret", "exit_node", "login_server"] };
  const form = el("div", { class: "form" }, el("label", { for: "vpn-name" }, "Name"), name, el("label", { for: "vpn-kind" }, "Kind"), kind);
  const labels = {};
  for (const [key, [input, text]] of Object.entries(rows2)) { labels[key] = el("label", { for: input.id }, text); form.append(labels[key], input); }
  const update = () => { for (const key of Object.keys(rows2)) { const on = wanted[kind.value].includes(key); rows2[key][0].hidden = !on; labels[key].hidden = !on; } };
  kind.addEventListener("change", update);
  update();
  form.append(el("span"), el("button", { class: "primary", onclick: async (event) => busy(event.target, async () => {
    const body = { name: name.value.trim(), kind: kind.value };
    for (const key of wanted[kind.value]) { const value = rows2[key][0].value.trim(); if (value) body[key] = value; }
    try { await api("POST", hostPath("/vpn"), body); show("vpn"); } catch (e) { failed("Could not add the VPN profile")(e); }
  }) }, "Add profile"));
  main.append(el("h2", {}, "New profile"), el("div", { class: "card" }, form));
};

pages.llm = async (main) => {
  main.append(el("h1", {}, `LLM servers on ${state.host}`));
  const list = await api("GET", hostPath("/llm"));
  const output = el("div");
  const rows = (Array.isArray(list) ? list : []).map(e => {
    const agent = el("select", { "aria-label": "Agent" }, el("option", { value: "" }, "every agent"), AGENTS.map(a => el("option", { value: a }, a)));
    return el("tr", {}, el("td", { class: "mono" }, e.name), el("td", {}, e.kind), el("td", { class: "mono" }, e.url), el("td", { class: "mono" }, e.model || ""),
      el("td", { class: "actions" }, agent,
        el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try { const r = await api("POST", hostPath(`/llm/${encodeURIComponent(e.name)}/check`), agent.value ? { agent: agent.value } : {}); output.append(notice(r.ok ? "the server works for it" : "the check found problems", r.ok ? "ok" : "error"), el("pre", {}, r.output)); }
          catch (err) { output.append(notice(err.message, "error")); }
        }) }, "Check"),
        el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try { const r = await api("GET", hostPath(`/llm/${encodeURIComponent(e.name)}/models`)); output.append(el("pre", {}, JSON.stringify(r, null, 2))); }
          catch (err) { output.append(notice(err.message, "error")); }
        }) }, "Models"),
        el("button", { class: "danger", onclick: async () => { if (!confirm(`Forget ${e.name}?`)) return; try { await api("DELETE", hostPath(`/llm/${encodeURIComponent(e.name)}`)); show("llm"); } catch (err) { failed(`Could not forget ${e.name}`)(err); } } }, "Remove")));
  });
  main.append(table(["Name", "Kind", "URL", "Model", ""], rows, "No LLM server yet."), output);
  const name = el("input", { id: "llm-name", placeholder: "lmstudio" });
  const kind = el("select", { id: "llm-kind" }, ["lmstudio", "ollama", "llamacpp", "vllm", "openai-compatible"].map(k => el("option", { value: k }, k)));
  const url = el("input", { id: "llm-url", class: "mono", placeholder: "http://192.168.1.20:1234" });
  const model = el("input", { id: "llm-model", class: "mono", placeholder: "optional" });
  const status = el("div", { class: "wide" });
  main.append(el("h2", {}, "Add a server"), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "llm-name" }, "Name"), name, el("label", { for: "llm-kind" }, "Kind"), kind,
    el("label", { for: "llm-url" }, "URL"), url, el("label", { for: "llm-model" }, "Model"), model,
    el("span"), el("button", { class: "primary", onclick: (ev) => busy(ev.target, async () => {
      clear(status);
      try { await api("POST", hostPath("/llm"), { name: name.value.trim(), kind: kind.value, url: url.value.trim(), model: model.value.trim() }); show("llm"); }
      catch (e) { status.append(notice(e.message, "error")); }
    }) }, "Add"), status),
    el("p", { class: "hint" }, "Then start an agent with --llm NAME (Options of a new start). A key is set on the server: sandbox-ai secrets set NAME, then sandbox-ai llm add … --api-key-secret NAME.")));
};

pages.settings = async (main) => {
  main.append(el("h1", {}, `Defaults on ${state.host}`));
  const data = await api("GET", hostPath("/defaults"));
  const values = data.values || {};
  const status = el("div");
  const save = (key, value) => async () => {
    clear(status);
    try { await api("POST", hostPath("/defaults"), { key, value }); status.append(notice(`${key} saved`, "ok")); }
    catch (e) { status.append(notice(e.message, "error")); }
  };
  const telemetry = el("select", { id: "telemetry" },
    [["off", "off: no usage statistics, error reports, feedback or update checks (recommended)"], ["strict", "strict: also no other calls than the model's"], ["on", "on: the agents' own settings"]]
      .map(([v, t]) => el("option", { value: v, selected: (values.telemetry || "off") === v }, t)));
  telemetry.addEventListener("change", () => save("telemetry", telemetry.value === "off" ? null : telemetry.value)());
  const mode = el("select", { id: "mode" }, ["lax", "low", "medium", "high", "paranoid"].map(m => el("option", { value: m, selected: (values.mode || "medium") === m }, m)));
  mode.addEventListener("change", () => save("mode", mode.value === "medium" ? null : mode.value)());
  const categories = data.canary_categories || ["credentials", "home", "workspace"];
  const chosen = values.canaries === false ? [] : (values.canaries || data.canary_default || []);
  const boxes = categories.map(c => el("input", { type: "checkbox", id: `canary-${c}`, value: c, checked: chosen.includes(c) }));
  for (const box of boxes) box.addEventListener("change", () => {
    const picked = boxes.filter(b => b.checked).map(b => b.value);
    save("canaries", picked.length ? picked : false)();
  });
  main.append(status, el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "telemetry" }, "Telemetry of the agents"), telemetry,
    el("label", { for: "mode" }, "Security mode"), mode,
    el("span", {}, "Canary files"), el("div", { class: "row" }, boxes.map((b, i) => el("label", {}, b, " ", categories[i])))),
    el("p", { class: "hint mono" }, data.path || ""),
    el("p", { class: "hint" }, "For every start on this host; an option given at a start wins.")));
};

pages.hosts = async (main) => {
  main.append(el("h1", {}, "Hosts"), el("p", { class: "hint" }, "Machines (or LXC containers) whose sandbox-ai this service manages over SSH, with your keys; nothing listens on them but sshd."));
  const output = el("div");
  const rows = state.hosts.map(h => el("tr", {}, el("td", { class: "mono" }, h.name), el("td", { class: "mono" }, h.kind === "local" ? "this machine" : `${h.ssh}${h.port && h.port !== 22 ? ":" + h.port : ""}`),
    el("td", {}, h.description || ""), el("td", { class: "actions" },
      el("button", { onclick: (ev) => busy(ev.target, async () => {
        clear(output);
        try {
          const r = await api("POST", `/api/hosts/${encodeURIComponent(h.name)}/test`);
          output.append(notice(r.error ? `${h.name}: ${r.error}` : `${h.name}: ${r.version}`, r.error ? "error" : "ok"));
          if (r.doctor) output.append(table(["", "Check", "Detail"], r.doctor.filter(d => d.status !== "ok").map(d => el("tr", {}, statusCell(d.status), el("td", {}, d.check), el("td", {}, d.detail || ""))), "The doctor found nothing to do."));
        } catch (e) { output.append(notice(e.message, "error")); }
      }) }, "Test"),
      h.kind === "local" ? null : el("button", { class: "danger", onclick: async () => { if (!confirm(`Remove ${h.name} from the list?`)) return; try { await api("DELETE", `/api/hosts/${encodeURIComponent(h.name)}`); render(); } catch (e) { failed(`Could not remove ${h.name}`)(e); } } }, "Remove"))));
  main.append(table(["Name", "SSH", "Description", ""], rows), output);
  const fields = { name: el("input", { id: "host-name", placeholder: "ct101" }), ssh: el("input", { id: "host-ssh", class: "mono", placeholder: "user@192.168.1.101" }),
    port: el("input", { id: "host-port", type: "number", min: 1, max: 65535, placeholder: "22" }), identity: el("input", { id: "host-identity", class: "mono", placeholder: "~/.ssh/id_ed25519 (optional)" }),
    description: el("input", { id: "host-description", placeholder: "Proxmox CT 101" }) };
  const status = el("div", { class: "wide" });
  main.append(el("h2", {}, "Add a host"), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "host-name" }, "Name"), fields.name, el("label", { for: "host-ssh" }, "SSH"), fields.ssh,
    el("label", { for: "host-port" }, "Port"), fields.port, el("label", { for: "host-identity" }, "Key"), fields.identity,
    el("label", { for: "host-description" }, "Description"), fields.description,
    el("span"), el("button", { class: "primary", onclick: (ev) => busy(ev.target, async () => {
      clear(status);
      const body = { name: fields.name.value.trim(), ssh: fields.ssh.value.trim(), identity: fields.identity.value.trim(), description: fields.description.value.trim() };
      if (fields.port.value) body.port = Number(fields.port.value);
      try { await api("POST", "/api/hosts", body); render(); } catch (e) { status.append(notice(e.message, "error")); }
    }) }, "Add"), status),
    el("p", { class: "hint" }, "The service's user needs SSH access with a key (ssh-copy-id), and sandbox-ai installed there. The host key is learnt at the first contact and checked from then on.")));
};

// --- terminals -------------------------------------------------------------------------------------------

// Why a start has no terminal to attach to (list --json's attach_why; guessed for an older sandbox-ai without it).
// The desktop app says the same.
const ATTACH_WHY = {
  foreground: "runs in the foreground of its own terminal (started with --no-tmux, as a task, or without a terminal): nothing to attach; start with tmux (the default for interactive starts) to attach from here",
  paused: "paused: resume it (Starts and sessions) to attach",
  mcp: "an MCP server sandbox: it has no terminal of its own",
  stopped: "does not run",
  damaged: "damaged: see sandbox-ai list on its host",
};

function attachWhy(s) {
  const why = s.attach_why || (s.kind === "mcp" ? "mcp" : s.kind === "run" ? "foreground" : s.state === "paused" ? "paused" : "");
  return ATTACH_WHY[why] || "nothing to attach";
}

function terminalKey(host, name) { return JSON.stringify([host, name]); }

// Every start that runs on the chosen host, however it began (a terminal, the desktop app, this page, another
// machine over SSH): one click attaches the ones in tmux, the others say why they cannot be attached.
async function refreshTerminalStarts(main) {
  const box = main && main.querySelector(".terminal-starts");
  if (!box || box.dataset.loading) return;
  const host = state.host;
  box.dataset.loading = "1";
  let starts;
  try { starts = await api("GET", hostPath("/starts")); }
  catch (e) { starts = e; }
  finally { delete box.dataset.loading; }
  if (state.page !== "terminals" || state.host !== host || !box.isConnected) return;
  const list = box.querySelector(".terminal-starts-list");
  if (starts instanceof Error) { delete box.dataset.shown; clear(list).append(notice(starts.message, "error")); return; }
  const running = starts.filter(s => s.state === "running" || s.state === "paused");
  box.querySelector("summary").textContent = `Running on ${host} (${running.length})`;
  // Unchanged since the last read: the table stays (no flicker under the pointer).
  const shown = JSON.stringify([host, running.map(s => [s.name, s.agent, s.kind, s.state, s.workspace, s.attachable, s.attach_why]),
    state.terminals.filter(t => !t.closed).map(t => t.key)]);
  if (box.dataset.shown === shown) return;
  box.dataset.shown = shown;
  clear(list);
  const rows = running.map(s => {
    const open = state.terminals.find(t => t.key === terminalKey(host, s.name) && !t.closed);
    const action = s.attachable
      ? el("td", { class: "actions" }, el("button", { class: open ? "" : "primary", onclick: () => openTerminal({ kind: "attach", name: s.name }, s.name) }, open ? "Show" : "Open terminal"))
      : el("td", { class: "hint" }, attachWhy(s));
    return el("tr", {}, el("td", { class: "mono" }, s.name), el("td", {}, s.agent || s.kind || ""), statusCell(s.state),
      el("td", { class: "mono" }, s.workspace || ""), action);
  });
  list.append(table(["Name", "Agent", "State", "Workspace", ""], rows, "Nothing runs on this host."));
}

pages.terminals = async (main) => {
  if (!main.querySelector(".terminal-tabs")) {
    clear(main).append(el("h1", {}, "Terminals"),
      el("details", { class: "terminal-starts", open: !state.terminals.length }, el("summary", {}, `Running on ${state.host}`),
        el("div", { class: "terminal-starts-list" }, el("p", { class: "hint" }, "Loading…"))),
      el("div", { class: "terminal-tabs", role: "tablist" }), el("div", { class: "terminal-boxes" }));
  }
  refreshTerminalStarts(main);
  const tabs = clear(main.querySelector(".terminal-tabs"));
  if (!state.terminals.length) {
    tabs.append(el("p", { class: "hint" }, window.Terminal ? "Open the terminal of a running start above, or start one on the page Starts and sessions." : "Terminals need xterm.js on the server: sandbox-ai web vendor"));
  }
  for (const t of state.terminals) {
    tabs.append(el("button", { role: "tab", class: t === state.current ? "selected" : "", onclick: () => { state.current = t; show("terminals"); } }, t.title),
      el("button", { "aria-label": `Close ${t.title}`, onclick: () => { t.ws.close(); } }, "×"));
    t.box.classList.toggle("hidden", t !== state.current);
  }
  if (state.current) { state.current.fit(); state.current.term.focus(); }
};

function openTerminal(params, title) {
  if (!window.Terminal) { toast("warning", "No terminals here", "Terminals need xterm.js on the server: sandbox-ai web vendor"); return; }
  // One tab per start: attaching again shows the tab already open.
  const key = params.kind === "attach" ? terminalKey(state.host, params.name) : null;
  const already = key && state.terminals.find(t => t.key === key && !t.closed);
  if (already) { state.current = already; show("terminals"); return; }
  const term = new window.Terminal({ cursorBlink: true, fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono"), theme: terminalTheme(), scrollback: 5000 });
  const fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
  if (fit) term.loadAddon(fit);
  const box = el("div", { class: "terminal" });
  show("terminals");
  document.querySelector("#main .terminal-boxes").append(box);
  term.open(box);
  if (fit) fit.fit();
  const query = new URLSearchParams(Object.assign({ host: state.host, csrf: state.me.csrf, cols: term.cols, rows: term.rows }, params));
  for (const [key, value] of [...query.entries()]) if (value === "") query.delete(key);
  const ws = new WebSocket(`wss://${location.host}/api/terminal?${query}`);
  ws.binaryType = "arraybuffer";
  const encoder = new TextEncoder();
  const entry = { title: `${state.host}: ${title}`, key, closed: false, term, ws, box, fit: () => { if (fit) { fit.fit(); if (ws.readyState === 1) ws.send(JSON.stringify({ resize: [term.cols, term.rows] })); } } };
  ws.onmessage = (event) => term.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
  ws.onclose = () => {
    entry.closed = true;
    term.write("\r\n[the terminal ended]\r\n");
    setTimeout(() => {
      state.terminals = state.terminals.filter(t => t !== entry);
      if (state.current === entry) state.current = state.terminals[state.terminals.length - 1] || null;
      term.dispose(); box.remove();
      if (state.page === "terminals") show("terminals");
    }, 1500);
  };
  term.onData((data) => { if (ws.readyState === 1) ws.send(encoder.encode(data)); });
  term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(JSON.stringify({ resize: [cols, rows] })); });
  window.addEventListener("resize", entry.fit);
  state.terminals.push(entry);
  state.current = entry;
  show("terminals");
}

loadThemes().then(render);
