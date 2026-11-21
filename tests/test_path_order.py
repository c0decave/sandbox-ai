"""Which sandbox-ai PATH finds first when there are several installations (sandbox/path_order.py: install.sh's notes,
sandbox-ai doctor). The user's case (2026-11-18): /usr/local/bin/sandbox-ai-app of a system installation (0.1.97) came
before ~/.local/bin, where `install.sh --from .` had put 0.1.99 - the shell never ran it."""
import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


P = load("sandbox_path_order_test", ROOT / "sandbox/path_order.py")
D = load("sandbox_doctor_path_order_test", ROOT / "sandbox/doctor.py")


def installation(data, version, bin_dir, commands=("sandbox-ai", "sandbox-ai-update", "sandbox-ai-app")):
    """DATA/versions/VERSION as install.sh makes it (the tree, current, links in BIN_DIR); the tree."""
    tree = data / "versions" / version
    (tree / "sandbox").mkdir(parents=True)
    (tree / "sandbox" / "agent_sandbox.py").write_text(f'"""sandbox-ai"""\n__version__ = "{version}"\n')
    for name in ("sandbox-ai", "sandbox-ai-app", "install.sh"):
        (tree / name).write_text("#!/bin/sh\n")
        (tree / name).chmod(0o755)
    (data / "current").symlink_to(f"versions/{version}")
    bin_dir.mkdir(parents=True, exist_ok=True)
    for command in commands:
        target = "install.sh" if command == "sandbox-ai-update" else command
        (bin_dir / command).symlink_to(data / "current" / target)
    return tree


def two(tmp_path, system="0.1.97", user="0.1.99"):
    prefix, home = tmp_path / "usr-local", tmp_path / "home"
    installation(prefix / "lib" / "sandbox-ai", system, prefix / "bin")
    mine = installation(home / ".local" / "share" / "sandbox-ai", user, home / ".local" / "bin")
    return prefix, home, mine


def test_a_system_installation_first_in_path_is_named_with_both_versions_and_the_ways_out(tmp_path):
    prefix, home, mine = two(tmp_path)
    path = os.pathsep.join([str(prefix / "bin"), "/usr/bin", str(home / ".local" / "bin")])
    notes = P.install_notes(home / ".local/share/sandbox-ai/current", P.COMMANDS, path, str(home / ".local/bin"), ".")
    assert len(notes) == 1, notes
    note = notes[0]
    assert note.startswith("warning: your PATH finds sandbox-ai, sandbox-ai-update, sandbox-ai-app in "
                           f"{prefix / 'bin'} first - sandbox-ai 0.1.97 (a system installation in {prefix})")
    assert f"before the 0.1.99 installed now in {home / '.local/bin'}" in note
    assert f"sudo {prefix}/lib/sandbox-ai/current/install.sh --uninstall --system --prefix {prefix}" in note
    assert f"sudo ./install.sh --from . --system --prefix {prefix}" in note
    assert f"put {home / '.local/bin'} before {prefix / 'bin'} in PATH" in note
    # The user's own first: nothing to say.
    path = os.pathsep.join([str(home / ".local" / "bin"), str(prefix / "bin")])
    assert P.install_notes(mine, P.COMMANDS, path, str(home / ".local/bin")) == []


def test_the_default_prefix_needs_no_prefix_option_and_a_download_updates_with_its_own_installer():
    found = P.Found("sandbox-ai", "/usr/local/bin/sandbox-ai", "/usr/local/lib/sandbox-ai/versions/0.1.97", "0.1.97",
                    "system", "/usr/local")
    text = P.ways_out(found)
    assert "sudo /usr/local/lib/sandbox-ai/current/install.sh --uninstall --system)" in text
    assert "update it (sudo /usr/local/lib/sandbox-ai/current/install.sh --system" in text and "--prefix" not in text
    assert "sudo /src/sb/install.sh --from /src/sb --system" in P.ways_out(found, "/src/sb")


def test_a_pip_script_and_a_foreign_file_first_in_path(tmp_path):
    first, mine_bin = tmp_path / "first", tmp_path / "home/.local/bin"
    mine = installation(tmp_path / "home/.local/share/sandbox-ai", "0.1.99", mine_bin)
    first.mkdir()
    (first / "sandbox-ai").write_text("#!/usr/bin/python3\nfrom sandbox.main import cli\ncli()\n")
    (first / "sandbox-ai-app").write_text("#!/bin/sh\necho mine\n")
    for name in ("sandbox-ai", "sandbox-ai-app"):
        (first / name).chmod(0o755)
    notes = P.install_notes(mine, P.COMMANDS, f"{first}:{mine_bin}", str(mine_bin))
    assert len(notes) == 2
    assert "finds sandbox-ai in" in notes[0] and "a pip installation" in notes[0] and "pip uninstall" in notes[0]
    assert "finds sandbox-ai-app in" in notes[1] and "not sandbox-ai's" in notes[1]
    assert f"rename or remove {first / 'sandbox-ai-app'}" in notes[1]


def test_the_cli_install_sh_runs(tmp_path, capsys):
    prefix, home, mine = two(tmp_path)
    assert P.main(["--mine", str(mine), "--folder", str(home / ".local/bin"), "--path",
                   f"{prefix / 'bin'}:{home / '.local/bin'}", "sandbox-ai-app"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("warning: your PATH finds sandbox-ai-app in") and out.count("\n") == 1
    assert P.main(["sandbox-ai"]) == 2


def test_doctor_names_the_installation_first_in_path_and_the_one_installed_later(tmp_path):
    prefix, home, mine = two(tmp_path)
    path = f"{prefix / 'bin'}:/usr/bin:{home / '.local/bin'}"
    status, title, detail, hint = D.check_path_order(mine, path)
    assert (status, title) == ("warn", "Commands in PATH")
    assert (f"sandbox-ai first in PATH is 0.1.97 from {prefix / 'bin/sandbox-ai'}, but 0.1.99 is installed in "
            f"{home / '.local/share/sandbox-ai'}") in detail
    assert f"sandbox-ai-app first in PATH is 0.1.97 from {prefix / 'bin/sandbox-ai-app'}" in detail
    assert f"the older one, 0.1.97 in {prefix}" in hint and "--uninstall --system" in hint
    assert f"put {home / '.local/bin'} first in PATH" in hint and hint.count("the older one") == 1
    # Started by its full path with only the other one in PATH: this one counts too.
    assert D.check_path_order(mine, str(prefix / "bin"))[0] == "warn"
    assert D.check_path_order(mine, f"{home / '.local/bin'}")[0] == "ok"
    # The newer one first: the older one later in PATH is still named (it is stale), without moving PATH.
    status, _, detail, hint = D.check_path_order(mine, f"{home / '.local/bin'}:{prefix / 'bin'}")
    assert status == "warn" and "first in PATH is 0.1.99" in detail and "first in PATH" not in hint


def test_doctor_is_content_with_one_version(tmp_path):
    prefix, home, mine = two(tmp_path, system="0.1.99")
    assert D.check_path_order(mine, f"{prefix / 'bin'}:{home / '.local/bin'}")[0] == "ok"
    assert "Commands in PATH" in dict(D.GROUPS)["System"]
