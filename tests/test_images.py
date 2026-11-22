"""sandbox-ai images, with podman replaced by recorded answers: listing, packages, extensions (validator,
changes, variants, history), the extension layer of sandbox-ai build and --image-variant."""
import fnmatch
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("images_tests", ROOT / "sandbox/images.py")
im = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(im)


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    """Extensions and their history in temporary directories, never in the HOME of whoever runs the tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(im, "user_bus", lambda uid=None: True)  # a desktop login (test_without_a_user_session)
    return tmp_path


def test_without_a_user_session_podman_uses_cgroupfs(monkeypatch):
    """An unprivileged LXC or Proxmox container without a systemd user session: a build's RUN steps would ask
    systemd for their cgroup ("sd-bus call: Permission denied"), so every podman command says cgroupfs."""
    monkeypatch.setattr(im, "user_bus", lambda uid=None: False)
    assert im.podman() == ["podman", "--cgroup-manager=cgroupfs"]
    assert im.podman("unix:///run/podman.sock") == ["podman", "--remote", "--url", "unix:///run/podman.sock"]
    monkeypatch.setattr(im, "user_bus", lambda uid=None: True)
    assert im.podman() == ["podman"]


def fake(argv, **kwargs):
    if argv[1:3] == ["image", "inspect"]:
        if argv[-1] == "localhost/claude-sandbox:latest":
            return subprocess.CompletedProcess(argv, 0, json.dumps([{
                "Id": "sha256abcdef0123456789", "Created": "2026-11-03T10:00:00.123Z", "Size": 2_100_000_000,
                "Config": {"Labels": {"sandbox-ai.agent-version": "2.9.1"}}}]), "")
        return subprocess.CompletedProcess(argv, 125, "", "image not known")
    if argv[1] == "run":
        assert "--network=none" in argv and "--cap-drop=ALL" in argv and "--read-only" in argv
        return subprocess.CompletedProcess(argv, 0, "deb\tgit\t1:2.47\nnpm\t@anthropic-ai/claude-code\t2.9.1\n"
                                                    "tool\tgh\tgh version 2.80\npip\truff\t0.6.0\nnoise\n", "")
    raise AssertionError(argv)


def test_images_and_packages(capsys):
    found = im.images(run=fake)
    assert found[0] == {"agent": "claude", "target": "claude", "variant": False,
                        "image": "localhost/claude-sandbox:latest", "present": True, "id": "sha256abcdef",
                        "created": "2026-11-03T10:00:00", "size": 2_100_000_000, "agent_version": "2.9.1",
                        "extended": False, "extension_hash": None, "built_hash": None, "status": "current"}
    assert not found[1]["present"] and found[1]["agent"] == "codex" and found[1]["status"] == "not-built"
    assert im.packages("claude", run=fake) == [("deb", "git", "1:2.47"), ("npm", "@anthropic-ai/claude-code", "2.9.1"),
                                               ("tool", "gh", "gh version 2.80"), ("pip", "ruff", "0.6.0")]
    with pytest.raises(ValueError):
        im.packages("other", run=fake)
    assert im.main([], run=fake) == 0
    out = capsys.readouterr().out
    assert "codex              not built (sandbox-ai build codex)" in out and "2.9.1" in out and "2.1 GB" in out
    assert "no extension" in out
    assert im.main(["packages", "claude", "--json"], run=fake) == 0
    assert json.loads(capsys.readouterr().out)[0] == {"kind": "deb", "name": "git", "version": "1:2.47"}


# -- the validator --

@pytest.mark.parametrize("fragment, rule, line, text", [
    ("FROM debian:12", "from", 1, "FROM"),
    ("ENTRYPOINT [\"/bin/sh\"]", "entrypoint", 1, "ENTRYPOINT"),
    ("cmd sleep 1", "entrypoint", 1, "cmd"),
    ("VOLUME /data", "volume", 1, "VOLUME"),
    ("ONBUILD RUN x", "onbuild", 1, "ONBUILD"),
    ("FETCH x", "unknown", 1, "FETCH"),
    ("RUN apt-get update && \\\n    chmod u+s /usr/bin/ping", "setuid", 2, "u+s"),
    ("RUN chmod 4755 /opt/tool", "setuid", 1, "4755"),
    ("RUN chmod g=rxs /opt/tool", "setuid", 1, "g=rxs"),
    ("RUN find /opt -exec chmod +s {} \\;", "setuid", 1, "+s"),
    ("RUN install -m 6755 x /usr/local/bin/x", "setuid", 1, "6755"),
    ("COPY --chmod=2755 files/x /usr/local/bin/x", "setuid", 1, "--chmod=2755"),
    ("RUN setcap cap_net_raw+ep /usr/bin/x", "setcap", 1, "setcap"),
    ("RUN ln -sf /bin/true /usr/sbin/nft", "protected", 1, "/usr/sbin/nft"),
    ("RUN rm -f /usr/bin/firejail", "protected", 1, "/usr/bin/firejail"),
    ("RUN cp fake /usr/local/bin/setpriv", "protected", 1, "/usr/local/bin/setpriv"),
    ("RUN echo /x.so > /etc/ld.so.preload", "protected", 1, "/etc/ld.so.preload"),
    ("RUN sed -i s/a/b/ /sandbox/runtime.py", "protected", 1, "/sandbox/runtime.py"),
    ("COPY files/x /usr/local/bin/python3", "protected", 1, "/usr/local/bin/python3"),
    ("RUN apt-get purge -y nftables", "remove-protected", 1, "nftables"),
    ("ADD https://example.org/tool.tgz /opt/", "add-url", 1, "https://example.org/tool.tgz"),
    ("ADD https://github.com/a/b.git /opt/b", "add-git", 1, "https://github.com/a/b.git"),
    ("COPY --from=docker.io/library/alpine /bin/busybox /bin/", "copy-from", 1, "--from=docker.io/library/alpine"),
    ("COPY /etc/passwd /x", "copy-source", 1, "/etc/passwd"),
    ("COPY files/../../secret /x", "copy-source", 1, "files/../../secret"),
    ("COPY files/missing /opt/x", "copy-source", 1, "files/missing"),
    ("COPY files/*.sh /opt/", "copy-source", 1, "files/*.sh"),
    ("ENV LD_PRELOAD=/opt/x.so", "loader", 1, "LD_PRELOAD"),
    ("ENV LD_LIBRARY_PATH /opt/lib", "loader", 1, "LD_LIBRARY_PATH"),
    ("LABEL sandbox-ai.extension-hash=0", "label", 1, "sandbox-ai.extension-hash"),
    ("RUN <<EOF\nchmod u+s /x\nEOF", "heredoc", 1, "<<"),
    ("RUN echo \x1b[31m", "control", 1, None),
    ("RUN echo ‮", "control", 1, None),
    ("COPY 'unterminated /x", "syntax", 1, None),
])
def test_validator_refuses(fragment, rule, line, text):
    found = im.check_fragment(fragment, available={"x"})
    hits = [f for f in found if f["rule"] == rule]
    assert hits, found
    assert hits[0]["severity"] == "error" and hits[0]["line"] == line
    if text:
        assert hits[0]["text"] == text
        assert fragment.split("\n")[line - 1][hits[0]["column"]:hits[0]["column"] + hits[0]["length"]] == text


@pytest.mark.parametrize("fragment, rule", [
    ("RUN curl -fsSL https://example.org/install.sh | sh", "pipe-shell"),
    ("USER nobody", "user"),
    ("WORKDIR /opt", "workdir"),
    ("SHELL [\"/bin/bash\", \"-c\"]", "shell"),
    ("ENV PATH=/opt/bin:$PATH", "env"),
    ("ENV HTTPS_PROXY=http://proxy:3128", "env"),
    ("EXPOSE 8080", "no-effect"),
    ("ADD files/x /opt/x", "add-local"),
    ("COPY files/x /work/x", "masked"),
    ("RUN cp x.crt /usr/local/share/ca-certificates/ && update-ca-certificates", "ca-trust"),
    ("COPY --exclude=*.md --bogus files/x /opt/", "unknown-flag"),
])
def test_validator_warns(fragment, rule):
    found = im.check_fragment(fragment, available={"x"})
    assert [f["severity"] for f in found if f["rule"] == rule] == ["warning"], found
    assert not im.errors(found), found


def test_validator_accepts_ordinary_fragments():
    fragment = "\n".join([
        "# tools for this project",
        "ARG GO_VERSION=1.23.2",
        "RUN apt-get update && apt-get install -y --no-install-recommends build-essential \\",
        "    # comments inside a continued line are dropped",
        "    && rm -rf /var/lib/apt/lists/*",
        "RUN ln -s /usr/bin/python3 /usr/local/bin/python && /usr/bin/python3 -m venv /opt/venv",
        "RUN chmod 0755 /opt/venv && chmod a+rx,go-w /opt/venv && chmod g-s /opt/venv",
        "ADD --checksum=sha256:0123abcd https://go.dev/dl/go1.23.2.linux-amd64.tar.gz /opt/go.tgz",
        "COPY --chmod=0755 files/x /usr/local/bin/x",
        "COPY [\"files/x\", \"/opt/x\"]",
        "ENV GOPATH=/opt/go PIP_INDEX_URL=https://pypi.org/simple",
        "LABEL org.opencontainers.image.title=mine",
    ])
    assert im.check_fragment(fragment, available={"x"}) == []
    assert im.logical_lines("RUN a \\\n\n  b\nENV X=1")[0] == (1, 3, "RUN", "a b")


@pytest.mark.parametrize("kind, value, ok", [
    ("apt", "ripgrep", True), ("apt", "libssl-dev=3.0.15-1~deb12u1", True), ("apt", "Bad", False),
    ("apt", "x; rm -rf /", False), ("npm", "@scope/pkg@^1.2", True), ("npm", "typescript", True),
    ("npm", "git+https://example.org/x.git", False), ("npm", "file:../x", False), ("npm", "@openai/codex@1.0", False),
    ("pip", "ruff==0.6.9", True), ("pip", "httpie[socks]>=3", True), ("pip", "https://x/y.whl", False),
    ("pip", "-e .", False), ("pip", "../local", False),
])
def test_package_checks(kind, value, ok):
    assert (im.check_package(kind, value, "codex") is None) == ok


def test_containerfile_restores_the_launchers_expectations():
    document = {"version": 1, "agent": "codex", "apt": ["ripgrep", "fd-find"], "npm": ["typescript@5"],
                "pip": ["ruff==0.6", "httpie[socks]>=3"], "files": [{"source": "x", "target": "/usr/local/bin/x",
                                                                    "mode": "0755"}]}
    text = im.containerfile("go", document, "USER nobody\nWORKDIR /opt\n")
    lines = text.splitlines()
    assert lines[1] == "FROM localhost/codex-sandbox:base" and lines[-2:] == ["USER root", "WORKDIR /work"]
    assert "    ripgrep fd-find \\" in lines and "RUN npm install --global typescript@5 \\" in lines
    assert "    && uv tool install --python /usr/bin/python3 'httpie[socks]>=3'" in lines
    assert "COPY --chmod=0755 files/x /usr/local/bin/x" in lines
    assert lines.index("USER nobody") < len(lines) - 2
    assert sum(1 for line in lines if line.startswith("FROM")) == 1
    assert im.extension_hash(text, {"x": b"1"}) != im.extension_hash(text, {"x": b"2"})


# -- changes, variants, history --

def test_extend_new_remove_and_history(config, capsys, tmp_path):
    tool = tmp_path / "host" / "tool.sh"
    tool.parent.mkdir()
    tool.write_text("#!/bin/sh\necho hi\n")
    assert im.main(["extend", "claude", "--add-apt", "ripgrep", "--add-pip", "ruff==0.6", "--add-file",
                    f"{tool}:/usr/local/bin/tool:0755"]) == 0
    assert "changed: +apt ripgrep" in capsys.readouterr().out
    directory = config / "config" / "sandbox-ai" / "images" / "claude"
    assert json.loads((directory / "image.json").read_text()) == {
        "version": 1, "agent": "claude", "apt": ["ripgrep"], "npm": [], "pip": ["ruff==0.6"],
        "files": [{"source": "tool.sh", "target": "/usr/local/bin/tool", "mode": "0755"}]}
    assert (directory / "files" / "tool.sh").read_bytes() == tool.read_bytes()
    assert oct((directory / "image.json").stat().st_mode & 0o777) == "0o600"
    # A new version replaces the old one; the fragment comes from standard input.
    fragment = "RUN go version\nCOPY files/tool.sh /opt/tool.sh\n"
    assert im.main(["extend", "claude", "--add-apt", "ripgrep=14.1.0-1", "--fragment", "-", "--json"],
                   stdin=io.BytesIO(fragment.encode())) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["apt"] == ["ripgrep=14.1.0-1"] and shown["fragment"] == fragment and shown["extended"]
    assert shown["findings"] == [] and "COPY files/tool.sh /opt/tool.sh" in shown["containerfile"]
    # Errors refuse the change; nothing is written.
    before = (directory / "image.json").read_text()
    assert im.main(["extend", "claude", "--add-npm", "x", "--fragment", "-"], stdin=io.BytesIO(b"FROM alpine\n")) == 1
    assert "FROM is not allowed" in capsys.readouterr().err
    assert (directory / "image.json").read_text() == before and (directory / "Containerfile.fragment").read_text() == fragment
    assert im.main(["extend", "claude", "--add-npm", "git+https://x/y"]) == 1
    assert im.main(["extend", "claude", "--remove-npm", "absent"]) == 1
    # check: findings and the final Containerfile, exit 1 on errors.
    assert im.main(["check", "claude", "--fragment", "-"], stdin=io.BytesIO(b"USER nobody\n")) == 0
    out = capsys.readouterr().out
    assert "warning: line 1 (USER)" in out and "FROM localhost/claude-sandbox:base" in out
    assert im.main(["check", "claude", "--fragment", "-", "--json"], stdin=io.BytesIO(b"VOLUME /x\n")) == 1
    assert json.loads(capsys.readouterr().out)["findings"][0]["rule"] == "volume"
    # A variant starts from the agent's extension.
    assert im.main(["new", "gpu", "--from", "claude"]) == 0
    assert "--image-variant gpu" in capsys.readouterr().out
    assert im.agent_of("gpu") == "claude" and im.variants() == [("gpu", "claude")]
    assert im.describe("gpu")["apt"] == ["ripgrep=14.1.0-1"] and im.describe("gpu")["available"] == ["tool.sh"]
    assert im.main(["new", "gpu", "--from", "claude"]) == 1, "exists already (for that agent)"
    for name in ("claude", "all", "vpn", "Upper", "a/b", "x" * 31):  # vpn: sandbox-ai build vpn
        assert im.main(["new", name, "--from", "codex"]) == 1
    assert im.main(["new", "empty", "--from", "codex", "--empty", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["image"] == "localhost/codex-sandbox-empty:latest"
    assert im.variant_image("codex", "empty") == "localhost/codex-sandbox-empty:latest"
    with pytest.raises(ValueError, match="is a variant of codex"):
        im.variant_image("claude", "empty")
    with pytest.raises(ValueError, match="no such variant"):
        im.variant_image("claude", "nothing")
    # Removing files and packages; a file nothing uses any more is dropped.
    assert im.main(["extend", "gpu", "--remove-file", "/usr/local/bin/tool", "--clear-fragment",
                    "--remove-apt", "ripgrep"]) == 0
    capsys.readouterr()
    assert im.describe("gpu")["available"] == [] and im.describe("gpu")["apt"] == []
    removed = []
    assert im.main(["remove", "gpu"], run=lambda argv, **k: removed.append(argv) or
                   subprocess.CompletedProcess(argv, 0, "", "")) == 0
    assert removed == [["podman", "image", "rm", "localhost/claude-sandbox-gpu:latest"]]
    assert im.variants() == [("empty", "codex")]
    assert im.main(["remove", "claude"]) == 0 and "leaves the extension out" in capsys.readouterr().out
    assert not directory.exists()
    assert im.main(["history", "--json"]) == 0
    actions = [(e["action"], e["target"]) for e in json.loads(capsys.readouterr().out)]
    assert actions == [("extend", "claude"), ("extend", "claude"), ("new", "gpu"), ("new", "empty"),
                       ("extend", "gpu"), ("remove", "gpu"), ("remove", "claude")]
    assert im.main(["history", "gpu"]) == 0
    assert capsys.readouterr().out.count("gpu") == 3


def test_extensions_stay_out_of_what_a_sandbox_can_write(config, tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "tool").write_text("x")
    registry = config / "state" / "sandbox-ai" / "exposed.json"
    registry.write_text(json.dumps([str(workspace)]))
    assert im.main(["extend", "codex", "--add-file", f"{workspace / 'tool'}:/usr/local/bin/tool"]) == 1
    assert "which a sandbox can write" in capsys.readouterr().err
    # A symlink a sandbox could have planted, on the way to the source.
    host = tmp_path / "host"
    host.mkdir()
    (host / "tool").write_text("x")
    (workspace / "link").symlink_to(host)
    assert im.main(["extend", "codex", "--add-file", f"{workspace / 'link' / 'tool'}:/usr/local/bin/tool"]) == 1
    assert "planted" in capsys.readouterr().err
    # The configuration directory itself inside a workspace.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(workspace / "config"))
    assert im.main(["extend", "codex", "--add-apt", "jq"]) == 1
    assert "which a sandbox can write" in capsys.readouterr().err
    assert not (workspace / "config").exists()
    # The source itself must be a regular file, not a link.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config / "config"))
    (host / "link").symlink_to(host / "tool")
    assert im.main(["extend", "codex", "--add-file", f"{host / 'link'}:/usr/local/bin/tool"]) == 1
    assert im.main(["extend", "codex", "--add-file", f"{host / 'tool'}:/usr/local/bin/tool:4755"]) == 1
    assert im.main(["extend", "codex", "--add-file", f"{host / 'tool'}:/usr/local/bin/tool:0777"]) == 1
    assert im.main(["extend", "codex", "--add-file", f"{host / 'tool'}:relative"]) == 1


# -- building and status --

class Engine:
    """Podman as far as the extension layers need it: images by name with their labels, builds recorded."""

    def __init__(self):
        self.images, self.calls, self.contexts = {}, [], []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            data = self.images.get(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if data else 125, json.dumps([data]) if data else "", "")
        if argv[1] == "tag":
            self.images[argv[3]] = self.images[argv[2]]
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1] == "build":
            context = Path(argv[-1])
            self.contexts.append({str(p.relative_to(context)): p.read_text() for p in context.rglob("*") if p.is_file()})
            labels = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--label")
            tag = argv[argv.index("--tag") + 1]
            self.images[tag] = {"Id": f"id-{len(self.calls)}", "Created": "2026-11-04T10:00:00Z", "Size": 1,
                                "Config": {"Labels": {"sandbox-ai.agent-version": "2.9.1", **labels}}}
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


def test_layers_are_built_on_the_base_and_labelled(config, tmp_path, capsys):
    engine = Engine()
    with pytest.raises(ValueError, match="no base image"):
        im.build_layers("claude", run=engine)
    engine.images["localhost/claude-sandbox:base"] = {"Id": "base1", "Config": {"Labels": {}}}
    # No extension: the base is the agent's image.
    assert im.build_layers("claude", run=engine) == 0
    assert engine.calls[-1] == ["podman", "tag", "localhost/claude-sandbox:base", "localhost/claude-sandbox:latest"]
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    assert im.main(["extend", "claude", "--add-apt", "jq", "--add-file", f"{tool}:/usr/local/bin/tool:0755"]) == 0
    assert im.main(["new", "web", "--from", "claude"]) == 0
    assert im.main(["new", "other", "--from", "codex", "--empty"]) == 0
    status = {row["target"]: row["status"] for row in im.images(run=engine)}
    assert status == {"claude": "outdated", "codex": "not-built", "opencode": "not-built", "opencode2": "not-built",
                      "hermes": "not-built",
                      "dsh": "not-built", "qwen": "not-built", "goose": "not-built", "openhands": "not-built",
                      "nanobot": "not-built", "letta": "not-built", "pi": "not-built", "web": "not-built",
                      "other": "not-built"}
    capsys.readouterr()
    assert im.main(["layer", "claude", "--variants"], run=engine) == 0
    builds = [call for call in engine.calls if call[1] == "build"]
    assert [b[b.index("--tag") + 1] for b in builds] == ["localhost/claude-sandbox:latest",
                                                          "localhost/claude-sandbox-web:latest"]
    assert "--pull=never" in builds[0] and "sandbox-ai.base-id=base1" in builds[0]
    assert engine.contexts[0]["files/tool"] == "#!/bin/sh\n" and set(engine.contexts[0]) == {"Containerfile", "files/tool"}
    assert engine.contexts[0]["Containerfile"].startswith("# Extension claude of claude")
    rows = {row["target"]: row for row in im.images(run=engine)}
    assert rows["claude"]["status"] == "current" and rows["web"]["status"] == "current"
    assert rows["claude"]["built_hash"] == rows["claude"]["extension_hash"] == im.describe("claude")["hash"]
    # The extension changes: outdated until rebuilt; a new base: base-changed.
    assert im.main(["extend", "web", "--add-npm", "typescript"]) == 0
    engine.images["localhost/claude-sandbox:base"] = {"Id": "base2", "Config": {"Labels": {}}}
    rows = {row["target"]: row for row in im.images(run=engine)}
    assert rows["web"]["status"] == "outdated" and rows["claude"]["status"] == "base-changed"
    assert im.main([], run=engine) == 0
    out = capsys.readouterr().out
    assert "base rebuilt: rebuild" in out and "web (claude)" in out and "extension changed: rebuild" in out
    # An extension with errors is not built; the others are.
    directory = config / "config" / "sandbox-ai" / "images" / "variants" / "claude" / "web"
    (directory / "Containerfile.fragment").write_text("FROM alpine\n")
    assert im.main(["layer", "claude", "--variants"], run=engine) == 1
    assert "FROM is not allowed" in capsys.readouterr().err
    assert {row["target"]: row["status"] for row in im.images(run=engine)}["web"] == "invalid"
    assert [e["action"] for e in im.history("claude")][-1] == "build"


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    """podman on PATH that logs every call; inspect knows the base images once built."""
    log = tmp_path / "podman.log"
    script = tmp_path / "bin" / "podman"
    script.parent.mkdir()
    script.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
with open({str(log)!r}, "a") as handle:
    handle.write(json.dumps(args) + "\\n")
if args[:2] == ["--remote", "--url"]:
    args = args[3:]
if args[:1] == ["--cgroup-manager=cgroupfs"]:  # no systemd user session: a global option first
    args = args[1:]
if args[:2] == ["image", "inspect"]:
    print(json.dumps([{{"Id": "base-id", "Config": {{"Labels": {{}}}}}}]))
""")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_PYTHON", sys.executable)
    monkeypatch.setenv("SANDBOX_AI_BUILD_NO_DNS_CHECK", "1")  # (its own tests below: test_build_network)
    cgroups = [] if os.path.exists(f"/run/user/{os.getuid()}/bus") else ["--cgroup-manager=cgroupfs"]

    def calls():
        """The engine's calls, each without the global option every one carries without a systemd user session."""
        found = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        for call in found:
            if call[:2] != ["--remote", "--url"]:
                assert call[:len(cgroups)] == cgroups, call
        return [call[len(cgroups):] if call[:2] != ["--remote", "--url"] else call for call in found]
    return calls


