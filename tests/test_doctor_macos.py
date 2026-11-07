"""sandbox-ai doctor on macOS: podman machine instead of this host's Linux checks."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_doctor_macos", ROOT / "sandbox/doctor.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


def machine(**fields):
    base = {"Name": "podman-machine-default", "State": "running", "Rootful": False,
            "Mounts": [{"Source": "/Users", "Target": "/Users"}, {"Source": "/private", "Target": "/private"},
                       {"Source": "/var/folders", "Target": "/var/folders"}],
            "Resources": {"CPUs": 6, "Memory": 12288}}
    return lambda cmd, timeout=30: (0, json.dumps([{**base, **fields}]))


def check(runner, home="/Users/ada", tmpdir="/var/folders/x/T"):
    return d.check_machine(runner, home=home, tmpdir=tmpdir)


def test_a_good_machine():
    state, title, detail, hint = check(machine())
    assert (state, title) == ("ok", "podman machine") and "6 CPUs, 12 GiB" in detail and "rootless" in detail


def test_what_is_wrong_and_how_to_fix_it():
    assert check(lambda cmd, timeout=30: (125, ""))[0] == "fail"
    assert "podman machine init" in check(lambda cmd, timeout=30: (125, ""))[3]
    stopped = check(machine(State="stopped"))
    assert stopped[0] == "fail" and stopped[3] == "podman machine start podman-machine-default"
    rootful = check(machine(Rootful=True))
    assert rootful[0] == "fail" and "--rootful=false" in rootful[3]
    unshared = check(machine(Mounts=[{"Source": "/Users"}]))
    assert unshared[0] == "fail" and "/var/folders/x/T" in unshared[2] and "podman machine set -v" in unshared[3]
    small = check(machine(Resources={"CPUs": 2, "Memory": 4096}))
    assert small[0] == "warn" and "--memory" in small[3]
    assert check(lambda cmd, timeout=30: (0, "not json"))[0] == "fail"


def test_on_macos_the_linux_checks_are_left_out(monkeypatch):
    monkeypatch.setattr(d.sys, "platform", "darwin")
    called = []
    for name in [n for n in dir(d) if n.startswith("check_") and callable(getattr(d, n))]:
        monkeypatch.setattr(d, name, lambda *a, name=name, **k: called.append(name) or ("ok", name, "", ""))
    d.checks("podman")
    linux_only = {"check_userns", "check_user_session", "check_proxy_nat", "check_pasta", "check_subids",
                  "check_newuidmap", "check_bwrap_tools", "check_tiocsti"}
    assert "check_machine" in called and "check_podman" in called and not linux_only & set(called)
    called.clear()
    monkeypatch.setattr(d.sys, "platform", "linux")
    d.checks("podman")
    assert "check_machine" not in called and "check_pasta" in called
    assert d.group_of("podman machine") == "Engine"
