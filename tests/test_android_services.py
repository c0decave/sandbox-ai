"""Android beside the agent: the services android-emulator (the emulator without a window, /dev/kvm for the service
alone) and adb (an adb server for the USB device it is given), sandbox-ai's own Containerfiles with what their
service starts with (service.json), a service's device= (checked as --device is, never for the agent), the license
of what the emulator's image is built from (accepted before anything is fetched), and the services' names in the
sandbox's hosts file. Podman is recorded answers; what only a machine with KVM and Google's downloads can prove is
the live check android-live (scripts/live_gaps.py)."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sv = load("services_android_tests", "sandbox/services.py")
im = load("images_for_android_tests", "sandbox/images.py")
s = load("launcher_for_android_tests", "sandbox/agent_sandbox.py")
sv._images = im
sv._launcher = s
im._services = sv
ID = "b" * 64
DIGEST = "sha256:" + "a" * 64
FLAG = "--accept-android-sdk-license"
DEBIAN = {"Id": "sha256:" + "e" * 64, "Digest": DIGEST, "RepoDigests": [f"docker.io/library/debian@{DIGEST}"],
          "Config": {}, "Os": "linux", "Architecture": "amd64"}


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(im, "user_bus", lambda uid=None: True)
    monkeypatch.setattr(im, "podman", lambda *args, **kwargs: ["podman"])
    return tmp_path


@pytest.fixture
def dev(tmp_path, monkeypatch):
    """A /dev of files that count as devices: kvm, and a USB node."""
    folder = tmp_path / "dev"
    (folder / "bus/usb/001").mkdir(parents=True)
    for name in ("kvm", "bus/usb/001/004", "sda"):
        (folder / name).write_bytes(b"")
    monkeypatch.setattr(s, "DEV", folder)
    monkeypatch.setattr(s, "device_mode", lambda path: (stat.S_IFBLK if Path(path).name == "sda" else stat.S_IFCHR)
                        | 0o660 if Path(path).is_file() else os.stat(path).st_mode)
    original = s.parse_device
    # /dev/NAME as a service names it is NAME of this folder.
    monkeypatch.setattr(s, "parse_device", lambda value, option="--device": original(
        str(folder / value[len("/dev/"):]) if value.startswith("/dev/") else value, option))
    return folder


def start(*options, mode=None):
    a = s.parse_args(["claude", *options])
    a.vpn_profile = None
    a.run_id = "claude-1234abcd"
    a.keep_home = False
    a.mode = mode
    return a


def built(name, template, **more):
    """The record of your image NAME, built from sandbox-ai's own Containerfile TEMPLATE."""
    record = {"kind": "built", "image": sv.IMAGE_TAG.format(name=name), "id": ID, "builtin": template, "ports": [],
              "user": "0:0", "os": {}, **more}
    found = {**sv.read_registry(), name: record}
    sv.write_registry(found)
    return record


class Podman:
    def __init__(self):
        self.calls, self.context = [], {}

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        words = argv[1:]
        if words[0] == "pull":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if words[:2] == ["image", "inspect"]:
            data = DEBIAN if "debian" in words[-1] else {"Id": "sha256:" + "d" * 64, "Config": {}, "Os": "linux",
                                                         "Architecture": "amd64"}
            return subprocess.CompletedProcess(argv, 0, json.dumps([data]), "")
        if words[0] == "build":
            folder = Path(argv[-1])
            self.context = {path.relative_to(folder).as_posix(): path.read_bytes() for path in folder.rglob("*")
                            if path.is_file()}
            self.containerfile = Path(argv[argv.index("--file") + 1]).read_text()
            return subprocess.CompletedProcess(argv, 0, "", "")
        if words[0] == "create":
            return subprocess.CompletedProcess(argv, 0, "container1\n", "")
        if words[0] == "cp":
            return subprocess.CompletedProcess(argv, 1, b"", b"no such file")
        if words[0] == "rm":
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


