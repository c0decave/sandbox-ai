#!/usr/bin/python3
"""sandbox-ai's `openhands` command in localhost/openhands-sandbox (sandbox/Containerfile.openhands), and the `uvx` the
Agent Canvas finds first on its PATH.

  openhands [Canvas options]              the Agent Canvas web UI (default: 127.0.0.1:8000, the sandbox's loopback)
  openhands task [--resume] --text=TEXT   one task without the UI: the agent's last answer on standard output
  openhands --version                     the reviewed releases in this image

Upstream's Canvas launcher (bin/agent-canvas.mjs) starts the agent server and the automation backend with uvx from
PyPI at every start. Here its uvx is this file (/opt/openhands/bin/uvx): the agent server and the automation backend
of the versions installed in the image's venv run from there, any other version, a git ref or a local checkout of
them is refused, and every other uvx call goes to the real uvx unchanged (MCP servers, kits).

The settings the launcher writes for this start (/etc/openhands/settings.json, read-only; sandbox/openhands.py:
managed_settings) are applied through the agent server's API as soon as it answers: the telemetry consent (denied),
the approvals, the model and the --mcp servers. The variables that pin the telemetry off, the local runtime and the
reviewed versions come from the launcher (sandbox/openhands.py: environment) and hold for every process here.

Runs with the image's /usr/bin/python3 (standard library only); `task` runs again under the venv's Python, where the
OpenHands SDK is. Before anything, sandbox-ai's telemetry guard (GUARD_PY, the image's and the launcher's copy:
sandbox/telemetry_guard.py) refuses what would turn OpenHands' telemetry on and pins the opt-outs for every process
started from here - the Canvas reads its own; the venv's Python runs the guard by itself (its .pth).
"""
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import time
import urllib.error
import urllib.request

OPT = Path(os.environ.get("SANDBOX_AI_OPENHANDS_OPT", "/opt/openhands"))
VENV = OPT / "venv"
CANVAS = OPT / "node_modules" / ".bin" / "agent-canvas"
SHIM_DIR = OPT / "bin"  # holds this file as uvx
REAL_UVX = os.environ.get("SANDBOX_AI_REAL_UVX", "/usr/local/bin/uvx")
SETTINGS = Path(os.environ.get("SANDBOX_AI_OPENHANDS_SETTINGS", "/etc/openhands/settings.json"))
EXTENSIONS = OPT / "node_modules" / "@openhands" / "extensions"
AGENT_SERVER, AUTOMATION = "openhands-agent-server", "openhands-automation"
SDK_PACKAGES = ("openhands-sdk", "openhands-tools", "openhands-workspace")
GUARD_PY = "/usr/local/lib/sandbox-ai/telemetry_guard.py"  # telemetry_guard.GUARD_PY (never from the environment)
# uvx's options that take a value, and those that do not (uv 0.13), as the Canvas' launcher writes them.
UVX_VALUE = {"--from", "--with", "--with-editable", "--with-requirements", "--python", "-p", "--index-url",
             "--extra-index-url", "--index", "--default-index", "--find-links", "-f", "--constraints", "-c",
             "--overrides", "--build-constraints", "-b", "--directory", "--project", "--cache-dir", "--config-file"}
UVX_FLAGS = {"--reinstall", "--refresh", "--isolated", "--quiet", "-q", "--offline", "--no-cache", "-n", "--verbose",
             "-v", "--no-progress", "--native-tls", "--system-certs", "--prerelease=allow"}
REFUSED_ARGS = {"--public", "--automation-ref", "--automation-repo", "--dynamic"}
LOOPBACK = ("127.0.0.1", "localhost", "::1")
DEFAULT_PORT = 8000
WAIT = 300  # seconds the settings wait for the agent server (its first start imports a lot)
LOG = Path.home() / ".openhands" / "sandbox-ai-settings.log"
TELEMETRY = {"telemetry": {"consent": "denied", "managed": True}}
TASKS = Path.home() / ".openhands" / "sandbox-ai-tasks"


def say(text):
    print(f"sandbox-ai: {text}", file=sys.stderr, flush=True)


# --- the versions in the image ----------------------------------------------------------------------------------------

def installed_versions(venv=VENV):
    """{distribution: version} of the OpenHands packages in VENV (their .dist-info folders)."""
    found = {}
    for info in sorted(venv.glob("lib/python3*/site-packages/openhands_*.dist-info")):
        name, _, version = info.name[:-len(".dist-info")].partition("-")
        found[name.replace("_", "-")] = version
    return found


# --- uvx --------------------------------------------------------------------------------------------------------------

