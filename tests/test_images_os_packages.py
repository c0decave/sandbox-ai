"""The OS packages of an image (hotfix 0.1.96, user reports from live use of the app's Images page):

  (a) "Was mich wundert: wenn man auf Erweiterungen geht, erscheint da gar kein Listing von möglichen zu
      installierenden Paketen" - `sandbox-ai images search` reads the package index of the image's own distribution
      in a throwaway container of it (apt, apk, dnf, pacman, zypper), cached per image, and says so without network.
  (b) "Es kann sein, dass wir nicht nur Debian, sondern z.B. auch Alpine Linux haben … das müsste auch supported
      werden, und noch andere Container-Linux-Derivate." - `--add-os` (--add-apt stays) and `images derive`, which
      installs into one of your images with its own distribution's manager; what cannot work is refused, saying why.

Unit tests with recorded answers, then the real ./sandbox-ai with live_fakes.py's Podman."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

from live_fakes import Host

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("images_os_tests", ROOT / "sandbox/images.py")
im = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(im)
osp = im.os_packages_module()

ALPINE = {"ID": "alpine", "VERSION_ID": "3.21.0", "PRETTY_NAME": "Alpine Linux v3.21"}
DIGEST = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(im, "user_bus", lambda uid=None: True)
    return tmp_path


def record_image(config, name, release, image=None, user=""):
    """One of your images recorded as `images pull` records it."""
    path = config / "config" / "sandbox-ai" / "user-images.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    found = json.loads(path.read_text())["images"] if path.exists() else {}
    found[name] = {"kind": "pulled", "ref": f"{name}:latest", "image": image or f"docker.io/library/{name}@{DIGEST}",
                   "digest": DIGEST, "reviewed": False, "id": "b" * 64, "os": release, "ports": [],
                   "config_user": user, "platform": "linux/amd64"}
    path.write_text(json.dumps({"version": 1, "images": found}))


@pytest.mark.parametrize("release, family", [
    ({"ID": "debian", "VERSION_CODENAME": "bookworm"}, "debian"),
    ({"ID": "ubuntu", "ID_LIKE": "debian"}, "debian"),
    (ALPINE, "alpine"),
    ({"ID": "fedora"}, "fedora"),
    ({"ID": "rocky", "ID_LIKE": "rhel centos fedora"}, "fedora"),
    ({"ID": "amzn", "ID_LIKE": "centos rhel fedora"}, "fedora"),
    ({"ID": "arch"}, "arch"),
    ({"ID": "opensuse-tumbleweed", "ID_LIKE": "opensuse suse"}, "suse"),
    ({"ID": "sles"}, "suse"),
    ({"ID": "wolfi"}, "alpine"),
    ({"ID": "nixos"}, None),
    ({}, None),
])
def test_the_distribution_of_an_os_release(release, family):
    assert osp.family_of(release) == family


def test_each_distribution_installs_with_its_own_manager():
    """(b) Alpine, Fedora/RHEL, Arch and openSUSE get their own install line; Debian's is exactly the one of the
    extensions before (no extension's hash changes, no image is "changed: rebuild" after the update)."""
    assert osp.install_lines("debian", ["jq", "git=1:2.39"]) == [
        "RUN apt-get update && apt-get install -y --no-install-recommends \\", "    jq git=1:2.39 \\",
        "    && rm -rf /var/lib/apt/lists/*"]
    assert osp.install_lines("alpine", ["curl", "jq=1.7.1-r0"]) == ["RUN apk add --no-cache curl jq=1.7.1-r0"]
    fedora = "\n".join(osp.install_lines("fedora", ["jq"]))
    assert "dnf install -y --setopt=install_weak_deps=False jq && dnf clean all" in fedora
    assert "microdnf install -y jq" in fedora
    assert osp.install_lines("arch", ["jq"]) == ["RUN pacman -Syu --noconfirm --needed jq && "
                                                 "rm -rf /var/cache/pacman/pkg/*"]
    assert "zypper -n install --no-recommends jq" in osp.install_lines("suse", ["jq"])[0]


@pytest.mark.parametrize("family, value, ok", [
    ("debian", "build-essential", True), ("debian", "jq=1.6-2.1", True), ("debian", "-oDebug=1", False),
    ("alpine", "py3-pip", True), ("alpine", "curl=8.11.1-r0", True), ("alpine", "curl; rm -rf /", False),
    ("fedora", "python3-devel", True), ("fedora", "jq-1.7.1", True), ("fedora", "--nogpgcheck", False),
    ("arch", "base-devel", True), ("arch", "jq=1.7", False), ("suse", "gcc=13", True), ("suse", "../x", False),
])
def test_package_names_per_distribution(family, value, ok):
    assert (osp.check_name(family, value) is None) is ok


def test_the_index_of_each_distribution_is_read():
    assert osp.parse_index("debian", "noise\n@@index\njq\t1.6-2.1\tlightweight and flexible command-line JSON "
                                     "processor\njq\t1.6-2.1+deb12u1\tlightweight and flexible command-line JSON "
                                     "processor\nbad line\n") == [
        ("jq", "1.6-2.1+deb12u1", "lightweight and flexible command-line JSON processor")]
    assert osp.parse_index("alpine", "@@index\ncurl\t8.11.1-r0\tURL retrieval utility and library\n") == [
        ("curl", "8.11.1-r0", "URL retrieval utility and library")]
    # dnf 4 may print the tabs of its --qf as written, and an empty line after each
    assert osp.parse_index("fedora", "@@index\njq\\t1.7.1-8.fc41\\tCommand-line JSON processor\n\n") == [
        ("jq", "1.7.1-8.fc41", "Command-line JSON processor")]
    assert osp.parse_index("arch", "@@index\nextra/jq 1.7.1-2 [installed]\n    Command-line JSON processor\n"
                                   "core/curl 8.11.1-1\n    command line tool and library for transferring data\n") == [
        ("curl", "8.11.1-1", "command line tool and library for transferring data"),
        ("jq", "1.7.1-2", "Command-line JSON processor")]
    assert osp.parse_index("suse", "@@index\nS  | Name | Summary                     | Type\n---+------+------------"
                                   "-----+--------\n   | jq   | A lightweight and flexible command-line JSON "
                                   "processor | package\n") == [
        ("jq", "", "A lightweight and flexible command-line JSON processor")]


def test_a_search_puts_the_exact_name_first():
    rows = [("libjq1", "1", "jq library"), ("jq", "1", "JSON processor"), ("jqp", "1", "jq playground"),
            ("yq", "1", "like jq for YAML"), ("curl", "1", "transfers")]
    assert [r[0] for r in osp.search(rows, "jq")] == ["jq", "jqp", "libjq1", "yq"]
    assert [r[0] for r in osp.search(rows, "jq yaml")] == ["yq"]
    assert len(osp.search(rows, "", limit=2)) == 2


def test_add_os_is_add_apt(config):
    """--add-os is the generic name; --add-apt stays (scripts, the web interface, the app)."""
    assert im.main(["extend", "claude", "--add-os", "jq", "--add-apt", "ripgrep"], out=lambda *_: None) == 0
    assert im.describe("claude")["apt"] == ["jq", "ripgrep"]
    assert im.main(["extend", "claude", "--remove-os", "jq"], out=lambda *_: None) == 0
    assert im.describe("claude")["apt"] == ["ripgrep"]


def test_the_distribution_of_targets_and_of_your_images(config):
    assert im.distribution("claude")["family"] == "debian"
    assert im.distribution("claude")["manager"] == "apt-get"
    record_image(config, "alp", ALPINE)
    found = im.distribution("alp")
    assert (found["family"], found["manager"], found["system"], found["kind"]) == \
        ("alpine", "apk", "Alpine Linux v3.21", "image")
    with pytest.raises(ValueError, match="no image nothing of yours"):
        im.distribution("nothing")


class Engine:
    """Podman as recorded: the image is there; the package index run prints INDEX or fails with STATUS."""

    def __init__(self, index="", status=0):
        self.index, self.status, self.calls = index, status, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([{"Id": "c" * 64}]), "")
        if argv[1] == "run":
            return subprocess.CompletedProcess(argv, self.status, "" if self.status else self.index,
                                               "W: Failed to fetch" if self.status else "")
        raise AssertionError(argv)

    def runs(self):
        return [argv for argv in self.calls if argv[1] == "run"]