# --- sandbox-ai's own Containerfiles and what their services start with ---------------------------------------------


def test_the_templates_say_what_their_service_starts_with(capsys):
    rows = {row["template"]: row for row in sv.templates(found={})}
    emulator, adb = rows["android-emulator"], rows["adb"]
    spec = sv.parse_spec("android-emulator:" + emulator["spec"])
    assert spec["ports"] == [5555, 5554, 5550] and spec["ready"] == 5550 and spec["ready_timeout"] == 600
    assert spec["devices"] == ["/dev/kvm"] and spec["data"] == ["/avd"] and spec["memory"] == "6g"
    assert spec["read_only"] is True and spec["user"] is None
    assert emulator["terms"]["id"] == "android-sdk-license" and emulator["terms"]["option"] == FLAG
    assert emulator["build_network"] is True and emulator["arches"] == ["x86_64"] and "arm64" in emulator["about"]
    spec = sv.parse_spec("adb:" + adb["spec"])
    assert spec["ports"] == [5037] and spec["devices"] == [] and spec["data"] == ["/adb"]
    assert adb["terms"] is None and adb["build_network"] is True and adb["arches"] == []
    assert rows["showcase-web"]["spec"] == "" and rows["showcase-web"]["terms"] is None
    assert sv.main(["templates"]) == 0
    text = capsys.readouterr().out
    assert "as a service: --service android-emulator" in text and "device=/dev/kvm" in text
    assert "Android Software Development Kit License Agreement" in text and FLAG in text


def test_what_you_give_replaces_the_templates_keys_one_by_one():
    assert sv.with_template("web:image=showcase-web,port=8080", "showcase-web") == "web:image=showcase-web,port=8080"
    merged = sv.parse_spec(sv.with_template("emu:image=android-emulator,memory=8g", "android-emulator"))
    assert merged["memory"] == "8g" and merged["ports"] == [5555, 5554, 5550] and merged["devices"] == ["/dev/kvm"]
    own = sv.parse_spec(sv.with_template("android-emulator:port=6000,device=/dev/kvm", "android-emulator"))
    assert own["ports"] == [6000] and own["ready"] is None and own["ready_timeout"] == 60  # its ready= named its port
    assert own["devices"] == ["/dev/kvm"] and own["data"] == ["/avd"]


def test_the_emulators_image_takes_the_variants_own_pins_and_scripts():
    """One list of pins: the image is built from the rows of sandbox/recipes/android-dev/sdk.lock (the image variant
    android-dev's), with its install.sh - nothing is pinned twice."""
    folder = sv.BUILTIN_IMAGES / "android-emulator"
    text = (folder / "Containerfile").read_text()
    info = sv.template_info("android-emulator")
    assert "recipes/android-dev/sdk.lock" in info["context"] and "recipes/android-dev/install.sh" in info["context"]
    lock = (ROOT / "sandbox/recipes/android-dev/sdk.lock").read_text().splitlines()
    pattern = sv.re.search(r"awk '\$1 ~ /(.+?)/'", text)[1]
    taken = [line.split()[0] for line in lock if line and not line.startswith("#")
             and sv.re.search(pattern, line.split()[0])]
    assert taken == ["cmdline-tools;23.0", "platform-tools", "emulator",
                     "system-images;android-37.0;google_apis;x86_64"]
    assert '= 4 ]' in text and "install.sh /opt/android-dev/emulator.lock" in text
    for package in taken:  # the package.xml of each is in the build's context
        assert f"recipes/android-dev/package-xml/{package.replace(';', '_')}.xml" in info["context"], package
    assert "developer.android.com/studio/terms" in text and "may not be passed on" in text
    for name, script in (("android-emulator", "android-emulator-service"), ("adb", "adb-service")):
        path = sv.BUILTIN_IMAGES / name / script
        assert subprocess.run(["sh", "-n", str(path)], capture_output=True).returncode == 0, script
        assert f'"/bin/sh", "/usr/local/bin/{script}"]' in (sv.BUILTIN_IMAGES / name / "Containerfile").read_text()
    service = (folder / "android-emulator-service").read_text()
    assert "sys.boot_completed" in service and "-no-window" in service and "-no-metrics" in service
    assert 'nc -lk 127.0.0.1 "$ready"' in service and "/dev/kvm" in service
    adb = (sv.BUILTIN_IMAGES / "adb" / "Containerfile").read_text()
    assert "apt-get install -y --no-install-recommends adb fastboot tini" in adb and "dl.google.com" not in adb


