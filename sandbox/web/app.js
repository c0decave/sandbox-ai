// sandbox-ai web: one page, plain JavaScript, no build step. Everything shown from the server goes through
// textContent (never innerHTML); every change sends the session's CSRF token. The page's own texts go through
// t() (i18n.js, loaded first): English or German; what the server or a host says is shown as it is.
"use strict";

const state = {
  me: null, host: localStorage.getItem("sandbox-ai-host") || "local", hosts: [], page: "starts", startsHost: null,
  themes: {}, theme: localStorage.getItem("sandbox-ai-theme") || "system", terminals: [], current: null,
  approvals: 0, approvalsError: null, approvalErrors: {}, timer: null, tick: null, asked: new Set(),
  // the questions a toast tells of (one toast for them, drawn anew from what is still open), the ones answered here
  // (never shown again until their host stops listing them), the open ones of the last poll
  toastQuestions: new Map(), questionToast: null, answered: new Map(), questions: [],
  boxes: null, terminalCount: 0, polling: false, pollAgain: false, policy: null, session: null, loginMessage: null,
  cards: [], shown: null,
  renderToken: 0,  // counts the pages drawn: a drawing that awaited an answer goes on only while it is still the last
  installing: {},  // host -> when its install began (it may outlast the page that started it)
  startsEvery: (() => { try { return localStorage.getItem("sandbox-ai-starts-every") === "1"; } catch (e) { return false; } })(),
  review: null,  // {host, path}: a project configuration to review (a start refused it as untrusted)
  help: null, helpFor: null,  // help.json's sections once loaded; the page whose (?) opened Help
  formHost: null,  // the host of the new start's form, when another than the table's was chosen there
};
const AGENTS = ["claude", "codex", "opencode"];
const PAGES = [
  ["starts", N_("Starts and sessions")], ["approvals", N_("Approvals")], ["terminals", N_("Terminals")],
  ["doctor", N_("Doctor")], ["logs", N_("Logs")], ["llm", N_("LLM servers")], ["vpn", N_("VPN profiles")], ["kits", N_("Kits")], ["canaries", N_("Canaries")], ["secrets", N_("Secrets")], ["logins", N_("Logins")], ["projects", N_("Projects")], ["mcp", N_("MCP servers")],
  ["presets", N_("Presets")], ["teams", N_("Teams")], ["images", N_("Images and tools")], ["activity", N_("Activity")], ["settings", N_("Defaults")], ["hosts", N_("Hosts")],
  ["help", N_("Help")],
];
// The pages that show one host's things: the address names the host too (#doctor@ct101).
const HOST_PAGES = ["starts", "doctor", "logs", "llm", "vpn", "kits", "canaries", "secrets", "logins", "projects", "mcp", "presets", "teams", "images", "activity", "settings"];

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

// CONFIRM: the confirmation a refusal names (the service's NEEDS_CONFIRM action), else null; CODE and PARAMS: which
// common refusal it is (the service's ERROR_CODES), for the page's own words (errorText).
class ApiError extends Error {
  constructor(status, message, confirm, code, params) {
    super(message); this.status = status; this.confirm = confirm || null; this.code = code || null;
    this.params = params && typeof params === "object" && !Array.isArray(params) ? params : {};
  }
}

// The page's words for the service's common refusals (webui.ERROR_CODES), filled in with the refusal's params; the
// service's own (English) message is shown below them. A code the page does not know: the message as it came.
const ERROR_TEXTS = {
  "no-host": N_("There is no host {host} (any more): choose another one, or add it on the page Hosts."),
  "confirm-needed": N_("This needs your confirmation first."),
  "bad-path": N_("{what}: an absolute path on that host is needed (no . or .. parts)."),
  "bad-name": N_("That is no valid name."),
  "host-unreachable": N_("{host} cannot be reached over SSH right now."),
  "timeout": N_("{host} did not answer in time."),
  "command-failed": N_("{command} failed on {host} (exit status {status})."),
  "unknown-preset-value": N_("The preset {preset} has no value {value}."),
  "no-preset": N_("There is no preset {preset}."),
  "login-needed": N_("Log in again."),
  "page-outdated": N_("The page is out of date: reload it."),
  "too-many-logins": N_("Too many failed logins: try again in {seconds} seconds."),
  "busy": N_("Something runs on {host} already: wait for its end."),
  "changed-meanwhile": N_("{host} was changed meanwhile: nothing was done. Look again, then ask again."),
  "nothing-checked": N_("Nothing was checked on {host}: there is no all-clear."),
};

// E's text in the page's language: the page's own words for a known code, else its message.
function errorText(e) {
  if (e instanceof ApiError && e.code && Object.prototype.hasOwnProperty.call(ERROR_TEXTS, e.code)) {
    const values = {};
    for (const [key, value] of Object.entries(e.params || {})) values[key] = String(value ?? "");
    for (const name of (ERROR_TEXTS[e.code].match(/\{(\w+)\}/g) || [])) if (!(name.slice(1, -1) in values)) values[name.slice(1, -1)] = "?";
    return t(ERROR_TEXTS[e.code], values);
  }
  return e && e.message ? e.message : String(e);
}

// The service's own words below the page's (for a known code), else nothing.
function errorDetail(e) {
  return e instanceof ApiError && e.code && Object.prototype.hasOwnProperty.call(ERROR_TEXTS, e.code) && e.message ? e.message : "";
}

// An error as a notice: the page's words, and below them what the service said (English, as it came).
function errorNotice(e, prefix) {
  const detail = errorDetail(e);
  const box = notice(`${prefix ? `${prefix}: ` : ""}${errorText(e)}`, "error");
  if (detail) box.append(el("div", { class: "hint mono error-detail", lang: "en" }, detail));
  return box;
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
  noteSession(response);
  if (response.status === 401 && path !== "/api/login" && state.me) sessionEnded();
  if (!response.ok) throw new ApiError(response.status, (data && data.error) || response.statusText, data && typeof data.confirm === "string" ? data.confirm : null,
    data && typeof data.code === "string" ? data.code : null, data && data.params);
  return data;
}

// How long the session lives: each answer says it (X-Session-Idle-Left: without use; the page's polls are none).
function noteSession(response) {
  const header = (name) => (response && response.headers && typeof response.headers.get === "function" ? response.headers.get(name) : null);
  const idle = Number(header("X-Session-Idle-Left")), lifetime = Number(header("X-Session-Lifetime-Left"));
  if (header("X-Session-Idle-Left") === null || !Number.isFinite(idle)) return;
  const now = Date.now();
  state.session = { idle: now + idle * 1000, lifetime: Number.isFinite(lifetime) && header("X-Session-Lifetime-Left") !== null ? now + lifetime * 1000 : Infinity, notified: false };
  tickSession();
}

// The session ended (idle too long, logged out elsewhere, its user changed): the login says so, and what was open.
function sessionEnded() {
  const byHost = {};
  for (const q of state.questions) byHost[q.host] = (byHost[q.host] || 0) + 1;
  const hosts = Object.entries(byHost).map(([host, count]) => t("{count} on {host}", { count, host })).join(", ");
  const count = state.questions.length;
  state.loginMessage = `${t("The session ended (idle too long, logged out, or its user changed): log in again.")}${count ? " " + (count === 1 ? t("When it ended, 1 question was open: {hosts}.", { hosts }) : t("When it ended, {count} questions were open: {hosts}.", { count, hosts })) : ""}`;
  stopPage();
  render();
  // told outside the page too: the tab's title, and a desktop notification when the user asked for those
  ended();
}

function endedTitle() { return `(${t("session ended")}) sandbox-ai`; }

function ended() {
  document.title = endedTitle();
  if (!notificationsOn()) return;
  try { new window.Notification(t("sandbox-ai: the session ended"), { body: t("Log in again to answer the questions of the sandboxes."), tag: "sandbox-ai-session" }); } catch (e) { /* the login says it */ }
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

// One question at a time in a container: a second confirmIn() there first puts back what was there before the first
// (never a prompt on top of a prompt, whose Cancel would bring the first one back live). When it closes, the focus
// goes back to what opened it (OPTIONS.trigger, else what had the focus), or to the first button there.
const asking = new WeakMap();

function confirmIn(container, text, yes, onYes, options) {
  const opts = options || {};
  const earlier = asking.get(container);
  if (earlier) earlier.restore(false);
  const active = typeof document !== "undefined" ? document.activeElement : null;
  const trigger = opts.trigger || (active && active !== document.body ? active : null);
  const saved = [...(container.childNodes || container.children)];
  let open = true;
  const entry = {
    restore: (focus) => {
      if (!open) return;
      open = false;
      if (asking.get(container) === entry) asking.delete(container);
      clear(container).append(...saved);
      if (focus !== false) returnFocus(trigger, container);
    },
  };
  asking.set(container, entry);
  const nodes = ask(text, yes, async (event) => {
    event.target.disabled = true;
    try { await onYes(event); } finally { entry.restore(); }
  }, () => { entry.restore(); if (opts.onCancel) opts.onCancel(); }, opts.kind);
  clear(container).append(...nodes);
  if (typeof nodes[2].focus === "function") nodes[2].focus();  // Cancel: Enter on a key held down does nothing
  return nodes;
}

function returnFocus(trigger, container) {
  if (trigger && trigger.isConnected && typeof trigger.focus === "function") { trigger.focus(); return; }
  if (container && container.isConnected) focusFirst(container);
}

// The buttons, fields and lists in NODE (to give the focus to, or to switch off).
function controlsIn(node) { return node && typeof node.querySelectorAll === "function" ? [...node.querySelectorAll("button, input, select, textarea")] : []; }

function focusFirst(node) {
  const first = controlsIn(node).find(c => !c.disabled && !c.hidden);
  if (first && typeof first.focus === "function") first.focus();
}

// A question put in place with ask(): the focus on its Cancel (its last button), as confirmIn() does.
function focusCancel(node) {
  const buttons = controlsIn(node).filter(c => c.tagName && String(c.tagName).toLowerCase() === "button");
  const last = buttons[buttons.length - 1];
  if (last && typeof last.focus === "function") last.focus();
}

// A refusal that names the confirmation it lacks (a 400 with one of the service's NEEDS_CONFIRM actions): the view was
// out of date (another tab, the host changed meanwhile). askRefused() asks for it in CONTAINER as every confirmation
// is, with the service's own reason; Yes sends it again, confirmed (RETRY).
function confirmable(e) {
  return e instanceof ApiError && e.status === 400 && typeof e.confirm === "string" && e.confirm && needsConfirm(e.confirm) ? e.confirm : null;
}

function askRefused(container, e, retry, options) {
  const why = String((e && e.message) || "").replace(/:\s*send \{"confirm": true\}\s*$/, "");
  return confirmIn(container, t("The service asks first: {why}", { why }), t("Yes, do it"), retry, options);
}

// Whether ACTION needs a confirmation (GET /api/policy: the service's own table); unknown yet: asked all the same.
function needsConfirm(action) {
  const table = state.policy && state.policy.confirm;
  return !table || typeof table !== "object" || Object.prototype.hasOwnProperty.call(table, action);
}

// What a screen reader is told (a dedicated live region; the page itself is not one).
function say(text) {
  const live = document.getElementById("live");
  if (live) { live.textContent = ""; live.textContent = text; }
}

// The page in the address, with its host where it shows one host's things (#approvals, #doctor@ct101, #starts@all):
// a reload or a link keeps both.
function addressOf() {
  let text = "";
  // a malformed address (#starts@%) is no page: the default one, never a blank page
  try { text = decodeURIComponent(String((typeof location !== "undefined" && location.hash) || "").replace(/^#/, "")); }
  catch (e) { return { page: null, host: null }; }
  const [page, host] = text.split("@");
  return { page: PAGES.some(([key]) => key === page) ? page : null, host: host || null };
}

// The host of the address, if it is one of the list ("all": only for the starts).
function useAddressHost(where) {
  if (!where.host || !HOST_PAGES.includes(where.page)) return;
  if (where.page === "starts" && where.host === "all") { state.startsHost = "all"; return; }
  if (!state.hosts.some(h => h.name === where.host)) return;
  state.host = where.host;
  if (where.page === "starts") state.startsHost = where.host;
}

function addressFor(page) {
  if (!HOST_PAGES.includes(page)) return `#${page}`;
  return `#${page}@${page === "starts" ? (state.startsHost || state.host) : state.host}`;
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

// A notice in the page: no live region of its own (the page has one: say()); an error or a result ("ok") is told to
// screen readers there when it is shown, and the same one drawn again by a poll is not told again within a minute.
const SAID = new Map();
function notice(text, kind) {
  if (kind === "error" || kind === "ok") {
    const words = String(text ?? ""), now = Date.now();
    if (!SAID.has(words) || now - SAID.get(words) > 60000) say(words);
    SAID.set(words, now);
    if (SAID.size > 50) SAID.delete(SAID.keys().next().value);
  }
  return el("div", { class: `notice ${kind || ""}` }, text);
}

// A row's button: LABEL, and for screen readers what it does to which thing on which host ("Check gpu on ct101").
// ATTRS: its further attributes (onclick, class, title, disabled).
function rowButton(label, name, host, attrs) {
  return el("button", Object.assign({ "aria-label": t("{action} {name} on {host}", { action: label, name, host }) }, attrs || {}), label);
}

// A field where a secret's VALUE is typed (an API key, a token, a VPN key: kept on a host): shown while it is typed,
// so a typo is seen before it is kept, with "Show value" beside it to hide it (someone looking on, a shared screen).
// The choice is remembered in this browser for every such field (SECRET_SHOWN_STORE). Only what is typed now is ever
// shown: a value already kept is never read back. ATTRS: the input's own (id, aria-describedby); OPTIONS.shown: how
// it starts, without the remembered choice (the login's password: hidden), OPTIONS.remember false: not remembered.
// Returns [input, the input and its button together]; the label's "for" names the input.
const SECRET_SHOWN_STORE = "sandbox-ai-show-secret-values";
function secretShownChoice() {
  try { return localStorage.getItem(SECRET_SHOWN_STORE) !== "0"; } catch (e) { return true; }
}
function secretInput(attrs, options) {
  const opts = Object.assign({ shown: undefined, remember: true, hiddenAutocomplete: "new-password" }, options || {});
  const input = el("input", Object.assign({ "data-secret-value": true }, attrs || {}));
  const toggle = el("button", { type: "button", class: "secret-toggle", "aria-controls": input.getAttribute("id") || null });
  const set = (shown) => {
    input.setAttribute("type", shown ? "text" : "password");
    // shown: no autofill, no spelling marks, no capitals added; hidden: the browser's password handling
    input.setAttribute("autocomplete", shown ? "off" : opts.hiddenAutocomplete);
    input.setAttribute("spellcheck", "false"); input.setAttribute("autocapitalize", "off"); input.setAttribute("autocorrect", "off");
    // the name stays "Show value" (pressed: shown), as a toggle button's should; the tooltip says what a press does
    toggle.setAttribute("aria-pressed", shown ? "true" : "false");
    toggle.setAttribute("title", shown ? t("Hide value") : t("Show value"));
  };
  toggle.textContent = t("Show value");
  toggle.addEventListener("click", () => {
    const shown = toggle.getAttribute("aria-pressed") !== "true";
    set(shown);
    if (opts.remember) { try { localStorage.setItem(SECRET_SHOWN_STORE, shown ? "1" : "0"); } catch (e) { /* this page only */ } }
  });
  set(opts.shown !== undefined ? opts.shown : opts.remember ? secretShownChoice() : true);
  return [input, el("div", { class: "secret-field" }, input, toggle)];
}

// --- notifications: toasts in the theme's colours, top right ---------------------------------------------
// Errors stay until they are closed; the others close after TOAST_SECONDS, not while the pointer or the focus is on
// them. Each is told to screen readers once, through the page's one live region (say()), never by a role of its own.
// Title and text are text nodes: what a sandbox chose is never markup.

const TOAST_SECONDS = 8;
const TOAST_MAX = 4;
const TOAST_LEVELS = { info: [N_("Information"), "i"], success: [N_("Done"), "✓"], warning: [N_("Warning"), "!"], error: [N_("Error"), "×"] };

function toast(level, title, text, options) {
  if (!Object.prototype.hasOwnProperty.call(TOAST_LEVELS, level)) level = "info";
  const opts = options || {};
  let box = document.getElementById("toasts");
  if (!box) { box = el("div", { id: "toasts", class: "toasts" }); document.body.append(box); }  // each toast has its own role
  const [name, sign] = TOAST_LEVELS[level];
  const seconds = level === "error" ? 0 : (opts.seconds ?? TOAST_SECONDS);
  let timer = null;
  const item = el("div", { class: `toast ${level}` });
  const dismiss = () => { clearTimeout(timer); item.remove(); };
  const closed = () => { dismiss(); if (opts.onClose) opts.onClose(); };  // by the user (OPTIONS.onClose)
  const start = () => { clearTimeout(timer); if (seconds > 0) timer = setTimeout(dismiss, seconds * 1000); };
  const body = String(text ?? "");
  item.append(
    el("span", { class: "glyph", "aria-hidden": "true" }, sign),
    el("div", { class: "body" },
      el("span", { class: "sr-only" }, `${t(name)}: `),
      el("strong", { class: "title" }, String(title ?? "")),
      body ? el("div", { class: "text" }, body.length > 2000 ? `${body.slice(0, 1999)}…` : body) : null,
      opts.action ? el("div", { class: "actions" },
        el("button", { onclick: () => { closed(); opts.action[1](); } }, opts.action[0])) : null),
    el("button", { class: "close", "aria-label": t("Close"), title: t("Close"), onclick: () => closed() }, "×"));
  for (const type of ["mouseenter", "focusin"]) item.addEventListener(type, () => clearTimeout(timer));
  for (const type of ["mouseleave", "focusout"]) item.addEventListener(type, start);
  box.prepend(item);  // the newest on top
  while (box.children.length > TOAST_MAX) box.lastElementChild.remove();
  say(`${t(name)}: ${String(title ?? "")}${body ? `. ${body.slice(0, 300)}` : ""}`);
  start();
  item.dismiss = dismiss;
  return item;
}

// What failed, as an error toast: catch (e) { failed(t("Could not …", values))(e); }. A known code in the page's
// words, the service's own below them.
function failed(title) { return (e) => toast("error", title, [errorText(e), errorDetail(e)].filter(Boolean).join("\n")); }

// The states and results the hosts name (list, doctor, devices), in the page's language; another one as it comes.
const STATES = { ok: N_("state|ok"), warn: N_("state|warn"), fail: N_("state|fail"), skip: N_("state|skip"), running: N_("state|running"),
  paused: N_("state|paused"), stopped: N_("state|stopped"), damaged: N_("state|damaged"), attached: N_("state|attached"), held: N_("state|held"), gone: N_("state|gone") };
function stateText(value) {
  if (value === undefined || value === null) return "";
  return Object.prototype.hasOwnProperty.call(STATES, value) ? t(STATES[value]) : String(value);
}

function statusCell(value) {
  const kind = { ok: "ok", warn: "warn", fail: "fail", skip: "skip", running: "ok", paused: "warn", stopped: "muted" }[value] || "muted";
  return el("td", { class: `status-${kind}` }, stateText(value));
}

// CAPTION: what the table is, for screen readers (a table is announced by it).
function table(columns, rows, empty, caption) {
  if (!rows.length) return el("p", { class: "hint" }, empty || t("Nothing here."));
  return el("table", {}, caption ? el("caption", { class: "sr-only" }, caption) : null, el("thead", {}, el("tr", {}, columns.map(c => el("th", {}, c)))),
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
  // the web login's own password, not a secret being kept on a host: hidden at first (the login page is often seen
  // by others) and not remembered with the secret fields' choice; "Show value" beside it all the same
  const [password, passwordField] = secretInput({ id: "password", required: true, "aria-label": t("Password") },
    { shown: false, remember: false, hiddenAutocomplete: "current-password" });
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
        status.append(errorNotice(e));
      }
    },
  },
  el("h1", {}, el("img", { src: "/static/icon.svg", alt: "", width: 28, height: 28 }), " sandbox-ai"),
  status,
  el("div", { class: "form" },
    el("label", { for: "user" }, t("User")), user,
    el("label", { for: "password" }, t("Password")), passwordField,
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
  if (!state.me && !state.loginMessage) {
    try { state.me = await api("GET", "/api/me"); } catch (e) { state.me = null; }
  }
  if (!state.me) { document.title = state.loginMessage ? endedTitle() : "sandbox-ai"; removeSessionBanner(); root.append(loginView(state.loginMessage)); return; }
  state.loginMessage = null;
  try { state.hosts = await api("GET", "/api/hosts"); } catch (e) { state.hosts = [{ name: "local", kind: "local" }]; }
  try { state.policy = await api("GET", "/api/policy"); } catch (e) { state.policy = null; }  // without it: every prompt is asked
  if (!state.hosts.some(h => h.name === state.host)) state.host = "local";
  const where = addressOf();
  state.page = where.page || state.page;
  useAddressHost(where);
  if (state.startsHost !== "all" && !state.hosts.some(h => h.name === state.startsHost)) state.startsHost = state.host;
  const hostSelect = el("select", { "aria-label": t("Host"), onchange: (e) => { state.host = e.target.value; state.startsHost = state.host; state.formHost = null; localStorage.setItem("sandbox-ai-host", state.host); show(state.page); } },
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
  clearInterval(state.timer);
  state.timer = setInterval(poll, 5000);
  clearInterval(state.tick);
  state.tick = setInterval(() => { tickApprovals(); tickSession(); }, 1000);
  // the poll first: the approvals page drawn now takes its answer instead of asking the same once more
  poll();
  show(state.page);
}

async function logout() {
  try { await api("POST", "/api/logout"); } catch (e) { /* the session is gone either way */ }
  stopPage();
  render();
}

// What a session's page holds goes with it: terminals, polls, toasts, what was asked and answered.
function stopPage() {
  for (const entry of state.terminals) { entry.dismissed = true; entry.ws.close(); entry.box.remove(); }
  state.terminals = []; state.current = null; state.me = null; clearInterval(state.timer); clearInterval(state.tick);
  state.asked.clear(); state.toastQuestions.clear(); state.questionToast = null; state.answered.clear(); state.questions = [];
  state.boxes = null; state.session = null; state.cards = []; state.shown = null;
  removeSessionBanner();
  for (const item of document.querySelectorAll("#toasts .toast")) item.remove();
}

// --- the session's end: a banner two minutes before (the page's polls keep no session alive) -----------------

const SESSION_WARNING = 120;

function removeSessionBanner() {
  const banner = typeof document !== "undefined" && document.getElementById("session-banner");
  if (banner) banner.remove();
}

function minutesText(seconds) { return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`; }

// Once a second (and at each answer): the banner while the session has SESSION_WARNING seconds or less left.
function tickSession() {
  if (!state.me || !state.session) { removeSessionBanner(); return; }
  const now = Date.now(), lifetimeFirst = state.session.lifetime < state.session.idle;
  const left = Math.max(0, Math.ceil((Math.min(state.session.idle, state.session.lifetime) - now) / 1000));
  let banner = document.getElementById("session-banner");
  if (left > SESSION_WARNING) { if (banner) banner.remove(); return; }
  if (!banner) {
    const stay = el("button", { class: "primary", onclick: (event) => busy(event.target, async () => {
      try { await api("GET", "/api/me"); say(t("You stay logged in.")); } catch (e) { failed(t("Could not keep the session"))(e); }
    }) }, t("Stay logged in"));
    banner = el("div", { id: "session-banner", class: "session-banner", role: "region", "aria-label": t("Session") },
      el("span", { class: "session-left" }), lifetimeFirst ? null : stay);
    document.body.prepend(banner);
    say(lifetimeFirst ? t("The session reaches its 12-hour limit in 2 minutes: log in again then.") : t("The session ends in 2 minutes without use: Stay logged in keeps it."));
    if (!state.session.notified && notificationsOn() && typeof document.hidden === "boolean" && document.hidden) {
      state.session.notified = true;
      try { new window.Notification(t("sandbox-ai: the session ends soon"), { body: t("In 2 minutes without use; open the page to stay logged in."), tag: "sandbox-ai-session" }); } catch (e) { /* the banner says it */ }
    }
  }
  banner.querySelector(".session-left").textContent = lifetimeFirst ? t("The session reaches its 12-hour limit in {time}: log in again then.", { time: minutesText(left) })
    : t("Session ends in {time}", { time: minutesText(left) });
}

// Every 5 s, in the background (it keeps no session alive): the open questions of every host (the badge, the
// tab's title; a failed check shows "?", never 0, which would read as "nothing asks"), and the page's lists that
// follow them.
// A poll still waiting (a host that does not answer takes up to 15 s) is not asked again on top of it: one asked for
// meanwhile (an answer was given) runs right after it, never dropped.
async function poll() {
  if (!state.me) return;
  if (state.polling) { state.pollAgain = true; return; }
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
  if (state.pollAgain) { state.pollAgain = false; poll(); }
}

function questionKey(q) { return `${q.host} ${q.id}`; }

// Whether HOST could not be asked in FOUND's check (a damaged hosts.json: no host but this machine was).
function notAsked(errors, host) {
  return Object.prototype.hasOwnProperty.call(errors, host) || (Object.prototype.hasOwnProperty.call(errors, "hosts.json") && host !== "local");
}

// The open questions of FOUND ({questions, errors, age}) without the ones answered here: a list read before the answer
// (a poll under way, or one the service gives again while that host is still being asked) may still have it; such a
// question never comes back with live buttons. An answered one is forgotten once its host's list, read after the
// answer (FOUND.age: how old each host's list is), no longer has it.
function openQuestions(found) {
  const errors = found.errors && typeof found.errors === "object" ? found.errors : {};
  const ages = found.age && typeof found.age === "object" ? found.age : {};
  const listed = found.questions.filter(q => q && typeof q === "object" && q.id !== undefined && typeof q.host === "string");
  const keys = new Set(listed.map(questionKey));
  const now = Date.now();
  for (const [key, when] of [...state.answered]) {
    const host = key.split(" ")[0];
    const read = now - (Number(ages[host]) || 0) * 1000;
    if (!keys.has(key) && !notAsked(errors, host) && read >= when) state.answered.delete(key);
  }
  return listed.filter(q => !state.answered.has(questionKey(q)));
}

// What a poll found: the count (badge, title, the live region when it changes), a toast for each new question
// (kept until it is answered or expires), a desktop notification when the user asked for those.
function noteApprovals(open) {
  const before = state.approvals;
  if (open instanceof Error) {
    state.approvals = null; state.approvalsError = open.message || String(open); state.approvalErrors = {};
  } else {
    const questions = openQuestions(open);
    const errors = open.errors && typeof open.errors === "object" ? open.errors : {};
    state.approvalErrors = errors;
    state.questions = questions;
    const failedHosts = Object.keys(errors);
    state.approvals = questions.length || !failedHosts.length ? questions.length : null;
    state.approvalsError = failedHosts.length ? failedHosts.map(h => `${h}: ${errors[h]}`).join("; ") : null;
    const keys = new Set(questions.map(questionKey));
    // A question gone (answered, expired) leaves the toast; one of a host that could not be asked stays in it.
    for (const key of [...state.toastQuestions.keys()]) if (!keys.has(key) && !notAsked(errors, key.split(" ")[0])) state.toastQuestions.delete(key);
    for (const key of [...state.asked]) if (!keys.has(key) && !notAsked(errors, key.split(" ")[0])) state.asked.delete(key);
    const fresh = questions.filter(q => !state.asked.has(questionKey(q)));
    for (const q of fresh) state.asked.add(questionKey(q));
    if (fresh.length) {
      if (state.page !== "approvals") for (const q of fresh) state.toastQuestions.set(questionKey(q), q);
      notifyDesktop(fresh);
    }
    for (const q of questions) if (state.toastQuestions.has(questionKey(q))) state.toastQuestions.set(questionKey(q), q);
    questionToast();
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

// The questions that came while another page was shown: one toast that stays (a canary that went off: an error),
// with the way to answer them, drawn anew from what is still open: an answer to one of them leaves the others in
// it; it goes with the last of them.
function questionToast() {
  const open = [...state.toastQuestions.values()];
  const signature = JSON.stringify(open.map(q => [questionKey(q), q.kind, q.question]));
  const old = state.questionToast;
  if (old && old.signature === signature) return;
  if (old) old.item.dismiss();
  state.questionToast = null;
  if (!open.length) return;
  const canary = open.find(q => q.kind === "canary");
  const first = canary || open[open.length - 1];
  const more = open.length > 1 ? t(" and {count} more", { count: open.length - 1 }) : "";
  const item = toast(canary ? "error" : "warning", t("sandbox-ai asks: {sandbox} on {host}", { sandbox: first.sandbox || first.run || t("a sandbox"), host: first.host }), `${first.question || first.kind || ""}${more}`,
    { action: [t("Open approvals"), () => show("approvals")], seconds: 0, onClose: () => { state.toastQuestions.clear(); state.questionToast = null; } });
  state.questionToast = { item, signature };
}

// A question answered here: never shown again with live buttons (see openQuestions), and out of the toast.
function answered(q) {
  state.answered.set(questionKey(q), Date.now());
  state.toastQuestions.delete(questionKey(q));
  state.questions = state.questions.filter(other => questionKey(other) !== questionKey(q));
  questionToast();
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
  // another page (or host) than the one shown: its heading gets the focus once it is drawn (a screen reader reads
  // where the user is now; the focus is not left on a button that is gone)
  const moved = state.shown !== addressFor(page);
  state.page = page;
  state.shown = addressFor(page);
  if (typeof location !== "undefined" && location.hash !== addressFor(page)) location.hash = addressFor(page);
  for (const button of document.querySelectorAll("nav button")) {
    const current = button.dataset.page === page;
    button.classList.toggle("selected", current);
    if (current) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
  }
  const main = document.getElementById("main");
  if (!main) return;
  for (const entry of state.terminals) entry.box.classList.add("hidden");
  if (page !== "terminals") clear(main);
  // a drawing still waiting for an answer is out of date from now on: it never draws over this one (pageLive)
  const token = ++state.renderToken;
  pages[page](main).then(() => {
    if (token !== state.renderToken) return;
    addHelpLink(main, page);
    if (moved && page !== "terminals" && state.page === page) focusHeading(main);
  },
    e => { if (token !== state.renderToken) return; clear(main); main.append(errorNotice(e)); if (moved) focusHeading(main); });
}

// Whether the page drawing that asks is still the one shown: false once another page (or this one again) is drawn.
// A page's drawing checks it after each answer it waited for, before it touches the page.
function pageLive() { const token = state.renderToken; return () => token === state.renderToken; }

function focusHeading(main) {
  const heading = main && typeof main.querySelector === "function" ? main.querySelector("h1") : null;
  if (!heading || typeof heading.focus !== "function") return;
  heading.setAttribute("tabindex", "-1");
  heading.focus();
}

if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  window.addEventListener("hashchange", () => {
    const where = addressOf();
    if (!state.me || !where.page) return;
    const before = [state.page, state.host, state.startsHost];
    useAddressHost(where);
    if (where.page !== before[0] || state.host !== before[1] || state.startsHost !== before[2]) show(where.page);
  });
}

// --- pages -----------------------------------------------------------------------------------------------

const pages = {};

// An MCP server's "Attach to…": one of the host's kept agent starts gets it (sandbox-ai mcp attach), kept for its
// later starts; a running one reaches it after a restart, done now when "restart" is ticked.
function attachControls(s, starts) {
  const server = s.name.replace(/^mcp-/, ""), host = s.host || state.host;
  const targets = starts.filter(other => other.kind !== "mcp" && other.directory && other.state !== "damaged" && (other.host || host) === host);
  const choose = el("select", { "aria-label": t("Attach {server} to", { server }) }, targets.map(target => el("option", { value: target.name }, `${target.name} (${stateText(target.state)})`)));
  const restart = el("input", { type: "checkbox" });
  const asking = el("span", { class: "inline-ask" });
  // asked first, with what was chosen when asking: what Yes sends is what the question named
  const attach = el("button", { disabled: !targets.length, title: targets.length ? "" : t("no kept agent start on this host"), onclick: () => {
    const start = choose.value, again = restart.checked;
    const text = again ? t("Attach the MCP server {server} to {start} on {host} and restart {start} now if it runs? What runs in it ends; the agent continues its conversation.", { server, start, host })
      : t("Attach the MCP server {server} to {start} on {host}? It reaches it from its next start on.", { server, start, host });
    confirmIn(asking, text, again ? t("Yes, attach and restart") : t("Yes, attach it"), async () => {
      try {
        const r = await api("POST", hostPath(`/mcp/${encodeURIComponent(server)}/attach`, host), { start, restart: again, confirm: true });
        toast(r.attached === "after a restart" ? "warning" : "success", `${server} → ${r.start}`, r.message);
        show("starts");
      } catch (e) { failed(t("Could not attach {server}", { server }))(e); }
    }, { kind: "primary", trigger: attach });
  } }, t("Attach to…"));
  return [attach, choose, el("label", { class: "hint", title: t("a running start cannot take a new MCP server while it runs: it is restarted, and the agent continues its conversation") }, restart, " ", t("restart if it runs")), asking];
}

// The starts of the chosen host, or of every host ("all": GET /api/starts, each with its host). The table is kept
// between the polls: its rows are keyed by host and name, and a row whose start is unchanged stays as it is (a Stop
// waiting for its confirmation, the focus in it); a changed one is drawn anew, a gone one goes. When a host cannot
// be asked, its rows stay as they were last seen, greyed, their buttons off (nothing is done on what may be gone).
// The cards below it (network, resources, a review) stay open; one whose start stopped or went is switched off.
const startsView = { main: null, host: null, body: null, rows: new Map(), loading: false, again: false, seen: {} };
const START_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$/;
const WORKSPACE_AS = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;  // workspace add --name NAME
const MCP_SERVER_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

// A start's identity: its id (list --json; it stays when the start is renamed), else its name (an older sandbox-ai).
// Rows and cards are keyed by host and that: a rename keeps them, and their Network or Resources card attached.
function startId(s) { return String(s.id || s.name); }
function startKey(s) { return `${s.host} ${startId(s)}`; }


// Whether a question of the last poll comes from the run of start S.
function startAsks(s) { return Boolean(s.run) && state.questions.some(q => q.host === s.host && q.run === s.run); }

function startSignature(s, starts) {
  const targets = s.kind === "mcp" ? starts.filter(other => other.kind !== "mcp" && other.host === s.host).map(other => [other.name, other.state, Boolean(other.directory)]) : null;
  return JSON.stringify([s.host, s.name, s.agent, s.kind, s.state, s.workspace, s.home, s.keep, s.started, s.attachable, s.directory ? 1 : 0, s.mcp, s.serves, targets, startAsks(s), LANGUAGE]);
}

// What failed when a start's button was pressed (its action: pause, resume, stop, delete).
const RUN_FAILED = { pause: N_("Could not pause {name} on {host}"), resume: N_("Could not resume {name} on {host}"),
  stop: N_("Could not stop {name} on {host}"), delete: N_("Could not delete {name} on {host}") };

function startRow(s, starts, main, all) {
  const host = s.host;
  const actions = el("td", { class: "actions" });
  const run = async (button, action, body) => busy(button, async () => {
    try {
      await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/${action}`, host), body || {});
      if (state.page === "starts") refreshStarts(main, true);
    } catch (e) { failed(t(RUN_FAILED[action], { name: s.name, host }))(e); }
  });
  const act = (label, action, extra) => rowButton(label, s.name, host, Object.assign({ onclick: (event) => run(event.target, action) }, extra || {}));
  const mcp = s.kind === "mcp";
  const stopText = mcp ? t("Stop the MCP server {name} on {host}? The starts it serves lose it until it starts again.", { name: s.name, host })
    : t("Stop {name} on {host}? What runs in it ends; {home}", { name: s.name, host,
      home: s.keep ? t("its HOME is kept: Resume starts it again.") : s.home === "own" ? t("its HOME is a directory of its own, which stays.") : t("its HOME is not kept: it is discarded with the start.") });
  const stop = rowButton(t("Stop"), s.name, host, { onclick: () => confirmIn(actions, stopText, t("Yes, stop it"), () => run(stop, "stop", { confirm: true }), { trigger: stop }) });
  if (s.attachable) actions.append(rowButton(t("Attach"), s.name, host, { class: "primary", onclick: () => openTerminal({ kind: "attach", name: s.name, host }, `${s.name}`) }));
  if (s.state === "running" && !mcp) actions.append(act(t("Pause"), "pause"), stop,
    rowButton(t("Network…"), s.name, host, { onclick: () => networkCard(main, s) }),
    rowButton(t("Resources…"), s.name, host, { title: t("workspaces, devices and resource limits while it runs"), onclick: () => resourcesCard(main, s) }));
  else if (s.state === "running" || (s.state === "paused" && mcp)) actions.append(stop);
  if (s.state === "paused" && !mcp) actions.append(act(t("Resume"), "resume"), stop);
  if (s.state === "stopped" && s.keep) {
    const resume = rowButton(t("Resume"), s.name, host, { onclick: () => reviewThenResume(main, s, resume) });
    const remove = rowButton(t("Delete"), s.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Delete {name} on {host} with its kept HOME? What is in that HOME is gone for good.", { name: s.name, host }),
      t("Yes, delete it"), () => run(remove, "delete", { confirm: true }), { trigger: remove }) });
    actions.append(resume, remove);
  }
  if (mcp) actions.append(...attachControls(s, starts));
  if (!mcp && s.state !== "damaged") {
    const rename = rowButton(t("Rename…"), s.name, host, { onclick: () => askRename(actions, s, main, rename) });
    actions.append(rename);
  }
  if (!mcp && ["running", "paused", "stopped"].includes(s.state) && (s.state !== "stopped" || s.keep))
    actions.append(rowButton(t("More…"), s.name, host, { title: t("resume with directories, mounts or MCP servers added, restart, stop with a longer wait"), onclick: () => moreCard(main, s) }));
  actions.append(rowButton(t("History"), s.name, host, { onclick: () => historyCard(main, s) }));
  // which MCP servers a start reaches (or will at its next start), and whom a server serves
  const mcpNote = mcp ? ((s.serves || []).length ? t("serves {starts}", { starts: s.serves.join(", ") }) : t("serves no start yet"))
    : (s.mcp || []).length ? `MCP: ${s.mcp.join(", ")}` : "";
  const asks = startAsks(s) ? el("button", { class: "badge asking small", title: t("a question of this start waits in Approvals"), onclick: () => show("approvals") }, t("asking")) : null;
  return el("tr", { "data-name": s.name, "data-host": host }, hostMarkerCell(host),
    el("td", { class: "mono start-name" }, el("span", { class: "tree-slot" }), s.name, asks ? " " : null, asks, el("span", { class: "stale-note hint" })), el("td", {}, s.agent || s.kind || ""), statusCell(s.state),
    el("td", { class: "mono" }, s.workspace || "", mcpNote ? el("div", { class: "hint" }, mcpNote) : null), el("td", {}, s.home || ""), el("td", {}, (s.started || "").replace("T", " ").replace("Z", "")), actions);
}

