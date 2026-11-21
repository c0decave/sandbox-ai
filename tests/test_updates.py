"""sandbox-ai update mode (sandbox/updates.py): the mode in defaults.json and its timer, the daily check with a fake
clock, download (checked in full; a bad signature refused), auto (only in the window, only while nothing runs, never a
downgrade), the rollback, the kinds of copies that do not update themselves, the command line's hint and the notify
event "update". install.sh itself is faked here (a runner that answers as it does); tests/test_install.py runs the
real one against a fake release server, end to end through ./sandbox-ai."""
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import home_guard  # noqa: E402

home_guard.ensure()

SPEC = importlib.util.spec_from_file_location("sandbox_updates_under_test", ROOT / "sandbox" / "updates.py")
U = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(U)
SIGNER = "SHA256:AbCdEf0123456789"


def local(*args):
    return datetime(*args).timestamp()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SANDBOX_AI_SOURCE", raising=False)
    monkeypatch.delenv("SANDBOX_AI_UPDATE_NOW", raising=False)
    (tmp_path / "home").mkdir()
    return tmp_path


def make_tree(folder, version):
    (folder / "sandbox").mkdir(parents=True)
    (folder / "sandbox" / "agent_sandbox.py").write_text(f'"""x"""\n__version__ = "{version}"\n')
    (folder / "install.sh").write_text("#!/bin/sh\nexit 99\n")
    return folder


@pytest.fixture
def installed(home, monkeypatch):
    """A user's installation made by install.sh, at 1.0.0 (0.9.0 kept before it)."""
    data = home / "share" / "sandbox-ai"
    for version in ("0.9.0", "1.0.0"):
        make_tree(data / "versions" / version, version)
    os.symlink("versions/1.0.0", data / "current")
    monkeypatch.setattr(U, "own_version", lambda: U.tree_version(data / "current") or "?")
    return {"kind": "user", "tree": str(data / "versions" / "1.0.0"), "data": str(data), "prefix": None,
            "package": None}


class FakeInstaller:
    """install.sh as updates.py runs it: --check, --fetch DIR, --from BUNDLE --verify."""
    def __init__(self, data, available="9.9.9", signature=f"good, by the trusted key {SIGNER}"):
        self.data, self.available, self.signature, self.calls = Path(data), available, signature, []
        self.fetch_error = None

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if "--check" in command:
            return subprocess.CompletedProcess(command, 0, f"installed: x\navailable: {self.available} (src)\n"
                                                           f"signature: {self.signature}\n", "")
        if "--fetch" in command:
            if self.fetch_error:
                return subprocess.CompletedProcess(command, 1, "", f"install.sh: error: {self.fetch_error}\n")
            folder = Path(command[command.index("--fetch") + 1])
            folder.mkdir(parents=True, exist_ok=True)
            version = command[command.index("--version") + 1]
            for name in (f"sandbox-ai-bundle-{version}.tar.gz", "SHA256SUMS", "SHA256SUMS.sig"):
                (folder / name).write_text(name)
            return subprocess.CompletedProcess(command, 0, f"{version}\ngood {SIGNER}\nsandbox-ai-bundle-{version}"
                                                           ".tar.gz\n", "")
        if "--from" in command:
            assert "--verify" in command, "a downloaded release is always checked again"
            version = Path(command[command.index("--from") + 1]).name[len("sandbox-ai-bundle-"):-len(".tar.gz")]
            make_tree(self.data / "versions" / version, version)
            link = self.data / ".new"
            os.symlink(f"versions/{version}", link)
            os.replace(link, self.data / "current")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(command)

    def count(self, word):
        return sum(word in call for call in self.calls)


def told():
    said = []
    return said, lambda title, text, urgency="normal": said.append((title, text))


