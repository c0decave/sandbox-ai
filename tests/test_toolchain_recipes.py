"""Toolchains built on this machine (sandbox/toolchain_recipes.py, sandbox/toolchains.py): a signed recipe instead of
a signed archive, for tools nobody but their publisher may pass on (the Android SDK). The recipe checked value by
value, its SHA-256 the tree's name, the license said and accepted before anything is fetched, every download held
to its size and hashes before it is unpacked, the zip unpacked without trusting it, the tree read-only in the store
and mounted like every other toolchain's. The downloads are small zips made here (never Google's)."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_toolchains_recipe_tests", ROOT / "sandbox/toolchains.py")
t = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(t)
r = t.module("toolchain_recipes")
FLAG = "--accept-android-sdk-license"


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv(t.LOCAL_CATALOGS_VARIABLE, raising=False)
    yield tmp_path / "data"
    for directory, dirs, _ in os.walk(tmp_path):  # the trees are read-only: writable again for pytest's cleanup
        for name in dirs:
            child = os.path.join(directory, name)
            if not os.path.islink(child):
                os.chmod(child, 0o700)


@pytest.fixture(scope="session")
def signing_key(tmp_path_factory):
    directory = tmp_path_factory.mktemp("recipe-key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(directory / "key")],
                   check=True)
    kind, key = (directory / "key.pub").read_text().split()[:2]
    return directory / "key", [(kind, key)]


def zipped(members):
    """A zip of MEMBERS: {name: bytes (a file; executable below bin/), ("link", target), ("mode", octal, bytes) or
    None (a folder)}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            info = zipfile.ZipInfo(name)
            if value is None:
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(info, b"")
            elif isinstance(value, tuple) and value[0] == "link":
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, value[1])
            elif isinstance(value, tuple):
                info.external_attr = (stat.S_IFREG | value[1]) << 16
                archive.writestr(info, value[2])
            else:
                info.external_attr = (stat.S_IFREG | (0o755 if "bin/" in name else 0o644)) << 16
                archive.writestr(info, value)
    return buffer.getvalue()


TOOLS = {"platform-tools/": None, "platform-tools/bin/adb": b"#!/bin/sh\necho adb\n",
         "platform-tools/source.properties": b"Pkg.Revision=37.0.1\n"}
BUILD_TOOLS = {"android-16/": None, "android-16/bin/aapt2": b"#!/bin/sh\necho aapt2\n",
               "android-16/lib64/": None, "android-16/lib64/libc++.so": b"\x7fELF",
               "android-16/aapt2-link": ("link", "bin/aapt2")}


def fetch_step(tmp_path, name, members, **more):
    data = zipped(members)
    path = tmp_path / name
    path.write_bytes(data)
    return {"fetch": name, "url": str(path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "sha1": hashlib.sha1(data).hexdigest(), **more}


def recipe(tmp_path, steps=None, terms="android-sdk-license", **more):
    steps = steps or [
        fetch_step(tmp_path, "platform-tools.zip", TOOLS), {"unpack": "platform-tools.zip", "to": "platform-tools",
                                                            "strip": 1},
        fetch_step(tmp_path, "build-tools.zip", BUILD_TOOLS), {"unpack": "build-tools.zip",
                                                               "to": "build-tools/{version}", "strip": 1},
        {"write": "licenses/android-sdk-license", "text": "\n{accepted}"},
        {"write": "platform-tools/package.xml", "text": "<license>{license}</license> {prefix}"},
        {"link": "adb", "to": "platform-tools/bin/adb"},
        {"chmod": "platform-tools/source.properties", "mode": "755"}]
    return {**({"terms": terms} if terms else {}), "vars": {"license": "the text", "accepted": "24333f8a"},
            "steps": steps, **more}


def entry(tmp_path, name="android-sdk", version="37.0.0", made=None, **more):
    """A catalog entry of a toolchain built here, its downloads written to TMP_PATH."""
    made = made or recipe(tmp_path)
    found = {"name": name, "version": version, "arch": t.machine(), "size": 2000, "unpacked": 4000,
             "title": "Android SDK", "description": "for tests", "license": "Android SDK License Agreement",
             "homepage": "https://developer.android.com", "commands": {"adb": "platform-tools/bin/adb",
                                                                       "aapt2": f"build-tools/{version}/bin/aapt2"},
             "env": {"ANDROID_HOME": "{prefix}", "ANDROID_SDK_ROOT": "{prefix}"}, "caches": {},
             "hosts": ["dl.google.com", "maven.google.com"], "allow": [], "needs": {"glibc": "2.17", "libraries": []},
             "requires": [], "sources": [], "recipe": made, **more}
    found.setdefault("sha256", r.digest(found["name"], found["version"], found["arch"], r.validated(made)))
    return found


def catalog(tmp_path, recipes, key=None, toolchains=(), name="toolchains.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"schema": t.SCHEMA, "version": 1, "toolchains": list(toolchains),
                                "recipes": recipes}))
    Path(str(path) + ".sig").unlink(missing_ok=True)
    if key is not None:
        k = t.module("release_keys")
        Path(str(path) + ".sig").write_bytes(k.sign(path.read_bytes(), key, k.MCP))
    return str(path)


