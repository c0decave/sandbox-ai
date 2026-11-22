"""sandbox-ai toolchain (sandbox/toolchains.py): the manifest checked value by value, the catalog's signature (the
MCP catalog's own key), the store (SHA-256 before anything is unpacked, unpacked without trusting it), what a
toolchain needs from the system, the wrappers and variables, the start's plan, and attach/detach with the namespace
helper faked (as tests/test_devices.py; the live check toolchain-live runs it for real)."""
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_toolchains_tests", ROOT / "sandbox/toolchains.py")
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv(t.LOCAL_CATALOGS_VARIABLE, raising=False)
    yield tmp_path / "data"
    # Unpacked trees are read-only (as installed): writable again, or pytest cannot remove tmp_path.
    for directory, dirs, _ in os.walk(tmp_path):
        for name in dirs:
            child = os.path.join(directory, name)
            if not os.path.islink(child):
                os.chmod(child, 0o700)


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory):
    directory = tmp_path_factory.mktemp("toolchain-key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(directory / "key")],
                   check=True)
    kind, key = (directory / "key.pub").read_text().split()[:2]
    return directory / "key", [(kind, key)]


def archive(members, mode="w:xz"):
    """A tar archive of MEMBERS: {name: bytes (a file, executable when below bin/) or None (a folder)}."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as tar:
        for name, value in members.items():
            info = tarfile.TarInfo(name)
            if value is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                info.size, info.mode = len(value), 0o755 if "bin/" in name else 0o644
                tar.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


GO = {"bin": None, "bin/go": b"#!/bin/sh\necho go\n", "bin/gofmt": b"#!/bin/sh\necho gofmt\n", "VERSION": b"go1\n"}


def entry(tmp_path, name="go", version="1.25.4", members=None, **more):
    """A catalog entry of NAME with its archive written to TMP_PATH."""
    data = archive(members or GO)
    path = tmp_path / f"toolchain-{name}-{version}.tar.xz"
    path.write_bytes(data)
    return {"name": name, "version": version, "arch": t.machine(), "url": str(path),
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "unpacked": 100, "title": name.title(),
            "description": f"{name} for tests", "license": "BSD-3-Clause", "homepage": "https://example.org",
            "commands": {"go": "bin/go", "gofmt": "bin/gofmt"},
            "env": {}, "caches": {}, "hosts": ["proxy.golang.org", "sum.golang.org"], "allow": [],
            "needs": {"glibc": "2.17", "libraries": []}, "requires": [], "sources": [], **more}


def catalog(tmp_path, entries, key=None, name="toolchains.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"schema": t.SCHEMA, "version": 1, "toolchains": entries}))
    Path(str(path) + ".sig").unlink(missing_ok=True)
    if key is not None:
        k = t.module("release_keys")
        Path(str(path) + ".sig").write_bytes(k.sign(path.read_bytes(), key, k.MCP))
    return str(path)


# --- the manifest ---------------------------------------------------------------------------------------------


def test_a_manifest_is_checked_value_by_value(tmp_path):
    good = entry(tmp_path, env={"GOFLAGS": "-mod=mod", "JAVA_HOME": "{prefix}"}, caches={"NPM_CONFIG_PREFIX": ".local"})
    checked = t.validated(good)
    assert checked["commands"] == {"go": "bin/go", "gofmt": "bin/gofmt"} and checked["needs"]["glibc"] == "2.17"
    assert "_signature" not in t.validated({**good, "_signature": {"state": "good"}})


@pytest.mark.parametrize("change, why", [
    ({"name": "Go"}, "name must be"), ({"name": "bin"}, "name must be"), ({"name": "../x"}, "name must be"),
    ({"version": "1 2"}, "version must be"), ({"arch": "mips"}, "arch is one of"),
    ({"url": "http://example.org/x.tar.xz"}, "url must be"), ({"url": "ftp://x"}, "url must be"),
    ({"sha256": "abc"}, "sha256 must be"), ({"size": -1}, "size must be"),
    ({"commands": {}}, "commands must map"), ({"commands": {"go": "/usr/bin/go"}}, "no path below"),
    ({"commands": {"go": "bin/../../x"}}, "no path below"), ({"commands": {"a b": "bin/go"}}, "commands must map"),
    ({"commands": {"../go": "bin/go"}}, "commands must map"), ({"commands": {"go": "bin/$(id)"}}, "no path below"),
    ({"env": {"LD_PRELOAD": "x"}}, "may not set LD_PRELOAD"), ({"env": {"PATH": "/x"}}, "may not set PATH"),
    ({"env": {"BASH_ENV": "/x"}}, "may not set BASH_ENV"), ({"env": {"NODE_OPTIONS": "-r x"}}, "may not set"),
    ({"env": {"HTTPS_PROXY": "http://x"}}, "may not set"), ({"env": {"A": "{home}"}}, "only {prefix}"),
    ({"env": {"A": "x\ny"}}, "env must map"), ({"env": {"1A": "x"}}, "env must map"),
    ({"caches": {"GOCACHE": "/tmp/x"}}, "no path below the sandbox's HOME"),
    ({"caches": {"GOCACHE": "../x"}}, "no path below the sandbox's HOME"),
    ({"env": {"A": "1"}, "caches": {"A": "x"}}, "is in env and in caches"),
    ({"hosts": ["exa mple.org"]}, "hosts must be"), ({"hosts": ["https://example.org"]}, "hosts must be"),
    ({"hosts": "example.org"}, "hosts must be"), ({"allow": ["--allow-github"]}, "allow must name"),
    ({"needs": {"glibc": "new"}}, "needs.glibc"), ({"needs": {"libraries": ["../libx.so"]}}, "needs.libraries"),
    ({"needs": {"kernel": "6"}}, "needs is"), ({"requires": ["go"]}, "requires itself"),
    ({"requires": ["Rust"]}, "requires must name"), ({"sources": [{"url": "x"}]}, "sources must be"),
    ({"surprise": 1}, "unknown key"),
])
def test_what_a_manifest_may_not_be(tmp_path, change, why):
    with pytest.raises(ValueError, match=why):
        t.validated({**entry(tmp_path), **change})


def test_a_name_and_version_as_given():
    assert t.spec("go") == ("go", None) and t.spec("jdk@21.0.9+10") == ("jdk", "21.0.9+10")
    for bad in ("Go", "bin", "go@", "go@1 2", "../go", ""):
        with pytest.raises(ValueError):
            t.spec(bad)


# --- the catalog and its signature ----------------------------------------------------------------------------


def test_a_signed_catalog_is_read_and_its_newest_entry_found(tmp_path, signing_key):
    key, trusted = signing_key
    other = "aarch64" if t.machine() == "x86_64" else "x86_64"
    source = catalog(tmp_path, [entry(tmp_path), entry(tmp_path, version="1.9.7"), entry(tmp_path, version="1.26.0"),
                                {**entry(tmp_path, name="zig"), "arch": other}, {"name": "broken"}], key)
    entries, signature, problems = t.read_catalog(source, keys=trusted)
    assert signature["state"] == "good" and signature["signer"].startswith("SHA256:")
    assert len(entries) == 4 and len(problems) == 1 and "left out" in problems[0]
    assert t.find_entry(entries, "go")["version"] == "1.26.0"  # by its numbers: 1.26 after 1.9
    assert t.find_entry(entries, "go", "1.9.7")["version"] == "1.9.7"
    with pytest.raises(ValueError, match="not for this machine"):
        t.find_entry(entries, "zig")
    with pytest.raises(ValueError, match="no toolchain rust in the catalog"):
        t.find_entry(entries, "rust")
    with pytest.raises(ValueError, match="not 2.0"):
        t.find_entry(entries, "go", "2.0")
    rows = t.catalog_listing(entries)
    assert [row["version"] for row in rows] == ["1.26.0", "1.25.4", "1.9.7"] and all(row["signed"] for row in rows)


def test_what_is_no_catalog(tmp_path):
    for text in ("{", "[]", json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": []})):
        (tmp_path / "x.json").write_text(text)
        with pytest.raises(ValueError, match="not JSON|not a toolchain catalog"):
            t.read_catalog(str(tmp_path / "x.json"))
    with pytest.raises(ValueError, match="an https URL or a file"):
        t.read_catalog("http://example.org/toolchains.json")


def test_add_refuses_an_unsigned_catalog_and_never_takes_a_bad_signature(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    said = []
    source = catalog(tmp_path, [entry(tmp_path)])
    with pytest.raises(ValueError, match="not signed by a trusted key.*--unsigned adds it anyway"):
        t.add("go", source, out=said.append, keys=trusted)
    assert not t.installed()
    # signed by a key nobody trusts: unsigned is not enough
    stranger = tmp_path / "stranger"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(stranger)], check=True)
    source = catalog(tmp_path, [entry(tmp_path)], stranger)
    with pytest.raises(ValueError, match="signature does not verify.*nothing of it is used"):
        t.add("go", source, unsigned=True, out=said.append, keys=trusted)
    # a signature over other bytes (the catalog changed after it was signed)
    source = catalog(tmp_path, [entry(tmp_path)], key)
    Path(source).write_text(Path(source).read_text().replace("BSD-3-Clause", "MIT"))
    with pytest.raises(ValueError, match="signature does not verify"):
        t.add("go", source, unsigned=True, out=said.append, keys=trusted)
    assert not t.installed() and not list(t.store().glob("[0-9a-f]*"))
    # --unsigned takes an unsigned one, and the store remembers that nobody signed it
    t.add("go", catalog(tmp_path, [entry(tmp_path)]), unsigned=True, out=said.append, keys=trusted)
    assert t.installed()["go"]["1.25.4"]["signed_by"] is None and any("added unsigned" in line for line in said)


def test_the_signature_is_the_mcp_catalogs_own_trust(tmp_path, signing_key):
    """One key ring for both catalogs: a signature in the release namespace is not taken."""
    key, trusted = signing_key
    k = t.module("release_keys")
    source = catalog(tmp_path, [entry(tmp_path)])
    Path(source + ".sig").write_bytes(k.sign(Path(source).read_bytes(), key, k.RELEASE))
    _, signature, _ = t.read_catalog(source, keys=trusted)
    assert signature["state"] == "bad" and "sandbox-ai-mcp" in signature["reason"]


# --- the store --------------------------------------------------------------------------------------------------


def test_add_checks_the_hash_unpacks_read_only_and_remembers_the_signer(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    said = []
    source = catalog(tmp_path, [entry(tmp_path)], key)
    done = t.add("go", source, out=said.append, keys=trusted)
    found = t.installed()["go"]["1.25.4"]
    tree = Path(found["path"])
    assert tree == t.store() / found["sha256"] and done[0]["path"] == str(tree)
    assert (tree / "bin/go").read_bytes() == GO["bin/go"] and found["signed_by"].startswith("SHA256:")
    assert not os.access(tree / "bin", os.W_OK) or os.geteuid() == 0
    assert (tree.stat().st_mode & 0o222) == 0 and ((tree / "bin/go").stat().st_mode & 0o7222) == 0
    assert not list(t.store().glob(".download-*"))  # the archive goes once it is unpacked
    assert any("downloading go 1.25.4" in line for line in said) and any("--toolchain go" in line for line in said)
    said.clear()
    t.add("go", source, out=said.append, keys=trusted)
    assert said == ["go 1.25.4 is in the store already"]
    assert t.store_listing(runs={})[0]["commands"] == ["go", "gofmt"]


def test_an_archive_that_is_not_the_signed_one_is_never_unpacked(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    item = entry(tmp_path)
    Path(item["url"]).write_bytes(archive({**GO, "bin/evil": b"x"}))
    with pytest.raises(ValueError, match="SHA-256 .* the signed catalog says .*: not used"):
        t.add("go", catalog(tmp_path, [item], key), out=lambda line: None, keys=trusted)
    assert not t.installed() and [p.name for p in t.store().iterdir() if not p.name.startswith(".lock")] == []


@pytest.mark.parametrize("members, why", [
    ({"../escape": b"x", **GO}, "leaves the tree"),
    ({**GO, "bin/go": b"x", "BIN/GO": b"y"}, None),  # (only refused where the file system ignores case)
])
def test_an_archive_is_unpacked_without_trusting_it(tmp_path, signing_key, data_home, members, why):
    key, trusted = signing_key
    item = entry(tmp_path, members=members)
    if why:
        with pytest.raises(ValueError, match=why):
            t.add("go", catalog(tmp_path, [item], key), out=lambda line: None, keys=trusted)
        assert not t.installed()
    else:
        t.add("go", catalog(tmp_path, [item], key), out=lambda line: None, keys=trusted)


def test_two_names_that_differ_only_in_case_are_refused_where_the_file_system_ignores_case(tmp_path, data_home):
    rootfs = t.module("mcp_rootfs")
    data = archive({"bin": None, "bin/Tool": b"a", "bin/tool": b"b"})
    with pytest.raises(ValueError, match="bin/Tool and bin/tool.*takes for one name"):
        rootfs.unpack(io.BytesIO(data), tmp_path / "tree", case_sensitive=False)
    assert not (tmp_path / "tree").exists()
    rootfs.unpack(io.BytesIO(data), tmp_path / "tree")  # a file, not bytes: never held in memory
    assert (tmp_path / "tree/bin/Tool").read_bytes() == b"a"
    assert t.case_sensitive(tmp_path) in (True, False)


def test_a_manifest_that_names_a_command_the_archive_lacks_is_not_added(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    item = entry(tmp_path, commands={"go": "bin/go", "gopls": "bin/gopls"})
    with pytest.raises(ValueError, match="has no bin/gopls"):
        t.add("go", catalog(tmp_path, [item], key), out=lambda line: None, keys=trusted)
    assert not t.installed() and not (t.store() / item["sha256"]).exists()


def test_add_says_when_the_disk_has_no_room(tmp_path, signing_key, data_home, monkeypatch):
    key, trusted = signing_key
    item = entry(tmp_path, unpacked=10 << 30)
    monkeypatch.setattr(t.shutil, "disk_usage", lambda path: types.SimpleNamespace(free=1 << 30))
    with pytest.raises(ValueError, match="needs .* MiB in .*; 1024 MiB are free"):
        t.add("go", catalog(tmp_path, [item], key), out=lambda line: None, keys=trusted)


def test_a_download_is_streamed_and_must_stay_https(tmp_path):
    data = archive(GO)

    class Response(io.BytesIO):
        url = "https://example.org/go.tar.xz"

        def geturl(self):
            return self.url
    size = t.download("https://example.org/go.tar.xz", hashlib.sha256(data).hexdigest(), tmp_path / "a",
                      opener=lambda url: Response(data))
    assert size == len(data) and (tmp_path / "a").read_bytes() == data
    Response.url = "http://example.org/go.tar.xz"
    with pytest.raises(ValueError, match="redirected to http://example.org/go.tar.xz, which is not https"):
        t.download("https://example.org/go.tar.xz", "0" * 64, tmp_path / "b", opener=lambda url: Response(data))
    Response.url = "https://example.org/go.tar.xz"
    with pytest.raises(ValueError, match="larger than"):
        t.download("https://example.org/go.tar.xz", "0" * 64, tmp_path / "c", opener=lambda url: Response(data),
                   limit=10)
    assert not (tmp_path / "b").exists() and not (tmp_path / "c").exists()
    with pytest.raises(ValueError, match="an https URL or a local file"):
        t.download("ftp://example.org/x", "0" * 64, tmp_path / "d")


def test_add_installs_what_a_toolchain_requires_and_remove_takes_it_out(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    rust = entry(tmp_path, name="rust", version="1.91.0", members={"bin": None, "bin/cargo": b"c"},
                 commands={"cargo": "bin/cargo"}, requires=["cc"], hosts=["crates.io"])
    cc = entry(tmp_path, name="cc", version="14.2", members={"bin": None, "bin/cc": b"c"}, commands={"cc": "bin/cc"},
               hosts=[])
    said = []
    t.add("rust", catalog(tmp_path, [rust, cc], key), out=said.append, keys=trusted)
    assert sorted(t.installed()) == ["cc", "rust"]
    assert [e["name"] for e in t.resolved(["rust"])] == ["cc", "rust"]  # the required one first
    t.remove("rust", out=said.append, runs={})
    assert sorted(t.installed()) == ["cc"] and not (t.store() / rust["sha256"]).exists()
    with pytest.raises(ValueError, match="rust is not in the store: sandbox-ai toolchain add rust"):
        t.resolved(["rust"])
    with pytest.raises(ValueError, match="not in the store"):
        t.remove("rust", out=said.append, runs={})


def test_remove_is_refused_while_a_running_sandbox_has_it_mounted(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    t.add("go", catalog(tmp_path, [entry(tmp_path)], key), out=lambda line: None, keys=trusted)
    run = tmp_path / "run"
    run.mkdir()
    record = {"run": "claude-1", "name": "work", "directory": str(run)}
    t.record_start(run, {"entries": [t.chosen(t.installed(), "go")]})
    with pytest.raises(ValueError, match="go 1.25.4 is mounted in work: detach it there"):
        t.remove("go", out=lambda line: None, runs={"claude-1": record})
    assert t.store_listing(runs={"claude-1": record})[0]["used_by"] == ["work"]


# --- what it needs from the system ------------------------------------------------------------------------------


def test_a_host_below_the_floor_is_refused_with_the_reason(tmp_path):
    item = t.validated(entry(tmp_path, needs={"glibc": "2.28", "libraries": ["libstdc++.so.6"]}))
    old, new = (lambda: (2, 26)), (lambda: (2, 42))
    problem, _ = t.needs_problem(item, "bwrap", glibc=old, has_library=lambda name: True)
    assert "needs glibc 2.28 or newer, this host has 2.26" in problem and "use Podman" in problem
    problem, _ = t.needs_problem(item, "bwrap", glibc=new, has_library=lambda name: False)
    assert "needs libstdc++.so.6, which this host does not have" in problem
    problem, _ = t.needs_problem(item, "bwrap", glibc=lambda: None, has_library=lambda name: True)
    assert "this host's C library is not glibc" in problem
    assert t.needs_problem(item, "bwrap", glibc=new, has_library=lambda name: True) == (None, None)
    # Podman: sandbox-ai's own images are known; an image of your own gets a note, not a check
    assert t.needs_problem(item, "podman", glibc=old, has_library=lambda name: False) == (None, None)
    problem, note = t.needs_problem(item, "podman", own_image=False)
    assert problem is None and "glibc 2.28 or newer, libstdc++.so.6 in the image" in note
    newer = t.validated(entry(tmp_path, needs={"glibc": "2.99", "libraries": ["libicuuc.so.76"]}))
    assert "sandbox-ai's images have 2.36" in t.needs_problem(newer, "podman")[0]
    newer["needs"]["glibc"] = None
    assert "libicuuc.so.76, which sandbox-ai's images do not have" in t.needs_problem(newer, "podman")[0]
    static = t.validated(entry(tmp_path, needs={"glibc": None, "libraries": []}))
    assert t.needs_problem(static, "bwrap", glibc=lambda: None) == (None, None)  # nothing but the kernel
    other = {**item, "arch": "aarch64" if t.machine() == "x86_64" else "x86_64"}
    assert "this machine is" in t.needs_problem(other, "podman")[0]


def test_the_hosts_libraries_are_looked_for_where_its_loader_looks(tmp_path):
    (tmp_path / "usr/lib/x86_64-linux-gnu").mkdir(parents=True)
    (tmp_path / "usr/lib/x86_64-linux-gnu/libstdc++.so.6").write_text("")
    (tmp_path / "opt/own").mkdir(parents=True)
    (tmp_path / "opt/own/libown.so.1").write_text("")
    (tmp_path / "etc/ld.so.conf.d").mkdir(parents=True)
    (tmp_path / "etc/ld.so.conf.d/own.conf").write_text("# mine\n/opt/own\n")
    assert t.host_has_library("libstdc++.so.6", tmp_path) and t.host_has_library("libown.so.1", tmp_path)
    assert not t.host_has_library("libssl.so.3", tmp_path)
    assert t.host_glibc() is None or t.host_glibc() >= (2, 17)


# --- the wrappers and the variables -----------------------------------------------------------------------------


def manifest(tmp_path, name, **more):
    return t.validated(entry(tmp_path, name=name, **more))


def test_the_session_folder_gets_wrappers_variables_and_mount_points(tmp_path):
    folder = tmp_path / "session" / "toolchains"
    folder.parent.mkdir()
    jdk = manifest(tmp_path, "jdk", commands={"java": "bin/java", "mvn": "maven/bin/mvn"},
                   env={"JAVA_HOME": "{prefix}", "MAVEN_OPTS": "-Xmx1g -Dx='y z'"},
                   caches={"GRADLE_USER_HOME": ".gradle"})
    go = manifest(tmp_path, "go")
    assert t.write_session(folder, [jdk, go]) == []
    assert sorted(p.name for p in (folder / "bin").iterdir()) == ["go", "gofmt", "java", "mvn"]
    assert (folder / "jdk").is_dir() and (folder / "go").is_dir() and not any((folder / "jdk").iterdir())
    text = (folder / "bin/mvn").read_text()
    assert text.splitlines()[0] == "#!/bin/sh" and os.access(folder / "bin/mvn", os.X_OK)
    assert '[ -n "${JAVA_HOME+x}" ] || export JAVA_HOME=/opt/toolchains/jdk' in text
    assert 'export GRADLE_USER_HOME="${HOME}"/.gradle' in text
    assert text.splitlines()[-1] == 'exec /opt/toolchains/jdk/maven/bin/mvn "$@"'
    env = (folder / "env.sh").read_text()
    assert 'PATH="/opt/toolchains/bin:${PATH}"' in env and "export JAVA_HOME=/opt/toolchains/jdk" in env
    assert (folder / "profile.sh").read_text() == t.PROFILE_TEXT
    # detached: its wrappers go, the others stay; the mount point stays (the unmount comes after)
    before = (folder / "bin/go").stat().st_ino
    t.write_session(folder, [go])
    assert sorted(p.name for p in (folder / "bin").iterdir()) == ["go", "gofmt"]
    assert (folder / "bin/go").stat().st_ino == before  # not written again
    assert "JAVA_HOME" not in (folder / "env.sh").read_text() and (folder / "jdk").is_dir()


def test_the_wrappers_run_and_keep_what_you_set_yourself(tmp_path):
    """A wrapper is sh: run for real, with the toolchain's place replaced by a folder here."""
    folder = tmp_path / "toolchains"
    tool = manifest(tmp_path, "tool", commands={"show": "bin/show"},
                    env={"TOOL_HOME": "{prefix}", "TOOL_OPT": "a 'b' $c"},
                    caches={"TOOL_CACHE": ".cache/tool"})
    t.write_session(folder, [tool])
    real = tmp_path / "real/tool/bin"
    real.mkdir(parents=True)
    (real / "show").write_text('#!/bin/sh\necho "$TOOL_HOME|$TOOL_OPT|$TOOL_CACHE|$0|$*"\n')
    (real / "show").chmod(0o755)
    text = (folder / "bin/show").read_text().replace("/opt/toolchains", str(tmp_path / "real"))
    (tmp_path / "show").write_text(text)
    (tmp_path / "show").chmod(0o755)
    env = {"PATH": os.environ["PATH"], "HOME": "/home/me"}
    out = subprocess.run([str(tmp_path / "show"), "x y", "z"], env=env, capture_output=True, text=True).stdout.strip()
    assert out == f"{tmp_path}/real/tool|a 'b' $c|/home/me/.cache/tool|{real}/show|x y z"
    out = subprocess.run([str(tmp_path / "show")], env={**env, "TOOL_HOME": "/mine", "TOOL_CACHE": ""},
                         capture_output=True, text=True).stdout.strip()
    assert out.startswith("/mine|a 'b' $c||")  # set by you, even to nothing: kept
    # env.sh puts the folder on PATH once and sets the same variables
    shell = f'. {folder}/env.sh; . {folder}/env.sh; echo "$PATH|$TOOL_HOME|$TOOL_CACHE"'
    out = subprocess.run(["sh", "-c", shell], env={"PATH": "/usr/bin:/bin", "HOME": "/home/me"}, capture_output=True,
                         text=True).stdout.strip()
    assert out == "/opt/toolchains/bin:/usr/bin:/bin|/opt/toolchains/tool|/home/me/.cache/tool"
    out = subprocess.run(["sh", "-c", f". {folder}/profile.sh; echo $PATH"], env={"PATH": "/usr/bin"},
                         capture_output=True, text=True).stdout.strip()
    assert out == "/opt/toolchains/bin:/usr/bin"