@pytest.fixture
def systemctl(monkeypatch):
    """The timer's commands, recorded (never run): no real systemd user manager is touched."""
    calls = []
    schedule = U.load_module("schedule")
    monkeypatch.setattr(schedule.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(schedule.sys, "platform", "linux")

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")
    return calls, run


# -- the mode and its timer -------------------------------------------------------------------------------------------

def test_without_a_setting_the_mode_is_notify(home):
    assert U.settings() == {"mode": "notify", "set": False, "window": "03:00-05:00", "source": None}
    assert U.release_source() == U.DEFAULT_SOURCE


def test_the_mode_is_kept_in_the_defaults_file_and_makes_the_timer(home, installed, systemctl):
    calls, run = systemctl
    said = []
    U.set_mode("auto", "02:00-04:30", "https://git.example/o/r", installed, said.append, run)
    document = json.loads((home / "config" / "sandbox-ai" / "defaults.json").read_text())
    assert document["update_mode"] == "auto" and document["update_window"] == "02:00-04:30"
    assert document["update_source"] == "https://git.example/o/r"
    assert U.settings()["mode"] == "auto" and U.settings()["set"] is True
    units = home / "config" / "systemd" / "user"
    service = (units / "sandbox-ai-update.service").read_text()
    assert service.startswith(U.UNIT_MARKER)
    assert f'"{installed["data"]}/current/sandbox/main.py" "update" "--scheduled"' in service, \
        "the timer runs the installation's current/, never one version's folder (gone after two updates)"
    assert "OnCalendar=*-*-* *:00/15:00" in (units / "sandbox-ai-update.timer").read_text()
    assert ["systemctl", "--user", "enable", "--now", "sandbox-ai-update.timer"] in calls
    assert "update mode: auto, window 02:00-04:30" in said
    # The schedules' timer is another one: untouched.
    assert not (units / "sandbox-ai-schedule.timer").exists()
    U.set_mode("off", info=installed, out=said.append, run=run)
    assert not (units / "sandbox-ai-update.timer").exists() and not (units / "sandbox-ai-update.service").exists()
    assert ["systemctl", "--user", "disable", "--now", "sandbox-ai-update.timer"] in calls
    assert U.settings()["mode"] == "off"


def test_the_timer_on_macos_is_a_launch_agent_of_its_own(home, installed, systemctl, monkeypatch):
    calls, run = systemctl
    monkeypatch.setattr(U.load_module("schedule").sys, "platform", "darwin")
    U.set_mode("notify", info=installed, out=lambda line: None, run=run)
    plist = home / "home" / "Library" / "LaunchAgents" / "ai.sandbox-ai.update.plist"
    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == "ai.sandbox-ai.update" and data["StartInterval"] == 900
    assert data["ProgramArguments"][-2:] == ["update", "--scheduled"]
    assert calls[-1][:2] == ["launchctl", "bootstrap"]
    assert U.timer_files("darwin") == [str(plist)]


def test_bad_values_are_refused(home, installed, systemctl):
    _, run = systemctl
    with pytest.raises(ValueError, match="one of off, notify, download, auto"):
        U.set_mode("always", info=installed, run=run)
    with pytest.raises(ValueError, match="window of mode auto"):
        U.set_mode("notify", "03:00", info=installed, run=run)
    with pytest.raises(ValueError, match="HH:MM"):
        U.set_mode("auto", "3 am", info=installed, run=run)
    with pytest.raises(ValueError, match="plain http only to this machine"):
        U.set_mode("notify", source="http://git.example/o/r", info=installed, run=run)
    assert not (home / "config" / "sandbox-ai" / "defaults.json").exists(), "nothing written"


def test_a_window_may_pass_midnight_and_an_hour_alone_is_that_hour():
    assert U.window_text("23:30-01:00") == "23:30-01:00" and U.window_text("03:00") == "03:00-04:00"
    assert U.in_window(local(2026, 11, 18, 0, 30), "23:30-01:00")
    assert not U.in_window(local(2026, 11, 18, 1, 0), "23:30-01:00")
    assert U.next_window(local(2026, 11, 18, 6, 0), "03:00-05:00") == local(2026, 11, 19, 3, 0)


@pytest.mark.parametrize("info, words", [
    ({"kind": "pip", "tree": "/usr/lib/python3/nowhere/site-packages"}, "holds no pip installation of sandbox-ai-app"),
    ({"kind": "package", "tree": "/tmp/.mount", "package": "appimage"}, "was not started from the AppImage file"),
    ({"kind": "package", "tree": "/x", "package": "macos-app"}, "is not inside a sandbox-ai.app"),
    ({"kind": "package", "tree": "/x", "package": "pyz"}, "was not started from the .pyz file"),
    ({"kind": "checkout", "tree": "/src/sandbox-ai"}, "git -C /src/sandbox-ai pull, then install it: ./install.sh "
                                                      "--from ."),
    ({"kind": "system", "tree": "/usr/local/lib/sandbox-ai/versions/1.0.0", "data": "/usr/local/lib/sandbox-ai",
      "prefix": "/usr/local"}, "sudo sandbox-ai update"),
])
def test_copies_that_do_not_update_themselves_get_notify_and_the_right_command(home, systemctl, info, words,
                                                                               monkeypatch):
    """A copy whose file or folder is not there to replace (here: none was started from a file), a checkout and a
    system installation: notify, and the way that updates it."""
    monkeypatch.delenv("SANDBOX_AI_PACKAGE", raising=False)
    info = {"data": None, "prefix": None, "package": None, **info}
    _, run = systemctl
    assert U.self_updating(info) is False
    for mode in ("download", "auto"):
        with pytest.raises(ValueError, match="notify works") as refused:
            U.set_mode(mode, info=info, run=run)
        assert words in str(refused.value)
    U.set_mode("notify", info=info, out=lambda line: None, run=run)
    assert U.status(info, run)["manual"] == U.manual_way(info) and words in U.manual_way(info)
    assert U.status(info, run)["self_updating"] is False
    with pytest.raises(ValueError, match="notify works"):
        U.apply(info)
    if info["kind"] == "system":  # (the tests may run as root, who installs a system installation by hand)
        monkeypatch.setattr(U, "installs_by_hand", lambda info, access=os.access: False)
    with pytest.raises(ValueError, match="does not replace itself|only root changes: sudo sandbox-ai update"):
        U.install_now(info, busy=lambda: [], runner=lambda *args, **kwargs: pytest.fail("nothing is run"))


# -- the copies install.sh did not install: they replace themselves where they can be written (package_update.py) -----

P = U.packages()


def package_copy(tmp_path, kind, monkeypatch, version="1.0.0"):
    """(info, target) of a copy of KIND as it lies on disk: the .pyz or the AppImage as a file beside the tree its
    start unpacked (SANDBOX_AI_PACKAGE names the file), sandbox-ai.app, a site folder with the package's record."""
    if kind == "pip":
        monkeypatch.setattr(P.importlib.util, "find_spec", lambda name: object())  # (the environment has a pip)
        site = tmp_path / "venv/lib/python3.12/site-packages"
        (site / "sandbox_ai").mkdir(parents=True)
        (site / "sandbox_ai/agent_sandbox.py").write_text(f'__version__ = "{version}"\n')
        record = site / f"sandbox_ai_app-{version}.dist-info"
        record.mkdir()
        (record / "INSTALLER").write_text("pip\n")
        return {"kind": "pip", "tree": str(site), "data": None, "prefix": None, "package": None}, site
    if kind == "macos-app":
        bundle = tmp_path / "Applications/sandbox-ai.app"
        tree = make_tree(bundle / "Contents/Resources/sandbox-ai", version)
        (bundle / "Contents/MacOS").mkdir()
        (bundle / "Contents/MacOS/sandbox-ai").write_text("#!/bin/sh\n")
        return {"kind": "package", "tree": str(tree), "data": None, "prefix": None, "package": "macos-app"}, bundle
    tree = make_tree(tmp_path / "cache" / kind / f"{version}-abc", version)
    name = "sandbox-ai.pyz" if kind == "pyz" else "sandbox-ai-x86_64.AppImage"
    file = tmp_path / "bin" / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"the package")
    monkeypatch.setenv("SANDBOX_AI_PACKAGE", str(file))
    return {"kind": "package", "tree": str(tree), "data": None, "prefix": None, "package": kind}, file


@pytest.mark.parametrize("kind", ["pyz", "appimage", "macos-app", "pip"])
def test_a_package_that_can_be_written_replaces_itself(kind, home, tmp_path, monkeypatch, systemctl):
    info, path = package_copy(tmp_path, kind, monkeypatch)
    assert P.target(info, machine="x86_64") == (path, None)
    assert U.self_updating(info) is True and U.manual_way(info) == "sandbox-ai update"
    assert U.refusal(info, "auto") is None and U.refusal(info, "download") is None
    _, run = systemctl
    status = U.status(info, run)
    assert status["self_updating"] is True and status["manual"] is None and status["refused"] == {}
    assert status["rollback"] is None and status["downloaded"] is None


@pytest.mark.parametrize("kind, command", [
    ("pyz", "{path} update"), ("appimage", "{path} cli update"),
    ("macos-app", "{path}/Contents/MacOS/sandbox-ai update"),
])
def test_a_package_in_a_folder_you_cannot_write_says_the_command_that_can(kind, command, home, tmp_path, monkeypatch):
    """Whoever may write the folder runs the same update: the exact command is said (never a guess at a download)."""
    info, path = package_copy(tmp_path, kind, monkeypatch)
    path_, why = P.target(info, access=lambda folder, mode: False, machine="x86_64")
    assert path_ is None
    assert f"{path.parent} is not writable for you" in why and "sudo -H " + command.format(path=path) in why


