"""Hermes' telemetry cannot be turned on from inside the sandbox either (sandbox/hermes_guard.py, sandbox/hermes.py:
guard_files, guard_mounts; the user's rule "Agents: telemetry ALWAYS off - never switchable, not even from inside the
sandbox").

Regression (2026-11-15, found in review): telemetry was pinned off in Hermes' read-only managed configuration, the
proxy refused its hosts and its consent question was answered "No thanks" before every start - but typing
`hermes setup telemetry` in the running sandbox (or the TUI's or dashboard's consent switch, `hermes config edit`,
any editor) turned local counting on for the session: Hermes v0.22.5 reads its shared-metrics switches only from the
profile's own writable ~/.hermes/config.yaml, never from the managed scope. HERMES_MANAGED_DIR or PYTEST_CURRENT_TEST
in the environment took the managed configuration away altogether. Now a guard in Hermes' own Python (a read-only
.pth and module in its site-packages) refuses those commands and variables with one message and keeps every switch
off in every read of the configuration, whatever a file says."""
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load("hermes_guard_tests", "sandbox/hermes_guard.py")
hermes = load("hermes_guard_hermes_tests", "sandbox/hermes.py")
MESSAGE = "sandbox-ai: Hermes' telemetry is always off in sandbox-ai and cannot be turned on"


# --- the guard's decisions --------------------------------------------------------------------------------------------

def test_the_message_is_the_one_the_user_reads():
    assert guard.MESSAGE == MESSAGE


@pytest.mark.parametrize("args, what", [
    (["setup", "telemetry"], "hermes setup telemetry"),
    (["-p", "work", "setup", "--quick", "telemetry"], "hermes setup telemetry"),
    (["config", "set", "telemetry.shared_metrics.enabled", "true"], "hermes config set telemetry.shared_metrics.enabled"),
    (["config", "set", "telemetry.shared_metrics.send", "true"], "hermes config set telemetry.shared_metrics.send"),
    (["config", "set", "--force", "Telemetry.Shared_Metrics.Endpoint=http://x"],
     "hermes config set telemetry.shared_metrics.endpoint"),
    (["config", "set", "telemetry", "{}"], "hermes config set telemetry"),
    (["config", "unset", "telemetry.shared_metrics.enabled"], "hermes config unset telemetry.shared_metrics.enabled"),
    (["config", "set", "monitoring.export.otlp.enabled", "true"], "hermes config set monitoring.export.otlp.enabled"),
    (["config", "set", "computer_use.cua_telemetry", "true"], "hermes config set computer_use.cua_telemetry"),
])
def test_every_command_that_turns_telemetry_on_is_refused(args, what):
    assert guard.refused(args)[0] == what


@pytest.mark.parametrize("args", [
    [], ["setup"], ["setup", "model"], ["config", "set", "model.default", "x"], ["config", "get", "telemetry"],
    ["config", "edit"], ["config", "set", "computer_use.permission_mode", "bounded"],
    ["config", "set", "telemetry_notes", "x"],  # only the keys themselves and what is below them
    ["--", "setup", "telemetry"], ["-z", "setup telemetry"],  # after --, and a prompt that names it
])
def test_everything_else_runs(args):
    assert guard.refused(args) is None


def test_variables_that_turn_telemetry_on_or_take_the_pins_away_are_refused():
    assert guard.refused_environment({}) is None
    assert guard.refused_environment(dict(guard.PINNED_ENV)) is None
    assert guard.refused_environment({"CUA_DRIVER_RS_TELEMETRY_ENABLED": "1"})[0] == "CUA_DRIVER_RS_TELEMETRY_ENABLED=1"
    assert guard.refused_environment({"OTEL_SDK_DISABLED": "false"})[0] == "OTEL_SDK_DISABLED=false"
    assert guard.refused_environment({"DO_NOT_TRACK": ""})[0] == "DO_NOT_TRACK="
    assert guard.refused_environment({"HERMES_MANAGED_DIR": "/tmp/empty"})[0] == "HERMES_MANAGED_DIR=/tmp/empty"
    assert guard.refused_environment({"PYTEST_CURRENT_TEST": ""})[0] == "PYTEST_CURRENT_TEST="
    assert guard.refused_environment({"HERMES_MANAGED_DIR": " "}) is None  # Hermes ignores an empty one