def builder(*args):
    return subprocess.run([str(ROOT / "sandbox-ai"), "build", *args], capture_output=True, text=True)


def test_builder_adds_the_extension_layer(fake_engine, tmp_path):
    result = builder("--build-arg", "CLAUDE_VERSION=1.2.3", "claude")
    assert result.returncode == 2  # options come after the agent
    result = builder("claude", "--build-arg", "CLAUDE_VERSION=1.2.3")
    assert result.returncode == 0, result.stderr
    calls = fake_engine()
    assert len(calls) == 1 and calls[0][:5] == ["build", "--tag", "localhost/claude-sandbox:latest", "--tag",
                                                "localhost/claude-sandbox:base"]
    assert im.main(["extend", "claude", "--add-apt", "jq"]) == 0
    assert im.main(["new", "web", "--from", "claude", "--empty"]) == 0
    result = builder("claude", "--build-arg", "CLAUDE_VERSION=1.2.3")
    assert result.returncode == 0, result.stderr
    calls = fake_engine()[1:]
    assert calls[0][:3] == ["build", "--tag", "localhost/claude-sandbox:base"] and "--tag" not in calls[0][3:]
    builds = [c for c in calls[1:] if c[0] == "build"]
    assert [b[b.index("--tag") + 1] for b in builds] == ["localhost/claude-sandbox:latest",
                                                          "localhost/claude-sandbox-web:latest"]
    assert "sandbox-ai.base-id=base-id" in builds[0]
    # A variant: its agent's base (with the agent's own extension the base keeps :base only) and that variant.
    count = len(fake_engine())
    result = builder("web", "--build-arg", "CLAUDE_VERSION=1.2.3")
    assert result.returncode == 0, result.stderr
    calls = fake_engine()[count:]
    builds = [c for c in calls if c[0] == "build"]
    assert [b[b.index("--tag") + 1] for b in builds] == ["localhost/claude-sandbox:base",
                                                          "localhost/claude-sandbox-web:latest"]
    # --extension-only: no base build, no version lookup.
    count = len(fake_engine())
    result = builder("--extension-only", "--podman-url", "unix:///tmp/engine.sock", "claude")
    assert result.returncode == 0, result.stderr
    calls = fake_engine()[count:]
    assert all(c[:3] == ["--remote", "--url", "unix:///tmp/engine.sock"] for c in calls)
    assert [c[3] for c in calls if "build" in c] == ["build", "build"]
    assert "sandbox-ai.agent-version" not in json.dumps(calls)
    # Errors in the extension stop the build before anything runs.
    count = len(fake_engine())
    (tmp_path / "config" / "sandbox-ai" / "images" / "claude" / "Containerfile.fragment").write_text("FROM x\n")
    result = builder("claude", "--build-arg", "CLAUDE_VERSION=1.2.3")
    assert result.returncode == 1 and "fix the extension of claude" in result.stderr
    assert len(fake_engine()) == count
    result = builder("nothing")
    assert result.returncode == 2 and "nor a usable variant" in result.stderr


