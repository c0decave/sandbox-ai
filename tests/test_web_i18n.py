"""The web page in English and German (sandbox/web/i18n.js): every text of the page has its German entry with the
same placeholders, no English text of the page is left outside t(), and in node the page is drawn in German and
switched back to English. Nothing here writes a login (HOME is moved for the session by conftest.py)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "sandbox" / "web"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


# --- a small lexer for app.js: its string and template literals, each with the call it is an argument of ---------

class Literal:
    def __init__(self, text, kind, start, frame, before, after):
        self.text, self.kind, self.start, self.frame, self.before, self.after = text, kind, start, frame, before, after

    @property
    def key(self):  # the first argument of t() or N_(): a key of the catalog
        return self.frame is not None and self.frame["open"] == "(" and self.frame["callee"] in ("t", "N_") \
            and self.frame["arg"] == 0


def unquote(raw):
    """The text of a quoted JavaScript string (the escapes app.js uses: \\n, \\", \\', \\\\, \\uXXXX)."""
    return re.sub(r"\\(u[0-9a-fA-F]{4}|.)", lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] == "u" and
                  len(m.group(1)) == 5 else {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), raw)


REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {"", "return", "typeof", "case", "===", "!==", "==", "!=", "=>", "<=", ">=",
                                                "&&", "||", "??"}
OPERATOR = re.compile(r"===|!==|==|!=|=>|<=|>=|&&|\|\||\?\?|\?\.|\.\.\.|\+\+|--")


def literals(src):
    """Every string literal and static part of a template literal of SRC, outside comments, as Literal objects
    (FRAME: the innermost open bracket, with the callee of a call and the argument's index)."""
    found, frames, tokens = [], [], []  # tokens: the last significant ones, for the context of a literal
    i, n = 0, len(src)
    template_depth = []  # brace depth in frames where a template's ${ was opened

    def frame():
        return frames[-1] if frames else None

    def add_literal(text, kind, start, end):
        found.append(Literal(text, kind, start, dict(frame()) if frames else None, tokens[-1] if tokens else "", end))
        tokens.append("literal")

    def next_significant(j):
        while j < n and src[j] in " \t\r\n":
            j += 1
        return src[j:j + 3]

    def read_template(j):
        """From the char after a backtick: its static parts as literals; at ${ the expression is lexed as code."""
        start = j
        chunk = []
        while j < n:
            c = src[j]
            if c == "\\":
                chunk.append(src[j:j + 2]); j += 2; continue
            if c == "`":
                add_literal("".join(chunk), "template", start, next_significant(j + 1)); return j + 1, False
            if c == "$" and src[j + 1:j + 2] == "{":
                add_literal("".join(chunk), "template", start, "${")
                frames.append({"open": "${", "callee": None, "arg": 0})
                template_depth.append(len(frames))
                return j + 2, True
            chunk.append(c); j += 1
        raise AssertionError("a template literal is not closed")

    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1; continue
        if src.startswith("//", i):
            i = src.find("\n", i) if src.find("\n", i) >= 0 else n; continue
        if src.startswith("/*", i):
            i = src.index("*/", i) + 2; continue
        if c in "\"'":
            j, start = i + 1, i
            while src[j] != c:
                j += 2 if src[j] == "\\" else 1
            text = unquote(src[i + 1:j])
            add_literal(text, "string", start, next_significant(j + 1))
            i = j + 1; continue
        if c == "`":
            i, _ = read_template(i + 1); continue
        if c == "}" and template_depth and template_depth[-1] == len(frames) and frames[-1]["open"] == "${":
            frames.pop(); template_depth.pop()
            i, _ = read_template(i + 1); continue
        if c == "/" and (tokens[-1] if tokens else "") in REGEX_BEFORE:  # a regex literal
            j, in_class = i + 1, False
            while src[j] != "/" or in_class:
                if src[j] == "\\":
                    j += 1
                elif src[j] == "[":
                    in_class = True
                elif src[j] == "]":
                    in_class = False
                j += 1
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            tokens.append("regex"); i = j; continue
        m = OPERATOR.match(src, i)
        if m:
            tokens.append(m.group()); i = m.end(); continue
        m = re.match(r"[A-Za-z_$][\w$]*", src[i:])
        if m:
            tokens.append(m.group()); i += len(m.group()); continue
        m = re.match(r"\d[\w.]*", src[i:])
        if m:
            tokens.append("number"); i += len(m.group()); continue
        if c in "([{":
            callee = None
            if c == "(" and tokens and re.fullmatch(r"[A-Za-z_$][\w$]*", tokens[-1]) and \
                    tokens[-1] not in ("if", "for", "while", "switch", "catch", "return", "typeof"):
                callee = tokens[-1] if len(tokens) < 2 or tokens[-2] not in (".", "?.") else "." + tokens[-1]
            frames.append({"open": c, "callee": callee, "arg": 0})
        elif c in ")]}":
            frames.pop()
        elif c == "," and frames:
            frames[-1]["arg"] += 1
        tokens.append(c); i += 1
    assert not frames, "brackets do not match"
    return found


