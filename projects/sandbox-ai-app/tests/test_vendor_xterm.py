"""scripts/vendor_xterm.py: pinned, verified, nothing but the needed regular files (no network here:
tarballs are built by the tests and their integrity is computed from them)."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vendor_xterm.py"
spec = importlib.util.spec_from_file_location("vendor_xterm", SCRIPT)
vx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vx)


def tarball(entries):
    """A .tgz of ENTRIES: [(name, bytes)] for files, [(name, ("symlink"|"hardlink"|"dir", target))]."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in entries:
            info = tarfile.TarInfo(name)
            if isinstance(content, tuple):
                kind, target = content
                info.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "dir": tarfile.DIRTYPE}[kind]
                info.linkname = target
                archive.addfile(info)
            else:
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


PINNED = json.loads((SCRIPT.parents[1] / "vendor" / "xterm.json").read_text())["packages"]
XV, FV = PINNED["@xterm/xterm"]["version"], PINNED["@xterm/addon-fit"]["version"]
XTERM = [("package/lib/xterm.js", b"window.Terminal = 1;"), ("package/css/xterm.css", b".xterm{}"),
         ("package/LICENSE", b"MIT"), ("package/package.json", b"{}")]
FIT = [("package/lib/addon-fit.js", b"window.FitAddon = 1;"), ("package/LICENSE", b"MIT fit")]


@pytest.fixture
def setup(tmp_path):
    def make(xterm=XTERM, fit=FIT, pin=True, registry_integrity=None):
        tarballs = {vx.tarball_url("@xterm/xterm", XV): tarball(xterm),
                    vx.tarball_url("@xterm/addon-fit", FV): tarball(fit)}
        pins = json.loads((SCRIPT.parents[1] / "vendor" / "xterm.json").read_text())
        for name, url in (("@xterm/xterm", vx.tarball_url("@xterm/xterm", XV)),
                          ("@xterm/addon-fit", vx.tarball_url("@xterm/addon-fit", FV))):
            pins["packages"][name]["integrity"] = vx.sri(tarballs[url]) if pin else ""
        path = tmp_path / "pins.json"
        path.write_text(json.dumps(pins))
        requested = []

        def get(url, limit=vx.TARBALL_LIMIT):
            requested.append(url)
            if url in tarballs:
                return tarballs[url]
            for name, version in (("@xterm/xterm", XV), ("@xterm/addon-fit", FV)):
                if url == f"{vx.REGISTRY}/{name.replace('/', '%2F')}/{version}":
                    integrity = registry_integrity or vx.sri(tarballs[vx.tarball_url(name, version)])
                    return json.dumps({"name": name, "version": version, "dist": {
                        "tarball": vx.tarball_url(name, version), "integrity": integrity}}).encode()
            raise AssertionError(f"unexpected request {url}")
        return path, tmp_path / "out", get, requested
    return make


def test_the_pin_file_has_no_invented_hashes():
    pins = json.loads((SCRIPT.parents[1] / "vendor" / "xterm.json").read_text())
    assert set(pins["packages"]) == {"@xterm/xterm", "@xterm/addon-fit"}
    for pin in pins["packages"].values():
        assert pin["integrity"] == "" or pin["integrity"].startswith("sha512-")
    # The values in use, as the lock files they were taken from give them (see the comment in the pin file).
    assert PINNED["@xterm/xterm"]["integrity"] in ("", "sha512-TQwDdQGtwwDt+2cgKDLn0IRaSxYu1tSUjgKarSDkUM0ZNiSRXFpjxEsvc/"
                                                       "Zgc5kq5omJ+V0a8/kIM2WD3sMOYg==")


def test_metadata_that_is_not_json_is_named(tmp_path):
    def get(url, limit=0):
        return b" <html>captive portal</html>"
    with pytest.raises(vx.Refused, match=r"did not answer with JSON \(first bytes: ' <html>captive"):
        vx.registry_dist("@xterm/xterm", XV, get)


def test_pinned_packages_are_verified_and_only_needed_files_written(setup):
    pins, out, get, requested = setup()
    lines = []
    manifest = vx.vendor(pins, out, get=get, out=lines.append)
    assert sorted(p.name for p in out.iterdir()) == ["LICENSE.addon-fit", "LICENSE.xterm", "addon-fit.js",
                                                    "manifest.json", "xterm.css", "xterm.js"]
    assert (out / "xterm.js").read_bytes() == b"window.Terminal = 1;"
    assert manifest["@xterm/xterm"]["files"]["xterm.js"] == hashlib.sha256(b"window.Terminal = 1;").hexdigest()
    assert not any("::warning" in line for line in lines)
    assert all(url.endswith(".tgz") for url in requested), "pinned packages need no registry metadata"