// Another name for a start (sandbox-ai rename OLD NEW), asked in its row; the run id stays.
function askRename(actions, s, main, trigger) {
  const id = `rename-${s.host}-${s.name}`.replace(/[^A-Za-z0-9_-]/g, "_");
  const input = el("input", { id, class: "mono", value: s.name, size: 16, pattern: START_NAME.source.slice(1, -1) });
  confirmIn(actions, el("span", {}, el("label", { for: id }, t("New name for {name} on {host}", { name: s.name, host: s.host })), " ", input), t("Rename"), async () => {
    const value = input.value.trim();
    if (!START_NAME.test(value)) { toast("warning", t("Which name?"), t("1-48 letters, digits, '_', '.' or '-', starting with a letter or digit.")); return; }
    if (value === s.name) return;
    try {
      const r = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/rename`, s.host), { new: value });
      toast("success", t("{name} on {host} is now {new}", { name: s.name, host: s.host, new: value }), (r && r.output) || "");
      if (state.page === "starts") refreshStarts(main, true);
    } catch (e) { failed(t("Could not rename {name} on {host}", { name: s.name, host: s.host }))(e); }
  }, { kind: "primary", trigger });
  if (typeof input.focus === "function") input.focus();
}

// A card about one start (its history, More, network, resources, the review of its HOME) opens where the user is:
// right below the start's row, as a row of its own across the table (with what belongs to the start below it);
// a card about the host (Clean up) below the page's tools. It is scrolled into view and its heading gets the focus.
// One card of each kind (its id) at a time. Without its row (the start gone, another page): at the end of MAIN.
function openCard(main, card, s) {
  const old = document.getElementById(card.id);
  if (old && old !== card) dropCard(old);
  const view = startsView;
  const entry = s && view.main === main && view.rows ? view.rows.get(startKey({ ...s, host: s.host || state.host })) : null;
  if (entry && entry.row.isConnected) {
    const key = startKey(entry.start);
    card.holder = el("tr", { class: "card-row", "data-card-for": key }, el("td", { colspan: COLUMNS }, card));
    view.cardRows = (view.cardRows || []).filter(c => c.card !== card).concat([{ key, row: card.holder, card }]);
    layoutStarts(view, main);
  } else if (!s && view.main === main && view.slot && view.slot.isConnected) view.slot.append(card);
  else main.append(card);
  revealCard(card);
  return card;
}

// A card taken away: its row in the table with it.
function dropCard(card) {
  if (card.holder && card.keepHolder) card.holder.hidden = true;  // a row of its own in that table (a canary event's)
  else if (card.holder) card.holder.remove();
  card.remove();
}

function revealCard(card) {
  if (typeof card.scrollIntoView === "function") card.scrollIntoView({ block: "nearest" });
  const heading = typeof card.querySelector === "function" ? card.querySelector("h2") : null;
  if (heading && typeof heading.focus === "function") { heading.setAttribute("tabindex", "-1"); heading.focus(); }
}

// What happened to a start (sandbox-ai list --history NAME on its host).
async function historyCard(main, s) {
  const old = document.getElementById("history-card");
  if (old) dropCard(old);
  const body = el("div", {}, el("p", { class: "hint" }, t("Loading…")));
  const card = el("div", { class: "card", id: "history-card" }, el("h2", {}, t("History of {name} on {host}", { name: s.name, host: s.host })), body,
    el("button", { onclick: () => dropCard(card) }, t("Close")));
  openCard(main, card, s);
  let found;
  try { found = await api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/history`, s.host)); } catch (e) { clear(body).append(errorNotice(e)); return; }
  const details = (e) => {
    if (e.event === "exit") return typeof e.status === "number" ? t("status {status}", { status: e.status }) : t("status unknown");
    if (e.event === "rename") return e.to ? t("renamed to {name}", { name: e.to }) : t("renamed from {name}", { name: e.from || "?" });
    return [e.agent, e.workspace, e.note].filter(Boolean).map(String).join(" ");
  };
  const rows = (Array.isArray(found) ? found : []).filter(e => e && typeof e === "object").slice().reverse().map(e =>
    el("tr", {}, el("td", {}, String(e.time || "").replace("T", " ").replace("Z", "")), el("td", {}, String(e.event || "")), el("td", { class: "mono" }, String(e.run || "-")), el("td", {}, details(e))));
  clear(body).append(table([t("Time"), t("Event"), t("Run"), t("Detail")], rows, t("No history of {name}.", { name: s.name }), t("History of {name} on {host}", { name: s.name, host: s.host })));
}

// More of resume and stop (sandbox-ai resume NAME --workspace DIR --mount SRC:DST[:ro|rw] --mcp NAME --restart;
// sandbox-ai stop NAME --wait SECONDS): what is added is kept for its later starts; what widens it is asked first.
function moreCard(main, s) {
  const host = s.host;
  const old = document.getElementById("more-card");
  if (old) dropCard(old);
  const workspaces = el("textarea", { id: "more-workspaces", rows: 2, class: "mono", placeholder: "/home/you/src/lib" });
  const mounts = el("textarea", { id: "more-mounts", rows: 2, class: "mono", placeholder: "/home/you/data:/mnt/data:ro" });
  const servers = el("input", { id: "more-mcp", class: "mono", placeholder: "files" });
  const restart = el("input", { id: "more-restart", type: "checkbox", checked: s.state === "running" });
  const wait = el("input", { id: "more-wait", type: "number", min: 1, max: 600, value: 30 });
  const status = el("div", { class: "wide" }), row = el("div", { class: "row" });
  const lines = (input) => input.value.split("\n").map(x => x.trim()).filter(Boolean);
  const request = () => {
    const body = { workspaces: lines(workspaces), mcp: servers.value.split(/\s+/).filter(Boolean), mounts: [] };
    for (const line of lines(mounts)) {
      const parts = line.split(":");
      if (parts.length < 2 || parts.length > 3 || (parts[2] && !["ro", "rw"].includes(parts[2]))) throw new Error(t("A mount is SOURCE:DESTINATION or SOURCE:DESTINATION:ro|rw: {line}", { line }));
      body.mounts.push({ source: parts[0], destination: parts[1], mode: parts[2] || "ro" });
    }
    if (s.state === "running" && restart.checked) body.restart = true;
    return body;
  };
  const resume = (event) => {
    clear(status);
    let body;
    try { body = request(); } catch (e) { status.append(errorNotice(e)); return; }
    const what = [body.restart ? t("it is restarted now (what runs in it ends; the agent continues its conversation)") : null,
      body.workspaces.length ? t("the directories {dirs}, writable", { dirs: body.workspaces.join(", ") }) : null,
      body.mounts.length ? t("the mounts {mounts}", { mounts: body.mounts.map(m => `${m.source}:${m.destination}:${m.mode}`).join(", ") }) : null,
      body.mcp.length ? t("the MCP servers {servers}", { servers: body.mcp.join(", ") }) : null].filter(Boolean);
    if (!what.length) { toast("warning", t("Nothing to add"), t("Name directories, mounts or MCP servers, or restart it.")); return; }
    confirmIn(row, t("Resume {name} on {host} with {what}? What is added is kept for its later starts.", { name: s.name, host, what: what.join(t(" and ")) }), t("Yes, resume it"), async () => {
      try { const r = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/resume`, host), Object.assign(body, { confirm: true })); toast("success", t("{name} on {host} resumed", { name: s.name, host }), (r && r.output) || ""); dropCard(card); if (state.page === "starts") refreshStarts(main, true); }
      catch (e) { status.append(errorNotice(e)); }
    }, { kind: "primary", trigger: event.target });
  };
  const stop = (event) => {
    const seconds = Number(wait.value);  // what the question names is what is sent, whatever the field says by then
    if (!Number.isInteger(seconds) || seconds < 1 || seconds > 600) { clear(status).append(notice(t("Wait 1 to 600 seconds."), "error")); return; }
    confirmIn(row, t("Stop {name} on {host}, waiting up to {seconds} seconds for it to end? What runs in it ends.", { name: s.name, host, seconds }), t("Yes, stop it"), async () => {
      try { const r = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/stop`, host), { wait: seconds, confirm: true }); toast("success", t("{name} on {host} stopped", { name: s.name, host }), (r && r.output) || ""); dropCard(card); if (state.page === "starts") refreshStarts(main, true); }
      catch (e) { status.append(errorNotice(e)); }
    }, { trigger: event.target });
  };
  row.append(el("button", { class: "primary", onclick: resume }, s.state === "running" ? t("Restart with these…") : t("Resume with these…")),
    s.state !== "stopped" ? el("button", { onclick: stop }, t("Stop and wait…")) : null, el("button", { onclick: () => dropCard(card) }, t("Close")));
  const card = el("div", { class: "card", id: "more-card" }, el("h2", {}, t("More for {name} on {host}", { name: s.name, host })),
    el("div", { class: "form" },
      el("label", { for: "more-workspaces" }, t("Directories (one per line)")), workspaces,
      el("label", { for: "more-mounts" }, t("Mounts (one per line)")), mounts,
      el("label", { for: "more-mcp" }, t("MCP servers")), servers,
      s.state === "running" ? el("label", { for: "more-restart" }, t("Restart it now")) : null, s.state === "running" ? restart : null,
      s.state !== "stopped" ? el("label", { for: "more-wait" }, t("Stop: wait up to (seconds)")) : null, s.state !== "stopped" ? wait : null,
      status, el("span"), row));
  openCard(main, card, s);
}

// A stopped start with a kept HOME: first what changed in the files of that HOME that decide what runs at a start
// (resume --review-home); when something did, or not everything could be checked, it is shown and asked first.
async function reviewThenResume(main, s, button) {
  const host = s.host;
  const old = document.getElementById("review-card");
  if (old) dropCard(old);
  const resume = async () => {
    try {
      const r = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/resume`, host), {});
      toast("success", t("{name} on {host} resumed", { name: s.name, host }), (r && r.output) || "");
      const card = document.getElementById("review-card");
      if (card) dropCard(card);
      if (state.page === "starts") refreshStarts(main, true);
    } catch (e) { failed(t("Could not resume {name} on {host}", { name: s.name, host }))(e); }
  };
  let found;
  try { found = await busy(button, () => api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/review`, host))); }
  catch (e) { found = e; }
  if (found instanceof Error) {
    const row = el("div", { class: "row" });
    openCard(main, el("div", { class: "card", id: "review-card" }, el("h2", {}, t("Resume {name} on {host}?", { name: s.name, host })),
      notice(t("What changed in its HOME could not be checked: {error}", { error: errorText(found) }), "warn"), row), s);
    confirmIn(row, t("Resume it without that check?"), t("Resume anyway"), resume, { kind: "primary", trigger: button, onCancel: () => { const c = document.getElementById("review-card"); if (c) dropCard(c); } });
    return;
  }
  const review = found && found.review && typeof found.review === "object" ? found.review : null;
  const changes = review && Array.isArray(review.changes) ? review.changes.filter(c => c && typeof c === "object") : [];
  if (!review || (!changes.length && review.complete !== false)) { await busy(button, resume); return; }
  const rows = changes.map(c => el("tr", {}, el("td", {}, String(c.change || "")), el("td", { class: "mono" }, String(c.path || "")),
    el("td", {}, c.note ? String(c.note) : "", c.diff ? el("details", {}, el("summary", {}, t("Differences")), el("pre", {}, String(c.diff))) : null)));
  const row = el("div", { class: "row" });
  openCard(main, el("div", { class: "card", id: "review-card" }, el("h2", {}, t("The HOME of {name} on {host} changed", { name: s.name, host })),
    el("p", {}, t("Since its last start ({since}), in files that decide what runs at a start (shell, git, ssh, the agent's settings, hooks, plugins, skills). If you did not make these changes, check them before you resume it.", { since: review.since || "?" })),
    review.complete === false ? notice(t("Its HOME has more files than were checked: not every change is named."), "warn") : null,
    rows.length ? table([t("Change"), t("File"), ""], rows) : null, row), s);
  confirmIn(row, t("Resume {name} on {host} with these changes?", { name: s.name, host }), t("Resume anyway"), resume,
    { kind: "primary", trigger: button, onCancel: () => { const c = document.getElementById("review-card"); if (c) dropCard(c); } });
}

// A row whose host could not be asked: as last seen, greyed, its buttons off.
function markRowStale(view, entry) {
  const seen = view.seen[entry.start.host];
  const seconds = seen ? Math.max(0, Math.round((Date.now() - seen) / 1000)) : null;
  entry.stale = true;
  entry.row.classList.add("stale");
  for (const control of controlsIn(entry.row)) control.disabled = true;
  const note = entry.row.querySelector(".stale-note");
  if (note) note.textContent = ` ${seconds === null ? t("(not seen yet)") : t("(last seen {seconds} s ago)", { seconds })}`;
}

// The rows of STARTS into the kept table: unchanged ones stay, changed ones are drawn anew, gone ones go; those of a
// host STALE says could not be asked stay, greyed.
function updateStartRows(view, starts, main, stale) {
  const keys = new Set(starts.map(startKey));
  for (const [key, entry] of view.rows) {
    if (keys.has(key)) continue;
    if (stale(entry.start.host)) markRowStale(view, entry);
    else { entry.row.remove(); view.rows.delete(key); }
  }
  for (const s of starts) {
    const signature = startSignature(s, starts);
    const entry = view.rows.get(startKey(s));
    if (entry && entry.signature === signature && !entry.stale) continue;
    const row = startRow(s, starts, main, view.host === "all");
    if (entry) entry.row.replaceWith(row); else view.body.append(row);
    view.rows.set(startKey(s), { signature, row, start: s, stale: false });
  }
  layoutStarts(view, main, [...starts.map(s => view.rows.get(startKey(s))), ...[...view.rows.values()].filter(e => e.stale)]);
}

// --- the starts, grouped: by host (a header row each: where it is, its counts, whether it answers), and in a host
// each start with what belongs to it below it, indented (the MCP server sandboxes it reaches, its helpers and
// companions when the listing names them, its Podman pod). Hosts and starts fold and unfold; what was chosen is kept
// across the polls and in this browser (keyed by host and start id).
const OPEN_KEY = "sandbox-ai-starts-open";
const OPEN_KEPT = 400;  // choices kept at most (the oldest go first)
const openChoices = { map: null };

function openMap() {
  if (openChoices.map === null) {
    openChoices.map = {};
    try {
      const kept = JSON.parse(localStorage.getItem(OPEN_KEY) || "{}");
      if (kept && typeof kept === "object" && !Array.isArray(kept)) for (const [key, value] of Object.entries(kept)) if (typeof value === "boolean") openChoices.map[key] = value;
    } catch (e) { /* not kept in this browser: open, as by default */ }
  }
  return openChoices.map;
}

// Whether the host or start KEY ("h HOST", "s HOST ID") is unfolded: as chosen, else unfolded.
function isOpen(key) { const map = openMap(); return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : true; }

function setOpen(key, open) {
  const map = openMap();
  delete map[key];
  map[key] = Boolean(open);
  const keys = Object.keys(map);
  for (const old of keys.slice(0, Math.max(0, keys.length - OPEN_KEPT))) delete map[old];
  try { localStorage.setItem(OPEN_KEY, JSON.stringify(map)); } catch (e) { /* this page only */ }
}

const hostOpenKey = (host) => `h ${host}`;
const startOpenKey = (s) => `s ${startKey(s)}`;

// The host's entry of the hosts list (state.hosts), or null.
function hostEntry(host) { return state.hosts.find(h => h.name === host) || null; }

// Where HOST is, in words: this machine, or a host of the list reached over SSH.
function hostWhere(host) {
  const h = hostEntry(host);
  if (host === "local" || (h && h.kind === "local")) return t("this machine");
  return h && h.ssh ? t("remote, over SSH: {ssh}", { ssh: `${h.ssh}${h.port && h.port !== 22 ? ":" + h.port : ""}` }) : t("remote, over SSH");
}

// The host of a row, on every row: its name, and for another machine that it is remote.
function hostMarkerCell(host) {
  const local = host === "local" || (hostEntry(host) || {}).kind === "local";
  return el("td", { class: "mono host-cell" }, el("span", { class: `host-marker ${local ? "local" : "remote"}`, title: hostWhere(host) }, host,
    local ? null : el("span", { class: "host-where" }, ` · ${t("remote")}`)));
}

// The MCP server names start S reaches or names (list --json: "mcp", what it starts with; "mcp_running", what its
// run reaches now).
function mcpNamesOf(s) {
  return [...new Set([...(Array.isArray(s.mcp) ? s.mcp : []), ...(Array.isArray(s.mcp_running) ? s.mcp_running : [])].filter(n => typeof n === "string" && n))];
}

// The starts of one host as a tree, one level deep: {roots, children: Map(key -> [start]), notes: Map(key ->
// [text])}. An MCP server sandbox goes below the first start it serves ("serves"); a start that names its lead
// ("helper_of", "companion_of", "parent", "lead": its name, id or run) below that one; a lead's helper's own helper
// below the lead too. What a start names but is not listed (an MCP server that does not run, its pod, its
// companions) is a note below it.
const START_PARENT_KEYS = ["helper_of", "companion_of", "parent", "lead"];

function startTree(starts) {
  const byRef = new Map();
  for (const s of starts) if (s.kind !== "mcp") for (const ref of [s.name, s.id, s.run]) if (typeof ref === "string" && ref && !byRef.has(ref)) byRef.set(ref, s);
  const parentOf = new Map();
  for (const s of starts) {
    const refs = s.kind === "mcp" ? (Array.isArray(s.serves) ? s.serves : []) : START_PARENT_KEYS.map(key => s[key]);
    const parent = refs.map(ref => (typeof ref === "string" ? byRef.get(ref) : undefined)).find(p => p && p !== s);
    if (parent) parentOf.set(s, parent);
  }
  // the start at the top of S's chain; in a cycle (two naming each other) each one stands on its own
  const topOf = (s) => {
    const seen = new Set([s]);
    let at = s;
    while (parentOf.has(at)) {
      const next = parentOf.get(at);
      if (seen.has(next)) return s;
      seen.add(next); at = next;
    }
    return at;
  };
  const roots = [], children = new Map(), notes = new Map();
  const rootOf = new Map(starts.map(s => [s, topOf(s)]));
  for (const s of starts) {
    const root = rootOf.get(s);
    if (root === s) { roots.push(s); continue; }
    const key = startKey(root);
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(s);
  }
  const servers = new Map(starts.filter(s => s.kind === "mcp").map(s => [String(s.name).replace(/^mcp-/, ""), s]));
  for (const s of roots) {
    const lines = [];
    if (s.kind !== "mcp") {
      for (const name of mcpNamesOf(s)) {
        const server = servers.get(name);
        if (!server) lines.push(t("MCP server {server}: not running now (it starts with {name})", { server: name, name: s.name }));
        else if (rootOf.get(server) !== s) lines.push(t("MCP server {server}: shown below {start}", { server: name, start: rootOf.get(server).name }));
      }
      if (typeof s.pod === "string" && s.pod) lines.push(t("Podman pod: {pod}", { pod: s.pod }));
      else if (s.backend === "podman" && s.run && (s.state === "running" || s.state === "paused")) lines.push(t("Podman: its run {run}", { run: s.run }));
      const companions = (Array.isArray(s.companions) ? s.companions : []).filter(c => typeof c === "string" && c);
      if (companions.length) lines.push(t("With: {companions}", { companions: companions.join(", ") }));
      // list --json's "teams": the teams it belongs to (the names of their team-NAME boards); its fellow members are
      // the other starts of the listing in the same team
      for (const team of (Array.isArray(s.teams) ? s.teams : []).filter(x => typeof x === "string" && x)) {
        const members = starts.filter(o => o !== s && o.kind !== "mcp" && Array.isArray(o.teams) && o.teams.includes(team)).map(o => o.name);
        lines.push(members.length ? t("Team {team}: {members}", { team, members: members.join(", ") }) : t("Team {team}", { team }));
      }
    }
    if (lines.length) notes.set(startKey(s), lines);
  }
  return { roots, children, notes };
}

// A fold button for KEY (a host's or a start's): a click folds or unfolds it, then ONTOGGLE lays the table out anew.
// showFold() says its state (aria-expanded) and what it folds; the button stays (the focus with it).
function foldButton(key, onToggle) {
  return el("button", { class: "small fold", onclick: () => { setOpen(key, !isOpen(key)); onToggle(); } });
}

function showFold(button, open, label, count) {
  button.setAttribute("aria-expanded", open ? "true" : "false");
  button.setAttribute("aria-label", label);
  button.setAttribute("title", label);
  button.disabled = false;
  clear(button).append(el("span", { "aria-hidden": "true" }, open ? "▾" : "▸"));
  if (count) button.append(` ${count}`);
}

// How a host answers, for its header: answering (as of the last listing), or not, with when it was last seen.
function hostAnswerText(view, host) {
  const error = (view.errors || {})[host];
  const seen = view.seen[host];
  const seconds = seen ? Math.max(0, Math.round((Date.now() - seen) / 1000)) : null;
  if (!error) return { text: seconds === null ? t("answering") : seconds < 2 ? t("answering") : t("answering (listed {seconds} s ago)", { seconds }), kind: "ok" };
  return { text: seconds === null ? t("not answering, not seen yet") : t("not answering: last seen {seconds} s ago", { seconds }), kind: "fail", error };
}

// The counts of a host's starts: how many, and how many run, are paused, stopped.
function hostCountText(starts) {
  const count = (value) => starts.filter(s => s.state === value).length;
  const parts = [t("{count} start(s)", { count: starts.length })];
  for (const [value, word] of [["running", N_("{count} running")], ["paused", N_("{count} paused")], ["stopped", N_("{count} stopped")]]) if (count(value)) parts.push(t(word, { count: count(value) }));
  return parts.join(", ");
}

const ANSWER_CLASS = { ok: "host-answer status-ok", fail: "host-answer status-fail" };
const COLUMNS = 8;  // the starts table's columns (its host, name, agent, state, workspace, HOME, started, actions)

// The header row of HOST (kept between the polls, with its fold button).
function hostHeader(view, host, starts, main) {
  let group = view.groups.get(host);
  if (!group) {
    const local = host === "local" || (hostEntry(host) || {}).kind === "local";
    group = { fold: foldButton(hostOpenKey(host), () => layoutStarts(view, main)), where: el("span", { class: `host-marker ${local ? "local" : "remote"}` }),
      counts: el("span", { class: "host-counts" }), answer: el("span", { class: "host-answer" }) };
    group.row = el("tr", { class: "host-group", "data-group": host }, el("td", { colspan: COLUMNS },
      group.fold, " ", el("strong", { class: "mono" }, host), " ", group.where, " ", group.counts, " ", group.answer));
    view.groups.set(host, group);
  }
  const open = isOpen(hostOpenKey(host));
  const answer = hostAnswerText(view, host);
  showFold(group.fold, open, open ? t("Collapse the starts of {host}", { host }) : t("Expand the starts of {host}", { host }));
  group.where.textContent = hostWhere(host);
  group.counts.textContent = hostCountText(starts);
  group.answer.textContent = answer.text;
  group.answer.className = ANSWER_CLASS[answer.kind];
  if (answer.error) group.answer.setAttribute("title", String(answer.error)); else group.answer.removeAttribute("title");
  return group.row;
}

// The rows of ENTRIES ({row, start, stale}; the listing's order, the stale ones last) in the table: by host, each
// start followed by what belongs to it; what is folded is hidden. Without ENTRIES: the last ones, laid out anew (a
// fold changed).
function layoutStarts(view, main, entries) {
  if (entries) view.order = entries;
  const list = (view.order || []).filter(entry => view.rows.get(startKey(entry.start)) === entry);
  const hosts = [...new Set(list.map(entry => entry.start.host))];
  const known = state.hosts.map(h => h.name);
  hosts.sort((a, b) => (known.includes(a) ? known.indexOf(a) : known.length) - (known.includes(b) ? known.indexOf(b) : known.length) || a.localeCompare(b));
  const order = [];
  const notes = new Map();
  // the cards opened below a start (openCard): right after its row, hidden with it; a closed one's row goes
  view.cardRows = (view.cardRows || []).filter(c => {
    const open = c.card.holder === c.row && c.card.parentNode && c.card.parentNode.parentNode === c.row;  // still in its row
    if (!open) c.row.remove();
    return open;
  });
  const placed = new Set();
  const cardsBelow = (row, key) => {
    for (const c of view.cardRows) if (c.key === key) { c.row.hidden = row.hidden; order.push(c.row); placed.add(c); }
  };
  for (const host of hosts) {
    const here = list.filter(entry => entry.start.host === host);
    const byKey = new Map(here.map(entry => [startKey(entry.start), entry]));
    const tree = startTree(here.map(entry => entry.start));
    const hostOpen = isOpen(hostOpenKey(host));
    order.push(hostHeader(view, host, here.map(entry => entry.start), main));
    for (const root of tree.roots) {
      const entry = byKey.get(startKey(root));
      const kids = tree.children.get(startKey(root)) || [];
      const lines = tree.notes.get(startKey(root)) || [];
      const open = isOpen(startOpenKey(root));
      entry.row.hidden = !hostOpen;
      entry.row.classList.remove("related");
      const slot = entry.row.querySelector(".tree-slot");
      if (slot) {
        const count = kids.length + lines.length;
        let fold = slot.querySelector(".fold");
        if (!count) clear(slot);
        else {
          if (!fold) { fold = foldButton(startOpenKey(root), () => layoutStarts(view, main)); clear(slot).append(fold, " "); }
          showFold(fold, open, open ? t("Collapse what belongs to {name} on {host}", { name: root.name, host }) : t("Expand what belongs to {name} on {host} ({count})", { name: root.name, host, count }), count);
        }
      }
      order.push(entry.row);
      cardsBelow(entry.row, startKey(root));
      for (const kid of kids) {
        const child = byKey.get(startKey(kid));
        child.row.hidden = !hostOpen || !open;
        child.row.classList.add("related");
        const kidSlot = child.row.querySelector(".tree-slot");
        if (kidSlot && !kidSlot.querySelector(".tree-branch")) clear(kidSlot).append(el("span", { class: "tree-branch", "aria-hidden": "true" }, "↳ "));
        order.push(child.row);
        cardsBelow(child.row, startKey(kid));
      }
      if (lines.length) {
        const key = startKey(root);
        const kept = (view.notes || new Map()).get(key);
        const row = kept && kept.text === JSON.stringify(lines) ? kept.row
          : el("tr", { class: "related related-note", "data-for": key }, el("td", {}, ""), el("td", { colspan: COLUMNS - 1 },
            el("span", { class: "tree-branch", "aria-hidden": "true" }, "↳ "), lines.map((line, i) => el("span", { class: "hint note-line" }, i ? " · " : "", line))));
        row.hidden = !hostOpen || !open;
        notes.set(key, { row, text: JSON.stringify(lines) });
        order.push(row);
      }
    }
  }
  for (const c of view.cardRows) if (!placed.has(c)) { c.row.hidden = false; order.push(c.row); }  // its start is gone: the card stays, last
  for (const [host, group] of view.groups) if (!hosts.includes(host)) { group.row.remove(); view.groups.delete(host); }
  for (const [key, kept] of view.notes || new Map()) if (!notes.has(key) || notes.get(key).row !== kept.row) kept.row.remove();
  view.notes = notes;
  if (order.some((row, i) => view.body.children[i] !== row) || view.body.children.length !== order.length) view.body.append(...order);
  view.table.hidden = !view.rows.size && !view.cardRows.length; view.empty.hidden = Boolean(view.rows.size);
  if (view.folds) for (const button of view.folds) button.disabled = !view.rows.size;
}

// Unfold (OPEN) or fold every host and start shown.
function foldAll(view, main, open) {
  for (const entry of view.rows.values()) { setOpen(hostOpenKey(entry.start.host), open); setOpen(startOpenKey(entry.start), open); }
  layoutStarts(view, main);
}

// What a listing found (an Error, a host's list, or {starts, errors, seen} of every host) into the kept table.
function applyStarts(view, found, main) {
  clear(view.status);
  const now = Date.now();
  let starts = null, errors = {};
  if (view.host === "all" && found && !(found instanceof Error) && Array.isArray(found.starts)) {
    starts = found.starts.filter(x => x && typeof x === "object" && typeof x.name === "string" && typeof x.host === "string");
    errors = found.errors && typeof found.errors === "object" ? found.errors : {};
    // how old each host's list is, by the service's clock, counted on here from now (never this browser's clock
    // against the service's)
    for (const [host, age] of Object.entries(found.age || {})) if (typeof age === "number" && Number.isFinite(age) && !notAsked(errors, host)) view.seen[host] = now - Math.max(0, age) * 1000;
  } else if (view.host !== "all" && Array.isArray(found)) {
    starts = found.filter(x => x && typeof x === "object" && typeof x.name === "string").map(x => Object.assign({}, x, { host: view.host }));
    view.seen[view.host] = now;
  }
  if (starts === null) {
    const why = found && found.message ? found.message : t("not a list of starts");
    view.status.append(notice(t("Could not list the starts: {error}", { error: why }), "error"));
    view.errors = {};
    for (const entry of view.rows.values()) { markRowStale(view, entry); view.errors[entry.start.host] = why; }
    if (view.host !== "all") view.errors[view.host] = why;
    layoutStarts(view, main);
    return;
  }
  view.errors = errors;
  for (const [host, why] of Object.entries(errors)) view.status.append(notice(t("Could not list the starts of {host}: {error}", { host, error: why }), "error"));
  const stale = (host) => notAsked(errors, host);
  updateStartRows(view, starts, main, stale);
  retireCards(starts, stale);
}

// With the page's poll (a background request): the table of the starts page, if it is the one shown. One asked for
// while a listing runs (a Stop, a Rename just done) runs right after it, never dropped.
// list or list --all (MCP servers that do not run too), of one host or of every host
function startsPath(host) {
  if (host === "all") return state.startsEvery ? "/api/starts/all" : "/api/starts";
  return hostPath(state.startsEvery ? "/starts/all" : "/starts", host);
}

async function refreshStarts(main, now) {
  const view = startsView, host = view.host;
  if (view.main !== main || !view.body || !view.body.isConnected) return;
  if (view.loading) { view.again = true; return; }
  view.loading = true;
  let found;
  try { found = await api("GET", startsPath(host), undefined, { background: !now }); }
  catch (e) { found = e; }
  finally { view.loading = false; }
  if (state.page === "starts" && view.main === main && view.host === host && view.body.isConnected) applyStarts(view, found, main);
  if (view.again) { view.again = false; refreshStarts(main, true); }
}

// The first visit: nothing runs or is kept, and no other machine is managed yet.
function firstRunCard() {
  const doctor = el("span", { class: "hint" }, t("Checking…"));
  api("GET", hostPath("/doctor", "local")).then((results) => {
    if (typeof results === "string") { doctor.textContent = t("an older doctor: open it to read it"); return; }
    const list = doctorChecks(results);
    const problems = list.filter(r => r.status === "fail").length, warnings = list.filter(r => r.status === "warn").length;
    doctor.textContent = problems ? t("{problems} problem(s), {warnings} warning(s)", { problems, warnings }) : warnings ? t("no problems, {warnings} warning(s)", { warnings }) : t("no problems");
  }).catch((e) => { doctor.textContent = e.message; });
  const step = (title, ...rest) => el("li", {}, el("strong", {}, title), " ", ...rest);
  return el("div", { class: "card first-run" }, el("h2", {}, t("First steps")),
    el("ol", {},
      step(t("The doctor of this machine:"), doctor, " ", el("button", { onclick: () => show("doctor") }, t("Open the doctor"))),
      step(t("Terminals:"), state.me && state.me.terminals ? t("available in this page") : t("they need xterm.js on the server: {command}", { command: "sandbox-ai web vendor" })),
      step(t("Other machines:"), el("button", { onclick: () => show("hosts") }, t("Add a host"))),
      step(t("Your first agent:"), el("button", { onclick: () => { const agent = document.getElementById("agent"); if (agent) agent.focus(); } }, t("Start one below"))),
      step(t("Questions of the sandboxes:"), notificationsButton() || el("span", { class: "hint" }, t("this browser has no desktop notifications")))));
}

pages.starts = async (main) => {
  const host = state.startsHost === "all" || state.hosts.some(h => h.name === state.startsHost) ? state.startsHost : state.host;
  state.startsHost = host;
  const all = host === "all";
  const title = () => (all ? t("Starts and sessions on every host") : t("Starts and sessions on {host}", { host }));
  main.append(el("h1", {}, title()), el("p", { class: "hint" }, t("Loading…")));
  let found;
  const live = pageLive();
  try { found = await api("GET", startsPath(host)); } catch (e) { found = e; }
  if (state.page !== "starts" || state.startsHost !== host || !live()) return;
  const view = startsView;
  view.main = main; view.host = host; view.rows = new Map(); view.loading = false; view.again = false; view.seen = {};
  view.groups = new Map(); view.notes = new Map(); view.order = []; view.errors = {}; view.cardRows = [];
  view.slot = el("div", { class: "card-slot" });
  view.status = el("div"); view.body = el("tbody");
  view.table = el("table", { class: "starts" }, el("caption", { class: "sr-only" }, all ? t("Starts on every host") : t("Starts on {host}", { host })),
    el("thead", {}, el("tr", {}, [t("Host"), t("Name"), t("Agent"), t("State"), t("Workspace"), "HOME", t("Started"), ""].map(c => el("th", {}, c)))), view.body);
  view.folds = [el("button", { class: "small", onclick: () => foldAll(view, main, true) }, t("Expand all")),
    el("button", { class: "small", onclick: () => foldAll(view, main, false) }, t("Collapse all"))];
  const listed = all ? (found && Array.isArray(found.starts) ? found.starts : []) : (Array.isArray(found) ? found : []);
  view.empty = !listed.length && state.hosts.length <= 1 && (all || host === "local") ? firstRunCard()
    : el("p", { class: "hint" }, all ? t("Nothing runs or is kept on any host.") : t("Nothing runs or is kept on this host."));
  const chooser = el("select", { id: "starts-host", onchange: (event) => {
    state.startsHost = event.target.value; state.formHost = null;
    if (state.startsHost !== "all") { state.host = state.startsHost; try { localStorage.setItem("sandbox-ai-host", state.host); } catch (e) { /* this page only */ } }
    show("starts");
  } }, el("option", { value: "all", selected: all }, t("all hosts")), state.hosts.map(h => el("option", { value: h.name, selected: h.name === host }, h.name)));
  const every = el("input", { type: "checkbox", id: "starts-every", checked: Boolean(state.startsEvery), onchange: (event) => {
    state.startsEvery = event.target.checked;
    try { localStorage.setItem("sandbox-ai-starts-every", state.startsEvery ? "1" : ""); } catch (e) { /* this page only */ }
    show("starts");
  } });
  const tools = el("div", { class: "row" }, el("label", { for: "starts-host" }, t("Host")), chooser,
    el("label", {}, every, " ", t("MCP servers that do not run too")),
    all ? null : el("button", { onclick: () => gcCard(main, host) }, t("Clean up…")), ...view.folds);
  clear(main).append(el("h1", {}, title()), tools, view.slot, view.status, el("div", { class: "table-scroll" }, view.table), view.empty);
  applyStarts(view, found, main);
  // a new start: in a terminal of the page, detachable like every start (Ctrl-b d), on the host chosen in its form
  const target = state.formHost && state.hosts.some(h => h.name === state.formHost) ? state.formHost : all ? state.host : host;
  const heading = el("h2", {}, t("New start on {host}", { host: target }));
  const form = el("div", { class: "card", id: "new-start" });
  main.append(heading, form);
  await drawStartForm(form, heading, target);
};

// The preset categories in the order sandbox-ai lists them (presets.CATEGORIES), in the page's words.
const PRESET_CATEGORIES = [["simple", N_("Simple")], ["development", N_("Development")], ["llm", N_("Local models")], ["analysis", N_("Analysis")],
  ["research", N_("Research")], ["media", N_("Media")], ["privacy", N_("Privacy")], ["testing", N_("Testing")], ["fun", N_("Fun")]];

// Whether a preset's VPN value must name a Tor profile (the tor preset's: its name or label says Tor).
function torValue(key, spec) { return key === "tor" || /\bTor\b/.test(String((spec && spec.label) || "")); }

// What `presets check` says is missing ({ok, output}: one "missing: ..." line each), as a list of texts.
function presetProblems(r) {
  const lines = String((r && r.output) || "").split("\n").map(x => x.trim()).filter(Boolean);
  const missing = lines.filter(x => x.startsWith("missing: ")).map(x => x.slice("missing: ".length));
  return missing.length ? missing : lines;
}