# --- a service's device ---------------------------------------------------------------------------------------------


def test_a_services_device_is_a_path_under_dev_or_a_usb_device():
    spec = sv.parse_spec("adb:port=5037,device=usb:18d1:4ee7,device=/dev/bus/usb/001/004")
    assert spec["devices"] == ["usb:18d1:4ee7", "/dev/bus/usb/001/004"]
    assert sv.parse_spec(sv.spec_text(spec)) == spec
    assert sv.spec_text(spec).endswith("device=usb:18d1:4ee7,device=/dev/bus/usb/001/004")
    assert "devices" in sv.default_service("x") and sv.parse_spec("x")["devices"] == []
    for bad in ("device=kvm", "device=/dev/../etc/passwd", "device=usb:1", "device=", "device=/dev/a b",
                "device=/dev/kvm,device=/dev/kvm"):
        with pytest.raises(ValueError, match="device="):
            sv.parse_spec(f"x:{bad}")
    with pytest.raises(ValueError, match="at most 8 devices"):
        sv.parse_spec("x:" + ",".join(f"device=/dev/ttyUSB{i}" for i in range(9)))


def test_the_emulator_gets_kvm_and_the_agent_does_not(dev, config):
    built("android-emulator", "android-emulator")
    a = start("--service", "android-emulator")
    service = sv.check(a, s)[0]
    assert service["ports"] == [5555, 5554, 5550] and service["memory"] == "6g" and service["user"] == "0:0"
    assert service["device_entries"] == [{"value": "/dev/kvm", "path": str(dev / "kvm"), "kind": "node"}]
    argv, _ = sv.create_command(a, "pod", service, config / "session", s, limits={"memory", "cpus", "pids"})
    at = argv.index("--device")
    assert argv[at + 1] == str(dev / "kvm") and "--cap-drop=ALL" in argv and "--read-only" in argv
    assert "--security-opt=no-new-privileges" in argv and argv[argv.index("--user") + 1] == "0:0"
    assert argv[argv.index("--memory") + 1] == "6g" and f"/avd:rw,noexec,nosuid,nodev,size=6g,U" in argv
    assert a.device == [] and s.devices(a) == []  # nothing of it for the agent's own container
    notes = "\n".join(sv.notes(a))
    assert f"it alone gets {dev / 'kvm'}" in notes and "the agent's sandbox does not" in notes
    plan = sv.plan(a, s)[0]
    assert plan["devices"] == [str(dev / "kvm")] and plan["ready"] == 5550 and plan["ready_timeout"] == 600
    assert sv.supervisor_config(a) == [{"name": "android-emulator", "ready": 5550, "timeout": 600}]
    # Another name for it, and limits of your own: the template's keys still come with its image.
    other = sv.check(start("--service", "emu:image=android-emulator,memory=8g,cpus=4"), s)[0]
    assert other["memory"] == "8g" and other["cpus"] == "4" and other["devices"] == ["/dev/kvm"]


