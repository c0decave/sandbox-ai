"""Browsing registries for "Your images" (the Images page): a list of well-known registries and your own, a search
where the registry has one (Docker Hub, Quay), else a repository looked up by name, and its tags from the registry's
own v2 API (tags/list, with the anonymous token the registry hands out). Add takes the chosen reference to the
existing pull - `sandbox-ai images pull`, pinned by its digest, UNCHECKED.

The network is used from this computer only (the app), in the background; nothing here pulls."""
from __future__ import annotations

import json
import re
import urllib.parse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLineEdit, QListWidget, QVBoxLayout,
)

from . import state as app_state
from .files import printable, printable_text
from .i18n import N_, tr
from . import package_search
from .package_search import Offline, json_of, short
from .widgets import background, button, fill, plain_label, selected_keys, set_notice, table

# (host, name, search: "dockerhub", "quay" or None)
REGISTRIES = (
    ("docker.io", N_("Docker Hub"), "dockerhub"),
    ("ghcr.io", N_("GitHub Container Registry"), None),
    ("quay.io", N_("Quay"), "quay"),
    ("gcr.io", N_("Google Container Registry"), None),
    ("mirror.gcr.io", N_("Google's mirror of Docker Hub"), None),
    ("public.ecr.aws", N_("Amazon ECR Public"), None),
    ("mcr.microsoft.com", N_("Microsoft Artifact Registry"), None),
    ("registry.gitlab.com", N_("GitLab"), None),
    ("registry.access.redhat.com", N_("Red Hat"), None),
)
OWN_KEY = "image_registries"  # the app's state: the registries you added
HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}(?::[0-9]{1,5})?|localhost:[0-9]{1,5}")
REPOSITORY = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*")
TAG = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
TAG_LIMIT = 200


def docker_hub_search(query, fetch=None):
    """[{name, description, stars, official}] of Docker Hub's search."""
    document = json_of("https://hub.docker.com/v2/search/repositories/?page_size=25&query=" +
                       urllib.parse.quote(query), fetch) or {}
    found = []
    for item in document.get("results") or []:
        if isinstance(item, dict) and isinstance(item.get("repo_name"), str):
            found.append({"name": item["repo_name"], "description": short(item.get("short_description")),
                          "stars": int(item.get("star_count") or 0), "official": bool(item.get("is_official"))})
    return found


def quay_search(query, fetch=None):
    document = json_of("https://quay.io/api/v1/find/repositories?query=" + urllib.parse.quote(query), fetch) or {}
    found = []
    for item in document.get("results") or []:
        if not isinstance(item, dict) or item.get("kind", "repository") != "repository":
            continue
        namespace = (item.get("namespace") or {}).get("name") if isinstance(item.get("namespace"), dict) else None
        if isinstance(namespace, str) and isinstance(item.get("name"), str):
            found.append({"name": f"{namespace}/{item['name']}", "description": short(item.get("description")),
                          "stars": int(item.get("stars") or 0), "official": False})
    return found


def repository_path(registry, name):
    """NAME as the registry's API knows it (Docker Hub's official images are library/NAME)."""
    return f"library/{name}" if registry == "docker.io" and "/" not in name else name


def docker_hub_tags(name, fetch=None):
    document = json_of(f"https://hub.docker.com/v2/repositories/{repository_path('docker.io', name)}/tags"
                       "?page_size=100&ordering=last_updated", fetch)
    if document is None:
        raise LookupError(name)
    return [t["name"] for t in document.get("results") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]


def quay_tags(name, fetch=None):
    document = json_of(f"https://quay.io/api/v1/repository/{name}/tag/?limit=100&onlyActiveTags=true", fetch)
    if document is None:
        raise LookupError(name)
    return [t["name"] for t in document.get("tags") or [] if isinstance(t, dict) and isinstance(t.get("name"), str)]


def challenge(header):
    """{realm, service, scope} of a WWW-Authenticate: Bearer header."""
    if not str(header).lower().startswith("bearer "):
        return {}
    return dict(re.findall(r'(\w+)="([^"]*)"', header[7:]))


def v2_tags(registry, name, fetch=None):
    """The tags of NAME from REGISTRY's v2 API (tags/list), with the anonymous token it hands out when it asks for
    one; LookupError when there is no such repository (or it is not public)."""
    url = f"https://{registry}/v2/{repository_path(registry, name)}/tags/list?n={TAG_LIMIT}"
    fetch = fetch or package_search.http_get
    status, headers, body = fetch(url, None, 4 << 20)
    if status == 401:
        given = challenge(headers.get("www-authenticate", ""))
        realm = given.get("realm", "")
        if not realm.startswith("https://"):
            raise ValueError(f"{registry} asks for a login ({headers.get('www-authenticate', 'no challenge')})")
        token_url = realm + "?" + urllib.parse.urlencode(
            {k: v for k, v in (("service", given.get("service")),
                               ("scope", given.get("scope") or f"repository:{repository_path(registry, name)}:pull"))
             if v})
        token = json_of(token_url, fetch) or {}
        bearer = token.get("token") or token.get("access_token")
        if not isinstance(bearer, str):
            raise ValueError(f"{registry} gave no token")
        status, headers, body = fetch(url, {"Authorization": f"Bearer {bearer}"}, 4 << 20)
    if status in (401, 403, 404):
        raise LookupError(name)
    if status != 200:
        raise ValueError(f"{url}: HTTP {status}")
    try:
        tags = json.loads(body).get("tags") or []
    except (ValueError, AttributeError, RecursionError):
        raise ValueError(f"{url}: no JSON") from None
    return [t for t in tags if isinstance(t, str)]


