"""Finding packages for an image (the Images page): a search per kind - its results with name, version and
description - and Add into the extension's list.

  OS packages  `sandbox-ai images search TARGET QUERY --json` on the chosen host: the package index of the image's own
               distribution (apt, apk, dnf, pacman, zypper), read once in a throwaway container of the image with the
               network an extension's build has, and cached there per image; without network an index read before
               is searched (and says so).
  npm          registry.npmjs.org/-/v1/search, from this computer.
  Python       PyPI has no search API: the exact name is looked up (/pypi/NAME/json), and the names that hold the
               words are taken from PyPI's list of every project name (its Simple index), downloaded at most once a
               day and cached here; the first matches get their version and summary looked up.

Everything runs in the background and can be cancelled; the answers of a search are kept per host, image and query
for this session. DeriveDialog: one of your images with OS packages added (`sandbox-ai images derive`)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLineEdit, QListWidget, QVBoxLayout,
)

from .files import printable, printable_text
from .i18n import tr
from .widgets import AsyncCommand, background, button, fill, list_menu, plain_label, selected_keys, set_notice, table

NPM_SEARCH = "https://registry.npmjs.org/-/v1/search?text={query}&size=25"
PYPI_PROJECT = "https://pypi.org/pypi/{name}/json"
PYPI_SIMPLE = "https://pypi.org/simple/"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
NAMES_AGE = 86400  # PyPI's list of names is downloaded again after a day
DETAILS = 12  # PyPI matches whose version and summary are looked up
ANSWER_LIMIT = 8 << 20
NAMES_LIMIT = 128 << 20


class Offline(Exception):
    """The registry could not be reached (no network, a proxy, a timeout)."""


def http_get(url, headers=None, limit=ANSWER_LIMIT, timeout=25):
    """(status, headers {lower-case name: value}, body) of GET URL; Offline when it cannot be reached. An HTTP error
    is an answer (its status), not an exception."""
    request = urllib.request.Request(url, headers={"User-Agent": "sandbox-ai-app", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - https URLs of this module
            body = answer.read(limit + 1)
            status, given = answer.status, answer.headers
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read(64 << 10)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise Offline(str(getattr(exc, "reason", exc))) from None
    if len(body) > limit:
        raise ValueError(f"{url}: the answer is larger than {limit >> 20} MB")
    return status, {k.lower(): v for k, v in given.items()}, body


def json_of(url, fetch=None, headers=None, limit=ANSWER_LIMIT):
    """The JSON document at URL; None for 404; ValueError for another status or no JSON."""
    status, _, body = (fetch or http_get)(url, headers, limit)
    if status == 404:
        return None
    if status != 200:
        raise ValueError(f"{url}: HTTP {status}")
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise ValueError(f"{url}: no JSON") from None


def short(text, length=200):
    text = " ".join(str(text or "").split())
    return text if len(text) <= length else text[:length - 1] + "…"


def npm_search(query, fetch=None):
    """[{name, version, summary}] of the npm registry's search for QUERY."""
    document = json_of(NPM_SEARCH.format(query=urllib.parse.quote(query)), fetch) or {}
    found = []
    for item in document.get("objects") or []:
        package = (item or {}).get("package") if isinstance(item, dict) else None
        if isinstance(package, dict) and isinstance(package.get("name"), str):
            found.append({"name": package["name"], "version": str(package.get("version") or ""),
                          "summary": short(package.get("description"))})
    return found


def normalized(name):
    """A Python project name as PyPI compares them (PEP 503)."""
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def cache_dir():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "sandbox-ai" / "app"


def pypi_names(fetch=None, now=time.time):
    """Every project name on PyPI (its Simple index as JSON), cached for a day; an older cached list when PyPI cannot
    be reached, else Offline."""
    path = cache_dir() / "pypi-names.txt"
    try:
        fresh = now() - path.stat().st_mtime < NAMES_AGE
    except OSError:
        fresh = None
    if not fresh:
        try:
            document = json_of(PYPI_SIMPLE, fetch, {"Accept": SIMPLE_JSON}, NAMES_LIMIT) or {}
            names = [p["name"] for p in document.get("projects") or [] if isinstance(p, dict)
                     and isinstance(p.get("name"), str)]
            if names:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.{os.getpid()}")
                temporary.write_text("\n".join(names) + "\n")
                os.replace(temporary, path)
                return names
        except Offline:
            if fresh is None:
                raise
    return path.read_text().split()