// The form of a new start on HOST, into CARD (HEADING says the host): its own host selector; the agent; the model
// (the agent's provider, or an LLM server of that host as --llm NAME or NAME:MODEL; "only this server" adds
// --no-provider-defaults and is off for the cloud); a preset of that host, by category, only those for the agent,
// with one field per value it takes (a folder, a port, an LLM server or a VPN profile of that host: a Tor value only
// Tor profiles) and its MCP servers as that host has them; the free options for the rest. Start checks the preset
// there first (presets check) and shows what is missing instead of starting.
async function drawStartForm(card, heading, host) {
  const live = pageLive();
  clear(card);
  heading.textContent = t("New start on {host}", { host });
  const hostChoice = el("select", { id: "start-host" }, state.hosts.map(h => el("option", { value: h.name, selected: h.name === host }, h.kind === "local" ? t("{name} (this machine)", { name: h.name }) : h.name)));
  hostChoice.addEventListener("change", () => { state.formHost = hostChoice.value; drawStartForm(card, heading, hostChoice.value); });
  const agent = el("select", { id: "agent" }, AGENTS.map(a => el("option", { value: a }, a)));
  const agentOf = () => (AGENTS.includes(agent.value) ? agent.value : AGENTS[0]);  // a select shows its first until chosen
  const model = el("select", { id: "start-model" });
  // as the app's wizard has it: a server chosen, nothing else (no cloud provider) unless unticked
  const onlyServer = el("input", { type: "checkbox", id: "start-only-server", checked: true });
  const modelHint = el("span", { class: "hint" });
  const servers = [], profiles = [], mcpNames = new Set();
  let presets = [], chosen = null, fields = [];
  const presetsLoaded = { done: false, error: null };
  const syncOnly = () => { onlyServer.disabled = !model.value || model.disabled; if (onlyServer.disabled && !model.value) onlyServer.title = t("only with a server of its own: the cloud is the agent's provider"); else onlyServer.removeAttribute("title"); };
  const drawModels = () => {
    const was = model.value;
    clear(model).append(el("option", { value: "" }, t("the agent's provider (cloud)")));
    for (const server of servers) {
      const why = modelProblem(server, agentOf());
      const label = `${server.name}${server.model ? ` (${server.model})` : ""}${why ? ` — ${why}` : ""}`;
      model.append(el("option", { value: server.name, disabled: Boolean(why), title: why || null }, label));
      for (const m of server.models || []) model.append(el("option", { value: `${server.name}:${m}`, disabled: Boolean(why), title: why || null }, `${server.name}:${m}`));
    }
    const still = [...model.children].find(o => o.value === was && !o.disabled);
    model.value = still ? was : "";
    // a preset that names the LLM server itself: the model is its value
    const byPreset = fields.some(f => f.spec.kind === "llm");
    model.disabled = byPreset;
    if (byPreset) model.value = "";
    modelHint.textContent = byPreset ? t("the preset chooses the LLM server (below)") : "";
    syncOnly();
    for (const f of fields) if (f.spec.kind === "llm") f.redraw();
  };
  model.addEventListener("change", syncOnly);
  const preset = el("select", { id: "preset" });
  const presetHint = el("span", { class: "hint" }, "");
  const presetValues = el("div", { class: "form preset-values wide" });
  const presetMcp = el("div", { class: "preset-mcp wide" });
  // the presets of that host for the chosen agent, by category
  const drawPresets = () => {
    const was = preset.value;
    clear(preset).append(el("option", { value: "" }, t("none")));
    const usable = presets.filter(p => !Array.isArray(p.agents) || p.agents.includes(agentOf()));
    const known = new Set(PRESET_CATEGORIES.map(([key]) => key));
    for (const [key, label] of [...PRESET_CATEGORIES, ...[...new Set(usable.map(p => p.category || "simple"))].filter(c => !known.has(c)).map(c => [c, c])]) {
      const group = usable.filter(p => (p.category || "simple") === key).sort((a, b) => String(a.id).localeCompare(String(b.id)));
      if (group.length) preset.append(el("optgroup", { label: known.has(key) ? t(label) : label }, group.map(p => el("option", { value: p.id }, `${p.id}: ${p.title || ""}`))));
    }
    const still = usable.some(p => p.id === was);
    preset.value = still ? was : "";
    if (!still && was) choosePreset();
    if (presetsLoaded.error) presetHint.textContent = t("this host's sandbox-ai has no presets yet");
  };
  // one field per value of the chosen preset (presets show NAME --json of that host), and its MCP servers
  const valueField = (key, spec) => {
    const id = `preset-value-${key}`, kind = spec.kind || "text";
    const label = el("label", { for: id }, String(spec.label || key), spec.optional ? ` ${t("(optional)")}` : "");
    let input;
    const f = { key, spec, label, redraw: () => {} };
    if (kind === "llm" || kind === "vpn") {
      input = el("select", { id });
      f.redraw = () => {
        const was = input.value || (spec.default !== undefined ? String(spec.default) : "");
        clear(input).append(el("option", { value: "" }, spec.optional ? t("none") : t("choose…")));
        if (kind === "llm") for (const server of servers) {
          const why = modelProblem(server, agentOf());
          input.append(el("option", { value: server.name, disabled: Boolean(why), title: why || null }, `${server.name}${why ? ` — ${why}` : ""}`));
          for (const m of server.models || []) input.append(el("option", { value: `${server.name}:${m}`, disabled: Boolean(why) }, `${server.name}:${m}`));
        } else for (const p of profiles.filter(x => !torValue(key, spec) || x.type === "tor")) input.append(el("option", { value: p.profile }, `${p.profile} (${p.type || "?"})`));
        const still = [...input.children].find(o => o.value === was && !o.disabled);
        input.value = still ? was : "";
        if (input.children.length === 1) input.append(el("option", { value: "", disabled: true }, kind === "llm" ? t("no LLM server on {host} yet: add one on the page LLM servers", { host })
          : torValue(key, spec) ? t("no Tor profile on {host} yet: add one on the page VPN profiles", { host }) : t("no VPN profile on {host} yet: add one on the page VPN profiles", { host })));
      };
      f.redraw();
    } else if (kind === "port" || kind === "number") {
      input = el("input", { id, type: "number", min: kind === "port" ? 1 : 0, max: kind === "port" ? 65535 : null, placeholder: spec.default !== undefined ? String(spec.default) : "" });
    } else {
      input = el("input", { id, class: ["folder", "file", "device", "host", "secret"].includes(kind) ? "mono" : null,
        placeholder: spec.default !== undefined ? String(spec.default) : kind === "folder" ? t("{path} (on that host)", { path: "/home/you/samples" }) : kind === "device" ? "/dev/ttyUSB0" : "" });
    }
    f.input = input;
    f.value = () => String(input.value || "").trim();
    return f;
  };
  const specHint = (spec) => el("span", { class: "hint" }, spec.hint ? String(spec.hint) : "");
  const choosePreset = async () => {
    clear(presetValues); clear(presetMcp); fields = []; chosen = null; presetHint.textContent = "";
    const id = preset.value;
    if (!id) { drawModels(); return; }
    const listed = presets.find(p => p.id === id) || {};
    presetHint.textContent = t("Loading…");
    let found;
    try { found = await api("GET", hostPath(`/presets/${encodeURIComponent(id)}`, host)); }
    catch (e) { if (preset.value === id) { presetHint.textContent = ""; presetValues.append(errorNotice(e)); } return; }
    if (preset.value !== id || !card.isConnected) return;
    chosen = found && typeof found === "object" ? found : listed;
    presetHint.textContent = `${chosen.description || listed.description || ""}`;
    const values = chosen.values && typeof chosen.values === "object" ? chosen.values : {};
    fields = Object.entries(values).filter(([, spec]) => spec && typeof spec === "object").map(([key, spec]) => valueField(key, spec));
    for (const f of fields) presetValues.append(f.label, el("div", {}, f.input, " ", specHint(f.spec)));
    const servers_ = (Array.isArray(chosen.mcp) ? chosen.mcp : []).filter(x => typeof x === "string" && !x.includes("{"));
    if (servers_.length) presetMcp.append(el("span", {}, t("MCP servers")), el("ul", {}, servers_.map(server => {
      const name = server.split(":").pop(), there = mcpNames.has(name) || mcpNames.has(server);
      return el("li", { class: there ? "status-ok" : "status-warn" }, el("span", { class: "mono" }, server), " ",
        there ? t("added on {host}", { host }) : t("not added on {host} yet: {command}", { host, command: `sandbox-ai mcp add ${server}` }),
        there ? null : el("button", { class: "small", onclick: () => { state.host = host; show("mcp"); } }, t("Open MCP servers")));
    })));
    for (const note of (Array.isArray(chosen.notes) ? chosen.notes : []).slice(0, 3)) presetMcp.append(el("p", { class: "hint" }, String(note)));
    drawModels();
  };
  preset.addEventListener("change", choosePreset);
  agent.addEventListener("change", () => { drawModels(); drawPresets(); });
  drawModels(); drawPresets();
  // what that host has: its LLM servers (and their models), presets, VPN profiles and MCP servers, asked at once
  const asked = [
    api("GET", hostPath("/llm", host)).then((list) => {
      for (const server of (Array.isArray(list) ? list : []).filter(x => x && typeof x.name === "string")) {
        servers.push(server);
        api("GET", hostPath(`/llm/${encodeURIComponent(server.name)}/models`, host)).then((models) => {
          server.models = (Array.isArray(models) ? models : []).map(m => m && m.name).filter(n => typeof n === "string" && n);
          if (card.isConnected) drawModels();
        }).catch(() => {});
      }
    }).catch(() => {}),
    api("GET", hostPath("/presets", host)).then((list) => { presets = (Array.isArray(list) ? list : []).filter(p => p && typeof p.id === "string"); })
      .catch((e) => { presetsLoaded.error = e; }),
    api("GET", hostPath("/vpn", host)).then((list) => { for (const p of (Array.isArray(list) ? list : []).filter(x => x && typeof x.profile === "string")) profiles.push(p); }).catch(() => {}),
    api("GET", hostPath("/mcp", host)).then((list) => { for (const x of (Array.isArray(list) ? list : []).filter(x => x && typeof x.name === "string")) mcpNames.add(x.name); }).catch(() => {}),
  ];
  const name = el("input", { id: "name", placeholder: t("automatic"), pattern: "[A-Za-z0-9][A-Za-z0-9_.-]{0,47}" });
  const workspace = el("input", { id: "workspace", placeholder: "/home/you/src/project", class: "mono" });
  const options = el("textarea", { id: "options", rows: 3, class: "mono", placeholder: "--mode high\n--allow-pypi\n-mm sonnet", "aria-describedby": "options-hint" });
  const startStatus = el("div", { class: "wide", id: "start-status" });
  const start = el("button", { class: "primary", onclick: () => busy(start, async () => {
    clear(startStatus);
    let list;
    try { list = shellWords(options.value); }
    catch (e) { startStatus.append(notice(t("Options: {error}", { error: e.message }), "error")); return; }
    // what is started is what the form says now: the preset checked on that host first, with these values
    const id = preset.value, values = {};
    for (const f of fields) if (f.value()) values[f.key] = f.value();
    let presetArgs = [];
    if (id) {
      let r;
      try { r = await api("POST", hostPath(`/presets/${encodeURIComponent(id)}/check`, host), { values, agent: agentOf() }); }
      catch (e) { startStatus.append(errorNotice(e, t("The preset {preset} could not be checked on {host}", { preset: id, host }))); return; }
      if (!r || !r.ok) {
        startStatus.append(notice(t("The preset {preset} is not ready on {host}: nothing was started.", { preset: id, host }), "error"),
          el("ul", { class: "preset-problems" }, presetProblems(r).map(line => el("li", {}, line))));
        return;
      }
      presetArgs = ["--preset", id, ...Object.entries(values).flatMap(([key, value]) => ["--preset-value", `${key}=${value}`])];
    }
    const server = model.disabled ? "" : model.value;  // "only this server" means nothing for the cloud
    list = withModel([...presetArgs, ...list], server, onlyServer.checked && Boolean(server));
    openTerminal({ kind: "start", host, agent: agentOf(), name: textOf(name), workspace: textOf(workspace), options: JSON.stringify(list) }, textOf(name) || t("new {agent}", { agent: agentOf() }));
  }) }, t("Start in a terminal"));
  card.append(el("div", { class: "form" },
    el("label", { for: "start-host" }, t("Host")), hostChoice,
    el("label", { for: "agent" }, t("Agent")), agent,
    el("label", { for: "start-model" }, t("Model")), el("div", { class: "row" }, model,
      el("label", {}, onlyServer, " ", t("only this server")), el("button", { class: "small", onclick: () => { state.host = host; show("llm"); } }, t("Add a model server…")), modelHint),
    el("label", { for: "name" }, t("Name")), name,
    el("label", { for: "workspace" }, t("Workspace")), workspace,
    el("label", { for: "preset" }, t("Preset")), el("div", {}, preset, " ", presetHint),
    presetValues, presetMcp,
    el("label", { for: "options" }, t("Options")), el("div", {}, options,
      el("div", { class: "hint", id: "options-hint" }, t("Further options, as on a command line (one or more per line): {examples}; quotes keep spaces together.", { examples: "-mm sonnet, --mode high" }))),
    startStatus,
    el("span"), el("div", { class: "row" }, start,
      el("span", { class: "hint" }, t("Detach with Ctrl-b d: the start runs on.")))));
  await Promise.all(asked);
  if (!live() || !card.isConnected) return;
  drawModels(); drawPresets();
}

// Why AGENT cannot use the LLM server SERVER (llm list --json: "agents" are those that can; llm.agent_problem), or "".
function modelProblem(server, agent) {
  if (!Array.isArray(server.agents) || server.agents.includes(agent)) return "";
  return agent === "claude" ? t("not for Claude Code: it needs a server with the Anthropic Messages API") : t("not for {agent}", { agent });
}

// The options of a new start with the model chosen: CHOICE (NAME or NAME:MODEL of an LLM server; "" the agent's
// provider) as --llm CHOICE, ONLY adds --no-provider-defaults; any --model, --llm or --no-provider-defaults of the
// options goes (the launcher refuses --model with --llm).
function withModel(list, choice, only) {
  if (!choice) return list;
  const out = [];
  for (let i = 0; i < list.length; i++) {
    const word = list[i];
    if (word === "--") { out.push(...list.slice(i)); break; }  // the agent's own arguments stay as they are
    if (["--model", "-mm", "--llm", "-lm"].includes(word)) { i++; continue; }
    if (word.startsWith("--model=") || word.startsWith("--llm=") || word === "--no-provider-defaults" || word === "-nd") continue;
    out.push(word);
  }
  const at = out.indexOf("--");
  const added = ["--llm", choice, ...(only ? ["--no-provider-defaults"] : [])];
  if (at < 0) out.push(...added); else out.splice(at, 0, ...added);
  return out;
}

// sandbox-ai gc on a host: first what it would remove (--dry-run), then that, asked first.
function gcCard(main, host) {
  const old = document.getElementById("gc-card");
  if (old) dropCard(old);
  const days = el("input", { id: "gc-days", type: "number", min: 0, max: 36500, size: 6, placeholder: t("keep_days there") });
  const result = el("div"), actions = el("div", { class: "row" });
  const older = () => (days.value.trim() === "" ? null : Number(days.value));
  const lines = (r) => {
    const items = [];
    for (const name of r.leftovers || []) items.push(t("the leftovers of {name} (ended; its HOME was not kept)", { name }));
    for (const e of r.removed || []) items.push(t("the kept start {name} with its HOME (last used {when})", { name: e.name, when: String(e.last_used || "?").replace("T", " ").replace("Z", "") }));
    if ((r.histories || []).length) items.push(t("the history of {names}", { names: r.histories.join(", ") }));
    for (const e of r.skipped || []) items.push(t("left alone: {name}: {why}", { name: e.name, why: e.reason }));
    for (const d of r.damaged || []) items.push(t("left alone: {path} is damaged (no readable session.json); remove it by hand", { path: d }));
    return items;
  };
  const preview = async (event) => busy(event.target, async () => {
    clear(result); clear(actions);
    // what the confirmation is about: the days of this preview and what it listed (not what the field says later)
    const asked = days.value.trim(), previewed = older();
    let r;
    try { r = previewed === null ? await api("GET", hostPath("/gc", host)) : await api("GET", hostPath(`/gc/${encodeURIComponent(asked)}`, host)); }
    catch (e) { result.append(errorNotice(e)); return; }
    r = r && typeof r === "object" ? r : {};
    const found = lines(r);
    const removing = (r.leftovers || []).length + (r.removed || []).length + (r.histories || []).length;
    result.append(found.length ? el("ul", {}, found.map(line => el("li", {}, line))) : el("p", { class: "hint" }, t("Nothing to remove.")));
    if (!removing) return;
    // the service runs a dry run first and removes nothing when it would now remove more than this preview showed
    const expected = { removed: (r.removed || []).map(e => String((e && (e.id || e.name)) || "")), leftovers: (r.leftovers || []).map(String), histories: (r.histories || []).map(String) };
    const go = el("button", { class: "danger", onclick: () => confirmIn(actions, t("Remove these from {host} for good? A kept start's HOME goes with it.", { host }), t("Yes, remove them"), async () => {
      if (days.value.trim() !== asked) {
        clear(result).append(notice(t("The days were changed after the preview: nothing was removed. Preview again."), "warn"));
        return;
      }
      try {
        const done = await api("POST", hostPath("/gc", host), { older_than: previewed, confirm: true, expected });
        clear(result).append(el("ul", {}, lines(done || {}).map(line => el("li", {}, line))));
        toast("success", t("Cleaned up {host}", { host }), "");
        if (state.page === "starts") refreshStarts(main, true);
      } catch (e) {
        if (e instanceof ApiError && e.status === 409) { clear(result).append(notice(e.message, "warn")); return; }
        failed(t("Could not clean up {host}", { host }))(e);
      }
    }, { trigger: go }) }, t("Remove these…"));
    actions.append(go);
  });
  const card = el("div", { class: "card", id: "gc-card" }, el("h2", {}, t("Clean up {host}", { host })),
    el("p", { class: "hint" }, t("Leftovers of ended starts, and kept starts unused for the given days with their HOME (empty: the host's keep_days). First a preview; nothing goes before you confirm.")),
    el("div", { class: "form" }, el("label", { for: "gc-days" }, t("Unused for days")), days,
      el("span"), el("div", { class: "row" }, el("button", { class: "primary", onclick: preview }, t("Preview")), el("button", { onclick: () => dropCard(card) }, t("Close")))),
    result, actions);
  openCard(main, card, null);
}

// A card about one running start (its network, its resources): switched off once the start stops or goes (a later
// listing says so), with a notice and its Close.
// REF: {host, id, name (a getter), rename(new)}: the card follows its start by host and id, so a rename (here or
// elsewhere) keeps it attached, under the new name.
function registerCard(card, notes, ref) {
  state.cards = state.cards.filter(entry => entry.card.isConnected && entry.card !== card);
  const entry = { card, ref, retired: false };
  entry.retire = (now) => {
    if (entry.retired) return;
    entry.retired = true;
    for (const control of controlsIn(card)) control.disabled = true;
    clear(notes).append(notice(now ? t("{name} on {host} is {state} now: nothing here changes it any more.", { name: ref.name, host: ref.host, state: stateText(now) })
      : t("{name} on {host} is gone: nothing here changes it any more.", { name: ref.name, host: ref.host }), "warn"),
      el("button", { onclick: () => dropCard(card) }, t("Close")));
  };
  state.cards.push(entry);
  return entry;
}

function retireCards(starts, stale) {
  state.cards = state.cards.filter(entry => entry.card.isConnected);
  for (const entry of state.cards) {
    if (stale(entry.ref.host)) continue;
    const s = starts.find(x => x.host === entry.ref.host && startId(x) === entry.ref.id);
    if (s && s.name !== entry.ref.name && !entry.retired) entry.ref.rename(s.name);
    if (!s || s.state !== "running") entry.retire(s ? s.state : null);
  }
}

// What a card keeps of its start: its host, its id, and its name, which a rename changes (NAME is read anew each
// time; HEADING(name) gives the card's title).
function cardRef(s, getName, setName, heading, title) {
  return { host: s.host || state.host, id: startId(s), get name() { return getName(); },
    rename: (fresh) => { setName(fresh); heading.textContent = title(); } };
}

const NET_PRESETS = ["pypi", "github", "gitlab", "kernel-org", "duckduckgo", "npm"];

// the network of a running start (sandbox-ai net allow|revoke|show|untaint on its host): what widens it is asked
// first (it applies at once: real DNS, ports on the host, wildcards, Git logins)
function networkCard(main, s) {
  const host = s.host || state.host;
  let name = s.name;  // a rename of the start changes it (cardRef): every request below names it as it is then
  const old = document.getElementById("network-card");
  if (old) dropCard(old);
  const field = (id, placeholder) => el("input", { id, placeholder, class: "mono" });
  const hosts = field("net-hosts", "pypi.org api.example.org"), tcp = field("net-tcp", "192.0.2.10:5432");
  const ports = field("net-ports", "3000 11434"), dns = el("input", { id: "net-dns", type: "checkbox" });
  const gitUrl = field("net-git-url", "https://git.example.org"), gitRef = field("net-git-ref", "GITEA_TOKEN");
  const gitKind = el("select", { id: "net-git-kind" }, el("option", { value: "env" }, t("variable")), el("option", { value: "secret" }, t("secret")), el("option", { value: "file" }, t("file (its path)")));
  const networks = field("net-allow", "192.0.2.0/24 2001:db8::/64"), nameservers = field("net-nameservers", "192.0.2.53");
  const presetBoxes = NET_PRESETS.map(p => el("input", { type: "checkbox", id: `net-preset-${p}`, value: p }));
  const keep = el("input", { id: "net-keep", type: "checkbox" });
  const shown = el("div", { class: "net-shown" });
  const notes = el("div");
  const words = (input) => String(input.value || "").split(/\s+/).filter(Boolean);
  const body = (op) => {
    const request = { op, allow_host: words(hosts), allow_tcp: words(tcp), host_ports: words(ports).map(Number), dns: dns.checked };
    // the further options only when given (the requests stay as small as what was asked)
    const more = { allow: words(networks), presets: presetBoxes.filter(b => b.checked).map(b => b.value), nameservers: words(nameservers) };
    for (const [key, value] of Object.entries(more)) if (value.length) request[key] = value;
    if (gitUrl.value.trim() && gitRef.value.trim()) request.git_login = { url: gitUrl.value.trim(), kind: gitKind.value, ref: gitRef.value.trim() };
    if (op === "allow" && keep.checked) request.keep = true;
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
    let taint = null;
    if (d.taint && typeof d.taint === "object" && d.taint.tainted) {
      const holder = el("span", { class: "inline-ask" });
      const untaint = el("button", { onclick: () => confirmIn(holder, t("Take back the taint of {name} on {host}? The rules for tainted runs no longer apply to it: only when what entered it is trusted.", { name, host }), t("Yes, untaint it"), async () => {
        try {
          const r = await api("POST", hostPath(`/net/${encodeURIComponent(name)}/untaint`, host), { confirm: true });
          toast("success", t("{name} on {host} is no longer tainted", { name, host }), (r && r.output) || "");
          await refresh();
        } catch (e) { failed(t("Could not untaint {name} on {host}", { name, host }))(e); }
      }, { trigger: untaint }) }, t("Untaint…"));
      holder.append(untaint);
      taint = el("div", { class: "notice warn" }, t("Untrusted content: tainted since {since}", { since: d.taint.since || "?" }), " ", holder);
    }
    clear(shown).append(taint || "", table([t("Kind"), t("What"), "", ""], rows, t("Nothing was allowed during this run."), t("Allowed during this run")));
  };
  const refresh = async () => {
    try { draw(await api("GET", hostPath(`/net/${encodeURIComponent(name)}`, host))); }
    catch (e) { clear(shown).append(errorNotice(e)); }
  };
  const send = async (op, request, confirm) => {
    try {
      const r = await api("POST", hostPath(`/net/${encodeURIComponent(name)}`, host), Object.assign({}, request, confirm ? { confirm: true } : {}));
      toast("success", op === "allow" ? t("Allowed for {name} on {host}", { name, host }) : t("Revoked for {name} on {host}", { name, host }), (r && r.output) || "");
      await refresh();
    } catch (e) {
      if (!confirm && confirmable(e)) { askRefused(actions, e, () => send(op, request, true)); return; }
      failed(t("Could not change the network of {name} on {host}", { name, host }))(e);
    }
  };
  const actions = el("div", { class: "row" });
  // What an allow widens, in words, for its confirmation.
  const widened = (r) => [r.allow_host.length ? t("the host names {names}", { names: r.allow_host.join(", ") }) : null,
    r.allow_tcp.length ? t("the addresses {addresses}", { addresses: r.allow_tcp.join(", ") }) : null,
    r.host_ports.length ? t("the ports {ports} on the host", { ports: r.host_ports.join(", ") }) : null,
    r.dns ? t("real DNS (every name it looks up)") : null,
    (r.allow || []).length ? t("every protocol and port of {networks}", { networks: r.allow.join(", ") }) : null,
    (r.presets || []).length ? t("the hosts of {presets}", { presets: r.presets.join(", ") }) : null,
    (r.nameservers || []).length ? t("the resolvers {servers} (real DNS)", { servers: r.nameservers.join(", ") }) : null,
    r.git_login ? t("a Git login for {url}", { url: r.git_login.url }) : null].filter(Boolean);
  const allow = el("button", { class: "primary", onclick: () => {
    const request = body("allow"), what = widened(request);
    if (!what.length) { toast("warning", t("Nothing to allow"), t("Name host names, addresses, ports, real DNS or a Git login first.")); return; }
    if (!needsConfirm("net-allow")) { send("allow", request, false); return; }
    confirmIn(actions, request.keep ? t("Allow {what} for {name} on {host} now, and keep it in its project's configuration for every later start?", { what: what.join(t(" and ")), name, host })
      : t("Allow {what} for {name} on {host} now? It applies at once, until the sandbox ends (or you revoke it).", { what: what.join(t(" and ")), name, host }),
      t("Yes, allow it"), () => send("allow", request, true), { kind: "primary", trigger: allow });
  } }, t("Allow…"));
  actions.append(allow,
    el("button", { onclick: (event) => busy(event.target, () => send("revoke", body("revoke"), false)) }, t("Revoke")),
    el("button", { onclick: refresh }, t("Refresh")));
  // net show --keep | --keep-all: what the run was given, into its project's .sandbox-ai.json (asked first)
  const keepRow = el("div", { class: "row" });
  const keepAsk = (all) => (event) => confirmIn(keepRow, all ? t("Keep everything {name} on {host} was given, also what approvals opened once or for the run, in its project's configuration? Every later start of that project gets it.", { name, host })
    : t("Keep what you gave {name} on {host} (allowed names and addresses, networks, DNS, ports, devices, workspaces) in its project's configuration? Every later start of that project gets it.", { name, host }), t("Yes, keep it"), async () => {
    try { const r = await api("POST", hostPath(`/net/${encodeURIComponent(name)}/keep`, host), { all, confirm: true }); toast("success", t("Kept for the later starts of {name}", { name }), (r && r.output) || ""); }
    catch (e) { failed(t("Could not keep it for {name} on {host}", { name, host }))(e); }
  }, { kind: "primary", trigger: event.target });
  keepRow.append(el("button", { onclick: keepAsk(false) }, t("Keep for later starts…")), el("button", { onclick: keepAsk(true) }, t("Keep all for later starts…")));
  const title = () => t("Network of {name} on {host}", { name, host });
  const heading = el("h2", {}, title());
  const card = el("div", { class: "card", id: "network-card" }, heading, notes,
    el("p", { class: "hint" }, t("Until the sandbox ends. Ports on the host go through a relay only you start; a Git login names where its token is, never the token.")),
    el("h3", {}, t("Allowed during this run")), shown,
    el("div", { class: "form" },
      el("label", { for: "net-hosts" }, t("Host names (TCP/443)")), hosts,
      el("label", { for: "net-tcp" }, "IP:PORT (TCP)"), tcp,
      el("label", { for: "net-ports" }, t("Ports on the host")), ports,
      el("label", { for: "net-dns" }, t("Real DNS")), dns,
      el("label", { for: "net-git-url" }, t("Git login: server")), gitUrl,
      el("label", { for: "net-git-kind" }, t("Git login: token in")), el("div", {}, gitKind, " ", gitRef),
      el("label", { for: "net-allow" }, t("Networks (every port)")), networks,
      el("span", {}, t("Known hosts")), el("div", { class: "row" }, presetBoxes.map((b, i) => el("label", {}, b, " ", NET_PRESETS[i]))),
      el("label", { for: "net-nameservers" }, t("Resolvers (real DNS)")), nameservers,
      el("label", { for: "net-keep" }, t("Also for later starts")), keep,
      el("span"), actions),
    el("h3", {}, t("Keep for the project")), keepRow, el("div", { class: "row" }, el("button", { onclick: () => dropCard(card) }, t("Close"))));
  openCard(main, card, s);
  registerCard(card, notes, cardRef(s, () => name, (fresh) => { name = fresh; }, heading, title));
  refresh();
}

// Why `workspace remove` refused (its phrases, workspaces.py): a copy the agent took (--force only takes it out of
// the sandbox's own view), or something open there (--force detaches it: what is open stays usable until closed).
function workspaceRefusal(message) {
  const text = String(message || "");
  if (/the agent took a copy into a namespace of its own|copies cannot be looked for/.test(text)) return "copy";
  if (/a process holds it|the kernel still holds it/.test(text)) return "open";
  return null;
}

// A size, CPUs or processes as limits takes them, as a number (none: infinite), to compare with limits --show.
const LIMIT_UNITS = { k: 2 ** 10, m: 2 ** 20, g: 2 ** 30, t: 2 ** 40 };
function limitNumber(key, value) {
  if (value === "none") return Infinity;
  const unit = String(value).slice(-1).toLowerCase();
  return key === "memory" && LIMIT_UNITS[unit] ? parseFloat(value) * LIMIT_UNITS[unit] : parseFloat(value);
}