def test_a_pip_installation_you_cannot_write_or_without_pip_says_the_command(home, tmp_path, monkeypatch):
    info, site = package_copy(tmp_path, "pip", monkeypatch)
    _, why = P.target(info, access=lambda folder, mode: False)
    assert f"{site} is not writable for you" in why and "sudo " in why and why.endswith(" update")
    monkeypatch.setattr(P.importlib.util, "find_spec", lambda name: None)
    _, why = P.target(info)
    assert "has no pip" in why and "pipx upgrade sandbox-ai-app" in why
    (next(site.glob("*.dist-info")) / "INSTALLER").write_text("uv\n")
    _, why = P.target(info)
    assert "uv tool upgrade sandbox-ai-app" in why


def test_packages_that_cannot_replace_themselves_say_why(home, tmp_path, monkeypatch):
    info, _ = package_copy(tmp_path, "appimage", monkeypatch)
    assert "no appimage is built for this machine (riscv64)" in P.target(info, machine="riscv64")[1]
    unpacked = tmp_path / "squashfs-root"
    unpacked.mkdir()
    monkeypatch.setenv("SANDBOX_AI_PACKAGE", str(unpacked))
    _, why = P.target(info, machine="aarch64")
    assert "runs from an unpacked AppImage" in why and "download sandbox-ai-aarch64.AppImage" in why
    info, _ = package_copy(tmp_path, "macos-app", monkeypatch)
    moved = {**info, "tree": "/private/var/folders/x/AppTranslocation/1/d/sandbox-ai.app/Contents/Resources/sandbox-ai"}
    assert "drag it into Applications" in P.target(moved, machine="arm64")[1]
    volume = {**info, "tree": "/Volumes/sandbox-ai 1.0.0/sandbox-ai.app/Contents/Resources/sandbox-ai"}
    assert "drag it into Applications" in P.target(volume, machine="arm64")[1]


def test_the_timer_of_a_package_runs_the_file_not_the_copy_of_one_version(home, tmp_path, monkeypatch):
    info, file = package_copy(tmp_path, "pyz", monkeypatch)
    command = U.timer_command(info)
    assert command[1:] == ["-I", str(file), "update", "--scheduled"] and "cache" not in " ".join(command)
    info, file = package_copy(tmp_path, "appimage", monkeypatch)
    assert U.timer_command(info) == [str(file), "cli", "update", "--scheduled"]
    info, bundle = package_copy(tmp_path, "macos-app", monkeypatch)
    assert U.timer_command(info) == [str(bundle / "Contents/MacOS/sandbox-ai"), "update", "--scheduled"]


def test_a_pip_installation_knows_its_version(tmp_path, monkeypatch):
    """Its modules lie in site-packages/sandbox_ai, not in a tree's sandbox/: "unknown" made every release newer."""
    info, site = package_copy(tmp_path, "pip", monkeypatch, "3.2.1")
    assert P.module_version(site / "sandbox_ai") == "3.2.1"
    assert U.tree_version(site) is None
    monkeypatch.setattr(U, "HERE", site / "sandbox_ai")
    assert U.own_version() == "3.2.1"


def test_the_token_never_follows_a_redirect_to_another_host(monkeypatch):
    request = P.urllib.request.Request("https://api.github.com/repos/o/r/releases/assets/1",
                                       headers={"Authorization": "token secret-token"})
    handler = P.SameHostRedirect()
    away = handler.redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/x")
    assert away.get_header("Authorization") is None
    same = handler.redirect_request(request, None, 302, "Found", {}, "https://api.github.com/other")
    assert same.get_header("Authorization") == "token secret-token"
    plain = handler.redirect_request(request, None, 302, "Found", {}, "http://api.github.com/other")
    assert plain.get_header("Authorization") is None
    with pytest.raises(P.UpdateError, match="not followed"):
        handler.redirect_request(request, None, 302, "Found", {}, "file:///etc/passwd")
    monkeypatch.setenv("SANDBOX_AI_TOKEN", "0123456789abcdef")
    with pytest.raises(P.UpdateError, match="refusing to send SANDBOX_AI_TOKEN over plain http"):
        P.open_url("http://git.example.org/api/v1/repos/o/r/releases/latest", "git.example.org")


def test_the_update_of_a_package_takes_its_own_options_and_passes_install_shs_over(home, tmp_path, monkeypatch):
    info, _ = package_copy(tmp_path, "pyz", monkeypatch)
    monkeypatch.setattr(U, "installation", lambda here=None: info)
    asked, said, errors = [], [], []
    monkeypatch.setattr(U, "install_now", lambda *args, **kwargs: asked.append(args[1:6]) or ("1.0.0", "1.0.1"))
    # (sandbox-ai hosts update sends --user and friends to every kind of installation)
    assert U.package_main(["--user", "--prefix", "/x", "--no-app-deps", "--version", "v1.0.1", "--source",
                           "https://git.example.org/o/r", "--allow-downgrade"], said.append, errors.append) == 0
    assert asked == [("1.0.1", "https://git.example.org/o/r", False, True, False)]
    assert "installed 1.0.1 (was 1.0.0)" in said[0] and "sandbox-ai update --rollback" in said[0]
    assert U.package_main(["--frobnicate"], said.append, errors.append) == 2 and "is no option" in errors[-1]
    assert U.package_main(["--version"], said.append, errors.append) == 2 and "needs a value" in errors[-1]
    assert U.package_main(["--version", "one"], said.append, errors.append) == 1 and "X.Y.Z" in errors[-1]
    assert U.package_main(["--source", "ftp://x/o/r"], said.append, errors.append) == 1


def test_the_kind_of_copy_is_read_from_its_files(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "HOMES", ("/nowhere/",))  # (the test's folders are below /root: a HOME there)
    user = make_tree(tmp_path / "home/u/.local/share/sandbox-ai/versions/1.0.0", "1.0.0")
    assert U.installation(user / "sandbox")["kind"] == "user"
    system = make_tree(tmp_path / "opt/lib/sandbox-ai/versions/1.0.0", "1.0.0")
    found = U.installation(system / "sandbox")
    assert found["kind"] == "system" and found["prefix"] == str(tmp_path / "opt")
    checkout = make_tree(tmp_path / "src", "1.0.0")
    (checkout / ".git").mkdir()
    assert U.installation(checkout / "sandbox")["kind"] == "checkout"
    pip = tmp_path / "site" / "sandbox_ai"
    pip.mkdir(parents=True)
    assert U.installation(pip)["kind"] == "pip"
    package = make_tree(tmp_path / "app", "1.0.0")
    (package / "PACKAGING").write_text("appimage\n")
    assert U.installation(package / "sandbox") == {"kind": "package", "tree": str(package), "data": None,
                                                   "prefix": None, "package": "appimage"}


def said(version, status=0):
    """A runner whose every `... --version` says VERSION (the new package's one start before it replaces the old)."""
    calls = []

    def runner(command, **kwargs):
        calls.append([str(word) for word in command])
        return subprocess.CompletedProcess(command, status, f"sandbox-ai {version}\n" if status == 0 else "", "boom\n")
    runner.calls = calls
    return runner


