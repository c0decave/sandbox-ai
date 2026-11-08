// sandbox-ai web: one page, plain JavaScript, no build step. Everything shown from the server goes through
// textContent (never innerHTML); every change sends the session's CSRF token.
"use strict";

const state = {
  me: null, host: localStorage.getItem("sandbox-ai-host") || "local", hosts: [], page: "starts",
  themes: {}, theme: localStorage.getItem("sandbox-ai-theme") || "system", terminals: [], current: null,
  approvals: 0, approvalsError: null, timer: null, asked: new Set(),
};
const AGENTS = ["claude", "codex", "opencode"];
const PAGES = [
  ["starts", "Starts and sessions"], ["approvals", "Approvals"], ["terminals", "Terminals"],
  ["doctor", "Doctor"], ["logs", "Logs"], ["llm", "LLM servers"], ["vpn", "VPN profiles"], ["kits", "Kits"], ["canaries", "Canaries"], ["settings", "Defaults"], ["hosts", "Hosts"],
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

// OPTIONS.background: a request of the page's own polls (X-Background: 1), which does not count as the user's
// activity: an open tab that nobody uses reaches its session's idle limit all the same.
async function api(method, path, body, options) {
  const headers = {};
  if (options && options.background) headers["X-Background"] = "1";
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
  for (const t of state.terminals) { t.dismissed = true; t.ws.close(); }
  state.terminals = []; state.me = null; clearInterval(state.timer); state.asked.clear();
  for (const item of document.querySelectorAll("#toasts .toast")) item.remove();
  render();
}

// Every 5 s, in the background (it keeps no session alive): the open questions of the chosen host (the badge; a
// failed check shows "?", never 0, which would read as "nothing asks"), and the page's lists that follow them.
async function poll() {
  if (!state.me) return;
  const host = state.host;
  let open;
  try {
    open = await api("GET", hostPath("/approvals"), undefined, { background: true });
    if (!Array.isArray(open)) throw new Error("not a list of questions");
    state.approvals = open.length; state.approvalsError = null;
    const fresh = open.filter(q => !state.asked.has(`${host} ${q.id}`));
    for (const q of fresh) state.asked.add(`${host} ${q.id}`);
    if (fresh.length && state.page !== "approvals") announce(fresh);
  } catch (e) { open = e; state.approvals = null; state.approvalsError = e.message || String(e); }
  if (!state.me || state.host !== host) return;
  const button = document.querySelector('nav button[data-page="approvals"]');
  if (button) {
    for (const badge of button.querySelectorAll(".badge")) badge.remove();
    if (state.approvals === null) button.append(el("span", { class: "badge unknown", title: `could not check: ${state.approvalsError}`, "aria-label": "could not check" }, "?"));
    else if (state.approvals) button.append(el("span", { class: "badge", "aria-label": `${state.approvals} open` }, state.approvals));
  }
  if (state.page === "approvals") pages.approvals(document.getElementById("main"), open).catch(() => {});
  if (state.page === "terminals") refreshTerminalStarts(document.getElementById("main"), true);
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

// An MCP server's "Attach to…": one of the host's kept agent starts gets it (sandbox-ai mcp attach), kept for its
// later starts; a running one reaches it after a restart, done now when "restart" is ticked.
function attachControls(s, starts) {
  const server = s.name.replace(/^mcp-/, "");
  const targets = starts.filter(t => t.kind !== "mcp" && t.directory && t.state !== "damaged");
  const choose = el("select", { "aria-label": `Attach ${server} to` }, targets.map(t => el("option", { value: t.name }, `${t.name} (${t.state})`)));
  const restart = el("input", { type: "checkbox" });
  const attach = el("button", { disabled: !targets.length, title: targets.length ? "" : "no kept agent start on this host", onclick: (ev) => busy(ev.target, async () => {
    try {
      const r = await api("POST", hostPath(`/mcp/${encodeURIComponent(server)}/attach`), { start: choose.value, restart: restart.checked });
      toast(r.attached === "after a restart" ? "warning" : "success", `${server} → ${r.start}`, r.message);
      show("starts");
    } catch (e) { failed(`Could not attach ${server}`)(e); }
  }) }, "Attach to…");
  return [attach, choose, el("label", { class: "hint", title: "a running start cannot take a new MCP server while it runs: it is restarted, and the agent continues its conversation" }, restart, " restart if it runs")];
}

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
    if (s.kind === "mcp") actions.append(...attachControls(s, starts));
    // which MCP servers a start reaches (or will at its next start), and whom a server serves
    const mcpNote = s.kind === "mcp" ? ((s.serves || []).length ? `serves ${s.serves.join(", ")}` : "serves no start yet")
      : (s.mcp || []).length ? `MCP: ${s.mcp.join(", ")}` : "";
    return el("tr", {}, el("td", { class: "mono" }, s.name), el("td", {}, s.agent || s.kind || ""), statusCell(s.state),
      el("td", { class: "mono" }, s.workspace || "", mcpNote ? el("div", { class: "hint" }, mcpNote) : null), el("td", {}, s.home || ""), el("td", {}, (s.started || "").replace("T", " ").replace("Z", "")), actions);
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

// The open questions of the chosen host. The table is kept between the polls: its rows are keyed by the question's
// id, a new question adds a row, an answered or expired one takes its row away, and every other row stays as it is
// (the scope chosen in it, an Allow waiting for its confirmation, the pointer over it). Allow asks first, in the
// row, naming the sandbox, what it asks for and the scope; the answer's output (sandbox-ai approvals) is a toast.
const SCOPES = { once: "this once", run: "for the rest of this run", always: "always (remembered for the project)" };
const approvalsView = { main: null, host: null, body: null, rows: new Map() };

function approvalRow(q, view) {
  const id = String(q.id), where = q.sandbox || q.run || state.host;
  const scope = el("select", { "aria-label": "Scope" }, Object.keys(SCOPES).map(s => el("option", { value: s }, s)));
  const actions = el("td", { class: "actions" });
  const row = el("tr", { "data-id": id }, el("td", {}, q.question, q.always ? el("div", { class: "hint" }, q.always) : null),
    el("td", { class: "mono" }, where), actions);
  const answer = (decision) => async (event) => busy(event.target, async () => {
    try {
      const r = await api("POST", hostPath(`/approvals/${encodeURIComponent(q.id)}`), { decision, scope: scope.value });
      toast("success", `${decision === "allow" ? "Allowed" : "Denied"} ${SCOPES[scope.value]}: ${where}`, (r && r.output) || q.question || "");
      row.remove(); view.rows.delete(id);
      poll();
    } catch (e) { failed(`Could not ${decision} the question of ${where}`)(e); buttons(); }
  });
  const buttons = () => clear(actions).append(scope,
    el("button", { class: "primary", onclick: () => askAllow() }, "Allow"),
    el("button", { class: "danger", onclick: answer("deny") }, "Deny"));
  const askAllow = () => clear(actions).append(
    el("div", { class: "confirm", role: "group", "aria-label": "Confirm" },
      `Allow ${where}: ${q.question || q.kind || "this"}, ${SCOPES[scope.value]}${scope.value === "always" && q.always ? ` (${q.always})` : ""}?`),
    el("button", { class: "primary", onclick: answer("allow") }, "Yes, allow"),
    el("button", { onclick: () => buttons() }, "Cancel"));
  buttons();
  return row;
}

// KNOWN: what poll() fetched already (the questions, or the Error of a failed check); else they are fetched here.
pages.approvals = async (main, known) => {
  const view = approvalsView, host = state.host;
  if (known === undefined || view.main !== main || view.host !== host || !view.body || !view.body.isConnected) {
    view.main = main; view.host = host; view.rows = new Map();
    view.status = el("div"); view.body = el("tbody");
    view.table = el("table", {}, el("thead", {}, el("tr", {}, ["Question", "Sandbox", ""].map(c => el("th", {}, c)))), view.body);
    view.empty = el("p", { class: "hint" }, "No open questions.");
    clear(main).append(el("h1", {}, `Approvals on ${host}`),
      el("p", { class: "hint" }, "What the sandboxes ask. Without an answer within 60 seconds a question is refused."),
      view.status, view.table, view.empty);
    view.table.hidden = true; view.empty.hidden = true;
  }
  let questions = known;
  if (questions === undefined) {
    try { questions = await api("GET", hostPath("/approvals")); } catch (e) { questions = e; }
  }
  if (state.page !== "approvals" || state.host !== host || view.main !== main) return;
  clear(view.status);
  if (!Array.isArray(questions)) {  // the rows shown stay: a failed check is no answer to them
    view.status.append(notice(`Could not check for questions: ${questions && questions.message ? questions.message : questions}`, "error"));
    view.empty.hidden = true;
    return;
  }
  const ids = new Set(questions.map(q => String(q.id)));
  for (const [id, row] of view.rows) if (!ids.has(id)) { row.remove(); view.rows.delete(id); }
  for (const q of questions) {
    if (view.rows.has(String(q.id))) continue;
    const row = approvalRow(q, view);
    view.rows.set(String(q.id), row);
    view.body.append(row);
  }
  view.table.hidden = !view.rows.size; view.empty.hidden = Boolean(view.rows.size);
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

// Kits (sandbox-ai kits on that host): what each brings, and adding to a kit of yours. sandbox-ai's own kits are
// read-only: with one of them (or none) chosen, what is added goes to a kit of yours, or a new one created first.
const KIT_KINDS = { always: "always (sandbox-ai)", default: "always (default kit)", builtin: "built-in", user: "yours" };

pages.kits = async (main) => {
  main.append(el("h1", {}, `Kits on ${state.host}`), el("p", { class: "hint" }, "Loading…"));
  const data = await api("GET", hostPath("/kits"));
  const kits = (Array.isArray(data.kits) ? data.kits : []).filter(k => k && typeof k.name === "string");
  const mine = kits.filter(k => k.kind === "user").map(k => k.name).sort();
  clear(main).append(el("h1", {}, `Kits on ${state.host}`),
    el("p", { class: "hint mono" }, data.kits_dir || ""),
    el("p", { class: "hint" }, "Every change runs sandbox-ai kits on that host, with its checks. sandbox-ai's own and the built-in kits are read-only: what you add goes to a kit of yours."));
  const output = el("div");
  const result = (title, r) => { clear(output).append(notice(title, "ok"), el("pre", {}, (r && r.output) || "")); toast("success", title, ""); };
  const listOf = (value) => Array.isArray(value) ? value.filter(v => typeof v === "string") : [];
  const rows = kits.map(k => {
    const own = k.kind === "user";
    const brings = el("td", {});
    for (const [agent, info] of Object.entries(k.agents || {})) {
      if (!info || typeof info !== "object") continue;
      const line = el("div", {}, el("strong", {}, `${agent}: `));
      const entries = [...listOf(info.skills).map(s => ["skill", s]), ...listOf(info.plugins).map(p => ["plugin", p])];
      if (!entries.length) line.append(el("span", { class: "hint" }, "nothing"));
      for (const [kind, value] of entries) {
        line.append(el("span", { class: "mono" }, `${kind} ${value}`));
        if (own) line.append(" ", el("button", { class: "danger small", title: `Remove the ${kind} ${value} for ${agent}`, "aria-label": `Remove the ${kind} ${value} for ${agent}`,
          onclick: (ev) => busy(ev.target, async () => {
            if (!confirm(`Remove the ${kind} ${value} for ${agent} from the kit ${k.name}?`)) return;
            const body = kind === "skill" ? { skill: value, agents: [agent] } : { plugin: value, agent };
            try { result(`${k.name}: removed ${value}`, await api("POST", hostPath(`/kits/${encodeURIComponent(k.name)}/remove-${kind}`), body)); show("kits"); }
            catch (e) { failed(`Could not remove ${value}`)(e); }
          }) }, "×"));
        line.append("  ");
      }
      if (info.error) line.append(el("span", { class: "status-bad" }, ` ✘ ${info.error}`));
      brings.append(line);
    }
    return el("tr", {}, el("td", { class: "mono" }, k.name), el("td", {}, KIT_KINDS[k.kind] || String(k.kind || "")), brings,
      el("td", { class: "actions" }, own ? el("button", { class: "danger", onclick: (ev) => busy(ev.target, async () => {
        if (!confirm(`Delete the kit ${k.name} with everything in it? Projects that use it cannot start until --kit ${k.name} is removed.`)) return;
        try { result(`${k.name} deleted`, await api("DELETE", hostPath(`/kits/${encodeURIComponent(k.name)}`), { confirm: true })); show("kits"); }
        catch (e) { failed(`Could not delete ${k.name}`)(e); }
      }) }, "Delete kit") : null));
  });
  main.append(table(["Kit", "Kind", "Brings", ""], rows, "No kits on this host."), output);

  // a new kit
  const newName = el("input", { id: "kit-new-name", placeholder: "my-kit", pattern: "[A-Za-z0-9][A-Za-z0-9_.-]{0,63}" });
  const agentBoxes = (prefix) => AGENTS.map(a => el("label", {}, el("input", { type: "checkbox", value: a, checked: true, id: `${prefix}-${a}` }), " ", a));
  const chosenAgents = (box) => [...box.querySelectorAll("input[type=checkbox]")].filter(b => b.checked).map(b => b.value);
  const newAgents = el("div", { class: "row" }, agentBoxes("kit-new"));
  const newStatus = el("div", { class: "wide" });
  main.append(el("h2", {}, "New kit"), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "kit-new-name" }, "Name"), newName, el("span", {}, "For"), newAgents,
    el("span"), el("button", { class: "primary", onclick: (ev) => busy(ev.target, async () => {
      clear(newStatus);
      try { result(`Kit ${newName.value.trim()} created`, await api("POST", hostPath("/kits"), { name: newName.value.trim(), agents: chosenAgents(newAgents) })); show("kits"); }
      catch (e) { newStatus.append(notice(e.message, "error")); }
    }) }, "Create"), newStatus)));

  // adding to a kit of yours (or a new one)
  const target = el("select", { id: "kit-target" }, mine.map(n => el("option", { value: n }, n)), el("option", { value: "" }, "a new kit…"));
  const targetName = el("input", { id: "kit-target-name", placeholder: "my-kit", value: mine.length ? "" : "my-kit" });
  const targetLabel = el("label", { for: "kit-target-name" }, "New kit's name");
  const updateTarget = () => { const fresh = target.value === ""; targetName.hidden = !fresh; targetLabel.hidden = !fresh; };
  target.addEventListener("change", updateTarget);
  updateTarget();
  const addStatus = el("div", { class: "wide" });
  const send = (action, body, title) => async (ev) => busy(ev.target, async () => {
    clear(addStatus);
    const fresh = target.value === "";
    const name = fresh ? targetName.value.trim() : target.value;
    try { result(title(name), await api("POST", hostPath(`/kits/${encodeURIComponent(name)}/${action}`), Object.assign(body(), { create: fresh }))); show("kits"); }
    catch (e) { addStatus.append(notice(e.message, "error")); }
  });
  const folder = el("input", { id: "kit-skill-path", class: "mono", placeholder: "/home/you/skills/review (on that host, with SKILL.md)" });
  const folderAs = el("input", { id: "kit-skill-as", placeholder: "the folder's name" });
  const skillName = el("input", { id: "kit-skill-name", placeholder: "release-notes", pattern: "[a-z0-9][a-z0-9-]{0,63}" });
  const skillDescription = el("input", { id: "kit-skill-description", placeholder: "What it does and when the agent should use it (one line)" });
  const skillAgents = el("div", { class: "row" }, agentBoxes("kit-skill"));
  const plugin = el("input", { id: "kit-plugin", class: "mono", placeholder: "sandcastle@sandbox-ai, or an npm package for OpenCode" });
  const pluginAgent = el("select", { id: "kit-plugin-agent" }, ["claude", "opencode"].map(a => el("option", { value: a }, a)));
  const pluginSource = el("input", { id: "kit-plugin-source", class: "mono", placeholder: "OWNER/REPO or https://… (only for a marketplace the kit does not declare yet)" });
  main.append(el("h2", {}, "Add to a kit of yours"), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "kit-target" }, "Kit"), target, targetLabel, targetName,
    el("span", {}, "For"), skillAgents,
    el("h3", { class: "wide" }, "A skill from a folder"),
    el("label", { for: "kit-skill-path" }, "Folder"), folder, el("label", { for: "kit-skill-as" }, "Name in the kit"), folderAs,
    el("span"), el("button", { class: "primary", onclick: send("add-skill", () => ({ path: folder.value.trim(), as: folderAs.value.trim(), agents: chosenAgents(skillAgents) }), n => `Skill copied into ${n}`) }, "Add skill from the folder"),
    el("p", { class: "hint wide" }, "The kit gets a copy: links are not followed, special files are skipped. A folder where sandboxes can write is copied with a note: review it first."),
    el("h3", { class: "wide" }, "A new skill"),
    el("label", { for: "kit-skill-name" }, "Name"), skillName, el("label", { for: "kit-skill-description" }, "Description"), skillDescription,
    el("span"), el("button", { class: "primary", onclick: send("new-skill", () => ({ skill: skillName.value.trim(), description: skillDescription.value.trim(), agents: chosenAgents(skillAgents) }), n => `New skill in ${n}`) }, "Write the skill (SKILL.md)"),
    el("h3", { class: "wide" }, "A plugin"),
    el("label", { for: "kit-plugin" }, "Plugin"), plugin, el("label", { for: "kit-plugin-agent" }, "Agent"), pluginAgent,
    el("label", { for: "kit-plugin-source" }, "Marketplace source"), pluginSource,
    el("span"), el("button", { class: "primary", onclick: send("add-plugin", () => ({ plugin: plugin.value.trim(), agent: pluginAgent.value, source: pluginSource.value.trim() }), n => `Plugin added to ${n}`) }, "Add plugin"),
    addStatus),
    el("p", { class: "hint" }, "Use a kit with --kit NAME (Options of a new start). Claude plugins are installed with sandbox-ai plugins claude on that host.")));
};