def test_a_mismatch_is_refused_and_nothing_written(setup):
    pins, out, get, _ = setup()
    data = json.loads(pins.read_text())
    data["packages"]["@xterm/addon-fit"]["integrity"] = vx.sri(b"something else")
    pins.write_text(json.dumps(data))
    with pytest.raises(vx.Refused, match="does not match"):
        vx.vendor(pins, out, get=get, out=lambda line: None)
    assert not out.exists()
    for bad in ("sha1-" + "A" * 28, "sha256-abc", "sha512-notbase64!!", "md5-x"):
        with pytest.raises(vx.Refused):
            vx.check_integrity(b"x", bad)


def test_unpinned_uses_the_registry_value_with_a_warning(setup):
    pins, out, get, requested = setup(pin=False)
    lines = []
    vx.vendor(pins, out, get=get, out=lines.append)
    warnings = [line for line in lines if line.startswith("::warning")]
    assert len(warnings) == 2 and all('"integrity": "sha512-' in w for w in warnings)
    assert any(url.endswith("/" + XV) for url in requested)
    with pytest.raises(vx.Refused, match="no pinned integrity"):
        vx.vendor(pins, out, require_pins=True, get=get, out=lines.append)


def test_unpinned_still_verified_against_the_registry_value(setup):
    pins, out, get, _ = setup(pin=False, registry_integrity=vx.sri(b"another tarball"))
    with pytest.raises(vx.Refused, match="does not match"):
        vx.vendor(pins, out, get=get, out=lambda line: None)
    assert not out.exists()


@pytest.mark.parametrize("bad", [
    [("package/lib/xterm.js", ("symlink", "/etc/passwd"))],
    [("package/lib/xterm.js", ("hardlink", "package/LICENSE"))],
    [("package/lib/xterm.js", ("dir", ""))],
    [("package/lib/xterm.js", b"one"), ("package/lib/xterm.js", b"two")],
])
def test_links_and_duplicates_are_refused(setup, bad):
    entries = [e for e in XTERM if e[0] != "package/lib/xterm.js"] + bad
    pins, out, get, _ = setup(xterm=entries)
    with pytest.raises(vx.Refused):
        vx.vendor(pins, out, get=get, out=lambda line: None)
    assert not out.exists()


def test_traversal_entries_are_never_written(setup, tmp_path):
    entries = XTERM + [("package/../../evil.js", b"evil"), ("/abs/evil.js", b"evil"), ("../evil.js", b"evil")]
    pins, out, get, _ = setup(xterm=entries)
    vx.vendor(pins, out, get=get, out=lambda line: None)
    assert not list(tmp_path.rglob("evil.js"))
    missing = [e for e in XTERM if e[0] != "package/css/xterm.css"] + [("package/css/../css/xterm.css", b"x")]
    pins, out, get, _ = setup(xterm=missing)
    with pytest.raises(vx.Refused, match="not in the tarball"):
        vx.vendor(pins, tmp_path / "out2", get=get, out=lambda line: None)


def test_only_registry_urls_are_fetched():
    for url in ("http://registry.npmjs.org/x.tgz", "https://evil.example/x.tgz", "file:///etc/passwd"):
        with pytest.raises(vx.Refused):
            vx.fetch(url)
    assert vx.tarball_url("@xterm/xterm", "5.5.0") == "https://registry.npmjs.org/@xterm/xterm/-/xterm-5.5.0.tgz"


def test_invalid_pins_are_refused(setup):
    pins, out, get, _ = setup()
    data = json.loads(pins.read_text())
    data["packages"]["@xterm/xterm"]["files"]["lib/xterm.js"] = "../escape.js"
    pins.write_text(json.dumps(data))
    with pytest.raises(vx.Refused):
        vx.vendor(pins, out, get=get, out=lambda line: None)
    data["packages"]["@xterm/xterm"]["files"]["lib/xterm.js"] = "xterm.js"
    data["packages"]["@xterm/xterm"]["version"] = "latest"
    pins.write_text(json.dumps(data))
    with pytest.raises(vx.Refused):
        vx.vendor(pins, out, get=get, out=lambda line: None)
