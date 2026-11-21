"""sandbox-ai packages: the offline copy of OSV's malicious packages (built from a small fake dump, signed with a
throwaway key, synced from a folder and over HTTP, refused when unsigned, tampered with or older), the per-project
allow-list, and the command itself through ./sandbox-ai."""
import functools
import gzip
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import release_kit  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_packages_under_test", ROOT / "sandbox/packages.py")
pk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pk)
rt = pk.runtime()

needs_ssh = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="needs ssh-keygen")


def advisory(ident, ecosystem, name, versions=None, events=None, **more):
    affected = {"package": {"ecosystem": ecosystem, "name": name}}
    if versions is not None:
        affected["versions"] = versions
    if events is not None:
        affected["ranges"] = [{"type": "SEMVER", "events": events}]
    return {"id": ident, "summary": f"Malicious code in {name}", "affected": [affected], **more}


ADVISORIES = [
    advisory("MAL-2025-1", "npm", "evil-pkg", ["0.0.1"], [{"introduced": "0"}]),        # the whole package
    advisory("MAL-2025-2", "npm", "left-pad", ["1.3.1"], [{"introduced": "1.3.1"}, {"fixed": "1.3.2"}]),
    advisory("MAL-2025-3", "PyPI", "Reqeusts", [], [{"introduced": "0"}]),
    advisory("MAL-2025-4", "PyPI", "requests", ["2.99.0", "2.99.1"]),
    advisory("MAL-2025-5", "crates.io", "rust_decimal_evil", ["1.0.0"], [{"introduced": "0"}]),
    advisory("MAL-2025-7", "Go", "github.com/evil/Mod", ["v1.0.1"]),
    advisory("MAL-2025-8", "npm", "@bad/scoped", None, [{"introduced": "2.0.0"}, {"fixed": "2.0.5"}]),  # bounds only
    advisory("MAL-2025-9", "npm", "taken-back", ["1.0.0"], withdrawn="2025-05-01T00:00:00Z"),
    advisory("GHSA-aaaa-bbbb-cccc", "npm", "lodash", ["4.17.20"]),                       # a vulnerability, not malware
    advisory("MAL-2025-10", "Maven", "org.evil:lib", ["1.0"]),                            # an ecosystem we do not guard
    advisory("MAL-2025-11", "npm", "bad\tname", ["1.0.0"]),                               # no package name
    advisory("MAL-2025-12", "npm", "left-pad", ["1.3.3"]),                               # a second advisory, one name
]


@pytest.fixture
def dump(tmp_path):
    """A small OSV dump: a folder of advisories, part of them in a .zip as OSV publishes them (ECOSYSTEM/all.zip)."""
    folder = tmp_path / "osv"
    folder.mkdir()
    with zipfile.ZipFile(folder / "npm.zip", "w") as archive:
        for entry in ADVISORIES[:2]:
            archive.writestr(f"{entry['id']}.json", json.dumps(entry))
        archive.writestr("GHSA-zzzz.json", "not read: no MAL- advisory")
    for entry in ADVISORIES[2:]:
        (folder / f"{entry['id']}.json").write_text(json.dumps(entry))
    (folder / "MAL-broken.json").write_text("{not json")
    return folder


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


@pytest.fixture(scope="module")
def key(tmp_path_factory):
    return release_kit.new_key(tmp_path_factory.mktemp("osv-key"))


def published(tmp_path, dump, key, name="published", generated="2026-11-10T00:00:00Z", sign=True):
    manifest, files = pk.build([dump], generated=generated)
    return pk.write_published(tmp_path / name, manifest, files, key.path if sign else None)


def test_a_copy_is_built_from_osv_advisories(dump):
    manifest, files = pk.build([dump], generated="2026-11-10T00:00:00Z")
    assert files["npm.list"].decode().splitlines() == [
        "@bad/scoped\tMAL-2025-8\t*",        # bounds without a list of versions: every version (the safe side)
        "evil-pkg\tMAL-2025-1\t*",
        "left-pad\tMAL-2025-12\t1.3.3",
        "left-pad\tMAL-2025-2\t1.3.1"]
    assert files["pypi.list"].decode().splitlines() == ["reqeusts\tMAL-2025-3\t*", "requests\tMAL-2025-4\t2.99.0,2.99.1"]
    assert files["crates.list"] == b"rust-decimal-evil\tMAL-2025-5\t*\n"
    assert files["go.list"] == b"github.com/evil/Mod\tMAL-2025-7\t1.0.1\n"
    assert manifest["format"] == 1 and manifest["generated"] == "2026-11-10T00:00:00Z"
    assert manifest["files"]["npm.list"]["entries"] == 4 and len(manifest["files"]) == 4
    # what the build wrote is what the proxy's lists read
    lists = {name[:-5]: rt.PackageList(data) for name, data in files.items()}
    assert lists["npm"].find("left-pad") == [("MAL-2025-12", {"1.3.3"}), ("MAL-2025-2", {"1.3.1"})]
    assert lists["npm"].find("lodash") == [] and lists["npm"].find("taken-back") == []
    (dump / "empty").mkdir()
    with pytest.raises(ValueError, match="no OSV advisory found"):
        pk.build([dump / "empty"])
    with pytest.raises(ValueError, match="not a folder"):
        pk.build([dump / "nothing-here"])