// the canary log: every opened decoy or token that left, by which process, with the agent's tool call, its prompt
// turn and the model requests around it (sandbox-ai canaries log on that host; redacted there, tokens never shown)
function canaryProcess(p) {
  if (!p || p.unknown !== undefined || !Object.keys(p).length) return `unknown${p && p.unknown ? ` (${p.unknown})` : ""}`;
  let text = `${p.comm || "?"} (pid ${p.pid ?? "?"}${p.ns_pid !== undefined && p.ns_pid !== p.pid ? `, ${p.ns_pid} in the sandbox` : ""})`;
  if (Array.isArray(p.argv) && p.argv.length) text += `: ${p.argv.join(" ")}`;
  if (Array.isArray(p.parents) && p.parents.length) text += ` ← ${p.parents.slice(0, 3).join(" ← ")}`;
  return text;
}

function canaryLinks(e) {
  const items = [];
  for (const t of e.tool_calls || []) items.push(el("li", {}, `tool call (${t.match}): `, el("span", { class: "mono" }, t.summary || ""), ` — ${(t.time || "").replace("T", " ").replace("Z", "")}, activity ${t.id}`));
  if (e.turn) items.push(el("li", {}, "prompt turn: ", el("span", {}, e.turn.summary || ""), ` — ${(e.turn.time || "").replace("T", " ").replace("Z", "")}, activity ${e.turn.id}`));
  for (const r of e.requests || []) items.push(el("li", { class: r.position === "carried the token" ? "status-bad" : "" }, `model request (${r.position}): `,
    el("span", { class: "mono" }, `${r.method || ""} ${r.url || r.host || ""}${r.status ? ` → ${r.status}` : ""}`),
    ` — ${(r.time || "").replace("T", " ").replace("Z", "")}, ${r.activity_id ? `activity ${r.activity_id}` : `${r.log} @${r.offset}`}`));
  return items;
}

