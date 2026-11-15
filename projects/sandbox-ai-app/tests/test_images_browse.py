"""The Images page finds packages and images (hotfix 0.1.96, from the user's live use):

  (a) "Was mich wundert: wenn man auf Erweiterungen geht, erscheint da gar kein Listing von möglichen zu
      installierenden Paketen – bei npm, Python etc. genauso." - each package list of the Extension tab has a search
      with results (name, version, description) and Add: the image's own distribution (sandbox-ai images search),
      npm's registry search, PyPI (the exact name, and names from PyPI's list of projects).
  (b) "Es kann sein, dass wir nicht nur Debian, sondern z.B. auch Alpine Linux haben … das müsste auch supported
      werden" - Your images: Add OS packages… (sandbox-ai images derive) searches and installs with the image's own
      distribution's manager.
  (c) "Bei 'Eigene Images' aus einer Registry: haben wir keine vordefinierten Registries, sodass man eine Liste sieht,
      aus der man etwas hinzufügen kann?" - Browse registries…: Docker Hub, GitHub, Quay, Google, Amazon, Microsoft,
      GitLab, Red Hat and your own; a search where the registry has one, tags from the v2 API; Add goes to the pinned
      pull, UNCHECKED.

Every network answer here is a fake: no test reaches a registry. The end-to-end tests run the real sandbox-ai of
SANDBOX_AI_HOME with live_fakes.py's Podman."""
import json
import os
import subprocess

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import package_search as ps, registry_browser as rb, widgets  # noqa: E402
from sandbox_ai_app.images_page import ImagesPage  # noqa: E402


def at_once(function, *args, done, **kwargs):
    try:
        result = function(*args)
    except Exception as exc:  # noqa: BLE001
        return done(None, exc)
    return done(result, None)


class Web:
    """Recorded answers by URL prefix: (status, headers, JSON or bytes); a URL with none is Offline."""

    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, url, headers=None, limit=None):
        self.asked.append((url, dict(headers or {})))
        for prefix, answer in self.answers.items():
            if url.startswith(prefix):
                answer = answer(url, headers) if callable(answer) else answer
                status, given, body = answer if isinstance(answer, tuple) else (200, {}, answer)
                return status, given, body if isinstance(body, bytes) else json.dumps(body).encode()
        raise ps.Offline("Temporary failure in name resolution")