def parse_uvx(args):
    """uvx's ARGS as (options: [(name, value)], command, command arguments); ValueError for an option this does not
    know (it might change what runs)."""
    options, index = [], 0
    while index < len(args):
        arg = args[index]
        if not arg.startswith("-"):
            return options, arg, list(args[index + 1:])
        name, sep, value = arg.partition("=")
        if name in UVX_VALUE:
            if not sep:
                if index + 1 >= len(args):
                    raise ValueError(f"{arg} without a value")
                index += 1
                value = args[index]
            options.append((name, value))
        elif arg in UVX_FLAGS:
            options.append((arg, None))
        else:
            raise ValueError(f"unknown option {arg}")
        index += 1
    return options, None, []


def pinned(spec):
    """(distribution, version) of a requirement NAME==VERSION, else None (a range, a URL, a path, git)."""
    name, sep, version = str(spec).partition("==")
    if not sep or not name or any(c in name + version for c in " <>!~;@/:[]"):
        return None
    return name.strip().lower().replace("_", "-"), version.strip()


def uvx_target(args, versions):
    """What the Canvas' uvx call ARGS asks for: ("agent-server"|"automation", the command's arguments), None for a
    call that is not OpenHands' (the real uvx runs it). ValueError for OpenHands of another version or source."""
    try:
        options, command, rest = parse_uvx(args)
    except ValueError:
        if any(AGENT_SERVER in arg or AUTOMATION in arg for arg in args):
            raise
        return None
    sources = [value for name, value in options if name in ("--from", "--with", "--with-editable")]
    mentioned = [value for value in sources if "openhands" in value.lower()]
    if not mentioned and command not in ("agent-server",):
        return None
    if any(name in ("--with-editable", "--with-requirements", "--directory", "--project", "--index-url",
                    "--extra-index-url", "--index", "--default-index", "--find-links", "-f")
           for name, _ in options):
        raise ValueError("OpenHands runs from the image's install only (no editable, local or other index source)")
    for value in mentioned:
        found = pinned(value)
        if found is None or found[0] not in versions or versions[found[0]] != found[1]:
            raise ValueError(f"{value}: this image has {', '.join(f'{k}=={v}' for k, v in sorted(versions.items()))}"
                             " (the release sandbox-ai reviewed); another version, a git ref or a checkout is not "
                             "run here")
    origin = dict(options).get("--from", "")
    if command == "agent-server" and pinned(origin) and pinned(origin)[0] == AGENT_SERVER:
        return "agent-server", rest
    if command == "uvicorn" and pinned(origin) and pinned(origin)[0] == AUTOMATION and \
            "openhands.automation.app:app" in rest:
        return "automation", rest
    raise ValueError(f"uvx {' '.join(args[:6])}...: not a call of the Canvas this image runs")


def wait_forever():
    while True:
        signal.pause()


def uvx_main(args):
    try:
        target = uvx_target(args, installed_versions())
    except ValueError as error:
        say(f"OpenHands' uvx: {error}")
        return 2
    if target is None:
        os.execv(REAL_UVX, [REAL_UVX, *args])
    kind, rest = target
    if kind == "agent-server":
        os.execv(str(VENV / "bin" / "agent-server"), [str(VENV / "bin" / "agent-server"), *rest])
    if os.environ.get("SANDBOX_AI_OPENHANDS_AUTOMATIONS") != "1":
        # The Canvas shows automations as unavailable while this waits (it stops the stack only when it exits).
        say("OpenHands' automation backend is off in this sandbox (sandbox-ai openhands --openhands-enable "
            "automations turns it on)")
        signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
        wait_forever()
    os.execv(str(VENV / "bin" / "uvicorn"), [str(VENV / "bin" / "uvicorn"), *rest])
    return 1


# --- the settings -----------------------------------------------------------------------------------------------------

def merge(base, over):
    """BASE with OVER laid over it leaf by leaf (dicts merge, everything else replaces)."""
    result = dict(base)
    for key, value in over.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) \
            else value
    return result


def read_settings(path=SETTINGS):
    """The settings this start lays over OpenHands' (sandbox/openhands.py: managed_settings); only the telemetry
    consent when there is no file (a start of the image without the launcher)."""
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, ValueError, RecursionError):
        document = {}
    return document if isinstance(document, dict) else {}


def patch_body(document, current=None):
    """The PATCH /api/settings body for DOCUMENT (read_settings) over the settings CURRENT (GET /api/settings, None
    when unknown): the agent's and the conversation's diffs, the --mcp servers an earlier start set and this one does
    not removed (null), and the telemetry consent (denied, managed) with what this start set (misc_settings)."""
    agent = dict(document.get("agent_settings_diff") or {})
    ours = list((document.get("sandbox_ai") or {}).get("mcp") or [])
    misc = ((current or {}).get("misc_settings") or {}) if isinstance(current, dict) else {}
    before = (misc.get("sandbox_ai") or {}).get("mcp") or [] if isinstance(misc, dict) else []
    gone = [name for name in before if isinstance(name, str) and name not in ours]
    if gone:
        agent["mcp_config"] = {**{name: None for name in gone}, **(agent.get("mcp_config") or {})}
    body = {"misc_settings_diff": merge(document.get("misc_settings_diff") or {},
                                        {**TELEMETRY, "sandbox_ai": {"mcp": ours}})}
    if agent:
        body["agent_settings_diff"] = agent
    if document.get("conversation_settings_diff"):
        body["conversation_settings_diff"] = dict(document["conversation_settings_diff"])
    return body