// What a running start is given while it runs (sandbox-ai workspace, devices and limits on its host): host
// directories at /workspaces/NAME, devices, its resource limits. Each change runs that command there, with its
// checks; what widens the sandbox (a writable directory, a device, a removal by force, a limit raised or taken away)
// asks first, in place, naming the host. Failures are error toasts with the command's own message.
function resourcesCard(main, s) {
  const host = s.host || state.host;
  let name = s.name;  // a rename of the start changes it, and WHERE with it (cardRef)
  const old = document.getElementById("resources-card");
  if (old) dropCard(old);
  let where = t("{name} on {host}", { name, host });
  const head = (columns) => el("thead", {}, el("tr", {}, columns.map(c => el("th", {}, c))));
  const notes = el("div");

  // workspaces: the start's (they stay) and the added ones (Remove; when it is refused, Force… with the refusal and
  // what forcing it leaves)
  const wsBody = el("tbody"), wsStatus = el("div");
  const wsTable = el("table", {}, el("caption", { class: "sr-only" }, t("Workspaces of {where}", { where })), head([t("In the sandbox"), t("Kind"), t("Name"), t("Mode"), t("Source"), t("On the host"), ""]), wsBody);
  const workspaceRow = (w) => {
    const target = String(w.sandbox || w.host || ""), actions = el("td", { class: "actions" });
    const remove = (force) => async (event) => busy(event.target, async () => {
      try {
        const r = await api("POST", hostPath(`/workspaces/${encodeURIComponent(name)}/remove`, host), force ? { workspace: target, force: true, confirm: true } : { workspace: target });
        toast(force ? "warning" : "success", t("Removed {what} from {where}", { what: target, where }), (r && r.output) || "");
        loadWorkspaces();
      } catch (e) {
        const kind = force ? null : workspaceRefusal(e.message);
        if (kind) { askForce(kind, e.message); return; }
        failed(t("Could not remove {what} from {where}", { what: target, where }))(e); buttons();
      }
    });
    const buttons = () => clear(actions).append(...(w.source === "added"
      ? [el("button", { onclick: remove(false) }, t("Remove")), el("button", { class: "danger", onclick: () => askForce(null, "") }, t("Force…"))]
      : [el("span", { class: "hint" }, t("until the run ends"))]));
    const askForce = (kind, refusal) => {
      const consequence = kind === "copy" ? t("Take {what} out of the sandbox's own view by force? The agent's copy stays usable, also for writing, until the sandbox restarts: only a restart takes it away.", { what: target })
        : kind === "open" ? t("Detach {what} from {where} by force? What a process there still holds stays usable, also for writing, until it is closed; only a restart takes it away for sure.", { what: target, where })
          : t("Remove {what} from {where} by force? What a process there still holds stays usable until it is closed, and a copy the agent took stays usable until the sandbox restarts: only a restart takes it away for sure.", { what: target, where });
      clear(actions).append(...ask(el("span", {}, refusal ? el("div", { class: "mono" }, t("It was refused: {why}", { why: refusal })) : null, consequence),
        t("Yes, by force"), remove(true), () => { buttons(); focusFirst(actions); }));
      focusCancel(actions);
    };
    buttons();
    // an added one: a folder or a single file at /workspaces/NAME, NAME chosen (--name) or its own
    const added = w.source === "added", at = String(w.sandbox || "").split("/").filter(Boolean).pop() || "-";
    return el("tr", {}, el("td", { class: "mono" }, target, w.note ? el("div", { class: "hint" }, String(w.note)) : null),
      el("td", {}, w.kind === "file" ? t("file") : t("folder")),
      el("td", { class: "mono" }, added ? at : "-", added && w.named ? el("span", { class: "hint" }, " ", t("(chosen)")) : null),
      el("td", {}, w.mode || "-"),
      el("td", {}, `${w.source || ""}${w.kept ? t(", kept for the session") : ""}`), el("td", { class: "mono" }, w.host || ""), actions);
  };
  const loadWorkspaces = async () => {
    let list;
    try { list = await api("GET", hostPath(`/workspaces/${encodeURIComponent(name)}`, host)); }
    catch (e) { clear(wsStatus).append(errorNotice(e)); wsTable.hidden = true; return; }
    list = (Array.isArray(list) ? list : []).filter(w => w && typeof w === "object");
    clear(wsStatus).append(list.length ? "" : el("p", { class: "hint" }, t("No workspaces listed.")));
    clear(wsBody).append(...list.map(workspaceRow));
    wsTable.hidden = !list.length;
  };
  // a folder or a single regular file of that host (workspace add PATH [--name NAME] [--ro]); a writable one asked
  // first. What Yes sends is what the question named.
  const directory = el("input", { id: "ws-dir", class: "mono", placeholder: t("{path} (on that host)", { path: "/home/you/src/other" }) });
  const wsName = el("input", { id: "ws-name", class: "mono", size: 16, placeholder: t("its own name"), pattern: WORKSPACE_AS.source.slice(1, -1) });
  const readOnly = el("input", { id: "ws-ro", type: "checkbox", checked: true });
  const wsActions = el("div", { class: "row" });
  const addWorkspace = (path, as, ro) => async (event) => busy(event.target, async () => {
    const sent = Object.assign({ path, read_only: ro }, as ? { name: as } : {}, ro ? {} : { confirm: true });
    try {
      const r = await api("POST", hostPath(`/workspaces/${encodeURIComponent(name)}`, host), sent);
      toast("success", t("Added {what} to {where}", { what: path, where }), (r && r.output) || "");
      directory.value = ""; wsName.value = ""; loadWorkspaces();
    } catch (e) { failed(t("Could not add {what} to {where}", { what: path, where }))(e); }
    wsButtons();
  });
  const wsButtons = () => clear(wsActions).append(el("button", { class: "primary", onclick: (event) => {
    const path = directory.value.trim(), as = wsName.value.trim();
    if (!path.startsWith("/")) { toast("warning", t("Which file or folder?"), t("An absolute path on {host}.", { host })); return; }
    if (as && !WORKSPACE_AS.test(as)) { toast("warning", t("Which name?"), t("1-64 letters, digits, '_', '.' or '-', starting with a letter or digit.")); return; }
    if (readOnly.checked) { addWorkspace(path, as, true)(event); return; }
    clear(wsActions).append(...ask(t("Give {name} write access to {path} on {host}, at {at}? The agent can change and delete what is in it, until you remove it (a folder is kept for the session's next start).", { name, path, host, at: `/workspaces/${as || path.split("/").filter(Boolean).pop() || ""}` }),
      t("Yes, writable"), addWorkspace(path, as, false), () => { wsButtons(); focusFirst(wsActions); }));
    focusCancel(wsActions);
  } }, t("Add workspace")), el("span", { class: "hint" }, t("at /workspaces/NAME (its own name unless one is given)")));
  wsButtons();

  // MCP servers while it runs (sandbox-ai mcp attach|detach ... --live): a server of that host (mcp list) or a helper
  // agent added now, through a relay of its own; each asked first, naming the host and the start. Whether the agent
  // sees one at once is its own matter (agent_restart): then a restart of the agent is offered (resume --restart,
  // asked first too; the conversation goes on).
  const mcpBody = el("tbody"), mcpStatus = el("div"), mcpResult = el("div");
  const mcpTable = el("table", {}, el("caption", { class: "sr-only" }, t("MCP servers of {where}", { where })), head([t("Server"), ""]), mcpBody);
  const mcpChoice = el("select", { id: "mcp-live-server" });
  const mcpActions = el("div", { class: "row" });
  const helperOf = (server) => { const m = /^helper-(claude|codex|opencode)-[0-9a-f]{8}$/.exec(server); return m ? m[1] : null; };
  const which = (value) => (value.startsWith("helper:") ? { helper: value.slice("helper:".length) } : { server: value });
  const shownServer = (body) => (body.helper ? t("{agent} as a helper", { agent: body.helper }) : body.server);
  const restartAgent = () => {
    const holder = el("div", { class: "row" });
    const again = el("button", { class: "primary", onclick: () => confirmIn(holder, t("Restart the agent of {name} on {host} now? What runs in it ends; the agent continues its conversation.", { name, host }), t("Yes, restart it"), async () => {
      try {
        const r = await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/resume`, host), { restart: true, confirm: true });
        toast("success", t("{name} on {host} restarted", { name, host }), (r && r.output) || "");
        holder.hidden = true;  // done: what the question put back stays out of sight
        if (state.page === "starts") refreshStarts(main, true);
      } catch (e) { failed(t("Could not restart {name} on {host}", { name, host }))(e); }
    }, { kind: "primary", trigger: again }) }, t("Restart the agent now (the conversation goes on)"));
    holder.append(again);
    return holder;
  };
  const mcpRow = (server) => {
    const actions = el("td", { class: "actions" }), helper = helperOf(server);
    const body = helper ? { helper } : { server };
    const remove = el("button", { class: "danger", onclick: () => confirmIn(actions, t("Detach {server} from {name} on {host} now? Its relay there closes: the agent's connections to it end and its calls fail, and its later starts go without it. The server's own sandbox keeps running.", { server: shownServer(body), name, host }), t("Yes, detach it"), async () => {
      try {
        const r = await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/mcp-live/remove`, host), Object.assign({}, body, { confirm: true }));
        toast("success", t("Detached {server} from {where}", { server: shownServer(body), where }), (r && r.message) || "");
        loadMcp();
      } catch (e) { failed(t("Could not detach {server} from {where}", { server: shownServer(body), where }))(e); }
    }, { trigger: remove }) }, t("Remove"));
    actions.append(remove);
    return el("tr", {}, el("td", { class: "mono" }, server, helper ? el("span", { class: "hint" }, " ", t("({agent} as a helper)", { agent: helper })) : null), actions);
  };
  const loadMcp = async () => {
    // what it reaches now: list --json's mcp_running (a session), mcp (a run without a kept start)
    let live = [], defined = [];
    try {
      const listed = await api("GET", hostPath("/starts", host));
      const me = (Array.isArray(listed) ? listed : []).find(e => e && typeof e === "object" && (startId(e) === startId(s) || e.name === name));
      const names = me ? (Array.isArray(me.mcp_running) ? me.mcp_running : !me.directory && Array.isArray(me.mcp) ? me.mcp : []) : [];
      live = names.filter(n => typeof n === "string" && n);
      clear(mcpStatus).append(live.length ? "" : el("p", { class: "hint" }, t("No MCP server reaches it now.")));
    } catch (e) { clear(mcpStatus).append(errorNotice(e)); }
    try {
      const servers = await api("GET", hostPath("/mcp", host));
      defined = (Array.isArray(servers) ? servers : []).map(d => d && typeof d === "object" ? d.name : null).filter(n => typeof n === "string" && MCP_SERVER_NAME.test(n));
    } catch (e) { mcpStatus.append(errorNotice(e)); }
    clear(mcpBody).append(...live.map(mcpRow));
    mcpTable.hidden = !live.length;
    const chosen = mcpChoice.value;
    clear(mcpChoice).append(...defined.filter(n => !live.includes(n)).map(n => el("option", { value: n }, n)),
      ...["claude", "codex", "opencode"].map(agent => el("option", { value: `helper:${agent}` }, t("{agent} as a helper", { agent }))));
    if (chosen && [...mcpChoice.children].some(o => o.value === chosen)) mcpChoice.value = chosen;
  };
  const addMcp = el("button", { class: "primary", onclick: () => {
    const value = mcpChoice.value || (mcpChoice.children[0] && mcpChoice.children[0].value);
    if (!value) { toast("warning", t("Which MCP server?"), t("Choose one of {host}, or a helper agent.", { host })); return; }
    const body = which(value);
    const text = body.helper ? t("Add {agent} as a helper to {name} on {host} now, while it runs? It works in a sandbox of its own on the start's main workspace; the agent reaches it through a relay of its own (kept for its later starts).", { agent: body.helper, name, host })
      : t("Add the MCP server {server} to {name} on {host} now, while it runs? The agent reaches it through a relay of its own (kept for its later starts).", { server: body.server, name, host });
    confirmIn(mcpActions, text, t("Yes, add it now"), async () => {
      clear(mcpResult);
      try {
        const r = await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/mcp-live`, host), Object.assign({}, body, { confirm: true }));
        toast(r && r.attached === "already" ? "warning" : "success", t("{server} → {where}", { server: shownServer(body), where }), (r && r.message) || "");
        mcpResult.append(el("p", { class: "hint" }, String((r && r.message) || "")));
        if (r && (r.agent_restart === "required" || r.agent_restart === "maybe")) mcpResult.append(restartAgent());
        loadMcp();
      } catch (e) { failed(t("Could not add {server} to {where}", { server: shownServer(body), where }))(e); }
    }, { kind: "primary", trigger: addMcp });
  } }, t("Add now…"));
  mcpActions.append(addMcp);

  // devices: given at the start (they stay) or added (Remove); adding asks first
  const devBody = el("tbody"), devStatus = el("div");
  const devTable = el("table", {}, el("caption", { class: "sr-only" }, t("Devices of {where}", { where })), head(["ID", t("State"), t("Nodes"), t("Device"), t("Rule"), t("Given"), ""]), devBody);
  const deviceRow = (d) => {
    const id = String(d.id ?? ""), info = d.device && typeof d.device === "object" ? d.device : {};
    const what = [info.usb, info.product && `"${info.product}"`, info.serial && t("serial {serial}", { serial: info.serial }), info.port && t("port {port}", { port: info.port })].filter(Boolean).join(" ") || "-";
    const removable = d.source === "added" && ["attached", "held", "gone"].includes(d.state);
    const remove = el("button", { onclick: (event) => busy(event.target, async () => {
      try {
        const r = await api("POST", hostPath(`/devices/${encodeURIComponent(name)}/remove`, host), { device: id });
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
    try { list = await api("GET", hostPath(`/devices/${encodeURIComponent(name)}`, host)); }
    catch (e) { clear(devStatus).append(errorNotice(e)); devTable.hidden = true; return; }
    list = (Array.isArray(list) ? list : []).filter(d => d && typeof d === "object");
    clear(devStatus).append(list.length ? "" : el("p", { class: "hint" }, t("No devices given to it.")));
    clear(devBody).append(...list.map(deviceRow));
    devTable.hidden = !list.length;
  };
  const device = el("input", { id: "dev-value", class: "mono", placeholder: t("{path} or {usb}", { path: "/dev/ttyUSB0", usb: "usb:2e8a:000a" }) });
  const devActions = el("div", { class: "row" });
  const addDevice = (value) => async (event) => busy(event.target, async () => {
    try {
      const r = await api("POST", hostPath(`/devices/${encodeURIComponent(name)}`, host), { device: value, confirm: true });
      toast("success", t("Gave {what} to {where}", { what: value, where }), (r && r.output) || "");
      device.value = ""; loadDevices();
    } catch (e) { failed(t("Could not give {what} to {where}", { what: value, where }))(e); }
    devButtons();
  });
  const devButtons = () => clear(devActions).append(el("button", { class: "primary", onclick: () => {
    const value = device.value.trim();
    if (!value.startsWith("/dev/") && !value.startsWith("usb:")) { toast("warning", t("Which device?"), t("A path under /dev or usb:VENDOR:PRODUCT[:SERIAL] of {host}.", { host })); return; }
    clear(devActions).append(...ask(t("Hand {device} of {host} to {name}? The agent can use that hardware until the sandbox ends or you remove it. A device rule may ask first (Approvals, within a minute).", { device: value, host, name }),
      t("Yes, hand it over"), addDevice(value), () => { devButtons(); focusFirst(devActions); }));
    focusCancel(devActions);
  } }, t("Add device")));
  devButtons();

  // limits: filled in with the current ones (limits --show); only a field that was changed is sent; one raised above
  // the current limit, or taken away (none), is asked first: a huge value is no limit either
  const limit = (id, placeholder) => el("input", { id, placeholder, class: "mono", size: 10 });
  const memory = limit("lim-memory", t("{examples} or none", { examples: "8g, 512m" })), cpus = limit("lim-cpus", t("{examples} or none", { examples: "2, 1.5" })), pids = limit("lim-pids", t("{examples} or none", { examples: "4096" }));
  const fields = [["memory", memory], ["cpus", cpus], ["pids", pids]];
  const limitsNow = el("p", { class: "hint", id: "limits-now" }, t("Reading the current limits…"));
  const limActions = el("div", { class: "row" });
  let loaded = null, current = null;
  const loadLimits = async () => {
    let found;
    try { found = await api("GET", hostPath(`/limits/${encodeURIComponent(name)}`, host)); }
    catch (e) { loaded = null; current = null; limitsNow.textContent = t("The current limits could not be read ({error}): a start has 8g, half the CPUs and 4096 processes unless changed.", { error: e.message }); return; }
    const f = found && typeof found === "object" ? found : {};
    const number = (v) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);
    const mem = number(f.memory), high = number(f.memory_high), cpu = number(f.cpus), pid = number(f.pids);
    current = { memory: mem, cpus: cpu, pids: pid };
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
    } catch (e) {
      // the limits changed since they were read here: the service asks, and so does the page
      if (!body.confirm && confirmable(e)) { askRefused(limActions, e, () => sendLimits(Object.assign({}, body, { confirm: true })), { trigger: apply }); return; }
      failed(t("Could not change the limits of {where}", { where }))(e);
    }
    await loadLimits();
  };
  const LIMIT_NAMES = { memory: N_("memory"), cpus: N_("CPU"), pids: N_("process") };
  const apply = el("button", { class: "primary", onclick: (event) => {
    const body = {};
    for (const [key, input] of fields) {
      const value = input.value.trim();
      if (value && (!loaded || value !== loaded[key])) body[key] = value;
    }
    if (!Object.keys(body).length) { toast("warning", t("Nothing to change"), t("Change a memory size, CPUs or processes (none: no limit).")); return; }
    const removed = Object.keys(body).filter(key => body[key] === "none");
    // above the current limit (none now: any number is lower); the current ones unknown: asked all the same
    const raised = Object.keys(body).filter(key => body[key] !== "none" && (!current || (current[key] !== null && limitNumber(key, body[key]) > current[key])));
    const names = (keys) => keys.map(k => t(LIMIT_NAMES[k])).join(t(" and "));
    if (!removed.length && !raised.length) { busy(event.target, () => sendLimits(body)); return; }
    const text = removed.length ? t("Take away the {limits} limit of {where}? The agent can then use as much of that host as it gets (a strict security mode keeps its cap).", { limits: names(removed), where })
      : t("Raise the {limits} limit of {where}? The agent can then use more of that host (a strict security mode keeps its cap).", { limits: names(raised), where });
    confirmIn(limActions, text, removed.length ? t("Yes, no limit") : t("Yes, raise it"), () => sendLimits(Object.assign(body, { confirm: true })), { trigger: apply });
  } }, t("Apply limits"));
  limActions.append(apply, el("button", { onclick: () => { loadWorkspaces(); loadMcp(); loadDevices(); loadLimits(); } }, t("Refresh")));

  const title = () => t("Workspaces, devices and limits of {where}", { where });
  const heading = el("h2", {}, title());
  const card = el("div", { class: "card", id: "resources-card" }, heading, notes,
    el("p", { class: "hint" }, t("While it runs, without a restart; each change runs sandbox-ai workspace, mcp, devices or limits on that host, with its checks.")),
    el("h3", {}, t("Workspaces")), wsStatus, wsTable,
    el("div", { class: "form" }, el("label", { for: "ws-dir" }, t("File or folder")), directory,
      el("label", { for: "ws-name" }, t("Name (optional)")), wsName,
      el("label", { for: "ws-ro" }, t("Read-only")), readOnly, el("span"), wsActions),
    el("h3", {}, t("MCP servers")), mcpStatus, mcpTable,
    el("div", { class: "form" }, el("label", { for: "mcp-live-server" }, t("Add an MCP server")), mcpChoice, el("span"), mcpActions),
    mcpResult,
    el("h3", {}, t("Devices")), devStatus, devTable,
    el("div", { class: "form" }, el("label", { for: "dev-value" }, t("Device")), device, el("span"), devActions),
    el("h3", {}, t("Resource limits")), limitsNow,
    el("p", { class: "hint" }, t("Only a changed field is sent; a limit raised or taken away (none) is asked first. A strict security mode keeps its caps.")),
    el("div", { class: "form" },
      el("label", { for: "lim-memory" }, t("Memory")), memory,
      el("label", { for: "lim-cpus" }, "CPUs"), cpus,
      el("label", { for: "lim-pids" }, t("Processes")), pids,
      el("span"), limActions),
    el("div", { class: "row" }, el("button", { onclick: () => dropCard(card) }, t("Close"))));
  openCard(main, card, s);
  registerCard(card, notes, cardRef(s, () => name, (fresh) => { name = fresh; where = t("{name} on {host}", { name, host }); }, heading, title));
  loadWorkspaces();
  loadMcp();
  loadDevices();
  loadLimits();
}

// The open questions of every host. The table is kept between the polls: its rows are keyed by the host and the
// question's id, a new question adds a row, an answered or expired one takes its row away, and every other row stays
// as it is (the scope chosen in it, an Allow waiting for its confirmation, the pointer over it). Allow asks first,
// in the row, naming the host, the sandbox, what it asks for and the scope; the answer goes to the question's own
// host, its output (sandbox-ai approvals) is a toast. Each row counts down the time the question has left (from
// when its host's list was read: the service says how old that is), and says it at 20 and 10 seconds; a row has the
// focus with Tab, and a (allow) or d (deny) answer it there. A browser question is opened here: "Open here and
// allow" opens its link in this browser, then allows it there without opening a browser on that host.
const SCOPES = { once: N_("this once"), run: N_("for the rest of this run"), always: N_("always (remembered for the project)") };
const SCOPE_ORDER = ["once", "run", "always"];
const approvalsView = { main: null, body: null, rows: new Map() };

// A link only for http(s) without a user part (https://bank.example@evil.example/ names another host than it
// seems): what a sandbox names is never a javascript: or data: URL.
function safeUrl(url) {
  const text = String(url || "");
  if (!/^https?:\/\/[^\s\u0000-\u001f\u007f]+$/i.test(text)) return null;
  const authority = /^https?:\/\/([^/?#]*)/i.exec(text)[1];
  return authority && !authority.includes("@") ? text : null;
}

function webLink(url) {
  const text = safeUrl(url);
  return text ? el("a", { href: text, target: "_blank", rel: "noopener noreferrer", class: "mono" }, text) : null;
}

function countdownText(left) {
  if (left === null) return [t("no time limit"), ""];
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
  // max_scope null: this question cannot be allowed here (allow_refused says why); missing (an older sandbox-ai): any
  const allowable = q.max_scope !== null;
  const max = SCOPE_ORDER.includes(q.max_scope) ? q.max_scope : "always";
  const widest = allowable ? SCOPE_ORDER.indexOf(max) : -1;
  const scope = el("select", { "aria-label": t("Scope") }, SCOPE_ORDER.map((s, i) => el("option", { value: s, disabled: i > widest, title: i > widest ? t("not for this question: at most {scope}", { scope: t(SCOPES[max]) }) : null }, t(SCOPES[s]))));
  const chosen = () => (SCOPE_ORDER.indexOf(scope.value) > widest || !scope.value ? "once" : scope.value);
  const actions = el("td", { class: "actions" });
  const left = el("td", { class: "countdown" });
  const browser = q.kind === "browser";
  const url = browser ? safeUrl(q.url) : null;
  const link = url ? webLink(url) : null;
  const destination = browser && q.url_host ? el("div", { class: "hint" }, t("opens {host}", { host: String(q.url_host) })) : null;
  const refused = browser && !url ? el("div", { class: "hint status-bad" }, t("not opened anywhere: {why}", { why: String(q.url_refused || t("not an http(s) link without a user part")) })) : null;
  const question = el("td", {}, q.question, link ? el("div", {}, link) : null, destination, refused,
    q.url_truncated ? el("div", { class: "hint" }, t("the URL was cut")) : null,
    q.always ? el("div", { class: "hint" }, q.always) : null,
    !allowable && q.allow_refused ? el("div", { class: "hint" }, t("cannot be allowed: {why}", { why: String(q.allow_refused) })) : null);
  const row = el("tr", { "data-id": id, "data-host": host, tabindex: 0, "aria-keyshortcuts": "a d", title: t("a: allow, d: deny") },
    el("td", { class: "mono" }, host), question, el("td", { class: "mono" }, where), left, actions);
  const entry = { row, deadline: null, said: null };
  // AGE: seconds since the host's list was read (its seconds_left were counted then)
  entry.update = (fresh, age) => {
    const seconds = fresh.seconds_left;
    entry.deadline = typeof seconds === "number" && Number.isFinite(seconds) ? Date.now() + (seconds - (Number(age) || 0)) * 1000 : null;
    if (entry.expired && (entry.deadline === null || entry.deadline > Date.now())) { entry.expired = false; buttons(); }  // the host says it is still open
    entry.tick();
  };
  entry.tick = () => {
    const seconds = entry.deadline === null ? null : Math.max(0, Math.ceil((entry.deadline - Date.now()) / 1000));
    const [text, level] = countdownText(seconds);
    left.textContent = text;
    left.className = `countdown${level ? ` ${level}` : ""}`;
    for (const mark of [20, 10]) {
      if (seconds !== null && seconds > 0 && seconds <= mark && (entry.said === null || entry.said > mark)) {
        entry.said = mark;
        say(t("{seconds} seconds left: {question} ({sandbox} on {host})", { seconds: mark, question: q.question || q.kind || "", sandbox: where, host }));
      }
    }
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
      answered(q);
      poll();
    } catch (e) { failed(decision === "allow" ? t("Could not allow the question of {sandbox} on {host}", { sandbox: where, host }) : t("Could not deny the question of {sandbox} on {host}", { sandbox: where, host }))(e); buttons(); }
  });
  // Deny: for the rest of the run (once when the user chose that, or all the question allows); always only as its own
  // "Block always…", asked first. The button says which it is.
  let picked = false;  // a scope chosen by the user (the first, "this once", is only the allow's default)
  const denyScope = () => ((picked && chosen() === "once") || (allowable && widest < 1) ? "once" : "run");
  const denyLabel = () => (denyScope() === "once" ? t("Deny once") : t("Deny for this run"));
  let deny = null;
  scope.addEventListener("change", () => { picked = true; if (deny) deny.textContent = denyLabel(); });
  const denyNow = (event) => answer("deny", { scope: denyScope() }, t("Denied {scope}", { scope: t(SCOPES[denyScope()]) }))(event);
  // Allow: a browser question is opened here (never on that host); one that cannot be allowed has no Allow.
  const allowButton = !allowable || (browser && !url) ? null : browser
    ? el("button", { class: "primary", title: t("opens the link in this browser, then allows it on {host} without opening a browser there", { host }), onclick: () => askAllow() }, t("Open here and allow"))
    : el("button", { class: "primary", onclick: () => askAllow() }, t("Allow"));
  const buttons = () => {
    if (entry.expired) return;
    deny = el("button", { class: "danger", onclick: denyNow }, denyLabel());
    clear(actions).append(...[allowable && !(browser && !url) ? scope : null, allowButton, deny,
      widest >= 2 ? el("button", { class: "danger", onclick: () => askBlock() }, t("Block always…")) : null].filter(Boolean));
  };
  const cancel = () => { buttons(); focusFirst(actions); };
  const askAllow = () => {
    const always = chosen() === "always";
    const text = browser ? t("Open {url} here and allow {sandbox} on {host}, {scope}? It opens in this browser; nothing opens on {host}.", { url: q.url_host || url, sandbox: where, host, scope: t(SCOPES[chosen()]) })
      : t("Allow {sandbox} on {host}: {question}, {scope}{always}?", { sandbox: where, host, question: q.question || q.kind || t("this"), scope: t(SCOPES[chosen()]), always: always && q.always ? ` (${q.always})` : "" });
    const body = Object.assign({ scope: chosen() }, browser ? { no_open: true } : {}, always ? { confirm: true } : {});
    const yes = answer("allow", body, t("Allowed {scope}", { scope: t(SCOPES[chosen()]) }));
    clear(actions).append(...ask(text, browser ? t("Yes, open and allow") : t("Yes, allow"), (event) => {
      if (browser && typeof window.open === "function") window.open(url, "_blank", "noopener,noreferrer");  // in the click: no pop-up blocker
      return yes(event);
    }, cancel, "primary"));
    focusCancel(actions);
  };
  const askBlock = () => {
    clear(actions).append(...ask(
      t("Block {sandbox} on {host} always: {question}? It is remembered for the project: every later question like it is refused without asking.", { sandbox: where, host, question: q.question || q.kind || t("this") }),
      t("Yes, block always"), answer("deny", { scope: "always", confirm: true }, t("Blocked always")), cancel));
    focusCancel(actions);
  };
  row.addEventListener("keydown", (event) => {
    if (event.target !== row || entry.expired || event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "a" && allowButton) { event.preventDefault(); askAllow(); }
    else if (event.key === "d") { event.preventDefault(); denyNow({ target: deny }); }
  });
  buttons();
  entry.update(q, 0);
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

// What a host remembers of its answers (approvals history, remembered, forget there): shown below the questions.
function approvalsRecords() {
  const hostChoice = el("select", { id: "records-host" }, state.hosts.map(h => el("option", { value: h.name, selected: h.name === state.host }, h.name)));
  const output = el("div");
  const showHistory = (event) => busy(event.target, async () => {
    const host = hostChoice.value;
    clear(output);
    try {
      const r = await api("GET", hostPath("/approvals-history", host));
      output.append(el("h3", {}, t("Answered questions on {host}", { host })), el("pre", {}, (r && r.output) || ""));
    } catch (e) { output.append(errorNotice(e)); }
  });
  const showRemembered = (event) => busy(event.target, async () => {
    const host = hostChoice.value;
    clear(output);
    let found;
    try { found = await api("GET", hostPath("/remembered", host)); } catch (e) { output.append(errorNotice(e)); return; }
    const rows = [];
    for (const [project, entry] of Object.entries(found && typeof found === "object" ? found : {})) {
      if (!entry || typeof entry !== "object") continue;
      for (const [field, verb] of [["hosts", N_("allowed")], ["tcp", N_("allowed")], ["blocked_hosts", N_("blocked")], ["blocked_tcp", N_("blocked")]]) {
        for (const value of Array.isArray(entry[field]) ? entry[field] : []) {
          const cell = el("td", { class: "actions" });
          const forget = el("button", { class: "danger", onclick: () => confirmIn(cell, verb === "blocked"
            ? t("Forget the block of {entry} for {project} on {host}? Its later starts ask again (or allow what the options allow).", { entry: value, project, host })
            : t("Forget that {entry} is allowed always for {project} on {host}? Its later starts ask again; a running one keeps it until it ends.", { entry: value, project, host }),
          t("Yes, forget it"), async () => {
            try {
              const r = await api("POST", hostPath("/remembered/forget", host), { project, entry: String(value), confirm: true });
              toast("success", t("Forgot {entry} for {project}", { entry: value, project }), (r && r.output) || "");
              showRemembered({ target: forget });
            } catch (e) { failed(t("Could not forget {entry}", { entry: value }))(e); }
          }, { trigger: forget }) }, t("Forget…"));
          cell.append(forget);
          rows.push(el("tr", {}, el("td", { class: "mono" }, project), el("td", {}, t(verb)), el("td", { class: "mono" }, String(value)), cell));
        }
      }
    }
    output.append(el("h3", {}, t("Remembered answers on {host}", { host })),
      table([t("Project"), t("Answer"), t("What"), ""], rows, t("Nothing is allowed or blocked always on this host."), t("Remembered answers on {host}", { host })));
  });
  return el("div", { class: "card" }, el("h2", {}, t("Answers given before")),
    el("div", { class: "row" }, el("label", { for: "records-host" }, t("Host")), hostChoice,
      el("button", { onclick: showHistory }, t("History")), el("button", { onclick: showRemembered }, t("Remembered answers"))), output);
}

// KNOWN: what poll() fetched already ({questions, errors, age}, or the Error of a failed check); else fetched here.
pages.approvals = async (main, known) => {
  const view = approvalsView;
  if (known === undefined || view.main !== main || !view.body || !view.body.isConnected) {
    view.main = main; view.rows = new Map();
    view.status = el("div"); view.body = el("tbody");
    view.table = el("table", {}, el("caption", { class: "sr-only" }, t("Open questions of every host")),
      el("thead", {}, el("tr", {}, [t("Host"), t("Question"), t("Sandbox"), t("Time left"), ""].map(c => el("th", {}, c)))), view.body);
    view.empty = el("p", { class: "hint" }, t("No open questions."));
    clear(main).append(el("h1", {}, t("Approvals")),
      el("p", { class: "hint" }, t("What the sandboxes of every host ask. A question without an answer in its time is refused. In a row: a allows, d denies.")),
      el("div", { class: "row" }, notificationsButton()),
      view.status, view.table, view.empty, approvalsRecords());
    view.table.hidden = true; view.empty.hidden = true;
  }
  let found = known;
  if (found === undefined && state.polling) {
    // the page's poll is asking right now: its answer is drawn here when it comes (poll()), not asked twice
    if (!view.rows.size) clear(view.status).append(el("p", { class: "hint" }, t("Loading…")));
    return;
  }
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
  for (const [host, why] of Object.entries(errors)) view.status.append(notice(host === "hosts.json" ? t("The list of hosts could not be read: {error}", { error: why }) : t("Could not check {host}: {error}", { host, error: why }), "error"));
  const ages = found.age && typeof found.age === "object" ? found.age : {};

  const questions = openQuestions(found);
  const keys = new Set(questions.map(questionKey));
  for (const [key, entry] of view.rows) {
    // a host that could not be asked keeps its rows: no answer from it is no answer to them
    if (!keys.has(key) && !notAsked(errors, key.split(" ")[0])) { entry.row.remove(); view.rows.delete(key); }
  }
  for (const q of questions) {
    const existing = view.rows.get(questionKey(q));
    if (existing) { existing.update(q, ages[q.host]); continue; }
    const entry = approvalRow(q, view);
    entry.update(q, ages[q.host]);
    view.rows.set(questionKey(q), entry);
    view.body.append(entry.row);
  }
  view.table.hidden = !view.rows.size; view.empty.hidden = Boolean(view.rows.size) || Boolean(Object.keys(errors).length);
};

// --- the doctor, for a human: the desktop app's view (projects/sandbox-ai-app/sandbox_ai_app/doctor_view.py) ----
// The checks grouped by area, problems and warnings first and open, each with its status in an icon and a word, what
// was found as text to read, the advice, and the fix command in a block of its own to copy (never run here); the
// fine ones folded away. An older sandbox-ai that answers with text: that text, as it is.
const DOCTOR_STATUSES = ["fail", "warn", "ok", "skip"];
const DOCTOR_SYMBOLS = { ok: "✔", warn: "!", fail: "✘", skip: "–" };
const DOCTOR_WORDS = { ok: N_("Fine"), warn: N_("Warning"), fail: N_("Problem"), skip: N_("Not checked") };
// The doctor's own areas (doctor.GROUPS), in its order; a check that fits none is in the last.
const DOCTOR_AREAS = { System: N_("System"), Engine: N_("Engine (Podman, Bubblewrap, images)"), Agents: N_("Agents (logins, kits, plugins)"),
  Project: N_("Project and security mode") };
const DOCTOR_GROUPS = Object.entries(DOCTOR_AREAS);
// The doctor's titles by area, for results without a group (an older sandbox-ai's --json).
const DOCTOR_PREFIXES = {
  System: ["Ordinary user", "Python", "sandbox-ai", "User namespaces", "systemd user session", "Connection proxy"],
  Engine: ["Podman", "podman machine", "pasta", "Delegated UID/GID ranges", "newuidmap", "Image ", "VPN", "Bubblewrap", "TIOCSTI", "Firejail", "nft", "setpriv", "slirp"],
  Agents: ["Login ", "Kit ", "Kits", "Plugins"],
};
const DOCTOR_COMMAND_WORDS = ["sandbox-ai", "sudo", "podman", "systemctl", "loginctl", "modprobe", "sysctl", "apt", "apt-get", "dnf",
  "pacman", "zypper", "brew", "usermod", "git", "echo", "chmod", "mkdir"];

// The area of the check titled CHECK.
function doctorGroupOf(check) {
  for (const [name, prefixes] of Object.entries(DOCTOR_PREFIXES)) if (prefixes.some(p => check === p || check.startsWith(p))) return name;
  return DOCTOR_GROUPS[DOCTOR_GROUPS.length - 1][0];
}

// The command in a doctor's hint, to copy (`podman info`, "sandbox-ai build claude", "sudo modprobe tun ..."), or ""
// when the hint is advice in words.
function doctorCommand(hint) {
  const whole = String(hint || "").trim();
  const quoted = /\x60([^\x60]+)\x60/.exec(whole);  // `a command` (\x60: the backtick)
  if (quoted) return quoted[1].trim();
  const text = whole.replace(/^(?:run|try|then run)\s*:\s+/i, "");
  if (!DOCTOR_COMMAND_WORDS.includes(text.split(" ", 1)[0])) return "";
  return text.split(/\s+\(|;\s|,\s+(?:or|then|and)\s/)[0].trim().replace(/\.+$/, "");
}

// [{status, check, detail, hint, group, command}] of a doctor --json answer (anything else in it is left out).
function doctorChecks(results) {
  const groups = DOCTOR_GROUPS.map(([key]) => key);
  return (Array.isArray(results) ? results : []).filter(r => r && typeof r === "object").map(r => {
    const status = DOCTOR_STATUSES.includes(r.status) ? r.status : "skip";
    const [check, detail, hint] = [r.check, r.detail, r.hint].map(v => (v === undefined || v === null ? "" : String(v)));
    return { status, check, detail, hint, group: groups.includes(r.group) ? r.group : doctorGroupOf(check), command: doctorCommand(hint) };
  });
}

// A command to copy: in a block of its own, with a Copy button (the clipboard, else the command selected).
function commandBlock(command) {
  const code = el("code", { class: "mono" }, command);
  const copy = el("button", { class: "small", "aria-label": t("Copy the command {command}", { command }), onclick: async () => {
    let copied = false;
    try {
      if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") { await navigator.clipboard.writeText(command); copied = true; }
    } catch (e) { copied = false; }
    if (!copied) selectText(code);
    copy.textContent = copied ? t("Copied") : t("Selected: copy it with Ctrl+C");
    say(copied ? t("Copied: {command}", { command }) : t("The command is selected: copy it with Ctrl+C"));
  } }, t("Copy"));
  return el("div", { class: "command-block" }, code, copy);
}

function selectText(node) {
  try {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return true;
  } catch (e) { return false; }
}

const DOCTOR_STATE_CLASS = { ok: "doctor-state status-ok", warn: "doctor-state status-warn", fail: "doctor-state status-fail", skip: "doctor-state status-skip" };

// One check: its status (icon and word), its name, what was found, and for a problem or warning how to fix it.
function doctorItem(c) {
  const item = el("li", { class: "doctor-check", "data-status": c.status },
    el("div", { class: "doctor-head" }, el("span", { class: DOCTOR_STATE_CLASS[c.status] }, el("span", { "aria-hidden": "true" }, DOCTOR_SYMBOLS[c.status]), " ", t(DOCTOR_WORDS[c.status])),
      el("strong", { class: "doctor-name" }, c.check)),
    c.detail ? el("p", { class: "doctor-detail" }, c.detail) : null);
  if (c.hint && (c.status === "fail" || c.status === "warn")) {
    // advice in words; a hint that is only the command is the command alone
    const bare = c.hint.trim().replace(/^(?:run|try|then run)\s*:\s+/i, "").replace(/\.+$/, "");
    if (!c.command || bare !== c.command) item.append(el("p", { class: "doctor-hint" }, el("span", { class: "doctor-label" }, t("What to do:")), " ", c.hint));
    if (c.command) item.append(commandBlock(c.command));
  }
  return item;
}

// The doctor's RESULTS (--json: a list of checks; an older sandbox-ai: its text) as one readable view. OPTIONS.level:
// the heading level of its areas (2 on the doctor page, 3 below a host's test); OPTIONS.host: whose doctor it is.
function doctorView(results, options) {
  const opts = options || {};
  const heading = `h${opts.level || 2}`, sub = `h${(opts.level || 2) + 1}`;
  if (typeof results === "string") {
    return el("div", { class: "doctor" }, notice(t("The sandbox-ai of {host} answered with text (an older version without the doctor's list): shown as it is. An update gives the readable list.", { host: opts.host || state.host }), "warn"),
      el("pre", { class: "doctor-text" }, results));
  }
  const checks = doctorChecks(results);
  const count = (status) => checks.filter(c => c.status === status).length;
  const problems = count("fail"), warnings = count("warn"), fine = count("ok"), skipped = count("skip");
  const view = el("div", { class: "doctor" });
  if (!checks.length) { view.append(notice(t("The doctor found nothing to check."), "")); return view; }
  view.append(el("p", { class: `doctor-summary notice ${problems ? "error" : warnings ? "warn" : "ok"}` },
    problems || warnings ? t("{problems} problem(s), {warnings} warning(s), {fine} fine: each says how to fix it.", { problems, warnings, fine })
      : t("Everything needed is in place: {fine} checks are fine.", { fine }),
    skipped ? ` ${t("{count} not checked.", { count: skipped })}` : ""));
  const order = (c) => DOCTOR_STATUSES.indexOf(c.status);
  for (const [key, title] of DOCTOR_GROUPS) {
    const open = checks.filter(c => c.group === key && (c.status === "fail" || c.status === "warn")).sort((a, b) => order(a) - order(b));
    if (!open.length) continue;
    const failedHere = open.filter(c => c.status === "fail").length, warnedHere = open.length - failedHere;
    view.append(el("section", { class: "doctor-group", "data-group": key }, el(heading, {}, t(title), " ",
      el("span", { class: "hint" }, t("{problems} problem(s), {warnings} warning(s)", { problems: failedHere, warnings: warnedHere }))),
    el("ul", { class: "doctor-checks" }, open.map(doctorItem))));
  }
  // the fine ones, and those not checked here, folded away, by area
  const folded = (status, summary) => {
    const rest = checks.filter(c => c.status === status);
    if (!rest.length) return null;
    return el("details", { class: "doctor-folded", "data-status": status }, el("summary", {}, summary),
      DOCTOR_GROUPS.filter(([key]) => rest.some(c => c.group === key)).map(([key, title]) => el("section", { class: "doctor-group", "data-group": key },
        el(sub, {}, t(title)), el("ul", { class: "doctor-checks compact" }, rest.filter(c => c.group === key).map(doctorItem)))));
  };
  for (const part of [folded("ok", fine === 1 ? t("1 check is fine") : t("{count} checks are fine", { count: fine })),
    folded("skip", skipped === 1 ? t("1 check was not made here") : t("{count} checks were not made here", { count: skipped }))]) if (part) view.append(part);
  return view;
}

pages.doctor = async (main) => {
  const host = state.host;
  const backend = state.doctorBackend || "";
  main.append(el("h1", {}, t("Doctor on {host}", { host })), el("p", { class: "hint" }, t("Checking…")));
  const live = pageLive();
  const results = await api("GET", backend ? hostPath(`/doctor/${encodeURIComponent(backend)}`) : hostPath("/doctor"));
  if (!live()) return;
  const choice = el("select", { id: "doctor-backend", onchange: (event) => { state.doctorBackend = event.target.value; show("doctor"); } },
    [["", N_("as the host has it")], ["podman", "Podman"], ["bwrap", "Bubblewrap"], ["both", N_("both")]].map(([v, label]) => el("option", { value: v, selected: v === backend }, v === "podman" || v === "bwrap" ? label : t(label))));
  clear(main).append(el("h1", {}, t("Doctor on {host}", { host })),
    el("div", { class: "row" }, el("label", { for: "doctor-backend" }, t("Backend")), choice),
    el("p", { class: "hint" }, t("Each command is only shown to copy: run it yourself in a terminal on {host}.", { host })),
    doctorView(results, { level: 2, host }));
};

pages.logs = async (main) => {
  const host = state.host, where = state.logsOf && state.logsOf.host === host ? state.logsOf : null;
  main.append(el("h1", {}, t("Logs on {host}", { host: state.host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  const data = where ? await api("POST", hostPath("/logs"), { directory: where.directory, project: where.project }) : await api("GET", hostPath("/logs"));
  if (!live()) return;
  const directory = el("input", { id: "logs-directory", class: "mono", placeholder: "/home/you/.local/state/sandbox-ai/logs/web", value: where ? where.directory : "" });
  const project = el("input", { id: "logs-project", class: "mono", placeholder: "/home/you/src/project", value: where ? where.project : "" });
  clear(main).append(el("h1", {}, t("Logs on {host}", { host: state.host })),
    el("div", { class: "card" }, el("div", { class: "form" }, el("label", { for: "logs-directory" }, t("Log directory")), directory, el("label", { for: "logs-project" }, t("Or a project")), project,
      el("span"), el("div", { class: "row" }, el("button", { class: "primary", onclick: () => { state.logsOf = directory.value.trim() || project.value.trim() ? { host, directory: directory.value.trim(), project: project.value.trim() } : null; show("logs"); } }, t("Show")),
        el("button", { onclick: () => { state.logsOf = null; show("logs"); } }, t("The default"))))));
  privacyCard(main, host);
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

// What a privacy check found ([{repository, checked, refused, scanned, findings, notes}]): a refused repository
// ("checked": false, why in "refused") is "not checked: why", never clean; the all-clear only when every
// repository was checked and nothing was found, never for a list that names none.
function privacyResult(found, host) {
  const list = (Array.isArray(found) ? found : []).filter(r => r && typeof r === "object");
  if (!list.length) return [notice(t("Nothing was checked on {host}: there is no all-clear.", { host }), "error")];
  const refused = list.filter(r => r.checked === false || r.refused);
  const findings = list.reduce((n, r) => n + (Array.isArray(r.findings) ? r.findings.length : 0), 0);
  const nodes = refused.map(r => notice(t("{repository}: not checked: {why}", { repository: String(r.repository || "?"), why: String(r.refused || t("refused")) }), "error"));
  if (findings) nodes.push(notice(t("{count} finding(s) that should stay private: see below.", { count: findings }), "error"));
  else if (!refused.length) nodes.push(notice(t("Nothing that should stay private."), "ok"));
  nodes.push(dataView(list.map(r => ({ repository: r.repository, checked: r.checked !== false && !r.refused, findings: Array.isArray(r.findings) ? r.findings.length : 0,
    scanned: r.scanned, notes: (Array.isArray(r.notes) ? r.notes : []).join("; ") }))));
  for (const r of list.filter(x => Array.isArray(x.findings) && x.findings.length)) {
    nodes.push(el("h3", {}, String(r.repository || "?")), dataView(r.findings.filter(f => f && typeof f === "object")));
  }
  return nodes;
}

// sandbox-ai privacy-check --json on a host: what the next push of each repository would publish that should stay
// private (nothing is pushed).
function privacyCard(main, host) {
  const paths = el("textarea", { id: "privacy-paths", rows: 2, class: "mono", placeholder: "/home/you/src/project" });
  const rules = el("input", { id: "privacy-rules", class: "mono", placeholder: t("default") }), project = el("input", { id: "privacy-project", class: "mono", placeholder: t("the directory's name") });
  const every = el("input", { id: "privacy-all", type: "checkbox" }), result = el("div", { class: "wide" });
  main.append(el("h2", {}, t("Privacy check of repositories")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "privacy-paths" }, t("Repositories (one per line)")), paths, el("label", { for: "privacy-rules" }, t("Rules directory")), rules,
    el("label", { for: "privacy-project" }, t("Project rule file")), project, el("label", { for: "privacy-all" }, t("Every file, also unchanged ones")), every,
    el("span"), el("button", { class: "primary", onclick: (event) => busy(event.target, async () => {
      clear(result).append(el("p", { class: "hint" }, t("Checking…")));
      let found;
      try { found = await api("POST", hostPath("/privacy-check", host), { paths: linesOf(paths.value), rules_dir: rules.value.trim(), project: project.value.trim(), all: every.checked }); }
      catch (e) { clear(result).append(errorNotice(e)); return; }
      clear(result).append(...privacyResult(found, host));
    }) }, t("Check")), result)));
}

pages.vpn = async (main) => {
  const live = pageLive();
  const profiles = await api("GET", hostPath("/vpn"));
  if (!live()) return;
  clear(main).append(el("h1", {}, t("VPN profiles on {host}", { host: state.host })),
    el("p", { class: "hint" }, t("A profile names the secret that holds its key or password (kept on the page Secrets of that host): no key passes through this page."), " ",
      el("button", { onclick: () => show("secrets") }, t("Open Secrets"))));
  const host = state.host;
  const shownProfile = el("div");
  // every request of a row goes to the row's host (the page's, as it was when it was drawn)
  const rows = (Array.isArray(profiles) ? profiles : []).filter(p => p && typeof p.profile === "string").map(p => {
    const actions = el("td", { class: "actions" });
    const remove = rowButton(t("Remove"), p.profile, host, { class: "danger", onclick: () => confirmIn(actions, t("Remove the VPN profile {profile} from {host}? Starts that use it ({option}) cannot start until it is added again.", { profile: p.profile, host, option: `--vpn ${p.profile}` }), t("Yes, remove it"), async () => {
      try { await api("DELETE", hostPath(`/vpn/${encodeURIComponent(p.profile)}`, host), { confirm: true }); show("vpn"); } catch (e) { failed(t("Could not remove {what} from {where}", { what: p.profile, where: host }))(e); }
    }, { trigger: remove }) });
    actions.append(
      rowButton(t("Show"), p.profile, host, { onclick: async (event) => busy(event.target, async () => {
        try { clear(shownProfile).append(el("h3", {}, p.profile), dataView(await api("GET", hostPath(`/vpn/${encodeURIComponent(p.profile)}`, host)))); } catch (e) { clear(shownProfile).append(errorNotice(e)); } }) }),
      rowButton(t("Check"), p.profile, host, { onclick: async (event) => busy(event.target, async () => {
        try { toast("success", t("{profile} works on {host}", { profile: p.profile, host }), (await api("POST", hostPath(`/vpn/${encodeURIComponent(p.profile)}/check`, host), {})).output || "ok"); }
        catch (e) { failed(t("The check of {profile} on {host} failed", { profile: p.profile, host }))(e); } }) }),
      remove);
    return el("tr", {}, el("td", { class: "mono" }, p.profile), el("td", {}, p.type || ""), el("td", {}, p.provider || ""), actions);
  });
  main.append(table([t("Profile"), t("Kind"), t("Provider"), ""], rows, t("No VPN profiles on this host.")), shownProfile);
  const name = el("input", { id: "vpn-name", placeholder: "home", pattern: "[a-z0-9][a-z0-9_-]{0,31}" });
  const kind = el("select", { id: "vpn-kind" }, [["wireguard", N_("WireGuard (a wg-quick file)")], ["openvpn", N_("OpenVPN (an .ovpn file)")],
    ["mullvad", N_("Mullvad (account number in a secret)")], ["tailscale", N_("Tailscale or Headscale (auth key in a secret)")], ["tor", N_("Tor (every connection through Tor)")]].map(([v, label]) => el("option", { value: v }, t(label))));
  const file = el("input", { id: "vpn-file", class: "mono", placeholder: t("{path} (on that host)", { path: "/home/you/vpn/home.conf" }) });
  const secret = el("input", { id: "vpn-secret", class: "mono", placeholder: "VPN_HOME" });
  const location = el("input", { id: "vpn-location", placeholder: "se-sto" });
  const exitNode = el("input", { id: "vpn-exit", placeholder: "exit-node-1" });
  const loginServer = el("input", { id: "vpn-login", placeholder: "https://headscale.example.org" });
  const hostname = el("input", { id: "vpn-hostname", class: "mono", placeholder: "sandbox-ai-home" }), tailnet = el("input", { id: "vpn-tailnet", class: "mono", placeholder: "100.64.0.0/10" });
  const bridges = el("input", { id: "vpn-bridges", class: "mono", placeholder: "/home/you/tor/bridges.txt" }), bridgeSecret = el("input", { id: "vpn-bridge-secret", class: "mono", placeholder: "TOR_BRIDGES" });
  const exitCountry = el("input", { id: "vpn-exit-country", placeholder: "de,nl" });
  const rows2 = { file: [file, N_("Configuration file")], secret: [secret, N_("Secret (its name)")], location: [location, N_("Location")],
    exit_node: [exitNode, N_("Exit node")], login_server: [loginServer, N_("Login server")], hostname: [hostname, N_("Node name")], tailnet_allow: [tailnet, N_("Tailnet range the agent may reach")],
    bridges: [bridges, N_("Bridges file")], bridge_secret: [bridgeSecret, N_("Bridges: secret (its name)")], exit_country: [exitCountry, N_("Exit countries")] };
  const wanted = { wireguard: ["file"], openvpn: ["file", "secret"], mullvad: ["secret", "location"], tailscale: ["secret", "exit_node", "login_server", "hostname", "tailnet_allow"],
    tor: ["bridges", "bridge_secret", "exit_country"] };
  // the switches of vpn add, each for the kinds that have it ([] for all); socks is asked first (it passes the proxy by)
  const SWITCHES = [["split", N_("only the server's routes through the tunnel"), ["openvpn"]], ["no_ipv6", N_("no IPv6 in the tunnel"), ["mullvad"]],
    ["endpoint_ipv6", N_("reach the relay over IPv6"), ["mullvad"]], ["ephemeral", N_("a new ephemeral node per start"), ["tailscale"]],
    ["socks", N_("a SOCKS port in the sandbox (passes the connection proxy by)"), ["tor"]], ["no_dns_probe", N_("no probe of where DNS leaves"), []], ["force", N_("replace a profile of that name"), []]];
  const switches = Object.fromEntries(SWITCHES.map(([key]) => [key, el("input", { type: "checkbox", id: `vpn-${key}` })]));
  const switchLabels = Object.fromEntries(SWITCHES.map(([key, label]) => [key, el("label", {}, switches[key], " ", t(label))]));
  const dnsServers = el("input", { id: "vpn-dns", class: "mono", placeholder: "10.64.0.1" }), checkUrls = el("input", { id: "vpn-check-urls", class: "mono", placeholder: "https://am.i.mullvad.net/json" });
  const provider = el("input", { id: "vpn-provider", class: "mono", placeholder: "proton" });
  const form = el("div", { class: "form" }, el("label", { for: "vpn-name" }, t("Name")), name, el("label", { for: "vpn-kind" }, t("Kind")), kind);
  const labels = {};
  for (const [key, [input, text]] of Object.entries(rows2)) { labels[key] = el("label", { for: input.id }, t(text)); form.append(labels[key], input); }
  const kindOf = () => (Object.prototype.hasOwnProperty.call(wanted, kind.value) ? kind.value : "wireguard");  // the select's first
  const update = () => {
    for (const key of Object.keys(rows2)) { const on = wanted[kindOf()].includes(key); rows2[key][0].hidden = !on; labels[key].hidden = !on; }
    for (const [key, , kinds] of SWITCHES) switchLabels[key].hidden = Boolean(kinds.length) && !kinds.includes(kindOf());
  };
  form.append(el("label", { for: "vpn-dns" }, t("Resolvers")), dnsServers, el("label", { for: "vpn-check-urls" }, t("Check URLs")), checkUrls,
    el("label", { for: "vpn-provider" }, t("Provider")), provider, el("span", {}, t("Options")), el("div", { class: "row" }, Object.values(switchLabels)));
  const addRow = el("div", { class: "row" });
  kind.addEventListener("change", update);
  update();
  form.append(el("span"), addRow);
  addRow.append(el("button", { class: "primary", onclick: (event) => {
    const body = { name: textOf(name), kind: kindOf() };
    const more = { dns: wordsOf(dnsServers.value), check_urls: wordsOf(checkUrls.value), provider: textOf(provider) };
    for (const [key, value] of Object.entries(more)) if (value.length) body[key] = value;
    for (const key of wanted[body.kind]) { const value = textOf(rows2[key][0]); if (value) body[key] = key === "tailnet_allow" ? wordsOf(value) : value; }
    for (const [key, , kinds] of SWITCHES) if (switches[key].checked && (!kinds.length || kinds.includes(body.kind))) body[key] = true;
    const send = (confirm) => busy(event.target, async () => { try { await api("POST", hostPath("/vpn", host), Object.assign({}, body, confirm ? { confirm: true } : {})); show("vpn"); } catch (e) { failed(t("Could not add the VPN profile"))(e); } });
    const asks = [body.socks ? t("Give the sandboxes of {profile} on {host} a SOCKS port? What goes through it passes the connection proxy by: no inspection, no approvals.", { profile: body.name, host }) : null,
      body.force ? t("Replace the VPN profile {profile} on {host}? Every start that uses it gets the new tunnel.", { profile: body.name, host }) : null].filter(Boolean);
    if (!asks.length) { send(false); return; }
    confirmIn(addRow, asks.join(" "), body.force ? t("Yes, replace it") : t("Yes, add it"), () => send(true), { kind: "primary", trigger: event.target });
  } }, t("Add profile")));
  main.append(el("h2", {}, t("New profile")), el("div", { class: "card" }, form));
};

// An LLM server's name as sandbox-ai llm takes it (llm.NAME): null when it is one (or empty: asked for when sent),
// else what is wrong with it, naming the first character that cannot be there, and a name that would do (FIXED).
const LLM_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;
function llmNameProblem(text) {
  const value = String(text || "");
  if (!value || LLM_NAME.test(value)) return null;
  const fixed = value.replace(/[^A-Za-z0-9_-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 32).replace(/[-_]+$/, "") || "llm";
  const at = [...value].findIndex((c, i) => !/[A-Za-z0-9]/.test(c) && (i === 0 || !/[_-]/.test(c)));
  const char = at < 0 ? "" : [...value][at];
  const shown = char === " " ? t("a space") : `“${char}”`;
  const message = at === 0 ? t("An LLM server's name starts with a letter or a digit, not {char}.", { char: shown })
    : at > 0 ? t("{char} cannot be in an LLM server's name: letters, digits, _ and - (up to 32).", { char: shown })
      : t("An LLM server's name is at most 32 characters.");
  return { text: message, fixed };
}

// What `llm show NAME --json` says of a server, as the body of POST /llm (llm add NAME ... --replace): the rest as it
// is, CHANGES on top. Its CA certificate is not in it: the host keeps only its own copy, which the page cannot name.
function llmBodyOf(view, changes) {
  const v = view && typeof view === "object" ? view : {};
  const headers = v.headers && typeof v.headers === "object" ? Object.entries(v.headers).map(([header, secret]) => ({ header, secret })) : [];
  const body = { name: v.name, kind: v.kind, url: v.url, model: v.model || "" };
  if (v.api_key_secret) body.api_key_secret = v.api_key_secret;
  if (headers.length) body.header_secrets = headers;
  if (v.vpn) body.vpn = v.vpn;
  if (v.anthropic) body.anthropic = true;
  if (v.insecure_http) body.insecure_http = true;
  return Object.assign(body, changes || {});
}

// llm models --json: [{name, size, loaded, details, type?, arch?, parameters?, quantization?, context?}] as a table:
// only the columns some model has a value for; loaded first, then by name; a long name cut (the whole one in its
// title); a filter. ACTIONS (the LLM page): {use(model, button, cell), copy(ref, button), current: its model, host}.
const MODEL_NAME_WIDTH = 48;  // as llm models cuts it (llm.NAME_WIDTH)
function modelsTable(server, models, actions) {
  const list = (Array.isArray(models) ? models : []).filter(m => m && typeof m === "object" && typeof m.name === "string")
    .sort((a, b) => ({ true: 0, false: 1 }[String(a.loaded)] ?? 2) - ({ true: 0, false: 1 }[String(b.loaded)] ?? 2) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const loaded = { true: N_("yes"), false: N_("no") };
  const has = (value) => value !== undefined && value !== null && value !== "";
  const contextOf = (n) => (n >= 1024 && n % 1024 === 0 ? `${n / 1024}k` : String(n));
  const columns = [
    ["loaded", N_("Loaded"), m => (loaded[String(m.loaded)] ? t(loaded[String(m.loaded)]) : "")],
    ["type", N_("Type"), m => [m.type, m.arch].filter(has).join(" · "), m => has(m.type) || has(m.arch)],
    ["parameters", N_("Params"), m => String(m.parameters ?? "")],
    ["quantization", N_("Quant"), m => String(m.quantization ?? "")],
    ["context", N_("Context"), m => (typeof m.context === "number" ? contextOf(m.context) : "")],
    ["size", N_("Size"), m => (typeof m.size === "number" ? readableSize(m.size) : "")],
  ].filter(([key, , , any]) => list.some(m => (any ? any(m) : has(m[key]))));
  const rows = list.map(m => {
    const cut = m.name.length > MODEL_NAME_WIDTH ? `${m.name.slice(0, MODEL_NAME_WIDTH - 1)}…` : m.name;
    const cells = [el("td", { class: "mono", title: cut !== m.name ? m.name : null }, cut), ...columns.map(([key, , value]) => el("td", { class: key === "context" ? "mono" : null, title: key === "context" && typeof m.context === "number" ? String(m.context) : null }, value(m)))];
    if (actions) {
      const cell = el("td", { class: "actions" });
      const ref = `${server}:${m.name}`;
      if (m.name === actions.current) cell.append(el("span", { class: "hint" }, t("the default"), " "));
      else cell.append(rowButton(t("Use as default"), ref, actions.host, { onclick: (ev) => actions.use(m.name, ev.target, cell) }), " ");
      cell.append(el("button", { "aria-label": t("Copy {ref}", { ref }), title: ref, onclick: (ev) => actions.copy(ref, ev.target) }, t("Copy {ref}", { ref: `${server}:${cut}` })));
      cells.push(cell);
    }
    return el("tr", { "data-model": m.name }, cells);
  });
  const filter = el("input", { type: "search", id: "llm-models-filter", class: "mono", placeholder: t("Filter"), "aria-label": t("Filter the models of {server}", { server }), autocomplete: "off" });
  const count = el("span", { class: "hint" });
  const apply = () => {
    const words = String(filter.value || "").trim().toLowerCase();
    let shownRows = 0;
    for (const row of rows) { row.hidden = Boolean(words) && !row.getAttribute("data-model").toLowerCase().includes(words); if (!row.hidden) shownRows += 1; }
    count.textContent = words ? t("{shown} of {all} models", { shown: shownRows, all: rows.length }) : t("{all} models", { all: rows.length });
  };
  filter.addEventListener("input", apply);
  apply();
  return el("div", {}, el("h3", {}, t("Models of {server}", { server })), rows.length > 1 ? el("div", { class: "row" }, filter, count) : null,
    table([t("Name"), ...columns.map(([, label]) => t(label)), ...(actions ? [""] : [])], rows, t("The server lists no models."), t("Models of {server}", { server })));
}

pages.llm = async (main) => {
  main.append(el("h1", {}, t("LLM servers on {host}", { host: state.host })));
  const live = pageLive();
  const list = await api("GET", hostPath("/llm"));
  if (!live()) return;
  const output = el("div");
  const host = state.host;
  const listBox = el("div");
  let servers = [];
  const path = (name, more) => hostPath(`/llm/${encodeURIComponent(name)}${more || ""}`, host);
  // the list again (after a change made from the form), the form left as it is
  const refreshList = async () => { try { drawList(await api("GET", hostPath("/llm", host))); } catch (e) { /* the list as it was */ } };
  // Use as default: the server saved again with that model, the rest from llm show (llm-replace, asked first)
  const useModel = (server) => async (model, trigger, holder) => busy(trigger, async () => {
    let view;
    try { view = await api("GET", path(server)); } catch (e) { output.append(errorNotice(e)); return; }
    if (view && view.ca) { output.append(notice(t("{name} has a CA certificate of its own ({file}): a change from this page would remove it. Use Edit… and give its file again.", { name: server, file: String(view.ca) }), "warn")); return; }
    const body = llmBodyOf(view, { model, replace: true });
    const asks = [t("Make {model} the default model of {name} on {host}? The server is saved again with it (sandbox-ai llm add --replace), the rest as it is.", { model, name: server, host }),
      body.insecure_http ? t("It still sends the prompts to {url} over plain http, unencrypted.", { url: body.url }) : null].filter(Boolean);
    confirmIn(holder, asks.join(" "), t("Yes, use it"), async () => {
      try { await api("POST", hostPath("/llm", host), Object.assign(body, { confirm: true })); toast("success", t("{model} is the default model of {name}", { model, name: server }), ""); await refreshList(); showModels(server, model); }
      catch (e) { output.append(errorNotice(e)); }
    }, { kind: "primary", trigger });
  });
  const copyRef = async (ref, button) => {
    let copied = false;
    try { if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") { await navigator.clipboard.writeText(ref); copied = true; } } catch (e) { copied = false; }
    // not copied (the browser does not allow it here): the reference in the button's place, selected
    if (copied) button.textContent = t("Copied");
    else { const code = el("code", { class: "mono" }, ref); button.replaceWith(code); selectText(code); }
    say(copied ? t("Copied: {command}", { command: ref }) : t("{ref} is selected: copy it with Ctrl+C", { ref }));
  };
  const showModels = async (server, current) => {
    clear(output);
    try { output.append(modelsTable(server, await api("GET", path(server, "/models")), { use: useModel(server), copy: copyRef, current, host })); }
    catch (err) { output.append(errorNotice(err)); }
  };
  // every request of a row goes to the row's host (the page's, as it was when it was drawn), never the host chosen since
  const drawList = (found) => {
    servers = (Array.isArray(found) ? found : []).filter(e => e && typeof e.name === "string");
    const rows = servers.map(e => {
      const agent = el("select", { "aria-label": t("Agent for the check of {name} on {host}", { name: e.name, host }) }, el("option", { value: "" }, t("every agent")), AGENTS.map(a => el("option", { value: a }, a)));
      const actions = el("td", { class: "actions" });
      const row = el("tr", {}, el("td", { class: "mono" }, e.name), el("td", {}, e.kind), el("td", { class: "mono" }, e.url), el("td", { class: "mono" }, e.model || ""), actions);
      const remove = rowButton(t("Remove"), e.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Remove the LLM server {name} on {host}? Starts with {option} cannot start until it is added again.", { name: e.name, host, option: `--llm ${e.name}` }), t("Yes, remove it"), async () => {
        try { await api("DELETE", path(e.name), { confirm: true }); show("llm"); } catch (err) { failed(t("Could not remove {name} on {host}", { name: e.name, host }))(err); }
      }, { trigger: remove }) });
      actions.append(agent,
        rowButton(t("Check"), e.name, host, { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try { const r = await api("POST", path(e.name, "/check"), agent.value ? { agent: agent.value } : {}); output.append(notice(r.ok ? t("the server works for it") : t("the check found problems"), r.ok ? "ok" : "error"), el("pre", {}, r.output)); }
          catch (err) { output.append(errorNotice(err)); }
        }) }),
        rowButton(t("Models"), e.name, host, { onclick: (ev) => busy(ev.target, () => showModels(e.name, e.model || null)) }),
        rowButton(t("Show"), e.name, host, { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try { output.append(el("h3", {}, e.name), dataView(await api("GET", path(e.name)))); }
          catch (err) { output.append(errorNotice(err)); }
        }) }),
        rowButton(t("Edit…"), e.name, host, { onclick: (ev) => busy(ev.target, () => editServer(e.name)) }),
        remove);
      return row;
    });
    clear(listBox).append(table([t("Name"), t("Kind"), "URL", t("Model"), ""], rows, t("No LLM server yet.")));
  };
  drawList(list);
  main.append(listBox, output);

  // the form: a new server, or (Edit…) one of the list prefilled from llm show and saved with --replace
  const name = el("input", { id: "llm-name", placeholder: "lmstudio", autocomplete: "off", spellcheck: "false", "aria-describedby": "llm-name-problem" });
  const nameProblem = el("div", { id: "llm-name-problem", class: "wide" });
  const kind = el("select", { id: "llm-kind" }, ["lmstudio", "ollama", "llamacpp", "vllm", "openai-compatible"].map(k => el("option", { value: k }, k)));
  const url = el("input", { id: "llm-url", class: "mono", placeholder: "http://192.168.1.20:1234" });
  const model = el("input", { id: "llm-model", class: "mono", placeholder: t("optional") });
  const modelPick = el("select", { id: "llm-model-pick", class: "mono" });
  const modelPickLabel = el("label", { for: "llm-model-pick" }, t("Default model (listed)"));
  modelPick.hidden = true; modelPickLabel.hidden = true;
  modelPick.addEventListener("change", () => { if (modelPick.value) model.value = modelPick.value; });
  // the NAME of the secret that holds the API key (sandbox-ai secrets there), never the key
  const keySecret = el("input", { id: "llm-key-secret", class: "mono", placeholder: t("optional, e.g. {name}", { name: "LMSTUDIO_KEY" }), pattern: "[A-Za-z_][A-Za-z0-9_]{0,63}", autocomplete: "off", "aria-describedby": "llm-hint" });
  const status = el("div", { class: "wide" });
  const testBox = el("div", { class: "wide", id: "llm-test-result" });
  const headerSecrets = el("input", { id: "llm-headers", class: "mono", placeholder: "X-Gateway-Key=GW_KEY" }), ca = el("input", { id: "llm-ca", class: "mono", placeholder: "/home/you/ca.pem" });
  const caNote = el("div", { class: "wide hint", id: "llm-ca-note" });
  const vpn = el("input", { id: "llm-vpn", class: "mono", placeholder: "home" });
  const anthropic = el("input", { id: "llm-anthropic", type: "checkbox" }), replace = el("input", { id: "llm-replace", type: "checkbox" }), insecure = el("input", { id: "llm-insecure", type: "checkbox" });
  const heading = el("h2", { id: "llm-form-title" }, t("Add a server"));
  const addRow = el("div", { class: "row" });
  let editing = null;  // {name, ca, saved}: the server the form changes (Edit…), as llm show said and as last saved
  const checkName = () => {
    clear(nameProblem);
    const problem = llmNameProblem(name.value.trim());
    if (problem) name.setAttribute("aria-invalid", "true"); else name.removeAttribute("aria-invalid");
    if (problem) nameProblem.append(notice(problem.text, "error"), el("button", { onclick: () => { name.value = problem.fixed; checkName(); } }, t("Use {name}", { name: problem.fixed })));
    return problem;
  };
  name.addEventListener("input", checkName);
  // what the form says as the body of POST /llm, or null (what is wrong shown in STATUS, every field kept)
  const formBody = () => {
    clear(status);
    if (!name.value.trim()) { status.append(notice(t("Which name? Letters, digits, _ and - (up to 32), starting with a letter or a digit."), "error")); return null; }
    const problem = checkName();
    if (problem) { status.append(notice(problem.text, "error")); return null; }
    const secret = keySecret.value.trim();
    if (secret && !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(secret)) { status.append(notice(t("A secret's name: letters, digits and _ (up to 64), not starting with a digit."), "error")); return null; }
    const headers = wordsOf(headerSecrets.value).map(pair => { const [header, ...rest] = pair.split("="); return { header, secret: rest.join("=") }; });
    const body = Object.assign({ name: name.value.trim(), kind: kind.value, url: url.value.trim(), model: model.value.trim() }, secret ? { api_key_secret: secret } : {});
    const more = { header_secrets: headers.length ? headers : null, ca: textOf(ca), vpn: textOf(vpn), anthropic: anthropic.checked, replace: replace.checked, insecure_http: insecure.checked };
    for (const [key, value] of Object.entries(more)) if (value) body[key] = value;
    return body;
  };
  // what is asked first: plain http outside the local networks, a server of that name replaced (and its CA dropped)
  const asksOf = (body) => [body.insecure_http ? t("Send the prompts to {url} over plain http, unencrypted, from {host}? Whoever is on the way can read them, the answers and the key.", { url: body.url, host }) : null,
    body.replace ? t("Replace the LLM server {name} on {host}? Every start that uses it sends its prompts (and its key) to {url} from then on.", { name: body.name, host, url: body.url }) : null,
    body.replace && editing && editing.ca && editing.name === body.name && !body.ca ? t("Its CA certificate ({file}) is removed: give its file again to keep it.", { file: editing.ca }) : null].filter(Boolean);
  const canon = (b) => JSON.stringify(Object.keys(b).filter(k => !["replace", "confirm"].includes(k) && b[k] !== undefined).sort().map(k => [k, b[k]]));
  const same = (body) => Boolean(editing && editing.saved) && canon(body) === canon(editing.saved);
  const setEditing = (view, saved) => {
    editing = view ? { name: view.name, ca: view.ca || null, saved: saved || null } : null;
    heading.textContent = editing ? t("Change the server {name}", { name: editing.name }) : t("Add a server");
    save.textContent = editing ? t("Save") : t("Add");
    cancelEdit.hidden = !editing;
    clear(caNote);
    if (editing && editing.ca) caNote.append(t("It has a CA certificate of its own ({file}): give its file again here, or saving removes it.", { file: editing.ca }));
  };
  const editServer = async (server) => {
    let view;
    try { view = await api("GET", path(server)); } catch (e) { clear(output).append(errorNotice(e)); return; }
    if (!view || typeof view !== "object" || typeof view.name !== "string") { clear(output).append(notice(t("{name}: llm show said nothing usable.", { name: server }), "error")); return; }
    const body = llmBodyOf(view);
    name.value = view.name; kind.value = view.kind || "openai-compatible"; url.value = view.url || ""; model.value = view.model || "";
    keySecret.value = view.api_key_secret || ""; headerSecrets.value = (body.header_secrets || []).map(h => `${h.header}=${h.secret}`).join(" ");
    ca.value = ""; vpn.value = view.vpn || ""; anthropic.checked = Boolean(view.anthropic); insecure.checked = Boolean(view.insecure_http); replace.checked = true;
    fillModels(Array.isArray(view.models) ? view.models.map(m => ({ name: m })) : []);
    clear(status); clear(testBox); checkName();
    setEditing(view, Object.assign(body, { replace: true }));
    if (typeof url.focus === "function") url.focus();
  };
  // the Default-model select, from what a test (or llm show's last check) listed
  const fillModels = (models) => {
    const names = (Array.isArray(models) ? models : []).filter(m => m && typeof m.name === "string")
      .sort((a, b) => ({ true: 0, false: 1 }[String(a.loaded)] ?? 2) - ({ true: 0, false: 1 }[String(b.loaded)] ?? 2) || (a.name < b.name ? -1 : 1)).map(m => m.name);
    clear(modelPick).append(el("option", { value: "" }, t("a listed model…")), ...names.map(n => el("option", { value: n, selected: n === model.value.trim() }, n)));
    modelPick.value = names.includes(model.value.trim()) ? model.value.trim() : "";
    modelPick.hidden = !names.length; modelPickLabel.hidden = !names.length;
  };
  const save = el("button", { class: "primary", onclick: (ev) => {
    const body = formBody();
    if (!body) return;
    const send = (confirm) => busy(ev.target, async () => { try { await api("POST", hostPath("/llm", host), Object.assign({}, body, confirm ? { confirm: true } : {})); show("llm"); } catch (e) { status.append(errorNotice(e)); } });
    const asks = asksOf(body);
    if (!asks.length) { send(false); return; }
    confirmIn(addRow, asks.join(" "), body.replace ? t("Yes, replace it") : t("Yes, add it"), () => send(true), { kind: "primary", trigger: ev.target });
  } }, t("Add"));
  // Test connection: sandbox-ai lists a server's models only once it is saved (llm models, llm check), so a new or
  // changed form is saved first (asked, with what saving asks); the result below, the form kept as it is
  const test = el("button", { onclick: (ev) => {
    const body = formBody();
    if (!body) return;
    const run = async (saveFirst) => {
      clear(testBox).append(el("p", { class: "hint" }, t("Testing {name}…", { name: body.name })));
      if (saveFirst) {
        try { await api("POST", hostPath("/llm", host), Object.assign({}, body, { confirm: true })); }
        catch (e) { clear(testBox).append(errorNotice(e, t("Not saved"))); return; }
        replace.checked = true;
        setEditing({ name: body.name, ca: body.ca ? `${body.name}.pem` : null }, Object.assign({}, body, { replace: true, ca: undefined }));
        await refreshList();
      }
      let models = null, modelsError = null, check = null;
      try { models = await api("GET", path(body.name, "/models")); } catch (e) { modelsError = e; }
      try { check = await api("POST", path(body.name, "/check"), {}); } catch (e) { check = { ok: false, output: errorText(e) }; }
      const listed = (Array.isArray(models) ? models : []).filter(m => m && typeof m.name === "string");
      clear(testBox).append(notice(check && check.ok ? t("{name} answers: {count} model(s) listed.", { name: body.name, count: listed.length }) : t("The test of {name} found problems.", { name: body.name }), check && check.ok ? "ok" : "error"),
        ...[modelsError ? errorNotice(modelsError) : null, check && check.output ? el("pre", {}, String(check.output)) : null].filter(Boolean));
      fillModels(listed);
    };
    if (same(body)) { busy(ev.target, () => run(false)); return; }
    const asks = [t("Test {name}: it is saved on {host} first (sandbox-ai llm add), then its models are listed and it is checked.", { name: body.name, host }), ...asksOf(body)];
    confirmIn(addRow, asks.join(" "), t("Yes, save and test"), () => run(true), { kind: "primary", trigger: ev.target });
  } }, t("Test connection"));
  const cancelEdit = el("button", { onclick: () => {
    for (const input of [name, url, model, keySecret, headerSecrets, ca, vpn]) input.value = "";
    for (const box of [anthropic, replace, insecure]) box.checked = false;
    clear(status); clear(testBox); fillModels([]); checkName(); setEditing(null);
  } }, t("Stop editing"));
  cancelEdit.hidden = true;
  addRow.append(save, test, cancelEdit);
  main.append(heading, el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "llm-name" }, t("Name")), name, nameProblem, el("label", { for: "llm-kind" }, t("Kind")), kind,
    el("label", { for: "llm-url" }, "URL"), url, el("label", { for: "llm-model" }, t("Model")), model, modelPickLabel, modelPick,
    el("label", { for: "llm-key-secret" }, t("API key: secret (its name)")), keySecret,
    el("label", { for: "llm-headers" }, t("Headers from secrets (HEADER=SECRET)")), headerSecrets, el("label", { for: "llm-ca" }, t("CA certificate")), ca, caNote,
    el("label", { for: "llm-vpn" }, t("VPN profile")), vpn, el("label", { for: "llm-anthropic" }, t("Also the Anthropic Messages API")), anthropic,
    el("label", { for: "llm-replace" }, t("Replace one of that name")), replace, el("label", { for: "llm-insecure" }, t("Plain http outside the local networks")), insecure,
    el("span"), addRow, status, testBox),
    el("p", { class: "hint", id: "llm-hint" }, t("Then start an agent with {option} (Options of a new start). A server that wants a key: keep it on the page Secrets of that host, and name that secret here.", { option: "--llm NAME" }), " ",
      el("button", { onclick: () => show("secrets") }, t("Open Secrets")))));
};

// Kits (sandbox-ai kits on that host): what each brings, and adding to a kit of yours. sandbox-ai's own kits are
// read-only: with one of them (or none) chosen, what is added goes to a kit of yours, or a new one created first.
const KIT_KINDS = { always: N_("always (sandbox-ai)"), default: N_("always (default kit)"), builtin: N_("built-in"), user: N_("yours") };

pages.kits = async (main) => {
  main.append(el("h1", {}, t("Kits on {host}", { host: state.host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  const data = await api("GET", hostPath("/kits"));
  if (!live()) return;
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
            const body = Object.assign(kind === "skill" ? { skill: value, agents: [agent] } : { plugin: value, agent }, { confirm: true });
            try { result(t("{kit}: removed {value}", { kit: k.name, value }), await api("POST", hostPath(`/kits/${encodeURIComponent(k.name)}/remove-${kind}`, host), body)); show("kits"); }
            catch (e) { failed(t("Could not remove {value}", { value }))(e); }
          }) }, "×"));
        line.append("  ");
      }
      if (info.error) line.append(el("span", { class: "status-bad" }, ` ✘ ${info.error}`));
      brings.append(line);
    }
    const kitActions = el("td", { class: "actions" });
    kitActions.append(rowButton(t("Show"), k.name, host, { onclick: (ev) => busy(ev.target, async () => {
      try { clear(output).append(el("h3", {}, k.name), dataView(await api("GET", hostPath(`/kits/${encodeURIComponent(k.name)}`, host)))); } catch (e) { clear(output).append(errorNotice(e)); }
    }) }));
    const trust = rowButton(t("Trust hooks…"), k.name, host, { title: t("the command shows the kit's hooks and asks again"), onclick: () => confirmIn(kitActions, t("Trust the hooks of the kit {kit} on {host}? They run in every later start with that kit; the terminal shows them first.", { kit: k.name, host }), t("Yes, show and trust them"),
      () => openTerminal({ kind: "trust-hooks", host, kit: k.name, confirm: "1" }, t("hooks of {kit}", { kit: k.name })), { kind: "primary", trigger: trust }) });
    kitActions.append(trust, rowButton(t("Revoke hooks"), k.name, host, { onclick: () => openTerminal({ kind: "trust-hooks", host, kit: k.name, revoke: "1" }, t("hooks of {kit}", { kit: k.name })) }));
    if (own) kitActions.append(rowButton(t("Delete kit"), k.name, host, { class: "danger", onclick: () => confirmIn(kitActions, t("Delete the kit {kit} on {host} with everything in it? Projects that use it cannot start until {option} is removed.", { kit: k.name, host, option: `--kit ${k.name}` }), t("Yes, delete it"), async () => {
      try { result(t("{kit} deleted", { kit: k.name }), await api("DELETE", hostPath(`/kits/${encodeURIComponent(k.name)}`, host), { confirm: true })); show("kits"); }
      catch (e) { failed(t("Could not delete {kit}", { kit: k.name }))(e); }
    }) }));
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
      catch (e) { newStatus.append(errorNotice(e)); }
    }) }, t("Create")), newStatus)));

  // adding to a kit of yours (or a new one)
  const target = el("select", { id: "kit-target" }, mine.map(n => el("option", { value: n }, n)), el("option", { value: "" }, t("a new kit…")));
  const targetName = el("input", { id: "kit-target-name", placeholder: "my-kit", value: mine.length ? "" : "my-kit" });
  const targetLabel = el("label", { for: "kit-target-name" }, t("New kit's name"));
  const updateTarget = () => { const fresh = target.value === ""; targetName.hidden = !fresh; targetLabel.hidden = !fresh; };
  target.addEventListener("change", updateTarget);
  updateTarget();
  const addStatus = el("div", { class: "wide" });
  const addAsk = el("div", { class: "row wide" });
  // add-skill, add-plugin, add-marketplace widen every later start of that kit: asked first, naming what and where
  // (the values as they were when asking are the ones sent); a new skill (written here) is not
  const WIDENS = { "add-skill": (b) => b.path, "add-plugin": (b) => b.plugin, "add-marketplace": (b) => b.marketplace, "add-hook": (b) => b.file };
  const send = (action, body, title) => async (ev) => {
    clear(addStatus);
    const fresh = target.value === "";
    const name = fresh ? targetName.value.trim() : target.value;
    const request = Object.assign(body(), { create: fresh });
    const go = () => busy(ev.target, async () => {
      try { result(title(name), await api("POST", hostPath(`/kits/${encodeURIComponent(name)}/${action}`, host), Object.assign({}, request, WIDENS[action] ? { confirm: true } : {}))); show("kits"); }
      catch (e) { addStatus.append(errorNotice(e)); }
    });
    if (!WIDENS[action] || !needsConfirm("kit-add")) { go(); return; }
    confirmIn(addAsk, t("Add {what} to the kit {kit} on {host}? Every later start with that kit gets it.", { what: WIDENS[action](request) || "?", kit: name, host }), t("Yes, add it"), go, { kind: "primary", trigger: ev.target });
  };
  const folder = el("input", { id: "kit-skill-path", class: "mono", placeholder: t("{path} (on that host, with SKILL.md)", { path: "/home/you/skills/review" }) });
  const folderAs = el("input", { id: "kit-skill-as", placeholder: t("the folder's name") });
  const skillName = el("input", { id: "kit-skill-name", placeholder: "release-notes", pattern: "[a-z0-9][a-z0-9-]{0,63}" });
  const skillDescription = el("input", { id: "kit-skill-description", placeholder: t("What it does and when the agent should use it (one line)") });
  const skillAgents = el("div", { class: "row" }, agentBoxes("kit-skill"));
  const plugin = el("input", { id: "kit-plugin", class: "mono", placeholder: t("{plugin}, or an npm package for OpenCode", { plugin: "sandcastle@sandbox-ai" }) });
  const pluginAgent = el("select", { id: "kit-plugin-agent" }, ["claude", "opencode"].map(a => el("option", { value: a }, a)));
  const pluginSource = el("input", { id: "kit-plugin-source", class: "mono", placeholder: t("OWNER/REPO or https://… (only for a marketplace the kit does not declare yet)") });
  const marketName = el("input", { id: "kit-market-name", class: "mono", placeholder: "my-plugins", pattern: "[A-Za-z0-9][A-Za-z0-9_.-]{0,63}" });
  const marketSource = el("input", { id: "kit-market-source", class: "mono", placeholder: t("OWNER/REPO, a Git URL or a folder on that host") });
  const marketAgent = el("select", { id: "kit-market-agent" }, ["claude", "opencode"].map(a => el("option", { value: a }, a)));
  const hookFile = el("input", { id: "kit-hook-file", class: "mono", placeholder: t("{path} (on that host: a script, hooks.json or a plugin module)", { path: "/home/you/hooks/guard.sh" }) });
  const hookAs = el("input", { id: "kit-hook-as", class: "mono", placeholder: t("the file's name") }), hookEvent = el("input", { id: "kit-hook-event", class: "mono", placeholder: "PreToolUse" });
  const hookMatcher = el("input", { id: "kit-hook-matcher", class: "mono", placeholder: "Bash|Write" }), hookTimeout = el("input", { id: "kit-hook-timeout", type: "number", min: 1, max: 3600 });
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
    el("h3", { class: "wide" }, t("A marketplace")),
    el("label", { for: "kit-market-name" }, t("Marketplace")), marketName, el("label", { for: "kit-market-source" }, t("Source")), marketSource,
    el("label", { for: "kit-market-agent" }, t("Agent")), marketAgent,
    el("span"), el("button", { class: "primary", onclick: send("add-marketplace", () => ({ marketplace: marketName.value.trim(), source: marketSource.value.trim(), agent: marketAgent.value }), n => t("Marketplace added to {kit}", { kit: n })) }, t("Add marketplace")),
    el("p", { class: "hint wide" }, t("A marketplace the kit declares: its plugins can then be added by NAME@MARKETPLACE.")),
    el("h3", { class: "wide" }, t("A hook")),
    el("label", { for: "kit-hook-file" }, t("Hook file")), hookFile, el("label", { for: "kit-hook-as" }, t("Name in the kit")), hookAs,
    el("label", { for: "kit-hook-event" }, t("Event")), hookEvent, el("label", { for: "kit-hook-matcher" }, t("Tools")), hookMatcher,
    el("label", { for: "kit-hook-timeout" }, t("Timeout (seconds)")), hookTimeout,
    el("span"), el("div", { class: "row" }, el("button", { class: "primary", onclick: send("add-hook", () => ({ file: hookFile.value.trim(), as: hookAs.value.trim(), event: hookEvent.value.trim(), matcher: hookMatcher.value.trim(), timeout: hookTimeout.value.trim(), agents: chosenAgents(skillAgents) }), n => t("Hook added to {kit}", { kit: n })) }, t("Add hook")),
      el("button", { class: "danger", onclick: (ev) => {
        const name = target.value, file = hookAs.value.trim() || hookFile.value.trim().split("/").pop();
        if (!name || !file) { toast("warning", t("Which hook?"), t("Choose a kit of yours and name the hook's file.")); return; }
        confirmIn(addAsk, t("Remove the hook {file} from the kit {kit} on {host}?", { file, kit: name, host }), t("Yes, remove it"), async () => {
          try { result(t("{kit}: removed {value}", { kit: name, value: file }), await api("POST", hostPath(`/kits/${encodeURIComponent(name)}/remove-hook`, host), { file, agents: chosenAgents(skillAgents), confirm: true })); show("kits"); }
          catch (e) { addStatus.append(errorNotice(e)); }
        }, { trigger: ev.target });
      } }, t("Remove hook…"))),
    addAsk, addStatus),
    el("p", { class: "hint" }, t("Use a kit with {option} (Options of a new start). Claude plugins are installed with {command} on that host.", { option: "--kit NAME", command: "sandbox-ai plugins claude" }), " ",
      el("button", { onclick: () => show("logins") }, t("Open Logins")))));

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

// One run's canaries (sandbox-ai canaries list RUN --json): its decoy files and what happened; the tokens in full only
// after a question; a sandbox a canary paused resumed after one too.
// SLOT: the row below the event (a table row of its own, hidden until a card is in it), else the end of MAIN.
async function canaryRun(main, run, slot) {
  const host = state.host;
  const old = document.getElementById("canary-run");
  if (old) dropCard(old);
  const body = el("div", {}, el("p", { class: "hint" }, t("Loading…"))), row = el("div", { class: "row" });
  const card = el("div", { class: "card", id: "canary-run" }, el("h2", {}, t("Canaries of {run} on {host}", { run, host })), body, row);
  if (slot) { clear(slot.children[0]).append(card); slot.hidden = false; card.holder = slot; card.keepHolder = true; }
  else main.append(card);
  revealCard(card);
  const tokens = el("button", { onclick: () => confirmIn(row, t("Show the canary tokens of {run} on {host} in full? They are decoys, but whoever sees them here can copy them.", { run, host }), t("Yes, show them"), async () => {
    try { clear(body).append(dataView(await api("POST", hostPath(`/canaries/${encodeURIComponent(run)}/tokens`, host), { confirm: true }))); } catch (e) { clear(body).append(errorNotice(e)); }
  }, { trigger: tokens }) }, t("Show the tokens…"));
  const resume = el("button", { class: "danger", onclick: () => confirmIn(row, t("Let {run} on {host} go on? A canary paused it: something opened a decoy or sent a token away.", { run, host }), t("Yes, resume it"), async () => {
    try { const r = await api("POST", hostPath(`/canaries/${encodeURIComponent(run)}/resume`, host), { confirm: true }); toast("success", t("{name} on {host} resumed", { name: run, host }), (r && r.output) || ""); } catch (e) { failed(t("Could not resume {name} on {host}", { name: run, host }))(e); }
  }, { trigger: resume }) }, t("Resume the sandbox…"));
  row.append(tokens, resume, el("button", { onclick: () => dropCard(card) }, t("Close")));
  try { clear(body).append(dataView(await api("GET", hostPath(`/canaries/${encodeURIComponent(run)}/list`, host)))); } catch (e) { clear(body).append(errorNotice(e)); }
}

pages.canaries = async (main) => {
  main.append(el("h1", {}, t("Canaries on {host}", { host: state.host })), el("p", { class: "hint" }, t("Loading…")));
  let logs;
  const live = pageLive();
  try { logs = await api("GET", hostPath("/canaries")); }
  catch (e) {
    if (!live()) return;
    clear(main).append(el("h1", {}, t("Canaries on {host}", { host: state.host })), e.status === 502 && (!e.code || e.code === "command-failed") ? notice(t("{error} (an older sandbox-ai there has no canaries log: {command})", { error: e.message, command: "sandbox-ai update" }), "error") : errorNotice(e));
    return;
  }
  if (!live()) return;
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
    const toggle = el("button", { "aria-expanded": "false", "aria-label": t("Show links of {run} on {host}", { run: e.run || "?", host: state.host }), onclick: () => { const open = detail.classList.toggle("hidden"); toggle.setAttribute("aria-expanded", String(!open)); toggle.textContent = open ? t("Show links") : t("Hide links"); } }, t("Show links"));
    const slot = el("tr", { class: "card-row", hidden: true }, el("td", { colspan: 7 }));
    rows.push(el("tr", { class: "canary-event" },
      el("td", {}, (e.time || "").replace("T", " ").replace("Z", "")), el("td", { class: "mono" }, e.run || ""),
      el("td", { class: "status-bad" }, `${e.event || "?"}${e.action ? ` (${e.action})` : ""}`),
      el("td", { class: "mono" }, `${e.canary || "-"} ${e.path || ""}`),
      el("td", {}, e.summary || e.error || ""), el("td", { class: "mono" }, canaryProcess(e.process)),
      el("td", { class: "actions" }, toggle, e.run ? rowButton(t("Run…"), e.run, state.host, { onclick: () => canaryRun(main, e.run, slot) }) : null)), detail, slot);
  }
  main.append(table([t("Time"), t("Run"), t("Event"), t("Canary"), t("What"), t("Process"), ""], rows));
};

// What lowers the protection of every later start on a host is asked first, in place, naming the host; Cancel puts
// the control back. Lowering is any step down from what the host has now: the mode (lax < low < medium < high <
// paranoid), the agents' telemetry (on < off < strict), fewer canary categories. The service compares the same way
// (GET /api/policy gives the order) and wants the confirmation as well.
const ORDER = { mode: ["lax", "low", "medium", "high", "paranoid"], telemetry: ["on", "off", "strict"] };

function lowers(key, before, after) {
  const order = (state.policy && state.policy.order && Array.isArray(state.policy.order[key]) && state.policy.order[key]) || ORDER[key];
  const from = order.indexOf(before), to = order.indexOf(after);
  return from < 0 || to < 0 || to < from;
}

pages.settings = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Defaults on {host}", { host })));
  const live = pageLive();
  const data = await api("GET", hostPath("/defaults"));
  if (!live()) return;
  const values = data.values || {};
  const status = el("div");
  // true, or the error (shown, unless it is a refusal that asks for a confirmation: that is asked)
  const save = async (key, value, confirmed) => {
    clear(status);
    try { await api("POST", hostPath("/defaults", host), Object.assign({ key, value }, confirmed ? { confirm: true } : {})); status.append(notice(t("{key} saved on {host}", { key, host }), "ok")); return true; }
    catch (e) { if (confirmed || !confirmable(e)) status.append(errorNotice(e)); return e; }
  };
  // CONTROL changed: saved at once, or asked first (WEAKER) with ASKING in the place next to it; the service may
  // still ask (the host's value changed since the page read it): then that is asked there
  const change = (control, asking, weaker, text, key, value, revert, accept) => {
    if (!weaker || !needsConfirm("defaults-lower")) {
      save(key, value).then(r => {
        if (r === true) { accept(); return; }
        if (!confirmable(r)) { revert(); return; }
        askRefused(asking, r, async () => { if (await save(key, value, true) === true) accept(); else revert(); }, { onCancel: revert, trigger: control });
      });
      return;
    }
    confirmIn(asking, text, t("Yes, lower it"), async () => { if (await save(key, value, true) === true) accept(); else revert(); }, { onCancel: revert, trigger: control });
  };
  let telemetryNow = values.telemetry || "off", modeNow = values.mode || "medium";
  const telemetry = el("select", { id: "telemetry" },
    [["off", N_("off: no usage statistics, error reports, feedback or update checks (recommended)")], ["strict", N_("strict: also no other calls than the model's")], ["on", N_("on: the agents' own settings")]]
      .map(([v, label]) => el("option", { value: v, selected: telemetryNow === v }, t(label))));
  const telemetryAsk = el("div", { class: "row" });
  telemetry.addEventListener("change", () => {
    const value = telemetry.value;
    const text = value === "on" ? t("Turn the agents' telemetry on for every later start on {host}? Their usage statistics, error reports and update checks reach their vendors again.", { host })
      : t("Make the agents' telemetry {level} instead of {now} for every later start on {host}? Fewer of their own calls are held back.", { level: value, now: telemetryNow, host });
    change(telemetry, telemetryAsk, lowers("telemetry", telemetryNow, value), text,
      "telemetry", value === "off" ? null : value, () => { telemetry.value = telemetryNow; }, () => { telemetryNow = value; });
  });
  const mode = el("select", { id: "mode" }, ORDER.mode.map(m => el("option", { value: m, selected: modeNow === m }, m)));
  const modeAsk = el("div", { class: "row" });
  mode.addEventListener("change", () => {
    const value = mode.value;
    change(mode, modeAsk, lowers("mode", modeNow, value), t("Make {mode} instead of {now} the security mode of every later start on {host}? It allows more (see {command} there); an option given at a start still wins.", { mode: value, now: modeNow, host, command: "sandbox-ai modes" }),
      "mode", value === "medium" ? null : value, () => { mode.value = modeNow; }, () => { modeNow = value; });
  });
  const categories = data.canary_categories || ["credentials", "home", "workspace"];
  // as defaults set canaries takes it: false, a list, "a,b", "all" or "none" ([] and ["none"]: no canary files)
  const named = (value) => {
    const list = typeof value === "string" ? value.split(",").map(v => v.trim()).filter(Boolean) : value;
    if (list === false || (Array.isArray(list) && (!list.length || (list.length === 1 && list[0] === "none")))) return [];
    return Array.isArray(list) && list.length === 1 && list[0] === "all" ? categories.slice() : list;
  };
  let canariesNow = named(values.canaries === undefined ? (data.canary_default || []) : values.canaries);
  if (!Array.isArray(canariesNow)) canariesNow = categories.slice();
  const boxes = categories.map(c => el("input", { type: "checkbox", id: `canary-${c}`, value: c, checked: canariesNow.includes(c) }));
  const canaryAsk = el("div", { class: "row" });
  for (const box of boxes) box.addEventListener("change", () => {
    const picked = boxes.filter(b => b.checked).map(b => b.value);
    const fewer = canariesNow.some(c => !picked.includes(c));
    const revert = () => { for (const b of boxes) b.checked = canariesNow.includes(b.value); };
    change(box, canaryAsk, fewer || !picked.length, picked.length ? t("Put no {categories} canary files into later starts on {host}? What opens them there is no longer noticed.", { categories: canariesNow.filter(c => !picked.includes(c)).join(t(" and ")), host })
      : t("Put no canary files at all into later starts on {host}? A decoy opened or a token sent away is no longer noticed.", { host }),
    "canaries", picked.length ? picked : false, revert, () => { canariesNow = picked; });
  });
  main.append(status, el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "telemetry" }, t("Telemetry of the agents")), telemetry, el("span"), telemetryAsk,
    el("label", { for: "mode" }, t("Security mode")), mode, el("span"), modeAsk,
    el("span", {}, t("Canary files")), el("div", { class: "row" }, boxes.map((b, i) => el("label", {}, b, " ", categories[i]))), el("span"), canaryAsk),
    el("p", { class: "hint mono" }, data.path || ""),
    el("p", { class: "hint" }, t("For every start on this host; an option given at a start wins."))));
  // keep_days, preset_dirs, subnet, subnet6: saved with Save; what lowers the protection the service asks first
  const more = [["keep_days", N_("Kept starts unused this many days are removed (empty: never)"), "30"], ["preset_dirs", N_("Extra preset directories (one per line)"), "/home/you/presets"],
    ["subnet", N_("Network of the starts (IPv4)"), "10.89.0.0/24"], ["subnet6", N_("Network of the starts (IPv6)"), "fd00:89::/64"]];
  const asking = el("div", { class: "row" });
  const fields = more.map(([key, label, example]) => {
    const now = values[key];
    const input = key === "preset_dirs" ? el("textarea", { id: `default-${key}`, rows: 2, class: "mono", placeholder: example }) : el("input", { id: `default-${key}`, class: "mono", placeholder: example });
    input.value = now === undefined || now === null ? "" : Array.isArray(now) ? now.join("\n") : String(now);
    const valueOf = () => {
      const text = input.value.trim();
      if (!text) return null;
      if (key === "keep_days") return Number(text);
      return key === "preset_dirs" ? linesOf(text) : text;
    };
    const saveButton = el("button", { onclick: async (event) => {
      const value = valueOf();
      const r = await save(key, value);
      if (r === true || !confirmable(r)) return;
      askRefused(asking, r, async () => { await save(key, value, true); }, { trigger: event.target });
    } }, t("Save"));
    return [el("label", { for: input.id }, t(label)), el("div", { class: "row" }, input, saveButton)];
  });
  main.append(el("h2", {}, t("More defaults")), el("div", { class: "card" }, el("div", { class: "form" }, fields, el("span"), asking)));
};

// The secrets of a host (sandbox-ai secrets there): names and where each is kept, never a value. A value is typed
// into a field shown while it is typed (secretInput: "Show value" hides it), sent once and cleared; the service hands
// it to `secrets set` on its standard input. Add stores a new name; a name that exists is asked about first.
pages.secrets = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Secrets on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  const list = await api("GET", hostPath("/secrets", host));
  if (!live()) return;
  const names = (Array.isArray(list) ? list : []).filter(x => x && typeof x.name === "string");
  clear(main).append(el("h1", {}, t("Secrets on {host}", { host })),
    el("p", { class: "hint" }, t("API keys and tokens in that host's keyring, for {options}: their names, never their values.", { options: "--secret-env, --git-token-secret, VPN, LLM" })));
  const rows = names.map(x => {
    const actions = el("td", { class: "actions" });
    const remove = rowButton(t("Delete"), x.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Delete the secret {name} on {host}? Starts, VPN profiles and Git logins that name it no longer get it.", { name: x.name, host }), t("Yes, delete it"), async () => {
      try { await api("DELETE", hostPath(`/secrets/${encodeURIComponent(x.name)}`, host), { confirm: true }); toast("success", t("{name} deleted on {host}", { name: x.name, host }), ""); show("secrets"); }
      catch (e) { failed(t("Could not delete {name} on {host}", { name: x.name, host }))(e); }
    }, { trigger: remove }) });
    actions.append(remove);
    return el("tr", {}, el("td", { class: "mono" }, x.name), el("td", {}, x.store || ""), actions);
  });
  main.append(table([t("Name"), t("Kept in"), ""], rows, t("No secrets on this host."), t("Secrets on {host}", { host })));
  const name = el("input", { id: "secret-name", class: "mono", placeholder: "GITEA_TOKEN", pattern: "[A-Za-z_][A-Za-z0-9_]{0,63}", autocomplete: "off" });
  const [value, valueField] = secretInput({ id: "secret-value", class: "mono", "aria-describedby": "secret-hint" });
  const status = el("div", { class: "wide" });
  const actions = el("div", { class: "row" });
  // WANTED: the name as it was when the user pressed Add (and was asked, if asked): what is sent is what was named
  const send = async (wanted, confirm) => {
    clear(status);
    try {
      const r = await api("POST", hostPath(`/secrets/${encodeURIComponent(wanted)}`, host), Object.assign({ secret: value.value }, confirm ? { confirm: true } : {}));
      value.value = "";
      toast("success", t("{name} stored on {host}", { name: wanted, host }), (r && r.output) || "");
      show("secrets");
    } catch (e) {
      // a secret of that name came meanwhile (another tab, the command line): asked now, the value kept for the Yes
      if (!confirm && confirmable(e)) { askReplace(wanted, t("A secret {name} exists on {host} now: replace its value? The old one is gone.", { name: wanted, host })); return; }
      value.value = ""; status.append(errorNotice(e));
    }
  };
  const askReplace = (wanted, text) => confirmIn(actions, text, t("Yes, replace it"), () => {
    if (name.value.trim() !== wanted) {  // the name was changed while the question was open: not what was asked
      status.append(notice(t("The name is {now} now, not {asked} as asked: nothing was sent. Press Add again.", { now: name.value.trim(), asked: wanted }), "warn"));
      return undefined;
    }
    return send(wanted, true);
  }, { trigger: keep });
  const keep = el("button", { class: "primary", onclick: () => {
    clear(status);
    const wanted = name.value.trim();
    if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(wanted)) { toast("warning", t("Which name?"), t("Letters, digits and _ (up to 64), not starting with a digit.")); return; }
    if (!value.value) { toast("warning", t("Which value?"), t("Type the value into the field Value.")); return; }
    if (names.some(x => x.name === wanted)) { askReplace(wanted, t("Replace the value of {name} on {host}? The old one is gone.", { name: wanted, host })); return; }
    send(wanted, false);
  } }, t("Add"));
  actions.append(keep);
  main.append(el("h2", {}, t("Set a secret")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "secret-name" }, t("Name")), name, el("label", { for: "secret-value" }, t("Value")), valueField, status,
    el("span"), actions),
  el("p", { class: "hint", id: "secret-hint" }, t("Shown here only while you type it (Show value hides it). Stored on that host for good, until you delete it: sent to its sandbox-ai on its standard input, never logged or sent back; a value already stored is never shown."))));
};

// The version of a host's sandbox-ai and whether an update is there (hosts list's status; null: not asked yet).
function versionText(s) {
  if (!s) return "…";
  if (!s.version) return s.error ? (sandboxMissing(s) ? t("not installed") : t("unreachable")) : "?";
  if (["checkout", "pip", "package"].includes(s.kind)) return `${s.version} (${s.kind})`;
  if (s.update) return t("{version} → {available} available", { version: s.version, available: s.available });
  return s.update === false ? t("{version}, up to date", { version: s.version }) : s.version;
}

// Whether a host answers over SSH but has no sandbox-ai (the shell found none: exit status 127, "not found").
function sandboxMissing(s) {
  const error = String((s && s.error) || "");
  return Boolean(s && !s.version && error && !/\(SSH:/.test(error) && /No such file or directory|not found|exit status 127/.test(error));
}

// What a key made private (ssh refuses key files others can read) and what was only named.
function keyNotices(r) {
  return [...(r.fixed || []).map(f => notice(t("{path} could be read by others (mode {was}), which ssh refuses: made private ({now}).", { path: f.path, was: f.was, now: f.now }), "ok")),

    ...(r.warnings || []).map(w => notice(w, "warn"))];
}

// The sudo of an update as a select says it: undefined (the host's setting), true or false.
function sudoChoice(select) {
  const value = select ? select.value : "";
  return value === "sudo" ? true : value === "none" ? false : undefined;
}

// One host's update: its own signed sandbox-ai update; a sudo that wants a password is asked in a terminal tab.
async function updateHost(name, output, sudo, version) {
  const body = Object.assign(sudo === undefined ? {} : { sudo }, version ? { version } : {});
  const r = await api("POST", `/api/hosts/${encodeURIComponent(name)}/update`, body);
  output.append(...keyNotices(r));
  if (r.changed) output.append(notice(`${name}: ${r.before || "?"} → ${r.after}${r.sudo ? " (sudo)" : ""}`, "ok"));
  else if (r.error && !r.after) {
    say(t("{host}: not updated: {error}", { host: name, error: r.error }));
    output.append(el("div", { class: "notice error" }, t("{host}: not updated: {error}", { host: name, error: r.error }),
      r.needs_password && terminalsHere() ? el("button", { onclick: () => openTerminal({ host: name, kind: "update" }, t("update {host}", { host: name })) }, t("Update in a terminal (sudo asks there)")) : null,
      r.needs_sudo ? el("button", { onclick: (ev) => busy(ev.target, () => updateHost(name, output, true).catch(failed(t("Could not update {host}", { host: name })))) }, t("Update with sudo")) : null));
    if (r.needs_password) output.append(passwordHelp(name, ["sandbox-ai", "hosts", "update", name, "--sudo"]));
  } else output.append(notice(`${name}: ${t("{version}, up to date", { version: r.after || r.before })}${r.error ? ` (${r.error})` : ""}`, r.error ? "warn" : "ok"));
  for (const note of r.notes || []) output.append(notice(`${name}: ${note}`, "warn"));
  return r;
}

// Whether this page has terminals (xterm.js on the server): a sudo that wants a password is asked in one.
function terminalsHere() { return Boolean(state.me && state.me.terminals) && typeof window !== "undefined" && Boolean(window.Terminal); }

// sudo on NAME wants a password, which this page cannot type: what to do, with the exact command (WORDS) to copy.
function passwordHelp(name, words) {
  return el("div", { class: "notice warn password-help" },
    el("p", {}, t("sudo on {host} wants a password, and this page never asks for one.", { host: name }), " ",
      terminalsHere() ? t("Run it in a terminal tab (the button above), where sudo asks, or on this machine:") : t("Run this in a terminal on this machine, where sudo asks for it:")),
    commandBlock(words.join(" ")),
    el("p", { class: "hint" }, t("Or allow it there without a password (sudoers NOPASSWD).")));
}

// sandbox-ai on a host, from this machine (`sandbox-ai hosts install`): for the SSH user or for every user, as root
// or with sudo (sudo -n: a password is asked in a terminal), from this machine's copy or the host downloads the
// signed release. It can take minutes (up to 15 for a step): the page stays usable, a toast tells the end.
// What the form says, as the service takes it ({mode, prefix, sudo, source, version}).
function installBody(form, root) {
  const mode = form.mode.value, source = form.source.value;
  const body = { mode, source };
  if (mode === "system") {
    body.prefix = form.prefix.value.trim() || "/usr/local";
    if (!root) body.sudo = form.sudo.value === "sudo";
  }
  if (source === "download" && form.version.value.trim()) body.version = form.version.value.trim();
  return body;
}

// The words of an install BODY on host H: for whom, how, from where (for its question and its result).
function installWords(body, root) {
  const who = body.mode === "system" ? t("for every user in {prefix}", { prefix: body.prefix || "/usr/local" }) : t("for the SSH user");
  const how = body.mode !== "system" ? t("without sudo") : root ? t("as root (the SSH user is root)") : body.sudo === false ? t("without sudo") : t("with sudo");
  const from = body.source === "download" ? (body.version ? t("the host downloads the signed release {version}", { version: body.version }) : t("the host downloads the newest signed release")) : t("from this machine's copy");
  return { who, how, from };
}

// The command line of an install BODY on NAME, as typed in a terminal of this machine.
function installCommand(name, body) {
  const words = ["sandbox-ai", "hosts", "install", name, body.mode === "system" ? "--system" : "--user"];
  if (body.mode === "system" && body.prefix && body.prefix !== "/usr/local") words.push(`--prefix=${body.prefix}`);
  if (body.mode === "system" && body.sudo === false) words.push("--no-sudo");
  if (body.source === "download") words.push("--download");
  if (body.version) words.push(`--version=${body.version}`);
  return words;
}

function installCard(main, h, onDone) {
  const old = document.getElementById("host-install-card");
  if (old) old.remove();
  const root = String(h.ssh || "").startsWith("root@");
  const user = String(h.ssh || "").split("@")[0] || "?";
  const form = {
    mode: el("select", { id: "install-mode" }, el("option", { value: "user", selected: !root }, t("for the SSH user ({user}): in its ~/.local", { user })),
      el("option", { value: "system", selected: root }, t("for every user: in a prefix (root's)"))),
    sudo: el("select", { id: "install-sudo", disabled: root }, root ? el("option", { value: "root" }, t("as root: the SSH user is root"))
      : [el("option", { value: "sudo", selected: true }, t("with sudo (sudo -n; a password is asked in a terminal)")), el("option", { value: "none" }, t("without sudo"))]),
    prefix: el("input", { id: "install-prefix", class: "mono", value: "/usr/local", pattern: "/[A-Za-z0-9._/-]{0,200}" }),
    source: el("select", { id: "install-source" }, el("option", { value: "here", selected: true }, t("this machine's copy (version {version})", { version: (state.me && state.me.version) || "?" })),
      el("option", { value: "download" }, t("the host downloads the signed release"))),
    version: el("input", { id: "install-version", class: "mono", placeholder: t("newest"), size: 10, pattern: "[0-9]+\\.[0-9]+\\.[0-9]+" }),
  };
  const howNote = el("span", { class: "hint", id: "install-how-note" });
  const rows = { sudo: [el("label", { for: "install-sudo" }, t("How")), el("div", {}, form.sudo, " ", howNote)],
    prefix: [el("label", { for: "install-prefix" }, t("Prefix")), form.prefix], version: [el("label", { for: "install-version" }, t("Version")), form.version] };
  const sync = () => {
    const system = form.mode.value === "system";
    for (const node of rows.prefix) node.hidden = !system;
    for (const node of rows.version) node.hidden = form.source.value !== "download";
    form.sudo.disabled = root || !system;
    howNote.textContent = system ? "" : t("for the SSH user: no sudo, nothing outside its HOME");
  };
  form.mode.addEventListener("change", sync); form.source.addEventListener("change", sync);
  const status = el("div", { class: "wide", id: "install-status" });
  const buttons = el("div", { class: "row" });
  const install = el("button", { class: "primary", disabled: Boolean(state.installing[h.name]), onclick: () => {
    clear(status);
    const body = installBody(form, root);
    if (body.prefix && (!/^\/[A-Za-z0-9._/-]{0,200}$/.test(body.prefix) || body.prefix.split("/").some(part => part === "." || part === ".."))) { status.append(notice(t("The prefix is an absolute path of letters, digits, . _ - and / (no . or .. parts)."), "error")); return; }
    if (body.version && !/^[0-9]+\.[0-9]+\.[0-9]+$/.test(body.version)) { status.append(notice(t("The version is X.Y.Z (empty: the newest)."), "error")); return; }
    const words = installWords(body, root);
    // the SSH target the question names is the one the service installs to (it refuses one changed meanwhile)
    const ssh = `${h.ssh}${h.port && h.port !== 22 ? ":" + h.port : ""}`;
    confirmIn(buttons, t("Install sandbox-ai on {host} ({ssh}) {who}, {how}; {from}?", { host: h.name, ssh, who: words.who, how: words.how, from: words.from }), t("Yes, install it"),
      () => runInstall(h, Object.assign({}, body, { expected: { ssh: h.ssh, port: h.port || 22 } }), status, install, onDone), { kind: "primary", trigger: install });
  } }, state.installing[h.name] ? t("Installing…") : t("Install"));
  buttons.append(install, el("button", { onclick: () => card.remove() }, t("Close")));
  const card = el("div", { class: "card", id: "host-install-card" }, el("h2", {}, t("Install sandbox-ai on {host}", { host: h.name })),
    el("p", { class: "hint" }, root ? t("The SSH user of {host} is root: an installation for every user needs no sudo.", { host: h.name })
      : t("An installation for every user on {host} needs sudo there, unless its SSH user is root (an ssh_config User root too): the install says which it was.", { host: h.name })),
    el("div", { class: "form" },
      el("label", { for: "install-mode" }, t("For whom")), form.mode, ...rows.sudo, ...rows.prefix,
      el("label", { for: "install-source" }, t("From")), form.source, ...rows.version, status,
      el("span"), buttons),
    el("p", { class: "hint" }, t("The host needs sshd, bash, tar, gzip and python3. Nothing is run there but install.sh, in a private temporary folder that is removed afterwards.")));
  sync();
  main.append(card);
  focusFirst(card);
  // one that runs already (from another page or tab), or how the last one here ended
  if (!state.installing[h.name]) api("GET", `/api/hosts/${encodeURIComponent(h.name)}/install`, undefined, { background: true }).then((found) => {
    if (!card.isConnected || !found || typeof found !== "object") return;
    if (found.running) {
      install.disabled = true; install.textContent = t("Installing…");
      status.append(notice(t("sandbox-ai is being installed on {host} (for {seconds} s, from another page): wait for its end.", { host: h.name, seconds: found.seconds ?? "?" }), "warn"));
    } else if (found.last && found.last.result && typeof found.last.result === "object") {
      status.append(el("p", { class: "hint" }, t("The last install here, {minutes} min ago:", { minutes: Math.round((Number(found.last.ago) || 0) / 60) })),
        ...installResult(h.name, found.last.choice || {}, found.last.result, root));
    }
  }).catch(() => { /* an older service: nothing to say */ });
  return card;
}

// The install of BODY on host H, confirmed: busy until it ends (the page stays usable), then its result in STATUS
// and a toast (STATUS may be gone by then: another page).
async function runInstall(h, body, status, button, onDone) {
  const name = h.name, root = String(h.ssh || "").startsWith("root@");
  const started = Date.now();
  state.installing[name] = started;
  if (button) { button.disabled = true; button.textContent = t("Installing…"); }
  say(t("Installing on {host}", { host: name }));
  clear(status).append(el("div", { class: "notice" }, t("Installing sandbox-ai on {host}… This can take several minutes (up to 15 for a step); you can use the page meanwhile.", { host: name }), " ",
    el("progress", { "aria-label": t("Installing on {host}", { host: name }) })));
  let r = null;
  try { r = await api("POST", `/api/hosts/${encodeURIComponent(name)}/install`, Object.assign({}, body, { confirm: true })); }
  catch (e) {
    // the answer was cut off (a browser gives up on one after some minutes; the network): the install goes on
    // there, and the service keeps how it ended
    if (!(e instanceof ApiError)) r = await waitForInstall(name, status, e, started);
    else {
      clear(status).append(notice(t("{host}: not installed: {error}", { host: name, error: e.message }), "error"));
      toast("error", t("Could not install on {host}", { host: name }), e.message);
    }
  } finally {
    delete state.installing[name];
    if (button) { button.disabled = false; button.textContent = t("Install"); }
  }
  if (!r) return null;
  clear(status).append(...installResult(name, body, r, root));
  if (r.error) toast("error", t("Could not install on {host}", { host: name }), r.error);
  else toast("success", t("sandbox-ai installed on {host}", { host: name }), `${r.before || t("none")} → ${r.after || "?"}`);
  if (onDone) onDone(r);
  return r;
}

// The install on NAME whose answer did not come (WHY): asked of the service until it ended there (GET …/install
// every 10 s, for at most 40 minutes), its result then, or null. Only an install that ended after STARTED (when this
// one was sent) is this one's: an older one's result is never shown as it.
async function waitForInstall(name, status, why, started) {
  clear(status).append(el("div", { class: "notice warn" }, t("The answer of the install on {host} did not come ({error}); it may still run there: asking the service how it goes…", { host: name, error: why && why.message ? why.message : String(why) })));
  for (let tries = 0; tries < 240; tries++) {
    await new Promise(resolve => setTimeout(resolve, 10000));
    let found = null;
    try { found = await api("GET", `/api/hosts/${encodeURIComponent(name)}/install`, undefined, { background: true }); } catch (e) { continue; }
    if (found && found.running) continue;
    const last = found && found.last;
    const ours = last && (started === undefined || Date.now() - Number(last.ago) * 1000 >= started - 2000);
    if (ours && last.result && typeof last.result === "object") return last.result;
    const error = ours && last.error ? last.error : t("the service knows of no install there (it was restarted?)");
    clear(status).append(notice(t("{host}: not installed: {error}", { host: name, error }), "error"));
    toast("error", t("Could not install on {host}", { host: name }), error);
    return null;
  }
  clear(status).append(notice(t("No end of the install on {host} in 40 minutes: check its version (Check versions).", { host: name }), "warn"));
  return null;
}

// What an install answered: before → after, how, its notes, what to do when sudo wants a password, its output.
function installResult(name, body, r, root) {
  const nodes = [];
  if (r.error) nodes.push(notice(t("{host}: not installed: {error}", { host: name, error: r.error }), "error"));
  else {
    const words = installWords({ mode: r.mode || body.mode, prefix: r.prefix || body.prefix, sudo: r.sudo, source: r.source || body.source, version: body.version }, Boolean(r.root) || root);
    nodes.push(notice(t("{host}: sandbox-ai {before} → {after}, installed {who}, {how}.", { host: name, before: r.before || t("none"), after: r.after || "?", who: words.who, how: r.root ? t("as root") : r.sudo ? t("with sudo") : t("without sudo") }), "ok"));
  }
  if (r.needs_password) {
    const sent = Object.assign({}, body, { mode: "system" });
    if (terminalsHere()) nodes.push(el("div", { class: "row" }, el("button", { class: "primary", onclick: () => openTerminal(Object.assign({ host: name, kind: "install" }, installQuery(sent)), t("install on {host}", { host: name })) }, t("Install in a terminal (sudo asks there)"))));
    nodes.push(passwordHelp(name, installCommand(name, sent)));
  }
  for (const note of r.notes || []) nodes.push(notice(`${name}: ${note}`, "warn"));
  if (r.output) nodes.push(el("details", { class: "install-output" }, el("summary", {}, t("What install.sh said")), el("pre", {}, String(r.output))));
  return nodes;
}

// An install BODY as the terminal's query (strings: sudo 1 or 0).
function installQuery(body) {
  const query = { mode: body.mode, source: body.source || "here" };
  if (body.prefix) query.prefix = body.prefix;
  if (body.version) query.version = body.version;
  if (body.sudo === true || body.sudo === false) query.sudo = body.sudo ? "1" : "0";
  query.confirm = "1";  // asked in the page already (the install's own confirmation)
  return query;
}

// A host's settings changed (hosts.json on this service's machine): a field left empty is left out.
function hostEditCard(main, h) {
  const old = document.getElementById("host-edit-card");
  if (old) old.remove();
  const fields = { ssh: el("input", { id: "edit-ssh", class: "mono", value: h.ssh || "" }), port: el("input", { id: "edit-port", type: "number", min: 1, max: 65535, value: h.port || 22 }),
    identity: el("input", { id: "edit-identity", class: "mono", value: h.identity || "", placeholder: t("{path} (optional)", { path: "~/.ssh/id_ed25519" }) }),
    command: el("input", { id: "edit-command", class: "mono", value: h.command || "", placeholder: t("sandbox-ai's path there (optional)") }),
    description: el("input", { id: "edit-description", value: h.description || "" }), update_sudo: el("input", { id: "edit-update-sudo", type: "checkbox", checked: Boolean(h.update_sudo) }) };
  const status = el("div", { class: "wide" });
  const buttons = el("div", { class: "row" });
  const saveHost = async (body, confirm) => {
    clear(status);
    try {
      const r = await api("POST", `/api/hosts/${encodeURIComponent(h.name)}`, Object.assign({}, body, confirm ? { confirm: true } : {}));
      toast("success", t("{host} saved", { host: h.name }), "");
      for (const w of (r && r.warnings) || []) toast("warning", t("Keys of {host}", { host: h.name }), w);
      render();
    } catch (e) {
      if (!confirm && confirmable(e)) { askRefused(buttons, e, () => saveHost(body, true), { trigger: save }); return; }
      status.append(errorNotice(e));
    }
  };
  // where and how this service's commands run there: a change is asked first, naming it (what was typed then is sent)
  const save = el("button", { class: "primary", onclick: () => {
    const body = { ssh: fields.ssh.value.trim(), port: Number(fields.port.value || 22), identity: fields.identity.value.trim(), command: fields.command.value.trim(),
      description: fields.description.value.trim(), update_sudo: Boolean(fields.update_sudo.checked) };
    const was = { ssh: h.ssh || "", port: h.port || 22, identity: h.identity || "", command: h.command || "", update_sudo: Boolean(h.update_sudo) };
    const moved = ["ssh", "port", "identity", "command", "update_sudo"].filter(key => body[key] !== was[key]);
    if (!moved.length || !needsConfirm("host-edit")) { busy(save, () => saveHost(body, false)); return; }
    const words = moved.map(key => `${key}: ${String(was[key]) || "-"} → ${String(body[key]) || "-"}`).join("; ");
    confirmIn(buttons, t("Change how this service reaches {host} ({changes})? Its commands go there from now on.", { host: h.name, changes: words }), t("Yes, change it"), () => saveHost(body, true), { kind: "primary", trigger: save });
  } }, t("Save"));
  buttons.append(save, el("button", { onclick: () => card.remove() }, t("Cancel")));
  const card = el("div", { class: "card", id: "host-edit-card" }, el("h2", {}, t("Edit {host}", { host: h.name })), el("div", { class: "form" },
    el("label", { for: "edit-ssh" }, "SSH"), fields.ssh, el("label", { for: "edit-port" }, t("Port")), fields.port,
    el("label", { for: "edit-identity" }, t("Key")), fields.identity, el("label", { for: "edit-command" }, t("Command")), fields.command,
    el("label", { for: "edit-description" }, t("Description")), fields.description,
    el("label", { for: "edit-update-sudo" }, t("Update with sudo")), fields.update_sudo, status,
    el("span"), buttons));
  main.append(card);
  focusFirst(card);
}

pages.hosts = async (main) => {
  main.append(el("h1", {}, t("Hosts")), el("p", { class: "hint" }, t("Machines (or LXC containers) whose sandbox-ai this service manages over SSH, with your keys; nothing listens on them but sshd.")));
  const output = el("div");
  const versions = {};
  const versionCells = {};
  const byName = Object.fromEntries(state.hosts.map(h => [h.name, h]));
  // a host whose sandbox-ai is missing or does not answer: its version cell says so and offers the install
  const showVersions = (found) => {
    Object.assign(versions, found);
    for (const [name, cell] of Object.entries(versionCells)) {
      const v = versions[name];
      clear(cell).append(versionText(v));
      if (v && !v.version && v.error) {
        cell.setAttribute("title", String(v.error));
        cell.append(" ", el("button", { class: "small primary", "aria-label": t("Install sandbox-ai on {host}", { host: name }), disabled: Boolean(state.installing[name]),
          onclick: () => installCard(main, byName[name], afterInstall) }, t("Install…")));
      } else cell.removeAttribute("title");
    }
  };
  const afterInstall = () => { if (state.page === "hosts") loadVersions(true); };
  const loadVersions = async (refresh) => {
    try { showVersions(await api(refresh ? "POST" : "GET", "/api/hosts-status", refresh ? {} : undefined)); }
    catch (e) { for (const cell of Object.values(versionCells)) cell.textContent = "?"; output.append(notice(t("Versions: {error}", { error: e.message }), "error")); }
  };
  const remote = state.hosts.filter(h => h.kind !== "local");
  // the starts and the open questions of each host, as the fleet's last listings found them (nothing is asked for it)
  const activityCells = {};
  const loadActivity = async () => {
    let found;
    try { found = await api("GET", "/api/hosts-activity", undefined, { background: true }); } catch (e) { found = {}; }
    for (const [name, [starts, questions]] of Object.entries(activityCells)) {
      const a = found && typeof found[name] === "object" && found[name] ? found[name] : {};
      starts.textContent = typeof a.starts === "number" ? t("{count} ({running} running)", { count: a.starts, running: a.running ?? 0 }) : "?";
      questions.textContent = typeof a.questions === "number" ? String(a.questions) : "?";
      questions.className = typeof a.questions === "number" && a.questions > 0 ? "status-warn" : "";
      const title = typeof a.age === "number" ? t("as seen {seconds} s ago", { seconds: Math.round(a.age) }) : t("not asked yet: open Starts and sessions (all hosts) or Approvals");
      starts.setAttribute("title", title); questions.setAttribute("title", title);
    }
  };
  // how each host's update runs: as its setting says (update_sudo), with sudo, or without
  const updateSudo = Object.fromEntries(remote.map(h => [h.name, el("select", { class: "small", "aria-label": t("sudo for the update of {host}", { host: h.name }) },
    el("option", { value: "" }, h.update_sudo ? t("as set: with sudo") : t("as set: without sudo")),
    el("option", { value: "sudo" }, t("with sudo")), el("option", { value: "none" }, t("without sudo")))]));
  // a release other than the newest (hosts update --version X.Y.Z)
  const updateVersion = Object.fromEntries(remote.map(h => [h.name, el("input", { class: "small mono", size: 8, placeholder: t("newest"), pattern: "[0-9]+\\.[0-9]+\\.[0-9]+", "aria-label": t("version for the update of {host}", { host: h.name }) })]));
  const rows = state.hosts.map(h => {
    const version = el("td", { class: "mono" }, h.kind === "local" ? t("this machine") : "…");
    if (h.kind !== "local") versionCells[h.name] = version;
    const startsCell = el("td", {}, "…"), questionsCell = el("td", {}, "…");
    activityCells[h.name] = [startsCell, questionsCell];
    return el("tr", {}, el("td", { class: "mono" }, h.name), el("td", { class: "mono" }, h.kind === "local" ? t("this machine") : `${h.ssh}${h.port && h.port !== 22 ? ":" + h.port : ""}`),
      version, startsCell, questionsCell, el("td", {}, h.description || "", h.update_sudo ? el("span", { class: "hint" }, " ", t("(updates with sudo)")) : null), el("td", { class: "actions" },
        el("button", { onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try {
            const r = await api("POST", `/api/hosts/${encodeURIComponent(h.name)}/test`);
            output.append(...keyNotices(r));
            output.append(notice(r.error ? `${h.name}: ${r.error}` : `${h.name}: ${r.version}${(r.fixed || []).length ? t(" (tried again after making the key private)") : ""}`, r.error ? "error" : "ok"));
            if (r.doctor) output.append(el("h2", {}, t("Doctor on {host}", { host: h.name })), doctorView(r.doctor, { level: 3, host: h.name }));
          } catch (e) { output.append(errorNotice(e)); }
        }) }, t("Test")),
        el("button", { title: t("version --verbose: where it is installed, its release keys"), onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          try { output.append(el("h2", {}, t("sandbox-ai on {host}", { host: h.name })), outputBlock(await api("GET", hostPath("/version", h.name)))); } catch (e) { output.append(errorNotice(e)); }
        }) }, t("Version")),
        h.kind === "local" ? null : updateSudo[h.name],
        h.kind === "local" ? null : updateVersion[h.name],
        h.kind === "local" ? null : el("button", { "aria-label": t("Update {host}", { host: h.name }), onclick: (ev) => busy(ev.target, async () => {
          clear(output);
          output.append(notice(t("Updating {host}…", { host: h.name })));
          try { await updateHost(h.name, output, sudoChoice(updateSudo[h.name]), updateVersion[h.name].value.trim()); } catch (e) { output.append(notice(`${h.name}: ${e.message}`, "error")); }
          loadVersions(false);
        }) }, t("Update")),
        h.kind === "local" ? null : el("button", { "aria-label": t("Install sandbox-ai on {host}", { host: h.name }), onclick: () => installCard(main, h, afterInstall) }, t("Install…")),
        h.kind === "local" ? null : el("button", { onclick: () => hostEditCard(main, h) }, t("Edit…")),
        h.kind === "local" ? null : el("button", { class: "danger", onclick: (ev) => confirmIn(ev.target.parentNode, t("Remove {host} ({ssh}) from the list? Nothing changes on it; this service no longer manages it.", { host: h.name, ssh: h.ssh }), t("Yes, remove it"), async () => {
          try { await api("DELETE", `/api/hosts/${encodeURIComponent(h.name)}`, { confirm: true }); render(); } catch (e) { failed(t("Could not remove {value}", { value: h.name }))(e); }
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
  main.append(table([t("Name"), "SSH", "sandbox-ai", t("Starts"), t("Open questions"), t("Description"), ""], rows, null, t("Hosts")),
    el("div", { class: "actions" }, updateAll, check, progress),
    el("p", { class: "hint" }, t("Update runs each host's own sandbox-ai update (signed, as install.sh installed it); a system installation updates with {sudo}, and when sudo wants a password a terminal tab asks for it. This machine updates with: {command}.", { sudo: "sudo -n", command: "sandbox-ai update" })),
    el("p", { class: "hint" }, t("Install… puts sandbox-ai on a host that has none (or another version): this machine's copy, or the host downloads the signed release; for the SSH user, or for every user as root or with sudo.")),
    output);
  if (remote.length) loadVersions(false);
  loadActivity();
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
      } catch (e) { status.append(errorNotice(e)); }
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
  if (starts instanceof Error) { delete box.dataset.shown; clear(list).append(errorNotice(starts)); return; }
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
      ? el("td", { class: "actions" }, rowButton(open ? t("Show") : t("Open terminal"), s.name, host, { class: open ? "" : "primary", onclick: () => openTerminal({ kind: "attach", name: s.name, host }, s.name) }))
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

// A start refused because its project configuration is not trusted (or changed since): the launcher says
// "Project configuration PATH is not trusted" (agent_sandbox.load_project_config). The path, or null.
function untrustedConfig(text) {
  const plain = String(text).replace(/\u001b\[[0-9;?]*[A-Za-z]/g, "").replace(/\r/g, "");
  const match = /Project configuration (\/[^\n]*?) is (?:not trusted|changed since it was trusted)/.exec(plain);
  return match ? match[1] : null;
}

function offerReview(host, path) {
  toast("warning", t("The project configuration is not trusted"), t("{path} on {host}: review it, and trust exactly what you saw.", { path, host }),
    { action: [t("Review it"), () => { state.review = { host, path }; state.host = host; show("projects"); }] });
}

function openTerminal(params, title) {
  if (!window.Terminal) { toast("warning", t("No terminals here"), t("Terminals need xterm.js on the server: {command}", { command: "sandbox-ai web vendor" })); return; }
  // One tab per start: attaching again shows the tab already open. PARAMS.host: the start's own (else the chosen).
  const host = params.host || state.host;
  const key = params.kind === "attach" ? terminalKey(host, params.name) : null;
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
  const query = new URLSearchParams(Object.assign({ csrf: state.me.csrf, cols: term.cols, rows: term.rows }, params, { host }));
  for (const [key, value] of [...query.entries()]) if (value === "") query.delete(key);
  const ws = new WebSocket(`wss://${location.host}/api/terminal?${query}`);
  ws.binaryType = "arraybuffer";
  const encoder = new TextEncoder();
  const entry = { id, title: `${host}: ${title}`, key, closed: false, opened: false, dismissed: false, ended: "", term, ws, box, fit: () => { if (fit) { fit.fit(); if (ws.readyState === 1) ws.send(JSON.stringify({ resize: [term.cols, term.rows] })); } } };
  ws.onopen = () => { entry.opened = true; };
  const decoder = new TextDecoder();
  let seen = "", offered = false;
  ws.onmessage = (event) => {
    term.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
    if (offered) return;
    seen = (seen + (typeof event.data === "string" ? event.data : decoder.decode(new Uint8Array(event.data), { stream: true }))).slice(-4000);
    const path = untrustedConfig(seen);
    if (path) { offered = true; offerReview(host, path); }
  };
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


// --- what the CLI has besides: project configurations, MCP servers, logins, presets, images, activity ---------------

function textOf(input) { return String((input && input.value) || "").trim(); }
function wordsOf(text) { return String(text || "").split(/\s+/).filter(Boolean); }
function linesOf(text) { return String(text || "").split("\n").map(x => x.trim()).filter(Boolean); }
function outputBlock(r) { return el("pre", {}, String((r && r.output) || "")); }

// A command's JSON as people read it: a list of objects as a table of their fields (each as text), else the JSON.
function dataView(data, empty) {
  if (Array.isArray(data) && data.length && data.every(x => x && typeof x === "object" && !Array.isArray(x))) {
    const keys = [...new Set(data.flatMap(x => Object.keys(x)))].slice(0, 8);
    return el("div", { class: "table-scroll" }, table(keys, data.map(x => el("tr", {}, keys.map(k => el("td", { class: "mono" },
      x[k] === undefined || x[k] === null ? "" : typeof x[k] === "object" ? JSON.stringify(x[k]) : String(x[k])))))));
  }
  if (Array.isArray(data) && !data.length) return el("p", { class: "hint" }, empty || t("Nothing here."));
  return el("pre", {}, JSON.stringify(data, null, 2));
}

// What a check answers ({ok, output}): fine or not, with what it said.
function checkResult(box, r) { clear(box).append(notice(r.ok ? t("Everything is in place.") : t("The check found problems."), r.ok ? "ok" : "error"), outputBlock(r)); }

// A change that the service may still ask for (the view was out of date): asked in ROW, then sent confirmed.
async function sendAsking(row, method, path, body, done, trigger) {
  try { done(await api(method, path, body)); }
  catch (e) {
    if (body.confirm !== true && confirmable(e)) { askRefused(row, e, async () => { try { done(await api(method, path, Object.assign({}, body, { confirm: true }))); } catch (err) { failed(t("It did not work"))(err); } }, { trigger }); return; }
    failed(t("It did not work"))(e);
  }
}

// Project configurations (sandbox-ai wizard trust): a .sandbox-ai.json is used only while it is exactly what was
// trusted. Review shows it as it takes effect with its SHA-256; Trust sends that digest, so a file changed since the
// review is not trusted.
pages.projects = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Project configurations on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  let list;
  const live = pageLive();
  try { list = await api("GET", hostPath("/projects", host)); } catch (e) { list = e; }
  if (!live()) return;
  clear(main).append(el("h1", {}, t("Project configurations on {host}", { host })),
    el("p", { class: "hint" }, t("A project's {file} gives its starts their options: it is used only while it is exactly what you trusted.", { file: ".sandbox-ai.json" })));
  const reviewBox = el("div");
  const path = el("input", { id: "project-path", class: "mono", placeholder: "/home/you/src/project/.sandbox-ai.json" });
  const review = async (wanted) => {
    clear(reviewBox).append(el("p", { class: "hint" }, t("Loading…")));
    let r;
    try { r = await api("POST", hostPath("/projects/review", host), { path: wanted }); }
    catch (e) { clear(reviewBox).append(errorNotice(e)); return; }
    const row = el("div", { class: "row" });
    const now = r.trusted ? t("trusted as it is") : r.trusted_digest ? t("changed since it was trusted") : t("not trusted");
    clear(reviewBox).append(el("div", { class: "card", id: "project-review" }, el("h2", {}, t("{path} on {host}", { path: r.path, host })),
      // wizard trust --show --json says what keeps it from being trusted in "problem" (and "error"): nothing to trust then
      el("p", {}, now), r.problem || r.error ? notice(String(r.problem || r.error), "error") : el("pre", {}, JSON.stringify(r.document, null, 2)),
      el("p", { class: "hint mono" }, t("SHA-256 {digest}", { digest: r.digest })), row));
    if (!r.problem && !r.error && !r.trusted && r.digest) {
      const trust = el("button", { class: "primary", onclick: () => confirmIn(row, t("Trust exactly this configuration of {path} on {host}? Every later start of that project gets its options; if the file changes, it is no longer trusted.", { path: r.path, host }), t("Yes, trust it"), async () => {
        try { const out = await api("POST", hostPath("/projects/trust", host), { path: r.path, digest: r.digest, confirm: true }); toast("success", t("{path} trusted on {host}", { path: r.path, host }), (out && out.output) || ""); show("projects"); }
        catch (e) { failed(t("Could not trust {path} on {host}", { path: r.path, host }))(e); }
      }, { kind: "primary", trigger: trust }) }, t("Trust exactly this…"));
      row.append(trust);
    }
  };
  main.append(el("div", { class: "card" }, el("div", { class: "form" }, el("label", { for: "project-path" }, t("Configuration file")), path,
    el("span"), el("button", { class: "primary", onclick: () => { if (path.value.trim()) review(path.value.trim()); } }, t("Review")))), reviewBox);
  const STATES_OF = { trusted: N_("trusted as it is"), changed: N_("changed since it was trusted"), missing: N_("missing"), unreadable: N_("unreadable") };
  if (list instanceof Error) main.append(errorNotice(list));
  else main.append(el("h2", {}, t("Trusted")), table([t("Configuration file"), t("State"), ""], (Array.isArray(list) ? list : []).filter(x => x && typeof x.path === "string").map(x => {
    const actions = el("td", { class: "actions" });
    const forget = rowButton(t("Forget…"), x.path, host, { class: "danger", onclick: () => confirmIn(actions, t("Forget the trust in {path} on {host}? Starts of that project refuse to start until it is trusted again.", { path: x.path, host }), t("Yes, forget it"), async () => {
      try { await api("POST", hostPath("/projects/forget", host), { path: x.path, confirm: true }); show("projects"); } catch (e) { failed(t("Could not forget {path} on {host}", { path: x.path, host }))(e); }
    }, { trigger: forget }) });
    actions.append(rowButton(t("Review"), x.path, host, { onclick: () => { path.value = x.path; review(x.path); } }), forget);
    return el("tr", {}, el("td", { class: "mono" }, x.path), el("td", {}, STATES_OF[x.state] ? t(STATES_OF[x.state]) : String(x.state || "")), actions);
  }), t("No trusted project configurations on this host.")));
  if (state.review && state.review.host === host) { const wanted = state.review.path; state.review = null; path.value = wanted; review(wanted); }
};

// MCP servers (sandbox-ai mcp): every defined one, running or not; start (for a start: attached to it), stop, the
// catalogs and adding from them.
pages.mcp = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("MCP servers on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  const list = await api("GET", hostPath("/mcp", host));
  if (!live()) return;
  clear(main).append(el("h1", {}, t("MCP servers on {host}", { host })), el("p", { class: "hint" }, t("Each runs in a sandbox of its own; an agent reaches it with {option}.", { option: "--mcp NAME" })));
  const servers = (Array.isArray(list) ? list : []).filter(x => x && typeof x.name === "string");
  const startBox = el("div");
  const startForm = (server) => {
    const backend = el("select", { id: "mcp-backend" }, el("option", { value: "" }, t("as defined")), ["podman", "bwrap"].map(b => el("option", { value: b }, b)));
    const vpn = el("input", { id: "mcp-vpn", class: "mono", placeholder: "home" });
    const forStart = el("input", { id: "mcp-for", class: "mono", placeholder: t("a start's name (optional)") });
    const restart = el("input", { id: "mcp-restart", type: "checkbox" });
    const row = el("div", { class: "row" });
    const go = (event) => {
      const body = { backend: backend.value, vpn: vpn.value.trim(), for: forStart.value.trim(), restart: restart.checked };
      const send = async (confirm) => {
        try { const r = await api("POST", hostPath(`/mcp/${encodeURIComponent(server)}/start`, host), Object.assign({}, body, confirm ? { confirm: true } : {})); toast("success", t("{name} started on {host}", { name: server, host }), (r && (r.output || r.message)) || ""); show("mcp"); }
        catch (e) { failed(t("Could not start {name} on {host}", { name: server, host }))(e); }
      };
      if (!body.for) { busy(event.target, () => send(false)); return; }
      confirmIn(row, body.restart ? t("Start {name} on {host} for {start} and restart {start} now? What runs in it ends; the agent continues its conversation.", { name: server, host, start: body.for })
        : t("Start {name} on {host} for {start}? {start} reaches it from its next start on (kept).", { name: server, host, start: body.for }), t("Yes, start it"), () => send(true), { kind: "primary", trigger: event.target });
    };
    row.append(el("button", { class: "primary", onclick: go }, t("Start")), el("button", { onclick: () => clear(startBox) }, t("Close")));
    clear(startBox).append(el("div", { class: "card" }, el("h2", {}, t("Start {name} on {host}", { name: server, host })), el("div", { class: "form" },
      el("label", { for: "mcp-backend" }, t("Backend")), backend, el("label", { for: "mcp-vpn" }, t("VPN profile")), vpn,
      el("label", { for: "mcp-for" }, t("For the start")), forStart, el("label", { for: "mcp-restart" }, t("Restart that start now")), restart, el("span"), row)));
  };
  main.append(table([t("Name"), t("State"), t("Serves"), t("Command"), ""], servers.map(x => {
    const actions = el("td", { class: "actions" });
    const stop = rowButton(t("Stop"), x.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Stop the MCP server {name} on {host}? The starts it serves lose it until it starts again.", { name: x.name, host }), t("Yes, stop it"), async () => {
      try { await api("POST", hostPath(`/mcp/${encodeURIComponent(x.name)}/stop`, host), { confirm: true }); show("mcp"); } catch (e) { failed(t("Could not stop {name} on {host}", { name: x.name, host }))(e); }
    }, { trigger: stop }) });
    actions.append(rowButton(t("Start…"), x.name, host, { onclick: () => startForm(x.name) }), stop);
    return el("tr", {}, el("td", { class: "mono" }, x.name), statusCell(x.status), el("td", {}, (x.serves || []).join(", ")), el("td", { class: "mono" }, String(x.command || "")), actions);
  }), t("No MCP servers on this host yet: add one from a catalog below.")), startBox);

  // the catalogs, and adding an entry with its options
  const catalogs = el("input", { id: "mcp-catalogs", class: "mono", placeholder: t("default catalogs (or files and https URLs)") });
  const catalogBox = el("div");
  const entry = el("input", { id: "mcp-entry", class: "mono", placeholder: "hub:binwalk" });
  const as = el("input", { id: "mcp-as", class: "mono", placeholder: t("the entry's name") });
  const mounts = el("textarea", { id: "mcp-mounts", rows: 2, class: "mono", placeholder: "/mnt/samples=/home/you/samples:ro" });
  const env = el("input", { id: "mcp-env", class: "mono", placeholder: "VT_API_KEY" });
  const network = el("select", { id: "mcp-network" }, el("option", { value: "" }, t("as the entry says")), ["none", "full"].map(n => el("option", { value: n }, n)));
  const memory = el("input", { id: "mcp-memory", placeholder: "512m" }), pids = el("input", { id: "mcp-pids", type: "number", min: 1, placeholder: "128" });
  const backend = el("select", { id: "mcp-add-backend" }, el("option", { value: "" }, t("Podman when it is there")), ["podman", "bwrap", "both"].map(b => el("option", { value: b }, b)));
  const vpn = el("input", { id: "mcp-add-vpn", class: "mono", placeholder: "home" });
  const noPull = el("input", { id: "mcp-no-pull", type: "checkbox" }), unsigned = el("input", { id: "mcp-unsigned", type: "checkbox" });
  const addRow = el("div", { class: "row" }), addStatus = el("div", { class: "wide" });
  const showCatalog = (event) => busy(event.target, async () => {
    clear(catalogBox).append(el("p", { class: "hint" }, t("Loading…")));
    let r;
    try { r = await api("POST", hostPath("/mcp-catalog", host), { catalogs: wordsOf(catalogs.value) }); } catch (e) { clear(catalogBox).append(errorNotice(e)); return; }
    const SIGNED = { good: N_("signed"), bad: N_("signature bad"), none: N_("unsigned") };
    clear(catalogBox).append(...(r.problems || []).map(x => notice(x, "warn")), table([t("Entry"), t("Network"), t("Title"), t("Signature"), ""], (r.entries || []).map(x =>
      el("tr", {}, el("td", { class: "mono" }, x.entry), el("td", { class: "mono" }, x.network), el("td", {}, x.title, x.published ? "" : ` (${t("not published yet")})`),
        el("td", { class: x.signature === "good" ? "status-ok" : "status-fail" }, t(SIGNED[x.signature] || N_("unsigned"))),
        el("td", { class: "actions" }, el("button", { onclick: () => { entry.value = x.entry; unsigned.checked = false; entry.focus(); } }, t("Add…"))))), t("The catalogs list no servers.")));
  });
  const add = (event) => {
    clear(addStatus);
    const body = { entry: entry.value.trim(), as: as.value.trim(), env: wordsOf(env.value), network: network.value, memory: memory.value.trim(), pids: pids.value.trim(),
      catalogs: wordsOf(catalogs.value), no_pull: noPull.checked, backend: backend.value, vpn: vpn.value.trim(), unsigned: unsigned.checked, mounts: [] };
    for (const line of linesOf(mounts.value)) {
      const match = /^([^=]+)=([^:]+)(?::(ro|rw))?$/.exec(line);
      if (!match) { addStatus.append(notice(t("A mount is TARGET=HOST or TARGET=HOST:ro|rw: {line}", { line }), "error")); return; }
      body.mounts.push({ target: match[1], host: match[2], mode: match[3] || "ro" });
    }
    const send = async (confirm) => {
      try { const r = await api("POST", hostPath("/mcp", host), Object.assign({}, body, confirm ? { confirm: true } : {})); toast("success", t("{name} added on {host}", { name: body.entry, host }), (r && r.output) || ""); show("mcp"); }
      catch (e) { addStatus.append(errorNotice(e)); }
    };
    const writable = body.mounts.filter(m => m.mode === "rw").map(m => m.host);
    const what = [body.unsigned ? t("from a catalog no trusted key signed") : null, writable.length ? t("with {dirs} writable", { dirs: writable.join(", ") }) : null].filter(Boolean);
    if (!what.length) { busy(event.target, () => send(false)); return; }
    confirmIn(addRow, t("Add the MCP server {name} on {host} {what}?", { name: body.entry, host, what: what.join(t(" and ")) }), t("Yes, add it"), () => send(true), { kind: "primary", trigger: event.target });
  };
  addRow.append(el("button", { class: "primary", onclick: add }, t("Add the server")));
  main.append(el("h2", {}, t("Catalogs")), el("div", { class: "card" }, el("div", { class: "form" }, el("label", { for: "mcp-catalogs" }, t("Catalogs")), catalogs,
    el("span"), el("button", { onclick: showCatalog }, t("Show the catalog"))), catalogBox),
  el("h2", {}, t("Add a server")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "mcp-entry" }, t("Catalog entry")), entry, el("label", { for: "mcp-as" }, t("Name for --mcp")), as,
    el("label", { for: "mcp-mounts" }, t("Mounts (one per line)")), mounts, el("label", { for: "mcp-env" }, t("Variables (secrets it names)")), env,
    el("label", { for: "mcp-network" }, t("Network")), network, el("label", { for: "mcp-memory" }, t("Memory")), memory, el("label", { for: "mcp-pids" }, t("Processes")), pids,
    el("label", { for: "mcp-add-backend" }, t("Backend")), backend, el("label", { for: "mcp-add-vpn" }, t("VPN profile")), vpn,
    el("label", { for: "mcp-no-pull" }, t("The image is there already")), noPull, el("label", { for: "mcp-unsigned" }, t("Also from an unsigned catalog")), unsigned,
    addStatus, el("span"), addRow)));
};

