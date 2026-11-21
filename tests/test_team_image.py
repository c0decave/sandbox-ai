"""The team image with the other agents as layers (sandbox/team_image.py, `sandbox-ai build team --with A,B`): each
layer is made from the agent's own Containerfile - the same reviewed release, lock file, checks and telemetry guard -
never a copy that could drift; images share the base and the layers they begin with. Images cannot be built here
(no Podman): the Containerfile's content is checked as text, the build through ./sandbox-ai with the fake Podman."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import live_fakes as F  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"
spec = importlib.util.spec_from_file_location("team_image_for_tests", SANDBOX / "team_image.py")
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)
gspec = importlib.util.spec_from_file_location("telemetry_guard_for_team_image", SANDBOX / "telemetry_guard.py")
guard = importlib.util.module_from_spec(gspec)
gspec.loader.exec_module(guard)

NINE = ("hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "opencode2", "pi")


def own(agent):
    return (SANDBOX / f"Containerfile.{agent}").read_text()


def instructions(text):
    """The Containerfile's instructions (comment lines out, continuation lines joined)."""
    joined, current = [], ""
    for line in text.splitlines():
        if line.startswith("#") and not current:
            continue
        current += line.rstrip("\\") + " " if line.endswith("\\") else line
        if not line.endswith("\\"):
            joined.append(" ".join(current.split()))
            current = ""
    return joined


def test_every_other_agent_is_a_layer_and_the_base_is_the_trio():
    assert set(T.LAYERS) == set(NINE) and list(T.LAYERS) == sorted(T.LAYERS)
    assert T.BASE_AGENTS == ("claude", "codex", "opencode")


@pytest.mark.parametrize("agent", NINE)
def test_a_layer_is_its_own_containerfile_after_the_common_base(agent):
    """Its reviewed release (every ARG of its stage), its lock file, its install and checks, its telemetry guard -
    the same instructions as its own image, in the same order; the common base is the team's."""
    layer = T.layer(agent)
    stage = "".join(T.stage(own(agent)))
    for arg in re.findall(r"^ARG \S+$", stage, re.M):
        assert arg in layer.splitlines(), arg
    for lock in re.findall(r"^COPY (Containerfile\.\S+\.lock) ", own(agent), re.M):
        assert f"COPY {lock} " in layer, lock
    if agent in guard.NAMES:  # (Hermes': hermes_guard.py, mounted into its site-packages by the launcher)
        assert f"RUN python3 -IS /usr/local/lib/sandbox-ai/telemetry_guard.py --install {agent}" in layer
    mine = [line for line in instructions(layer) if not line.startswith(("ARG ", "RUN apt-get update && apt-get "
                                                                          "install -y --no-install-recommends "))]
    body = instructions("".join(T.base_lines(T.stage(own(agent)))[1]))
    body = body[:body.index("ENV HOME=/root")]
    if agent not in T.CHANGES and not any(line.startswith("ENV UV_") for line in body):
        assert mine == body
    # Nothing of the base again, nothing that installs an unpinned release.
    assert "FROM " not in layer and "npm install --global" not in layer and "@latest" not in layer
    assert "ENV HOME=/root" not in layer and "COPY --from=uv" not in layer


@pytest.mark.parametrize("agent", ["hermes", "nanobot"])
def test_uvs_own_variables_hold_only_for_the_layers_step(agent):
    """ENV UV_CACHE_DIR=/tmp/uv-cache UV_NO_CONFIG=1 in the image would hold for Claude Code's uvx as well (no cache, no
    configuration): in the team image they are the export of the step that installs the agent."""
    assert re.search(r"^ENV UV_PYTHON_INSTALL_DIR=", own(agent), re.M)
    layer = T.layer(agent)
    assert not re.search(r"^ENV UV_", layer, re.M)
    assert f"RUN export UV_PYTHON_INSTALL_DIR=/opt/{agent}/python UV_NO_CONFIG=1 UV_CACHE_DIR=/tmp/uv-cache \\\n" \
           "    && uv python install --no-bin" in layer


def test_opencode2_is_opencode2_only_and_opencode_stays_opencode_1():
    layer = T.layer("opencode2")
    assert "install -m 0755 package/bin/opencode /usr/local/bin/opencode2 \\" in layer
    assert "/usr/local/bin/opencode \\" not in layer and "ln -s opencode /usr/local/bin/opencode2" not in layer
    assert "opencode --version" not in layer.replace("opencode2 --version", "")
    whole = T.containerfile(["opencode2"])
    assert whole.rstrip().endswith("RUN claude --version && codex --version && opencode --version\n"
                                   "ENV HOME=/root\nWORKDIR /work")