def test_the_first_toolchain_keeps_a_command_two_name(tmp_path):
    folder = tmp_path / "toolchains"
    one = manifest(tmp_path, "jdk-21", commands={"java": "bin/java"})
    two = manifest(tmp_path, "jdk-25", commands={"java": "bin/java", "jwebserver": "bin/jwebserver"})
    notes = t.write_session(folder, [one, two])
    assert notes == ["java stays jdk-21's; jdk-25's is /opt/toolchains/jdk-25/bin/java"]
    assert "exec /opt/toolchains/jdk-21/bin/java" in (folder / "bin/java").read_text()
    assert t.environment([{**one, "env": {"JAVA_HOME": "{prefix}"}}, {**two, "env": {"JAVA_HOME": "{prefix}"},
                                                                      "caches": {"X_CACHE": ".x"}}]) == \
        {"JAVA_HOME": "/opt/toolchains/jdk-21", "X_CACHE": "/root/.x"}


# --- a start ------------------------------------------------------------------------------------------------------


def stored(tmp_path, signing_key, *entries):
    key, trusted = signing_key
    source = catalog(tmp_path, list(entries), key)
    for item in entries:
        t.add(f"{item['name']}@{item['version']}", source, out=lambda line: None, keys=trusted)
    return t.installed()


def test_a_starts_plan_mounts_opens_and_says_so(tmp_path, signing_key, data_home):
    stored(tmp_path, signing_key, entry(tmp_path), entry(tmp_path, version="1.24.9"),
           entry(tmp_path, name="python", version="3.14.0", allow=["pypi", "nonsense"], hosts=[],
                 commands={"go": "bin/go"}))
    plan = t.start_plan(["go", "python"], "podman", presets=("pypi", "github", "npm"))
    assert [(e["name"], e["version"]) for e in plan["entries"]] == [("go", "1.25.4"), ("python", "3.14.0")]
    assert plan["hosts"] == ["proxy.golang.org", "sum.golang.org"] and plan["presets"] == ["pypi"]
    assert any("are opened: proxy.golang.org, sum.golang.org, --allow-pypi" in note for note in plan["notes"])
    assert any("the preset nonsense, which this sandbox-ai does not have" in note for note in plan["notes"])
    mounts = t.start_mounts(plan["entries"])
    assert mounts[0] == (t.store() / plan["entries"][0]["sha256"], "/opt/toolchains/go")
    shown = t.plan_shown(plan)
    assert shown[0]["sandbox"] == "/opt/toolchains/go" and shown[0]["mode"] == "ro" and shown[0]["signed_by"]
    assert t.start_plan(["go@1.24.9"], "podman")["entries"][0]["version"] == "1.24.9"
    with pytest.raises(ValueError, match="one version of a toolchain per sandbox"):
        t.start_plan(["go", "go@1.24.9"], "podman")
    assert len(t.start_plan(["go", "go"], "podman")["entries"]) == 1
    with pytest.raises(ValueError, match="go 9.9 is not in the store \\(installed: 1.24.9, 1.25.4\\)"):
        t.start_plan(["go@9.9"], "podman")
    with pytest.raises(ValueError, match="--toolchain go: go 1.25.4 needs glibc 2.17 or newer, this host has 2.5"):
        t.start_plan(["go"], "bwrap", glibc=lambda: (2, 5))