@pytest.mark.parametrize("argv, args", [
    (["/opt/hermes/venv/bin/python", "-m", "hermes_cli.main", "setup", "telemetry"], ["setup", "telemetry"]),
    (["python", "-Im", "hermes_cli.main", "x"], ["x"]),
    (["python", "-mhermes_cli.main"], []),
    (["python", "-X", "dev", "-W", "ignore", "-m", "hermes_cli", "y"], ["y"]),
    (["python", "--check-hash-based-pycs", "never", "/opt/hermes/src/hermes_cli/main.py", "z"], ["z"]),
    (["python", "-m", "tui_gateway.entry"], None),
    (["python", "-I", "/etc/hermes/sandbox-ai-telemetry-answer.py"], None),
    (["python", "-c", "import hermes_cli.main", "setup", "telemetry"], None),
    (["python", "-Ic", "pass"], None),
    (["python"], None), ([], None),
])
def test_a_hermes_command_line_is_told_from_any_other_python(argv, args):
    assert guard.cli_args(argv) == args


def test_every_switch_reads_off_and_nothing_else_changes():
    config = {"telemetry": {"shared_metrics": {"enabled": True, "send": "yes", "offer_version": 2}},
              "monitoring": {"gateway_health_export": {"enabled": 1}, "export": {"otlp": {"enabled": True,
                                                                                          "endpoint": "e"}}},
              "computer_use": {"cua_telemetry": True, "permission_mode": "bounded"}, "model": {"default": "m"}}
    assert guard.scrub(config) is config
    assert config == {"telemetry": {"shared_metrics": {"enabled": False, "send": False, "offer_version": 2}},
                      "monitoring": {"gateway_health_export": {"enabled": False},
                                     "export": {"otlp": {"enabled": False, "endpoint": "e"}}},
                      "computer_use": {"cua_telemetry": False, "permission_mode": "bounded"}, "model": {"default": "m"}}
    # Nothing added where nothing was (an empty file stays empty: Hermes tells a fresh install by it).
    assert guard.scrub({}) == {} and guard.scrub({"telemetry": None}) == {"telemetry": None}
    assert guard.scrub(None) is None and guard.scrub([1]) == [1]


def test_the_variables_are_pinned_and_the_scope_variables_dropped():
    environ = {"DO_NOT_TRACK": "0", "HERMES_MANAGED_DIR": "/x", "PYTEST_CURRENT_TEST": "t", "KEEP": "1"}
    guard.pin_environment(environ)
    assert environ == {**guard.PINNED_ENV, "KEEP": "1"}


def test_the_launcher_and_the_guard_pin_the_same_variables():
    assert hermes.TELEMETRY_ENV == guard.PINNED_ENV
    assert guard.MANAGED_DIR == hermes.MANAGED_DIR


# --- the guard in a Python with a stand-in Hermes: .pth, refusals, patched modules ----------------------------------

