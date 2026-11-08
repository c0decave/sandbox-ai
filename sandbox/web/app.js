// sandbox-ai web: one page, plain JavaScript, no build step. Everything shown from the server goes through
// textContent (never innerHTML); every change sends the session's CSRF token. The page's own texts go through
// t() (i18n.js, loaded first): English or German; what the server or a host says is shown as it is.
"use strict";

const state = {
  me: null, host: localStorage.getItem("sandbox-ai-host") || "local", hosts: [], page: "starts",
  themes: {}, theme: localStorage.getItem("sandbox-ai-theme") || "system", terminals: [], current: null,
  approvals: 0, approvalsError: null, approvalErrors: {}, timer: null, tick: null, asked: new Set(),
  questionToasts: new Map(), boxes: null, terminalCount: 0, polling: false,
};
const AGENTS = ["claude", "codex", "opencode"];
const PAGES = [
  ["starts", N_("Starts and sessions")], ["approvals", N_("Approvals")], ["terminals", N_("Terminals")],
  ["doctor", N_("Doctor")], ["logs", N_("Logs")], ["llm", N_("LLM servers")], ["vpn", N_("VPN profiles")], ["kits", N_("Kits")], ["canaries", N_("Canaries")], ["settings", N_("Defaults")], ["hosts", N_("Hosts")],
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

// The API path of SUFFIX on HOST (the chosen host unless given: a question names its own).
function hostPath(suffix, host) { return `/api/hosts/${encodeURIComponent(host || state.host)}${suffix}`; }

// One rule for what ends, removes or weakens something: it is asked in place, in the page (never the browser's
// own dialog), naming the host and what happens; Cancel puts back what was there. ask() gives the nodes,
// confirmIn() puts them into CONTAINER (a table cell, a row of buttons) instead of what it holds.
function ask(text, yes, onYes, onCancel, kind) {
  return [el("div", { class: "confirm", role: "group", "aria-label": t("Confirm") }, text),
    el("button", { class: kind || "danger", onclick: onYes }, yes), el("button", { onclick: onCancel }, t("Cancel"))];
}

function confirmIn(container, text, yes, onYes, options) {
  const opts = options || {};
  const saved = [...(container.childNodes || container.children)];
  const restore = () => clear(container).append(...saved);
  const nodes = ask(text, yes, async (event) => {
    event.target.disabled = true;
    try { await onYes(event); } finally { restore(); }
  }, () => { restore(); if (opts.onCancel) opts.onCancel(); }, opts.kind);
  clear(container).append(...nodes);
  if (typeof nodes[2].focus === "function") nodes[2].focus();  // Cancel: Enter on a key held down does nothing
  return nodes;
}

// What a screen reader is told (a dedicated live region; the page itself is not one).
function say(text) {
  const live = document.getElementById("live");
  if (live) { live.textContent = ""; live.textContent = text; }
}

// The page in the address (#approvals): a reload or a link keeps it.
function pageFromHash() {
  const page = decodeURIComponent(String((typeof location !== "undefined" && location.hash) || "").replace(/^#/, ""));
  return PAGES.some(([key]) => key === page) ? page : null;
}

// The arguments of a free-form command line, split as a shell does (no expansion): quotes keep spaces, a
// backslash takes the next character as it is. A quote left open is an error, not a guess.
function shellWords(text) {
  const words = [];
  let word = null, quote = null;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quote === "'") { if (c === "'") quote = null; else word += c; continue; }
    if (quote === '"') {
      if (c === '"') quote = null;
      else if (c === "\\" && i + 1 < text.length && '"\\$`'.includes(text[i + 1])) word += text[++i];
      else if (c === "\\" && text[i + 1] === "\n") i++;
      else word += c;
      continue;
    }
    if (c === "\\" && text[i + 1] === "\n") { i++; continue; }  // a line continued
    if (/\s/.test(c)) { if (word !== null) { words.push(word); word = null; } continue; }
    if (word === null) word = "";
    if (c === "'" || c === '"') quote = c;
    else if (c === "\\" && i + 1 < text.length) word += text[++i];
    else word += c;
  }
  if (quote) throw new Error(quote === "'" ? t("a single quote is not closed") : t("a double quote is not closed"));
  if (word !== null) words.push(word);
  return words;
}

// Sizes as limits takes and shows them: exact (8g) where a unit fits, else in bytes; and rounded for reading.
const SIZE_UNITS = [["t", 2 ** 40], ["g", 2 ** 30], ["m", 2 ** 20], ["k", 2 ** 10]];
function exactSize(bytes) {
  for (const [unit, factor] of SIZE_UNITS) if (bytes >= factor && bytes % factor === 0) return `${bytes / factor}${unit}`;
  return String(bytes);
}
function readableSize(bytes) {
  for (const [unit, factor] of SIZE_UNITS) if (bytes >= factor) return `${Math.round((bytes / factor) * 10) / 10}${unit}`;
  return `${bytes} B`;
}

function notice(text, kind) { return el("div", { class: `notice ${kind || ""}`, role: kind === "error" ? "alert" : "status" }, text); }

// --- notifications: toasts in the theme's colours, top right ---------------------------------------------
// Errors stay until they are closed (role "alert"); the others (role "status") close after TOAST_SECONDS, not
// while the pointer or the focus is on them. Title and text are text nodes: what a sandbox chose is never markup.

const TOAST_SECONDS = 8;
const TOAST_MAX = 4;
const TOAST_LEVELS = { info: [N_("Information"), "i"], success: [N_("Done"), "✓"], warning: [N_("Warning"), "!"], error: [N_("Error"), "×"] };

function toast(level, title, text, options) {
  if (!Object.prototype.hasOwnProperty.call(TOAST_LEVELS, level)) level = "info";
  const opts = options || {};
  let box = document.getElementById("toasts");
  if (!box) { box = el("div", { id: "toasts", class: "toasts", "aria-live": "polite" }); document.body.append(box); }
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
      el("span", { class: "sr-only" }, `${t(name)}: `),
      el("strong", { class: "title" }, String(title ?? "")),
      body ? el("div", { class: "text" }, body.length > 2000 ? `${body.slice(0, 1999)}…` : body) : null,
      opts.action ? el("div", { class: "actions" },
        el("button", { onclick: () => { dismiss(); opts.action[1](); } }, opts.action[0])) : null),
    el("button", { class: "close", "aria-label": t("Close"), title: t("Close"), onclick: dismiss }, "×"));
  for (const type of ["mouseenter", "focusin"]) item.addEventListener(type, () => clearTimeout(timer));
  for (const type of ["mouseleave", "focusout"]) item.addEventListener(type, start);
  box.prepend(item);  // the newest on top
  while (box.children.length > TOAST_MAX) box.lastElementChild.remove();
  start();
  item.dismiss = dismiss;
  return item;
}

// What failed, as an error toast: catch (e) { failed(t("Could not …", values))(e); }
function failed(title) { return (e) => toast("error", title, e && e.message ? e.message : String(e)); }

function statusCell(value) {
  const kind = { ok: "ok", warn: "warn", fail: "fail", skip: "skip", running: "ok", paused: "warn", stopped: "muted" }[value] || "muted";
  return el("td", { class: `status-${kind}` }, value === undefined || value === null ? "" : String(value));
}

function table(columns, rows, empty) {
  if (!rows.length) return el("p", { class: "hint" }, empty || t("Nothing here."));
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
  for (const entry of state.terminals) entry.term.options.theme = terminalTheme();
}

function terminalTheme() {
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = state.themes[state.theme] || state.themes[dark ? "dark" : "light"] || {};
  return theme.terminal || {};
}

// --- login -----------------------------------------------------------------------------------------------

function loginView(message) {
  const user = el("input", { id: "user", autocomplete: "username", required: true, "aria-label": t("User") });
  const password = el("input", { id: "password", type: "password", autocomplete: "current-password", required: true, "aria-label": t("Password") });
  const code = el("input", { id: "code", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}", maxlength: 6, required: true, "aria-label": t("Code") });
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
    el("label", { for: "user" }, t("User")), user,
    el("label", { for: "password" }, t("Password")), password,
    el("label", { for: "code" }, t("Code from your TOTP app")), code,
    el("label", { for: "language" }, t("Language")), languageSelect("language"),
    el("button", { class: "primary", type: "submit" }, t("Log in"))),
  el("p", { class: "hint" }, t("A login is made on the server: {command}", { command: "sandbox-ai web user add NAME" })));
  return form;
}

// The page's language (English or German, i18n.js): chosen here, remembered in this browser; the page is drawn anew.
function languageSelect(id) {
  return el("select", { id, "aria-label": t("Language"), onchange: (event) => { setLanguage(event.target.value); render(); } },
    LANGUAGES.map(([code, name]) => el("option", { value: code, lang: code, selected: code === LANGUAGE }, name)));
}

// --- the frame -------------------------------------------------------------------------------------------

async function render() {
  const root = clear(document.getElementById("app"));
  if (!state.me) {
    try { state.me = await api("GET", "/api/me"); } catch (e) { state.me = null; }
  }
  if (!state.me) { document.title = "sandbox-ai"; root.append(loginView()); return; }
  try { state.hosts = await api("GET", "/api/hosts"); } catch (e) { state.hosts = [{ name: "local", kind: "local" }]; }
  if (!state.hosts.some(h => h.name === state.host)) state.host = "local";
  state.page = pageFromHash() || state.page;
  const hostSelect = el("select", { "aria-label": t("Host"), onchange: (e) => { state.host = e.target.value; localStorage.setItem("sandbox-ai-host", state.host); show(state.page); } },
    state.hosts.map(h => el("option", { value: h.name, selected: h.name === state.host }, h.kind === "local" ? t("{name} (this machine)", { name: h.name }) : `${h.name} (${h.ssh})`)));
  // the themes' own names, as the desktop app shows them (light and dark in the page's language)
  const themeSelect = el("select", { "aria-label": t("Theme"), onchange: (e) => applyTheme(e.target.value) },
    el("option", { value: "system", selected: state.theme === "system" }, t("as the system")),
    Object.entries(state.themes).map(([key, theme]) => el("option", { value: key, selected: state.theme === key }, t(theme.label || key))));
  const nav = el("nav", { "aria-label": t("Pages") }, PAGES.map(([key, label]) =>
    el("button", { "data-page": key, class: state.page === key ? "selected" : "", "aria-current": state.page === key ? "page" : null, onclick: () => show(key) }, t(label))));
  root.append(el("div", { class: "layout" },
    el("header", {},
      el("span", { class: "brand" }, el("img", { src: "/static/icon.svg", alt: "" }), "sandbox-ai"),
      el("label", {}, t("Host"), " ", hostSelect),
      el("span", { class: "spacer" }),
      languageSelect("header-language"),
      themeSelect,
      el("span", { class: "hint" }, state.me.user),
      el("button", { onclick: logout }, t("Log out"))),
    nav, el("main", { id: "main" })));
  show(state.page);
  clearInterval(state.timer);
  state.timer = setInterval(poll, 5000);
  clearInterval(state.tick);
  state.tick = setInterval(tickApprovals, 1000);
  poll();
}

async function logout() {
  try { await api("POST", "/api/logout"); } catch (e) { /* the session is gone either way */ }
  for (const entry of state.terminals) { entry.dismissed = true; entry.ws.close(); entry.box.remove(); }
  state.terminals = []; state.current = null; state.me = null; clearInterval(state.timer); clearInterval(state.tick);
  state.asked.clear(); state.questionToasts.clear(); state.boxes = null;
  for (const item of document.querySelectorAll("#toasts .toast")) item.remove();
  render();
}

// Every 5 s, in the background (it keeps no session alive): the open questions of every host (the badge, the
// tab's title; a failed check shows "?", never 0, which would read as "nothing asks"), and the page's lists that
// follow them.
// A poll still waiting (a host that does not answer takes up to 15 s) is not asked again on top of it.
async function poll() {
  if (!state.me || state.polling) return;
  let open;
  state.polling = true;
  try {
    open = await api("GET", "/api/approvals", undefined, { background: true });
    if (!open || !Array.isArray(open.questions)) throw new Error(t("not a list of questions"));
  } catch (e) { open = e instanceof Error ? e : new Error(String(e)); }
  finally { state.polling = false; }
  if (!state.me) return;
  noteApprovals(open);
  const main = document.getElementById("main");
  if (state.page === "approvals") pages.approvals(main, open).catch(() => {});
  if (state.page === "terminals") refreshTerminalStarts(main, true);
  if (state.page === "starts") refreshStarts(main);
}

function questionKey(q) { return `${q.host} ${q.id}`; }