def test_the_index_is_read_once_per_image_and_cached(config):
    """(a) The first search reads the index (apt-get update in a throwaway container: --rm, the image as it is,
    never pulled); the next ones search the cache."""
    engine = Engine("@@index\njq\t1.6-2.1\tJSON processor\nripgrep\t13.0.0-4\trecursive grep\n")
    found = im.package_search("claude", "jq", run=engine)
    assert found["results"] == [{"name": "jq", "version": "1.6-2.1", "summary": "JSON processor"}]
    assert (found["family"], found["manager"], found["count"]) == ("debian", "apt-get", 2)
    (run,) = engine.runs()
    assert "--rm" in run and "--pull=never" in run and "--entrypoint=/bin/sh" in run
    assert run[run.index("--entrypoint=/bin/sh") + 1] == "localhost/claude-sandbox:latest"
    assert "apt-get" in run[-1] and "apt-cache dumpavail" in run[-1]
    assert im.package_search("claude", "grep", run=engine)["results"][0]["name"] == "ripgrep"
    assert len(engine.runs()) == 1, "searched in the cache"
    im.package_search("claude", "grep", refresh=True, run=engine)
    assert len(engine.runs()) == 2


def test_without_network_the_search_says_so(config):
    """(a) "works without network (says so)": no index yet - the error names the network; an index cached
    before is used, with a note."""
    with pytest.raises(im.IndexUnavailable, match="no network"):
        im.package_search("claude", "jq", run=Engine(status=3))
    im.package_search("claude", "jq", run=Engine("@@index\njq\t1\tJSON\n"))
    found = im.package_search("claude", "jq", refresh=True, run=Engine(status=3))
    assert found["results"][0]["name"] == "jq" and "no network" in found["stale"]