# -- the launcher --

def launcher():
    spec = importlib.util.spec_from_file_location("agent_sandbox_images", ROOT / "sandbox/agent_sandbox.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_image_variant_option(config):
    s = launcher()
    assert im.main(["new", "web", "--from", "claude", "--empty"]) == 0
    a = s.parse_args(["claude", "-iv", "web"])
    assert a.image == "localhost/claude-sandbox-web:latest"
    s.check_image_variant(a)
    # Another agent's variant is made for this one too (a preset or board switched to another agent).
    adopted = s.parse_args(["codex", "--image-variant", "web"])
    s.check_image_variant(adopted)
    assert adopted.image == "localhost/codex-sandbox-web:latest"
    for argv, message in ((["claude", "--image-variant", "nope"], "no such variant"),
                          (["claude", "--image-variant", "web", "--image", "localhost/x"], "exclude each other"),
                          (["claude", "--image-variant", "web", "--backend", "bwrap"], "only to the Podman")):
        with pytest.raises(ValueError, match=message):
            s.check_image_variant(s.parse_args(argv))
    # Forbidding --image forbids other images (variants too), not the default one.
    s.check_policy(s.parse_args(["claude"]), {"version": 1, "forbid": ["--image"]})
    for argv in (["claude", "--image", "localhost/x"], ["claude", "-iv", "web"]):
        with pytest.raises(ValueError, match="administrator's policy"):
            s.check_policy(s.parse_args(argv), {"version": 1, "forbid": ["--image"]})
    s.check_policy(s.parse_args(["claude", "-iv", "web"]), {"version": 1, "forbid": ["--image-variant=other"]})


def test_tor_needs_no_recipe_and_a_variant_called_tor_still_works(config):
    """No recipe tor (the user's decision, live use 2026-11-14): Tor networking needs nothing in the agent's image - the
    Tor container beside it carries everything. A variant of yours called tor stays an ordinary variant: started with
    --image-variant tor, built with --variant (`build tor` is the Tor VPN's image)."""
    assert "tor" not in im.RECIPES
    lines = []
    with pytest.raises(SystemExit):  # (not among the recipes argparse offers)
        im.main(["recipe", "tor", "--from", "claude"], out=lines.append)
    assert im.main(["new", "tor", "--from", "claude", "--empty"], out=lines.append) == 0
    assert im.variant_image("claude", "tor") == "localhost/claude-sandbox-tor:latest"
    assert im.build_command("tor") == "sandbox-ai build --variant tor"
    assert im.build_command("media") == "sandbox-ai build media"


def test_the_dev_recipe(config):
    """`images recipe dev`: what developing sandbox-ai needs with --nested-containers - Podman and its helpers, the
    kernel tools of the kill switch tests, tor, a static busybox for local test images, the app tests' libraries -
    all Debian packages of the image's release, no fragment (nothing from elsewhere); the validator accepts it."""
    lines = []
    assert im.main(["recipe", "dev", "--from", "claude"], out=lines.append) == 0
    assert "sandbox-ai build dev" in lines[-1] and "--image-variant dev" in lines[-1]
    document = json.loads((im.config_root() / "variants" / "claude" / "dev" / "image.json").read_text())
    for package in ("podman", "fuse-overlayfs", "slirp4netns", "passt", "uidmap", "nftables", "iproute2", "tor",
                    "git", "python3-venv", "busybox-static", "libegl1", "libgl1", "libxkbcommon0", "libfontconfig1",
                    "libdbus-1-3", "xvfb", "spectrwm", "xdotool", "x11-apps"):
        assert package in document["apt"], package
    assert all("=" not in package for package in document["apt"])  # Debian's own versions of the release
    assert not document.get("npm") and not document.get("pip") and not document.get("files")
    path = im.config_root() / "variants" / "claude" / "dev" / im.FRAGMENT  # the agent's own fragment, copied (none here)
    fragment = path.read_text() if path.exists() else ""
    assert im.errors(im.check(document, fragment, {})) == []
    text = im.containerfile("dev", document, fragment)
    assert "apt-get install -y --no-install-recommends" in text and "podman" in text and "curl" not in text
    assert "dev" in im.RECIPES and "Podman" in im.RECIPES["dev"]["description"]
    with pytest.raises(ValueError, match="exists already"):
        im.recipe("dev", "claude")


def test_the_django_recipe(config):
    """`images recipe django` (the showcase preset showcase-full-stack): Django 5.2 LTS and psycopg 3 in a venv of
    their own from sandbox-ai's lockfile (every wheel pinned by version and SHA-256, pip --require-hashes --no-deps
    --only-binary, no build step), TypeScript from an npm lockfile (SHA-512, npm ci --ignore-scripts), psql and libpq
    from Debian; nothing installed from a range, the validator accepts it."""
    lines = []
    assert im.main(["recipe", "django", "--from", "codex"], out=lines.append) == 0
    assert "sandbox-ai build django" in lines[-1] and "--image-variant django" in lines[-1]
    document, fragment, files = im.load("django")
    assert im.errors(im.check(document, fragment, files)) == []
    assert {"postgresql-client", "libpq5"} <= set(document["apt"]) and not document["npm"] and not document["pip"]
    assert {e["target"] for e in document["files"]} == {"/opt/django/requirements.txt", "/opt/django/package.json",
                                                        "/opt/django/package-lock.json"}
    for name in ("requirements.txt", "package.json", "package-lock.json"):
        assert files[name] == (im.RECIPE_FILES / "django" / name).read_bytes(), name
    text = im.containerfile("django", document, fragment)
    assert "python3 -m venv /opt/django/venv" in text
    assert "pip install --no-cache-dir --require-hashes --no-deps --only-binary :all: -r /opt/django/requirements.txt" \
        in text
    assert "cd /opt/django" in text and "npm ci --omit=dev --ignore-scripts" in text and "npm install" not in text
    assert "/usr/local/bin/django-admin" in text and "/usr/local/bin/tsc" in text and "curl" not in text
    # The lockfiles: one exact version and hash per package, nothing else named.
    requirements = (im.RECIPE_FILES / "django" / "requirements.txt").read_text()
    pins = re.findall(r"^([a-z0-9-]+)==(\S+) \\\n\s+--hash=sha256:([0-9a-f]{64})$", requirements, re.M)
    assert {name for name, _, _ in pins} == {"django", "asgiref", "sqlparse", "psycopg", "typing-extensions"}
    assert dict((name, version) for name, version, _ in pins)["django"].startswith("5.2.")
    named = [line for line in requirements.splitlines() if line and not line.startswith(("#", " "))]
    assert len(named) == len(pins), "every requirement pinned with its hash"
    lock = json.loads((im.RECIPE_FILES / "django" / "package-lock.json").read_text())
    manifest = json.loads((im.RECIPE_FILES / "django" / "package.json").read_text())
    assert manifest["private"] is True and manifest["dependencies"] == {"typescript": "6.0.3"}
    entries = {path: entry for path, entry in lock["packages"].items() if path}
    assert list(entries) == ["node_modules/typescript"] and entries["node_modules/typescript"]["version"] == "6.0.3"
    assert re.fullmatch(r"sha512-[A-Za-z0-9+/]{86}==", entries["node_modules/typescript"]["integrity"])
    assert not entries["node_modules/typescript"].get("hasInstallScript")
    assert "django" in im.RECIPES and "Django" in im.RECIPES["django"]["description"]


def test_the_packaging_ships_the_recipes_and_service_images():
    """The recipes' files (lockfiles, android-dev's scripts and package.xml files) and sandbox-ai's own Containerfiles
    go into the wheel and the sdist: every file below sandbox/recipes, also in a folder of a recipe's own."""
    tomllib = pytest.importorskip("tomllib")
    patterns = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]["sandbox_ai"]
    assert {"recipes/*/*.json", "recipes/*/*.txt", "service-images/*/*"} <= set(patterns)
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "graft sandbox/recipes" in manifest and "graft sandbox/service-images" in manifest
    shipped = {path.relative_to(ROOT / "sandbox").as_posix() for path in (ROOT / "sandbox").glob("recipes/**/*")
               if path.is_file()}
    assert "recipes/android-dev/package-xml/emulator.xml" in shipped and "recipes/android-dev/sdk.lock" in shipped
    shipped |= {path.relative_to(ROOT / "sandbox").as_posix() for path in (ROOT / "sandbox").glob("service-images/*/*")}
    for path in shipped:
        assert any(fnmatch.fnmatch(path, pattern) for pattern in patterns), path