STUB_HERMES = {
    # v0.22.5's semantics of the modules the guard patches, JSON for YAML.
    "hermes_constants.py": '''
import os
from pathlib import Path
def get_hermes_home(): return Path(os.environ["HERMES_HOME"])
''',
    "hermes_cli/__init__.py": "",
    "hermes_cli/observability/__init__.py": "",
    "hermes_cli/managed_scope.py": '''
import os
from pathlib import Path
def get_managed_dir():
    override = os.environ.get("HERMES_MANAGED_DIR", "").strip()
    if override: p = Path(override)
    elif "PYTEST_CURRENT_TEST" in os.environ: return None
    else: p = Path("/etc/hermes")
    return p if p.is_dir() else None
''',
    "hermes_cli/config.py": '''
import copy, json
from hermes_constants import get_hermes_home
from hermes_cli import managed_scope
DEFAULT_CONFIG = {"telemetry": {"shared_metrics": {"enabled": False, "send": False}},
                  "monitoring": {"export": {"otlp": {"enabled": False}}}, "computer_use": {"cua_telemetry": False}}
def get_config_path(): return get_hermes_home() / "config.yaml"
def _file(path):
    return json.loads(path.read_text()) if path.exists() else {}
def _merge(base, over):
    for key, value in over.items():
        base[key] = _merge(base.get(key, {}), value) if isinstance(value, dict) and isinstance(base.get(key), dict) \\
            else value
    return base
def _read_raw_config_impl(*, want_deepcopy):
    return _file(get_config_path())
def read_raw_config(): return _read_raw_config_impl(want_deepcopy=True)
def read_raw_config_readonly(): return _read_raw_config_impl(want_deepcopy=False)
def _load_config_impl(*, want_deepcopy):
    config = _merge(copy.deepcopy(DEFAULT_CONFIG), _file(get_config_path()))
    managed = managed_scope.get_managed_dir()
    return _merge(config, _file(managed / "config.yaml")) if managed else config
def load_config(): return _load_config_impl(want_deepcopy=True)
def _write_user_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(json.dumps(config))
''',
    "hermes_cli/observability/shared_metrics_consent.py": '''
from hermes_cli.config import _file, _write_user_config, get_config_path, read_raw_config
OFFER_VERSION = 2
def _section(cfg):
    t = cfg.get("telemetry") if isinstance(cfg, dict) else None
    s = t.get("shared_metrics") if isinstance(t, dict) else None
    return s if isinstance(s, dict) else {}
def consent_state(raw_cfg):
    s = _section(raw_cfg); enabled = s.get("enabled") is True; answered = "enabled" in s or "send" in s
    reask = answered and not enabled and s.get("offer_version") != OFFER_VERSION
    return {"enabled": enabled, "send": enabled and s.get("send") is True, "decided": answered and not reask,
            "reask": reask}
def _current_state(): return consent_state(read_raw_config())
def set_answer(target, enabled, send):
    s = target.setdefault("telemetry", {}).setdefault("shared_metrics", {})
    s["enabled"], s["send"], s["offer_version"] = enabled, enabled and send, OFFER_VERSION
def save_consent(enabled, send, config=None):
    path = get_config_path(); user = _file(path); set_answer(user, enabled, send); _write_user_config(path, user)
    if config is not None: set_answer(config, enabled, send)
''',
    "hermes_cli/observability/relay_shared_metrics.py": '''
from hermes_cli.config import read_raw_config_readonly
def enabled():
    config = read_raw_config_readonly() or {}
    for key in ("telemetry", "shared_metrics"):
        config = config.get(key) if isinstance(config, dict) else None
    return isinstance(config, dict) and config.get("enabled") is True
''',
    "hermes_cli/observability/shared_metrics_send_config.py": '''
from dataclasses import dataclass
@dataclass(frozen=True)
class SendConfig:
    enabled: bool
    send: bool
    endpoint: str
def resolve_send_config(config):
    s = ((config or {}).get("telemetry") or {}).get("shared_metrics") or {}
    enabled = s.get("enabled") is True
    return SendConfig(enabled=enabled, send=enabled and s.get("send") is True, endpoint=s.get("endpoint") or "e")
''',
    "hermes_cli/setup.py": '''
def setup_telemetry(config):
    from hermes_cli.observability.shared_metrics_consent import save_consent
    save_consent(True, True, config)
    print("Shared metrics sent to Nous once a day.")
SETUP_SECTIONS = [("model", "Model", lambda config: None), ("telemetry", "Shared Metrics", setup_telemetry)]
''',
    "hermes_cli/main.py": '''
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["setup"]:
    from hermes_cli.setup import SETUP_SECTIONS
    from hermes_cli.config import load_config
    config = load_config()
    [entry for entry in SETUP_SECTIONS if entry[0] == args[1]][0][2](config)
elif args[:1] == ["consent"]:  # the TUI's and the dashboard's switch
    from hermes_cli.observability.shared_metrics_consent import save_consent
    save_consent(True, True)
elif args[:1] == ["probe"]:
    from hermes_cli.config import load_config, read_raw_config, read_raw_config_readonly
    from hermes_cli.managed_scope import get_managed_dir
    from hermes_cli.observability import relay_shared_metrics, shared_metrics_consent, shared_metrics_send_config
    merged = load_config()
    print(json.dumps({"gate": relay_shared_metrics.enabled(),
                      "send": shared_metrics_send_config.resolve_send_config(read_raw_config_readonly()).send,
                      "state": shared_metrics_consent._current_state(),
                      "otlp": merged["monitoring"]["export"]["otlp"]["enabled"],
                      "cua": merged["computer_use"]["cua_telemetry"], "managed": str(get_managed_dir()),
                      "env": {name: os.environ.get(name) for name in ("DO_NOT_TRACK", "HERMES_MANAGED_DIR")}}))
else:
    print("hermes ran", args)
''',
}