@pytest.mark.parametrize("strict, offline, why", [(True, False, "security mode high"), (False, True, "offline")])
def test_the_strict_modes_and_offline_mount_without_opening(tmp_path, signing_key, data_home, strict, offline, why):
    stored(tmp_path, signing_key, entry(tmp_path, allow=["github"]))
    plan = t.start_plan(["go"], "podman", strict=strict, offline=offline, mode="high", presets=("github",))
    assert plan["hosts"] == [] and plan["presets"] == [] and len(plan["entries"]) == 1
    assert plan["closed"] == ["proxy.golang.org", "sum.golang.org", "--allow-github"]
    assert any(f"{why}: the registries of go stay closed" in note for note in plan["notes"])


def test_a_start_says_when_a_toolchain_was_added_unsigned(tmp_path, signing_key, data_home):
    t.add("go", catalog(tmp_path, [entry(tmp_path)]), unsigned=True, out=lambda line: None, keys=signing_key[1])
    plan = t.start_plan(["go"], "podman")
    assert any("go 1.25.4 was added unsigned" in note and note.startswith("warning:") for note in plan["notes"])
    assert t.plan_shown(plan)[0]["signed_by"] is None


# --- attach and detach (the namespace helper faked) -------------------------------------------------------------


class Launcher:
    PRESETS = {"github": ["github.com"], "pypi": ["pypi.org"]}
    POLICY_FILE = "/etc/sandbox-ai/policy.json"

    def __init__(self, forbid=(), machine=False):
        self.forbid, self.machine = list(forbid), machine

    def load_policy(self):
        return {"forbid": self.forbid}

    def machine_record(self, record):
        return self.machine


