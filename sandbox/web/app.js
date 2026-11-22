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
  boxes: null, terminalCount: 0, renders: 0, polling: false, pollAgain: false, policy: null, session: null, loginMessage: null,
  cards: [], shown: null,
  renderToken: 0,  // counts the pages drawn: a drawing that awaited an answer goes on only while it is still the last
  installing: {},  // host -> when its install began (it may outlast the page that started it)
  startsEvery: (() => { try { return localStorage.getItem("sandbox-ai-starts-every") === "1"; } catch (e) { return false; } })(),
  review: null,  // {host, path}: a project configuration to review (a start refused it as untrusted)
  help: null, helpFor: null,  // help.json's sections once loaded; the page whose (?) opened Help
  formHost: null,  // the host of the new start's form, when another than the table's was chosen there
  // safeguards: the records seen per host (the first look only notes them), every other poll asks the host
  safeguardsSeen: new Set(), safeguardsPrimed: new Set(), safeguardTick: 0, safeguardsNotifier: null, safeguardMuted: {},
};
const AGENTS = ["claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"];
const PAGES = [
  ["starts", N_("Starts and sessions")], ["approvals", N_("Approvals")], ["terminals", N_("Terminals")],
  ["doctor", N_("Doctor")], ["logs", N_("Logs")], ["llm", N_("LLM servers")], ["vpn", N_("VPN profiles")], ["bridges", N_("Bridges")], ["kits", N_("Kits")], ["canaries", N_("Canaries")], ["safeguards", N_("Safeguards")], ["reviews", N_("Workspace review")], ["pins", N_("MCP tool pins")], ["secrets", N_("Secrets")], ["logins", N_("Logins")], ["projects", N_("Projects")], ["mcp", N_("MCP servers")],
  ["presets", N_("Presets")], ["teams", N_("Teams")], ["images", N_("Images and tools")], ["activity", N_("Activity")], ["monitor", N_("System monitor")], ["settings", N_("Defaults")], ["hosts", N_("Hosts")],
  ["help", N_("Help")],
];
// The pages that show one host's things: the address names the host too (#doctor@ct101).
const HOST_PAGES = ["starts", "doctor", "logs", "llm", "vpn", "kits", "canaries", "reviews", "pins", "secrets", "logins", "projects", "mcp", "presets", "teams", "images", "activity", "monitor", "settings"];

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
  "host-too-old": N_("sandbox-ai {version} on {host} does not know {what} yet: update it on the page Hosts (Update, or Update from this machine's copy)."),
  "mcp-unsigned-catalog": N_("{entry} was not added on {host}: its catalog is not signed by a trusted key."),
  "mcp-unpublished": N_("{entry} was not added on {host}: its catalog has no signed image of it yet."),
  "mcp-build-asked": N_("{entry} would be built on {host} from {repository} at the commit {commit} ({path}; source archive SHA-256 {archive_sha256}) and added unsigned."),
  "mcp-unpublished-start": N_("{names}: the catalog on {host} has no signed image of it yet, so the start does not go on."),
  "shared-wildcard": N_("{wildcards} lies below {suffix}, a shared service where anyone can rent a name: the agent could open any server anyone rents below {suffix}, and send data there."),
};

// E's text in the page's language: the page's own words for a known code, else its message.
function errorText(e) {
  if (e instanceof ApiError && e.code && Object.prototype.hasOwnProperty.call(ERROR_TEXTS, e.code)) {
    const values = {};
    for (const [key, value] of Object.entries(e.params || {})) values[key] = String(value ?? "");
    for (const name of (ERROR_TEXTS[e.code].match(/\{(\w+)\}/g) || [])) if (!(name.slice(1, -1) in values)) values[name.slice(1, -1)] = "?";
    return t(ERROR_TEXTS[e.code], values).replace(/ {2,}/g, " ");  // a value not known ("") leaves no gap
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

// A field for the NAME of a stored secret (KIND "secret") or of a VPN profile ("vpn") offers what HOST has: a
// <datalist> (one per kind and host) read when a page draws such a field (GET /secrets: names only, never a value;
// GET /vpn: the profiles), so a profile or secret added or removed shows the next time. Any other name typed stays
// possible; a host that cannot list them (an older one) leaves the field typed in. The answer fills that host's list
// only, so a late one never reaches another host's field.
// session: the running agent sessions of that host (a preset's bridge joins one; bridgeSessions' choice)
const NAME_LISTS = { secret: ["/secrets", (x) => x && x.name], vpn: ["/vpn", (x) => x && x.profile],
  session: ["/starts", (x) => x && x.state === "running" && (x.kind === "session" || x.kind === "run") && !x.helper_of && x.name] };
const NAME_PATTERNS = { secret: /^[A-Za-z_][A-Za-z0-9_]{0,63}$/, vpn: /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/, session: /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/ };
function offerNames(input, kind, host) {
  host = host || state.host;
  const id = `names-${kind}-${String(host).replace(/[^A-Za-z0-9_.-]/g, "_")}`;
  let list = document.getElementById(id);
  if (!list) { list = el("datalist", { id }); document.body.append(list); }
  input.setAttribute("list", id);
  input.setAttribute("autocomplete", "off");
  if (!list.reading) {
    const [path, nameOf] = NAME_LISTS[kind];
    list.reading = api("GET", hostPath(path, host)).then((found) => {
      const names = [...new Set((Array.isArray(found) ? found : []).map(nameOf).filter(n => typeof n === "string" && NAME_PATTERNS[kind].test(n)))].sort();
      clear(list).append(...names.map(n => el("option", { value: n })));
    }).catch(() => {}).then(() => { list.reading = null; });
  }
  return input;
}

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

// --- icon buttons: the desktop app's drawings of the Sessions page's rows (button_icons.py SVGS - the same lines on a
// 24x24 grid in currentColor; its tests check that they are the same), made as SVG elements, never as markup. A
// button shows its icon alone by default, its text the first line of its tooltip and inside its accessible name;
// "Show button texts beside the icons" (kept in this browser, as the app's setting button_texts) shows the texts too.
const BUTTON_ICONS = {"attach":[["rect",{"x":"2.5","y":"7.5","width":"13","height":"13","rx":"2"}],["path",{"d":"M5.5 11.5l2.5 2-2.5 2M10 16h3"}],["path",{"d":"M14 3.5h6.5V10M20.5 3.5L13 11"}]],"shell":[["path",{"d":"M4 6l6 6-6 6"}],["path",{"d":"M12.5 18.5h8"}]],"pause":[["path",{"d":"M8.5 5.5v13M15.5 5.5v13"}]],"resume":[["path",{"d":"M7.5 5l11 7-11 7z"}]],"delete":[["path",{"d":"M4 6.5h16M9.5 6.5V4h5v2.5"}],["path",{"d":"M6 6.5l1 13.5h10l1-13.5"}],["path",{"d":"M10 10.5v6M14 10.5v6"}]],"resume-with":[["path",{"d":"M3.5 5l10 7-10 7z"}],["path",{"d":"M20 9v6M17 12h6"}]],"export":[["path",{"d":"M12 15V3.5M7 8.5l5-5 5 5"}],["path",{"d":"M4 13.5V19a1.5 1.5 0 0 0 1.5 1.5h13A1.5 1.5 0 0 0 20 19v-5.5"}]],"checkpoints":[["path",{"d":"M3.5 12A8.5 8.5 0 1 0 6 6"}],["path",{"d":"M3 3.5V8h4.5"}],["path",{"d":"M12 7.5V12l3 2"}]],"memory":[["path",{"d":"M5 5.5A2.5 2.5 0 0 1 7.5 3H19v15H7.5A2.5 2.5 0 0 0 5 20.5z"}],["path",{"d":"M5 20.5A2.5 2.5 0 0 1 7.5 18"}],["path",{"d":"M11 3v7l2-1.5 2 1.5V3"}]],"files":[["path",{"d":"M13.5 2.5H6A1.5 1.5 0 0 0 4.5 4v16A1.5 1.5 0 0 0 6 21.5h12a1.5 1.5 0 0 0 1.5-1.5V8.5z"}],["path",{"d":"M13.5 2.5v6h6"}],["path",{"d":"M8 13h8M8 17h5"}]],"storage":[["ellipse",{"cx":"12","cy":"5.5","rx":"7.5","ry":"2.5"}],["path",{"d":"M4.5 5.5v13c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5v-13"}],["path",{"d":"M4.5 12c0 1.4 3.4 2.5 7.5 2.5s7.5-1.1 7.5-2.5"}]],"optimize":[["path",{"d":"M15.5 2.5L11 11.5"}],["path",{"d":"M6 11.5h9.5l1.5 9H4.5z"}],["path",{"d":"M8.5 15v5.5M12.5 15v5.5"}]],"allow":[["circle",{"cx":"9","cy":"12","r":"6.5"}],["path",{"d":"M2.5 12h13"}],["path",{"d":"M9 5.5c2.2 2.2 2.2 10.8 0 13M9 5.5c-2.2 2.2-2.2 10.8 0 13"}],["path",{"d":"M20 9v6M17 12h6"}]],"limits":[["path",{"d":"M3.5 6h9M17.5 6h3M3.5 12h3M11.5 12h9M3.5 18h11M19.5 18h1"}],["circle",{"cx":"15","cy":"6","r":"2.2"}],["circle",{"cx":"9","cy":"12","r":"2.2"}],["circle",{"cx":"17","cy":"18","r":"2.2"}]]};
const BUTTON_TEXTS_STORE = "sandbox-ai-button-texts";
function buttonTextsShown() { try { return localStorage.getItem(BUTTON_TEXTS_STORE) === "1"; } catch (e) { return false; } }
function showButtonTexts(on) {
  try { localStorage.setItem(BUTTON_TEXTS_STORE, on ? "1" : ""); } catch (e) { /* this page only */ }
  document.body.classList.toggle("button-texts", Boolean(on));
}
function svgIcon(key) {
  const svg = document.createElementNS(SVG_NS, "svg");
  for (const [k, v] of [["viewBox", "0 0 24 24"], ["width", "18"], ["height", "18"], ["aria-hidden", "true"], ["focusable", "false"], ["class", "button-icon"]]) svg.setAttribute(k, v);
  const lines = document.createElementNS(SVG_NS, "g");
  for (const [k, v] of [["fill", "none"], ["stroke", "currentColor"], ["stroke-width", "2"], ["stroke-linecap", "round"], ["stroke-linejoin", "round"]]) lines.setAttribute(k, v);
  for (const [tag, attrs] of BUTTON_ICONS[key] || []) {
    const part = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs)) part.setAttribute(k, v);
    lines.append(part);
  }
  svg.append(lines);
  return svg;
}
// KEY's icon in BUTTON (made with its LABEL as text): the label beside it only with the texts shown; the tooltip
// starts with the label, above what it does (TIP).
function withIcon(button, key, label, tip) {
  button.setAttribute("title", tip ? `${label}\n${tip}` : label);
  button.setAttribute("data-icon", key);
  button.classList.add("icon-button");
  clear(button).append(svgIcon(key), el("span", { class: "button-text" }, label));
  return button;
}
// A start's row button with KEY's icon (rowButton's accessible name; ATTRS.title: what it does).
function iconRowButton(key, label, name, host, attrs) {
  const tip = attrs && attrs.title;
  return withIcon(rowButton(label, name, host, attrs), key, label, tip);
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

// What a command that succeeded said: its output, as a success - or, when the launcher warned (you decide what a
// sandbox gets: a host path you chose with a risk, HOME, sandbox-ai's own folders, a kernel tree, is given and the
// launcher says what it means), its warnings first, as a warning that stays until it is closed.
function doneToast(title, r) {
  const warnings = r && Array.isArray(r.warnings) ? r.warnings.map(String) : [];
  const output = (r && r.output) || "";
  if (!warnings.length) { toast("success", title, output); return; }
  toast("warning", title, `${t("You decide what a sandbox gets: it is given, and the launcher says what it means (read-only is the safer choice):")}\n${warnings.map(w => `• ${w}`).join("\n")}${output ? `\n\n${output}` : ""}`, { seconds: 0 });
}

function toast(level, title, text, options) {
  if (!Object.prototype.hasOwnProperty.call(TOAST_LEVELS, level)) level = "info";
  const opts = options || {};
  let box = document.getElementById("toasts");
  if (!box) { box = el("div", { id: "toasts", class: "toasts" }); document.body.append(box); }  // each toast has its own role
  const [name, sign] = TOAST_LEVELS[level];
  // An error stays until closed, unless its caller says how long (an approval's: a short notice, also in its page)
  const seconds = level === "error" && opts.seconds === undefined ? 0 : (opts.seconds ?? TOAST_SECONDS);
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

// --- safeguards: what refused or ended something in a sandbox (sandbox-ai safeguards on that host) ------------
// A host's new records become toasts, grouped and rate-limited as the launcher's desktop notifications are
// (safeguards.py's Notifier): the first of a run, kind and place at once, those that follow within a minute as one
// ("12 connections to x.example dropped by the firewall in the last minute"), at most SAFEGUARD_RATE warnings a
// minute (errors always: a canary, an out-of-memory kill, a failed check), the rest summed up later. Kinds muted
// in the host's defaults (safeguard_mute) never pop up; the Safeguards page lists them all.

const SAFEGUARD_WINDOW = 60000;
const SAFEGUARD_RATE = 4;
const SAFEGUARD_TITLES = {
  firewall: N_("connection dropped by the firewall"), dns: N_("name lookup refused"), network: N_("connection refused by the proxy"),
  rule: N_("refused by a rule"), "approval-timeout": N_("refused without an answer"), push: N_("push refused by the privacy check"),
  publish: N_("publishing refused"), mode: N_("refused by the security mode"), policy: N_("refused by the administrator's policy"),
  taint: N_("held: the run is tainted"), device: N_("device refused"), memory: N_("process ended: memory nearly full"),
  "supervisor-memory": N_("the supervisor dropped its caches"), guard: N_("a safety check failed"), canary: N_("a canary went off"),
  oom: N_("out of memory: the kernel ended a process"),
  "mcp-server": N_("an MCP server's process ended or failed"), loop: N_("loop guard intervened"),
  resources: N_("resource warning"), "hidden-text": N_("hidden text in what the agent reads"),
  "workspace-review": N_("the agent changed a file your host runs or trusts"),
  package: N_("package guard"),
  "secret-send": N_("a secret on its way to another host"), "secret-context": N_("a secret entered the model's context"),
};
const SAFEGUARD_GROUPED = {
  firewall: N_("{count} connections to {target} dropped by the firewall in the last minute"),
  dns: N_("{count} lookups of {target} refused in the last minute"),
  network: N_("{count} connections to {target} refused in the last minute"),
  rule: N_("{count} actions refused by {target} in the last minute"),
  "approval-timeout": N_("{count} questions about {target} refused without an answer in the last minute"),
  push: N_("{count} pushes to {target} refused in the last minute"),
  publish: N_("{count} publishing requests to {target} refused in the last minute"),
  "hidden-text": N_("hidden text found {count} times in the last minute ({target})"),
  "workspace-review": N_("{count} workspace reviews with changes in the last minute ({target})"),
  package: N_("{count} package guard findings ({target}) in the last minute"),
  "secret-send": N_("{count} requests carried a secret to {target} in the last minute"),
  "secret-context": N_("secrets entered the model's context {count} times in the last minute ({target})"),
};

function safeguardTitle(kind) { return SAFEGUARD_TITLES[kind] ? t(SAFEGUARD_TITLES[kind]) : String(kind); }

function safeguardFix(r) { return r.fix ? (r.stays ? t("Stays refused: {fix}", { fix: r.fix }) : t("To allow it: {fix}", { fix: r.fix })) : null; }

class SafeguardNotifier {
  constructor(send, clock) {
    this.send = send; this.clock = clock || (() => Date.now());
    this.groups = new Map(); this.sent = []; this.held = new Map(); this.muted = new Set();
  }
  add(r) {
    if (!r || !Object.prototype.hasOwnProperty.call(SAFEGUARD_TITLES, r.kind) || r.announced || this.muted.has(r.kind)) return;
    const now = this.clock(), key = JSON.stringify([r.run || null, r.kind, r.target || r.what || ""]);
    const number = Number.isInteger(r.count) && r.count > 0 ? r.count : 1;
    const group = this.groups.get(key);
    if (group) { group.fresh += number; group.total += number; group.record = r; return; }
    this.groups.set(key, { since: now, fresh: 0, total: number, record: r });
    this.emit({ level: r.level === "error" ? "error" : "warning", run: r.run, kind: r.kind, count: 1,
      title: `${r.run || state.host}: ${safeguardTitle(r.kind)}`,
      text: [r.what, r.process ? t("Process: {process}", { process: r.process }) : null, r.why ? t("Why: {why}", { why: r.why }) : null, safeguardFix(r)].filter(Boolean).join("\n") }, now);
  }
  flush(final) {
    const now = this.clock();
    for (const [key, group] of this.groups) {
      if (!final && now - group.since < SAFEGUARD_WINDOW) continue;
      if (!group.fresh) { this.groups.delete(key); continue; }
      const r = group.record;
      const what = SAFEGUARD_GROUPED[r.kind] ? t(SAFEGUARD_GROUPED[r.kind], { count: group.total, target: r.target || "?" })
        : t("{what} ({count} times in the last minute)", { what: safeguardTitle(r.kind), count: group.total });
      this.emit({ level: r.level === "error" ? "error" : "warning", run: r.run, kind: r.kind, count: group.total,
        title: `${r.run || state.host}: ${what}`, text: [r.why ? t("Why: {why}", { why: r.why }) : null, safeguardFix(r)].filter(Boolean).join("\n") }, now, true);
      Object.assign(group, { since: now, fresh: 0, total: 0 });
    }
    if (this.held.size && (final || this.room(now))) {
      let count = 0; const kinds = new Set(), runs = new Set();
      for (const [key, n] of this.held) { const [run, kind] = JSON.parse(key); count += n; kinds.add(kind); runs.add(run); }
      this.held.clear();
      const run = runs.size === 1 ? [...runs][0] : null;
      this.emit({ level: "warning", run, kind: "summary", count, title: `${run || state.host}: ${t("{count} more safeguard events", { count })}`,
        text: t("Kinds: {kinds}", { kinds: [...kinds].sort().join(", ") }) }, now, true);
    }
  }
  room(now) {
    this.sent = this.sent.filter(t => now - t < SAFEGUARD_WINDOW);
    return this.sent.length < SAFEGUARD_RATE;
  }
  emit(n, now, force) {
    if (n.level !== "error" && !force && !this.room(now)) {
      const key = JSON.stringify([n.run || null, n.kind]);
      this.held.set(key, (this.held.get(key) || 0) + n.count);
      return;
    }
    if (n.level !== "error") { this.room(now); this.sent.push(now); }
    try { this.send(n); } catch (e) { /* a toast that fails never stops the others */ }
  }
}

function safeguardsNotifier() {
  if (!state.safeguardsNotifier) state.safeguardsNotifier = new SafeguardNotifier(n =>
    toast(n.level, n.title, n.text, { action: [t("Show safeguards"), () => show("safeguards")] }));
  return state.safeguardsNotifier;
}

// Every other poll (10 s: it is a command on that host); an older sandbox-ai there has no safeguards: no toasts.
async function pollSafeguards() {
  const notifier = safeguardsNotifier();
  state.safeguardTick = (state.safeguardTick + 1) % 2;
  if (state.safeguardTick) { notifier.flush(); return; }
  const host = state.host;
  let records;
  try { records = await api("GET", hostPath("/safeguards")); } catch (e) { return; }
  if (!Array.isArray(records) || host !== state.host) return;
  const primed = state.safeguardsPrimed.has(host);
  if (!primed) {
    state.safeguardsPrimed.add(host);
    try { state.safeguardMuted[host] = new Set(((await api("GET", hostPath("/defaults"))).values || {}).safeguard_mute || []); }
    catch (e) { state.safeguardMuted[host] = new Set(); }
  }
  notifier.muted = state.safeguardMuted[host] || new Set();
  for (const r of records) {
    const key = `${host} ${r.id}`;
    if (state.safeguardsSeen.has(key)) continue;
    state.safeguardsSeen.add(key);
    if (primed && state.page !== "safeguards") notifier.add(r);
  }
  notifier.flush();
  if (state.page === "safeguards" && primed) pages.safeguards(clear(document.getElementById("main")), records);
}

// --- themes: the desktop app's ---------------------------------------------------------------------------

async function loadThemes() {
  try {
    const data = await (await fetch("/static/themes.json", { cache: "no-cache" })).json();
    state.themes = data.themes || {};
    state.fontLists = data.fonts || {};
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

// the INDEXth of a set of colours apart from each other: ACCENT first, then its hue turned on by the golden angle
function seriesColour(accent, index, dark) {
  if (index === 0) return accent;
  const hex = String(accent).replace("#", "");
  const [r, g, b] = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
  const high = Math.max(r, g, b), low = Math.min(r, g, b);
  let hue = 0.6;
  if (high !== low && [r, g, b].every(Number.isFinite)) {
    const d = high - low;
    hue = (high === r ? (g - b) / d + (g < b ? 6 : 0) : high === g ? (b - r) / d + 2 : (r - g) / d + 4) / 6;
  }
  return `hsl(${Math.round(((hue + index * 0.382) % 1) * 360)}, 65%, ${dark ? 66 : 38}%)`;
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
  // the charts' series (the system monitor): the accent first, then its hue turned on, readable on the background
  for (let i = 0; i < 8; i++) root.setProperty(`--series${i}`, seriesColour(colors.accent || "#2563eb", i, Boolean(theme.dark)));
  for (const entry of state.terminals) entry.term.options.theme = terminalTheme();
  applyFonts();
}

function terminalTheme() {
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = state.themes[state.theme] || state.themes[dark ? "dark" : "light"] || {};
  return theme.terminal || {};
}

// --- fonts: the app's lists (themes.json "fonts"), chosen in this browser; nothing is downloaded -----------

state.fonts = { ui: localStorage.getItem("sandbox-ai-font-ui") || "", mono: localStorage.getItem("sandbox-ai-font-mono") || "" };
state.fontLists = state.fontLists || {};
const GENERIC_FONTS = new Set(["serif", "sans-serif", "monospace", "system-ui", "ui-monospace", "ui-sans-serif"]);

function cssFamilies(families) {
  return [...new Set(families.filter(Boolean))].map(f => (GENERIC_FONTS.has(f) ? f : `"${f}"`)).join(", ");
}

// The font of KIND ("ui" or "mono") chosen here, if it is one of the list (else "": the theme's).
function chosenFont(kind) {
  const name = state.fonts[kind];
  return name && (state.fontLists[kind] || []).some(([family]) => family === name) ? name : "";
}

// Installed on this computer? A font that is missing renders in the fallback, so the text keeps the generic
// family's width for each of three generics. (document.fonts.check() says true for any system font name.)
// null: cannot tell here.
function fontInstalled(family) {
  try {
    const context = document.createElement("canvas").getContext("2d");
    if (!context) return null;
    const sample = "mmmmmmmmmmlli1|WQ@#0Ow";
    return ["monospace", "sans-serif", "serif"].some((generic) => {
      context.font = `72px ${generic}`;
      const fallback = context.measureText(sample).width;
      context.font = `72px "${family}", ${generic}`;
      return context.measureText(sample).width !== fallback;
    });
  } catch (e) { return null; }
}

// The chosen fonts as CSS variables, before the theme's (--font) and the page's monospace fonts (--mono, also the
// terminals'): a missing font falls back to them, at the end a generic family.
function applyFonts() {
  const root = document.documentElement.style;
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme = state.themes[state.theme] || state.themes[dark ? "dark" : "light"] || {};
  const families = (theme.font && theme.font.families) || [];
  const ui = chosenFont("ui"), mono = chosenFont("mono");
  if (families.length || ui) root.setProperty("--font", cssFamilies([ui, ...families, "sans-serif"]));
  root.removeProperty("--mono");
  const fallback = (getComputedStyle(document.documentElement).getPropertyValue("--mono") || "").trim() || "monospace";
  const stack = mono ? `${cssFamilies([mono])}, ${fallback}` : fallback;
  if (mono) root.setProperty("--mono", stack);
  for (const t of state.terminals) { t.term.options.fontFamily = stack; t.fit(); }
}

function chooseFont(kind, name) {
  state.fonts[kind] = name;
  try { localStorage.setItem(`sandbox-ai-font-${kind}`, name); } catch (e) { /* kept for this page only */ }
  applyFonts();
}

// Two lists in the header: the theme's font or one of the app's; one that is not installed here is marked and
// says how to get it.
function fontPicker() {
  const hint = el("p", { class: "hint" });
  const showHint = () => {
    const missing = ["ui", "mono"].map(kind => [kind, state.fonts[kind]]).filter(([kind, name]) => name && chosenFont(kind) && fontInstalled(name) === false);
    clear(hint).append(...missing.map(([kind, name]) => el("span", { class: "font-missing" },
      t("{name} is not installed on this computer: the theme's font is used until it is. To get it: {how}", { name, how: howToGet(kind, name) }))));
  };
  const select = (kind, label) => el("label", {}, label, el("select", { onchange: (e) => { chooseFont(kind, e.target.value); showHint(); } },
    el("option", { value: "", selected: !chosenFont(kind) }, t("the theme's")),
    (state.fontLists[kind] || []).map(([family, how]) => {
      const installed = fontInstalled(family);
      return el("option", { value: family, selected: chosenFont(kind) === family, title: installed === false ? t("Not installed. To get it: {how}", { how }) : undefined },
        installed === false ? t("{family} (not installed)", { family }) : family);
    })));
  showHint();
  return el("details", { class: "fonts" }, el("summary", {}, t("Fonts")),
    el("div", { class: "font-panel" },
      select("ui", t("Interface font")), select("mono", t("Monospace font (terminal, code, logs)")),
      el("p", { class: "hint" }, t("Nothing is downloaded: install a font on this computer to use it.")), hint));
}

function howToGet(kind, name) {
  const found = (state.fontLists[kind] || []).find(([family]) => family === name);
  return found ? found[1] : "";
}

// --- login -----------------------------------------------------------------------------------------------

function loginView(message) {
  const user = el("input", { id: "user", autocomplete: "username", required: true, "aria-label": t("User") });
  // the web login's own password, not a secret being kept on a host: hidden at first (the login page is often seen
  // by others) and not remembered with the secret fields' choice; "Show value" beside it all the same
  const [password, passwordField] = secretInput({ id: "password", required: true, "aria-label": t("Password") },
    { shown: false, remember: false, hiddenAutocomplete: "current-password" });
  const code = el("input", { id: "code", inputmode: "numeric", autocomplete: "one-time-code", pattern: "[0-9]{6}", maxlength: 6, "aria-label": t("Code") });  // empty: a login without TOTP (web user add --no-totp)
  const status = el("div");
  if (message) status.append(notice(message, "error"));
  const form = el("form", {
    class: "card login", onsubmit: async (event) => {
      event.preventDefault();
      clear(status);
      try {
        const answer = await api("POST", "/api/login", { user: user.value, password: password.value, code: code.value });
        state.me = await api("GET", "/api/me");
        password.value = "";
        render();
        // A login made without TOTP (your choice): said at each login.
        if (answer && answer.second_factor === false) toast("warning", t("Login without a second factor"), t("This login has no TOTP: whoever learns its password can do everything here. Add one on the server: {command}", { command: `sandbox-ai web user reset ${user.value}` }));
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
    el("label", { for: "code" }, t("Code from your TOTP app (empty for a login without one)")), code,
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

// Only the newest render() draws: one still waiting for an answer when another starts (a login, a session that
// ended, a language chosen) drew its page as well, a second frame under the first or one over the login page.
async function render() {
  const run = ++state.renders;
  if (!state.me && !state.loginMessage) {
    try { state.me = await api("GET", "/api/me"); } catch (e) { state.me = null; }
  }
  if (run !== state.renders) return;
  if (!state.me) { const root = clear(document.getElementById("app")); document.title = state.loginMessage ? endedTitle() : "sandbox-ai"; removeSessionBanner(); root.append(loginView(state.loginMessage)); return; }
  state.loginMessage = null;
  try { state.hosts = await api("GET", "/api/hosts"); } catch (e) { state.hosts = [{ name: "local", kind: "local" }]; }
  try { state.policy = await api("GET", "/api/policy"); } catch (e) { state.policy = null; }  // without it: every prompt is asked
  if (run !== state.renders || !state.me) return;
  const root = clear(document.getElementById("app"));
  document.body.classList.toggle("button-texts", buttonTextsShown());  // (the icon buttons' texts, as chosen here)
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
      fontPicker(),
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
  pollSafeguards().catch(() => {});
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
    // (a row the page focused itself - the start the system monitor's Session led to - keeps the focus)
    const kept = state.rowFocused;
    state.rowFocused = false;
    if (moved && page !== "terminals" && state.page === page && !kept) focusHeading(main);
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
const START_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$/;  // a start's name or id
const NEW_START_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$/;  // a name given to a start (--name, rename): the launcher's
const WORKSPACE_AS = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;  // workspace add --name NAME
// a folder of another machine (sandbox-ai's remotefs.py): sshfs://, nfs:// or smb://..., mounted by that host
const REMOTE_SOURCE = /^(sshfs|nfs|smb):\/\/[^\s,]+$/i;
const MCP_SERVER_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$/;

// A start's identity: its id (list --json; it stays when the start is renamed), else its name (an older sandbox-ai).
// Rows and cards are keyed by host and that: a rename keeps them, and their Network or Resources card attached.
function startId(s) { return String(s.id || s.name); }
function startKey(s) { return `${s.host} ${startId(s)}`; }


// Whether a question of the last poll comes from the run of start S.
function startAsks(s) { return Boolean(s.run) && state.questions.some(q => q.host === s.host && q.run === s.run); }

function startSignature(s, starts) {
  const targets = s.kind === "mcp" ? starts.filter(other => other.kind !== "mcp" && other.host === s.host).map(other => [other.name, other.state, Boolean(other.directory)]) : null;
  return JSON.stringify([s.host, s.name, s.agent, s.kind, s.state, s.workspace, s.home, s.keep, s.started, s.attachable, s.directory ? 1 : 0, s.mcp, s.llm, s.serves, targets, startAsks(s), s.ask, LANGUAGE]);
}

// --ask in words (the desktop app's ask_choice.py has the same): on asks, off refuses without asking, allow asks
// nothing and allows every question at once (sandbox-ai 0.1.80), marked with a warning wherever it shows.
const ASK_LABELS = { on: N_("Ask me (default)"), off: N_("Refuse without asking"), allow: N_("Always allow everything") };
const ASK_ALLOW_HELP = N_("Nothing is asked: every question is allowed at once, for the run, and recorded in the approvals history and the connection log. What rules deny, the privacy check's blocking findings and a canary's alarm stay as they are.");
function askLabel(value) { return value === "allow" ? `⚠ ${t(ASK_LABELS.allow)}` : t(ASK_LABELS[value] || value); }

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
  // (the icon of each is its action's: pause, resume)
  const act = (label, action, extra) => iconRowButton(action, label, s.name, host, Object.assign({ onclick: (event) => run(event.currentTarget || event.target, action) }, extra || {}));
  const mcp = s.kind === "mcp";
  const stopText = mcp ? t("Stop the MCP server {name} on {host}? The starts it serves lose it until it starts again.", { name: s.name, host })
    : t("Stop {name} on {host}? What runs in it ends; {home}", { name: s.name, host,
      home: s.keep ? t("its HOME is kept: Resume starts it again.") : s.home === "own" ? t("its HOME is a directory of its own, which stays.") : t("its HOME is not kept: it is discarded with the start.") });
  const stop = rowButton(t("Stop"), s.name, host, { onclick: () => confirmIn(actions, stopText, t("Yes, stop it"), () => run(stop, "stop", { confirm: true }), { trigger: stop }) });
  if (s.attachable) actions.append(iconRowButton("attach", t("Attach"), s.name, host, { class: "primary", title: t("its terminal, in a tab of this page (detach: Ctrl-b d)"), onclick: () => openTerminal({ kind: "attach", name: s.name, host }, `${s.name}`) }));
  // A shell of yours in it, under the agent's rules (sandbox-ai shell): a tab of its own each time.
  if (s.attachable && s.state === "running") actions.append(iconRowButton("shell", t("Shell"), s.name, host, { title: t("a shell of yours in this sandbox, under the agent's rules (sandbox-ai shell)"), onclick: () => openTerminal({ kind: "shell", name: s.name, host }, t("shell: {name}", { name: s.name })) }));
  if (s.state === "running" && !mcp) actions.append(act(t("Pause"), "pause", { title: t("freeze it as it is (sandbox-ai pause)") }), stop,
    iconRowButton("allow", t("Network…"), s.name, host, { title: t("what it may reach now: allow, revoke, what was allowed and refused during the run"), onclick: () => networkCard(main, s) }),
    iconRowButton("limits", t("Resources…"), s.name, host, { title: t("workspaces, devices and resource limits while it runs"), onclick: () => resourcesCard(main, s) }));
  else if (s.state === "running" || (s.state === "paused" && mcp)) actions.append(stop);
  if (s.state === "paused" && !mcp) actions.append(act(t("Resume"), "resume", { title: t("thaw it (sandbox-ai resume)") }), stop);
  if (s.state === "stopped" && s.keep) {
    const resume = iconRowButton("resume", t("Resume"), s.name, host, { title: t("start it again with its kept HOME (sandbox-ai resume)"), onclick: () => reviewThenResume(main, s, resume) });
    const remove = iconRowButton("delete", t("Delete"), s.name, host, { class: "danger", title: t("remove it with its kept HOME; asked first (sandbox-ai delete)"), onclick: () => confirmIn(actions, t("Delete {name} on {host} with its kept HOME? What is in that HOME is gone for good.", { name: s.name, host }),
      t("Yes, delete it"), () => run(remove, "delete", { confirm: true }), { trigger: remove }) });
    actions.append(resume, remove);
  }
  if (mcp) actions.append(...attachControls(s, starts));
  if (!mcp && s.state !== "damaged") {
    const rename = rowButton(t("Rename…"), s.name, host, { onclick: () => askRename(actions, s, main, rename) });
    actions.append(rename);
  }
  // A kept start (a session), running or not, as one self-sufficient file (sandbox-ai board export): downloaded
  if (!mcp && s.keep && ["running", "paused", "stopped"].includes(s.state)) {
    const exported = iconRowButton("export", t("Export"), s.name, host, { title: t("one file another machine imports: its options, MCP and LLM servers, rulesets, kits, presets and image recipes - folders as placeholders, secrets by name only (sandbox-ai board export)"),
      onclick: () => exportSession(s, exported) });
    actions.append(exported);
  }
  // A second session there from its state (sandbox-ai session duplicate): its own HOME with the agent's conversation
  if (!mcp && s.keep && ["running", "paused", "stopped"].includes(s.state))
    actions.append(iconRowButton("copy", t("Duplicate…"), s.name, host, { title: t("a second session from its state: its own HOME (the agent's conversation continued on its own), memory and checkpoints; the workspace shared or copied (sandbox-ai session duplicate)"), onclick: () => duplicateCard(main, s) }));
  // More… starts it again with more only when it keeps its start (a run without one has nothing to start again
  // with: the CLI refuses); a running or paused one without it still gets the longer stop
  if (!mcp && (s.keep ? ["running", "paused", "stopped"].includes(s.state) : ["running", "paused"].includes(s.state)))
    actions.append(iconRowButton("resume-with", t("More…"), s.name, host, { title: s.keep ? t("resume with directories, mounts or MCP servers added, restart, stop with a longer wait") : t("stop with a longer wait"), onclick: () => moreCard(main, s) }));
  actions.append(rowButton(t("History"), s.name, host, { onclick: () => historyCard(main, s) }));
  // its workspaces as they were (sandbox-ai checkpoint): listed, taken now, compared, restored after a preview
  if (!mcp && s.state !== "damaged") actions.append(iconRowButton("checkpoints", t("Checkpoints…"), s.name, host, { title: t("the workspaces as they were when it started (and later): what changed, put back (sandbox-ai checkpoint)"), onclick: () => checkpointsCard(main, s) }));
  // its agent's memory and the agents' session files, read only (sandbox-ai session memory|files): while it runs, or
  // once stopped with its HOME kept
  if (!mcp && (["running", "paused", "orphaned"].includes(s.state) || (s.state === "stopped" && s.keep))) {
    actions.append(iconRowButton("memory", t("Memory…"), s.name, host, { title: t("its agent's memory file and auto memory: the live copy while it runs, else what went back to the project's memory store (sandbox-ai session memory)"), onclick: () => inspectCard(main, s, "memory") }),
      iconRowButton("files", t("Session files…"), s.name, host, { title: t("the agents' own session files in its HOME, one shown as a conversation; the start's folder as one archive (sandbox-ai session files, export)"), onclick: () => inspectCard(main, s, "files") }));
  }
  // its storage (sandbox-ai gc --check --home NAME): its HOME by kind, its project's logs and checkpoints, what can
  // be done with it, each with its saving and command
  if (!mcp && s.keep && s.state !== "damaged") actions.append(iconRowButton("storage", t("Storage…"), s.name, host, { title: t("its HOME by kind, the logs and checkpoints of its project, its growth, and what can be freed - each with its command (sandbox-ai gc --check --home NAME)"), onclick: () => storageCard(main, host, s.name) }));
  // the LLM server it works with (--llm NAME[:MODEL]): what that server has loaded and uses now, while it runs
  const llm = !mcp && typeof s.llm === "string" && LLM_CHOICE.test(s.llm) ? s.llm : null;
  if (llm && ["running", "paused"].includes(s.state)) actions.append(rowButton(t("LLM status"), s.name, host, { title: t("what the LLM server {value} has loaded and uses now", { value: llm }), onclick: () => llmStatusCard(main, s) }));
  // which MCP servers a start reaches (or will at its next start), and whom a server serves
  const mcpNote = [mcp ? ((s.serves || []).length ? t("serves {starts}", { starts: s.serves.join(", ") }) : t("serves no start yet"))
    : (s.mcp || []).length ? `MCP: ${s.mcp.join(", ")}` : "", llm ? `LLM: ${llm}` : ""].filter(Boolean).join(" · ");
  const asks = startAsks(s) ? el("button", { class: "badge asking small", title: t("a question of this start waits in Approvals"), onclick: () => show("approvals") }, t("asking")) : null;
  // --ask allow (list --json "ask"): nobody is asked, every question allowed
  const allowsAll = s.ask === "allow" && ["running", "paused"].includes(s.state)
    ? el("span", { class: "badge allows-all", title: t(ASK_ALLOW_HELP) }, `⚠ ${t("asks nothing, allows every question")}`) : null;
  const status = statusCell(s.state);
  if (allowsAll) status.append(" ", allowsAll);
  return el("tr", { "data-name": s.name, "data-host": host }, hostMarkerCell(host),
    el("td", { class: "mono start-name" }, el("span", { class: "tree-slot" }), s.name, asks ? " " : null, asks, el("span", { class: "stale-note hint" })), el("td", {}, s.agent || s.kind || ""), status,
    el("td", { class: "mono" }, s.workspace || "", mcpNote ? el("div", { class: "hint" }, mcpNote) : null), el("td", {}, s.home || ""), el("td", {}, (s.started || "").replace("T", " ").replace("Z", "")), actions);
}

// The LLM server start S works with (list --json's "llm": --llm NAME[:MODEL]): what it has loaded and uses now (llm
// status NAME on the start's host), read again every LLM_STATUS_SECONDS while the card is open (a poll of the page's
// own: not the user's activity). A host whose sandbox-ai does not know it yet says so, once.
const LLM_CHOICE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,31}(:[\w.:/@+-]{1,200}(,[\w.:/@+-]{1,200}){0,49})?$/;  // NAME[:MODEL[,MODEL...]]
function llmStatusCard(main, s) {
  const value = String(s.llm), server = value.split(":")[0];
  const old = document.getElementById("llm-status-card");
  if (old) dropCard(old);
  const body = el("div", {}, el("p", { class: "hint" }, t("Loading…")));
  const card = el("div", { class: "card", id: "llm-status-card" }, el("h2", {}, t("LLM server {value} of {name} on {host}", { value, name: s.name, host: s.host })), body,
    el("button", { onclick: () => dropCard(card) }, t("Close")));
  openCard(main, card, s);
  let timer = null;
  const read = async () => {
    clearTimeout(timer);
    if (!card.isConnected) return;
    let found;
    try { found = await api("GET", hostPath(`/llm/${encodeURIComponent(server)}/status`, s.host), undefined, { background: true }); }
    catch (e) {
      if (!card.isConnected) return;
      clear(body).append(errorNotice(e));
      if (e && e.code !== "host-too-old") timer = setTimeout(read, LLM_STATUS_SECONDS * 1000);
      return;
    }
    if (!card.isConnected) return;
    const status = (Array.isArray(found) ? found : []).find(x => x && x.name === server);
    clear(body).append(...(status ? [el("p", { class: "llm-now" }, llmStatusText(status)), el("pre", { class: "hint" }, llmStatusDetail(status))]
      : [notice(t("No status of {name}.", { name: server }), "warn")]));
    timer = setTimeout(read, LLM_STATUS_SECONDS * 1000);
  };
  read();
  return card;
}

// A session as one file (sandbox-ai board export --session NAME on its host): offered as a download. It holds the
// host's folder paths and names, never a secret's value (said in the toast).
async function exportSession(s, button) {
  await busy(button, async () => {
    let r;
    try { r = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/export`, s.host), {}); }
    catch (e) { failed(t("Could not export {name} on {host}", { name: s.name, host: s.host }))(e); return; }
    const text = JSON.stringify((r && r.bundle) || {}, null, 1);
    const link = el("a", { href: URL.createObjectURL(new Blob([text], { type: "application/json" })), download: (r && r.file) || "session.sandbox-ai-bundle.json" });
    document.body.append(link);
    link.click();
    link.remove();
    toast("success", t("{name} on {host} exported", { name: s.name, host: s.host }),
      t("The file holds the host's folder paths and names - never a secret's value: pass it on knowingly."));
  });
}

// A bundle taken in on HOST (sandbox-ai board import): Check shows its plan (nothing written) - each folder with the
// bundle's path as its default, the values it withheld, what is created, reused, replaced and missing -, Import (asked
// first: board-import) writes its parts and keeps the board (never started).
function importCard(main, host) {
  const old = document.getElementById("import-card");
  if (old) dropCard(old);
  const file = el("input", { id: "import-file", type: "file", accept: ".json,application/json" });
  const name = el("input", { id: "import-as", class: "mono", size: 20, placeholder: t("the bundle's") });
  const replace = el("input", { type: "checkbox", id: "import-replace" });
  const unsigned = el("input", { type: "checkbox", id: "import-unsigned" });
  const inputs = el("div"), result = el("div"), actions = el("div", { class: "row" });
  let bundle = null, plan = null;
  const folders = new Map(), values = new Map();
  const body = (check) => {
    const map = {}, given = {};
    for (const [marker, input] of folders) if (input.value.trim()) map[marker] = input.value.trim();
    for (const [key, input] of values) if (input.value) given[key] = input.value;
    const replaced = [];
    if (replace.checked && plan) for (const p of plan.problems || []) for (const m of String(p.fix || "").matchAll(/--replace ([a-z]+:[A-Za-z0-9][A-Za-z0-9_.@-]{0,63})/g)) replaced.push(m[1].split("@")[0]);
    return { bundle, check, map, values: given, as: name.value.trim() || null, replace: [...new Set(replaced)], unsigned: unsigned.checked, ...(check ? {} : { confirm: true }) };
  };
  const draw = (r) => {
    plan = r;
    clear(inputs); folders.clear(); values.clear();
    for (const f of r.folders || []) {
      const id = `import-folder-${folders.size}`;
      const input = el("input", { id, class: "mono", size: 40, value: f.to || f.default || "" });
      folders.set(f.marker, input);
      inputs.append(el("div", { class: "row" }, el("label", { for: id }, t("Folder {marker} ({label})", { marker: f.marker, label: f.label || "" })), input));
    }
    for (const v of r.values || []) {
      const key = String(v.marker || "").replace(/^<text:|>$/g, "");
      const id = `import-value-${values.size}`;
      const [input, field] = secretInput({ id, size: 30 });
      values.set(key, input);
      inputs.append(el("div", { class: "row" }, el("label", { for: id }, t("Value {marker} ({label})", { marker: v.marker, label: v.label || "" })), field));
    }
    const items = [];
    for (const [title, key] of [[t("Created"), "create"], [t("Reused (the same is here)"), "reuse"], [t("Replaced"), "replace"]])
      if ((r[key] || []).length) items.push(`${title}: ${r[key].map(x => `${x.kind} ${x.name}`).join(", ")}`);
    for (const [kind, list] of Object.entries(r.missing || {})) for (const m of list || []) items.push(t("Missing here ({kind}): {name} - {fix}", { kind, name: m.name, fix: m.fix }));
    for (const w of r.withheld || []) items.push(t("Not in the bundle: {what}", { what: w }));
    for (const n of r.notes || []) items.push(t("Note: {text}", { text: n }));
    for (const w of r.warnings || []) items.push(t("Warning: {text}", { text: w }));
    if (r.command) items.push(t("Starts as: {command}", { command: r.command }));
    clear(result).append(el("ul", {}, items.map(line => el("li", {}, line))));
    if ((r.problems || []).length) result.append(notice(t("Not importable as it is: {problems}", { problems: r.problems.map(p => p.text + (p.fix ? ` (${p.fix})` : "")).join("; ") }), "warn"));
  };
  const check = async (event) => busy(event.target, async () => {
    clear(actions);
    const chosen = file.files && file.files[0];
    if (!chosen) { toast("warning", t("Which bundle?"), t("Choose the bundle's file first.")); return; }
    try { bundle = JSON.parse(await chosen.text()); } catch (e) { clear(result).append(notice(t("Not a bundle: its JSON cannot be read (an archive with images is imported with sandbox-ai board import)."), "warn")); return; }
    let r;
    try { r = await api("POST", hostPath("/board-import", host), body(true)); } catch (e) { clear(result).append(errorNotice(e)); return; }
    draw(r || {});
    if ((r.problems || []).length) return;
    actions.append(el("button", { class: "primary", onclick: (go) => confirmIn(actions, t("Import the board on {host}: write its parts there (MCP and LLM servers, kits, presets, rulesets, image recipes) and keep the board? It is not started.", { host }), t("Yes, import it"), async () => {
      try {
        const done = await api("POST", hostPath("/board-import", host), body(false));
        draw(done || {});
        toast("success", t("Imported as {name} on {host}", { name: (done && done.board) || "", host }), t("Not started: start it with the desktop app's Board, or: {command}", { command: (done && done.command) || "" }));
      } catch (e) { clear(result).append(errorNotice(e)); }
    }, { trigger: go.target }) }, t("Import")));
  });
  const card = el("div", { class: "card", id: "import-card" }, el("h2", {}, t("Import a board on {host}", { host })),
    el("p", { class: "hint" }, t("A bundle from elsewhere is passed on, not yours: what a passed-on preset may not carry is refused, and nothing is written before you see its plan and confirm it.")),
    el("div", { class: "row" }, el("label", { for: "import-file" }, t("Bundle file")), file),
    el("div", { class: "row" }, el("label", { for: "import-as" }, t("Board's name")), name),
    el("label", {}, replace, " ", t("Replace the parts of the same name here")),
    el("label", {}, unsigned, " ", t("Take MCP servers of an unsigned catalog")), inputs,
    el("div", { class: "row" }, el("button", { onclick: check }, t("Check")), el("button", { onclick: () => dropCard(card) }, t("Close"))),
    actions, result);
  openCard(main, card);
  return card;
}

// What an import (or a duplicate: its import) made or would make (sandbox-ai session import --json), as lines.
function sessionImportLines(r) {
  const lines = [];
  if (r.imported) lines.push(t("Made: {name}. Resume continues it (the agent resumes its conversation).", { name: String(r.name || "") }));
  else if (r.checked) lines.push(t("It can start there as {name} (checked: nothing was kept).", { name: String(r.name || "") }));
  lines.push(t("Starts with: {options}", { options: (r.options || []).map(String).join(" ") || "-" }));
  if (r.workspace) lines.push(t("Workspace: {path}", { path: String(r.workspace) }) + (r.workspace_files ? " " + t("(its files from the archive)") : ""));
  if (r.include_secrets) lines.push(t("The archive holds logins and keys."));
  for (const item of r.asked || []) lines.push(t("Asked first: {item}", { item: String(item) }));
  for (const item of r.warnings || []) lines.push(t("Note: {item}", { item: String(item) }));
  if ((r.approvals || []).length) lines.push((r.granted || []).length ? t("Network approvals granted again: {list}", { list: r.approvals.join(" ") })
    : t("Network approvals not granted (tick Grant its network approvals again): {list}", { list: r.approvals.join(" ") }));
  if (r.memory) lines.push(t("Memory: {count} files added to the project's memory there", { count: r.memory.added || 0 }));
  if (r.checkpoints) lines.push(t("Checkpoints: {count}", { count: (r.checkpoints.ids || []).length }));
  return lines;
}

// Duplicate…: a second session of its host from the state of S (sandbox-ai session duplicate), not started. Its
// logins and keys along and its network approvals granted again are each asked in place (session-export-secrets,
// session-grant-approvals); what it made is listed (as text, never markup).
function duplicateCard(main, s) {
  const host = s.host, old = document.getElementById("duplicate-card");
  if (old) dropCard(old);
  const name = el("input", { id: "duplicate-name", class: "mono", size: 20, value: `${s.name}-2`.slice(0, 40) });
  const files = el("input", { type: "checkbox", id: "duplicate-workspace" });
  const secrets = el("input", { type: "checkbox", id: "duplicate-secrets" });
  const grant = el("input", { type: "checkbox", id: "duplicate-grant" });
  const actions = el("div", { class: "row" }), result = el("div");
  const go = el("button", { class: "primary", onclick: () => {
    const value = name.value.trim();
    if (!NEW_START_NAME.test(value) || value.length > 40) { toast("warning", t("Which name?"), t("1-40 letters, digits, '_', '.' or '-', starting with a letter or digit.")); return; }
    const asks = [secrets.checked ? t("its logins, keys and secrets go along: whoever has them can log in as you") : null,
      grant.checked ? t("its network approvals are granted again as options of the duplicate") : null].filter(Boolean);
    const run = () => busy(go, async () => {
      let r;
      try { r = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/session-duplicate`, host), Object.assign({ new: value, with_workspace: Boolean(files.checked), include_secrets: Boolean(secrets.checked), grant_approvals: Boolean(grant.checked) }, asks.length ? { confirm: true } : {})); }
      catch (e) { clear(result).append(errorNotice(e)); return; }
      secrets.checked = false;  // (chosen anew each time)
      clear(result).append(el("ul", {}, sessionImportLines(r || {}).map(line => el("li", {}, line))));
      toast("success", t("{name} duplicated as {new} on {host}", { name: s.name, new: value, host }), t("Not started: Resume starts it."));
      if (state.page === "starts") refreshStarts(main, true);
    });
    if (asks.length) confirmIn(actions, t("Duplicate {name} as {new} on {host}? {asks}.", { name: s.name, new: value, host, asks: asks.join("; ") }), t("Yes, duplicate"), run, { trigger: go });
    else run();
  } }, t("Duplicate"));
  const card = el("div", { class: "card", id: "duplicate-card" }, el("h2", {}, t("Duplicate {name} on {host}", { name: s.name, host })),
    el("p", { class: "hint" }, t("A second session there from its state: its configuration, a copy of its HOME with the agent's conversation (both continue it on their own), memory and checkpoints. Both can run; a running one is paused for the moment of the snapshot. Not started.")),
    el("div", { class: "row" }, el("label", { for: "duplicate-name" }, t("Name of the duplicate")), name),
    el("label", {}, files, " ", t("Copy its workspace too (beside it as WORKSPACE-NEW)")),
    el("label", {}, secrets, " ", t("With logins and keys")),
    el("label", {}, grant, " ", t("Grant its network approvals again")),
    el("div", { class: "row" }, go, el("button", { onclick: () => dropCard(card) }, t("Close"))), actions, result);
  openCard(main, card, s);
  return card;
}

// Import a session…: an archive of sandbox-ai session export on HOST (its path there) as a session to resume -
// checked first (--check: its files against its manifest, its configuration with the launcher's dry run, what it
// asks), then imported after a question in place (session-import; its network approvals: session-grant-approvals).
function sessionImportCard(main, host) {
  const old = document.getElementById("session-import-card");
  if (old) dropCard(old);
  const path = el("input", { id: "session-import-path", class: "mono", size: 40, placeholder: t("/home/you/web.tar.gz") });
  const name = el("input", { id: "session-import-as", class: "mono", size: 20, placeholder: t("its name there") });
  const maps = el("textarea", { id: "session-import-maps", class: "mono", rows: 2, cols: 50, placeholder: t("one per line: FOLDER THERE=FOLDER HERE") });
  const noFiles = el("input", { type: "checkbox", id: "session-import-no-workspace" });
  const replace = el("input", { type: "checkbox", id: "session-import-replace" });
  const grant = el("input", { type: "checkbox", id: "session-import-grant" });
  const actions = el("div", { class: "row" }), result = el("div");
  const body = (check) => {
    const pairs = [];
    for (const line of maps.value.split("\n")) {
      if (!line.trim()) continue;
      const at = line.indexOf("=");
      if (at < 1) throw new Error(t("A folder mapped is FOLDER THERE=FOLDER HERE: {line}", { line }));
      pairs.push([line.slice(0, at).trim(), line.slice(at + 1).trim()]);
    }
    return Object.assign({ path: path.value.trim(), map: pairs, check, replace: Boolean(replace.checked), no_workspace: Boolean(noFiles.checked), grant_approvals: Boolean(grant.checked) && !check },
      name.value.trim() ? { as: name.value.trim() } : {}, check ? {} : { confirm: true });
  };
  const check = (event) => busy(event.target, async () => {
    clear(actions);
    let r;
    try { r = await api("POST", hostPath("/session-import", host), body(true)); } catch (e) { clear(result).append(errorNotice(e)); return; }
    clear(result).append(el("ul", {}, sessionImportLines(r || {}).map(line => el("li", {}, line))));
    const asked = (r.asked || []).map(String);
    const question = t("Import it as {name} on {host}? It is not started.", { name: String(r.name || ""), host })
      + (asked.length ? " " + t("Its configuration gives the sandbox this of that machine: {items}", { items: asked.join("; ") }) : "")
      + (grant.checked && (r.approvals || []).length ? " " + t("Its network approvals are granted again: {list}", { list: r.approvals.join(" ") }) : "");
    actions.append(el("button", { class: "primary", onclick: (go) => confirmIn(actions, question, t("Yes, import it"), async () => {
      try {
        const done = await api("POST", hostPath("/session-import", host), body(false));
        clear(result).append(el("ul", {}, sessionImportLines(done || {}).map(line => el("li", {}, line))));
        toast("success", t("Imported as {name} on {host}", { name: String((done && done.name) || ""), host }), t("Not started: Resume starts it."));
        if (state.page === "starts") refreshStarts(main, true);
      } catch (e) { clear(result).append(errorNotice(e)); }
    }, { trigger: go.target }) }, t("Import")));
  });
  const card = el("div", { class: "card", id: "session-import-card" }, el("h2", {}, t("Import a session on {host}", { host })),
    el("p", { class: "hint" }, t("An archive of sandbox-ai session export (a file on that host) as a session Resume continues, conversation and all. It is checked as untrusted input first - its files against its manifest, its configuration with the launcher's own dry run - and nothing is kept before you see what it starts with and confirm it.")),
    el("div", { class: "row" }, el("label", { for: "session-import-path" }, t("Archive")), path),
    el("div", { class: "row" }, el("label", { for: "session-import-as" }, t("Name there")), name),
    el("div", { class: "row" }, el("label", { for: "session-import-maps" }, t("Folders mapped")), maps),
    el("label", {}, noFiles, " ", t("Leave the archive's workspace files out")),
    el("label", {}, replace, " ", t("Replace a stopped session of that name")),
    el("label", {}, grant, " ", t("Grant its network approvals again")),
    el("div", { class: "row" }, el("button", { onclick: check }, t("Check")), el("button", { onclick: () => dropCard(card) }, t("Close"))),
    actions, result);
  openCard(main, card);
  return card;
}

// Another name for a start (sandbox-ai rename OLD NEW), asked in its row; the run id stays.
function askRename(actions, s, main, trigger) {
  const id = `rename-${s.host}-${s.name}`.replace(/[^A-Za-z0-9_-]/g, "_");
  const input = el("input", { id, class: "mono", value: s.name, size: 16, pattern: NEW_START_NAME.source.slice(1, -1) });
  confirmIn(actions, el("span", {}, el("label", { for: id }, t("New name for {name} on {host}", { name: s.name, host: s.host })), " ", input), t("Rename"), async () => {
    const value = input.value.trim();
    if (!NEW_START_NAME.test(value)) { toast("warning", t("Which name?"), t("1-40 letters, digits, '_', '.' or '-', starting with a letter or digit.")); return; }
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

// The checkpoints of a start's workspaces (sandbox-ai checkpoint on its host): taken when it began, every few minutes
// or here (Take one now); Changes shows what changed since one, Restore… its preview (sandbox-ai checkpoint restore
// --dry-run) and then, confirmed in place (checkpoint-restore), puts it back - a checkpoint of the state before is
// taken first, so a restore is undone by restoring that one. Delete is confirmed too (checkpoint-delete).
function sizeText(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = n / 1024, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(1)} ${units[unit]}`;
}
const CHECKPOINT_KINDS = { start: N_("at the start"), manual: N_("taken by hand"), periodic: N_("periodic"), restore: N_("before a restore") };
function changesView(found) {
  const counts = (found && found.counts) || {};
  const box = el("div", {}, el("p", {}, t("{changed} changed, {added} added, {deleted} deleted", { changed: counts.changed || 0, added: counts.added || 0, deleted: counts.deleted || 0 })));
  for (const root of (found && Array.isArray(found.roots)) ? found.roots : []) {
    const base = String(root.sandbox || root.host || "");
    const join = (rel) => rel ? `${base.replace(/\/$/, "")}/${rel}` : base;
    const lines = [];
    for (const [key, mark] of [["changed", "M"], ["added", "A"], ["deleted", "D"]]) {
      for (const item of (Array.isArray(root[key]) ? root[key] : []).slice(0, 200)) {
        const [rel, what] = Array.isArray(item) ? item : [item, null];
        lines.push(`${mark} ${join(String(rel))}${what && what !== "content" ? ` (${what})` : ""}`);
      }
      const more = (Array.isArray(root[key]) ? Math.max(0, root[key].length - 200) : 0) + ((root.more || {})[key] || 0);
      if (more) lines.push(t("… and {count} more", { count: more }));
    }
    box.append(el("h3", {}, `${base} (${String(root.host || "")})`), lines.length ? el("pre", { class: "mono" }, lines.join("\n")) : el("p", { class: "hint" }, t("No change.")));
  }
  return box;
}
function checkpointsCard(main, s) {
  const host = s.host;
  const old = document.getElementById("checkpoints-card");
  if (old) dropCard(old);
  const body = el("div", {}, el("p", { class: "hint" }, t("Loading…"))), detail = el("div", { class: "wide" });
  const label = el("input", { id: "checkpoint-label", class: "mono", size: 30, placeholder: t("why (optional)") });
  const take = el("button", { class: "primary", onclick: () => busy(take, async () => {
    try { await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/checkpoints`, host), { label: label.value.trim() }); }
    catch (e) { failed(t("Could not take a checkpoint of {name} on {host}", { name: s.name, host }))(e); return; }
    label.value = "";
    toast("success", t("Checkpoint of {name} on {host} taken", { name: s.name, host }));
    load();
  }) }, t("Take one now"));
  const card = el("div", { class: "card", id: "checkpoints-card" }, el("h2", {}, t("Checkpoints of {name} on {host}", { name: s.name, host })),
    el("p", { class: "hint" }, t("Taken on the host, where the sandbox cannot reach them: one when a start begins, others every few minutes or by hand. A restore shows what changes first and takes a checkpoint of the state now before it writes.")),
    el("div", { class: "row" }, el("label", { for: "checkpoint-label" }, t("Label"), " "), label, take), body, detail,
    el("button", { onclick: () => dropCard(card) }, t("Close")));
  openCard(main, card, s);
  const showChanges = (c, button) => busy(button, async () => {
    clear(detail).append(el("p", { class: "hint" }, t("Loading…")));
    let found;
    try { found = await api("GET", hostPath(`/checkpoints/${encodeURIComponent(c.id)}/diff`, host)); }
    catch (e) { clear(detail).append(errorNotice(e)); return; }
    clear(detail).append(el("h3", {}, t("Changed since {id}", { id: c.id })), changesView(found));
  });
  const restore = (c, button, actions) => busy(button, async () => {
    clear(detail).append(el("p", { class: "hint" }, t("Loading…")));
    let preview;
    try { preview = await api("GET", hostPath(`/checkpoints/${encodeURIComponent(c.id)}/restore`, host)); }
    catch (e) { clear(detail).append(errorNotice(e)); return; }
    const counts = preview.counts || {};
    clear(detail).append(el("h3", {}, t("Restoring {id} changes", { id: c.id })), changesView(preview));
    if (!(counts.changed || counts.added || counts.deleted)) { detail.append(notice(t("Nothing to restore: the folders are as they were."), "info")); return; }
    const running = ["running", "paused"].includes(s.state) ? " " + t("{name} runs: its agent may write while the restore runs (pause it first).", { name: s.name }) : "";
    confirmIn(actions, t("Put the workspaces of {name} on {host} back as they were at {time}? {changed} changed files get their content of then back, {added} files added since are removed and {deleted} deleted files are put back; a checkpoint of the state now is taken first.", { name: s.name, host, time: String(c.created || "").replace("T", " ").replace("Z", ""), changed: counts.changed || 0, added: counts.added || 0, deleted: counts.deleted || 0 }) + running,
      t("Yes, restore"), async () => {
        let done;
        try { done = await api("POST", hostPath(`/checkpoints/${encodeURIComponent(c.id)}/restore`, host), { confirm: true }); }
        catch (e) { failed(t("Could not restore {id} on {host}", { id: c.id, host }))(e); return; }
        toast("success", t("{id} restored on {host}", { id: c.id, host }), t("Undo it by restoring {id}.", { id: done && done.before ? done.before : "?" }));
        clear(detail);
        load();
      }, { trigger: button });
  });
  const remove = (c, button, actions) => confirmIn(actions, t("Delete the checkpoint {id} of {name} on {host}? What only it holds is gone for good.", { id: c.id, name: s.name, host }), t("Yes, delete it"), async () => {
    try { await api("POST", hostPath(`/checkpoints/${encodeURIComponent(c.id)}/delete`, host), { confirm: true }); }
    catch (e) { failed(t("Could not delete {id} on {host}", { id: c.id, host }))(e); return; }
    load();
  }, { trigger: button });
  async function load() {
    let found;
    try { found = await api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/checkpoints`, host)); }
    catch (e) { clear(body).append(errorNotice(e)); return; }
    if (!card.isConnected) return;
    const rows = (Array.isArray(found) ? found : []).filter(c => c && typeof c.id === "string").map(c => {
      const actions = el("td", { class: "actions" });
      const changes = el("button", { onclick: () => showChanges(c, changes) }, t("Changes"));
      const back = el("button", { onclick: () => restore(c, back, actions) }, t("Restore…"));
      const gone = el("button", { class: "danger", onclick: () => remove(c, gone, actions) }, t("Delete"));
      actions.append(changes, back, gone);
      return el("tr", {}, el("td", { class: "mono" }, c.id), el("td", {}, String(c.created || "").replace("T", " ").replace("Z", "")),
        el("td", {}, t(CHECKPOINT_KINDS[c.kind] || String(c.kind || ""))), el("td", {}, String(c.files || 0)), el("td", {}, sizeText(c.bytes)),
        el("td", {}, String(c.label || (c.roots || []).map(r => r.sandbox || r.host).join(", "))), actions);
    });
    clear(body).append(table([t("Checkpoint"), t("Taken"), t("Why"), t("Files"), t("Size"), t("Label / folders"), ""], rows,
      t("No checkpoints of {name} yet.", { name: s.name }), t("Checkpoints of {name} on {host}", { name: s.name, host })));
  }
  load();
  return card;
}

// A start's agent memory and the agents' session files (sandbox-ai session memory|files on its host), read only: the
// files on the left, the chosen one's text on the right (Search keeps the lines that hold a text), and the start's
// folder written as one archive on its host (sandbox-ai session export, confirmed in place: session-export). The
// files are written by the agent: every name and text is put in as text (el(), textContent), never as markup.
// A file marked as a login or key (its "secret": why) is listed like the others, with the why; its text is shown only
// after a question in place (show-secret-file: POST .../secret). The host's default hide_secrets (Settings) leaves
// such files out, and the list says how many. An export leaves them out unless "With logins and keys" is ticked
// (asked first: session-export-secrets); the names left out are listed.
const SECRET_WHY = { "an agent's login": N_("an agent's login"), "a login, token or key (by its name)": N_("a login, token or key (by its name)"),
  "an agent's provider keys": N_("an agent's provider keys"), "holds a private key": N_("holds a private key"),
  "holds a placeholder of a secret the launcher gave": N_("holds a placeholder of a secret the launcher gave") };
function secretWhy(why) { return SECRET_WHY[why] ? t(SECRET_WHY[why]) : String(why || ""); }
const ROLE_TITLES = { user: N_("user"), assistant: N_("assistant"), system: N_("system"), reasoning: N_("reasoning"), "tool-call": N_("tool call"), "tool-result": N_("tool result"), event: N_("event") };
function shownTime(value) { return String(value || "").replace("T", " ").replace("Z", "").slice(0, 19); }
function conversationText(found) {
  const messages = Array.isArray(found && found.messages) ? found.messages : [];
  if (!messages.length) return t("Nothing to show as a conversation.");
  return messages.map(m => `== ${shownTime(m.time) || "-"}  ${t(ROLE_TITLES[m.role] || String(m.role || ""))}${m.tool ? ": " + String(m.tool) : ""}${m.error ? " (" + t("error") + ")" : ""}\n${String(m.text || "")}`).join("\n\n");
}
function inspectCard(main, s, kind) {
  const host = s.host, memory = kind === "memory", id = memory ? "memory-card" : "session-files-card";
  const old = document.getElementById(id);
  if (old) dropCard(old);
  const title = memory ? t("Memory of {name} on {host}", { name: s.name, host }) : t("Session files of {name} on {host}", { name: s.name, host });
  const where = el("p", { class: "hint" }), list = el("div", { class: "inspect-list" }, el("p", { class: "hint" }, t("Loading…")));
  const text = el("pre", { class: "mono inspect-text", tabindex: "0", "aria-label": t("Text") }, t("Choose a file."));
  const notes = el("div", { class: "hint" });
  const asking = el("div", { class: "row" });
  let shown = "", current = null, source = "auto";
  const allowed = new Set();  // the marked files shown after the question, while the card is open
  const search = el("input", { type: "search", class: "mono", size: 24, placeholder: t("Search"), "aria-label": t("Search in the text"), oninput: () => mark() });
  const full = memory ? null : el("input", { type: "checkbox", id: "session-files-full", onchange: () => { if (current) open(current); } });
  const from = memory ? el("select", { "aria-label": t("Which copy"), onchange: () => { source = from.value; load(); } },
    el("option", { value: "auto" }, t("automatic: the HOME while it runs, else the store")), el("option", { value: "home" }, t("the copy in its HOME")),
    el("option", { value: "store" }, t("the project's memory store"))) : null;
  function mark() {
    const needle = search.value.trim().toLowerCase();
    if (!needle) { text.textContent = shown; return; }
    const lines = shown.split("\n").filter(line => line.toLowerCase().includes(needle));
    text.textContent = lines.length ? lines.join("\n") : t("No line holds {text}.", { text: search.value.trim() });
  }
  const path = el("input", { id: `${id}-path`, class: "mono", size: 40, placeholder: t("/home/you/{name}.tar.gz", { name: s.name }) });
  const exportRow = el("div", { class: "row" });
  const withSecrets = el("input", { type: "checkbox", id: `${id}-secrets` });
  const withFiles = el("input", { type: "checkbox", id: `${id}-workspace` });
  const exported = el("button", { onclick: () => {
    const value = path.value.trim();
    if (!value.startsWith("/")) { toast("error", t("Name the archive with its full path on {host}.", { host })); return; }
    const include = withSecrets.checked;
    confirmIn(exportRow, include
      ? t("Write the folder of {name} - its records and its HOME as its agent left it, WITH its logins, keys and secrets - into {path} on {host}? Whoever has the archive can log in as you with them.", { name: s.name, path: value, host })
      : t("Write the folder of {name} - its records and its HOME as its agent left it (transcripts, memory, what it kept) - into {path} on {host}? Logins and keys are left out.", { name: s.name, path: value, host }),
      include ? t("Yes, export with logins and keys") : t("Yes, export"), () => busy(exported, async () => {
        let done;
        try { done = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/session-export`, host), Object.assign({ path: value, confirm: true }, include ? { include_secrets: true } : {}, withFiles.checked ? { with_workspace: true } : {})); }
        catch (e) { failed(t("Could not export {name} on {host}", { name: s.name, host }))(e); return; }
        withSecrets.checked = false;  // (each export with logins and keys is chosen anew)
        const left = Array.isArray(done && done.left_out) ? done.left_out : [];
        const secrets = left.filter(item => item && item.secret), others = left.filter(item => item && !item.secret);
        toast("success", t("{name} exported to {path} on {host}", { name: s.name, path: String(done.path || value), host }), t("{count} files; {left} left out.", { count: done.files || 0, left: left.length }));
        clear(notes);
        if (secrets.length) notes.append(el("div", {}, t("Logins, keys and secrets left out ({count}) - the archive is made to be passed on; tick With logins and keys to take them in:", { count: secrets.length })),
          ...secrets.slice(0, 200).map(item => el("div", { class: "mono" }, `${String(item.path)} (${secretWhy(item.why)})`)));
        else if (done && done.include_secrets) notes.append(el("div", { class: "status-warn" }, t("Its logins, keys and secrets are in the archive: whoever has it can log in as you.")));
        notes.append(...others.slice(0, 200).map(item => el("div", { class: "mono" }, `${t("left out")}: ${String(item.path)} (${String(item.why)})`)));
      }), { trigger: exported });
  } }, t("Export…"));
  exportRow.append(el("label", { for: `${id}-path` }, t("Archive"), " "), path,
    el("label", { title: t("Take the logins, keys and secrets of its HOME into the archive too (asked first; --include-secrets)") }, withSecrets, " ", t("With logins and keys")),
    el("label", { title: t("Take the workspace's files into the archive too (--with-workspace); without, the import maps the folder") }, withFiles, " ", t("With its workspace's files")), exported);
  // A search in the session files (sandbox-ai session files --search on the start's host), as the desktop app's file
  // manager searches: the names by a pattern (* ? [abc], ** for any folders, a part of the name without a wildcard),
  // and the files whose text holds what is typed - each with its line; bounded there, a login or key never looked into
  const finder = memory ? null : filesSearch(s, (item) => open(item), (found) => showFound(found), () => load());
  const card = el("div", { class: "card", id }, el("h2", {}, title), where, finder ? finder.row : null,
    el("div", { class: "row" }, from, full ? el("label", {}, full, " ", t("tool input and output in full")) : null, search),
    el("div", { class: "inspect" }, list, text), asking, notes,
    s.keep ? exportRow : null,
    el("button", { onclick: () => dropCard(card) }, t("Close")));
  openCard(main, card, s);
  function open(item) {
    current = item;
    if (!item.secret || allowed.has(item.name) || !needsConfirm("show-secret-file")) { read(item); return; }
    text.textContent = t("{file} is marked as a login or key ({why}): its text is shown only when you ask.", { file: item.name, why: secretWhy(item.secret) });
    confirmIn(asking, t("Show the text of {file} ({why})? Anyone who sees this screen can read the login or key in it.", { file: item.name, why: secretWhy(item.secret) }),
      t("Yes, show it"), () => { allowed.add(item.name); if (current === item) read(item); });
  }
  async function read(item) {
    text.textContent = t("Loading…");
    const name = encodeURIComponent(item.name);
    const secret = Boolean(item.secret);
    let found;
    try {
      found = secret ? (memory ? await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/memory/${source}/${name}/secret`, host), { confirm: true })
        : await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/session-files/${name}/secret`, host), { confirm: true, full: Boolean(full && full.checked) }))
        : memory ? await api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/memory/${source}/${name}`, host))
        : full && full.checked ? await api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/session-files/${name}/full`, host))
          : await api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/session-files/${name}`, host));
    } catch (e) { clear(text).append(errorNotice(e)); return; }
    if (!card.isConnected || current !== item) return;
    if (memory) {
      const file = (Array.isArray(found.files) ? found.files : [])[0] || {};
      shown = file.binary ? t("(binary: not shown)") : String(file.text === null || file.text === undefined ? t("(not shown)") : file.text);
    } else shown = conversationText(found);
    if (secret) shown = t("Warning: it holds a login or key ({why}) - do not pass this on.", { why: secretWhy(item.secret) }) + "\n\n" + shown;
    clear(notes).append(...(Array.isArray(found.notes) ? found.notes : []).map(note => el("div", {}, String(note))));
    mark();
  }
  // What a search found, in the list's place: each file with its line and the part of it where the text is.
  function showFound(found) {
    if (!card.isConnected) return;
    if (found instanceof Error) { clear(list).append(errorNotice(found)); return; }
    const report = found.search || {}, files = Array.isArray(found.files) ? found.files : [];
    const withText = Boolean(report.text);
    const rows = files.filter(f => f && typeof f.name === "string").map(f => el("tr", {},
      el("td", {}, el("button", { class: "mono", onclick: () => open(f) }, f.name)), el("td", {}, String(f.agent || "")),
      withText ? el("td", { class: "num" }, String(f.line || "")) : null,
      withText ? el("td", { class: "mono" }, String(f.excerpt || "")) : null,
      el("td", {}, sizeText(f.size)), el("td", {}, shownTime(f.modified)),
      el("td", { class: f.secret ? "status-warn" : "" }, f.secret ? secretWhy(f.secret) : "")));
    clear(list).append(el("p", { class: "hint", role: "status" }, filesFoundText(report, files.length)),
      table([t("File"), t("Agent"), ...(withText ? [t("Line"), t("Found")] : []), t("Size"), t("Modified"), t("Login or key")], rows,
        t("No session file matches."), title));
    const said = filesSearchNotes(report);
    if (said.length) list.append(...said.map(line => el("p", { class: "hint" }, line)));
  }
  async function load() {
    let found;
    try {
      found = memory ? await api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/memory/${source}`, host))
        : await api("GET", hostPath(`/starts/${encodeURIComponent(s.name)}/session-files`, host));
    } catch (e) { clear(list).append(errorNotice(e)); return; }
    if (!card.isConnected) return;
    where.textContent = memory ? (found.source === "home" ? t("Its memory as the agent has it now, in its HOME: {path}", { path: String(found.path || "") })
      : t("What went back to the project's memory store: {path}", { path: String(found.path || "") }))
      : t("The agents' session files in its HOME: {path}", { path: String(found.home || "") });
    const files = Array.isArray(found.files) ? found.files : [];
    const rows = files.filter(f => f && typeof f.name === "string").map(f => {
      const pick = el("button", { class: "mono", onclick: () => open(f) }, f.name);
      return el("tr", {}, el("td", {}, pick), memory ? null : el("td", {}, String(f.agent || "") + (f.verified === false ? " *" : "")),
        el("td", {}, sizeText(f.size)), el("td", {}, shownTime(f.modified)),
        el("td", { class: f.secret ? "status-warn" : "" }, f.secret ? secretWhy(f.secret) : ""));
    });
    clear(list).append(table([t("File"), ...(memory ? [] : [t("Agent")]), t("Size"), t("Modified"), t("Login or key")], rows,
      memory ? t("No memory files yet.") : t("No session files yet."), title));
    if (found.hidden) list.append(el("p", { class: "hint" }, t("{count} files that hold a login or key are not listed (Settings: Hide logins and keys in file views).", { count: found.hidden })));
    if (!memory && files.some(f => f && f.verified === false)) list.append(el("p", { class: "hint" }, t("* where this agent keeps its sessions is not verified against its source: its files are listed as they look")));
    clear(notes).append(...(Array.isArray(found.notes) ? found.notes : []).map(note => el("div", {}, String(note))));
  }
  load();
  return card;
}

// The search row of a start's session files: PATTERN, TEXT, Match case, Search and Show all. SHOW(found or an Error)
// shows what was found; ALL() lists every file again.
function filesSearch(s, open, show, all) {
  const pattern = el("input", { type: "search", class: "mono", size: 22, id: "files-search-pattern", placeholder: "*.jsonl, **/rollout-*",
    "aria-label": t("File name or pattern"), title: t(FILES_PATTERN_HELP) });
  const text = el("input", { type: "search", size: 22, id: "files-search-text", placeholder: t("Text in the files"), "aria-label": t("Text in the files"),
    title: t("Only the files whose text holds this (each with its line); a file marked as a login or key is never looked into") });
  const caseBox = el("input", { type: "checkbox", id: "files-search-case" });
  const go = el("button", { type: "submit" }, t("Search"));
  const again = el("button", { type: "button", onclick: () => { pattern.value = ""; text.value = ""; all(); } }, t("Show all"));
  const run = () => busy(go, async () => {
    const body = { pattern: pattern.value.trim() };
    if (text.value.trim()) body.text = text.value;
    if (caseBox.checked) body.case = true;
    let found;
    try { found = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/session-files-search`, s.host), body); }
    catch (e) { found = e; }
    show(found);
  });
  const row = el("form", { class: "row files-search", role: "search", "aria-label": t("Search the session files"), onsubmit: (event) => { event.preventDefault(); run(); } },
    pattern, text, el("label", { title: t("Letter case counts in the name and the text") }, caseBox, " ", t("Match case")), go, again);
  return { row, pattern, text, caseBox, run };
}
const FILES_PATTERN_HELP = N_("The file's name, or with a / its path in the HOME: * any characters, ? one, [abc] one of them, ** any folders (.claude/**/*.jsonl); without a wildcard a part of the name. Empty: every file.");

// "N files found", with what was searched for.
function filesFoundText(report, count) {
  const pattern = String(report.pattern || ""), text = report.text ? String(report.text) : "";
  if (pattern && text) return t("{count} files named {pattern} that hold {text}", { count, pattern, text });
  if (text) return t("{count} files that hold {text}", { count, text });
  return t("{count} files named {pattern}", { count, pattern: pattern || "*" });
}

// What a search left out or could not do, each in a line: binary files, files read only in part, logins and keys
// (found by name only), files not readable, the limits reached (the host's own words).
function filesSearchNotes(report) {
  const lines = [];
  if (report.binary) lines.push(t("{count} files are not text (binary): not looked into.", { count: report.binary }));
  if (report.partly) lines.push(t("{count} files are larger than the search reads of one file: only their start was looked into.", { count: report.partly }));
  if (report.secret) lines.push(t("{count} files marked as a login or key: found by their name only, never looked into.", { count: report.secret }));
  for (const entry of Array.isArray(report.unreadable) ? report.unreadable : []) lines.push(t("Not read: {why}", { why: String(entry) }));
  for (const entry of Array.isArray(report.limits) ? report.limits : []) lines.push(String(entry));
  return lines;
}

// What happened to a start (sandbox-ai list --history NAME on its host).
// It follows a rename (cardRef: read again under the new name), and is switched off once the start is gone.
async function historyCard(main, s) {
  let name = s.name;
  const old = document.getElementById("history-card");
  if (old) dropCard(old);
  const body = el("div", {}, el("p", { class: "hint" }, t("Loading…"))), notes = el("div");
  const title = () => t("History of {name} on {host}", { name, host: s.host });
  const heading = el("h2", {}, title());
  const card = el("div", { class: "card", id: "history-card" }, heading, notes, body,
    el("button", { onclick: () => dropCard(card) }, t("Close")));
  openCard(main, card, s);
  const details = (e) => {
    if (e.event === "exit") return typeof e.status === "number" ? t("status {status}", { status: e.status }) : t("status unknown");
    if (e.event === "rename") return e.to ? t("renamed to {name}", { name: e.to }) : t("renamed from {name}", { name: e.from || "?" });
    return [e.agent, e.workspace, e.note].filter(Boolean).map(String).join(" ");
  };
  const load = async () => {
    const asked = name;
    let found;
    try { found = await api("GET", hostPath(`/starts/${encodeURIComponent(asked)}/history`, s.host)); }
    catch (e) { if (asked === name) clear(body).append(errorNotice(e)); return; }
    if (asked !== name || !card.isConnected) return;  // renamed meanwhile: the reading under the new name counts
    const rows = (Array.isArray(found) ? found : []).filter(e => e && typeof e === "object").slice().reverse().map(e =>
      el("tr", {}, el("td", {}, String(e.time || "").replace("T", " ").replace("Z", "")), el("td", {}, String(e.event || "")), el("td", { class: "mono" }, String(e.run || "-")), el("td", {}, details(e))));
    clear(body).append(table([t("Time"), t("Event"), t("Run"), t("Detail")], rows, t("No history of {name}.", { name }), title()));
  };
  registerCard(card, notes, cardRef(s, () => name, (fresh) => { name = fresh; load(); }, heading, title, () => true));
  await load();
}

// More of resume and stop (sandbox-ai resume NAME --workspace DIR --mount SRC:DST[:ro|rw] --mcp NAME --restart;
// sandbox-ai stop NAME --wait SECONDS): what is added is kept for its later starts; what widens it is asked first.
// A running or paused start takes what is added only at a start: it is restarted (--restart, ticked for both); a
// stopped one is resumed after the review of its kept HOME, as Resume does (reviewThenResume). A start that keeps
// nothing to start again with gets only the longer stop. The card follows a rename and is switched off once the
// start's state is another than the one it was opened for (its offers are that state's).
function moreCard(main, s) {
  const host = s.host;
  let name = s.name;
  const old = document.getElementById("more-card");
  if (old) dropCard(old);
  const again = Boolean(s.keep);  // it can start again (resume with more, a restart)
  const live = s.state === "running" || s.state === "paused";
  const workspaces = el("textarea", { id: "more-workspaces", rows: 2, class: "mono", placeholder: "/home/you/src/lib" });
  const mounts = el("textarea", { id: "more-mounts", rows: 2, class: "mono", placeholder: "/home/you/data:/mnt/data:ro" });
  const servers = el("input", { id: "more-mcp", class: "mono", placeholder: "files" });
  const restart = el("input", { id: "more-restart", type: "checkbox", checked: live });
  const wait = el("input", { id: "more-wait", type: "number", min: 1, max: 600, value: 30 });
  const status = el("div", { class: "wide" }), row = el("div", { class: "row" }), notes = el("div");
  const lines = (input) => input.value.split("\n").map(x => x.trim()).filter(Boolean);
  const request = () => {
    const body = { workspaces: lines(workspaces), mcp: servers.value.split(/\s+/).filter(Boolean), mounts: [] };
    for (const line of lines(mounts)) {
      const parts = line.split(":");
      if (parts.length < 2 || parts.length > 3 || (parts[2] && !["ro", "rw"].includes(parts[2]))) throw new Error(t("A mount is SOURCE:DESTINATION or SOURCE:DESTINATION:ro|rw: {line}", { line }));
      body.mounts.push({ source: parts[0], destination: parts[1], mode: parts[2] || "ro" });
    }
    if (live && restart.checked) body.restart = true;
    const added = body.workspaces.length || body.mounts.length || body.mcp.length;
    if (live && added && !body.restart) throw new Error(t("{name} is {state}: it takes directories, mounts and MCP servers only at a start. Tick “Restart it now”.", { name, state: stateText(s.state) }));
    return body;
  };
  const done = (r) => { doneToast(t("{name} on {host} resumed", { name, host }), r); dropCard(card); if (state.page === "starts") refreshStarts(main, true); };
  const resume = (event) => {
    clear(status);
    let body;
    try { body = request(); } catch (e) { status.append(errorNotice(e)); return; }
    const what = [body.restart ? t("it is restarted now (what runs in it ends; the agent continues its conversation)") : null,
      body.workspaces.length ? t("the directories {dirs}, writable", { dirs: body.workspaces.join(", ") }) : null,
      body.mounts.length ? t("the mounts {mounts}", { mounts: body.mounts.map(m => `${m.source}:${m.destination}:${m.mode}`).join(", ") }) : null,
      body.mcp.length ? t("the MCP servers {servers}", { servers: body.mcp.join(", ") }) : null].filter(Boolean);
    if (!what.length) { toast("warning", t("Nothing to add"), t("Name directories, mounts or MCP servers, or restart it.")); return; }
    const trigger = event.target;
    confirmIn(row, t("Resume {name} on {host} with {what}? What is added is kept for its later starts.", { name, host, what: what.join(t(" and ")) }), t("Yes, resume it"), async () => {
      Object.assign(body, { confirm: true });
      // (again after a server was built: its HOME was reviewed already)
      const again = async () => {
        try { done(await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/resume`, host), body)); }
        catch (e) { if (!offerBuilds(e, again)) status.append(errorNotice(e)); }
      };
      if (s.state === "stopped") {  // its kept HOME is reviewed first, as Resume does
        await reviewThenResume(main, Object.assign({}, s, { name }), trigger, { body, done,
          fail: (e) => { if (!offerBuilds(e, again)) clear(status).append(errorNotice(e)); } });
        return;
      }
      await again();
    }, { kind: "primary", trigger });
  };
  // A server the launcher named as built with `mcp add NAME --build` (its catalog has no signed image yet): offered
  // to be built there (asked with its commit), one after the other, then the resume again (AGAIN).
  const offerBuilds = (e, again) => {
    if (!(e instanceof ApiError && e.code === "mcp-unpublished-start")) return false;
    const servers = Object.keys(e.params || {}).filter(k => /^server[0-9]$/.test(k)).sort().map(k => { try { return JSON.parse(e.params[k]); } catch (_) { return null; } })
      .filter(x => x && typeof x.name === "string" && MCP_SERVER_NAME.test(x.name));
    if (!servers.length) return false;
    clear(status);
    const offer = el("div", { class: "row" });
    status.append(el("div", { class: "notice warn" }, el("p", {}, errorText(e)), offer, el("div", { class: "hint mono error-detail", lang: "en" }, e.message || "")));
    const next = (left) => {
      if (!left.length) { clear(status); again(); return; }
      const [server, ...rest] = left;
      buildMcpServer(offer, host, { entry: server.name, unsigned: !!server.unsigned, mounts: server.mounts || [], confirm: true },
        (r) => { toast("success", t("{name} built and added on {host} (not signed)", { name: server.name, host }), (r && r.output) || ""); clear(offer); next(rest); },
        (failure) => { clear(status).append(errorNotice(failure)); });
    };
    next(servers);
    return true;
  };
  const stop = (event) => {
    const seconds = Number(wait.value);  // what the question names is what is sent, whatever the field says by then
    if (!Number.isInteger(seconds) || seconds < 1 || seconds > 600) { clear(status).append(notice(t("Wait 1 to 600 seconds."), "error")); return; }
    confirmIn(row, t("Stop {name} on {host}, waiting up to {seconds} seconds for it to end? What runs in it ends.", { name, host, seconds }), t("Yes, stop it"), async () => {
      try { const r = await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/stop`, host), { wait: seconds, confirm: true }); toast("success", t("{name} on {host} stopped", { name, host }), (r && r.output) || ""); dropCard(card); if (state.page === "starts") refreshStarts(main, true); }
      catch (e) { status.append(errorNotice(e)); }
    }, { trigger: event.target });
  };
  // the MCP servers of its later starts, taken out one by one (mcp detach NAME START): for a start that does not run
  // now; a running one has them in its own card (mcp detach --live)
  const kept = again && s.state !== "running" ? (Array.isArray(s.mcp) ? s.mcp : []).filter(x => typeof x === "string" && MCP_SERVER_NAME.test(x)) : [];
  const keptBody = el("tbody");
  for (const server of kept) {
    const actions = el("td", { class: "actions" });
    const remove = el("button", { class: "danger", onclick: () => confirmIn(actions, t("Take {server} out of {name} on {host}? Its later starts go without it; the server's own sandbox keeps running.", { server, name, host }), t("Yes, take it out"), async () => {
      try {
        const r = await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/mcp/remove`, host), { server, confirm: true });
        toast("success", t("Detached {server} from {where}", { server, where: t("{name} on {host}", { name, host }) }), (r && r.message) || "");
        actions.closest("tr").remove();
      } catch (e) { failed(t("Could not detach {server} from {where}", { server, where: t("{name} on {host}", { name, host }) }))(e); }
    }, { trigger: remove }) }, t("Remove…"));
    actions.append(remove);
    keptBody.append(el("tr", {}, el("td", { class: "mono" }, server), actions));
  }
  const keptTable = kept.length ? el("table", { class: "wide" }, el("caption", {}, t("MCP servers of its later starts")), el("thead", {}, el("tr", {}, el("th", {}, t("Server")), el("th", {}, ""))), keptBody) : null;
  row.append(...[again ? el("button", { class: "primary", onclick: resume }, live ? t("Restart with these…") : t("Resume with these…")) : null,
    live ? el("button", { onclick: stop }, t("Stop and wait…")) : null, el("button", { onclick: () => dropCard(card) }, t("Close"))].filter(Boolean));
  const title = () => t("More for {name} on {host}", { name, host });
  const heading = el("h2", {}, title());
  const card = el("div", { class: "card", id: "more-card" }, heading, notes,
    again ? null : el("p", { class: "hint" }, t("{name} keeps nothing to start again with (a run without a kept start): directories, mounts and MCP servers go with a new start.", { name })),
    el("div", { class: "form" },
      again ? el("label", { for: "more-workspaces" }, t("Directories (one per line)")) : null, again ? workspaces : null,
      again ? el("label", { for: "more-mounts" }, t("Mounts (one per line)")) : null, again ? mounts : null,
      again ? el("label", { for: "more-mcp" }, t("MCP servers")) : null, again ? servers : null,
      again && live ? el("label", { for: "more-restart" }, t("Restart it now")) : null, again && live ? restart : null,
      live ? el("label", { for: "more-wait" }, t("Stop: wait up to (seconds)")) : null, live ? wait : null,
      status, el("span"), row), keptTable);
  openCard(main, card, s);
  const opened = s.state;
  registerCard(card, notes, cardRef(s, () => name, (fresh) => { name = fresh; }, heading, title, (now) => now === opened));
}

// An MCP server its catalog has no signed image of yet, built on HOST from its source and added UNSIGNED (POST /mcp
// with build: `mcp add --build`). What would be built is read there first (the service answers mcp-build-asked with the
// commit and the source archive's SHA-256: nothing built), asked in CONTAINER, and exactly that is built after a yes
// (commit, source_sha256, confirm_build); DONE(result) after, FAIL(error) on a refusal or a No. BODY: what POST /mcp
// takes (entry, mounts, unsigned, ...).
function buildMcpServer(container, host, body, done, fail) {
  const send = (extra) => api("POST", hostPath("/mcp", host), Object.assign({}, body, { build: true }, extra || {}));
  (async () => {
    let asked;
    try { done(await send({})); return; } catch (e) { asked = e; }
    if (!(asked instanceof ApiError && asked.code === "mcp-build-asked")) { fail(asked); return; }
    const p = asked.params || {};
    const mounts = (body.mounts || []).map(m => `${m.target}=${m.host}:${m.mode || "ro"}`).join(", ");
    const question = [t("{entry} has no signed image in its catalog yet. Build it on {host} from its reviewed source and add it UNSIGNED?", { entry: p.entry || body.entry, host }),
      t("Source: {repository}, commit {commit}, {path}; source archive SHA-256 {sha256}; catalog {catalog} ({signed}).", { repository: p.repository || "?", commit: p.commit || "?", path: p.path || "?",
        sha256: p.archive_sha256 || "?", catalog: p.catalog_label || "?", signed: p.signature === "good" ? t("signed: the entry was reviewed, its image is not published yet") : t("NOT signed: nobody vouched for the entry") }),
      mounts ? t("Its folders: {mounts}.", { mounts }) : null,
      t("Podman builds it as the catalog's CI does (base images pinned by digest) and its smoke test checks it with sandbox-ai's own run line; that takes minutes. Built here, not signed: nobody signed this image.")].filter(Boolean).join(" ");
    confirmIn(container, question, t("Build it, unsigned"), async () => {
      try { done(await send({ commit: p.commit, source_sha256: p.archive_sha256, confirm_build: true, confirm: true })); }
      catch (e) { fail(e); }
    }, { kind: "primary", onCancel: () => fail(asked) });
  })();
}

// A stopped start with a kept HOME: first what changed in the files of that HOME that decide what runs at a start
// (resume --review-home); when something did, or not everything could be checked, it is shown and asked first.
// WITH (More…): {body: what the resume adds (asked for already), done(result), fail(error)}.
async function reviewThenResume(main, s, button, withMore) {
  const host = s.host;
  const old = document.getElementById("review-card");
  if (old) dropCard(old);
  const resume = async () => {
    try {
      const r = await api("POST", hostPath(`/starts/${encodeURIComponent(s.name)}/resume`, host), withMore ? withMore.body : {});
      const card = document.getElementById("review-card");
      if (card) dropCard(card);
      if (withMore) { withMore.done(r); return; }
      doneToast(t("{name} on {host} resumed", { name: s.name, host }), r);
      if (state.page === "starts") refreshStarts(main, true);
    } catch (e) { if (withMore) withMore.fail(e); else failed(t("Could not resume {name} on {host}", { name: s.name, host }))(e); }
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
  if (view.focused) markFocused(view);
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

// The start the system monitor's Session asked for (state.startsFocus): its row unfolded, marked, scrolled to and
// focused (marked again when a poll draws it anew); not listed (it ended): said.
function focusStart(view, main) {
  const wanted = state.startsFocus;
  if (!wanted) return;
  state.startsFocus = null;
  const entries = [...view.rows.values()].filter(e => e.start.host === wanted.host);
  const entry = entries.find(e => wanted.run && e.start.run === wanted.run) || entries.find(e => e.start.name === wanted.name || e.start.id === wanted.name);
  if (!entry) { toast("warning", t("Session"), t("{name} is not listed among the starts of {host}: it has ended.", { name: wanted.name, host: wanted.host })); return; }
  setOpen(hostOpenKey(wanted.host), true);
  const tree = startTree(entries.map(e => e.start));
  for (const root of tree.roots) if ((tree.children.get(startKey(root)) || []).some(kid => startKey(kid) === startKey(entry.start))) setOpen(startOpenKey(root), true);
  layoutStarts(view, main);
  view.focused = startKey(entry.start);
  markFocused(view);
  if (entry.row.scrollIntoView) entry.row.scrollIntoView({ block: "center" });
  entry.row.focus();
  state.rowFocused = true;
}

function markFocused(view) {
  for (const [key, entry] of view.rows) {
    entry.row.classList.toggle("focused", key === view.focused);
    if (key === view.focused) entry.row.setAttribute("tabindex", "-1");
  }
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
    all ? null : el("button", { onclick: () => gcCard(main, host) }, t("Clean up…")),
    all ? null : withIcon(el("button", { "aria-label": t("Optimize…"), onclick: () => storageCard(main, host, null) }, t("Optimize…")), "optimize", t("Optimize…"), t("what could be freed, each with how much, its command and whether it is safe while starts run; tick and apply (sandbox-ai gc --check)")),
    all ? null : el("button", { onclick: () => importCard(main, host) }, t("Import a board…")),
    all ? null : el("button", { title: t("an archive of sandbox-ai session export as a session to resume, checked first (sandbox-ai session import)"), onclick: () => sessionImportCard(main, host) }, t("Import a session…")), ...view.folds,
    el("label", { title: t(BUTTON_TEXTS_HELP) }, el("input", { type: "checkbox", id: "button-texts", checked: buttonTextsShown(), onchange: (event) => showButtonTexts(event.target.checked) }), " ", t("Show button texts beside the icons")));
  // Two tabs, as the desktop app's Sessions page has them: the starts (with a new start's form) and the schedules
  const parts = { starts: el("div", { id: "starts-panel", role: "tabpanel", "aria-label": t("Sessions & sandboxes") }),
    schedules: el("div", { id: "schedules-panel", role: "tabpanel", "aria-label": t("Schedules") }) };
  const tabs = el("div", { class: "tabs", role: "tablist", "aria-label": t("Starts and sessions") }, STARTS_TABS.map(([key, label]) =>
    el("button", { role: "tab", "data-tab": key, "aria-controls": parts[key].id, disabled: key === "schedules" && all, title: key === "schedules" && all ? t("Choose a host: schedules are each host's own.") : undefined,
      onclick: () => { chooseStartsTab(key); drawStartsTabs(tabs, parts, all); } }, t(label))));
  clear(main).append(el("h1", {}, title()), tabs, parts.starts, parts.schedules);
  parts.starts.append(tools, view.slot, view.status, el("div", { class: "table-scroll" }, view.table), view.empty);
  drawStartsTabs(tabs, parts, all);
  view.focused = null;
  applyStarts(view, found, main);
  focusStart(view, main);
  // a new start: in a terminal of the page, detachable like every start (Ctrl-b d), on the host chosen in its form
  const target = state.formHost && state.hosts.some(h => h.name === state.formHost) ? state.formHost : all ? state.host : host;
  const heading = el("h2", {}, t("New start on {host}", { host: target }));
  const form = el("div", { class: "card", id: "new-start" });
  parts.starts.append(heading, form);
  await drawStartForm(form, heading, target);
  if (!all && live()) await schedulesSection(parts.schedules, host, live);
};

// The starts page's tabs: [key, label]; the one chosen is kept in this browser (a page with every host shows the
// starts: schedules are each host's own).
const STARTS_TABS = [["starts", N_("Sessions & sandboxes")], ["schedules", N_("Schedules")]];
const STARTS_TAB_STORE = "sandbox-ai-starts-tab";
const BUTTON_TEXTS_HELP = N_("On: the icon buttons of the starts' rows show their texts too. Off (default): the icons alone, each button's text first in its tooltip. Kept in this browser.");
function startsTab() {
  let chosen = null;
  try { chosen = localStorage.getItem(STARTS_TAB_STORE); } catch (e) { chosen = null; }
  return STARTS_TABS.some(([key]) => key === chosen) ? chosen : "starts";
}
function chooseStartsTab(key) { try { localStorage.setItem(STARTS_TAB_STORE, key); } catch (e) { /* this page only */ } state.startsTab = key; }
function drawStartsTabs(tabs, parts, all) {
  const shown = all ? "starts" : (state.startsTab || startsTab());
  for (const tab of tabs.children) {
    const key = tab.getAttribute("data-tab");
    tab.setAttribute("aria-selected", String(key === shown));
    tab.className = key === shown ? "active" : "";
  }
  parts.starts.hidden = shown !== "starts";
  parts.schedules.hidden = shown !== "schedules";
}

// Scheduled runs of a host (sandbox-ai schedule there): each schedule with when it runs, the agent, its ask mode, the
// next and the last run; Run now, Pause/Resume, Edit, History (each run's report) and Remove… per row; a form to add
// one (Edit fills it: saved under the same name it replaces it, its history stays); the timer of that host on or off.
// Add, Remove… and the timer are asked first (schedule-add, schedule-remove, schedule-timer).
// (the schedule's --ask: an object, its keys in this order)
const SCHEDULE_ASK = { off: N_("off: refused, nobody answers (the default)"), allow: N_("allow: every question allowed and recorded"),
  on: N_("on: questions wait for an answer") };
const SCHEDULE_WHEN = [["every", N_("every (30m, 2h, 1d)")], ["at", N_("at (HH:MM daily, or YYYY-MM-DD HH:MM once)")], ["cron", N_("cron (M H DOM MON DOW)")], ["", N_("only on push")]];

async function schedulesSection(main, host, live) {
  const box = el("div", {}, el("p", { class: "hint" }, t("Loading…")));
  main.append(el("h2", { id: "schedules" }, t("Schedules on {host}", { host })), box);
  let data;
  try { data = await api("GET", hostPath("/schedules", host)); } catch (e) { if (live()) clear(box).append(errorNotice(e)); return; }
  if (!live()) return;
  const schedules = Array.isArray(data && data.schedules) ? data.schedules.filter(x => x && typeof x.name === "string") : [];
  const timer = (data && data.timer) || {};
  const result = el("div", { class: "wide check-result" });
  const history = el("div");
  const shown = (seconds) => (typeof seconds === "number" ? new Date(seconds * 1000).toLocaleString() : "-");
  // the form
  const field = (id, attrs) => el("input", Object.assign({ id, class: "mono", autocomplete: "off", spellcheck: "false" }, attrs || {}));
  const name = field("schedule-name", { placeholder: "nightly" });
  const whenKind = el("select", { id: "schedule-when" }, SCHEDULE_WHEN.map(([key, label]) => el("option", { value: key }, t(label))));
  const whenValue = field("schedule-when-value", { placeholder: "02:30", "aria-label": t("When") });
  const onPush = field("schedule-on-push", { placeholder: "/home/you/src/app" });
  const agent = el("select", { id: "schedule-agent" }, el("option", { value: "" }, t("(the board's)")), AGENTS.map(a => el("option", { value: a }, a)));
  agent.value = "claude";
  const options = field("schedule-options", { placeholder: "--offline -w /home/you/src/app" });
  const prompt = el("textarea", { id: "schedule-prompt", rows: 3, placeholder: t("Review yesterday's commits") });
  const agentArgs = field("schedule-agent-args", { placeholder: "exec \"review\"" });
  const preset = field("schedule-preset");
  const board = field("schedule-board");
  const workdir = field("schedule-workdir", { placeholder: "/home/you/src/app" });
  const ask = el("select", { id: "schedule-ask" }, Object.entries(SCHEDULE_ASK).map(([key, label]) => el("option", { value: key }, t(label))));
  const limit = field("schedule-limit", { placeholder: "60m" });
  const missed = el("select", { id: "schedule-missed" }, el("option", { value: "once" }, t("once on wake")), el("option", { value: "skip" }, t("skip them")));
  const commit = field("schedule-commit", { placeholder: "nightly-result" });
  const push = field("schedule-push", { placeholder: "origin" });
  const notify = el("input", { type: "checkbox", id: "schedule-notify", checked: true });
  let replace = false;
  const fill = (x) => {  // a schedule into the form (Edit), or an empty form (New)
    replace = Boolean(x);
    const timing = (x && x.timing) || null;
    name.value = x ? x.name : ""; whenKind.value = timing ? timing.kind : x ? "" : "every"; whenValue.value = timing ? String(timing.value) : "";
    onPush.value = (x && x.on_push) || ""; agent.value = x ? (x.board ? "" : x.agent || "") : "claude";
    options.value = x && Array.isArray(x.options) ? x.options.map(o => (/[\s"']/.test(o) ? JSON.stringify(o) : o)).join(" ") : "";
    agentArgs.value = x && Array.isArray(x.agent_args) ? x.agent_args.map(o => (/[\s"']/.test(o) ? JSON.stringify(o) : o)).join(" ") : "";
    prompt.value = (x && x.prompt) || ""; preset.value = (x && x.preset) || ""; board.value = (x && x.board) || "";
    workdir.value = (x && x.workdir) || ""; ask.value = (x && x.ask) || "off";
    limit.value = x && x.limit ? `${Math.round(x.limit / 60)}m` : ""; missed.value = (x && x.missed) || "once";
    commit.value = (x && x.commit) || ""; push.value = (x && x.push) || ""; notify.checked = x ? x.notify !== false : true;
    name.focus();
  };
  const body = () => {
    const b = { name: name.value.trim(), ask: ask.value, missed: missed.value, notify: notify.checked, replace };
    if (whenKind.value && whenValue.value.trim()) b[whenKind.value] = whenValue.value.trim();
    for (const [key, input] of [["on_push", onPush], ["agent", agent], ["options", options], ["agent_args", agentArgs], ["prompt", prompt],
      ["preset", preset], ["board", board], ["workdir", workdir], ["limit", limit], ["commit", commit], ["push", push]]) {
      const v = input.value.trim(); if (v) b[key] = v;
    }
    return b;
  };
  const row = el("div", { class: "row" });
  const save = (event) => busy(event.target, async () => {
    clear(result);
    const b = body();
    try { await api("POST", hostPath("/schedules", host), Object.assign({}, b, { check_only: true })); }
    catch (e) { result.append(errorNotice(e, t("The schedule was not saved"))); return; }
    const send = async () => {
      try { const r = await api("POST", hostPath("/schedules", host), Object.assign({}, b, { confirm: true })); doneToast(t("Saved"), r); show("starts"); }
      catch (e) { result.append(errorNotice(e, t("The schedule was not saved"))); }
    };
    if (!needsConfirm("schedule-add")) { await send(); return; }
    confirmIn(row, t("Let {host} start {agent} by itself as the schedule {name} says? Each run is a normal start under its rules; its questions: {ask}.", { host, agent: b.agent || t("(the board's)"), name: b.name, ask: b.ask }), t("Yes, save it"), send, { kind: "primary", trigger: event.target });
  });
  row.append(el("button", { class: "primary", onclick: save }, t("Save schedule…")), el("button", { onclick: () => { fill(null); clear(result); } }, t("New")));
  // the history of one schedule, with each run's report (its summary and the start's sandbox-ai report, Markdown)
  const showHistory = async (x) => {
    clear(history).append(el("p", { class: "hint" }, t("Loading…")));
    let found;
    try { found = await api("GET", hostPath(`/schedules/${encodeURIComponent(x.name)}/history`, host)); } catch (e) { clear(history).append(errorNotice(e)); return; }
    const report = el("div");
    const runs = (Array.isArray(found && found.runs) ? found.runs : []).slice().reverse().map(r => {
      const open = r.summary || r.report ? rowButton(t("Report"), String(r.run), host, { onclick: (event) => busy(event.target, async () => {
        clear(report);
        try { const m = await api("GET", hostPath(`/schedules/${encodeURIComponent(x.name)}/report/${encodeURIComponent(r.run)}`, host)); report.append(el("pre", { class: "report" }, String(m.markdown || ""))); }
        catch (e) { report.append(errorNotice(e)); }
      }) }) : el("span", { class: "hint" }, String(r.note || ""));
      const changed = r.changes ? t("{files} file(s), {commits} commit(s)", { files: r.changes.files, commits: r.changes.commits }) : "-";
      return el("tr", {}, el("td", { class: "mono" }, String(r.run || "")), el("td", {}, String(r.reason || "")), el("td", {}, shown(r.started)),
        el("td", {}, String(r.status || "")), el("td", {}, `${Math.round(r.seconds || 0)} s`), el("td", {}, changed), el("td", { class: "actions" }, open));
    });
    clear(history).append(el("h3", {}, t("History of {name}", { name: x.name })),
      table([t("Run"), t("Why"), t("Started"), t("Status"), t("Duration"), t("Changed"), ""], runs, t("No runs yet.")), report);
  };
  const act = (x, path, title) => (event) => busy(event.target, async () => {
    try { const r = await api("POST", hostPath(`/schedules/${encodeURIComponent(x.name)}/${path}`, host), {}); doneToast(title, r); show("starts"); }
    catch (e) { failed(title)(e); }
  });
  const rows = schedules.map(x => {
    const actions = el("td", { class: "actions" });
    const remove = rowButton(t("Remove…"), x.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Remove the schedule {name} on {host}? It starts nothing any more; its history and reports go too.", { name: x.name, host }), t("Yes, remove it"), async () => {
      try { await api("POST", hostPath(`/schedules/${encodeURIComponent(x.name)}/remove`, host), { confirm: true }); show("starts"); } catch (e) { failed(t("Could not remove it"))(e); }
    }, { trigger: remove }) });
    actions.append(
      rowButton(t("Run now"), x.name, host, { onclick: act(x, "run", t("Started: {name}", { name: x.name })), disabled: x.running }),
      x.paused ? rowButton(t("Resume"), x.name, host, { onclick: act(x, "resume", t("Resumed: {name}", { name: x.name })) })
        : rowButton(t("Pause"), x.name, host, { onclick: act(x, "pause", t("Paused: {name}", { name: x.name })) }),
      rowButton(t("Edit"), x.name, host, { onclick: () => fill(x) }),
      rowButton(t("History"), x.name, host, { onclick: () => showHistory(x) }), remove);
    const last = x.last ? `${x.last.status} ${shown(x.last.started)}` : "-";
    return el("tr", {}, el("td", { class: "mono" }, x.name), el("td", {}, String(x.when || "")), el("td", {}, String(x.board ? `${x.agent} (${x.board})` : x.agent || "")),
      el("td", {}, String(x.ask || "")), el("td", {}, shown(x.next)), el("td", {}, last), el("td", {}, stateText(x.state)), actions);
  });
  const timerOn = Boolean(timer.installed) && timer.active !== false;
  const timerRow = el("div", { class: "row" });
  const timerButton = el("button", { onclick: (event) => confirmIn(timerRow, timerOn ? t("Turn the timer of {host} off? No schedule starts by itself until it is on again.", { host })
    : t("Turn the timer of {host} on? It runs sandbox-ai schedule tick every minute as that host's user (a systemd user timer or a LaunchAgent).", { host }),
  timerOn ? t("Yes, turn it off") : t("Yes, turn it on"), async () => {
    try { const r = await api("POST", hostPath("/schedules-timer", host), { on: !timerOn, confirm: true }); doneToast(t("Timer"), r); show("starts"); } catch (e) { failed(t("Timer"))(e); }
  }, { trigger: event.target }) }, timerOn ? t("Turn the timer off…") : t("Turn the timer on…"));
  timerRow.append(el("span", {}, timerOn ? t("Timer: on") : t("Timer: off - nothing starts by itself")), timerButton);
  const form = el("div", { class: "form" },
    el("label", { for: "schedule-name" }, t("Name")), name,
    el("label", { for: "schedule-when" }, t("When")), el("div", { class: "row" }, whenKind, whenValue),
    el("label", { for: "schedule-on-push" }, t("Also on a push to (a repository of that host)")), onPush,
    el("label", { for: "schedule-agent" }, t("Agent")), agent,
    el("label", { for: "schedule-prompt" }, t("Task")), prompt,
    el("label", { for: "schedule-options" }, t("Start options")), options,
    el("label", { for: "schedule-agent-args" }, t("Agent arguments (its one-shot form, without a task)")), agentArgs,
    el("label", { for: "schedule-preset" }, t("Preset")), preset,
    el("label", { for: "schedule-board" }, t("Board (kept on that host)")), board,
    el("label", { for: "schedule-workdir" }, t("Workspace")), workdir,
    el("label", { for: "schedule-ask" }, t("Questions")), ask,
    el("label", { for: "schedule-limit" }, t("Time limit")), limit,
    el("label", { for: "schedule-missed" }, t("Runs missed while off")), missed,
    el("label", { for: "schedule-commit" }, t("Commit the changes to the branch")), commit,
    el("label", { for: "schedule-push" }, t("Then push it to the remote")), push,
    el("label", { for: "schedule-notify" }, t("Notify when a run ends")), notify,
    el("span"), row, result);
  clear(box).append(
    el("p", { class: "hint" }, t("An agent started on a timetable or a trigger, with a report of each run. Nobody answers questions at night: the ask mode off refuses what is not allowed.")),
    timerRow,
    table([t("Name"), t("When"), t("Agent"), t("Questions"), t("Next run"), t("Last run"), t("State"), ""], rows, t("No schedules on this host.")),
    history, el("div", { class: "card" }, form));
}

// The preset categories of a sandbox-ai without `presets categories` (before 0.1.94), in the page's words. A newer
// host says its own (presets.CATEGORY_INFO: the one list, with English and German titles): presetGroups.
const PRESET_CATEGORIES = [["simple", N_("Simple")], ["development", N_("Development")], ["llm", N_("Local models")], ["analysis", N_("Analysis")],
  ["research", N_("Research")], ["media", N_("Media")], ["privacy", N_("Privacy")], ["testing", N_("Testing")], ["fun", N_("Fun")]];
// The older categories a newer host still takes, and where it lists them (presets.LEGACY_CATEGORIES).
const LEGACY_PRESET_CATEGORIES = { simple: "other", analysis: "security", media: "fun" };

// [[key, label, category of a preset -> key]] of the host's categories (CATEGORIES: `presets categories --json` there,
// in order, each title in the page's language), else the page's own list; a category no list names comes last.
function presetGroups(categories, presets) {
  if (Array.isArray(categories) && categories.length) {
    const keys = categories.map(c => String(c.id));
    const of = (p) => { const c = LEGACY_PRESET_CATEGORIES[p.category] || p.category || "other"; return keys.includes(c) ? c : "other"; };
    return { of, groups: categories.map(c => [String(c.id), String((c.title && (c.title[LANGUAGE] || c.title.en)) || c.id)]) };
  }
  const known = new Set(PRESET_CATEGORIES.map(([key]) => key));
  const of = (p) => p.category || "simple";
  const extra = [...new Set(presets.map(of))].filter(c => !known.has(c)).map(c => [c, c]);
  return { of, groups: [...PRESET_CATEGORIES.map(([key, label]) => [key, t(label)]), ...extra] };
}

// A preset's title in the page's language (its "translations"), else its own.
function presetTitle(p) {
  const own = p && p.translations && p.translations[LANGUAGE];
  return String((own && own.title) || (p && p.title) || "");
}

// A preset that starts something not live-tested yet (presets list --json "experimental", from the launcher's one
// list EXPERIMENTAL_PARTS): {names}, else null. A badge only: listed and started like any other.
function presetExperimental(p) {
  const badge = p && p.experimental && typeof p.experimental === "object" ? p.experimental : null;
  return badge && Array.isArray(badge.names) ? badge : null;
}

function experimentalText(badge) {
  return t("Not live-tested yet: {names}", { names: badge.names.filter(n => typeof n === "string").join(", ") });
}

function experimentalBadge(p) {
  const badge = presetExperimental(p);
  return badge ? el("span", { class: "badge experimental", title: experimentalText(badge) }, t("not live-tested yet")) : null;
}

// Whether a preset's VPN value must name a Tor profile (the tor preset's: its name or label says Tor).
function torValue(key, spec) { return key === "tor" || /\bTor\b/.test(String((spec && spec.label) || "")); }

// What a preset's bridge (presets show --json: "bridge") proposes, in words: made only in the start's terminal, once it
// runs, on the human's yes (sandbox-ai asks there, with bridge add's warnings); "" for a preset without one.
function presetBridgeText(bridge) {
  if (!bridge || typeof bridge !== "object" || !Array.isArray(bridge.allow)) return "";
  const what = bridge.allow.filter(x => typeof x === "string").join(", ")
    + (Array.isArray(bridge.observer_actions) && bridge.observer_actions.length ? ` (${t("observer actions: {actions}", { actions: bridge.observer_actions.join(", ") })})` : "")
    + (bridge.both === true ? `, ${t("both ways")}` : "");
  return (bridge.direction === "in" ? t("It proposes a bridge: the session you choose may reach this start ({what}).", { what })
    : t("It proposes a bridge: this start may reach the session you choose ({what}).", { what }))
    + " " + t("Nothing is made by itself: once the start runs, its terminal asks you, with the warnings of bridge add, and makes it only on your yes.");
}

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
  // a server may list 70 models or more: typing here shows only those whose name holds the text
  const modelFilter = el("input", { type: "search", id: "start-model-filter", class: "mono", placeholder: t("Filter"), "aria-label": t("Filter the models"), autocomplete: "off", hidden: true });
  // as the app's wizard has it: a server chosen, nothing else (no cloud provider) unless unticked
  const onlyServer = el("input", { type: "checkbox", id: "start-only-server", checked: true });
  // Claude Code at a server of your own: the tools the proxy keeps in its requests (--llm-tools; core by default)
  const claudeTools = el("select", { id: "start-llm-tools", "aria-label": t("Claude Code's tools for the local model") },
    el("option", { value: "" }, t("those local models use (core)")), el("option", { value: "all" }, t("all of Claude Code's tools")));
  const claudeToolsRow = el("label", { hidden: true, title: t("Claude Code's tools that local models never used (Agent, Monitor, Workflow, Cron ...: ~10k tokens per request) are left out; Bash, Read, Edit, Write, Glob, Grep, TodoWrite and every MCP tool stay. Other lists: --llm-tools core,TOOL in the options.") },
    t("Claude Code's tools:"), " ", claudeTools);
  const modelHint = el("span", { class: "hint" });
  // the effort (--effort): the levels the cloud model chosen takes (sandbox-ai models --json of that host: the same
  // table the launcher refuses by), else the agent's; a level the next model does not take is reset, said below it
  const effort = el("select", { id: "start-effort", "aria-describedby": "start-effort-hint" });
  const effortHint = el("span", { class: "hint", id: "start-effort-hint" });
  let cloud = null;  // that host's table of cloud models (models --json), null: an older sandbox-ai without it
  // the use case (llm recommend --json on that host): picks the agent and a model of that host's servers, measured best
  // for it, and starts with --use-case (its settings for that model in that agent over the model profile, under yours)
  const useCase = el("select", { id: "start-use-case", "aria-describedby": "start-use-case-hint" },
    el("option", { value: "" }, t("none: choose the agent and the model yourself")));
  const useCaseHint = el("div", { class: "hint", id: "start-use-case-hint" });
  const useCaseKnown = el("ul", { class: "use-case-known", id: "start-use-case-known", "aria-label": t("What we know") });
  // its pairs: every model of the chosen server with the agent it was measured best in, best first (not measured and
  // unsafe ones too, with why); "Use this pair" sets the agent and the model
  const useCasePairs = el("ul", { class: "use-case-pairs", id: "start-use-case-pairs", "aria-label": t("Recommended pairs") });
  let recommended = null;
  const servers = [], profiles = [], mcpNames = new Set();
  let presets = [], presetCategories = [], chosen = null, fields = [];
  const presetsLoaded = { done: false, error: null };
  const cloudOf = () => { const value = String(model.value || ""); return value.startsWith(CLOUD_MARK) ? value.slice(CLOUD_MARK.length) : ""; };
  const syncOnly = () => {
    const server = Boolean(model.value) && !cloudOf();
    onlyServer.disabled = !server || model.disabled;
    if (onlyServer.disabled && !server) onlyServer.title = t("only with a server of its own: the cloud is the agent's provider"); else onlyServer.removeAttribute("title");
    claudeToolsRow.hidden = !(agentOf() === "claude" && server && !model.disabled);
  };
  const drawEfforts = (note) => {
    const was = effort.value;
    const entry = cloudAgent(cloud, agentOf());
    const levels = cloudEfforts(cloud, agentOf(), cloudOf());
    const listed = cloudModel(cloud, agentOf(), cloudOf());
    const fallback = listed && listed.default_effort ? t("the agent's own default ({level} for this model)", { level: listed.default_effort }) : t("the agent's own default");
    clear(effort).append(el("option", { value: "" }, fallback), ...levels.map(level => el("option", { value: level }, level)));
    effort.value = levels.includes(was) ? was : "";
    effort.disabled = !levels.length;
    effort.title = !entry ? t("this host's sandbox-ai has no table of cloud models yet: --effort in the options")
      : !(entry.efforts || []).length ? `${t("{agent} sets its effort per model in its own configuration", { agent: agentOf() })}${entry.effort_note ? `: ${entry.effort_note}` : ""}` : "";
    effortHint.textContent = note && was && !effort.value ? t("The effort {level} was reset: {model} does not take it.", { level: was, model: cloudOf() || t("the agent's default model") }) : "";
  };
  const drawModels = () => {
    const was = model.value;
    clear(model).append(el("option", { value: "" }, t("the agent's provider (cloud)")));
    // the agent's cloud models, grouped by provider (sandbox-ai models --json of that host)
    for (const [provider, rows] of cloudGroups(cloud, agentOf())) {
      model.append(el("optgroup", { label: t("Cloud: {provider}", { provider }) }, rows.map(m =>
        el("option", { value: CLOUD_MARK + m.id, title: cloudTitle(m) }, `${m.name} — ${m.id}`))));
    }
    for (const server of servers) {
      const why = modelProblem(server, agentOf());
      const label = `${server.name}${server.model ? ` (${server.model})` : ""}${why ? ` — ${why}` : ""}`;
      model.append(el("option", { value: server.name, disabled: Boolean(why), title: why || null }, label));
      for (const m of server.models || []) {
        const about = (server.about || {})[m] || {};
        const extra = [about.loaded === true ? t("loaded") : null, wholeNumber(about.size) ? gbText(about.size) : null].filter(Boolean);
        const light = supportOf(about.support);  // the research's traffic light, in words (a select has no colours)
        model.append(el("option", { value: `${server.name}:${m}`, disabled: Boolean(why), title: why || (light ? lightSummary(light) : null) },
          `${server.name}:${m}${extra.length ? `  (${extra.join(", ")})` : ""}${lightSuffix(light)}`));
      }
    }
    const still = [...model.querySelectorAll("option")].find(o => o.value === was && !o.disabled);
    model.value = still ? was : "";
    modelFilter.hidden = model.querySelectorAll("option").length <= 15;
    filterModels();
    // a preset that names the LLM server itself: the model is its value
    const byPreset = fields.some(f => f.spec.kind === "llm");
    model.disabled = byPreset;
    if (byPreset) model.value = "";
    modelHint.textContent = byPreset ? t("the preset chooses the LLM server (below)") : "";
    syncOnly();
    drawEfforts(false);
    for (const f of fields) if (f.spec.kind === "llm") f.redraw();
  };
  model.addEventListener("change", () => { syncOnly(); drawEfforts(true); });
  effort.addEventListener("change", () => { effortHint.textContent = ""; });
  function filterModels() {
    const words = String(modelFilter.value || "").trim().toLowerCase();
    for (const option of model.querySelectorAll("option")) option.hidden = Boolean(words) && Boolean(option.value) && option.value !== model.value && !option.value.toLowerCase().includes(words) && !option.textContent.toLowerCase().includes(words);
  }
  modelFilter.addEventListener("input", filterModels);
  const preset = el("select", { id: "preset" });
  const presetHint = el("span", { class: "hint" }, "");
  const presetValues = el("div", { class: "form preset-values wide" });
  const presetMcp = el("div", { class: "preset-mcp wide" });
  // the presets of that host for the chosen agent, by category
  const drawPresets = () => {
    const was = preset.value;
    clear(preset).append(el("option", { value: "" }, t("none")));
    const usable = presets.filter(p => !Array.isArray(p.agents) || p.agents.includes(agentOf()));
    const { of, groups } = presetGroups(presetCategories, usable);
    for (const [key, label] of groups) {
      const group = usable.filter(p => of(p) === key).sort((a, b) => String(a.id).localeCompare(String(b.id)));
      if (group.length) preset.append(el("optgroup", { label }, group.map(p => el("option", { value: p.id, title: presetExperimental(p) ? experimentalText(presetExperimental(p)) : null }, `${p.id}: ${presetTitle(p)}${presetExperimental(p) ? ` (${t("not live-tested yet")})` : ""}`))));
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
          for (const m of server.models || []) {
            const light = supportOf(((server.about || {})[m] || {}).support);
            input.append(el("option", { value: `${server.name}:${m}`, disabled: Boolean(why), title: light ? lightSummary(light) : null }, `${server.name}:${m}${lightSuffix(light)}`));
          }
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
      input = el("input", { id, class: ["folder", "file", "device", "host", "secret", "session"].includes(kind) ? "mono" : null,
        placeholder: spec.default !== undefined ? String(spec.default) : kind === "folder" ? t("{path} (on that host)", { path: "/home/you/samples" }) : kind === "device" ? "/dev/ttyUSB0"
          : kind === "session" ? t("a running session (empty: chosen in the terminal once the start runs)") : "" });
      if (kind === "secret") offerNames(input, "secret", host);  // that host's secrets to choose from (names only)
      if (kind === "session") offerNames(input, "session", host);  // that host's running sessions (a preset's bridge)
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
    const proposed = presetBridgeText(chosen.bridge);
    if (proposed) presetMcp.append(el("p", { class: "hint status-warn", id: "preset-bridge" }, proposed));
    for (const note of (Array.isArray(chosen.notes) ? chosen.notes : []).slice(0, 3)) presetMcp.append(el("p", { class: "hint" }, String(note)));
    drawModels();
  };
  preset.addEventListener("change", choosePreset);
  const drawUseCases = () => {
    const was = useCase.value;
    clear(useCase).append(el("option", { value: "" }, t("none: choose the agent and the model yourself")));
    for (const found of recommended || []) {
      useCase.append(el("option", { value: found.use_case, title: String(found.about || "") || null },
        `${t(String(found.label || found.use_case))} — ${t(USE_CASE_STATUS[found.status] || String(found.status || ""))}`));
    }
    useCase.value = [...useCase.children].some(o => o.value === was) ? was : "";
  };
  // the agent chosen and what that model's support says of it (llm models --json's "support": model_profiles.support)
  const supportFor = (pair) => {
    const about = ((servers.find(x => x.name === pair.server) || {}).about || {})[pair.model];
    return supportOf(about && about.support);
  };
  // what is known of PAIR's model in AGENTNAME, as the app says it: the use case table's rank of it there (llm
  // recommend --json's in_agents: "rank 3, from other tasks"), else the profile's status (llm models --json's support,
  // else in_agents' own), else untested
  const statusIn = (pair, agentName) => {
    const known = pair.in_agents && typeof pair.in_agents === "object" ? pair.in_agents[agentName] : null;
    const rank = known && Number.isInteger(known.rank) && known.rank > 0 ? known.rank : null;
    if (rank) return known.measured ? t("rank {rank}, measured", { rank }) : t("rank {rank}, from other tasks", { rank });
    const s = supportFor(pair);
    const own = s && Array.isArray(s.harnesses) ? s.harnesses.find(h => h && h.agent === agentName) : null;
    const status = own ? own.status : s && textList(s.untested).includes(agentName) && s.status ? s.status
      : known && typeof known.status === "string" && known.status ? known.status : "untested";
    return STATUS_WORDS[status] ? t(STATUS_WORDS[status]) : String(status);
  };
  // the agent's name as that host's sandbox-ai says it (support's "names", else the use case's harness_names), else
  // its id
  const agentLabel = (pair, agentName, found) => {
    const s = supportFor(pair);
    if (s && typeof namesOf(s)[agentName] === "string") return namesOf(s)[agentName];
    const names = found && found.harness_names && typeof found.harness_names === "object" ? found.harness_names : {};
    return typeof names[agentName] === "string" ? names[agentName] : agentName;
  };
  const drawPairs = () => {
    clear(useCasePairs);
    const found = (recommended || []).find(r => r.use_case === useCase.value);
    if (!found) return;
    const chosenServer = String(model.value || "").startsWith(CLOUD_MARK) ? "" : String(model.value || "").split(":")[0];
    const pairs = (Array.isArray(found.pairs) ? found.pairs : []).filter(p => p && typeof p.model === "string" && typeof p.server === "string"
      && (!chosenServer || p.server === chosenServer));
    for (const p of pairs) {
      const harness = String(p.harness_name || p.harness || "") || t("any agent");
      const where = p.unsafe ? t("⚠ unsafe for this use case") : p.source === "table"
        ? (p.basis === "measured" ? t("rank {rank}, measured", { rank: p.rank }) : t("rank {rank}, from other tasks", { rank: p.rank }))
        : t("not measured for this use case");
      const lines = [el("strong", {}, `${p.model} × ${harness}`), ` — ${where}${p.fit_text ? ` · ${p.fit_text}` : ""}`];
      const why = p.unsafe ? t("Unsafe for this use case: {reason}", { reason: String(p.unsafe) }) : String(p.why || "");
      const more = [why, p.evidence ? t("Measured: {evidence}", { evidence: [p.evidence, p.date].filter(Boolean).join(", ") }) : ""];
      if (p.harness && agent.value !== p.harness) {
        more.push(p.source === "not measured"
          ? t("{model} is not measured for this use case, best by its traffic light in {harness}; in {agent}: {status}", { model: p.model, harness, agent: agentLabel(p, agent.value, found), status: statusIn(p, agent.value) })
          : t("{model} is measured best in {harness}; in {agent}: {status}", { model: p.model, harness, agent: agentLabel(p, agent.value, found), status: statusIn(p, agent.value) }));
      }
      const value = `${p.server}:${p.model}`;
      const take = () => {
        if (p.harness && AGENTS.includes(p.harness) && agent.value !== p.harness) { agent.value = p.harness; drawModels(); drawPresets(); }
        if ([...model.children].some(o => o.value === value && !o.disabled)) { model.value = value; syncOnly(); }
        drawPairs();
      };
      // an unsafe pair: asked in place first, with why (Cancel puts the button back)
      const holder = el("span", { class: "row" });
      holder.append(el("button", { class: "small", type: "button", onclick: () => (p.unsafe
        ? confirmIn(holder, t("The research marks {model} unsafe for this use case: {reason}. With untrusted input it may follow a planted instruction again. Use it anyway?", { model: p.model, reason: String(p.unsafe) }), t("Use it anyway"), take)
        : take()) }, t("Use this pair")));
      useCasePairs.append(el("li", { class: p.unsafe ? "status-warn" : p.source === "table" ? null : "muted" }, ...lines,
        el("div", { class: "hint" }, more.filter(Boolean).join(" · ")), holder));
    }
  };
  const chooseUseCase = () => {
    const found = (recommended || []).find(r => r.use_case === useCase.value);
    clear(useCaseKnown); useCaseHint.textContent = "";
    if (!found) { drawPairs(); return; }
    const c = found.choice;
    if (c) {
      if (AGENTS.includes(c.harness) && agent.value !== c.harness) { agent.value = c.harness; drawModels(); drawPresets(); }
      const value = `${c.server}:${c.model}`;
      if ([...model.children].some(o => o.value === value && !o.disabled)) { model.value = value; syncOnly(); }
    }
    useCaseHint.textContent = useCaseText(found);
    for (const [sign, texts] of [["✗", [...((c && c.fails) || []), ...(found.fails || [])]], ["?", found.unknown || []]])
      for (const text of texts) useCaseKnown.append(el("li", { class: sign === "✗" ? "status-warn" : null }, `${sign} ${text}`));
    drawPairs();
  };
  useCase.addEventListener("change", chooseUseCase);
  agent.addEventListener("change", () => { drawModels(); drawPresets(); drawPairs(); });
  model.addEventListener("change", () => drawPairs());
  drawModels(); drawPresets();
  // what that host has: its LLM servers (and their models), presets, VPN profiles and MCP servers, asked at once
  const asked = [
    api("GET", hostPath("/llm", host)).then((list) => {
      for (const server of (Array.isArray(list) ? list : []).filter(x => x && typeof x.name === "string")) {
        servers.push(server);
        api("GET", hostPath(`/llm/${encodeURIComponent(server.name)}/models`, host)).then((models) => {
          const listed = (Array.isArray(models) ? models : []).filter(m => m && typeof m.name === "string" && m.name);
          server.models = listed.map(m => m.name);  // every one of them, however many
          server.about = Object.fromEntries(listed.map(m => [m.name, { size: m.size, loaded: m.loaded, support: m.support }]));
          if (card.isConnected) drawModels();
        }).catch(() => {});
      }
    }).catch(() => {}),
    api("GET", hostPath("/presets", host)).then((list) => { presets = (Array.isArray(list) ? list : []).filter(p => p && typeof p.id === "string"); })
      .catch((e) => { presetsLoaded.error = e; }),
    // its categories (an older sandbox-ai has none: the page's own list)
    api("GET", hostPath("/preset-categories", host)).then((list) => { presetCategories = (Array.isArray(list) ? list : []).filter(c => c && typeof c.id === "string"); })
      .catch(() => {}),
    api("GET", hostPath("/vpn", host)).then((list) => { for (const p of (Array.isArray(list) ? list : []).filter(x => x && typeof x.profile === "string")) profiles.push(p); }).catch(() => {}),
    api("GET", hostPath("/mcp", host)).then((list) => { for (const x of (Array.isArray(list) ? list : []).filter(x => x && typeof x.name === "string")) mcpNames.add(x.name); }).catch(() => {}),
    // its table of cloud models (an older sandbox-ai has none: the agent's provider only, --model in the options)
    api("GET", hostPath("/models", host)).then((found) => { cloud = found && typeof found === "object" && found.agents && typeof found.agents === "object" ? found : null; }).catch(() => {}),
  ];
  // (not awaited with the rest: it asks every server of that host, a few seconds each)
  useCaseHint.textContent = t("Asking the model servers what they have (sandbox-ai llm recommend)…");
  api("GET", hostPath("/llm-recommend", host), undefined, { background: true }).then((found) => {
    recommended = (found && Array.isArray(found.use_cases) ? found.use_cases : []).filter(r => r && typeof r.use_case === "string");
    if (!card.isConnected) return;
    useCaseHint.textContent = "";
    drawUseCases(); chooseUseCase();
  }).catch(() => { if (card.isConnected) useCaseHint.textContent = t("This host's sandbox-ai has no use cases yet (sandbox-ai llm recommend): update it."); });
  const name = el("input", { id: "name", placeholder: t("empty: a codename like brisk-otter-lantern"), pattern: NEW_START_NAME.source.slice(1, -1) });
  const workspace = el("input", { id: "workspace", placeholder: "/home/you/src/project", class: "mono", required: true, "aria-describedby": "workspace-hint" });
  const options = el("textarea", { id: "options", rows: 3, class: "mono", placeholder: "--mode high\n--allow-pypi\n-mm sonnet", "aria-describedby": "options-hint" });
  // --ask: ask (the default), refuse without asking, or allow every question at once (marked, explained below it)
  const ask = el("select", { id: "start-ask", "aria-describedby": "start-ask-hint" }, ["on", "off", "allow"].map(value => el("option", { value }, askLabel(value))));
  const askHint = el("div", { class: "hint", id: "start-ask-hint" });
  ask.addEventListener("change", () => {
    askHint.textContent = ask.value === "allow" ? `⚠ ${t(ASK_ALLOW_HELP)}` : "";
    askHint.className = ask.value === "allow" ? "hint status-warn" : "hint";
  });
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
    if (textOf(name) && !NEW_START_NAME.test(textOf(name))) { startStatus.append(notice(t("Which name? 1-40 letters, digits, '_', '.' or '-', starting with a letter or digit."), "error")); return; }
    // the folder the agent works on is named (the field, or --workspace in the options): with neither, the
    // launcher would take the directory it runs in (this service's own, or the SSH login's HOME)
    if (!textOf(workspace) && !namesWorkspace(list)) {
      startStatus.append(notice(t("Which workspace? Name the folder on {host} the agent works on (its /work): nothing was started.", { host }), "error"));
      if (typeof workspace.focus === "function") workspace.focus();
      return;
    }
    const chosenCloud = model.disabled ? "" : cloudOf();
    const server = model.disabled || chosenCloud ? "" : model.value;  // "only this server" means nothing for the cloud
    if (useCase.value && !server && !fields.some(f => f.spec.kind === "llm" && f.value())) {
      startStatus.append(notice(t("A use case chooses among the models of a server of yours: choose a model server (Model), or no use case."), "error"));
      return;
    }
    // the chooser's --ask first: an --ask of the options comes later and wins, as on a command line
    list = withModel([...presetArgs, ...(["off", "allow"].includes(ask.value) ? ["--ask", ask.value] : []), ...list], server, onlyServer.checked && Boolean(server));
    if (server && agentOf() === "claude" && claudeTools.value) list = withOption(list, ["--llm-tools", claudeTools.value]);
    if (useCase.value) list = withOption(list, ["--use-case", useCase.value]);
    if (chosenCloud || (effort.value && !effort.disabled)) list = withCloudModel(list, chosenCloud, effort.disabled ? "" : effort.value);
    openTerminal({ kind: "start", host, agent: agentOf(), name: textOf(name), workspace: textOf(workspace), options: JSON.stringify(list) }, textOf(name) || t("new {agent}", { agent: agentOf() }));
  }) }, t("Start in a terminal"));
  card.append(el("div", { class: "form" },
    el("label", { for: "start-host" }, t("Host")), hostChoice,
    el("label", { for: "start-use-case" }, t("Use case")), el("div", {}, useCase, useCaseHint, useCaseKnown, useCasePairs),
    el("label", { for: "agent" }, t("Agent")), agent,
    el("label", { for: "start-model" }, t("Model")), el("div", { class: "row" }, model,
      modelFilter, el("label", {}, onlyServer, " ", t("only this server")), claudeToolsRow, el("button", { class: "small", onclick: () => { state.host = host; show("llm"); } }, t("Add a model server…")), modelHint),
    el("label", { for: "start-effort" }, t("Effort")), el("div", { class: "row" }, effort, effortHint),
    el("label", { for: "name" }, t("Name")), name,
    el("label", { for: "workspace" }, t("Workspace")), el("div", {}, workspace,
      el("div", { class: "hint", id: "workspace-hint" }, t("Required: the folder on that host the agent works on, as /work (an absolute path, or ~/…). The folder this service runs in is never taken instead."),
        " ", t("A folder of another machine: sshfs://USER@MACHINE/PATH - that host mounts it with sshfs (its SSH keys and ~/.ssh/config), the agent gets only the files."))),
    el("label", { for: "preset" }, t("Preset")), el("div", {}, preset, " ", presetHint),
    presetValues, presetMcp,
    el("label", { for: "start-ask" }, t("Questions")), el("div", {}, ask, askHint),
    el("label", { for: "options" }, t("Options")), el("div", {}, options,
      el("div", { class: "hint", id: "options-hint" }, t("Further options, as on a command line (one or more per line): {examples}; quotes keep spaces together.", { examples: "-mm sonnet, --mode high" }))),
    startStatus,
    el("span"), el("div", { class: "row" }, start,
      el("span", { class: "hint" }, t("Detach with Ctrl-b d: the start runs on.")))));
  await Promise.all(asked);
  if (!live() || !card.isConnected) return;
  drawModels(); drawPresets();
}

// The use cases' statuses (llm recommend --json: model_usecases.STATUS_TEXT) and the table's labels, in the page's
// language; a label the table has that is not here is shown as it comes.
const USE_CASE_STATUS = { measured: N_("measured on this use case's tasks"), indirect: N_("from other tasks than this use case's own"), "not tested": N_("not tested yet") };
const USE_CASE_LABELS = [N_("Coding and debugging"), N_("Sandbox behaviour"), N_("Untrusted input"), N_("Web research"), N_("Code review"),
  N_("Security and reverse engineering"), N_("Large codebase"), N_("Building from a spec"), N_("Ops: containers, CI, shell"),
  N_("Small fast jobs"), N_("Vision"), N_("Teams and helpers"), N_("Recovery: compaction and resume")];

// What FOUND (one use case of llm recommend --json) chose and why, in one text: the choice, its rank or where it comes
// from, why, the evidence and what fits; or that nothing was found (not tested yet, or nothing of its table there).
function useCaseText(found) {
  const c = found.choice;
  if (!c) return found.status === "not tested" ? t("Not tested yet, and no model of this host's servers to go by: choose the model yourself.") : t("Nothing of this use case's table is on this host's servers: choose the model yourself.");
  const where = c.rank ? t("rank {rank}", { rank: c.rank }) : c.related ? t("nearest: the choice for {use_case}", { use_case: c.related }) : t("nearest: by the traffic light, nothing measured for it");
  const parts = [t("Choice: {model} in {harness} ({where})", { model: `${c.server}:${c.model}`, harness: c.harness_name || c.harness, where }), String(c.why || "")];
  if (c.evidence) parts.push(t("Measured: {evidence}", { evidence: [c.evidence, c.date].filter(Boolean).join(", ") }));
  if (c.settings_text) parts.push(t("Settings over the model profile: {settings}", { settings: c.settings_text }));
  if (c.fit_text) parts.push(String(c.fit_text));
  if (c.note) parts.push(String(c.note));
  return parts.filter(Boolean).join(" · ");
}

// Whether the options LIST (before a "--") name a workspace: --workspace DIR, --workspace=DIR, -w DIR, -wDIR.
function namesWorkspace(list) {
  for (const word of list) {
    if (word === "--") return false;
    if (word === "--workspace" || word.startsWith("--workspace=") || word === "-w" || (word.startsWith("-w") && !word.startsWith("-wa"))) return true;
  }
  return false;
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
    if (/^(--model|--llm|-mm|-lm)=/.test(word) || word === "--no-provider-defaults" || word === "-nd") continue;
    out.push(word);
  }
  const at = out.indexOf("--");
  const added = ["--llm", choice, ...(only ? ["--no-provider-defaults"] : [])];
  if (at < 0) out.push(...added); else out.splice(at, 0, ...added);
  return out;
}

// The value of a cloud model in the start form's Model choice (an LLM server's name never starts with it).
const CLOUD_MARK = "=";

// AGENT's part of that host's table of cloud models (sandbox-ai models --json), or null.
function cloudAgent(cloud, agent) {
  const entry = cloud && cloud.agents ? cloud.agents[agent] : null;
  return entry && typeof entry === "object" && Array.isArray(entry.models) ? entry : null;
}

// AGENT's listed model MODEL, or null (not listed: it is passed on as it is).
function cloudModel(cloud, agent, model) {
  const entry = cloudAgent(cloud, agent);
  return entry && model ? entry.models.find(m => m && m.id === model) || null : null;
}

// The effort levels to offer for AGENT with MODEL: the model's own where the table knows them (a list), else the
// agent's (a model not listed, or one whose levels are not known per model): cloud_models.efforts, as the app has it.
function cloudEfforts(cloud, agent, model) {
  const entry = cloudAgent(cloud, agent);
  if (!entry) return [];
  const listed = cloudModel(cloud, agent, model);
  const levels = listed && Array.isArray(listed.efforts) ? listed.efforts : entry.efforts;
  return (Array.isArray(levels) ? levels : []).filter(level => typeof level === "string");
}

// [[provider, models]] of AGENT's cloud models, in the table's order.
function cloudGroups(cloud, agent) {
  const entry = cloudAgent(cloud, agent);
  const groups = new Map();
  for (const m of entry ? entry.models : []) {
    if (!m || typeof m.id !== "string" || typeof m.name !== "string") continue;
    const provider = String(m.provider || "?");
    if (!groups.has(provider)) groups.set(provider, []);
    groups.get(provider).push(m);
  }
  return [...groups.entries()];
}

// What a cloud model's option says on hover: its levels and context.
function cloudTitle(m) {
  const levels = m.efforts === null ? t("the agent's levels") : (m.efforts || []).length ? m.efforts.join(", ") : t("none");
  const parts = [t("Effort: {levels}", { levels }) + (m.default_effort ? ` (${t("default {level}", { level: m.default_effort })})` : "")];
  if (Number.isInteger(m.context) && m.context > 0) parts.push(t("Context: {tokens} tokens", { tokens: m.context >= 1e6 ? `${Math.floor(m.context / 1e6)}M` : `${Math.round(m.context / 1000)}k` }));
  return parts.join("\n");
}

// The options LIST with the cloud MODEL (--model) and EFFORT (--effort) chosen in the form: those of the options go
// (the form's choice wins), the new ones before "--" (the agent's own arguments stay as they are).
function withCloudModel(list, model, effort) {
  const drop = new Set([...(model ? ["--model", "-mm"] : []), ...(effort ? ["--effort", "-ef"] : [])]);
  const out = [];
  for (let i = 0; i < list.length; i++) {
    const word = list[i];
    if (word === "--") { out.push(...list.slice(i)); break; }
    if (drop.has(word)) { i++; continue; }
    if ([...drop].some(option => word.startsWith(`${option}=`))) continue;
    out.push(word);
  }
  return withOption(out, [...(model ? ["--model", model] : []), ...(effort ? ["--effort", effort] : [])]);
}

// The options LIST with ADDED before its "--" (the agent's own arguments stay last).
function withOption(list, added) {
  const at = list.indexOf("--");
  return at < 0 ? [...list, ...added] : [...list.slice(0, at), ...added, ...list.slice(at)];
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

// The storage check of a host (NAME: of one start): what could be freed, each item with how much, its exact command
// and whether it is safe while starts run; ticked items applied one by one, asked first (storage-apply).
const STORAGE_KINDS = { caches: N_("Empty the download caches (filled again when needed)"), build: N_("Remove the build output and temp folders (made again by a build)"), toolchains: N_("Remove the toolchains (installed again by their tools)"), transcripts: N_("Remove old transcripts for good (never the newest)"), pack: N_("Pack the HOME into one archive (unpacked again when resumed)"), unpack: N_("Unpack the HOME now"), compact: N_("Compact old transcripts in place (read as before)"), logs: N_("Remove the old log files of its project"), gc: N_("What Clean up… does (leftovers, logs compressed, expired starts)"), images: N_("Remove sandbox-ai's Podman images without a name"), log_folders: N_("Remove the log folders not written for half a year") };
function storageSize(value) {
  let size = Number(value) || 0;
  for (const [unit, shift] of [["TiB", 40], ["GiB", 30], ["MiB", 20], ["KiB", 10]]) {
    if (Math.abs(size) >= 2 ** shift) { const n = size / 2 ** shift; return `${Math.abs(n) < 100 ? n.toFixed(1) : n.toFixed(0)} ${unit}`; }
  }
  return `${size} B`;
}
function storageCard(main, host, name) {
  const old = document.getElementById("storage-card");
  if (old) dropCard(old);
  const result = el("div"), actions = el("div", { class: "row" });
  const path = name ? `/gc/check/${encodeURIComponent(name)}` : "/gc/check";
  const load = async () => {
    clear(result); clear(actions);
    result.append(el("p", { class: "hint" }, t("Measuring…")));
    let r;
    try { r = await api("GET", hostPath(path, host)); } catch (e) { clear(result).append(errorNotice(e)); return; }
    r = r && typeof r === "object" ? r : {};
    const plan = (Array.isArray(r.plan) ? r.plan : []).filter(item => item && Array.isArray(item.argv) && item.argv.every(part => typeof part === "string"));
    clear(result);
    if (name) {
      const start = ((r.detail || {}).starts || [])[0] || {};
      const kinds = Object.entries(start.kinds || {}).filter(([, v]) => Number(v) > 0).sort((a, b) => b[1] - a[1]);
      result.append(el("p", {}, t("Its HOME: {size} ({state})", { size: storageSize(start.bytes), state: String(start.state || "?") })),
        kinds.length ? el("p", { class: "hint" }, kinds.map(([k, v]) => `${k} ${storageSize(v)}`).join(", ")) : null,
        el("p", { class: "hint" }, t("The logs of its project: {size}; the workspace checkpoints of its project: {count}, {checkpoints}", { size: storageSize((r.logs || {}).bytes), count: Number((r.checkpoints || {}).count) || 0, checkpoints: storageSize((r.checkpoints || {}).bytes) })));
      for (const entry of r.starts || []) if (entry && typeof entry.growth === "number") result.append(el("p", { class: "hint" }, t("Since the last check: {growth}", { growth: (entry.growth >= 0 ? "+" : "-") + storageSize(Math.abs(entry.growth)) })));
    } else {
      result.append(el("p", {}, t("{size} could be freed (the notice comes above {threshold}).", { size: storageSize(r.total), threshold: storageSize(r.threshold) })));
      for (const entry of r.grew || []) if (entry) result.append(el("p", { class: "hint" }, t("{name} grew by {size} since the last check.", { name: String(entry.name), size: storageSize(entry.bytes) })));
    }
    if (!plan.length) { result.append(el("p", { class: "hint" }, t("Nothing could be freed."))); return; }
    const boxes = plan.map(item => el("input", { type: "checkbox", checked: item.checked === true }));
    const rows = plan.map((item, i) => el("tr", { title: `${String(item.what || "")}\n${String(item.command || "")}` },
      el("td", {}, el("label", {}, boxes[i], " ", STORAGE_KINDS[item.kind] ? t(STORAGE_KINDS[item.kind]) : String(item.what || ""))),
      el("td", { class: "num" }, (item.estimate ? t("about {size}", { size: storageSize(item.bytes) }) : storageSize(item.bytes))),
      el("td", {}, String(item.start || "-")),
      el("td", {}, item.safe_running ? t("safe") : t("only while it is stopped")),
      el("td", { class: "mono" }, String(item.command || ""))));
    result.append(el("div", { class: "table-scroll" }, el("table", {}, el("thead", {}, el("tr", {}, ...[t("What"), t("Frees"), t("Name"), t("While it runs"), t("Command")].map(h => el("th", {}, h)))), el("tbody", {}, ...rows))));
    const go = el("button", { class: "danger", onclick: () => {
      const chosen = plan.filter((item, i) => boxes[i].checked);
      if (!chosen.length) { toast("info", t("Tick what to do first."), ""); return; }
      confirmIn(actions, t("Run these {count} command(s) on {host}? What they remove is gone for good (caches and build output are made again when needed; compacted transcripts and packed HOMEs are read as before).", { count: chosen.length, host }), t("Yes, apply them"), async () => {
        let done = 0;
        for (const item of chosen) {
          try { await api("POST", hostPath("/gc/apply", host), { argv: item.argv, confirm: true }); done += 1; }
          catch (e) { failed(t("Not done: {command}", { command: String(item.command || "") }))(e); }
        }
        toast("success", t("Applied {count} item(s).", { count: done }), "");
        load();
      }, { trigger: go });
    } }, t("Apply selected…"));
    actions.append(go);
  };
  const card = el("div", { class: "card", id: "storage-card" }, el("h2", {}, name ? t("Storage of {name} on {host}", { name, host }) : t("Optimize on {host}", { host })),
    el("p", { class: "hint" }, t("What could be freed, each with how much, whether it is safe while starts run, and the exact command. Tick what to do and Apply selected: it is asked first, then each command runs on its own. Nothing is removed before.")),
    el("div", { class: "row" }, el("button", { onclick: (event) => busy(event.target, load) }, t("Check again")), el("button", { onclick: () => dropCard(card) }, t("Close"))),
    result, actions);
  openCard(main, card, null);
  load();
}

// A card about one start (its network, its resources while it runs; More, its history): switched off once the start
// is in a state the card does not fit (REF.alive; for network and resources: no longer running) or goes (a later
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
    if (!s || !entry.ref.alive(s.state)) entry.retire(s ? s.state : null);
  }
}

// What a card keeps of its start: its host, its id, and its name, which a rename changes (NAME is read anew each
// time; HEADING(name) gives the card's title).
// ALIVE(state): whether the card still fits a start in that state (by default: while it runs).
function cardRef(s, getName, setName, heading, title, alive) {
  return { host: s.host || state.host, id: startId(s), get name() { return getName(); },
    rename: (fresh) => { setName(fresh); heading.textContent = title(); }, alive: alive || ((now) => now === "running") };
}

const NET_PRESETS = ["pypi", "github", "gitlab", "kernel-org", "duckduckgo", "npm"];
// The names each preset opens (agent_sandbox.PRESETS, a test agrees): a preset the run has all of is shown ticked
const NET_PRESET_HOSTS = {"pypi": ["pypi.org", "files.pythonhosted.org"], "github": ["github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"], "gitlab": ["gitlab.com", "registry.gitlab.com"], "kernel-org": ["kernel.org", "www.kernel.org", "git.kernel.org", "cdn.kernel.org", "mirrors.kernel.org", "lore.kernel.org"], "duckduckgo": ["duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com", "links.duckduckgo.com", "api.duckduckgo.com"], "npm": ["registry.npmjs.org"]};

// What a card's form keeps of what was typed and not sent - the desktop app's rule (sandbox_ai_app/drafts.py): a
// card closed (Network… or Resources… of another start, Close, a refusal) and opened again for the same start fills
// it in again; in this page's memory only (never in the browser's storage), dropped once sent and taken. No field
// kept here holds a secret's value: a Git login and an API key name where the token is (a variable, a secret, a
// file), never the token.
const DRAFTS = new Map();
function cardDraft(key, inputs) {
  const isBox = (input) => input.getAttribute("type") === "checkbox";
  const read = (input) => (isBox(input) ? Boolean(input.checked) : String(input.value || ""));
  const initial = Object.fromEntries(Object.entries(inputs).map(([id, input]) => [id, read(input)]));
  const kept = DRAFTS.get(key);
  if (kept) for (const [id, input] of Object.entries(inputs)) {
    if (!(id in kept) || input.disabled) continue;
    if (isBox(input)) input.checked = kept[id]; else input.value = kept[id];
  }
  const save = () => {
    const now = Object.fromEntries(Object.entries(inputs).map(([id, input]) => [id, read(input)]));
    if (Object.keys(now).every(id => now[id] === initial[id])) DRAFTS.delete(key); else DRAFTS.set(key, now);
  };
  for (const input of Object.values(inputs)) { input.addEventListener("input", save); input.addEventListener("change", save); }
  return { restored: Boolean(kept), save, drop: () => DRAFTS.delete(key) };
}

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
  // a secret of that host as an API key its proxy puts in for one server (net allow --api-key HOST=NAME=SECRET)
  const keyHost = field("net-key-host", "api.example.com"), keyName = field("net-key-name", "EXAMPLE_KEY"), keySecret = field("net-key-secret", "EXAMPLE_KEY");
  const blockedList = el("div", { class: "net-blocked" });
  const presetBoxes = NET_PRESETS.map(p => el("input", { type: "checkbox", id: `net-preset-${p}`, value: p }));
  const keep = el("input", { id: "net-keep", type: "checkbox" });
  const shown = el("div", { class: "net-shown" });
  const notes = el("div");
  // what was typed and not sent for this start (cardDraft): filled in again
  const draft = cardDraft(`net ${host} ${startId(s)}`, { hosts, tcp, ports, dns, gitUrl, gitRef, gitKind, networks, nameservers, keyHost, keyName, keySecret, keep,
    ...Object.fromEntries(presetBoxes.map(b => [`preset-${b.value}`, b])) });
  // several in a field, separated by commas or spaces (as the launcher's list options take them), each once
  const words = (input) => [...new Set(String(input.value || "").split(/[\s,]+/).filter(Boolean))];
  const body = (op) => {
    const request = { op, allow_host: words(hosts), allow_tcp: words(tcp), host_ports: words(ports).map(Number), dns: dns.checked };
    // the further options only when given (the requests stay as small as what was asked)
    const more = { allow: words(networks), presets: presetBoxes.filter(b => b.checked && !b.disabled).map(b => b.value), nameservers: words(nameservers) };
    for (const [key, value] of Object.entries(more)) if (value.length) request[key] = value;
    if (gitUrl.value.trim() && gitRef.value.trim()) request.git_login = { url: gitUrl.value.trim(), kind: gitKind.value, ref: gitRef.value.trim() };
    const text = (input) => String(input.value || "").trim();
    if (op === "allow" && text(keyHost) && text(keySecret)) request.api_key = { host: text(keyHost).toLowerCase(), name: text(keyName) || text(keySecret), secret: text(keySecret) };
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
    // a preset the run has every name of: ticked and greyed, never sent again (opened again, the card shows it)
    const have = new Set([...list(d.host_wildcards).map(String), ...[...list(d.https), ...list(d.tcp)].flatMap(e => list(e && e.names).map(String))]);
    presetBoxes.forEach((box, i) => {
      const given = NET_PRESET_HOSTS[NET_PRESETS[i]].every(h => have.has(h));
      if (given) { box.checked = true; box.disabled = true; box.title = t("allowed already during this run"); }
      else if (box.disabled) { box.disabled = false; box.checked = false; box.title = ""; }
    });
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
    for (const g of list(d.git)) add(t("Git login"), String(g && g.url), [g && g.user ? t("user {user}", { user: g.user }) : "", g && g.in_sandbox ? t("token in the sandbox (the agent can read it)") : t("with its proxy")].filter(Boolean).join(", "), { git_login: { url: String(g && g.url), kind: "env", ref: "REVOKE" } });
    for (const n of list(d.all_ports)) add(t("All ports"), String(n), t("every protocol and port"), { allow: [String(n)] });  // net revoke --allow
    // an API key given during the run: taken back by its variable (net revoke --api-key NAME); its host stays allowed
    for (const k of list(d.api_keys)) add(t("API key"), String(k && k.host), t("as {name}", { name: String(k && k.name) }), k && k.name ? { api_key: { name: String(k.name) } } : null);
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
  // approvals blocked NAME --json there: what it was refused and why; a destination can be allowed for the run
  const drawBlocked = (data) => {
    const rows = (Array.isArray(data) ? data : []).slice(0, 50).map((b) => {
      const target = b && typeof b.target === "object" && b.target ? b.target : {};
      const request = target.host ? { allow_host: [String(target.host)] } : target.tcp ? { allow_tcp: [String(target.tcp)] } : null;
      const holder = el("span", { class: "inline-ask" });
      const allowed = (Array.isArray(b && b.actions) ? b.actions : []).includes("allow-run");
      if (request && allowed) {
        const button = el("button", { onclick: () => confirmIn(holder, t("Allow {what} for {name} on {host} now? It applies at once, until the sandbox ends (or you revoke it).", { what: String(b.what || ""), name, host }),
          t("Yes, allow it"), () => send("allow", Object.assign({ op: "allow", allow_host: [], allow_tcp: [], host_ports: [], dns: false }, request), true), { kind: "primary", trigger: button }) }, t("Allow…"));
        holder.append(button);
      }
      return el("tr", {}, el("td", { class: "mono" }, String((b && b.time) || "").slice(0, 19).replace("T", " ")), el("td", {}, String((b && b.kind) || "")),
        el("td", { class: "mono" }, String((b && b.what) || "")), el("td", { class: "hint" }, String((b && b.why) || "")), el("td", { class: "actions" }, holder));
    });
    clear(blockedList).append(table([t("Time"), t("Kind"), t("What"), t("Why"), ""], rows, t("Nothing was refused."), t("Refused during this run")));
  };
  const refresh = async () => {
    try { draw(await api("GET", hostPath(`/net/${encodeURIComponent(name)}`, host))); }
    catch (e) { clear(shown).append(errorNotice(e)); }
    try { drawBlocked(await api("GET", hostPath(`/blocked/${encodeURIComponent(name)}`, host))); }
    catch (e) { clear(blockedList).append(errorNotice(e)); }
  };
  const send = async (op, request, confirm) => {
    try {
      const r = await api("POST", hostPath(`/net/${encodeURIComponent(name)}`, host), Object.assign({}, request, confirm ? { confirm: true } : {}));
      draft.drop();  // taken: what was typed is done with
      toast("success", op === "allow" ? t("Allowed for {name} on {host}", { name, host }) : t("Revoked for {name} on {host}", { name, host }), (r && r.output) || "");
      await refresh();
    } catch (e) {
      // A wildcard below a shared service (*.blob.core.windows.net): asked by itself, with its risk and the shared
      // suffix named; only that yes sends {confirm_shared_wildcard: true} (then as --allow-shared-wildcard).
      if (op === "allow" && confirmable(e) === "shared-wildcard" && !request.confirm_shared_wildcard) {
        const p = e.params || {};
        confirmIn(actions, t("{wildcards} lies below {suffix}, a shared service where anyone can rent a name: the agent could open any server anyone rents below {suffix}, and send data there. Allow it for {name} on {host} anyway?",
          { wildcards: String(p.wildcards || ""), suffix: String(p.suffix || ""), name, host }),
          t("Yes, allow anyway"), () => send(op, Object.assign({}, request, { confirm_shared_wildcard: true }), true));
        return;
      }
      if (!confirm && confirmable(e)) { askRefused(actions, e, () => send(op, request, true)); return; }
      // A Git login the run's connection proxy cannot use (no proxy, a server it does not inspect, an older start):
      // refused by sandbox-ai, which names the only other way - into the sandbox, where the agent can read the
      // token. That is asked about by itself, saying so; only that yes sends {in_sandbox: true}.
      const said = [errorText(e), errorDetail(e)].filter(Boolean).join("\n");
      if (op === "allow" && request.git_login && !request.git_login.in_sandbox && said.includes("--in-sandbox")) {
        confirmIn(actions, t("{name} on {host} cannot be given the Git login for {url} through its connection proxy. The only other way puts the token into the sandbox's session directory for its Git: the agent can read it there. Give it so?", { name, host, url: request.git_login.url }) + "\n" + said,
          t("Yes, into the sandbox"), () => send(op, Object.assign({}, request, { git_login: Object.assign({}, request.git_login, { in_sandbox: true }) }), true));
        return;
      }
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
    r.git_login ? t("a Git login for {url}", { url: r.git_login.url }) : null,
    r.api_key ? t("the secret {secret} as the API key {name} for {server}", { secret: r.api_key.secret, name: r.api_key.name, server: r.api_key.host }) : null].filter(Boolean);
  const allow = el("button", { class: "primary", onclick: () => {
    const request = body("allow"), what = widened(request);
    if (!what.length) { toast("warning", t("Nothing to allow"), t("Name host names, addresses, ports, real DNS, a Git login or an API key first.")); return; }
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
    el("p", { class: "hint" }, t("Host names, IP:PORT, ports, networks and resolvers: one or more each, separated by commas or spaces.")),
    el("h3", {}, t("Allowed during this run")), shown,
    el("h3", {}, t("Refused during this run")), blockedList,
    el("div", { class: "form" },
      el("label", { for: "net-hosts" }, t("Host names (TCP/443)")), hosts,
      el("label", { for: "net-tcp" }, "IP:PORT (TCP)"), tcp,
      el("label", { for: "net-ports" }, t("Ports on the host")), ports,
      el("label", { for: "net-dns" }, t("Real DNS")), dns,
      el("label", { for: "net-git-url" }, t("Git login: server")), gitUrl,
      el("label", { for: "net-git-kind" }, t("Git login: token in")), el("div", {}, gitKind, " ", gitRef),
      el("label", { for: "net-key-host" }, t("API key: server")), keyHost,
      el("label", { for: "net-key-secret" }, t("API key: secret of {host}", { host })), keySecret,
      el("label", { for: "net-key-name" }, t("API key: variable (the agent sends its placeholder)")), keyName,
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
      el("td", {}, `${w.source || ""}${w.kept ? t(", kept for the session") : ""}`),
      // a folder of another machine (sshfs://): its source, and where this host mounted it
      el("td", { class: "mono" }, w.remote ? String(w.remote) : (w.host || ""), w.remote ? el("div", { class: "hint" }, t("mounted with sshfs at {path}", { path: String(w.host || "") })) : null), actions);
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
  const directory = el("input", { id: "ws-dir", class: "mono", size: 40, placeholder: t("{path} (on that host) or {remote}", { path: "/home/you/src/other", remote: "sshfs://you@machine/srv/project" }) });
  const wsName = el("input", { id: "ws-name", class: "mono", size: 16, placeholder: t("its own name"), pattern: WORKSPACE_AS.source.slice(1, -1) });
  const readOnly = el("input", { id: "ws-ro", type: "checkbox", checked: true });
  const wsActions = el("div", { class: "row" });
  const wsDraft = cardDraft(`workspace ${host} ${startId(s)}`, { directory, wsName, readOnly });  // kept until sent
  const addWorkspace = (path, as, ro) => async (event) => busy(event.target, async () => {
    const sent = Object.assign({ path, read_only: ro }, as ? { name: as } : {}, ro ? {} : { confirm: true });
    try {
      const r = await api("POST", hostPath(`/workspaces/${encodeURIComponent(name)}`, host), sent);
      doneToast(t("Added {what} to {where}", { what: path, where }), r);
      directory.value = ""; wsName.value = ""; wsDraft.drop(); loadWorkspaces();
    } catch (e) { failed(t("Could not add {what} to {where}", { what: path, where }))(e); }
    wsButtons();
  });
  const wsButtons = () => clear(wsActions).append(el("button", { class: "primary", onclick: (event) => {
    const path = directory.value.trim(), as = wsName.value.trim();
    const remote = REMOTE_SOURCE.test(path);
    if (!path.startsWith("/") && !remote) { toast("warning", t("Which file or folder?"), t("An absolute path on {host}, or a folder of another machine: sshfs://USER@MACHINE/PATH.", { host })); return; }
    if (as && !WORKSPACE_AS.test(as)) { toast("warning", t("Which name?"), t("1-64 letters, digits, '_', '.' or '-', starting with a letter or digit.")); return; }
    if (readOnly.checked) { addWorkspace(path, as, true)(event); return; }
    const at = `/workspaces/${as || path.split("/").filter(Boolean).pop() || ""}`;
    clear(wsActions).append(...ask(remote
      ? t("Give {name} write access to {path}, a folder of another machine that {host} mounts (sshfs, NFS or SMB), at {at}? The agent changes and deletes the files on that machine, until you remove it; their traffic does not pass the sandbox's proxy.", { name, path, host, at })
      : t("Give {name} write access to {path} on {host}, at {at}? The agent can change and delete what is in it, until you remove it (a folder is kept for the session's next start).", { name, path, host, at }),
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
  const helperOf = (server) => { const m = /^helper-(claude|codex|opencode|opencode2|hermes|dsh|qwen|goose|openhands|nanobot|letta|pi)-[0-9a-f]{8}$/.exec(server); return m ? m[1] : null; };
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
        if (r && (r.agent_restart === "required" || r.agent_restart === "maybe"))  // a restart needs a kept start (resume --restart)
          mcpResult.append(s.keep ? restartAgent() : el("p", { class: "hint" }, t("{name} keeps nothing to start again with: the agent sees it after a new start.", { name })));
        loadMcp();
      } catch (e) { failed(t("Could not add {server} to {where}", { server: shownServer(body), where }))(e); }
    }, { kind: "primary", trigger: addMcp });
  } }, t("Add now…"));
  mcpActions.append(addMcp);

  // Skills, subagents, commands and plugins while it runs (sandbox-ai kits|plugins attach|detach): copied into the
  // sandbox's read-only kit folders on that host (a plugin: from the plugin store), each asked first. What the agent
  // needs to see one is in the answer (reload): nothing, a command typed in it, or its restart inside the sandbox -
  // offered then, asked first too (kits restart-agent: the sandbox, network and MCP servers stay).
  const kitKinds = [["kit", t("A kit")], ["skill", t("A skill folder")], ["command", t("A command file")],
    ["agent_file", t("A subagent file")], ["plugin", t("A plugin (NAME@MARKETPLACE)")]];
  const kitKind = el("select", { id: "kits-live-kind" }, ...kitKinds.map(([value, label]) => el("option", { value }, label)));
  const kitValue = el("input", { id: "kits-live-value", placeholder: t("a kit's name, a path on that host, or NAME@MARKETPLACE"), autocomplete: "off", spellcheck: "false" });
  const kitKeep = el("input", { id: "kits-live-keep", type: "checkbox" });
  const kitFindings = el("input", { id: "kits-live-findings", type: "checkbox" });
  const kitActions = el("div", { class: "row" }), kitResult = el("div");
  const kitGone = el("input", { id: "kits-live-remove", placeholder: t("a skill, command, subagent or kit; or NAME@MARKETPLACE"), autocomplete: "off", spellcheck: "false" });
  const kitGoneActions = el("div", { class: "row" });
  const restartInside = () => {
    const holder = el("div", { class: "row" });
    const again = el("button", { class: "primary", onclick: () => confirmIn(holder, t("Start the agent of {name} on {host} again, inside its sandbox? What it does now ends; its conversation continues, the sandbox, its network and MCP servers stay.", { name, host }), t("Yes, start it again"), async () => {
      try {
        const r = await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/agent-restart`, host), { confirm: true });
        toast("success", t("The agent of {name} on {host} started again", { name, host }), (r && r.message) || "");
        holder.hidden = true;
      } catch (e) { failed(t("Could not start the agent of {name} on {host} again", { name, host }))(e); }
    }, { kind: "primary", trigger: again }) }, t("Start the agent again (the conversation goes on)"));
    holder.append(again);
    return holder;
  };
  const kitShow = (r) => {
    clear(kitResult).append(el("p", { class: "hint" }, String((r && r.message) || "")));
    const reloads = r && Array.isArray(r.reload) ? r.reload : [];
    if (reloads.some(x => x && x.reload === "restart")) kitResult.append(restartInside());
  };
  const addKit = el("button", { class: "primary", onclick: () => {
    const kind = kitKind.value, value = kitValue.value.trim();
    if (!value) { toast("warning", t("What to give?"), t("A kit's name, a path on {host}, or a plugin as NAME@MARKETPLACE.", { host })); return; }
    const body = { [kind]: value, keep: kitKeep.checked, allow_findings: kitFindings.checked };
    const text = kind === "plugin"
      ? t("Give the plugin {what} to {name} on {host} now, while it runs? It is installed into the plugin store if needed; its code runs as the agent, its hooks only once approved.", { what: value, name, host })
      : t("Give {what} to {name} on {host} now, while it runs? Its agent gets it read-only at once; a kit's hooks are not given.", { what: value, name, host });
    confirmIn(kitActions, text, t("Yes, give it now"), async () => {
      clear(kitResult);
      try {
        const r = await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/kits-live`, host), Object.assign({}, body, { confirm: true }));
        toast("success", t("{what} → {where}", { what: value, where }), (r && r.message) || "");
        kitShow(r);
      } catch (e) { failed(t("Could not give {what} to {where}", { what: value, where }))(e); }
    }, { kind: "primary", trigger: addKit });
  } }, t("Give now…"));
  kitActions.append(addKit);
  const removeKit = el("button", { class: "danger", onclick: () => {
    const value = kitGone.value.trim();
    if (!value) { toast("warning", t("What to take out?"), t("What was given: its name, or a plugin as NAME@MARKETPLACE.")); return; }
    const body = value.includes("@") && !value.startsWith("@") ? { plugin: value } : { name: value };
    confirmIn(kitGoneActions, t("Take {what} out of {name} on {host} now (and out of its later starts where it was kept)?", { what: value, name, host }), t("Yes, take it out"), async () => {
      try {
        const r = await api("POST", hostPath(`/starts/${encodeURIComponent(name)}/kits-live/remove`, host), Object.assign({}, body, { confirm: true }));
        toast("success", t("{what} taken out of {where}", { what: value, where }), (r && r.message) || "");
        kitShow(r);
      } catch (e) { failed(t("Could not take {what} out of {where}", { what: value, where }))(e); }
    }, { trigger: removeKit });
  } }, t("Take out…"));
  kitGoneActions.append(removeKit);

  // MCP servers of another host (sandbox-ai mcp copy, run by the service's machine, which reaches both hosts): the other
  // hosts' servers of their own, read in the background; Check is a dry run - what is copied (a catalog server added
  // from its catalog there, never copied raw), what changes, and what this host cannot take as it is: each folder of
  // the other host gets a folder of this one or is left out, a missing secret is named (set it on the Secrets page
  // of this host), an image is pulled here or sent. Copy and add now… is asked first and attaches them to this start.
  const copyChoice = el("select", { id: "mcp-copy-server" });
  const copyAs = el("input", { id: "mcp-copy-as", placeholder: t("the same name"), autocomplete: "off", spellcheck: "false" });
  const copyImage = el("select", { id: "mcp-copy-image" },
    el("option", { value: "" }, t("as needed (a catalog's is pulled here)")),
    el("option", { value: "pull" }, t("pull it here by its digest")),
    el("option", { value: "copy" }, t("send it from the other host")));
  const copyFlag = (id, text) => { const box = el("input", { type: "checkbox", id }); return { box, label: el("label", { for: id }, text) }; };
  const copyFlags = {
    secrets_later: copyFlag("mcp-copy-secrets-later", t("copy although a secret it uses is missing here")),
    no_vpn: copyFlag("mcp-copy-no-vpn", t("without its VPN profile")),
    current_pin: copyFlag("mcp-copy-pin", t("the image the catalog here pins")),
    unsigned: copyFlag("mcp-copy-unsigned", t("unsigned, as the other host took it")),
    replace: copyFlag("mcp-copy-replace", t("replace a server of that name here")),
  };
  const copyPaths = el("div"), copyPlan = el("div"), copyActions = el("div", { class: "row" });
  const pathFields = new Map();  // the other host's folder -> { input, drop }
  let copyChecked = null;  // the request whose check found nothing left to resolve
  const copySource = () => { try { return JSON.parse(copyChoice.value || "null"); } catch (e) { return null; } };
  const loadCopySources = async () => {
    clear(copyChoice).append(el("option", { value: "" }, t("Reading the servers of the other hosts…")));
    const others = (Array.isArray(state.hosts) ? state.hosts : []).map(h => h && h.name).filter(n => typeof n === "string" && n !== host);
    const found = await Promise.all(others.map(async (other) => {
      try {
        const servers = await api("GET", hostPath("/mcp", other), undefined, { background: true });
        return (Array.isArray(servers) ? servers : []).filter(d => d && typeof d === "object" && MCP_SERVER_NAME.test(String(d.name)) && d.own !== false && d.catalog !== "built-in")
          .map(d => ({ from: other, name: String(d.name), kind: String(d.kind || "") }));
      } catch (e) { return []; }
    }));
    const options = found.flat().map(o => el("option", { value: JSON.stringify({ from: o.from, name: o.name }) }, `${o.name} (${o.from})`));
    clear(copyChoice).append(...(options.length ? options : [el("option", { value: "" }, t("No other host has a server of its own."))]));
  };
  const copyBody = (dryRun) => {
    const chosen = copySource();
    if (!chosen) return null;
    const paths = {}, drop = [];
    for (const [old, field] of pathFields) {
      if (field.drop.checked) drop.push(old);
      else if (field.input.value.trim()) paths[old] = field.input.value.trim();
    }
    const body = { from: chosen.from, names: [chosen.name], paths, drop_paths: drop, dry_run: dryRun };
    if (copyAs.value.trim()) body.as = copyAs.value.trim();
    if (copyImage.value === "pull") body.pull_image = true;
    if (copyImage.value === "copy") body.copy_image = true;
    for (const [key, f] of Object.entries(copyFlags)) if (f.box.checked) body[key] = true;
    if (!dryRun) body.attach = name;
    return body;
  };
  const showCopyPlan = (result) => {
    const plans = result && Array.isArray(result.servers) ? result.servers : [];
    clear(copyPlan);
    clear(copyPaths);
    for (const plan of plans) {
      const lines = [];
      lines.push(el("p", {}, plan.how === "catalog" ? t("{server}: added from its catalog here (the same pinned image), never copied as it is.", { server: String(plan.server) })
        : t("{server}: its definition is checked here as an own server's is.", { server: String(plan.server) })));
      for (const text of [...(plan.copied || []), ...(plan.changes || []).map(c => t("changed: {what}", { what: c })), ...(plan.notes || [])])
        lines.push(el("p", { class: "hint", lang: "en" }, String(text)));
      for (const text of plan.warnings || []) lines.push(notice(String(text), "warn"));
      const secrets = plan.secrets && Array.isArray(plan.secrets.missing) ? plan.secrets.missing : [];
      if (secrets.length) lines.push(notice(t("{host} has no secret {names}: keep it on the page Secrets of {host} (its value is typed there, never copied), or copy it anyway.", { host, names: secrets.join(", ") }), "warn"));
      for (const problem of plan.problems || []) lines.push(notice(`${problem.text}${problem.fix ? ` — ${problem.fix}` : ""}`, "error"));
      copyPlan.append(...lines);
      for (const path of plan.paths || []) {
        const old = String(path.path || "");
        if (!old) continue;
        const kept = pathFields.get(old);
        const input = el("input", { class: "mono", placeholder: t("a folder of {host}", { host }), value: kept ? kept.input.value : (path.to || ""), "aria-label": t("Folder of {host} for {path}", { host, path: old }) });
        const drop = el("input", { type: "checkbox", "aria-label": t("leave out {path}", { path: old }) });
        drop.checked = kept ? kept.drop.checked : !!path.dropped;
        pathFields.set(old, { input, drop });
        const label = path.kind === "mount" ? t("{target} in the container (on {from}: {path})", { target: String(path.target || ""), from: String(result.from || ""), path: old }) : t("{path} (on {from})", { path: old, from: String(result.from || "") });
        copyPaths.append(el("div", { class: "form" }, el("label", {}, label), input, el("label", {}, drop, " ", t("leave out"))));
      }
    }
    if (copyPaths.children.length) copyPlan.append(el("p", { class: "hint" }, t("A folder of the other host is never mounted here unasked: give each one a folder of {host} (the same path keeps it), or leave it out; then check again.", { host })));
  };
  const checkCopy = el("button", { onclick: (event) => busy(event.target, async () => {
    const body = copyBody(true);
    if (!body) { toast("warning", t("Which MCP server?"), t("Choose a server of another host.")); return; }
    clear(copyPlan).append(el("p", { class: "hint" }, t("Checking…")));
    try {
      const result = await api("POST", hostPath("/mcp-copy", host), body);
      showCopyPlan(result);
      const blocked = (result.servers || []).some(p => (p.problems || []).length);
      copyChecked = blocked ? null : JSON.stringify(copyBody(true));
      copyNow.disabled = blocked;
    } catch (e) { clear(copyPlan).append(errorNotice(e)); copyChecked = null; copyNow.disabled = true; }
  }) }, t("Check"));
  const copyNow = el("button", { class: "primary", disabled: true, onclick: () => {
    if (copyChecked !== JSON.stringify(copyBody(true))) { copyNow.disabled = true; toast("warning", t("Check again"), t("The choices changed since the check.")); return; }
    const body = copyBody(false);
    confirmIn(copyActions, t("Copy {server} from {from} to {host} and give it to {name} now, while it runs? It is defined on {host} with the folders, variables and network shown above.", { server: body.names[0], from: body.from, host, name }) + (body.replace ? " " + t("The server of that name on {host} is replaced.", { host }) : ""), t("Yes, copy and add it"), async () => {
      try {
        const r = await api("POST", hostPath("/mcp-copy", host), Object.assign({}, body, { confirm: true }));
        const attached = (r && Array.isArray(r.attached) ? r.attached : [])[0] || {};
        if (r && (r.servers || []).some(p => (p.problems || []).length)) { showCopyPlan(r); toast("warning", t("Not copied"), t("Resolve what is said, then check again.")); return; }
        toast(attached.error ? "warning" : "success", t("{server} → {where}", { server: body.as || body.names[0], where }), String(attached.error || attached.message || ""));
        copyNow.disabled = true;
        copyChecked = null;
        loadMcp();
        loadCopySources();
      } catch (e) { failed(t("Could not copy {server} to {host}", { server: body.names[0], host }))(e); }
    }, { kind: "primary", trigger: copyNow });
  } }, t("Copy and add now…"));
  copyActions.append(checkCopy, copyNow);
  for (const control of [copyChoice, copyAs, copyImage, ...Object.values(copyFlags).map(f => f.box)])
    control.addEventListener("change", () => { copyNow.disabled = true; });
  copyChoice.addEventListener("change", () => { pathFields.clear(); clear(copyPaths); clear(copyPlan); });

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
    devHeld = list;
    showDetected();
  };
  const device = el("input", { id: "dev-value", class: "mono", placeholder: t("{path} or {usb}", { path: "/dev/ttyUSB0", usb: "usb:2e8a:000a" }) });
  // the devices that host has (sandbox-ai devices scan there, never this machine's for another host), grouped by
  // kind, minus those the run has and those sandbox-ai refuses; one chosen goes into the field. A GPU kind is shown,
  // not offered: --gpu cannot change while it runs.
  const detected = el("select", { id: "dev-detected" });
  const detectedNote = el("p", { class: "hint" });
  let devHeld = [], devScan = null, devScanError = null;
  const DEVICE_KINDS = [["usb", N_("USB devices")], ["serial", N_("Serial ports")], ["video", N_("Cameras and video")], ["render", N_("Render nodes")],
    ["kvm", N_("Virtual machines")], ["sound", N_("Sound")], ["gpu", N_("GPU (--gpu)")]];
  const showDetected = () => {
    clear(detected);
    if (devScanError) { detected.hidden = true; detectedNote.textContent = t("The devices of {host} could not be read: {error}. Type a path or usb:VENDOR:PRODUCT[:SERIAL].", { host, error: errorText(devScanError) }); return; }
    if (!devScan) { detected.hidden = true; detectedNote.textContent = ""; return; }
    const active = devHeld.filter(d => ["attached", "held", "gone"].includes(d.state));
    const values = new Set(active.map(d => d.value)), nodes = new Set(active.flatMap(d => Array.isArray(d.nodes) ? d.nodes : []));
    const has = (d) => values.has(d.value) || (Array.isArray(d.nodes) ? d.nodes : []).some(n => nodes.has(n));
    const offered = devScan.filter(d => !d.refused && !has(d));
    detected.append(el("option", { value: "" }, t("Choose a detected device…")));
    for (const [kind, title] of DEVICE_KINDS) {
      const group = offered.filter(d => d.kind === kind);
      if (!group.length) continue;
      detected.append(el("optgroup", { label: t(title) }, ...group.map(d => d.option === "--gpu"
        ? el("option", { value: "", disabled: true, title: t("--gpu cannot change while it runs: set it on the part and start again") }, `${d.label} (${t("restart needed")})`)
        : el("option", { value: d.value, title: d.note || d.value }, d.label))));
    }
    detected.hidden = !offered.length;
    const held = devScan.filter(d => !d.refused && has(d)).length, refused = devScan.filter(d => d.refused).length;
    detectedNote.textContent = offered.length ? [t("{count} detected on {host}", { count: offered.length, host }), held ? t("{count} the run has already", { count: held }) : "",
      refused ? t("{count} refused by sandbox-ai (input devices, keyboards ...), not offered", { count: refused }) : ""].filter(Boolean).join("; ")
      : t("No devices detected on {host}: type a path or usb:VENDOR:PRODUCT[:SERIAL].", { host });
  };
  const scanDevices = async () => {
    try {
      const r = await api("GET", hostPath("/device-scan", host));
      devScan = (r && Array.isArray(r.devices) ? r.devices : []).filter(d => d && typeof d === "object" && typeof d.value === "string" && typeof d.label === "string");
      devScanError = null;
    } catch (e) { devScan = null; devScanError = e; }
    showDetected();
  };
  detected.addEventListener("change", () => { if (detected.value) device.value = detected.value; });
  const devActions = el("div", { class: "row" });
  const devDraft = cardDraft(`device ${host} ${startId(s)}`, { device });  // kept until sent
  const addDevice = (value) => async (event) => busy(event.target, async () => {
    try {
      const r = await api("POST", hostPath(`/devices/${encodeURIComponent(name)}`, host), { device: value, confirm: true });
      toast("success", t("Gave {what} to {where}", { what: value, where }), (r && r.output) || "");
      device.value = ""; devDraft.drop(); loadDevices();
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
  limActions.append(apply, el("button", { onclick: () => { loadWorkspaces(); loadMcp(); loadDevices(); scanDevices(); loadLimits(); } }, t("Refresh")));

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
    el("h3", {}, t("Skills and plugins")),
    el("p", { class: "hint" }, t("Given to its agent now, read-only (sandbox-ai kits attach, plugins attach on that host); whether the agent sees it by itself, after a command or after it started again, the answer says.")),
    el("div", { class: "form" }, el("label", { for: "kits-live-kind" }, t("What")), kitKind,
      el("label", { for: "kits-live-value" }, t("Which")), kitValue,
      el("label", { for: "kits-live-keep" }, t("Also for its later starts")), kitKeep,
      el("label", { for: "kits-live-findings" }, t("Also with hidden text (invisible characters, instructions to the model)")), kitFindings,
      el("span"), kitActions,
      el("label", { for: "kits-live-remove" }, t("Take out")), kitGone, el("span"), kitGoneActions),
    kitResult,
    el("h4", {}, t("From another host")),
    el("p", { class: "hint" }, t("A server of another host copied to {host} (sandbox-ai mcp copy): checked first, then attached to {name} now.", { host, name })),
    el("div", { class: "form" }, el("label", { for: "mcp-copy-server" }, t("MCP server")), copyChoice,
      el("label", { for: "mcp-copy-as" }, t("Name here (optional)")), copyAs,
      el("label", { for: "mcp-copy-image" }, t("Its image")), copyImage,
      ...Object.values(copyFlags).flatMap(f => [f.label, f.box])),
    copyPaths, copyPlan, copyActions,
    el("h3", {}, t("Devices")), devStatus, devTable,
    el("div", { class: "form" }, el("label", { for: "dev-detected" }, t("Detected on {host}", { host })), detected, el("span"), detectedNote,
      el("label", { for: "dev-value" }, t("Device")), device, el("span"), devActions),
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
  loadCopySources();
  loadDevices();
  scanDevices();
  loadLimits();
}

// The questions of every host in one table, with tabs: Open (waiting for an answer, counting down), Allowed,
// Refused, Timed out (nobody answered in time: still open - an answer counts for the request's next try) and All,
// each with its count. The table is kept between the polls: its rows are keyed by the host and the question's id, a
// row whose state changed is drawn anew, every other row stays as it is (the scope chosen in it, the pointer over
// it). A click answers at once - no confirmation, not even for "always": a note offers to undo it for a few seconds.
// The answer goes to the question's own host; what failed is a short notice that closes by itself (and stays in the
// page's note line). Rows can be selected (their boxes) and answered together. An open row counts down the time its
// question has left (from when its host's list was read: the service says how old that is), and says it at 20 and 10
// seconds; a row has the focus with Tab, and a (allow once), r (always allow: this run), p (always, also for later
// sessions: the project) or d (deny) answer it there. One button per scope (user 2026-11-19, as in the app): Allow once,
// Always allow, Always, also for later sessions - a scope the question cannot get is a disabled button that says why.
// A browser question is opened here: "Open here and allow" opens its link in this browser, then allows it there
// without opening a browser on that host.
const SCOPES = { once: N_("this once"), run: N_("for the rest of this run"), always: N_("always (remembered for the project)") };
const SCOPE_ORDER = ["once", "run", "always"];
// The Allow buttons: [scope, label, what it does, its key in a focused row], and the note after each.
const ALLOW_BUTTONS = [["once", N_("Allow once"), N_("Allowed this time only: the next same request is asked again"), "a"],
  ["run", N_("Always allow"), N_("Every same request of this run (this session) is allowed without a question"), "r"],
  ["always", N_("Always, also for later sessions"), N_("Allowed now and remembered for the project: also at its later starts and resumed sessions, without a question (forget it under Remembered answers)"), "p"]];
const ALLOWED_NOTES = { once: N_("Allowed once"), run: N_("Always allowed in this run (this session)"), always: N_("Always allowed, also for later sessions") };

// Why question Q (still open, or answered: PAST) cannot be allowed with SCOPE, or null - as sandbox-ai caps it: a
// canary's alarm and a page to open once; its max_scope (the policy, the run's security mode, the kind) otherwise.
function scopeRefused(q, scope, past) {
  if (q.max_scope === null) return q.allow_refused ? t("cannot be allowed: {why}", { why: String(q.allow_refused) }) : t("cannot be allowed here");
  if (past && scope === "once") return t("Answered already: Always allow allows it for the rest of this run.");
  if (scope === "once") return null;
  if (q.kind === "canary") return t("A canary's alarm is answered this once: Allow once lets the sandbox go on, Deny stops it.");
  if (q.kind === "browser") return t("A page is opened once: nothing is kept for it.");
  // the package guard's question about a known malicious version (approvals list --json "package")
  if (q.package && q.package.malicious) return t("A known malicious package version is allowed for this one download at most: never for the run or for later sessions.");
  const max = SCOPE_ORDER.includes(q.max_scope) ? q.max_scope : (past ? "run" : "always");
  return SCOPE_ORDER.indexOf(scope) > SCOPE_ORDER.indexOf(max) ? t("not for this question: at most {scope}", { scope: t(SCOPES[max]) }) : null;
}
// The package guard's question (approvals list --json "package": ecosystem, name, version, age_days, malicious,
// new_name_days, allow): what it means, under the question's own line - as text, never markup.
function packageNote(q) {
  const p = q.package;
  if (!p || typeof p !== "object" || !p.name) return null;
  if (p.malicious) return el("div", { class: "hint status-bad" }, t("Package guard: this version is on the list of known malicious packages (advisory {advisory}). Deny it unless you know why you need it; an allow covers this one download only.", { advisory: String(p.malicious) }));
  return el("div", { class: "hint" }, t("Package guard: a version this young, or a package this new, may be malware nobody has reported yet, or a made-up name somebody registered.") +
    (typeof p.allow === "string" && p.allow ? " " + t("To allow the package in every run of the project: {command}", { command: p.allow }) : ""));
}
// The secret guard's question (approvals list --json "secret": host, found [{id, kind, label, where, encoding}],
// allow): what it means, under the question's own line - as text, never markup; no value is in it.
function secretNote(q) {
  const s = q.secret;
  if (!s || typeof s !== "object" || !Array.isArray(s.found) || !s.found.length) return null;
  return el("div", { class: "hint status-warn" }, t("Secret guard: this request carries a secret to a host it was not given for. The value is never shown, only what kind it is. Deny it unless you know that it belongs there (a test fixture, a token of that host).") +
    (typeof s.allow === "string" && s.allow ? " " + t("To allow that kind of value for that host in every run of the project: {command}", { command: s.allow }) : ""));
}
const APPROVAL_TABS = [["open", N_("state|Open")], ["allowed", N_("Allowed")], ["refused", N_("Refused")], ["timeout", N_("Timed out")], ["all", N_("All")]];
const APPROVAL_STATES = { open: N_("open"), timeout: N_("timed out"), allowed: N_("allowed"), refused: N_("refused") };
const UNDO_SECONDS = 8;
const approvalsView = { main: null, body: null, rows: new Map(), tab: "open", selected: new Set(), all: null };

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
  if (left <= 0) return [t("timed out - still answerable"), "warn"];
  const text = left >= 60 ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : t("{seconds} s", { seconds: left });
  return [text, left < 10 ? "bad" : left < 20 ? "warn" : ""];
}

// Once a second: each row's time left (from the host's seconds_left, counted down here).
function tickApprovals() {
  const view = approvalsView;
  if (!view.body || !view.body.isConnected) return;
  for (const entry of view.rows.values()) if (entry.tick) entry.tick();
}

// The state of question Q (an older sandbox-ai lists open ones without it).
function approvalState(q) { return APPROVAL_STATES[q.state] ? q.state : "open"; }

// Q still waits for its answer (open, or timed out and still answerable): answered as a question, not changed after.
function stillOpen(q) { return q.answerable !== false && ["open", "timeout"].includes(approvalState(q)); }

// How long an error notice stays: at least a second and a half (the user: "about a second"), longer for a long text.
function noticeSeconds(text) { return Math.max(1.5, Math.min(12, 1 + String(text || "").length / 18)); }

// What went wrong with an answer: a notice that closes by itself, and the same in the page's note line.
function approvalError(view, title, e) {
  const text = [errorText(e), errorDetail(e)].filter(Boolean).join("\n");
  toast("error", title, text, { seconds: noticeSeconds(`${title} ${text}`) });
  if (view.note) clear(view.note).append(el("span", { class: "status-bad" }, `${title}: ${text}`));
}

// Done: said in the note line with Undo for UNDO_SECONDS (several done together: one note, one Undo for them all).
function approvalDone(view, text, undo) {
  if (!view.note) return;
  clearTimeout(view.undoTimer);
  const button = undo ? el("button", { class: "small", onclick: (event) => busy(event.target, async () => {
    clearTimeout(view.undoTimer);
    clear(view.note).append(el("span", {}, t("Undoing…")));
    await undo();
  }) }, t("Undo")) : null;
  clear(view.note).append(...[el("span", { class: "status-ok" }, text), button ? " " : null, button].filter(Boolean));
  say(text);
  view.undoTimer = setTimeout(() => { if (button && button.isConnected) button.remove(); }, UNDO_SECONDS * 1000);
}

// Send ACTION (allow, deny, ask-again) for question Q with SCOPE; the command's output, or an Error.
async function approvalSend(q, action, scope, extra) {
  const path = hostPath(`/approvals/${encodeURIComponent(q.id)}${action === "ask-again" ? "/ask-again" : ""}`, String(q.host));
  const body = action === "ask-again" ? {} : Object.assign({ decision: action }, scope ? { scope } : {}, extra || {});
  return api("POST", path, body);
}

// What takes back ACTION on Q (as it was before): a question that was still open is asked again (the run forgets the
// answer; a connection allowed for the run is closed again), a changed answer is changed back.
function approvalUndo(q, action) {
  if (stillOpen(q)) return ["ask-again", null];
  const before = (q.result && q.result.decision) || (approvalState(q) === "allowed" ? "allow" : "deny");
  if (action === "ask-again") return [before, "run"];
  return [action === "allow" ? "deny" : "allow", "run"];
}

// Do ACTION (with SCOPE) on every question of ITEMS ([q]) at once; one note with Undo for what was done.
async function approvalAct(view, items, action, scope, extra, label) {
  const done = [], failures = [], closed = [];
  for (const q of items) {
    try {
      const result = await approvalSend(q, action, scope, extra);
      // allowed, but its name does not exist: the question is closed (denied for the run), said in one line
      if (result && typeof result.closed === "string" && result.closed) closed.push(result.closed); else done.push(q);
      answered(q);
    }
    catch (e) { failures.push([q, e]); }
  }
  for (const [q, e] of failures) approvalError(view, t("Not done: {question} ({sandbox} on {host})", { question: q.question || q.kind || q.id, sandbox: q.sandbox || q.run || t("a sandbox"), host: q.host }), e);
  if (done.length) {
    const what = done.length === 1 ? (done[0].question || done[0].kind || done[0].id) : t("{count} questions", { count: done.length });
    const always = scope === "always" && done.some(q => q.always) ? ` ${done.find(q => q.always).always}` : "";
    // (a page opened has no Undo: the run decides nothing about it)
    approvalDone(view, `${label}: ${what}${always}`, done.every(q => q.kind === "browser") ? null : async () => {
      const undone = [];
      for (const q of done) {
        const [back, backScope] = approvalUndo(q, action);
        try { await approvalSend(q, back, backScope); undone.push(q); } catch (e) { approvalError(view, t("Could not undo: {question}", { question: q.question || q.id }), e); }
      }
      if (undone.length) approvalDone(view, t("Undone: {count}", { count: undone.length }) + (scope === "always" ? ` ${t("(remembered always for the project: forget it under Remembered answers)")}` : ""), null);
      refreshApprovals(view);
    });
  }
  if (closed.length) {
    const text = closed.map(why => t("Closed, not allowed: {why}", { why })).join(" ");
    if (view.note) {
      if (!done.length) clear(view.note);
      view.note.append(done.length ? " " : "", el("span", { class: "approval-closed" }, text));
    }
    toast("info", t("Question closed"), text, { seconds: noticeSeconds(text) });
    say(text);
  }
  for (const q of items) if (!failures.some(([f]) => f === q)) view.selected.delete(questionKey(q));
  refreshApprovals(view);
}

// After an answer: the open questions (badge, toasts) and every state of this page read again.
function refreshApprovals(view) {
  view.all = null;
  poll();
}

// A row of a question that still waits for its answer (open, or timed out and still answerable).
function approvalRow(q, view) {
  const id = String(q.id), host = String(q.host), where = q.sandbox || q.run || t("a sandbox");
  // max_scope null: this question cannot be allowed here (allow_refused says why); missing (an older sandbox-ai): any
  const allowable = q.max_scope !== null;
  const max = SCOPE_ORDER.includes(q.max_scope) ? q.max_scope : "always";
  const widest = allowable ? SCOPE_ORDER.indexOf(max) : -1;
  const actions = el("td", { class: "actions" });
  const left = el("td", { class: "countdown" });
  const browser = q.kind === "browser";
  const url = browser ? safeUrl(q.url) : null;
  const link = url ? webLink(url) : null;
  const destination = browser && q.url_host ? el("div", { class: "hint" }, t("opens {host}", { host: String(q.url_host) })) : null;
  const refused = browser && !url ? el("div", { class: "hint status-bad" }, t("not opened anywhere: {why}", { why: String(q.url_refused || t("not an http(s) link without a user part")) })) : null;
  // a host whose sandbox-ai cannot allow it without opening a browser there (before 0.1.78): nothing is opened here
  const tooOld = browser && url && typeof q.host_too_old === "string" ? el("div", { class: "hint status-bad" },
    t("sandbox-ai {version} on {host} cannot allow this without opening a browser there: update it on the page Hosts (Update, or Update from this machine's copy).", { version: q.host_too_old, host })) : null;
  // A tool call's arguments in full, as the call will send them (approvals list --json "details"): open by itself
  // where the question's line shortens them, scrollable, as text (never markup).
  const details = typeof q.details === "string" && q.details ? el("details", { class: "call-arguments", open: q.details_whole === false },
    el("summary", {}, t("The call's arguments in full")),
    el("div", { class: "hint" }, t("As the call will send them (pretty-printed JSON; credentials redacted).")),
    el("pre", { class: "mono", tabindex: 0, "aria-label": t("The call's arguments in full") }, q.details)) : null;
  const question = el("td", {}, q.question, details, link ? el("div", {}, link) : null, destination, refused, tooOld,
    packageNote(q), secretNote(q),
    q.url_truncated ? el("div", { class: "hint" }, t("the URL was cut")) : null,
    q.always ? el("div", { class: "hint" }, q.always) : null,
    approvalState(q) === "timeout" ? el("div", { class: "hint" }, t("Nobody answered in time: the request failed, the question stays open. Allowed now, its next try goes through.")) : null,
    !allowable && q.allow_refused ? el("div", { class: "hint" }, t("cannot be allowed: {why}", { why: String(q.allow_refused) })) : null);
  const select = selectCell(q, view);
  const row = el("tr", { "data-id": id, "data-host": host, "data-state": approvalState(q), tabindex: 0, "aria-keyshortcuts": "a r p d", title: t("a: allow once, r: always allow, p: always, also for later sessions, d: deny") },
    select, el("td", { class: "mono" }, host), question, el("td", { class: "mono" }, where), left, actions);
  row.box = select.box;
  const entry = { row, q, deadline: null, said: null };
  // AGE: seconds since the host's list was read (its seconds_left were counted then)
  entry.update = (fresh, age) => {
    entry.q = fresh;
    const seconds = fresh.seconds_left;
    entry.deadline = approvalState(fresh) === "timeout" ? Date.now() : typeof seconds === "number" && Number.isFinite(seconds) ? Date.now() + (seconds - (Number(age) || 0)) * 1000 : null;
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
  };
  const answer = (decision, body, label) => (event) => busy(event.target, async () => {
    if (browser && decision === "allow" && typeof window.open === "function") window.open(url, "_blank", "noopener,noreferrer");  // in the click: no pop-up blocker
    const { scope: chosenScope, ...extra } = body;
    await approvalAct(view, [entry.q], decision, chosenScope, extra, label);
  });
  // Deny for this run and Deny once; where the question allows only once, the one Deny is once (all it allows).
  // Block always is a deny remembered for the project.
  const denyOnly = allowable && widest < 1;
  const denyWith = (scope) => (event) => answer("deny", { scope }, t("Denied {scope}", { scope: t(SCOPES[scope]) }))(event);
  const allowWith = (scope) => (event) => answer("allow", Object.assign({ scope }, browser ? { no_open: true } : {}), t(ALLOWED_NOTES[scope]))(event);
  // Allow once, Always allow, Always, also for later sessions: a browser question is opened here (never on that host)
  // and has no Allow at all where its link cannot be opened; a scope the question cannot get is off, saying why.
  const allowButtons = browser && (!url || tooOld) ? [] : ALLOW_BUTTONS.map(([scope, label, what, key]) => {
    const why = scopeRefused(q, scope, false);
    const text = browser && scope === "once" ? t("Open here and allow") : t(label);
    const title = why || (browser && scope === "once" ? t("opens the link in this browser, then allows it on {host} without opening a browser there", { host }) : `${t(what)} (${key})`);
    return el("button", { class: scope === "once" ? "primary" : "", disabled: Boolean(why), title, "data-scope": scope, onclick: allowWith(scope) }, text);
  });
  const deny = el("button", { class: "danger", onclick: denyWith(denyOnly ? "once" : "run") }, denyOnly ? t("Deny once") : t("Deny for this run"));
  const denyOnce = denyOnly ? null : el("button", { title: t("Refused this time only: the sandbox may ask again"), onclick: denyWith("once") }, t("Deny once"));
  clear(actions).append(...[...allowButtons, deny, denyOnce,
    widest >= 2 && ["connect", "dns"].includes(q.kind) ? el("button", { class: "danger", title: t("Refused now and at every later start of this project, without a question"), onclick: (event) => answer("deny", { scope: "always" }, t("Blocked always"))(event) }, t("Block always")) : null].filter(Boolean));
  row.addEventListener("keydown", (event) => {
    if (event.target !== row || event.ctrlKey || event.metaKey || event.altKey) return;
    const index = ["a", "r", "p"].indexOf(event.key);
    if (index >= 0 && allowButtons[index]) {
      event.preventDefault();
      const scope = ALLOW_BUTTONS[index][0];
      if (!scopeRefused(q, scope, false)) allowWith(scope)({ target: allowButtons[index] });
    } else if (event.key === "d") { event.preventDefault(); denyWith(denyOnly ? "once" : "run")({ target: deny }); }
  });
  entry.update(q, 0);
  return entry;
}

// The box that selects Q's row for the answers to several at once.
function selectCell(q, view) {
  const box = el("input", { type: "checkbox", "aria-label": t("Select {question}", { question: q.question || q.kind || q.id }) });
  box.checked = view.selected.has(questionKey(q));
  box.addEventListener("change", () => { if (box.checked) view.selected.add(questionKey(q)); else view.selected.delete(questionKey(q)); drawBulk(view); });
  const cell = el("td", { class: "select" }, box);
  cell.box = box;
  return cell;
}

// A row of a question answered already (allowed or refused; an older sandbox's refusal without an answer): what the
// run decided can be changed from here - Always allow (for the run), Always, also for later sessions (where that is
// kept; else off, saying why), Deny, Ask again.
function pastRow(q, view) {
  const host = String(q.host), where = q.sandbox || q.run || t("a sandbox");
  const result = q.result && typeof q.result === "object" ? q.result : {};
  const said = t(APPROVAL_STATES[approvalState(q)]) + (result.scope ? ` (${t(SCOPES[result.scope] || result.scope)})` : "") + (q.asked_again ? ` · ${t("asked again")}` : "");
  const actions = el("td", { class: "actions" });
  const offered = Array.isArray(q.actions) ? q.actions : [];
  const widest = SCOPE_ORDER.indexOf(q.max_scope);
  const run = (action, label, scope) => (event) => busy(event.target, async () => {
    await approvalAct(view, [q], action, action === "allow" ? scope : action === "deny" ? "run" : null, null, label);
  });
  if (offered.includes("allow") && widest >= 1) {
    actions.append(...ALLOW_BUTTONS.filter(([scope]) => scope !== "once").map(([scope, label, what]) => {
      const why = scopeRefused(q, scope, true);
      return el("button", { class: scope === "run" ? "primary" : "", disabled: Boolean(why), title: why || t(what), "data-scope": scope, onclick: run("allow", t(ALLOWED_NOTES[scope]), scope) }, t(label));
    }));
  }
  if (offered.includes("deny")) actions.append(el("button", { class: "danger", onclick: run("deny", t("Denied for this run")) }, t("Deny for this run")));
  if (offered.includes("ask-again")) actions.append(el("button", { title: t("Forget what the run decided: its next try is asked again"), onclick: run("ask-again", t("Asked again")) }, t("Ask again")));
  const select = selectCell(q, view);
  const row = el("tr", { "data-id": String(q.id), "data-host": host, "data-state": approvalState(q) },
    select, el("td", { class: "mono" }, host),
    el("td", {}, q.question, result.reason && result.reason !== "answered" ? el("div", { class: "hint" }, result.reason) : null),
    el("td", { class: "mono" }, where),
    el("td", { class: approvalState(q) === "allowed" ? "status-ok" : approvalState(q) === "refused" ? "status-bad" : "status-warn" }, said), actions);
  row.box = select.box;
  return { row, q, update: () => {}, tick: null };
}

// The answers to every row selected at once: Allow once (open questions), Always allow, Always, also for later
// sessions, Deny, Ask again - each for the selected rows it fits (a scope a row cannot get leaves that row out).
function drawBulk(view) {
  if (!view.bulk) return;
  const chosen = [...view.selected].map(key => view.rows.get(key)).filter(entry => entry && !entry.row.hidden).map(entry => entry.q);
  // (a page to open is opened here one by one, in its own row: never allowed from the selection)
  const fits = (action, scope) => chosen.filter(q => (stillOpen(q)
    ? (action === "allow" ? q.kind !== "browser" && !scopeRefused(q, scope, false) : action === "deny")
    : (q.actions || []).includes(action) && (action !== "allow" || (SCOPE_ORDER.indexOf(q.max_scope) >= 1 && !scopeRefused(q, scope, true)))));
  const button = (action, label, scope, kind, note) => {
    const items = fits(action, scope);
    return el("button", { class: kind || "", disabled: !items.length, onclick: (event) => busy(event.target, async () => {
      await approvalAct(view, fits(action, scope), action, scope, null, note || label);
    }) }, `${label} (${items.length})`);
  };
  clear(view.bulk).append(...(chosen.length ? [el("span", { class: "hint" }, t("{count} selected", { count: chosen.length })),
    ...ALLOW_BUTTONS.map(([scope, label], i) => button("allow", t(label), scope, i ? "" : "primary", t(ALLOWED_NOTES[scope]))),
    button("deny", t("Deny for this run"), "run", "danger"), button("ask-again", t("Ask again"), null),
    el("button", { onclick: () => { view.selected.clear(); for (const entry of view.rows.values()) if (entry.row.box) entry.row.box.checked = false; drawBulk(view); } }, t("Clear the selection"))] : []));
  view.bulk.hidden = !chosen.length;
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

// What a host remembers for its projects (approvals remembered, forget there): below the table.
function approvalsRecords() {
  const hostChoice = el("select", { id: "records-host" }, state.hosts.map(h => el("option", { value: h.name, selected: h.name === state.host }, h.name)));
  const output = el("div");
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
  return el("div", { class: "card" }, el("h2", {}, t("Remembered for the projects")),
    el("p", { class: "hint" }, t("What was allowed or blocked always: every later start of the project keeps to it.")),
    el("div", { class: "row" }, el("label", { for: "records-host" }, t("Host")), hostChoice,
      el("button", { onclick: showRemembered }, t("Remembered answers"))), output);
}

// The page's frame: the tabs with their counts, the bulk answers, the note line (what was done, with Undo; what
// failed), the table.
function approvalsFrame(view, main) {
  view.main = main; view.rows = new Map(); view.selected = new Set(); view.all = null;
  view.status = el("div"); view.body = el("tbody");
  view.note = el("p", { class: "approval-note", role: "status", "aria-live": "polite" });
  view.bulk = el("div", { class: "row bulk" });
  view.bulk.hidden = true;
  view.tabs = el("div", { class: "tabs", role: "tablist", "aria-label": t("Approvals") }, APPROVAL_TABS.map(([key, label]) => {
    const tab = el("button", { role: "tab", "data-tab": key, "aria-selected": String(view.tab === key), onclick: () => { view.tab = key; drawApprovalTabs(view); } }, t(label));
    tab.label = t(label);
    tab.key = key;
    return tab;
  }));
  const all = el("input", { type: "checkbox", "aria-label": t("Select every row shown") });
  all.addEventListener("change", () => {
    for (const [key, entry] of view.rows) {
      if (entry.row.hidden) continue;
      if (all.checked) view.selected.add(key); else view.selected.delete(key);
      if (entry.row.box) entry.row.box.checked = all.checked;
    }
    drawBulk(view);
  });
  view.table = el("table", {}, el("caption", { class: "sr-only" }, t("Questions of every host")),
    el("thead", {}, el("tr", {}, el("th", {}, all), ...[t("Host"), t("Question"), t("Sandbox"), t("State"), ""].map(c => el("th", {}, c)))), view.body);
  view.empty = el("p", { class: "hint" }, t("No open questions."));
  clear(main).append(el("h1", {}, t("Approvals")),
    el("p", { class: "hint" }, t("What the sandboxes of every host ask, and what was answered. A click answers at once (Undo takes it back for a few seconds). A question nobody answered in time stays open under Timed out: allowed later, the request's next try goes through. In a row: a allows, d denies.")),
    el("div", { class: "row" }, notificationsButton()),
    view.tabs, view.status, view.note, view.bulk, view.table, view.empty, approvalsRecords());
  view.table.hidden = true; view.empty.hidden = true;
}

// The tabs' counts and which rows the chosen tab shows.
function drawApprovalTabs(view) {
  const counts = { open: 0, allowed: 0, refused: 0, timeout: 0, all: 0 };
  for (const entry of view.rows.values()) { counts[approvalState(entry.q)] += 1; counts.all += 1; }
  for (const tab of view.tabs.children) {
    const key = tab.key;
    tab.textContent = `${tab.label} (${counts[key]})`;
    tab.setAttribute("aria-selected", String(view.tab === key));
    tab.className = view.tab === key ? "active" : "";
  }
  let shown = 0;
  for (const entry of view.rows.values()) {
    entry.row.hidden = view.tab !== "all" && approvalState(entry.q) !== view.tab;
    if (!entry.row.hidden) shown += 1;
  }
  view.table.hidden = !shown;
  view.empty.hidden = Boolean(shown);
  view.empty.textContent = view.tab === "open" ? t("No open questions.") : view.tab === "all" ? t("No questions.") : t("Nothing here.");
  drawBulk(view);
}

// KNOWN: what poll() fetched already ({questions, errors, age}, or the Error of a failed check); else fetched here.
// Every state is read too (GET /api/approvals/all), one read at a time.
pages.approvals = async (main, known) => {
  const view = approvalsView;
  if (known === undefined || view.main !== main || !view.body || !view.body.isConnected) {
    if (view.main !== main || !view.body || !view.body.isConnected) approvalsFrame(view, main);
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
  if (!view.reading) {
    view.reading = true;
    try {
      const all = await api("GET", "/api/approvals/all", undefined, { background: true });
      view.all = all && Array.isArray(all.questions) ? all : null;
    } catch (e) { view.all = null; } finally { view.reading = false; }
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
  // The open questions (the poll's, freshest), then every other state (answered here: never back with live buttons).
  const questions = openQuestions(found);
  const keys = new Set(questions.map(questionKey));
  for (const q of view.all ? view.all.questions : []) {
    if (!q || typeof q !== "object" || q.id === undefined || typeof q.host !== "string" || keys.has(questionKey(q))) continue;
    if (stillOpen(q) && state.answered.has(questionKey(q))) continue;
    questions.push(q);
    keys.add(questionKey(q));
  }
  for (const [key, entry] of view.rows) {
    // a host that could not be asked keeps its rows: no answer from it is no answer to them
    if (!keys.has(key) && !notAsked(errors, key.split(" ")[0])) { entry.row.remove(); view.rows.delete(key); view.selected.delete(key); }
  }
  for (const q of questions) {
    const key = questionKey(q);
    const existing = view.rows.get(key);
    const kind = stillOpen(q) ? "open" : `${approvalState(q)} ${JSON.stringify(q.result || {})} ${(q.actions || []).join(",")}`;
    if (existing && existing.kind === kind) { existing.update(q, ages[q.host]); continue; }
    const entry = stillOpen(q) ? approvalRow(q, view) : pastRow(q, view);
    entry.kind = kind;
    entry.update(q, ages[q.host]);
    if (existing) existing.row.replaceWith(entry.row); else view.body.append(entry.row);
    view.rows.set(key, entry);
  }
  drawApprovalTabs(view);
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

// The doctor's first check, sandbox-ai itself (id "sandbox-ai-version", with version, kind, path, prefix, python);
// where a sandbox-ai is installed, by the kind the doctor and the hosts list give (the app's doctor_view.WHERE).
const DOCTOR_VERSION_ID = "sandbox-ai-version";
const DOCTOR_WHERE = { system: N_("system installation in {prefix}"), user: N_("the user's own installation"), checkout: N_("a checkout"),
  pip: N_("a pip installation"), package: N_("a package ({package})") };
const VERSION_PATTERN = /[0-9]+\.[0-9]+\.[0-9]+(?:[.+-][0-9A-Za-z.+-]+)?/;

function doctorWhere(info) {
  if (!info || typeof info !== "object" || !Object.prototype.hasOwnProperty.call(DOCTOR_WHERE, info.kind)) return "";
  return t(DOCTOR_WHERE[info.kind], { prefix: String(info.prefix || "?"), package: String(info.package || "?") });
}

// The line above a doctor's checks: "sandbox-ai 0.1.80 on HOST — system installation in /usr/local (PATH), Python
// 3.12.3" from its first check; from an older doctor that only says "0.1.70 (PATH)", that; without such a check KNOWN
// (the hosts list's status of HOST) marked "(from the hosts list)"; "" when no version is known.
function doctorVersionLine(results, host, known) {
  const entry = (Array.isArray(results) ? results : []).find(r => r && typeof r === "object" && (r.id === DOCTOR_VERSION_ID || r.check === "sandbox-ai"));
  let version = null, where = "", source = "";
  if (entry) {
    const detail = String(entry.detail || "");
    const found = VERSION_PATTERN.exec(detail);
    version = entry.version ? String(entry.version) : found ? found[0] : null;
    if (entry.version && entry.kind) {
      where = doctorWhere(entry);
      if (entry.path) where = where ? `${where} (${entry.path})` : String(entry.path);
      if (entry.python) where += `, ${t("Python {version}", { version: String(entry.python) })}`;
    } else if (found && found.index === 0) where = detail.slice(found[0].length).replace(/^[\s,;]+/, "");
  }
  if (!version && known && typeof known === "object" && known.version) {
    version = String(known.version); where = doctorWhere(known); source = t("(from the hosts list)");
  }
  if (!version) return "";
  return t("sandbox-ai {version} on {host}", { version, host: host || state.host }) + (where ? ` — ${where}` : "") + (source ? ` ${source}` : "");
}

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
  const line = doctorVersionLine(results, opts.host, opts.known);
  const versionLine = line ? el("p", { class: "doctor-version" }, el("strong", {}, line)) : null;
  if (typeof results === "string") {
    return el("div", { class: "doctor" }, versionLine, notice(t("The sandbox-ai of {host} answered with text (an older version without the doctor's list): shown as it is. An update gives the readable list.", { host: opts.host || state.host }), "warn"),
      el("pre", { class: "doctor-text" }, results));
  }
  const checks = doctorChecks(results);
  const count = (status) => checks.filter(c => c.status === status).length;
  const problems = count("fail"), warnings = count("warn"), fine = count("ok"), skipped = count("skip");
  const view = el("div", { class: "doctor" }, versionLine);
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
    doctorView(results, { level: 2, host, known: (state.hostVersions || {})[host] }));
  if (live()) await updatesSection(main, host, live);
};

// The update mode of a host (sandbox-ai update mode): the mode, the last check and a newer version; a change is asked
// first (update-mode) and sent as a confirmed POST.
const UPDATE_MODES = [["off", N_("off - nothing is checked unless asked")], ["notify", N_("notify - a newer signed release is announced")],
  ["download", N_("download - also fetched and checked, installed when you say")], ["auto", N_("auto - also installed in the window while nothing runs")]];

async function updatesSection(main, host, live) {
  const box = el("div", { class: "card", id: "updates" });
  main.append(el("h2", {}, t("Updates on {host}", { host })), box);
  let data;
  try { data = await api("GET", hostPath("/update-mode", host)); } catch (e) { if (live()) box.append(errorNotice(e)); return; }
  if (!live() || !data || typeof data !== "object") return;
  const result = data.result && typeof data.result === "object" ? data.result : null;
  const when = (seconds) => typeof seconds === "number" ? new Date(seconds * 1000).toLocaleString() : t("never");
  const found = !result ? t("not checked yet") : result.error ? t("failed: {error}", { error: String(result.error) })
    : result.newer ? t("{available} is available (installed: {installed})", { available: String(result.available), installed: String(result.installed) })
    : t("up to date ({installed})", { installed: String(result.installed || data.installed || "") });
  const lines = [[t("Mode"), String(data.mode) + (data.chosen ? "" : " " + t("(the default)"))],
    [t("Last check"), `${when(data.last_check)}: ${found}`],
    [t("Next check"), data.mode === "off" ? t("none") : when(data.next_check)]];
  if (data.mode === "auto") lines.push([t("Update window"), String(data.window || "")]);
  if (data.downloaded) lines.push([t("Downloaded"), String(data.downloaded)]);
  if (data.waiting && data.waiting.text) lines.push([t("Waiting"), String(data.waiting.text)]);
  if (data.manual) lines.push([t("This copy"), String(data.manual)]);
  const mode = el("select", { id: "update-mode" }, UPDATE_MODES.map(([value, label]) => el("option", {
    value, selected: value === data.mode, disabled: Boolean((data.refused || {})[value]) }, t(label))));
  const windowField = el("input", { id: "update-window", value: String(data.window || "03:00-05:00"), size: 12, placeholder: "03:00-05:00" });
  const row = el("div", { class: "row" });
  const save = el("button", { onclick: (event) => {
    const body = { mode: mode.value, confirm: true };
    if (mode.value === "auto" && windowField.value.trim()) body.at = windowField.value.trim();
    confirmIn(row, t("Set the update mode of {host} to {mode}?", { host, mode: mode.value }), t("Yes, set it"), async () => {
      try { await api("POST", hostPath("/update-mode", host), body); doneToast(t("Update mode"), { output: mode.value }); show("doctor"); } catch (e) { failed(t("Update mode"))(e); }
    }, { trigger: event.target });
  } }, t("Set…"));
  row.append(el("label", { for: "update-mode" }, t("Update mode")), mode, el("label", { for: "update-window" }, t("Window (auto)")), windowField, save);
  box.append(el("dl", { class: "details" }, ...lines.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v)])), row);
}

// The logs of a host (sandbox-ai logs --json DIR | --project PATH): a log directory, a project's (its default log
// directory), or one of that host's starts (its workspace is its project). Never the directory the service (or the
// SSH login) runs in: with nothing chosen it shows the first start's, or asks for one. The form comes first; what
// the command says (no logs there yet, an error) is shown below it.
pages.logs = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Logs on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  let starts = [];
  try { starts = await api("GET", hostPath("/starts", host)); } catch (e) { starts = []; }
  if (!live()) return;
  const projects = [...new Set((Array.isArray(starts) ? starts : []).filter(x => x && typeof x.workspace === "string" && x.workspace.startsWith("/") && x.kind !== "mcp")
    .map(x => x.workspace))];
  const where = state.logsOf && state.logsOf.host === host ? state.logsOf : projects.length ? { host, directory: "", project: projects[0] } : null;
  const directory = el("input", { id: "logs-directory", class: "mono", placeholder: "/home/you/.local/state/sandbox-ai/logs/web", value: where ? where.directory : "" });
  const project = el("input", { id: "logs-project", class: "mono", placeholder: "/home/you/src/project", value: where ? where.project : "" });
  const ofStart = el("select", { id: "logs-start", class: "mono" }, el("option", { value: "" }, projects.length ? t("choose…") : t("no start on {host}", { host })),
    projects.map(path => el("option", { value: path, selected: Boolean(where && !where.directory && where.project === path) }, path)));
  ofStart.addEventListener("change", () => { if (ofStart.value) { state.logsOf = { host, directory: "", project: ofStart.value }; show("logs"); } });
  const box = el("div");
  clear(main).append(el("h1", {}, t("Logs on {host}", { host })),
    el("div", { class: "card" }, el("div", { class: "form" },
      el("label", { for: "logs-start" }, t("A start's project")), ofStart,
      el("label", { for: "logs-directory" }, t("Log directory")), directory, el("label", { for: "logs-project" }, t("Or a project")), project,
      el("span"), el("div", { class: "row" }, el("button", { class: "primary", onclick: () => { state.logsOf = directory.value.trim() || project.value.trim() ? { host, directory: directory.value.trim(), project: project.value.trim() } : null; show("logs"); } }, t("Show"))))),
    box);
  privacyCard(main, host);
  if (!where) { box.append(notice(t("Choose a start's project, a log directory or a project folder on {host}.", { host }), "info")); return; }
  const shown = where.directory || where.project;
  box.append(el("p", { class: "hint" }, t("Loading…")));
  let data;
  try { data = await api("POST", hostPath("/logs", host), { directory: where.directory, project: where.project }); }
  catch (e) {
    if (!live()) return;
    // the command's own words for a directory that is not there (logs.py): no start logged there yet
    clear(box).append(/no log directory/.test(String(e && e.message)) ? notice(t("No logs yet for {where} on {host}: a start writes them with a log directory (--log-dir, the wizard's logging question).", { where: shown, host }), "info") : errorNotice(e));
    return;
  }
  if (!live()) return;
  clear(box);
  const summaries = Array.isArray(data) ? data : [data];
  for (const s of summaries.filter(x => x && typeof x === "object")) {
    box.append(el("div", { class: "card" },
      el("div", { class: "mono hint" }, s.directory || ""),
      el("p", {}, t("{denied} refused connection(s), {events} file event(s)", { denied: s.denied ?? 0, events: s.file_events ?? 0 })),
      (s.targets || []).length ? table([t("Refused destination"), t("Count")], s.targets.slice(0, 20).map(([target, n]) => el("tr", {}, el("td", { class: "mono" }, target), el("td", {}, n)))) : null,
      (s.suggestions || []).length ? el("div", {}, el("h2", {}, t("To allow them")), table([t("Option"), t("Why"), t("Count")], s.suggestions.map(x => el("tr", {}, el("td", { class: "mono" }, x.option), el("td", {}, x.why), el("td", {}, x.count))))) : null,
      s.telemetry && s.telemetry.spend && s.telemetry.spend.total ? el("div", { class: "spend-summary" }, el("h2", {}, t("Model spend")),
        el("p", {}, t("In all: {cost} (sandbox-ai spend)", { cost: s.telemetry.spend.total.cost || "-" })),
        table([t("Model"), t("Cost"), t("Requests")], (s.telemetry.spend.models || []).map(m => el("tr", {}, el("td", { class: "mono" }, m.key || "?"), el("td", {}, m.cost || "-"), el("td", { class: "num" }, String(m.requests || 0)))))) : null,
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

// --- session bridges ----------------------------------------------------------------------------------------------
// The bridges of the service's machine and of its hosts (sandbox-ai bridge list --hosts there; each bridge once,
// HOST:ID for one of a host): Show, Resume (a suspended one) and Revoke act where the bridge is. New bridge: two
// running sessions of any host (HOST:NAME), what A may do to B, checked first by the launcher's dry run, whose
// warnings are confirmed before it is made. Every request goes to the service's own sandbox-ai ("local"), which
// reaches the hosts over SSH.
const BRIDGE_HOST = "local";
const BRIDGE_ACTIONS = [["alert", N_("Alert you")], ["hint", N_("Hint")], ["net-cut", N_("Cut B's network")], ["pause", N_("Pause B")]];
const BRIDGE_DO = { resume: "resume", revoke: "revoke" };  // the actions on a bridge (the route's last part)
const BRIDGE_ASK = [["first", N_("the first call of each tool")], ["each", N_("every call")], ["never", N_("never (writes are asked all the same)")]];
function bridgeId(b) { return b.host ? `${b.host}:${b.id}` : String(b.id); }
function bridgeHostOf(name) {
  const text = String(name || ""), at = text.indexOf(":");
  const host = at > 0 ? text.slice(0, at) : null;
  return host === "local" ? null : host;
}
// What the launcher does with A and B when both run on other hosts (null: one of them runs here).
function bridgePlacement(a, b) {
  const ha = bridgeHostOf(a), hb = bridgeHostOf(b);
  if (!ha || !hb) return null;
  return ha === hb ? t("Both sessions run on {host}: the bridge is made there (no hop through this machine) and listed here as {host}:ID.", { host: ha })
    : t("The sessions run on {a} and {b}: each host runs its half of the bridge, and this machine passes their messages on over two SSH connections (the hosts never connect to each other). When this machine sleeps or loses a host, it re-establishes the bridge with the same grant once both sessions still run (sandbox-ai 0.1.99 or later on both; else it ends); both need sandbox-ai 0.1.98 or later.", { a: ha, b: hb });
}
// The running agent sessions of every host as bridge add names them: this machine's NAME, a host's HOST:NAME.
function bridgeSessions(starts) {
  return (Array.isArray(starts) ? starts : []).filter(s => s && s.state === "running" && (s.kind === "session" || s.kind === "run") && !s.helper_of && typeof s.name === "string")
    .map(s => ({ value: s.host && s.host !== "local" ? `${s.host}:${s.name}` : s.name, agent: String(s.agent || "") }))
    .sort((x, y) => (bridgeHostOf(x.value) ? 1 : 0) - (bridgeHostOf(y.value) ? 1 : 0) || (x.value < y.value ? -1 : x.value > y.value ? 1 : 0));
}

pages.bridges = async (main) => {
  const live = pageLive();
  let rows = [], problem = null, starts = [];
  try { rows = await api("GET", hostPath("/bridges", BRIDGE_HOST)); } catch (e) { problem = e; }
  try { const all = await api("GET", "/api/starts"); starts = all && Array.isArray(all.starts) ? all.starts : []; } catch (e) { starts = []; }
  if (!live()) return;
  clear(main).append(el("h1", {}, t("Bridges")),
    el("p", { class: "hint" }, t("A bridge lets one running session reach another (messages, questions, files, observing) through a broker on their host, never a path between the sandboxes. A session of one of your hosts is HOST:NAME: two of one host are bridged there, two of two hosts through this machine. Only you make, widen or end one.")));
  if (problem) main.append(errorNotice(problem));
  const shown = el("div");
  const list = (Array.isArray(rows) ? rows : []).filter(b => b && typeof b.id === "string");
  const trs = list.map(b => {
    const ident = bridgeId(b), actions = el("td", { class: "actions" });
    const act = (action, title) => async () => {
      try { await api("POST", hostPath(`/bridges/${encodeURIComponent(ident)}/${action}`, BRIDGE_HOST), { confirm: true }); show("bridges"); } catch (e) { failed(title)(e); }
    };
    actions.append(rowButton(t("Show"), ident, BRIDGE_HOST, { onclick: (event) => busy(event.target, async () => {
      try { clear(shown).append(el("h3", {}, t("Bridge {id}", { id: ident })), dataView(await api("GET", hostPath(`/bridges/${encodeURIComponent(ident)}`, BRIDGE_HOST)))); } catch (e) { clear(shown).append(errorNotice(e)); } }) }));
    if (b.state === "suspended") {
      const resume = rowButton(t("Resume"), ident, BRIDGE_HOST, { onclick: () => confirmIn(actions, t("Resume bridge {id} between {a} and {b}? They reach each other again as it allows.", { id: ident, a: b.a, b: b.b }), t("Yes, resume it"),
        act(BRIDGE_DO.resume, t("Could not resume bridge {id}", { id: ident })), { kind: "primary", trigger: resume }) });
      actions.append(resume);
    }
    const revoke = rowButton(t("Revoke"), ident, BRIDGE_HOST, { class: "danger", onclick: () => confirmIn(actions, t("Revoke bridge {id} between {a} and {b}? Both lose it at once.", { id: ident, a: b.a, b: b.b }), t("Yes, revoke it"),
      act(BRIDGE_DO.revoke, t("Could not revoke bridge {id}", { id: ident })), { trigger: revoke }) });
    actions.append(revoke);
    const both = b.b_to_a && b.b_to_a !== "-";
    return el("tr", {}, el("td", { class: "mono" }, ident), el("td", {}, `${b.a} ${both ? "⇄" : "→"} ${b.b}`), el("td", { class: "mono" }, b.a_to_b || "-"),
      el("td", { class: "mono" }, b.b_to_a || "-"), el("td", {}, stateText(b.state) + (b.reconnecting === true ? " " + t("(reconnecting)") : "")), el("td", {}, b.until || "-"), actions);
  });
  main.append(table([t("Bridge"), t("Between"), t("A may"), t("B may"), t("State"), t("Until"), ""], trs, t("No bridge between running sessions.")), shown);
  // New bridge
  const sessions = bridgeSessions(starts);
  const options = () => sessions.map(s => el("option", { value: s.value }, s.agent ? `${s.value} (${s.agent})` : s.value));
  const a = el("select", { id: "bridge-a" }, options()), b = el("select", { id: "bridge-b" }, options());
  if (sessions.length > 1) b.selectedIndex = 1;
  const box = (id) => el("input", { id, type: "checkbox" });
  const message = box("bridge-message"), ask = box("bridge-ask"), watch = box("bridge-watch"), both = box("bridge-both");
  const reads = el("textarea", { id: "bridge-read", rows: 2, class: "mono", placeholder: t("one path per line, relative to its workspace (. for all of it)") });
  const writes = el("textarea", { id: "bridge-write", rows: 2, class: "mono", placeholder: t("one path per line, relative to its workspace (. for all of it)") });
  const until = el("input", { id: "bridge-until", placeholder: t("30m, 2h, 1d, HH:MM or an ISO time; empty: until either session ends") });
  const askMode = el("select", { id: "bridge-ask-mode" }, BRIDGE_ASK.map(([v, label]) => el("option", { value: v }, t(label))));
  const observer = Object.fromEntries(BRIDGE_ACTIONS.map(([key]) => [key, el("input", { type: "checkbox", id: `bridge-${key}`, checked: key === "alert" })]));
  const note = el("p", { class: "hint" }), result = el("div", { class: "wide" }), addRow = el("div", { class: "row" });
  const confirmBox = el("div", { class: "wide" });  // (the question below the plan: confirmIn replaces what it is put in)
  const update = () => {
    note.textContent = bridgePlacement(a.value, b.value) || "";
    for (const input of Object.values(observer)) input.disabled = !watch.checked;
  };
  for (const input of [a, b, watch]) input.addEventListener("change", update);
  update();
  const bodyOf = () => {
    const allow = [message.checked ? "message" : null, ask.checked ? "ask" : null, ...linesOf(writes.value).map(x => `files:rw:${x}`),
      ...linesOf(reads.value).map(x => `files:ro:${x}`), watch.checked ? "watch" : null].filter(Boolean);
    return { a: a.value, b: b.value, allow, both: both.checked === true, until: textOf(until), ask: askMode.value || "first",
      observer_actions: watch.checked ? BRIDGE_ACTIONS.map(([key]) => key).filter(key => observer[key].checked) : null };
  };
  addRow.append(el("button", { class: "primary", onclick: (event) => busy(event.target, async () => {
    const body = bodyOf();
    clear(confirmBox);
    if (!body.a || !body.b || body.a === body.b) { clear(result).append(el("p", { class: "notice error" }, t("Choose two different running sessions."))); return; }
    if (!body.allow.length) { clear(result).append(el("p", { class: "notice error" }, t("Choose at least one capability."))); return; }
    let plan;
    try { plan = await api("POST", hostPath("/bridges", BRIDGE_HOST), Object.assign({}, body, { dry_run: true })); } catch (e) { clear(result).append(errorNotice(e)); return; }
    const warnings = (plan && Array.isArray(plan.warnings) ? plan.warnings : []).map(w => el("li", {}, `⚠ ${w}`));
    const notes = (plan && Array.isArray(plan.notes) ? plan.notes : []).map(n => el("li", {}, n));
    clear(result).append(el("p", {}, t("{a} may reach {b}: {caps}", { a: body.a, b: body.b, caps: body.allow.join(",") })),
      ...(body.both ? [el("p", {}, t("{a} may reach {b}: {caps}", { a: body.b, b: body.a, caps: body.allow.join(",") }))] : []),
      warnings.length ? el("ul", {}, warnings) : el("span"), notes.length ? el("ul", { class: "hint" }, notes) : el("span"));
    confirmIn(confirmBox, t("Create this bridge?"), t("Yes, create it"), async () => {
      try {
        const made = await api("POST", hostPath("/bridges", BRIDGE_HOST), Object.assign({}, body, { confirm: true }));
        const ident = made && made.host ? `${made.host}:${made.id}` : (made && made.id) || "?";
        toast("success", t("Bridge {id} is active", { id: ident }), (made && Array.isArray(made.notes) ? made.notes : []).join("\n"));
        show("bridges");
      } catch (e) { failed(t("Not bridged"))(e); }
    }, { kind: "primary", trigger: event.target });
  }) }, t("Check and create…")));
  const actionsRow = el("div", { class: "row" }, BRIDGE_ACTIONS.map(([key, label]) => el("label", {}, observer[key], " ", t(label))));
  const form = el("div", { class: "form" },
    el("label", { for: "bridge-a" }, t("Session A (reaches B)")), a, el("label", { for: "bridge-b" }, t("Session B")), b,
    el("span"), note,
    el("span", {}, t("What A may do to B")), el("div", { class: "row" }, el("label", {}, message, " ", t("Send messages (message)")), el("label", {}, ask, " ", t("Ask questions and wait for the answer (ask)")), el("label", {}, watch, " ", t("Observe B (watch)"))),
    el("label", { for: "bridge-read" }, t("Read files (files:ro)")), reads, el("label", { for: "bridge-write" }, t("Write files (files:rw)")), writes,
    el("span", {}, t("The observer may")), actionsRow,
    el("span"), el("label", {}, both, " ", t("Both directions (B may do the same to A)")),
    el("label", { for: "bridge-until" }, t("Until")), until, el("label", { for: "bridge-ask-mode" }, t("Ask you about")), askMode,
    el("span"), el("p", { class: "hint" }, t("Every write is asked on the Approvals page, with its diff, whatever the ask mode says. .git is never reached.")),
    el("span"), addRow, result, confirmBox);
  main.append(el("h2", {}, t("New bridge")), sessions.length < 2 ? el("p", { class: "hint" }, t("A bridge joins two running sessions, here or on your hosts; {count} run now.", { count: sessions.length })) : el("span"), el("div", { class: "card" }, form));
};

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
      // Edit…: the profile's settings (vpn show: "settings" - names of secrets, hosts, countries; never a key), changed
      // in place with vpn set; an emptied field clears the setting
      rowButton(t("Edit…"), p.profile, host, { onclick: async (event) => busy(event.target, async () => {
        let facts;
        try { facts = await api("GET", hostPath(`/vpn/${encodeURIComponent(p.profile)}`, host)); } catch (e) { clear(shownProfile).append(errorNotice(e)); return; }
        const settings = facts && typeof facts.settings === "object" && facts.settings;
        if (!settings) { clear(shownProfile).append(el("p", { class: "hint" }, t("The sandbox-ai on {host} is older and cannot change a profile in place: update it, or add the profile again.", { host }))); return; }
        const keys = Object.keys(settings);
        rowEditor(shownProfile, t("Edit the VPN profile {profile}", { profile: p.profile }), keys.map(key => ({ key, label: `--${key}`, value: settings[key] === null ? "" : String(settings[key]), kind: key.endsWith("secret") ? "secret" : "" })),
          (v, row, result, trigger) => {
            const changed = {};
            for (const key of keys) { const before = settings[key] === null ? "" : String(settings[key]); if (v[key] !== before) changed[key] = v[key] || (key.startsWith("bootstrap-") ? "default" : "none"); }
            if (!Object.keys(changed).length) { clear(result).append(el("p", { class: "hint" }, t("Nothing changed."))); return; }
            confirmIn(row, t("Change the VPN profile {profile} on {host}? It is for the starts from now on: a sandbox that runs keeps the VPN it was started with.", { profile: p.profile, host }), t("Yes, change it"), async () => {
              try { const r = await api("POST", hostPath(`/vpn/${encodeURIComponent(p.profile)}/set`, host), { settings: changed, confirm: true }); toast("success", t("The VPN profile {profile} was changed on {host}", { profile: p.profile, host }), (r && r.output) || ""); show("vpn"); }
              catch (e) { clear(result).append(errorNotice(e, t("The profile was not changed"))); }
            }, { trigger });
          }, host);
      }) }),
      remove, notWhileRunning("vpn", p.profile, host));
    return el("tr", {}, el("td", { class: "mono" }, p.profile), el("td", {}, p.type || ""), el("td", {}, p.provider || ""), actions);
  });
  main.append(table([t("Profile"), t("Kind"), t("Provider"), ""], rows, t("No VPN profiles on this host.")), shownProfile);
  const name = el("input", { id: "vpn-name", placeholder: "home", pattern: "[a-z0-9][a-z0-9_-]{0,31}" });
  const kind = el("select", { id: "vpn-kind" }, [["wireguard", N_("WireGuard (a wg-quick file)")], ["openvpn", N_("OpenVPN (an .ovpn file)")],
    ["mullvad", N_("Mullvad (account number in a secret)")], ["tailscale", N_("Tailscale or Headscale (auth key in a secret)")], ["tor", N_("Tor (every connection through Tor)")]].map(([v, label]) => el("option", { value: v }, t(label))));
  const file = el("input", { id: "vpn-file", class: "mono", placeholder: t("{path} (on that host)", { path: "/home/you/vpn/home.conf" }) });
  const secret = offerNames(el("input", { id: "vpn-secret", class: "mono", placeholder: "VPN_HOME" }), "secret", host);
  const location = el("input", { id: "vpn-location", placeholder: "se-sto" });
  const exitNode = el("input", { id: "vpn-exit", placeholder: "exit-node-1" });
  const loginServer = el("input", { id: "vpn-login", placeholder: "https://headscale.example.org" });
  const hostname = el("input", { id: "vpn-hostname", class: "mono", placeholder: "sandbox-ai-home" }), tailnet = el("input", { id: "vpn-tailnet", class: "mono", placeholder: "100.64.0.0/10" });
  const bridges = el("input", { id: "vpn-bridges", class: "mono", placeholder: "/home/you/tor/bridges.txt" }), bridgeSecret = offerNames(el("input", { id: "vpn-bridge-secret", class: "mono", placeholder: "TOR_BRIDGES" }), "secret", host);
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
  if (typeof v.memory_gb === "number" && v.memory_gb > 0) body.memory_gb = v.memory_gb;
  return Object.assign(body, changes || {});
}

// The traffic light of the local-model research (llm models --json's and wizard models --json's "support": sandbox-ai's
// model_profiles.support, read from model_profiles.json on that host at run time): per model and agent green
// (verified), yellow (partly working: partial or smoke test only), red (failing or known not to work), grey (not
// tested); the model's colour is its best agent's. Colour, a symbol of its own and words: never the colour alone.
// The details: per agent what worked (✓) and what failed (✗), the recommended settings, the speed, the round and its
// date; "no data yet" when nothing is known. The app's words (model_profile.py).
const LIGHTS = ["green", "yellow", "red", "grey"];
const LIGHT_WORDS = { green: N_("verified"), yellow: N_("partly working"), red: N_("does not work"), grey: N_("not tested") };
const LIGHT_SYMBOLS = { green: "✓", yellow: "~", red: "✗", grey: "?" };
const LIGHT_CLASSES = { green: "light light-green", yellow: "light light-yellow", red: "light light-red", grey: "light light-grey" };
const STATUS_WORDS = { verified: N_("verified"), "smoke-only": N_("smoke test only"), partial: N_("partly working"), failing: N_("failing"),
  "known-bad": N_("known not to work"), untested: N_("this pair untested") };
const supportOf = (s) => (s && typeof s === "object" && LIGHTS.includes(s.light) ? s : null);
const textList = (v) => (Array.isArray(v) ? v.filter(x => typeof x === "string") : []);
// an agent's name as the host's sandbox-ai names it (support's "names"), else its id
const namesOf = (s) => (s.names && typeof s.names === "object" ? s.names : {});
function agentsOf(s, colour) { const names = namesOf(s); return textList(s.by_light && s.by_light[colour]).map(a => (typeof names[a] === "string" ? names[a] : a)); }
function namesIn(s, agents) { const names = namesOf(s); return textList(agents).map(a => (typeof names[a] === "string" ? names[a] : a)); }
function lightSummary(s) {
  const parts = ["green", "yellow", "red"].filter(c => agentsOf(s, c).length)
    .map(c => t("{words} in {agents}", { words: t(LIGHT_WORDS[c]), agents: agentsOf(s, c).join(", ") }));
  if ((s.status === "failing" || s.status === "known-bad") && textList(s.untested).length)
    parts.push((Array.isArray(s.harnesses) && s.harnesses.length ? t("{words} in every other agent", { words: t(LIGHT_WORDS.red) }) : t("{words} in every agent", { words: t(LIGHT_WORDS.red) })));
  return parts.join("; ") || t("not tested: no data yet");
}
function lightLabel(s) {
  const agents = agentsOf(s, s.light), words = t(LIGHT_WORDS[s.light]);
  if (!agents.length) return `${LIGHT_SYMBOLS[s.light]} ${words}${s.light !== "grey" ? `: ${t("every agent")}` : ""}`;
  return `${LIGHT_SYMBOLS[s.light]} ${words}: ${agents.slice(0, 3).join(", ")}${agents.length > 3 ? ` +${agents.length - 3}` : ""}`;
}
// a derived pair's evidence where its status is shown: "(49 runs, 13 use cases, rounds 4-6)" (none: hand-written)
function measuredText(counts) {
  if (!counts || typeof counts !== "object" || !wholeNumber(counts.runs)) return "";
  const parts = [counts.runs === 1 ? t("1 run") : t("{runs} runs", { runs: counts.runs })];
  if (wholeNumber(counts.use_cases) && counts.use_cases > 0)
    parts.push(counts.use_cases === 1 ? t("1 use case") : t("{use_cases} use cases", { use_cases: counts.use_cases }));
  if (typeof counts.rounds === "string" && counts.rounds) parts.push(counts.rounds);
  return ` (${parts.join(", ")})`;
}
function supportDetails(s) {
  const box = el("div", { class: "support-details" });
  if (!s.family) return box.append(el("p", {}, t("No data yet: no model family of sandbox-ai's research matches this name."))), box;
  const line = (text) => el("li", {}, text);
  box.append(el("p", {}, t("Model family: {label}", { label: String(s.label || s.family) })));
  if ((s.status === "failing" || s.status === "known-bad") && s.reason) box.append(el("p", { class: "status-bad" }, t("Known not to work: {reason}", { reason: String(s.reason) })));
  if (s.speed) box.append(el("p", {}, t("Measured speed: {speed}", { speed: String(s.speed) })));
  if (s.settings_text) box.append(el("p", {}, t("Recommended for every agent: {settings}", { settings: String(s.settings_text) })));
  for (const pair of Array.isArray(s.harnesses) ? s.harnesses.filter(x => x && typeof x === "object") : []) {
    const colour = LIGHTS.includes(pair.light) ? pair.light : "grey";
    const when = [textList(pair.rounds).join(", "), typeof pair.date === "string" ? pair.date : ""].filter(Boolean).join(", ");
    const items = [when ? line(t("Evidence of {when}", { when })) : null,
      pair.reason ? line(t("Why: {reason}", { reason: String(pair.reason) })) : null,
      pair.evidence ? line(t("Measured: {evidence}", { evidence: String(pair.evidence) })) : null,
      wholeNumber(pair.runs) && wholeNumber(pair.solved) ? line(t("{solved} of {runs} runs solved", { solved: pair.solved, runs: pair.runs })) : null,
      pair.settings_text ? line(t("Recommended settings: {settings}", { settings: String(pair.settings_text) })) : null,
      pair.speed ? line(t("Measured speed: {speed}", { speed: String(pair.speed) })) : null,
      ...textList(pair.works).map(x => el("li", { class: "status-ok" }, `✓ ${x}`)),
      ...textList(pair.fails).map(x => el("li", { class: "status-bad" }, `✗ ${x}`))].filter(Boolean);
    box.append(el("h4", {}, el("span", { class: LIGHT_CLASSES[colour], "aria-hidden": "true" }, LIGHT_SYMBOLS[colour]), " ",
      `${namesIn(s, [pair.agent])[0] || ""}: ${STATUS_WORDS[pair.status] ? t(STATUS_WORDS[pair.status]) : String(pair.status || "")}${measuredText(pair.measured_counts)}`), el("ul", {}, items));
  }
  if (textList(s.untested).length) box.append(el("p", { class: "hint" }, t("Not tested in: {agents}", { agents: namesIn(s, s.untested).join(", ") })));
  return box;
}
// a model's light in a table's cell: the symbol in its colour and the words; a click opens the details below it
function supportCell(s, model) {
  const summary = lightSummary(s);
  return el("details", { class: "support" },
    el("summary", { title: summary, "aria-label": t("Support of {model}: {summary}", { model, summary }) },
      el("span", { class: LIGHT_CLASSES[s.light], "aria-hidden": "true" }, LIGHT_SYMBOLS[s.light]), " ", lightLabel(s).slice(2)),
    supportDetails(s));
}
// an <option>'s words for a model's light (a select has no colours): " — ✓ verified: OpenCode, …", and its title
const lightSuffix = (s) => (s ? `  — ${lightLabel(s)}` : "");

// llm models --json: [{name, size, loaded, details, type?, arch?, parameters?, quantization?, context?}] as a table:
// only the columns some model has a value for; loaded first, then by name; a long name cut (the whole one in its
// title); a filter. ACTIONS (the LLM page): {use(model, button, cell), copy(ref, button), current: its model, host}.
const MODEL_NAME_WIDTH = 48;  // as llm models cuts it (llm.NAME_WIDTH)
let modelTables = 0;  // two tables can be in the page at once (a server's models, a test's): each filter its own id
function modelsTable(server, models, actions) {
  const filterId = `llm-models-filter${modelTables++ ? `-${modelTables}` : ""}`;
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
    ["support", N_("Support"), m => (supportOf(m.support) ? supportCell(m.support, m.name) : ""), m => Boolean(supportOf(m.support))],
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
  const filter = el("input", { type: "search", id: filterId, class: "mono", placeholder: t("Filter"), "aria-label": t("Filter the models of {server}", { server }), autocomplete: "off" });
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

// What an LLM server has loaded and uses now (llm status --json; wizard models --status's "status"), as its API
// reports it: the loaded models with the memory they take (VRAM, RAM) or their size, the server's VRAM/RAM, KV cache
// and requests; what it does not report is said. The same words as the app's (wizard_llm.status_text).
const LLM_STATUS_SECONDS = 20;  // between two readings while a page shows them
const NOT_REPORTED = { loaded: N_("the loaded models"), memory: N_("VRAM/RAM use"), kv_cache: N_("KV cache"), requests: N_("requests"), totals: N_("the host's total RAM/VRAM") };
const wholeNumber = (v) => typeof v === "number" && Number.isInteger(v) && v >= 0;
function gbText(bytes) {  // as sandbox-ai llm and the app show sizes: decimal units, one place
  for (const [unit, factor] of [["GB", 1e9], ["MB", 1e6], ["KB", 1e3]]) if (bytes >= factor) return `${(bytes / factor).toFixed(1)} ${unit}`;
  return `${bytes} B`;
}
function usageParts(u) {
  const usage = u && typeof u === "object" ? u : {}, parts = [];
  if (wholeNumber(usage.vram_bytes)) parts.push(wholeNumber(usage.vram_total_bytes) ? t("VRAM {size} of {total}", { size: gbText(usage.vram_bytes), total: gbText(usage.vram_total_bytes) }) : t("VRAM {size}", { size: gbText(usage.vram_bytes) }));
  if (wholeNumber(usage.ram_bytes)) parts.push(t("RAM {size}", { size: gbText(usage.ram_bytes) }));
  if (wholeNumber(usage.vram_free_bytes)) parts.push(t("{size} VRAM free", { size: gbText(usage.vram_free_bytes) }));
  if (typeof usage.kv_cache_percent === "number") parts.push(t("KV cache {percent} %", { percent: String(usage.kv_cache_percent) }));
  if (typeof usage.cpu_kv_cache_percent === "number") parts.push(t("CPU KV cache {percent} %", { percent: String(usage.cpu_kv_cache_percent) }));
  if (wholeNumber(usage.requests_running) || wholeNumber(usage.requests_waiting)) parts.push(t("requests: {running} running, {waiting} waiting", { running: wholeNumber(usage.requests_running) ? usage.requests_running : "?", waiting: wholeNumber(usage.requests_waiting) ? usage.requests_waiting : "?" }));
  if (wholeNumber(usage.slots_total)) parts.push(t("{count} slots", { count: usage.slots_total }));
  return parts;
}
function loadedText(m) {
  const extra = [];
  if (wholeNumber(m.memory_bytes)) {
    const split = [wholeNumber(m.vram_bytes) ? t("VRAM {size}", { size: gbText(m.vram_bytes) }) : null, wholeNumber(m.ram_bytes) ? t("RAM {size}", { size: gbText(m.ram_bytes) }) : null].filter(Boolean);
    extra.push(t("{size} in memory", { size: gbText(m.memory_bytes) }) + (split.length ? `: ${split.join(", ")}` : ""));
  } else if (wholeNumber(m.size)) extra.push(gbText(m.size));
  if (wholeNumber(m.context) && m.context > 0) extra.push(t("context {tokens}", { tokens: m.context }));
  return `${m.name}${extra.length ? ` (${extra.join("; ")})` : ""}`;
}
const loadedOf = (s) => (Array.isArray(s.loaded) ? s.loaded.filter(m => m && typeof m.name === "string") : null);
function llmStatusText(s) {
  if (!s || typeof s !== "object") return "";
  if (s.reachable === false) return t("not reachable: {error}", { error: String(s.error || "?") });
  const loaded = loadedOf(s), parts = [];
  if (loaded === null) parts.push(t("what is loaded is not reported"));
  else if (!loaded.length) parts.push(t("no model loaded"));
  else parts.push(t("loaded: {models}", { models: loaded.slice(0, 4).map(loadedText).join(", ") + (loaded.length > 4 ? " …" : "") }));
  parts.push(...usageParts(s.usage));
  const missing = (Array.isArray(s.not_reported) ? s.not_reported : []).filter(k => NOT_REPORTED[k] && k !== "loaded" && k !== "totals").map(k => t(NOT_REPORTED[k]));
  if (missing.length) parts.push(t("not reported: {what}", { what: missing.join(", ") }));
  return parts.join(" · ");
}
function llmStatusDetail(s) {
  if (!s || typeof s !== "object") return "";
  if (s.reachable === false) return t("not reachable: {error}", { error: String(s.error || "?") });
  const lines = (loadedOf(s) || []).map(m => t("Loaded: {model}", { model: loadedText(m) }) + (typeof m.expires_at === "string" && m.expires_at ? ` · ${t("unloaded at {time}", { time: m.expires_at })}` : ""));
  lines.push(...usageParts(s.usage));
  if (typeof s.read_at === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(s.read_at)) lines.push(t("read at {time} (UTC)", { time: s.read_at.slice(11, 19) }));
  const missing = (Array.isArray(s.not_reported) ? s.not_reported : []).filter(k => NOT_REPORTED[k]).map(k => t(NOT_REPORTED[k]));
  if (missing.length) lines.push(t("Not reported by its API: {what}", { what: missing.join(", ") }));
  if (s.kind === "llamacpp" && !(s.usage && wholeNumber(s.usage.requests_waiting))) lines.push(t("llama-server --metrics also reports the requests waiting."));
  return lines.join("\n");
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
  // Use as default: the server saved again with that model, the rest from llm show (llm-replace, asked first). Its
  // CA certificate is not in what is sent and stays as it is (llm add --replace keeps the stored one).
  const useModel = (server) => async (model, trigger, holder) => busy(trigger, async () => {
    let view;
    try { view = await api("GET", path(server)); } catch (e) { output.append(errorNotice(e)); return; }
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
    drawStatus();
  };
  // What each server has loaded and uses now (llm status --json on that host): read after the list and every
  // LLM_STATUS_SECONDS while the page is shown (a poll of the page's own: not the user's activity); a host whose
  // sandbox-ai does not know it yet says so once and is not asked again.
  const statusBox = el("div", { id: "llm-status", class: "llm-status" });
  let statuses = {}, statusTimer = null, statusAsked = false;
  const drawStatus = () => {
    const lines = servers.filter(e => statuses[e.name]).map(e => el("div", { class: "hint", "data-server": e.name, title: llmStatusDetail(statuses[e.name]) || null },
      el("span", { class: "mono" }, e.name), ": ", llmStatusText(statuses[e.name])));
    if (lines.length || Object.keys(statuses).length) clear(statusBox).append(...lines);
  };
  const readStatus = async () => {
    clearTimeout(statusTimer);
    if (!live() || !servers.length || statusAsked) return;
    statusAsked = true;
    let found;
    try { found = await api("GET", hostPath("/llm-status", host), undefined, { background: true }); }
    catch (e) {
      statusAsked = false;
      if (!live()) return;
      clear(statusBox).append(errorNotice(e, t("What the servers have loaded now could not be read")));
      if (e && e.code !== "host-too-old") statusTimer = setTimeout(readStatus, LLM_STATUS_SECONDS * 1000);
      return;
    }
    statusAsked = false;
    if (!live()) return;
    statuses = {};
    for (const s of Array.isArray(found) ? found : []) if (s && typeof s.name === "string") statuses[s.name] = s;
    drawStatus();
    statusTimer = setTimeout(readStatus, LLM_STATUS_SECONDS * 1000);
  };
  drawList(list);
  main.append(listBox, statusBox, output);
  readStatus();

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
  const keySecret = offerNames(el("input", { id: "llm-key-secret", class: "mono", placeholder: t("optional, e.g. {name}", { name: "LMSTUDIO_KEY" }), pattern: "[A-Za-z_][A-Za-z0-9_]{0,63}", autocomplete: "off", "aria-describedby": "llm-hint" }), "secret", host);
  // the key itself, typed here (review 5 L4): stored as that secret of the host first (secrets set, on its standard
  // input), only on Add/Save, as the Logins page stores a token; never sent anywhere else
  const [keyValue, keyField] = secretInput({ id: "llm-key-value", class: "mono", placeholder: t("optional: stored as that secret on Save"), value: "" });
  const status = el("div", { class: "wide" });
  const testBox = el("div", { class: "wide", id: "llm-test-result" });
  const headerSecrets = el("input", { id: "llm-headers", class: "mono", placeholder: "X-Gateway-Key=GW_KEY" }), ca = el("input", { id: "llm-ca", class: "mono", placeholder: "/home/you/ca.pem" });
  const caNote = el("div", { class: "wide hint", id: "llm-ca-note" });
  const vpn = offerNames(el("input", { id: "llm-vpn", class: "mono", placeholder: "home" }), "vpn", host);
  const anthropic = el("input", { id: "llm-anthropic", type: "checkbox" }), replace = el("input", { id: "llm-replace", type: "checkbox" }), insecure = el("input", { id: "llm-insecure", type: "checkbox" });
  // the server's memory for what fits it (llm recommend, a start's --use-case); empty: not said (its own host's for
  // a server on that host's loopback)
  const memory = el("input", { id: "llm-memory", type: "number", min: 1, max: 65536, step: "any", placeholder: t("optional, e.g. {name}", { name: "128" }) });
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
    const gb = String(memory.value || "").trim();
    if (gb && !(Number(gb) >= 1 && Number(gb) <= 65536)) { status.append(notice(t("The server's memory: 1 to 65536 GB, or empty."), "error")); return null; }
    if (gb) body.memory_gb = Number(gb);
    else if (editing && editing.memory) body.memory_gb = 0;  // emptied: forgotten
    return body;
  };
  // what is asked first: plain http outside the local networks, a server of that name replaced
  const asksOf = (body) => [body.insecure_http ? t("Send the prompts to {url} over plain http, unencrypted, from {host}? Whoever is on the way can read them, the answers and the key.", { url: body.url, host }) : null,
    body.replace ? t("Replace the LLM server {name} on {host}? Every start that uses it sends its prompts (and its key) to {url} from then on.", { name: body.name, host, url: body.url }) : null].filter(Boolean);
  const setEditing = (view) => {
    editing = view ? { name: view.name, ca: view.ca || null, memory: view.memory_gb || null } : null;
    heading.textContent = editing ? t("Change the server {name}", { name: editing.name }) : t("Add a server");
    save.textContent = editing ? t("Save") : t("Add");
    cancelEdit.hidden = !editing;
    clear(caNote);
    if (editing && editing.ca) caNote.append(t("It has a CA certificate of its own ({file}), which is kept; a file given here replaces it.", { file: editing.ca }));
  };
  const editServer = async (server) => {
    let view;
    try { view = await api("GET", path(server)); } catch (e) { clear(output).append(errorNotice(e)); return; }
    if (!view || typeof view !== "object" || typeof view.name !== "string") { clear(output).append(notice(t("{name}: llm show said nothing usable.", { name: server }), "error")); return; }
    const body = llmBodyOf(view);
    name.value = view.name; kind.value = view.kind || "openai-compatible"; url.value = view.url || ""; model.value = view.model || "";
    keySecret.value = view.api_key_secret || ""; headerSecrets.value = (body.header_secrets || []).map(h => `${h.header}=${h.secret}`).join(" ");
    ca.value = ""; vpn.value = view.vpn || ""; anthropic.checked = Boolean(view.anthropic); insecure.checked = Boolean(view.insecure_http); replace.checked = true;
    memory.value = typeof view.memory_gb === "number" ? String(view.memory_gb) : "";
    fillModels(Array.isArray(view.models) ? view.models.map(m => ({ name: m })) : []);
    clear(status); clear(testBox); checkName();
    setEditing(view);
    if (typeof url.focus === "function") url.focus();
  };
  // the Default-model select, from what a test (or llm show's last check) listed
  const fillModels = (models) => {
    const listed = (Array.isArray(models) ? models : []).filter(m => m && typeof m.name === "string")
      .sort((a, b) => ({ true: 0, false: 1 }[String(a.loaded)] ?? 2) - ({ true: 0, false: 1 }[String(b.loaded)] ?? 2) || (a.name < b.name ? -1 : 1));
    const names = listed.map(m => m.name);
    clear(modelPick).append(el("option", { value: "" }, t("a listed model…")), ...listed.map(m => {
      const light = supportOf(m.support);  // the research's traffic light, in words
      return el("option", { value: m.name, selected: m.name === model.value.trim(), title: light ? lightSummary(light) : null }, `${m.name}${lightSuffix(light)}`);
    }));
    modelPick.value = names.includes(model.value.trim()) ? model.value.trim() : "";
    modelPick.hidden = !names.length; modelPickLabel.hidden = !names.length;
  };
  // a key typed: stored as the secret named first (a secret of that name there is asked about: the service's
  // secret-replace), then the server added; when the add is refused, a secret made for it is removed again
  const storeKey = async (body, replace) => {
    const value = String(keyValue.value || "");
    if (!value) return { stored: false, made: false };
    try { await api("POST", hostPath(`/secrets/${encodeURIComponent(body.api_key_secret)}`, host), Object.assign({ secret: value }, replace ? { confirm: true } : {})); }
    catch (e) {
      if (!replace && confirmable(e)) return { ask: e };
      throw e;
    }
    keyValue.value = "";
    return { stored: true, made: !replace };
  };
  const save = el("button", { class: "primary", onclick: (ev) => {
    const body = formBody();
    if (!body) return;
    if (keyValue.value && !body.api_key_secret) { status.append(notice(t("Name the secret the key is stored as (API key: secret)."), "error")); return; }
    const add = (confirm, replaceKey) => busy(ev.target, async () => {
      let key;
      try { key = await storeKey(body, replaceKey); } catch (e) { status.append(errorNotice(e, t("The key was not stored; nothing was added"))); return; }
      if (key.ask) { askRefused(addRow, key.ask, () => add(confirm, true), { trigger: ev.target }); return; }
      try { await api("POST", hostPath("/llm", host), Object.assign({}, body, confirm ? { confirm: true } : {})); show("llm"); }
      catch (e) {
        status.append(errorNotice(e), ...plainHttpHint(e));
        if (key.made) {
          try { await api("DELETE", hostPath(`/secrets/${encodeURIComponent(body.api_key_secret)}`, host), { confirm: true }); status.append(el("p", { class: "hint" }, t("The secret {name} stored for it was removed again.", { name: body.api_key_secret }))); }
          catch (err) { status.append(el("p", { class: "hint" }, t("The secret {name} stored for it stays: remove it on the page Secrets if it is not needed.", { name: body.api_key_secret }))); }
        }
      }
    });
    const send = (confirm) => add(confirm, false);
    const asks = asksOf(body);
    if (!asks.length) { send(false); return; }
    confirmIn(addRow, asks.join(" "), body.replace ? t("Yes, replace it") : t("Yes, add it"), () => send(true), { kind: "primary", trigger: ev.target });
  } }, t("Add"));
  // Test connection: the models the server at that URL lists now (sandbox-ai wizard models --kind --url --json on
  // that host), before anything is stored: nothing is saved, no name is needed, and the form is left as it is. Its
  // key is named (the command reads that secret there) and its CA goes as a file of that host; a header secret and
  // a VPN profile do not, so a server that needs one of those can refuse the test and still work for the starts.
  // With "Plain http outside the local networks" ticked and a key named, the test sends that key unencrypted too:
  // asked first, as Add asks (the command refuses plain http to a public address otherwise, as llm add does).
  // the command's refusal of plain http names its option; here it is the form's checkbox
  const plainHttpHint = (e) => (String((e && e.message) || "").includes("--insecure-http") && !insecure.checked
    ? [el("p", { class: "hint" }, t("To allow it, tick “Plain http outside the local networks” above."))] : []);
  const test = el("button", { onclick: (ev) => {
    const risky = insecure.checked && keySecret.value.trim() && url.value.trim().toLowerCase().startsWith("http://");
    if (!risky) { runTest(ev, false); return; }
    // the test sends the key only (no prompt); Add asks again for the server's prompts
    confirmIn(addRow, t("Test {url} over plain http, unencrypted, from {host}? The test sends the key {secret} unencrypted: whoever is on the way can read it. Nothing is saved; Add asks again for the prompts.", { url: url.value.trim(), host, secret: keySecret.value.trim() }),
      t("Yes, test it"), () => runTest(ev, true), { kind: "primary", trigger: ev.target });
  } }, t("Test connection"));
  const runTest = (ev, insecureConfirmed) => busy(ev.target, async () => {
    clear(status);
    const address = url.value.trim();
    if (!address) { status.append(notice(t("Which URL? {example} of the server to test.", { example: "http://192.168.1.20:1234" }), "error")); return; }
    const secret = keySecret.value.trim();
    if (secret && !/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(secret)) { status.append(notice(t("A secret's name: letters, digits and _ (up to 64), not starting with a digit."), "error")); return; }
    // an edited server's own CA is kept unless a file replaces it: the test uses it too (named; read on that host)
    const caOf = !textOf(ca) && editing && editing.ca && address.toLowerCase().startsWith("https://") ? { ca_of: editing.name } : {};
    const sent = Object.assign({ kind: kind.value, url: address }, textOf(ca) ? { ca: textOf(ca) } : caOf, secret ? { api_key_secret: secret } : {},
      insecureConfirmed ? { insecure_http: true, confirm: true } : {});
    clear(testBox).append(el("p", { class: "hint" }, t("Testing {url}…", { url: address })));
    let models, now = null;
    // with what it has loaded and uses now (--status); a host whose sandbox-ai does not know it yet: the models alone
    try { models = await api("POST", hostPath("/llm-models", host), Object.assign({ status: true }, sent)); }
    catch (first) {
      let e = first;
      if (e && e.code === "host-too-old") { try { models = await api("POST", hostPath("/llm-models", host), sent); e = null; } catch (again) { e = again; } }
      if (e) {
        clear(testBox).append(errorNotice(e, t("{url} did not answer with a list of models", { url: address })), ...plainHttpHint(e),
          ...(keyValue.value ? [el("p", { class: "hint" }, t("A key typed here is stored only with Add or Save: the test used the secret named, as that host has it now."))] : []));
        fillModels([]); return;
      }
    }
    if (models && !Array.isArray(models) && typeof models === "object") { now = models.status && typeof models.status === "object" ? models.status : null; models = models.models; }
    const listed = (Array.isArray(models) ? models : []).filter(m => m && typeof m.name === "string");
    const partly = headerSecrets.value.trim() || vpn.value.trim();
    clear(testBox).append(notice(listed.length ? t("{url} answers: {count} model(s) listed.", { url: address, count: listed.length })
      : t("{url} answers, but lists no model: load or pull one there.", { url: address }), listed.length ? "ok" : "warn"),
      ...[now ? el("p", { class: "hint", id: "llm-test-status", title: llmStatusDetail(now) || null }, t("Now: {status}", { status: llmStatusText(now) })) : null,
        listed.length ? modelsTable(address, listed) : null,
        partly ? el("p", { class: "hint" }, t("The test sends no header secret and does not go through a VPN profile: a server that needs one can refuse it and still work for the starts. {check} checks it as the agents reach it, once it is saved.", { check: t("Check") })) : null].filter(Boolean));
    fillModels(listed);
  });
  const cancelEdit = el("button", { onclick: () => {
    for (const input of [name, url, model, keySecret, keyValue, headerSecrets, ca, vpn]) input.value = "";
    for (const box of [anthropic, replace, insecure]) box.checked = false;
    clear(status); clear(testBox); fillModels([]); checkName(); setEditing(null);
  } }, t("Stop editing"));
  cancelEdit.hidden = true;
  addRow.append(save, test, cancelEdit);
  main.append(heading, el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "llm-name" }, t("Name")), name, nameProblem, el("label", { for: "llm-kind" }, t("Kind")), kind,
    el("label", { for: "llm-url" }, "URL"), url, el("label", { for: "llm-model" }, t("Model")), model, modelPickLabel, modelPick,
    el("label", { for: "llm-key-secret" }, t("API key: secret (its name)")), keySecret,
    el("label", { for: "llm-key-value" }, t("API key (the value, optional)")), keyField,
    el("label", { for: "llm-headers" }, t("Headers from secrets (HEADER=SECRET)")), headerSecrets, el("label", { for: "llm-ca" }, t("CA certificate")), ca, caNote,
    el("label", { for: "llm-vpn" }, t("VPN profile")), vpn, el("label", { for: "llm-anthropic" }, t("Also the Anthropic Messages API")), anthropic,
    el("label", { for: "llm-memory", title: t("For what fits the server: the model's size and a quarter more against three quarters of it (sandbox-ai llm set NAME --memory GB)") }, t("Memory (GB)")), memory,
    el("label", { for: "llm-replace" }, t("Replace one of that name")), replace, el("label", { for: "llm-insecure" }, t("Plain http outside the local networks")), insecure,
    el("span"), addRow, status, testBox),
    el("p", { class: "hint", id: "llm-hint" }, t("Then start an agent with {option} (Options of a new start). A server that wants a key: name the secret it is kept as, and type the key once here (or keep it on the page Secrets of that host).", { option: "--llm NAME" }), " ",
      el("button", { onclick: () => show("secrets") }, t("Open Secrets")))));
};

// Kits (sandbox-ai kits on that host): what each brings, and adding to a kit of yours. sandbox-ai's own kits are
// read-only: with one of them (or none) chosen, what is added goes to a kit of yours, or a new one created first.
const KIT_KINDS = { always: N_("always (sandbox-ai)"), default: N_("always (default kit)"), builtin: N_("built-in"), user: N_("yours") };

// The marketplace browser of the Kits page (sandbox-ai plugins marketplaces --json on that host): every marketplace
// (sandbox-ai's own, the kits', the plugin store's, those Claude, Codex and OpenCode know there), one picked shows its
// plugins with their state, and each action changes the kit chosen here (the default kit or one of yours) by
// sandbox-ai kits on that host. The agents' own configuration is only read, never changed.
const MARKET_ORIGINS = { "sandbox-ai": N_("sandbox-ai's own"), kit: N_("a kit"), store: N_("plugin store"), claude: N_("Claude on that host"),
  codex: N_("Codex on that host"), "codex-curated": N_("Codex (curated)"), suggested: N_("suggested") };
const marketView = { host: null, key: null, kit: null };

function marketPluginState(p) {
  const words = [];
  for (const k of Array.isArray(p.kits) ? p.kits : []) words.push(k.enabled ? t("on in {kit}", { kit: k.kit }) : t("off in {kit}", { kit: k.kit }));
  if (p.store && p.store.installed) words.push(p.store.version ? t("installed ({version})", { version: p.store.version }) : t("installed"));
  const a = p.agent || {};
  if (a.installed) words.push(t("the agent has it installed"));
  if (a.enabled === true) words.push(t("the agent enables it"));
  if (a.enabled === false) words.push(t("the agent has it off"));
  if (a.configured) words.push(t("in opencode.json"));
  return words.join(" · ");
}

// After a kit change: the Kits page again, only while it still shows that host (never pulled back from elsewhere).
function kitsAgain(host) {
  if (state.page === "kits" && state.host === host) show("kits");
}

// What the agents on that host have set up themselves (sandbox-ai kits host-list there): read only, grouped by agent
// and kind. The parts ticked go into a kit of yours (kits host-import --kit, asked first: kit-add) or into a project's
// memory there (--project, asked first: memory-add); nothing without a choice. Hooks, plugins, marketplaces and MCP
// servers are shown only; a part with secret-looking lines, or with what the hidden-text checker finds, is refused unless
// that box is ticked too.
async function agentParts(section, host, live, mine) {
  const heading = () => el("h2", {}, t("From the agents on {host}", { host }));
  clear(section).append(heading(), el("p", { class: "hint" }, t("Loading…")));
  let data;
  try { data = await api("GET", hostPath("/agent-parts", host)); }
  catch (e) { if (live()) clear(section).append(heading(), errorNotice(e)); return; }
  if (!live()) return;
  const parts = (Array.isArray(data.parts) ? data.parts : []).filter(p => p && typeof p.id === "string" && typeof p.kind === "string");
  const agents = (Array.isArray(data.agents) ? data.agents : []).filter(a => a && typeof a.agent === "string");
  const preview = el("div"), status = el("div"), ask = el("div", { class: "row" });
  const ticked = new Set();
  const flagsOf = (p) => [p.scripts && p.scripts.length ? t("scripts: {names}", { names: p.scripts.slice(0, 3).join(", ") }) : null,
    p.secrets ? t("{count} secret-looking line(s)", { count: p.secrets }) : null,
    p.findings ? t("{count} hidden-text finding(s)", { count: p.findings }) : null, p.private ? t("private") : null,
    p.hooks ? t("own hooks: taken out on import") : null, !p.importable ? (p.why || t("shown only")) : null].filter(Boolean).join("; ");
  const rows = [];
  for (const agent of agents) {
    for (const p of parts.filter(x => x.agent === agent.agent)) {
      const box = el("input", { type: "checkbox", id: `part-${rows.length}`, disabled: !p.importable, "aria-label": t("Choose {part}", { part: p.id }) });
      box.addEventListener("change", () => { if (box.checked) ticked.add(p.id); else ticked.delete(p.id); });
      const show = rowButton(t("Show"), p.id, host, { onclick: (ev) => busy(ev.target, async () => {
        try { clear(preview).append(el("h3", { class: "mono" }, p.id), dataView(await api("GET", hostPath(`/agent-parts/${encodeURIComponent(p.id)}`, host)))); }
        catch (e) { clear(preview).append(errorNotice(e)); }
      }) });
      rows.push(el("tr", {}, el("td", {}, box), el("td", {}, agent.title || agent.agent), el("td", {}, p.kind),
        el("td", { class: "mono" }, p.name), el("td", {}, p.description || "", p.description ? el("br") : null, el("span", { class: "hint" }, flagsOf(p))),
        el("td", { class: "actions" }, show)));
    }
  }
  const target = el("select", { id: "parts-kit" }, mine.map(n => el("option", { value: n }, n)), el("option", { value: "" }, t("a new kit…")));
  const targetName = el("input", { id: "parts-kit-name", placeholder: "my-kit", value: mine.length ? "" : "my-kit" });
  const project = el("input", { id: "parts-project", class: "mono", placeholder: t("{path} (on that host: the project's folder)", { path: "/home/you/project" }) });
  const secrets = el("input", { type: "checkbox", id: "parts-secrets" });
  const findings = el("input", { type: "checkbox", id: "parts-findings" });
  const agentBoxes = el("div", { class: "row" }, AGENTS.map(a => el("label", {}, el("input", { type: "checkbox", value: a, id: `parts-agent-${a}` }), " ", a)));
  const chosenAgents = () => [...agentBoxes.querySelectorAll("input[type=checkbox]")].filter(b => b.checked).map(b => b.value);
  const run = (ev, into) => {
    clear(status);
    const chosen = parts.filter(p => ticked.has(p.id)).map(p => p.id);
    if (!chosen.length) { status.append(notice(t("Tick the parts to import first: nothing is imported without a choice."), "warn")); return; }
    const fresh = target.value === "";
    const kit = fresh ? targetName.value.trim() : target.value;
    const body = into === "kit" ? { parts: chosen, kit, new: fresh } : { parts: chosen, project: project.value.trim() };
    Object.assign(body, { agents: chosenAgents(), allow_secrets: secrets.checked, allow_findings: findings.checked, confirm: true });
    const go = () => busy(ev.target, async () => {
      try {
        const r = await api("POST", hostPath("/agent-parts-import", host), body);
        clear(status).append(notice(t("Imported"), "ok"), el("pre", {}, (r && r.output) || ""));
        toast("success", t("Imported"), "");
        if (into === "kit") kitsAgain(host);
      } catch (e) { status.append(errorNotice(e)); }
    });
    const key = into === "kit" ? "kit-add" : "memory-add";
    if (!needsConfirm(key)) { go(); return; }
    confirmIn(ask, into === "kit" ? t("Copy {parts} into the kit {kit} on {host}? Every later start with that kit gets them.", { parts: chosen.join(", "), kit, host })
      : t("Add {parts} to the memory of {project} on {host}? Every later start of that project gets them; memory may hold private notes.", { parts: chosen.join(", "), project: body.project, host }),
    t("Yes, import them"), go, { kind: "primary", trigger: ev.target });
  };
  clear(section).append(heading(),
    el("p", { class: "hint" }, t("What the agents on {host} have set up themselves: skills, commands, prompts, subagents, memory files and auto memory, hooks, plugins, marketplaces and MCP servers. Read only; nothing is imported until you tick parts and choose where they go. Hooks, plugins, marketplaces and MCP servers are shown only. Memory and instructions files may hold private notes.", { host })),
    table(["", t("Agent"), t("Kind"), t("Name"), t("About"), ""], rows, t("The agents on this host have no parts of their own.")),
    preview,
    el("div", { class: "card" }, el("div", { class: "form" },
      el("label", { for: "parts-kit" }, t("Kit")), target, el("label", { for: "parts-kit-name" }, t("New kit's name")), targetName,
      el("span"), el("button", { class: "primary", onclick: (ev) => run(ev, "kit") }, t("Import into the kit")),
      el("label", { for: "parts-project" }, t("Project")), project,
      el("span"), el("button", { onclick: (ev) => run(ev, "project") }, t("Add to the project's memory")),
      el("span", {}, t("For (empty: each part's own agent)")), agentBoxes,
      el("label", { for: "parts-secrets" }, t("Also with secret-looking lines")), secrets,
      el("label", { for: "parts-findings" }, t("Also with hidden text (invisible characters, instructions to the model)")), findings,
      ask, status)));
}

async function kitMarketplaces(section, host, live) {
  clear(section).append(el("h2", {}, t("Marketplaces")), el("p", { class: "hint" }, t("Loading…")));
  let data;
  try { data = await api("GET", hostPath("/marketplaces", host)); }
  catch (e) { if (live()) clear(section).append(el("h2", {}, t("Marketplaces")), errorNotice(e)); return; }
  if (!live()) return;
  if (marketView.host !== host) Object.assign(marketView, { host, key: null, kit: null });
  renderMarketplaces(section, host, data, live);
}

// FOCUS after a redraw of the section: "kit" (the kit chooser), "plugins" (the heading of the plugins shown),
// "refresh" (its Refresh button); the redraw keeps the reader where they were instead of the page's top.
function renderMarketplaces(section, host, data, live, focus) {
  const here = () => section.isConnected && state.page === "kits" && state.host === host && (!live || live());
  const again = (where) => renderMarketplaces(section, host, data, live, where);
  const reload = (where) => { if (!here()) return; kitMarketplaces(section, host, live || (() => true)).then(() => focusIn(where)); };
  const focusIn = (where) => {
    if (!where || !section.isConnected) return;
    const target = where === "kit" ? section.querySelector("#market-kit") : where === "refresh" ? section.querySelector("#market-refresh")
      : section.querySelector("#market-plugins");
    if (target) target.focus();
  };
  const markets = (Array.isArray(data.marketplaces) ? data.marketplaces : []).filter(m => m && typeof m.name === "string" && typeof m.agent === "string");
  const agents = data.agents && typeof data.agents === "object" ? data.agents : {};
  const editable = (Array.isArray(data.kits) ? data.kits : []).filter(k => k && k.editable && typeof k.name === "string" && (k.kind === "default" || k.kind === "user"));
  if (!editable.some(k => k.name === marketView.kit)) marketView.kit = editable.length ? (editable.find(k => k.kind === "user") || editable[0]).name : null;
  const kitChoice = el("select", { id: "market-kit" }, editable.map(k => el("option", { value: k.name, selected: k.name === marketView.kit }, k.kind === "default" ? t("the default kit (every sandbox)") : k.name)));
  if (marketView.kit) kitChoice.value = marketView.kit;
  const status = el("div");
  const ask = el("div", { class: "row" });
  const kit = () => editable.find(k => k.name === kitChoice.value) || null;
  const inKit = (entries) => { const k = kit(); return (Array.isArray(entries) ? entries : []).find(e => k && e.kit === k.name && e.kind === k.kind) || null; };
  // one kit change: asked first when it widens (kit-add) or narrows (kit-remove) every later start with that kit
  const change = (ev, action, body, question, confirmKey, title) => {
    const k = kit();
    if (!k) { toast("warning", t("Which kit?"), t("Create a kit of yours first, or use the default kit.")); return; }
    const go = () => busy(ev.target, async () => {
      clear(status);
      try {
        const r = await api("POST", hostPath(`/kits/${encodeURIComponent(k.name)}/${action}`, host), Object.assign({}, body, { confirm: true }));
        toast("success", title, (r && r.output) || "");
        reload(marketView.key ? "plugins" : "kit");  // only while this host's Kits page is still shown
      } catch (e) {
        if (here()) status.append(errorNotice(e));
        else failed(t("Could not change the kit {kit} on {host}", { kit: k.name, host }))(e);  // said where the user is now
      }
    });
    if (!needsConfirm(confirmKey)) { go(); return; }
    confirmIn(ask, question, confirmKey === "kit-add" ? t("Yes, change the kit") : t("Yes, take it out"), go, { kind: confirmKey === "kit-add" ? "primary" : undefined, trigger: ev.target });
  };
  const kitLabel = () => { const k = kit(); return k ? (k.kind === "default" ? t("the default kit") : k.name) : "?"; };
  kitChoice.addEventListener("change", () => { marketView.kit = kitChoice.value; again("kit"); });

  clear(section).append(el("h2", {}, t("Marketplaces")),
    el("p", { class: "hint" }, t("Every plugin marketplace on {host}: sandbox-ai's own, those the kits declare, and those Claude, Codex and OpenCode already know there. Pick one to see its plugins; what you add, enable, disable or remove changes the kit chosen here. The agents' own configuration is only read.", { host })),
    el("div", { class: "card" }, el("div", { class: "form" }, el("label", { for: "market-kit" }, t("Kit")), kitChoice,
      el("span"), el("div", { class: "row" },
        el("button", { onclick: () => openTerminal({ kind: "plugins", host, agent: "claude", marketplaces_only: "1" }, t("plugins on {host}", { host }), () => reload()) }, t("Fetch the plugin lists")),
        el("button", { onclick: () => openTerminal({ kind: "plugins", host, agent: "claude" }, t("plugins on {host}", { host }), () => reload()) }, t("Install the kits' plugins")),
        el("button", { onclick: () => pluginStatus() }, t("Plugin status")),
        el("button", { id: "market-refresh", onclick: () => reload("refresh") }, t("Refresh"))),
      el("p", { class: "hint wide" }, t("A marketplace's plugin list comes from a copy fetched before: {command} fetches the kits' marketplaces in a sandbox that reaches only their hosts.", { command: "sandbox-ai plugins claude --marketplaces-only" })))),
    ask, status);
  // sandbox-ai plugins --status there: what the kits declare and what is installed, as it says it
  const pluginStatus = async () => {
    clear(status).append(el("p", { class: "hint" }, t("Loading…")));
    try {
      const r = await api("GET", hostPath("/plugins-status", host));
      if (here()) clear(status).append(el("h3", {}, t("Plugin status on {host}", { host })), el("pre", {}, String((r && r.output) || "")));
    } catch (e) { if (here()) clear(status).append(errorNotice(e)); }
  };

  const keyOf = (m) => `${m.agent}:${m.name}`;
  const rows = markets.map(m => {
    const declared = inKit(m.kits);
    const actions = el("td", { class: "actions" });
    actions.append(rowButton(marketView.key === keyOf(m) ? t("Shown") : t("Plugins"), m.name, host, { class: marketView.key === keyOf(m) ? "primary" : undefined,
      onclick: () => { marketView.key = keyOf(m); again("plugins"); } }));
    if (m.agent === "claude" && declared) {
      // the kit's plugins of that marketplace go with it (the command refuses otherwise): named in the question
      const its = (Array.isArray(m.plugins) ? m.plugins : []).filter(p => p && typeof p.id === "string" && inKit(p.kits)).map(p => p.id);
      actions.append(rowButton(t("Remove from the kit"), m.name, host, { class: "danger", onclick: (ev) => change(ev, "remove-marketplace", { marketplace: m.name, with_plugins: its.length > 0 },
        its.length ? t("Take the marketplace {marketplace} with its plugins ({plugins}) out of {kit} on {host}? Every later start with that kit goes without them.", { marketplace: m.name, plugins: its.join(", "), kit: kitLabel(), host })
          : t("Take the marketplace {marketplace} out of {kit} on {host}? The kit has none of its plugins.", { marketplace: m.name, kit: kitLabel(), host }), "kit-remove", t("{kit}: removed {value}", { kit: kitLabel(), value: m.name })) }));
    } else if (m.agent === "claude" && m.importable && m.source_value) {
      actions.append(rowButton(t("Add to the kit"), m.name, host, { onclick: (ev) => change(ev, "add-marketplace", { marketplace: m.name, source: m.source_value },
        t("Declare the marketplace {marketplace} ({source}) in {kit} on {host}? Its plugins can then be added there.", { marketplace: m.name, source: m.source_value, kit: kitLabel(), host }), "kit-add", t("Marketplace added to {kit}", { kit: kitLabel() })) }));
      if (m.agent_config && (m.plugins || []).some(p => p.agent && p.agent.enabled === true)) {
        actions.append(rowButton(t("Import with Claude's plugins"), m.name, host, { onclick: (ev) => change(ev, "import-marketplace", { marketplace: m.name, from: "claude", with_plugins: true },
          t("Declare the marketplace {marketplace} in {kit} on {host} with the plugins Claude enables from it there? Every later start with that kit gets them.", { marketplace: m.name, kit: kitLabel(), host }), "kit-add", t("Marketplace added to {kit}", { kit: kitLabel() })) }));
      }
    } else if (m.agent === "codex" && m.import) {
      actions.append(rowButton(t("Import for Claude"), m.name, host, { onclick: (ev) => change(ev, "import-marketplace", { marketplace: m.name, from: "codex" },
        t("Declare Codex's marketplace {marketplace} for Claude, as {name}, in {kit} on {host}?", { marketplace: m.name, name: m.import.name, kit: kitLabel(), host }), "kit-add", t("Marketplace added to {kit}", { kit: kitLabel() })) }));
    }
    const origins = (Array.isArray(m.origins) ? m.origins : []).map(o => MARKET_ORIGINS[o] ? t(MARKET_ORIGINS[o]) : String(o)).join(", ");
    const count = Array.isArray(m.plugins) ? m.plugins.length : 0;
    return el("tr", {}, el("td", {}, m.agent), el("td", { class: "mono" }, m.name, m.description ? el("div", { class: "hint" }, m.description) : null),
      el("td", { class: "mono" }, m.source_value || "?", m.problem ? el("div", { class: "hint" }, t("not for a kit: {why}", { why: m.problem })) : null),
      el("td", {}, origins, declared ? el("div", { class: "hint" }, t("declared in {kit}", { kit: kitLabel() })) : null),
      el("td", {}, m.listed_from ? String(count) : t("{count} (list not fetched)", { count })), actions);
  });
  section.append(table([t("Agent"), t("Marketplace"), t("Source"), t("From"), t("Plugins"), ""], rows, t("No marketplace on this host."), t("Marketplaces")));

  // the plugins of the marketplace picked
  const picked = markets.find(m => keyOf(m) === marketView.key);
  if (picked) {
    const marketDeclared = inKit(picked.kits);
    const pluginRows = (Array.isArray(picked.plugins) ? picked.plugins : []).filter(p => p && typeof p.id === "string").map(p => {
      const mine = inKit(p.kits);
      const actions = el("td", { class: "actions" });
      if (picked.agent === "claude") {
        if (!mine) {
          const body = { plugin: p.id, agent: "claude" };
          if (!marketDeclared && picked.importable && picked.source_value) body.source = picked.source_value;
          if (marketDeclared || body.source) actions.append(rowButton(t("Add to the kit"), p.id, host, { class: "primary", onclick: (ev) => change(ev, "add-plugin", body,
            t("Add {what} to the kit {kit} on {host}? Every later start with that kit gets it.", { what: p.id, kit: kitLabel(), host }), "kit-add", t("Plugin added to {kit}", { kit: kitLabel() })) }));
          const elsewhere = (p.kits || []).some(k => k.enabled && k.kind === "default");
          if (elsewhere && kit() && kit().kind !== "default") actions.append(rowButton(t("Disable"), p.id, host, { onclick: (ev) => change(ev, "disable-plugin", { plugin: p.id, agent: "claude" },
            t("Turn {plugin} off in every start with {kit} on {host}, though the default kit enables it?", { plugin: p.id, kit: kitLabel(), host }), "kit-remove", t("{kit}: {plugin} disabled", { kit: kitLabel(), plugin: p.id })) }));
        } else {
          actions.append(mine.enabled
            ? rowButton(t("Disable"), p.id, host, { onclick: (ev) => change(ev, "disable-plugin", { plugin: p.id, agent: "claude" },
              t("Turn {plugin} off in every start with {kit} on {host}? It stays in the kit (and installed).", { plugin: p.id, kit: kitLabel(), host }), "kit-remove", t("{kit}: {plugin} disabled", { kit: kitLabel(), plugin: p.id })) })
            : rowButton(t("Enable"), p.id, host, { onclick: (ev) => change(ev, "enable-plugin", { plugin: p.id, agent: "claude" },
              t("Turn {plugin} on in every start with {kit} on {host}?", { plugin: p.id, kit: kitLabel(), host }), "kit-add", t("{kit}: {plugin} enabled", { kit: kitLabel(), plugin: p.id })) }),
          rowButton(t("Remove"), p.id, host, { class: "danger", onclick: (ev) => change(ev, "remove-plugin", { plugin: p.id, agent: "claude" },
            t("Remove the {kind} {value} for {agent} from the kit {kit} on {host}?", { kind: "plugin", value: p.id, agent: "claude", kit: kitLabel(), host }), "kit-remove", t("{kit}: removed {value}", { kit: kitLabel(), value: p.id })) }));
        }
      }
      return el("tr", {}, el("td", { class: "mono" }, p.name || p.id, p.version ? el("span", { class: "hint" }, ` ${p.version}`) : null),
        el("td", {}, p.description || (p.listed === false ? t("not in its plugin list (named by a kit or the agent)") : "")),
        el("td", {}, marketPluginState(p) || el("span", { class: "hint" }, t("in no kit"))), actions);
    });
    section.append(...[el("h3", { id: "market-plugins", tabindex: "-1" }, t("Plugins of {marketplace} ({agent})", { marketplace: picked.name, agent: picked.agent })),
      picked.agent === "codex" ? el("p", { class: "hint" }, t("Codex's own marketplace, as Codex has it on that host: kits give Codex plugins of the marketplaces in Claude's format.")) : null,
      picked.agent === "claude" && !marketDeclared && !picked.importable ? el("p", { class: "hint" }, t("Its plugins cannot go into a kit: {why}", { why: picked.problem || "?" })) : null,
      picked.listed_from ? null : el("p", { class: "hint" }, t("Its plugin list is not fetched yet: only the plugins a kit or the agent names are shown.")),
      table([t("Plugin"), t("Description"), t("State"), ""], pluginRows, t("No plugins known."))].filter(Boolean));
  }

  // OpenCode: no marketplaces; its npm plugins
  const oc = agents.opencode;
  if (oc && Array.isArray(oc.plugins)) {
    const ocRows = oc.plugins.filter(p => p && typeof p.name === "string").map(p => {
      const mine = inKit(p.kits);
      const actions = el("td", { class: "actions" });
      if (!mine && p.importable) actions.append(rowButton(t("Add to the kit"), p.name, host, { class: "primary", onclick: (ev) => change(ev, "add-plugin", { plugin: p.name, agent: "opencode" },
        t("Add {what} to the kit {kit} on {host}? Every later start with that kit gets it.", { what: p.name, kit: kitLabel(), host }), "kit-add", t("Plugin added to {kit}", { kit: kitLabel() })) }));
      // one the default kit enables: off for the starts with the kit chosen here, as for Claude's
      if (!mine && (p.kits || []).some(k => k.enabled && k.kind === "default") && kit() && kit().kind !== "default")
        actions.append(rowButton(t("Disable"), p.name, host, { onclick: (ev) => change(ev, "disable-plugin", { plugin: p.name, agent: "opencode" },
          t("Turn {plugin} off in every start with {kit} on {host}, though the default kit enables it?", { plugin: p.name, kit: kitLabel(), host }), "kit-remove", t("{kit}: {plugin} disabled", { kit: kitLabel(), plugin: p.name })) }));
      if (mine) actions.append(mine.enabled
        ? rowButton(t("Disable"), p.name, host, { onclick: (ev) => change(ev, "disable-plugin", { plugin: p.name, agent: "opencode" },
          t("Turn {plugin} off in every start with {kit} on {host}? It stays in the kit (and installed).", { plugin: p.name, kit: kitLabel(), host }), "kit-remove", t("{kit}: {plugin} disabled", { kit: kitLabel(), plugin: p.name })) })
        : rowButton(t("Enable"), p.name, host, { onclick: (ev) => change(ev, "enable-plugin", { plugin: p.name, agent: "opencode" },
          t("Turn {plugin} on in every start with {kit} on {host}?", { plugin: p.name, kit: kitLabel(), host }), "kit-add", t("{kit}: {plugin} enabled", { kit: kitLabel(), plugin: p.name })) }),
        rowButton(t("Remove"), p.name, host, { class: "danger", onclick: (ev) => change(ev, "remove-plugin", { plugin: p.name, agent: "opencode" },
          t("Remove the {kind} {value} for {agent} from the kit {kit} on {host}?", { kind: "plugin", value: p.name, agent: "opencode", kit: kitLabel(), host }), "kit-remove", t("{kit}: removed {value}", { kit: kitLabel(), value: p.name })) }));
      return el("tr", {}, el("td", { class: "mono" }, p.name), el("td", {}, marketPluginState(p) || el("span", { class: "hint" }, t("in no kit"))), actions);
    });
    section.append(el("h3", {}, t("OpenCode's npm plugins")),
      el("p", { class: "hint" }, t("OpenCode has no marketplaces: its plugins are npm packages, which a kit lists and OpenCode installs itself (it needs {option}).", { option: "--allow-host registry.npmjs.org" })),
      table([t("Plugin"), t("State"), ""], ocRows, t("No npm plugins in opencode.json or a kit.")));
    const modules = (Array.isArray(oc.modules) ? oc.modules : []).filter(m => typeof m === "string");
    if (modules.length) section.append(el("p", { class: "hint" }, t("OpenCode's plugin modules there (a kit takes one as a hook, below):")), el("ul", {}, modules.map(m => el("li", { class: "mono" }, m))));
  }
  const notes = [];
  for (const [agent, info] of Object.entries(agents)) {
    if (!info || !Array.isArray(info.notes)) continue;
    for (const note of info.notes) if (typeof note === "string") notes.push(el("li", {}, el("strong", {}, `${agent}: `), note));
  }
  for (const e of Array.isArray(data.errors) ? data.errors : []) if (e && typeof e.error === "string") notes.push(el("li", { class: "status-bad" }, `${e.kit || "?"} (${e.agent || "?"}): ${e.error}`));
  if (notes.length) section.append(el("ul", { class: "hint" }, notes));
  focusIn(focus);
}

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
      const entries = [...listOf(info.skills).map(s => ["skill", s]), ...listOf(info.plugins).map(p => ["plugin", p]), ...listOf(info.disabled).map(p => ["plugin", p, true])];
      if (!entries.length) line.append(el("span", { class: "hint" }, t("nothing")));
      for (const [kind, value, off] of entries) {
        line.append(el("span", { class: "mono" }, `${kind} ${value}`));
        if (off) line.append(el("span", { class: "hint" }, ` ${t("(off)")}`));
        const holder = el("span", { class: "inline-ask" });
        if (own || (k.kind === "default" && kind === "plugin")) line.append(" ", holder);
        const label = t("Remove the {kind} {value} for {agent}", { kind, value, agent });
        holder.append(el("button", { class: "danger small", title: label, "aria-label": label,
          onclick: () => confirmIn(holder, t("Remove the {kind} {value} for {agent} from the kit {kit} on {host}?", { kind, value, agent, kit: k.name, host }), t("Yes, remove it"), async () => {
            const body = Object.assign(kind === "skill" ? { skill: value, agents: [agent] } : { plugin: value, agent }, { confirm: true });
            try { result(t("{kit}: removed {value}", { kit: k.name, value }), await api("POST", hostPath(`/kits/${encodeURIComponent(k.name)}/remove-${kind}`, host), body)); kitsAgain(host); }
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
      try { result(t("{kit} deleted", { kit: k.name }), await api("DELETE", hostPath(`/kits/${encodeURIComponent(k.name)}`, host), { confirm: true })); kitsAgain(host); }
      catch (e) { failed(t("Could not delete {kit}", { kit: k.name }))(e); }
    }) }));
    return el("tr", {}, el("td", { class: "mono" }, k.name), el("td", {}, KIT_KINDS[k.kind] ? t(KIT_KINDS[k.kind]) : String(k.kind || "")), brings, kitActions);
  });
  main.append(table([t("Kit"), t("Kind"), t("Brings"), ""], rows, t("No kits on this host.")), output);
  const marketSection = el("section", { id: "kit-marketplaces" });
  main.append(marketSection);
  kitMarketplaces(marketSection, host, live);  // its own request: an older host's refusal stays in that section
  const partsSection = el("section", { id: "kit-agent-parts" });
  main.append(partsSection);
  agentParts(partsSection, host, live, mine);  // the same: its own request, its own refusal

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
      try { result(t("Kit {kit} created", { kit: newName.value.trim() }), await api("POST", hostPath("/kits"), { name: newName.value.trim(), agents: chosenAgents(newAgents) })); kitsAgain(host); }
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
      try { result(title(name), await api("POST", hostPath(`/kits/${encodeURIComponent(name)}/${action}`, host), Object.assign({}, request, WIDENS[action] ? { confirm: true } : {}))); kitsAgain(host); }
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
  // a kit's marketplaces are Claude's (OpenCode has none) and come from GitHub or a Git URL: a folder is refused by
  // sandbox-ai kits (sandboxes get only sandbox-ai's own marketplace as a directory)
  const marketSource = el("input", { id: "kit-market-source", class: "mono", placeholder: t("OWNER/REPO or https://… (a Git repository)") });
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
    el("span"), el("button", { class: "primary", onclick: send("add-marketplace", () => ({ marketplace: marketName.value.trim(), source: marketSource.value.trim() }), n => t("Marketplace added to {kit}", { kit: n })) }, t("Add marketplace")),
    el("p", { class: "hint wide" }, t("A marketplace the kit declares for Claude (OpenCode has no marketplaces): its plugins can then be added by NAME@MARKETPLACE.")),
    el("h3", { class: "wide" }, t("A hook")),
    el("label", { for: "kit-hook-file" }, t("Hook file")), hookFile, el("label", { for: "kit-hook-as" }, t("Name in the kit")), hookAs,
    el("label", { for: "kit-hook-event" }, t("Event")), hookEvent, el("label", { for: "kit-hook-matcher" }, t("Tools")), hookMatcher,
    el("label", { for: "kit-hook-timeout" }, t("Timeout (seconds)")), hookTimeout,
    el("span"), el("div", { class: "row" }, el("button", { class: "primary", onclick: send("add-hook", () => ({ file: hookFile.value.trim(), as: hookAs.value.trim(), event: hookEvent.value.trim(), matcher: hookMatcher.value.trim(), timeout: hookTimeout.value.trim(), agents: chosenAgents(skillAgents) }), n => t("Hook added to {kit}", { kit: n })) }, t("Add hook")),
      el("button", { class: "danger", onclick: (ev) => {
        const name = target.value, file = hookAs.value.trim() || hookFile.value.trim().split("/").pop();
        if (!name || !file) { toast("warning", t("Which hook?"), t("Choose a kit of yours and name the hook's file.")); return; }
        confirmIn(addAsk, t("Remove the hook {file} from the kit {kit} on {host}?", { file, kit: name, host }), t("Yes, remove it"), async () => {
          try { result(t("{kit}: removed {value}", { kit: name, value: file }), await api("POST", hostPath(`/kits/${encodeURIComponent(name)}/remove-hook`, host), { file, agents: chosenAgents(skillAgents), confirm: true })); kitsAgain(host); }
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

// The system monitor: what the running sandboxes of the host use now (GET .../monitor: sandbox-ai top, as the
// collectors recorded it) grouped by project, with a trend of the last 15 minutes, the host's totals, the warnings
// with what helps (raise a limit, stop an idle run: asked first, in place) and the history of the run chosen or of
// the host over a window (GET .../monitor/usage/24h: sandbox-ai stats usage). Read again every MONITOR_EVERY ms while
// the page is shown, in place; never while another page is.
const MONITOR_EVERY = 10000;
const MONITOR_WINDOWS = [["15m", N_("15 minutes")], ["1h", N_("1 hour")], ["24h", N_("24 hours")], ["7d", N_("7 days")], ["30d", N_("30 days")]];
const MONITOR_TITLES = {
  memory: N_("memory near the limit"), oom: N_("out of memory: the kernel ended processes"), cpu: N_("held back by the CPU limit"),
  io: N_("waiting for the disk (IO pressure)"), idle: N_("idle, holding memory"), pids: N_("processes and threads near the limit"),
  "host-memory": N_("the host is low on memory"), "host-swap": N_("the host is swapping"),
};
const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs, ...children) {
  const node = document.createElementNS ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) if (value !== null && value !== undefined) node.setAttribute(key, String(value));
  for (const child of children) if (child) node.append(child);
  return node;
}

function monitorTitle(kind) { return MONITOR_TITLES[kind] ? t(MONITOR_TITLES[kind]) : String(kind || ""); }
// The seconds between two samples of the runs shown (each recorder's own: monitor_interval; 10 unless changed).
function monitorSeconds(data) {
  const found = [...new Set(((data && data.runs) || []).map(r => monitorNumber(r.interval)).filter(v => v))].sort((a, b) => a - b);
  return found.length ? found.join("/") : "10";
}
function monitorNumber(value) { return typeof value === "number" && Number.isFinite(value) ? value : null; }
function gibText(value) { return ((monitorNumber(value) || 0) / 1073741824).toFixed(1); }
function rateText(value) { return `${readableSize(Math.round(monitorNumber(value) || 0))}/s`; }

// POINTS ([[t, cpu, mem, io, net], ...]) as a small SVG: memory as a filled area, CPU as a line, each to its own top.
function trendSvg(points, width, height, label, columns) {
  const svg = svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label, class: "trend" });
  const rows = (Array.isArray(points) ? points : []).filter(p => Array.isArray(p) && p.length >= 3);
  if (rows.length < 2) return svg;
  const [line, area] = columns || [1, 2];
  const t0 = rows[0][0], span = Math.max(1, rows[rows.length - 1][0] - t0);
  const xy = (column) => {
    const top = Math.max(...rows.map(r => monitorNumber(r[column]) || 0)) || 1;
    return rows.map(r => [((r[0] - t0) / span) * (width - 2) + 1, height - 1 - ((monitorNumber(r[column]) || 0) / top) * (height - 2)]);
  };
  const filled = xy(area);
  svg.append(svgEl("polygon", { points: [[1, height - 1], ...filled, [width - 1, height - 1]].map(p => p.join(",")).join(" "), class: "trend-memory" }));
  svg.append(svgEl("polyline", { points: xy(line).map(p => p.join(",")).join(" "), class: "trend-cpu" }));
  return svg;
}

// The charts and gauges of the run chosen (nothing chosen: the host), as the desktop app's (monitor_charts.py): its
// CPU, memory (the 92 % where the sandbox is slowed down, the limit where the kernel ends processes), disk IO, network
// and processes over the window - the history's detail (stats usage --series) and every look at top since (a bounded
// ring per run and the host) - and every run's CPU and memory stacked. Lines break where samples are missing (a gap);
// pointing at a chart shows the exact values at that time. Inline SVG, coloured by the theme's variables (--series0
// ... set by applyTheme): a change of the theme is followed without drawing again.
const MONITOR_SLOW = 0.92;        // agent_sandbox.MEMORY_HIGH: memory.high at this share of the limit
const MONITOR_NEAR = 0.90;        // monitor.MEMORY_NEAR, PIDS_NEAR: the warnings start here
const MONITOR_THROTTLE = 50;      // monitor.THROTTLE_RATIO
const MONITOR_HOST_LOW = 0.10;    // monitor.HOST_MEMORY_LOW
const MONITOR_RING = 720;         // looks kept per run and for the host
const MONITOR_SLOTS = 240;        // monitor.SERIES_POINTS: the time slots of a window
const MONITOR_STACKED = 8;        // runs stacked on their own; the others as one
const MONITOR_GAP = 2.5;          // a step this many times the usual one is a gap
const MONITOR_SECONDS = { "15m": 900, "1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000 };

function monitorPointsOfDetail(detail) {
  if (!detail || !Array.isArray(detail.t)) return [];
  const columns = Object.entries(detail).filter(([key, values]) => key !== "t" && Array.isArray(values) && values.length === detail.t.length);
  const points = [];
  detail.t.forEach((when, index) => {
    if (monitorNumber(when) === null) return;
    const point = { t: when };
    for (const [key, values] of columns) if (monitorNumber(values[index]) !== null) point[key] = values[index];
    points.push(point);
  });
  return points.sort((a, b) => a.t - b.t);
}

function monitorPointsOfSeries(rows, keys) {
  return (Array.isArray(rows) ? rows : []).filter(r => Array.isArray(r) && r.length === keys.length + 1 && monitorNumber(r[0]) !== null)
    .map(r => { const point = { t: r[0] }; keys.forEach((key, i) => { if (monitorNumber(r[i + 1]) !== null) point[key] = r[i + 1]; }); return point; })
    .sort((a, b) => a.t - b.t);
}

function monitorMerged(history, recent, since) {
  const found = history.filter(p => p.t >= since);
  const last = found.length ? found[found.length - 1].t : -Infinity;
  return found.concat(recent.filter(p => p.t > last && p.t >= since));
}

// {slot: {key: value}} of POINTS in slots of STEP seconds: averages (memory, processes and limits: the highest)
function monitorSlots(points, step) {
  const sums = new Map();
  for (const p of points) {
    const slot = Math.floor(p.t / step);
    if (!sums.has(slot)) sums.set(slot, {});
    const values = sums.get(slot);
    for (const [key, value] of Object.entries(p)) {
      if (key === "t" || monitorNumber(value) === null) continue;
      const highest = key === "mem" || key === "pids" || key.endsWith("_lim");
      const had = values[key] || [0, 0];
      values[key] = highest ? [Math.max(had[0], value), 1] : [had[0] + value, had[1] + 1];
    }
  }
  const found = new Map();
  for (const [slot, values] of sums) found.set(slot, Object.fromEntries(Object.entries(values).map(([k, [v, n]]) => [k, v / n])));
  return found;
}

// several runs' points added up per slot (a limit only where every run of the slot has one)
function monitorTotal(series, step) {
  const each = series.map(points => monitorSlots(points, step));
  const slots = [...new Set(each.flatMap(m => [...m.keys()]))].sort((a, b) => a - b);
  return slots.map(slot => {
    const present = each.filter(m => m.has(slot)).map(m => m.get(slot));
    const point = { t: (slot + 1) * step };
    for (const key of new Set(present.flatMap(v => Object.keys(v)))) {
      if (key.endsWith("_lim") && present.some(v => !(key in v))) continue;
      point[key] = present.reduce((sum, v) => sum + (v[key] || 0), 0);
    }
    return point;
  });
}

// SERIES ({run: points}) as one list of points {t, run: KEY's value} per slot (every run stacked)
function monitorJoined(series, key, step) {
  const slots = new Map();
  for (const [run, points] of Object.entries(series)) {
    for (const [slot, values] of monitorSlots(points, step)) {
      if (!(key in values)) continue;
      if (!slots.has(slot)) slots.set(slot, {});
      slots.get(slot)[run] = values[key];
    }
  }
  return [...slots.entries()].sort((a, b) => a[0] - b[0]).map(([slot, values]) => Object.assign({ t: (slot + 1) * step }, values));
}

// a line of a chart: LABEL, VALUE (a key or point => number), COLOUR (a CSS variable's name), KIND (line, area,
// stack, limit)
function chartLine(label, value, colour, kind) {
  const get = typeof value === "function" ? value : (p => monitorNumber(p[value]));
  return { label, value: (p) => { const v = get(p); return monitorNumber(v); }, colour, kind: kind || "line" };
}

function chartTime(seconds, span, exact) {
  const d = new Date(seconds * 1000);
  const two = (n) => String(n).padStart(2, "0");
  const clock = `${two(d.getHours())}:${two(d.getMinutes())}` + (exact ? `:${two(d.getSeconds())}` : "");
  return span >= 86400 ? `${two(d.getDate())}.${two(d.getMonth() + 1)}. ${clock}` : clock;
}

// the chart (see above): TITLE, POINTS ([{t, key: value}]), LINES (chartLine), UNIT (a value's text), SPAN ([start,
// end] seconds), EMPTY (said without a value), NAME (its data-chart)
function chartSvg(name, title, points, lines, unit, span, empty) {
  const W = 480, H = 160, left = 56, right = 8, top = 24, bottom = 18;
  const area = { x: left, y: top, w: W - left - right, h: H - top - bottom };
  const latest = lines.map(line => { const v = points.map(line.value).filter(x => x !== null); return v.length ? [line, v[v.length - 1]] : null; }).filter(Boolean);
  const summary = `${title}: ` + (latest.length ? latest.map(([line, v]) => `${line.label} ${unit(v)}`).join(", ") : empty);
  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": summary, class: "chart", "data-chart": name });
  svg.append(svgEl("title", {}, document.createTextNode(summary)));
  svg.append(svgEl("text", { x: 4, y: 15, class: "chart-title" }, document.createTextNode(title)));
  let x = W - right;
  for (const [line, v] of latest.slice().reverse()) {
    const words = `${line.label} ${unit(v)}`;
    x -= words.length * 6.2 + 10;
    if (x < W * 0.45) break;
    svg.append(svgEl("text", { x: Math.round(x), y: 15, class: "chart-legend", style: `fill:var(--${line.colour})`, "data-line": line.label }, document.createTextNode(words)));
  }
  const figure = el("figure", { class: "monitor-chart" }, svg);
  if (!latest.length) {
    svg.append(svgEl("text", { x: W / 2, y: H / 2, class: "chart-empty", "text-anchor": "middle" }, document.createTextNode(empty)));
    return figure;
  }
  const [start, end] = span[1] > span[0] ? span : [span[0] - 30, span[0] + 30];
  const stacks = new Map(), below = points.map(() => 0);
  lines.forEach((line, index) => {
    if (line.kind !== "stack") return;
    stacks.set(index, points.map((p, i) => { const low = below[i]; below[i] += line.value(p) || 0; return [low, below[i]]; }));
  });
  let highest = 0;
  lines.forEach((line, index) => {
    for (const [i, p] of points.entries()) highest = Math.max(highest, stacks.has(index) ? stacks.get(index)[i][1] : line.value(p) || 0);
  });
  const roof = highest * 1.1 || 1;
  const X = (t) => area.x + area.w * (t - start) / Math.max(1e-9, end - start);
  const Y = (v) => area.y + area.h - area.h * Math.min(Math.max(v, 0), roof) / roof;
  const steps = points.slice(1).map((p, i) => p.t - points[i].t).filter(s => s > 0).sort((a, b) => a - b);
  const gap = steps.length ? MONITOR_GAP * steps[Math.floor(steps.length / 2)] : Infinity;
  svg.append(svgEl("rect", { x: area.x, y: area.y, width: area.w, height: area.h, class: "chart-frame" }));
  svg.append(svgEl("text", { x: area.x - 4, y: Y(highest) + 4, class: "chart-axis", "text-anchor": "end" }, document.createTextNode(unit(highest))));
  svg.append(svgEl("text", { x: area.x - 4, y: area.y + area.h, class: "chart-axis", "text-anchor": "end" }, document.createTextNode(unit(0))));
  svg.append(svgEl("text", { x: area.x, y: H - 3, class: "chart-axis" }, document.createTextNode(chartTime(start, end - start))));
  svg.append(svgEl("text", { x: area.x + area.w, y: H - 3, class: "chart-axis", "text-anchor": "end" }, document.createTextNode(chartTime(end, end - start))));
  lines.forEach((line, index) => {
    const pieces = [];
    let piece = [], before = null;
    points.forEach((p, i) => {
      const v = stacks.has(index) ? stacks.get(index)[i][1] : line.value(p);
      if (v === null || (before !== null && p.t - before > gap)) { if (piece.length) pieces.push(piece); piece = []; }
      if (v !== null) piece.push([i, X(p.t), Y(v)]);
      before = p.t;
    });
    if (piece.length) pieces.push(piece);
    const colour = `var(--${line.colour})`;
    for (const part of pieces) {
      const xy = (list) => list.map(([, px, py]) => `${px.toFixed(1)},${py.toFixed(1)}`).join(" ");
      if (line.kind === "area") {
        svg.append(svgEl("polygon", { points: `${part[0][1].toFixed(1)},${area.y + area.h} ${xy(part)} ${part[part.length - 1][1].toFixed(1)},${area.y + area.h}`, class: "chart-area", style: `fill: ${colour}` }));
      } else if (line.kind === "stack") {
        const base = part.slice().reverse().map(([i, px]) => `${px.toFixed(1)},${Y(stacks.get(index)[i][0]).toFixed(1)}`).join(" ");
        svg.append(svgEl("polygon", { points: `${xy(part)} ${base}`, class: "chart-stack", style: `fill: ${colour}` }));
      }
      if (part.length === 1) svg.append(svgEl("circle", { cx: part[0][1].toFixed(1), cy: part[0][2].toFixed(1), r: 2.5, style: `fill: ${colour}`, class: "chart-dot" }));
      else svg.append(svgEl("polyline", { points: xy(part), class: line.kind === "limit" ? "chart-limit" : "chart-line", style: `stroke: ${colour}`, "data-line": line.label }));
    }
  });
  // the exact values at the time pointed at
  const cursor = svgEl("line", { x1: 0, x2: 0, y1: area.y, y2: area.y + area.h, class: "chart-cursor", visibility: "hidden" });
  const tip = svgEl("g", { class: "chart-tip", visibility: "hidden" });
  svg.append(cursor, tip);
  const tipAt = (offsetX, width) => {
    const at = area.x + Math.max(0, Math.min(1, ((offsetX * W / (width || W)) - area.x) / area.w)) * area.w;
    let nearest = 0;
    points.forEach((p, i) => { if (Math.abs(X(p.t) - at) < Math.abs(X(points[nearest].t) - at)) nearest = i; });
    const p = points[nearest];
    const rows = [chartTime(p.t, end - start, end - start < 86400)].concat(lines.map(line => [line, line.value(p)]).filter(([, v]) => v !== null).map(([line, v]) => `${line.label}: ${unit(v)}`));
    return { x: X(p.t), rows };
  };
  const show = (event) => {
    const { x: px, rows } = tipAt(event.offsetX || 0, svg.clientWidth);
    cursor.setAttribute("x1", px.toFixed(1)); cursor.setAttribute("x2", px.toFixed(1)); cursor.setAttribute("visibility", "visible");
    while (tip.firstChild) tip.removeChild(tip.firstChild);
    const width = Math.max(...rows.map(r => r.length)) * 6.2 + 10, height = rows.length * 13 + 6;
    const tx = px + width + 8 > W ? px - width - 6 : px + 6;
    tip.append(svgEl("rect", { x: tx.toFixed(1), y: area.y + 2, width: width.toFixed(1), height, class: "chart-tip-box" }));
    rows.forEach((row, i) => tip.append(svgEl("text", { x: (tx + 5).toFixed(1), y: area.y + 15 + i * 13, class: "chart-tip-text" }, document.createTextNode(row))));
    tip.setAttribute("visibility", "visible");
    figure.setAttribute("data-tip", rows.join("\n"));
  };
  svg.addEventListener("mousemove", show);
  svg.addEventListener("mouseleave", () => { cursor.setAttribute("visibility", "hidden"); tip.setAttribute("visibility", "hidden"); });
  return figure;
}

// a bar of what is used now of its limit: LEVEL ok, warn (the warning's share), bad (where the kernel steps in), muted
function monitorLevel(share, warn, bad, warned) {
  const found = share === null ? "muted" : share >= bad ? "bad" : share >= warn ? "warn" : "ok";
  if (warned === "error") return "bad";
  return warned && (found === "ok" || found === "muted") ? "warn" : found;
}

function gaugeBar(label, share, text, level) {
  const shown = share === null ? 0 : Math.max(0, Math.min(1, share));
  const svg = svgEl("svg", { viewBox: "0 0 200 10", preserveAspectRatio: "none", class: "gauge-bar", "aria-hidden": "true" },
    svgEl("rect", { x: 0.5, y: 0.5, width: 199, height: 9, rx: 2, class: "gauge-edge" }),
    svgEl("rect", { x: 1, y: 1, width: (198 * shown).toFixed(1), height: 8, rx: 2, class: "gauge-fill" }));
  return el("div", { class: ["gauge", `level-${level}`].join(" "), role: "meter", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(shown * 100), "aria-label": `${label}: ${text}`, title: `${label}: ${text}` },
    el("span", { class: "gauge-text" }, `${label}: ${text}`), svg);
}

function monitorGauges(row, hostData) {
  if (!row) {
    const s = (hostData && hostData.sample) || {}, kinds = new Set(((hostData && hostData.warnings) || []).map(w => w.kind));
    const busy = monitorNumber(s.cpu_pct), total = monitorNumber(s.mem_total), free = monitorNumber(s.mem_avail);
    const swap = monitorNumber(s.swap_used), swapTotal = monitorNumber(s.swap_total);
    const used = total && free !== null ? 1 - free / total : null;
    return el("div", { class: "gauges" },
      gaugeBar(t("CPU"), busy === null ? null : busy / 100, busy === null ? "-" : t("{percent} % busy of {cpus}", { percent: Math.round(busy), cpus: s.cpus || "?" }), monitorLevel(busy === null ? null : busy / 100, 0.9, 0.98)),
      gaugeBar(t("Memory"), used, used === null ? "-" : t("{used} of {total} GiB used, {free} GiB free", { used: gibText(total - free), total: gibText(total), free: gibText(free) }), monitorLevel(used, 0.85, 1 - MONITOR_HOST_LOW, kinds.has("host-memory") ? "warning" : null)),
      swapTotal ? gaugeBar(t("Swap"), (swap || 0) / swapTotal, t("{used} of {total} GiB", { used: gibText(swap), total: gibText(swapTotal) }), monitorLevel((swap || 0) / swapTotal, 0.5, 0.9, kinds.has("host-swap") ? "warning" : null))
        : gaugeBar(t("Swap"), null, t("none"), "muted"));
  }
  const s = row.sample || {}, warned = {};
  for (const w of row.warnings || []) { const kind = w.kind === "oom" ? "memory" : w.kind; warned[kind] = w.level === "error" ? "error" : warned[kind] || "warning"; }
  const cpu = monitorNumber(s.cpu), cpuLim = monitorNumber(s.cpu_lim), mem = monitorNumber(s.mem), memLim = monitorNumber(s.mem_lim);
  const pids = monitorNumber(s.pids), pidsLim = monitorNumber(s.pids_lim), thr = monitorNumber(s.thr) || 0;
  const percent = (share) => Math.round(share * 100);
  return el("div", { class: "gauges" },
    cpu === null ? gaugeBar(t("CPU"), null, "-", "muted") : cpuLim
      ? gaugeBar(t("CPU"), cpu / cpuLim, t("{used} of {limit} CPUs ({percent} %)", { used: cpu.toFixed(2), limit: cpuLim, percent: percent(cpu / cpuLim) }), thr >= MONITOR_THROTTLE ? "bad" : monitorLevel(cpu / cpuLim, 0.9, 2, warned.cpu))
      : gaugeBar(t("CPU"), (monitorNumber(s.cpu_pct) || 0) / 100, t("{used} CPUs, no limit", { used: cpu.toFixed(2) }), monitorLevel(null, 1, 1, warned.cpu)),
    mem === null ? gaugeBar(t("Memory"), null, "-", "muted") : memLim
      ? gaugeBar(t("Memory"), mem / memLim, t("{used} of {limit} GiB ({percent} %)", { used: gibText(mem), limit: gibText(memLim), percent: percent(mem / memLim) }), monitorLevel(mem / memLim, MONITOR_NEAR, MONITOR_SLOW, warned.memory))
      : gaugeBar(t("Memory"), null, t("{used} GiB, no limit", { used: gibText(mem) }), monitorLevel(null, 1, 1, warned.memory)),
    pids === null ? gaugeBar(t("Processes"), null, "-", "muted") : pidsLim
      ? gaugeBar(t("Processes"), pids / pidsLim, t("{current} of {limit} ({percent} %)", { current: pids, limit: pidsLim, percent: percent(pids / pidsLim) }), monitorLevel(pids / pidsLim, MONITOR_NEAR, 0.98, warned.pids))
      : gaugeBar(t("Processes"), null, t("{current}, no limit", { current: pids }), monitorLevel(null, 1, 1, warned.pids)));
}

// the charts of a run (or the host: HOST true) over SPAN, from POINTS
function monitorCharts(name, points, span, windowLabel, host) {
  const title = (what) => t("{name}: {what}, the last {window}", { name, what, window: windowLabel });
  const has = (...keys) => points.some(p => keys.some(k => k in p));
  const gibUnit = (v) => `${gibText(v)} GiB`, count = (v) => String(Math.round(v)), cpus = (v) => t("{value} CPUs", { value: v.toFixed(2) });
  const ioNet = (key, what, split, summed, words, both) => {
    const lines = has(...split) || !has(summed) ? [chartLine(words[0], split[0], "series0"), chartLine(words[1], split[1], "series2")] : [];
    if (has(summed)) lines.push(chartLine(both, summed, "series4"));
    return chartSvg(key, title(what), points, lines, rateText, span, host ? t("Not recorded there: an older sandbox-ai there does not record this of the host (sandbox-ai hosts update).") : t("No samples in this window yet."));
  };
  const empty = t("No samples in this window yet.");
  const used = (p) => (monitorNumber(p.mem_total) !== null && monitorNumber(p.mem_avail) !== null ? p.mem_total - p.mem_avail : null);
  return [
    host ? chartSvg("cpu", title(t("CPU busy")), points, [chartLine(t("CPU busy"), "cpu_pct", "accent", "area"), chartLine(t("all CPUs"), () => 100, "bad", "limit")], (v) => `${Math.round(v)} %`, span, empty)
      : chartSvg("cpu", title(t("CPUs")), points, [chartLine(t("CPU"), "cpu", "accent", "area"), chartLine(t("CPU limit"), "cpu_lim", "bad", "limit")], cpus, span, empty),
    host ? chartSvg("memory", title(t("memory")), points, has("mem_total") ? [chartLine(t("used"), used, "series1", "area"),
      chartLine(t("low (warning)"), (p) => (monitorNumber(p.mem_total) !== null ? p.mem_total * (1 - MONITOR_HOST_LOW) : null), "warn", "limit"),
      chartLine(t("total"), "mem_total", "bad", "limit"), chartLine(t("swap used"), "swap_used", "series4")] : [chartLine(t("Free memory"), "mem_avail", "series1", "area")], gibUnit, span, empty)
      : chartSvg("memory", title(t("memory")), points, [chartLine(t("Memory"), "mem", "series1", "area"),
        chartLine(t("slowed down from (92 %)"), (p) => (monitorNumber(p.mem_lim) ? p.mem_lim * MONITOR_SLOW : null), "warn", "limit"),
        chartLine(t("limit (processes ended)"), "mem_lim", "bad", "limit")], gibUnit, span, empty),
    ioNet("io", t("disk IO"), ["rd", "wr"], "io", [t("read"), t("written")], t("read + written")),
    ioNet("network", t("network"), ["rx", "tx"], "net", [t("received"), t("sent")], t("received + sent")),
    chartSvg("processes", title(t("processes and threads")), points, host ? [chartLine(t("processes and threads"), "pids", "series3", "area")]
      : [chartLine(t("processes and threads"), "pids", "series3", "area"), chartLine(t("warning (90 %)"), (p) => (monitorNumber(p.pids_lim) ? p.pids_lim * MONITOR_NEAR : null), "warn", "limit"),
        chartLine(t("limit"), "pids_lim", "bad", "limit")], count, span, host ? t("Not recorded there: an older sandbox-ai there does not record this of the host (sandbox-ai hosts update).") : empty),
  ];
}

// every run's CPUs and memory, stacked (the MONITOR_STACKED heaviest on their own)
function monitorAllCharts(series, names, hostSample, span, step, windowLabel) {
  return [["cpu", t("Every run's CPUs"), (v) => t("{value} CPUs", { value: v.toFixed(2) }), t("the host's CPUs"), monitorNumber(hostSample.cpus)],
    ["mem", t("Every run's memory"), (v) => `${gibText(v)} GiB`, t("the host's memory"), monitorNumber(hostSample.mem_total)]].map(([key, what, unit, limitLabel, limit]) => {
    const points = monitorJoined(series, key, step);
    const latest = (run) => { for (let i = points.length - 1; i >= 0; i--) if (run in points[i]) return points[i][run]; return 0; };
    const order = Object.keys(series).sort((a, b) => latest(b) - latest(a));
    const own = order.slice(0, MONITOR_STACKED), rest = order.slice(MONITOR_STACKED);
    if (rest.length) for (const p of points) p.__others = rest.reduce((sum, run) => sum + (p[run] || 0), 0);
    const lines = own.map((run, i) => chartLine(names[run] || run, run, `series${i}`, "stack"));
    if (rest.length) lines.push(chartLine(t("{count} others", { count: rest.length }), "__others", "muted", "stack"));
    if (limit) lines.push(chartLine(limitLabel, () => limit, "bad", "limit"));
    return chartSvg(`all-${key}`, t("{what}, the last {window}", { what, window: windowLabel }), points, lines, unit, span, t("No sandbox is running."));
  });
}

// a sparkline in a cell (CPU, memory) of the spark's COLUMN, scaled to LIMIT (else its highest); no text of its own
function cellSpark(points, column, limit, label) {
  const rows = (Array.isArray(points) ? points : []).filter(p => Array.isArray(p) && p.length > column);
  if (rows.length < 2) return null;
  const width = 44, height = 16;
  const roof = Math.max(monitorNumber(limit) || 0, ...rows.map(r => monitorNumber(r[column]) || 0)) || 1;
  const xy = rows.map((r, i) => `${(i * (width - 2) / (rows.length - 1) + 1).toFixed(1)},${(height - 1 - Math.min(monitorNumber(r[column]) || 0, roof) / roof * (height - 2)).toFixed(1)}`).join(" ");
  return svgEl("svg", { width, height, viewBox: `0 0 ${width} ${height}`, class: `cell-spark ${column === 2 ? "memory" : "cpu"}`, role: "img", "aria-label": label },
    column === 2 ? svgEl("polygon", { points: `1,${height - 1} ${xy} ${width - 1},${height - 1}` }) : svgEl("polyline", { points: xy }));
}

// What a run's model requests cost so far and its budgets (GET .../spend/RUN: sandbox-ai spend run), each budget as a
// bar; Raise budget lets it spend more (asked first, in place: sandbox-ai spend raise).
function spendCost(entry) {
  if (!entry) return "-";
  const parts = [];
  if (entry.usd || (entry.requests || 0) > (entry.subscription || 0) + (entry.local || 0) + (entry.unknown || 0)) parts.push(`USD ${Number(entry.usd || 0).toFixed(2)}`);
  if (entry.unknown) parts.push(t("{count} of unknown cost", { count: entry.unknown }));
  if (entry.subscription) parts.push(t("subscription"));
  if (!parts.length && entry.local) parts.push(t("0 (local)"));
  return parts.join(" + ") || "USD 0.00";
}

function spendPanel(host, run, name, refresh) {
  const box = el("div", { class: "spend-panel" }, el("p", { class: "hint" }, t("Loading…")));
  (async () => {
    let s;
    try { s = await api("GET", hostPath(`/spend/${encodeURIComponent(run)}`, host)); }
    catch (e) { clear(box).append(el("p", { class: "hint" }, t("No model request of {name} counted yet (the connection proxy counts them).", { name }))); return; }
    const rows = (s.budgets || []).map(b => {
      const percent = Math.round((b.share || 0) * 100);
      return el("div", { class: "budget" }, el("progress", { max: 100, value: Math.min(100, percent), "aria-label": t("{label}: {percent} % used", { label: b.label, percent }) }),
        " ", t("{percent} % of USD {usd} - {label}", { percent, usd: Number(b.usd).toFixed(2), label: b.label }));
    });
    const actions = el("div", { class: "row" });
    const amount = el("input", { type: "number", min: "0.01", step: "0.01", value: "5", id: "spend-raise-usd", "aria-label": t("USD more") });
    const raise = el("button", { onclick: () => {
      const usd = amount.value;
      if (!(Number(usd) > 0)) { toast("error", t("Raise budget"), t("Give a number of USD above 0.")); return; }
      confirmIn(actions, t("Let {name} on {host} spend USD {usd} more than its budgets? Its refused model requests go again.", { name, host, usd }), t("Yes, raise it"), async () => {
        try { const r = await api("POST", hostPath(`/spend/${encodeURIComponent(run)}/raise`, host), { usd, confirm: true }); toast("success", t("Budget of {name} raised", { name }), (r && r.output) || ""); refresh(); }
        catch (e) { failed(t("Could not raise the budget of {name}", { name }))(e); }
      }, { trigger: raise });
    } }, t("Raise budget…"));
    actions.append(el("label", { for: "spend-raise-usd" }, t("USD more"), " "), amount, raise);
    clear(box).append(...[el("h3", {}, t("Model spend of {name}", { name })),
      el("p", {}, t("{cost} so far - {count} model requests", { cost: spendCost(s), count: s.requests || 0 })),
      ...(rows.length ? rows : [el("p", { class: "hint" }, t("No budget: --budget USD at the start, or sandbox-ai spend limit set."))]),
      s.refused ? notice(t("A budget is used up: its model requests are refused until it is raised."), "error") : null,
      (s.unknown_models || []).length ? el("p", { class: "hint" }, t("No price for {models}: counted in tokens only.", { models: s.unknown_models.join(", ") })) : null,
      // Under a budget a model without a price is counted at the table's highest prices (sandbox-ai spend run).
      s.fallback && s.fallback_price ? el("p", { class: "hint" }, t("{count} request(s) at the fallback price USD {price_in} in / USD {price_out} out per million tokens: no price for {models}.", { count: s.fallback, price_in: Number(s.fallback_price.in || 0).toFixed(2), price_out: Number(s.fallback_price.out || 0).toFixed(2), models: (s.fallback_models || []).join(", ") })) : null,
      actions].filter(Boolean));
  })();
  return box;
}

// A run's way on (as the desktop app's right click on it: Go to Terminals, Go to Sessions): Terminal attaches it in a
// tab of this page (one per start: an open one is shown) or says why it has no terminal; Session shows the starts
// page of its host with its row marked. An MCP server's sandbox has neither (no terminal, listed below its start).
function monitorGoto(host, row) {
  if (row.kind === "mcp") return el("td", {});
  const name = row.name || row.run;
  return el("td", { class: "actions" },
    rowButton(t("Terminal"), name, host, { title: t("its terminal tab: attached when none is open"), onclick: () => monitorTerminal(host, row) }),
    rowButton(t("Session"), name, host, { title: t("its row on the starts page, marked"), onclick: () => monitorSession(host, row) }));
}

// The start of a host's list (GET .../starts) that the monitor's ROW is: by its run, else by its name.
function monitorStart(starts, row) {
  const list = (Array.isArray(starts) ? starts : []).filter(s => s && typeof s === "object");
  return list.find(s => row.run && s.run === row.run) || list.find(s => row.name && (s.name === row.name || s.id === row.name)) || null;
}

async function monitorTerminal(host, row) {
  const name = row.name || row.run;
  let starts;
  try { starts = await api("GET", hostPath("/starts", host)); }
  catch (e) { failed(t("Could not list the starts of {host}", { host }))(e); return; }
  const s = monitorStart(starts, row);
  if (!s) { toast("warning", t("Terminal"), t("{name} is not listed among the starts of {host}: it has ended.", { name, host })); return; }
  if (!s.attachable) { toast("warning", t("Terminal"), t("{name} on {host} has no terminal to attach to: {why}", { name: s.name, host, why: attachWhy(s) })); return; }
  openTerminal({ kind: "attach", name: s.name, host }, s.name);
}

function monitorSession(host, row) {
  if (state.startsHost !== "all") state.startsHost = host;  // (every host shown there: it is among them)
  state.startsFocus = { host, run: row.run || "", name: row.name || row.run || "" };
  show("starts");
}

function monitorWarningRow(main, host, row, w, refresh) {
  const name = row ? row.name : host;
  const actions = el("td", { class: "actions" });
  const argv = Array.isArray(w.argv) ? w.argv : [];
  if (row && argv[0] === "limits" && ["--memory", "--cpus", "--pids"].includes(argv[2]) && argv[3]) {
    const key = argv[2].slice(2), value = String(argv[3]);
    const raise = el("button", { onclick: () => confirmIn(actions, t("Raise {option} of {name} on {host} to {value} for the rest of its run? It may then use more of that host.", { option: argv[2], name, host, value }), t("Yes, raise it"), async () => {
      try { const r = await api("POST", hostPath(`/limits/${encodeURIComponent(row.run)}`, host), { [key]: value, confirm: true }); toast("success", t("Limits of {where}", { where: name }), (r && r.output) || ""); refresh(); }
      catch (e) { failed(t("Could not change the limits of {where}", { where: name }))(e); }
    }, { trigger: raise }) }, t("Raise to {value}", { value }));
    actions.append(raise);
  }
  if (row && argv[0] === "stop") {  // (an idle MCP server or helper offers none: stopping it breaks its users)
    const stop = el("button", { onclick: () => confirmIn(actions, t("Stop {name} on {host}? What runs in it ends.", { name, host }), t("Yes, stop it"), async () => {
      try { const r = await api("POST", hostPath(`/starts/${encodeURIComponent(row.name)}/stop`, host), { confirm: true }); toast("success", t("{name} on {host} stopped", { name, host }), (r && r.output) || ""); refresh(); }
      catch (e) { failed(t("Could not stop {name} on {host}", { name, host }))(e); }
    }, { trigger: stop }) }, t("Stop"));
    actions.append(stop);
  }
  const since = monitorNumber(w.since);
  return el("tr", { class: `monitor-warning ${w.level === "error" ? "error" : "warning"}` },
    el("td", {}, since ? new Date(since * 1000).toLocaleTimeString() : ""), el("td", { class: "mono" }, name || ""),
    el("td", { class: w.level === "error" ? "status-bad" : "status-warn" }, monitorTitle(w.kind)),
    el("td", {}, w.text || ""), el("td", { class: "mono" }, w.advice || ""), actions);
}

pages.monitor = async (main) => {
  const host = state.host;
  const live = pageLive();
  main.append(el("h1", {}, t("System monitor on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  state.monitorWindow = state.monitorWindow || "24h";
  const hostLine = el("p", { class: "monitor-host" }), runsBox = el("div"), warningsBox = el("div"), historyBox = el("div");
  const windowSelect = el("select", { id: "monitor-window", "aria-label": t("History") },
    MONITOR_WINDOWS.map(([key, label]) => el("option", { value: key, selected: key === state.monitorWindow }, t(label))));
  let data = null, history = null;
  // every look at top per run and for the host (MONITOR_RING each; a run no longer listed is let go): the charts'
  // points after the history's
  const ring = new Map(), historyPoints = new Map();
  const record = (found) => {
    const now = Date.now() / 1000, listed = new Set(["host"]);
    const add = (key, sample) => {
      if (!sample || typeof sample !== "object") return;
      const list = ring.get(key) || [];
      const point = { t: monitorNumber(sample.t) !== null ? sample.t : now };
      for (const [k, v] of Object.entries(sample)) if (k !== "t" && monitorNumber(v) !== null) point[k] = v;
      if (list.length && list[list.length - 1].t >= point.t) return;
      list.push(point);
      if (list.length > MONITOR_RING) list.splice(0, list.length - MONITOR_RING);
      ring.set(key, list);
    };
    for (const row of (found && found.runs) || []) {
      if (!row || typeof row.run !== "string") continue;
      listed.add(row.run);
      if (!ring.has(row.run)) for (const p of monitorPointsOfSeries(row.spark, ["cpu", "mem", "io", "net"])) add(row.run, { t: p.t, cpu: p.cpu, mem: p.mem });
      add(row.run, row.sample);
    }
    add("host", found && found.host && found.host.sample);
    for (const key of [...ring.keys()]) if (!listed.has(key)) ring.delete(key);
  };
  const pointsOf = (run, since) => {
    if (!historyPoints.has(run)) {
      const rows = (history && Array.isArray(history.runs)) ? history.runs : [];
      const hostRow = (history && history.host) || {};
      const row = run === "host" ? hostRow : rows.find(r => r.run === run) || {};
      const found = monitorPointsOfDetail(row.detail);
      historyPoints.set(run, found.length ? found : monitorPointsOfSeries(row.series, run === "host" ? ["cpu_pct", "mem_avail", "io", "load1"] : ["cpu", "mem", "io", "net"]));
    }
    return monitorMerged(historyPoints.get(run), ring.get(run) || [], since);
  };
  // the gauges and charts: drawn anew at every look at top (the rest of the history only when it is read again)
  const chartsHolder = el("div", { class: "monitor-now" });
  const drawCharts = () => {
    const runs = (history && Array.isArray(history.runs)) ? history.runs : [];
    const chosen = state.monitorRun && runs.find(r => r.run === state.monitorRun);
    const running = state.monitorRun && data && (data.runs || []).find(r => r.run === state.monitorRun);
    const windowLabel = t((MONITOR_WINDOWS.find(([key]) => key === state.monitorWindow) || MONITOR_WINDOWS[2])[1]);
    const seconds = MONITOR_SECONDS[state.monitorWindow] || 86400, now = Date.now() / 1000, span = [now - seconds, now];
    const step = Math.max(10, seconds / MONITOR_SLOTS);
    const listed = ((data && data.runs) || []).filter(r => r && typeof r.run === "string");
    const name = running ? running.name || running.run : chosen ? chosen.name || chosen.run : host;
    clear(chartsHolder).append(
      el("p", { class: "hint" }, t("The bars: what the run chosen (nothing chosen: the host) uses now of its limits, in the colours of the warnings. The charts: the window chosen, from the history and every look since; point at one for the exact values at that time.")),
      monitorGauges(running || null, data && data.host),
      el("div", { class: "monitor-charts" },
        ...monitorCharts(name, pointsOf(running || chosen ? state.monitorRun : "host", span[0]), span, windowLabel, !(running || chosen)),
        ...monitorAllCharts(Object.fromEntries(listed.map(r => [r.run, pointsOf(r.run, span[0])])), Object.fromEntries(listed.map(r => [r.run, r.name || r.run])), (data && data.host && data.host.sample) || {}, span, step, windowLabel)));
  };
  const drawHistory = () => {
    const runs = (history && Array.isArray(history.runs)) ? history.runs : [];
    const chosen = state.monitorRun && runs.find(r => r.run === state.monitorRun);
    const windowLabel = t((MONITOR_WINDOWS.find(([key]) => key === state.monitorWindow) || MONITOR_WINDOWS[2])[1]);
    const caption = chosen ? t("{name}: CPUs and memory, the last {window}", { name: chosen.name || chosen.run, window: windowLabel })
      : t("{host}: CPU busy and free memory, the last {window}", { host, window: windowLabel });
    const points = chosen ? chosen.series : (history && history.host && history.host.series) || [];
    const running = state.monitorRun && data && (data.runs || []).find(r => r.run === state.monitorRun);
    const spent = history && history.spend;
    drawCharts();
    clear(historyBox).append(...[el("p", {}, caption),
      points && points.length > 1 ? trendSvg(points, 640, 140, caption) : el("p", { class: "hint" }, t("No history recorded in this window yet.")),
      chartsHolder,
      running ? spendPanel(host, running.run, running.name || running.run, refresh)
        : spent && spent.total && spent.total.requests ? el("p", {}, t("Model requests of this window: {cost} ({count} requests; sandbox-ai spend)", { cost: spendCost(spent.total), count: spent.total.requests })) : null].filter(Boolean));
  };
  let historyAsked = 0, historyAt = 0;  // only the window chosen last is drawn (an earlier, slower answer is dropped)
  const loadHistory = async () => {
    const ticket = ++historyAsked;
    historyAt = Date.now();
    let found = null;
    try { found = await api("GET", hostPath(`/monitor/usage/${state.monitorWindow}`, host)); } catch (e) { found = null; }
    if (ticket !== historyAsked) return;
    history = found;
    historyPoints.clear();
    if (live()) { draw(); drawHistory(); }
  };
  const draw = () => {
    const s = (data && data.host && data.host.sample) || {};
    const parts = [];
    if (monitorNumber(s.cpu_pct) !== null) parts.push(t("CPU {percent} % of {cpus}", { percent: s.cpu_pct, cpus: s.cpus || "?" }));
    if (monitorNumber(s.load1) !== null) parts.push(t("load {load}", { load: s.load1 }));
    if (monitorNumber(s.mem_total)) parts.push(t("memory free {free} of {total} GiB", { free: gibText(s.mem_avail), total: gibText(s.mem_total) }));
    if (monitorNumber(s.swap_total)) parts.push(t("swap {used} of {total} GiB", { used: gibText(s.swap_used), total: gibText(s.swap_total) }));
    if (monitorNumber(s.rd) !== null) parts.push(t("disk {read} read, {written} written", { read: rateText(s.rd), written: rateText(s.wr) }));
    hostLine.textContent = parts.join(" · ");
    const runs = (data && Array.isArray(data.runs)) ? data.runs : [];
    const groups = new Map();
    for (const row of runs) { const key = row.project || ""; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(row); }
    const rows = [];
    for (const [project, members] of groups) {
      rows.push(el("tr", { class: "monitor-project" }, el("th", { colspan: 10, scope: "rowgroup", title: project }, project ? project.split("/").filter(Boolean).pop() : t("(no project)"))));
      for (const row of members) {
        const m = row.sample || {};
        const cpu = monitorNumber(m.cpu) !== null ? `${m.cpu.toFixed(2)} (${Math.round(m.cpu_pct || 0)} %)` : "-";
        const memory = monitorNumber(m.mem) === null ? "-" : monitorNumber(m.mem_lim) ? `${gibText(m.mem)} / ${gibText(m.mem_lim)} GiB` : `${gibText(m.mem)} GiB`;
        const choose = el("button", { class: "link", "aria-pressed": String(state.monitorRun === row.run), onclick: () => { state.monitorRun = state.monitorRun === row.run ? null : row.run; draw(); drawHistory(); } }, `${row.parent ? "↳ " : ""}${row.name || row.run}`);
        rows.push(el("tr", { class: state.monitorRun === row.run ? "selected" : "" },
          el("td", {}, choose), el("td", {}, row.agent || ""),
          el("td", { class: "num" }, cpu, cellSpark(row.spark, 1, m.cpu_lim, t("CPU trend of {name}", { name: row.name || row.run }))),
          el("td", { class: "num" }, memory, cellSpark(row.spark, 2, m.mem_lim, t("Memory trend of {name}", { name: row.name || row.run }))),
          el("td", { class: "num" }, monitorNumber(m.rd) !== null ? `${rateText(m.rd)} / ${rateText(m.wr)}` : "-"),
          el("td", { class: "num" }, monitorNumber(m.rx) !== null ? `${rateText(m.rx)} / ${rateText(m.tx)}` : "-"),
          el("td", { class: "num" }, monitorNumber(m.pids) !== null ? String(m.pids) : "-"),
          el("td", {}, trendSvg(row.spark, 120, 26, t("Trend of {name}", { name: row.name || row.run }))),
          el("td", { class: (row.warnings || []).some(w => w.level === "error") ? "status-bad" : "status-warn" }, (row.warnings || []).map(w => monitorTitle(w.kind)).join(", ")),
          el("td", { class: "num" }, spendCost(history && history.spend && history.spend.runs && history.spend.runs[row.run])),
          monitorGoto(host, row)));
      }
    }
    clear(runsBox).append(table([t("Run"), t("Agent"), t("CPU"), t("Memory"), t("Disk IO"), t("Network"), t("Processes"), t("Trend"), t("Warnings"), t("Cost"), ""], rows, t("No sandbox is running on this host.")));
    const warningRows = [];
    for (const row of runs) for (const w of row.warnings || []) warningRows.push(monitorWarningRow(main, host, row, w, refresh));
    for (const w of (data && data.host && data.host.warnings) || []) warningRows.push(monitorWarningRow(main, host, null, w, refresh));
    clear(warningsBox).append(el("h2", {}, t("Warnings")), table([t("Since"), t("Run"), t("Warning"), t("What"), t("What to do"), ""], warningRows, t("No warning on this host.")));
  };
  const refresh = async () => {
    try { data = await api("GET", hostPath("/monitor", host)); record(data); }
    catch (e) {
      if (!live()) return;
      clear(runsBox).append(e.status === 502 ? notice(t("{error} (an older sandbox-ai there has no system monitor: sandbox-ai update)", { error: e.message }), "error") : errorNotice(e));
      return;
    }
    if (live()) { draw(); if (history) drawCharts(); }
  };
  windowSelect.addEventListener("change", () => { state.monitorWindow = windowSelect.value; loadHistory(); });
  await refresh();
  if (!live()) return;
  clear(main).append(el("h1", {}, t("System monitor on {host}", { host })),
    el("p", { class: "hint" }, t("What the running sandboxes use, recorded every {seconds} seconds by each run's launcher: CPU (of its limit), memory of its limit, disk IO, network and processes, grouped by project; an MCP server or helper under the start it serves. Choose a run for its history.", { seconds: monitorSeconds(data) })),
    hostLine, runsBox, el("h2", {}, t("History")), el("div", { class: "row" }, el("label", { for: "monitor-window" }, t("Time span"), " "), windowSelect), historyBox, warningsBox);
  loadHistory();
  const again = () => setTimeout(async () => {
    if (!live() || state.page !== "monitor" || document.hidden) { if (live() && state.page === "monitor") again(); return; }
    await refresh();
    if (live() && Date.now() - historyAt >= 6 * MONITOR_EVERY) loadHistory();  // (the cost of the runs with it)
    if (live()) again();
  }, MONITOR_EVERY);
  again();
};

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

// what the safeguards refused or ended (sandbox-ai safeguards on that host): why, the process, the command that
// would allow it or that it stays refused; newest first, warnings in the theme's warning colour, errors in its bad one
pages.safeguards = async (main, known) => {
  main.append(el("h1", {}, t("Safeguards on {host}", { host: state.host })), el("p", { class: "hint" }, t("Loading…")));
  let records = known;
  if (!records) {
    try { records = await api("GET", hostPath("/safeguards")); }
    catch (e) { clear(main).append(el("h1", {}, t("Safeguards on {host}", { host: state.host })), notice(e.status === 502 ? t("{error} (an older sandbox-ai there records no safeguards: sandbox-ai update)", { error: e.message }) : e.message, "error")); return; }
  }
  clear(main).append(el("h1", {}, t("Safeguards on {host}", { host: state.host })),
    el("p", { class: "hint" }, t("Whatever a sandbox's safeguards refused or ended: the firewall, name lookups, the proxy, rules, questions nobody answered, the privacy check of pushes, the security mode, the administrator's policy, the run's taint, devices, memory, canaries. Each with why and what would allow it. Mute kinds on the Defaults page; they are still listed here.")));
  const kinds = [...new Set(records.map(r => r.kind))].sort();
  const filter = el("select", { id: "safeguard-kind", "aria-label": t("Kind") }, el("option", { value: "" }, t("every kind")),
    kinds.map(k => el("option", { value: k, selected: state.safeguardKind === k }, `${safeguardTitle(k)} (${k})`)));
  const body = el("div");
  const fill = () => {
    state.safeguardKind = filter.value;
    const shown = records.filter(r => !filter.value || r.kind === filter.value).slice().reverse();
    const rows = shown.map(r => el("tr", { class: `safeguard ${r.level === "error" ? "error" : "warning"}` },
      el("td", {}, (r.time || "").replace("T", " ").replace("Z", "")), el("td", { class: "mono" }, r.run || ""),
      el("td", { class: r.level === "error" ? "status-bad" : "status-warn" }, safeguardTitle(r.kind)),
      el("td", {}, `${r.what || ""}${r.count > 1 ? ` ×${r.count}` : ""}`, r.process ? el("div", { class: "hint mono" }, r.process) : null),
      el("td", {}, r.why || ""),
      el("td", { class: "mono" }, r.stays ? t("stays refused: {fix}", { fix: r.fix || "" }) : (r.fix || "")),
      el("td", { class: "actions" }, r.fix && !r.stays && navigator.clipboard ? el("button", { onclick: async () => {
        try { await navigator.clipboard.writeText(r.fix); toast("success", t("Copied"), r.fix); } catch (e) { failed(t("Could not copy"))(e); }
      } }, t("Copy")) : null)));
    clear(body).append(table([t("Time"), t("Run"), t("Kind"), t("What"), t("Why"), t("To allow it"), ""], rows, t("No safeguard refused or ended anything on this host.")));
  };
  filter.addEventListener("change", fill);
  main.append(el("div", { class: "row" }, el("label", { for: "safeguard-kind" }, t("Kind"), " "), filter), body);
  fill();
};
// The workspace review (sandbox-ai workspace review --all on that host): what each run changed in files the host runs
// or trusts, newest first. Diff shows the change and the hidden-text findings; Keep records it as wanted; Restore…,
// asked in place, puts the file back as it was at the start from the before-state kept outside every sandbox (an
// added file is removed). A change without one (binary, larger, a run before 0.1.133) offers Keep only, and says why.
const REVIEW_CHANGES = { added: N_("added"), changed: N_("changed"), removed: N_("removed") };
const REVIEW_DECISIONS = { kept: N_("decision|kept"), restored: N_("restored") };
const PIN_CHANGES = { new: N_("new"), changed: N_("changed"), gone: N_("gone") };
pages.reviews = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Workspace review on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  let reviews;
  try { reviews = await api("GET", hostPath("/workspace-review", host)); } catch (e) { reviews = e; }
  if (!live()) return;
  clear(main).append(el("h1", {}, t("Workspace review on {host}", { host })),
    el("p", { class: "hint" }, t("What each run changed in files your host runs or trusts: Git hooks and configuration, editor tasks, agent settings, build scripts, CI workflows, instruction files. Nothing is reverted on its own: keep each change, or restore the file as it was at the start.")));
  if (reviews instanceof Error) { main.append(errorNotice(reviews)); return; }
  const detail = el("div", { id: "review-detail" });
  const rows = [];
  for (const review of (Array.isArray(reviews) ? reviews : []).slice().reverse()) {
    for (const c of Array.isArray(review.changes) ? review.changes : []) {
      const file = `${String(c.sandbox || "").replace(/\/$/, "")}/${c.path}`;
      const decided = c.decision && REVIEW_DECISIONS[c.decision.decision] ? `${t(REVIEW_DECISIONS[c.decision.decision])} ${String(c.decision.time || "").replace("T", " ").replace("Z", "")}` : null;
      const actions = el("td", { class: "actions" });
      const target = { log_dir: review.log_dir, run: review.run, path: file };
      const showDiff = () => clear(detail).append(el("div", { class: "card" }, el("h2", {}, file),
        el("p", { class: "hint" }, `${t("Run")}: ${review.run} · ${c.why || ""}`),
        c.restorable ? null : notice(t("No before-state recorded: {why}. Keep it, or restore the start's checkpoint.", { why: c.why_not || "" }), "warn"),
        el("pre", { class: "mono" }, (Array.isArray(c.diff) ? c.diff : []).join("\n")),
        Array.isArray(c.findings) && c.findings.length ? el("pre", { class: "mono status-warn" }, [t("Hidden-text findings:"), ...c.findings].join("\n")) : null));
      actions.append(rowButton(t("Diff"), file, host, { onclick: showDiff }));
      if (!decided) actions.append(rowButton(t("Keep"), file, host, { title: t("Record that this change is wanted (nothing is written)"), onclick: async () => {
        try { await api("POST", hostPath("/workspace-review/keep", host), target); toast("success", t("{path}: kept as the agent left it", { path: file })); show("reviews"); }
        catch (e) { failed(t("Could not keep {path} on {host}", { path: file, host }))(e); }
      } }));
      if (c.restorable && !(c.decision && c.decision.decision === "restored")) {
        const restore = rowButton(t("Restore…"), file, host, { onclick: () => confirmIn(actions, c.change === "added"
          ? t("{path} was added by the agent: restoring removes it. Go ahead on {host}?", { path: file, host })
          : t("Put {path} back as it was at the start of run {run} on {host}? What the agent wrote there is replaced.", { path: file, run: review.run, host }),
        t("Yes, restore"), async () => {
          try { await api("POST", hostPath("/workspace-review/restore", host), Object.assign({ confirm: true }, target)); toast("success", t("{path}: restored as it was at the start", { path: file })); show("reviews"); }
          catch (e) { failed(t("Could not restore {path} on {host}", { path: file, host }))(e); }
        }, { trigger: restore }) });
        actions.append(restore);
      }
      rows.push(el("tr", {}, el("td", {}, String(review.time || "").replace("T", " ").replace("Z", "")), el("td", { class: "mono" }, review.run || ""),
        el("td", {}, REVIEW_CHANGES[c.change] ? t(REVIEW_CHANGES[c.change]) : String(c.change || "")), el("td", { class: "mono" }, file),
        el("td", {}, c.why || ""), el("td", { class: decided ? "" : "status-warn" }, decided || (c.restorable ? t("open") : t("open (Keep only)"))), actions));
    }
  }
  main.append(table([t("Time"), t("Run"), t("Change"), t("File"), t("Why it matters"), t("Decision"), ""], rows, t("No run changed a file the host runs or trusts.")), detail);
};

// The MCP tool definitions pinned per project (sandbox-ai mcp tools --all on that host), and whether the list each
// server gave last differs. Definitions shows them with what changed; Re-pin… (asked in place) makes the list seen
// last the pin (that host refuses one with hidden text); Forget… drops the pin (the next list is pinned anew).
pages.pins = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("MCP tool pins on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  let pins;
  try { pins = await api("GET", hostPath("/mcp-tools", host)); } catch (e) { pins = e; }
  if (!live()) return;
  clear(main).append(el("h1", {}, t("MCP tool pins on {host}", { host })),
    el("p", { class: "hint" }, t("What each MCP server's tools told the model when the project trusted them: a later change is asked about. Here the definitions, whether the list seen last differs, and re-pinning it.")));
  if (pins instanceof Error) { main.append(errorNotice(pins)); return; }
  const detail = el("div", { id: "pin-detail" });
  const rows = (Array.isArray(pins) ? pins : []).filter(x => x && typeof x.server === "string").map(x => {
    const actions = el("td", { class: "actions" });
    const target = { project: x.project, server: x.server };
    const seen = !x.seen ? t("not seen since") : x.differs ? t("{time}: {count} differ", { time: String(x.seen).replace("T", " ").replace("Z", ""), count: x.differs }) : t("{time}: as pinned", { time: String(x.seen).replace("T", " ").replace("Z", "") });
    if (x.project) {
      actions.append(rowButton(t("Definitions"), x.server, host, { onclick: async () => {
        let pin;
        try { pin = await api("POST", hostPath("/mcp-tools/show", host), target); } catch (e) { clear(detail).append(errorNotice(e)); return; }
        const lines = [];
        for (const change of (pin && Array.isArray(pin.changes) ? pin.changes : [])) lines.push(`${change.tool}: ${PIN_CHANGES[change.change] ? t(PIN_CHANGES[change.change]) : String(change.change)}`);
        const seenTools = pin && pin.seen && pin.seen.tools ? pin.seen.tools : {};
        for (const [name, item] of Object.entries(pin && pin.tools ? pin.tools : {})) {
          lines.push("", `${name}  sha256 ${String(item.digest || "").slice(0, 16)}`, `  ${t("pinned")}: ${item.definition || ""}`);
          if (seenTools[name] && seenTools[name].digest !== item.digest) lines.push(`  ${t("seen last")}: ${seenTools[name].definition || ""}`);
        }
        for (const [name, item] of Object.entries(seenTools)) if (!(pin.tools || {})[name]) lines.push("", `${name} (${t("new")})`, `  ${t("seen last")}: ${item.definition || ""}`);
        clear(detail).append(el("div", { class: "card" }, el("h2", {}, t("{server} in {project}", { server: x.server, project: x.project })), el("pre", { class: "mono" }, lines.join("\n"))));
      } }));
      if (x.differs) {
        const repin = rowButton(t("Re-pin…"), x.server, host, { onclick: () => confirmIn(actions, t("Pin the list MCP server {server} gave last for {project} on {host}? Its later lists are compared with it: what changed in it is no longer asked about.", { server: x.server, project: x.project, host }), t("Yes, re-pin"), async () => {
          try { await api("POST", hostPath("/mcp-tools/repin", host), Object.assign({ confirm: true }, target)); toast("success", t("{server}: the list seen last is pinned now", { server: x.server })); show("pins"); }
          catch (e) { failed(t("Could not re-pin {server} on {host}", { server: x.server, host }))(e); }
        }, { trigger: repin }) });
        actions.append(repin);
      }
      const forget = rowButton(t("Forget…"), x.server, host, { class: "danger", onclick: () => confirmIn(actions, t("Drop the pin of MCP server {server} for {project} on {host}? Its next list is pinned without a question.", { server: x.server, project: x.project, host }), t("Yes, forget it"), async () => {
        try { await api("POST", hostPath("/mcp-tools/forget", host), Object.assign({ confirm: true }, target)); toast("success", t("{server}: pin dropped", { server: x.server })); show("pins"); }
        catch (e) { failed(t("Could not forget the pin of {server} on {host}", { server: x.server, host }))(e); }
      }, { trigger: forget }) });
      actions.append(forget);
    }
    return el("tr", {}, el("td", { class: "mono" }, x.project || t("(not named: pinned before 0.1.133)")), el("td", { class: "mono" }, x.server),
      el("td", {}, String(x.tools ?? "")), el("td", {}, String(x.pinned || "").replace("T", " ").replace("Z", "")), el("td", {}, x.how || ""),
      el("td", { class: x.differs ? "status-warn" : "" }, seen), actions);
  });
  main.append(table([t("Project"), t("Server"), t("Tools"), t("Pinned"), t("How"), t("Last seen"), ""], rows, t("No MCP tool definitions pinned yet (a server's first list is pinned after its run).")), detail);
};

// What lowers the protection of every later start on a host is asked first, in place, naming the host; Cancel puts
// the control back. Lowering is any step down from what the host has now: the mode (lax < low < medium < high <
// paranoid), the agents' telemetry (off < strict; it is always off), fewer canary categories. The service compares the same way
// (GET /api/policy gives the order) and wants the confirmation as well.
const ORDER = { mode: ["lax", "low", "medium", "high", "paranoid"], telemetry: ["off", "strict"] };

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
  // an old "on" (gone since 0.1.95: the launcher ignores it) is shown as off
  let telemetryNow = values.telemetry === "strict" ? "strict" : "off", modeNow = values.mode || "medium";
  const telemetry = el("select", { id: "telemetry" },
    [["off", N_("off: no usage statistics, error reports, feedback or update checks (recommended)")], ["strict", N_("strict: also no other calls than the model's")]]
      .map(([v, label]) => el("option", { value: v, selected: telemetryNow === v }, t(label))));
  const telemetryAsk = el("div", { class: "row" });
  telemetry.addEventListener("change", () => {
    const value = telemetry.value;
    const text = t("Make the agents' telemetry {level} instead of {now} for every later start on {host}? Fewer of their own calls are held back.", { level: value, now: telemetryNow, host });
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
  // which safeguards are announced (desktop notifications, the app's and these toasts); unticked: safeguard_mute
  const safeguardKinds = data.safeguard_kinds || [];
  const muted = values.safeguard_mute || [];
  const announced = safeguardKinds.map(k => el("input", { type: "checkbox", id: `safeguard-${k.kind}`, value: k.kind, checked: !muted.includes(k.kind) }));
  for (const box of announced) box.addEventListener("change", () => {
    const silenced = announced.filter(b => !b.checked).map(b => b.value);
    state.safeguardMuted[host] = new Set(silenced);
    if (state.safeguardsNotifier && host === state.host) state.safeguardsNotifier.muted = state.safeguardMuted[host];
    save("safeguard_mute", silenced.length ? silenced : null);
  });
  // the file views (Memory, Session files): logins and keys listed and marked by default; ticked, left out (hide_secrets)
  const hideSecrets = el("input", { type: "checkbox", id: "hide-secrets", checked: values.hide_secrets === true });
  hideSecrets.addEventListener("change", async () => {
    const on = hideSecrets.checked;
    if (await save("hide_secrets", on ? true : null) !== true) hideSecrets.checked = !on;
  });
  main.append(status, el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "hide-secrets" }, t("Hide logins and keys in file views")),
    el("div", {}, hideSecrets, el("p", { class: "hint" }, t("Off (the default): files that look like logins or keys (~/.git-credentials, an agent's login, a private key) are listed in Memory and Session files and marked; their text is shown only when you ask. On: they are left out of those lists (hide_secrets). An export leaves them out either way unless you take them in."))),
    el("label", { for: "telemetry" }, t("Telemetry of the agents")), telemetry, el("span"), telemetryAsk,
    el("label", { for: "mode" }, t("Security mode")), mode, el("span"), modeAsk,
    el("span", {}, t("Canary files")), el("div", { class: "row" }, boxes.map((b, i) => el("label", {}, b, " ", categories[i]))), el("span"), canaryAsk,
    safeguardKinds.length ? el("span", {}, t("Safeguards announced")) : null,
    safeguardKinds.length ? el("div", { class: "row" }, announced.map((b, i) => el("label", { title: `${safeguardKinds[i].kind} (${safeguardKinds[i].level})` }, b, " ", safeguardKinds[i].title))) : null),
    el("p", { class: "hint mono" }, data.path || ""),
    el("p", { class: "hint" }, t("For every start on this host; an option given at a start wins."))));
  // keep_days, preset_dirs, subnet, subnet6, name_style: saved with Save; what lowers the protection the service asks first
  const more = [["keep_days", N_("Kept starts unused this many days are removed (empty: 90, the default; 0: never)"), "30"], ["preset_dirs", N_("Extra preset directories (one per line)"), "/home/you/presets"],
    ["subnet", N_("Network of the starts (IPv4)"), "10.89.0.0/24"], ["subnet6", N_("Network of the starts (IPv6)"), "fd00:89::/64"],
    ["name_style", N_("Names of new starts: codename or project (empty: codename)"), "project"],
    ["ask_timeout", N_("Seconds a request waits for the answer to its question, 5-3600 (empty: 60); unanswered, the question stays open"), "120"],
    ["package_min_age_days", N_("Days a package version must be old before the package guard lets it through without asking, 0-3650 (empty: 14; 0: no age check)"), "14"]];
  const asking = el("div", { class: "row" });
  const fields = more.map(([key, label, example]) => {
    const now = values[key];
    const input = key === "preset_dirs" ? el("textarea", { id: `default-${key}`, rows: 2, class: "mono", placeholder: example }) : el("input", { id: `default-${key}`, class: "mono", placeholder: example });
    input.value = now === undefined || now === null ? "" : Array.isArray(now) ? now.join("\n") : String(now);
    const valueOf = () => {
      const text = input.value.trim();
      if (!text) return null;
      if (key === "keep_days" || key === "ask_timeout" || key === "package_min_age_days") return Number(text);
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
  // the storage check (sandbox-ai gc --check) and its automatic rules: each saved at once, a rule off when empty
  const storageFields = [
    ["storage_check", N_("Storage check (off, weekly, daily)"), "weekly"], ["storage_threshold_gb", N_("Notice above (GB)"), "5"],
    ["storage_growth_gb", N_("Name a start that grew more than (GB)"), "2"],
    ["storage_caches_days", N_("Empty the caches of starts unused for (days)"), ""], ["storage_compact_days", N_("Compact transcripts older than (days)"), ""],
    ["storage_pack_days", N_("Pack stopped starts unused for (days)"), ""], ["storage_logs_days", N_("Remove log folders not written for (days)"), ""],
  ].map(([key, label, placeholder]) => {
    const input = el("input", { id: `default-${key}`, value: values[key] === undefined || values[key] === null ? "" : String(values[key]), placeholder, size: 8 });
    const saveButton = el("button", { onclick: async () => {
      const text = input.value.trim();
      await save(key, text === "" ? null : (key === "storage_check" ? text : Number(text)));
    } }, t("Save"));
    return [el("label", { for: input.id }, t(label)), el("div", { class: "row" }, input, saveButton)];
  });
  main.append(el("h2", {}, t("Storage")), el("div", { class: "card" }, el("p", { class: "hint" }, t("The storage check (sandbox-ai gc --check) and its automatic rules, each off while empty: applied by the check and gc, each action logged and said in the next notice.")), el("div", { class: "form" }, storageFields)));
  if (live()) await phoneNotifications(main, host, live);
};

// Phone notifications of a host (sandbox-ai notify there): its channels - ntfy, Telegram, WhatsApp, a webhook - with
// their events, when they send and how much a message says; tokens only as the names of that host's secrets (page
// Secrets), never typed here. Add and Remove are asked first (notify-add, notify-remove); Test sends a message now;
// Edit puts a channel into the form (saved under the same name, it replaces it).
const NOTIFY_KINDS = ["ntfy", "telegram", "whatsapp", "webhook"];
const NOTIFY_EVENTS = [["questions", N_("Questions")], ["alarms", N_("Alarms")], ["safeguards", N_("Safeguards")], ["limits", N_("Limits")], ["run-ended", N_("Run ended")], ["run-failed", N_("Run failed")], ["budget", N_("Budget nearly or all used")]];
// The fields of each kind: [field (notify.json's name), label, placeholder]
const NOTIFY_FIELDS = {
  ntfy: [["url", N_("Server"), "https://ntfy.sh"], ["topic", N_("Topic"), "a-long-random-topic"], ["token_secret", N_("Token (a secret's name, optional)"), "NTFY_TOKEN"]],
  telegram: [["bot_token_secret", N_("Bot token (a secret's name)"), "TG_BOT"], ["chat", N_("Chat ID"), "123456789"], ["api", N_("Another API URL (optional)"), "https://api.telegram.org"]],
  whatsapp: [["phone_id", N_("Phone-number ID"), "106540352242922"], ["token_secret", N_("Access token (a secret's name)"), "WA_TOKEN"], ["to", N_("Recipient"), "+4917612345678"], ["template", N_("Approved template (optional)"), "sandbox_alert"], ["language", N_("Its language"), "en"], ["api", N_("Another API URL (optional)"), "https://graph.facebook.com/v23.0"]],
  webhook: [["url", N_("URL"), "https://hooks.example.org/in"], ["url_secret", N_("Or: the URL is the secret"), "HOOK_URL"], ["token_secret", N_("Token (a secret's name, optional)"), "HOOK_TOKEN"]],
};

async function phoneNotifications(main, host, live) {
  const box = el("div", {}, el("p", { class: "hint" }, t("Loading…")));
  main.append(el("h2", { id: "phone-notifications" }, t("Phone notifications")), box);
  let data;
  try { data = await api("GET", hostPath("/notify", host)); } catch (e) { if (live()) clear(box).append(errorNotice(e)); return; }
  if (!live()) return;
  const channels = Array.isArray(data && data.channels) ? data.channels.filter(c => c && typeof c.name === "string") : [];
  const result = el("div", { class: "wide check-result" });
  const form = el("div", { class: "form" });
  const name = el("input", { id: "notify-name", class: "mono", placeholder: t("phone"), autocomplete: "off" });
  const kind = el("select", { id: "notify-kind" }, NOTIFY_KINDS.map(k => el("option", { value: k }, k)));
  kind.value = NOTIFY_KINDS[0];  // (said, not left to the browser: the first kind's fields are drawn at once)
  const own = el("div", { class: "form wide" });
  const inputs = {};
  const drawOwn = (values) => {
    clear(own);
    for (const [field, label, example] of NOTIFY_FIELDS[kind.value] || []) {
      const input = el("input", { id: `notify-${field}`, class: "mono", placeholder: example, autocomplete: "off", spellcheck: "false" });
      input.value = values && values[field] !== undefined && values[field] !== null ? String(values[field]) : "";
      inputs[field] = input;
      own.append(el("label", { for: input.id }, t(label)), input);
    }
  };
  kind.addEventListener("change", () => drawOwn({}));
  const events = NOTIFY_EVENTS.map(([key, label]) => {
    const box = el("input", { type: "checkbox", id: `notify-event-${key}` });
    box.checked = key === "questions" || key === "alarms";
    return [key, box, el("label", { for: box.id }, t(label))];
  });
  const when = el("select", { id: "notify-when" }, el("option", { value: "away" }, t("away: while neither the app nor the web interface watches")), el("option", { value: "always" }, t("always")));
  const detail = el("select", { id: "notify-detail" }, el("option", { value: "minimal" }, t("minimal: only that something waits")), el("option", { value: "normal", selected: true }, t("normal: kind, run, what, time")), el("option", { value: "full" }, t("full: also destinations and paths")));
  detail.value = "normal";
  const quiet = el("input", { id: "notify-quiet", class: "mono", placeholder: "22:00-07:00" });
  const rate = el("input", { id: "notify-rate", class: "mono", type: "number", min: "0", max: "86400", value: "60" });
  const fill = (c) => {  // a channel into the form (Edit): its fields, its events, when, detail, quiet hours, rate
    name.value = c ? c.name : ""; kind.value = c ? c.kind : "ntfy";
    drawOwn(c || {});
    for (const [key, box] of events) box.checked = c ? (c.events || []).includes(key) : key === "questions" || key === "alarms";
    when.value = (c && c.when) || "away"; detail.value = (c && c.detail) || "normal";
    quiet.value = (c && c.quiet) || ""; rate.value = String(c && Number.isInteger(c.rate) ? c.rate : 60);
    name.focus();
  };
  drawOwn({});
  const body = () => {
    const b = { name: name.value.trim(), kind: kind.value, events: events.filter(([, box]) => box.checked).map(([key]) => key), when: when.value, detail: detail.value };
    for (const [field] of NOTIFY_FIELDS[kind.value] || []) { const v = inputs[field].value.trim(); if (v) b[field] = v; }
    if (quiet.value.trim()) b.quiet = quiet.value.trim();
    if (rate.value.trim() !== "") b.rate = Number(rate.value);
    return b;
  };
  const row = el("div", { class: "row" });
  const save = (event) => busy(event.target, async () => {
    clear(result);
    const b = body();
    try { await api("POST", hostPath("/notify", host), Object.assign({}, b, { check_only: true })); }
    catch (e) { result.append(errorNotice(e, t("The channel was not saved"))); return; }
    const send = async () => {
      try { await api("POST", hostPath("/notify", host), Object.assign({}, b, { confirm: true })); toast("success", t("Saved"), t("{name} on {host}: try it with Test.", { name: b.name, host })); show("settings"); }
      catch (e) { result.append(errorNotice(e, t("The channel was not saved"))); }
    };
    if (!needsConfirm("notify-add")) { await send(); return; }
    confirmIn(row, t("Send the questions and alarms of {host} to {name} ({kind})? The service receives what the detail allows: the kind, the run's name, a short what, and with full also destinations and paths.", { host, name: b.name, kind: b.kind }), t("Yes, save it"), send, { kind: "primary", trigger: event.target });
  });
  row.append(el("button", { class: "primary", onclick: save }, t("Save channel…")), el("button", { onclick: () => { fill(null); clear(result); } }, t("New")));
  // each channel: Test, Edit, Remove… (the same in its right-click menu)
  const rows = channels.map(c => {
    const actions = el("td", { class: "actions" });
    const test = rowButton(t("Test"), c.name, host, { onclick: (event) => busy(event.target, async () => {
      clear(result);
      try { const r = await api("POST", hostPath(`/notify/${encodeURIComponent(c.name)}/test`, host), {}); result.append(notice(r.ok ? t("Sent: {name}", { name: c.name }) : t("Not sent: {name}", { name: c.name }), r.ok ? "ok" : "error"), outputBlock(r)); }
      catch (e) { result.append(errorNotice(e)); }
    }) });
    const edit = rowButton(t("Edit"), c.name, host, { onclick: () => fill(c) });
    const remove = rowButton(t("Remove…"), c.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Remove the phone channel {name} on {host}? Its questions and alarms no longer reach you there.", { name: c.name, host }), t("Yes, remove it"), async () => {
      try { await api("POST", hostPath(`/notify/${encodeURIComponent(c.name)}/remove`, host), { confirm: true }); show("settings"); } catch (e) { failed(t("Could not remove it"))(e); }
    }, { trigger: remove }) });
    actions.append(test, edit, remove);
    const status = c.status && typeof c.status === "object" ? (c.status.ok ? t("sent") : t("failed: {error}", { error: String(c.status.error || "") })) : "";
    const tr = el("tr", {}, el("td", { class: "mono" }, c.name), el("td", {}, String(c.kind || "")), el("td", { class: "mono" }, String(c.where || "")),
      el("td", {}, (c.events || []).join(", ")), el("td", {}, `${c.when || ""}, ${c.detail || ""}${c.quiet ? `, ${t("quiet {hours}", { hours: c.quiet })}` : ""}`), el("td", {}, status), actions);
    return tr;
  });
  const link = el("input", { id: "notify-link", class: "mono", placeholder: "https://web.example.org:8443", value: (data && data.link) || "" });
  const linkSave = el("button", { onclick: (event) => busy(event.target, async () => {
    clear(result);
    try { await api("POST", hostPath("/notify-link", host), { url: link.value.trim() || null }); toast("success", t("Saved"), link.value.trim() || t("no link")); }
    catch (e) { result.append(errorNotice(e)); }
  }) }, t("Save link"));
  form.append(el("label", { for: "notify-name" }, t("Name")), name, el("label", { for: "notify-kind" }, t("Kind")), kind, own,
    el("span", {}, t("Events")), el("div", { class: "row" }, ...events.flatMap(([, box, label]) => [box, label])),
    el("label", { for: "notify-when" }, t("When")), when, el("label", { for: "notify-detail" }, t("A message says")), detail,
    el("label", { for: "notify-quiet" }, t("Quiet hours (alarms still go)")), quiet, el("label", { for: "notify-rate" }, t("At most one message per (seconds)")), rate,
    el("span"), row, el("label", { for: "notify-link" }, t("Link in the messages")), el("div", { class: "row" }, link, linkSave), result);
  clear(box).append(
    el("p", { class: "hint" }, t("Questions and alarms of {host} to your phone while you are away: sent by that host, never from inside a sandbox. Tokens are the names of that host's secrets (page Secrets). WhatsApp needs an approved message template to start a conversation.", { host })),
    table([t("Name"), t("Kind"), t("Sends to"), t("Events"), t("When, detail"), t("Last message"), ""], rows, t("No phone channels on this host.")),
    el("div", { class: "card" }, form));
}

// What protects a host's secrets (GET /vault: `secrets status` there) and the step that unlocks them:
// the system's keyring or Keychain, or the private files with their primary password (docs/SECRETS_DESIGN.md).
// The password is typed into a field that starts hidden and whose choice is not remembered (as the login's), sent
// once over this page's HTTPS and cleared; the service hands it to `secrets unlock --password-stdin` on that host.
// Changing or removing it is the command line's (sandbox-ai secrets password change|remove).
function protectionWords(p) {
  if (p.backend === "keyring") return t("Kept in the system's keyring, which encrypts them.");
  if (p.backend === "keychain") return t("Kept in the macOS Keychain, which encrypts them.");
  if (!p.password) return p.plain
    ? t("Kept in plain files, not encrypted ({count}): whoever reads that host's files or a backup has them. Set a primary password.", { count: p.plain })
    : t("Kept in private files: the first secret needs a primary password, which encrypts them.");
  const left = p.plain ? " " + t("{count} still in plain files: encrypted at the next unlock.", { count: p.plain }) : "";
  return (p.locked ? t("Encrypted with the primary password; locked: a start that needs a secret waits for it.")
    : t("Encrypted with the primary password; unlocked.")) + left;
}

function secretsProtection(p, host) {
  const box = el("div", { class: "card", id: "secrets-protection" });
  const status = el("div", { class: "wide" });
  const actions = el("div", { class: "row" });
  const form = el("div", { class: "form" });
  const sent = (title, r) => { toast("success", title, (r && r.output) || ""); show("secrets"); };
  const passwordField = (id) => secretInput({ id, class: "mono" }, { shown: false, remember: false });
  const open = (fields, button) => { clear(form).append(...fields, el("span"), el("div", { class: "row" }, button)); };
  box.append(el("p", {}, protectionWords(p)), actions, form, status);
  const files = p.backend === "file" || p.password || p.plain;
  if (!files) return box;
  if (!p.password) {
    actions.append(el("button", { class: "primary", onclick: () => {
      const [first, firstField] = passwordField("primary-password-new");
      const [again, againField] = passwordField("primary-password-again");
      open([el("label", { for: "primary-password-new" }, t("New primary password (8 characters or more)")), firstField,
        el("label", { for: "primary-password-again" }, t("The same again")), againField,
        el("p", { class: "hint wide" }, t("It is asked once per session. There is no way to recover it: without it nobody reads these secrets, you neither."))],
      el("button", { class: "primary", onclick: async () => {
        clear(status);
        if (first.value.length < 8) { status.append(notice(t("A primary password has 8 characters or more."), "warn")); return; }
        if (first.value !== again.value) { status.append(notice(t("The two passwords do not match."), "warn")); return; }
        const password = first.value; first.value = ""; again.value = "";
        try { sent(t("Primary password set on {host}", { host }), await api("POST", hostPath("/vault/new", host), { password })); }
        catch (e) { status.append(errorNotice(e)); }
      } }, t("Set the primary password")));
      first.focus();
    } }, t("Set a primary password…")));
  } else if (p.locked) {
    actions.append(el("button", { class: "primary", onclick: () => {
      const [password, passwordBox] = passwordField("primary-password");
      const time = el("select", { id: "primary-password-for" },
        ...[["30m", t("30 minutes")], ["8h", t("8 hours")], ["1d", t("1 day")]].map(([value, text]) => el("option", Object.assign({ value }, value === "8h" ? { selected: true } : {}), text)));
      open([el("label", { for: "primary-password" }, t("Primary password")), passwordBox,
        el("label", { for: "primary-password-for" }, t("Unlocked for")), time],
      el("button", { class: "primary", onclick: async () => {
        clear(status);
        if (!password.value) { status.append(notice(t("Type the primary password."), "warn")); return; }
        const typed = password.value; password.value = "";
        try { sent(t("Secrets unlocked on {host}", { host }), await api("POST", hostPath("/vault/unlock", host), { password: typed, for: time.value || "8h" })); }
        catch (e) { status.append(errorNotice(e)); }
      } }, t("Unlock")));
      password.focus();
    } }, t("Unlock…")));
  } else {
    actions.append(el("button", { onclick: async () => {
      clear(status);
      try { sent(t("Secrets locked on {host}", { host }), await api("POST", hostPath("/vault/lock", host), {})); }
      catch (e) { status.append(errorNotice(e)); }
    } }, t("Lock")));
  }
  return box;
}

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
  // (an older sandbox-ai on that host knows no primary password: the page shows its secrets as before)
  let protection = null;
  try { protection = await api("GET", hostPath("/vault", host)); } catch (e) { protection = null; }
  if (!live()) return;
  if (protection && typeof protection === "object" && typeof protection.backend === "string") main.append(secretsProtection(protection, host));
  const rows = names.map(x => {
    const actions = el("td", { class: "actions" });
    const remove = rowButton(t("Delete"), x.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Delete the secret {name} on {host}? Starts, VPN profiles and Git logins that name it no longer get it.", { name: x.name, host }), t("Yes, delete it"), async () => {
      try { await api("DELETE", hostPath(`/secrets/${encodeURIComponent(x.name)}`, host), { confirm: true }); toast("success", t("{name} deleted on {host}", { name: x.name, host }), ""); show("secrets"); }
      catch (e) { failed(t("Could not delete {name} on {host}", { name: x.name, host }))(e); }
    }, { trigger: remove }) });
    // Edit…: another name (secrets rename: carried through every login, VPN profile, project file and kept start
    // that names it). Its value is never shown: a new one is typed below under the same name (asked first).
    const edit = rowButton(t("Edit…"), x.name, host, { onclick: () => rowEditor(secretEditor, t("Edit the secret {name}", { name: x.name }),
      [{ key: "new", label: t("Name"), value: x.name }],
      (v, row, result, trigger) => {
        if (!v.new || v.new === x.name) { clear(result).append(el("p", { class: "hint" }, t("Nothing changed. To replace its value: type the name and the new value below (Add asks first)."))); return; }
        confirmIn(row, t("Rename the secret {old} to {new} on {host}? Its name is changed in every login, VPN profile, trusted project file and kept start that names it; what still names {old} afterwards is said.", { old: x.name, new: v.new, host }), t("Yes, rename it"), async () => {
          try { const r = await api("POST", hostPath(`/secrets/${encodeURIComponent(x.name)}/rename`, host), { new: v.new, confirm: true }); clear(secretEditor).append(el("div", { class: "card" }, el("h3", {}, t("{old} is now {new} on {host}", { old: x.name, new: v.new, host })), outputBlock(r), el("button", { onclick: () => show("secrets") }, t("OK")))); }
          catch (e) { clear(result).append(errorNotice(e, t("The secret was not renamed"))); }
        }, { trigger });
      }, host) });
    // Send to another host…: that host's `sandbox-ai hosts` names the hosts it reaches; the value goes from its store
    // over SSH on standard input (secrets send), never through this page
    const send = rowButton(t("Send to another host…"), x.name, host, { onclick: () => rowEditor(secretEditor, t("Send the secret {name} to another host", { name: x.name }),
      [{ key: "host", label: t("Host (a name of sandbox-ai hosts there)"), value: "" }],
      (v, row, result, trigger) => {
        if (!v.host) { clear(result).append(el("p", { class: "hint" }, t("Name the host."))); return; }
        const post = (replace) => api("POST", hostPath(`/secrets/${encodeURIComponent(x.name)}/send`, host), Object.assign({ host: v.host, confirm: true }, replace ? { replace: true } : {}));
        const done = (r) => { toast("success", t("{name} is kept on {target} now", { name: x.name, target: v.host }), (r && r.output) || ""); clear(secretEditor); };
        confirmIn(row, t("Send the secret {name} from {host} to {target}? Its value goes over SSH on standard input into that host's secret store and is kept there from then on: whoever controls {target} can use it.", { name: x.name, host, target: v.host }), t("Yes, send it"), async () => {
          try { done(await post(false)); }
          catch (e) {
            if (/already/.test(errorText(e)) && /--replace/.test(errorText(e) + errorDetail(e))) {
              confirmIn(row, t("{target} keeps a secret {name} already: replace its value there? Everything that names it on that host gets the new one.", { name: x.name, target: v.host }), t("Yes, replace it"), async () => {
                try { done(await post(true)); } catch (err) { clear(result).append(errorNotice(err, t("The secret was not sent"))); }
              }, { trigger });
              return;
            }
            clear(result).append(errorNotice(e, t("The secret was not sent")));
          }
        }, { trigger });
      }, host) });
    actions.append(edit, send, remove);
    return el("tr", {}, el("td", { class: "mono" }, x.name), el("td", {}, x.store || ""), actions);
  });
  const secretEditor = el("div");
  main.append(table([t("Name"), t("Kept in"), ""], rows, t("No secrets on this host."), t("Secrets on {host}", { host })), secretEditor);
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
  // its own update mode (sandbox-ai update mode; 0.1.122 on): changed on the Doctor page of that host
  const mode = typeof s.update_mode === "string" ? `; ${t("updates: {mode}", { mode: s.update_mode })}` : "";
  if (["checkout", "pip", "package"].includes(s.kind)) return `${s.version} (${s.kind})${mode}`;
  if (s.update) return t("{version} → {available} available", { version: s.version, available: s.available }) + mode;
  return (s.update === false ? t("{version}, up to date", { version: s.version }) : s.version) + mode;
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

// The host updated with this machine's copy (hosts update --from-here), asked first like an install: for a host that
// cannot reach the release server, or before a release is published.
function fromHereButton(name, output, sudo) {
  return el("button", { title: t("hosts update --from-here: this machine's sandbox-ai, as the installation the host has (the user's own or the system's)"),
    onclick: (ev) => confirmIn(ev.target.parentNode, t("Put this machine's sandbox-ai on {host}, as the installation it has there? It runs there with the rights of that installation.", { host: name }), t("Yes, update from here"), async () => {
      output.append(notice(t("Updating {host}…", { host: name })));
      try { await updateHost(name, output, typeof sudo === "function" ? sudo() : sudo, "", true); } catch (e) { output.append(notice(`${name}: ${e.message}`, "error")); }
      if (state.page === "hosts" && typeof state.reloadVersions === "function") state.reloadVersions();  // its version cell, as Update does
    }) }, t("Update from this machine's copy…"));
}

// One host's update: its own signed sandbox-ai update; a sudo that wants a password is asked in a terminal tab.
async function updateHost(name, output, sudo, version, fromHere) {
  const body = Object.assign(sudo === undefined ? {} : { sudo }, version ? { version } : {}, fromHere ? { from_here: true, confirm: true } : {});
  const r = await api("POST", `/api/hosts/${encodeURIComponent(name)}/update`, body);
  output.append(...keyNotices(r));
  if (r.changed) output.append(notice(`${name}: ${r.before || "?"} → ${r.after}${r.sudo ? " (sudo)" : ""}`, "ok"));
  else if (r.error && !r.after) {
    say(t("{host}: not updated: {error}", { host: name, error: r.error }));
    output.append(el("div", { class: "notice error" }, t("{host}: not updated: {error}", { host: name, error: r.error }),
      r.needs_password && terminalsHere() ? el("button", { onclick: () => openTerminal(fromHere
        ? Object.assign({ host: name, kind: "install" }, installQuery(Object.assign({ mode: r.mode || "user", prefix: r.prefix, source: "here" }, r.mode === "system" ? { sudo: true } : {})))
        : { host: name, kind: "update" }, t("update {host}", { host: name })) }, t("Update in a terminal (sudo asks there)")) : null,
      r.needs_sudo ? el("button", { onclick: (ev) => busy(ev.target, () => updateHost(name, output, true, "", fromHere).catch(failed(t("Could not update {host}", { host: name })))) }, t("Update with sudo")) : null,
      !fromHere && /no published release/.test(r.error) ? fromHereButton(name, output, sudo) : null));
    if (r.needs_password) output.append(passwordHelp(name, ["sandbox-ai", "hosts", "update", name, "--sudo", ...(fromHere ? ["--from-here"] : [])]));
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
    state.hostVersions = Object.assign(state.hostVersions || {}, found);  // the doctor page's version of an older host
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
  state.reloadVersions = () => loadVersions(false);
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
            if (r.doctor) output.append(el("h2", {}, t("Doctor on {host}", { host: h.name })), doctorView(r.doctor, { level: 3, host: h.name, known: versions[h.name] }));
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
        h.kind === "local" ? null : fromHereButton(h.name, output, () => sudoChoice(updateSudo[h.name])),
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
  "no-terminal": N_("runs without its terminal: its start did not make one (its supervisor.log says why); stop it and start it again"),
  ended: N_("no agent any more: its tmux session has ended (the agent and its shell window), its sandbox has not; stop it, then resume it"),
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
// "Project configuration PATH is not trusted" (agent_sandbox.load_project_config). The path, or null. Only the
// launcher's own words count: what comes after its "agent-sandbox: /work is ..." line (printed once the
// configuration is accepted) is the sandbox's, and an agent must not raise this offer with text of its own.
const SANDBOX_RUNS = "agent-sandbox: /work is ";
function untrustedConfig(text) {
  const plain = String(text).replace(/\u001b\[[0-9;?]*[A-Za-z]/g, "").replace(/\r/g, "").split(SANDBOX_RUNS)[0];
  const match = /Project configuration (\/[^\n]*?) is (?:not trusted|changed since it was trusted)/.exec(plain);
  return match ? match[1] : null;
}

function offerReview(host, path) {
  toast("warning", t("The project configuration is not trusted"), t("{path} on {host}: review it, and trust exactly what you saw.", { path, host }),
    { action: [t("Review it"), () => { state.review = { host, path }; state.host = host; show("projects"); }] });
}

function openTerminal(params, title, onClose) {
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
  // Only a new start's terminal is read for the launcher's refusal, and only until the sandbox runs (attached
  // ones show only what runs inside).
  let seen = "", offered = params.kind !== "start";
  ws.onmessage = (event) => {
    term.write(typeof event.data === "string" ? event.data : new Uint8Array(event.data));
    if (offered) return;
    seen = (seen + (typeof event.data === "string" ? event.data : decoder.decode(new Uint8Array(event.data), { stream: true }))).slice(-4000);
    const path = untrustedConfig(seen);
    if (path) { offered = true; offerReview(host, path); }
    else if (seen.includes(SANDBOX_RUNS)) offered = true;  // it runs: what follows is the sandbox's
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
    if (onClose && entry.opened) { try { onClose(); } catch (e) { /* the page it would refresh is gone */ } }
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

// What in a reviewed project file weakens the sandbox (the service's "marks", modes.project_marks: options read as
// the launcher reads them, short forms too), one line each, nothing cut: every extra workspace with what the agent may
// do there, the agent arguments, a laxer mode, canaries off, a kept HOME, debug logs, the project's ruleset and MCP
// servers. The texts are the app's (project_trust). null when nothing weakens.
const MARK_TEXTS = {
  canaries: N_("fewer canary tokens: a secret leaving is noticed less often"),
  keep: N_("the sandbox HOME is kept after the start: what the agent wrote there stays and runs again"),
  log_debug: N_("debug logs: the logs keep much more of what passes"),
  rules: N_("the project's own ruleset decides requests (it may allow what is asked otherwise)"),
  mcp_servers: N_("the project defines its own MCP servers (programs with their own folders and network)"),
  workspaces: N_("further folders the agent sees in every start of the project"),
  agent_args: N_("arguments for the agent itself: they can change what it does"),
};
function projectMarks(r) {
  const marks = Array.isArray(r.marks) ? r.marks.filter(m => m && typeof m === "object") : [];
  if (!marks.length) return null;
  const doc = r.document && typeof r.document === "object" ? r.document : {};
  const lines = [];
  for (const m of marks) {
    const value = (v) => typeof v === "string" ? v : JSON.stringify(v);
    if (m.why === "mode") lines.push(`⚠ mode: ${value(doc.mode)} - ${t("a laxer security mode than your default ({mode})", { mode: String(m.default || "") })}`);
    else if (m.why === "option") lines.push(`⚠ ${String(m.option || "")}${m.value === null || m.value === undefined ? "" : ` ${String(m.value)}`}: ${m.option === "--ask" && m.value === "allow" ? t("asks nothing: every question is allowed at once") : t("widens what the sandbox may do or see")}`);
    else if (m.why === "workspace") lines.push(`    ⚠ ${String(m.value || "")} - ${m.read_only ? t("read-only for the agent, outside the project") : t("writable for the agent, outside the project")}`);
    else if (m.why === "agent_args") lines.push(`⚠ agent_args: ${t(MARK_TEXTS.agent_args)}`, ...(Array.isArray(doc.agent_args) ? doc.agent_args.map(a => `    ${value(a)}`) : []));
    else if (MARK_TEXTS[m.why]) lines.push(`⚠ ${String(m.key || m.why)}: ${t(MARK_TEXTS[m.why])}`);
    else lines.push(`⚠ ${String(m.key || "?")}: ${t("widens what the sandbox may do or see")}`);
  }
  return el("div", { class: "notice warn project-marks" }, el("pre", {}, lines.join("\n")),
    el("p", {}, t("Lines marked {mark} weaken the sandbox: trust them only if you chose them.", { mark: "⚠" })));
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
      el("p", {}, now), r.problem || r.error ? notice(String(r.problem || r.error), "error") : [projectMarks(r), el("pre", { class: "project-file" }, JSON.stringify(r.document, null, 2))],
      el("p", { class: "hint mono" }, t("SHA-256 {digest}", { digest: r.digest })), row));
    if (!r.problem && !r.error && !r.trusted && r.digest) {
      const trust = el("button", { class: "primary", onclick: () => confirmIn(row, t("Trust exactly this configuration of {path} on {host}? Every later start of that project gets its options; if the file changes, it is no longer trusted.", { path: r.path, host }), t("Yes, trust it"), async () => {
        try { const out = await api("POST", hostPath("/projects/trust", host), { path: r.path, digest: r.digest, confirm: true }); toast("success", t("{path} trusted on {host}", { path: r.path, host }), (out && out.output) || ""); show("projects"); }
        catch (e) { failed(t("Could not trust {path} on {host}", { path: r.path, host }))(e); }
      }, { kind: "primary", trigger: trust }) }, t("Trust exactly this…"));
      row.append(trust);
    }
  };
  // the wizard (sandbox-ai wizard new | edit PATH) in a terminal of the page: it asks there, shows what it writes and
  // trusts it there
  const wizard = (where) => (terminalsHere() ? openTerminal(Object.assign({ host, kind: "wizard" }, where ? { path: where } : {}), t("wizard on {host}", { host }))
    : toast("warning", t("No terminals here"), t("Run it in a terminal on {host}: {command}", { host, command: where ? `sandbox-ai wizard edit ${where}` : "sandbox-ai wizard" })));
  main.append(el("div", { class: "card" }, el("div", { class: "form" }, el("label", { for: "project-path" }, t("Configuration file")), path,
    el("span"), el("div", { class: "row" }, el("button", { class: "primary", onclick: () => { if (path.value.trim()) review(path.value.trim()); } }, t("Review")),
      el("button", { onclick: () => wizard(path.value.trim()) }, t("Edit in the wizard…")), el("button", { onclick: () => wizard("") }, t("New with the wizard…"))))), reviewBox);
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

// The catalog an MCP server was added from (mcp list --json: "catalog", "signed"; a host older than 0.1.78 names
// none), with whether its signature was verified.
function mcpOriginCell(x) {
  const catalog = typeof x.catalog === "string" ? x.catalog : t("unknown");
  // (mcp add --build: built on that host from its source, never signed - said so)
  const signed = x.built && typeof x.built === "object" ? t("built here, not signed") : x.signed === true ? t("signed") : x.signed === false ? t("unsigned") : "";
  return el("td", {}, catalog, signed ? el("div", { class: x.signed ? "hint" : "hint status-warn" }, signed) : null);
}

// MCP servers (sandbox-ai mcp): every defined one, running or not; start (for a start: attached to it), stop, the
// catalogs and adding from them.
// An MCP server's network as `mcp add|set --network` names it, for people (mcp_egress.py: what each reaches).
const MCP_NETWORKS = { full: N_("internet (no LAN)"), allowlist: N_("only its hosts"), lan: N_("internet + LAN (warning)"),
  none: N_("network|none") };

// The network cell of a server of `mcp list --json`: a catalog server's kind (its hosts, the parts of your network
// opened to it), with what to change (egress_note: switch to its hosts, it reaches your network) beneath; a server
// with a command reaches what its own sandbox's rules let it.
function mcpNetworkCell(x) {
  const definition = (x && typeof x.definition === "object" && x.definition) || {};
  if (x.network === null || x.network === undefined) return el("td", { class: "hint" }, t("its sandbox's rules"));
  const kind = typeof definition.network === "string" ? definition.network : "none";
  const extra = [...(Array.isArray(definition.hosts) && kind === "allowlist" ? definition.hosts : []),
    ...(Array.isArray(definition.allow_private) ? definition.allow_private : []),
    ...(Array.isArray(definition.allow_tcp) ? definition.allow_tcp : [])].filter(v => typeof v === "string");
  const cell = el("td", { class: kind === "lan" ? "status-warn" : "" }, t(MCP_NETWORKS[kind] || kind),
    extra.length ? el("div", { class: "hint mono" }, extra.join(", ")) : "");
  if (typeof x.egress_note === "string" && x.egress_note) cell.append(el("div", { class: "hint", lang: "en" }, x.egress_note));
  return cell;
}

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
    const vpn = offerNames(el("input", { id: "mcp-vpn", class: "mono", placeholder: "home" }), "vpn", host);
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
  main.append(table([t("Name"), t("State"), t("Network"), t("Serves"), t("Catalog"), t("Command"), ""], servers.map(x => {
    const actions = el("td", { class: "actions" });
    const stop = rowButton(t("Stop"), x.name, host, { class: "danger", onclick: () => confirmIn(actions, t("Stop the MCP server {name} on {host}? The starts it serves lose it until it starts again.", { name: x.name, host }), t("Yes, stop it"), async () => {
      try { await api("POST", hostPath(`/mcp/${encodeURIComponent(x.name)}/stop`, host), { confirm: true }); show("mcp"); } catch (e) { failed(t("Could not stop {name} on {host}", { name: x.name, host }))(e); }
    }, { trigger: stop }) });
    actions.append(rowButton(t("Start…"), x.name, host, { onclick: () => startForm(x.name) }), stop);
    return el("tr", {}, el("td", { class: "mono" }, x.name), statusCell(x.status), mcpNetworkCell(x), el("td", {}, (x.serves || []).join(", ")), mcpOriginCell(x), el("td", { class: "mono" }, String(x.command || "")), actions);
  }), t("No MCP servers on this host yet: add one from a catalog below.")), startBox);

  // the catalogs, and adding an entry with its options
  const catalogs = el("input", { id: "mcp-catalogs", class: "mono", placeholder: t("default catalogs (or files and https URLs)") });
  const catalogBox = el("div");
  const entry = el("input", { id: "mcp-entry", class: "mono", placeholder: "hub:binwalk" });
  const as = el("input", { id: "mcp-as", class: "mono", placeholder: t("the entry's name") });
  const mounts = el("textarea", { id: "mcp-mounts", rows: 2, class: "mono", placeholder: "/mnt/samples=/home/you/samples:ro" });
  // its variables as `mcp add --env` takes them, one per line; the catalog's descriptions of the entry's (Add…)
  const env = el("textarea", { id: "mcp-env", rows: 3, class: "mono", placeholder: t("NAME, NAME=VALUE or NAME@SECRET") });
  const envHelp = el("div", { id: "mcp-env-help", class: "hint" });
  const showVariables = (variables) => {
    clear(envHelp);
    const list = Array.isArray(variables) ? variables.filter(v => v && typeof v.name === "string") : [];
    envHelp.append(t("One per line: NAME (that host's environment, else its secret NAME), NAME=VALUE (a value that is no secret) or NAME@SECRET (that host's secret SECRET)."));
    if (!list.length) return;
    envHelp.append(el("ul", {}, list.map(v => el("li", {}, el("span", { class: "mono" }, v.name),
      v.description ? ` - ${v.description}` : "",
      ` (${[v.required ? t("required") : t("optional"), v.secret ? t("a secret: NAME or NAME@SECRET, never a value") : t("a value with NAME=VALUE")].join(", ")})`))));
    // the required ones by name, where nothing was typed yet
    if (!env.value.trim()) env.value = list.filter(v => v.required).map(v => v.name).join("\n");
  };
  showVariables([]);
  // where it reaches (mcp add --network): the internet without your local network, only its entry's hosts, your
  // network too (asked first), or nothing
  const network = el("select", { id: "mcp-network" }, el("option", { value: "" }, t("as the entry says")),
    ["full", "allowlist", "lan", "none"].map(n => el("option", { value: n }, t(MCP_NETWORKS[n]))));
  const memory = el("input", { id: "mcp-memory", placeholder: "512m" }), pids = el("input", { id: "mcp-pids", type: "number", min: 1, placeholder: "128" });
  const backend = el("select", { id: "mcp-add-backend" }, el("option", { value: "" }, t("Podman when it is there")), ["podman", "bwrap", "both"].map(b => el("option", { value: b }, b)));
  const vpn = offerNames(el("input", { id: "mcp-add-vpn", class: "mono", placeholder: "home" }), "vpn", host);
  const noPull = el("input", { id: "mcp-no-pull", type: "checkbox" }), unsigned = el("input", { id: "mcp-unsigned", type: "checkbox" });
  const addRow = el("div", { class: "row" }), addStatus = el("div", { class: "wide" });
  const showCatalog = (event) => busy(event.target, async () => {
    clear(catalogBox).append(el("p", { class: "hint" }, t("Loading…")));
    let r;
    try { r = await api("POST", hostPath("/mcp-catalog", host), { catalogs: wordsOf(catalogs.value) }); } catch (e) { clear(catalogBox).append(errorNotice(e)); return; }
    const SIGNED = { good: N_("signed"), bad: N_("signature bad"), none: N_("unsigned") };
    clear(catalogBox).append(...(r.problems || []).map(x => notice(x, "warn")), table([t("Entry"), t("Network"), t("Title"), t("Catalog"), t("Signature"), ""], (r.entries || []).map(x =>
      el("tr", {}, el("td", { class: "mono" }, x.entry), el("td", { class: "mono" }, x.network), el("td", {}, x.title, x.published ? "" : ` (${t("not published yet")})`),
        el("td", {}, typeof x.catalog === "string" ? x.catalog : ""),
        el("td", { class: x.signature === "good" ? "status-ok" : "status-fail" }, t(SIGNED[x.signature] || N_("unsigned"))),
        el("td", { class: "actions" }, el("button", { onclick: () => { entry.value = x.entry; unsigned.checked = false; env.value = ""; showVariables(x.env); entry.focus(); } }, t("Add…"))))), t("The catalogs list no servers.")));
  });
  const add = (event) => {
    clear(addStatus);
    const body = { entry: entry.value.trim(), as: as.value.trim(), env: linesOf(env.value), network: network.value, memory: memory.value.trim(), pids: pids.value.trim(),
      catalogs: wordsOf(catalogs.value), no_pull: noPull.checked, backend: backend.value, vpn: vpn.value.trim(), unsigned: unsigned.checked, mounts: [] };
    for (const line of linesOf(mounts.value)) {
      const match = /^([^=]+)=([^:]+)(?::(ro|rw))?$/.exec(line);
      if (!match) { addStatus.append(notice(t("A mount is TARGET=HOST or TARGET=HOST:ro|rw: {line}", { line }), "error")); return; }
      body.mounts.push({ target: match[1], host: match[2], mode: match[3] || "ro" });
    }
    const send = async (confirm) => {
      try { const r = await api("POST", hostPath("/mcp", host), Object.assign({}, body, confirm ? { confirm: true } : {})); toast("success", t("{name} added on {host}", { name: body.entry, host }), (r && r.output) || ""); show("mcp"); }
      catch (e) {
        clear(addStatus);
        // refused only because no trusted key signed its catalog: said, and offered to add it anyway (--unsigned)
        if (e instanceof ApiError && e.code === "mcp-unsigned-catalog" && !body.unsigned) { offerUnsigned(e); return; }
        // no signed image in its catalog yet (pending): offered to be built from its source (asked with the commit)
        if (e instanceof ApiError && e.code === "mcp-unpublished") { offerBuild(e); return; }
        addStatus.append(errorNotice(e));
      }
    };
    const offerBuild = (e) => {
      const offer = el("div", { class: "row" });
      addStatus.append(el("div", { class: "notice warn" }, el("p", {}, errorText(e)), offer, el("div", { class: "hint mono error-detail", lang: "en" }, e.message || "")));
      buildMcpServer(offer, host, Object.assign({}, body, { confirm: true }),
        (r) => { toast("success", t("{name} built and added on {host} (not signed)", { name: body.entry, host }), (r && r.output) || ""); show("mcp"); },
        (failure) => { clear(addStatus).append(errorNotice(failure)); });
    };
    const offerUnsigned = (e) => {
      const catalog = String((e.params && e.params.catalog) || ""), name = body.entry;
      const question = [catalog ? t("{name} was not added: its catalog {catalog} is not signed by a trusted key.", { name, catalog })
        : t("{name} was not added: its catalog is not signed by a trusted key.", { name }),
      t("Unsigned means that nobody vouched that the server is built from its pinned sources and works with sandbox-ai; it runs in a sandbox of its own all the same."),
      t("Add {name} anyway (--unsigned)?", { name })].join(" ");
      const offer = el("div", { class: "row" });
      addStatus.append(el("div", { class: "notice warn" }, offer, el("div", { class: "hint mono error-detail", lang: "en" }, e.message || "")));
      confirmIn(offer, t("On {host}: {text}", { host, text: question }), t("Add unsigned"), async () => {
        body.unsigned = true;
        unsigned.checked = true;
        await send(true);
      }, { kind: "primary", onCancel: () => { clear(addStatus).append(errorNotice(e)); } });
    };
    const writable = body.mounts.filter(m => m.mode === "rw").map(m => m.host);
    const what = [body.unsigned ? t("from a catalog no trusted key signed") : null, writable.length ? t("with {dirs} writable", { dirs: writable.join(", ") }) : null,
      body.network === "lan" ? t("reaching your local network and this host (internet + LAN)") : null].filter(Boolean);
    if (!what.length) { busy(event.target, () => send(false)); return; }
    confirmIn(addRow, t("Add the MCP server {name} on {host} {what}?", { name: body.entry, host, what: what.join(t(" and ")) }), t("Yes, add it"), () => send(true), { kind: "primary", trigger: event.target });
  };
  addRow.append(el("button", { class: "primary", onclick: add }, t("Add the server")));
  main.append(el("h2", {}, t("Catalogs")), el("div", { class: "card" }, el("div", { class: "form" }, el("label", { for: "mcp-catalogs" }, t("Catalogs")), catalogs,
    el("span"), el("button", { onclick: showCatalog }, t("Show the catalog"))), catalogBox),
  el("h2", {}, t("Add a server")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "mcp-entry" }, t("Catalog entry")), entry, el("label", { for: "mcp-as" }, t("Name for --mcp")), as,
    el("label", { for: "mcp-mounts" }, t("Mounts (one per line)")), mounts, el("label", { for: "mcp-env" }, t("Variables (one per line)")), el("div", {}, env, envHelp),
    el("label", { for: "mcp-network" }, t("Network")), network, el("label", { for: "mcp-memory" }, t("Memory")), memory, el("label", { for: "mcp-pids" }, t("Processes")), pids,
    el("label", { for: "mcp-add-backend" }, t("Backend")), backend, el("label", { for: "mcp-add-vpn" }, t("VPN profile")), vpn,
    el("label", { for: "mcp-no-pull" }, t("The image is there already")), noPull, el("label", { for: "mcp-unsigned" }, t("Also from an unsigned catalog")), unsigned,
    addStatus, el("span"), addRow)));
};

// The logins of the sessions (sandbox-ai login --status --sessions): what each session's login file says about itself
// - written by its agent, so every value is put in as text - and its fingerprint, never a token. Import as central
// login… is `login AGENT --from-session NAME --expect FINGERPRINT` (confirmed in place: login-import).
const LOGIN_SOURCES = { store: N_("central login"), host: N_("this computer's login"), file: N_("a login file"), none: N_("no login") };
const LOGIN_KINDS = { subscription: N_("subscription"), "api-key": N_("API key"), "subscription+api-key": N_("subscription and API key") };
function loginSource(row) {
  return row.source === "session" ? t("from session {name}", { name: String(row.source_session || "?") }) : t(LOGIN_SOURCES[row.source] || String(row.source || "?"));
}
function loginAccount(info) {
  const a = (info && info.account) || {};
  return [a.email, a.organization, a.account_id, ...(Array.isArray(a.providers) ? a.providers : [])].filter(Boolean).map(String).join(", ");
}
function loginDetails(row) {
  const info = row.info || {}, a = info.account || {};
  const pairs = [[t("Session"), row.name], [t("Agent"), row.agent], [t("State"), row.state], [t("Login from"), loginSource(row)],
    [t("Account (as the file says)"), a.email], [t("Organization"), a.organization], [t("Plan"), a.plan], [t("Account id"), a.account_id],
    [t("Providers"), (Array.isArray(a.providers) ? a.providers : []).join(", ")], [t("Kind"), info.kind ? t(LOGIN_KINDS[info.kind] || String(info.kind)) : ""],
    [t("Expires"), shownTime(info.expires)], [t("Renewable to"), shownTime(info.refresh_expires)], [t("Last refresh"), shownTime(info.last_refresh)],
    [t("Fingerprint"), info.fingerprint], [t("The same login as the central one"), row.login ? (row.shares_central ? t("yes") : t("no")) : ""],
    [t("Problem"), row.problem]];
  return el("dl", { class: "details" }, ...pairs.filter(([, v]) => v).flatMap(([k, v]) => [el("dt", {}, k), el("dd", { class: "mono" }, String(v))]));
}
// A row's Edit…: FIELDS ([{key, label, value, placeholder, kind}]: KIND "secret" offers that host's secret names) in
// a small form in HOLDER, filled in with what the row has now - never a secret's value, only names. Save hands what
// the fields hold ({key: text}) to SAVE(values, row, result, trigger); Cancel drops the form.
function rowEditor(holder, title, fields, save, host) {
  const inputs = fields.map((f, i) => {
    const input = el("input", { id: `row-edit-${i}`, class: "mono", value: f.value || "", placeholder: f.placeholder || "" });
    return f.kind === "secret" ? offerNames(input, "secret", host) : input;
  });
  const row = el("div", { class: "row" }), result = el("div", { class: "wide" });
  const values = () => Object.fromEntries(fields.map((f, i) => [f.key, inputs[i].value.trim()]));
  row.append(el("button", { class: "primary", onclick: (event) => save(values(), row, result, event.target) }, t("Save…")), el("button", { onclick: () => clear(holder) }, t("Cancel")));
  clear(holder).append(el("div", { class: "card" }, el("h3", {}, title), el("div", { class: "form" },
    ...fields.flatMap((f, i) => [el("label", { for: `row-edit-${i}` }, f.label), inputs[i]]), el("span"), row, result)));
  if (inputs[0]) inputs[0].focus();
  return inputs;
}

// Why a kind of credential cannot be given to a sandbox that runs: its Give… button is there, greyed out, with this.
const NOT_WHILE_RUNNING = {
  publish: N_("A publish login cannot be given to a sandbox that runs: its tools' variables are set when it starts, and the questions before every upload are rules of that start. Restart it with --publish KIND; the token alone, without those questions, as an API key: the start's Network card."),
  identities: N_("An identity is chosen when a sandbox starts (--identity NAME): its commit name, its Git logins and its publish logins are set up then. Restart the sandbox with it; one of its Git logins for a running sandbox: the start's Network card (Git login)."),
  vpn: N_("A VPN is set up before the agent starts (its tunnel and kill switch are the sandbox's network): a running sandbox cannot be given one. Restart it with --vpn PROFILE."),
};
function notWhileRunning(kind, what, host) {
  return rowButton(t("Give to a running sandbox…"), what, host, { disabled: "", title: t(NOT_WHILE_RUNNING[kind]) });
}

// An agent login for a sandbox that runs on HOST (sandbox-ai login give): the central login of AGENT, or the session
// FROM's. In HOLDER: the running starts of that agent to choose from, then the login as that host describes it
// (POST /session-logins/give {check_only}: whose it is, what it replaces - never a token), asked in place, then
// given with the fingerprint that was shown.
async function giveAgentLogin(holder, host, agent, from) {
  let starts;
  clear(holder).append(el("p", { class: "hint" }, t("Loading…")));
  try { starts = await api("GET", hostPath("/starts", host)); } catch (e) { clear(holder).append(errorNotice(e)); return; }
  const running = (Array.isArray(starts) ? starts : []).filter(s => s && s.state === "running" && s.agent === agent && s.kind !== "mcp" && typeof s.name === "string" && s.name !== from);
  if (!running.length) { clear(holder).append(el("p", { class: "hint" }, t("No other sandbox of {agent} runs on {host} now: a login is given to a sandbox that runs (a stopped one takes the stored login at its next start).", { agent, host }))); return; }
  const choice = el("select", { id: "login-give-run" }, running.map(s => el("option", { value: s.name }, s.name)));
  const restart = el("input", { id: "login-give-restart", type: "checkbox" });
  const row = el("div", { class: "row" }), result = el("div", { class: "wide" });
  const body = () => Object.assign({ run: choice.value, agent }, from ? { from_session: from } : {});
  const show_ = (event) => busy(event.target, async () => {
    let shown;
    try { shown = await api("POST", hostPath("/session-logins/give", host), Object.assign({ check_only: true }, body())); }
    catch (e) { clear(result).append(errorNotice(e, t("The login was not given"))); return; }
    const login = (shown && typeof shown.login === "object" && shown.login) || {};
    if (shown && shown.same) { clear(result).append(el("p", { class: "hint" }, t("{name} has this login already: nothing to give.", { name: choice.value }))); return; }
    const lines = [t("Give {source} ({agent}) to {name} on {host}? Its agent works as that account from now on.", { source: String((shown && shown.source) || ""), agent, name: choice.value, host }),
      `${t("Account (as the file says)")}: ${loginAccount(login) || "?"}`, `${t("Fingerprint")}: ${String(login.fingerprint || "?")}`,
      shown && shown.current ? (shown.same_account ? t("It replaces the login the sandbox has now, of the same account.") : t("It replaces the login the sandbox has now, of ANOTHER account (or one that cannot be told).")) : "",
      t("The two then share the login: subscription logins rotate their refresh tokens, and two sandboxes that refresh the same one can log each other out."),
      shown && !shown.reads_again ? (restart.checked && shown.restart ? t("Its agent is started again inside the sandbox: what it does right now ends, its conversation continues.") : t("{agent} reads its login when it starts: until its agent is started again (sandbox-ai kits restart-agent), it goes on with the login it has.", { agent })) : ""].filter(Boolean);
    confirmIn(row, lines.join("\n"), t("Yes, give it"), async () => {
      try {
        const r = await api("POST", hostPath("/session-logins/give", host), Object.assign({ fingerprint: String(login.fingerprint || ""), restart: restart.checked, confirm: true }, body()));
        toast("success", t("{name} has the {agent} login now on {host}", { name: choice.value, agent, host }), (Array.isArray(r && r.notes) ? r.notes : []).map(String).join("\n"));
        show("logins");
      } catch (e) { clear(result).append(errorNotice(e, t("The login was not given"))); }
    }, { trigger: event.target });
  });
  row.append(el("button", { class: "primary", onclick: show_ }, t("Show the login…")), el("button", { onclick: () => clear(holder) }, t("Cancel")));
  clear(holder).append(el("div", { class: "card" }, el("h3", {}, from ? t("Give the login of {name} to a running sandbox", { name: from }) : t("Give the stored {agent} login to a running sandbox", { agent })),
    el("div", { class: "form" }, el("label", { for: "login-give-run" }, t("Sandbox")), choice,
      ...(agent === "claude" ? [] : [el("label", { for: "login-give-restart" }, t("Start its agent again inside the sandbox")), restart]), el("span"), row, result)));
  choice.focus();
}

async function drawSessionLogins(box, host) {
  let found;
  try { found = await api("GET", hostPath("/session-logins", host)); } catch (e) { clear(box).append(errorNotice(e)); return; }
  const rows = Array.isArray(found && found.sessions) ? found.sessions.filter(r => r && typeof r === "object") : [];
  const central = (found && typeof found.central === "object" && found.central) || {};
  if (!rows.length) { clear(box).append(el("p", { class: "hint" }, t("No running or kept session of an agent whose login sandbox-ai keeps."))); return; }
  const detail = el("div");
  const trs = rows.map(row => {
    const info = (row.info && typeof row.info === "object") ? row.info : {};
    const actions = el("td", { class: "actions" });
    actions.append(rowButton(t("Details"), row.name, host, { onclick: () => clear(detail).append(el("h3", {}, t("Login of {name}", { name: String(row.name) })), loginDetails(row)) }));
    if (row.login && /^[0-9a-f]{8}$/.test(String(info.fingerprint || ""))) {
      const stored = central[row.agent] && typeof central[row.agent] === "object" ? central[row.agent] : null;
      const other = Boolean(stored && stored.fingerprint !== info.fingerprint);
      const account = loginAccount(info) || String(info.fingerprint);
      const text = other ? t("Make the {agent} login of {name} ({account}) the central login on {host}? It replaces the central login ({old}) for every {agent} sandbox started from now on.", { agent: row.agent, name: row.name, account, host, old: loginAccount(stored) || String(stored.fingerprint || "") })
        : t("Make the {agent} login of {name} ({account}) the central login on {host}? Every {agent} sandbox started from now on uses it.", { agent: row.agent, name: row.name, account, host });
      const button = rowButton(t("Import as central login…"), row.name, host, { class: "primary", onclick: () => confirmIn(actions, text, t("Yes, import"), async () => {
        try {
          await api("POST", hostPath("/session-logins/import", host), { agent: row.agent, name: row.name, fingerprint: info.fingerprint, replace: other, confirm: true });
          toast("success", t("The {agent} login of {name} is the central login on {host}", { agent: row.agent, name: row.name, host }));
          show("logins");
        } catch (e) { failed(t("Could not import the login"))(e); }
      }, { trigger: button }) });
      actions.append(button);
      actions.append(rowButton(t("Give to a running sandbox…"), row.name, host, { onclick: () => giveAgentLogin(detail, host, String(row.agent), String(row.name)) }));
    }
    return el("tr", {}, el("td", { class: "mono" }, String(row.name)), el("td", { class: "mono" }, String(row.agent)), el("td", {}, String(row.state || "")),
      el("td", {}, loginSource(row)), el("td", {}, row.login ? loginAccount(info) : String(row.problem || t("no login"))),
      el("td", {}, info.kind ? t(LOGIN_KINDS[info.kind] || String(info.kind)) : ""), el("td", {}, shownTime(info.expires)),
      el("td", { class: "mono" }, String(info.fingerprint || "")), actions);
  });
  clear(box).append(table([t("Session"), t("Agent"), t("State"), t("Login from"), t("Account (as the file says)"), t("Kind"), t("Expires"), t("Fingerprint"), ""], trs), detail);
}

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
    actions.append(rowButton(t("Log in (terminal)"), agent, host, { class: "primary", onclick: () => openTerminal({ kind: "login", host, agent }, t("login {agent}", { agent })) }), logout,
      rowButton(t("Give to a running sandbox…"), agent, host, { onclick: () => giveAgentLogin(agentGive, host, agent, null) }));
    return el("tr", {}, el("td", { class: "mono" }, agent), actions);
  });
  const agentGive = el("div");
  const update = el("input", { id: "plugins-update", type: "checkbox" }), pluginKits = el("input", { id: "plugins-kits", class: "mono", placeholder: "egm" });
  const loginStatus = el("div");
  main.append(el("h2", {}, t("The agents' logins")), table([t("Agent"), ""], agentRows), agentGive,
    el("div", { class: "row" }, el("button", { onclick: (event) => busy(event.target, async () => {
      try { clear(loginStatus).append(outputBlock(await api("GET", hostPath("/login-status", host)))); } catch (e) { clear(loginStatus).append(errorNotice(e)); }
    }) }, t("Which login each agent's sandboxes get"))), loginStatus);
  // the logins of the sessions (login --status --sessions): which login each running or kept session uses, never a
  // token (a fingerprint); one imported as the central login, confirmed in place, only the login shown
  const sessionLogins = el("div", {}, el("p", { class: "hint" }, t("Loading…")));
  main.append(el("h2", {}, t("Logins of the sessions")), sessionLogins);
  drawSessionLogins(sessionLogins, host);
  main.append(
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

  // A login is checked first (check_only: its server as sandbox-ai checks it, a publish login that replaces one
  // asked), then asked (QUESTION, if any), then its token stored (token.store), then added: nothing is stored for a
  // login that is refused. When the add still fails, a token stored for it under a new name is removed again.
  // Errors are said in RESULT, below the form, which keeps what was typed.
  // O: {token, row, result, path, body, secret, replace (ready's), question, trigger}
  const addLogin = (o) => {
    clear(o.result);
    const send = (confirm) => api("POST", hostPath(o.path, host), Object.assign({}, o.body, confirm ? { confirm: true } : {}));
    const orphan = async () => {  // what became of a secret stored for this login that was then refused
      if (o.replace !== false) return null;
      try {
        await api("DELETE", hostPath(`/secrets/${encodeURIComponent(o.secret)}`, host), { confirm: true });
        secretNames = secretNames.filter(n => n !== o.secret);
        return t("The secret {name} stored for it was removed again.", { name: o.secret });
      } catch (e) { return t("The secret {name} stored for it stays: remove it on the page Secrets if it is not needed.", { name: o.secret }); }
    };
    const refused = async (e) => { const note = await orphan(); o.result.append(errorNotice(e, t("The login was not added"))); if (note) o.result.append(el("p", { class: "hint" }, note)); };
    const add = (confirm) => o.token.store(o.row, o.secret, o.replace, async () => {
      try { await send(confirm); show("logins"); }
      catch (e) {
        if (!confirm && confirmable(e)) {  // a login of that kind came meanwhile: asked now
          askRefused(o.row, e, async () => { try { await send(true); show("logins"); } catch (err) { await refused(err); } }, { trigger: o.trigger });
          return;
        }
        await refused(e);
      }
    });
    const checked = (confirm) => (o.question ? confirmIn(o.row, o.question, t("Yes, add it"), () => add(true), { kind: "primary", trigger: o.trigger }) : add(confirm));
    return busy(o.trigger, async () => {
      try { await api("POST", hostPath(o.path, host), Object.assign({}, o.body, { check_only: true })); }
      catch (e) {
        if (confirmable(e)) { askRefused(o.row, e, () => checked(true), { trigger: o.trigger }); return; }
        o.result.append(errorNotice(e, t("The login was not added; nothing was stored")));
        return;
      }
      checked(false);
    });
  };

  await section(t("Git logins (every sandbox's proxy)"), "/git-logins", (box, list) => {
    const rows = (Array.isArray(list) ? list : []).filter(x => x && x.url).map(x => {
      const actions = el("td", { class: "actions" });
      // Edit…: its server, its user, which secret holds the token (git-logins edit); the token itself is that
      // secret's value, replaced on the page Secrets - never shown here
      actions.append(rowButton(t("Edit…"), x.url, host, { onclick: () => rowEditor(gitEditor, t("Edit the Git login for {url}", { url: x.url }),
        [{ key: "new_url", label: t("Server"), value: x.url }, { key: "user", label: t("User"), value: x.user || "" }, { key: "secret", label: t("Secret (its name)"), value: x.secret || "", kind: "secret" }],
        (v, row, result, trigger) => {
          const body = { url: x.url };
          if (v.new_url && v.new_url !== x.url) body.new_url = v.new_url;
          if (v.user && v.user !== (x.user || "")) body.user = v.user;
          if (v.secret && v.secret !== (x.secret || "")) body.secret = v.secret;
          if (Object.keys(body).length === 1) { clear(result).append(el("p", { class: "hint" }, t("Nothing changed."))); return; }
          confirmIn(row, t("Change the Git login for {url} on {host}? It is for the starts from now on: a sandbox that runs keeps the login it was started with (its Network card gives it the new one).", { url: x.url, host }), t("Yes, change it"), async () => {
            try { const r = await api("POST", hostPath("/git-logins/edit", host), Object.assign({ confirm: true }, body)); toast("success", t("The Git login for {url} was changed on {host}", { url: x.url, host }), (r && r.output) || ""); show("logins"); }
            catch (e) { clear(result).append(errorNotice(e, t("The login was not changed"))); }
          }, { trigger });
        }, host) }));
      actions.append(removeButton(actions, t("Remove the Git login for {url} on {host}? Sandboxes no longer push there with it.", { url: x.url, host }), "/git-logins/remove", { url: x.url }, x.url));
      return el("tr", {}, el("td", { class: "mono" }, x.url), el("td", { class: "mono" }, x.user || ""), el("td", { class: "mono" }, x.secret || ""), actions);
    });
    const gitEditor = el("div");
    const url = el("input", { id: "git-url", class: "mono", placeholder: "https://git.example.org" }), user = el("input", { id: "git-user", class: "mono", placeholder: "x-access-token" });
    const token = tokenSource("git", () => gitTokenName(url.value));
    url.addEventListener("input", token.refresh);
    const row = el("div", { class: "row" }), result = el("div", { class: "wide check-result" });
    const add = (event) => token.ready(row, event.target, (secret, replace) => {
      const body = { url: url.value.trim(), secret, user: user.value.trim() };
      const question = t("Give every sandbox on {host} that reaches {url} the Git login of the secret {secret}? The connection proxy adds it; the token never enters a sandbox.", { host, url: body.url, secret });
      addLogin({ token, row, result, path: "/git-logins", body, secret, replace, trigger: event.target,
        question: replace === null ? question : `${question} ${t("The token is stored first as the secret {secret} on {host}, for good.", { secret, host })}` });
    });
    row.append(el("button", { class: "primary", onclick: add }, t("Add…")), el("button", { onclick: check("/git-logins/check", () => ({ url: url.value.trim() })) }, t("Check")));
    clear(box).append(table([t("Server"), t("User"), t("Secret"), ""], rows, t("No Git logins on this host.")), gitEditor, el("div", { class: "card" }, el("div", { class: "form" },
      el("label", { for: "git-url" }, t("Server")), url, el("label", { for: "git-user" }, t("User")), user, ...token.fields, el("span"), row, result)));
  });

  await section(t("Publish logins (with --publish only)"), "/publish-logins", (box, list) => {
    const rows = (Array.isArray(list) ? list : []).filter(x => x && x.kind).map(x => {
      const actions = el("td", { class: "actions" });
      actions.append(rowButton(t("Edit…"), x.kind, host, { onclick: () => rowEditor(publishEditor, t("Edit the publish login {kind}", { kind: x.kind }),
        [{ key: "secret", label: t("Secret (its name)"), value: x.secret || "", kind: "secret" }, { key: "url", label: t("Another server (optional)"), value: "", placeholder: `https://${x.host || ""}` }],
        (v, row, result, trigger) => {
          const body = {};
          if (v.secret && v.secret !== (x.secret || "")) body.secret = v.secret;
          if (v.url) body.url = v.url;
          if (!Object.keys(body).length) { clear(result).append(el("p", { class: "hint" }, t("Nothing changed."))); return; }
          confirmIn(row, t("Change the publish login {kind} on {host}? It is for the starts from now on: a sandbox that runs keeps the login it was started with until it is restarted.", { kind: x.kind, host }), t("Yes, change it"), async () => {
            try { const r = await api("POST", hostPath(`/publish-logins/${encodeURIComponent(x.kind)}/edit`, host), Object.assign({ confirm: true }, body)); toast("success", t("The publish login {kind} was changed on {host}", { kind: x.kind, host }), (r && r.output) || ""); show("logins"); }
            catch (e) { clear(result).append(errorNotice(e, t("The login was not changed"))); }
          }, { trigger });
        }, host) }));
      actions.append(removeButton(actions, t("Remove the publish login {kind} on {host}?", { kind: x.kind, host }), `/publish-logins/${encodeURIComponent(x.kind)}/remove`, {}, x.kind), notWhileRunning("publish", x.kind, host));
      return el("tr", {}, el("td", { class: "mono" }, x.kind), el("td", { class: "mono" }, x.host || ""), el("td", { class: "mono" }, x.secret || ""), actions);
    });
    const publishEditor = el("div");
    const kind = el("select", { id: "publish-kind" }, PUBLISH_KINDS.map(k => el("option", { value: k }, k)));
    const url = el("input", { id: "publish-url", class: "mono", placeholder: "https://npm.example.org" });
    const token = tokenSource("publish", () => PUBLISH_TOKEN_NAMES[kind.value] || "PUBLISH_TOKEN");
    kind.addEventListener("change", token.refresh);
    const row = el("div", { class: "row" }), result = el("div", { class: "wide check-result" });
    const add = (event) => token.ready(row, event.target, (secret, replace) => addLogin({ token, row, result, path: "/publish-logins",
      body: { kind: kind.value, secret, url: url.value.trim() }, secret, replace, trigger: event.target }));
    row.append(el("button", { class: "primary", onclick: add }, t("Add")),
      el("button", { onclick: check("/publish-logins/check", () => ({ kind: kind.value })) }, t("Check")),
      el("button", { onclick: (event) => busy(event.target, async () => { try { clear(result).append(outputBlock(await api("GET", hostPath("/publish-logins/kinds", host)))); } catch (e) { clear(result).append(errorNotice(e)); } }) }, t("What each kind opens")));
    clear(box).append(table([t("Kind"), t("Host"), t("Secret"), ""], rows, t("No publish logins on this host.")), publishEditor, el("div", { class: "card" }, el("div", { class: "form" },
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
      actions.append(remove, notWhileRunning("identities", name, host));
      for (const g of Array.isArray(x.git) ? x.git : []) actions.append(removeButton(actions, t("Remove the Git login {url} of {name} on {host}?", { url: g.url, name, host }), `/identities/${encodeURIComponent(name)}/logout`, { url: g.url }, `${name} ${g.url}`));
      for (const p of Array.isArray(x.publish) ? x.publish : []) actions.append(removeButton(actions, t("Remove the publish login {kind} of {name} on {host}?", { kind: p.kind, name, host }), `/identities/${encodeURIComponent(name)}/unpublish`, { kind: p.kind }, `${name} ${p.kind}`));
      const logins = [...(Array.isArray(x.git) ? x.git.map(g => g.url) : []), ...(Array.isArray(x.publish) ? x.publish.map(p => p.kind) : [])].join(", ");
      return el("tr", {}, el("td", { class: "mono" }, name), el("td", {}, [x.name, x.email].filter(Boolean).join(" ")), el("td", {}, x.description || ""), el("td", { class: "mono" }, logins), actions);
    });
    const ident = el("input", { id: "identity-name", class: "mono", placeholder: "work" }), person = el("input", { id: "identity-person", placeholder: "Ann Example" });
    const email = el("input", { id: "identity-email", class: "mono", placeholder: "ann@example.org" }), description = el("input", { id: "identity-description" });
    const loginUrl = el("input", { id: "identity-url", class: "mono", placeholder: "https://github.com" }), loginUser = el("input", { id: "identity-user", class: "mono" });
    const loginSecret = offerNames(el("input", { id: "identity-secret", class: "mono", placeholder: "GH_WORK" }), "secret", host);
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
    const badge = presetExperimental(found) || presetExperimental(p);
    clear(detail).append(el("div", { class: "card" }, el("h2", {}, `${found.id || p.id}: ${found.title || ""}`), el("p", {}, found.description || ""),
      badge ? el("p", { class: "hint" }, experimentalText(badge)) : null,
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
    el("tr", {}, el("td", { class: "mono" }, p.id), el("td", {}, presetTitle(p), experimentalBadge(p) ? " " : null, experimentalBadge(p)), el("td", {}, p.origin || ""), el("td", { class: "actions" }, rowButton(t("Show"), p.id, host, { onclick: () => open(p) })))), t("No presets on this host.")), detail);
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
    if (team.damaged) {
      // its record cannot be read: only Delete (team delete --yes looks its members up by every agent's name)
      const members = AGENTS.map(a => `${team.name}-${a}`).join(", ");
      const remove = rowButton(t("Delete…"), team.name, host, { class: "danger", onclick: () => confirmIn(actions, t("The record of the team {team} on {host} is damaged. Delete it with every member of its name ({members}), their HOMEs and its board? What is in those HOMEs is gone for good.", { team: team.name, host, members }), t("Yes, delete it"), async () => {
        try { const r = await api("DELETE", hostPath(`/teams/${encodeURIComponent(team.name)}`, host), { confirm: true }); toast("success", t("Team {team} on {host} deleted", { team: team.name, host }), (r && r.output) || ""); show("teams"); }
        catch (e) { failed(t("Could not delete the team {team} on {host}", { team: team.name, host }))(e); }
      }, { trigger: remove }) });
      actions.append(remove);
      rows.push(el("tr", { "data-team": team.name }, el("td", { class: "mono" }, team.name), el("td", { class: "hint" }, t("damaged: its record cannot be read")), el("td", {}, ""), actions), slot);
      continue;
    }
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
  // a new team, started in a terminal of the page: its members (team.AGENTS) and that host's VPN profiles with their
  // type (vpn list --json there), as the app's team dialog has them (team-choices). Whether one profile may run in
  // several members at once is team start's to say: it refuses with its reason, shown in the terminal, and starts
  // nothing; the page only offers the OpenVPN confirmation where one OpenVPN profile is in several members.
  let choices;
  try { choices = await api("GET", hostPath("/team-choices", host)); } catch (e) { choices = e; }
  if (!live()) return;
  const listed = choices && !(choices instanceof Error) && Array.isArray(choices.agents) ? choices.agents.filter(a => typeof a === "string" && /^[a-z][a-z0-9-]{0,19}$/.test(a)) : [];
  const teamAgents = listed.length ? listed : AGENTS;
  const profiles = (choices && Array.isArray(choices.profiles) ? choices.profiles : []).filter(p => p && typeof p.profile === "string");
  const vpnKind = (p) => (typeof p.provider === "string" && p.provider && !["generic", "?"].includes(p.provider) ? p.provider : String(p.type || "?"));
  const vpnSelect = (id, empty, label) => el("select", { id, "aria-label": label, onchange: () => changed() }, el("option", { value: "" }, empty),
    profiles.map(p => el("option", { value: p.profile }, `${p.profile} (${vpnKind(p)})`)));
  const name = el("input", { id: "team-name", class: "mono", placeholder: "review", pattern: TEAM_NAME.source.slice(1, -1) });
  const lead = el("select", { id: "team-lead", onchange: () => changed() }, teamAgents.map(a => el("option", { value: a }, a)));
  const others = teamAgents.map(a => el("input", { type: "checkbox", id: `team-agent-${a}`, value: a, checked: a === "codex", onchange: () => changed() }));
  const workspace = el("input", { id: "team-workspace", class: "mono", placeholder: "/home/you/src/project" });
  const vpn = vpnSelect("team-vpn", t("(none)"), t("VPN profile"));
  vpn.setAttribute("title", t("The VPN in front of every member: each member is a sandbox of its own with its own VPN container (sandbox-ai team start --vpn)"));
  // Per member (advanced): another profile for a member (team start --member-vpn AGENT=PROFILE), a row for each
  // agent of the team
  const memberVpn = Object.fromEntries(teamAgents.map(a => [a, vpnSelect(`team-vpn-${a}`, t("(the team's)"), t("VPN profile of {agent}", { agent: a }))]));
  // ... and each member's role (added to its first prompt: team start --role AGENT=TEXT) and its own model, LLM server
  // and security mode (--member-model, --member-llm, --member-mode: in place of the team's for it); team start checks
  // their values and says what it refuses in the terminal
  const memberRole = Object.fromEntries(teamAgents.map(a => [a, el("input", { id: `team-role-${a}`, maxlength: 1000, placeholder: t("Role, e.g. reviews every change"), "aria-label": t("Role of {agent}", { agent: a }) })]));
  const memberModel = Object.fromEntries(teamAgents.map(a => [a, el("input", { id: `team-model-${a}`, class: "mono", placeholder: t("Model (the team's)"), "aria-label": t("Model of {agent}", { agent: a }) })]));
  const memberLlm = Object.fromEntries(teamAgents.map(a => [a, el("input", { id: `team-llm-${a}`, class: "mono", placeholder: t("LLM server (the team's)"), "aria-label": t("LLM server of {agent}", { agent: a }) })]));
  const memberMode = Object.fromEntries(teamAgents.map(a => [a, el("select", { id: `team-mode-${a}`, "aria-label": t("Mode of {agent}", { agent: a }) }, el("option", { value: "" }, t("Mode (the team's)")), ORDER.mode.map(m => el("option", { value: m }, m)))]));
  const memberRows = Object.fromEntries(teamAgents.map(a => [a, el("div", { class: "row", id: `team-vpn-row-${a}` }, el("label", { for: `team-vpn-${a}`, class: "mono" }, a), memberVpn[a], memberRole[a], memberModel[a], memberLlm[a], memberMode[a])]));
  const perMember = el("details", { id: "team-per-member", class: "wide" }, el("summary", {}, t("Per member (advanced)")),
    el("p", { class: "hint" }, t("Another VPN profile for a member (sandbox-ai team start --member-vpn AGENT=PROFILE)")),
    el("p", { class: "hint" }, t("A member's role is added to its first prompt; its own model, LLM server and mode replace the team's for it (team start --role, --member-model, --member-llm, --member-mode).")), Object.values(memberRows));
  const shared = el("input", { type: "checkbox", id: "team-shared-openvpn", onchange: () => changed() });
  const sharedRow = el("label", { class: "wide" }, shared, " ", t("Allow one OpenVPN profile in several members (your server must allow it: duplicate-cn)"));
  const options = el("textarea", { id: "team-options", rows: 2, class: "mono", placeholder: "--mode high\n--allow-pypi" });
  const status = el("div", { class: "wide" });
  const chosen = () => [lead.value, ...others.filter(b => b.checked && b.value !== lead.value).map(b => b.value)];
  const own = () => Object.fromEntries(chosen().filter(a => memberVpn[a] && memberVpn[a].value).map(a => [a, memberVpn[a].value]));
  const changed = () => {
    const agents = chosen(), mine = own(), team = vpn.value || "";
    for (const [a, row] of Object.entries(memberRows)) row.hidden = !agents.includes(a);
    const used = agents.map(a => mine[a] || team).filter(Boolean);
    sharedRow.hidden = !used.some((p, i) => used.indexOf(p) !== i && (profiles.find(x => x.profile === p) || {}).type === "openvpn");
  };
  changed();
  const start = el("button", { class: "primary", onclick: () => {
    clear(status);
    const team = name.value.trim(), path = workspace.value.trim();
    const agents = chosen();
    if (!TEAM_NAME.test(team)) { status.append(notice(t("A team's name: 1-20 of a-z, 0-9, '_' and '-', starting with a letter or digit."), "error")); return; }
    if (agents.length < 2) { status.append(notice(t("A team has two or more agents: the lead and at least one more."), "error")); return; }
    if (!path.startsWith("/") && !path.startsWith("~")) { status.append(notice(t("The shared workspace: an absolute path on {host}.", { host }), "error")); return; }
    let list;
    try { list = shellWords(options.value || ""); } catch (e) { status.append(notice(t("Options: {error}", { error: e.message }), "error")); return; }
    changed();
    const mine = own(), query = { kind: "team-start", host, team, agents: agents.join(","), workspace: path, options: JSON.stringify(list) };
    if (vpn.value) query.vpn = vpn.value;
    if (Object.keys(mine).length) query.member_vpn = JSON.stringify(mine);
    if (!sharedRow.hidden && shared.checked) query.shared_openvpn = "1";
    const typed = (w) => String((w && w.value) || "").trim();
    const roles = Object.fromEntries(agents.map(a => [a, typed(memberRole[a])]).filter(([, v]) => v));
    const memberOptions = Object.fromEntries(agents.map(a => [a, Object.fromEntries([["model", memberModel[a]], ["llm", memberLlm[a]], ["mode", memberMode[a]]].map(([k, w]) => [k, typed(w)]).filter(([, v]) => v))]).filter(([, v]) => Object.keys(v).length));
    if (Object.keys(roles).length) query.roles = JSON.stringify(roles);
    if (Object.keys(memberOptions).length) query.member_options = JSON.stringify(memberOptions);
    openTerminal(query, t("team {team}", { team }));
  } }, t("Start the team in a terminal"));
  const listing = choices instanceof Error ? errorNotice(choices)
    : choices && choices.error ? notice(t("Could not list the VPN profiles: {error}", { error: String(choices.error) }), "warn") : null;
  main.append(el("h2", {}, t("New team on {host}", { host })), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "team-name" }, t("Name")), name,
    el("label", { for: "team-lead" }, t("Lead")), lead,
    el("span", {}, t("Members")), el("div", { class: "row" }, others.map((b, i) => el("label", {}, b, " ", teamAgents[i]))),
    el("label", { for: "team-workspace" }, t("Shared workspace")), workspace,
    el("label", { for: "team-vpn" }, t("VPN profile")), vpn,
    listing ? el("div", { class: "wide" }, listing) : null,
    perMember, sharedRow,
    el("p", { class: "hint wide" }, t("Each member is a sandbox of its own with its own VPN container. Whether one profile can run in several members at once, team start checks: if not, it says why in the terminal and starts nothing.")),
    el("label", { for: "team-options" }, t("Options for every member")), options,
    status, el("span"), el("div", { class: "row" }, start, el("span", { class: "hint" }, t("The members start one after another; the terminal says how each went.")))),
  el("p", { class: "hint" }, t("The members share the workspace: nothing keeps two agents from changing the same file."))));
};

// Images and tools: the images (sandbox-ai images) with their extensions and variants (packages, files, a
// Containerfile fragment; checked, changed, removed), a build in a terminal, the demo device.
const BUILD_TARGETS = ["claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi", "all", "team", "cairn", "vpn", "tor", "browser"];
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
  const variantName = el("input", { id: "image-new-name", class: "mono", placeholder: "data", pattern: "(?=.{1,30}$)[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*" });
  const variantFrom = el("select", { id: "image-new-from" }, AGENTS.map(a => el("option", { value: a }, a)));
  const empty = el("input", { id: "image-new-empty", type: "checkbox" });
  const recipe = el("select", { id: "image-recipe" }, [["media", N_("media: ffmpeg, PulseAudio, espeak-ng, Piper …")], ["android", N_("android: Android debugging - adb, apktool, smali, jadx, Frida")], ["android-dev", N_("android-dev: Android development - everything of android, the SDK for API 37, 36 and 35, NDK, emulator, Gradle, Kotlin (about 4.1 GB; building it accepts Google's Android SDK license)")]].map(([v, label]) => el("option", { value: v }, t(label))));
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
  // a build in a terminal: the agents, all, team, vpn, tor, and the variants (images --json: "target", "variant": true)
  const variants = listed.filter(x => x.variant && !BUILD_TARGETS.includes(x.target)).map(x => x.target);
  const buildTarget = el("select", { id: "build-target" }, [...BUILD_TARGETS, ...new Set(variants)].map(v => el("option", { value: v }, v)));
  const only = el("input", { id: "build-only", type: "checkbox" });
  const pin = el("input", { id: "build-pin", class: "mono", placeholder: "1.2.3", size: 10, value: "" });
  const uv = el("input", { id: "build-uv", class: "mono", placeholder: t("latest"), size: 12, value: "" }), node = el("input", { id: "build-node", class: "mono", placeholder: "node@sha256:…", value: "" });
  main.append(el("h2", {}, t("Build")), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "build-target" }, t("Image")), buildTarget, el("label", { for: "build-only" }, t("Only the extension layers")), only,
    el("label", { for: "build-pin" }, t("Agent version (one agent only)")), pin,
    el("label", { for: "build-uv" }, t("uv image tag")), uv, el("label", { for: "build-node" }, t("Node.js base image")), node,
    el("span"), el("button", { class: "primary", onclick: () => openTerminal({ kind: "build", host, target: buildTarget.value, extension_only: only.checked ? "1" : "",
      pin: textOf(pin), uv: textOf(uv), node: textOf(node) }, t("build {target}", { target: buildTarget.value })) }, t("Build in a terminal"))),
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
const ACTIVITY_KINDS = ["prompt", "reply", "tool", "command", "file", "network", "approval", "mcp", "session", "tokens", "resources"];
// What the agents did (sandbox-ai activity --json): of a start of that host (a session by its name, a run by its
// name), a project (its default log directory), a log directory, or a sandbox HOME's transcripts. Never the
// directory the service (or the SSH login) runs in: with nothing chosen it shows the first running start's (else
// the first start's), or asks for one.
pages.activity = async (main) => {
  const host = state.host;
  main.append(el("h1", {}, t("Activity on {host}", { host })), el("p", { class: "hint" }, t("Loading…")));
  const live = pageLive();
  let starts = [];
  try { starts = await api("GET", hostPath("/starts", host)); } catch (e) { starts = []; }
  if (!live()) return;
  starts = (Array.isArray(starts) ? starts : []).filter(x => x && typeof x.name === "string" && x.kind !== "mcp" && x.state !== "damaged");
  const first = starts.find(x => x.state === "running") || starts[0];
  const start = el("select", { id: "activity-start", class: "mono" }, el("option", { value: "" }, starts.length ? t("none") : t("no start on {host}", { host })),
    starts.map(x => el("option", { value: x.name, selected: x === first }, `${x.name} (${stateText(x.state)})`)));
  const where = el("select", { id: "activity-where" }, [["", N_("none")], ["project", N_("a project (its default log directory)")], ["log_dir", N_("a log directory")], ["home", N_("a sandbox HOME (its transcripts)")]]
    .map(([v, label]) => el("option", { value: v }, t(label))));
  start.value = first ? first.name : ""; where.value = "";  // what is shown first (a browser takes it from the options too)
  const path = el("input", { id: "activity-path", class: "mono", placeholder: "/home/you/src/project", value: "" });
  const run = el("input", { id: "activity-run", class: "mono", placeholder: t("every run there"), value: "" }), tail = el("input", { id: "activity-tail", type: "number", min: 1, max: 2000, value: 100 });
  const grep = el("input", { id: "activity-grep", value: "" }), since = el("input", { id: "activity-since", placeholder: "2h", value: "" });
  const kinds = ACTIVITY_KINDS.map(k => el("input", { type: "checkbox", id: `activity-${k}`, value: k }));
  const follow = el("input", { id: "activity-follow", type: "checkbox" });
  const box = el("div");
  // which one is read: a path given (with its kind), else the start chosen (a session by --session, a run by its
  // name), else nothing
  const source = () => {
    const value = path.value.trim();
    if (where.value && value) return { [where.value]: value };
    const chosen = starts.find(x => x.name === start.value);
    if (!chosen) return null;
    return chosen.directory ? { session: chosen.name } : { run: chosen.name };
  };
  const load = async () => {
    const from = source();
    if (!from) { clear(box).append(notice(t("Choose a start, or a project, log directory or sandbox HOME on {host}.", { host }), "info")); return; }
    const body = Object.assign({ tail: Number(tail.value) || 100, kinds: kinds.filter(k => k.checked).map(k => k.value), grep: grep.value.trim(), since: since.value.trim() }, from);
    if (run.value.trim() && !from.run) body.run = run.value.trim();
    let r;
    try { r = await api("POST", hostPath("/activity", host), body, { background: follow.checked }); }
    catch (e) { clear(box).append(/no activity|no log directory|no directory/.test(String(e && e.message)) ? notice(t("No activity recorded there yet: {error}", { error: errorText(e) }), "info") : errorNotice(e)); return; }
    const events = Array.isArray(r.events) ? r.events : [];
    clear(box).append(table([t("Time"), t("Run"), t("Kind"), t("What")], events.slice().reverse().map(e => el("tr", {}, el("td", {}, String(e.time || "").replace("T", " ").replace("Z", "")),
      el("td", { class: "mono" }, String(e.run || "")), el("td", {}, String(e.kind || "")), el("td", { class: "mono" }, String(e.summary || e.text || e.command || e.path || JSON.stringify(e))))), t("No activity found.")));
  };
  const tick = () => { if (state.page !== "activity" || state.host !== host || !box.isConnected) return; if (follow.checked) load().finally(() => setTimeout(tick, 5000)); else setTimeout(tick, 5000); };
  clear(main).append(el("h1", {}, t("Activity on {host}", { host })), el("div", { class: "card" }, el("div", { class: "form" },
    el("label", { for: "activity-start" }, t("noun|Start")), start,
    el("label", { for: "activity-where" }, t("Or read")), el("div", { class: "row" }, where, path),
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
  starts: ["sessions", "sandbox", "security-modes", "workspaces-mounts", "network", "presets", "limits", "devices", "scheduled-runs"],
  approvals: ["approvals", "taint", "network", "proxy"], terminals: ["sessions"], doctor: ["sandbox", "releases-update"],
  logs: ["logs", "privacy-check"], llm: ["llm"], vpn: ["vpn-tor"], kits: ["kits"], canaries: ["canaries"], secrets: ["secrets"],
  logins: ["secrets", "proxy"], projects: ["workspaces-mounts", "rulesets", "policy"], mcp: ["mcp"], presets: ["presets"], teams: ["teams", "mcp"],
  images: ["display", "devices", "releases-update"], activity: ["logs", "memory-watch"], settings: ["security-modes", "canaries", "policy", "logs", "phone-notifications"],
  hosts: ["hosts", "web", "releases-update"], monitor: ["monitoring", "memory-watch", "limits"],
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