def write_stubs(directory):
    for name, text in STUB_HERMES.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text(text)
    return directory


def guarded_python(tmp_path, files, managed):
    """A venv of this Python standing in for Hermes' (/opt/hermes/venv): the stand-in Hermes on its path (as
    hermes-agent.pth puts /opt/hermes/src there) and FILES - the guard as the launcher writes it - in its
    site-packages, as the launcher mounts them; the managed configuration at MANAGED instead of /etc/hermes."""
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=120)
    python = venv / "bin/python"
    site = Path(subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                               capture_output=True, text=True, check=True).stdout.strip())
    (site / "hermes-agent.pth").write_text(str(write_stubs(tmp_path / "src")) + "\n")
    for name, content in files.items():
        if name.endswith(".py"):
            assert 'MANAGED_DIR = "/etc/hermes"' in content
            content = content.replace('MANAGED_DIR = "/etc/hermes"', f'MANAGED_DIR = "{managed}"', 1)
        (site / Path(name).name).write_text(content)
    return python


@pytest.fixture
def sandbox(tmp_path):
    """(run, home): Hermes' Python with the guard, a HERMES_HOME, the managed configuration of a start."""
    managed = tmp_path / "etc-hermes"
    managed.mkdir()
    (managed / "config.yaml").write_text(json.dumps({"telemetry": {"shared_metrics": {"enabled": False}}}))
    python = guarded_python(tmp_path, hermes.guard_files(), managed)
    home = tmp_path / "root/.hermes"
    home.mkdir(parents=True)

    def run(*args, env=None, module=True):
        command = [str(python), "-m", "hermes_cli.main", *args] if module else [str(python), *args]
        return subprocess.run(command, capture_output=True, text=True, timeout=120,
                              env={"PATH": os.environ["PATH"], "HERMES_HOME": str(home), **(env or {})})
    return run, home


def probe(run, **env):
    done = run("probe", env=env)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_hermes_setup_telemetry_typed_in_the_sandbox_is_refused(sandbox):
    """The gap: `hermes setup telemetry` in the running sandbox turned local counting on. Refused, with the message,
    before Hermes runs; the answer stays No thanks; nothing counts."""
    run, home = sandbox
    for args in (["setup", "telemetry"], ["config", "set", "telemetry.shared_metrics.send", "true"],
                 ["config", "set", "monitoring.export.otlp.enabled", "true"]):
        done = run(*args)
        assert done.returncode == 2 and done.stdout == "", args
        assert done.stderr.startswith(MESSAGE + " (hermes ") and "is refused" in done.stderr, done.stderr
    assert not (home / "config.yaml").exists()
    assert probe(run) == {"gate": False, "send": False, "state": {"enabled": False, "send": False, "decided": True,
                                                                  "reask": False},
                          "otlp": False, "cua": False, "managed": str(home.parents[1] / "etc-hermes"),
                          "env": {"DO_NOT_TRACK": "1", "HERMES_MANAGED_DIR": None}}
    assert run("chat", "-q", "hi").stdout == "hermes ran ['chat', '-q', 'hi']\n"


def test_variables_that_turn_it_on_or_drop_the_managed_configuration_are_refused(sandbox):
    run, _ = sandbox
    for name, value in (("CUA_DRIVER_RS_TELEMETRY_ENABLED", "1"), ("OTEL_SDK_DISABLED", "false"),
                        ("HERMES_MANAGED_DIR", "/nonexistent"), ("PYTEST_CURRENT_TEST", "x")):
        done = run("--version", env={name: value})
        why = "Hermes' configuration stays the one at " if name in guard.SCOPE_ENV else "it stays "
        assert done.returncode == 2 and done.stdout == "", done.stderr
        assert done.stderr.startswith(f"{MESSAGE} ({name}={value} is refused: {why}"), done.stderr