APP = (WEB / "app.js").read_text()
I18N = (WEB / "i18n.js").read_text()
HTML = (WEB / "index.html").read_text()
LITERALS = literals(APP)


def catalog():
    """TEXTS of i18n.js, read as JSON (its entries are written so: double quotes, comment lines of their own)."""
    body = I18N.split("const TEXTS = {", 1)[1].split("\n};", 1)[0]
    body = "\n".join(line for line in body.splitlines() if not line.strip().startswith("//"))
    return json.loads("{" + body + "}")


def keys_of_the_page():
    keys = {}
    for lit in LITERALS:
        if lit.key:
            assert lit.kind == "string" and lit.after[:1] in (")", ","), \
                f"a key is one plain string literal, not built: {lit.text!r}"
            keys.setdefault(lit.text, lit.start)
    for key in re.findall(r'data-i18n="([^"]*)"', HTML):
        keys.setdefault(key, None)
    return keys


def test_the_lexer_sees_the_page_as_it_is():
    assert len(LITERALS) > 1000 and sum(lit.key for lit in LITERALS) > 400
    sample = literals('t("a {b}", { b: "x" }); el("p", {}, `n ${t("k")} m`, "done") // "comment"\nx = /"/g; y("z")')
    assert [(lit.text, lit.key) for lit in sample] == [("a {b}", True), ("x", False), ("p", False), ("n ", False),
                                                       ("k", True), (" m", False), ("done", False), ("z", False)]


def test_every_text_of_the_page_has_its_german_entry_with_the_same_placeholders():
    texts, keys = catalog(), keys_of_the_page()
    missing = sorted(key for key in keys if not (texts.get(key) or {}).get("de"))
    assert not missing, f"texts without a German entry in i18n.js: {missing}"
    for key, entry in texts.items():
        assert set(entry) == {"de"} and entry["de"].strip(), key
        assert sorted(PLACEHOLDER.findall(key)) == sorted(PLACEHOLDER.findall(entry["de"])), (key, entry["de"])
    themes = {theme.get("label") for theme in json.loads((WEB / "themes.json").read_text())["themes"].values()}
    unused = sorted(set(texts) - set(keys) - themes)
    assert not unused, f"entries of i18n.js no text of the page uses: {unused}"
    assert len(keys) > 400


def test_each_placeholder_of_a_text_is_given_where_it_is_shown():
    """t("… {host} …", { host }) : each {name} of a key is named in the values passed with it (an N_() key is filled
    where it is shown, with its own t())."""
    for match in re.finditer(r'(?<![\w.$])t\(\s*("(?:[^"\\]|\\.)*")', APP):
        key = json.loads(match.group(1))
        names = set(PLACEHOLDER.findall(key))
        if not names:
            continue
        depth, j = 1, match.end()
        while depth:  # to the end of this call (strings in the values hold no unbalanced brackets)
            depth += {"(": 1, ")": -1}.get(APP[j], 0)
            j += 1
        values = APP[match.end():j - 1]
        for name in names:
            assert re.search(rf"\b{name}\b", values), (key, name, values)