@pytest.fixture
def running(tmp_path, signing_key, data_home):
    """A running sandbox as the launcher leaves it: its run directory, its session folder with /opt/toolchains."""
    stored(tmp_path, signing_key, entry(tmp_path, allow=["github"]),
           entry(tmp_path, name="rust", version="1.91.0", members={"bin": None, "bin/cargo": b"c"},
                 commands={"cargo": "bin/cargo"}, requires=["cc"], hosts=["crates.io"]),
           entry(tmp_path, name="cc", version="14.2", members={"bin": None, "bin/cc": b"c"},
                 commands={"cc": "bin/cc"}, hosts=[]))
    run, session, logs = tmp_path / "run", tmp_path / "session", tmp_path / "logs"
    for directory in (run, session, logs):
        directory.mkdir()
    t.write_session(t.session_folder(session), [])
    record = {"run": "claude-1", "backend": "podman", "directory": str(run), "session_dir": str(session),
              "log_dir": str(logs)}
    calls, opened, said = [], [], []

    def runner(command, **options):
        request = json.loads(command[-1])
        calls.append(request)
        assert command[1:4] == ["-I", str(Path(t.__file__)), "--internal-helper"]
        return types.SimpleNamespace(returncode=runner.status, stderr=runner.error, stdout="")
    runner.status, runner.error = 0, ""

    def net_main(argv, out=print, brief=False, prog="net"):
        opened.append(argv)
        return net_main.status
    net_main.status = 0
    found = {"user": os.getpid(), "mntns": {"mnt:[1]": os.getpid()}, "pids": [os.getpid()]}
    options = dict(out=said.append, runner=runner, finder=lambda record: found, launcher=Launcher(),
                   net_main=net_main, keeper=lambda record, value, add=True: False)
    return types.SimpleNamespace(record=record, calls=calls, opened=opened, said=said, runner=runner,
                                 net_main=net_main, options=options, folder=t.session_folder(session), logs=logs)