def test_your_alpine_image_is_searched_with_apk(config):
    record_image(config, "alp", ALPINE)
    engine = Engine("@@index\ncurl\t8.11.1-r0\tURL retrieval utility and library\n")
    found = im.package_search("alp", "curl", run=engine)
    assert found["family"] == "alpine" and found["results"][0]["version"] == "8.11.1-r0"
    (run,) = engine.runs()
    assert "apk update" in run[-1] and f"docker.io/library/alp@{DIGEST}" in run


def test_an_unknown_distribution_is_refused_clearly(config):
    record_image(config, "nix", {"ID": "nixos", "PRETTY_NAME": "NixOS 24.11"})
    with pytest.raises(ValueError, match="NixOS 24.11: sandbox-ai knows the packages of Debian/Ubuntu"):
        im.package_search("nix", "jq", run=Engine())
    with pytest.raises(ValueError, match="write a Containerfile"):
        im.derive("nix2", "nix", ["jq"])


def test_derive_installs_into_your_alpine_image_with_apk(config, monkeypatch):
    """(b) Your Alpine image with OS packages: apk add, its own user again after, built with the network an
    extension's build has, recorded as one of your images (unchecked) that says where it comes from."""
    record_image(config, "redis", ALPINE, user="redis")
    built = {}

    def build(name, file, context=None, replace=False, run=None, out=print, network=False, derived=None, **kw):
        built.update(name=name, text=Path(file).read_text(), network=network, derived=derived, replace=replace)
        return name, {"derived": derived}
    monkeypatch.setattr(im.services_module(), "build", build)
    im.derive("redis-tools", "redis", ["curl", "jq=1.7.1-r0"], out=lambda *_: None)
    assert built["text"].splitlines() == [
        f"# Your image redis (docker.io/library/redis@{DIGEST}) with Alpine packages: generated by sandbox-ai images "
        "derive (UNCHECKED, as the image)",
        f"FROM docker.io/library/redis@{DIGEST}", "USER root", "RUN apk add --no-cache curl jq=1.7.1-r0", "USER redis"]
    assert built["network"] is True
    assert built["derived"] == {"from": "redis", "family": "alpine", "packages": ["curl", "jq=1.7.1-r0"]}
    with pytest.raises(ValueError, match="is not an Alpine package name"):
        im.derive("x", "redis", ["curl;id"])


def test_derive_refuses_npm_and_pip_saying_why(config):
    """(b) "Refuse clearly where something can't work": npm/pip need Node.js/Python in the image, and on Alpine
    (musl) wheels built for glibc do not install."""
    record_image(config, "alp", ALPINE)
    for option in ("--add-npm", "--add-pip"):
        assert im.main(["derive", "x", "--from", "alp", option, "ruff"], out=lambda *_: None) == 1
    with pytest.raises(ValueError, match="musl libc"):
        im.derive("x", "alp", [], pip=["ruff"])


