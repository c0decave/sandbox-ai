#!/usr/bin/env python3
"""What names a secret or a Git server in what sandbox-ai keeps, and a rename carried through it.

A secret is named (never its value) by: the Git logins (sandbox-ai git-logins), the publish logins, the identities,
VPN profiles (an OpenVPN login, a Mullvad account, a Tailscale auth key, Tor's bridge lines), LLM endpoints (their
key), MCP servers (env_secrets), the options of the trusted project files (.sandbox-ai.json: --git-token-secret
URL=NAME, --secret-env [VAR=]NAME, --api-key HOST=VAR[=NAME], --mcp-env SERVER:VAR@NAME) and the options the kept
starts (sessions) are resumed with.

`sandbox-ai secrets rename OLD NEW` (secret_store.py) changes each of them it can change safely - sandbox-ai's own
lists, the VPN profiles, a project file only while it is exactly what you trusted (it is trusted again as written),
a kept start's options under its lock - and says what still names the old one (an LLM endpoint, an MCP server, a
project file changed since it was trusted, a start that is being started or stopped right now). `git-logins edit`
uses url_references to say what else names a server whose address changed.

Names and addresses only: no value is read here. Standard library only."""
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()
_modules = {}

# The start options that name a secret, with their short forms (agent_sandbox.SHORT_OPTIONS; a test keeps them the
# same) and how the secret's name is found in the value.
SECRET_OPTIONS = {"--git-token-secret": "-gk", "--secret-env": "-se", "--api-key": "-ky", "--mcp-env": "-mv"}
GIT_OPTIONS = ("--git-token-secret", "--git-token-env", "--git-token-file")


def module(name):
    """sandbox-ai's NAME.py, loaded once by path (the launcher as "agent_sandbox")."""
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_credrefs", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def launcher():
    return module("agent_sandbox")


def printable(text):
    return launcher().printable(text)


# --- options (a project file's, a kept start's) -----------------------------------------------------------------------

def long_form(flag):
    """--LONG of FLAG (a long option, or the short form of one that names a secret), else None."""
    for long, short in SECRET_OPTIONS.items():
        if flag in (long, short):
            return long
    return None


def secret_of(option, value):
    """The secret's name VALUE of OPTION refers to (None: none, or not of that form)."""
    if not isinstance(value, str):
        return None
    if option == "--git-token-secret":
        url, sep, name = value.rpartition("=")
        return name if sep and url else None
    if option == "--secret-env":
        return value.rpartition("=")[2] or None
    if option == "--api-key":
        parts = value.split("=")
        return parts[-1] if len(parts) in (2, 3) and parts[-1] else None
    if option == "--mcp-env":
        head, sep, name = value.rpartition("@")
        return name if sep and ":" in head and "=" not in head else None
    return None


def renamed_value(option, value, old, new):
    """VALUE of OPTION naming the secret NEW instead of OLD (the variable the agent sees keeps its name)."""
    if secret_of(option, value) != old:
        return value
    if option == "--secret-env":
        variable = value.rpartition("=")[0] or old
        return f"{variable}={new}"
    if option == "--api-key":
        parts = value.split("=")
        return "=".join([parts[0], parts[1], new])
    if option == "--mcp-env":
        return value.rpartition("@")[0] + "@" + new
    return value.rpartition("=")[0] + "=" + new


def option_values(options):
    """[(index of the value's token, long option, value, joined)] of the options in OPTIONS (a list of words) that
    name a secret: `--option VALUE` (JOINED False) and `--option=VALUE` (True; the index is the token's own)."""
    found, index = [], 0
    options = options if isinstance(options, list) else []
    while index < len(options):
        token = options[index]
        if not isinstance(token, str):
            index += 1
            continue
        flag, sep, joined = token.partition("=")
        option = long_form(flag) if flag.startswith("-") else None
        if option and sep:
            found.append((index, option, joined, True))
        elif option and index + 1 < len(options):
            found.append((index + 1, option, options[index + 1], False))
            index += 1
        index += 1
    return found


def options_naming(options, name):
    """The options of OPTIONS that name the secret NAME, as text ("--secret-env NAME")."""
    return [f"{option} {value}" for _, option, value, _ in option_values(options) if secret_of(option, value) == name]