// What a poll found: the count (badge, title, the live region when it changes), a toast for each new question
// (kept until it is answered or expires), a desktop notification when the user asked for those.
function noteApprovals(open) {
  const before = state.approvals;
  if (open instanceof Error) {
    state.approvals = null; state.approvalsError = open.message || String(open); state.approvalErrors = {};
  } else {
    const questions = open.questions.filter(q => q && typeof q === "object");
    const errors = open.errors && typeof open.errors === "object" ? open.errors : {};
    state.approvalErrors = errors;
    const failedHosts = Object.keys(errors);
    state.approvals = questions.length || !failedHosts.length ? questions.length : null;
    state.approvalsError = failedHosts.length ? failedHosts.map(h => `${h}: ${errors[h]}`).join("; ") : null;
    const keys = new Set(questions.map(questionKey));
    // A question gone (answered, expired) takes its toast along; one of a host that could not be asked stays.
    for (const [key, item] of state.questionToasts) {
      if (!keys.has(key) && !failedHosts.includes(key.split(" ")[0])) { item.dismiss(); state.questionToasts.delete(key); }
    }
    for (const key of [...state.asked]) if (!keys.has(key) && !failedHosts.includes(key.split(" ")[0])) state.asked.delete(key);
    const fresh = questions.filter(q => !state.asked.has(questionKey(q)));
    for (const q of fresh) state.asked.add(questionKey(q));
    if (fresh.length) {
      if (state.page !== "approvals") announce(fresh);
      notifyDesktop(fresh);
    }
  }
  const count = state.approvals;
  document.title = count ? `(${count}) sandbox-ai` : "sandbox-ai";
  if (count !== before) say(count === null ? t("Approvals: could not check") : count === 1 ? t("1 open question") : t("{count} open questions", { count }));
  const button = document.querySelector('nav button[data-page="approvals"]');
  if (button) {
    for (const badge of button.querySelectorAll(".badge")) badge.remove();
    const partly = state.approvalsError && count ? t("could not check {hosts}", { hosts: Object.keys(state.approvalErrors).join(", ") }) : "";
    if (count === null) button.append(el("span", { class: "badge unknown", title: t("could not check: {error}", { error: state.approvalsError }), "aria-label": t("could not check") }, "?"));
    else if (count) button.append(el("span", { class: "badge", title: partly || null, "aria-label": partly ? t("{count} open; {partly}", { count, partly }) : t("{count} open", { count }) }, count));
  }
}

// New questions: a warning toast that stays until the question is answered or expires (a canary that went off:
// an error), with the way to answer them.
function announce(questions) {
  const canary = questions.find(q => q.kind === "canary");
  const first = canary || questions[0];
  const more = questions.length > 1 ? t(" and {count} more", { count: questions.length - 1 }) : "";
  const item = toast(canary ? "error" : "warning", t("sandbox-ai asks: {sandbox} on {host}", { sandbox: first.sandbox || first.run || t("a sandbox"), host: first.host }), `${first.question || first.kind || ""}${more}`,
    { action: [t("Open approvals"), () => show("approvals")], seconds: 0 });
  for (const q of questions) state.questionToasts.set(questionKey(q), item);
}

// Desktop notifications: only once the user asked for them (a click on the approvals page) and the browser allows.
function notificationsOn() {
  let wanted = false;
  try { wanted = localStorage.getItem("sandbox-ai-notify") === "1"; } catch (e) { wanted = false; }
  return wanted && Boolean(window.Notification) && window.Notification.permission === "granted";
}

function notifyDesktop(questions) {
  if (!notificationsOn() || (typeof document.hidden === "boolean" && !document.hidden && state.page === "approvals")) return;
  for (const q of questions.slice(0, 3)) {
    try {
      const note = new window.Notification(t("sandbox-ai asks: {sandbox} on {host}", { sandbox: q.sandbox || q.run || t("a sandbox"), host: q.host }), { body: String(q.question || q.kind || ""), tag: questionKey(q) });

      note.onclick = () => { if (window.focus) window.focus(); show("approvals"); };
    } catch (e) { /* a browser without them for pages: the toast and the badge say it */ }
  }
}

function show(page) {
  if (!PAGES.some(([key]) => key === page)) page = "starts";
  state.page = page;
  if (typeof location !== "undefined" && location.hash !== `#${page}`) location.hash = page;
  for (const button of document.querySelectorAll("nav button")) {
    const current = button.dataset.page === page;
    button.classList.toggle("selected", current);
    if (current) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
  const main = document.getElementById("main");
  if (!main) return;
  for (const entry of state.terminals) entry.box.classList.add("hidden");
  if (page !== "terminals") clear(main);
  pages[page](main).catch(e => { clear(main); main.append(notice(e.message, "error")); });
}

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener("hashchange", () => { const page = pageFromHash(); if (state.me && page && page !== state.page) show(page); });
}

// --- pages -----------------------------------------------------------------------------------------------

const pages = {};

// An MCP server's "Attach to…": one of the host's kept agent starts gets it (sandbox-ai mcp attach), kept for its
// later starts; a running one reaches it after a restart, done now when "restart" is ticked.
function attachControls(s, starts) {
  const server = s.name.replace(/^mcp-/, "");
  const targets = starts.filter(other => other.kind !== "mcp" && other.directory && other.state !== "damaged");
  const choose = el("select", { "aria-label": t("Attach {server} to", { server }) }, targets.map(target => el("option", { value: target.name }, `${target.name} (${target.state})`)));
  const restart = el("input", { type: "checkbox" });
  const attach = el("button", { disabled: !targets.length, title: targets.length ? "" : t("no kept agent start on this host"), onclick: (ev) => busy(ev.target, async () => {
    try {
      const r = await api("POST", hostPath(`/mcp/${encodeURIComponent(server)}/attach`), { start: choose.value, restart: restart.checked });
      toast(r.attached === "after a restart" ? "warning" : "success", `${server} → ${r.start}`, r.message);
      show("starts");
    } catch (e) { failed(t("Could not attach {server}", { server }))(e); }
  }) }, t("Attach to…"));
  return [attach, choose, el("label", { class: "hint", title: t("a running start cannot take a new MCP server while it runs: it is restarted, and the agent continues its conversation") }, restart, " ", t("restart if it runs"))];
}

// The starts of the chosen host. The table is kept between the polls: its rows are keyed by the start's name, and
// a row whose start is unchanged stays as it is (a Stop waiting for its confirmation, the focus in it); a changed
// one is drawn anew, a gone one goes. The cards below it (network, resources, a review) stay open.
const startsView = { main: null, host: null, body: null, rows: new Map(), loading: false };

function startSignature(s, starts) {
  const targets = s.kind === "mcp" ? starts.filter(other => other.kind !== "mcp").map(other => [other.name, other.state, Boolean(other.directory)]) : null;
  return JSON.stringify([s.name, s.agent, s.kind, s.state, s.workspace, s.home, s.keep, s.started, s.attachable, s.directory ? 1 : 0, s.mcp, s.serves, targets]);
}

// What failed when a start's button was pressed (its action: pause, resume, stop, delete).
const RUN_FAILED = { pause: N_("Could not pause {name} on {host}"), resume: N_("Could not resume {name} on {host}"),
  stop: N_("Could not stop {name} on {host}"), delete: N_("Could not delete {name} on {host}") };