def test_a_host_without_kvm_and_what_no_service_may_be_given(dev, config):
    built("android-emulator", "android-emulator")
    (dev / "kvm").unlink()
    with pytest.raises(ValueError, match=r"--service android-emulator: its device \S*/dev/kvm: No such file"):
        sv.check(start("--service", "android-emulator"), s)
    built("adb", "adb")
    with pytest.raises(ValueError, match="--service adb: its device .*block devices"):  # a disk: as --device says
        sv.check(start("--service", "adb:device=/dev/sda"), s)
    with pytest.raises(ValueError, match="--service adb: its device expects a path under"):  # as --device says
        sv.check(start("--service", "adb:device=/etc/passwd"), s)
    service = sv.check(start("--service", "adb:device=/dev/bus/usb/001/004"), s)[0]
    assert service["ports"] == [5037] and service["device_entries"][0]["path"] == str(dev / "bus/usb/001/004")


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_the_strict_modes_take_no_device_for_a_service(dev, config, mode):
    built("android-emulator", "android-emulator")
    with pytest.raises(ValueError, match=f"not taken in the security mode {mode}.*--mode medium"):
        sv.check(start("--service", "android-emulator", mode=mode), s)


def test_the_policy_and_the_device_rules_hold_for_a_services_device(dev, config, monkeypatch):
    built("android-emulator", "android-emulator")
    a = start("--service", "android-emulator")
    a.policy = {"forbid": ["--device"]}
    with pytest.raises(ValueError, match="--device is not allowed by the administrator's policy"):
        sv.check(a, s)

    verdict = {"reason": "no virtual machines here", "rule": "kvm", "ruleset": "mine"}

    class Rules(s.RUNTIME.Ruleset):
        devices_active = True

        def device(self, facts):
            return verdict
    monkeypatch.setattr(s.RUNTIME, "Ruleset", Rules)
    for decision, why in (("deny", "no virtual machines here"), ("ask", "starts before anyone could answer")):
        verdict["decision"] = decision
        with pytest.raises(ValueError, match=why):
            sv.check(start("--service", "android-emulator"), s)


def test_the_adb_service_takes_port_5037_unless_the_hosts_adb_server_has_it(dev, config):
    built("adb", "adb")
    value = "adb:device=/dev/bus/usb/001/004"
    with pytest.raises(ValueError, match="port 5037 is a port of the host"):
        sv.check(start("--service", value, "--allow-host-port", "5037"), s)
    service = sv.check(start("--service", value), s)[0]
    assert service["ports"] == [5037] and service["data"] == ["/adb"] and service["memory"] == "256m"


def test_the_services_names_are_the_loopback_in_the_sandbox(dev, config):
    built("android-emulator", "android-emulator")
    a = start("--service", "android-emulator")
    sv.check(a, s)
    names = s.own_host_names(a)
    assert "android-emulator" in names and "agent-sandbox" in names
    hosts = s.hosts_file({}, names)
    assert hosts.splitlines()[0].startswith("127.0.0.1 localhost ") and " android-emulator" in hosts.splitlines()[0]
    assert "android-emulator" not in s.own_host_names(start())


# --- the build: the license first -----------------------------------------------------------------------------------