# -- names during the build: Podman's own network, else the host's, else a clear stop --


def dns_engine(tmp_path, monkeypatch, resolves):
    """podman whose `run ... getent hosts` succeeds only on the networks in RESOLVES ("default", "host")."""
    log = tmp_path / "podman.log"
    script = tmp_path / "bin" / "podman"
    script.parent.mkdir(exist_ok=True)
    script.write_text(f"""#!{sys.executable}
import json, sys
args = sys.argv[1:]
with open({str(log)!r}, "a") as handle:
    handle.write(json.dumps(args) + "\\n")
while args and args[0].startswith("--cgroup-manager"):
    args = args[1:]
if args[:1] == ["run"]:
    network = "host" if "--network=host" in args else "default"
    sys.exit(0 if network in {resolves!r} else 1)
if args[:2] == ["image", "inspect"]:
    print(json.dumps([{{"Id": "base-id", "Config": {{"Labels": {{}}}}}}]))
""")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_PYTHON", sys.executable)
    monkeypatch.delenv("SANDBOX_AI_BUILD_NO_DNS_CHECK", raising=False)

    def builds():
        found = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return [[a for a in call if not a.startswith("--cgroup-manager")] for call in found]
    return builds


@pytest.mark.parametrize("target", ["claude", "vpn", "tor", "team"])
def test_a_build_whose_network_resolves_nothing_uses_the_hosts(tmp_path, monkeypatch, target):
    """The user's `sandbox-ai build all` and `build vpn` failed at every RUN step: "Temporary failure resolving
    deb.debian.org" (Podman's build network reached no resolver). Checked first; only the host's network resolves:
    the build uses it and says so."""
    builds = dns_engine(tmp_path, monkeypatch, {"host"})
    result = builder(target, "--build-arg", "CLAUDE_VERSION=1.2.3", "--build-arg", "CODEX_VERSION=1.2.3",
                     "--build-arg", "OPENCODE_VERSION=1.2.3")
    assert result.returncode == 0, result.stderr
    assert "resolves no names here" in result.stderr and "--network=host" in result.stderr
    built = [c for c in builds() if c[:1] == ["build"]]
    assert built and all("--network=host" in c for c in built)