// Logins (sandbox-ai login, plugins, git-logins, publish-logins, identities): the tokens stay secrets of that host.
// A Git or publish login's token is typed here (stored as a secret first: tokenSource) or a stored secret is chosen;
// a token already stored is never shown or sent back.
pages.logins = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Logins on {host}", { host })));
  main.append(el("p", { class: "hint" }, t("Tokens are kept in that host's secrets, for good: a token typed here is stored there first, one already stored is never shown."), " ", el("button", { onclick: () => show("secrets") }, t("Open Secrets"))));
  // the agents' own logins and Claude's plugins: interactive, in a terminal
  const agentRows = AGENTS.map(agent => {
    const actions = el("td", { class: "actions" });
    const logout = rowButton(t("Log out…"), agent, host, { class: "danger", onclick: () => confirmIn(actions, t("Log {agent} out on {host}? Its sandboxes need a new login.", { agent, host }), t("Yes, log out"), () => openTerminal({ kind: "login", host, agent, logout: "1", confirm: "1" }, t("logout {agent}", { agent })), { trigger: logout }) });
    actions.append(rowButton(t("Log in (terminal)"), agent, host, { class: "primary", onclick: () => openTerminal({ kind: "login", host, agent }, t("login {agent}", { agent })) }), logout);
    return el("tr", {}, el("td", { class: "mono" }, agent), actions);
  });
  const update = el("input", { id: "plugins-update", type: "checkbox" }), pluginKits = el("input", { id: "plugins-kits", class: "mono", placeholder: "egm" });
  main.append(el("h2", {}, t("The agents' logins")), table([t("Agent"), ""], agentRows),
    el("div", { class: "card" }, el("h3", {}, t("Claude plugins")), el("div", { class: "form" },
      el("label", { for: "plugins-update" }, t("Also update them")), update, el("label", { for: "plugins-kits" }, t("Plugins of built-in kits")), pluginKits,
      el("span"), el("button", { onclick: () => openTerminal({ kind: "plugins", host, agent: "claude", update: update.checked ? "1" : "", kits: wordsOf(pluginKits.value.replace(/,/g, " ")).join(",") }, t("plugins on {host}", { host })) }, t("Install in a terminal")))));

  const live = pageLive();
  const section = async (title, path, draw) => {
    if (!live()) return;  // another page was drawn meanwhile: nothing more of this one
    const box = el("div", {}, el("p", { class: "hint" }, t("Loading…")));
    main.append(el("h2", {}, title), box);
    let found;
    try { found = await api("GET", hostPath(path, host)); } catch (e) { clear(box).append(errorNotice(e)); return; }
    if (live()) draw(box, found);
  };
  // WHAT: what the row is about (its server, kind or name), for the button's name
  const removeButton = (holder, text, path, body, what) => {
    const button = rowButton(t("Remove…"), what, host, { class: "danger", onclick: () => confirmIn(holder, text, t("Yes, remove it"), async () => {
      try { await api("POST", hostPath(path, host), Object.assign({}, body, { confirm: true })); show("logins"); } catch (e) { failed(t("Could not remove it"))(e); }
    }, { trigger: button }) });
    return button;
  };
  const check = (path, body) => async (event) => { const box = event.target.parentNode.parentNode.querySelector(".check-result") || event.target.parentNode; await busy(event.target, async () => { try { checkResult(box, await api("POST", hostPath(path, host), body())); } catch (e) { clear(box).append(errorNotice(e)); } }); };
  // the names of that host's secrets (secrets list), for "Use a stored secret" and to ask before one is replaced
  let secretNames = [], secretsError = null;
  try { const list = await api("GET", hostPath("/secrets", host)); secretNames = (Array.isArray(list) ? list : []).filter(x => x && typeof x.name === "string").map(x => x.name); }
  catch (e) { secretsError = e; }
  if (!live()) return;
  // The token of a login (git-logins, publish-logins take a secret's NAME): typed here, stored first as a secret of
  // that host under a name it suggests (SUGGEST(); editable), as the page Secrets does (secrets set NAME, the token on
  // its standard input, never in a command line, a log or an answer), or a secret already stored there. PREFIX: the
  // fields' ids. ready(row, trigger, go): checks the fields, asks before a stored secret is replaced, then go(name,
  // replace): replace null for a stored secret, else whether the replacement was confirmed. store(row, name,
  // replace, then): stores a typed token (asks again when one of that name came meanwhile), then then().
  const tokenSource = (prefix, suggest) => {
    const source = el("select", { id: `${prefix}-source`, value: "token" }, el("option", { value: "token" }, t("Enter the token")), el("option", { value: "stored" }, t("Use a stored secret")));
    const [token, tokenField] = secretInput({ id: `${prefix}-token`, class: "mono", "aria-describedby": `${prefix}-secret-hint` });
    const name = el("input", { id: `${prefix}-secret`, class: "mono", value: suggest(), pattern: "[A-Za-z_][A-Za-z0-9_]{0,63}", autocomplete: "off", spellcheck: "false", "aria-describedby": `${prefix}-secret-hint` });
    let edited = false;
    name.addEventListener("input", () => { edited = name.value.trim() !== "" && name.value.trim() !== suggest(); });
    const stored = el("select", { id: `${prefix}-stored`, class: "mono" }, secretNames.length ? secretNames.map(n => el("option", { value: n }, n))
      : el("option", { value: "", disabled: true, selected: true }, secretsError ? t("the secrets of {host} could not be listed", { host }) : t("no secrets on {host} yet", { host })));
    const tokenLabel = el("label", { for: `${prefix}-token` }, t("Token")), nameLabel = el("label", { for: `${prefix}-secret` }, t("Stored as the secret"));
    const storedLabel = el("label", { for: `${prefix}-stored` }, t("Stored secret"));
    const hint = el("p", { class: "hint wide", id: `${prefix}-secret-hint` }, t("A secret's name is what the token is stored under on that host, for good (page Secrets): logins and starts name it, never the token itself."));
    const redraw = () => {
      const typed = source.value === "token";
      for (const n of [tokenLabel, tokenField, nameLabel, name]) n.hidden = !typed;
      for (const n of [storedLabel, stored]) n.hidden = typed;
    };
    source.addEventListener("change", redraw);
    redraw();
    const refresh = () => { if (!edited) name.value = suggest(); };
    const ready = (row, trigger, go) => {
      if (source.value === "stored") {
        if (!stored.value) { toast("warning", t("Which secret?"), t("Choose a stored secret, or enter the token.")); return; }
        go(stored.value, null); return;
      }
      const wanted = name.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(wanted)) { toast("warning", t("Which name?"), t("Letters, digits and _ (up to 64), not starting with a digit.")); return; }
      if (!token.value) { toast("warning", t("Which token?"), t("Enter the token, or use a stored secret.")); return; }
      if (secretNames.includes(wanted)) { confirmIn(row, t("Replace the value of {name} on {host}? The old one is gone.", { name: wanted, host }), t("Yes, replace it"), () => go(wanted, true), { trigger }); return; }
      go(wanted, false);
    };
    const store = async (row, wanted, replace, then) => {
      if (replace === null) return then();
      try { await api("POST", hostPath(`/secrets/${encodeURIComponent(wanted)}`, host), Object.assign({ secret: token.value }, replace ? { confirm: true } : {})); }
      catch (e) {
        // a secret of that name came meanwhile (another tab, the command line): asked now, the token kept for the Yes
        if (!replace && confirmable(e)) { askRefused(row, e, () => store(row, wanted, true, then)); return undefined; }
        token.value = ""; failed(t("Could not store the token as {name} on {host}", { name: wanted, host }))(e); return undefined;
      }
      token.value = "";
      if (!secretNames.includes(wanted)) secretNames.push(wanted);
      return then();
    };
    const fields = [el("label", { for: `${prefix}-source` }, t("Token from")), source, tokenLabel, tokenField, nameLabel, name, storedLabel, stored, hint];
    return { fields, ready, store, refresh, typed: () => source.value === "token" };
  };

  await section(t("Git logins (every sandbox's proxy)"), "/git-logins", (box, list) => {
    const rows = (Array.isArray(list) ? list : []).filter(x => x && x.url).map(x => {
      const actions = el("td", { class: "actions" });
      actions.append(removeButton(actions, t("Remove the Git login for {url} on {host}? Sandboxes no longer push there with it.", { url: x.url, host }), "/git-logins/remove", { url: x.url }, x.url));
      return el("tr", {}, el("td", { class: "mono" }, x.url), el("td", { class: "mono" }, x.user || ""), el("td", { class: "mono" }, x.secret || ""), actions);
    });
    const url = el("input", { id: "git-url", class: "mono", placeholder: "https://git.example.org" }), user = el("input", { id: "git-user", class: "mono", placeholder: "x-access-token" });
    const token = tokenSource("git", () => gitTokenName(url.value));
    url.addEventListener("input", token.refresh);
    const row = el("div", { class: "row" }), result = el("div", { class: "wide check-result" });
    const add = (event) => token.ready(row, event.target, (secret, replace) => {
      const body = { url: url.value.trim(), secret, user: user.value.trim() };
      const question = t("Give every sandbox on {host} that reaches {url} the Git login of the secret {secret}? The connection proxy adds it; the token never enters a sandbox.", { host, url: body.url, secret });
      confirmIn(row, replace === null ? question : `${question} ${t("The token is stored first as the secret {secret} on {host}, for good.", { secret, host })}`, t("Yes, add it"),
        () => token.store(row, secret, replace, () => sendAsking(row, "POST", hostPath("/git-logins", host), Object.assign(body, { confirm: true }), () => show("logins"))), { kind: "primary", trigger: event.target });
    });
    row.append(el("button", { class: "primary", onclick: add }, t("Add…")), el("button", { onclick: check("/git-logins/check", () => ({ url: url.value.trim() })) }, t("Check")));
    clear(box).append(table([t("Server"), t("User"), t("Secret"), ""], rows, t("No Git logins on this host.")), el("div", { class: "card" }, el("div", { class: "form" },
      el("label", { for: "git-url" }, t("Server")), url, el("label", { for: "git-user" }, t("User")), user, ...token.fields, el("span"), row, result)));
  });

  await section(t("Publish logins (with --publish only)"), "/publish-logins", (box, list) => {
    const rows = (Array.isArray(list) ? list : []).filter(x => x && x.kind).map(x => {
      const actions = el("td", { class: "actions" });
      actions.append(removeButton(actions, t("Remove the publish login {kind} on {host}?", { kind: x.kind, host }), `/publish-logins/${encodeURIComponent(x.kind)}/remove`, {}, x.kind));
      return el("tr", {}, el("td", { class: "mono" }, x.kind), el("td", { class: "mono" }, x.host || ""), el("td", { class: "mono" }, x.secret || ""), actions);
    });
    const kind = el("select", { id: "publish-kind" }, PUBLISH_KINDS.map(k => el("option", { value: k }, k)));
    const url = el("input", { id: "publish-url", class: "mono", placeholder: "https://npm.example.org" });
    const token = tokenSource("publish", () => PUBLISH_TOKEN_NAMES[kind.value] || "PUBLISH_TOKEN");
    kind.addEventListener("change", token.refresh);
    const row = el("div", { class: "row" }), result = el("div", { class: "wide check-result" });
    const add = (event) => token.ready(row, event.target, (secret, replace) => busy(event.target, () => token.store(row, secret, replace,
      () => sendAsking(row, "POST", hostPath("/publish-logins", host), { kind: kind.value, secret, url: url.value.trim() }, () => show("logins"), event.target))));
    row.append(el("button", { class: "primary", onclick: add }, t("Add")),
      el("button", { onclick: check("/publish-logins/check", () => ({ kind: kind.value })) }, t("Check")),
      el("button", { onclick: (event) => busy(event.target, async () => { try { clear(result).append(outputBlock(await api("GET", hostPath("/publish-logins/kinds", host)))); } catch (e) { clear(result).append(errorNotice(e)); } }) }, t("What each kind opens")));
    clear(box).append(table([t("Kind"), t("Host"), t("Secret"), ""], rows, t("No publish logins on this host.")), el("div", { class: "card" }, el("div", { class: "form" },
      el("label", { for: "publish-kind" }, t("Kind")), kind, ...token.fields, el("label", { for: "publish-url" }, t("Another server (optional)")), url, el("span"), row, result)));
  });

  await section(t("Identities (--identity NAME)"), "/identities", (box, found) => {
    const all = found && typeof found === "object" && !Array.isArray(found) ? found : {};
    const shown = el("div");
    const rows = Object.entries(all).filter(([, x]) => x && typeof x === "object").map(([name, x]) => {
      const actions = el("td", { class: "actions" });
      actions.append(rowButton(t("Show"), name, host, { onclick: async (event) => busy(event.target, async () => { try { clear(shown).append(el("h3", {}, name), dataView(await api("GET", hostPath(`/identities/${encodeURIComponent(name)}`, host)))); } catch (e) { clear(shown).append(errorNotice(e)); } }) }));
      const remove = rowButton(t("Remove…"), name, host, { class: "danger", onclick: () => confirmIn(actions, t("Remove the identity {name} on {host} with its logins? Starts with {option} cannot start.", { name, host, option: `--identity ${name}` }), t("Yes, remove it"), async () => {
        try { await api("DELETE", hostPath(`/identities/${encodeURIComponent(name)}`, host), { confirm: true }); show("logins"); } catch (e) { failed(t("Could not remove it"))(e); }
      }, { trigger: remove }) });
      actions.append(remove);
      for (const g of Array.isArray(x.git) ? x.git : []) actions.append(removeButton(actions, t("Remove the Git login {url} of {name} on {host}?", { url: g.url, name, host }), `/identities/${encodeURIComponent(name)}/logout`, { url: g.url }, `${name} ${g.url}`));
      for (const p of Array.isArray(x.publish) ? x.publish : []) actions.append(removeButton(actions, t("Remove the publish login {kind} of {name} on {host}?", { kind: p.kind, name, host }), `/identities/${encodeURIComponent(name)}/unpublish`, { kind: p.kind }, `${name} ${p.kind}`));
      const logins = [...(Array.isArray(x.git) ? x.git.map(g => g.url) : []), ...(Array.isArray(x.publish) ? x.publish.map(p => p.kind) : [])].join(", ");
      return el("tr", {}, el("td", { class: "mono" }, name), el("td", {}, [x.name, x.email].filter(Boolean).join(" ")), el("td", {}, x.description || ""), el("td", { class: "mono" }, logins), actions);
    });
    const ident = el("input", { id: "identity-name", class: "mono", placeholder: "work" }), person = el("input", { id: "identity-person", placeholder: "Ann Example" });
    const email = el("input", { id: "identity-email", class: "mono", placeholder: "ann@example.org" }), description = el("input", { id: "identity-description" });
    const loginUrl = el("input", { id: "identity-url", class: "mono", placeholder: "https://github.com" }), loginUser = el("input", { id: "identity-user", class: "mono" });
    const loginSecret = el("input", { id: "identity-secret", class: "mono", placeholder: "GH_WORK" });
    const publishKind = el("select", { id: "identity-kind" }, el("option", { value: "" }, t("a Git server (above)")), PUBLISH_KINDS.map(k => el("option", { value: k }, k)));
    const row = el("div", { class: "row" }), loginRow = el("div", { class: "row" }), result = el("div", { class: "wide check-result" });
    const fields = () => ({ name: person.value.trim(), email: email.value.trim(), description: description.value.trim() });
    row.append(el("button", { class: "primary", onclick: (event) => busy(event.target, () => sendAsking(row, "POST", hostPath("/identities", host), Object.assign({ identity: ident.value.trim() }, fields()), () => show("logins"), event.target)) }, t("Add")),
      el("button", { onclick: (event) => busy(event.target, () => sendAsking(row, "POST", hostPath(`/identities/${encodeURIComponent(ident.value.trim())}`, host), fields(), () => show("logins"), event.target)) }, t("Change it")),
      el("button", { onclick: check("/identities-check", () => ({ identity: ident.value.trim() })) }, t("Check")));
    loginRow.append(el("button", { class: "primary", onclick: (event) => busy(event.target, () => {
      const name = encodeURIComponent(ident.value.trim());
      return publishKind.value ? sendAsking(loginRow, "POST", hostPath(`/identities/${name}/publish`, host), { kind: publishKind.value, secret: loginSecret.value.trim(), url: loginUrl.value.trim() }, () => show("logins"), event.target)
        : sendAsking(loginRow, "POST", hostPath(`/identities/${name}/login`, host), { url: loginUrl.value.trim(), secret: loginSecret.value.trim(), user: loginUser.value.trim() }, () => show("logins"), event.target);
    }) }, t("Add the login")));
    clear(box).append(table([t("Identity"), t("Commits as"), t("Description"), t("Logins"), ""], rows, t("No identities on this host: starts work as default.")), shown,
      el("div", { class: "card" }, el("div", { class: "form" },
        el("label", { for: "identity-name" }, t("Identity")), ident, el("label", { for: "identity-person" }, t("Commit name")), person,
        el("label", { for: "identity-email" }, t("Commit e-mail")), email, el("label", { for: "identity-description" }, t("Description")), description, el("span"), row, result,
        el("h3", { class: "wide" }, t("A login of this identity")),
        el("label", { for: "identity-kind" }, t("For")), publishKind, el("label", { for: "identity-url" }, t("Server")), loginUrl,
        el("label", { for: "identity-user" }, t("User")), loginUser, el("label", { for: "identity-secret" }, t("Secret (its name)")), loginSecret, el("span"), loginRow)));
  });
};
const PUBLISH_KINDS = ["npm", "pypi", "testpypi", "crates", "gitlab", "github"];
// The name a typed token of a publish login is stored under unless changed (the usual ones of each registry).
const PUBLISH_TOKEN_NAMES = { npm: "NPM_TOKEN", pypi: "PYPI_TOKEN", testpypi: "TESTPYPI_TOKEN", crates: "CARGO_TOKEN", gitlab: "GITLAB_TOKEN", github: "GITHUB_TOKEN" };
// ... and of a Git login: GIT_TOKEN_<HOST> of its server (git.example.org: GIT_TOKEN_GIT_EXAMPLE_ORG), up to 64.
function gitTokenName(address) {
  let hostname = "";
  try { hostname = new URL(String(address || "").trim()).hostname; } catch (e) { hostname = ""; }
  const part = hostname.toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  return part ? `GIT_TOKEN_${part}`.slice(0, 64).replace(/_+$/, "") : "GIT_TOKEN";
}