def test_a_new_package_that_does_not_start_or_is_another_version_replaces_nothing(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    info, file = package_copy(tmp_path, "pyz", monkeypatch)
    real = P.module_version  # (this copy: the tree its start unpacked, 1.0.0)
    monkeypatch.setattr(P, "module_version", lambda folder=None: real(folder or Path(info["tree"]) / "sandbox"))
    new = tmp_path / "downloaded.pyz"
    new.write_bytes(b"the new package")
    for runner, words in ((said("2.0.0", status=1), "does not start (exit status 1: boom); nothing replaced"),
                          (said("1.9.9"), "the file of release 2.0.0 says it is sandbox-ai 1.9.9; nothing replaced")):
        with pytest.raises(P.UpdateError, match=words.replace("(", r"\(").replace(")", r"\)")):
            P.replace_file(info, new, "2.0.0", file, runner)
        assert file.read_bytes() == b"the package" and sorted(p.name for p in file.parent.iterdir()) == [file.name]
        assert P.previous(info) is None
    # The good one: started once (isolated, nothing of the running package in its environment), the old one kept.
    runner = said("2.0.0")
    assert P.replace_file(info, new, "2.0.0", file, runner) == ("1.0.0", "2.0.0")
    assert runner.calls[0][1] == "-I" and runner.calls[0][-1] == "--version" and ".sandbox-ai.pyz.update-" in \
        runner.calls[0][2]
    assert file.read_bytes() == b"the new package" and sorted(p.name for p in file.parent.iterdir()) == [file.name]
    kept = P.previous(info)
    assert kept["version"] == "1.0.0" and Path(kept["path"]).read_bytes() == b"the package"
    # This copy (unpacked from the file before) still runs: the file in place is what the next start is.
    assert P.module_version() == "1.0.0" and P.current_version(info) == "2.0.0"
    assert P.rollback(info) == ("2.0.0", "1.0.0") and file.read_bytes() == b"the package"
    assert P.current_version(info) == "1.0.0" and P.previous(info)["version"] == "2.0.0"
    assert P.rollback(info) == ("1.0.0", "2.0.0") and file.read_bytes() == b"the new package", "and forward again"
    file.write_bytes(b"put there by hand")  # (another file in its place: what this copy says of itself counts again)
    assert P.current_version(info) == "1.0.0"


def test_the_macos_app_is_left_as_it_was_when_the_disk_image_is_not_the_release(home, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    info, bundle = package_copy(tmp_path, "macos-app", monkeypatch)
    image = tmp_path / "sandbox-ai-arm64.dmg"
    image.write_bytes(b"image")
    real = P.module_version

    def version_in(folder):
        return real(folder)
    monkeypatch.setattr(P, "module_version", lambda folder=None: "1.0.0" if folder is None else real(folder))
    calls = []

    def machine(holds="2.0.0", attach=0, starts="2.0.0"):
        def runner(command, **kwargs):
            command = [str(word) for word in command]
            calls.append(command[:2])
            if command[1:2] == ["attach"]:
                if attach == 0 and holds:
                    mount = Path(command[command.index("-mountpoint") + 1])
                    make_tree(mount / "sandbox-ai.app/Contents/Resources/sandbox-ai", holds)
                    (mount / "sandbox-ai.app/Contents/MacOS").mkdir()
                    (mount / "sandbox-ai.app/Contents/MacOS/sandbox-ai").write_text("#!/bin/sh\n")
                return subprocess.CompletedProcess(command, attach, "", "hdiutil: attach failed - no mountable file "
                                                                        "systems\n" if attach else "")
            if command[1:2] == ["detach"]:
                import shutil
                shutil.rmtree(command[-1], ignore_errors=True)
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[0] == "cp":
                import shutil
                shutil.copytree(command[2], command[3], symlinks=True)
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 0, f"sandbox-ai {starts}\n", "")
        return runner
    which = lambda name: "/usr/bin/hdiutil" if name == "hdiutil" else None  # noqa: E731 - (no ditto: cp -R)
    before = sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*"))
    for runner, words in ((machine(attach=1), "hdiutil could not open sandbox-ai-arm64.dmg"),
                          (machine(holds="1.5.0"), "holds no sandbox-ai.app of sandbox-ai 2.0.0"),
                          (machine(holds=None), "holds no sandbox-ai.app of sandbox-ai 2.0.0"),
                          (machine(starts="1.0.0"), "says it is sandbox-ai 1.0.0; nothing replaced")):
        with pytest.raises(P.UpdateError, match=words):
            P.replace_app(info, image, "2.0.0", bundle, runner, which=which)
        assert sorted(str(p.relative_to(bundle)) for p in bundle.rglob("*")) == before
        assert sorted(p.name for p in bundle.parent.iterdir()) == ["sandbox-ai.app"], "nothing is left beside the app"
        assert version_in(bundle / "Contents/Resources/sandbox-ai/sandbox") == "1.0.0"
    assert ["/usr/bin/hdiutil", "detach"] in calls, "an image that was attached is detached, whatever happens"
    with pytest.raises(P.UpdateError, match="needs hdiutil"):
        P.replace_app(info, image, "2.0.0", bundle, machine(), which=lambda name: None)
    assert P.replace_app(info, image, "2.0.0", bundle, machine(), which=which) == ("1.0.0", "2.0.0")
    assert version_in(bundle / "Contents/Resources/sandbox-ai/sandbox") == "2.0.0"
    assert sorted(p.name for p in bundle.parent.iterdir()) == ["sandbox-ai.app"]
    assert P.previous(info)["version"] == "1.0.0"
    monkeypatch.setattr(P, "module_version", lambda folder=None: "2.0.0" if folder is None else version_in(folder))
    assert P.rollback(info) == ("2.0.0", "1.0.0")
    assert version_in(bundle / "Contents/Resources/sandbox-ai/sandbox") == "1.0.0"


def test_pip_installs_the_checked_file_and_the_apps_pyside_only_where_it_is(home, tmp_path, monkeypatch):
    info, site = package_copy(tmp_path, "pip", monkeypatch)
    wheel = tmp_path / "sandbox_ai_app-2.0.0-py3-none-any.whl"
    command, extras = P.pip_command(site, wheel)
    assert command[1:4] == ["-I", "-m", "pip"] and command[-1] == str(wheel)
    assert "--no-deps" in command and "--disable-pip-version-check" in command and "--no-input" in command
    assert "--index-url" not in command and "sandbox-ai-app" not in command, "the file, never a name for an index"
    assert extras is None, "no PySide6 in this environment: nothing but the file is installed"
    (site / "PySide6_Essentials-6.12.0.dist-info").mkdir()
    command, extras = P.pip_command(site, wheel)
    assert extras[-1] == f"{wheel}[gui]" and "--no-deps" not in extras
    import site as site_module
    monkeypatch.setattr(site_module, "getusersitepackages", lambda: str(site))
    command, extras = P.pip_command(site, wheel)
    assert command[1] == "-E" and "--user" in command, "a --user installation: -I would hide its folder"


@pytest.mark.skipif(not __import__("shutil").which("ssh-keygen"), reason="ssh-keygen (OpenSSH) is needed")
def test_a_package_takes_only_what_a_trusted_key_signed_as_that_release(tmp_path, monkeypatch):
    """The same rules as install.sh's: once this copy lists release keys, SHA256SUMS must carry a good signature by
    one of them - and it must be the list of the release that is offered: an older signed release served again under
    a newer tag is found out by the bundle its SHA256SUMS lists. The keys are this copy's own, so a release signed
    by a trusted key that lists a new key hands over to it (rotation), as for install.sh's installations."""
    keys = P.load("release_keys")
    made = {}
    for name in ("old", "new", "evil"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(tmp_path / name)],
                       check=True)
        kind, key = (tmp_path / f"{name}.pub").read_text().split()[:2]
        made[name] = (kind, key)

    def sign(data, name):
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(tmp_path / name), "-n", "sandbox-ai-release"],
                              input=data, capture_output=True, check=True).stdout
    sums = f"{'a' * 64}  sandbox-ai-bundle-2.0.0.tar.gz\n{'b' * 64}  sandbox-ai.pyz\n".encode()
    monkeypatch.setattr(P, "trusted_keys", lambda: (keys, [made["old"]]))
    assert P.judge(sums, sign(sums, "old"), "the release 2.0.0") == f"good {keys.fingerprint(made['old'][1])}"
    with pytest.raises(P.UpdateError, match="which is not a trusted release key; nothing updated. Trusted release "
                                            "keys: SHA256:"):
        P.judge(sums, sign(sums, "evil"), "the release 2.0.0")
    with pytest.raises(P.UpdateError, match=r"the release 2.0.0 is not signed \(no SHA256SUMS.sig\)"):
        P.judge(sums, None, "the release 2.0.0")
    with pytest.raises(P.UpdateError, match="does not match SHA256SUMS"):
        P.judge(sums + b"changed", sign(sums, "old"), "the release 2.0.0")
    other = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(tmp_path / "old"), "-n", "git"], input=sums,
                           capture_output=True, check=True).stdout
    with pytest.raises(P.UpdateError, match="the signature is for 'git'"):
        P.judge(sums, other, "the release 2.0.0")
    # Rotation: the copy that came with a release listing both keys takes one signed by the new key alone.
    monkeypatch.setattr(P, "trusted_keys", lambda: (keys, [made["old"], made["new"]]))
    assert P.judge(sums, sign(sums, "new"), "the release 2.0.0").startswith("good SHA256:")
    # What is signed is the list of ONE release: the one whose bundle it names.
    assert P.signed_version(sums, "2.0.0", "the release 2.0.0")[1] == "2.0.0"
    with pytest.raises(P.UpdateError, match="lists the files of sandbox-ai 2.0.0, not those of 9.9.9"):
        P.signed_version(sums, "9.9.9", "the release 9.9.9")  # (2.0.0's signed files, served as 9.9.9)
    with pytest.raises(P.UpdateError, match="lists no sandbox-ai bundle"):
        P.signed_version(f"{'b' * 64}  sandbox-ai.pyz\n".encode(), "2.0.0", "the release 2.0.0")
    with pytest.raises(P.UpdateError, match="is older than the installed 2.0.0, although the source lists it as the "
                                            "newest"):
        P.refuse_older("1.9.0", "2.0.0")
    P.refuse_older("1.9.0", "2.0.0", allow_downgrade=True)
    # A copy that lists no key yet (a fork): checksums only - unless the administrator's policy demands signatures.
    monkeypatch.setattr(P, "trusted_keys", lambda: (keys, []))
    monkeypatch.setattr(P, "policy_requires_signatures", lambda: False)
    assert P.judge(sums, None, "the release 2.0.0") == "unsigned"
    monkeypatch.setattr(P, "policy_requires_signatures", lambda: True)
    with pytest.raises(P.UpdateError, match="requires signed updates"):
        P.judge(sums, sign(sums, "old"), "the release 2.0.0")