def test_the_emulators_image_is_built_only_once_the_license_is_accepted(config):
    podman = Podman()
    said = []
    with pytest.raises(ValueError, match="no terminal to ask on.*images build-image android-emulator --builtin "
                                         "android-emulator " + FLAG):
        sv.build("android-emulator", None, builtin="android-emulator", run=podman, out=said.append, terminal=False,
                 machine="x86_64")
    with pytest.raises(ValueError, match="security mode high asks no question"):
        sv.build("android-emulator", None, builtin="android-emulator", run=podman, out=said.append, terminal=True,
                 read=lambda _: "y", mode="high", machine="x86_64")
    with pytest.raises(ValueError, match="was not accepted"):
        sv.build("android-emulator", None, builtin="android-emulator", run=podman, out=said.append, terminal=True,
                 read=lambda _: "", machine="x86_64")
    assert podman.calls == [], "nothing is pulled or built before"
    text = " ".join(said)
    assert "Android Software Development Kit License Agreement" in text and "redistributing" in text
    with pytest.raises(ValueError, match="built for x86_64 only, this machine is aarch64"):
        sv.build("android-emulator", None, builtin="android-emulator", run=podman, accepted=True, machine="aarch64")
    said.clear()
    name, entry = sv.build("android-emulator", None, builtin="android-emulator", run=podman, out=said.append,
                           accepted=True, machine="x86_64")
    build = next(call for call in podman.calls if call[1] == "build")
    assert "--network=none" not in build and "--pull=never" in build  # it fetches: sandbox-ai's own Containerfile
    assert any("fetches what it is built from" in line for line in said)
    assert f"FROM docker.io/library/debian@{DIGEST}" in podman.containerfile
    # The context: the template's own files, and the image variant android-dev's lock and scripts as shared/.
    assert {"Containerfile", "android-emulator-service", "service.json", "shared/install.sh", "shared/sdk.lock",
            "shared/android-sdk-license", "shared/emulator.xml", "shared/platform-tools.xml",
            "shared/cmdline-tools_23.0.xml",
            "shared/system-images_android-37.0_google_apis_x86_64.xml"} == set(podman.context)
    assert podman.context["shared/sdk.lock"] == (ROOT / "sandbox/recipes/android-dev/sdk.lock").read_bytes()
    assert entry["builtin"] == "android-emulator" and entry["terms"]["id"] == "android-sdk-license"
    assert entry["terms"]["how"] == "option" and entry["build_network"] is True and entry["user"] == "0:0"
    assert sv.read_registry()["android-emulator"]["terms"]["accepted"].endswith("Z")


def test_the_images_command_hands_the_acceptance_on(config, monkeypatch):
    """`sandbox-ai images build-image NAME --builtin android-emulator --accept-android-sdk-license` (images.py's own
    parser hands the option to services.py's)."""
    monkeypatch.setattr(sv, "machine_arch", lambda value=None: "x86_64")
    podman = Podman()
    assert im.main(["build-image", "emu", "--builtin", "android-emulator", FLAG], run=podman) == 0
    entry = sv.read_registry()["emu"]
    assert entry["builtin"] == "android-emulator" and entry["terms"]["how"] == "option"
    # Under another name it is still the emulator's image: its service starts with what the template brings.
    assert sv.parse_spec(sv.with_template("phone:image=emu", entry["builtin"]))["devices"] == ["/dev/kvm"]


def test_the_adb_image_needs_no_license_and_is_built_with_the_network(config):
    podman = Podman()
    said = []
    name, entry = sv.build("adb", None, builtin="adb", run=podman, out=said.append, terminal=False)
    build = next(call for call in podman.calls if call[1] == "build")
    assert "--network=none" not in build and build[-1] == str(sv.BUILTIN_IMAGES / "adb")
    assert "terms" not in entry and not any("License" in line for line in said)


def test_a_start_offers_the_build_as_a_question_of_its_own_and_never_in_the_strict_modes(config):
    with pytest.raises(ValueError) as refused:
        sv.check(start("--service", "android-emulator"), s)
    fixes = refused.value.fixes
    assert [item["id"] for item in fixes] == ["service:android-emulator"]
    fix = fixes[0]
    assert fix["commands"] == [["sandbox-ai", "images", "build-image", "android-emulator", "--builtin",
                                "android-emulator", FLAG]]
    assert fix["terms"] == "android-sdk-license" and fix["choice"] and fix["verdict"] == "offered"
    assert "Android Software Development Kit License Agreement" in fix["security"]
    assert "developer.android.com/studio/terms" in fix["security"] and "UNCHECKED" in fix["security"]
    f = sv.fixes_module()
    judged = f.judge(fixes, "high")[0]
    assert judged["verdict"] == "refused" and "a license, which is yours" in judged["why"] and FLAG in judged["why"]
    assert f.judge(fixes, "medium")[0]["verdict"] == "offered"
    # --fix does not accept a license for you; --fix-only names it; a terminal asks, No first.
    ran = []
    with pytest.raises(ValueError, match="--fix-only service:android-emulator does it"):
        f.resolve(str(refused.value), fixes, fix_all=True, terminal=False, out=lambda _: None,
                  run_command=lambda command, terminal: ran.append(command) or (0, []))
    assert ran == []
    asked = []
    with pytest.raises(ValueError):
        f.resolve(str(refused.value), fixes, terminal=True, out=lambda _: None,
                  read=lambda prompt: asked.append(prompt) or "", run_command=lambda c, t: ran.append(c) or (0, []))
    assert ran == [] and len(asked) == 1 and "[y/N]" in asked[0] and "License Agreement" in asked[0]
    assert f.resolve(str(refused.value), fixes, terminal=True, out=lambda _: None, read=lambda _: "y",
                     run_command=lambda c, t: ran.append(c) or (0, [])) == ["service:android-emulator"]
    assert ran == fix["commands"]
    # adb: built as showcase-web is - offered with everything else, no license.
    with pytest.raises(ValueError) as refused:
        sv.check(start("--service", "adb"), s)
    assert refused.value.fixes[0]["terms"] == "" and not refused.value.fixes[0]["choice"]
    assert refused.value.fixes[0]["commands"] == [["sandbox-ai", "images", "build-image", "adb", "--builtin", "adb"]]