def pypi_project(name, fetch=None):
    """{name, version, summary} of the PyPI project NAME, else None."""
    document = json_of(PYPI_PROJECT.format(name=urllib.parse.quote(name)), fetch)
    info = document.get("info") if isinstance(document, dict) else None
    if not isinstance(info, dict):
        return None
    return {"name": str(info.get("name") or name), "version": str(info.get("version") or ""),
            "summary": short(info.get("summary"))}


def pypi_search(query, fetch=None, names=None, cancelled=lambda: False):
    """[{name, version, summary}] for QUERY on PyPI: the exact name first, then names holding every word (those
    beginning with the first word first, shorter ones first), the first DETAILS of them looked up. NAMES: PyPI's
    list (default: pypi_names)."""
    words = [normalized(w) for w in str(query).split()]
    if not words:
        return []
    found, seen = [], set()
    exact = pypi_project(words[0], fetch) if len(words) == 1 else None
    if exact:
        found.append(exact)
        seen.add(normalized(exact["name"]))
    listed = pypi_names(fetch) if names is None else names
    matches = sorted((n for n in listed if all(w in normalized(n) for w in words) and normalized(n) not in seen),
                     key=lambda n: (not normalized(n).startswith(words[0]), len(n), n.lower()))
    for name in matches[:DETAILS]:
        if cancelled():
            break
        project = pypi_project(name, fetch) or {"name": name, "version": "", "summary": ""}
        found.append(project)
    found += [{"name": n, "version": "", "summary": ""} for n in matches[DETAILS:60]]
    return found


def os_answer(status, out, err):
    """(document, problem) of `sandbox-ai images search ... --json`."""
    try:
        document = json.loads(out) if status == 0 else None
    except (ValueError, RecursionError):
        document = None
    if isinstance(document, dict) and isinstance(document.get("results"), list):
        return document, None
    said = (err or out or "").strip().splitlines()
    text = said[-1] if said else ""
    text = text.removeprefix("sandbox-ai images: ")
    if "invalid choice" in text:
        return None, tr("This sandbox-ai cannot search packages yet; update it (sandbox-ai update).")
    return None, printable_text(text[-400:]) or tr("The search failed (exit status {status}).", status=status)


def rows_of(results):
    return [{"name": str(r.get("name") or ""), "version": str(r.get("version") or ""),
             "summary": str(r.get("summary") or "")} for r in results if isinstance(r, dict) and r.get("name")]