def renamed_options(options, old, new):
    """(OPTIONS with every reference to the secret OLD naming NEW, how many changed)."""
    changed, result = 0, list(options)
    for index, option, value, joined in option_values(options):
        fresh = renamed_value(option, value, old, new)
        if fresh != value:
            result[index] = f"{result[index].partition('=')[0]}={fresh}" if joined else fresh
            changed += 1
    return result, changed


def options_for_url(options, url):
    """The --git-token-* options of OPTIONS for the server URL (as text), compared as the launcher names a server."""
    found, index = [], 0
    options = options if isinstance(options, list) else []
    while index < len(options):
        token = options[index]
        flag, sep, joined = token.partition("=") if isinstance(token, str) else ("", "", "")
        if flag in GIT_OPTIONS:
            value = joined if sep else options[index + 1] if index + 1 < len(options) else ""
            if same_server(str(value).rpartition("=")[0], url):
                found.append(f"{flag} {value}")
            index += 0 if sep else 1
        index += 1
    return found


def same_server(first, second):
    """Whether the two Git server addresses are one server (scheme, host and port; a user in the URL left out)."""
    def key(url):
        try:
            return launcher().git_server(url, "git-logins")["url"]
        except (ValueError, KeyError, TypeError):
            return str(url).rstrip("/").lower()
    return bool(first) and bool(second) and key(first) == key(second)


# --- where sandbox-ai keeps options -----------------------------------------------------------------------------------

def project_files():
    """[(path, document or None, trusted now)] of the trusted project files that are there: the document read only
    when the file is JSON of the right shape; TRUSTED NOW: it is exactly what you trusted."""
    l = launcher()
    found = []
    for path, digest in sorted(l.trusted_configs(note=lambda line: None).items()):
        try:
            data = l.read_config_bytes(Path(path))
            document = json.loads(data.decode())
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(document, dict) or not isinstance(document.get("options", []), list):
            continue
        found.append((Path(path), document, hashlib.sha256(data).hexdigest() == digest))
    return found