function startRow(s, starts, main) {
  const host = state.host;
  const actions = el("td", { class: "actions" });
  const run = async (button, action, body) => busy(button, async () => {
    try {
      await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/${action}`, host), body || {});
      if (state.page === "starts" && state.host === host) refreshStarts(main, true);
    } catch (e) { failed(t(RUN_FAILED[action], { name: s.name, host }))(e); }
  });
  const act = (label, action, extra) => el("button", Object.assign({ onclick: (event) => run(event.target, action) }, extra || {}), label);
  const mcp = s.kind === "mcp";
  const stopText = mcp ? t("Stop the MCP server {name} on {host}? The starts it serves lose it until it starts again.", { name: s.name, host })
    : t("Stop {name} on {host}? What runs in it ends; {home}", { name: s.name, host,
      home: s.keep ? t("its HOME is kept: Resume starts it again.") : s.home === "own" ? t("its HOME is a directory of its own, which stays.") : t("its HOME is not kept: it is discarded with the start.") });
  const stop = el("button", { onclick: () => confirmIn(actions, stopText, t("Yes, stop it"), () => run(stop, "stop", { confirm: true })) }, t("Stop"));
  if (s.attachable) actions.append(el("button", { class: "primary", onclick: () => openTerminal({ kind: "attach", name: s.name }, `${s.name}`) }, t("Attach")));
  if (s.state === "running" && !mcp) actions.append(act(t("Pause"), "pause"), stop,
    el("button", { onclick: () => networkCard(main, s.name) }, t("Network…")),
    el("button", { title: t("workspaces, devices and resource limits while it runs"), onclick: () => resourcesCard(main, s.name) }, t("Resources…")));
  else if (s.state === "running" || (s.state === "paused" && mcp)) actions.append(stop);
  if (s.state === "paused" && !mcp) actions.append(act(t("Resume"), "resume"), stop);
  if (s.state === "stopped" && s.keep) {
    const resume = el("button", { onclick: () => reviewThenResume(main, s, resume) }, t("Resume"));
    const remove = el("button", { class: "danger", onclick: () => confirmIn(actions, t("Delete {name} on {host} with its kept HOME? What is in that HOME is gone for good.", { name: s.name, host }),
      t("Yes, delete it"), () => run(remove, "delete", { confirm: true })) }, t("Delete"));
    actions.append(resume, remove);
  }
  if (mcp) actions.append(...attachControls(s, starts));
  // which MCP servers a start reaches (or will at its next start), and whom a server serves
  const mcpNote = mcp ? ((s.serves || []).length ? t("serves {starts}", { starts: s.serves.join(", ") }) : t("serves no start yet"))
    : (s.mcp || []).length ? `MCP: ${s.mcp.join(", ")}` : "";
  return el("tr", { "data-name": s.name }, el("td", { class: "mono" }, s.name), el("td", {}, s.agent || s.kind || ""), statusCell(s.state),
    el("td", { class: "mono" }, s.workspace || "", mcpNote ? el("div", { class: "hint" }, mcpNote) : null), el("td", {}, s.home || ""), el("td", {}, (s.started || "").replace("T", " ").replace("Z", "")), actions);
}

// A stopped start with a kept HOME: first what changed in the files of that HOME that decide what runs at a start
// (resume --review-home); when something did, or not everything could be checked, it is shown and asked first.
async function reviewThenResume(main, s, button) {
  const host = state.host;
  const old = document.getElementById("review-card");
  if (old) old.remove();
  const resume = async () => {
    try {
      const r = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/resume`, host), {});
      toast("success", t("{name} on {host} resumed", { name: s.name, host }), (r && r.output) || "");
      const card = document.getElementById("review-card");
      if (card) card.remove();
      if (state.page === "starts" && state.host === host) refreshStarts(main, true);
    } catch (e) { failed(t("Could not resume {name} on {host}", { name: s.name, host }))(e); }
  };
  let found;
  try { found = await busy(button, () => api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/review`, host))); }
  catch (e) { found = e; }
  if (found instanceof Error) {
    const row = el("div", { class: "row" });
    main.append(el("div", { class: "card", id: "review-card" }, el("h2", {}, t("Resume {name} on {host}?", { name: s.name, host })),
      notice(t("What changed in its HOME could not be checked: {error}", { error: found.message }), "warn"), row));
    confirmIn(row, t("Resume it without that check?"), t("Resume anyway"), resume, { kind: "primary", onCancel: () => { const c = document.getElementById("review-card"); if (c) c.remove(); } });
    return;
  }
  const review = found && found.review && typeof found.review === "object" ? found.review : null;
  const changes = review && Array.isArray(review.changes) ? review.changes.filter(c => c && typeof c === "object") : [];
  if (!review || (!changes.length && review.complete !== false)) { await busy(button, resume); return; }
  const rows = changes.map(c => el("tr", {}, el("td", {}, String(c.change || "")), el("td", { class: "mono" }, String(c.path || "")),
    el("td", {}, c.note ? String(c.note) : "", c.diff ? el("details", {}, el("summary", {}, t("Differences")), el("pre", {}, String(c.diff))) : null)));
  const row = el("div", { class: "row" });
  main.append(el("div", { class: "card", id: "review-card" }, el("h2", {}, t("The HOME of {name} on {host} changed", { name: s.name, host })),
    el("p", {}, t("Since its last start ({since}), in files that decide what runs at a start (shell, git, ssh, the agent's settings, hooks, plugins, skills). If you did not make these changes, check them before you resume it.", { since: review.since || "?" })),
    review.complete === false ? notice(t("Its HOME has more files than were checked: not every change is named."), "warn") : null,
    rows.length ? table([t("Change"), t("File"), ""], rows) : null, row));
  confirmIn(row, t("Resume {name} on {host} with these changes?", { name: s.name, host }), t("Resume anyway"), resume,
    { kind: "primary", onCancel: () => { const c = document.getElementById("review-card"); if (c) c.remove(); } });
}

// The rows of STARTS into the kept table: unchanged ones stay, changed ones are drawn anew, gone ones go.
function updateStartRows(view, starts, main) {
  const names = new Set(starts.map(s => s.name));
  for (const [name, entry] of view.rows) if (!names.has(name)) { entry.row.remove(); view.rows.delete(name); }
  for (const s of starts) {
    const signature = startSignature(s, starts);
    const entry = view.rows.get(s.name);
    if (entry && entry.signature === signature) continue;
    const row = startRow(s, starts, main);
    if (entry) entry.row.replaceWith(row); else view.body.append(row);
    view.rows.set(s.name, { signature, row });
  }
  const order = starts.map(s => view.rows.get(s.name).row);
  if (order.some((row, i) => view.body.children[i] !== row)) view.body.append(...order);
  view.table.hidden = !starts.length; view.empty.hidden = Boolean(starts.length);
}

// With the page's poll (a background request): the table of the starts page, if it is the one shown.
async function refreshStarts(main, now) {
  const view = startsView, host = state.host;
  if (view.main !== main || view.host !== host || !view.body || !view.body.isConnected || view.loading) return;
  view.loading = true;
  let starts;
  try { starts = await api("GET", hostPath("/starts"), undefined, { background: !now }); }
  catch (e) { starts = e; }
  finally { view.loading = false; }
  if (state.page !== "starts" || state.host !== host || view.main !== main || !view.body.isConnected) return;
  clear(view.status);
  if (!Array.isArray(starts)) { view.status.append(notice(t("Could not list the starts: {error}", { error: starts && starts.message ? starts.message : starts }), "error")); return; }
  updateStartRows(view, starts.filter(x => x && typeof x === "object" && typeof x.name === "string"), main);
}

pages.starts = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Starts and sessions on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const listed = await api("GET", hostPath("/starts"));
  const starts = (Array.isArray(listed) ? listed : []).filter(x => x && typeof x === "object" && typeof x.name === "string");
  if (state.page !== "starts" || state.host !== host) return;
  const view = startsView;
  view.main = main; view.host = host; view.rows = new Map(); view.loading = false;
  view.status = el("div"); view.body = el("tbody");
  view.table = el("table", {}, el("thead", {}, el("tr", {}, [t("Name"), t("Agent"), t("State"), t("Workspace"), "HOME", t("Started"), ""].map(c => el("th", {}, c)))), view.body);
  view.empty = el("p", { class: "hint" }, t("Nothing runs or is kept on this host."));
  clear(main).append(el("h1", {}, t("Starts and sessions on {host}", { host })), view.status, view.table, view.empty);
  updateStartRows(view, starts, main);
  // a new start: in a terminal of the page, detachable like every start (Ctrl-b d)
  const agent = el("select", { id: "agent" }, AGENTS.map(a => el("option", { value: a }, a)));
  const name = el("input", { id: "name", placeholder: t("automatic"), pattern: "[A-Za-z0-9][A-Za-z0-9_.-]{0,47}" });
  const workspace = el("input", { id: "workspace", placeholder: "/home/you/src/project", class: "mono" });
  const options = el("textarea", { id: "options", rows: 3, class: "mono", placeholder: "--mode high\n--allow-pypi\n-m sonnet\n--preset-value repo='my repo'", "aria-describedby": "options-hint" });
  // a preset to start from (sandbox-ai presets on that host; an older sandbox-ai has none)
  const preset = el("select", { id: "preset" }, el("option", { value: "" }, t("none")));
  const presetHint = el("span", { class: "hint" }, "");
  let presets = [];
  try { presets = await api("GET", hostPath("/presets")); } catch (e) { presetHint.textContent = t("this host's sandbox-ai has no presets yet"); }
  if (!Array.isArray(presets)) presets = [];
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
  const startStatus = el("div", { class: "wide" });
  main.append(el("h2", {}, t("New start")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "agent" }, t("Agent")), agent,
    el("label", { for: "name" }, t("Name")), name,
    el("label", { for: "workspace" }, t("Workspace")), workspace,
    el("label", { for: "preset" }, t("Preset")), el("div", {}, preset, " ", presetHint),
    el("label", { for: "options" }, t("Options")), el("div", {}, options,
      el("div", { class: "hint", id: "options-hint" }, t("As on a command line (one or more per line): {examples}; quotes keep spaces together.", { examples: "-m sonnet, --mode high" }))),
    startStatus,
    el("span"), el("div", { class: "row" },
      el("button", { class: "primary", onclick: () => {
        clear(startStatus);
        let list;
        try { list = shellWords(options.value); }
        catch (e) { startStatus.append(notice(t("Options: {error}", { error: e.message }), "error")); return; }
        openTerminal({ kind: "start", agent: agent.value, name: name.value.trim(), workspace: workspace.value.trim(), options: JSON.stringify(list) }, name.value.trim() || t("new {agent}", { agent: agent.value }));
      } }, t("Start in a terminal")),
      el("span", { class: "hint" }, t("Detach with Ctrl-b d: the start runs on."))))));
};

// the network of a running start (sandbox-ai net allow|revoke|show on its host)
function networkCard(main, name) {
  const old = document.getElementById("network-card");
  if (old) old.remove();
  const field = (id, placeholder) => el("input", { id, placeholder, class: "mono" });
  const hosts = field("net-hosts", "pypi.org api.example.org"), tcp = field("net-tcp", "192.0.2.10:5432");
  const ports = field("net-ports", "3000 11434"), dns = el("input", { id: "net-dns", type: "checkbox" });
  const gitUrl = field("net-git-url", "https://git.example.org"), gitRef = field("net-git-ref", "GITEA_TOKEN");
  const gitKind = el("select", { id: "net-git-kind" }, el("option", { value: "env" }, t("variable")), el("option", { value: "secret" }, t("secret")));
  const shown = el("div", { class: "net-shown" });
  const host = state.host;
  const words = (input) => input.value.split(/\s+/).filter(Boolean);
  const body = (op) => {
    const request = { op, allow_host: words(hosts), allow_tcp: words(tcp), host_ports: words(ports).map(Number), dns: dns.checked };
    if (gitUrl.value.trim() && gitRef.value.trim()) request.git_login = { url: gitUrl.value.trim(), kind: gitKind.value, ref: gitRef.value.trim() };
    return request;
  };
  const revoke = (request, what) => async (event) => busy(event.target, async () => {
    try {
      const r = await api("POST", hostPath(`/net/${encodeURIComponent(name)}`, host), Object.assign({ op: "revoke" }, request));
      toast("success", t("Revoked {what} for {name} on {host}", { what, name, host }), (r && r.output) || "");
      await refresh();
    } catch (e) { failed(t("Could not revoke {what} for {name} on {host}", { what, name, host }))(e); }
  });
  // net show --json as a table: what was allowed during the run, each with its own Revoke
  const draw = (data) => {
    const d = data && typeof data === "object" ? data : {};
    const list = (value) => (Array.isArray(value) ? value : []);
    const rows = [];
    const add = (kind, what, detail, request) => rows.push(el("tr", {}, el("td", {}, kind), el("td", { class: "mono" }, what), el("td", { class: "hint" }, detail || ""),
      el("td", { class: "actions" }, request ? el("button", { onclick: revoke(request, what), "aria-label": t("Revoke {what}", { what }) }, t("Revoke")) : el("span", { class: "hint" }, t("on that host: {command}", { command: "sandbox-ai net revoke" })))));
    for (const w of list(d.host_wildcards)) add(t("Host wildcard"), String(w), t("every name below it, as it is looked up"), { allow_host: [String(w)] });
    for (const h of list(d.https)) {
      const names = list(h && h.names).map(String);
      add("HTTPS (TCP/443)", names.join(", ") || String(h && h.address), names.length ? String(h.address) : t("an address without a name"), names.length ? { allow_host: names } : null);
    }
    for (const c of list(d.tcp)) {
      const address = String(c && c.address), target = `${address.includes(":") ? `[${address}]` : address}:${c && c.port}`;
      add("TCP", target, list(c && c.names).join(", "), { allow_tcp: [target] });
    }
    for (const port of list(d.host_ports)) add(t("Port on the host"), `127.0.0.1:${port}`, t("through a relay"), { host_ports: [Number(port)] });
    if (list(d.dns).length) add(t("Real DNS"), list(d.dns).join(", "), t("every lookup logged"), { dns: true });
    for (const g of list(d.git)) add(t("Git login"), String(g && g.url), g && g.user ? t("user {user}", { user: g.user }) : "", { git_login: { url: String(g && g.url), kind: "env", ref: "REVOKE" } });
    for (const n of list(d.all_ports)) add(t("All ports"), String(n), t("every protocol and port"), null);
    const taint = d.taint && typeof d.taint === "object" && d.taint.tainted ? notice(t("Untrusted content: tainted since {since}", { since: d.taint.since || "?" }), "warn") : null;
    clear(shown).append(taint || "", table([t("Kind"), t("What"), "", ""], rows, t("Nothing was allowed during this run.")));
  };
  const refresh = async () => {
    try { draw(await api("GET", hostPath(`/net/${encodeURIComponent(name)}`, host))); }
    catch (e) { clear(shown).append(notice(e.message, "error")); }
  };
  const change = (op) => async (event) => busy(event.target, async () => {
    try { await api("POST", hostPath(`/net/${encodeURIComponent(name)}`, host), body(op)); await refresh(); } catch (e) { failed(t("Could not change the network of {name} on {host}", { name, host }))(e); }
  });
  main.append(el("div", { class: "card", id: "network-card" }, el("h2", {}, t("Network of {name} on {host}", { name, host })),
    el("p", { class: "hint" }, t("Until the sandbox ends. Ports on the host go through a relay only you start; a Git login names where its token is, never the token.")),
    el("h3", {}, t("Allowed during this run")), shown,
    el("div", { class: "form" },
      el("label", { for: "net-hosts" }, t("Host names (TCP/443)")), hosts,
      el("label", { for: "net-tcp" }, "IP:PORT (TCP)"), tcp,
      el("label", { for: "net-ports" }, t("Ports on the host")), ports,
      el("label", { for: "net-dns" }, t("Real DNS")), dns,
      el("label", { for: "net-git-url" }, t("Git login: server")), gitUrl,
      el("label", { for: "net-git-kind" }, t("Git login: token in")), el("div", {}, gitKind, " ", gitRef),
      el("span"), el("div", { class: "row" },
        el("button", { class: "primary", onclick: change("allow") }, t("Allow")),
        el("button", { onclick: change("revoke") }, t("Revoke")),
        el("button", { onclick: refresh }, t("Refresh"))))));

  refresh();
}

// What a running start is given while it runs (sandbox-ai workspace, devices and limits on its host): host
// directories at /workspaces/NAME, devices, its resource limits. Each change runs that command there, with its
// checks; what widens the sandbox (a writable directory, a device, a removal by force) asks first, in place, naming
// the host. Failures are error toasts with the command's own message.
function resourcesCard(main, name) {
  const old = document.getElementById("resources-card");
  if (old) old.remove();
  const host = state.host, where = t("{name} on {host}", { name, host });
  const head = (columns) => el("thead", {}, el("tr", {}, columns.map(c => el("th", {}, c))));

  // workspaces: the start's (they stay) and the added ones (Remove; Force… asks first)
  const wsBody = el("tbody"), wsStatus = el("div");
  const wsTable = el("table", {}, head([t("In the sandbox"), t("Mode"), t("Source"), t("On the host"), ""]), wsBody);
  const workspaceRow = (w) => {
    const target = String(w.sandbox || w.host || ""), actions = el("td", { class: "actions" });
    const remove = (force) => async (event) => busy(event.target, async () => {
      try {
        const r = await api("POST", hostPath(`/workspaces/${encodeURIComponent(name)}/remove`), force ? { workspace: target, force: true, confirm: true } : { workspace: target });
        toast(force ? "warning" : "success", t("Removed {what} from {where}", { what: target, where }), (r && r.output) || "");
        loadWorkspaces();
      } catch (e) { failed(t("Could not remove {what} from {where}", { what: target, where }))(e); buttons(); }
    });
    const buttons = () => clear(actions).append(...(w.source === "added"
      ? [el("button", { onclick: remove(false) }, t("Remove")), el("button", { class: "danger", onclick: () => askForce() }, t("Force…"))]
      : [el("span", { class: "hint" }, t("until the run ends"))]));
    const askForce = () => clear(actions).append(...ask(t("Detach {what} from {where} by force? What a process there still holds stays usable, also for writing, until it is closed; only a restart takes it away for sure.", { what: target, where }),
      t("Yes, by force"), remove(true), () => buttons()));
    buttons();
    return el("tr", {}, el("td", { class: "mono" }, target), el("td", {}, w.mode || "-"),
      el("td", {}, `${w.source || ""}${w.kept ? t(", kept for the session") : ""}`), el("td", { class: "mono" }, w.host || ""), actions);
  };
  const loadWorkspaces = async () => {
    let list;
    try { list = await api("GET", hostPath(`/workspaces/${encodeURIComponent(name)}`)); }
    catch (e) { clear(wsStatus).append(notice(e.message, "error")); wsTable.hidden = true; return; }
    list = (Array.isArray(list) ? list : []).filter(w => w && typeof w === "object");
    clear(wsStatus).append(list.length ? "" : el("p", { class: "hint" }, t("No workspaces listed.")));
    clear(wsBody).append(...list.map(workspaceRow));
    wsTable.hidden = !list.length;
  };
  const directory = el("input", { id: "ws-dir", class: "mono", placeholder: t("{path} (on that host)", { path: "/home/you/src/other" }) });
  const readOnly = el("input", { id: "ws-ro", type: "checkbox", checked: true });
  const wsActions = el("div", { class: "row" });
  const addWorkspace = (dir, ro) => async (event) => busy(event.target, async () => {
    try {
      const r = await api("POST", hostPath(`/workspaces/${encodeURIComponent(name)}`), ro ? { directory: dir, read_only: true } : { directory: dir, read_only: false, confirm: true });
      toast("success", t("Added {what} to {where}", { what: dir, where }), (r && r.output) || "");
      directory.value = ""; loadWorkspaces();
    } catch (e) { failed(t("Could not add {what} to {where}", { what: dir, where }))(e); }
    wsButtons();
  });
  const wsButtons = () => clear(wsActions).append(el("button", { class: "primary", onclick: (event) => {
    const dir = directory.value.trim();
    if (!dir.startsWith("/")) { toast("warning", t("Which directory?"), t("An absolute path on {host}.", { host })); return; }
    if (readOnly.checked) { addWorkspace(dir, true)(event); return; }
    clear(wsActions).append(...ask(t("Give {name} write access to {path} on {host}? The agent can change and delete what is in it, until you remove it (kept for the session's next start).", { name, path: dir, host }),
      t("Yes, writable"), addWorkspace(dir, false), () => wsButtons()));
  } }, t("Add workspace")), el("span", { class: "hint" }, t("at /workspaces/its name")));
  wsButtons();

  // devices: given at the start (they stay) or added (Remove); adding asks first
  const devBody = el("tbody"), devStatus = el("div");
  const devTable = el("table", {}, head(["ID", t("State"), t("Nodes"), t("Device"), t("Rule"), t("Given"), ""]), devBody);
  const deviceRow = (d) => {
    const id = String(d.id ?? ""), info = d.device && typeof d.device === "object" ? d.device : {};
    const what = [info.usb, info.product && `"${info.product}"`, info.serial && t("serial {serial}", { serial: info.serial }), info.port && t("port {port}", { port: info.port })].filter(Boolean).join(" ") || "-";
    const removable = d.source === "added" && ["attached", "held", "gone"].includes(d.state);
    const remove = el("button", { onclick: (event) => busy(event.target, async () => {
      try {
        const r = await api("POST", hostPath(`/devices/${encodeURIComponent(name)}/remove`), { device: id });
        toast("success", t("Removed {what} from {where}", { what: d.value || id, where }), (r && r.output) || "");
        loadDevices();
      } catch (e) { failed(t("Could not remove {what} from {where}", { what: d.value || id, where }))(e); }
    }) }, t("Remove"));
    return el("tr", {}, el("td", {}, id), statusCell(d.state), el("td", { class: "mono" }, (d.nodes || []).join(", ") || "-"), el("td", {}, what),
      el("td", {}, d.rule ? `${d.rule} (${d.ruleset || ""})` : "-"), el("td", { class: "mono" }, `${d.value || ""} (${d.source || ""})`),
      el("td", { class: "actions" }, removable ? remove : null));
  };
  const loadDevices = async () => {
    let list;
    try { list = await api("GET", hostPath(`/devices/${encodeURIComponent(name)}`)); }
    catch (e) { clear(devStatus).append(notice(e.message, "error")); devTable.hidden = true; return; }
    list = (Array.isArray(list) ? list : []).filter(d => d && typeof d === "object");
    clear(devStatus).append(list.length ? "" : el("p", { class: "hint" }, t("No devices given to it.")));
    clear(devBody).append(...list.map(deviceRow));
    devTable.hidden = !list.length;
  };
  const device = el("input", { id: "dev-value", class: "mono", placeholder: t("{path} or {usb}", { path: "/dev/ttyUSB0", usb: "usb:2e8a:000a" }) });
  const devActions = el("div", { class: "row" });
  const addDevice = (value) => async (event) => busy(event.target, async () => {
    try {
      const r = await api("POST", hostPath(`/devices/${encodeURIComponent(name)}`), { device: value, confirm: true });
      toast("success", t("Gave {what} to {where}", { what: value, where }), (r && r.output) || "");
      device.value = ""; loadDevices();
    } catch (e) { failed(t("Could not give {what} to {where}", { what: value, where }))(e); }
    devButtons();
  });
  const devButtons = () => clear(devActions).append(el("button", { class: "primary", onclick: () => {
    const value = device.value.trim();
    if (!value.startsWith("/dev/") && !value.startsWith("usb:")) { toast("warning", t("Which device?"), t("A path under /dev or usb:VENDOR:PRODUCT[:SERIAL] of {host}.", { host })); return; }
    clear(devActions).append(...ask(t("Hand {device} of {host} to {name}? The agent can use that hardware until the sandbox ends or you remove it. A device rule may ask first (Approvals, within a minute).", { device: value, host, name }),
      t("Yes, hand it over"), addDevice(value), () => devButtons()));
  } }, t("Add device")));
  devButtons();

  // limits: filled in with the current ones (limits --show); only a field that was changed is sent
  const limit = (id, placeholder) => el("input", { id, placeholder, class: "mono", size: 10 });
  const memory = limit("lim-memory", t("{examples} or none", { examples: "8g, 512m" })), cpus = limit("lim-cpus", t("{examples} or none", { examples: "2, 1.5" })), pids = limit("lim-pids", t("{examples} or none", { examples: "4096" }));
  const fields = [["memory", memory], ["cpus", cpus], ["pids", pids]];
  const limitsNow = el("p", { class: "hint", id: "limits-now" }, t("Reading the current limits…"));
  const limActions = el("div", { class: "row" });
  let loaded = null;
  const loadLimits = async () => {
    let found;
    try { found = await api("GET", hostPath(`/limits/${encodeURIComponent(name)}`, host)); }
    catch (e) { loaded = null; limitsNow.textContent = t("The current limits could not be read ({error}): a start has 8g, half the CPUs and 4096 processes unless changed.", { error: e.message }); return; }
    const f = found && typeof found === "object" ? found : {};
    const number = (v) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);
    const mem = number(f.memory), high = number(f.memory_high), cpu = number(f.cpus), pid = number(f.pids);
    loaded = { memory: mem === null ? "none" : exactSize(mem), cpus: cpu === null ? "none" : String(cpu), pids: pid === null ? "none" : String(Math.round(pid)) };
    for (const [key, input] of fields) input.value = loaded[key];
    limitsNow.textContent = t("now: {memory} · {cpus} · {pids}", {
      memory: (mem === null ? t("no memory limit") : readableSize(mem)) + (high !== null ? t(" (held back from {size})", { size: readableSize(high) }) : ""),
      cpus: cpu === null ? t("no CPU limit") : cpu === 1 ? t("1 CPU") : t("{count} CPUs", { count: cpu }),
      pids: pid === null ? t("no process limit") : t("{count} processes", { count: Math.round(pid) }) });
  };
  const sendLimits = async (body) => {
    try {
      const r = await api("POST", hostPath(`/limits/${encodeURIComponent(name)}`, host), body);
      toast("success", t("Limits of {where}", { where }), (r && r.output) || "");
    } catch (e) { failed(t("Could not change the limits of {where}", { where }))(e); }
    await loadLimits();
  };
  const LIMIT_NAMES = { memory: N_("memory"), cpus: N_("CPU"), pids: N_("process") };
  const limButtons = () => clear(limActions).append(el("button", { class: "primary", onclick: (event) => {
    const body = {};
    for (const [key, input] of fields) {
      const value = input.value.trim();
      if (value && (!loaded || value !== loaded[key])) body[key] = value;
    }
    if (!Object.keys(body).length) { toast("warning", t("Nothing to change"), t("Change a memory size, CPUs or processes (none: no limit).")); return; }
    const removed = Object.keys(body).filter(key => body[key] === "none");
    if (!removed.length) { busy(event.target, () => sendLimits(body)); return; }
    confirmIn(limActions, t("Take away the {limits} limit of {where}? The agent can then use as much of that host as it gets (a strict security mode keeps its cap).", { limits: removed.map(k => t(LIMIT_NAMES[k])).join(t(" and ")), where }),
      t("Yes, no limit"), () => sendLimits(Object.assign(body, { confirm: true })));
  } }, t("Apply limits")), el("button", { onclick: () => { loadWorkspaces(); loadDevices(); loadLimits(); } }, t("Refresh")));
  limButtons();

  main.append(el("div", { class: "card", id: "resources-card" }, el("h2", {}, t("Workspaces, devices and limits of {where}", { where })),
    el("p", { class: "hint" }, t("While it runs, without a restart; each change runs sandbox-ai workspace, devices or limits on that host, with its checks.")),
    el("h3", {}, t("Workspaces")), wsStatus, wsTable,
    el("div", { class: "form" }, el("label", { for: "ws-dir" }, t("Directory")), directory,
      el("label", { for: "ws-ro" }, t("Read-only")), readOnly, el("span"), wsActions),
    el("h3", {}, t("Devices")), devStatus, devTable,
    el("div", { class: "form" }, el("label", { for: "dev-value" }, t("Device")), device, el("span"), devActions),
    el("h3", {}, t("Resource limits")), limitsNow,
    el("p", { class: "hint" }, t("Only a changed field is sent; none takes the limit away (asked first). A strict security mode keeps its caps.")),
    el("div", { class: "form" },
      el("label", { for: "lim-memory" }, t("Memory")), memory,
      el("label", { for: "lim-cpus" }, "CPUs"), cpus,
      el("label", { for: "lim-pids" }, t("Processes")), pids,

      el("span"), limActions)));
  loadWorkspaces();
  loadDevices();
  loadLimits();
}

// The open questions of every host. The table is kept between the polls: its rows are keyed by the host and the
// question's id, a new question adds a row, an answered or expired one takes its row away, and every other row stays
// as it is (the scope chosen in it, an Allow waiting for its confirmation, the pointer over it). Allow asks first,
// in the row, naming the host, the sandbox, what it asks for and the scope; the answer goes to the question's own
// host, its output (sandbox-ai approvals) is a toast. Each row counts down the time the question has left.
const SCOPES = { once: N_("this once"), run: N_("for the rest of this run"), always: N_("always (remembered for the project)") };
const SCOPE_ORDER = ["once", "run", "always"];
const approvalsView = { main: null, body: null, rows: new Map() };

// A link only for http(s): what a sandbox names is never a javascript: or data: URL.
function webLink(url) {
  const text = String(url || "");
  if (!/^https?:\/\/[^\s\u0000-\u001f\u007f]+$/i.test(text)) return null;
  return el("a", { href: text, target: "_blank", rel: "noopener noreferrer", class: "mono" }, text);
}

function countdownText(left) {
  if (left === null) return ["", ""];
  if (left <= 0) return [t("expired"), "bad"];
  const text = left >= 60 ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : t("{seconds} s", { seconds: left });
  return [text, left < 10 ? "bad" : left < 20 ? "warn" : ""];
}

// Once a second: each row's time left (from the host's seconds_left, counted down here).
function tickApprovals() {
  const view = approvalsView;
  if (!view.body || !view.body.isConnected) return;
  for (const entry of view.rows.values()) entry.tick();
}

function approvalRow(q, view) {
  const id = String(q.id), host = String(q.host), where = q.sandbox || q.run || t("a sandbox");
  const max = SCOPE_ORDER.includes(q.max_scope) ? q.max_scope : "always";
  const widest = SCOPE_ORDER.indexOf(max);
  const scope = el("select", { "aria-label": t("Scope") }, SCOPE_ORDER.map((s, i) => el("option", { value: s, disabled: i > widest, title: i > widest ? t("not for this question: at most {scope}", { scope: t(SCOPES[max]) }) : null }, t(SCOPES[s]))));
  const chosen = () => (SCOPE_ORDER.indexOf(scope.value) > widest || !scope.value ? "once" : scope.value);
  const actions = el("td", { class: "actions" });
  const left = el("td", { class: "countdown" });
  const link = q.kind === "browser" ? webLink(q.url) : null;
  const question = el("td", {}, q.question, link ? el("div", {}, link) : null, q.always ? el("div", { class: "hint" }, q.always) : null);
  const row = el("tr", { "data-id": id, "data-host": host }, el("td", { class: "mono" }, host), question, el("td", { class: "mono" }, where), left, actions);
  const entry = { row, deadline: null };
  entry.update = (fresh) => {
    entry.deadline = typeof fresh.seconds_left === "number" && Number.isFinite(fresh.seconds_left) ? Date.now() + fresh.seconds_left * 1000 : null;
    if (entry.expired && (entry.deadline === null || fresh.seconds_left > 0)) { entry.expired = false; buttons(); }  // the host says it is still open
    entry.tick();
  };
  entry.tick = () => {
    const seconds = entry.deadline === null ? null : Math.max(0, Math.ceil((entry.deadline - Date.now()) / 1000));
    const [text, level] = countdownText(seconds);
    left.textContent = text;
    left.className = `countdown${level ? ` ${level}` : ""}`;
    if (seconds === 0 && !entry.expired) {
      entry.expired = true;
      clear(actions).append(el("span", { class: "hint" }, t("refused: no answer in time")));
    }
  };
  const answer = (decision, body, label) => async (event) => busy(event.target, async () => {
    try {
      const r = await api("POST", hostPath(`/approvals/${encodeURIComponent(q.id)}`, host), Object.assign({ decision }, body));
      toast("success", t("{answer}: {sandbox} on {host}", { answer: label, sandbox: where, host }), (r && r.output) || q.question || "");
      row.remove(); view.rows.delete(questionKey(q));
      const item = state.questionToasts.get(questionKey(q));
      if (item) { item.dismiss(); state.questionToasts.delete(questionKey(q)); }
      poll();
    } catch (e) { failed(decision === "allow" ? t("Could not allow the question of {sandbox} on {host}", { sandbox: where, host }) : t("Could not deny the question of {sandbox} on {host}", { sandbox: where, host }))(e); buttons(); }
  });
  // Deny: for the rest of the run (once when the user chose that, or all the question allows); always only as its own
  // "Block always…", asked first.
  let picked = false;  // a scope chosen by the user (the first, "this once", is only the allow's default)
  scope.addEventListener("change", () => { picked = true; });
  const denyScope = () => ((picked && chosen() === "once") || widest < 1 ? "once" : "run");
  const buttons = () => {
    if (entry.expired) return;
    clear(actions).append(...[scope,
      el("button", { class: "primary", onclick: () => askAllow(false) }, t("Allow")),
      link ? el("button", { title: t("allow without opening a browser on {host}: you opened the link yourself", { host }), onclick: () => askAllow(true) }, t("Opened it — allow")) : null,
      el("button", { class: "danger", onclick: (event) => answer("deny", { scope: denyScope() }, t("Denied {scope}", { scope: t(SCOPES[denyScope()]) }))(event) }, t("Deny")),
      widest >= 2 ? el("button", { class: "danger", onclick: () => askBlock() }, t("Block always…")) : null].filter(Boolean));
  };
  const askAllow = (noOpen) => clear(actions).append(...ask(
    t("Allow {sandbox} on {host}: {question}, {scope}{always}{noOpen}?", { sandbox: where, host, question: q.question || q.kind || t("this"), scope: t(SCOPES[chosen()]),
      always: chosen() === "always" && q.always ? ` (${q.always})` : "", noOpen: noOpen ? t(", without opening a browser there") : "" }),
    t("Yes, allow"), answer("allow", Object.assign({ scope: chosen() }, noOpen ? { no_open: true } : {}), t("Allowed {scope}", { scope: t(SCOPES[chosen()]) })), () => buttons(), "primary"));
  const askBlock = () => clear(actions).append(...ask(
    t("Block {sandbox} on {host} always: {question}? It is remembered for the project: every later question like it is refused without asking.", { sandbox: where, host, question: q.question || q.kind || t("this") }),
    t("Yes, block always"), answer("deny", { scope: "always", confirm: true }, t("Blocked always")), () => buttons()));
  buttons();
  entry.update(q);
  return entry;
}

function notificationsButton() {
  if (!window.Notification) return null;
  const label = () => (notificationsOn() ? t("Desktop notifications: on") : window.Notification.permission === "denied" ? t("Desktop notifications: blocked by the browser") : t("Desktop notifications…"));
  const button = el("button", { onclick: async () => {
    if (notificationsOn()) { try { localStorage.setItem("sandbox-ai-notify", ""); } catch (e) { /* kept for this page only */ } button.textContent = label(); return; }
    let permission = window.Notification.permission;
    try { if (permission === "default") permission = await window.Notification.requestPermission(); } catch (e) { permission = "denied"; }
    try { localStorage.setItem("sandbox-ai-notify", permission === "granted" ? "1" : ""); } catch (e) { /* as above */ }
    button.textContent = label();
  } }, label());
  return button;
}

// KNOWN: what poll() fetched already ({questions, errors}, or the Error of a failed check); else fetched here.
pages.approvals = async (main, known) => {
  const view = approvalsView;
  if (known === undefined || view.main !== main || !view.body || !view.body.isConnected) {
    view.main = main; view.rows = new Map();
    view.status = el("div"); view.body = el("tbody");
    view.table = el("table", {}, el("thead", {}, el("tr", {}, [t("Host"), t("Question"), t("Sandbox"), t("Time left"), ""].map(c => el("th", {}, c)))), view.body);
    view.empty = el("p", { class: "hint" }, t("No open questions."));
    clear(main).append(el("h1", {}, t("Approvals")),
      el("p", { class: "hint" }, t("What the sandboxes of every host ask. A question without an answer in its time is refused.")),
      el("div", { class: "row" }, notificationsButton()),
      view.status, view.table, view.empty);
    view.table.hidden = true; view.empty.hidden = true;
  }
  let found = known;
  if (found === undefined) {
    try {
      found = await api("GET", "/api/approvals");
      if (!found || !Array.isArray(found.questions)) throw new Error(t("not a list of questions"));
    } catch (e) { found = e; }
    noteApprovals(found);
  }
  if (state.page !== "approvals" || view.main !== main) return;
  clear(view.status);
  if (found instanceof Error || !found || !Array.isArray(found.questions)) {  // the rows shown stay: a failed check is no answer to them
    view.status.append(notice(t("Could not check for questions: {error}", { error: found && found.message ? found.message : found }), "error"));
    view.empty.hidden = true;
    return;
  }
  const errors = found.errors && typeof found.errors === "object" ? found.errors : {};
  for (const [host, why] of Object.entries(errors)) view.status.append(notice(t("Could not check {host}: {error}", { host, error: why }), "error"));

  const questions = found.questions.filter(q => q && typeof q === "object" && q.id !== undefined && typeof q.host === "string");
  const keys = new Set(questions.map(questionKey));
  for (const [key, entry] of view.rows) {
    // a host that could not be asked keeps its rows: no answer from it is no answer to them
    if (!keys.has(key) && !Object.prototype.hasOwnProperty.call(errors, key.split(" ")[0])) { entry.row.remove(); view.rows.delete(key); }
  }
  for (const q of questions) {
    const existing = view.rows.get(questionKey(q));
    if (existing) { existing.update(q); continue; }
    const entry = approvalRow(q, view);
    view.rows.set(questionKey(q), entry);
    view.body.append(entry.row);
  }
  view.table.hidden = !view.rows.size; view.empty.hidden = Boolean(view.rows.size) || Boolean(Object.keys(errors).length);
};

pages.doctor = async (main) => {
  main.append(el("h1", {}, t("Doctor on {host}", { host: state.host })), el("p", { class: "hint" }, t("Checking…")));
  const results = await api("GET", hostPath("/doctor"));
  const problems = results.filter(r => r.status === "fail").length, warnings = results.filter(r => r.status === "warn").length;
  clear(main).append(el("h1", {}, t("Doctor on {host}", { host: state.host })),
    notice(problems ? t("{problems} problem(s), {warnings} warning(s)", { problems, warnings }) : warnings ? t("no problems, {warnings} warning(s)", { warnings }) : t("no problems"), problems ? "error" : "ok"),
    table(["", t("column|Check"), t("Detail"), t("What to do")], results.map(r => el("tr", {}, statusCell(r.status), el("td", {}, r.check), el("td", {}, r.detail || ""), el("td", { class: "mono" }, r.hint || "")))));
};

pages.logs = async (main) => {
  main.append(el("h1", {}, t("Logs on {host}", { host: state.host })), el("p", { class: "hint" }, t("Loading…")));
  const data = await api("GET", hostPath("/logs"));
  clear(main).append(el("h1", {}, t("Logs on {host}", { host: state.host })));
  const summaries = Array.isArray(data) ? data : [data];
  for (const s of summaries) {
    main.append(el("div", { class: "card" },
      el("div", { class: "mono hint" }, s.directory || ""),
      el("p", {}, t("{denied} refused connection(s), {events} file event(s)", { denied: s.denied ?? 0, events: s.file_events ?? 0 })),
      (s.targets || []).length ? table([t("Refused destination"), t("Count")], s.targets.slice(0, 20).map(([target, n]) => el("tr", {}, el("td", { class: "mono" }, target), el("td", {}, n)))) : null,
      (s.suggestions || []).length ? el("div", {}, el("h2", {}, t("To allow them")), table([t("Option"), t("Why"), t("Count")], s.suggestions.map(x => el("tr", {}, el("td", { class: "mono" }, x.option), el("td", {}, x.why), el("td", {}, x.count))))) : null,
      el("details", {}, el("summary", {}, t("All of it (JSON)")), el("pre", {}, JSON.stringify(s, null, 2)))));
  }
};

pages.vpn = async (main) => {
  const profiles = await api("GET", hostPath("/vpn"));
  clear(main).append(el("h1", {}, t("VPN profiles on {host}", { host: state.host })),
    el("p", { class: "hint" }, t("A profile names the secret that holds its key or password ({command} on that host): no key passes through this page.", { command: "sandbox-ai secrets set NAME" })));
  const host = state.host;
  const rows = profiles.map(p => {
    const actions = el("td", { class: "actions" },
      el("button", { onclick: async (event) => busy(event.target, async () => {
        try { toast("success", t("{profile} works", { profile: p.profile }), (await api("POST", hostPath(`/vpn/${encodeURIComponent(p.profile)}/check`), {})).output || "ok"); }
        catch (e) { failed(t("The check of {profile} failed", { profile: p.profile }))(e); } }) }, t("Check")),
      el("button", { class: "danger", onclick: () => confirmIn(actions, t("Remove the VPN profile {profile} from {host}? Starts that use it ({option}) cannot start until it is added again.", { profile: p.profile, host, option: `--vpn ${p.profile}` }), t("Yes, remove it"), async () => {
        try { await api("DELETE", hostPath(`/vpn/${encodeURIComponent(p.profile)}`, host)); show("vpn"); } catch (e) { failed(t("Could not remove {what} from {where}", { what: p.profile, where: host }))(e); }
      }) }, t("Remove")));
    return el("tr", {}, el("td", { class: "mono" }, p.profile), el("td", {}, p.type || ""), el("td", {}, p.provider || ""), actions);
  });
  main.append(table([t("Profile"), t("Kind"), t("Provider"), ""], rows, t("No VPN profiles on this host.")));
  const name = el("input", { id: "vpn-name", placeholder: "home", pattern: "[a-z0-9][a-z0-9_-]{0,31}" });
  const kind = el("select", { id: "vpn-kind" }, [["wireguard", N_("WireGuard (a wg-quick file)")], ["openvpn", N_("OpenVPN (an .ovpn file)")],
    ["mullvad", N_("Mullvad (account number in a secret)")], ["tailscale", N_("Tailscale or Headscale (auth key in a secret)")]].map(([v, label]) => el("option", { value: v }, t(label))));
  const file = el("input", { id: "vpn-file", class: "mono", placeholder: t("{path} (on that host)", { path: "/home/you/vpn/home.conf" }) });
  const secret = el("input", { id: "vpn-secret", class: "mono", placeholder: "VPN_HOME" });
  const location = el("input", { id: "vpn-location", placeholder: "se-sto" });
  const exitNode = el("input", { id: "vpn-exit", placeholder: "exit-node-1" });
  const loginServer = el("input", { id: "vpn-login", placeholder: "https://headscale.example.org" });
  const rows2 = { file: [file, N_("Configuration file")], secret: [secret, N_("Secret (its name)")], location: [location, N_("Location")],
    exit_node: [exitNode, N_("Exit node")], login_server: [loginServer, N_("Login server")] };
  const wanted = { wireguard: ["file"], openvpn: ["file", "secret"], mullvad: ["secret", "location"], tailscale: ["secret", "exit_node", "login_server"] };
  const form = el("div", { class: "form" }, el("label", { for: "vpn-name" }, t("Name")), name, el("label", { for: "vpn-kind" }, t("Kind")), kind);
  const labels = {};
  for (const [key, [input, text]] of Object.entries(rows2)) { labels[key] = el("label", { for: input.id }, t(text)); form.append(labels[key], input); }
  const update = () => { for (const key of Object.keys(rows2)) { const on = wanted[kind.value].includes(key); rows2[key][0].hidden = !on; labels[key].hidden = !on; } };
  kind.addEventListener("change", update);
  update();
  form.append(el("span"), el("button", { class: "primary", onclick: async (event) => busy(event.target, async () => {
    const body = { name: name.value.trim(), kind: kind.value };
    for (const key of wanted[kind.value]) { const value = rows2[key][0].value.trim(); if (value) body[key] = value; }
    try { await api("POST", hostPath("/vpn"), body); show("vpn"); } catch (e) { failed(t("Could not add the VPN profile"))(e); }
  }) }, t("Add profile")));
  main.append(el("h2", {}, t("New profile")), el("div", { class: "card" }, form));
};

// llm models --json: [{name, size, loaded, details}], as a table
function modelsTable(server, models) {
  const list = (Array.isArray(models) ? models : []).filter(m => m && typeof m === "object");
  const loaded = { true: N_("yes"), false: N_("no") };
  return el("div", {}, el("h3", {}, t("Models of {server}", { server })),
    table([t("Name"), t("Size"), t("Loaded"), t("Details")], list.map(m => el("tr", {}, el("td", { class: "mono" }, String(m.name ?? "")),
      el("td", {}, typeof m.size === "number" ? readableSize(m.size) : "?"), el("td", {}, loaded[String(m.loaded)] ? t(loaded[String(m.loaded)]) : "?"), el("td", {}, String(m.details || "")))),
    t("The server lists no models.")));
}

pages.llm = async (main) => {
  main.append(el("h1", {}, t("LLM servers on {host}", { host: state.host })));
  const list = await api("GET", hostPath("/llm"));
  const output = el("div");
  const host = state.host;
  const rows = (Array.isArray(list) ? list : []).map(e => {
    const agent = el("select", { "aria-label": t("Agent") }, el("option", { value: "" }, t("every agent")), AGENTS.map(a => el("option", { value: a }, a)));
    const actions = el("td", { class: "actions" });
    const row = el("tr", {}, el("td", { class: "mono" }, e.name), el("td", {}, e.kind), el("td", { class: "mono" }, e.url), el("td", { class: "mono" }, e.model || ""), actions);
    actions.append(agent,
        el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try { const r = await api("POST", hostPath(`/llm/${encodeURIComponent(e.name)}/check`), agent.value ? { agent: agent.value } : {}); output.append(notice(r.ok ? t("the server works for it") : t("the check found problems"), r.ok ? "ok" : "error"), el("pre", {}, r.output)); }
          catch (err) { output.append(notice(err.message, "error")); }
        }) }, t("Check")),
        el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try { output.append(modelsTable(e.name, await api("GET", hostPath(`/llm/${encodeURIComponent(e.name)}/models`)))); }
          catch (err) { output.append(notice(err.message, "error")); }
        }) }, t("Models")),
        el("button", { class: "danger", onclick: () => confirmIn(actions, t("Forget the LLM server {name} on {host}? Starts with {option} cannot start until it is added again.", { name: e.name, host, option: `--llm ${e.name}` }), t("Yes, forget it"), async () => {
          try { await api("DELETE", hostPath(`/llm/${encodeURIComponent(e.name)}`, host)); show("llm"); } catch (err) { failed(t("Could not forget {name} on {host}", { name: e.name, host }))(err); }
        }) }, t("Remove")));
    return row;
  });
  main.append(table([t("Name"), t("Kind"), "URL", t("Model"), ""], rows, t("No LLM server yet.")), output);
  const name = el("input", { id: "llm-name", placeholder: "lmstudio" });
  const kind = el("select", { id: "llm-kind" }, ["lmstudio", "ollama", "llamacpp", "vllm", "openai-compatible"].map(k => el("option", { value: k }, k)));
  const url = el("input", { id: "llm-url", class: "mono", placeholder: "http://192.168.1.20:1234" });
  const model = el("input", { id: "llm-model", class: "mono", placeholder: t("optional") });
  const status = el("div", { class: "wide" });
  main.append(el("h2", {}, t("Add a server")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "llm-name" }, t("Name")), name, el("label", { for: "llm-kind" }, t("Kind")), kind,
    el("label", { for: "llm-url" }, "URL"), url, el("label", { for: "llm-model" }, t("Model")), model,
    el("span"), el("button", { class: "primary", onclick: (ev) => busy(ev.target, async () => {
      clear(status);
      try { await api("POST", hostPath("/llm"), { name: name.value.trim(), kind: kind.value, url: url.value.trim(), model: model.value.trim() }); show("llm"); }
      catch (e) { status.append(notice(e.message, "error")); }
    }) }, t("Add")), status),
    el("p", { class: "hint" }, t("Then start an agent with {option} (Options of a new start). A key is set on the server: {secret}, then {add}.", { option: "--llm NAME", secret: "sandbox-ai secrets set NAME", add: "sandbox-ai llm add … --api-key-secret NAME" }))));

};

// Kits (sandbox-ai kits on that host): what each brings, and adding to a kit of yours. sandbox-ai's own kits are
// read-only: with one of them (or none) chosen, what is added goes to a kit of yours, or a new one created first.
const KIT_KINDS = { always: N_("always (sandbox-ai)"), default: N_("always (default kit)"), builtin: N_("built-in"), user: N_("yours") };

pages.kits = async (main) => {
  main.append(el("h1", {}, t("Kits on {host}", { host: state.host })), el("p", { class: "hint" }, t("Loading…")));
  const data = await api("GET", hostPath("/kits"));
  const host = state.host;
  const kits = (Array.isArray(data.kits) ? data.kits : []).filter(k => k && typeof k.name === "string");
  const mine = kits.filter(k => k.kind === "user").map(k => k.name).sort();
  clear(main).append(el("h1", {}, t("Kits on {host}", { host: state.host })),
    el("p", { class: "hint mono" }, data.kits_dir || ""),
    el("p", { class: "hint" }, t("Every change runs sandbox-ai kits on that host, with its checks. sandbox-ai's own and the built-in kits are read-only: what you add goes to a kit of yours.")));
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
      if (!entries.length) line.append(el("span", { class: "hint" }, t("nothing")));
      for (const [kind, value] of entries) {
        line.append(el("span", { class: "mono" }, `${kind} ${value}`));
        const holder = el("span", { class: "inline-ask" });
        if (own) line.append(" ", holder);
        const label = t("Remove the {kind} {value} for {agent}", { kind, value, agent });
        holder.append(el("button", { class: "danger small", title: label, "aria-label": label,
          onclick: () => confirmIn(holder, t("Remove the {kind} {value} for {agent} from the kit {kit} on {host}?", { kind, value, agent, kit: k.name, host }), t("Yes, remove it"), async () => {
            const body = kind === "skill" ? { skill: value, agents: [agent] } : { plugin: value, agent };
            try { result(t("{kit}: removed {value}", { kit: k.name, value }), await api("POST", hostPath(`/kits/${encodeURIComponent(k.name)}/remove-${kind}`, host), body)); show("kits"); }
            catch (e) { failed(t("Could not remove {value}", { value }))(e); }
          }) }, "×"));
        line.append("  ");
      }
      if (info.error) line.append(el("span", { class: "status-bad" }, ` ✘ ${info.error}`));
      brings.append(line);
    }
    const kitActions = el("td", { class: "actions" });
    if (own) kitActions.append(el("button", { class: "danger", onclick: () => confirmIn(kitActions, t("Delete the kit {kit} on {host} with everything in it? Projects that use it cannot start until {option} is removed.", { kit: k.name, host, option: `--kit ${k.name}` }), t("Yes, delete it"), async () => {
      try { result(t("{kit} deleted", { kit: k.name }), await api("DELETE", hostPath(`/kits/${encodeURIComponent(k.name)}`, host), { confirm: true })); show("kits"); }
      catch (e) { failed(t("Could not delete {kit}", { kit: k.name }))(e); }
    }) }, t("Delete kit")));
    return el("tr", {}, el("td", { class: "mono" }, k.name), el("td", {}, KIT_KINDS[k.kind] ? t(KIT_KINDS[k.kind]) : String(k.kind || "")), brings, kitActions);
  });
  main.append(table([t("Kit"), t("Kind"), t("Brings"), ""], rows, t("No kits on this host.")), output);

  // a new kit
  const newName = el("input", { id: "kit-new-name", placeholder: "my-kit", pattern: "[A-Za-z0-9][A-Za-z0-9_.-]{0,63}" });
  const agentBoxes = (prefix) => AGENTS.map(a => el("label", {}, el("input", { type: "checkbox", value: a, checked: true, id: `${prefix}-${a}` }), " ", a));
  const chosenAgents = (box) => [...box.querySelectorAll("input[type=checkbox]")].filter(b => b.checked).map(b => b.value);
  const newAgents = el("div", { class: "row" }, agentBoxes("kit-new"));
  const newStatus = el("div", { class: "wide" });
  main.append(el("h2", {}, t("New kit")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "kit-new-name" }, t("Name")), newName, el("span", {}, t("For")), newAgents,
    el("span"), el("button", { class: "primary", onclick: (ev) => busy(ev.target, async () => {
      clear(newStatus);
      try { result(t("Kit {kit} created", { kit: newName.value.trim() }), await api("POST", hostPath("/kits"), { name: newName.value.trim(), agents: chosenAgents(newAgents) })); show("kits"); }
      catch (e) { newStatus.append(notice(e.message, "error")); }
    }) }, t("Create")), newStatus)));

  // adding to a kit of yours (or a new one)
  const target = el("select", { id: "kit-target" }, mine.map(n => el("option", { value: n }, n)), el("option", { value: "" }, t("a new kit…")));
  const targetName = el("input", { id: "kit-target-name", placeholder: "my-kit", value: mine.length ? "" : "my-kit" });
  const targetLabel = el("label", { for: "kit-target-name" }, t("New kit's name"));
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
  const folder = el("input", { id: "kit-skill-path", class: "mono", placeholder: t("{path} (on that host, with SKILL.md)", { path: "/home/you/skills/review" }) });
  const folderAs = el("input", { id: "kit-skill-as", placeholder: t("the folder's name") });
  const skillName = el("input", { id: "kit-skill-name", placeholder: "release-notes", pattern: "[a-z0-9][a-z0-9-]{0,63}" });
  const skillDescription = el("input", { id: "kit-skill-description", placeholder: t("What it does and when the agent should use it (one line)") });
  const skillAgents = el("div", { class: "row" }, agentBoxes("kit-skill"));
  const plugin = el("input", { id: "kit-plugin", class: "mono", placeholder: t("{plugin}, or an npm package for OpenCode", { plugin: "sandcastle@sandbox-ai" }) });
  const pluginAgent = el("select", { id: "kit-plugin-agent" }, ["claude", "opencode"].map(a => el("option", { value: a }, a)));
  const pluginSource = el("input", { id: "kit-plugin-source", class: "mono", placeholder: t("OWNER/REPO or https://… (only for a marketplace the kit does not declare yet)") });
  main.append(el("h2", {}, t("Add to a kit of yours")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "kit-target" }, t("Kit")), target, targetLabel, targetName,
    el("span", {}, t("For")), skillAgents,
    el("h3", { class: "wide" }, t("A skill from a folder")),
    el("label", { for: "kit-skill-path" }, t("Folder")), folder, el("label", { for: "kit-skill-as" }, t("Name in the kit")), folderAs,
    el("span"), el("button", { class: "primary", onclick: send("add-skill", () => ({ path: folder.value.trim(), as: folderAs.value.trim(), agents: chosenAgents(skillAgents) }), n => t("Skill copied into {kit}", { kit: n })) }, t("Add skill from the folder")),
    el("p", { class: "hint wide" }, t("The kit gets a copy: links are not followed, special files are skipped. A folder where sandboxes can write is copied with a note: review it first.")),
    el("h3", { class: "wide" }, t("A new skill")),
    el("label", { for: "kit-skill-name" }, t("Name")), skillName, el("label", { for: "kit-skill-description" }, t("Description")), skillDescription,
    el("span"), el("button", { class: "primary", onclick: send("new-skill", () => ({ skill: skillName.value.trim(), description: skillDescription.value.trim(), agents: chosenAgents(skillAgents) }), n => t("New skill in {kit}", { kit: n })) }, t("Write the skill (SKILL.md)")),
    el("h3", { class: "wide" }, t("A plugin")),
    el("label", { for: "kit-plugin" }, t("Plugin")), plugin, el("label", { for: "kit-plugin-agent" }, t("Agent")), pluginAgent,
    el("label", { for: "kit-plugin-source" }, t("Marketplace source")), pluginSource,
    el("span"), el("button", { class: "primary", onclick: send("add-plugin", () => ({ plugin: plugin.value.trim(), agent: pluginAgent.value, source: pluginSource.value.trim() }), n => t("Plugin added to {kit}", { kit: n })) }, t("Add plugin")),
    addStatus),
    el("p", { class: "hint" }, t("Use a kit with {option} (Options of a new start). Claude plugins are installed with {command} on that host.", { option: "--kit NAME", command: "sandbox-ai plugins claude" }))));

};

// the canary log: every opened decoy or token that left, by which process, with the agent's tool call, its prompt
// turn and the model requests around it (sandbox-ai canaries log on that host; redacted there, tokens never shown)
function canaryProcess(p) {
  if (!p || p.unknown !== undefined || !Object.keys(p).length) return `${t("unknown")}${p && p.unknown ? ` (${p.unknown})` : ""}`;
  let text = `${p.comm || "?"} (pid ${p.pid ?? "?"}${p.ns_pid !== undefined && p.ns_pid !== p.pid ? t(", {pid} in the sandbox", { pid: p.ns_pid }) : ""})`;
  if (Array.isArray(p.argv) && p.argv.length) text += `: ${p.argv.join(" ")}`;
  if (Array.isArray(p.parents) && p.parents.length) text += ` ← ${p.parents.slice(0, 3).join(" ← ")}`;
  return text;
}

function canaryLinks(e) {
  const items = [];
  const when = (time) => (time || "").replace("T", " ").replace("Z", "");
  for (const call of e.tool_calls || []) items.push(el("li", {}, t("tool call ({match}): ", { match: call.match }), el("span", { class: "mono" }, call.summary || ""), ` — ${when(call.time)}, ${t("activity {id}", { id: call.id })}`));
  if (e.turn) items.push(el("li", {}, t("prompt turn: "), el("span", {}, e.turn.summary || ""), ` — ${when(e.turn.time)}, ${t("activity {id}", { id: e.turn.id })}`));
  for (const r of e.requests || []) items.push(el("li", { class: r.position === "carried the token" ? "status-bad" : "" }, t("model request ({position}): ", { position: r.position }),
    el("span", { class: "mono" }, `${r.method || ""} ${r.url || r.host || ""}${r.status ? ` → ${r.status}` : ""}`),
    ` — ${when(r.time)}, ${r.activity_id ? t("activity {id}", { id: r.activity_id }) : `${r.log} @${r.offset}`}`));
  return items;
}

pages.canaries = async (main) => {
  main.append(el("h1", {}, t("Canaries on {host}", { host: state.host })), el("p", { class: "hint" }, t("Loading…")));
  let logs;
  try { logs = await api("GET", hostPath("/canaries")); }
  catch (e) { clear(main).append(el("h1", {}, t("Canaries on {host}", { host: state.host })), notice(e.status === 502 ? t("{error} (an older sandbox-ai there has no canaries log: {command})", { error: e.message, command: "sandbox-ai update" }) : e.message, "error")); return; }
  clear(main).append(el("h1", {}, t("Canaries on {host}", { host: state.host })),
    el("p", { class: "hint" }, t("Every decoy file opened and every canary token that left a sandbox: by which process (for a file, the one the supervisor found holding it open; inotify itself names nobody), with the agent's tool call, its prompt turn and the model requests around it.")));
  const events = (Array.isArray(logs) ? logs : []).flatMap(log => (log.events || []).map(e => Object.assign({ agent: log.agent, activity: log.activity }, e)));
  if (!events.length) { main.append(el("p", { class: "hint" }, t("No canary was touched on this host."))); return; }
  const rows = [];
  for (const e of events.slice().reverse()) {
    const links = canaryLinks(e);
    const detail = el("tr", { class: "canary-links hidden" }, el("td", { colspan: 7 },
      links.length ? el("ul", {}, links) : el("p", { class: "hint" }, e.activity ? t("Nothing of the agent's in that time window.") : t("No activity timeline for this run: nothing to link.")),
      (e.processes || []).length > 1 ? el("p", { class: "hint" }, t("also open in: {processes}", { processes: e.processes.slice(1).map(canaryProcess).join("; ") })) : null));
    const toggle = el("button", { "aria-expanded": "false", onclick: () => { const open = detail.classList.toggle("hidden"); toggle.setAttribute("aria-expanded", String(!open)); toggle.textContent = open ? t("Show links") : t("Hide links"); } }, t("Show links"));
    rows.push(el("tr", { class: "canary-event" },
      el("td", {}, (e.time || "").replace("T", " ").replace("Z", "")), el("td", { class: "mono" }, e.run || ""),
      el("td", { class: "status-bad" }, `${e.event || "?"}${e.action ? ` (${e.action})` : ""}`),
      el("td", { class: "mono" }, `${e.canary || "-"} ${e.path || ""}`),
      el("td", {}, e.summary || e.error || ""), el("td", { class: "mono" }, canaryProcess(e.process)),
      el("td", { class: "actions" }, toggle)), detail);
  }
  main.append(table([t("Time"), t("Run"), t("Event"), t("Canary"), t("What"), t("Process"), ""], rows));
};

// What lowers the protection of every later start on a host (the mode lax or low, the agents' telemetry on, fewer
// canary files) is asked first, in place, naming the host; Cancel puts the control back. The server wants the
// confirmation as well.
const LOW_MODES = ["lax", "low"];

pages.settings = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Defaults on {host}", { host })));
  const data = await api("GET", hostPath("/defaults"));
  const values = data.values || {};
  const status = el("div");
  const save = async (key, value, confirmed) => {
    clear(status);
    try { await api("POST", hostPath("/defaults", host), Object.assign({ key, value }, confirmed ? { confirm: true } : {})); status.append(notice(t("{key} saved on {host}", { key, host }), "ok")); return true; }
    catch (e) { status.append(notice(e.message, "error")); return false; }
  };
  // CONTROL changed: saved at once, or asked first (WEAKER) with ASKING in the place next to it
  const change = (asking, weaker, text, key, value, revert, accept) => {
    if (!weaker) { save(key, value).then(ok => (ok ? accept() : revert())); return; }
    confirmIn(asking, text, t("Yes, lower it"), async () => { if (await save(key, value, true)) accept(); else revert(); }, { onCancel: revert });
  };
  let telemetryNow = values.telemetry || "off", modeNow = values.mode || "medium";
  const telemetry = el("select", { id: "telemetry" },
    [["off", N_("off: no usage statistics, error reports, feedback or update checks (recommended)")], ["strict", N_("strict: also no other calls than the model's")], ["on", N_("on: the agents' own settings")]]
      .map(([v, label]) => el("option", { value: v, selected: telemetryNow === v }, t(label))));
  const telemetryAsk = el("div", { class: "row" });
  telemetry.addEventListener("change", () => {
    const value = telemetry.value;
    change(telemetryAsk, value === "on", t("Turn the agents' telemetry on for every later start on {host}? Their usage statistics, error reports and update checks reach their vendors again.", { host }),
      "telemetry", value === "off" ? null : value, () => { telemetry.value = telemetryNow; }, () => { telemetryNow = value; });
  });
  const mode = el("select", { id: "mode" }, ["lax", "low", "medium", "high", "paranoid"].map(m => el("option", { value: m, selected: modeNow === m }, m)));
  const modeAsk = el("div", { class: "row" });
  mode.addEventListener("change", () => {
    const value = mode.value;
    change(modeAsk, LOW_MODES.includes(value), t("Make {mode} the security mode of every later start on {host}? It allows more than medium (see {command} there); an option given at a start still wins.", { mode: value, host, command: "sandbox-ai modes" }),
      "mode", value === "medium" ? null : value, () => { mode.value = modeNow; }, () => { modeNow = value; });
  });
  const categories = data.canary_categories || ["credentials", "home", "workspace"];
  let canariesNow = values.canaries === false ? [] : (values.canaries || data.canary_default || []);
  const boxes = categories.map(c => el("input", { type: "checkbox", id: `canary-${c}`, value: c, checked: canariesNow.includes(c) }));
  const canaryAsk = el("div", { class: "row" });
  for (const box of boxes) box.addEventListener("change", () => {
    const picked = boxes.filter(b => b.checked).map(b => b.value);
    const fewer = canariesNow.some(c => !picked.includes(c));
    const revert = () => { for (const b of boxes) b.checked = canariesNow.includes(b.value); };
    change(canaryAsk, fewer || !picked.length, picked.length ? t("Put no {categories} canary files into later starts on {host}? What opens them there is no longer noticed.", { categories: canariesNow.filter(c => !picked.includes(c)).join(t(" and ")), host })
      : t("Put no canary files at all into later starts on {host}? A decoy opened or a token sent away is no longer noticed.", { host }),
    "canaries", picked.length ? picked : false, revert, () => { canariesNow = picked; });
  });
  main.append(status, el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "telemetry" }, t("Telemetry of the agents")), telemetry, el("span"), telemetryAsk,
    el("label", { for: "mode" }, t("Security mode")), mode, el("span"), modeAsk,
    el("span", {}, t("Canary files")), el("div", { class: "row" }, boxes.map((b, i) => el("label", {}, b, " ", categories[i]))), el("span"), canaryAsk),
    el("p", { class: "hint mono" }, data.path || ""),
    el("p", { class: "hint" }, t("For every start on this host; an option given at a start wins."))));
};

// The version of a host's sandbox-ai and whether an update is there (hosts list's status; null: not asked yet).
function versionText(s) {
  if (!s) return "…";
  if (!s.version) return s.error ? t("unreachable") : "?";
  if (["checkout", "pip", "package"].includes(s.kind)) return `${s.version} (${s.kind})`;
  if (s.update) return t("{version} → {available} available", { version: s.version, available: s.available });
  return s.update === false ? t("{version}, up to date", { version: s.version }) : s.version;
}

// What a key made private (ssh refuses key files others can read) and what was only named.
function keyNotices(r) {
  return [...(r.fixed || []).map(f => notice(t("{path} could be read by others (mode {was}), which ssh refuses: made private ({now}).", { path: f.path, was: f.was, now: f.now }), "ok")),

    ...(r.warnings || []).map(w => notice(w, "warn"))];
}

// One host's update: its own signed sandbox-ai update; a sudo that wants a password is asked in a terminal tab.
async function updateHost(name, output, sudo) {
  const body = sudo === undefined ? {} : { sudo };
  const r = await api("POST", `/api/hosts/${encodeURIComponent(name)}/update`, body);
  output.append(...keyNotices(r));
  if (r.changed) output.append(notice(`${name}: ${r.before || "?"} → ${r.after}${r.sudo ? " (sudo)" : ""}`, "ok"));
  else if (r.error && !r.after) {
    output.append(el("div", { class: "notice error", role: "alert" }, t("{host}: not updated: {error}", { host: name, error: r.error }),
      r.needs_password ? el("button", { onclick: () => openTerminal({ host: name, kind: "update" }, t("update {host}", { host: name })) }, t("Update in a terminal (sudo asks there)")) : null,
      r.needs_sudo ? el("button", { onclick: (ev) => busy(ev.target, () => updateHost(name, output, true).catch(failed(t("Could not update {host}", { host: name })))) }, t("Update with sudo")) : null));
  } else output.append(notice(`${name}: ${t("{version}, up to date", { version: r.after || r.before })}${r.error ? ` (${r.error})` : ""}`, r.error ? "warn" : "ok"));
  for (const note of r.notes || []) output.append(notice(`${name}: ${note}`, "warn"));
  return r;
}

pages.hosts = async (main) => {
  main.append(el("h1", {}, t("Hosts")), el("p", { class: "hint" }, t("Machines (or LXC containers) whose sandbox-ai this service manages over SSH, with your keys; nothing listens on them but sshd.")));
  const output = el("div", { "aria-live": "polite" });
  const versions = {};
  const versionCells = {};
  const showVersions = (found) => {
    Object.assign(versions, found);
    for (const [name, cell] of Object.entries(versionCells)) cell.textContent = versionText(versions[name]);
  };
  const loadVersions = async (refresh) => {
    try { showVersions(await api(refresh ? "POST" : "GET", "/api/hosts-status", refresh ? {} : undefined)); }
    catch (e) { for (const cell of Object.values(versionCells)) cell.textContent = "?"; output.append(notice(t("Versions: {error}", { error: e.message }), "error")); }
  };
  const remote = state.hosts.filter(h => h.kind !== "local");
  const rows = state.hosts.map(h => {
    const version = el("td", { class: "mono" }, h.kind === "local" ? t("this machine") : "…");
    if (h.kind !== "local") versionCells[h.name] = version;
    return el("tr", {}, el("td", { class: "mono" }, h.name), el("td", { class: "mono" }, h.kind === "local" ? t("this machine") : `${h.ssh}${h.port && h.port !== 22 ? ":" + h.port : ""}`),
      version, el("td", {}, h.description || "", h.update_sudo ? el("span", { class: "hint" }, " ", t("(updates with sudo)")) : null), el("td", { class: "actions" },
        el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try {
            const r = await api("POST", `/api/hosts/${encodeURIComponent(h.name)}/test`);
            output.append(...keyNotices(r));
            output.append(notice(r.error ? `${h.name}: ${r.error}` : `${h.name}: ${r.version}${(r.fixed || []).length ? t(" (tried again after making the key private)") : ""}`, r.error ? "error" : "ok"));
            if (r.doctor) output.append(table(["", t("column|Check"), t("Detail")],
 r.doctor.filter(d => d.status !== "ok").map(d => el("tr", {}, statusCell(d.status), el("td", {}, d.check), el("td", {}, d.detail || ""))), t("The doctor found nothing to do.")));
          } catch (e) { output.append(notice(e.message, "error")); }
        }) }, t("Test")),
        h.kind === "local" ? null : el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          output.append(notice(t("Updating {host}…", { host: h.name })));
          try { await updateHost(h.name, output); } catch (e) { output.append(notice(`${h.name}: ${e.message}`, "error")); }
          loadVersions(false);
        }) }, t("Update")),
        h.kind === "local" ? null : el("button", { class: "danger", onclick: (ev) => confirmIn(ev.target.parentNode, t("Remove {host} ({ssh}) from the list? Nothing changes on it; this service no longer manages it.", { host: h.name, ssh: h.ssh }), t("Yes, remove it"), async () => {
          try { await api("DELETE", `/api/hosts/${encodeURIComponent(h.name)}`); render(); } catch (e) { failed(t("Could not remove {value}", { value: h.name }))(e); }
        }) }, t("Remove"))));
  });
  const progress = el("progress", { max: Math.max(remote.length, 1), value: 0, hidden: true, "aria-label": t("Update progress") });
  const updateAll = el("button", { class: "primary", disabled: !remote.length, onclick: (ev) => busy(ev.target, async () => {
    clear(output);
    progress.hidden = false; progress.value = 0;
    let updated = 0, failedCount = 0;
    for (const h of remote) {
      output.append(notice(t("Updating {host} ({number} of {count})…", { host: h.name, number: progress.value + 1, count: remote.length })));
      try { const r = await updateHost(h.name, output); if (r.changed) updated++; else if (r.error && !r.after) failedCount++; }
      catch (e) { failedCount++; output.append(notice(`${h.name}: ${e.message}`, "error")); }
      progress.value += 1;
    }
    output.append(notice(t("{count} hosts: {updated} updated, {current} up to date, {failed} not updated", { count: remote.length, updated, current: remote.length - updated - failedCount, failed: failedCount }), failedCount ? "warn" : "ok"));
    progress.hidden = true;
    loadVersions(false);
  }) }, t("Update all"));
  const check = el("button", { disabled: !remote.length, onclick: (ev) => busy(ev.target, () => loadVersions(true)) }, t("Check versions"));
  main.append(table([t("Name"), "SSH", "sandbox-ai", t("Description"), ""], rows),
    el("div", { class: "actions" }, updateAll, check, progress),
    el("p", { class: "hint" }, t("Update runs each host's own sandbox-ai update (signed, as install.sh installed it); a system installation updates with {sudo}, and when sudo wants a password a terminal tab asks for it. This machine updates with: {command}.", { sudo: "sudo -n", command: "sandbox-ai update" })),
    output);
  if (remote.length) loadVersions(false);
  const fields = { name: el("input", { id: "host-name", placeholder: "ct101" }), ssh: el("input", { id: "host-ssh", class: "mono", placeholder: "user@192.168.1.101" }),
    port: el("input", { id: "host-port", type: "number", min: 1, max: 65535, placeholder: "22" }), identity: el("input", { id: "host-identity", class: "mono", placeholder: t("{path} (optional)", { path: "~/.ssh/id_ed25519" }) }),
    description: el("input", { id: "host-description", placeholder: "Proxmox CT 101" }), update_sudo: el("input", { id: "host-update-sudo", type: "checkbox" }) };
  const status = el("div", { class: "wide" });
  main.append(el("h2", {}, t("Add a host")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "host-name" }, t("Name")), fields.name, el("label", { for: "host-ssh" }, "SSH"), fields.ssh,
    el("label", { for: "host-port" }, t("Port")), fields.port, el("label", { for: "host-identity" }, t("Key")), fields.identity,
    el("label", { for: "host-description" }, t("Description")), fields.description,
    el("label", { for: "host-update-sudo" }, t("Update with sudo")), fields.update_sudo,
    el("span"), el("button", { class: "primary", onclick: (ev) => busy(ev.target, async () => {
      clear(status);
      const body = { name: fields.name.value.trim(), ssh: fields.ssh.value.trim(), identity: fields.identity.value.trim(), description: fields.description.value.trim() };
      if (fields.port.value) body.port = Number(fields.port.value);
      if (fields.update_sudo.checked) body.update_sudo = true;
      try {
        const r = await api("POST", "/api/hosts", body);
        render();
        // toasts: they outlast the page drawn anew
        for (const f of r.fixed || []) toast("success", t("Key of {host} made private", { host: r.name }), t("{path} could be read by others (mode {was}), which ssh refuses: now {now}.", { path: f.path, was: f.was, now: f.now }));
        for (const w of r.warnings || []) toast("warning", t("Keys of {host}", { host: r.name }), w);
        if (r.connection) toast(r.connection.ok ? "success" : "error", t("{host}: tried again", { host: r.name }), r.connection.ok ? t("The connection works: {version}", { version: r.connection.version }) : r.connection.error);
      } catch (e) { status.append(notice(e.message, "error")); }
    }) }, t("Add")), status),
    el("p", { class: "hint" }, t("The service's user needs SSH access with a key (ssh-copy-id), and sandbox-ai installed there. The host key is learnt at the first contact and checked from then on. A key file others can read (ssh refuses it) is made private when the host is added or tested."))));
};

// --- terminals -------------------------------------------------------------------------------------------

// Why a start has no terminal to attach to (list --json's attach_why; guessed for an older sandbox-ai without it).
// The desktop app says the same.
const ATTACH_WHY = {
  foreground: N_("runs in the foreground of its own terminal (started with --no-tmux, as a task, or without a terminal): nothing to attach; start with tmux (the default for interactive starts) to attach from here"),
  paused: N_("paused: resume it (Starts and sessions) to attach"),
  mcp: N_("an MCP server sandbox: it has no terminal of its own"),
  stopped: N_("does not run"),
  damaged: N_("damaged: see sandbox-ai list on its host"),
};

function attachWhy(s) {
  const why = s.attach_why || (s.kind === "mcp" ? "mcp" : s.kind === "run" ? "foreground" : s.state === "paused" ? "paused" : "");
  return t(ATTACH_WHY[why] || N_("nothing to attach"));
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
  box.querySelector("summary").textContent = t("Running on {host} ({count})", { host, count: running.length });
  // Unchanged since the last read: the table stays (no flicker under the pointer).
  const shown = JSON.stringify([host, running.map(s => [s.name, s.agent, s.kind, s.state, s.workspace, s.attachable, s.attach_why]),
    state.terminals.filter(entry => !entry.closed).map(entry => entry.key)]);
  if (box.dataset.shown === shown) return;
  box.dataset.shown = shown;
  clear(list);
  const rows = running.map(s => {
    const open = state.terminals.find(entry => entry.key === terminalKey(host, s.name) && !entry.closed);
    const action = s.attachable
      ? el("td", { class: "actions" }, el("button", { class: open ? "" : "primary", onclick: () => openTerminal({ kind: "attach", name: s.name }, s.name) }, open ? t("Show") : t("Open terminal")))
      : el("td", { class: "hint" }, attachWhy(s));
    return el("tr", {}, el("td", { class: "mono" }, s.name), el("td", {}, s.agent || s.kind || ""), statusCell(s.state),
      el("td", { class: "mono" }, s.workspace || ""), action);
  });
  list.append(table([t("Name"), t("Agent"), t("State"), t("Workspace"), ""], rows, t("Nothing runs on this host.")));
}

// The terminals' own boxes live on from one drawing of the page to the next (another page, a host added: the
// frame is drawn anew), so that an open terminal is still there when its page is shown again.
function terminalBoxes() {
  if (!state.boxes) state.boxes = el("div", { class: "terminal-boxes" });
  return state.boxes;
}

pages.terminals = async (main) => {
  if (!main.querySelector(".terminal-tabs")) {
    clear(main).append(el("h1", {}, t("Terminals")),
      el("details", { class: "terminal-starts", open: !state.terminals.length }, el("summary", {}, t("Running on {host}", { host: state.host })),
        el("div", { class: "terminal-starts-list" }, el("p", { class: "hint" }, t("Loading…")))),
      el("div", { class: "terminal-bar" }, el("div", { class: "terminal-tabs", role: "tablist", "aria-label": t("Terminals") }), el("div", { class: "terminal-close" })),
      terminalBoxes());
  } else if (terminalBoxes().parentNode !== main) main.append(terminalBoxes());
  for (const t of state.terminals) if (t.box.parentNode !== terminalBoxes()) terminalBoxes().append(t.box);
  refreshTerminalStarts(main);
  const tabs = clear(main.querySelector(".terminal-tabs"));
  const closer = clear(main.querySelector(".terminal-close"));
  if (!state.terminals.length) {
    closer.append(el("p", { class: "hint" }, window.Terminal ? t("Open the terminal of a running start above, or start one on the page Starts and sessions.") : t("Terminals need xterm.js on the server: {command}", { command: "sandbox-ai web vendor" })));
  }
  const select = (entry) => { state.current = entry; show("terminals"); };
  state.terminals.forEach((entry, i) => {
    const current = entry === state.current;
    tabs.append(el("button", { role: "tab", id: `${entry.id}-tab`, "aria-controls": entry.id, "aria-selected": current ? "true" : "false", tabindex: current ? "0" : "-1",
      class: `${current ? "selected" : ""}${entry.closed ? " ended" : ""}`, title: entry.closed ? entry.ended : null, onclick: () => select(entry),
      onkeydown: (event) => {  // the arrow keys move between the tabs (one tab stop for the whole list)
        const step = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
        if (!step) return;
        event.preventDefault();
        const next = state.terminals[(i + step + state.terminals.length) % state.terminals.length];
        select(next);
        const tab = document.getElementById(`${next.id}-tab`);
        if (tab) tab.focus();
      } }, entry.closed ? t("{title} (ended)", { title: entry.title }) : entry.title));
    entry.box.classList.toggle("hidden", !current);
  });
  if (state.current) {
    const entry = state.current;
    closer.append(el("button", { "aria-label": t("Close {title}", { title: entry.title }), title: t("Close the tab (a start runs on)"), onclick: () => closeTerminal(entry) }, entry.closed ? t("Close tab") : t("Close tab (detach)")));
    if (!entry.closed) entry.fit();
    entry.term.focus();
  }
};

// Why a terminal's WebSocket closed, in words: the service puts its reason in the close frame (a refused terminal,
// the session that ended, a message too big); 1006 is a connection refused before it opened or lost.
const CLOSE_CODES = { 1000: N_("ended"), 1005: N_("ended"), 1001: N_("the service went away"), 1002: N_("a protocol error"), 1006: N_("the connection was refused or lost"),
  1008: N_("refused by the service"), 1009: N_("a message too big"), 1011: N_("an error of the service") };

function closeText(event, opened) {
  // the reason is the server's text: shown as text (toast) and, in the terminal, without control characters
  const reason = String(event.reason || "").replace(/[\u0000-\u001f\u007f-\u009f]/g, " ").trim();
  const what = event.code === 1006 && !opened ? t("could not connect (refused, or the session ended: reload the page)") : t(CLOSE_CODES[event.code] || N_("closed"));
  return `${t("{what} (code {code})", { what, code: event.code })}${reason ? `: ${reason}` : ""}`;
}

// The tab of an ended terminal stays, with why it ended, until it is closed here; closing a running one detaches.
function closeTerminal(entry) {
  entry.dismissed = true;
  if (!entry.closed) { entry.ws.close(); return; }  // onclose takes the tab away
  state.terminals = state.terminals.filter(other => other !== entry);
  if (state.current === entry) state.current = state.terminals[state.terminals.length - 1] || null;
  entry.term.dispose(); entry.box.remove();
  if (state.page === "terminals") show("terminals");
}

function openTerminal(params, title) {
  if (!window.Terminal) { toast("warning", t("No terminals here"), t("Terminals need xterm.js on the server: {command}", { command: "sandbox-ai web vendor" })); return; }
  // One tab per start: attaching again shows the tab already open.
  const key = params.kind === "attach" ? terminalKey(state.host, params.name) : null;
  const already = key && state.terminals.find(t => t.key === key && !t.closed);


  if (already) { state.current = already; show("terminals"); return; }
  const term = new window.Terminal({ cursorBlink: true, fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--mono"), theme: terminalTheme(), scrollback: 5000 });
  const fit = window.FitAddon ? new window.FitAddon.FitAddon() : null;
  if (fit) term.loadAddon(fit);
  const id = `terminal-${++state.terminalCount}`;
  const box = el("div", { class: "terminal", id, role: "tabpanel", "aria-labelledby": `${id}-tab` });
  show("terminals");
  terminalBoxes().append(box);
  term.open(box);
  if (fit) fit.fit();
  const query = new URLSearchParams(Object.assign({ host: state.host, csrf: state.me.csrf, cols: term.cols, rows: term.rows }, params));
  for (const [key, value] of [...query.entries()]) if (value === "") query.delete(key);
  const ws = new WebSocket(`wss://${location.host}/api/terminal?${query}`);
  ws.binaryType = "arraybuffer";
  const encoder = new TextEncoder();
  const entry = { id, title: `${state.host}: ${title}`, key, closed: false, opened: false, dismissed: false, ended: "", term, ws, box, fit: () => { if (fit) { fit.fit(); if (ws.readyState === 1) ws.send(JSON.stringify({ resize: [term.cols, term.rows] })); } } };
  ws.onopen = () => { entry.opened = true; };
  ws.onmessage = (event) => term.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
  ws.onclose = (event) => {
    entry.closed = true;
    window.removeEventListener("resize", entry.fit);
    entry.ended = closeText(event, entry.opened);
    if (entry.dismissed) { closeTerminal(entry); return; }
    term.write(`\r\n[${t("terminal: {why}", { why: entry.ended })}]\r\n`);
    if (event.code !== 1000 && event.code !== 1005) toast(event.code === 1006 && entry.opened ? "warning" : "error", t("Terminal {title}", { title: entry.title }), entry.ended);

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
