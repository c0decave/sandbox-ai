"""--audio virtual: a sound server inside the sandbox only. The image of this test machine has no PulseAudio, so a
stand-in listens on the socket PulseAudio would; the real one needs the media variant (sandbox-ai images recipe
media) and is not tried here."""
import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_sandbox_audio", ROOT / "sandbox/agent_sandbox.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)
rt = s.RUNTIME

STAND_IN = r"""
import socket, sys, time
path = next(arg.split("socket=")[1].split()[0] for arg in sys.argv if "module-native-protocol-unix" in arg)
server = socket.socket(socket.AF_UNIX)
server.bind(path)
server.listen(4)
time.sleep(60)
"""


def test_the_command_runs_pulseaudio_as_its_own_user_without_capabilities():
    command = rt.audio_command(Path("/tmp/sandbox-audio/native"))
    assert command[0] == "setpriv" and "--reuid=2" in command and "--regid=2" in command and "--clear-groups" in command
    assert command[command.index("--") + 1] == "pulseaudio" and "--no-new-privs" in command
    assert "--load=module-null-sink sink_name=sandbox sink_properties=device.description=sandbox" in command
    assert "--load=module-native-protocol-unix socket=/tmp/sandbox-audio/native auth-anonymous=1" in command
    assert not any("alsa" in arg or "udev" in arg or "detect" in arg for arg in command), "no sound card is reached"


def test_the_server_is_started_and_waited_for(tmp_path):
    started = []

    def popen(command, **kwargs):
        assert kwargs["env"]["HOME"] == str(tmp_path) and kwargs["env"]["XDG_RUNTIME_DIR"] == str(tmp_path)
        started.append(command)
        program = command[command.index("--") + 1:]
        return subprocess.Popen([sys.executable, "-c", STAND_IN, *program[1:]], stdout=subprocess.DEVNULL,
                                stderr=kwargs["stderr"])
    processes = rt.start_audio({"kind": "virtual"}, which=lambda name: "/usr/bin/pulseaudio", popen=popen,
                               directory=tmp_path, uid=None or __import__("os").getuid())
    try:
        assert len(processes) == 1 and processes[0].poll() is None
        with socket.socket(socket.AF_UNIX) as client:
            client.connect(str(tmp_path / "native"))  # what PULSE_SERVER names: the agent's programs reach it
    finally:
        for process in processes:
            process.kill()
            process.wait()


def test_without_pulseaudio_in_the_image_it_says_how_to_get_it_and_goes_on(tmp_path, capsys):
    assert rt.start_audio({"kind": "virtual"}, which=lambda name: None, directory=tmp_path) == []
    assert "sandbox-ai images recipe media --from AGENT" in capsys.readouterr().err


def test_a_server_that_does_not_start_is_reported_not_fatal(tmp_path, capsys):
    def popen(command, **kwargs):
        return subprocess.Popen([sys.executable, "-c", "import sys; print('E: [pulseaudio] no', file=sys.stderr)"],
                                stderr=kwargs["stderr"])
    assert rt.start_audio({}, which=lambda name: "x", popen=popen, directory=tmp_path,
                          uid=__import__("os").getuid()) == []
    assert "pulseaudio exited" in capsys.readouterr().err


def test_the_launcher_names_the_server_for_the_agent():
    env = s.agent_environment(s.parse_args(["claude", "--audio", "virtual"]))
    assert env["PULSE_SERVER"] == f"unix:{rt.AUDIO_SOCKET}"
    assert "PULSE_SERVER" not in s.agent_environment(s.parse_args(["claude"]))
    with pytest.raises(SystemExit):
        s.parse_args(["claude", "--audio", "host"])  # not offered: the host's server would hand over its microphones


def test_the_media_recipe(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    images = s.load_images_module() if hasattr(s, "load_images_module") else None
    if images is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_images_audio", ROOT / "sandbox/images.py")
        images = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(images)
    lines = []
    assert images.main(["recipe", "media", "--from", "claude"], out=lines.append) == 0
    assert "sandbox-ai build media" in lines[-1] and "--image-variant media" in lines[-1]
    document = json.loads((images.config_root() / "media" / "image.json").read_text())
    assert "pulseaudio" in document["apt"] and "ffmpeg" in document["apt"] and "piper-tts" in document["pip"]
    assert images.main(["recipe", "media", "--from", "claude"], out=lines.append) != 0  # exists already
    assert images.main(["recipe", "media", "--from", "codex", "--as", "media-codex"], out=lines.append) == 0