def write_project(path, document):
    """PATH (a trusted project file, unchanged since) replaced by DOCUMENT and trusted as written - as `net allow
    --keep` writes it: beside it and renamed over it, never through a link."""
    l = launcher()
    if path.is_symlink():
        raise ValueError(f"{path} is a symlink; refusing to write through it")
    data = (json.dumps(document, indent=2) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    l.trust_config(path, hashlib.sha256(data).hexdigest())


def kept_starts():
    """[(id, shown name)] of the kept starts (sessions) of this user."""
    sessions = module("sessions")
    try:
        names = sorted(entry.name for entry in sessions.root().iterdir() if entry.is_dir())
    except OSError:
        return []
    found = []
    for key in names:
        with contextlib.suppress(OSError, ValueError, RecursionError):
            data = sessions.load(key)
            found.append((key, sessions.display(key, data)))
    return found


# --- a secret's references --------------------------------------------------------------------------------------------

def vpn_fields(profile):
    """[(section, field)] of a VPN profile's fields that name a secret of yours (not the keys the profile keeps
    itself under its own names, which go with it)."""
    kind = profile.get("type")
    section = profile.get(kind) if isinstance(profile.get(kind), dict) else {}
    found = []
    if kind == "openvpn" and not section.get("auth_secret_owned"):
        found.append((kind, "auth_secret"))
    if kind == "tailscale":
        found.append((kind, "auth_key_secret"))
    if kind == "tor" and not section.get("bridges_secret_owned"):
        found.append((kind, "bridges_secret"))
    if isinstance(profile.get("mullvad"), dict):
        found.append(("mullvad", "account_secret"))
    return found


def vpn_own_secrets(profile):
    """The secrets a VPN profile keeps under names of its own (its keys): they are not renamed."""
    kind = profile.get("type")
    section = profile.get(kind) if isinstance(profile.get(kind), dict) else {}
    names = [section.get("private_key_secret"), *(section.get("preshared_key_secrets") or {}).values()]
    if section.get("auth_secret_owned"):
        names.append(section.get("auth_secret"))
    if section.get("bridges_secret_owned"):
        names.append(section.get("bridges_secret"))
    return [name for name in names if isinstance(name, str)]


def secret_references(name):
    """What names the secret NAME: [{"kind", "what", ...}] - kind "git-logins", "publish-logins", "identity", "vpn",
    "vpn-own" (a key a VPN profile keeps itself), "llm", "mcp", "project" (with "path", "trusted"), "start" (with
    "id"). Something that cannot be read is left out: this is a report, not a check."""
    found = []
    with contextlib.suppress(ValueError, OSError):
        for entry in module("git_logins").load():
            if entry.get("secret") == name:
                found.append({"kind": "git-logins", "what": f"the Git login for {entry['url']}"})
    with contextlib.suppress(ValueError, OSError):
        for entry in module("publish_logins").load():
            if entry.get("secret") == name:
                found.append({"kind": "publish-logins", "what": f"the publish login {entry['kind']}"})
    with contextlib.suppress(ValueError, OSError):
        for identity, data in sorted(module("identities").load().items()):
            if any(isinstance(e, dict) and e.get("secret") == name
                   for e in list(data.get("git", [])) + list(data.get("publish", []))):
                found.append({"kind": "identity", "what": f"the identity {identity}", "name": identity})
    with contextlib.suppress(Exception):  # noqa: BLE001 - a report: a damaged profile is not this command's to refuse
        vpn = module("vpn")
        for profile_name in vpn.list_profiles():
            with contextlib.suppress(ValueError, OSError, KeyError):
                profile = vpn.load_profile(profile_name)
                if any((profile.get(section) or {}).get(field) == name for section, field in vpn_fields(profile)):
                    found.append({"kind": "vpn", "what": f"the VPN profile {profile_name}", "name": profile_name})
                if name in vpn_own_secrets(profile):
                    found.append({"kind": "vpn-own", "what": f"the VPN profile {profile_name} (its own key)",
                                  "name": profile_name})
    with contextlib.suppress(Exception):  # noqa: BLE001
        for endpoint, record in sorted(module("llm").load_store()["endpoints"].items()):
            headers = record.get("headers") if isinstance(record, dict) else None
            named = [record.get("api_key_secret")] if isinstance(record, dict) else []
            named += list(headers.values()) if isinstance(headers, dict) else []
            if name in named:
                found.append({"kind": "llm", "what": f"the LLM endpoint {endpoint} (sandbox-ai llm add {endpoint} ...)",
                              "name": endpoint})
    with contextlib.suppress(Exception):  # noqa: BLE001
        mcp = module("mcp")
        path = mcp.config_file()
        document = json.loads(path.read_text()) if path.is_file() else {}
        servers = document.get("servers") if isinstance(document.get("servers"), dict) else document
        for server, definition in sorted(servers.items() if isinstance(servers, dict) else []):
            secrets = definition.get("env_secrets") if isinstance(definition, dict) else None
            if isinstance(secrets, dict) and name in secrets.values():
                found.append({"kind": "mcp", "what": f"the MCP server {server} (sandbox-ai mcp add {server} ... --env "
                                                     f"VARIABLE@{name})", "name": server})
    for path, document, trusted in project_files():
        named = options_naming(document.get("options", []), name)
        if named:
            found.append({"kind": "project", "what": f"{path} ({', '.join(named)})", "path": str(path),
                          "trusted": trusted})
    sessions = module("sessions")
    for key, shown in kept_starts():
        with contextlib.suppress(OSError, ValueError, RecursionError):
            named = options_naming(sessions.load(key).get("options", []), name)
            if named:
                found.append({"kind": "start", "what": f"the start {shown} ({', '.join(named)})", "id": key,
                              "name": shown})
    return found


def rename_secret_references(old, new):
    """Every reference to the secret OLD that sandbox-ai can change safely now names NEW. Returns (changed, left):
    two lists of texts - what was changed, and what still names OLD with the reason."""
    changed, left = [], []
    for name, key in (("git_logins", "git-logins"), ("publish_logins", "publish-logins")):
        loaded = module(name)
        try:
            with loaded.settings_lock(loaded.path()):
                logins = loaded.load()
                hits = [entry for entry in logins if entry.get("secret") == old]
                for entry in hits:
                    entry["secret"] = new
                if hits:
                    loaded.save(logins)
                    changed += [f"the Git login for {entry['url']}" if key == "git-logins" else
                                f"the publish login {entry['kind']}" for entry in hits]
        except (ValueError, OSError) as exc:
            left.append(f"sandbox-ai {key}: not read ({exc})")
    identities = module("identities")
    try:
        with identities.settings_lock(identities.path()):
            known = identities.load()
            hits = []
            for identity, data in sorted(known.items()):
                entries = [e for e in list(data.get("git", [])) + list(data.get("publish", []))
                           if isinstance(e, dict) and e.get("secret") == old]
                for entry in entries:
                    entry["secret"] = new
                if entries:
                    hits.append(identity)
            if hits:
                identities.save(known)
                changed += [f"the identity {identity}" for identity in hits]
    except (ValueError, OSError) as exc:
        left.append(f"sandbox-ai identities: not read ({exc})")
    try:
        vpn = module("vpn")
        profiles = vpn.list_profiles()
    except Exception as exc:  # noqa: BLE001 - the VPN module not loadable here: said, the rename goes on
        profiles, vpn = [], None
        left.append(f"VPN profiles: not read ({exc})")
    for profile_name in profiles:
        try:
            profile = vpn.load_profile(profile_name)
        except (ValueError, OSError, KeyError):
            continue
        fields = [(section, field) for section, field in vpn_fields(profile)
                  if (profile.get(section) or {}).get(field) == old]
        if fields:
            try:
                for section, field in fields:
                    profile[section][field] = new
                vpn.save_profile(profile)
                changed.append(f"the VPN profile {profile_name}")
            except (ValueError, OSError) as exc:
                left.append(f"the VPN profile {profile_name}: not written ({exc})")
    for path, document, trusted in project_files():
        options, count = renamed_options(document.get("options", []), old, new)
        if not count:
            continue
        if not trusted:
            left.append(f"{path}: changed since you trusted it, not touched (edit it, then: sandbox-ai wizard trust "
                        f"{path})")
            continue
        try:
            write_project(path, {**document, "options": options})
            changed.append(f"{path} (trusted again as written)")
        except (ValueError, OSError) as exc:
            left.append(f"{path}: not written ({getattr(exc, 'strerror', None) or exc})")
    sessions = module("sessions")
    for key, shown in kept_starts():
        try:
            if not options_naming(sessions.load(key).get("options", []), old):
                continue
            with sessions.session_lock(key):
                data = sessions.load(key)
                data["options"], count = renamed_options(data.get("options", []), old, new)
                if count:
                    sessions.save(key, data)
                    changed.append(f"the start {shown} (its options for the next resume)")
        except (OSError, ValueError, RecursionError) as exc:
            left.append(f"the start {shown}: {exc}")
    for reference in secret_references(old):
        if reference["kind"] in ("llm", "mcp", "vpn-own"):
            left.append(reference["what"])
    return changed, left


# --- a Git server's references ------------------------------------------------------------------------------------

def url_references(url):
    """What else names the Git server URL (texts): an identity's login, a trusted project file's or a kept start's
    --git-token-* option. The Git logins list itself is the caller's."""
    found = []
    with contextlib.suppress(ValueError, OSError):
        for identity, data in sorted(module("identities").load().items()):
            if any(isinstance(e, dict) and same_server(e.get("url"), url) for e in data.get("git", [])):
                found.append(f"the identity {identity} (sandbox-ai identities login {identity} URL --secret NAME)")
    for path, document, _trusted in project_files():
        named = options_for_url(document.get("options", []), url)
        if named:
            found.append(f"{path} ({', '.join(named)})")
    sessions = module("sessions")
    for key, shown in kept_starts():
        with contextlib.suppress(OSError, ValueError, RecursionError):
            named = options_for_url(sessions.load(key).get("options", []), url)
            if named:
                found.append(f"the start {shown} ({', '.join(named)})")
    return found


def running_starts():
    """[(run id, shown name)] of the agent sandboxes that run now (they keep what they were started with)."""
    with contextlib.suppress(Exception):  # noqa: BLE001 - a note only
        net = module("net")
        return [(run, str(net.label(run, record))) for run, record in sorted(net.running().items())
                if record.get("kind") in (None, "agent")]
    return []