// Presets (sandbox-ai presets): what each sets, what is missing before it starts, and the extra directories.
pages.presets = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Presets on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  let list;
  const live = pageLive();
  try { list = await api("GET", hostPath("/presets", host)); } catch (e) { list = e; }
  if (!live()) return;
  clear(main).append(el("h1", {}, t("Presets on {host}", { host })));
  const detail = el("div");
  const open = async (p) => {
    clear(detail).append(el("p", { class: "hint" }, t("Loading…")));
    let found;
    try { found = await api("GET", hostPath(`/presets/${encodeURIComponent(p.id)}`, host)); } catch (e) { clear(detail).append(errorNotice(e)); return; }
    const values = found && typeof found.values === "object" && found.values ? found.values : {};
    const inputs = Object.entries(values).map(([key, spec]) => [key, el("input", { id: `preset-value-${key}`, class: "mono", placeholder: spec && spec.default !== undefined ? String(spec.default) : "" }), spec]);
    const agent = el("select", { id: "preset-agent" }, el("option", { value: "" }, t("any agent")), AGENTS.map(a => el("option", { value: a }, a)));
    const result = el("div", { class: "wide" });
    clear(detail).append(el("div", { class: "card" }, el("h2", {}, `${found.id || p.id}: ${found.title || ""}`), el("p", {}, found.description || ""),
      el("pre", {}, JSON.stringify(found, null, 2)),
      el("div", { class: "form" }, inputs.map(([key, input, spec]) => [el("label", { for: input.id }, (spec && spec.label) || key), input]),
        el("label", { for: "preset-agent" }, t("Agent")), agent,
        el("span"), el("button", { class: "primary", onclick: (event) => busy(event.target, async () => {
          const body = { values: Object.fromEntries(inputs.map(([key, input]) => [key, input.value.trim()])), agent: agent.value };
          try { checkResult(result, await api("POST", hostPath(`/presets/${encodeURIComponent(p.id)}/check`, host), body)); } catch (e) { clear(result).append(errorNotice(e)); }
        }) }, t("Check what is missing")), result)));
  };
  if (list instanceof Error) main.append(errorNotice(list));
  else main.append(table([t("Preset"), t("Title"), t("Origin"), ""], (Array.isArray(list) ? list : []).filter(p => p && p.id).map(p =>
    el("tr", {}, el("td", { class: "mono" }, p.id), el("td", {}, p.title || ""), el("td", {}, p.origin || ""), el("td", { class: "actions" }, rowButton(t("Show"), p.id, host, { onclick: () => open(p) })))), t("No presets on this host.")), detail);
  // the extra preset directories (defaults.json preset_dirs)
  const dirsBox = el("div");
  main.append(el("h2", {}, t("Preset directories")), dirsBox);
  let dirs;
  try { dirs = await api("GET", hostPath("/preset-dirs", host)); } catch (e) { if (live()) dirsBox.append(errorNotice(e)); return; }
  if (!live()) return;
  const listed = Array.isArray(dirs && dirs.dirs) ? dirs.dirs : [];
  const directory = el("input", { id: "preset-dir", class: "mono", placeholder: "/home/you/presets" });
  const row = el("div", { class: "row" });
  dirsBox.append(el("p", { class: "hint mono" }, [dirs.builtin, dirs.yours].filter(Boolean).join("  ·  ")),
    table([t("Directory"), t("State"), ""], listed.map(d => {
      const path = typeof d === "string" ? d : String(d.path || d.directory || "");
      return el("tr", {}, el("td", { class: "mono" }, path), el("td", {}, typeof d === "object" ? String(d.state || d.used || "") : ""),
        el("td", { class: "actions" }, rowButton(t("Remove"), path, host, { onclick: (event) => busy(event.target, async () => { try { await api("POST", hostPath("/preset-dirs/remove", host), { directory: path }); show("presets"); } catch (e) { failed(t("Could not remove it"))(e); } }) })));
    }), t("No extra preset directories.")),
    el("div", { class: "card" }, el("div", { class: "form" }, el("label", { for: "preset-dir" }, t("Directory")), directory, el("span"), row)));
  row.append(el("button", { class: "primary", onclick: (event) => {
    const dir = directory.value.trim();  // the directory the question names is the one sent
    if (!dir.startsWith("/")) { toast("warning", t("Which directory?"), t("An absolute path on {host}.", { host })); return; }
    confirmIn(row, t("Read presets from {dir} on {host} too, for every later start?", { dir, host }), t("Yes, add it"), async () => {
      try { await api("POST", hostPath("/preset-dirs", host), { directory: dir, confirm: true }); show("presets"); } catch (e) { failed(t("Could not add it"))(e); }
    }, { kind: "primary", trigger: event.target });
  } }, t("Add…")));
};