def test_a_list_that_would_hide_entries_is_refused():
    assert pk.check_list(b"") == 0 and pk.check_list(b"a\tMAL-1\t*\nb\tMAL-2\t1.0,1.1\n") == 2
    for data, why in ((b"b\tMAL-1\t*\na\tMAL-2\t*\n", "not sorted"), (b"a\tMAL-1\t*\na\tMAL-1\t*\n", "not sorted"),
                      (b"a\tMAL-1\t*", "cut"), (b"a\tMAL-1\n", "not NAME"), (b"a\t\t*\n", "not NAME"),
                      (b"a b\tMAL-1\t*\n", "not NAME"), (b"a\tMAL-1\t*\n\nb\tMAL-2\t*\n", "empty line"),
                      ("ä\tMAL-1\t*\n".encode(), "not ASCII"), (b"a\tMAL-1\t1.0,\n", "not NAME")):
        with pytest.raises(ValueError, match=why):
            pk.check_list(data, "x.list")


@needs_ssh
def test_sync_takes_a_signed_copy_and_refuses_everything_else(tmp_path, home, dump, key):
    keys = [(key.kind, key.key)]
    said = []
    info = pk.sync(published(tmp_path, dump, key), keys=keys, out=said.append)
    assert info["present"] and info["signed"] and info["signer"] == key.fingerprint and info["total"] == 8
    assert info["entries"] == {"npm": 4, "pypi": 2, "crates": 1, "go": 1} and info["generated"] == "2026-11-10T00:00:00Z"
    assert info["directory"] == str(tmp_path / "data/sandbox-ai/osv-malicious") and not info["problem"]
    assert "npm.list: 4 entries" in said
    lists = rt.open_package_lists(pk.mount_dir())
    assert lists["npm"].find("evil-pkg") == [("MAL-2025-1", None)]
    assert pk.lookup("npm:left-pad@1.3.1")["malicious"] == ["MAL-2025-2"]
    assert pk.lookup("npm:left-pad@1.3.0")["malicious"] == [] and pk.lookup("pypi:Reqeusts")["malicious"]
    before = (pk.data_dir() / "npm.list").read_bytes()

    def refused(source, why, **kwargs):
        with pytest.raises(ValueError, match=why):
            pk.sync(source, keys=kwargs.get("keys", keys), out=lambda line: None)
        assert (pk.data_dir() / "npm.list").read_bytes() == before  # the copy here stays as it was
    # not signed
    refused(published(tmp_path, dump, key, "unsigned", sign=False), "no signature .* an unsigned copy is never taken")
    # signed by a key this installation does not trust
    other = release_kit.new_key(tmp_path / "other-key")
    refused(published(tmp_path, dump, other, "other"), "not a trusted release key")
    # a release's own signature is none of a copy's (another namespace)
    replay = published(tmp_path, dump, key, "replay")
    (replay / pk.SIGNATURE).write_bytes(release_kit.rk.sign((replay / pk.MANIFEST).read_bytes(), key.path))
    refused(replay, "the signature is for 'sandbox-ai-release'")
    # the manifest changed after signing
    changed = published(tmp_path, dump, key, "changed")
    (changed / pk.MANIFEST).write_text((changed / pk.MANIFEST).read_text().replace("2026-11-10", "2026-11-11"))
    refused(changed, "does not match")
    # a list changed (an entry taken out): its checksum is in the signed manifest
    cut = published(tmp_path, dump, key, "cut")
    with gzip.open(cut / "osv-malicious-npm.list.gz", "wb") as stream:
        stream.write(b"left-pad\tMAL-2025-2\t1.3.1\n")
    refused(cut, "does not match its checksum")
    (cut / "osv-malicious-npm.list.gz").write_bytes(b"not gzip")
    refused(cut, "not a gzip file")
    # older than the copy here: a rollback would bring taken-out malware back
    refused(published(tmp_path, dump, key, "older", generated="2026-11-01T00:00:00Z"), "older .* than the one here")
    refused(tmp_path / "nowhere", "no such folder")
    refused("ftp://example.org/x", "https://")
    refused("http://example.org/x", "https://")
    refused(published(tmp_path, dump, key, "nokeys"), "knows no release key", keys=[])
    # a copy of your own making, not signed: only by the SHA-256 of its manifest, which you name
    own = published(tmp_path, dump, key, "own", generated="2026-11-11T00:00:00Z", sign=False)
    digest = hashlib.sha256((own / pk.MANIFEST).read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="does not have the SHA-256 you named"):
        pk.sync(own, keys=keys, out=lambda line: None, checksum="0" * 64)
    with pytest.raises(ValueError, match="64 hex digits"):
        pk.sync(own, keys=keys, out=lambda line: None, checksum="abc")
    info = pk.sync(own, keys=keys, out=lambda line: None, checksum=digest.upper())
    assert info["generated"] == "2026-11-11T00:00:00Z" and info["signed"] is False
    assert info["unsigned"] == "taken by the SHA-256 you named"
    assert "not signed (taken by the SHA-256 you named)" in "\n".join(pk.status_lines(info, colour=False))
    # the same age again, and a newer one, are taken
    assert pk.sync(published(tmp_path, dump, key, "newer", generated="2026-11-12T00:00:00Z"), keys=keys,
                   out=lambda line: None)["generated"] == "2026-11-12T00:00:00Z"
    assert not [path for path in pk.data_dir().parent.iterdir() if path.name != "osv-malicious"]  # nothing left over