def test_the_timers_pip_install_fetches_nothing_from_pypi(home, tmp_path, monkeypatch):
    """update mode auto: nothing but the release's file is fetched - the app's PySide6 stays as it is, and that is
    said with the command that brings it to the release's pins; by hand the pins follow the release."""
    info, site = package_copy(tmp_path, "pip", monkeypatch)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    (site / "PySide6_Essentials-6.12.0.dist-info").mkdir()
    wheel = tmp_path / "sandbox_ai_app-2.0.0-py3-none-any.whl"
    ran = []

    def pip(command, **kwargs):
        ran.append([str(word) for word in command])
        (site / "sandbox_ai/agent_sandbox.py").write_text('__version__ = "2.0.0"\n')
        return subprocess.CompletedProcess(command, 0, "", "")
    real = P.module_version
    monkeypatch.setattr(P, "module_version", lambda folder=None: "1.0.0" if folder is None else real(folder))
    old, new, note = P.replace_pip(info, wheel, "2.0.0", site, pip, extras=False)
    assert (old, new) == ("1.0.0", "2.0.0") and len(ran) == 1 and "--no-deps" in ran[0]
    assert "an update by the timer fetches nothing from PyPI" in note and f"{wheel}[gui]" in note
    old, new, note = P.replace_pip(info, wheel, "2.0.0", site, pip)
    assert len(ran) == 3 and ran[2][-1] == f"{wheel}[gui]" and note is None, "by hand: the pins follow the release"
    # ... and updates.py hands the timer's install on as one (apply(scheduled=True) of update mode auto).
    seen = []
    monkeypatch.setattr(P, "verified_pending", lambda info: {"version": "2.0.0", "file": str(wheel)})
    monkeypatch.setattr(P, "install", lambda info, found, runner, scheduled=False: seen.append(scheduled) or
                        ("1.0.0", "2.0.0", None))
    monkeypatch.setattr(P, "drop_pending", lambda info, environ=None: None)
    U.apply_package(info, "1.0.0", True, pip, lambda line: None)
    U.apply_package(info, "1.0.0", False, pip, lambda line: None)
    assert seen == [True, False]