def test_attach_mounts_the_stores_tree_read_only_and_opens_its_registries(running):
    r = running
    assert t.attach(r.record, "go", **r.options) == ["go"]
    request = r.calls[0]
    tree = t.tree(t.installed()["go"]["1.25.4"]["sha256"])
    assert request["op"] == "add" and request["name"] == "go" and request["source"] == str(tree)
    assert request["identity"] == t.identity(tree) and request["base"] == t.identity(r.folder)
    assert request["placeholder"] == t.identity(r.folder / "go") and request["targets"] == [os.getpid()]
    assert request["starts"][str(os.getpid())] is not None  # each PID with its start time
    assert sorted(p.name for p in (r.folder / "bin").iterdir()) == ["go", "gofmt"]
    assert r.opened == [["allow", "claude-1", "--allow-host", "proxy.golang.org", "--allow-host", "sum.golang.org",
                         "--allow-github"]]
    rows = t.run_listing(r.record)
    assert rows[0]["name"] == "go" and rows[0]["source"] == "added" and rows[0]["sandbox"] == "/opt/toolchains/go"
    assert rows[0]["opened"] == ["proxy.golang.org", "sum.golang.org", "--allow-github"]
    assert any("attached go 1.25.4 to claude-1 at /opt/toolchains/go, read-only: go, gofmt" in line for line in r.said)
    events = [json.loads(line) for line in (r.logs / "toolchains.jsonl").read_text().splitlines()]
    assert [e["event"] for e in events] == ["attach", "open"] and events[0]["sha256"] == request["source"][-64:]
    with pytest.raises(ValueError, match="go 1.25.4 is attached to claude-1 already"):
        t.attach(r.record, "go", **r.options)


