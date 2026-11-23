"""sandbox-ai doctor --json: one object per check, with the area of the table it belongs to (the app groups by it)."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_doctor_json", ROOT / "sandbox/doctor.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


def test_json_has_status_check_detail_hint_and_group(monkeypatch, capsys):
    results = [("ok", "Python", "3.12", ""), ("warn", "Image claude", "missing", "sandbox-ai build claude"),
               ("fail", "Login codex", "no login", "sandbox-ai login codex"), ("ok", "Project configuration", "-", "")]
    monkeypatch.setattr(d, "checks", lambda backend, podman_url=None: results)
    assert d.cli(["--json"]) == 1, "a failed check: status 1"
    out = json.loads(capsys.readouterr().out)
    fix = out[1].pop("fix")  # what fixes it, as a start's fix plan says it (sandbox/fixes.py)
    assert out[1] == {"status": "warn", "check": "Image claude", "detail": "missing", "hint": "sandbox-ai build claude",
                      "group": "Engine"}
    assert fix["commands"] == [["sandbox-ai", "build", "claude"]] and fix["verdict"] == "offered"
    assert out[2]["fix"]["commands"] == [["sandbox-ai", "login", "codex"]] and out[2]["fix"]["terminal"] is True
    assert "fix" not in out[0] and "fix" not in out[3]
    assert [entry["group"] for entry in out] == ["System", "Engine", "Agents", "Project"]
    monkeypatch.setattr(d, "checks", lambda backend, podman_url=None: results[:1])
    assert d.cli(["--json"]) == 0


def only_the_installation(monkeypatch):
    """Every check but the first (this sandbox-ai) a stand-in: nothing of the host is asked."""
    for name in [n for n in dir(d) if n.startswith("check_") and n != "check_installation"]:
        monkeypatch.setattr(d, name, lambda *a, name=name, **k: ("ok", "Ordinary user" if name == "check_user" else
                                                                 name, "x", ""))


def test_the_first_entry_is_sandbox_ai_itself_with_its_version(monkeypatch, capsys):
    """doctor --json starts with sandbox-ai's own version, where it is installed and its Python (id
    "sandbox-ai-version"): the Hosts page heads a host's doctor with it."""
    only_the_installation(monkeypatch)
    d.cli(["--json"])
    out = json.loads(capsys.readouterr().out)
    first = out[0]
    assert first["id"] == "sandbox-ai-version" and first["check"] == "sandbox-ai" and first["group"] == "System"
    assert first["version"] == d.launcher().__version__ and first["detail"].startswith(first["version"] + ", ")
    assert first["kind"] in ("system", "user", "package", "pip", "checkout", "unknown") and first["path"]
    assert first["python"].count(".") == 2 and "Python " + first["python"] in first["detail"]
    assert first["status"] in ("ok", "warn") and not any("id" in entry for entry in out[1:])


def test_the_text_output_names_sandbox_ai_first(monkeypatch, capsys):
    only_the_installation(monkeypatch)
    d.cli(["--plain", "--no-color"])
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert "sandbox-ai" in lines[0] and d.launcher().__version__ in lines[0]
    d.cli(["--no-color"])
    table = capsys.readouterr().out.splitlines()
    rows = [line for line in table if line.startswith("  ") and line.split()[0] in ("ok", "WARN", "FAIL", "--")]
    assert rows[0].split()[1] == "sandbox-ai" and d.launcher().__version__ in rows[0]


def test_where_sandbox_ai_is_installed_is_read_as_the_hosts_list_reads_it(tmp_path, monkeypatch):
    """A system installation with its prefix, the user's own, a package, pip and a checkout (a warning); no network."""
    # A lib/sandbox-ai below a home folder is a user's own (hosts.HOMES): pytest's folder may be under /root or
    # /home (--basetemp), where the system installation below would count as the user's - not here.
    hosts = d.hosts_module()
    monkeypatch.setattr(hosts, "HOMES", tuple(h for h in hosts.HOMES if not str(tmp_path).startswith(h)))
    def place(path, install=True, packaging=None, git=False):
        path.mkdir(parents=True)
        if install:
            (path / "install.sh").write_text("#!/bin/sh\n")
        if packaging:
            (path / "PACKAGING").write_text(packaging)
        if git:
            (path / ".git").mkdir()
        return path
    system = d.installation_facts(place(tmp_path / "usr/local/lib/sandbox-ai/versions/0.1.80"))
    assert system["kind"] == "system" and system["prefix"] == str(tmp_path / "usr/local")
    status, title, detail, hint = d.check_installation(tmp_path / "usr/local/lib/sandbox-ai/versions/0.1.80")
    assert status == "ok" and title == "sandbox-ai" and not hint
    assert f"system installation in {tmp_path}/usr/local ({tmp_path}/usr/local/lib/sandbox-ai/versions/0.1.80)" in detail
    user = d.installation_facts(place(tmp_path / "home/.local/share/sandbox-ai/versions/0.1.80"))
    assert user["kind"] == "user" and user["prefix"] is None
    assert "the user's own installation" in d.where_text(user)
    package = d.installation_facts(place(tmp_path / "pyz", install=True, packaging="pyz\n"))
    assert package["kind"] == "package" and d.where_text(package).startswith("a package (pyz)")
    assert d.installation_facts(place(tmp_path / "site/sandbox", install=False))["kind"] == "pip"
    place(tmp_path / "repo", git=True)
    assert d.installation_facts(tmp_path / "repo")["kind"] == "checkout"
    status, _, detail, hint = d.check_installation(tmp_path / "repo")
    assert status == "warn" and "a checkout" in detail and "install.sh --from ." in hint


def update_status(**changes):
    base = {"mode": "notify", "chosen": False, "timer": {"installed": False, "active": None, "kind": "systemd"},
            "last_check": None, "result": None, "waiting": None, "downloaded": None, "self_updating": True,
            "manual": None}
    return {**base, **changes}


def test_the_update_mode_and_its_timer_are_reported():
    """sandbox-ai update mode in the doctor: the mode, its timer (download and auto need it; one not active is a
    warning), the last check and a newer release with the way to it."""
    status, title, detail, hint = d.check_updates(update_status())
    assert (status, title) == ("ok", "Updates") and "mode notify (the default)" in detail and "no timer" in detail
    assert d.group_of(title) == "System"
    assert d.check_updates(update_status(mode="off", chosen=True))[2] == "mode off: nothing is checked unless asked"
    status, _, detail, hint = d.check_updates(update_status(mode="auto", chosen=True))
    assert status == "warn" and hint == "make the timer: sandbox-ai update mode auto"
    on = {"installed": True, "active": False, "kind": "systemd"}
    status, _, detail, hint = d.check_updates(update_status(mode="download", chosen=True, timer=on))
    assert status == "warn" and "timer sandbox-ai-update NOT active" in detail and "update mode download" in hint
    newer = {"available": "9.9.9", "installed": "1.0.0", "signature": "good", "signer": "SHA256:x", "newer": True}
    status, _, detail, hint = d.check_updates(update_status(
        mode="download", chosen=True, timer={**on, "active": True}, last_check=1, result=newer, downloaded="9.9.9"))
    assert status == "warn" and "9.9.9 is available (installed: 1.0.0), signed by SHA256:x" in detail
    assert hint == "a newer release is there: sandbox-ai update --apply"
    status, _, _, hint = d.check_updates(update_status(result=newer, self_updating=False,
                                                       manual="pipx upgrade sandbox-ai-app"))
    assert hint == "a newer release is there: pipx upgrade sandbox-ai-app"