@needs_ssh
def test_sync_over_http_fetches_whole_lists_and_names_no_package(tmp_path, home, dump, key):
    folder = published(tmp_path, dump, key)
    seen = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.path)
            super().do_GET()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        info = pk.sync(f"http://127.0.0.1:{server.server_address[1]}/", keys=[(key.kind, key.key)],
                       out=lambda line: None)
    finally:
        server.shutdown()
    assert info["total"] == 8 and info["source"] == f"http://127.0.0.1:{server.server_address[1]}"
    assert sorted(seen) == ["/osv-malicious-crates.list.gz", "/osv-malicious-go.list.gz",
                            "/osv-malicious-npm.list.gz", "/osv-malicious-pypi.list.gz", "/osv-malicious.json",
                            "/osv-malicious.json.sig"]
    assert not any(name in path for path in seen for name in ("evil", "left-pad", "requests"))
    assert pk.default_source().endswith("/releases/download/osv-malicious")


def test_the_copy_can_be_built_here_from_osvs_dumps_and_says_it_is_unsigned(tmp_path, home, dump):
    fetched = []

    def download(url, limit, into):
        fetched.append(url)
        with zipfile.ZipFile(into, "w") as archive:
            for entry in ADVISORIES:
                if pk.OSV_NAMES.get("npm") in url and entry["affected"][0]["package"]["ecosystem"] == "npm":
                    archive.writestr(f"{entry['id']}.json", json.dumps(entry))
    info = pk.sync_osv(out=lambda line: None, download=download)
    assert fetched == [f"https://osv-vulnerabilities.storage.googleapis.com/{name}/all.zip"
                       for name in ("npm", "PyPI", "crates.io", "Go")]  # whole ecosystems: no package is named
    assert info["present"] and not info["signed"] and info["entries"]["npm"] == 4 and info["entries"]["pypi"] == 0
    assert "not signed" in "\n".join(pk.status_lines(info, colour=False))
    assert not [path for path in pk.data_dir().parent.iterdir() if path.name != "osv-malicious"]  # the dumps are gone
    info = pk.sync_osv([dump], out=lambda line: None)
    assert info["total"] == 8 and "built here" in info["source"]