def test_install_now_checks_fetches_and_installs_in_one_go(home, installed):
    """--install (the app's Install now): the newest signed release through the same signed path as download and
    --apply; nothing when the installed one is the newest; never while something runs unless said."""
    fake = FakeInstaller(installed["data"])
    said = []
    with pytest.raises(U.Busy):
        U.install_now(installed, runner=fake, busy=lambda: ["brisk-otter runs"], out=said.append)
    assert fake.calls == [], "asked what runs before anything is asked of the network"
    assert U.install_now(installed, runner=fake, busy=lambda: [], out=said.append) == ("1.0.0", "9.9.9")
    assert [word for call in fake.calls for word in ("--check", "--fetch", "--verify") if word in call] == \
        ["--check", "--fetch", "--verify"]
    assert U.load_state()["last_install"]["to"] == "9.9.9" and U.load_state()["result"]["available"] == "9.9.9"
    assert U.install_now(installed, runner=FakeInstaller(installed["data"], available="9.9.9"), busy=lambda: [],
                         out=said.append) is None
    assert said[-1] == "sandbox-ai 9.9.9 is already installed and up to date"
    unsigned = FakeInstaller(installed["data"], available="10.0.0", signature="none (the release has no "
                                                                              "SHA256SUMS.sig)")
    with pytest.raises(ValueError, match="10.0.0 is there, but not signed: not offered; nothing installed"):
        U.install_now(installed, runner=unsigned, busy=lambda: [], out=said.append)
    assert unsigned.count("--fetch") == 0


def test_a_system_installation_is_installed_by_hand_by_who_may_write_it_only(home, tmp_path, monkeypatch):
    data = tmp_path / "opt/lib/sandbox-ai"
    for version in ("0.9.0", "1.0.0"):
        make_tree(data / "versions" / version, version)
    os.symlink("versions/1.0.0", data / "current")
    info = {"kind": "system", "tree": str(data / "versions/1.0.0"), "data": str(data), "prefix": str(tmp_path / "opt"),
            "package": None}
    monkeypatch.setattr(U, "own_version", lambda: U.tree_version(data / "current") or "?")
    assert U.self_updating(info) is False, "download and auto stay with a user's own copies"
    assert U.installs_by_hand(info, access=lambda path, mode: False) is False
    assert U.installs_by_hand(info, access=lambda path, mode: True) is True, "root, or whoever may write the prefix"
    monkeypatch.setattr(U, "installs_by_hand", lambda info, access=os.access: False)
    with pytest.raises(ValueError, match="only root changes: sudo sandbox-ai update$"):
        U.install_now(info, busy=lambda: [])
    with pytest.raises(ValueError, match="only root changes: sudo sandbox-ai update --rollback$"):
        U.rollback(info, busy=lambda: [])
    monkeypatch.undo()
    monkeypatch.setattr(U, "own_version", lambda: U.tree_version(data / "current") or "?")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    fake = FakeInstaller(data)
    assert U.install_now(info, runner=fake, busy=lambda: [], out=lambda line: None) == ("1.0.0", "9.9.9")
    assert all(["--system", "--prefix", str(tmp_path / "opt")] == call[-2 - 1:] or "--system" in call
               for call in fake.calls), "as the same kind of installation, under its prefix"


# -- the daily check --------------------------------------------------------------------------------------------------

def test_the_check_runs_once_a_day_and_announces_a_version_once(home, installed):
    fake = FakeInstaller(installed["data"])
    said, tell = told()
    at = local(2026, 11, 18, 10, 0)
    for offset in (0, 3600, 6 * 3600):
        U.scheduled(installed, clock=lambda: at + offset, runner=fake, busy=lambda at: [], tell=tell,
                    out=lambda line: None)
    assert fake.count("--check") == 1, "throttled: one check a day"
    assert said == [("sandbox-ai 9.9.9 is available", f"installed: 1.0.0; update: sandbox-ai update; release notes: "
                                                       f"{U.DEFAULT_SOURCE}/releases/tag/v9.9.9")]
    U.scheduled(installed, clock=lambda: at + 86400, runner=fake, busy=lambda at: [], tell=tell, out=lambda line: None)
    assert fake.count("--check") == 2 and len(said) == 1, "the next day: checked again, the same version not again"
    assert fake.count("--fetch") == 0, "notify downloads nothing"
    command = next(call for call in fake.calls if "--check" in call)
    assert command[command.index("--source") + 1] == U.DEFAULT_SOURCE
    shown = U.status(installed, lambda *a, **k: subprocess.CompletedProcess(a, 1))
    assert shown["result"]["newer"] and shown["result"]["available"] == "9.9.9"
    assert shown["next_check"] == at + 2 * 86400


def test_a_failed_check_is_tried_again_after_an_hour(home, installed):
    fake = FakeInstaller(installed["data"])
    failing = lambda command, **kw: subprocess.CompletedProcess(command, 1, "", "install.sh: cannot reach x: down\n")
    at = local(2026, 11, 18, 10, 0)
    U.scheduled(installed, clock=lambda: at, runner=failing, busy=lambda at: [], tell=told()[1], out=lambda l: None)
    assert U.load_state()["result"]["error"] == "cannot reach x: down"
    U.scheduled(installed, clock=lambda: at + 1800, runner=fake, busy=lambda at: [], tell=told()[1], out=lambda l: None)
    assert fake.count("--check") == 0
    U.scheduled(installed, clock=lambda: at + 3600, runner=fake, busy=lambda at: [], tell=told()[1], out=lambda l: None)
    assert fake.count("--check") == 1


def test_mode_off_checks_nothing(home, installed, systemctl):
    U.set_mode("off", info=installed, out=lambda line: None, run=systemctl[1])
    fake = FakeInstaller(installed["data"])
    assert U.scheduled(installed, runner=fake, tell=told()[1], out=lambda line: None) == 0
    assert fake.calls == []


def test_an_unsigned_or_badly_signed_release_is_never_offered(home, installed, systemctl):
    U.set_mode("download", info=installed, out=lambda line: None, run=systemctl[1])
    for signature in ("BAD: signed by SHA256:evil, which is not a trusted release key", "none (no SHA256SUMS.sig)"):
        (home / "state" / "sandbox-ai" / "update.json").unlink(missing_ok=True)
        fake = FakeInstaller(installed["data"], signature=signature)
        said, tell = told()
        U.scheduled(installed, runner=fake, busy=lambda at: [], tell=tell, out=lambda line: None)
        assert said == [] and fake.count("--fetch") == 0
        assert "not offered" in U.result_text(U.status(installed, systemctl[1])["result"])


# -- download ---------------------------------------------------------------------------------------------------------

def test_download_fetches_and_checks_it_but_switches_nothing(home, installed, systemctl):
    U.set_mode("download", source="https://git.example/o/r", info=installed, out=lambda line: None,
               run=systemctl[1])
    fake = FakeInstaller(installed["data"])
    U.scheduled(installed, runner=fake, busy=lambda at: [], tell=told()[1], out=lambda line: None)
    fetch = next(call for call in fake.calls if "--fetch" in call)
    assert fetch[fetch.index("--fetch") + 1] == str(Path(installed["data"]) / "update-pending")
    assert fetch[fetch.index("--version") + 1] == "9.9.9"
    assert fetch[fetch.index("--source") + 1] == "https://git.example/o/r"
    assert U.pending_release(installed)["version"] == "9.9.9"
    assert U.own_version() == "1.0.0", "nothing switched"
    U.scheduled(installed, runner=fake, busy=lambda at: [], tell=told()[1], out=lambda line: None)
    assert fake.count("--fetch") == 1, "fetched once"
    old, new = U.apply(installed, runner=fake, busy=lambda: [])
    assert (old, new) == ("1.0.0", "9.9.9") and U.own_version() == "9.9.9"
    assert U.pending_release(installed) is None and U.load_state()["last_install"]["by"] == "hand"