def test_a_build_whose_network_resolves_builds_as_before(tmp_path, monkeypatch):
    builds = dns_engine(tmp_path, monkeypatch, {"default", "host"})
    result = builder("claude", "--build-arg", "CLAUDE_VERSION=1.2.3")
    assert result.returncode == 0, result.stderr
    built = [c for c in builds() if c[:1] == ["build"]]
    assert built and not any("--network=host" in c for c in built) and "resolves no names" not in result.stderr


def test_no_name_resolves_anywhere_stops_before_building(tmp_path, monkeypatch):
    builds = dns_engine(tmp_path, monkeypatch, set())
    result = builder("claude", "--build-arg", "CLAUDE_VERSION=1.2.3")
    assert result.returncode == 1 and "not even on the host's network" in result.stderr and "--dns IP" in result.stderr
    assert not [c for c in builds() if c[:1] == ["build"]], "nothing built"


def test_a_network_given_to_the_build_is_left_as_given(tmp_path, monkeypatch):
    builds = dns_engine(tmp_path, monkeypatch, set())
    result = builder("claude", "--build-arg", "CLAUDE_VERSION=1.2.3", "--dns", "192.0.2.53")
    assert result.returncode == 0, result.stderr
    assert not [c for c in builds() if c[:1] == ["run"]], "no check"