def test_where_the_administrator_requires_signatures_only_a_signed_copy_is_taken(tmp_path, home, dump, monkeypatch,
                                                                                 capsys):
    monkeypatch.setattr(pk, "signatures_required", lambda: True)
    for options in (["--osv", "--from", str(dump)], ["--from", str(dump), "--sha256", "0" * 64]):
        assert pk.main(["sync", *options], out=lambda line: None) == 2
        assert "requires signatures (require_signed_updates)" in capsys.readouterr().err
    assert pk.status()["present"] is False
    # the policy as the launcher reads it: none on this machine means no such demand; one that cannot be read, yes
    launcher = pk.module("agent_sandbox")
    monkeypatch.undo()
    monkeypatch.setattr(launcher, "load_policy", lambda path=None: {})
    assert pk.signatures_required() is False
    monkeypatch.setattr(launcher, "load_policy", lambda path=None: {"require_signed_updates": True})
    assert pk.signatures_required() is True

    def broken(path=None):
        raise SystemExit("Policy /etc/sandbox-ai/policy.json: not JSON")
    monkeypatch.setattr(launcher, "load_policy", broken)
    assert pk.signatures_required() is True


def test_the_status_needs_no_runtime(tmp_path, home, dump):
    """doctor, `packages status` and the note of a start read the copy's manifest: they load no 18000 lines for it;
    a start hands the module its own runtime instead of loading a second one."""
    pk.sync_osv([dump], out=lambda line: None)
    spec = importlib.util.spec_from_file_location("sandbox_packages_fresh", ROOT / "sandbox/packages.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    assert fresh.status()["total"] == 8 and fresh.mount_dir() == pk.data_dir() and fresh.sync_due() is False
    assert fresh._modules == {}, "no sibling module loaded for the status"
    assert fresh.ECOSYSTEMS == rt.PACKAGE_ECOSYSTEMS == tuple(pk.OSV_NAMES)
    assert fresh.stamp_seconds("2026-11-18T12:26:40Z") == rt.package_time("2026-11-18T12:26:40Z") == 1795004800
    assert fresh.stamp_seconds("yesterday") is None and fresh.stamp_seconds(None) is None
    launcher = pk.module("agent_sandbox")
    assert launcher.load_packages_module().runtime() is launcher.RUNTIME


def test_status_says_age_and_what_is_missing(tmp_path, home, dump):
    assert pk.status()["present"] is False and pk.mount_dir() is None
    assert "sandbox-ai packages sync" in "\n".join(pk.status_lines(pk.status(), colour=False))
    pk.sync_osv([dump], out=lambda line: None)
    made = rt.package_time(pk.status()["generated"])
    fresh, old = pk.status(now=made + 3 * 86400), pk.status(now=made + 20 * 86400)
    assert (fresh["age_days"], fresh["stale"]) == (3, False) and (old["age_days"], old["stale"]) == (20, True)
    assert "older than 14 days" in "\n".join(pk.status_lines(old, colour=False))
    assert pk.sync_due(now=made + 10) is False and pk.sync_due(now=made + 2 * 86400) is True
    (pk.data_dir() / "go.list").unlink()
    assert pk.status()["problem"] == "missing: go.list"
    (pk.data_dir() / pk.MANIFEST).write_text("{}")
    assert "not a copy" in pk.status()["problem"] and pk.status()["present"] is False


def test_the_allow_list_is_per_project_and_never_takes_a_malicious_version(tmp_path, home, dump):
    pk.sync_osv([dump], out=lambda line: None)
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    added, refused = pk.allow(one, ["npm:Left-Pad@1.3.2", "pypi:Requests", "npm:evil-pkg", "npm:left-pad@1.3.1"])
    assert added == ["npm:left-pad@1.3.2", "pypi:requests"]
    assert refused == [("npm:evil-pkg", "MAL-2025-1"), ("npm:left-pad@1.3.1", "MAL-2025-2")]
    assert pk.allow(one, ["npm:left-pad@1.3.2"]) == ([], [])  # there already
    pk.allow(two, ["crates:serde"])
    assert pk.allowed_for(one) == ["npm:left-pad@1.3.2", "pypi:requests"] and pk.allowed_for(two) == ["crates:serde"]
    assert pk.allowed_for(tmp_path / "three") == []
    assert os.stat(pk.allow_file()).st_mode & 0o777 == 0o600
    assert pk.forget(one, ["pypi:requests", "npm:nothing"]) == ["pypi:requests"]
    assert pk.allowed_for(one) == ["npm:left-pad@1.3.2"]
    with pytest.raises(ValueError, match="ECOSYSTEM:NAME"):
        pk.allow(one, ["left-pad"])
    # a damaged file is kept aside, never written over; entries that are no packages are not read
    pk.allow_file().write_text("[1, 2]")
    assert pk.read_allowed() == {} and pk.allow(one, ["go:github.com/x/y@v1.0.0"])[0] == ["go:github.com/x/y@1.0.0"]
    assert [path.name for path in pk.allow_file().parent.glob("packages.json.damaged-*")]
    data = json.loads(pk.allow_file().read_text())
    data["projects"][str(one.resolve())]["allow"] += ["bogus", 5, "npm:ok"]
    pk.allow_file().write_text(json.dumps(data))
    assert pk.allowed_for(one) == ["go:github.com/x/y@1.0.0", "npm:ok"]


def test_a_runs_settings_are_the_strictest_of_its_rulesets_and_mode():
    rulesets = [{"name": "policy", "packages": {"malicious": "block"}},
                {"name": "rules.json", "packages": {"min_age_days": 30}},
                {"name": "trial.json", "mode": "audit", "packages": {"min_age": "deny"}},
                {"name": "project", "packages": {"min_age_days": 3, "new_names": "report"}}, {"name": "none"}]
    settings = pk.proxy_settings(rulesets, {"new_names": "ask"}, "high", ["npm:a", "npm:a", "pypi:b"])
    assert {key: settings[key] for key in rt.PACKAGE_DEFAULTS} == {
        "malicious": "block", "min_age": "ask", "min_age_days": 30, "new_names": "ask"}
    assert settings["by"] == {"malicious": "policy", "min_age_days": "rules.json", "new_names": "mode high"}
    assert settings["allow"] == ["npm:a", "pypi:b"]
    assert settings["audit"] == [{"name": "trial.json", "min_age": "deny"}]
    assert "audit" not in pk.proxy_settings([]) and pk.proxy_settings([])["min_age_days"] == 14


# --- the command, through the real entry point -----------------------------------------------------------------

def run(home, *args, cwd=None):
    env = {**os.environ, "XDG_DATA_HOME": str(home / "data"), "XDG_CONFIG_HOME": str(home / "config"),
           "XDG_STATE_HOME": str(home / "state"), "NO_COLOR": "1"}
    return subprocess.run([str(ROOT / "sandbox-ai"), "packages", *args], capture_output=True, text=True, env=env,
                          cwd=cwd or home, timeout=120)


@needs_ssh
def test_the_command_end_to_end(tmp_path, dump, key):
    home = tmp_path
    done = run(home)
    assert done.returncode == 0 and "no copy of the known malicious packages" in done.stdout
    # a copy signed by a key this installation does not trust is refused by the real command too
    done = run(home, "build", "--from", str(dump), "--out", str(tmp_path / "pub"), "--sign", str(key.path))
    assert done.returncode == 0 and "osv-malicious.json.sig" in done.stdout, done.stderr
    done = run(home, "sync", "--from", str(tmp_path / "pub"))
    assert done.returncode == 2 and "not a trusted release key" in done.stderr and "not taken" in done.stderr
    assert run(home, "status", "--json").stdout and json.loads(run(home, "status", "--json").stdout)["present"] is False
    # built here from OSV dumps: taken, and said to be unsigned
    done = run(home, "sync", "--osv", "--from", str(dump))
    assert done.returncode == 0 and "nobody signed them" in done.stdout and "not signed" in done.stdout, done.stderr
    info = json.loads(run(home, "--json").stdout)
    assert info["present"] and info["total"] == 8 and info["signed"] is False
    done = run(home, "check", "npm:evil-pkg@9.9.9", "npm:left-pad@1.3.0")
    assert done.returncode == 1
    assert "npm:evil-pkg@9.9.9: KNOWN MALICIOUS: MAL-2025-1 (https://osv.dev/vulnerability/MAL-2025-1)" in done.stdout
    assert "npm:left-pad@1.3.0: not on the list" in done.stdout
    project = tmp_path / "project"
    project.mkdir()
    done = run(home, "allow", "npm:left-pad@1.3.2", "npm:evil-pkg", cwd=project)
    assert done.returncode == 1 and "allowed npm:left-pad@1.3.2 in every run of" in done.stdout
    assert "an allow-list never covers it" in done.stderr
    assert json.loads(run(home, "list", "--json", cwd=project).stdout) == {str(project.resolve()): ["npm:left-pad@1.3.2"]}
    assert "npm:left-pad@1.3.2" in run(home, "list", "--all").stdout and "(none)" in run(home, "list").stdout
    assert run(home, "forget", "npm:left-pad@1.3.2", "--project", str(project)).returncode == 0
    assert run(home, "forget", "npm:left-pad@1.3.2", "--project", str(project)).returncode == 1
    done = run(home, "allow", "leftpad")
    assert done.returncode == 2 and "ECOSYSTEM:NAME" in done.stderr
    assert "sync" in run(home, "--help").stdout