def test_a_start_offers_the_toolchain_android_sdk_the_same_way():
    fix = s.toolchain_fix("android-sdk@36")
    assert fix["commands"] == [["sandbox-ai", "toolchain", "add", "android-sdk@36", FLAG]]
    assert fix["terms"] == "android-sdk-license" and fix["choice"] == "license:toolchain:android-sdk@36"
    assert "Android Software Development Kit License Agreement" in fix["security"]
    assert sv.fixes_module().judge([fix], "paranoid")[0]["verdict"] == "refused"
    plain = s.toolchain_fix("go")
    assert plain["commands"] == [["sandbox-ai", "toolchain", "add", "go"]] and not plain["terms"]


# --- through ./sandbox-ai -------------------------------------------------------------------------------------------


def run_cli(*argv):
    return subprocess.run([str(ROOT / "sandbox-ai"), *argv], capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL)


def test_the_command_line_end_to_end(config):
    listed = run_cli("images", "templates", "--json")
    assert listed.returncode == 0, listed.stderr
    rows = {row["template"]: row for row in json.loads(listed.stdout)}
    assert rows["android-emulator"]["terms"]["option"] == FLAG and "device=/dev/kvm" in rows["android-emulator"]["spec"]
    assert rows["adb"]["spec"].startswith("port=5037")
    refused = run_cli("images", "build-image", "android-emulator", "--builtin", "android-emulator")
    assert refused.returncode != 0
    text = refused.stdout + refused.stderr
    if sv.machine_arch() == "x86_64":
        assert "Android Software Development Kit License Agreement" in text and FLAG in text
    else:  # (Google's emulator for Linux is x86_64 only: said before any license is asked for)
        assert "built for x86_64 only" in text
    assert FLAG in run_cli("images", "build-image", "--help").stdout
    # Tab: sandbox-ai's own services where a service or a template is named, and the option that accepts.
    assert {"android-emulator", "adb"} <= set(run_cli("__complete", "--", "claude", "--service", "").stdout.split())
    assert {"android-emulator", "adb", "showcase-web"} <= set(
        run_cli("__complete", "--", "images", "build-image", "x", "--builtin", "").stdout.split())
    assert FLAG in run_cli("__complete", "--", "images", "build-image", "--a").stdout
    assert FLAG in run_cli("__complete", "--", "toolchain", "add", "--a").stdout
    assert "android-emulator" in run_cli("claude", "--help").stdout
    if sys.platform == "linux":
        started = run_cli("claude", "--service", "android-emulator", "--dry-run", "--backend", "podman")
        assert started.returncode != 0
        assert "sandbox-ai images build-image android-emulator --builtin android-emulator" in started.stderr
        assert "License Agreement" in started.stderr