def left_in_store():
    """What the store holds besides its lock: trees, half-made builds, downloads (none after a refusal)."""
    return sorted(path.name for path in t.store().iterdir() if path.name != ".lock") if t.store().is_dir() else []


def never(url):
    raise AssertionError(f"nothing is fetched before the license is accepted: {url}")


# --- the recipe ---------------------------------------------------------------------------------------------------


def test_a_recipe_is_checked_value_by_value_and_names_its_tree(tmp_path):
    made = entry(tmp_path)
    checked = t.validated(made)
    assert checked["recipe"]["terms"] == "android-sdk-license" and checked["url"] == ""
    assert [next(iter(step)) for step in checked["recipe"]["steps"]] == [
        "fetch", "unpack", "fetch", "unpack", "write", "write", "link", "chmod"]
    assert checked["sha256"] == r.digest("android-sdk", "37.0.0", t.machine(), checked["recipe"])
    other = recipe(tmp_path)
    other["steps"][4]["text"] = "\nanother"
    assert r.digest("android-sdk", "37.0.0", t.machine(), r.validated(other)) != checked["sha256"]
    with pytest.raises(ValueError, match="sha256 is not its recipe's"):  # a recipe changed under its name
        t.validated({**made, "recipe": other})
    with pytest.raises(ValueError, match="a recipe, not an archive"):
        t.validated({**made, "url": "https://example.org/sdk.tar.xz"})


def changed(tmp_path, index, **change):
    made = recipe(tmp_path)
    made["steps"][index] = {**made["steps"][index], **change}
    return made


@pytest.mark.parametrize("make, why", [
    (lambda p: {**recipe(p), "terms": "some-eula"}, "does not know"),
    (lambda p: {**recipe(p), "run": "curl | sh"}, "a recipe is"),
    (lambda p: {**recipe(p), "steps": []}, "steps must be"),
    (lambda p: {**recipe(p), "vars": {"prefix": "/"}}, "vars must map"),
    (lambda p: {**recipe(p), "vars": {"A": "x"}}, "vars must map"),
    (lambda p: changed(p, 0, url="http://example.org/x.zip"), "url must be"),
    (lambda p: changed(p, 0, url="ftp://example.org/x.zip"), "url must be"),
    (lambda p: changed(p, 0, size=0), "size must be"),
    (lambda p: changed(p, 0, sha256="abc"), "needs its sha256"),
    (lambda p: {**recipe(p), "steps": [{"fetch": "a.zip", "url": "https://example.org/a.zip", "size": 3},
                                       {"unpack": "a.zip"}]}, "needs its sha256"),
    (lambda p: changed(p, 0, fetch="../a.zip"), "fetch names a file"),
    (lambda p: changed(p, 0, shell="rm -rf /"), "fetch is"),
    (lambda p: changed(p, 1, unpack="other.zip"), "unpacked once, after its fetch"),
    (lambda p: changed(p, 3, unpack="platform-tools.zip"), "unpacked once, after its fetch"),
    (lambda p: changed(p, 1, strip=99), "strip is"),
    (lambda p: changed(p, 4, text="{nobody}"), "no variable of the recipe"),
    (lambda p: changed(p, 4, mode="4755"), "write is"),
    (lambda p: changed(p, 7, mode="777"), "chmod is"),
    (lambda p: {**recipe(p), "steps": recipe(p)["steps"][:1]}, "never unpacked"),
    (lambda p: {**recipe(p), "steps": [{"exec": "x"}]}, "exactly one of"),
])
def test_what_a_recipe_may_not_be(tmp_path, make, why):
    with pytest.raises(ValueError, match=why):
        r.validated(make(tmp_path))