# The literals outside t() that look like words but are not text to translate: values sent to the server or compared
# with what it sends, selectors, examples in placeholders (names), and names that are the same in German.
NOT_WORDS = {
    "sandbox-ai", " sandbox-ai", "(prefers-color-scheme: dark)", "nav button", "#toasts .toast",
    'nav button[data-page="approvals"]', "noopener noreferrer", "use strict",
    "home", "lmstudio", "my-kit", "release-notes", "ct101", "exit-node-1", "se-sto", "VPN_HOME", "Proxmox CT 101",
    "pypi.org api.example.org", "GITEA_TOKEN", "button, input, select, textarea",
    "HTTPS (TCP/443)", "IP:PORT (TCP)", "CPUs", "SSH", "URL", "ok",
    # examples in the placeholders of the CLI's other commands, and names of programs
    "files", "Podman", "Bubblewrap", "sandbox-ai-home", "TOR_BRIDGES", "de,nl", "proton", "X-Gateway-Key=GW_KEY",
    "PreToolUse", "Bash|Write", "hub:binwalk", "VT_API_KEY", "egm", "x-access-token", "work", "Ann Example",
    "GH_WORK",
    # examples of the start form, the teams, the kits' marketplaces and the images (names, a Containerfile line)
    "my-plugins", "review", "ffmpeg jq", "RUN apt-get update && apt-get install -y --no-install-recommends tree", "data",
    # the launcher's own words a start's terminal is read for (never shown by the page)
    "agent-sandbox: /work is ",
}
# Where a literal is read by people even when it is one lowercase word: (callee, argument) of the page's helpers.
TEXT_POSITIONS = {("toast", 1), ("toast", 2), ("notice", 0), ("failed", 0), ("ask", 0), ("ask", 1), ("say", 0),
                  ("confirmIn", 1), ("confirmIn", 2), ("Error", 0), ("openTerminal", 1), ("Notification", 0),
                  ("table", 2), ("act", 0)}
ATTRIBUTE = re.compile(r'(title|placeholder|"aria-label"|alt)\s*:\s*([\w.]+\s*\?\s*)?$')
CODE_LIKE = re.compile(r"\s*(--?\w|/|~/|https?://|[\w.-]+@[\w.-]+|\d|sandbox-ai \w)")  # commands, paths: as typed


def css_classes():
    return set(re.findall(r"\.([A-Za-z][\w-]*)", (WEB / "app.css").read_text())) | {"hidden", "selected", "ended"}


def looks_like_text(text):
    """Words to read: two words or more, or one capitalised word (a label: Stop, Cancel)."""
    stripped = text.strip()
    return bool(re.search(r"[A-Za-z]{2,}[^A-Za-z\n]*\s[^A-Za-z\n]*[A-Za-z]{2,}", stripped)
                or re.fullmatch(r"[A-Z][a-z]{2,}[.…:?!]?", stripped))


def read_by_people(lit, src):
    """A child of el() (from its third argument on), a text argument of the page's helpers, an attribute people
    read (title, placeholder, aria-label, alt), or what is put into textContent."""
    frame = lit.frame or {}
    if frame.get("open") == "(" and (frame.get("callee") == "el" and frame["arg"] >= 2
                                     or (frame.get("callee"), frame["arg"]) in TEXT_POSITIONS):
        return True
    head = src[max(0, lit.start - 60):lit.start]
    return bool(ATTRIBUTE.search(head) or re.search(r"textContent\s*=\s*([\w.]+\s*\?\s*)?$", head))