def test_attach_brings_what_a_toolchain_requires(running):
    r = running
    assert t.attach(r.record, "rust", **r.options) == ["cc", "rust"]
    assert [call["name"] for call in r.calls] == ["cc", "rust"]
    assert sorted(p.name for p in (r.folder / "bin").iterdir()) == ["cargo", "cc"]
    with pytest.raises(ValueError, match="rust requires cc: detach it first"):
        t.detach(r.record, "cc", **{k: v for k, v in r.options.items() if k in ("out", "runner", "finder", "keeper")})


def test_attach_offline_and_a_refused_network_leave_the_registries_closed(running):
    r = running
    t.attach(r.record, "go", offline=True, **r.options)
    assert r.opened == [] and any("--offline: its registries stay closed" in line for line in r.said)
    assert t.run_listing(r.record)[0]["opened"] == []
    r.net_main.status = 1  # the run's security mode refuses (net allow says why)
    t.attach(r.record, "rust", **r.options)
    assert r.opened and any("its registries stay closed (crates.io)" in line and "mounted and works without" in line
                            for line in r.said)
    assert [row["name"] for row in t.run_listing(r.record)] == ["go", "cc", "rust"]


def test_attach_is_refused_before_anything_is_mounted(running, tmp_path):
    r = running
    with pytest.raises(ValueError, match="--toolchain is not allowed by the administrator's policy"):
        t.attach(r.record, "go", **{**r.options, "launcher": Launcher(forbid=["--toolchain"])})
    with pytest.raises(ValueError, match="podman machine's VM.*resume NAME --restart --toolchain go"):
        t.attach(r.record, "go", **{**r.options, "launcher": Launcher(machine=True)})
    with pytest.raises(ValueError, match="needs glibc 2.17 or newer, this host has 2.5"):
        t.attach({**r.record, "backend": "bwrap"}, "go", glibc=lambda: (2, 5), **r.options)
    with pytest.raises(ValueError, match="zig is not in the store: sandbox-ai toolchain add zig"):
        t.attach(r.record, "zig", **r.options)
    old = {**r.record, "session_dir": str(tmp_path / "old-session")}
    (tmp_path / "old-session").mkdir()
    with pytest.raises(ValueError, match="started by a sandbox-ai without toolchains.*restart it with --toolchain go"):
        t.attach(old, "go", **r.options)
    with pytest.raises(ValueError, match="processes were not found"):
        t.attach(r.record, "go", **{**r.options, "finder": lambda record: None})
    assert r.calls == [] and r.opened == [] and t.run_listing(r.record) == []
    assert not (r.logs / "toolchains.jsonl").exists() or "attach" not in (r.logs / "toolchains.jsonl").read_text()


def test_a_helper_that_fails_attaches_nothing(running):
    r = running
    r.runner.status, r.runner.error = 1, "mount /opt/toolchains/go: Operation not permitted"
    with pytest.raises(ValueError, match="could not attach go: mount /opt/toolchains/go: Operation not permitted"):
        t.attach(r.record, "go", **r.options)
    assert t.run_listing(r.record) == [] and not (r.folder / "bin/go").exists() and r.opened == []
    r.runner.status = t.UNSURE
    with pytest.raises(ValueError, match="may still be mounted.*toolchain detach claude-1 go --force"):
        t.attach(r.record, "go", **r.options)


