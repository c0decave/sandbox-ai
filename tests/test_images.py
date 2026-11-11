"""sandbox-ai images, with podman replaced by recorded answers: listing, packages, extensions (validator,
changes, variants, history), the extension layer of sandbox-ai build and --image-variant."""
import importlib.util
import io
import json
import os
from pathlib import Path
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
    assert im.main(["new", "gpu", "--from", "codex"]) == 1, "exists already"
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
    assert status == {"claude": "outdated", "codex": "not-built", "opencode": "not-built", "web": "not-built",
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
    directory = config / "config" / "sandbox-ai" / "images" / "web"
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
    for argv, message in ((["codex", "--image-variant", "web"], "variant of claude"),
                          (["claude", "--image-variant", "nope"], "no such variant"),
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


def test_the_tor_recipe(config):
    """`images recipe tor`: torsocks from main, torbrowser-launcher from Debian's contrib section (the fragment adds
    it), the agent's own fragment kept before it; the validator accepts the result."""
    lines = []
    assert im.main(["recipe", "tor", "--from", "claude"], out=lines.append) == 0
    assert "sandbox-ai build --variant tor" in lines[-1] and "--image-variant tor" in lines[-1]
    document = json.loads((im.config_root() / "tor" / "image.json").read_text())
    assert document["apt"] == ["torsocks"] and "tor" not in document["apt"]  # tor itself runs in the VPN container
    fragment = (im.config_root() / "tor" / im.FRAGMENT).read_text()
    assert "Components: contrib" in fragment and "torbrowser-launcher" in fragment
    assert "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg" in fragment
    assert im.errors(im.check(document, fragment, {})) == []
    text = im.containerfile("tor", document, fragment)
    assert text.index("torsocks") < text.index("torbrowser-launcher")
    # The agent's own fragment stays, the recipe's follows it.
    assert im.main(["extend", "codex", "--fragment", "-"], out=lines.append, stdin=io.StringIO("RUN true\n")) == 0
    assert im.main(["recipe", "tor", "--from", "codex", "--as", "tor-codex"], out=lines.append) == 0
    codex = (im.config_root() / "tor-codex" / im.FRAGMENT).read_text()
    assert codex.index("RUN true") < codex.index("torbrowser-launcher")



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
    """`images new tor` makes an empty variant without the Tor tools: for a recipe's name the refusal says to make it
    from the recipe and build it."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    with pytest.raises(ValueError, match=r"images recipe tor --from opencode && sandbox-ai build --variant tor"):
        im.variant_image("opencode", "tor")
    with pytest.raises(ValueError, match=r"images new other --from opencode"):
        im.variant_image("opencode", "other")