def test_download_refuses_a_bad_signature(home, installed, systemctl):
    U.set_mode("download", info=installed, out=lambda line: None, run=systemctl[1])
    fake = FakeInstaller(installed["data"])
    fake.fetch_error = "signed by SHA256:evil, which is not a trusted release key; nothing downloaded"
    assert U.scheduled(installed, runner=fake, busy=lambda at: [], tell=told()[1], out=lambda line: None) == 1
    assert U.pending_release(installed) is None
    assert not (Path(installed["data"]) / "update-pending").exists()
    assert "not a trusted release key" in U.load_state()["download_error"]["error"]
    with pytest.raises(ValueError, match="nothing is downloaded"):
        U.apply(installed, runner=fake, busy=lambda: [])


# -- auto -------------------------------------------------------------------------------------------------------------

def test_auto_installs_only_in_the_window_and_while_nothing_runs(home, installed, systemctl):
    U.set_mode("auto", "03:00-05:00", info=installed, out=lambda line: None, run=systemctl[1])
    fake = FakeInstaller(installed["data"])
    said, tell = told()
    noon = local(2026, 11, 18, 12, 0)
    U.scheduled(installed, clock=lambda: noon, runner=fake, busy=lambda at: [], tell=tell, out=lambda line: None)
    assert U.own_version() == "1.0.0" and U.pending_release(installed), "outside the window: only downloaded"
    lines = []
    night = local(2026, 11, 19, 3, 30)
    U.scheduled(installed, clock=lambda: night, runner=fake, busy=lambda at: ["brisk-otter-lantern runs"],
                tell=tell, out=lines.append)
    assert U.own_version() == "1.0.0"
    waiting = U.load_state()["waiting"]
    assert waiting["reasons"] == ["brisk-otter-lantern runs"] and waiting["next"] == night + 900
    assert "9.9.9 waits: brisk-otter-lantern runs; tried again at 2026-11-19 03:45" in lines[-1]
    last = local(2026, 11, 19, 4, 50)
    U.scheduled(installed, clock=lambda: last, runner=fake, busy=lambda at: ["x runs"], tell=tell, out=lines.append)
    assert U.load_state()["waiting"]["next"] == local(2026, 11, 20, 3, 0) and "the next window" in lines[-1]
    U.scheduled(installed, clock=lambda: local(2026, 11, 20, 3, 0), runner=fake, busy=lambda at: [], tell=tell,
                out=lines.append)
    assert U.own_version() == "9.9.9" and "waiting" not in U.load_state()
    assert said[-1][0] == "sandbox-ai updated: 1.0.0 -> 9.9.9"
    assert f"{U.DEFAULT_SOURCE}/releases/tag/v9.9.9" in said[-1][1]
    install = next(call for call in fake.calls if "--from" in call)
    assert "--verify" in install and "--no-app-deps" in install, "checked again; nothing from PyPI unasked"


def test_auto_never_downgrades(home, installed, systemctl):
    U.set_mode("auto", info=installed, out=lambda line: None, run=systemctl[1])
    fake = FakeInstaller(installed["data"], available="0.9.5")
    U.scheduled(installed, clock=lambda: local(2026, 11, 18, 3, 30), runner=fake, busy=lambda at: [],
                tell=told()[1], out=lambda line: None)
    assert fake.count("--fetch") == 0 and fake.count("--from") == 0 and U.own_version() == "1.0.0"
    # A bundle older than the installed version left in the folder: refused and removed.
    folder = Path(installed["data"]) / "update-pending"
    folder.mkdir()
    for name in ("sandbox-ai-bundle-0.9.5.tar.gz", "SHA256SUMS", "SHA256SUMS.sig"):
        (folder / name).write_text(name)
    with pytest.raises(ValueError, match="never a downgrade"):
        U.apply(installed, runner=fake, busy=lambda: [])
    assert not folder.exists() and fake.count("--from") == 0


def test_what_runs_holds_an_install_back(home, installed):
    (Path(installed["data"]) / "update-pending").mkdir()
    for name in ("sandbox-ai-bundle-9.9.9.tar.gz", "SHA256SUMS", "SHA256SUMS.sig"):
        (Path(installed["data"]) / "update-pending" / name).write_text(name)
    fake = FakeInstaller(installed["data"])
    with pytest.raises(U.Busy, match="the MCP server mcp-files runs"):
        U.apply(installed, runner=fake, busy=lambda: ["the MCP server mcp-files runs"])
    assert U.apply(installed, runner=fake, busy=lambda: ["x runs"], while_running=True) == ("1.0.0", "9.9.9")


def test_busy_reasons_name_runs_servers_and_schedules_due_soon(home, monkeypatch):
    starts, schedule = U.load_module("starts"), U.load_module("schedule")
    monkeypatch.setattr(starts, "entries", lambda include_all=False, prune=True: [
        {"name": "brisk-otter", "kind": "session", "state": "running"},
        {"name": "kept-one", "kind": "session", "state": "stopped"},
        {"name": "paused-one", "kind": "run", "state": "paused"},
        {"name": "mcp-files", "kind": "mcp", "state": "running"}])
    at = local(2026, 11, 18, 2, 30)
    monkeypatch.setattr(schedule, "names", lambda: ["nightly", "weekly"])
    monkeypatch.setattr(schedule, "load", lambda name: {"name": name, "timing": {"at": "x"}, "paused": False})
    monkeypatch.setattr(schedule, "running", lambda name: False)
    monkeypatch.setattr(schedule, "next_due", lambda doc, after: at + (1800 if doc["name"] == "nightly" else 86400))
    assert U.busy_reasons(at) == ["brisk-otter runs", "the MCP server mcp-files runs",
                                  "the schedule nightly starts a run at 03:00"]
    monkeypatch.setattr(starts, "entries", lambda include_all=False, prune=True: 1 / 0)
    assert U.busy_reasons(at)[0].startswith("what runs could not be read"), "unknown counts as running"


# -- rollback ---------------------------------------------------------------------------------------------------------

def test_rollback_goes_back_to_the_version_kept_before(home, installed):
    assert U.rollback_target(installed) == "0.9.0"
    with pytest.raises(U.Busy):
        U.rollback(installed, busy=lambda: ["x runs"])
    assert U.rollback(installed, busy=lambda: []) == ("1.0.0", "0.9.0")
    assert os.readlink(Path(installed["data"]) / "current") == "versions/0.9.0"
    state = U.load_state()
    assert state["skip_version"] == "1.0.0" and state["last_install"]["by"] == "rollback"
    # download and auto leave the version gone back from alone; a newer one is taken.
    fake = FakeInstaller(installed["data"], available="1.0.0")
    U.save_settings({"update_mode": "download"})
    U.scheduled(installed, runner=fake, busy=lambda at: [], tell=told()[1], out=lambda line: None)
    assert fake.count("--fetch") == 0
    U.rollback(installed, busy=lambda: [])  # (and forward again: the other kept one)
    assert os.readlink(Path(installed["data"]) / "current") == "versions/1.0.0"