def test_a_variant_on_an_alpine_image_stays_refused(config):
    """The agent images stay Debian: the agent's layers (Firejail, nftables) are built for Debian 12, so a variant's
    base must be one - Alpine is refused there, saying what to take."""
    record_image(config, "alp", ALPINE)
    with pytest.raises(ValueError, match="Debian 12 \\(bookworm\\)"):
        im.new_variant("onalp", "claude", base="alp")


# -- end to end: the real ./sandbox-ai, Podman faked (live_fakes.py) --

@pytest.fixture
def host(tmp_path):
    return Host(tmp_path / "host")


def test_e2e_search_add_os_and_offline(host, tmp_path):
    """User (a): no listing of installable packages in the Extension tab. `sandbox-ai images search claude jq --json`
    lists them (name, version, summary) from the image's own index; `--add-os` adds one; without network it says so."""
    host.add_image("localhost/claude-sandbox:latest")
    index = tmp_path / "index.txt"
    index.write_text("@@index\njq\t1.6-2.1\tlightweight and flexible command-line JSON processor\n"
                     "jo\t1.9-1\tJSON output from a shell\n")
    result = host.run("images", "search", "claude", "jq", "--json", env={"FAKE_PODMAN_PACKAGE_INDEX": str(index)})
    assert result.returncode == 0, result.stderr
    found = json.loads(result.stdout)
    assert found["results"][0] == {"name": "jq", "version": "1.6-2.1",
                                   "summary": "lightweight and flexible command-line JSON processor"}
    assert found["system"] == "Debian GNU/Linux 12 (bookworm)" and found["manager"] == "apt-get"
    runs = [c for c in host.calls() if c[:1] == ["run"] or "run" in c[:3]]
    assert len(runs) == 1
    # cached: no network needed the second time
    again = host.run("images", "search", "claude", "JSON", env={"FAKE_PODMAN_PACKAGE_INDEX_STATUS": "3"})
    assert again.returncode == 0 and "jq" in again.stdout and "jo" in again.stdout
    offline = host.run("images", "search", "claude", "jq", "--refresh", "--json",
                       env={"FAKE_PODMAN_PACKAGE_INDEX_STATUS": "3"})
    assert offline.returncode == 0 and "no network" in json.loads(offline.stdout)["stale"]
    added = host.run("images", "extend", "claude", "--add-os", "jq", "--json")
    assert added.returncode == 0, added.stderr
    assert json.loads(added.stdout)["apt"] == ["jq"]
    assert "apt-get install -y --no-install-recommends \\\n    jq \\" in json.loads(added.stdout)["containerfile"]


def test_e2e_search_without_network_and_without_an_index(host):
    host.add_image("localhost/claude-sandbox:base")  # not built further: the base's repositories are the same
    result = host.run("images", "search", "claude", "jq", env={"FAKE_PODMAN_PACKAGE_INDEX_STATUS": "3"})
    assert result.returncode == 1
    assert "could not be fetched - no network?" in result.stderr


def test_e2e_your_alpine_image_os_and_search(host, tmp_path):
    """User (b): "nicht nur Debian, sondern z.B. auch Alpine Linux". Your Alpine image: `images os` names apk, the
    search reads Alpine's index."""
    image = f"docker.io/library/alpine@{DIGEST}"
    path = host.home / ".config/sandbox-ai/user-images.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "images": {"alpine": {
        "kind": "pulled", "ref": "alpine:3.21", "image": image, "digest": DIGEST, "reviewed": False, "id": "f" * 64,
        "os": ALPINE, "ports": []}}}))
    host.add_image(image)
    shown = host.run("images", "os", "alpine")
    assert shown.returncode == 0 and "OS packages install with apk (Alpine)" in shown.stdout, shown.stderr
    index = tmp_path / "apk.txt"
    index.write_text("@@index\ncurl\t8.11.1-r0\tURL retrieval utility and library\n")
    found = host.run("images", "search", "alpine", "curl", "--json", env={"FAKE_PODMAN_PACKAGE_INDEX": str(index)})
    assert found.returncode == 0, found.stderr
    assert json.loads(found.stdout)["results"][0]["name"] == "curl"
    refused = host.run("images", "derive", "x", "--from", "alpine", "--add-pip", "ruff")
    assert refused.returncode == 1 and "musl libc" in refused.stderr