pages.canaries = async (main) => {
  main.append(el("h1", {}, `Canaries on ${state.host}`), el("p", { class: "hint" }, "Loading…"));
  let logs;
  try { logs = await api("GET", hostPath("/canaries")); }
  catch (e) { clear(main).append(el("h1", {}, `Canaries on ${state.host}`), notice(e.status === 502 ? `${e.message} (an older sandbox-ai there has no canaries log: sandbox-ai update)` : e.message, "error")); return; }
  clear(main).append(el("h1", {}, `Canaries on ${state.host}`),
    el("p", { class: "hint" }, "Every decoy file opened and every canary token that left a sandbox: by which process (for a file, the one the supervisor found holding it open; inotify itself names nobody), with the agent's tool call, its prompt turn and the model requests around it."));
  const events = (Array.isArray(logs) ? logs : []).flatMap(log => (log.events || []).map(e => Object.assign({ agent: log.agent, activity: log.activity }, e)));
  if (!events.length) { main.append(el("p", { class: "hint" }, "No canary was touched on this host.")); return; }
  const rows = [];
  for (const e of events.slice().reverse()) {
    const links = canaryLinks(e);
    const detail = el("tr", { class: "canary-links hidden" }, el("td", { colspan: 7 },
      links.length ? el("ul", {}, links) : el("p", { class: "hint" }, e.activity ? "Nothing of the agent's in that time window." : "No activity timeline for this run: nothing to link."),
      (e.processes || []).length > 1 ? el("p", { class: "hint" }, `also open in: ${e.processes.slice(1).map(canaryProcess).join("; ")}`) : null));
    const toggle = el("button", { "aria-expanded": "false", onclick: () => { const open = detail.classList.toggle("hidden"); toggle.setAttribute("aria-expanded", String(!open)); toggle.textContent = open ? "Show links" : "Hide links"; } }, "Show links");
    rows.push(el("tr", { class: "canary-event" },
      el("td", {}, (e.time || "").replace("T", " ").replace("Z", "")), el("td", { class: "mono" }, e.run || ""),
      el("td", { class: "status-bad" }, `${e.event || "?"}${e.action ? ` (${e.action})` : ""}`),
      el("td", { class: "mono" }, `${e.canary || "-"} ${e.path || ""}`),
      el("td", {}, e.summary || e.error || ""), el("td", { class: "mono" }, canaryProcess(e.process)),
      el("td", { class: "actions" }, toggle)), detail);
  }
  main.append(table(["Time", "Run", "Event", "Canary", "What", "Process", ""], rows));
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

// The version of a host's sandbox-ai and whether an update is there (hosts list's status; null: not asked yet).
function versionText(s) {
  if (!s) return "…";
  if (!s.version) return s.error ? "unreachable" : "?";
  if (["checkout", "pip", "package"].includes(s.kind)) return `${s.version} (${s.kind})`;
  if (s.update) return `${s.version} → ${s.available} available`;
  return s.update === false ? `${s.version}, up to date` : s.version;
}

// What a key made private (ssh refuses key files others can read) and what was only named.
function keyNotices(r) {
  return [...(r.fixed || []).map(f => notice(`${f.path} could be read by others (mode ${f.was}), which ssh refuses: made private (${f.now}).`, "ok")),
    ...(r.warnings || []).map(w => notice(w, "warn"))];
}

// One host's update: its own signed sandbox-ai update; a sudo that wants a password is asked in a terminal tab.
async function updateHost(name, output, sudo) {
  const body = sudo === undefined ? {} : { sudo };
  const r = await api("POST", `/api/hosts/${encodeURIComponent(name)}/update`, body);
  output.append(...keyNotices(r));
  if (r.changed) output.append(notice(`${name}: ${r.before || "?"} → ${r.after}${r.sudo ? " (sudo)" : ""}`, "ok"));
  else if (r.error && !r.after) {
    output.append(el("div", { class: "notice error", role: "alert" }, `${name}: not updated: ${r.error}`,
      r.needs_password ? el("button", { onclick: () => openTerminal({ host: name, kind: "update" }, `update ${name}`) }, "Update in a terminal (sudo asks there)") : null,
      r.needs_sudo ? el("button", { onclick: (ev) => busy(ev.target, () => updateHost(name, output, true).catch(failed(`Could not update ${name}`))) }, "Update with sudo") : null));
  } else output.append(notice(`${name}: ${r.after || r.before}, up to date${r.error ? ` (${r.error})` : ""}`, r.error ? "warn" : "ok"));
  for (const note of r.notes || []) output.append(notice(`${name}: ${note}`, "warn"));
  return r;
}

pages.hosts = async (main) => {
  main.append(el("h1", {}, "Hosts"), el("p", { class: "hint" }, "Machines (or LXC containers) whose sandbox-ai this service manages over SSH, with your keys; nothing listens on them but sshd."));
  const output = el("div", { "aria-live": "polite" });
  const versions = {};
  const versionCells = {};
  const showVersions = (found) => {
    Object.assign(versions, found);
    for (const [name, cell] of Object.entries(versionCells)) cell.textContent = versionText(versions[name]);
  };
  const loadVersions = async (refresh) => {
    try { showVersions(await api(refresh ? "POST" : "GET", "/api/hosts-status", refresh ? {} : undefined)); }
    catch (e) { for (const cell of Object.values(versionCells)) cell.textContent = "?"; output.append(notice(`Versions: ${e.message}`, "error")); }
  };
  const remote = state.hosts.filter(h => h.kind !== "local");
  const rows = state.hosts.map(h => {
    const version = el("td", { class: "mono" }, h.kind === "local" ? "this machine" : "…");
    if (h.kind !== "local") versionCells[h.name] = version;
    return el("tr", {}, el("td", { class: "mono" }, h.name), el("td", { class: "mono" }, h.kind === "local" ? "this machine" : `${h.ssh}${h.port && h.port !== 22 ? ":" + h.port : ""}`),
      version, el("td", {}, h.description || "", h.update_sudo ? el("span", { class: "hint" }, " (updates with sudo)") : null), el("td", { class: "actions" },
        el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try {
            const r = await api("POST", `/api/hosts/${encodeURIComponent(h.name)}/test`);
            output.append(...keyNotices(r));
            output.append(notice(r.error ? `${h.name}: ${r.error}` : `${h.name}: ${r.version}${(r.fixed || []).length ? " (tried again after making the key private)" : ""}`, r.error ? "error" : "ok"));
            if (r.doctor) output.append(table(["", "Check", "Detail"], r.doctor.filter(d => d.status !== "ok").map(d => el("tr", {}, statusCell(d.status), el("td", {}, d.check), el("td", {}, d.detail || ""))), "The doctor found nothing to do."));
          } catch (e) { output.append(notice(e.message, "error")); }
        }) }, "Test"),
        h.kind === "local" ? null : el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          output.append(notice(`Updating ${h.name}…`));
          try { await updateHost(h.name, output); } catch (e) { output.append(notice(`${h.name}: ${e.message}`, "error")); }
          loadVersions(false);
        }) }, "Update"),
        h.kind === "local" ? null : el("button", { class: "danger", onclick: async () => { if (!confirm(`Remove ${h.name} from the list?`)) return; try { await api("DELETE", `/api/hosts/${encodeURIComponent(h.name)}`); render(); } catch (e) { failed(`Could not remove ${h.name}`)(e); } } }, "Remove")));
  });
  const progress = el("progress", { max: Math.max(remote.length, 1), value: 0, hidden: true, "aria-label": "Update progress" });
  const updateAll = el("button", { class: "primary", disabled: !remote.length, onclick: (ev) => busy(ev.target, async () => {
    clear(output);
    progress.hidden = false; progress.value = 0;
    let updated = 0, failedCount = 0;
    for (const h of remote) {
      output.append(notice(`Updating ${h.name} (${progress.value + 1} of ${remote.length})…`));
      try { const r = await updateHost(h.name, output); if (r.changed) updated++; else if (r.error && !r.after) failedCount++; }
      catch (e) { failedCount++; output.append(notice(`${h.name}: ${e.message}`, "error")); }
      progress.value += 1;
    }
    output.append(notice(`${remote.length} hosts: ${updated} updated, ${remote.length - updated - failedCount} up to date, ${failedCount} not updated`, failedCount ? "warn" : "ok"));
    progress.hidden = true;
    loadVersions(false);
  }) }, "Update all");
  const check = el("button", { disabled: !remote.length, onclick: (ev) => busy(ev.target, () => loadVersions(true)) }, "Check versions");
  main.append(table(["Name", "SSH", "sandbox-ai", "Description", ""], rows),
    el("div", { class: "actions" }, updateAll, check, progress),
    el("p", { class: "hint" }, "Update runs each host's own sandbox-ai update (signed, as install.sh installed it); a system installation updates with sudo -n, and when sudo wants a password a terminal tab asks for it. This machine updates with: sandbox-ai update."),
    output);
  if (remote.length) loadVersions(false);
  const fields = { name: el("input", { id: "host-name", placeholder: "ct101" }), ssh: el("input", { id: "host-ssh", class: "mono", placeholder: "user@192.168.1.101" }),
    port: el("input", { id: "host-port", type: "number", min: 1, max: 65535, placeholder: "22" }), identity: el("input", { id: "host-identity", class: "mono", placeholder: "~/.ssh/id_ed25519 (optional)" }),
    description: el("input", { id: "host-description", placeholder: "Proxmox CT 101" }), update_sudo: el("input", { id: "host-update-sudo", type: "checkbox" }) };
  const status = el("div", { class: "wide" });
  main.append(el("h2", {}, "Add a host"), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "host-name" }, "Name"), fields.name, el("label", { for: "host-ssh" }, "SSH"), fields.ssh,
    el("label", { for: "host-port" }, "Port"), fields.port, el("label", { for: "host-identity" }, "Key"), fields.identity,
    el("label", { for: "host-description" }, "Description"), fields.description,
    el("label", { for: "host-update-sudo" }, "Update with sudo"), fields.update_sudo,
    el("span"), el("button", { class: "primary", onclick: (ev) => busy(ev.target, async () => {
      clear(status);
      const body = { name: fields.name.value.trim(), ssh: fields.ssh.value.trim(), identity: fields.identity.value.trim(), description: fields.description.value.trim() };
      if (fields.port.value) body.port = Number(fields.port.value);
      if (fields.update_sudo.checked) body.update_sudo = true;
      try {
        const r = await api("POST", "/api/hosts", body);
        render();
        // toasts: they outlast the page drawn anew
        for (const f of r.fixed || []) toast("success", `Key of ${r.name} made private`, `${f.path} could be read by others (mode ${f.was}), which ssh refuses: now ${f.now}.`);
        for (const w of r.warnings || []) toast("warning", `Keys of ${r.name}`, w);
        if (r.connection) toast(r.connection.ok ? "success" : "error", `${r.name}: tried again`, r.connection.ok ? `The connection works: ${r.connection.version}` : r.connection.error);
      } catch (e) { status.append(notice(e.message, "error")); }
    }) }, "Add"), status),
    el("p", { class: "hint" }, "The service's user needs SSH access with a key (ssh-copy-id), and sandbox-ai installed there. The host key is learnt at the first contact and checked from then on. A key file others can read (ssh refuses it) is made private when the host is added or tested.")));
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
async function refreshTerminalStarts(main, background) {
  const box = main && main.querySelector(".terminal-starts");
  if (!box || box.dataset.loading) return;
  const host = state.host;
  box.dataset.loading = "1";
  let starts;
  try { starts = await api("GET", hostPath("/starts"), undefined, { background: Boolean(background) }); }
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
    tabs.append(el("button", { role: "tab", class: `${t === state.current ? "selected" : ""}${t.closed ? " ended" : ""}`, title: t.closed ? t.ended : null, onclick: () => { state.current = t; show("terminals"); } }, t.closed ? `${t.title} (ended)` : t.title),
      el("button", { "aria-label": `Close ${t.title}`, title: "Close the tab (a start runs on)", onclick: () => closeTerminal(t) }, "×"));
    t.box.classList.toggle("hidden", t !== state.current);
  }
  if (state.current) { if (!state.current.closed) state.current.fit(); state.current.term.focus(); }
};