@pytest.mark.parametrize("step, why", [
    ({"write": "../x", "text": "x"}, "not a path below"), ({"write": "/etc/x", "text": "x"}, "not a path below"),
    ({"link": "adb", "to": "/bin/sh"}, "absolute"), ({"link": "adb", "to": "../../x"}, "leaves the tree"),
    ({"chmod": "nothing", "mode": "755"}, "no file of the tree"),
])
def test_a_step_never_leaves_the_tree(tmp_path, data_home, step, why):
    made = recipe(tmp_path)
    made["steps"] = made["steps"][:4] + [step]
    with pytest.raises(ValueError, match=why):
        t.install(t.validated(entry(tmp_path, made=made)), unsigned=True, out=lambda _: None, accepted=True)
    assert left_in_store() == []


# --- the catalog ---------------------------------------------------------------------------------------------------


def test_the_catalog_lists_recipes_beside_the_archives_and_an_older_one_has_none(tmp_path, signing_key):
    key, trusted = signing_key
    entries, signature, problems = t.read_catalog(catalog(tmp_path, [entry(tmp_path)], key), keys=trusted)
    assert signature["state"] == "good" and not problems
    assert [(e["name"], e["version"], bool(e.get("recipe"))) for e in entries] == [("android-sdk", "37.0.0", True)]
    rows = t.catalog_listing(entries, found={})
    assert rows[0]["recipe"] is True and rows[0]["terms"]["id"] == "android-sdk-license"
    assert rows[0]["terms"]["option"] == FLAG and "developer.android.com/studio/terms" in rows[0]["terms"]["url"]
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"schema": t.SCHEMA, "version": 1, "toolchains": []}))
    assert t.read_catalog(str(old), keys=trusted)[0] == []
    broken = entry(tmp_path)
    broken["recipe"]["steps"][0]["url"] = "http://example.org/x.zip"
    entries, _, problems = t.read_catalog(catalog(tmp_path, [broken], key, name="broken.json"), keys=trusted)
    assert entries == [] and "url must be" in problems[0] and "left out" in problems[0]


# --- the license ---------------------------------------------------------------------------------------------------