def tags_of(registry, name, fetch=None):
    """The tags of NAME on REGISTRY, newest first where the registry says (Docker Hub, Quay), at most TAG_LIMIT."""
    if registry == "docker.io":
        found = docker_hub_tags(name, fetch)
    elif registry == "quay.io":
        found = quay_tags(name, fetch)
    else:
        found = sorted(v2_tags(registry, name, fetch), reverse=True)
    return [t for t in found if TAG.fullmatch(t)][:TAG_LIMIT]


def search(registry, query, fetch=None):
    """The repositories of REGISTRY for QUERY: its own search, else the name looked up (its tags as proof)."""
    kind = next((k for host, _, k in REGISTRIES if host == registry), None)
    if kind == "dockerhub":
        return docker_hub_search(query, fetch)
    if kind == "quay":
        return quay_search(query, fetch)
    name = query.strip().lower()
    if not REPOSITORY.fullmatch(name):
        raise ValueError(tr("{registry} has no search: type a repository, e.g. org/app.", registry=registry))
    tags_of(registry, name, fetch)
    return [{"name": name, "description": "", "stars": None, "official": False}]


def reference(registry, name, tag):
    return f"{registry}/{repository_path(registry, name)}:{tag}"


def own_registries():
    current = app_state.current()
    found = current.get(OWN_KEY, []) if current is not None else []
    return [h for h in found if isinstance(h, str) and HOST.fullmatch(h)] if isinstance(found, list) else []


def remember_registry(host):
    current = app_state.current()
    if current is not None:
        current.set(OWN_KEY, sorted({*own_registries(), host}))