class PackageBrowser(QGroupBox):
    """The search of one kind ("apt": the image's OS packages, "npm", "pip"): a query, its results, Add. ADD(value)
    puts a name into the list; TARGET() names the image whose OS packages are searched (default: the page's)."""

    def __init__(self, kind, page, add, target=None):
        super().__init__(tr("Find packages"))
        self.kind, self.page, self.add_to, self.target_of = kind, page, add, target or (lambda: page.target)
        self.cache, self.generation, self.rows, self.running = {}, 0, [], None
        layout = QVBoxLayout(self)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setPlaceholderText({
            "apt": tr("search the image's distribution, e.g. json or ffmpeg"),
            "npm": tr("search npm, e.g. typescript or eslint"),
            "pip": tr("search PyPI, e.g. ruff or httpie")}[kind])
        self.query.setAccessibleName(tr("Search for packages"))
        self.query.returnPressed.connect(self.search)
        self.search_button = button(tr("Search"), {
            "apt": tr("Search the package index of the image's distribution (read once in a container of the image, "
                      "with the network an extension's build has)"),
            "npm": tr("Search the npm registry (registry.npmjs.org) from this computer"),
            "pip": tr("Look up the name on PyPI, and names holding the words in PyPI's list of projects (downloaded "
                      "at most once a day; PyPI has no search API)")}[kind], self.search)
        self.cancel_button = button(tr("Cancel"), tr("Stop this search"), self.cancel)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.query, 1)
        row.addWidget(self.search_button)
        row.addWidget(self.cancel_button)
        if kind == "apt":
            self.refresh_button = button(tr("Read the index again"), tr("Fetch the package index of the image anew "
                                                                        "(it is kept for a week)"),
                                         lambda: self.search(refresh=True))
            row.addWidget(self.refresh_button)
        layout.addLayout(row)
        self.results = table([tr("Name"), tr("Version"), tr("Description")], stretch=2, compact=(1,))
        self.results.setAccessibleName(tr("Packages found"))
        self.results.itemSelectionChanged.connect(self.update_buttons)
        self.results.itemDoubleClicked.connect(lambda *_: self.add())
        layout.addWidget(self.results, 1)
        bottom = QHBoxLayout()
        self.status = plain_label(role="secondary", selectable=True)
        self.add_button = button(tr("Add"), tr("Add the selected packages to the list (saved with Save)"), self.add)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(self.add_button)
        layout.addLayout(bottom)
        list_menu(self.results, lambda: [self.add_button])  # (a right click: Add, as the button)
        self.command = AsyncCommand(timeout=960, parent=self)
        self.command.finished.connect(self.os_found)
        self.update_buttons()

    def key(self, query, target):
        host = self.page.host() if self.kind == "apt" else ""
        return (host, target if self.kind == "apt" else "", query.strip().lower())

    def search(self, refresh=False):
        query = self.query.text().strip()
        target = self.target_of() if self.kind == "apt" else None
        if self.kind == "apt" and not target:
            set_notice(self.status, tr("Select an image first: its own distribution is searched."), "warn")
            return
        if not query and self.kind != "apt":
            set_notice(self.status, tr("Type a word to search for."), "warn")
            return
        key = self.key(query, target)
        if not refresh and key in self.cache:
            self.show_rows(*self.cache[key])
            return
        self.cancel(quiet=True)
        self.generation += 1
        self.running = key
        self.cancel_button.setEnabled(True)
        if self.kind == "apt":
            set_notice(self.status, tr("Searching the packages of {name}… (the first search reads its package index in "
                                       "a container of the image: a minute or two)", name=printable(target)))
            self.command.start(self.page.run_argv("images", "search", target, query, "--json", "--limit", "100",
                                                  *(["--refresh"] if refresh else [])))
            return
        generation = self.generation
        set_notice(self.status, tr("Searching npm…") if self.kind == "npm" else
                   tr("Looking up on PyPI… (the first search downloads PyPI's list of project names, about 30 MB, "
                      "kept for a day)"))
        function = npm_search if self.kind == "npm" else \
            (lambda q: pypi_search(q, cancelled=lambda: generation != self.generation))
        background(function, query, done=lambda result, error: self.found(generation, key, result, error),
                   owner=self.page, what=f"package search ({self.kind})")

    def found(self, generation, key, result, error):
        if generation != self.generation:
            return  # cancelled, or another search since
        self.running = None
        self.cancel_button.setEnabled(False)
        if isinstance(error, Offline):
            self.show_problem(tr("No network: {where} cannot be reached from this computer ({error}).",
                                 where="registry.npmjs.org" if self.kind == "npm" else "pypi.org",
                                 error=printable_text(str(error))[:200]))
            return
        if error is not None:
            self.show_problem(printable_text(str(error))[:400])
            return
        rows = rows_of(result or [])
        note = tr("{count} found on npm", count=len(rows)) if self.kind == "npm" else \
            tr("{count} found on PyPI (the exact name, then names holding the words)", count=len(rows))
        self.cache[key] = (rows, note, None)
        self.show_rows(rows, note, None)

    def os_found(self, status, out, err):
        if status == -2 or self.running is None:
            return
        key, self.running = self.running, None
        self.cancel_button.setEnabled(False)
        document, problem = os_answer(status, out, err)
        if document is None:
            self.show_problem(problem)
            return
        rows = rows_of(document["results"])
        note = tr("{count} of {total} packages of {system} ({manager}) in {image}", count=len(rows),
                  total=document.get("count", len(rows)), system=printable(document.get("system") or ""),
                  manager=printable(document.get("manager") or ""), image=printable(document.get("image") or ""))
        stale = printable_text(document["stale"]) if document.get("stale") else None
        self.cache[key] = (rows, note, stale)
        self.show_rows(rows, note, stale)

    def show_rows(self, rows, note, stale):
        self.rows = rows
        fill(self.results, [[printable(r["name"]), printable(r["version"]), printable(r["summary"])] for r in rows],
             keys=[r["name"] for r in rows])
        set_notice(self.status, note + (" · " + tr("without network: {why}", why=stale) if stale else ""),
                   "warn" if stale else None)
        self.update_buttons()

    def show_problem(self, text):
        self.rows = []
        fill(self.results, [])
        set_notice(self.status, text, "error")
        self.update_buttons()

    def cancel(self, quiet=False):
        was = self.running is not None
        self.generation += 1
        self.running = None
        self.command.stop()
        self.cancel_button.setEnabled(False)
        if was and not quiet:
            set_notice(self.status, tr("Search cancelled."))

    def update_buttons(self):
        self.add_button.setEnabled(bool(selected_keys(self.results)))

    def add(self):
        for name in selected_keys(self.results):
            self.add_to(name)