def test_a_changed_containerfile_stops_the_build_instead_of_a_silent_copy(tmp_path):
    for name in ("Containerfile.team", "Containerfile.opencode2"):
        (tmp_path / name).write_text((SANDBOX / name).read_text())
    T.layer("opencode2", here=tmp_path)
    (tmp_path / "Containerfile.opencode2").write_text(own("opencode2").replace("ln -s opencode", "ln -sf opencode"))
    with pytest.raises(ValueError, match="no longer applies"):
        T.layer("opencode2", here=tmp_path)


def test_goose_gets_the_package_its_base_has_beyond_the_teams():
    assert "bzip2" in T.apt_packages(T.stage(own("goose"))) and "bzip2" not in T.apt_packages(
        T.stage(own("team")))
    assert "RUN apt-get update && apt-get install -y --no-install-recommends bzip2 \\\n" in T.layer("goose")
    assert "apt-get" not in T.layer("qwen")


def test_the_teams_uv_is_the_one_the_layers_were_reviewed_with():
    """The layers install their CPython with the image's uv (Hermes 3.14, nanobot and OpenHands 3.12): the team's is
    the same pinned release as theirs, never latest."""
    pin = re.search(r"^ARG UV_VERSION=(\S+)$", own("team"), re.M).group(1)
    assert pin != "latest"
    for agent in NINE:
        assert re.search(r"^ARG UV_VERSION=(\S+)$", own(agent), re.M).group(1) == pin, agent


def test_the_containerfile_puts_the_layers_in_one_order_on_the_base():
    text = T.containerfile(["qwen", "hermes", "claude"])
    assert text.splitlines()[1:3] == [f"ARG TEAM_BASE={T.BASE_IMAGE}", "FROM ${TEAM_BASE}"]
    assert text.index("# --- Hermes") < text.index("# --- Qwen Code")
    assert text.count("FROM ") == 1
    with pytest.raises(ValueError, match="Containerfile.team"):
        T.containerfile(["claude", "codex"])
    with pytest.raises(ValueError, match="no agent of the team image"):
        T.containerfile(["gemini"])


def test_image_names_and_what_they_hold():
    assert T.image(["claude"]) == T.BASE_IMAGE == "localhost/team-sandbox:latest"
    assert T.image(["claude", "qwen", "hermes"]) == "localhost/team-hermes-qwen-sandbox:latest"
    assert T.agents_of("localhost/team-hermes-qwen-sandbox:latest") == ["claude", "codex", "opencode", "hermes", "qwen"]
    assert T.agents_of(T.BASE_IMAGE) == ["claude", "codex", "opencode"]
    for other in ("localhost/claude-sandbox:latest", "localhost/team-qwen-hermes-sandbox:latest",
                  "localhost/team-gemini-sandbox:latest", "localhost/team-hermes-hermes-sandbox:latest",
                  "localhost/sandbox-ai-cairn:latest"):
        assert T.agents_of(other) is None, other
    every = T.image(list(T.AGENTS))
    assert T.agents_of(every) == list(T.AGENTS)
    assert T.build_command(["claude", "pi", "dsh"]) == "sandbox-ai build team --with dsh,pi"
    assert T.build_command(["codex"]) == "sandbox-ai build team"
    assert T.parse(["qwen,hermes", "pi"]) == ["hermes", "pi", "qwen"] and T.parse(["all"]) == list(T.LAYERS)
    with pytest.raises(ValueError):
        T.parse([""])


def test_versions_are_the_reviewed_releases():
    found = T.versions(list(NINE))
    for agent in NINE:
        assert re.search(rf"^ARG {agent.upper()}_VERSION={re.escape(found[agent])}$", own(agent), re.M)


def test_the_guard_of_opencode2_leaves_opencode_1s_command_alone(tmp_path):
    """The team image: `opencode` is OpenCode 1's guarded command already, OpenCode 2's binary is `opencode2`: its guard
    takes opencode2 only (its own image: both names, as before)."""
    bin_dir, guard_dir = tmp_path / "bin", tmp_path / "lib/sandbox-ai"
    bin_dir.mkdir()
    guard_py = str(guard_dir / "telemetry_guard.py")
    (bin_dir / "opencode").write_text("#!/bin/sh\necho opencode1\n")
    (bin_dir / "opencode").chmod(0o755)
    guard.install("opencode", command_dir=bin_dir, guard_dir=guard_dir)
    (bin_dir / "opencode2").write_text("#!/bin/sh\necho opencode2\n")
    (bin_dir / "opencode2").chmod(0o755)
    real = guard.install("opencode2", command_dir=bin_dir, guard_dir=guard_dir)
    assert real == guard_dir / "real/opencode2" and real.read_text() == "#!/bin/sh\necho opencode2\n"
    assert (bin_dir / "opencode2").read_text() == guard.shim("opencode2", guard_py)
    assert (bin_dir / "opencode").read_text() == guard.shim("opencode", guard_py)
    assert (guard_dir / "real/opencode").read_text() == "#!/bin/sh\necho opencode1\n"