def test_a_missing_variant_of_a_recipes_name_points_to_the_recipe(tmp_path, monkeypatch):
    """`images new media` makes an empty variant without the media tools: for a recipe's name the refusal says to make
    it from the recipe and build it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ValueError, match=r"images recipe media --from opencode && sandbox-ai build media"):
        im.variant_image("opencode", "media")
    with pytest.raises(ValueError, match=r"images new other --from opencode"):
        im.variant_image("opencode", "other")


def test_a_recipes_variant_for_each_agent_that_needs_it(config, fake_engine, capsys):
    """User report from live use (2026-11-14): a dsh start wanted the variant tor while tor was claude's - a variant
    name belonged to one agent. Now each agent has its own variants (images/variants/AGENT/NAME): a recipe's variant
    (media) for claude and for dsh, each its own image (localhost/AGENT-sandbox-media), and where a name is a variant
    of several agents its commands take it as NAME@AGENT."""
    assert im.main(["recipe", "media", "--from", "claude"], out=[].append) == 0
    lines = []
    assert im.main(["recipe", "media", "--from", "dsh"], out=lines.append) == 0, "no clash with claude's tor"
    assert im.variants() == [("media", "claude"), ("media", "dsh")]
    assert im.variant_image("dsh", "media") == "localhost/dsh-sandbox-media:latest"
    assert im.variant_image("claude", "media") == "localhost/claude-sandbox-media:latest"
    with pytest.raises(ValueError, match="media is a variant of claude and dsh: name one of them, e.g. media@claude"):
        im.agent_of("media")
    assert im.agent_of("media@dsh") == "dsh" and im.describe("media@dsh")["target"] == "media@dsh"
    assert im.build_command(im.shown_target("media", "dsh")) == "sandbox-ai build media@dsh"
    targets = dict(im.targets())
    assert targets["media@claude"] == "claude" and targets["media@dsh"] == "dsh"
    assert im.main(["extend", "media@dsh", "--add-apt", "jq"]) == 0
    assert "jq" in im.describe("media@dsh")["apt"] and "jq" not in im.describe("media@claude")["apt"]
    assert im.build_plan("media@dsh") == ("dsh", "variant", 0)
    assert [e["agent"] for e in im.history("media@dsh") if e["action"] == "new"] == ["dsh"]
    # The build of one: its own image, the other untouched.
    result = builder("--variant", "media@dsh")
    assert result.returncode == 0, result.stderr
    tags = [c[c.index("--tag") + 1] for c in fake_engine() if c[0] == "build"]
    assert tags[-1] == "localhost/dsh-sandbox-media:latest" and "localhost/claude-sandbox-media:latest" not in tags
    # Removed: the other stays, and is a plain name again.
    assert im.main(["remove", "media@claude", "--keep-image"]) == 0
    assert im.variants() == [("media", "dsh")] and im.agent_of("media") == "dsh"
    with pytest.raises(ValueError, match="exists already"):
        im.new_variant("media", "dsh")


def test_variants_of_before_move_to_their_agents_folder(config):
    """A variant of before (images/NAME, one agent per name) is moved to images/variants/AGENT/NAME the first time the
    variants are read; its extension and history stay."""
    old = im.config_root() / "web"
    old.mkdir(parents=True)
    (old / "image.json").write_text(json.dumps({**im.empty_document("claude"), "apt": ["jq"]}))
    (old / im.FRAGMENT).write_text("RUN true\n")
    assert im.variants() == [("web", "claude")]
    moved = im.config_root() / "variants" / "claude" / "web"
    assert not old.exists() and (moved / "image.json").exists() and (moved / im.FRAGMENT).read_text() == "RUN true\n"
    assert im.describe("web")["apt"] == ["jq"] and im.describe("web")["path"] == str(moved)
    # An agent's own extension stays where it is.
    assert im.main(["extend", "claude", "--add-apt", "ripgrep"]) == 0
    assert (im.config_root() / "claude" / "image.json").exists() and im.variants() == [("web", "claude")]


def test_a_start_names_the_build_of_its_own_agents_variant(config):
    """image_fix (what a start says, and builds, when the variant's image is missing): the agent's own variant -
    NAME@AGENT where another agent has one of that name."""
    spec = importlib.util.spec_from_file_location("launcher_for_variants", ROOT / "sandbox/agent_sandbox.py")
    s = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(s)
    assert im.main(["recipe", "media", "--from", "dsh"], out=[].append) == 0
    a = s.parse_args(["dsh", "--image-variant", "media"])
    assert s.image_fix(a, "dsh", "localhost/dsh-sandbox-media:latest") == "sandbox-ai build --variant media"
    assert im.main(["recipe", "media", "--from", "claude"], out=[].append) == 0
    assert s.image_fix(a, "dsh", "localhost/dsh-sandbox-media:latest") == "sandbox-ai build --variant media@dsh"
    assert s.variant_base(a) is None  # (on the agent's own base)


def test_the_android_recipe(config):
    """`images recipe android` (the preset android-debugging): the device tools of Debian's release, and every
    analysis tool as a release file pinned by its SHA-256 - apktool 3.0.3 with its own aapt2 and framework out of its
    own jar, baksmali and smali 3.0.10, jadx 1.5.6, Frida 17.25.2's bindings from a hash-locked requirements.txt and
    frida-server plus frida-gadget for every Android ABI; nothing is fetched or unpacked when the agent runs, and the
    validator accepts it."""
    lines = []
    assert im.main(["recipe", "android", "--from", "claude"], out=lines.append) == 0
    assert "sandbox-ai build android" in lines[-1] and "--image-variant android" in lines[-1]
    document, fragment, files = im.load("android")
    assert im.errors(im.check(document, fragment, files)) == []
    for package in ("adb", "fastboot", "aapt", "zipalign", "libapksig-java", "default-jre-headless", "unzip",
                    "xz-utils", "python3-venv"):
        assert package in document["apt"], package
    assert all("=" not in package for package in document["apt"])  # Debian's own versions of the release
    assert not document["npm"] and not document["pip"]
    assert {e["target"] for e in document["files"]} == {"/opt/frida/requirements.txt"}
    assert files["requirements.txt"] == (im.RECIPE_FILES / "android" / "requirements.txt").read_bytes()

    # Every release file: an ADD with the SHA-256 of that very file, and no other way in.
    pinned = re.findall(r"^ADD --checksum=sha256:([0-9a-f]{64}) \\\n\s+(\S+) (\S+)$", fragment, re.M)
    assert len(pinned) == 12, pinned
    by_url = {url: (digest, target) for digest, url, target in pinned}
    assert by_url["https://github.com/iBotPeaches/Apktool/releases/download/v3.0.3/apktool_3.0.3.jar"] == (
        "dbf930b076c6b9be08d57c449cacefc3bdd6b71ebd59b3066fc0e1f5b14f9423", "/opt/android/apktool.jar")
    assert by_url["https://github.com/baksmali/smali/releases/download/3.0.10/baksmali-3.0.10-fat-release.jar"][0] == \
        "37ae4a41a8886e15c20b8362fa4250f96bbdb55e1a608199ad8b5dff068b588f"
    assert by_url["https://github.com/baksmali/smali/releases/download/3.0.10/smali-3.0.10-fat-release.jar"][0] == \
        "32fa0e88a6c397b3922201adf5f3e534fbaed5a663c71d0c558c3ddce0af844a"
    assert by_url["https://github.com/skylot/jadx/releases/download/v1.5.6/jadx-1.5.6.zip"][0] == \
        "545ea2be9c242511bc145755cf4bda2485ade42966e096f8b4d3da2a230e8974"
    # frida-server and frida-gadget for all four Android ABIs, one version, as published (xz)
    frida = {url.rsplit("/", 1)[1]: target for url, (_, target) in by_url.items() if "frida" in url}
    for abi in ("arm", "arm64", "x86", "x86_64"):
        server = f"frida-server-17.25.2-android-{abi}.xz"
        gadget = f"frida-gadget-17.25.2-android-{abi}.so.xz"
        assert frida[server] == f"/opt/frida/android/{abi}/{server}", abi
        assert frida[gadget] == f"/opt/frida/android/{abi}/{gadget}", abi
    assert len(frida) == 8
    assert all("/releases/download/17.25.2/" in url for url in by_url if "frida/frida" in url)

    # The bindings: one version, hash-locked, the same one the binaries are; pip installs nothing else.
    requirements = (im.RECIPE_FILES / "android" / "requirements.txt").read_text()
    pins = re.findall(r"^([a-z0-9-]+)==(\S+) \\$", requirements, re.M)
    assert pins == [("frida", "17.25.2")], pins
    assert len(re.findall(r"^    --hash=sha256:[0-9a-f]{64}", requirements, re.M)) == 2
    assert "frida-tools" in requirements and "is NOT here" in requirements, "why frida-tools is left out"
    assert "pip install --no-cache-dir --require-hashes --no-deps --only-binary :all:" in fragment
    assert "17.25.2" in im.RECIPES["android"]["description"]

    # Nothing is unpacked, made executable or fetched while the agent runs: apktool's aapt2 and framework are taken
    # out of its jar here and checked, and every tool must run once in the build.
    for checked in ("df312db814c018019b2b79a993b041b738d7b76f4a8f28da25bc874a026368cc  aapt2",
                    "5dd984016ed5a5eb0eef866e2c6e8cd352e1427828ec23adb18005cf5648f3d7  framework/1.apk",
                    "fe3e12c45acf75f92369685fd02d1d7a7323385dc725680a9b98a0dac0ea554b  jadx/lib/jadx-1.5.6-all.jar"):
        assert checked in fragment, checked
    assert fragment.count("sha256sum --check --strict") == 3
    assert "--frame-path /opt/android/framework --aapt /opt/android/aapt2" in fragment
    assert "chmod -R a-x,a+r /opt/frida/android" in fragment
    for ran in ("apktool --version", "baksmali --version", "smali --version", "jadx --version",
                "apksigner --version", "import frida; print('frida', frida.__version__)"):
        assert ran in fragment, ran
    text = im.containerfile("android", document, fragment)
    assert "curl" not in text and "wget" not in text and "pipe-shell" not in text
    assert "COPY --chmod=0644 files/requirements.txt /opt/frida/requirements.txt" in text
    assert "android" in im.RECIPES and "Android" in im.RECIPES["android"]["description"]
    with pytest.raises(ValueError, match="exists already"):
        im.recipe("android", "claude")


def test_the_android_recipe_warns_about_nothing_the_other_recipes_do_not(config):
    """The android fragment is as clean as the others: no warning the validator would raise for a sandbox (no USER,
    WORKDIR, SHELL, ENV of a watched variable, no ADD of a local archive, no script from the network)."""
    im.recipe("android", "codex")
    document, fragment, files = im.load("android@codex")
    findings = im.check(document, fragment, files)
    assert [f for f in findings if f["severity"] == "error"] == []
    assert {f["id"] for f in findings} <= {"add-url"}, findings


ANDROID_DEV = im.RECIPE_FILES / "android-dev"


def sdk_lock_rows():
    """[(package, version, dest, size, sha1, sha256, url)] of sandbox/recipes/android-dev/sdk.lock."""
    rows = []
    for line in (ANDROID_DEV / "sdk.lock").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            words = line.split()
            assert len(words) == 7, line
            rows.append(tuple(words))
    return rows


def test_the_android_dev_recipe_has_everything_of_the_android_recipe(config):
    """User 2026-11-21: ONE combined image - android-dev builds on the variant android. Its recipe includes android's
    (packages, files and fragment first, unchanged), so the variant has adb, apktool, smali, jadx and Frida too, and
    the validator takes it as it takes android's."""
    lines = []
    assert im.main(["recipe", "android-dev", "--from", "claude"], out=lines.append) == 0
    assert "sandbox-ai build android-dev" in lines[-1] and "--image-variant android-dev" in lines[-1]
    document, fragment, files = im.load("android-dev")
    findings = im.check(document, fragment, files)
    assert findings == [], findings
    android = im.RECIPES["android"]
    assert im.RECIPES["android-dev"]["includes"] == ["android"]
    assert document["apt"][:len(android["apt"])] == android["apt"], "android's packages first"
    for package in ("openjdk-17-jdk-headless", "curl", "unzip", "libpulse0", "libnss3", "libxcomposite1"):
        assert package in document["apt"], package
    assert len(document["apt"]) == len(set(document["apt"])), "each package once"
    assert all("=" not in package for package in document["apt"])  # Debian's own versions of the release
    assert not document["npm"] and not document["pip"]
    targets = {entry["target"] for entry in document["files"]}
    assert "/opt/frida/requirements.txt" in targets  # android's
    assert {"/opt/android-dev/sdk.lock", "/opt/android-dev/install.sh", "/opt/android-dev/android-sdk-license",
            "/usr/local/bin/android-emulator"} <= targets
    modes = {entry["target"]: entry["mode"] for entry in document["files"]}
    assert modes["/usr/local/bin/android-emulator"] == "0755" and modes["/opt/android-dev/install.sh"] == "0644"
    assert files["sdk.lock"] == (ANDROID_DEV / "sdk.lock").read_bytes()
    assert files["install.sh"] == (ANDROID_DEV / "install.sh").read_bytes()
    # android's fragment comes first, as it is; android-dev's after it
    assert fragment.startswith(im.ANDROID_FRAGMENT) and fragment.endswith(im.ANDROID_DEV_FRAGMENT)
    assert fragment.index("apktool --version") < fragment.index("install.sh")
    text = im.containerfile("android-dev", document, fragment)
    assert not re.search(r"\|\s*(?:sh|bash|python3?)\b", text), "no script from the network"
    # The android variant itself is unchanged by it.
    assert "install.sh" not in im.recipe_parts("android")["fragment"]
    assert im.recipe_parts("android")["apt"] == android["apt"]
    with pytest.raises(ValueError, match="no recipe"):
        im.recipe_parts("no-such-recipe")


def test_the_android_dev_recipe_pins_every_download():
    """Every download of android-dev is a row of sdk.lock: HTTPS, its size, Google's SHA-1 for an SDK archive (Google
    publishes no SHA-256; the first real build prints it, to be added) or the publisher's SHA-256 (Gradle, Kotlin),
    one folder below /opt each; the API levels 37, 36 and 35 with platform and build-tools, the NDK, CMake, the
    emulator and one x86_64 system image of API 37. The package.xml of each SDK package is beside it."""
    rows = sdk_lock_rows()
    by_package = {row[0]: row for row in rows if row[0] != "-"}
    for package, version, dest, size, sha1, sha256, url in rows:
        assert url.startswith("https://"), url
        assert dest.startswith("/opt/") and ".." not in dest, dest
        assert size.isdigit() and int(size) > 1 << 20, (url, size)
        assert re.fullmatch(r"[0-9a-f]{40}|-", sha1) and re.fullmatch(r"[0-9a-f]{64}|-", sha256), url
        assert sha1 != "-" or sha256 != "-", f"{url}: no checksum"
        if url.startswith("https://dl.google.com/android/repository/"):
            assert sha1 != "-", f"{url}: Google's SHA-1"
            assert dest == "/opt/android-sdk/" + package.replace(";", "/") or package in ("platform-tools", "emulator")
        else:
            assert url.startswith("https://github.com/") and sha256 != "-", f"{url}: the publisher's SHA-256"
    assert len({row[2] for row in rows}) == len(rows), "one folder each"
    # The API levels: 37 (the current stable one) and the two before it, platform and build-tools each
    for platform, tools in (("platforms;android-37.0", "build-tools;37.0.0"), ("platforms;android-36", "build-tools;36.0.0"),
                            ("platforms;android-35", "build-tools;35.0.0")):
        assert platform in by_package and tools in by_package, (platform, tools)
    assert {"cmdline-tools;23.0", "platform-tools", "ndk;30.0.16248370", "cmake;4.1.2", "cmake;3.22.1", "emulator",
            "system-images;android-37.0;google_apis;x86_64"} <= set(by_package)
    assert by_package["emulator"][1] == "37.2.13" and by_package["platform-tools"][1] == "37.0.1"
    # the NDK's SHA-1 and size as Google gives them on github.com/android/ndk/releases/tag/r30 as well
    assert by_package["ndk;30.0.16248370"][3:5] == ("738633529", "5107f898313790e449e87eee2183d9a20602dee9")
    others = {row[6]: row for row in rows if row[0] == "-"}
    gradle = others["https://github.com/gradle/gradle-distributions/releases/download/v9.8.2/gradle-9.8.2-bin.zip"]
    assert gradle[1:6] == ("9.8.2", "/opt/gradle", "151620138", "-",
                           "21bd5da2de49b642904077f38c0efdc7778e92b3ed0889392dca61b97e389607")
    kotlin = others["https://github.com/JetBrains/kotlin/releases/download/v2.4.21/kotlin-compiler-2.4.21.zip"]
    assert kotlin[1:6] == ("2.4.21", "/opt/kotlinc", "89733618", "-",
                           "7cc140e76daf416a0424a5557f99afd7ca89ebd463ea1101261e3e01c33e75cd")
    assert len(rows) == 15
    # The package.xml sdkmanager would write, one per SDK package and no other
    import xml.etree.ElementTree as ET
    xmls = {path.name: path for path in (ANDROID_DEV / "package-xml").glob("*.xml")}
    assert set(xmls) == {package.replace(";", "_") + ".xml" for package in by_package}
    for package in by_package:
        root = ET.parse(xmls[package.replace(";", "_") + ".xml"]).getroot()
        local = root.find("localPackage")
        assert local is not None and local.get("path") == package and local.get("obsolete") == "false", package
        assert local.find("uses-license").get("ref") == "android-sdk-license"
        assert root.find("license").get("id") == "android-sdk-license"
        assert "Android Software Development Kit License Agreement" in root.find("license").text
    image = ET.parse(xmls["system-images_android-37.0_google_apis_x86_64.xml"]).getroot().find("localPackage")
    details = image.find("type-details")
    assert [child.tag for child in details] == ["api-level", "extension-level", "base-extension", "tag", "vendor",
                                                "abi"], "each element once"
    assert details.find("abi").text == "x86_64" and details.find("tag/id").text == "google_apis"
    assert ET.parse(xmls["platforms_android-37.0.xml"]).getroot().find("localPackage/type-details/api-level").text \
        == "37.0"
    # The license acceptance sdkmanager writes for that license (the SHA-1 of its text), and only that one
    assert (ANDROID_DEV / "android-sdk-license").read_text() == "\n24333f8a63b6825ea9c5514f83c2829b004d1fee"
    # Every file the recipe copies in is there, and every file there is copied in
    copied = {source for source, _, _ in im.RECIPES["android-dev"]["files"]}
    present = {str(path.relative_to(im.RECIPE_FILES)) for path in ANDROID_DEV.rglob("*") if path.is_file()}
    assert copied == present, copied ^ present


def test_the_android_dev_lock_is_what_its_script_makes():
    """sdk.lock and package-xml/ come from scripts/android_dev_lock.py (Google's repository as nixpkgs keeps it):
    regenerated from the rows themselves the script keeps every SHA-256 a build confirmed and writes the same
    header; a package of a preview channel (the preview license) is refused."""
    spec = importlib.util.spec_from_file_location("android_dev_lock", ROOT / "scripts" / "android_dev_lock.py")
    lock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lock)
    text = (ANDROID_DEV / "sdk.lock").read_text()
    assert "as nixpkgs keeps it (nixpkgs@" in text
    assert lock.confirmed(text) == {row[6]: row[5] for row in sdk_lock_rows() if row[5] != "-"}
    confirmed = text.replace("-" + " " * 64 + " https://dl.google.com/android/repository/platform-tools_r37.0.1",
                             "a" * 64 + " https://dl.google.com/android/repository/platform-tools_r37.0.1")
    assert lock.confirmed(confirmed)["https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip"] \
        == "a" * 64
    assert [name for _, name, _ in lock.PACKAGES][-1] == "37.0/google_apis/x86_64"
    # Every pin was at least 14 days old (sandbox-ai's rule) when it was made, which its comment line says.
    for _, _, by in lock.PACKAGES:
        assert by <= "2026-11-07", by
    for row in sdk_lock_rows():
        assert re.search(r"# .*(?:in Google's repository by|released) 2026-\d\d-\d\d.*\n" + re.escape(row[0] + " "),
                         text), row
    entry = {"license": "android-sdk-preview-license", "path": "emulator", "archives": []}
    lock.PACKAGES = [("packages/emulator", "37.3.6", "2026-11-01")]
    with pytest.raises(SystemExit, match="preview"):
        lock.generate({"packages": {"emulator": {"37.3.6": entry}}, "licenses": {}}, "test")