def request(url, key, method="GET", body=None, timeout=10):
    """A request to the Canvas on the loopback, never through the sandbox's proxy; (status, parsed JSON or None)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"X-Session-API-Key": key, "Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as answer:
            raw = answer.read()
            status = answer.status
    except urllib.error.HTTPError as error:
        return error.code, None
    try:
        return status, json.loads(raw or b"null")
    except (ValueError, RecursionError):
        return status, None


def apply_settings(port, key, document, wait=WAIT, log=None, send=request, sleep=time.sleep):
    """Wait for the agent server behind the Canvas (port PORT), then apply DOCUMENT; True when it took. A model or
    MCP change the server refuses (say, an ACP agent chosen in the UI) is dropped and the rest applied again: the
    telemetry consent always."""
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + wait
    while True:
        try:
            status, _ = send(base + "/server_info", key, timeout=5)
        except OSError:
            status = None
        if status == 200:
            break
        if time.monotonic() >= deadline:
            (log or say)(f"OpenHands did not answer on 127.0.0.1:{port} within {wait} s: its settings were not set")
            return False
        sleep(1)
    try:
        status, current = send(base + "/api/settings", key)
    except OSError:
        status, current = None, None
    body = patch_body(document, current if status == 200 else None)
    attempts = [body] + ([{k: v for k, v in body.items() if k != "agent_settings_diff"}]
                         if "agent_settings_diff" in body else [])
    for attempt in attempts:
        try:
            status, _ = send(base + "/api/settings", key, "PATCH", attempt)
        except OSError as error:
            status = str(error)
        if status == 200:
            (log or say)("OpenHands' settings for this start are set" +
                         ("" if attempt is body else " (without its model and MCP servers, which it refused)"))
            return True
        (log or say)(f"OpenHands refused this start's settings ({status})")
    return False


def log_to_file(text):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as out:
            out.write(time.strftime("%Y-%m-%dT%H:%M:%S ") + text + "\n")
    except OSError:
        pass


# --- the Canvas -------------------------------------------------------------------------------------------------------

def refused(args):
    """(argument, why) of the first Canvas option a sandbox refuses, else None."""
    for index, arg in enumerate(args):
        name = arg.split("=", 1)[0]
        if name in REFUSED_ARGS:
            return name, "not in a sandbox (sandbox-ai openhands: the Canvas on the loopback, the reviewed release)"
        following = args[index + 1] if index + 1 < len(args) else ""
        # --host VALUE, --host=VALUE, -H VALUE, or -HVALUE (Node's parsers take a short option's value attached)
        value = following if arg in ("-H", "--host") else arg.split("=", 1)[1] if arg.startswith("--host=") else \
            arg[2:] if arg.startswith("-H") else None
        if value is not None and value not in LOOPBACK:
            return arg, "the Canvas listens on the sandbox's loopback only (127.0.0.1)"
    return None


def port_of(args, env):
    """The Canvas' port: -p/--port (VALUE after it, or attached: -pVALUE, --port=VALUE, -p=VALUE), else PORT, else
    8000. A value that is no port (not a number of 1-65535) is passed over, as the launcher's canvas_port does."""
    for index, arg in enumerate(args):
        name, sep, value = arg.partition("=")
        if name in ("-p", "--port"):
            value = value if sep else (args[index + 1] if index + 1 < len(args) else "")
        elif arg.startswith("-p") and not arg.startswith("--"):
            value = arg[2:]  # -pVALUE
        else:
            continue
        if value.isdigit() and 0 < int(value) < 65536:
            return int(value)
    return int(env["PORT"]) if str(env.get("PORT", "")).isdigit() else DEFAULT_PORT


def canvas_env(env):
    """The Canvas' variables: this file's uvx first on its PATH, a key of its own per start (the page gets it on the
    loopback), the public skills from the Canvas' own package."""
    env = dict(env)
    env["PATH"] = f"{SHIM_DIR}:{env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}"
    env["LOCAL_BACKEND_API_KEY"] = env.get("LOCAL_BACKEND_API_KEY") or secrets.token_hex(24)
    env.setdefault("EXTENSIONS_PATH", str(EXTENSIONS))
    return env