def test_the_cli_says_names_and_refuses_unknown_agents():
    run = lambda *args: subprocess.run([sys.executable, "-I", str(SANDBOX / "team_image.py"), *args],  # noqa: E731
                                       capture_output=True, text=True)
    assert run("layers", "qwen,hermes,claude").stdout == "hermes qwen\n"
    assert run("image", "pi").stdout == "localhost/team-pi-sandbox:latest\n"
    assert run("layers-of", "localhost/team-hermes-qwen-sandbox:latest").stdout == "hermes qwen\n"
    assert run("layers-of", T.BASE_IMAGE).stdout == "\n"
    refused = run("layers", "gemini")
    assert refused.returncode == 2 and "gemini is no agent of the team image" in refused.stderr
    assert run("layers-of", "localhost/claude-sandbox:latest").returncode == 2
    assert run("containerfile", "dsh").stdout.startswith("# sandbox-ai's team image with DeepSeek Harness")


# --- the build through ./sandbox-ai (the fake Podman) ------------------------------------------------------------

@pytest.fixture
def host(tmp_path):
    return F.Host(tmp_path)


def labels_of(call):
    return {call[i + 1].split("=", 1)[0]: call[i + 1].split("=", 1)[1] for i, word in enumerate(call)
            if word == "--label"}


def tag_of(call):
    return call[call.index("--tag") + 1]


def test_build_team_with_builds_the_base_first_then_the_layers(host):
    done = host.run("build", "team", "--with", "qwen,hermes", timeout=240)
    assert done.returncode == 0, done.stderr[-2000:]
    builds = host.builds()
    assert [tag_of(call) for call in builds] == ["localhost/team-sandbox:latest",
                                                 "localhost/team-hermes-qwen-sandbox:latest"]
    layered = builds[-1]
    assert "--pull=never" in layered and "--with" not in " ".join(builds[0])
    assert labels_of(layered) == {"sandbox-ai.role": "team", "sandbox-ai.team-agents": "claude,codex,opencode,hermes,qwen",
                                  "sandbox-ai.hermes-version": T.versions(["hermes"])["hermes"],
                                  "sandbox-ai.qwen-version": T.versions(["qwen"])["qwen"]}
    assert "not there yet: building it first" in done.stderr
    assert "localhost/team-hermes-qwen-sandbox:latest" in host.images()
    # The base is there now: only the layers are built.
    again = host.run("build", "team", "--with=pi", timeout=240)
    assert again.returncode == 0, again.stderr[-2000:]
    assert [tag_of(call) for call in host.builds()[len(builds):]] == ["localhost/team-pi-sandbox:latest"]


def test_building_the_base_again_builds_the_layered_images_on_it(host):
    host.add_image("localhost/team-sandbox:latest", "localhost/team-hermes-sandbox:latest",
                   "localhost/team-dsh-pi-sandbox:latest", "localhost/claude-sandbox:latest")
    done = host.run("build", "team", timeout=240)
    assert done.returncode == 0, done.stderr[-2000:]
    assert [tag_of(call) for call in host.builds()] == ["localhost/team-sandbox:latest",
                                                        "localhost/team-dsh-pi-sandbox:latest",
                                                        "localhost/team-hermes-sandbox:latest"]
    assert "team-hermes-sandbox again, on the new base" in done.stderr


def test_build_team_with_refuses_what_is_no_reviewed_release(host):
    done = host.run("build", "team", "--with", "gemini", timeout=120)
    assert done.returncode == 2 and "gemini is no agent of the team image" in done.stderr
    host.add_image("localhost/team-sandbox:latest")
    done = host.run("build", "team", "--with", "hermes", "--build-arg", "HERMES_VERSION=9.9.9", timeout=120)
    assert done.returncode == 2 and "reviewed, 0.22.5, not 9.9.9" in done.stderr, done.stderr
    assert host.builds() == []