def test_every_other_way_to_switch_it_on_reads_off(sandbox):
    """What no command line shows - the setup menu, the TUI's or the dashboard's switch, an edited config.yaml,
    another Python process of Hermes', the variables in a child that is no Hermes command - all read off."""
    run, home = sandbox
    # The TUI's / dashboard's consent switch: the message, and No thanks written.
    done = run("consent")
    assert done.returncode == 0 and done.stderr == MESSAGE + "\n"
    assert json.loads((home / "config.yaml").read_text())["telemetry"]["shared_metrics"] == {
        "enabled": False, "send": False, "offer_version": 2}
    # An editor (hermes config edit, sed, the agent's file tools): the file says on, every read says off.
    (home / "config.yaml").write_text(json.dumps({
        "telemetry": {"shared_metrics": {"enabled": True, "send": True}},
        "monitoring": {"export": {"otlp": {"enabled": True}}}, "computer_use": {"cua_telemetry": True}}))
    found = probe(run)
    assert (found["gate"], found["send"], found["otlp"], found["cua"]) == (False, False, False, False)
    assert found["state"]["decided"] is True and found["state"]["enabled"] is False
    # Not a Hermes command line (python -c, a script): no refusal, the scope variable dropped, the pins hold.
    code = ("import os, json; from hermes_cli.setup import SETUP_SECTIONS; SETUP_SECTIONS[1][2]({}); "
            "from hermes_cli.observability.relay_shared_metrics import enabled; "
            "print(json.dumps([enabled(), os.environ.get('HERMES_MANAGED_DIR'), os.environ['DO_NOT_TRACK']]))")
    done = run("-c", code, env={"HERMES_MANAGED_DIR": "/nonexistent", "DO_NOT_TRACK": "0"}, module=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [MESSAGE, "[false, null, \"1\"]"]


def test_the_launchers_answer_still_runs_under_the_guard(sandbox, tmp_path):
    """The launcher's answer before every start (python -I, its own script) is no Hermes command: it runs and takes
    back an opt-in an earlier run wrote."""
    run, home = sandbox
    (home / "config.yaml").write_text(json.dumps({"telemetry": {"shared_metrics": {"enabled": True, "send": True}}}))
    script = tmp_path / hermes.TELEMETRY_ANSWER
    script.write_text("from hermes_cli.observability.shared_metrics_consent import save_consent\n"
                      "save_consent(False, False)\n")
    done = run("-I", str(script), module=False)
    assert done.returncode == 0 and done.stderr == ""
    assert json.loads((home / "config.yaml").read_text())["telemetry"]["shared_metrics"]["enabled"] is False


# --- the launcher's part: the files, where they are mounted, the image they fit ------------------------------------

def test_the_guard_files_and_their_mounts():
    files = hermes.guard_files()
    assert files == {f"{hermes.GUARD_DIR}/sandbox_ai_hermes_guard.py": (ROOT / "sandbox/hermes_guard.py").read_text(),
                     f"{hermes.GUARD_DIR}/sandbox-ai-telemetry-off.pth":
                         "import sandbox_ai_hermes_guard; sandbox_ai_hermes_guard.install()\n"}
    mounts = hermes.guard_mounts("/s")
    assert mounts == [(Path("/s") / name, f"{hermes.SITE_PACKAGES}/{Path(name).name}") for name in files]
    assert hermes.SITE_PACKAGES == "/opt/hermes/venv/lib/python3.14/site-packages"


def test_the_site_packages_are_those_of_the_image():
    """The guard lands in the venv's site-packages: CPython 3.14 in /opt/hermes/venv (Containerfile.hermes)."""
    containerfile = (ROOT / "sandbox/Containerfile.hermes").read_text()
    version = re.search(r"uv python install --no-bin (\d+\.\d+)", containerfile).group(1)
    assert f"uv venv --python {version} --python-preference only-managed /opt/hermes/venv" in containerfile
    assert hermes.SITE_PACKAGES == f"/opt/hermes/venv/lib/python{version}/site-packages"
    assert 'xargs -I{} sh -c \'echo /opt/hermes/src > "{}/hermes-agent.pth"\'' in containerfile


def test_the_launcher_refuses_the_same_arguments_and_says_so_in_the_plan():
    assert hermes.refused_arg(["config", "set", "telemetry.shared_metrics.send", "true"])[0] == \
        "config set telemetry.shared_metrics.send"
    assert hermes.refused_arg(["setup", "telemetry"])[0] == "setup telemetry"
    plan = hermes.plan(type("A", (), {"hermes_enable": [], "agent": "hermes"})())["telemetry"]
    assert plan["guard"] == f"{hermes.SITE_PACKAGES}/sandbox-ai-telemetry-off.pth"
    assert plan["refused_arguments"] == ["setup telemetry", "config set|unset telemetry",
                                         "config set|unset monitoring", "config set|unset computer_use.cua_telemetry"]
    assert plan["refused_environment"] == [*guard.PINNED_ENV, *guard.SCOPE_ENV]


def test_the_launcher_does_not_pass_what_would_take_the_pins_away(monkeypatch, capsys):
    """--env HERMES_MANAGED_DIR (or PYTEST_CURRENT_TEST) would make every Hermes start in the sandbox a refused one:
    not passed, said so; the opt-out variables win over what is passed (as for every agent)."""
    s = load("launcher_hermes_guard_tests", "sandbox/agent_sandbox.py")
    monkeypatch.setenv("HERMES_MANAGED_DIR", "/x")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "y")
    monkeypatch.setenv("CUA_DRIVER_RS_TELEMETRY_ENABLED", "1")
    env = s.agent_environment(s.parse_args(["hermes", "--env", "HERMES_MANAGED_DIR", "--env", "PYTEST_CURRENT_TEST",
                                            "--env", "CUA_DRIVER_RS_TELEMETRY_ENABLED"]))
    assert "HERMES_MANAGED_DIR" not in env and "PYTEST_CURRENT_TEST" not in env
    assert {name: env[name] for name in guard.PINNED_ENV} == guard.PINNED_ENV
    assert "HERMES_MANAGED_DIR, PYTEST_CURRENT_TEST are not passed to Hermes" in capsys.readouterr().err
    # Another agent's run keeps what you pass.
    assert s.agent_environment(s.parse_args(["claude", "--env", "HERMES_MANAGED_DIR"]))["HERMES_MANAGED_DIR"] == "/x"