def test_detach_takes_the_wrappers_first_and_puts_them_back_when_the_unmount_is_refused(running):
    r = running
    options = {k: v for k, v in r.options.items() if k in ("out", "runner", "finder", "keeper")}
    t.attach(r.record, "go", **r.options)
    seen = []

    def busy(command, **more):
        seen.append(sorted(p.name for p in (r.folder / "bin").iterdir()))  # what the sandbox sees during the unmount
        return types.SimpleNamespace(returncode=t.BUSY, stdout="", stderr="/opt/toolchains/go is in use in the "
                                     "sandbox (a process runs from it or has a file of it open): --force detaches it "
                                     "anyway")
    with pytest.raises(ValueError, match="could not detach go: .*is in use.*--force"):
        t.detach(r.record, "go", **{**options, "runner": busy})
    assert seen == [[]] and sorted(p.name for p in (r.folder / "bin").iterdir()) == ["go", "gofmt"]
    assert [row["name"] for row in t.run_listing(r.record)] == ["go"]
    t.detach(r.record, "go", force=True, **options)
    request = r.calls[-1]
    assert request["op"] == "remove" and request["force"] is True and request["base"] == t.identity(r.folder)
    assert t.run_listing(r.record) == [] and list((r.folder / "bin").iterdir()) == []
    assert not (r.folder / "go").exists()
    assert any("detached go from claude-1; the hosts opened with it stay open" in line for line in r.said)
    with pytest.raises(ValueError, match="go is not attached to claude-1"):
        t.detach(r.record, "go", **options)
    t.attach(r.record, "go", offline=True, **r.options)  # and again


def test_a_toolchain_of_the_start_stays_for_the_run(running):
    r = running
    t.record_start(r.record["directory"], {"entries": [t.chosen(t.installed(), "go")]})
    assert t.run_listing(r.record)[0]["source"] == "start"
    options = {k: v for k, v in r.options.items() if k in ("out", "runner", "finder", "keeper")}
    with pytest.raises(ValueError, match="go was given at the start of claude-1 and stays for the run"):
        t.detach(r.record, "go", **options)
    with pytest.raises(ValueError, match="attached to claude-1 already"):
        t.attach(r.record, "go", **r.options)
    assert r.calls == []


class FakeLibc:
    """setns, unshare, the mount calls: recorded, each succeeding unless told otherwise."""

    def __init__(self, fail=None):
        self.calls, self.fail, self.next_fd = [], fail or {}, 700

    def setns(self, fd, kind):
        self.calls.append(("setns", kind))
        return 0

    def unshare(self, kind):
        self.calls.append(("unshare", kind))
        return 0

    def syscall(self, number, *args):
        self.calls.append(("syscall", number))
        if number in self.fail:
            import ctypes
            ctypes.set_errno(self.fail[number])
            return -1
        if number == 428:  # open_tree
            self.next_fd += 1
            return os.open("/dev/null", os.O_RDONLY)
        return 0

    def umount2(self, path, flags):
        self.calls.append(("umount2", path, flags))
        return 0


def fake_proc(tmp_path, pids):
    proc = tmp_path / "proc"
    for pid in pids:
        (proc / str(pid) / "ns").mkdir(parents=True)
        for kind in ("user", "mnt"):
            (proc / str(pid) / "ns" / kind).write_text("")
        (proc / str(pid) / "stat").write_text(f"{pid} (x) S " + " ".join(["0"] * 18) + f" {pid}00 0\n")
    return proc


def test_the_helper_enters_the_user_namespace_then_each_mount_namespace(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, [100, 101])
    source = tmp_path / "tree"
    source.mkdir()
    folder = tmp_path / "toolchains"
    (folder / "go").mkdir(parents=True)
    monkeypatch.setattr(t, "MOUNT", str(folder))
    c = FakeLibc()
    request = {"op": "add", "user": 100, "name": "go", "targets": [100, 101], "source": str(source),
               "identity": t.identity(source), "base": t.identity(folder), "placeholder": t.identity(folder / "go"),
               "starts": {"100": 10000, "101": 10100}}
    assert t.helper(request, proc=proc, c=c) == 0
    kinds = [call[1] for call in c.calls if call[0] in ("setns", "unshare")]
    assert kinds == [t.CLONE_NEWUSER, t.CLONE_NEWNS, t.CLONE_NEWNS, t.CLONE_NEWNS]  # user, own, then each of the two
    numbers = [call[1] for call in c.calls if call[0] == "syscall"]
    assert numbers == [428, 442, 428, 442, 429, 429]  # a clone per namespace, each made read-only, then moved
    # a PID that names another process now is refused before anything is entered
    with pytest.raises(OSError, match="process 101 of the sandbox is gone"):
        t.helper({**request, "starts": {"100": 10000, "101": 5}}, proc=proc, c=FakeLibc())
    # /opt/toolchains must be the session's own folder, the mount point its empty placeholder
    with pytest.raises(OSError, match="is not the folder of its session"):
        t.helper({**request, "base": [1, 2]}, proc=proc, c=FakeLibc())
    with pytest.raises(OSError, match="is mounted in the sandbox already"):
        t.helper({**request, "placeholder": [1, 2]}, proc=proc, c=FakeLibc())
    (folder / "go").rmdir()
    (folder / "go").symlink_to(tmp_path)
    with pytest.raises(OSError):  # never through a link
        t.helper(request, proc=proc, c=FakeLibc())
    with pytest.raises(OSError, match="not a toolchain request"):
        t.helper({**request, "name": "../x"}, proc=proc, c=FakeLibc())
    with pytest.raises(OSError, match="not a toolchain request"):
        t.helper({**request, "name": "bin"}, proc=proc, c=FakeLibc())


def test_without_mount_setattr_nothing_is_attached(tmp_path, monkeypatch):
    """A clone that cannot be made read-only, nosuid and nodev (Linux before 5.12) is never mounted."""
    proc = fake_proc(tmp_path, [100])
    source, folder = tmp_path / "tree", tmp_path / "toolchains"
    source.mkdir()
    (folder / "go").mkdir(parents=True)
    monkeypatch.setattr(t, "MOUNT", str(folder))
    c = FakeLibc(fail={442: errno.ENOSYS})
    with pytest.raises(OSError, match="mount_setattr"):
        t.helper({"op": "add", "user": 100, "name": "go", "targets": [100], "source": str(source),
                  "identity": t.identity(source), "base": t.identity(folder),
                  "placeholder": t.identity(folder / "go"), "starts": {"100": 10000}}, proc=proc, c=c)
    assert 429 not in [call[1] for call in c.calls if call[0] == "syscall"]