def test_rollback_without_another_version_says_so(home, installed):
    import shutil
    shutil.rmtree(Path(installed["data"]) / "versions" / "0.9.0")
    with pytest.raises(ValueError, match="no other version is kept"):
        U.rollback(installed, busy=lambda: [])
    with pytest.raises(ValueError, match="only an installation made by install.sh"):
        U.rollback({"kind": "checkout", "tree": "/x", "data": None, "prefix": None, "package": None}, busy=lambda: [])
    with pytest.raises(ValueError, match="this copy does not replace itself: /x holds no pip installation"):
        U.rollback({"kind": "pip", "tree": "/x", "data": None, "prefix": None, "package": None}, busy=lambda: [])


# -- the command line's hint ------------------------------------------------------------------------------------------

def test_the_hint_is_one_line_on_stderr_at_most_once_a_day(home, installed, monkeypatch):
    import io
    monkeypatch.setattr(U, "installation", lambda here=None: installed)
    at = local(2026, 11, 18, 10, 0)
    U.save_state({"last_check": at, "result": {"available": "9.9.9", "signature": "good", "installed": "1.0.0"}})
    stream, started = io.StringIO(), []
    hint = lambda command, argv, when, interactive=True: U.cli_hint(
        command, argv, stream, lambda: when, interactive, lambda: started.append(when))
    assert hint("ps", [], at) == "sandbox-ai: 9.9.9 is available (installed: 1.0.0): sandbox-ai update"
    assert hint("ps", [], at + 3600) is None, "once a day"
    assert hint("list", ["--json"], at + 86400) is None and hint("ps", [], at + 86400, False) is None
    assert hint("update", [], at + 86400) is None
    assert hint("ps", [], at + 86400) is not None and stream.getvalue().count("\n") == 2
    assert started == [at + 86400], "no timer: the day's check is started in the background, once"
    U.save_settings({"update_mode": "off"})
    assert hint("ps", [], at + 3 * 86400) is None


# -- the notify channels ----------------------------------------------------------------------------------------------

def test_the_notify_event_update():
    spec = importlib.util.spec_from_file_location("sandbox_notify_for_updates_test", ROOT / "sandbox" / "notify.py")
    notify = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(notify)
    assert "update" in notify.EVENTS and "update" not in notify.DEFAULT_EVENTS
    assert notify.check_events(["update", "alarms"]) == ["alarms", "update"]
    title, text, _ = notify.render({"detail": "normal"}, [{"event": "update", "what": "sandbox-ai 9.9.9 is available",
                                                           "time": 0}], host="box")
    assert title == "sandbox-ai: update" and text.startswith("sandbox-ai 9.9.9 is available")
    title, text, _ = notify.render({"detail": "minimal"}, [{"event": "update", "time": 0}], host="box")
    assert text == "a sandbox-ai update on box"


def test_download_and_auto_never_start_a_check_from_a_command(home, installed, monkeypatch):
    """Without a timer only notify's check starts from a command on a terminal: an install never begins from a
    command that may be a start itself."""
    import io
    monkeypatch.setattr(U, "installation", lambda here=None: installed)
    U.save_settings({"update_mode": "auto"})
    started = []
    U.cli_hint("ps", [], io.StringIO(), lambda: local(2026, 11, 18, 3, 30), True, lambda: started.append(1))
    assert started == []


def test_the_command_line(home):
    """sandbox-ai update mode through main.py: its help, the mode shown and set (the timer refused here: no systemctl
    on PATH, said as a note), wrong words refused; update --apply --help is update's help."""
    env = {**os.environ, "PATH": "/nonexistent", "HOME": str(home / "home")}
    run = lambda *args: subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), *args],
                                       capture_output=True, text=True, env=env, timeout=120)
    shown = run("update", "mode", "--help")
    assert shown.returncode == 0 and "Usage:  sandbox-ai update mode [MODE] [OPTIONS]" in shown.stdout
    assert "--rollback" in run("update", "--apply", "--help").stdout
    said = run("update", "mode")
    assert said.returncode == 0 and "mode: notify (the default)" in said.stdout
    made = run("update", "mode", "notify", "--json")
    data = json.loads(made.stdout)
    assert made.returncode == 0 and data["mode"] == "notify" and data["chosen"] is True
    assert any("no timer" in line for line in data["said"]), "no systemctl here: said, the mode still set"
    wrong = run("update", "mode", "sometimes")
    assert wrong.returncode == 2
    assert run("update", "--apply", "--now").returncode == 2
    refused = run("update", "--rollback")
    assert refused.returncode == 1 and "only an installation made by install.sh" in refused.stderr


def test_a_failed_download_or_install_is_not_tried_at_every_tick(home, installed, systemctl):
    U.set_mode("auto", info=installed, out=lambda line: None, run=systemctl[1])
    fake = FakeInstaller(installed["data"])
    fake.fetch_error = "cannot reach git.example: down"
    at = local(2026, 11, 18, 12, 0)
    for offset in (0, 900, 1800):
        U.scheduled(installed, clock=lambda: at + offset, runner=fake, busy=lambda at: [], tell=told()[1],
                    out=lambda line: None)
    assert fake.count("--fetch") == 1, "tried again an hour later, not every 15 minutes"
    U.scheduled(installed, clock=lambda: at + 3600, runner=fake, busy=lambda at: [], tell=told()[1],
                out=lambda line: None)
    assert fake.count("--fetch") == 2
    fake.fetch_error = None
    calls = []

    def runner(command, **kwargs):
        if "--from" in command:
            calls.append(command)
            return subprocess.CompletedProcess(command, 1, "", "install.sh: error: checksum mismatch\n")
        return fake(command, **kwargs)
    said, tell = told()
    night = local(2026, 11, 19, 3, 0)
    for offset in (0, 900, 1800):
        U.scheduled(installed, clock=lambda: night + offset, runner=runner, busy=lambda at: [], tell=tell,
                    out=lambda line: None)
    assert len(calls) == 1 and [title for title, _ in said] == ["sandbox-ai update failed"], \
        "a failed install: told once, tried again the next day"


def test_one_timer_run_at_a_time(home, installed):
    schedule = U.load_module("schedule")
    with schedule.Lock(U.state_path().with_name("update.lock")):
        lines = []
        fake = FakeInstaller(installed["data"])
        assert U.scheduled(installed, runner=fake, tell=told()[1], out=lines.append) == 0
    assert lines == ["another update run is under way: nothing done"] and fake.calls == []