// Teams of agents (sandbox-ai team on that host): one sandbox per agent on a shared workspace, the first leads, a task
// board (an MCP server without network) between them. A team starts in a terminal of the page, where team start says
// how each member started; each member is a start of its own (NAME-AGENT on Starts and sessions). Show opens its
// members and board right below its row; Stop and Delete are asked first, naming the team and the host.
const TEAM_NAME = /^[a-z0-9][a-z0-9_-]{0,19}$/;

async function teamCard(team, slot, host, posting) {
  const old = document.getElementById("team-card");
  if (old) dropCard(old);
  const body = el("div", {}, el("p", { class: "hint" }, t("Loading…")));
  const agent = el("select", { id: "team-post-agent" }, (team.members || []).map(a => el("option", { value: a }, a)));
  const task = el("textarea", { id: "team-post-task", rows: 3, placeholder: t("What the member should do (it reports back on the board)") });
  const postRow = el("div", { class: "row" });
  const card = el("div", { class: "card", id: "team-card" }, el("h2", {}, t("Team {team} on {host}", { team: team.name, host })), body,
    el("h3", {}, t("Post a task")), el("div", { class: "form" }, el("label", { for: "team-post-agent" }, t("For")), agent,
      el("label", { for: "team-post-task" }, t("Task")), task, el("span"), postRow),
    el("div", { class: "row" }, el("button", { onclick: () => dropCard(card) }, t("Close"))));
  clear(slot.children[0]).append(card); slot.hidden = false; card.holder = slot; card.keepHolder = true;
  revealCard(card);
  if (posting && typeof task.focus === "function") task.focus();
  const load = async () => {
    let found;
    try { found = await api("GET", hostPath(`/teams/${encodeURIComponent(team.name)}`, host)); } catch (e) { clear(body).append(errorNotice(e)); return; }
    if (!card.isConnected) return;
    const states = found && found.states && typeof found.states === "object" ? found.states : {};
    const board = found && found.board && typeof found.board === "object" ? found.board : {};
    const members = (Array.isArray(found && found.members) ? found.members : team.members || []).filter(a => typeof a === "string");
    const memberRows = members.map(a => {
      const member = `${team.name}-${a}`;
      return el("tr", {}, el("td", { class: "mono" }, member), el("td", {}, a === found.lead ? t("leads") : t("member")), statusCell(states[a]),
        el("td", { class: "actions" }, states[a] === "running" ? rowButton(t("Attach"), member, host, { class: "primary", onclick: () => openTerminal({ kind: "attach", name: member, host }, member) }) : null));
    });
    const tasks = (Array.isArray(board.tasks) ? board.tasks : []).filter(x => x && typeof x === "object").slice(-50).reverse();
    clear(body).append(...[el("p", { class: "hint mono" }, String((found && found.workspace) || team.workspace || "")),
      table([t("Member"), t("Role"), t("State"), ""], memberRows, t("No members."), t("Members of {team} on {host}", { team: team.name, host })),
      board.error ? notice(t("The board could not be read: {error}", { error: String(board.error) }), "warn") : null,
      el("h3", {}, t("Board")),
      table(["#", t("From"), t("To"), t("State"), t("Task")], tasks.map(x => el("tr", {}, el("td", {}, String(x.id ?? "")), el("td", { class: "mono" }, String(x.from || "?")),
        el("td", { class: "mono" }, String(x.to || "")), el("td", {}, String(x.state || "")), el("td", {}, String(x.task || "").slice(0, 500)))), t("No tasks on the board yet."), t("Board of {team} on {host}", { team: team.name, host }))].filter(Boolean));
  };
  const post = el("button", { class: "primary", onclick: (ev) => busy(ev.target, async () => {
    const text = task.value.trim(), to = agent.value;
    if (!text) { toast("warning", t("Which task?"), t("Write what the member should do.")); return; }
    try {
      const r = await api("POST", hostPath(`/teams/${encodeURIComponent(team.name)}/post`, host), { agent: to, task: text });
      toast("success", t("Task posted for {agent} in {team} on {host}", { agent: to, team: team.name, host }), (r && r.output) || "");
      task.value = "";
      await load();
    } catch (e) { failed(t("Could not post the task in {team} on {host}", { team: team.name, host }))(e); }
  }) }, t("Post"));
  postRow.append(post, el("button", { onclick: (ev) => busy(ev.target, load) }, t("Reload")));
  await load();
}