def test_the_android_dev_fragment_installs_checks_and_says_the_license():
    """The fragment: install.sh with the lock, the one license acceptance, the tools on PATH through /usr/local/bin
    (PATH itself stays the image's), and every tool run once while the image is built - sdkmanager must see every
    package of the lock and avdmanager must make an AVD of the system image, else the build stops. The build says
    that building it accepts Google's SDK license; no ENV of a watched variable."""
    fragment = im.ANDROID_DEV_FRAGMENT
    assert "RUN sh /opt/android-dev/install.sh /opt/android-dev/sdk.lock /opt/android-dev/package-xml" in fragment
    assert "cp /opt/android-dev/android-sdk-license /opt/android-sdk/licenses/android-sdk-license" in fragment
    assert "By building this image you accept the Android Software" in fragment
    assert "you accepted the Android Software Development Kit License" in fragment
    assert "ENV PATH" not in fragment and "LD_LIBRARY_PATH=" not in fragment.split("RUN", 1)[0]
    for name in ("ANDROID_HOME=/opt/android-sdk", "ANDROID_NDK_HOME=/opt/android-sdk/ndk/30.0.16248370",
                 "JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64"):
        assert name in fragment, name
    for tool in ("adb", "fastboot", "emulator", "cmake", "ninja", "sdkmanager", "avdmanager", "ndk-build", "gradle",
                 "kotlinc", "kotlin"):
        assert f"/usr/local/bin/{tool}" in fragment, tool
    for ran in ("sdkmanager --list_installed", "avdmanager --silent create avd", "adb version", "emulator -version",
                "gradle --version", "kotlinc -version", "cmake --version", "ndk-build --version",
                "java -version 2>&1 | grep -q 'version \"17\\.'"):
        assert ran in fragment, ran
    assert "does not see $package" in fragment and "exit 1" in fragment
    assert im.check_fragment(fragment) == []
    for text in (im.RECIPES["android-dev"]["description"], fragment):
        assert "https://developer.android.com/studio/terms" in text
    assert "x86_64 only" in im.RECIPES["android-dev"]["description"]
    assert "4.1 GB" in im.RECIPES["android-dev"]["description"]