class RegistryBrowser(QDialog):
    """The registry picker of "Your images": registry, search (or name), results, tags; reference() is the image
    chosen for `sandbox-ai images pull`."""

    def __init__(self, parent=None, fetch=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Browse registries"))
        self.fetch, self.generation, self.results_rows, self.tags_for = fetch, 0, [], None
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.registry = QComboBox()
        self.registry.setAccessibleName(tr("Registry"))
        self.fill_registries()
        self.registry.currentIndexChanged.connect(self.registry_changed)
        self.new_registry = QLineEdit()
        self.new_registry.setPlaceholderText(tr("your registry, e.g. registry.example.org"))
        self.new_registry.returnPressed.connect(self.add_registry)
        top.addWidget(self.registry, 2)
        top.addWidget(self.new_registry, 2)
        top.addWidget(button(tr("Add registry"), tr("Add your own registry to this list (kept by the app)"),
                             self.add_registry))
        layout.addLayout(top)
        row = QHBoxLayout()
        self.query = QLineEdit()
        self.query.setAccessibleName(tr("Search the registry"))
        self.query.returnPressed.connect(self.search)
        self.search_button = button(tr("Search"), tr("Search the registry, or look the repository up where it has no "
                                                     "search"), self.search)
        self.cancel_button = button(tr("Cancel"), tr("Stop this search"), self.cancel)
        self.cancel_button.setEnabled(False)
        row.addWidget(self.query, 1)
        row.addWidget(self.search_button)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)
        self.results = table([tr("Repository"), tr("Description"), tr("Stars"), tr("Official")], stretch=1,
                             compact=(2, 3))
        self.results.setAccessibleName(tr("Repositories found"))
        self.results.itemSelectionChanged.connect(self.repository_chosen)
        layout.addWidget(self.results, 2)
        self.tags = QListWidget()
        self.tags.setAccessibleName(tr("Tags"))
        self.tags.currentTextChanged.connect(self.update_reference)
        layout.addWidget(plain_label(tr("Tags"), role="secondary"))
        layout.addWidget(self.tags, 1)
        self.status = plain_label(role="secondary", selectable=True)
        layout.addWidget(self.status)
        self.chosen = plain_label(selectable=True)
        layout.addWidget(self.chosen)
        layout.addWidget(plain_label(tr("Add pulls the image once and pins it by its digest (sandbox-ai images pull). "
                                        "Nobody reviewed it: it stays UNCHECKED."), role="notice-error"))
        buttons = QDialogButtonBox()
        self.ok = buttons.addButton(tr("Add…"), QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(tr("Close"), QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(820, 640)
        self.registry_changed()

    # -- registries --

    def fill_registries(self, chosen=None):
        self.registry.blockSignals(True)
        self.registry.clear()
        for host, name, kind in REGISTRIES:
            self.registry.addItem(f"{tr(name)} ({host})" + ("" if kind else " · " + tr("no search: name and tags")),
                                  host)
        for host in own_registries():
            if all(host != known for known, _, _ in REGISTRIES):
                self.registry.addItem(tr("{host} (yours) · no search: name and tags", host=host), host)
        if chosen is not None:
            self.registry.setCurrentIndex(max(0, self.registry.findData(chosen)))
        self.registry.blockSignals(False)

    def host(self):
        return self.registry.currentData()

    def searchable(self):
        return any(host == self.host() and kind for host, _, kind in REGISTRIES)

    def add_registry(self):
        host = self.new_registry.text().strip().lower().removeprefix("https://").rstrip("/")
        if not HOST.fullmatch(host):
            set_notice(self.status, tr("Not a registry's host name (e.g. registry.example.org or "
                                       "registry.example.org:5000)."), "error")
            return
        remember_registry(host)
        self.new_registry.clear()
        self.fill_registries(host)
        self.registry_changed()

    def registry_changed(self):
        self.cancel(quiet=True)
        self.query.setPlaceholderText(tr("search, e.g. postgres or redis") if self.searchable() else
                                      tr("repository, e.g. org/app (this registry has no search)"))
        self.show_results([])
        set_notice(self.status, "")

    # -- searching --

    def busy(self, text):
        self.generation += 1
        self.cancel_button.setEnabled(True)
        set_notice(self.status, text)
        return self.generation

    def search(self):
        query, host = self.query.text().strip(), self.host()
        if not query:
            set_notice(self.status, tr("Type a word to search for."), "warn")
            return
        generation = self.busy(tr("Asking {registry}…", registry=host))
        background(search, host, query, self.fetch,
                   done=lambda result, error: self.searched(generation, result, error), what="registry search")

    def searched(self, generation, result, error):
        if generation != self.generation:
            return
        self.cancel_button.setEnabled(False)
        if error is not None:
            self.show_results([])
            set_notice(self.status, self.problem(error), "error")
            return
        self.show_results(result or [])
        set_notice(self.status, tr("{count} repositories", count=len(self.results_rows)))
        if len(self.results_rows) == 1:
            self.results.selectRow(0)

    def problem(self, error):
        if isinstance(error, Offline):
            return tr("No network: {registry} cannot be reached from this computer ({error}).", registry=self.host(),
                      error=printable_text(str(error))[:200])
        if isinstance(error, LookupError):
            return tr("{registry} has no public repository {name}.", registry=self.host(),
                      name=printable(str(error.args[0] if error.args else "")))
        return printable_text(str(error))[:400]

    def show_results(self, rows):
        self.results_rows = rows
        self.tags.clear()
        self.tags_for = None
        fill(self.results, [[printable(r["name"]), printable(r.get("description") or ""),
                             "" if r.get("stars") is None else str(r["stars"]),
                             tr("official") if r.get("official") else ""] for r in rows],
             keys=[r["name"] for r in rows])
        self.update_reference()

    def repository_chosen(self):
        keys = selected_keys(self.results)
        if not keys or keys[0] == self.tags_for:
            self.update_reference()
            return
        name, host = keys[0], self.host()
        self.tags_for = name
        self.tags.clear()
        self.update_reference()
        generation = self.busy(tr("Reading the tags of {name}…", name=printable(name)))
        background(tags_of, host, name, self.fetch,
                   done=lambda result, error: self.tags_read(generation, name, result, error), what="registry tags")

    def tags_read(self, generation, name, result, error):
        if generation != self.generation:
            return
        self.cancel_button.setEnabled(False)
        if error is not None:
            set_notice(self.status, self.problem(error if not isinstance(error, LookupError) else LookupError(name)),
                       "error")
            return
        self.tags.addItems(list(result or []))
        if self.tags.count():
            latest = self.tags.findItems("latest", Qt.MatchFlag.MatchExactly)
            self.tags.setCurrentItem(latest[0] if latest else self.tags.item(0))
        set_notice(self.status, tr("{count} tags of {name}", count=self.tags.count(), name=printable(name)))
        self.update_reference()

    def cancel(self, quiet=False):
        if self.cancel_button.isEnabled() and not quiet:
            set_notice(self.status, tr("Search cancelled."))
        self.generation += 1
        self.cancel_button.setEnabled(False)

    def reference(self):
        keys = selected_keys(self.results)
        tag = self.tags.currentItem().text() if self.tags.currentItem() is not None else None
        if not keys or not tag or keys[0] != self.tags_for:
            return None
        return reference(self.host(), keys[0], tag)

    def update_reference(self, *_):
        chosen = self.reference()
        self.chosen.setText(tr("Image: {reference}", reference=chosen) if chosen else
                            tr("Choose a repository and a tag."))
        self.ok.setEnabled(chosen is not None)