def canvas_main(args):
    problem = refused(args)
    if problem:
        say(f"{problem[0]}: {problem[1]}")
        return 2
    env = canvas_env(os.environ)
    port, document = port_of(args, env), read_settings()
    if os.fork() == 0:  # the settings, once the server answers; the Canvas itself is this process
        try:
            os.setsid()
            apply_settings(port, env["LOCAL_BACKEND_API_KEY"], document, log=log_to_file)
        finally:
            os._exit(0)
    os.execve(str(CANVAS), [str(CANVAS), *args], env)
    return 1


# --- one task ---------------------------------------------------------------------------------------------------------

def task_words(args):
    """(resume, text) of `openhands task [--resume] [--text=TEXT | TEXT...]`; ValueError without a text."""
    resume, text, words = False, None, []
    for arg in args:
        if arg in ("--resume", "-r", "--continue", "-c"):
            resume = True
        elif arg.startswith("--text="):
            text = arg[len("--text="):]
        else:
            words.append(arg)
    text = text if text is not None else " ".join(words)
    if not text.strip():
        raise ValueError("openhands task needs a text: openhands task --text=TEXT")
    return resume, text


def load_persisted():
    """OpenHands' own settings of this HOME (what the UI saved), else its defaults."""
    from openhands.agent_server.persistence.models import PersistedSettings
    try:
        from openhands.agent_server.persistence.store import FileSettingsStore
        from openhands.sdk.utils.cipher import Cipher
        key_file = Path.home() / ".openhands" / "agent-canvas" / "secret-key.txt"
        key = os.environ.get("OH_SECRET_KEY") or (key_file.read_text().strip() if key_file.exists() else "")
        store = FileSettingsStore(Path.home() / ".openhands", cipher=Cipher(key) if key else None)
        return store.load() or PersistedSettings()
    except Exception:  # noqa: BLE001 - a settings file this cannot read: the defaults and this start's settings
        return PersistedSettings()


def task_main(args):
    """One task with the SDK in this process (the venv's Python): the UI's settings with this start's over them, the
    local workspace (the current folder), no approvals (nobody to ask), the agent's last answer printed."""
    try:
        resume, text = task_words(args)
    except ValueError as error:
        say(str(error))
        return 2
    os.environ.setdefault("EXTENSIONS_PATH", str(EXTENSIONS))
    from openhands.sdk import Conversation
    from openhands.sdk.conversation.response_utils import get_agent_final_response
    from openhands.tools.preset.default import register_default_tools
    register_default_tools(enable_browser=False)  # the agent server's tools (terminal, file editor ...); no browser
    document = read_settings()
    settings = load_persisted()
    diff = {key: document[key] for key in ("agent_settings_diff", "conversation_settings_diff") if document.get(key)}
    if diff:
        settings.update(diff)
    agent = settings.agent_settings.create_agent()
    TASKS.mkdir(parents=True, exist_ok=True)
    last = TASKS / "last"
    conversation_id = None
    if resume and last.exists():
        import uuid
        conversation_id = uuid.UUID(last.read_text().strip())
    conversation = Conversation(agent=agent, workspace=os.getcwd(), persistence_dir=str(TASKS),
                                conversation_id=conversation_id, visualizer=None,
                                max_iteration_per_run=settings.conversation_settings.max_iterations)
    last.write_text(str(conversation.id) + "\n")
    conversation.send_message(text)
    conversation.run()
    print(get_agent_final_response(conversation.state.events))
    return 0


def telemetry_guard():
    """sandbox-ai's telemetry guard for OpenHands (telemetry_guard.python_guard): MESSAGE and exit status 2 for what
    would turn its telemetry on, else os.environ pinned. Nothing where the image has no guard (an older image)."""
    if not Path(GUARD_PY).is_file():
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("sandbox_ai_telemetry_guard", GUARD_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.python_guard("openhands")


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    telemetry_guard()
    if Path(argv[0]).name == "uvx":
        return uvx_main(argv[1:])
    args = argv[1:]
    if args[:1] == ["--sandbox-ai-task"]:
        return task_main(args[1:])
    if args[:1] == ["task"]:
        python = VENV / "bin" / "python"
        os.execv(str(python), [str(python), "-I", str(Path(__file__).resolve()), "--sandbox-ai-task", *args[1:]])
    if args[:1] in (["--version"], ["-v"]):
        versions = installed_versions()
        print(f"sandbox-ai openhands: Agent Canvas {canvas_version()}, "
              + ", ".join(f"{name} {version}" for name, version in sorted(versions.items())))
        return 0
    return canvas_main(args)


def canvas_version():
    try:
        package = OPT / "node_modules" / "@openhands" / "agent-canvas" / "package.json"
        return json.loads(package.read_text())["version"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return "?"


if __name__ == "__main__":
    sys.exit(main())