def english_left(src, classes=frozenset()):
    """The literals of SRC outside t() that people read, as (literal, allowed by NOT_WORDS)."""
    for lit in literals(src):
        text = lit.text
        if lit.key or not re.search(r"[A-Za-z]{2,}", text):
            continue
        people = read_by_people(lit, src)
        if not (looks_like_text(text) or people):
            continue
        if lit.before in ("===", "!==") or lit.after.startswith(("===", "!==")):
            continue  # compared with what the server sends
        if lit.after[:1] == ":" and lit.before in ("{", ","):
            continue  # an object's key
        if not people and all(word in classes for word in text.split()):
            continue  # class names
        if CODE_LIKE.match(text):
            continue
        yield lit, text in NOT_WORDS


def doctor_titles_span():
    """Where app.js keeps the doctor's check titles by area (DOCTOR_PREFIXES, the desktop app's doctor_view.PREFIXES):
    compared with what a host's doctor names, never shown as the page's own text."""
    start = APP.index("const DOCTOR_PREFIXES = {")
    return start, APP.index("};", start)


def test_no_english_text_of_the_page_is_left_outside_t():
    start, end = doctor_titles_span()
    found = [(lit, allowed) for lit, allowed in english_left(APP, css_classes()) if not start <= lit.start < end]
    left = [f"app.js:{APP.count(chr(10), 0, lit.start) + 1}: {lit.text!r}" for lit, allowed in found if not allowed]
    assert not left, "English text outside t():\n" + "\n".join(left)
    used = {lit.text for lit, allowed in found if allowed}
    assert used == NOT_WORDS, f"no longer needed in NOT_WORDS: {sorted(NOT_WORDS - used)}"


def test_the_scan_finds_english_where_people_read_it():
    """The scan above, on a page that forgot t() in each of the usual places."""
    page = ('el("button", {}, "Stop"); el("span", { class: "hint" }, "nothing"); notice("expired", "warn"); '
            'x.textContent = open ? "Show links" : t("Hide links"); el("input", { placeholder: "optional" }); '
            'toast("error", `Could not stop ${name}`, ""); const M = { a: "this once" }; t("{n} ok", { n: "fine" }); '
            'if (r.state === "after a restart") el("td", { class: "mono hint" }, "--mode high");')
    assert [lit.text for lit, _ in english_left(page, css_classes())] == \
        ["Stop", "nothing", "expired", "Show links", "optional", "Could not stop ", "this once"]


def test_the_page_loads_its_texts_first_and_the_service_serves_them():
    import sandbox.webui as w
    assert HTML.index('src="/static/i18n.js"') < HTML.index('src="/static/app.js"')
    assert w.STATIC_FILES["i18n.js"].startswith("text/javascript")
    assert "script-src 'self'" in w.CSP, "a file of the service: no inline script, no other origin"
    # the language: chosen (kept in this browser, wrapped: storage may be refused), else the browser's
    assert "localStorage.getItem(LANGUAGE_STORE)" in I18N and "nav.language" in I18N
    assert I18N.count("try {") >= 2 and "document.documentElement.lang = LANGUAGE" in I18N
    assert "innerHTML" not in I18N and "innerHTML" not in APP.split("function languageSelect")[1].split("\n}")[0]
    assert 'languageSelect("header-language")' in APP and "setLanguage(event.target.value); render();" in APP


# --- in node: the page drawn in German, then switched to English ---------------------------------------------------