def fake_sdk_tools(tmp_path, downloads):
    """A bin folder with stand-ins: curl copies the file named by the URL's last part from DOWNLOADS, unzip unpacks
    with Python's zipfile (modes kept) where Info-ZIP is missing."""
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "curl").write_text("#!/bin/sh\nout=''\nwhile [ $# -gt 1 ]; do case \"$1\" in --output) out=$2; shift;; "
                               "esac; shift; done\nexec cp \"" + str(downloads) + "/${1##*/}\" \"$out\"\n")
    (shim / "unzip").write_text(
        f"#!{sys.executable}\nimport os, sys, zipfile\nargs = [a for a in sys.argv[1:] if a != '-q']\n"
        "target = args[args.index('-d') + 1]\nwith zipfile.ZipFile(args[0]) as z:\n"
        "    for info in z.infolist():\n        path = z.extract(info, target)\n"
        "        mode = info.external_attr >> 16\n        if mode & 0o777 and not info.is_dir():\n"
        "            os.chmod(path, mode & 0o777)\n")
    for tool in ("curl", "unzip"):
        (shim / tool).chmod(0o755)
    return shim


def fake_archive(path, entries):
    import zipfile
    with zipfile.ZipFile(path, "w") as archive:
        for name, (data, mode) in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100000 | mode) << 16
            archive.writestr(info, data)
    data = path.read_bytes()
    import hashlib
    return len(data), hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest()


def run_install(tmp_path, rows, shim):
    lock = tmp_path / "sdk.lock"
    lock.write_text("# a lock for the test\n" + "".join(" ".join(str(word) for word in row) + "\n" for row in rows))
    env = {**os.environ, "PATH": f"{shim}:{os.environ['PATH']}", "ANDROID_DEV_PREFIX": str(tmp_path / "opt"),
           "ANDROID_DEV_WORK": str(tmp_path / "work")}
    return subprocess.run(["sh", str(ANDROID_DEV / "install.sh"), str(lock), str(ANDROID_DEV / "package-xml")],
                          env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(shutil.which("sha1sum") is None or shutil.which("sha256sum") is None,
                    reason="needs coreutils' sha1sum and sha256sum")
def test_install_sh_checks_every_download_before_it_unpacks_it(tmp_path):
    """install.sh, run as the image build runs it (with stand-ins for curl and unzip): a download whose size, SHA-1
    or SHA-256 differs from the lock stops the build loudly and nothing of it is unpacked; a good one is unpacked to
    its folder (an archive's one folder becomes the package, its modes kept) with the package.xml of the SDK
    package; a Google archive without a SHA-256 in the lock gets it printed for the lock; the license is said."""
    if os.uname().machine not in ("x86_64", "amd64"):
        pytest.skip("install.sh refuses other machines (the SDK's Linux tools are x86_64 only)")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    shim = fake_sdk_tools(tmp_path, downloads)
    size, sha1, sha256 = fake_archive(downloads / "platform-tools_r37.0.1-linux.zip",
                                      {"platform-tools/adb": (b"#!/bin/sh\necho adb\n", 0o755),
                                       "platform-tools/source.properties": (b"Pkg.Revision=37.0.1\n", 0o644)})
    tsize, tsha1, tsha256 = fake_archive(downloads / "gradle-9.8.2-bin.zip", {"gradle-9.8.2/bin/gradle": (b"x", 0o755)})
    opt = tmp_path / "opt"
    good = [("platform-tools", "37.0.1", opt / "android-sdk/platform-tools", size, sha1, "-",
             "https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip"),
            ("-", "9.8.2", opt / "gradle", tsize, "-", tsha256,
             "https://github.com/gradle/gradle-distributions/releases/download/v9.8.2/gradle-9.8.2-bin.zip")]
    result = run_install(tmp_path, good, shim)
    assert result.returncode == 0, result.stderr
    assert "you accept the Android Software Development Kit License Agreement" in result.stdout
    assert f"unconfirmed SHA-256 {sha256} of https://dl.google.com/android/repository/platform-tools_r37.0.1" \
        in result.stdout
    assert "2 downloads checked and installed" in result.stdout
    adb = opt / "android-sdk/platform-tools/adb"
    assert adb.read_text() == "#!/bin/sh\necho adb\n" and os.access(adb, os.X_OK)
    assert (opt / "android-sdk/platform-tools/package.xml").read_bytes() == \
        (ANDROID_DEV / "package-xml/platform-tools.xml").read_bytes()
    assert (opt / "gradle/bin/gradle").exists() and not (opt / "gradle/package.xml").exists()
    assert not (tmp_path / "work").exists(), "no archive left behind"

    # Each kind of difference: the build stops with the reason, the package is not there.
    for name, row in (("size", (size + 1, sha1, "-")), ("SHA-1", (size, "0" * 40, "-")),
                      ("SHA-256", (size, sha1, "0" * 64)), ("checksum", (size, "-", "-"))):
        shutil.rmtree(opt, ignore_errors=True)
        result = run_install(tmp_path, [("platform-tools", "37.0.1", opt / "android-sdk/platform-tools", *row,
                                         good[0][6])], shim)
        assert result.returncode == 1, name
        assert "ERROR" in result.stderr and (name.upper() in result.stderr or name in result.stderr), result.stderr
        assert not (opt / "android-sdk/platform-tools").exists(), name
    # Only HTTPS, only below the prefix (/opt in the build)
    for bad_url, bad_dest, said in (("http://dl.google.com/x.zip", opt / "x", "only https://"),
                                    (good[0][6], tmp_path / "elsewhere", "a destination below")):
        result = run_install(tmp_path, [("platform-tools", "37.0.1", bad_dest, size, sha1, "-", bad_url)], shim)
        assert result.returncode == 1 and said in result.stderr, result.stderr


def test_the_android_emulator_helper():
    """android-emulator (in the variant's /usr/local/bin): the AVD of the image's own system image, headless unless
    --gui, its own adb server on port 5038 (5037 is the host's), no usage metrics, and a clear word when the sandbox
    has no /dev/kvm."""
    text = (ANDROID_DEV / "android-emulator").read_text()
    assert text.startswith("#!/bin/sh\n")
    assert 'image="system-images;android-37.0;google_apis;x86_64"' in text
    assert "ANDROID_EMULATOR_ADB_PORT:-5038" in text and "-no-window" in text and "-no-metrics" in text
    assert "--device /dev/kvm" in text and "sys.boot_completed" in text
    assert subprocess.run(["sh", "-n", str(ANDROID_DEV / "android-emulator")]).returncode == 0
    assert subprocess.run(["sh", "-n", str(ANDROID_DEV / "install.sh")]).returncode == 0
    usage = subprocess.run(["sh", str(ANDROID_DEV / "android-emulator")], capture_output=True, text=True)
    assert usage.returncode == 2 and "usage: android-emulator start|stop|status" in usage.stderr