@pytest.fixture
def quiet(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(ps, "background", at_once)
    monkeypatch.setattr(rb, "background", at_once)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    monkeypatch.setattr(ps, "http_get", Web({}))  # nothing reaches the network unless a test says so
    return tmp_path


# -- npm and PyPI --

def test_npm_search_lists_name_version_and_description(quiet):
    web = Web({"https://registry.npmjs.org/-/v1/search?text=type%20script": {"objects": [
        {"package": {"name": "typescript", "version": "5.7.2", "description": "TypeScript is a language for "
                                                                             "application scale JavaScript"}},
        {"package": {"name": "ts-node", "version": "10.9.2", "description": None}}, {"bad": 1}]}})
    assert ps.npm_search("type script", web) == [
        {"name": "typescript", "version": "5.7.2",
         "summary": "TypeScript is a language for application scale JavaScript"},
        {"name": "ts-node", "version": "10.9.2", "summary": ""}]


def test_pypi_has_no_search_the_name_and_the_list_of_names_are_used(quiet):
    """PyPI has no search API: the exact name is looked up, and names holding the word come from its Simple index
    (downloaded once a day, cached), the first of them looked up too."""
    names = {"projects": [{"name": n} for n in ("ruff", "ruff-lsp", "pyruff", "requests", "flake8-ruff")]}
    project = {"ruff": {"info": {"name": "ruff", "version": "0.8.4", "summary": "An extremely fast Python linter"}},
               "ruff-lsp": {"info": {"name": "ruff-lsp", "version": "0.0.59", "summary": "LSP for Ruff"}}}
    web = Web({"https://pypi.org/simple/": names,
               "https://pypi.org/pypi/": lambda url, _: (200, {}, project[url.split("/")[4]]) if url.split("/")[4]
               in project else (404, {}, b"")})
    found = ps.pypi_search("ruff", web)
    assert [f["name"] for f in found] == ["ruff", "ruff-lsp", "pyruff", "flake8-ruff"]
    assert found[0] == {"name": "ruff", "version": "0.8.4", "summary": "An extremely fast Python linter"}
    assert ("https://pypi.org/simple/", {"Accept": "application/vnd.pypi.simple.v1+json"}) in web.asked
    # the list is cached: the next search does not download it again, and works without PyPI's list online
    asked = len(web.asked)
    ps.pypi_search("requests", Web({"https://pypi.org/pypi/": (404, {}, b"")}))
    assert len(web.asked) == asked
    assert (quiet / "cache/sandbox-ai/app/pypi-names.txt").read_text().split()[0] == "ruff"


def test_pypi_without_network_says_so(quiet):
    with pytest.raises(ps.Offline):
        ps.pypi_search("ruff", Web({}))


def test_the_npm_tab_finds_and_adds(quiet, monkeypatch):
    """(a) The npm list of the Extension tab: a search, its results, Add puts the name into the list."""
    page = ImagesPage(FakeSai(), lambda *a, **kw: None)
    page._module = Module()
    monkeypatch.setattr(ps, "http_get", Web({"https://registry.npmjs.org/": {"objects": [
        {"package": {"name": "typescript", "version": "5.7.2", "description": "TypeScript"}}]}}))
    browser = page.lists["npm"].browser
    browser.query.setText("typescript")
    browser.search()
    assert [browser.results.item(0, c).text() for c in range(3)] == ["typescript", "5.7.2", "TypeScript"]
    assert browser.status.text() == "1 found on npm"
    browser.results.selectRow(0)
    browser.add()
    assert page.lists["npm"].values() == ["typescript"]
    monkeypatch.setattr(ps, "http_get", Web({}))  # offline now: the same query comes from this session's answers
    browser.search()
    assert browser.results.rowCount() == 1
    browser.query.setText("eslint")
    browser.search()
    assert browser.status.text().startswith("No network: registry.npmjs.org cannot be reached from this computer")
    page.deleteLater()


# -- the image's own distribution --

class FakeSai:
    def host_command(self, host, *args):
        return ["sandbox-ai", *args]

    def module(self, name):
        raise ValueError("not in this test")


class Module:
    """sandbox-ai's images module as the editor uses it."""

    def check_package(self, kind, value, agent=None):
        return None if value and " " not in value else "not a package"

    def package_name(self, kind, value):
        return value.split("=", 1)[0]

    def check_fragment(self, *_):
        return []

    def containerfile(self, *_):
        return ""


def test_the_os_tab_searches_the_images_own_distribution(quiet):
    """(a) The OS packages of the selected image: `sandbox-ai images search TARGET QUERY --json` on the chosen host,
    in the background; its answer fills the table; Add; the next search of the same query is answered at once;
    Cancel stops it."""
    page = ImagesPage(FakeSai(), lambda *a, **kw: None)
    page._module = Module()
    browser = page.lists["apt"].browser
    started = []
    browser.command.start = lambda argv: started.append(argv) or True
    browser.command.stop = lambda: None
    browser.query.setText("jq")
    browser.search()
    assert started == [] and "Select an image first" in browser.status.text()
    page.target = "claude"
    browser.search()
    assert started[-1] == ["sandbox-ai", "images", "search", "claude", "jq", "--json", "--limit", "100"]
    browser.os_found(0, json.dumps({"system": "Debian GNU/Linux 12 (bookworm)", "manager": "apt-get",
                                    "image": "localhost/claude-sandbox:latest", "count": 63000, "results": [
                                        {"name": "jq", "version": "1.6-2.1", "summary": "JSON processor"}]}), "")
    assert browser.status.text() == "1 of 63000 packages of Debian GNU/Linux 12 (bookworm) (apt-get) in " \
                                    "localhost/claude-sandbox:latest"
    browser.results.selectRow(0)
    browser.add()
    assert page.lists["apt"].values() == ["jq"]
    browser.search()
    assert len(started) == 1, "answered from this session's answers"
    browser.search(refresh=True)
    assert started[-1][-1] == "--refresh"
    browser.cancel()
    assert browser.status.text() == "Search cancelled."
    browser.os_found(0, "{}", "")  # a late answer of the cancelled search changes nothing
    assert browser.status.text() == "Search cancelled."
    browser.query.setText("ffmpeg")
    browser.search()
    browser.os_found(1, "", "sandbox-ai images: the package index of localhost/claude-sandbox:latest could not be "
                            "fetched - no network? (...)")
    assert "could not be fetched - no network?" in browser.status.text()
    page.deleteLater()


# -- registries --

def test_the_predefined_registries():
    hosts = [host for host, _, _ in rb.REGISTRIES]
    assert hosts == ["docker.io", "ghcr.io", "quay.io", "gcr.io", "mirror.gcr.io", "public.ecr.aws",
                     "mcr.microsoft.com", "registry.gitlab.com", "registry.access.redhat.com"]
    assert {host for host, _, kind in rb.REGISTRIES if kind} == {"docker.io", "quay.io"}


def test_docker_hub_and_quay_searches(quiet):
    web = Web({"https://hub.docker.com/v2/search/repositories/": {"results": [
        {"repo_name": "redis", "short_description": "Redis is the world's fastest data platform", "star_count": 13000,
         "is_official": True},
        {"repo_name": "bitnami/redis", "short_description": "Bitnami Redis", "star_count": 300, "is_official": False}]},
        "https://quay.io/api/v1/find/repositories": {"results": [
            {"kind": "repository", "name": "keycloak", "namespace": {"name": "keycloak"}, "description": "Keycloak",
             "stars": 12}, {"kind": "user", "name": "x"}]}})
    assert rb.search("docker.io", "redis", web)[0] == {"name": "redis", "stars": 13000, "official": True,
                                                       "description": "Redis is the world's fastest data platform"}
    assert rb.search("quay.io", "keycloak", web) == [{"name": "keycloak/keycloak", "description": "Keycloak",
                                                      "stars": 12, "official": False}]


def test_tags_from_the_registrys_v2_api_with_its_anonymous_token(quiet):
    """ghcr.io and the others without a search: the name is looked up, its tags from /v2/NAME/tags/list after the
    anonymous token the registry's challenge names."""
    def tags(url, headers):
        if (headers or {}).get("Authorization") != "Bearer anon":
            return 401, {"www-authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                                             'scope="repository:astral-sh/uv:pull"'}, b""
        return 200, {}, {"name": "astral-sh/uv", "tags": ["0.5.10", "0.5.11", "latest", "sha256-x.sig"]}
    web = Web({"https://ghcr.io/v2/astral-sh/uv/tags/list": tags,
               "https://ghcr.io/token?service=ghcr.io&scope=repository%3Aastral-sh%2Fuv%3Apull": {"token": "anon"},
               "https://ghcr.io/v2/": (404, {}, b"")})
    assert rb.tags_of("ghcr.io", "astral-sh/uv", web) == ["sha256-x.sig", "latest", "0.5.11", "0.5.10"]
    assert rb.search("ghcr.io", "astral-sh/uv", web) == [{"name": "astral-sh/uv", "description": "", "stars": None,
                                                          "official": False}]
    with pytest.raises(LookupError):
        rb.search("ghcr.io", "nobody/nothing", web)
    with pytest.raises(ValueError, match="has no search"):
        rb.search("ghcr.io", "Not A Name!", web)
    assert rb.reference("docker.io", "redis", "7-alpine") == "docker.io/library/redis:7-alpine"


def test_the_registry_browser_hands_the_reference_to_the_pinned_pull(quiet, monkeypatch):
    """(c) Browse registries…: Docker Hub's search, a repository's tags, Add → the existing pull dialog with the
    reference filled in: `sandbox-ai images pull REF --as NAME --json`, UNCHECKED; your own registry is kept."""
    from sandbox_ai_app import state
    kept = {}
    monkeypatch.setattr(state, "current", lambda: type("S", (), {"get": lambda s, k, d=None: kept.get(k, d),
                                                                 "set": lambda s, k, v: kept.update({k: v})})())
    web = Web({"https://hub.docker.com/v2/search/repositories/": {"results": [
        {"repo_name": "redis", "short_description": "Redis", "star_count": 13000, "is_official": True}]},
        "https://hub.docker.com/v2/repositories/library/redis/tags": {"results": [{"name": "7-alpine"},
                                                                                {"name": "latest"}]}})
    dialog = rb.RegistryBrowser(None, web)
    assert dialog.registry.count() == 9 and dialog.host() == "docker.io"
    dialog.query.setText("redis")
    dialog.search()
    assert [dialog.results.item(0, c).text() for c in range(4)] == ["redis", "Redis", "13000", "official"]
    assert dialog.tags.count() == 2 and dialog.reference() == "docker.io/library/redis:latest"
    assert dialog.ok.isEnabled()
    dialog.tags.setCurrentRow(0)
    assert dialog.reference() == "docker.io/library/redis:7-alpine"
    dialog.new_registry.setText("https://registry.example.org:5000/")
    dialog.add_registry()
    assert kept["image_registries"] == ["registry.example.org:5000"]
    assert dialog.host() == "registry.example.org:5000" and dialog.registry.count() == 10
    dialog.new_registry.setText("not a host")
    dialog.add_registry()
    assert "Not a registry's host name" in dialog.status.text()
    dialog.registry.setCurrentIndex(0)
    dialog.query.setText("postgres")
    monkeypatch.setattr(ps, "http_get", Web({}))
    dialog.fetch = None
    dialog.search()
    assert dialog.status.text().startswith("No network: docker.io cannot be reached from this computer")
    dialog.deleteLater()

    page = ImagesPage(FakeSai(), lambda *a, **kw: None)
    pulled = []
    monkeypatch.setattr(rb, "RegistryBrowser", lambda parent: type("B", (), {
        "exec": lambda s: True, "reference": lambda s: "docker.io/library/redis:7-alpine", "deleteLater": lambda s: 0})())
    monkeypatch.setattr(widgets, "exec_dropped", lambda dialog: dialog.exec() if not hasattr(dialog, "argv") else (
        pulled.append(dialog.argv()) and False))
    page.browse_registries()
    assert pulled == [["images", "pull", "docker.io/library/redis:7-alpine", "--as", "redis", "--json"]]
    page.deleteLater()


# -- end to end: the real sandbox-ai, Podman faked --

HOME = os.environ.get("SANDBOX_AI_HOME")
e2e = pytest.mark.skipif(not HOME, reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


@pytest.fixture
def user(qapp, tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("live_fakes_for_browse", Path(HOME) / "tests/live_fakes.py")
    fakes = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fakes)
    host = fakes.Host(tmp_path)
    for name, value in host.env.items():
        if name.startswith(("FAKE_", "XDG_", "SANDBOX_AI_BUILD")) or name in ("HOME", "PATH"):
            monkeypatch.setenv(name, value)
    monkeypatch.chdir(host.workspace)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    return host


def run_now(argv):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout, done.stderr


@e2e
def test_e2e_the_extension_tab_lists_the_images_packages(user, tmp_path, monkeypatch):
    """User (a): "erscheint da gar kein Listing von möglichen zu installierenden Paketen". The real `sandbox-ai images
    search` answers the app's search of claude's image (its Debian index, read in a throwaway container); Add and
    Save put the package into the extension through `images extend`."""
    from sandbox_ai_app import backend
    user.add_image("localhost/claude-sandbox:latest")
    index = tmp_path / "index.txt"
    index.write_text("@@index\njq\t1.6-2.1\tlightweight and flexible command-line JSON processor\n")
    monkeypatch.setenv("FAKE_PODMAN_PACKAGE_INDEX", str(index))
    page = ImagesPage(backend.SandboxAI(), lambda *a, **kw: None)
    page.extension_shown(*run_now(page.run_argv("images", "extend", "claude", "--show", "--json")))
    assert page.target == "claude"
    browser = page.lists["apt"].browser
    browser.command.start = lambda argv: browser.os_found(*run_now(argv)) or True
    browser.query.setText("json")
    browser.search()
    assert browser.results.rowCount() == 1, browser.status.text()
    assert [browser.results.item(0, c).text() for c in range(3)] == [
        "jq", "1.6-2.1", "lightweight and flexible command-line JSON processor"]
    assert "packages of Debian GNU/Linux 12 (bookworm) (apt-get)" in browser.status.text()
    browser.results.selectRow(0)
    browser.add()
    assert page.lists["apt"].values() == ["jq"]
    argv, stdin = page.save_argv()
    assert argv[-4:] == ["claude", "--add-apt", "jq", "--json"] and stdin is None
    saved = run_now(argv)
    assert saved[0] == 0, saved[2]
    assert json.loads(run_now(page.run_argv("images", "extend", "claude", "--show", "--json"))[1])["apt"] == ["jq"]
    page.deleteLater()


@e2e
def test_e2e_your_alpine_image_gets_os_packages_with_apk(user, tmp_path, monkeypatch):
    """User (b): "nicht nur Debian, sondern z.B. auch Alpine Linux". Add OS packages… on your Alpine image: the search
    reads Alpine's index (apk), and the derive builds a new image of yours, recorded as derived and UNCHECKED."""
    from sandbox_ai_app import backend
    image = "localhost/sandbox-ai-image-tools:latest"
    record = user.home / ".config/sandbox-ai/user-images.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"version": 1, "images": {"tools": {
        "kind": "built", "image": image, "containerfile": "/x/Containerfile", "reviewed": False, "id": "f" * 64,
        "os": {"ID": "alpine", "VERSION_ID": "3.21.0", "PRETTY_NAME": "Alpine Linux v3.21"}, "ports": []}}}))
    user.add_image(image)
    index = tmp_path / "apk.txt"
    index.write_text("@@index\ncurl\t8.11.1-r0\tURL retrieval utility and library\n")
    monkeypatch.setenv("FAKE_PODMAN_PACKAGE_INDEX", str(index))
    page = ImagesPage(backend.SandboxAI(), lambda *a, **kw: None)
    page.user_images_listed(*run_now(page.run_argv("images", "list", "--json")))
    page.user_table.selectRow(0)
    assert page.derive_button.isEnabled()
    dialog = ps.DeriveDialog(page, page, "tools", "Alpine Linux v3.21", ["tools"])
    dialog.browser.command.start = lambda argv: dialog.browser.os_found(*run_now(argv)) or True
    dialog.browser.query.setText("curl")
    dialog.browser.search()
    assert dialog.browser.results.item(0, 0).text() == "curl", dialog.browser.status.text()
    assert "(apk)" in dialog.browser.status.text()
    dialog.browser.results.selectRow(0)
    dialog.browser.add()
    dialog.name.setText("tools-curl")
    assert dialog.ok.isEnabled()
    status, out, err = run_now(page.run_argv(*dialog.argv()))
    assert status == 0, err
    made = json.loads(out)
    assert made["derived"] == {"from": "tools", "family": "alpine", "packages": ["curl"]}
    assert made["reviewed"] is False and made["build_network"] is True
    build = [c for c in user.builds()][-1]
    assert "--network=none" not in build
    dialog.deleteLater()
    page.deleteLater()