DRIVER = r"""
const fs = require("fs"), vm = require("vm"), path = require("path");
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this.listeners = {}; this.parent = null;
    this.className = ""; this.text = ""; this.hidden = false; this.disabled = false; this.dataset = {};
    const node = this; this.classList = { toggle(c, on) { const set = new Set(node.className.split(" ").filter(Boolean));
      if (on === undefined ? !set.has(c) : on) set.add(c); else set.delete(c); node.className = [...set].join(" "); return set.has(c); },
      add(c) { this.toggle(c, true); }, remove(c) { this.toggle(c, false); } }; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, f) { (this.listeners[type] = this.listeners[type] || []).push(f); }
  fire(type) { for (const f of this.listeners[type] || []) f({ target: this, preventDefault() {} }); }
  append(...nodes) { for (let n of nodes) { if (typeof n === "string") { const t = new Node("#text"); t.text = n; n = t; }
    if (n.parent) n.remove(); n.parent = this; this.children.push(n); } }
  prepend(n) { n.parent = this; this.children.unshift(n); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); this.parent = null; }
  removeChild(n) { n.remove(); return n; }
  replaceWith(n) { const i = this.parent.children.indexOf(this); n.parent = this.parent; this.parent.children[i] = n; this.parent = null; }
  focus() {}
  get parentNode() { return this.parent; }
  get firstChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1]; }
  get childNodes() { return this.children; }
  get isConnected() { let n = this; while (n.parent) n = n.parent; return n === body; }
  get textContent() { return this.tagName === "#text" ? this.text : this.children.map(c => c.textContent).join(""); }
  set textContent(v) { this.children = []; if (String(v) !== "") this.append(String(v)); }
  all() { const out = []; const walk = n => { out.push(n); n.children.forEach(walk); }; walk(this); return out; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) { const cls = sel.replace(/^\./, ""); return this.all().filter(n => n !== this && (n.className || "").split(" ").includes(cls)); }
}
const body = new Node("body"), app = new Node("div"); app.setAttribute("id", "app"); body.append(app);
const document = { body, title: "", createElement: t => new Node(t), createTextNode: t => { const n = new Node("#text"); n.text = t; return n; },
  getElementById: id => body.all().find(n => n.id === id) || null, querySelector: () => null,
  querySelectorAll: sel => (sel === "[data-i18n]" ? body.all().filter(n => n.attributes["data-i18n"]) : []),
  documentElement: { lang: "en", style: { setProperty() {} } } };
const stored = {}, storage = { getItem: k => (k in stored ? stored[k] : null), setItem: (k, v) => { stored[k] = String(v); }, removeItem: k => { delete stored[k]; } };
const answer = (method, url) => {
  if (url === "/api/me") return { user: "admin", csrf: "c" };
  if (url === "/api/hosts") return [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1" }];
  if (url === "/api/approvals") return { questions: [{ host: "ct1", id: "1-1", sandbox: "web", kind: "connect", question: "connect to pypi.org:443", seconds_left: 30, max_scope: "always" }], errors: {} };
  if (url.endsWith("/starts")) return [{ name: "web", agent: "claude", kind: "session", state: "running", keep: true, home: "kept", directory: "/d", attachable: true }];
  if (url.endsWith("/presets")) return [];
  return {};
};
const fetch = async (url, opts) => ({ status: 200, ok: true, json: async () => answer(opts.method, url) });
const context = vm.createContext({ document, Node, setTimeout: () => 0, clearTimeout() {}, setInterval: () => 0, clearInterval() {}, console,
  window: {}, location: { host: "x", hash: "" }, localStorage: storage, fetch, navigator: { language: process.argv[3] } });
const dir = path.dirname(process.argv[2]);
vm.runInContext(fs.readFileSync(path.join(dir, "i18n.js"), "utf8"), context);
vm.runInContext(fs.readFileSync(process.argv[2], "utf8").replace(/loadThemes\(\)\.then\(render\);\s*$/, ""), context);
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); };
const texts = () => body.all().filter(n => n.tagName === "button" || n.tagName === "h1" || n.tagName === "h2" || n.tagName === "th" || n.tagName === "label" || n.tagName === "option").map(n => n.textContent);
const button = (text) => body.all().find(n => n.tagName === "button" && n.textContent === text);
(async () => {
  const result = { lang: [document.documentElement.lang, vm.runInContext("LANGUAGE", context)] };
  await vm.runInContext("render()", context); await settle();
  result.frame = texts();
  result.labels = body.all().filter(n => n.attributes["aria-label"]).map(n => n.attributes["aria-label"]);
  button("Stoppen").fire("click");
  result.asked = body.all().find(n => n.className === "confirm").parent.textContent;
  result.placeholders = body.all().filter(n => n.attributes.placeholder).map(n => n.attributes.placeholder);
  result.toast = vm.runInContext('toast("warning", t("Done"), "<b>x</b>").textContent', context);
  result.filled = vm.runInContext('t("{name} on {host}", { name: "<img src=x>", host: "{host}" })', context);
  await vm.runInContext('state.page = "approvals"; pages.approvals(document.getElementById("main"))', context); await settle();
  result.approvals = texts();
  result.closed = vm.runInContext("closeText({ code: 1006, reason: 'gone' }, true)", context);
  // back to English: chosen in the header, remembered, the page drawn anew
  const select = body.all().find(n => n.tagName === "select" && n.id === "header-language");
  result.choices = select.children.map(o => [o.value, o.textContent, "selected" in o.attributes, o.attributes.lang]);
  select.value = "en"; select.fire("change"); await settle();
  result.after = [document.documentElement.lang, stored["sandbox-ai-language"], texts()];
  // a storage that refuses: the browser's language, and nothing breaks
  const refusing = vm.createContext({ document: { documentElement: {} }, localStorage: { getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); }, removeItem() { throw new Error("denied"); } },
    navigator: { language: "de-AT" } });
  vm.runInContext(fs.readFileSync(path.join(dir, "i18n.js"), "utf8"), refusing);
  result.refusing = [vm.runInContext("LANGUAGE", refusing), vm.runInContext('setLanguage("en")', refusing), vm.runInContext('t("Cancel")', refusing), refusing.document.documentElement.lang];
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_the_page_in_german_and_back_to_english(tmp_path):
    driver = tmp_path / "i18n.js"
    driver.write_text(DRIVER)
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), "de-DE"], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["lang"] == ["de", "de"], "German from the browser's language, set on the document"
    frame = result["frame"]
    for text in ("Starts und Sitzungen", "Freigaben", "Standards", "Abmelden", "Verbinden", "Pausieren", "Stoppen",
                 "Netzwerk…", "Ressourcen…", "Neuer Start auf local", "In einem Terminal starten", "Arbeitsordner", "Zustand",
                 "local (dieser Rechner)", "ct1 (root@ct1)", "wie das System", "Deutsch", "English"):
        assert text in frame, (text, frame)
    assert "Stop" not in frame and "Log out" not in frame and "Approvals" not in frame
    assert {"Sprache", "Host", "Farbschema", "Seiten"} <= set(result["labels"]), result["labels"]
    assert result["asked"] == ("web auf local stoppen? Was darin läuft, endet; sein HOME bleibt erhalten: Fortsetzen "
                               "startet ihn wieder.Ja, stoppenAbbrechen")
    assert "automatisch" in result["placeholders"]
    assert "--mode high\n--allow-pypi\n-mm sonnet" in result["placeholders"], \
        "options stay as typed (the model's flag is -mm: -m is --mount)"
    assert result["toast"] == "!Warnung: Fertig<b>x</b>×", "the level in German, the text as it is"
    assert result["filled"] == "<img src=x> auf {host}", "values are put in as text, once"
    approvals = result["approvals"]
    for text in ("Freigaben", "Host", "Frage", "Verbleibende Zeit", "Erlauben", "Für diesen Lauf ablehnen", "Immer sperren…",
                 "nur dieses Mal", "für den Rest dieses Laufs", "immer (für das Projekt gemerkt)"):
        assert text in approvals, (text, approvals)
    assert result["closed"] == "die Verbindung wurde abgelehnt oder ging verloren (Code 1006): gone", \
        "the service's reason stays as it came"
    assert result["choices"] == [["en", "English", False, "en"], ["de", "Deutsch", True, "de"]], "each in its own language"
    lang, stored, after = result["after"]
    assert lang == "en" and stored == "en"
    for text in ("Starts and sessions", "Approvals", "Log out", "Stop", "Attach", "New start on local", "local (this machine)"):
        assert text in after, (text, after)
    assert "Abmelden" not in after
    assert result["refusing"] == ["de", "en", "Cancel", "en"], "a storage that refuses changes nothing else"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_an_english_browser_gets_english(tmp_path):
    driver = tmp_path / "i18n.js"
    driver.write_text(DRIVER.replace("button(\"Stoppen\")", "button(\"Stop\")").split("  result.placeholders")[0]
                      + "  console.log(JSON.stringify(result));\n})();\n")
    run = subprocess.run(["node", str(driver), str(WEB / "app.js"), "en-GB"], capture_output=True, text=True,
                         timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["lang"] == ["en", "en"] and "Starts and sessions" in result["frame"]
    assert result["asked"].startswith("Stop web on local? What runs in it ends; its HOME is kept")


# --- the German texts address the user as "Sie", never "du" (the desktop app's wording) -----------------------------

DU_WORDS = re.compile(r"\b(du|dich|dir|dein|deine|deinen|deinem|deiner|deines|euch|euer|eure|euren|hast|bist|kannst|"
                      r"musst|willst|möchtest|solltest|wirst|darfst|brauchst|siehst|findest|weißt)\b", re.IGNORECASE)
# an informal imperative at the start of a sentence (Wähle ..., Gib ...), not followed by "Sie" (Wählen Sie ...)
DU_IMPERATIVE = re.compile(r"(?:^|[.:;!?]\s+|\(\s*)(Wähle|Gib|Klicke|Drücke|Öffne|Füge|Starte|Prüfe|Melde|Lade|Nimm|"
                           r"Schreibe|Lies|Sieh|Trage|Nutze|Verwende|Benenne|Lösche|Entferne|Speichere|Halte|Achte|"
                           r"Versuche|Warte|Bestätige|Kopiere|Führe|Lege|Setze|Stelle|Gehe|Schau|Vertraue|Erlaube|"
                           r"Aktiviere|Installiere|Aktualisiere|Behalte|Mach|Mache|Hole|Antworte|Schließe|Wechsle|"
                           r"Ändere|Baue|Gestatte|Sende|Schicke)\s+(?!Sie\b)")


def du_forms(text):
    """The du-forms of a German TEXT (its {placeholders} left out: {dir} is no "dir")."""
    plain = PLACEHOLDER.sub(" ", text)
    return [m.group(0) for m in DU_WORDS.finditer(plain)] + [m.group(1) for m in DU_IMPERATIVE.finditer(plain)]


def test_the_german_texts_say_sie_never_du():
    found = {key: du_forms(entry["de"]) for key, entry in catalog().items()}
    assert not {key: forms for key, forms in found.items() if forms}, "German texts with du-forms (Sie, please)"
    assert du_forms("Wähle einen Host. Gib dein Passwort ein.") == ["dein", "Wähle", "Gib"]
    assert du_forms("Wählen Sie einen Host aus {dir}. Geben Sie Ihr Passwort ein.") == []
    assert du_forms("Hast du das gesehen?") == ["Hast", "du"]


def test_the_german_texts_keep_their_words():
    """One German word for one thing, as the desktop app has it: Sitzung (never Session), Sandboxen (never
    Sandboxes); Neu laden reads a list again, Aktualisieren updates sandbox-ai."""
    texts = catalog()
    for key, entry in texts.items():
        assert not re.search(r"\b(Session|Sessions|Sandboxes|Sandboxs)\b", entry["de"]), (key, entry["de"])
    assert texts["Refresh"]["de"] == "Neu laden" and texts["Reload"]["de"] == "Neu laden"
    assert texts["Update"]["de"] == "Aktualisieren" and texts["Update all"]["de"] == "Alle aktualisieren"


# The words the desktop app chose for its German (commit 84854c4, review 5 M10): one word for each thing, in the app
# and the web alike. A German text of the page says the right column, never the left one ("Git-Login" and
# "Veröffentlichungs-Login" are the app's compounds: they stay).
GLOSSARY = [
    (r"\bWorkspaces?\b|Arbeitsbereich", "Arbeitsordner"),
    (r"\bBoards?\b", "Tafel"),
    (r"\bPresets?\b|\bVorgabe", "Vorlage"),
    (r"\bMaschine|\bComputer", "Rechner"),
    (r"\bProvider", "Anbieter"),
    (r"\bLogs\b|\bLog-|-Log\b", "Protokoll"),
    (r"Privatsphäre", "Datenschutz"),
    (r"\bThema\b", "Farbschema (the setting) or Design"),
    (r"Verzeichnis", "Ordner"),
    (r"(?<!Git-)(?<!Veröffentlichungs-)\bLogins?\b(?!-Server)", "Anmeldung"),
    (r"Markierung (?:von|zurück)|markierte Läufe", "Taint aufheben, Läufe mit Taint"),
    (r"mit Gewalt", "erzwungen"),
    (r"\b(?:immer )?blockier", "sperren (an approval); the browser's own blocking aside"),
    (r"\bAnhalten\b|\bAnhängen\b|angehängt|\bAnwenden\b", "Pausieren, Verbinden, Übernehmen (the buttons)"),
    (r"\S …", "… without a space, as the English has it"),
    (r"\bMarketplaces?\b", "Marktplatz"),
]
# what a browser does is its own word
GLOSSARY_EXCEPT = {"Desktop notifications: blocked by the browser", "media: ffmpeg, PulseAudio, espeak-ng, Piper …"}
# a progress message says what happens ("{name} wird gestoppt…"), not a first-person command ("Stoppe {name}…")
FIRST_PERSON = re.compile(r"^(Lese|Prüfe|Stoppe|Lösche|Frage|Starte|Hole|Lade|Teste|Suche|Speichere|Schreibe|Entferne|"
                          r"Warte|Sende|Baue|Erstelle|Installiere|Aktualisiere|Verbinde|Pausiere|Setze)\b.*…$")


def app_texts():
    import importlib.util
    spec = importlib.util.spec_from_file_location("app_i18n_for_web_test",
                                                  ROOT / "projects/sandbox-ai-app/sandbox_ai_app/i18n.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TEXTS


def test_the_german_words_are_the_apps():
    """Review 5 M10: one German word for each thing, as the app has it (its glossary above); progress messages
    describe what happens; a text the app and the page share reads the same in both (but where a key means another
    thing on the page: "Change" a column there, "Start" a button)."""
    texts = catalog()
    wrong = {key: [word for word, pattern in ((w, re.compile(w)) for w, _ in GLOSSARY) if pattern.search(entry["de"])]
             for key, entry in texts.items() if key not in GLOSSARY_EXCEPT}
    assert not {key: words for key, words in wrong.items() if words}, "German words the app does not use"
    assert not [key for key, entry in texts.items() if FIRST_PERSON.match(entry["de"])], "first-person progress"
    assert texts["Logs"]["de"] == "Protokolle" and texts["Privacy"]["de"] == "Datenschutz"
    assert texts["Workspace"]["de"] == texts["Workspaces"]["de"] == "Arbeitsordner"
    assert texts["Untaint…"]["de"] == "Taint aufheben…" and texts["Theme"]["de"] == "Farbschema"
    app = app_texts()
    shared = {key: (entry["de"], app[key].get("de")) for key, entry in texts.items() if key in app and app[key].get("de")}
    assert len(shared) > 250
    differ = {key: pair for key, pair in shared.items() if pair[0] != pair[1]}
    # another meaning on the page
    assert set(differ) <= {"Change", "Start"}, differ