# --- end to end: the real ./sandbox-ai (only Podman stood in for, live_fakes.py) -----------------------------------

def guard_mounts_of(host):
    run = [call for call in host.calls() if call[:1] == ["run"] or "run" in call[:3] and "--pod" in call]
    assert run, host.calls()
    mounts = [arg for arg in run[-1] if arg.startswith("type=bind,")]
    return {arg.split(",dst=", 1)[1].split(",", 1)[0]: arg for arg in mounts}


@pytest.mark.parametrize("extra", [[], ["--binary", "/bin/bash"]])
def test_e2e_a_hermes_sandbox_gets_the_guard_read_only(tmp_path, extra):
    """`sandbox-ai hermes` (and a shell in Hermes' image, where `hermes` can be typed too): the guard and its .pth
    are mounted read-only into Hermes' site-packages from the session directory; the files are the launcher's. Run
    in a stand-in of Hermes' Python, `hermes setup telemetry` is refused with the message."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image("localhost/hermes-sandbox:latest")
    done = host.run("hermes", "--no-tmux", "--no-auth", *extra,
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(tmp_path / "kept")})
    assert done.returncode == 0, done.stderr
    mounts = guard_mounts_of(host)
    session = tmp_path / "kept/session"
    for name, content in hermes.guard_files().items():
        target = f"{hermes.SITE_PACKAGES}/{Path(name).name}"
        assert target in mounts and mounts[target].endswith(",ro,nodev"), mounts.get(target)
        assert f"/{name},dst=" in mounts[target]
        assert (session / name).read_text() == content
    if not extra:  # the managed configuration (the pins), read-only too
        assert mounts["/etc/hermes"].endswith(",ro,nodev")
    kept = {name: (session / name).read_text() for name in hermes.guard_files()}
    managed = tmp_path / "etc-hermes"
    managed.mkdir()
    python = guarded_python(tmp_path, kept, managed)
    home = tmp_path / "root/.hermes"
    refused = subprocess.run([str(python), "-m", "hermes_cli.main", "setup", "telemetry"], capture_output=True,
                             text=True, env={"PATH": os.environ["PATH"], "HERMES_HOME": str(home)}, timeout=120)
    assert refused.returncode == 2 and refused.stderr.startswith(MESSAGE), refused.stderr
    assert not (home / "config.yaml").exists()


def test_e2e_the_launcher_refuses_it_in_hermes_arguments_too(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image("localhost/hermes-sandbox:latest")
    refused = host.run("hermes", "--no-tmux", "--no-auth", "--", "config", "set", "telemetry.shared_metrics.send",
                       "true", env={"FAKE_PODMAN_RUN": "0:0"})
    assert refused.returncode != 0
    assert "config set telemetry.shared_metrics.send in Hermes' arguments: Hermes' telemetry is always off" in \
        refused.stderr


# --- with the pinned Hermes itself ----------------------------------------------------------------------------------

HERMES_PYTHON_FOR_TESTS = os.environ.get("SANDBOX_AI_TEST_HERMES_PYTHON")


@pytest.mark.skipif(not HERMES_PYTHON_FOR_TESTS, reason="SANDBOX_AI_TEST_HERMES_PYTHON: a Python with Hermes v0.22.5")
def test_the_guard_with_the_pinned_hermes(tmp_path):
    """Hermes v0.22.5 itself (its venv): every function the guard patches is there; `hermes setup telemetry` is
    refused; Hermes' own consent writer, an edited config.yaml and its gates all stay off; Hermes still runs."""
    directory = tmp_path / "guard"
    directory.mkdir()
    managed = tmp_path / "etc-hermes"
    managed.mkdir()
    (managed / "config.yaml").write_text(json.dumps(hermes.managed_config(
        type("A", (), {"hermes_enable": [], "agent": "hermes"})())))
    (directory / "sandbox_ai_hermes_guard.py").write_text(
        (ROOT / "sandbox/hermes_guard.py").read_text().replace('MANAGED_DIR = "/etc/hermes"',
                                                              f'MANAGED_DIR = "{managed}"', 1))
    home = tmp_path / "root/.hermes"
    home.mkdir(parents=True)
    boot = (f"import sys; sys.path.insert(0, {str(directory)!r}); import sandbox_ai_hermes_guard as g; "
            "g.install(argv=[sys.executable, '-m', 'hermes_cli.main', *sys.argv[1:]]); ")

    def hermes_run(code, *args):
        return subprocess.run([HERMES_PYTHON_FOR_TESTS, "-I", "-c", boot + code, *args], capture_output=True,
                              text=True, timeout=300,
                              env={"PATH": os.environ["PATH"], "HOME": str(home.parent), "HERMES_HOME": str(home)})

    done = hermes_run("pass", "setup", "telemetry")
    assert done.returncode == 2 and done.stderr.startswith(MESSAGE), done.stderr
    check = (
        "import json, warnings\n"
        "from hermes_cli.observability import relay_shared_metrics as r, shared_metrics_consent as c, "
        "shared_metrics_send_config as sc, shared_metrics_update as u, shared_metrics_engagement\n"
        "from hermes_cli import config, setup, managed_scope\n"
        "patched = [getattr(m, n)._sandbox_ai_guard for m, n in ((config, '_read_raw_config_impl'), "
        "(config, '_load_config_impl'), (c, 'consent_state'), (c, 'set_answer'), (c, 'save_consent'), "
        "(r, 'enabled'), (sc, 'resolve_send_config'), (setup, 'setup_telemetry'), (managed_scope, 'get_managed_dir'))]\n"
        "c.save_consent(True, True)\n"
        "written = config.read_user_config_raw()['telemetry']['shared_metrics']\n"
        "open(config.get_config_path(), 'w').write('telemetry:\\n  shared_metrics:\\n    enabled: true\\n    send: true\\n'"
        "'monitoring:\\n  export:\\n    otlp:\\n      enabled: true\\n')\n"
        "print(json.dumps({'patched': all(patched), 'written': written, 'gate': r.enabled(), "
        "'send': r._resolved_send_config().send, 'state': c._current_state(), 'update': u._collection_on(), "
        "'otlp': config.load_config()['monitoring']['export']['otlp']['enabled'], "
        "'sections': [e[2] is setup.setup_telemetry for e in setup.SETUP_SECTIONS if e[0] == 'telemetry']}))\n")
    done = hermes_run(check)
    assert done.returncode == 0, done.stderr
    found = json.loads(done.stdout.splitlines()[-1])
    assert found == {"patched": True, "written": {"enabled": False, "send": False, "offer_version": 2},
                     "gate": False, "send": False, "state": {"enabled": False, "send": False, "decided": True,
                                                             "reask": False},
                     "update": False, "otlp": False, "sections": [True]}
    assert MESSAGE in done.stderr
    version = hermes_run("import runpy; sys.argv = ['hermes', '--version']; "
                         "runpy.run_module('hermes_cli.main', run_name='__main__')", "--version")
    assert version.returncode == 0 and "Hermes Agent v0.22.5" in version.stdout, version.stderr
