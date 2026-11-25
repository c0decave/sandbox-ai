"""Stand-ins for the toolchains of sandbox-ai-mcp's toolchains.json: the same names, commands, registries and
requirements as the published catalog (2026-11-22), each a tiny archive of empty commands. Written as a catalog
nobody signed (`sandbox-ai toolchain add NAME --catalog FILE --unsigned`, or as SANDBOX_AI_LOCAL_CATALOGS's
sandbox-ai-mcp/toolchains.json), so that a test's store has them without the network.

  catalog(directory, names=None)  the catalog file (and its archives) in DIRECTORY; its path
  install(directory, names=None)  each added to the store of this process's XDG_DATA_HOME (toolchains.add)
  opens(names)                    {"hosts": [...], "presets": [...]}: what a start with these --toolchain values opens
                                  (with what they require)
  writable(directory)             the unpacked (read-only) trees below DIRECTORY made writable, to be removed
"""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import platform
import tarfile

ROOT = Path(__file__).resolve().parents[1]
# name: (version, commands, hosts, the launcher's presets it opens, the toolchains it requires) - as published.
TOOLCHAINS = {
    "cc": ("0.17.1-1", ["cc", "c++", "gcc", "g++", "clang", "clang++", "ar", "ranlib", "cmake", "ctest", "cpack",
                        "ninja"], [], [], ["zig"]),
    "dotnet": ("10.0.403", ["dotnet"], ["api.nuget.org", "globalcdn.nuget.org"], ["github"], []),
    "go": ("1.27.3", ["go", "gofmt"], ["proxy.golang.org", "sum.golang.org"], ["github"], []),
    "jdk": ("21.0.13+9", ["java", "javac", "jar", "mvn", "gradle", "kotlin", "kotlinc"],
            ["repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "plugins-artifacts.gradle.org",
             "services.gradle.org", "downloads.gradle.org"], ["github"], []),
    "node": ("26.13.0", ["node", "npm", "npx", "tsc", "tsserver"], [], ["npm", "github"], []),
    "pyside6": ("6.12.0", ["pyside6-uic", "pyside6-rcc", "pyside6-designer", "pyside6-lupdate", "pyside6-qmllint"],
                [], [], ["python"]),
    "python": ("3.14.10", ["python", "python3", "pip", "pip3", "uv", "uvx"], [], ["pypi"], []),
    "qt": ("6.12.0", ["qmake", "qt-cmake", "moc", "uic", "rcc", "lupdate", "lrelease", "designer", "qmllint"],
           [], [], ["pyside6"]),
    "rust": ("1.99.1", ["cargo", "rustc", "rustdoc", "rustfmt", "cargo-clippy"],
             ["crates.io", "index.crates.io", "static.crates.io"], ["github"], ["cc"]),
    "zig": ("0.17.1", ["zig"], [], ["github"], []),
    # (in the published catalog a recipe built on the user's machine from Google's downloads, after their license was
    # accepted: tests/test_toolchain_recipes.py; here an archive like the others - what a start mounts is the same)
    "android-sdk": ("37.0.0", ["adb", "fastboot", "apkanalyzer", "aapt", "aapt2", "apksigner", "zipalign", "d8",
                               "aidl", "ndk-build", "cmake", "ninja"], ["dl.google.com", "maven.google.com"], [],
                    ["jdk"]),
}


def machine():
    value = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(value, value)


def archive(commands):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        folder = tarfile.TarInfo("bin")
        folder.type, folder.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(folder)
        for command in commands:
            data = f"#!/bin/sh\necho {command}\n".encode()
            info = tarfile.TarInfo(f"bin/{command}")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def catalog(directory, names=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    entries = []
    for name in names or TOOLCHAINS:
        version, commands, hosts, allow, requires = TOOLCHAINS[name]
        data = archive(commands)
        path = directory / f"toolchain-{name}-{version}.tar.xz"
        path.write_bytes(data)
        entries.append({"name": name, "version": version, "arch": machine(), "url": str(path),
                        "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "unpacked": 4096,
                        "title": f"{name} (a stand-in)", "description": f"a stand-in of the toolchain {name} for tests",
                        "license": "MIT", "homepage": "https://example.org",
                        "commands": {command: f"bin/{command}" for command in commands}, "env": {}, "caches": {},
                        "hosts": hosts, "allow": allow, "needs": {"glibc": None, "libraries": []},
                        "requires": requires, "sources": []})
    path = directory / "toolchains.json"
    path.write_text(json.dumps({"schema": "sandbox-ai-toolchains", "version": 1, "toolchains": entries}))
    return str(path)


def install(directory, names=None):
    spec = importlib.util.spec_from_file_location("toolchains_for_fakes", ROOT / "sandbox" / "toolchains.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = catalog(directory)
    for name in names or TOOLCHAINS:
        module.add(name, source, unsigned=True, out=lambda line: None)


def writable(directory):
    """The store's trees below DIRECTORY writable again (they are unpacked read-only), so that it can be removed."""
    import os
    for folder, dirs, _ in os.walk(directory):
        for name in dirs:
            child = os.path.join(folder, name)
            if not os.path.islink(child):
                os.chmod(child, 0o700)


def with_required(names):
    found = []

    def take(name):
        if name in found:
            return
        for other in TOOLCHAINS[name][4]:
            take(other)
        found.append(name)
    for name in names:
        take(name.split("@", 1)[0])
    return found


def opens(names):
    every = with_required(names)
    return {"hosts": list(dict.fromkeys(host for name in every for host in TOOLCHAINS[name][2])),
            "presets": list(dict.fromkeys(preset for name in every for preset in TOOLCHAINS[name][3]))}