pages.teams = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Teams on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  let list;
  try { list = await api("GET", hostPath("/teams", host)); } catch (e) { list = e; }
  if (!live()) return;
  clear(main).append(el("h1", {}, t("Teams on {host}", { host })),
    el("p", { class: "hint" }, t("One sandbox per agent on a shared workspace; the first agent leads, and a task board between them (an MCP server of its own, without network) carries the tasks. Each member is a start of its own: NAME-AGENT.")));
  if (list instanceof Error) main.append(errorNotice(list));
  const teams = (Array.isArray(list) ? list : []).filter(x => x && typeof x.name === "string" && Array.isArray(x.members));
  const rows = [];
  for (const team of teams) {
    const actions = el("td", { class: "actions" });
    const slot = el("tr", { class: "card-row", hidden: true }, el("td", { colspan: 4 }));
    const members = team.members.map(a => `${team.name}-${a}`).join(", ");
    const stop = rowButton(t("Stop…"), team.name, host, { onclick: () => confirmIn(actions, t("Stop the team {team} on {host}? What runs in its members ({members}) ends, and its board stops.", { team: team.name, host, members }), t("Yes, stop it"), async () => {
      try { const r = await api("POST", hostPath(`/teams/${encodeURIComponent(team.name)}/stop`, host), { confirm: true }); toast("success", t("Team {team} on {host} stopped", { team: team.name, host }), (r && r.output) || ""); show("teams"); }
      catch (e) { failed(t("Could not stop the team {team} on {host}", { team: team.name, host }))(e); }
    }, { trigger: stop }) });
    const remove = rowButton(t("Delete…"), team.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Delete the team {team} on {host} with its members ({members}), their HOMEs and its board? What is in those HOMEs is gone for good.", { team: team.name, host, members }), t("Yes, delete it"), async () => {
      try { const r = await api("DELETE", hostPath(`/teams/${encodeURIComponent(team.name)}`, host), { confirm: true }); toast("success", t("Team {team} on {host} deleted", { team: team.name, host }), (r && r.output) || ""); show("teams"); }
      catch (e) { failed(t("Could not delete the team {team} on {host}", { team: team.name, host }))(e); }
    }, { trigger: remove }) });
    actions.append(rowButton(t("Show"), team.name, host, { onclick: () => teamCard(team, slot, host, false) }),
      rowButton(t("Post a task…"), team.name, host, { onclick: () => teamCard(team, slot, host, true) }), stop, remove);
    rows.push(el("tr", { "data-team": team.name }, el("td", { class: "mono" }, team.name), el("td", {}, team.members.join(", ")), el("td", { class: "mono" }, team.workspace || ""), actions), slot);
  }
  main.append(table([t("Team"), t("Members"), t("Workspace"), ""], rows, t("No teams on this host."), t("Teams on {host}", { host })));
  // a new team, started in a terminal of the page
  const name = el("input", { id: "team-name", class: "mono", placeholder: "review", pattern: TEAM_NAME.source.slice(1, -1) });
  const lead = el("select", { id: "team-lead" }, AGENTS.map(a => el("option", { value: a }, a)));
  const others = AGENTS.map(a => el("input", { type: "checkbox", id: `team-agent-${a}`, value: a, checked: a === "codex" }));
  const workspace = el("input", { id: "team-workspace", class: "mono", placeholder: "/home/you/src/project" });
  const options = el("textarea", { id: "team-options", rows: 2, class: "mono", placeholder: "--mode high\n--allow-pypi" });
  const status = el("div", { class: "wide" });
  const start = el("button", { class: "primary", onclick: () => {
    clear(status);
    const team = name.value.trim(), path = workspace.value.trim();
    const agents = [lead.value, ...others.filter(b => b.checked && b.value !== lead.value).map(b => b.value)];
    if (!TEAM_NAME.test(team)) { status.append(notice(t("A team's name: 1-20 of a-z, 0-9, '_' and '-', starting with a letter or digit."), "error")); return; }
    if (agents.length < 2) { status.append(notice(t("A team has two or three agents: the lead and at least one more."), "error")); return; }
    if (!path.startsWith("/") && !path.startsWith("~")) { status.append(notice(t("The shared workspace: an absolute path on {host}.", { host }), "error")); return; }
    let list;
    try { list = shellWords(options.value); } catch (e) { status.append(notice(t("Options: {error}", { error: e.message }), "error")); return; }
    openTerminal({ kind: "team-start", host, team, agents: agents.join(","), workspace: path, options: JSON.stringify(list) }, t("team {team}", { team }));
  } }, t("Start the team in a terminal"));
  main.append(el("h2", {}, t("New team on {host}", { host })), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "team-name" }, t("Name")), name,
    el("label", { for: "team-lead" }, t("Lead")), lead,
    el("span", {}, t("Members")), el("div", { class: "row" }, others.map((b, i) => el("label", {}, b, " ", AGENTS[i]))),
    el("label", { for: "team-workspace" }, t("Shared workspace")), workspace,
    el("label", { for: "team-options" }, t("Options for every member")), options,
    status, el("span"), el("div", { class: "row" }, start, el("span", { class: "hint" }, t("The members start one after another; the terminal says how each went.")))),
  el("p", { class: "hint" }, t("The members share the workspace: nothing keeps two agents from changing the same file."))));
};

// Images and tools: the images (sandbox-ai images) with their extensions and variants (packages, files, a
// Containerfile fragment; checked, changed, removed), a build in a terminal, the demo device.
const BUILD_TARGETS = ["claude", "codex", "opencode", "all", "team", "vpn"];
const IMAGE_STATES = { current: N_("current"), outdated: N_("extension changed: rebuild"), "base-changed": N_("base rebuilt: rebuild"),
  invalid: N_("extension has errors"), "not-built": N_("not built") };
const IMAGE_TARGET = /^[a-z0-9][a-z0-9_.-]{0,29}$/;

// A file of an extension as the form takes it: SOURCE:TARGET[:MODE], SOURCE on that host.
function imageFile(line) {
  const parts = line.split(":");
  if (parts.length < 2 || parts.length > 3 || !parts[0].startsWith("/") || !parts[1].startsWith("/") || (parts[2] && !/^0?[0-7]{3}$/.test(parts[2]))) throw new Error(t("A file is SOURCE:TARGET or SOURCE:TARGET:MODE (absolute paths, MODE like 0755): {line}", { line }));
  return Object.assign({ source: parts[0], target: parts[1] }, parts[2] ? { mode: parts[2] } : {});
}

// An extension's findings and final Containerfile (images check --json), into BOX.
function imageCheckView(box, r) {
  const findings = (Array.isArray(r && r.findings) ? r.findings : []).filter(f => f && typeof f === "object");
  const errorsFound = findings.filter(f => f.severity === "error").length;
  clear(box).append(...[notice(errorsFound ? t("{count} error(s): it cannot be built like this.", { count: errorsFound }) : findings.length ? t("No errors, {count} warning(s).", { count: findings.length }) : t("No findings."), errorsFound ? "error" : "ok"),
    findings.length ? el("ul", {}, findings.map(f => el("li", { class: f.severity === "error" ? "status-bad" : "" }, `${f.severity || ""}: ${f.line ? t("line {line}", { line: f.line }) : String(f.where || "")}${f.text ? ` (${f.text})` : ""}: ${f.message || ""}`))) : null,
    r && r.containerfile ? el("details", {}, el("summary", {}, t("The final Containerfile")), el("pre", {}, String(r.containerfile))) : null].filter(Boolean));
}

pages.images = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Images and tools on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  let images;
  try { images = await api("GET", hostPath("/images", host)); } catch (e) { images = e; }
  if (!live()) return;
  clear(main).append(el("h1", {}, t("Images and tools on {host}", { host })));
  const listed = (Array.isArray(images) ? images : []).filter(x => x && typeof x === "object" && typeof x.target === "string" && IMAGE_TARGET.test(x.target));
  const output = el("div");
  const shown = (title, node) => { clear(output).append(el("div", { class: "card", id: "image-detail" }, el("h2", {}, title), node, el("button", { onclick: () => clear(output) }, t("Close")))); revealCard(output.children[0]); };
  const rows = listed.map(x => {
    const actions = el("td", { class: "actions" });
    const look = (label, title, path, draw) => rowButton(label, x.target, host, { onclick: (ev) => busy(ev.target, async () => {
      try { shown(title, draw(await api("GET", hostPath(path, host)))); } catch (e) { shown(title, errorNotice(e)); }
    }) });
    actions.append(
      look(t("Packages"), t("Packages in {image} on {host}", { image: x.target, host }), `/images/${encodeURIComponent(x.target)}/packages`, (r) => dataView(r, t("No packages listed (is it built?)."))),
      look(t("Extension"), t("Extension of {image} on {host}", { image: x.target, host }), `/images/${encodeURIComponent(x.target)}`, (r) => {
        const box = el("div");
        imageCheckView(box, r);
        const list = (key) => (Array.isArray(r && r[key]) ? r[key] : []);
        const rows = [...["apt", "npm", "pip"].map(k => [k, list(k).join(" ")]), ["files", list("files").map(f => `${f.source} → ${f.target} (${f.mode})`).join(", ")]];
        return el("div", {}, table([t("Kind"), t("What")], rows.map(([k, v]) => el("tr", {}, el("td", { class: "mono" }, k), el("td", { class: "mono" }, v || "-")))), box);
      }),
      look(t("History"), t("History of {image} on {host}", { image: x.target, host }), `/images/${encodeURIComponent(x.target)}/history`, (r) => dataView(r, t("No changes or builds yet."))));
    if (x.extended || x.variant) {
      const remove = rowButton(t("Remove…"), x.target, host, { class: "danger", onclick: () => {
        const keep = el("input", { type: "checkbox", id: `image-keep-${x.target}` });
        confirmIn(actions, el("span", {}, x.variant ? t("Remove the variant {image} on {host} with its extension? Its image goes too, unless kept.", { image: x.target, host })
          : t("Remove the extension of {image} on {host}? The next build is the agent's image without it.", { image: x.target, host }),
        x.variant ? el("label", {}, " ", keep, " ", t("keep its image")) : null), t("Yes, remove it"), async () => {
          try { const r = await api("POST", hostPath(`/images/${encodeURIComponent(x.target)}/remove`, host), { keep_image: Boolean(keep.checked), confirm: true }); toast("success", t("{image} on {host} removed", { image: x.target, host }), (r && r.output) || ""); show("images"); }
          catch (e) { failed(t("Could not remove {image} on {host}", { image: x.target, host }))(e); }
        }, { trigger: remove });
      } });
      actions.append(remove);
    }
    const size = typeof x.size === "number" && x.size > 0 ? readableSize(x.size) : "";
    return el("tr", {}, el("td", { class: "mono" }, x.target), el("td", {}, x.variant ? t("variant of {agent}", { agent: x.agent || "?" }) : x.extended ? t("extended") : t("as built")),
      el("td", { class: x.status === "current" ? "status-ok" : x.status === "invalid" ? "status-fail" : "status-warn" }, IMAGE_STATES[x.status] ? t(IMAGE_STATES[x.status]) : String(x.status || "")),
      el("td", { class: "mono" }, String(x.agent_version || "")), el("td", {}, String(x.created || "").replace("T", " ")), el("td", {}, size), actions);
  });
  main.append(el("h2", {}, t("Images")), images instanceof Error ? errorNotice(images)
    : el("div", { class: "table-scroll" }, table([t("Image"), t("Kind"), t("State"), t("Agent version"), t("Built"), t("Size"), ""], rows, t("No images yet: build one below."), t("Images on {host}", { host }))), output);
  // the extension of an agent's image or a variant: what it adds (packages, host files, a Containerfile fragment)
  const targets = [...new Set([...AGENTS, ...listed.map(x => x.target)])];
  const target = el("select", { id: "image-target" }, targets.map(v => el("option", { value: v }, v)));
  const field = (id, placeholder) => el("input", { id, class: "mono", placeholder });
  const adds = { apt: field("image-add-apt", "ffmpeg jq"), npm: field("image-add-npm", "typescript"), pip: field("image-add-pip", "ruff") };
  const drops = { apt: field("image-remove-apt", ""), npm: field("image-remove-npm", ""), pip: field("image-remove-pip", "") };
  const files = el("textarea", { id: "image-add-files", rows: 2, class: "mono", placeholder: "/home/you/tool.sh:/usr/local/bin/tool:0755" });
  const dropFiles = field("image-remove-files", "/usr/local/bin/tool");
  const fragment = el("textarea", { id: "image-fragment", rows: 4, class: "mono", placeholder: "RUN apt-get update && apt-get install -y --no-install-recommends tree" });
  const clearFragment = el("input", { id: "image-clear-fragment", type: "checkbox" });
  const extendStatus = el("div", { class: "wide" }), extendRow = el("div", { class: "row" });
  const changes = () => {
    const body = {};
    for (const kind of ["apt", "npm", "pip"]) {
      if (wordsOf(adds[kind].value).length) body[`add_${kind}`] = wordsOf(adds[kind].value);
      if (wordsOf(drops[kind].value).length) body[`remove_${kind}`] = wordsOf(drops[kind].value);
    }
    const added = linesOf(files.value).map(imageFile);
    if (added.length) body.add_files = added;
    if (wordsOf(dropFiles.value).length) body.remove_files = wordsOf(dropFiles.value);
    if (clearFragment.checked) body.clear_fragment = true;
    else if (textOf(fragment)) body.fragment = fragment.value;
    return body;
  };
  const check = el("button", { onclick: (ev) => busy(ev.target, async () => {
    clear(extendStatus);
    try { imageCheckView(extendStatus, await api("POST", hostPath(`/images/${encodeURIComponent(target.value)}/check`, host), textOf(fragment) && !clearFragment.checked ? { fragment: fragment.value } : {})); }
    catch (e) { extendStatus.append(errorNotice(e)); }
  }) }, t("Check"));
  const save = el("button", { class: "primary", onclick: () => {
    clear(extendStatus);
    let body;
    try { body = changes(); } catch (e) { extendStatus.append(notice(e.message, "error")); return; }
    if (!Object.keys(body).length) { toast("warning", t("Nothing to change"), t("Name packages, files or a fragment.")); return; }
    const image = target.value;  // the image the question names is the one changed
    const send = (confirm) => busy(save, async () => {
      try {
        const r = await api("POST", hostPath(`/images/${encodeURIComponent(image)}/extend`, host), Object.assign({}, body, confirm ? { confirm: true } : {}));
        imageCheckView(extendStatus, r);
        extendStatus.prepend(notice(t("{image} on {host} changed: {changes}. Build it to use it.", { image, host, changes: (Array.isArray(r && r.changes) ? r.changes : []).join(", ") || t("nothing") }), "ok"));
      } catch (e) { extendStatus.append(errorNotice(e)); }
    });
    if (!body.add_files) { send(false); return; }
    confirmIn(extendRow, t("Build {files} of {host} into {image}? Every later start of that image can read them.", { files: body.add_files.map(f => f.source).join(", "), host, image }), t("Yes, add them"), () => send(true), { kind: "primary", trigger: save });
  } }, t("Save the changes…"));
  extendRow.append(save, check);
  main.append(el("h2", {}, t("Change an extension")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "image-target" }, t("Image")), target,
    el("label", { for: "image-add-apt" }, t("Add Debian packages")), adds.apt, el("label", { for: "image-remove-apt" }, t("Remove Debian packages")), drops.apt,
    el("label", { for: "image-add-npm" }, t("Add npm packages")), adds.npm, el("label", { for: "image-remove-npm" }, t("Remove npm packages")), drops.npm,
    el("label", { for: "image-add-pip" }, t("Add Python tools")), adds.pip, el("label", { for: "image-remove-pip" }, t("Remove Python tools")), drops.pip,
    el("label", { for: "image-add-files" }, t("Add files of that host (one per line)")), files, el("label", { for: "image-remove-files" }, t("Remove files")), dropFiles,
    el("label", { for: "image-fragment" }, t("Containerfile fragment (replaces the saved one)")), fragment, el("label", { for: "image-clear-fragment" }, t("Clear the fragment")), clearFragment,
    extendStatus, el("span"), extendRow),
  el("p", { class: "hint" }, t("Each change is checked on that host (no FROM, no setuid, nothing the sandbox's setup relies on); then build the image."))));
  // a new variant, empty or from the agent's extension, or from a recipe
  const variantName = el("input", { id: "image-new-name", class: "mono", placeholder: "data", pattern: "[a-z0-9][a-z0-9_.-]{0,29}" });
  const variantFrom = el("select", { id: "image-new-from" }, AGENTS.map(a => el("option", { value: a }, a)));
  const empty = el("input", { id: "image-new-empty", type: "checkbox" });
  const recipe = el("select", { id: "image-recipe" }, [["media", N_("media: ffmpeg, PulseAudio, espeak-ng, Piper …")], ["tor", N_("tor: torsocks, torbrowser-launcher")]].map(([v, label]) => el("option", { value: v }, t(label))));
  const recipeAs = el("input", { id: "image-recipe-as", class: "mono", placeholder: t("the recipe's name") });
  const variantStatus = el("div", { class: "wide" });
  const variant = (path, body, done) => async (ev) => busy(ev.target, async () => {
    clear(variantStatus);
    try { const r = await api("POST", hostPath(path, host), body()); toast("success", done(), (r && (r.output || r.image)) || ""); show("images"); }
    catch (e) { variantStatus.append(errorNotice(e)); }
  });
  main.append(el("h2", {}, t("New variant")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "image-new-name" }, t("Name")), variantName, el("label", { for: "image-new-from" }, t("From")), variantFrom,
    el("label", { for: "image-new-empty" }, t("Empty (not from the agent's extension)")), empty,
    el("span"), el("button", { class: "primary", onclick: variant("/images-new", () => ({ name: variantName.value.trim(), from: variantFrom.value, empty: empty.checked }), () => t("Variant {image} made on {host}", { image: variantName.value.trim(), host })) }, t("Make the variant")),
    el("label", { for: "image-recipe" }, t("Or from a recipe")), recipe, el("label", { for: "image-recipe-as" }, t("Its name")), recipeAs,
    variantStatus, el("span"), el("button", { onclick: variant("/images-recipe", () => ({ recipe: recipe.value, from: variantFrom.value, as: recipeAs.value.trim() }), () => t("Variant {image} made on {host}", { image: recipeAs.value.trim() || recipe.value, host })) }, t("Make it from the recipe"))),
  el("p", { class: "hint" }, t("A variant is started with {option}.", { option: "--image-variant NAME" }))));
  // what was changed and built, on that host
  const historyBox = el("div");
  main.append(el("h2", {}, t("Changes and builds")), el("div", { class: "row" }, el("button", { onclick: (ev) => busy(ev.target, async () => {
    try { clear(historyBox).append(dataView(await api("GET", hostPath("/images-history", host)), t("No changes or builds yet."))); } catch (e) { clear(historyBox).append(errorNotice(e)); }
  }) }, t("Show the history"))), historyBox);
  // a build in a terminal: the agents, all, team, vpn, and the variants (images --json: "target", "variant": true)
  const variants = listed.filter(x => x.variant && !BUILD_TARGETS.includes(x.target)).map(x => x.target);
  const buildTarget = el("select", { id: "build-target" }, [...BUILD_TARGETS, ...new Set(variants)].map(v => el("option", { value: v }, v)));
  const only = el("input", { id: "build-only", type: "checkbox" });
  main.append(el("h2", {}, t("Build")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "build-target" }, t("Image")), buildTarget, el("label", { for: "build-only" }, t("Only the extension layers")), only,
    el("span"), el("button", { class: "primary", onclick: () => openTerminal({ kind: "build", host, target: buildTarget.value, extension_only: only.checked ? "1" : "" }, t("build {target}", { target: buildTarget.value })) }, t("Build in a terminal"))),
  el("p", { class: "hint" }, t("Each build takes the newest releases of the agent and its base images."))));
  // the demo device: an emulated serial device to practise hardware analysis on (demo_device.DEFAULT_PORT: 4000)
  const status = el("div"), port = el("input", { id: "demo-port", type: "number", min: 1024, max: 65535, placeholder: "4000" });
  const refresh = async () => { try { const r = await api("GET", hostPath("/demo-device", host)); clear(status).append(notice(r.output || "", r.ok ? "ok" : "")); } catch (e) { clear(status).append(errorNotice(e)); } };
  const act = (path, body) => (event) => busy(event.target, async () => { try { const r = await api("POST", hostPath(path, host), body()); toast("success", t("Demo device"), (r && r.output) || ""); } catch (e) { failed(t("Demo device"))(e); } await refresh(); });
  main.append(el("h2", {}, t("Demo device")), el("div", { class: "card" }, status, el("div", { class: "form" }, el("label", { for: "demo-port" }, t("Port")), port,
    el("span"), el("div", { class: "row" }, el("button", { class: "primary", onclick: act("/demo-device/start", () => (port.value ? { port: Number(port.value) } : {})) }, t("Start")),
      el("button", { onclick: act("/demo-device/stop", () => ({})) }, t("Stop")), el("button", { onclick: () => openTerminal({ kind: "demo-device", host, port: port.value }, t("demo device")) }, t("Start in a terminal")), el("button", { onclick: refresh }, t("Refresh"))))));
  refresh();
};

// Activity (sandbox-ai activity --json): the last events of the runs, filtered; Follow asks again every few seconds.
const ACTIVITY_KINDS = ["prompt", "reply", "tool", "command", "file", "network", "approval", "mcp", "session", "tokens"];
pages.activity = async (main) => {
  const host = state.host;
  const run = el("input", { id: "activity-run", class: "mono", placeholder: t("every run") }), tail = el("input", { id: "activity-tail", type: "number", min: 1, max: 2000, value: 100 });
  const grep = el("input", { id: "activity-grep" }), since = el("input", { id: "activity-since", placeholder: "2h" });
  const kinds = ACTIVITY_KINDS.map(k => el("input", { type: "checkbox", id: `activity-${k}`, value: k }));
  const follow = el("input", { id: "activity-follow", type: "checkbox" });
  const box = el("div");
  const load = async () => {
    let r;
    try { r = await api("POST", hostPath("/activity", host), { run: run.value.trim(), tail: Number(tail.value) || 100, kinds: kinds.filter(k => k.checked).map(k => k.value), grep: grep.value.trim(), since: since.value.trim() }, { background: follow.checked }); }
    catch (e) { clear(box).append(errorNotice(e)); return; }
    const events = Array.isArray(r.events) ? r.events : [];
    clear(box).append(table([t("Time"), t("Run"), t("Kind"), t("What")], events.slice().reverse().map(e => el("tr", {}, el("td", {}, String(e.time || "").replace("T", " ").replace("Z", "")),
      el("td", { class: "mono" }, String(e.run || "")), el("td", {}, String(e.kind || "")), el("td", { class: "mono" }, String(e.summary || e.text || e.command || e.path || JSON.stringify(e))))), t("No activity found.")));
  };
  const tick = () => { if (state.page !== "activity" || state.host !== host || !box.isConnected) return; if (follow.checked) load().finally(() => setTimeout(tick, 5000)); else setTimeout(tick, 5000); };
  clear(main).append(el("h1", {}, t("Activity on {host}", { host })), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "activity-run" }, t("Run")), run, el("label", { for: "activity-tail" }, t("Last events")), tail,
    el("span", {}, t("Kinds")), el("div", { class: "row" }, kinds.map((k, i) => el("label", {}, k, " ", ACTIVITY_KINDS[i]))),
    el("label", { for: "activity-grep" }, t("Containing")), grep, el("label", { for: "activity-since" }, t("Since")), since,
    el("label", { for: "activity-follow" }, t("Follow (every 5 seconds)")), follow,
    el("span"), el("button", { class: "primary", onclick: (event) => busy(event.target, load) }, t("Show")))), box);
  await load();
  setTimeout(tick, 5000);
};

// --- help: the desktop app's help about sandbox-ai itself (help.json, exported from the app's help_content.py by
// scripts/export_web_help.py; a test keeps it current), in the page's language. Each page's heading has a (?) that
// opens the sections about what that page does; the Help page shows all of them.
const HELP_FOR = {
  starts: ["sessions", "sandbox", "security-modes", "workspaces-mounts", "network", "presets", "limits", "devices"],
  approvals: ["approvals", "taint", "network", "proxy"], terminals: ["sessions"], doctor: ["sandbox", "releases-update"],
  logs: ["logs", "privacy-check"], llm: ["llm"], vpn: ["vpn-tor"], kits: ["kits"], canaries: ["canaries"], secrets: ["secrets"],
  logins: ["secrets", "proxy"], projects: ["workspaces-mounts", "rulesets", "policy"], mcp: ["mcp"], presets: ["presets"], teams: ["teams", "mcp"],
  images: ["display", "devices", "releases-update"], activity: ["logs", "memory-watch"], settings: ["security-modes", "canaries", "policy", "logs"],
  hosts: ["hosts", "web", "releases-update"],
};

async function helpSections() {
  if (!state.help) {
    const data = await (await fetch("/static/help.json", { cache: "no-cache" })).json();
    state.help = (Array.isArray(data && data.sections) ? data.sections : []).filter(x => x && typeof x.id === "string");
  }
  return state.help;
}

function inLanguage(text) { return text && typeof text === "object" ? String(text[LANGUAGE] || text.en || "") : String(text || ""); }

// A help text as nodes: paragraphs (a blank line between), "- " lines as a list, `code` as code; all of it text.
function helpBody(text) {
  const inline = (line) => String(line).split("`").map((part, i) => (i % 2 ? el("code", {}, part) : part));
  return String(text).split("\n\n").map(paragraph => {
    const lines = paragraph.split("\n"), nodes = [];
    let list = null;
    for (const line of lines) {
      if (line.startsWith("- ")) { if (!list) { list = el("ul"); nodes.push(list); } list.append(el("li", {}, inline(line.slice(2)))); }
      else { list = null; nodes.push(el("p", {}, inline(line))); }
    }
    return nodes;
  }).flat();
}

// The (?) beside a page's heading (after it, not in it: the heading stays the page's name): Help with that page's
// sections first and open.
function addHelpLink(main, page) {
  const heading = main && typeof main.querySelector === "function" ? main.querySelector("h1") : null;
  if (!heading || !HELP_FOR[page] || typeof heading.after !== "function") return;
  const next = heading.nextElementSibling;
  if (next && next.classList && next.classList.contains("help-link")) return;
  heading.classList.add("with-help");
  heading.after(el("button", { class: "small help-link", title: t("Help for this page"), "aria-label": t("Help for this page"), onclick: () => { state.helpFor = page; show("help"); } }, "?"));
}

pages.help = async (main) => {
  const from = state.helpFor && HELP_FOR[state.helpFor] ? state.helpFor : null;
  const label = from ? (PAGES.find(([key]) => key === from) || [null, from])[1] : null;
  main.append(el("h1", {}, t("Help")), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  let sections;
  try { sections = await helpSections(); } catch (e) { if (live()) clear(main).append(el("h1", {}, t("Help")), notice(t("The help could not be loaded: {error}", { error: e.message }), "error")); return; }
  if (!live()) return;
  const wanted = from ? HELP_FOR[from] : [];
  const byId = new Map(sections.map(x => [x.id, x]));
  const ordered = [...wanted.map(id => byId.get(id)).filter(Boolean), ...sections.filter(x => !wanted.includes(x.id))];
  const boxes = new Map();
  const open = (id) => { const box = boxes.get(id); if (!box) return; box.setAttribute("open", ""); revealCard(box); };
  clear(main).append(el("h1", {}, t("Help")),
    el("p", { class: "hint" }, from ? t("What the page {page} is about first; below it, everything else about sandbox-ai.", { page: t(label) }) : t("What sandbox-ai does, and what each option and command means. The same help as in the desktop app.")),
    from ? el("div", { class: "row" }, el("button", { onclick: () => show(from) }, t("Back to {page}", { page: t(label) })), el("button", { onclick: () => { state.helpFor = null; show("help"); } }, t("All sections"))) : null);
  for (const x of ordered) {
    const box = el("details", { class: "card help-section", id: `help-${x.id}`, open: wanted.includes(x.id) },
      el("summary", {}, el("h2", { class: "help-title" }, inLanguage(x.title))),
      helpBody(inLanguage(x.body)),
      (x.cli || []).length ? el("p", { class: "hint" }, t("Commands and options:"), " ", (x.cli || []).map((c, i) => [i ? ", " : "", el("code", {}, String(c))])) : null,
      (x.see || []).filter(id => byId.has(id)).length ? el("p", { class: "hint" }, t("See also:"), " ", x.see.filter(id => byId.has(id)).map(id => el("button", { class: "small", onclick: () => open(id) }, inLanguage(byId.get(id).title)))) : null);
    boxes.set(x.id, box);
    main.append(box);
  }
};

loadThemes().then(render);