def test_the_helper_unmounts_strictly_and_only_what_is_mounted(tmp_path, monkeypatch):
    proc = fake_proc(tmp_path, [100])
    folder = tmp_path / "toolchains"
    (folder / "go").mkdir(parents=True)
    monkeypatch.setattr(t, "MOUNT", str(folder))
    request = {"op": "remove", "user": 100, "name": "go", "targets": [100], "base": t.identity(folder),
               "placeholder": t.identity(folder / "go"), "starts": {"100": 10000}}
    c = FakeLibc()
    assert t.helper(request, proc=proc, c=c) == 0
    assert not [call for call in c.calls if call[0] == "umount2"]  # nothing is mounted there: nothing to unmount

    class Busy(FakeLibc):
        def umount2(self, path, flags):
            import ctypes
            self.calls.append(("umount2", path, flags))
            if flags & t.MNT_DETACH:
                self.detached = True
                return 0
            ctypes.set_errno(errno.EBUSY)
            return -1
    covered = iter([True, True, False])
    monkeypatch.setattr(t, "covered", lambda target, placeholder: next(covered))
    with pytest.raises(BlockingIOError, match="is in use in the sandbox.*--force detaches it anyway"):
        t.helper(request, proc=proc, c=Busy())
    c = Busy()
    assert t.helper({**request, "force": True}, proc=proc, c=c) == 0
    flags = [call[2] for call in c.calls if call[0] == "umount2"]
    assert flags == [t.UMOUNT_NOFOLLOW, t.MNT_DETACH | t.UMOUNT_NOFOLLOW]  # strictly first, then detached


def test_run_helper_names_only_the_sandboxes_own_namespaces(tmp_path):
    proc = fake_proc(tmp_path, [100, 101, 102])
    for pid, user in ((100, "user:[1]"), (101, "user:[1]"), (102, "user:[9]")):  # 102: a namespace the agent made
        (proc / str(pid) / "ns" / "user").unlink()
        (proc / str(pid) / "ns" / "user").symlink_to(user)
    seen = []

    def runner(command, **options):
        seen.append(json.loads(command[-1]))
        return types.SimpleNamespace(returncode=0, stderr="", stdout="")
    found = {"user": 100, "mntns": {"a": 100, "b": 101, "c": 102}}
    assert t.run_helper("add", found, "go", "/store/x", [1, 2], [3, 4], [5, 6], proc=proc, runner=runner) == (0, "")
    assert seen[0]["targets"] == [100, 101] and seen[0]["starts"] == {"100": 10000, "101": 10100}
    assert t.run_helper("add", {"user": 100, "mntns": {}}, "go", proc=proc, runner=runner)[0] == 1


# --- the command line -------------------------------------------------------------------------------------------


def run_cli(*argv, env=None):
    return subprocess.run([str(ROOT / "sandbox-ai"), "toolchain", *argv], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=120)


def test_the_command_line_end_to_end(tmp_path, signing_key, data_home):
    """./sandbox-ai toolchain catalog, add, list, show, remove: against a catalog of this test, unsigned (the
    signers file of the installation is not this test's to change) and refused until --unsigned says so."""
    source = catalog(tmp_path, [entry(tmp_path, sources=[{"url": "https://go.dev/dl/go1.25.4.linux-amd64.tar.gz",
                                                          "sha256": "a" * 64}])])
    env = {"XDG_DATA_HOME": str(data_home), "XDG_STATE_HOME": str(tmp_path / "state"),
           "XDG_RUNTIME_DIR": str(tmp_path / "runtime")}
    (tmp_path / "runtime").mkdir(mode=0o700)
    shown = run_cli("catalog", "--catalog", source, env=env)
    assert shown.returncode == 0 and "NOT signed by a trusted key" in shown.stdout and "go" in shown.stdout
    listed = json.loads(run_cli("catalog", "--json", "--catalog", source, env=env).stdout)
    assert listed["toolchains"][0]["name"] == "go" and listed["toolchains"][0]["installed"] is False
    assert listed["signature"]["state"] == "unsigned" and listed["arch"] == t.machine()
    refused = run_cli("add", "go", "--catalog", source, env=env)
    assert refused.returncode == 1 and "not signed by a trusted key" in refused.stderr
    assert "--unsigned" in refused.stderr
    added = run_cli("add", "go", "--catalog", source, "--unsigned", env=env)
    assert added.returncode == 0, added.stderr
    assert "added go 1.25.4 (go, gofmt)" in added.stdout
    rows = json.loads(run_cli("list", "--json", env=env).stdout)
    assert rows[0]["name"] == "go" and rows[0]["signed_by"] is None and rows[0]["used_by"] == []
    table = run_cli("list", env=env).stdout
    assert "go" in table and "NO" in table
    text = run_cli("show", "go", env=env).stdout
    assert "in the sandbox: /opt/toolchains/go (read-only)" in text and "opens: proxy.golang.org" in text
    assert "needs: glibc 2.17+" in text and "from: https://go.dev/dl/go1.25.4.linux-amd64.tar.gz" in text
    assert "signed by: nobody (added unsigned)" in text
    shown_json = json.loads(run_cli("show", "go", "--json", env=env).stdout)
    assert shown_json["commands"] == {"go": "bin/go", "gofmt": "bin/gofmt"}
    assert json.loads(run_cli("list", "--run", "--json", env=env).stdout) == []  # nothing runs
    nothing = run_cli("attach", "go", env=env)
    assert nothing.returncode == 1 and "no sandbox is running" in nothing.stderr
    assert run_cli("remove", "go", env=env).returncode == 0
    assert json.loads(run_cli("list", "--json", env=env).stdout) == []
    missing = run_cli("remove", "go", env=env)
    assert missing.returncode == 1 and "not in the store" in missing.stderr
    usage = run_cli("--help")
    assert usage.returncode == 0 and "attach" in usage.stdout and "detach" in usage.stdout