// Why a terminal's WebSocket closed, in words: the service puts its reason in the close frame (a refused terminal,
// the session that ended, a message too big); 1006 is a connection refused before it opened or lost.
const CLOSE_CODES = { 1000: "ended", 1005: "ended", 1001: "the service went away", 1002: "a protocol error", 1006: "the connection was refused or lost",
  1008: "refused by the service", 1009: "a message too big", 1011: "an error of the service" };

function closeText(event, opened) {
  // the reason is the server's text: shown as text (toast) and, in the terminal, without control characters
  const reason = String(event.reason || "").replace(/[\u0000-\u001f\u007f-\u009f]/g, " ").trim();
  const what = event.code === 1006 && !opened ? "could not connect (refused, or the session ended: reload the page)" : CLOSE_CODES[event.code] || "closed";
  return `${what} (code ${event.code})${reason ? `: ${reason}` : ""}`;
}

// The tab of an ended terminal stays, with why it ended, until it is closed here; closing a running one detaches.
function closeTerminal(entry) {
  entry.dismissed = true;
  if (!entry.closed) { entry.ws.close(); return; }  // onclose takes the tab away
  state.terminals = state.terminals.filter(t => t !== entry);
  if (state.current === entry) state.current = state.terminals[state.terminals.length - 1] || null;
  entry.term.dispose(); entry.box.remove();
  if (state.page === "terminals") show("terminals");
}

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
  const entry = { title: `${state.host}: ${title}`, key, closed: false, opened: false, dismissed: false, ended: "", term, ws, box, fit: () => { if (fit) { fit.fit(); if (ws.readyState === 1) ws.send(JSON.stringify({ resize: [term.cols, term.rows] })); } } };
  ws.onopen = () => { entry.opened = true; };
  ws.onmessage = (event) => term.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
  ws.onclose = (event) => {
    entry.closed = true;
    window.removeEventListener("resize", entry.fit);
    entry.ended = closeText(event, entry.opened);
    if (entry.dismissed) { closeTerminal(entry); return; }
    term.write(`\r\n[terminal: ${entry.ended}]\r\n`);
    if (event.code !== 1000 && event.code !== 1005) toast(event.code === 1006 && entry.opened ? "warning" : "error", `Terminal ${entry.title}`, entry.ended);
    // A refused WebSocket says nothing of why: if the session ended, the login is shown.
    if (!entry.opened) api("GET", "/api/me", undefined, { background: true }).catch(() => {});
    if (state.page === "terminals") show("terminals");
  };
  term.onData((data) => { if (ws.readyState === 1) ws.send(encoder.encode(data)); });
  term.onResize(({ cols, rows }) => { if (ws.readyState === 1) ws.send(JSON.stringify({ resize: [cols, rows] })); });
  window.addEventListener("resize", entry.fit);
  state.terminals.push(entry);
  state.current = entry;
  show("terminals");
}

loadThemes().then(render);