class DeriveDialog(QDialog):
    """`sandbox-ai images derive NAME --from IMAGE --add-os PKG ...`: one of your images with OS packages added by its
    own distribution's manager (apk, dnf, pacman, zypper, apt-get), recorded as another of your images, UNCHECKED."""

    def __init__(self, parent, page, source, system, taken=()):
        super().__init__(parent)
        self.setWindowTitle(tr("Add OS packages to {name}", name=source))
        self.source, self.taken = source, set(taken)
        layout = QVBoxLayout(self)
        layout.addWidget(plain_label(tr("{name} is {system}. The packages are installed with its own package manager "
                                        "and the network an extension's build has; the result is another of your "
                                        "images, UNCHECKED like this one.", name=source, system=system or "?"),
                                     role="secondary"))
        form = QFormLayout()
        self.name = QLineEdit(f"{source}-plus"[:30])
        form.addRow(tr("Name of the new image"), self.name)
        layout.addLayout(form)
        self.chosen = QListWidget()
        self.chosen.setAccessibleName(tr("Packages to add"))
        self.chosen.setMaximumHeight(110)
        layout.addWidget(self.chosen)
        entry_row = QHBoxLayout()
        self.entry = QLineEdit()
        self.entry.setPlaceholderText(tr("package name"))
        self.entry.returnPressed.connect(lambda: self.add(self.entry.text().strip()))
        entry_row.addWidget(self.entry, 1)
        entry_row.addWidget(button(tr("Add"), tr("Add it to the packages"), lambda: self.add(self.entry.text().strip())))
        self.remove_button = button(tr("Remove"), tr("Remove the selected package"), self.remove)
        entry_row.addWidget(self.remove_button)
        list_menu(self.chosen, lambda: [self.remove_button])  # (a right click: Remove, as the button)
        layout.addLayout(entry_row)
        self.browser = PackageBrowser("apt", page, self.add, target=lambda: source)
        layout.addWidget(self.browser, 1)
        self.problem = plain_label(role="notice-error", selectable=True)
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox()
        self.ok = buttons.addButton(tr("Build it"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Cancel"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.name.textChanged.connect(self.check)
        self.resize(760, 620)
        self.check()

    def packages(self):
        return [self.chosen.item(i).text() for i in range(self.chosen.count())]

    def add(self, value):
        if value and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9@+._=:~-]{0,160}", value) and value not in self.packages():
            self.chosen.addItem(value)
            self.entry.clear()
        self.check()

    def remove(self):
        for item in self.chosen.selectedItems():
            self.chosen.takeItem(self.chosen.row(item))
        self.check()

    def check(self):
        name = self.name.text().strip()
        problem = ""
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,29}", name):
            problem = tr("Lower-case letters, digits, '.', '_' and '-', up to 30 characters; not an agent's name.")
        elif name in self.taken:
            problem = tr("You have an image {name} already: choose another name, or replace it.", name=name)
        self.problem.setText(problem)
        self.ok.setEnabled(not problem and bool(self.packages()))

    def argv(self):
        return ["images", "derive", self.name.text().strip(), "--from", self.source,
                *[word for package in self.packages() for word in ("--add-os", package)], "--json"]


def browser_for(package_list):
    """The search under PACKAGE_LIST (images_page.PackageList), adding into it."""
    return PackageBrowser(package_list.kind, package_list.page, package_list.add_value)