def test_nothing_is_fetched_before_the_license_is_accepted(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    source = catalog(tmp_path, [entry(tmp_path)], key)
    said = []
    with pytest.raises(ValueError, match="no terminal to ask on.*toolchain add android-sdk " + FLAG):
        t.add("android-sdk", source, out=said.append, keys=trusted, opener=never, terminal=False)
    text = " ".join(said)
    assert "Android Software Development Kit License Agreement" in text
    assert "https://developer.android.com/studio/terms" in text and "redistributing" in text
    assert "fetched from Google by this machine" in text
    with pytest.raises(ValueError, match="was not accepted"):
        t.add("android-sdk", source, out=said.append, keys=trusted, opener=never, terminal=True, read=lambda _: "n")
    with pytest.raises(ValueError, match="was not accepted"):  # Enter alone is no
        t.add("android-sdk", source, out=said.append, keys=trusted, opener=never, terminal=True, read=lambda _: "")
    assert t.installed() == {} and left_in_store() == []


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_the_strict_modes_never_ask_about_a_license(tmp_path, signing_key, data_home, mode):
    key, trusted = signing_key
    source = catalog(tmp_path, [entry(tmp_path)], key)
    with pytest.raises(ValueError, match=f"security mode {mode} asks no question.*{FLAG}"):
        t.add("android-sdk", source, out=lambda _: None, keys=trusted, opener=never, terminal=True,
              read=lambda _: "y", mode=mode)
    done = t.add("android-sdk", source, out=lambda _: None, keys=trusted, accepted=True, mode=mode)
    assert done[0]["terms"]["how"] == "option"


def test_the_question_on_a_terminal_and_the_option_both_accept(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    source = catalog(tmp_path, [entry(tmp_path)], key)
    asked = []
    done = t.add("android-sdk", source, out=lambda _: None, keys=trusted, terminal=True,
                 read=lambda prompt: asked.append(prompt) or "y")
    assert "Do you accept the Android Software Development Kit License Agreement? [y/N]" in asked[0]
    assert done[0]["terms"]["id"] == "android-sdk-license" and done[0]["terms"]["how"] == "asked"
    assert t.installed()["android-sdk"]["37.0.0"]["terms"]["accepted"].endswith("Z")
    said = []
    t.add("android-sdk", source, out=said.append, keys=trusted, opener=never, terminal=False)  # there: no question
    assert said == ["android-sdk 37.0.0 is in the store already"]


def test_a_recipe_needs_the_catalogs_signature_like_an_archive(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    with pytest.raises(ValueError, match="not signed by a trusted key"):
        t.add("android-sdk", catalog(tmp_path, [entry(tmp_path)]), out=lambda _: None, keys=trusted, opener=never,
              accepted=True)
    source = catalog(tmp_path, [entry(tmp_path)], key)
    Path(source).write_text(Path(source).read_text().replace("for tests", "for others"))
    with pytest.raises(ValueError, match="signature does not verify"):
        t.add("android-sdk", source, out=lambda _: None, keys=trusted, opener=never, accepted=True)


# --- the build -----------------------------------------------------------------------------------------------------


def test_add_builds_the_tree_here_read_only_and_mounts_it_like_any_other(tmp_path, signing_key, data_home):
    key, trusted = signing_key
    said = []
    done = t.add("android-sdk", catalog(tmp_path, [entry(tmp_path)], key), out=said.append, keys=trusted,
                 accepted=True)
    record = done[0]
    tree = Path(record["path"])
    assert tree == t.store() / record["sha256"] and record["signed_by"] and record["recipe"]["steps"]
    assert (tree / "platform-tools/bin/adb").read_bytes() == TOOLS["platform-tools/bin/adb"]
    assert os.access(tree / "platform-tools/bin/adb", os.X_OK)
    assert (tree / "build-tools/37.0.0/bin/aapt2").is_file()  # {version} in a step's path
    assert os.readlink(tree / "build-tools/37.0.0/aapt2-link") == "bin/aapt2"
    assert (tree / "licenses/android-sdk-license").read_text() == "\n24333f8a"
    assert (tree / "platform-tools/package.xml").read_text() == "<license>the text</license> /opt/toolchains/android-sdk"
    assert os.readlink(tree / "adb") == "platform-tools/bin/adb"
    assert stat.S_IMODE((tree / "platform-tools/source.properties").stat().st_mode) == 0o555  # chmod 755, then sealed
    for path in [tree, *tree.rglob("*")]:
        if not path.is_symlink():
            assert not path.stat().st_mode & 0o222, path  # nothing of it is writable
    assert not list(t.store().glob(".build-*")) and not list(t.store().glob("download-*"))
    text = "\n".join(said)
    assert "fetching 1/2" in text and "fetching 2/2" in text and "building android-sdk 37.0.0 on this machine" in text
    assert "added android-sdk 37.0.0 (aapt2, adb): sandbox-ai AGENT --toolchain android-sdk" in text
    plan = t.start_plan(["android-sdk"], "podman")
    assert t.start_mounts(plan["entries"]) == [(tree, "/opt/toolchains/android-sdk")]
    assert plan["hosts"] == ["dl.google.com", "maven.google.com"]
    assert t.environment(plan["entries"])["ANDROID_HOME"] == "/opt/toolchains/android-sdk"
    listed = t.store_listing(runs={})[0]
    assert listed["recipe"] is True and listed["terms"]["id"] == "android-sdk-license"
    shown = "\n".join(t.show_lines(t.chosen(t.installed(), "android-sdk")))
    assert "built on this machine" in shown and "Android Software Development Kit License Agreement" in shown
    assert str(tmp_path / "platform-tools.zip") in shown
    # A run's registry keeps the manifest, not the recipe's texts.
    assert "recipe" not in t.registered(plan["entries"][0], "start")["manifest"]
    t.remove("android-sdk", out=lambda _: None, runs={})
    assert t.installed() == {} and not tree.exists()


@pytest.mark.parametrize("change, why", [
    ({"size": 7}, "more than the 7 bytes"), ({"sha256": "0" * 64}, "SHA-256 .* the recipe says"),
    ({"sha1": "0" * 40}, "SHA-1 .* the recipe says"),
])
def test_a_download_that_is_not_the_recipes_is_never_unpacked(tmp_path, signing_key, data_home, change, why):
    made = recipe(tmp_path)
    made["steps"][2] = {**made["steps"][2], **change}
    with pytest.raises(ValueError, match=why):
        t.install(t.validated(entry(tmp_path, made=made)), unsigned=True, out=lambda _: None, accepted=True)
    assert t.installed() == {} and left_in_store() == []


def test_a_download_shorter_than_the_recipe_says_is_refused(tmp_path, data_home):
    made = recipe(tmp_path)
    made["steps"][0]["size"] += 1
    with pytest.raises(ValueError, match="bytes, the recipe says"):
        t.install(t.validated(entry(tmp_path, made=made)), unsigned=True, out=lambda _: None, accepted=True)


@pytest.mark.parametrize("members, why", [
    ({"../../escape": b"x"}, "leaves the tree"),
    ({"a/": None, "a/link": ("link", "/etc/passwd")}, "absolute"),
    ({"a/": None, "a/link": ("link", "../../../etc")}, "leaves the tree"),
    ({"a": ("link", "b"), "a/file": b"x"}, "which is no folder"),
])
def test_a_zip_is_unpacked_without_trusting_it(tmp_path, data_home, members, why):
    made = recipe(tmp_path, steps=[fetch_step(tmp_path, "evil.zip", members), {"unpack": "evil.zip", "to": "x"}])
    with pytest.raises(ValueError, match=why):
        t.install(t.validated(entry(tmp_path, made=made, commands={"adb": "x/adb"})), unsigned=True,
                  out=lambda _: None, accepted=True)
    assert not (tmp_path / "escape").exists() and left_in_store() == []


def test_special_bits_are_dropped_and_two_names_one_file_system_takes_for_one_are_refused(tmp_path, data_home):
    members = {"bin/": None, "bin/su": ("mode", 0o4755, b"x"), "bin/open": ("mode", 0o666, b"y")}
    target = tmp_path / "tree"
    r.unzip(_file(tmp_path, "bits.zip", members), target)
    assert stat.S_IMODE((target / "bin/su").stat().st_mode) == 0o755
    assert stat.S_IMODE((target / "bin/open").stat().st_mode) == 0o644
    case = _file(tmp_path, "case.zip", {"include/xt_DSCP.h": b"a", "include/xt_dscp.h": b"b"})
    r.unzip(case, tmp_path / "sensitive")
    with pytest.raises(ValueError, match="takes for one name"):
        r.unzip(case, tmp_path / "insensitive", folded={})


def _file(tmp_path, name, members):
    path = tmp_path / name
    path.write_bytes(zipped(members))
    return path


def test_a_command_the_tree_lacks_and_a_full_disk_refuse_the_build(tmp_path, data_home, monkeypatch):
    with pytest.raises(ValueError, match="its tree has no bin/nothing"):
        t.install(t.validated(entry(tmp_path, commands={"adb": "bin/nothing"})), unsigned=True, out=lambda _: None,
                  accepted=True)
    assert left_in_store() == []
    monkeypatch.setattr(t.shutil, "disk_usage", lambda _: type("U", (), {"free": 1 << 20})())
    with pytest.raises(ValueError, match="needs .* MiB"):
        t.install(t.validated(entry(tmp_path, size=3 << 30, unpacked=5 << 30)), unsigned=True, out=lambda _: None,
                  accepted=True)


def test_what_it_requires_is_added_with_it(tmp_path, signing_key, data_home):
    import tarfile
    key, trusted = signing_key
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        info = tarfile.TarInfo("bin/java")
        info.size, info.mode = 2, 0o755
        tar.addfile(info, io.BytesIO(b"#!"))
    (tmp_path / "jdk.tar.xz").write_bytes(buffer.getvalue())
    jdk = {"name": "jdk", "version": "21.0.13+9", "arch": t.machine(), "url": str(tmp_path / "jdk.tar.xz"),
           "sha256": hashlib.sha256(buffer.getvalue()).hexdigest(), "commands": {"java": "bin/java"}}
    source = catalog(tmp_path, [entry(tmp_path, requires=["jdk"])], key, toolchains=[jdk])
    said = []
    done = t.add("android-sdk", source, out=said.append, keys=trusted, accepted=True)
    assert [item["name"] for item in done] == ["android-sdk", "jdk"]
    assert "android-sdk requires the toolchain jdk: it is added too" in said
    assert [e["name"] for e in t.start_plan(["android-sdk"], "podman")["entries"]] == ["jdk", "android-sdk"]


# --- the command line ----------------------------------------------------------------------------------------------


def run_cli(*argv, env=None):
    return subprocess.run([str(ROOT / "sandbox-ai"), "toolchain", *argv], capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL, env={**os.environ, **(env or {})})


def test_the_command_line_end_to_end(tmp_path, data_home):
    source = catalog(tmp_path, [entry(tmp_path)])
    listed = run_cli("catalog", "--catalog", source)
    assert listed.returncode == 0, listed.stderr
    assert "android-sdk" in listed.stdout and "37.0.0" in listed.stdout
    assert "built on this machine" in listed.stdout and FLAG in listed.stdout
    rows = json.loads(run_cli("catalog", "--catalog", source, "--json").stdout)["toolchains"]
    assert rows[0]["recipe"] is True and rows[0]["terms"]["title"].startswith("Android Software Development Kit")
    refused = run_cli("add", "android-sdk", "--catalog", source, "--unsigned")
    assert refused.returncode == 1 and "no terminal to ask on" in refused.stderr
    assert f"sandbox-ai toolchain add android-sdk {FLAG}" in refused.stderr
    assert "Android Software Development Kit License Agreement" in refused.stdout
    (tmp_path / "config/sandbox-ai").mkdir(parents=True)
    (tmp_path / "config/sandbox-ai/defaults.json").write_text(json.dumps({"version": 1, "mode": "paranoid"}))
    strict = run_cli("add", "android-sdk", "--catalog", source, "--unsigned")
    assert strict.returncode == 1 and "security mode paranoid asks no question" in strict.stderr
    added = run_cli("add", "android-sdk", "--catalog", source, "--unsigned", FLAG)
    assert added.returncode == 0, added.stderr
    assert f"accepted with {FLAG}" in added.stdout and "added android-sdk 37.0.0" in added.stdout
    shown = run_cli("show", "android-sdk")
    assert "built on this machine" in shown.stdout and "accepted" in shown.stdout
    row = json.loads(run_cli("list", "--json").stdout)[0]
    assert row["name"] == "android-sdk" and row["recipe"] is True
    tree = data_home / "sandbox-ai/toolchains" / row["sha256"]
    assert (tree / "licenses/android-sdk-license").is_file()
    assert run_cli("remove", "android-sdk").returncode == 0 and not tree.exists()
    assert FLAG in run_cli("add", "--help").stdout

