#!/usr/bin/env python3
"""sandbox-ai llm: LLM servers for the agents, local or remote, kept by name.

  sandbox-ai llm add NAME --kind KIND --url URL [--api-key-secret SECRET] [--header-secret HEADER=SECRET]
                     [--ca FILE] [--vpn PROFILE] [--model MODEL] [--anthropic] [--insecure-http] [--replace]
  sandbox-ai llm list [--json]
  sandbox-ai llm show NAME [--json]
  sandbox-ai llm remove NAME
  sandbox-ai llm models NAME [--sort name|size|loaded] [--json]
  sandbox-ai llm check NAME [--agent AGENT] [--model MODEL]
  sandbox-ai llm status [NAME] [--json] [--watch SECONDS]
  sandbox-ai llm loaded NAME [--json]
  sandbox-ai llm load NAME MODEL [--context TOKENS] [--ttl SECONDS] [--force]
  sandbox-ai llm unload NAME MODEL|INSTANCE [--force]
  sandbox-ai llm detect [--address IP]... [--json]
  sandbox-ai llm set NAME [--model MODEL] [--param KEY=VALUE]... [--force-param KEY=VALUE]... [--unset KEY]...
                     [--reset] [--loop-guard on|off|LAYER=VALUE]... [--evict never|allow] [--memory GB]
  sandbox-ai llm params [NAME | --kind KIND] [--json]
  sandbox-ai llm profiles [MODEL] [--agent AGENT] [--json]
  sandbox-ai llm recommend [USE_CASE] [--use-case NAME] [--llm|--server NAME] [--agent AGENT] [--host HOST] [--json]

KIND is ollama, lmstudio, llamacpp, vllm or openai-compatible. URL is the server's root, with a path
prefix if a gateway puts one in front (https://gw.example.org/ollama); each agent gets the API path it
needs. https is checked against the system's CAs (or --ca FILE); plain http goes only to the host's
loopback (127.0.0.1 or localhost, reached with --allow-host-port), private, CGNAT/Tailscale
(100.64.0.0/10) and unique-local IPv6 addresses, unless --insecure-http.

A start with --llm NAME[:MODEL] opens exactly that server's address and port and configures the agent
for it, the server's other models selectable in the agent (--llm-models all|MODEL,...); --llm NAME:MODEL,MODEL...
names several itself (the first is the agent's; only these are offered).
OpenRouter is one: llm add openrouter -k openai-compatible -u https://openrouter.ai/api/v1 -s SECRET -m MODEL
(with -a, its Anthropic-compatible API, for Claude Code too).
The key (a secret of sandbox-ai secrets, never stored here) is added by the connection proxy on the wire,
so the agent only ever sees a placeholder. Endpoints are kept in ~/.config/sandbox-ai/llm.json. An endpoint
added with --vpn is checked, listed and watched through that VPN (llm check|models|status).

llm status shows what each server has loaded and uses now, as far as its API reports it: Ollama the loaded models
with their VRAM and RAM, vLLM the KV cache and the requests running and waiting, llama.cpp its slots (and requests
with --metrics; memory in router mode), LM Studio the loaded models. The host's total RAM and VRAM are not reported by
these APIs (llama.cpp's router mode excepted).

llm loaded lists the loaded models with their instance, context, memory and how they stay loaded (LM Studio: JIT
with an idle TTL, which Auto-Evict may unload for another model, or explicit), and the running sandboxes using them;
llm load loads one explicitly (LM Studio, Ollama, llama.cpp in router mode; a JIT instance is replaced, never held
twice in memory) and llm unload unloads one; an unload, or a load that replaces what is loaded, is refused while a
running sandbox uses the model, unless --force.

Standard library only.
"""
import argparse
import base64
import contextlib
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error

LOADING = threading.RLock()  # the lazily made request handlers
HERE = Path(__file__).resolve().parent


def launch_log():
    """launchlog.py, the one copy of this process (launcher.log; DEBUG lines with SANDBOX_AI_DEBUG=1)."""
    loaded = sys.modules.get("sandbox_ai_launchlog")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_launchlog", HERE / "launchlog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = sys.modules.setdefault("sandbox_ai_launchlog", module)
    return loaded


LOG = launch_log()


_filelock = None


def settings_lock(file, timeout=None):
    """The lock of a read-modify-write of the settings file FILE (filelock.py): commands that run at the same time
    never lose each other's change. TIMEOUT: seconds to wait for it (filelock's own by default)."""
    global _filelock
    if _filelock is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_filelock", HERE / "filelock.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _filelock = loaded
    return _filelock.locked(file) if timeout is None else _filelock.locked(file, timeout)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
AGENTS = ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi")  # the launcher's agents (a test keeps both the same)
SECRET = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
HEADER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")
VPN_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")  # as sandbox-ai vpn names them (vpn.PROFILE_NAME)
# Model ids as servers report them ("qwen2.5-coder:7b", "org/Model-GGUF"); ASCII, so they fit every config format.
MODEL_NAME = re.compile(r"[\w.:/@+-]{1,200}", re.ASCII)
# Headers the proxy cannot set for a server: they frame the request or route it.
RESERVED_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer",
                    "expect", "keep-alive", "proxy-authorization", "proxy-connection"}
KINDS = {
    "ollama": {"label": "Ollama", "port": 11434},
    "lmstudio": {"label": "LM Studio", "port": 1234},
    "llamacpp": {"label": "llama.cpp server", "port": 8080},
    "vllm": {"label": "vLLM", "port": 8000},
    "openai-compatible": {"label": "OpenAI-compatible server", "port": None},
}
# Plain http is accepted to these (and the host's loopback): private, CGNAT (Tailscale), link-local IPv4,
# unique-local IPv6 (Tailscale's fd7a:115c:a1e0::/48 too).
LOCAL_NETWORKS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                                     "100.64.0.0/10", "169.254.0.0/16", "fc00::/7")]
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
STORE_LIMIT = 1 << 20
CA_LIMIT = 1 << 20
RESPONSE_LIMIT = 8 << 20
TIMEOUT = 10


def printable(text):
    """TEXT safe for a terminal: control, bidi and other non-printable characters escaped."""
    return "".join(c if c in "\n\t" or c.isprintable() else f"\\x{ord(c):02x}" if ord(c) < 256
                   else f"\\u{ord(c):04x}" for c in str(text))


def load_secret_store_module():
    spec = importlib.util.spec_from_file_location("sandbox_secret_store_llm", HERE / "secret_store.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_traffic_module():
    spec = importlib.util.spec_from_file_location("sandbox_llm_traffic_llm", HERE / "llm_traffic.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_vpn_module():
    spec = importlib.util.spec_from_file_location("sandbox_vpn_llm", HERE / "vpn.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- URLs and addresses --------------------------------------------------------------------------------

URL = re.compile(r"(https?)://(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(?::([0-9]{1,5}))?(/[A-Za-z0-9._~/-]{0,200})?")


def address_kind(ip):
    """"loopback", "local" (plain http allowed), "public" or "invalid" for an IP address."""
    ip = ipaddress.ip_address(ip)
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "loopback"
    # A link-local IPv6 address needs a scope ID, which no URL here carries.
    if ip.is_unspecified or ip.is_multicast or (ip.version == 4 and ip.is_reserved) or \
            (ip.version == 6 and ip.is_link_local):
        return "invalid"
    if any(ip.version == net.version and ip in net for net in LOCAL_NETWORKS):
        return "local"
    return "public"


def parse_url(url):
    """The parts of an endpoint URL: scheme, host (lower case; IPv6 without brackets), hostform (as in a URL),
    port, prefix ("" or "/a/b"), address (an IP literal, else None), loopback, origin and url (normalized),
    note (what was changed). ValueError names what is wrong."""
    match = URL.fullmatch(url) if isinstance(url, str) else None
    if not match:
        raise ValueError(f"expected http(s)://HOST[:PORT][/PREFIX], without user, query or fragment: "
                         f"{printable(url)!r}")
    scheme, host, port, path = match[1], match[2].lower(), match[3], match[4] or ""
    address = None
    if host.startswith("["):
        host = host[1:-1]
        try:
            address = ipaddress.IPv6Address(host)
        except ValueError:
            raise ValueError(f"invalid IPv6 address: {printable(url)!r}") from None
        host = str(address)
    elif re.fullmatch(r"[0-9.]+", host):
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError:
            raise ValueError(f"invalid IPv4 address: {printable(url)!r}") from None
        host = str(address)
    elif any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in host.split(".")):
        raise ValueError(f"invalid host name: {printable(url)!r}")
    default = 443 if scheme == "https" else 80
    port = int(port) if port else default
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {printable(url)!r}")
    segments = [part for part in path.split("/") if part]
    if any(part in (".", "..") for part in segments):
        raise ValueError(f"the path may not contain . or .. segments: {printable(url)!r}")
    note = None
    if segments and segments[-1] == "v1":
        # The root is kept; each agent adds the API path it needs (/v1 for OpenAI's API, /v1/messages ...).
        segments, note = segments[:-1], "the trailing /v1 was dropped: the URL names the server's root"
    prefix = "".join("/" + part for part in segments)
    loopback = host == "localhost" or (address is not None and address_kind(address) == "loopback")
    if loopback and host not in LOOPBACK_HOSTS:
        raise ValueError(f"only 127.0.0.1, ::1 or localhost reach the host's loopback: {printable(url)!r}")
    if address is not None and address_kind(address) == "invalid":
        raise ValueError(f"not an address a server can have: {printable(url)!r}")
    hostform = f"[{host}]" if ":" in host else host
    origin = f"{scheme}://{hostform}" + ("" if port == default else f":{port}")
    return {"scheme": scheme, "host": host, "hostform": hostform, "port": port, "prefix": prefix,
            "address": str(address) if address is not None else None, "loopback": loopback,
            "origin": origin, "url": origin + prefix, "note": note}


def plain_http_problem(parsed, addresses=None):
    """Why plain http to this endpoint is refused (None if it is fine): only the host's loopback and local
    addresses (private, CGNAT, unique-local) get it. ADDRESSES are a host name's resolved addresses; a name
    that is not resolved yet is checked when it is."""
    if parsed["scheme"] != "http" or parsed["loopback"]:
        return None
    candidates = [parsed["address"]] if parsed["address"] else list(addresses or [])
    public = [a for a in candidates if address_kind(a) != "local"]
    if public:
        return (f"plain http to {', '.join(public)} would cross networks you do not control unencrypted; use "
                "https, or --insecure-http if you accept that")
    return None


def plain_http_resolved(parsed, resolver=None):
    """(why plain http to PARSED is refused or None, whether its name did not resolve): plain_http_problem with a
    host name resolved here first, as `llm add` and the "test connection" before it check it. A name that does not
    resolve now is checked again at every start."""
    if parsed["scheme"] != "http" or parsed["loopback"] or parsed["address"]:
        return plain_http_problem(parsed), False
    try:
        addresses = (resolver or resolve)(parsed["host"], parsed["port"])
    except OSError:
        return None, True
    return plain_http_problem(parsed, addresses), False


# --- the store -----------------------------------------------------------------------------------------

def config_dir():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai"


def store_path():
    return config_dir() / "llm.json"


def ca_dir():
    return config_dir() / "llm"


def read_private(path, limit, what):
    """Bytes of a regular file of ours that only we can write; never through a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as exc:
        reason = "it is a symlink" if exc.errno == 40 else exc.strerror
        raise ValueError(f"{what} {path}: {reason}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"{what} {path} is not a regular file")
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(f"{what} {path} must belong to you and be writable by you only")
        data = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(data) > limit:
        raise ValueError(f"{what} {path} is larger than {limit >> 10} KiB")
    return data


def write_private(path, data):
    """Replace PATH atomically with DATA (mode 0600) in a 0700 directory; a symlink at PATH is replaced, not
    followed."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=".llm-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def load_store(path=None):
    """{"version": 1, "endpoints": {NAME: record}}; empty without a file."""
    path = Path(path or store_path())
    try:
        data = read_private(path, STORE_LIMIT, "The LLM endpoints file")
    except FileNotFoundError:
        return {"version": 1, "endpoints": {}}
    try:
        store = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ValueError(f"The LLM endpoints file {path} is not valid JSON") from None
    if not isinstance(store, dict) or store.get("version") != 1 or not isinstance(store.get("endpoints"), dict):
        raise ValueError(f"The LLM endpoints file {path}: expected version 1 with an object \"endpoints\"")
    return store


def save_store(store, path=None):
    write_private(Path(path or store_path()), (json.dumps(store, indent=2, sort_keys=True) + "\n").encode())


def check_record(name, record):
    """A stored endpoint, checked field by field (the file may have been edited by hand) and with its URL's
    parts under "parsed"; ValueError names the endpoint and the field."""
    where = f"LLM endpoint {printable(name)}"
    if not NAME.fullmatch(name) or not isinstance(record, dict):
        raise ValueError(f"{where}: invalid entry")
    known = {"kind", "url", "api_key_secret", "headers", "ca", "vpn", "model", "anthropic", "insecure_http",
             "models", "checked", "contexts", "vision", "params", "forced", "loop_guard", "evict", "memory_gb"}
    if set(record) - known:
        raise ValueError(f"{where}: unknown fields {sorted(set(record) - known)}")
    if not isinstance(record.get("kind"), str) or record["kind"] not in KINDS:  # (a list in a file edited by hand)
        raise ValueError(f"{where}: kind is one of {', '.join(KINDS)}")
    try:
        parsed = parse_url(record.get("url"))
    except ValueError as exc:
        raise ValueError(f"{where}: url: {exc}") from None
    secret = record.get("api_key_secret")
    if secret is not None and (not isinstance(secret, str) or not SECRET.fullmatch(secret)):
        raise ValueError(f"{where}: api_key_secret names a secret (letters, digits, _)")
    headers = record.get("headers", {})
    if not isinstance(headers, dict) or not all(
            isinstance(k, str) and HEADER.fullmatch(k) and k.lower() not in RESERVED_HEADERS and
            isinstance(v, str) and SECRET.fullmatch(v) for k, v in headers.items()):
        raise ValueError(f"{where}: headers maps header names to secret names")
    lowered = [k.lower() for k in headers]
    if len(set(lowered)) != len(lowered):
        raise ValueError(f"{where}: a header is named twice")
    if secret and {"authorization", "x-api-key"} & set(lowered):
        raise ValueError(f"{where}: the key goes into Authorization or x-api-key already; drop that header secret")
    ca = record.get("ca")
    if ca is not None and (not isinstance(ca, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}\.pem", ca)):
        raise ValueError(f"{where}: ca is the name of a file in {ca_dir()}")
    vpn = record.get("vpn")
    if vpn is not None and (not isinstance(vpn, str) or not VPN_PROFILE.fullmatch(vpn)):
        raise ValueError(f"{where}: vpn names a VPN profile")
    model = record.get("model")
    if model is not None and (not isinstance(model, str) or not MODEL_NAME.fullmatch(model)):
        raise ValueError(f"{where}: model is a model name")
    for flag in ("anthropic", "insecure_http"):
        if not isinstance(record.get(flag, False), bool):
            raise ValueError(f"{where}: {flag} is true or false")
    models = record.get("models", [])
    if not isinstance(models, list) or not all(isinstance(m, str) and MODEL_NAME.fullmatch(m) for m in models):
        raise ValueError(f"{where}: models is a list of model names")
    contexts = record.get("contexts", {})
    if not isinstance(contexts, dict) or not all(isinstance(m, str) and MODEL_NAME.fullmatch(m) and context_value(n)
                                                 for m, n in contexts.items()):
        raise ValueError(f"{where}: contexts maps model names to their context window (tokens)")
    vision = record.get("vision", [])
    if not isinstance(vision, list) or not all(isinstance(m, str) and MODEL_NAME.fullmatch(m) for m in vision):
        raise ValueError(f"{where}: vision is a list of model names (the models that read images)")
    check_params(where, record["kind"], record.get("params", {}), record.get("forced", {}), record.get("loop_guard"))
    if record.get("evict", "never") not in EVICT:
        raise ValueError(f"{where}: evict is never or allow")
    if record.get("memory_gb") is not None and memory_value(record["memory_gb"]) is None:
        raise ValueError(f"{where}: memory_gb is the server's memory in GB (a number from 1 to {MEMORY_MAX_GB})")
    if parsed["scheme"] == "http" and not record.get("insecure_http"):
        problem = plain_http_problem(parsed)
        if problem:
            raise ValueError(f"{where}: {problem}")
    return {"name": name, "kind": record["kind"], "url": parsed["url"], "api_key_secret": secret,
            "headers": dict(headers), "ca": ca, "vpn": vpn, "model": model,
            "anthropic": record.get("anthropic", False), "insecure_http": record.get("insecure_http", False),
            "models": list(models), "checked": record.get("checked"), "contexts": dict(contexts),
            "vision": list(vision), "params": {scope: dict(values) for scope, values in record.get("params", {}).items()},
            "forced": {scope: list(keys) for scope, keys in record.get("forced", {}).items()},
            "loop_guard": dict(record.get("loop_guard") or {}), "evict": record.get("evict", "never"),
            "memory_gb": record.get("memory_gb"), "parsed": parsed}


MEMORY_MAX_GB = 65536


def memory_value(value):
    """VALUE (GB) as the store keeps a server's memory (a whole number when it is one), or None when it is none."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= MEMORY_MAX_GB:
        return None
    return int(value) if float(value).is_integer() else round(float(value), 1)


def endpoint(name, path=None):
    """The checked endpoint NAME; ValueError if there is none."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(name_refusal(name))
    store = load_store(path)
    if name not in store["endpoints"]:
        known = ", ".join(sorted(store["endpoints"])) or "none yet"
        raise ValueError(f"no LLM endpoint {name} (known: {known}); add one with: sandbox-ai llm add {name} "
                         "--kind KIND --url URL")
    return check_record(name, store["endpoints"][name])


def record_of(endpoint):
    """The stored form of a checked endpoint."""
    record = {key: endpoint[key] for key in ("kind", "url", "api_key_secret", "headers", "ca", "vpn", "model",
                                              "anthropic", "insecure_http", "models", "checked", "contexts",
                                              "vision", "params", "forced", "loop_guard", "evict")
              if key in endpoint and (key != "evict" or endpoint[key] != "never")}
    kept = {key: value for key, value in record.items() if value is not None and value is not False
            and value != {} and value != []}
    return kept


def ca_text(endpoint):
    """The PEM text of the endpoint's own CA (copied into ca_dir() by `llm add`), or None. A test of a server that
    is not registered yet (discover_url) carries the text itself, not a file of the store."""
    if endpoint.get("ca_text"):
        return endpoint["ca_text"]
    if not endpoint.get("ca"):
        return None
    try:
        return read_private(ca_dir() / endpoint["ca"], CA_LIMIT, f"The CA of LLM endpoint {endpoint['name']}").decode()
    except FileNotFoundError:
        raise ValueError(f"LLM endpoint {endpoint['name']}: its CA {ca_dir() / endpoint['ca']} is missing; add the "
                         "endpoint again with --ca FILE") from None
    except UnicodeDecodeError:
        raise ValueError(f"LLM endpoint {endpoint['name']}: its CA is not PEM text") from None


def check_ca(text, source):
    """TEXT holds PEM certificates the TLS library accepts as CAs."""
    if "-----BEGIN CERTIFICATE-----" not in text:
        raise ValueError(f"--ca {source}: no PEM certificate (-----BEGIN CERTIFICATE-----)")
    import ssl
    try:
        ssl.create_default_context().load_verify_locations(cadata=text)
    except (ssl.SSLError, ValueError) as exc:
        raise ValueError(f"--ca {source}: not a usable CA certificate: {printable(exc)}") from None


def secret_values(endpoint, get=None):
    """(key or None, [(header, value)]) from sandbox-ai secrets; values are checked to be one header line and are
    never part of an error message."""
    get = get or load_secret_store_module().get
    key = None
    if endpoint.get("api_key_secret"):
        name = endpoint["api_key_secret"]
        key = get(name)
        if key is None:
            raise ValueError(f"LLM endpoint {endpoint['name']}: no secret {name}; keep the key with: "
                             f"sandbox-ai secrets set {name}")
        key = key.strip()
        # One header value, no spaces: what API keys look like, and nothing that could add a header.
        if not re.fullmatch(r"[\x21-\x7e]{1,4096}", key):
            raise ValueError(f"LLM endpoint {endpoint['name']}: the secret {name} does not look like an API key "
                             "(one word of printable ASCII)")
    headers = []
    for header, name in sorted(endpoint.get("headers", {}).items()):
        value = get(name)
        if value is None:
            raise ValueError(f"LLM endpoint {endpoint['name']}: no secret {name} for the header {header}; keep it "
                             f"with: sandbox-ai secrets set {name}")
        value = value.strip()
        if not re.fullmatch(r"[\x21-\x7e](?:[\x20-\x7e]{0,4094}[\x21-\x7e])?", value):
            raise ValueError(f"LLM endpoint {endpoint['name']}: the secret {name} is not one line of printable ASCII")
        headers.append((header, value))
    return key, headers


# --- talking to a server from the host (models, check) ---------------------------------------------------
# urllib.request, http.client and ssl are loaded where a request is made: they take about 20 ms to load, half of an
# `llm list` (which the app asks every host for), which talks to no server.

_handlers = None


def handlers():
    """(NoRedirect, PinnedHTTPConnection, PinnedHTTPHandler), made once, at the first request."""
    global _handlers
    if _handlers is None:
        with LOADING:
            if _handlers is None:
                _handlers = http_handlers()
    return _handlers


def http_handlers():
    import http.client
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        """A redirect would carry the key to wherever the server points; never followed."""

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise urllib.error.HTTPError(req.full_url, code, f"redirect to {printable(newurl)[:200]} not followed",
                                         headers, fp)

    class PinnedHTTPConnection(http.client.HTTPConnection):
        """A plain http connection to ADDRESS, the address checked (plain_http_problem) for the URL's host name: not
        one the name resolves to a moment later."""

        def __init__(self, host, address=None, **kwargs):
            super().__init__(host, **kwargs)
            self.pinned = address

        def connect(self):
            self.sock = socket.create_connection((self.pinned, self.port), self.timeout, self.source_address)

    class PinnedHTTPHandler(urllib.request.HTTPHandler):
        def __init__(self, address):
            super().__init__()
            self.address = address

        def http_open(self, req):
            return self.do_open(lambda host, **kwargs: PinnedHTTPConnection(host, address=self.address, **kwargs), req)

    return NoRedirect, PinnedHTTPConnection, PinnedHTTPHandler


def __getattr__(name):
    """llm.NoRedirect, llm.PinnedHTTPConnection and llm.PinnedHTTPHandler, made on first use."""
    names = ("NoRedirect", "PinnedHTTPConnection", "PinnedHTTPHandler")
    if name in names:
        return handlers()[names.index(name)]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def tls_context(endpoint):
    import ssl
    context = ssl.create_default_context()
    ca = ca_text(endpoint)
    if ca:
        context.load_verify_locations(cadata=ca)
    return context


class Client:
    """Requests to one endpoint from the host, with its key and headers, its CA, no proxy, no redirects. A key or
    header secret goes over plain http only where `llm add` accepts it: the host's loopback and local addresses, or an
    endpoint added with --insecure-http. The name is resolved and checked for every request, and the request goes to
    the address checked."""

    def __init__(self, endpoint, get=None, timeout=TIMEOUT, opener=None):
        self.endpoint, self.timeout = endpoint, timeout
        key, headers = secret_values(endpoint, get)
        self.secret = bool(key or headers)
        self.headers = {"Accept": "application/json", "User-Agent": "sandbox-ai"}
        if key:
            self.headers["Authorization"] = f"Bearer {key}"
        self.headers.update(dict(headers))
        parsed = endpoint["parsed"]
        self.pinned = bool(key or headers) and parsed["scheme"] == "http" and not parsed["loopback"] \
            and not endpoint.get("insecure_http")
        context = tls_context(endpoint) if parsed["scheme"] == "https" else None
        self.given = opener
        import urllib.request
        # A remote server must not be reached through the host's HTTP proxy variables.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), handlers()[0](),
                                                            urllib.request.HTTPSHandler(context=context))

    def checked_opener(self, resolver=None):
        """The opener for a request: for a keyed plain http one, after the host's addresses were checked now, one that
        connects to the address checked; ValueError where the key would cross networks unencrypted."""
        if not self.pinned:
            return self.opener
        parsed = self.endpoint["parsed"]
        try:
            addresses = [parsed["address"]] if parsed["address"] else (resolver or resolve)(parsed["host"],
                                                                                             parsed["port"])
        except OSError as exc:
            raise ValueError(f"{parsed['host']} does not resolve: {printable(exc)}") from None
        problem = plain_http_problem(parsed, addresses)
        if problem or not addresses:
            raise ValueError(f"{problem or 'no address'} (the API key would go with it; nothing was sent)")
        import urllib.request
        no_redirect, _, pinned_handler = handlers()
        return self.given or urllib.request.build_opener(urllib.request.ProxyHandler({}), no_redirect(),
                                                         pinned_handler(addresses[0]))

    def call(self, path, body=None):
        """(status, bytes) of GET PATH (POST with BODY, a dict) below the endpoint's root; OSError when there is no
        answer at all; ValueError for a key that would go over plain http to a public address."""
        import urllib.request
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers, **({"Content-Type": "application/json"} if data is not None else {}))
        request = urllib.request.Request(self.endpoint["url"] + path, data=data, headers=headers,
                                         method="GET" if data is None else "POST")
        opener = self.checked_opener()
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read(RESPONSE_LIMIT + 1)[:RESPONSE_LIMIT]
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read(65536) if exc.fp else b""

    def text(self, path):
        """The text answer of GET PATH (Prometheus' /metrics); ValueError as json() says it."""
        return self.json(path, parse=lambda data: data.decode("utf-8"))

    def json(self, path, parse=json.loads, body=None):
        """The JSON answer of GET PATH (POST with BODY, a dict); ValueError says what went wrong (never with a
        secret)."""
        import ssl
        begun = time.monotonic()
        where = f"{'POST' if body is not None else 'GET'} {self.endpoint['url']}{path}"
        try:
            status, data = self.call(path, body)
        except (OSError, ssl.SSLError) as exc:
            # (polled by the app's model server page: said once until it answers again)
            LOG.changed(("llm reached", self.endpoint["url"]), False, "llm",
                        f"{where}: no answer after {time.monotonic() - begun:.1f} s: "
                        f"{printable(getattr(exc, 'reason', exc))}", "WARNING")
            raise ValueError(f"{self.endpoint['url']}{path}: {printable(getattr(exc, 'reason', exc))}") from None
        if LOG.state(("llm reached", self.endpoint["url"])) is False:
            LOG.changed(("llm reached", self.endpoint["url"]), True, "llm", f"{self.endpoint['url']} answers again")
        LOG.log("DEBUG" if status == 200 else "INFO", "llm",
                f"{where}: HTTP {status} after {time.monotonic() - begun:.1f} s")
        if status in (401, 403):
            raise ValueError(f"{self.endpoint['url']}{path}: the server refused the key (HTTP {status})")
        if 300 <= status < 400:
            raise ValueError(f"{self.endpoint['url']}{path}: HTTP {status}, a redirect, which is not followed (it "
                             "would carry the key elsewhere); register the URL it points to")
        if status != 200:
            raise ValueError(f"{self.endpoint['url']}{path}: HTTP {status}")
        try:
            return parse(data)
        except (ValueError, RecursionError):
            raise ValueError(f"{self.endpoint['url']}{path}: the answer is not "
                             f"{'JSON' if parse is json.loads else 'text'}") from None


class VPNClient(Client):
    """Requests to an endpoint added with --vpn: sent through that profile's tunnel (a container of its own,
    vpn.channel), the name resolved by the VPN's resolvers. The same rules as from the host: the key over plain http
    only to a local address (the address checked in the tunnel), unless --insecure-http; no redirect followed."""

    def __init__(self, endpoint, tunnel, get=None, timeout=TIMEOUT):
        super().__init__(endpoint, get, timeout, opener=object())  # (no opener: every request goes through TUNNEL)
        self.tunnel = tunnel

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers, **({"Content-Type": "application/json"} if data is not None else {}))
        try:
            answer = self.tunnel.request(
                "http", wait=self.timeout + 15, timeout=self.timeout, url=self.endpoint["url"] + path,
                method="GET" if data is None else "POST", headers=headers,
                body=base64.b64encode(data).decode() if data is not None else None,
                ca=ca_text(self.endpoint) or None, secret=self.secret, limit=RESPONSE_LIMIT,
                plain_networks=None if self.endpoint.get("insecure_http") else [str(n) for n in LOCAL_NETWORKS])
        except ValueError as exc:
            raise OSError(f"through the VPN {self.endpoint.get('vpn')}: {exc}") from None
        return answer["status"], base64.b64decode(answer.get("body") or "")


@contextlib.contextmanager
def through_vpn(endpoint, podman_url=None):
    """The endpoint's VPN tunnel (vpn.channel) while the block runs, or None for an endpoint without --vpn."""
    if not endpoint.get("vpn"):
        yield None
        return
    with load_vpn_module().channel(endpoint["vpn"], podman_url) as tunnel:
        yield tunnel


def model_date(value):
    """The date of a model as its server gives it (OpenAI's "created": seconds since 1970; Ollama's "modified_at": ISO
    8601), as YYYY-MM-DD; "" when there is none."""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < 32503680000:
        return time.strftime("%Y-%m-%d", time.gmtime(value))
    if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value):
        return value[:10]
    return ""


def with_date(details, value):
    """DETAILS with the model's date (its version as the server knows it) appended."""
    date = model_date(value)
    return ", ".join(part for part in (details, date) if part)


def lmstudio_native(fetch):
    """{key: model} of LM Studio's own REST API (0.4 and later: GET /api/v1/models, {"models": [{key, type,
    size_bytes, params_string, quantization: {name}, max_context_length, loaded_instances: [{id, config:
    {context_length}}]}]}); {} where it has none (older, or behind a gateway)."""
    try:
        listed = fetch("/api/v1/models")["models"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if not isinstance(listed, list):
        return {}
    return {m["key"]: m for m in listed if isinstance(m, dict) and isinstance(m.get("key"), str)}


def lmstudio_quantization(model):
    value = model.get("quantization")
    return value.get("name") if isinstance(value, dict) else None


def discover_raw(kind, fetch):
    """Models of a server as [{name, size, loaded, details}] (untrusted: see clean_models). FETCH(PATH) returns the
    JSON of GET PATH below the server's root."""
    if kind == "ollama":
        tags = fetch("/api/tags")["models"]
        try:
            loaded = {model["name"] for model in fetch("/api/ps")["models"]}
        except (OSError, ValueError, KeyError, TypeError):
            loaded = None
        return [{"name": m["name"], "size": m.get("size"), "loaded": None if loaded is None else m["name"] in loaded,
                 "details": with_date(" ".join(v for v in (m.get("details", {}).get("parameter_size"),
                                                          m.get("details", {}).get("quantization_level")) if v),
                                      m.get("modified_at")),
                 "arch": m.get("details", {}).get("family"), "parameters": m.get("details", {}).get("parameter_size"),
                 "quantization": m.get("details", {}).get("quantization_level"),
                 # (its families: clip for a vision model, bert for an embedding model)
                 "families": m.get("details", {}).get("families")}
                for m in tags]
    if kind == "lmstudio":
        # LM Studio 0.4's own REST API (/api/v1/models) has each model's size (size_bytes) and its loaded instances;
        # the v0 API (state loaded/not-loaded, no size) names the models as before. Either may be missing.
        native = lmstudio_native(fetch)
        try:
            data = fetch("/api/v0/models")["data"]
            return [{"name": m["id"], "size": (native.get(m["id"]) or {}).get("size_bytes"),
                     "loaded": m.get("state") == "loaded",
                     "details": m.get("quantization", ""), "type": m.get("type"), "arch": m.get("arch"),
                     "parameters": (native.get(m["id"]) or {}).get("params_string"),
                     "quantization": m.get("quantization"), "context": m.get("max_context_length"),
                     "capabilities": ["vision"] if m.get("type") == "vlm" or (native.get(m["id"]) or {}).get("vision")
                     else []}
                    for m in data if m.get("type") in ("llm", "vlm")]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if native:
            return [{"name": key, "size": m.get("size_bytes"), "loaded": bool(m.get("loaded_instances")),
                     "details": "", "type": m.get("type"), "arch": m.get("architecture"),
                     "parameters": m.get("params_string"), "quantization": lmstudio_quantization(m),
                     "context": m.get("max_context_length"),
                     "capabilities": ["vision"] if m.get("vision") or (m.get("capabilities") or {}).get("vision")
                     is True else []}
                    for key, m in native.items() if m.get("type") == "llm"]
        # Older LM Studio, or one behind a gateway: the OpenAI-compatible listing only.
        return [{"name": m["id"], "size": None, "loaded": None, "details": with_date("", m.get("created"))}
                for m in fetch("/v1/models")["data"]]
    if kind == "vllm":
        # vLLM serves exactly the models it has loaded.
        return [{"name": m["id"], "size": None, "loaded": True,
                 "details": with_date(f"max_model_len {m['max_model_len']}" if m.get("max_model_len") else "",
                                      m.get("created")), "context": m.get("max_model_len")}
                for m in fetch("/v1/models")["data"]]
    if kind == "llamacpp":
        answer = fetch("/v1/models")
        result = []
        for m in answer["data"]:
            meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
            status = m.get("status") if isinstance(m.get("status"), dict) else {}
            # A single model is loaded; the router mode reports a status per model.
            loaded = status["value"] == "loaded" if isinstance(status.get("value"), str) else (True if not status else None)
            result.append({"name": m["id"], "size": meta.get("size"), "loaded": loaded,
                           "details": with_date(f"n_ctx_train {meta['n_ctx_train']}" if meta.get("n_ctx_train")
                                                else "", m.get("created")),
                           "parameters": params_text(meta.get("n_params"))})
        return result
    if kind == "openai-compatible":
        # OpenRouter and the like name each model's modalities (architecture: input/output) and its context.
        return [{"name": m["id"], "size": None, "loaded": None, "details": with_date("", m.get("created")),
                 "capabilities": modality_capabilities(m.get("architecture")),
                 "context": m.get("context_length")}
                for m in fetch("/v1/models")["data"]]
    raise ValueError(f"Unknown server type: {kind}")


CAPABILITIES = ("text", "code", "vision", "image", "embedding", "reasoning", "tools")


def params_text(value):
    """A parameter count the server gives as a number (llama.cpp's n_params): "7.2B", "350M"; None otherwise."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return None
    return f"{value / 1e9:.1f}B" if value >= 1e9 else f"{value / 1e6:.0f}M"


def modality_capabilities(architecture):
    """The capabilities an OpenAI-compatible listing's ARCHITECTURE (OpenRouter: input_modalities, output_modalities,
    modality "text+image->text") names: vision for an image input, image for an image output."""
    if not isinstance(architecture, dict):
        return []
    inputs = architecture.get("input_modalities") if isinstance(architecture.get("input_modalities"), list) else []
    outputs = architecture.get("output_modalities") if isinstance(architecture.get("output_modalities"), list) else []
    modality = architecture.get("modality") if isinstance(architecture.get("modality"), str) else ""
    given, _, made = modality.partition("->")
    found = []
    if "image" in inputs or "image" in given:
        found.append("vision")
    if "image" in outputs or "image" in made:
        found.append("image")
    if "text" in outputs or "text" in made:
        found.append("text")
    return found


def clean_models(models):
    """Server answers are untrusted: keep well-typed entries, make text terminal-safe."""
    result = []
    for model in models:
        name = model.get("name") if isinstance(model, dict) else None
        if not isinstance(name, str) or not MODEL_NAME.fullmatch(name):
            continue
        size, loaded, details = model.get("size"), model.get("loaded"), model.get("details")
        context = model.get("context")

        def word(key, limit=40):  # a short, terminal-safe text field the server may or may not give
            value = model.get(key)
            return printable(value)[:limit] if isinstance(value, str) and value.strip() else None
        entry = {"name": name,
                 "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                 "loaded": loaded if isinstance(loaded, bool) else None,
                 "details": printable(details)[:80] if isinstance(details, str) else ""}
        # The columns a front end shows when the server gives them (LM Studio, Ollama): only when known.
        extra = {"type": word("type", 20), "arch": word("arch"), "parameters": word("parameters", 20),
                 "quantization": word("quantization", 20),
                 "context": context if isinstance(context, int) and not isinstance(context, bool)
                 and 0 < context < 1 << 31 else None}
        # What the server says the model can do (a list of CAPABILITIES' words), and Ollama's families (short words)
        capabilities = model.get("capabilities") if isinstance(model.get("capabilities"), list) else []
        extra["capabilities"] = [c for c in dict.fromkeys(capabilities) if c in CAPABILITIES] or None
        families = model.get("families") if isinstance(model.get("families"), list) else []
        extra["families"] = [printable(f)[:20] for f in families[:8] if isinstance(f, str) and f.strip()] or None
        result.append({**entry, **{key: value for key, value in extra.items() if value is not None}})
    return result


def discover(kind, fetch):
    """Models of a server, checked (see clean_models); ValueError when the listing fails."""
    try:
        return clean_models(discover_raw(kind, fetch))
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"unexpected answer from the {KINDS[kind]['label']}: {printable(exc)}") from None


# --- what a server has loaded and uses now (llm status, wizard models --status) -----------------------------------
#
# Only what each server's API reports, never a guess:
#   Ollama     /api/ps: the loaded models, the memory each takes ("size") and how much of it is in VRAM ("size_vram"),
#              their context ("context_length") and when they are unloaded ("expires_at"). No KV cache, no requests,
#              no totals of the host.
#   LM Studio  /api/v1/models (0.4 and later): the loaded instances with their context; /api/v0/models: loaded or
#              not. No memory use, no KV cache, no requests.
#   llama.cpp  /v1/models (its size; in router mode its status and the memory a loaded model allocated per device,
#              with the devices' total and free memory), /props (slots, context), /slots (which slots process a
#              request), /metrics with --metrics (requests processing and deferred). No KV cache use.
#   vLLM       /v1/models (it serves what it has loaded), /metrics: KV cache use (kv_cache_usage_perc; before V1
#              gpu_cache_usage_perc and cpu_cache_usage_perc), requests running and waiting. No memory, no sizes.
#   any other OpenAI-compatible server: /v1/models only - what is loaded and what it uses is not reported.

STATUS_TIMEOUT = 5  # seconds per request of a status read
STATUS_BUDGET = 12  # seconds after which a status read asks nothing more (what it has is shown)
USAGE_KEYS = ("vram_bytes", "ram_bytes", "vram_total_bytes", "vram_free_bytes", "kv_cache_percent",
              "cpu_kv_cache_percent", "requests_running", "requests_waiting", "slots_total")
# What a front end says "not reported" for, when the status lacks it.
REPORTABLE = ("loaded", "memory", "kv_cache", "requests", "totals")
METRIC = re.compile(r"([A-Za-z_:][A-Za-z0-9_:]*)(?:\{[^}\n]{0,2000}\})?[ \t]+([^ \t]+)")


def parse_metrics(text, names):
    """{name: [value, ...]} of the Prometheus text exposition TEXT, for NAMES only (one value per sample: labels set
    apart engines and models); finite numbers only."""
    found = {}
    for line in str(text or "").splitlines()[:50000]:
        if not line or line.startswith("#"):
            continue
        match = METRIC.match(line)
        if not match or match[1] not in names:
            continue
        try:
            value = float(match[2])
        except ValueError:
            continue
        if value == value and abs(value) != float("inf"):
            found.setdefault(match[1], []).append(value)
    return found


def count(value):
    """VALUE if it is a whole number of bytes (or of anything) a server may report, else None."""
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1 << 62 else None


class Remembered:
    """FETCH(PATH), each path asked once (the listing and the status read share the answers), and after DEADLINE
    (time.monotonic()) nothing more is asked: what the status has then is shown."""

    def __init__(self, fetch, deadline=None):
        self.fetch, self.deadline, self.kept = fetch, deadline, {}

    def __call__(self, path):
        if path not in self.kept:
            if self.deadline is not None and time.monotonic() > self.deadline:
                raise ValueError(f"{path}: not asked, the server took too long already")
            try:
                self.kept[path] = (True, self.fetch(path))
            except Exception as exc:  # noqa: BLE001 - kept and raised again for every asker
                self.kept[path] = (False, exc)
        ok, value = self.kept[path]
        if ok:
            return value
        raise value


def optional(fetch, path, default=None):
    """FETCH(PATH), or DEFAULT where the server does not answer it (an API it lacks, an option it was not started
    with)."""
    try:
        return fetch(path)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
        return default


def status_raw(kind, fetch, fetch_text, models):
    """{"loaded": [{name, size?, memory_bytes?, vram_bytes?, ram_bytes?, context?, expires_at?}] or None (not
    reported), "usage": {...}, "reported_by": [...]} of a server of KIND whose MODELS (clean_models) were just
    listed with FETCH; FETCH_TEXT(PATH) reads a text answer (/metrics). Untrusted: see clean_status."""
    sizes = {m["name"]: m["size"] for m in models}
    usage, by = {}, []
    loaded = None
    if kind == "ollama":
        running = optional(fetch, "/api/ps")
        if isinstance(running, dict) and isinstance(running.get("models"), list):
            loaded, by = [], ["Ollama /api/ps"]
            for m in running["models"]:
                if not isinstance(m, dict):
                    continue
                memory, vram = count(m.get("size")), count(m.get("size_vram"))
                loaded.append({"name": m.get("name"), "size": sizes.get(m.get("name")), "memory_bytes": memory,
                               "vram_bytes": vram, "ram_bytes": memory - vram if memory is not None and
                               vram is not None and memory >= vram else None,
                               "context": m.get("context_length"), "expires_at": m.get("expires_at")})
            for key in ("vram_bytes", "ram_bytes"):
                values = [entry[key] for entry in loaded if entry[key] is not None]
                if values or not loaded:
                    usage[key] = sum(values)
    elif kind == "lmstudio":
        native = lmstudio_native(fetch)
        if native:
            loaded, by = [], ["LM Studio /api/v1/models"]
            for key, m in native.items():
                for instance in m.get("loaded_instances") or []:
                    if isinstance(instance, dict):
                        config = instance.get("config") if isinstance(instance.get("config"), dict) else {}
                        loaded.append({"name": instance.get("id") or key, "size": count(m.get("size_bytes")),
                                       "context": config.get("context_length"),
                                       # an idle TTL: loaded JIT, which Auto-Evict may unload for another model
                                       "jit": instance_ttl(instance) is not None})
        elif any(m["loaded"] is not None for m in models):
            loaded, by = [{"name": m["name"], "size": m["size"], "context": None} for m in models if m["loaded"]], \
                ["LM Studio /api/v0/models"]
    elif kind == "llamacpp":
        listing = optional(fetch, "/v1/models", {})
        raw = {m.get("id"): m for m in (listing.get("data") if isinstance(listing, dict) else None) or []
               if isinstance(m, dict)}
        loaded, by = [], ["llama.cpp /v1/models"]
        devices = {}
        for m in models:
            if m["loaded"] is not True:
                continue
            entry = {"name": m["name"], "size": m["size"], "context": None}
            memory = raw.get(m["name"], {}).get("memory")
            effective = memory.get("effective") if isinstance(memory, dict) else None
            if isinstance(effective, dict) and isinstance(effective.get("devices"), list):
                # Router mode: what the instance allocated, per device (GPU) and on the host (RAM).
                vram = ram = 0
                for device in effective["devices"]:
                    if not isinstance(device, dict):
                        continue
                    used = sum(count(device.get(k)) or 0 for k in ("model", "context", "compute"))
                    if device.get("kind") == "host":
                        ram += used
                    else:
                        vram += used
                        if isinstance(device.get("name"), str) and count(device.get("total")) is not None:
                            devices[device["name"]] = (count(device["total"]), count(device.get("free")))
                entry.update(vram_bytes=vram, ram_bytes=ram, memory_bytes=vram + ram)
                if isinstance(effective.get("n_ctx"), int):
                    entry["context"] = effective["n_ctx"]
            loaded.append(entry)
        if any("vram_bytes" in entry for entry in loaded):
            usage["vram_bytes"] = sum(entry.get("vram_bytes") or 0 for entry in loaded)
            usage["ram_bytes"] = sum(entry.get("ram_bytes") or 0 for entry in loaded)
        if devices:
            usage["vram_total_bytes"] = sum(total for total, _ in devices.values())
            if all(free is not None for _, free in devices.values()):
                usage["vram_free_bytes"] = sum(free for _, free in devices.values())
        props = optional(fetch, "/props")
        if isinstance(props, dict):
            by.append("llama.cpp /props")
            if count(props.get("total_slots")):
                usage["slots_total"] = props["total_slots"]
            settings = props.get("default_generation_settings")
            n_ctx = settings.get("n_ctx") if isinstance(settings, dict) else None
            if len(loaded) == 1 and loaded[0]["context"] is None and isinstance(n_ctx, int):
                loaded[0]["context"] = n_ctx
        slots = optional(fetch, "/slots")
        if isinstance(slots, list) and slots:
            by.append("llama.cpp /slots")
            usage["slots_total"] = len(slots)
            usage["requests_running"] = sum(1 for s in slots if isinstance(s, dict) and s.get("is_processing") is True)
        metrics = parse_metrics(optional(fetch_text, "/metrics", ""),
                                {"llamacpp:requests_processing", "llamacpp:requests_deferred"})
        if metrics:
            by.append("llama.cpp /metrics")
            for name, key in (("llamacpp:requests_processing", "requests_running"),
                              ("llamacpp:requests_deferred", "requests_waiting")):
                if name in metrics:
                    usage[key] = int(sum(metrics[name]))
    elif kind == "vllm":
        listing = optional(fetch, "/v1/models", {})
        raw = {m.get("id"): m for m in (listing.get("data") if isinstance(listing, dict) else None) or []
               if isinstance(m, dict)}
        loaded, by = [{"name": m["name"], "size": None, "context": raw.get(m["name"], {}).get("max_model_len")}
                      for m in models], ["vLLM /v1/models"]
        metrics = parse_metrics(optional(fetch_text, "/metrics", ""), {
            "vllm:num_requests_running", "vllm:num_requests_waiting", "vllm:kv_cache_usage_perc",
            "vllm:gpu_cache_usage_perc", "vllm:cpu_cache_usage_perc"})
        if metrics:
            by.append("vLLM /metrics")
            for name, key in (("vllm:num_requests_running", "requests_running"),
                              ("vllm:num_requests_waiting", "requests_waiting")):
                if name in metrics:
                    usage[key] = int(sum(metrics[name]))
            # A fraction (1 is 100 %); with several engines the fullest one.
            cache = metrics.get("vllm:kv_cache_usage_perc") or metrics.get("vllm:gpu_cache_usage_perc")
            if cache:
                usage["kv_cache_percent"] = round(100 * max(cache), 1)
            if metrics.get("vllm:cpu_cache_usage_perc"):
                usage["cpu_kv_cache_percent"] = round(100 * max(metrics["vllm:cpu_cache_usage_perc"]), 1)
    return {"loaded": loaded, "usage": usage, "reported_by": by if loaded is not None or usage else []}


def clean_status(status):
    """A status_raw answer checked (a server's answer is untrusted): names as models are named, numbers only where
    numbers belong, texts terminal-safe; "not_reported" names what the server's API does not tell (REPORTABLE)."""
    loaded = None
    if isinstance(status.get("loaded"), list):
        loaded = []
        for entry in status["loaded"][:200]:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not MODEL_NAME.fullmatch(name):
                continue
            item = {"name": name}
            for key in ("size", "memory_bytes", "vram_bytes", "ram_bytes"):
                if count(entry.get(key)) is not None:
                    item[key] = entry[key]
            context = entry.get("context")
            if isinstance(context, int) and not isinstance(context, bool) and 0 < context < 1 << 31:
                item["context"] = context
            if isinstance(entry.get("expires_at"), str) and entry["expires_at"].strip():
                item["expires_at"] = printable(entry["expires_at"].strip())[:40]
            if entry.get("jit") is True:
                item["jit"] = True
            loaded.append(item)
    usage = {}
    raw = status.get("usage") if isinstance(status.get("usage"), dict) else {}
    for key in USAGE_KEYS:
        value = raw.get(key)
        if key.endswith("_percent"):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100:
                usage[key] = round(float(value), 1)
        elif count(value) is not None:
            usage[key] = value
    missing = []
    if loaded is None:
        missing.append("loaded")
    if "vram_bytes" not in usage and "ram_bytes" not in usage:
        missing.append("memory")
    if "kv_cache_percent" not in usage:
        missing.append("kv_cache")
    if "requests_running" not in usage and "requests_waiting" not in usage:
        missing.append("requests")
    if "vram_total_bytes" not in usage:
        missing.append("totals")
    return {"loaded": loaded, "usage": usage,
            "reported_by": [printable(v)[:60] for v in status.get("reported_by") or [] if isinstance(v, str)][:8],
            "not_reported": missing}


def now_text():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def discover_status(kind, fetch, fetch_text, budget=STATUS_BUDGET):
    """(models, status) of a server of KIND: its models (as discover lists them; ValueError when it does not answer)
    and what it has loaded and uses now (clean_status, with "read_at"). Each path is asked once; after BUDGET
    seconds nothing more is asked."""
    deadline = time.monotonic() + budget
    fetch, fetch_text = Remembered(fetch, deadline), Remembered(fetch_text, deadline)
    models = discover(kind, fetch)
    try:
        status = clean_status(status_raw(kind, fetch, fetch_text, models))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"unexpected answer from the {KINDS[kind]['label']}: {printable(exc)}") from None
    return models, dict(status, read_at=now_text())


def endpoint_status(endpoint, get=None, client=None):
    """What the registered ENDPOINT has loaded and uses now, as `llm status --json` gives it per server: {name, kind,
    url, reachable, error, read_at, models (how many it lists), loaded, usage, reported_by, not_reported}. Never
    raises for the server: what went wrong is its "error". The models are not remembered (a watch reads often)."""
    base = {"name": endpoint["name"], "kind": endpoint["kind"], "url": endpoint["url"]}
    try:
        client = client or Client(endpoint, get, timeout=STATUS_TIMEOUT)
        models, status = discover_status(endpoint["kind"], client.json, client.text)
    except (OSError, ValueError) as exc:
        return {**base, "reachable": False, "error": printable(exc)[:600], "read_at": now_text(), "models": None,
                "loaded": None, "usage": {}, "reported_by": [], "not_reported": list(REPORTABLE)}
    return {**base, "reachable": True, "error": None, "models": len(models), **status}


def vpn_status(endpoint, get=None):
    """endpoint_status, through the endpoint's VPN when it was added with --vpn (a tunnel brought up for the
    reading: some seconds); a VPN that does not come up is the reading's error."""
    if not endpoint.get("vpn"):
        return endpoint_status(endpoint, get)
    try:
        with through_vpn(endpoint) as tunnel:
            return endpoint_status(endpoint, get, client=VPNClient(endpoint, tunnel, get, timeout=STATUS_TIMEOUT))
    except (OSError, ValueError) as exc:
        return {"name": endpoint["name"], "kind": endpoint["kind"], "url": endpoint["url"], "reachable": False,
                "error": printable(f"VPN {endpoint['vpn']}: {exc}")[:600], "read_at": now_text(), "models": None,
                "loaded": None, "usage": {}, "reported_by": [], "not_reported": list(REPORTABLE)}


def status_lines(entry):
    """`llm status` as text for one server (ENTRY: endpoint_status, or a status of wizard models --status)."""
    head = f"{entry['name']}: {KINDS[entry['kind']]['label']} at {entry['url']}" if entry.get("name") else None
    lines = [head] if head else []
    when = f" (read {entry['read_at']})" if entry.get("read_at") else ""
    if entry.get("reachable") is False:
        return [printable(line) for line in lines + [f"  not reachable{when}: {entry.get('error') or '?'}"]]
    loaded, usage = entry.get("loaded"), entry.get("usage") or {}
    if entry.get("models") is not None:
        shown = f"{len(loaded)} loaded" if loaded is not None else "what is loaded is not reported"
        lines.append(f"  reachable{when}: {entry['models']} model(s), {shown}")
    elif entry.get("read_at"):
        lines.append(f"  read {entry['read_at']}")
    for model in loaded or []:
        parts = []
        if model.get("memory_bytes") is not None:
            split = [f"{format_size(model[k])} {label}" for k, label in (("vram_bytes", "VRAM"), ("ram_bytes", "RAM"))
                     if model.get(k) is not None]
            parts.append(f"{format_size(model['memory_bytes'])} in memory" + (f" ({', '.join(split)})" if split else ""))
        elif model.get("size") is not None:
            parts.append(f"size {format_size(model['size'])}")
        if model.get("context"):
            parts.append(f"context {model['context']}")
        if model.get("expires_at"):
            parts.append(f"unloaded at {model['expires_at']}")
        if model.get("jit"):
            parts.append(f"JIT: {JIT_WARNING}")
        lines.append(f"  loaded: {model['name']}" + (f"  {', '.join(parts)}" if parts else ""))
    numbers = []
    if "vram_bytes" in usage:
        total = f" of {format_size(usage['vram_total_bytes'])}" if "vram_total_bytes" in usage else ""
        numbers.append(f"VRAM {format_size(usage['vram_bytes'])}{total}")
    if "ram_bytes" in usage:
        numbers.append(f"RAM {format_size(usage['ram_bytes'])}")
    if "vram_free_bytes" in usage:
        numbers.append(f"{format_size(usage['vram_free_bytes'])} VRAM free")
    if "kv_cache_percent" in usage:
        numbers.append(f"KV cache {usage['kv_cache_percent']}%")
    if "cpu_kv_cache_percent" in usage:
        numbers.append(f"CPU KV cache {usage['cpu_kv_cache_percent']}%")
    if "requests_running" in usage or "requests_waiting" in usage:
        numbers.append("requests " + ", ".join(f"{usage[k]} {word}" for k, word in (
            ("requests_running", "running"), ("requests_waiting", "waiting")) if k in usage))
    if "slots_total" in usage:
        numbers.append(f"{usage['slots_total']} slot(s)")
    if numbers:
        lines.append(f"  usage: {', '.join(numbers)}")
    words = {"loaded": "loaded models", "memory": "VRAM/RAM use", "kv_cache": "KV cache", "requests": "requests",
             "totals": "the host's total RAM/VRAM"}
    missing = [words[k] for k in entry.get("not_reported") or [] if k in words]
    if missing:
        lines.append(f"  not reported by its API: {', '.join(missing)}")
    if entry.get("kind") == "llamacpp" and "requests_waiting" not in usage:
        lines.append("  (llama-server --metrics also reports the requests waiting)")
    return [printable(line) for line in lines]


def sort_models(models, key):
    if key == "name":
        return sorted(models, key=lambda m: m["name"])
    if key == "size":
        return sorted(models, key=lambda m: (m["size"] is None, -(m["size"] or 0), m["name"]))
    if key == "loaded":
        return sorted(models, key=lambda m: ({True: 0, False: 1, None: 2}[m["loaded"]], m["name"]))
    raise ValueError("Sort by name, size or loaded")


def format_size(value):
    if value is None:
        return "-"
    for unit, factor in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if value >= factor:
            return f"{value / factor:.1f} {unit}"
    return f"{value} B"


NAME_WIDTH = 48  # a longer model name is cut in the table (with "…"); --json and NAME:MODEL keep it whole


def model_table(models):
    """Aligned lines: a column only when some model has a value for it (LM Studio has no sizes, vLLM no
    quantization ...), the name column no wider than NAME_WIDTH."""
    columns = [("NAME", lambda m: m["name"] if len(m["name"]) <= NAME_WIDTH else m["name"][:NAME_WIDTH - 1] + "…"),
               ("LOADED", lambda m: {True: "yes", False: "no", None: ""}[m["loaded"]]),
               ("TYPE", lambda m: m.get("type") or ""),
               ("PARAMS", lambda m: m.get("parameters") or ""),
               ("QUANT", lambda m: m.get("quantization") or ""),
               ("CONTEXT", lambda m: f"{m['context'] // 1024}k" if m.get("context") else
                f"{m['served_context'] // 1024}k" if m.get("served_context") else
                "default" if m.get("served_how") == "default" else ""),
               ("SIZE", lambda m: format_size(m["size"]) if m["size"] is not None else ""),
               ("DETAILS", lambda m: m["details"] if m["details"] != (m.get("quantization") or "") else ""),
               # the traffic light of the local-model research (model_support): only with a usable table
               ("SUPPORT", lambda m: profiles_module().support_cell(m["support"]) if m.get("support") else "")]
    shown = [(title, cell) for index, (title, cell) in enumerate(columns)
             if index == 0 or any(cell(m) for m in models)]
    rows = [["#", *[title for title, _ in shown]]]
    rows += [[str(i), *[cell(m) for _, cell in shown]] for i, m in enumerate(models, 1)]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(v.ljust(width) for v, width in zip(row, widths)).rstrip() for row in rows]
    return lines if models else [lines[0], "(no models found)"]


def model_support(models):
    """MODELS (llm models' entries) each with its "support": how far the local-model research supports it, per
    harness (model_profiles.support: the colour, the harnesses of each, what worked and failed); none when the
    profiles cannot be read."""
    table = profile_table()
    if table is None:
        return models
    module = profiles_module()
    for model in models:
        model["support"] = module.support(model["name"], table)
    return models


def name_refusal(name):
    """Why NAME is no name for an LLM endpoint, in words that name the offending character (front ends show this)."""
    if not isinstance(name, str) or not name:
        return "an LLM endpoint's name is 1-32 letters, digits, _ or -"
    if len(name) > 32:
        return f"an LLM endpoint's name is at most 32 characters; {printable(name)!r} has {len(name)}"
    if not re.fullmatch(r"[A-Za-z0-9]", name[0]):
        return (f"an LLM endpoint's name starts with a letter or a digit, not {printable(name[0])!r} "
                f"({printable(name)!r})")
    bad = sorted({c for c in name[1:] if not re.fullmatch(r"[A-Za-z0-9_-]", c)})
    if bad:
        shown = ", ".join(repr(printable(c)) for c in bad[:4])
        return (f"an LLM endpoint's name takes letters, digits, _ and -; {printable(name)!r} has {shown} "
                f"(try {printable(re.sub(r'[^A-Za-z0-9_-]', '-', name)[:32])!r})")
    return f"an LLM endpoint's name is 1-32 letters, digits, _ or -: {printable(name)!r}"


def discover_url(kind, url, get=None, client=None, ca=None, api_key_secret=None, insecure_http=False, resolver=None,
                 ca_text=None, with_status=False):
    """The models a server of KIND at URL lists, before it is registered anywhere (the "test connection" of the app
    and the web): the URL is checked as `llm add` checks it, nothing is stored. The key of API_KEY_SECRET is sent
    only where `llm add` would accept the URL: plain http to a public address (or a name that resolves to one)
    needs INSECURE_HTTP, as `llm add --insecure-http`. CA_TEXT: a CA's PEM text already checked (a registered
    server's own, ca_text), in place of the file CA. WITH_STATUS: (models, what it has loaded and uses now:
    discover_status), each request with STATUS_TIMEOUT."""
    if kind not in KINDS:
        raise ValueError(f"Unknown server type: {printable(kind)}")
    parsed = parse_url(url)
    if api_key_secret is not None and not SECRET.fullmatch(str(api_key_secret)):
        raise ValueError("the API key's secret is named with letters, digits and _")
    if insecure_http and parsed["scheme"] != "http":
        raise ValueError("--insecure-http is for plain http URLs")
    if api_key_secret is not None and not insecure_http:
        problem, _ = plain_http_resolved(parsed, resolver)  # a name that does not resolve is not reached either
        if problem:
            raise ValueError(f"{problem} (the API key would go with it)")
    text = ca_text
    if ca is not None:
        text = read_private(Path(ca).expanduser(), CA_LIMIT, f"--ca {ca}").decode("ascii", "strict")
        check_ca(text, ca)
    endpoint = {"name": "test", "kind": kind, "url": parsed["url"], "parsed": parsed,
                "api_key_secret": api_key_secret, "headers": {}, "ca": None, "ca_text": text, "vpn": None,
                "model": None, "anthropic": False, "insecure_http": bool(insecure_http)}
    if with_status:
        client = client or Client(endpoint, get, timeout=STATUS_TIMEOUT)
        return discover_status(kind, client.json, client.text)
    return discover(kind, (client or Client(endpoint, get)).json)


def endpoint_models(endpoint, get=None, client=None):
    """The models the endpoint lists now, checked; remembered in the store for later starts, with the context window
    of those whose listing gives one (vLLM, LM Studio)."""
    client = client or Client(endpoint, get)
    models = discover(endpoint["kind"], client.json)
    remember_models(endpoint["name"], [m["name"] for m in models],
                    {m["name"]: m["context"] for m in models if m.get("context")}, reads_images(models))
    return models


def reads_images(models):
    """The names of the listed MODELS that read images (their capabilities name vision), in their order."""
    return [m["name"] for m in models if "vision" in (m.get("capabilities") or [])]


def remember_models(name, names, contexts=None, vision=None):
    """The models NAME lists now, in its record (with CONTEXTS, {model: tokens}, the context windows known, and VISION,
    the models that read images): read and written back under the store's lock, and only while NAME is still there
    (an `llm remove` meanwhile is never undone)."""
    with settings_lock(store_path()):
        store = load_store()
        record = store["endpoints"].get(name)
        if isinstance(record, dict):
            record["models"] = names[:500]
            record["checked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            known = {model: value for model, value in (contexts or {}).items()
                     if model in record["models"] and context_value(value)}
            if known:
                record["contexts"] = known
            else:
                record.pop("contexts", None)
            if vision is not None:
                seeing = [model for model in vision if model in record["models"]]
                if seeing:
                    record["vision"] = seeing
                else:
                    record.pop("vision", None)
            save_store(store)


# --- the context window --------------------------------------------------------------------------------------------
#
# Local models are in neither Codex's nor OpenCode's catalog: without a context window the agents guess (and compact
# too late, or cut what the server would have taken). What each server reports for a model, the window it serves:
#   vLLM       /v1/models: max_model_len
#   llama.cpp  /props: default_generation_settings.n_ctx (a slot's context); in router mode /v1/models' n_ctx first
#   LM Studio  /api/v1/models: a loaded instance's context_length, else max_context_length (also /api/v0/models)
#   Ollama     /api/ps: a loaded model's context_length, else /api/show: num_ctx of its parameters, else the
#              model's own <arch>.context_length
CONTEXT_TIMEOUT = 3          # seconds per request at a start (never fatal: the agent's own guess then)
CONTEXT_RANGE = (512, 1 << 24)
OUTPUT_LIMIT = 32768         # tokens OpenCode may ask for in one answer, at most a quarter of the window


def context_value(value):
    """VALUE when it is a plausible context window (tokens), else None (the answers are untrusted)."""
    if isinstance(value, int) and not isinstance(value, bool) and CONTEXT_RANGE[0] <= value <= CONTEXT_RANGE[1]:
        return value
    return None


def output_limit(context):
    """OpenCode's limit.output for a window of CONTEXT tokens: a quarter of it, at most OUTPUT_LIMIT."""
    return max(256, min(OUTPUT_LIMIT, context // 4))


def model_context(kind, fetch, model, post=None):
    """The context window (tokens) a server of KIND reports for MODEL, or None. FETCH(PATH) is the JSON of GET PATH,
    POST(PATH, BODY) of a POST (Ollama's /api/show)."""
    def listed(answer, key, id_key="id"):
        entries = answer.get(key) if isinstance(answer, dict) else None
        return [m for m in entries if isinstance(m, dict) and m.get(id_key) == model] \
            if isinstance(entries, list) else []
    if kind == "vllm":
        for m in listed(optional(fetch, "/v1/models", {}), "data"):
            return context_value(m.get("max_model_len"))
    elif kind == "llamacpp":
        for m in listed(optional(fetch, "/v1/models", {}), "data"):
            memory = m.get("memory") if isinstance(m.get("memory"), dict) else {}
            effective = memory.get("effective") if isinstance(memory.get("effective"), dict) else {}
            if context_value(effective.get("n_ctx")):
                return effective["n_ctx"]
        props = optional(fetch, "/props", {})
        settings = props.get("default_generation_settings") if isinstance(props, dict) else None
        if isinstance(settings, dict) and context_value(settings.get("n_ctx")):
            return settings["n_ctx"]
        return context_value(props.get("n_ctx")) if isinstance(props, dict) else None
    elif kind == "lmstudio":
        native = lmstudio_native(fetch).get(model) or {}
        for instance in native.get("loaded_instances") or []:
            config = instance.get("config") if isinstance(instance, dict) else None
            if isinstance(config, dict) and context_value(config.get("context_length")):
                return config["context_length"]
        if context_value(native.get("max_context_length")):
            return native["max_context_length"]
        for m in listed(optional(fetch, "/api/v0/models", {}), "data"):
            return context_value(m.get("loaded_context_length")) or context_value(m.get("max_context_length"))
    elif kind == "ollama":
        for m in listed(optional(fetch, "/api/ps", {}), "models", "name"):
            if context_value(m.get("context_length")):
                return m["context_length"]
        if post is None:
            return None
        try:
            shown = post("/api/show", {"model": model})
        except (OSError, ValueError):
            return None
        if not isinstance(shown, dict):
            return None
        parameters = shown.get("parameters")
        found = re.search(r"^num_ctx\s+(\d{1,9})\s*$", parameters, re.MULTILINE) if isinstance(parameters, str) \
            else None
        if found and context_value(int(found.group(1))):
            return int(found.group(1))
        info = shown.get("model_info") if isinstance(shown.get("model_info"), dict) else {}
        values = [context_value(v) for k, v in info.items() if isinstance(k, str) and k.endswith(".context_length")]
        return next((v for v in values if v), None)
    return None


def context_window(endpoint, model, get=None, client=None, behind_vpn=False):
    """The context window of MODEL at ENDPOINT for a start: the one remembered (llm models), else what the server
    reports now (CONTEXT_TIMEOUT per request); None when neither says (or the server sits behind its VPN, which
    the host does not reach). BEHIND_VPN (a start with --vpn): never asked from this host, whose own address the
    server would see beside the sandbox's VPN (Tor) exit. Never raises: the agent's own guess stays then."""
    known = context_value((endpoint.get("contexts") or {}).get(model))
    if known or endpoint.get("vpn") or behind_vpn or os.environ.get("SANDBOX_AI_LLM_NO_CONTEXT_FETCH") == "1":
        return known
    try:
        client = client or Client(endpoint, get, timeout=CONTEXT_TIMEOUT)
        return model_context(endpoint["kind"], client.json, model,
                             lambda path, body: client.json(path, body=body))
    except Exception as exc:  # noqa: BLE001 - a server that cannot say leaves the agents' defaults
        LOG.info("llm", f"the context window of {model} at {endpoint.get('name')} is not known: the agent's own "
                        "default stays", exc)
        return None


# --- sampling parameters and the loop guard ------------------------------------------------------------------------
#
# What a model is run with (temperature, top_k, repeat_penalty, ...) is kept per endpoint and model ("params": {"*" or
# MODEL: {KEY: VALUE}}; "forced": {"*" or MODEL: [KEY]}) and applied by the connection proxy (runtime.llm_request) to
# the request bodies that go to that endpoint, whatever agent sends them - never to any other server. A value is set
# where the request has none; a forced one replaces the agent's own; max_tokens is a ceiling (lowered to it, never
# raised). num_ctx is the model's context window: never sent (no API of these servers takes it per request), written
# into the agents' configurations instead (OpenCode limit.context, Codex model_context_window).
#
# Which field reaches which server and API (the servers' documentation: Ollama docs/api/openai-compatibility and
# anthropic-compatibility, LM Studio's OpenAI-compatible chat completions, llama.cpp tools/server, vLLM's extra
# sampling parameters); a kind accepts a few the proxy cannot send (Ollama's Modelfile ones), shown with the hint.
STANDARD_FIELDS = ("temperature", "top_p", "presence_penalty", "frequency_penalty", "max_tokens", "seed")
KEEP_ALIVE_MAX = 365 * 86400  # seconds (a year: -1 keeps a model for good)
# (type, minimum, maximum, help)
PARAMS = {
    "temperature": (float, 0.0, 2.0, "randomness; 0 is greedy (prone to loops in long answers)"),
    "top_p": (float, 0.0, 1.0, "nucleus sampling: the smallest set of tokens with this probability mass"),
    "top_k": (int, 0, 1000, "sample from the K most likely tokens (0: all)"),
    "min_p": (float, 0.0, 1.0, "drop tokens below this share of the most likely one's probability"),
    "repeat_penalty": (float, 0.5, 2.0, "penalty for tokens of the last repeat_last_n (1.0: none; 1.05-1.15 "
                                        "against loops)"),
    "repeat_last_n": (int, -1, 131072, "how many recent tokens repeat_penalty looks at (-1: the context, 0: off)"),
    "repetition_penalty": (float, 0.5, 2.0, "vLLM's repetition penalty (1.0: none)"),
    "presence_penalty": (float, -2.0, 2.0, "penalty for tokens that occurred at all (OpenAI's)"),
    "frequency_penalty": (float, -2.0, 2.0, "penalty growing with how often a token occurred (OpenAI's)"),
    "dry_multiplier": (float, 0.0, 5.0, "llama.cpp's DRY sampler against repeated sequences (0: off; 0.8 typical)"),
    "max_tokens": (int, 16, 1 << 20, "the most tokens of one answer: a ceiling, the agent's own lower value stays "
                                      "(0: no ceiling - the agent's own value, over a model profile's)"),
    "seed": (int, 0, (1 << 31) - 1, "a fixed seed (reproducible answers)"),
    "num_ctx": (int, CONTEXT_RANGE[0], CONTEXT_RANGE[1], "the context window (tokens) the agents are told; the "
                                                         "server must load the model with at least this much "
                                                         "(Ollama: at most the window it serves is told)"),
    "cache_prompt": (bool, None, None, "llama.cpp: reuse the KV cache of the previous request (default on)"),
    # Keeping the model loaded, per request: Ollama's keep_alive (its OpenAI-compatible chat API takes it since
    # Ollama 0.35.0, openai/openai.go ChatCompletionRequest.KeepAlive; older ones ignore it), LM Studio's ttl (its
    # docs, "Idle TTL and Auto-Evict": the request's ttl sets the idle TTL of a model JIT-loaded by it).
    "keep_alive": (int, -1, KEEP_ALIVE_MAX, "Ollama: seconds the model stays loaded after each request (-1: until "
                                            "it is unloaded, 0: unloaded at once; the server's OLLAMA_KEEP_ALIVE, 5 "
                                            "minutes, else)"),
    "ttl": (int, 1, KEEP_ALIVE_MAX, "LM Studio: seconds a JIT-loaded model stays loaded while idle (its idle TTL; "
                                    "default 60 minutes). A model a start loads explicitly stays until unloaded"),
    # At the model's load (LM Studio's POST /api/v1/models/load: context_length - num_ctx -, flash_attention,
    # offload_kv_cache_to_gpu; its REST API has no KV cache quantization): when a start or `llm load` loads it.
    "flash_attention": (bool, None, None, "LM Studio, at the model's load: Flash Attention (less memory, faster)"),
    "offload_kv_cache_to_gpu": (bool, None, None, "LM Studio, at the model's load: the KV cache in GPU memory (false: "
                                                  "in RAM, slower, more room for the model)"),
    # The agents' wait for a slow server (a large local model may be silent for minutes before its first token):
    # Claude Code CLAUDE_STREAM_IDLE_TIMEOUT_MS and CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS (2.1.322), Codex
    # stream_idle_timeout_ms of its provider (0.169.0), OpenCode chunkTimeout and headerTimeout of its provider
    # (1.19.5), Qwen Code its provider entry's timeout and streamIdleTimeoutMs (0.26.4), Goose OPENAI_TIMEOUT and
    # GOOSE_STREAM_TIMEOUT (1.57.0), Pi httpIdleTimeoutMs and retry.provider.timeoutMs of its settings (1.4.0).
    # Unset: LOCAL_WAIT for a model server of your own (agent_wait).
    "stream_idle_timeout": (int, 10, 86400, "seconds an agent waits for the first and the next part of a streamed "
                                            "answer before it gives up (default: an hour for a server of your own; "
                                            "never sent to the server)"),
}
# A ceiling that 0 lifts: max_tokens=0 (your value, the start's or the endpoint's) leaves the agent's own value, over
# the model profile's ceiling (every family has one: an answer a local model cannot end blocks the server).
NO_CEILING = ("max_tokens",)
# Never sent with a request: at the model's load (LOAD_ONLY, by the start or `llm load`), or into the agents'
# configurations (AGENT_ONLY).
LOAD_ONLY = {"lmstudio": ("num_ctx", "flash_attention", "offload_kv_cache_to_gpu"), "ollama": ("num_ctx",)}
AGENT_ONLY = ("num_ctx", "stream_idle_timeout")
# What only the server's own start or settings decide (shown as information, never set by sandbox-ai): (setting,
# where it is set). The servers' documentation: Ollama's FAQ (OLLAMA_CONTEXT_LENGTH, OLLAMA_KV_CACHE_TYPE with
# OLLAMA_FLASH_ATTENTION, OLLAMA_KEEP_ALIVE), LM Studio's REST load (no KV cache quantization there), llama.cpp's
# tools/server README (--ctx-size, --cache-type-k/-v, --flash-attn), vLLM's engine arguments (--max-model-len,
# --kv-cache-dtype).
SERVER_ONLY = {
    "ollama": (("context window of every model", "OLLAMA_CONTEXT_LENGTH on the server (or a Modelfile's PARAMETER "
                                                 "num_ctx; `sandbox-ai llm load` loads with num_ctx)"),
               ("KV cache type", "OLLAMA_KV_CACHE_TYPE=f16|q8_0|q4_0 with OLLAMA_FLASH_ATTENTION=1 on the server"),
               ("kept loaded by default", "OLLAMA_KEEP_ALIVE and OLLAMA_MAX_LOADED_MODELS on the server")),
    "lmstudio": (("KV cache quantization", "the model's load settings in LM Studio (K and V cache quantization): "
                                           "its REST API has no field for it"),),
    "llamacpp": (("context window", "llama-server --ctx-size N"),
                 ("KV cache type", "llama-server --cache-type-k and --cache-type-v (f16, q8_0, q4_0 ...)"),
                 ("Flash Attention", "llama-server --flash-attn")),
    "vllm": (("context window", "vllm serve --max-model-len N"),
             ("KV cache type", "vllm serve --kv-cache-dtype (auto, fp8 ...)")),
    "openai-compatible": (("context window, KV cache", "the server's own settings"),),
}
# Sent on each API (chat: /v1/chat/completions, completions: /v1/completions, responses: /v1/responses, messages:
# Anthropic's /v1/messages) to a server of each kind; the rest of a kind's own keys only where named.
_RESPONSES = ("temperature", "top_p", "max_tokens")   # max_tokens as max_output_tokens
_MESSAGES = ("temperature", "top_p", "top_k", "max_tokens")
_LLAMACPP = STANDARD_FIELDS + ("top_k", "min_p", "repeat_penalty", "repeat_last_n", "dry_multiplier", "cache_prompt")
SENT = {
    "ollama": {"chat": STANDARD_FIELDS + ("keep_alive",), "completions": STANDARD_FIELDS, "responses": _RESPONSES,
               "messages": _MESSAGES},
    "lmstudio": {"chat": STANDARD_FIELDS + ("top_k", "repeat_penalty", "ttl"),
                 "completions": STANDARD_FIELDS + ("top_k", "repeat_penalty", "ttl"), "responses": _RESPONSES,
                 "messages": _MESSAGES},
    # llama.cpp takes its own sampling fields on every API: on /v1/messages and /v1/responses those the API has no
    # field for inside "extra_params" (its server README: "llama.cpp parameters that the Anthropic API has no field
    # for, such as seed, min_p or repeat_penalty"; NESTED below).
    "llamacpp": {"chat": _LLAMACPP, "completions": _LLAMACPP, "responses": _LLAMACPP, "messages": _LLAMACPP},
    "vllm": {"chat": STANDARD_FIELDS + ("top_k", "min_p", "repetition_penalty"),
             "completions": STANDARD_FIELDS + ("top_k", "min_p", "repetition_penalty"), "responses": _RESPONSES,
             "messages": _MESSAGES},
    "openai-compatible": {"chat": STANDARD_FIELDS, "completions": STANDARD_FIELDS, "responses": _RESPONSES,
                          "messages": _MESSAGES},
}
# Sent inside the request's "extra_params" object on these APIs (llama.cpp's own fields beside a foreign API's).
NESTED = {"llamacpp": {"messages": tuple(k for k in _LLAMACPP if k not in _MESSAGES),
                       "responses": tuple(k for k in _LLAMACPP if k not in _RESPONSES)}}
# Kept for a kind but never sent: Ollama takes these only from a Modelfile (PARAMETER ...) or its native API.
MODELFILE_ONLY = {"ollama": ("top_k", "min_p", "repeat_penalty", "repeat_last_n")}
# The loop guard's layers, each switched on or off (and tuned) on its own: sandbox-ai's defaults here, your own in
# defaults.json ("loop_guard": {...}), an endpoint's (llm set --loop-guard), a start's (--llm-param loop_guard...)
# over them. Only for --llm starts: nothing of it touches another server's traffic or a start without --llm.
#   steps        OpenCode: the most agentic steps of a turn of its build agent (plan and explore about half, general
#                two thirds); at the last one OpenCode asks for a text answer (agent.<name>.steps; 0: OpenCode's own).
#                150: a debugging task took a local model 21 to 102 requests where it was solved (the local-model
#                research; with 40 OpenCode stopped after 41, nothing fixed). The stop for loops is the proxy's guard
#                below, not a low limit.
#   compaction   OpenCode: automatic compaction with pruning of old tool output and room for it, tool output cut at
#                1000 lines / 50,000 bytes (compaction, tool_output)
#   instructions OpenCode: rules against loops as an instruction file of its own (instructions; your AGENTS.md stays)
#   sampling     the model family's recommended sampling as the lowest default (Qwen3, QwQ, Qwen3-Coder,
#                DeepSeek-R1, gpt-oss), never greedy for those that warn against it
#   context      warn when the agents are told a larger context window than the server loaded
#   warn, block  the proxy, on each request: the same tool call with the same result WARN times since the human's last
#                message: a note in that result tells the model so (and stays there in every later request: the
#                server's prompt cache holds); BLOCK times: the note says to stop calling tools, and an answer that
#                calls one again is replaced by sandbox-ai's own, which ends the turn (the model summarises; the human
#                goes on). Polling (sleep, --watch, wait) counts against three times both. A call is compared without
#                the free text harnesses have the model add (Claude Code's Bash description, Codex's justification).
#   failures     the proxy, on each request: the same tool failed this many times in a row (other arguments each
#                time: guessing - Laguna-S in Codex called read_mcp_resource 110 times with guessed URIs): a note in
#                that result tells the model to stop guessing and change its approach, again every as many failures
#                (a note only: the model goes on)
#   text        the proxy, on a streamed answer: one block repeated TEXT times in a row (4 KiB at least): the answer
#                ends cleanly with a note, and the next request gets stronger anti-repetition sampling
#   rejected     the proxy, on each request: the very same request (its path and body as they go to the server)
#                that the server rejected this many times (HTTP 4xx but 408 and 429) is not sent again for ten minutes:
#                the agent gets sandbox-ai's error at once, the server's reason goes into the connection log. OpenCode
#                answered Strata's "requests are never truncated" (HTTP 400) with 551 identical requests in 150 s (the
#                Strata test, 2026-11-17). A different request always goes; one that succeeds is forgotten
#   watchdog_requests, watchdog_minutes   for unattended runs: tool use paused after this many model requests or
#                minutes in one turn (0: off, the default)
#   progress_notes   one more line beside the instructions (Claude Code: its system prompt): keep PROGRESS.md with
#                what is done and what is next, and read it first after an interruption or a compaction (off by
#                default; a model profile's pair switches it on where the research measured it helping: round 5)
LOOP_DEFAULTS = {"enabled": True, "steps": 150, "compaction": True, "instructions": True, "sampling": True,
                 "context": True, "warn": 3, "block": 6, "failures": 6, "text": 10, "rejected": 3,
                 "watchdog_requests": 0, "watchdog_minutes": 0, "progress_notes": False}
LOOP_RANGES = {"steps": (5, 1000), "warn": (2, 100), "block": (3, 200), "failures": (3, 200), "text": (3, 100),
               "rejected": (2, 100), "watchdog_requests": (5, 100000), "watchdog_minutes": (1, 10080)}  # 0 is off besides these
# Sampling a model family's makers recommend (model cards: Qwen3 thinking T 0.6 / top_p 0.95 / top_k 20 / min_p 0 and
# never greedy; Qwen3 Instruct-2507 (no thinking) T 0.7 / top_p 0.8; Qwen3-Coder T 0.7 / top_p 0.8 / top_k 20 /
# repetition penalty 1.05; DeepSeek-R1 T 0.6 / top_p 0.95; gpt-oss T 1.0 / top_p 1.0). The first match wins;
# min_temperature: a lower temperature the agent asks for is raised to the family's.
FAMILY_PRESETS = (
    (re.compile(r"qwen-?3[^/]*coder", re.I), {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05,
                                             "repetition_penalty": 1.05, "min_temperature": 0.3}),
    (re.compile(r"qwen-?3[^/]*instruct", re.I), {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "min_p": 0.0,
                                                "min_temperature": 0.3}),
    (re.compile(r"qwen-?3|qwq", re.I), {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0,
                                       "min_temperature": 0.3}),
    (re.compile(r"deepseek-?r1", re.I), {"temperature": 0.6, "top_p": 0.95, "min_temperature": 0.3}),
    (re.compile(r"gpt-oss", re.I), {"temperature": 1.0, "top_p": 1.0}),
)


def kind_params(kind):
    """The parameter names a server of KIND takes (num_ctx and stream_idle_timeout always: they go to the agents'
    configurations; LOAD_ONLY's at the model's load)."""
    names = {*AGENT_ONLY, *MODELFILE_ONLY.get(kind, ()), *LOAD_ONLY.get(kind, ())}
    for keys in SENT.get(kind, {}).values():
        names.update(keys)
    return [name for name in PARAMS if name in names]


def param_value(kind, key, value, where="--param"):
    """VALUE (text from the command line, or a stored JSON value) of the parameter KEY for a server of KIND, checked
    (known name, the kind takes it, type, range); ValueError says why."""
    if key not in PARAMS:
        raise ValueError(f"{where}: unknown parameter {printable(key)!r}; known: {', '.join(PARAMS)}")
    if key not in kind_params(kind):
        raise ValueError(f"{where} {key}: {KINDS[kind]['label']} does not take it; it takes "
                         f"{', '.join(kind_params(kind))}")
    kind_of, low, high, _ = PARAMS[key]
    if kind_of is bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ("true", "on", "yes", "1"):
            return True
        if text in ("false", "off", "no", "0"):
            return False
        raise ValueError(f"{where} {key}: true or false, not {printable(value)!r}")
    if isinstance(value, bool):
        raise ValueError(f"{where} {key}: a number, not {value}")
    try:
        if kind_of is int:
            number = value if isinstance(value, int) else int(str(value).strip(), 10)
        else:
            number = float(value) if isinstance(value, (int, float)) else float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError(f"{where} {key}: {'a whole number' if kind_of is int else 'a number'}, not "
                         f"{printable(value)!r}") from None
    if number != number or number in (float("inf"), float("-inf")) or \
            not (low <= number <= high or (number == 0 and key in NO_CEILING)):
        raise ValueError(f"{where} {key}: from {low} to {high}{' (0: none)' if key in NO_CEILING else ''}, not "
                         f"{printable(value)}")
    return number


def parse_param(kind, text, where="--param"):
    """(key, value) of KEY=VALUE."""
    key, sep, value = str(text).partition("=")
    if not sep or not value.strip():
        raise ValueError(f"{where} expects KEY=VALUE (e.g. temperature=0.7): {printable(text)!r}")
    key = key.strip()
    return key, param_value(kind, key, value, where)


def loop_setting(key, value, where="--loop-guard"):
    """VALUE of the loop guard's layer KEY, checked: on/off (true/false) for a switch, a number (0: off) for a count."""
    if key not in LOOP_DEFAULTS:
        raise ValueError(f"{where}: unknown layer {printable(key)!r}; the layers: {', '.join(LOOP_DEFAULTS)}")
    text = str(value).strip().lower() if not isinstance(value, (bool, int)) else value
    if isinstance(LOOP_DEFAULTS[key], bool):
        if isinstance(value, bool):
            return value
        if text in ("on", "true", "yes", "1"):
            return True
        if text in ("off", "false", "no", "0"):
            return False
        raise ValueError(f"{where} {key}: on or off, not {printable(value)!r}")
    if isinstance(value, bool):
        raise ValueError(f"{where} {key}: a number (0: off), not {value}")
    if text == "off":
        return 0
    if isinstance(value, int) or (isinstance(text, str) and re.fullmatch(r"[0-9]{1,6}", text)):
        number = int(value if isinstance(value, int) else text)
        low, high = LOOP_RANGES[key]
        if number == 0 or low <= number <= high:
            return number
    low, high = LOOP_RANGES[key]
    raise ValueError(f"{where} {key}: 0 (off) or {low}-{high}, not {printable(value)!r}")


def parse_loop(texts, where="--loop-guard", into=None):
    """The layers of on | off | KEY=VALUE | KEY=default texts, over INTO (a copy): on/off the whole guard,
    KEY=default drops KEY (it falls back to the defaults)."""
    found = dict(into or {})
    for text in texts or []:
        text = str(text).strip()
        key, sep, value = text.partition("=")
        if not sep:
            found["enabled"] = loop_setting("enabled", key, where)
        elif value.strip().lower() == "default":
            loop_setting(key.strip(), LOOP_DEFAULTS.get(key.strip(), 0), where)
            found.pop(key.strip(), None)
        else:
            found[key.strip()] = loop_setting(key.strip(), value, where)
    return found


def check_loop(document, where):
    """A stored set of the loop guard's layers ({} or some of LOOP_DEFAULTS), checked."""
    if not isinstance(document, dict):
        raise ValueError(f"{where}: loop_guard is an object of layers ({', '.join(LOOP_DEFAULTS)})")
    for key, value in document.items():
        if loop_setting(key, value, f"{where}: loop_guard") != value or \
                isinstance(value, bool) != isinstance(LOOP_DEFAULTS.get(key), bool):
            raise ValueError(f"{where}: loop_guard {key} is not a stored value")
    return document


def user_loop():
    """The loop guard of your defaults file (defaults.json "loop_guard"), {} without one; the launcher checks the
    file at a start (an unusable one stops it there), here it only counts when it is usable."""
    path = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "defaults.json"
    try:
        document = json.loads(read_private(path, 1 << 16, "The defaults file").decode())
        return dict(check_loop(document.get("loop_guard", {}), str(path)))
    except (OSError, ValueError, AttributeError, RecursionError):
        return {}


def loop_config(endpoint, start=None, user=None):
    """The loop guard as it applies: sandbox-ai's defaults, the model profile's (an endpoint selection() marked:
    profile_loop), yours (USER, else defaults.json), the endpoint's, the start's (START) over each other; every layer
    off when "enabled" is off."""
    found = {**LOOP_DEFAULTS, **profile_loop(endpoint), **(user_loop() if user is None else user),
             **(endpoint.get("loop_guard") or {}), **(start or {})}
    if not found["enabled"]:
        found.update({key: False if isinstance(value, bool) else 0 for key, value in LOOP_DEFAULTS.items()})
    return found


def family_preset(model):
    """The sampling MODEL's family recommends ({} for others)."""
    for pattern, values in FAMILY_PRESETS:
        if pattern.search(model or ""):
            return dict(values)
    return {}


# --- model x harness profiles (model_profiles.py, model_profiles.json) -------------------------------------------
#
# The settings the local-model research found for a model family in each harness, applied at a start UNDER yours:
# the profile < the endpoint's values (llm set) < the start's --llm-param < explicit options (--env). selection()
# marks the endpoint of a start with its agent and model ("llm_profile"; --no-llm-profile: no mark), and only a
# marked endpoint has a profile: `llm show`, `llm set` and the stored records never do.
_profiles_module = None


def profiles_module():
    """model_profiles.py, loaded once."""
    global _profiles_module
    with LOADING:
        if _profiles_module is None:
            spec = importlib.util.spec_from_file_location("sandbox_model_profiles_llm", HERE / "model_profiles.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _profiles_module = module
    return _profiles_module


def profile_param(key, value):
    """VALUE of the parameter KEY as some kind of server takes it (a profile serves every kind; a start keeps what
    its kind takes: profile_params)."""
    problem = None
    for kind in KINDS:
        try:
            return param_value(kind, key, value, "--param")
        except ValueError as exc:
            problem = problem or exc
    raise problem


def profile_table():
    """The profiles (model_profiles.json), checked with this module's own rules; None when the file cannot be used
    (logged: the start goes on without profiles)."""
    try:
        return profiles_module().load(None, profile_param, loop_setting)
    except (OSError, ValueError) as exc:
        LOG.info("llm", "the model profiles cannot be used: starts go on without them", exc)
        return None


def model_profile(endpoint, model):
    """The profile in force for MODEL at ENDPOINT (marked by selection() with the start's agent and use case), with
    the use case's settings for that pair over it (pair_profile), or None."""
    marked = endpoint.get("llm_profile") if isinstance(endpoint, dict) else None
    if not isinstance(marked, dict) or not model or model == "*":
        return None
    return pair_profile(model, marked["agent"], marked.get("use_case"), profile_table())


def pair_profile(model, agent, case=None, table=None):
    """The profile of MODEL in AGENT (TABLE: the profiles, None: none), with the use case CASE's settings for that
    model in that agent over it (model_usecases.overlay: a model of no family gets the use case's alone); None when
    neither knows it."""
    profile = profiles_module().match(model, agent, table) if table is not None else None
    if not case:
        return profile
    try:
        cases = usecase_table()
        module = usecases_module()
        entry = module.use_case(cases, case)
        found = module.settings_for(cases, entry["id"], model, agent)
    except ValueError as exc:  # (selection() refused an unknown use case already; a damaged table: the profile)
        LOG.info("llm", "the use case's settings cannot be used: the model's profile alone", exc)
        return profile
    return module.overlay(profile, found, entry["id"], entry["label"], agent) if found is not None else profile


def profile_params(endpoint, model):
    """The profile's parameters for MODEL that ENDPOINT's kind takes (never Ollama's Modelfile-only ones: its APIs
    have no field for them), {} without a profile."""
    profile = model_profile(endpoint, model)
    if profile is None:
        return {}
    taken = set(kind_params(endpoint["kind"])) - set(MODELFILE_ONLY.get(endpoint["kind"], ()))
    return {key: value for key, value in profile["params"].items() if key in taken}


def profile_request(endpoint, model):
    """The profile's parameters for MODEL the proxy sets in its requests (the lowest default): without the agents'
    and the load's own (num_ctx, stream_idle_timeout, LM Studio's load settings)."""
    return {key: value for key, value in profile_params(endpoint, model).items()
            if key not in AGENT_ONLY and key not in LOAD_ONLY.get(endpoint["kind"], ())}


def profile_loop(endpoint):
    """The loop guard's layers of the profile of the start's model (selection()'s mark), {} without one."""
    marked = endpoint.get("llm_profile") if isinstance(endpoint, dict) else None
    profile = model_profile(endpoint, marked.get("model")) if isinstance(marked, dict) else None
    return dict(profile["loop_guard"]) if profile is not None else {}


def profile_families(endpoint, sampling):
    """proxy.json's "families" of the profiles ([[pattern, values]], before FAMILY_PRESETS'): a model of the server
    the start did not list gets its family's profile (over the family preset with SAMPLING)."""
    marked = endpoint.get("llm_profile") if isinstance(endpoint, dict) else None
    table = profile_table() if isinstance(marked, dict) else None
    found = []
    for family in (table or {}).get("families", []):
        example = family["examples"][0]
        own = profile_request(endpoint, example)
        if own:
            found.append([family["pattern"], {**(family_preset(example) if sampling else {}), **own}])
    return found


def told_context(endpoint, model, override=None, reported=None, loaded=None):
    """(the context window the agents are told for MODEL, whether you chose it): your num_ctx (the start's, the
    endpoint's), else the profile's at most what the server has the model LOADED with or REPORTS (never more than it
    keeps; nothing when neither is known: the agent's own guess then), else the loaded or reported one."""
    own = effective_params(endpoint, model, override, profile=False)[0].get("num_ctx")
    if own:
        return own, True
    keeps = loaded or reported
    soft = context_value(profile_params(endpoint, model).get("num_ctx"))
    return (min(soft, keeps) if soft and keeps else keeps), False


def profile_report(a, endpoint, model):
    """What the profile of MODEL in the start's agent does (the dry run's llm.profile, the start's note), or None
    for a model of no family: its id, status and evidence, "values" (what it sets that this kind takes: parameters,
    loop_guard.LAYER, env.NAME), "applied" (those it sets: none when --no-llm-profile switched it off),
    "overridden" (those you set yourself: yours win), "not_taken" (parameters this kind has no field for) and its
    warnings (a pair known not to work, more MCP servers than the model copes with)."""
    table = profile_table()
    case = getattr(a, "use_case", None)
    if table is None and not case:
        return None
    module = profiles_module()
    profile = pair_profile(model, a.agent, case, table)
    if profile is None:
        return None
    enabled = bool(endpoint.get("llm_profile"))
    taken = set(kind_params(endpoint["kind"])) - set(MODELFILE_ONLY.get(endpoint["kind"], ()))
    values = module.settings(profile)
    not_taken = sorted(key for key in profile["params"] if key not in taken)
    for key in not_taken:
        values.pop(key)
    try:
        override, start = start_params(a, endpoint)
    except ValueError:
        override, start = {}, {}  # validate() reports it
    own = effective_params(endpoint, model, override, profile=False)[0]
    layers = {**user_loop(), **(endpoint.get("loop_guard") or {}), **start}
    given = passed_names(a)

    def yours(key):
        if key == "tools":
            return getattr(a, "llm_tools", None) is not None
        if key.startswith("loop_guard."):
            return key[len("loop_guard."):] in layers or layers.get("enabled") is False
        if key.startswith("env."):
            return key[len("env."):] in given
        return key in own
    warnings = list(profile["warnings"])
    servers = len(getattr(a, "mcp", None) or [])
    if profile["mcp_max"] is not None and servers > profile["mcp_max"]:
        warnings.append(f"{servers} MCP servers are attached; it copes with {profile['mcp_max'] or 'no'} MCP "
                        f"server{'' if profile['mcp_max'] == 1 else 's'} at most")
    return {"id": profile["id"], "name": profile["name"], "family": profile["family"], "label": profile["label"],
            "agent": profile["agent"], "pattern": profile["pattern"], "status": profile["status"],
            "status_text": profile["status_text"], "pair": profile["pair"], "evidence": profile["evidence"],
            "reasoning": profile["reasoning"], "enabled": enabled, "values": values,
            "applied": [key for key in values if not yours(key)] if enabled else [],
            "overridden": [key for key in values if yours(key)], "not_taken": not_taken, "warnings": warnings,
            "notes": profile["notes"], "builtin": profile["builtin"],
            # the use case whose settings for this model in this agent go over the profile (None: none)
            "use_case": profile.get("use_case")}


def profile_lines(report, name):
    """The start's lines of REPORT (profile_report) for the endpoint NAME: what the profile sets, yours that win,
    how to switch it off, and its warnings (never a refusal)."""
    module = profiles_module()
    head = module.headline(report)
    case = report.get("use_case")
    if case and report.get("family") is None:
        head = f"use case {case['id']}'s settings for {report['label']} × {report['agent']} (no model profile)"
    elif case:
        head += f" with use case {case['id']}"
    lines = []
    if not report["enabled"]:
        lines.append(f"agent-sandbox: note: --llm {name}: {head} is off (--no-llm-profile)")
    else:
        text = module.values_text({key: report["values"][key] for key in report["applied"]}) or "nothing left to set"
        if report["overridden"]:
            text += "; yours win over its " + module.values_text({key: report["values"][key]
                                                                  for key in report["overridden"]})
        bad = report["status"] in module.WARNING_STATUSES
        lines.append(f"agent-sandbox: {'WARNING: ' if bad else ''}--llm {name}: {head}: {text} (--no-llm-profile "
                     "switches it off)")
    lines += [f"agent-sandbox: WARNING: --llm {name}: {report['label']}: {warning}" for warning in report["warnings"]]
    return [printable(line) for line in lines]


def loop_lines(config):
    """The loop guard's layers in one line."""
    if not config["enabled"]:
        return "off"
    shown = []
    for key, value in config.items():
        if key == "enabled":
            continue
        shown.append(f"{key}={'on' if value is True else 'off' if value in (False, 0) else value}")
    return ", ".join(shown)


def check_params(where, kind, params, forced, guard):
    """The stored "params", "forced" and "loop_guard" ({layer: value}) of an endpoint, checked (ValueError names the
    field)."""
    if not isinstance(params, dict) or len(params) > 501:
        raise ValueError(f"{where}: params maps \"*\" or a model name to its parameters")
    for scope, values in params.items():
        if not isinstance(scope, str) or not (scope == "*" or MODEL_NAME.fullmatch(scope)) or \
                not isinstance(values, dict):
            raise ValueError(f"{where}: params maps \"*\" or a model name to its parameters")
        for key, value in values.items():
            if param_value(kind, key, value, f"{where}: params of {scope}") != value or \
                    isinstance(value, bool) != (PARAMS[key][0] is bool):
                raise ValueError(f"{where}: params of {scope}: {key} is not a stored value")
    if not isinstance(forced, dict) or not all(
            isinstance(scope, str) and isinstance(keys, list) and keys and
            all(isinstance(k, str) and k in (params.get(scope) or {}) for k in keys)
            for scope, keys in forced.items()):
        raise ValueError(f"{where}: forced maps a scope of params to the names of its forced parameters")
    if guard is not None:
        check_loop(guard, where)


def start_params(a, endpoint):
    """({key: value}, {layer: value}) of the start's --llm-param options: KEY=VALUE a sampling parameter,
    loop_guard=on|off the whole loop guard, loop_guard.LAYER=VALUE one of its layers."""
    values, loop = {}, {}
    for text in getattr(a, "llm_param", None) or []:
        key, sep, value = str(text).partition("=")
        key = key.strip()
        if key == "loop_guard" and sep:
            loop = parse_loop([value], "--llm-param loop_guard", loop)
        elif key.startswith("loop_guard.") and sep:
            loop = parse_loop([f"{key[len('loop_guard.'):]}={value}"], "--llm-param loop_guard", loop)
        else:
            key, value = parse_param(endpoint["kind"], text, "--llm-param")
            values[key] = value
    return values, loop


def effective_params(endpoint, model, override=None, profile=True):
    """({key: value}, {forced keys}) for MODEL: the model profile's (PROFILE, an endpoint selection() marked:
    profile_params), the endpoint's every-model values, its model's over them, the start's OVERRIDE (--llm-param:
    forced) over all."""
    params, forced = endpoint.get("params") or {}, endpoint.get("forced") or {}
    values, strong = (profile_params(endpoint, model) if profile else {}), set()
    for scope in ("*", model):
        for key, value in (params.get(scope) or {}).items():
            values[key] = value
            strong.discard(key)
        strong.update(forced.get(scope) or [])
    for key, value in (override or {}).items():
        values[key] = value
        strong.add(key)
    return values, strong


def modelfile_hint(endpoint, model, values):
    """Ollama: the Modelfile that loads MODEL with the values its OpenAI/Anthropic APIs do not take, or None."""
    keys = [key for key in (*MODELFILE_ONLY.get(endpoint["kind"], ()), "num_ctx") if key in values]
    if endpoint["kind"] != "ollama" or not keys or model in (None, "*"):
        return None
    lines = [f"FROM {model}", *[f"PARAMETER {key} {values[key]}" for key in keys]]
    return ("Ollama takes " + ", ".join(keys) + " only from a Modelfile (its OpenAI- and Anthropic-compatible APIs "
            "have no field for them): save\n    " + "\n    ".join(lines) + "\n  as Modelfile, run: ollama create "
            f"{model.replace(':', '-').replace('/', '-')}-sandbox -f Modelfile, and start with --llm "
            f"{endpoint['name']}:{model.replace(':', '-').replace('/', '-')}-sandbox"
            + (" (or set OLLAMA_CONTEXT_LENGTH on the server for the context window of every model)"
               if "num_ctx" in keys else ""))


def params_lines(endpoint):
    """Lines of `llm show` for the parameters and the loop guard."""
    lines = []
    for scope, values in sorted((endpoint.get("params") or {}).items(), key=lambda item: item[0] != "*"):
        forced = set((endpoint.get("forced") or {}).get(scope) or [])
        shown = ", ".join(f"{key}={str(value).lower() if isinstance(value, bool) else value}"
                          + (" (forced)" if key in forced else "") for key, value in values.items())
        lines.append(f"  parameters of {'every model' if scope == '*' else scope}: {shown}")
        never = [key for key in values if key in MODELFILE_ONLY.get(endpoint["kind"], ())]
        if never:
            lines.append(f"    not sent to {KINDS[endpoint['kind']]['label']} (Modelfile only): {', '.join(never)}")
    own = endpoint.get("loop_guard") or {}
    lines.append(f"  loop guard: {loop_lines(loop_config(endpoint))}"
                 + (f" (this server's own: {', '.join(f'{k}={v}' for k, v in own.items())})" if own else ""))
    if local_model_server(endpoint):
        lines.append("  other models: " + ("the server may unload them (evict allow)" if endpoint.get("evict") == "allow"
                                           else "kept loaded where the server lets it (evict never)"))
    return lines


def local_model_server(endpoint):
    """True if ENDPOINT is a model server of your own that loads and unloads models (Ollama, LM Studio, llama.cpp,
    vLLM, or an OpenAI-compatible server on the host's loopback or a local address); False for an OpenAI-compatible
    service at a public host (OpenRouter and the like), where nothing is loaded for you."""
    if endpoint["kind"] != "openai-compatible":
        return True
    parsed = endpoint["parsed"]
    return bool(parsed["loopback"] or (parsed["address"] and address_kind(parsed["address"]) == "local"))


def proxy_params(a, endpoint, model):
    """proxy.json's parameters of the --llm endpoint: what runtime.llm_request applies (the kind, every model's and
    each model's values, the forced ones, the start's override) and the loop guard."""
    override, start = start_params(a, endpoint)
    loop = loop_config(endpoint, start)
    names, limited = offered_models(a)
    # The family preset (the loop guard's sampling layer) and the model profile's parameters over it: the relay's
    # lowest default (runtime.llm_values), under the endpoint's values and the start's.
    presets = {name: {**(family_preset(name) if loop["sampling"] else {}), **profile_request(endpoint, name)}
               for name in names}
    # "models": the only models the relay takes requests for (--llm-models, --llm NAME:MODEL,...), None for any model
    # of the server; "families": the family presets for a model of the server that was not listed at the start;
    # "mcp": the start's MCP servers by name (runtime.llm_resource_hint finds one of their tools as its example);
    # "keepalive": the seconds of silence after which the relay keeps a streamed answer alive (KEEPALIVE: a model
    # server of your own, which sends nothing while it reads a long prompt).
    servers = [name for name in dict.fromkeys(getattr(a, "mcp", None) or []) if isinstance(name, str)]
    # "tools": Claude Code's own tools the relay keeps in its requests (--llm-tools; none: every tool).
    keep = kept_tools(start_tools(a)[0]) if runs_claude(a) else None
    return {"kind": endpoint["kind"], "params": endpoint.get("params") or {}, "forced": endpoint.get("forced") or {},
            **({"mcp": servers} if servers else {}), **({"tools": {"keep": keep}} if keep is not None else {}),
            **({"keepalive": KEEPALIVE} if local_model_server(endpoint) else {}),
            "override": override, "sent": {api: list(keys) for api, keys in SENT[endpoint["kind"]].items()},
            "nested": {api: list(keys) for api, keys in NESTED.get(endpoint["kind"], {}).items()},
            "presets": {name: values for name, values in presets.items() if values}, "loop": loop,
            "models": names if limited else None,
            "families": profile_families(endpoint, loop["sampling"]) +
                        ([[pattern.pattern, values] for pattern, values in FAMILY_PRESETS] if loop["sampling"] else [])}


def agent_context(endpoint, model, a=None, reported=None, loaded=None, served=None):
    """The context window the agents are told for MODEL: num_ctx of its parameters (the start's first), else the
    window the server has the model LOADED with, else what it reports (REPORTED). With the loop guard's context layer:
    a warning when the agent would be told more than the server keeps (it cuts the conversation then) - not when
    the window Ollama SERVES the model with is known: start_context tells the agent that, and says so."""
    start = start_params(a, endpoint) if a is not None else ({}, {})
    told, chosen = told_context(endpoint, model, start[0], reported, loaded)
    keeps = loaded or (reported if chosen else None)
    if told and keeps and told > keeps and not served and loop_config(endpoint, start[1])["context"]:
        print(f"agent-sandbox: WARNING: --llm {endpoint['name']}: the agent is told a context window of {told} "
              f"tokens{' (num_ctx)' if chosen else ''}, but the server {'has' if loaded else 'reports'} {keeps} for "
              f"{model}{' loaded' if loaded else ''}: it would cut the conversation, which makes models repeat "
              "themselves; load the model with a larger context or lower num_ctx", file=sys.stderr)
    return told


def loaded_context(endpoint, model, get=None, client=None, behind_vpn=False):
    """The context window the server has MODEL loaded with now (LM Studio: its loaded instance; Ollama: /api/ps),
    or None (not loaded, another kind, behind a VPN). Never raises."""
    if endpoint["kind"] not in ("lmstudio", "ollama") or endpoint.get("vpn") or behind_vpn or \
            os.environ.get("SANDBOX_AI_LLM_NO_CONTEXT_FETCH") == "1":
        return None
    try:
        client = client or Client(endpoint, get, timeout=CONTEXT_TIMEOUT)
        if endpoint["kind"] == "ollama":
            for m in (optional(client.json, "/api/ps", {}) or {}).get("models") or []:
                if isinstance(m, dict) and m.get("name") == model:
                    return context_value(m.get("context_length"))
            return None
        for instance in (lmstudio_native(client.json).get(model) or {}).get("loaded_instances") or []:
            config = instance.get("config") if isinstance(instance, dict) else None
            if isinstance(config, dict) and context_value(config.get("context_length")):
                return config["context_length"]
    except Exception:  # noqa: BLE001 - only a warning depends on it
        return None
    return None


def ollama_served_context(endpoint, model, client=None, get=None, behind_vpn=False, load=True):
    """(the context window Ollama serves MODEL's agent requests with, how it was learnt: "loaded", "modelfile" or
    "loaded now"), or (None, None). Its OpenAI-compatible API takes no num_ctx: a request runs with the window the
    model is loaded with (/api/ps context_length), else the one Ollama loads it with at that request - the
    Modelfile's num_ctx (/api/show parameters), else the server's OLLAMA_CONTEXT_LENGTH (or its default), which its
    API does not report: then the model is loaded as the agent's first request would load it (POST /api/generate
    without a prompt or options; it waits for that request anyway) and /api/ps read - LOAD=False: (None, None) then.
    Not for another kind, nor from this host for a server behind a VPN. Never raises."""
    if endpoint.get("kind") != "ollama" or endpoint.get("vpn") or behind_vpn or \
            os.environ.get("SANDBOX_AI_LLM_NO_CONTEXT_FETCH") == "1":
        return None, None

    names = {model} if ":" in model.rsplit("/", 1)[-1] else {model, model + ":latest"}  # /api/ps: llama3:latest

    def running(answer):
        models = answer.get("models") if isinstance(answer, dict) else None
        for m in models if isinstance(models, list) else []:
            if isinstance(m, dict) and m.get("name") in names:
                return context_value(m.get("context_length"))
        return None
    try:
        client = client or Client(endpoint, get, timeout=LOAD_TIMEOUT)
        found = running(optional(client.json, "/api/ps", {}))
        if found:
            return found, "loaded"
        try:
            shown = client.json("/api/show", body={"model": model})
        except ValueError:
            shown = None
        parameters = shown.get("parameters") if isinstance(shown, dict) else None
        own = re.search(r"^num_ctx\s+(\d{1,9})\s*$", parameters, re.MULTILINE) if isinstance(parameters, str) \
            else None
        if own and context_value(int(own.group(1))):
            return int(own.group(1)), "modelfile"
        if not load:
            return None, None
        client.json("/api/generate", body={"model": model, "stream": False})
        found = running(optional(client.json, "/api/ps", {}))
        return (found, "loaded now") if found else (None, None)
    except Exception as exc:  # noqa: BLE001 - what cannot be learnt leaves the told window
        LOG.info("llm", f"the window Ollama serves {model} with is not known", exc)
        return None, None


OLLAMA_HOW = {"loaded": "as it is loaded", "modelfile": "its Modelfile's num_ctx", "loaded now": "as it loads it",
              "default": "as it loads a model that is not loaded"}


def served_windows(endpoint, models, client, budget=STATUS_BUDGET):
    """MODELS (a listing's entries) of an Ollama ENDPOINT with the window Ollama serves each one's OpenAI-compatible
    requests with, as ollama_served_context learns it but loading nothing: "served_context" (tokens) and
    "served_how" - "loaded" (/api/ps), "modelfile" (its Modelfile's num_ctx, /api/show), or "default" without a
    number: the server's OLLAMA_CONTEXT_LENGTH (or its own default), which its API does not report (the start learns
    it by loading the model). Asked within BUDGET seconds (a model not reached by then is left as it is); never
    remembered as the model's window (it changes with every load). Another kind's MODELS stay as they are, and so
    does what cannot be read. Returns MODELS."""
    if endpoint.get("kind") != "ollama" or not models:
        return models
    try:
        answer = client.json("/api/ps")
    except Exception as exc:  # noqa: BLE001 - an old or refusing server: the listing as it is
        LOG.info("llm", f"{endpoint.get('name')}: /api/ps not read for the served windows", exc)
        return models
    running = {}
    for m in (answer.get("models") if isinstance(answer, dict) else None) or []:
        if isinstance(m, dict) and isinstance(m.get("name"), str) and context_value(m.get("context_length")):
            running[m["name"]] = m["context_length"]
    end = time.monotonic() + budget
    for model in models:
        if model["name"] in running:
            model.update(served_context=running[model["name"]], served_how="loaded")
            continue
        if time.monotonic() > end:
            break
        try:
            shown = client.json("/api/show", body={"model": model["name"]})
        except Exception as exc:  # noqa: BLE001 - not known for this one
            LOG.info("llm", f"{endpoint.get('name')}: /api/show of {model['name']} not read", exc)
            continue
        parameters = shown.get("parameters") if isinstance(shown, dict) else None
        own = re.search(r"^num_ctx\s+(\d{1,9})\s*$", parameters, re.MULTILINE) if isinstance(parameters, str) \
            else None
        if own and context_value(int(own.group(1))):
            model.update(served_context=int(own.group(1)), served_how="modelfile")
        elif isinstance(shown, dict):
            model["served_how"] = "default"
    return models


def served_context(a, found, model, load=True):
    """(the window Ollama serves MODEL with for this start, how it was learnt), or (None, None): ollama_served_context,
    learnt once per start (kept on A); never by a dry run (no request: the app's previews run one for every change),
    nor from this host for a server behind a VPN. LOAD=False (the server's other models the agent offers): a model
    neither loaded nor with a Modelfile num_ctx is not loaded - it gets the window Ollama loaded the start's model
    with by default ("default"), the one it loads that model with too (a model whose own maximum is smaller is cut
    to that by Ollama: then less is told than served, never more)."""
    if found.get("kind") != "ollama" or getattr(a, "dry_run", False):
        return None, None
    kept = getattr(a, "_ollama_served", None)
    if not isinstance(kept, dict) or kept.get("endpoint") != found.get("name"):
        kept = {"endpoint": found.get("name"), "models": {}, "told": {}}
        try:
            a._ollama_served = kept
        except AttributeError:
            pass
    known = kept["models"].get(model)
    if known and (known[0] or not load or known[2]):
        return known[:2]
    served, how = ollama_served_context(found, model, behind_vpn=bool(getattr(a, "vpn", None)), load=load)
    if how == "loaded now":
        print(printable(f"agent-sandbox: --llm {found['name']}: Ollama loaded {model} (as the agent's first request "
                        f"would) to learn the window it serves it with: {served} tokens"), file=sys.stderr)
    if not served and not load:
        default = [v[0] for v in kept["models"].values() if v[1] == "loaded now"]
        served, how = (default[0], "default") if default else (None, None)
    kept["models"][model] = (served, how, load)
    return served, how


def start_context(a, found, model):
    """The context window the agent is told for the start's MODEL: agent_context's (your num_ctx, else the profile's
    at most what the server has loaded or reports, else that), at most the window Ollama serves it with
    (served_context: its OpenAI-compatible API takes no num_ctx and sandbox-ai's start sends none, so a request runs
    with the window Ollama loads the model with, and it cuts a longer prompt without a word) - a note says so when
    that lowers it. None when nothing is known (the agent's own default then). Learnt once per start (kept on A)."""
    kept = getattr(a, "_ollama_served", None)
    if found.get("kind") == "ollama" and isinstance(kept, dict) and kept.get("endpoint") == found.get("name") and \
            model in kept["told"]:
        return kept["told"][model]
    behind = bool(getattr(a, "vpn", None))
    # What the server reports and has loaded before served_context may load the model: what the agent would be told.
    reported, loaded = context_window(found, model, behind_vpn=behind), loaded_context(found, model, behind_vpn=behind)
    served, how = served_context(a, found, model)
    told = context_value(agent_context(found, model, a, reported, loaded, served))
    kept = getattr(a, "_ollama_served", None)
    if isinstance(kept, dict) and served:
        kept["told"][model] = served if not told or told > served else told
    if not served or (told and told <= served):
        return told
    if told and getattr(a, "agent", None) != "hermes":  # Hermes says its own (it refuses less than 64k)
        print(printable(f"agent-sandbox: note: --llm {found['name']}: Ollama serves {model} with {served} tokens "
                        f"({OLLAMA_HOW.get(how, how)}), not the {told} the agent would be told, and cuts a longer "
                        f"prompt without a word: the agent is told {served}. Ollama's OpenAI-compatible API takes no "
                        "num_ctx; for a larger window set OLLAMA_CONTEXT_LENGTH on the server (and restart it), give "
                        "the model PARAMETER num_ctx in a Modelfile (ollama create), or load it before the start with "
                        f"sandbox-ai llm load {found['name']} {model} --context TOKENS (kept while it stays loaded)"),
              file=sys.stderr)
    return served


def served_limits(a, found, model, contexts):
    """CONTEXTS ({model: window} of model_limits) with each of the server's other models lowered to the window
    Ollama serves it with (served_context, without loading it), the start's MODEL learnt first."""
    if found.get("kind") != "ollama":
        return contexts
    start_context(a, found, model)
    for name in contexts:
        if name != model:
            served = served_context(a, found, name, load=False)[0]
            if served and (not contexts[name] or served < contexts[name]):
                contexts[name] = served
    return contexts


# What each agent assumes for a model whose context window it is not told (read in the sources of the versions
# sandbox-ai builds, 2026-11-16): Codex' model_info_from_slug (its fallback for a model it does not know), OpenCode's
# limit.context 0 (OpenCode 1.19.5 never compacts a model of window 0 on its own; OpenCode 2.2.5 takes 0 too), Qwen
# Code 0.26.4's tokenLimit (DEFAULT_TOKEN_LIMIT 2e5 for a name its table does not match), Goose 1.57.0's
# DEFAULT_CONTEXT_LIMIT, nanobot 0.4.0's context_window_tokens, Pi 1.4.0's default (docs/PI_REVIEW.md), Hermes
# 0.22.5's CONTEXT_PROBE_TIERS (it asks the server, else 256,000 and lower after a refusal), OpenHands' LiteLLM
# catalog, Letta Code 0.34.36's LOCAL_ENDPOINT_DEFAULT_CONTEXT_WINDOW (also its most: it reads llama.cpp's own
# /props, nothing of sandbox-ai's). Claude Code's is claude_window_environment's note. {agent: (name, assumption)}
AGENT_WINDOWS = {
    "codex": ("Codex", "assumes 272,000 tokens (its fallback for a model it does not know)"),
    "opencode": ("OpenCode", "takes a window of 0 (none) for it and never compacts the conversation on its own"),
    "opencode2": ("OpenCode 2", "takes a window of 0 (none) for it"),
    "qwen": ("Qwen Code", "assumes 200,000 tokens for a model name it does not know"),
    "goose": ("Goose", "assumes 128,000 tokens for a model name it does not know"),
    "dsh": ("DSH", "uses its own default (and grew past a window it was told in sandbox-ai's tests)"),
    "pi": ("Pi", "assumes 128,000 tokens"),
    "openhands": ("OpenHands", "takes the window from LiteLLM's catalog, which has none for a local model"),
    "nanobot": ("nanobot", "assumes 200,000 tokens (its contextWindowTokens default)"),
    "hermes": ("Hermes", "asks the server itself, else assumes 256,000 tokens and lowers it after a refusal"),
    "letta": ("Letta Code", "assumes 128,000 tokens"),
}
LETTA_WINDOW = 128000  # Letta Code's LOCAL_ENDPOINT_DEFAULT_CONTEXT_WINDOW (also the most it takes)


def unknown_window_note(a, found, model, context, agent):
    """At a start with a model server of your own (FOUND) whose window for MODEL is not known (CONTEXT None): a note
    with what AGENT assumes instead (AGENT_WINDOWS) - it may compact too late (the server refuses or cuts the
    conversation) or too early - and how to set the window; once per agent and start (kept on A). Nothing is
    written for the window then: a guess of sandbox-ai's would be as wrong. Letta Code takes no window from
    sandbox-ai (it reads llama.cpp's /props itself: no note there): for it also a known window under the
    LETTA_WINDOW it assumes.
    Claude Code's note is claude_window_environment's; a public service's models the agents' catalogs know."""
    letta = agent == "letta"
    if agent not in AGENT_WINDOWS or (context and not (letta and context < LETTA_WINDOW)) or \
            (letta and found.get("kind") == "llamacpp") or not local_model_server(found):
        return
    noted = getattr(a, "_window_noted", None)
    if not isinstance(noted, set):
        noted = set()
        try:
            a._window_noted = noted
        except AttributeError:
            pass
    if agent in noted:
        return
    noted.add(agent)
    label, assumes = AGENT_WINDOWS[agent]
    if letta and context:
        text = (f"{model} has a context window of {context} tokens, but {label} {assumes} for a server that is not "
                "llama.cpp and has no setting sandbox-ai can give it: it compacts too late, and the server refuses "
                f"or cuts the conversation. Load the model with {LETTA_WINDOW} tokens or more")
    elif letta:
        text = (f"the context window of {model} is not known; {label} {assumes} for a server that is not llama.cpp "
                "and has no setting sandbox-ai can give it: if the server keeps less, it compacts too late. Load "
                f"the model with {LETTA_WINDOW} tokens or more")
    else:
        text = (f"the context window of {model} is not known (the server does not say it, or is not asked from "
                f"this host); {label} {assumes}, so it may compact too late (the server refuses or cuts the "
                f"conversation) or too early. Set the window: sandbox-ai llm set {found['name']} --model {model} "
                "--param num_ctx=TOKENS (or --llm-param num_ctx=TOKENS for this start)")
    print(printable(f"agent-sandbox: note: --llm {found['name']}: {text}"), file=sys.stderr)


def loop_notes(name, endpoint, given):
    """What `llm set NAME --loop-guard GIVEN` says besides: a layer set while the guard is off acts only once it is
    on; a block count at or below the warn count pauses tool use before the model was ever warned."""
    if not given:
        return []
    own = endpoint.get("loop_guard") or {}
    layers = [text for text in given if "=" in str(text)]
    config = loop_config(endpoint)
    notes = []
    if layers and not config["enabled"]:
        notes.append(f"note: the loop guard of {name} is off (" + ("this server's own setting" if own.get("enabled") is
                     False else "your defaults") + f"): the layers apply once it is on (sandbox-ai llm set {name} "
                     "--loop-guard on)")
    elif config["warn"] and config["block"] and config["block"] <= config["warn"]:
        notes.append(f"note: block ({config['block']}) comes before warn ({config['warn']}): tool use is paused "
                     "before the model was ever warned; a block count above the warn count warns first")
    return notes


def set_params(args, out=print):
    """sandbox-ai llm set NAME [--model MODEL] [--param K=V]... [--force-param K=V]... [--unset K]... [--reset]
    [--loop-guard on|off|N]: the parameters of one model (every model without --model), under the store's lock."""
    if not (args.param or args.force_param or args.unset or args.reset or args.loop_guard or args.evict or
            args.memory is not None):
        raise ValueError("llm set: nothing to change (--param, --force-param, --unset, --reset, --loop-guard, "
                         "--evict or --memory)")
    if args.memory is not None and args.memory != 0 and memory_value(args.memory) is None:
        raise ValueError(f"--memory: the server's memory in GB, 1 to {MEMORY_MAX_GB} (0 forgets it)")
    scope = args.model or "*"
    if scope != "*" and not MODEL_NAME.fullmatch(scope):
        raise ValueError(f"--model: not a model name: {printable(scope)!r}")
    with settings_lock(store_path()):
        store = load_store()
        if args.name not in store["endpoints"]:
            endpoint(args.name)  # its refusal names the known ones
        found = check_record(args.name, store["endpoints"][args.name])
        record = store["endpoints"][args.name]
        params = {key: dict(value) for key, value in (record.get("params") or {}).items()}
        forced = {key: list(value) for key, value in (record.get("forced") or {}).items()}
        values = {} if args.reset else params.get(scope, {})
        strong = set() if args.reset else set(forced.get(scope, []))
        for key in args.unset:
            if key not in PARAMS:
                raise ValueError(f"--unset: unknown parameter {printable(key)!r}")
            values.pop(key, None)
            strong.discard(key)
        for text, force in [(t, False) for t in args.param] + [(t, True) for t in args.force_param]:
            key, value = parse_param(found["kind"], text, "--force-param" if force else "--param")
            values[key] = value
            (strong.add if force else strong.discard)(key)
        params[scope] = values
        forced[scope] = [key for key in values if key in strong]
        record["params"] = {key: value for key, value in params.items() if value}
        record["forced"] = {key: value for key, value in forced.items() if value and key in record["params"]}
        for field in ("params", "forced"):
            if not record[field]:
                record.pop(field)
        if args.loop_guard:
            record["loop_guard"] = parse_loop(args.loop_guard, into=record.get("loop_guard") or {})
            if not record["loop_guard"]:
                record.pop("loop_guard")
        if args.evict == "allow":
            record["evict"] = "allow"
        elif args.evict == "never":
            record.pop("evict", None)
        if args.memory is not None:
            if args.memory:
                record["memory_gb"] = memory_value(args.memory)
            else:
                record.pop("memory_gb", None)
        found = check_record(args.name, record)
        save_store(store)
    for line in describe(found):
        out(line)
    for note in loop_notes(args.name, found, args.loop_guard):
        out(note)
    hint = modelfile_hint(found, args.model, effective_params(found, args.model)[0]) if args.model else None
    if hint:
        out(printable("note: " + hint))
    reported = context_value((found.get("contexts") or {}).get(args.model)) if args.model else None
    chosen = ((found.get("params") or {}).get(scope) or {}).get("num_ctx")
    if chosen and reported and chosen > reported:
        out(f"note: num_ctx {chosen} is larger than the {reported} tokens the server reported for {args.model}: "
            "the agent would be told more than the server keeps")
    return 0


# --- keeping the other models loaded --------------------------------------------------------------------------------
#
# Two sandboxes on different models of one server must not unload each other's model ("evict": "never", the
# default; "allow" leaves it to the server). sandbox-ai's own reads (llm status, models, check, the context window)
# only list (GET) or POST an empty body that names no model: none of them loads one. What unloads models is the server:
#   LM Studio  JIT loading (on by default) loads a model at its first request, and Auto-Evict (on by default,
#              Developer > Server Settings) unloads the models loaded that way before it loads another; models loaded
#              explicitly (lms load, POST /api/v1/models/load) are not affected (LM Studio's docs, "Idle TTL and
#              Auto-Evict"). So with evict never a start loads its model explicitly when no instance of it is loaded
#              (with num_ctx as its context_length), and leaves a loaded one as it is.
#   Ollama     keeps up to OLLAMA_MAX_LOADED_MODELS (default 3 per GPU) and unloads one idle for OLLAMA_KEEP_ALIVE (5
#              minutes) or when memory runs short; its API does not report either setting, and its OpenAI-compatible
#              API has no keep_alive field: llm check says how to set them on the server.
#   llama.cpp  one model per server, in router mode up to --models-max at once (least recently used unloaded).
#   vLLM       one model per server: nothing is unloaded.
EVICT = ("never", "allow")
LOAD_TIMEOUT = 300   # seconds LM Studio may take to load a model at a start (never fatal)


def evict_notes(endpoint):
    """What the server itself does about unloading models, and how to change it there (llm check, the app)."""
    kind, allowed = endpoint["kind"], endpoint.get("evict") == "allow"
    if kind == "lmstudio":
        return ["LM Studio unloads a JIT-loaded model when it JIT-loads another (Auto-Evict, on by default: Developer "
                "> Server Settings); " + ("evict allow: starts leave loading to JIT" if allowed else
                                          "a start loads its model explicitly (not affected by Auto-Evict)")]
    if kind == "ollama":
        return ["Ollama unloads a model idle for OLLAMA_KEEP_ALIVE (default 5m) and when more than "
                "OLLAMA_MAX_LOADED_MODELS (default 3 per GPU) or more than its memory are asked for; its API does not "
                "report them: set OLLAMA_MAX_LOADED_MODELS=2 or more and OLLAMA_KEEP_ALIVE=-1 (or 1h) on the server "
                "so that two sandboxes keep their models"]
    if kind == "llamacpp":
        return ["llama.cpp serves one model; in router mode up to --models-max at once (the least recently used is "
                "unloaded): start it with a --models-max that fits the models you use together"]
    return []


def keep_loaded(endpoint, model, a=None, get=None, client=None, behind_vpn=False, runs=None):
    """evict never (the default): LM Studio loads MODEL explicitly, unless an instance of it is loaded, so that its
    Auto-Evict never unloads it for another sandbox's model. An instance LM Studio has loaded JIT (it carries an idle
    TTL: instance_kept "jit") is said, and when no other running sandbox uses MODEL (RUNS: net.running(), read when
    needed) it is replaced by an explicit one: the JIT instance unloaded first, then MODEL loaded with num_ctx (else
    the JIT instance's context) - never twice in memory (a second load is a second instance, MODEL:2). Returns a note
    (a warning starts with WARNING:), or None; never raises (a model that does not load is loaded by the agent's first
    request, as before). Not from this host for a server behind a VPN."""
    if endpoint["kind"] != "lmstudio" or endpoint.get("evict") == "allow" or endpoint.get("vpn") or behind_vpn or \
            os.environ.get("SANDBOX_AI_LLM_NO_CONTEXT_FETCH") == "1":
        return None
    try:
        # One start at a time per server, from its look at the loaded instances to its load: two starts at once (two
        # windows, a double click) both found the model not loaded and both loaded it - in memory twice (MODEL:2).
        with settings_lock(load_lock_path(endpoint), timeout=2 * LOAD_TIMEOUT + UNLOAD_TIMEOUT):
            return load_ahead(endpoint, model, a, get, client, runs)
    except Exception as exc:  # noqa: BLE001 - never fatal
        return f"LM Studio: {model} not loaded ahead ({printable(exc)[:200]}): left to JIT loading"


def load_lock_path(endpoint):
    """The file whose lock (filelock: FILE.lock) one start holds while it loads a model on ENDPOINT's server (its URL:
    two endpoints of one server share it)."""
    digest = hashlib.sha256(str(endpoint.get("url") or endpoint.get("name")).encode("utf-8", "replace")).hexdigest()
    return ca_dir() / "loading" / digest[:24]


def load_ahead(endpoint, model, a, get, client, runs):
    """keep_loaded's work, under its lock."""
    client = client or Client(endpoint, get, timeout=LOAD_TIMEOUT)
    native = lmstudio_native(client.json)
    if not native:
        return f"LM Studio's /api/v1/models (0.4 or later) does not answer: {model} is left to JIT loading"
    found = native.get(model)
    if found is None:
        return None  # an instance id or a model LM Studio does not list: its own JIT loading
    instances = [i for i in found.get("loaded_instances") or [] if isinstance(i, dict)]
    override = start_params(a, endpoint)[0] if a is not None else None
    values = effective_params(endpoint, model, override)[0]
    most = context_value(found.get("max_context_length"))
    if values.get("num_ctx") and most and "num_ctx" not in effective_params(endpoint, model, override, False)[0]:
        values["num_ctx"] = min(values["num_ctx"], most)  # the profile's window: at most what the model takes
    if not instances:
        status, _, refused = lmstudio_load(client, model, values.get("num_ctx"), values)
        if status >= 300:
            return f"LM Studio did not load {model} (HTTP {status}): left to JIT loading"
        return f"LM Studio loaded {model} for this start (not JIT: Auto-Evict leaves it loaded; unload it in LM " \
               "Studio or with lms unload)" + (f"; {refused}" if refused else "")
    if any(instance_kept("lmstudio", i) != "jit" for i in instances):
        return None  # loaded explicitly (or LM Studio does not say how): left as it is
    warning = f"WARNING: LM Studio has {model} loaded JIT (idle TTL): {JIT_WARNING}"
    users = model_users(endpoint, runs, exclude=getattr(a, "run_id", None)).get(model)
    if users:
        return f"{warning}. The running sandbox(es) {', '.join(users)} use it, so it is left as it is; " \
               f"sandbox-ai llm load {endpoint['name']} {model} --force loads it explicitly"
    context = values.get("num_ctx") or next((instance_context(i) for i in instances if instance_context(i)), None)
    for instance in instances:
        status, _ = client.call("/api/v1/models/unload", {"instance_id": instance.get("id") or model})
        if status >= 300:
            return f"{warning}. Its JIT instance did not unload (HTTP {status}): left as it is"
    status, _, refused = lmstudio_load(client, model, context, values)
    if status >= 300:
        return f"{warning}. Its JIT instance was unloaded, but LM Studio did not load {model} explicitly (HTTP " \
               f"{status}): the agent's first request loads it JIT again"
    told = f" (context {context})" if context else ""
    return f"LM Studio had {model} loaded JIT (Auto-Evict could unload it for another sandbox's model); no other " \
           f"running sandbox uses it: loaded explicitly for this start{told}, the JIT instance unloaded first, " \
           "so the model is never in memory twice" + (f"; {refused}" if refused else "")


# --- the models a server has loaded; loading and unloading them (llm loaded, load, unload) --------------------------
#
# What each server's API offers, checked against its documentation (November 2026):
#   LM Studio 0.4  GET /api/v1/models: per model its loaded_instances [{id, config: {context_length, ...}}]
#                  POST /api/v1/models/load {model, context_length?, ...} -> {type, instance_id, load_time_seconds,
#                  status "loaded"}; POST /api/v1/models/unload {instance_id} -> {instance_id}.
#                  Not in its documentation, seen by others' live tests (0.4.26): an instance with an idle TTL (loaded
#                  JIT, or with lms load --ttl) carries remaining_ttl_seconds, one loaded without a TTL (REST, lms load)
#                  has none; the load endpoint refuses keys it does not know (a "ttl" too: HTTP 400
#                  "unrecognized_keys"); a second load of a model is a second instance (MODEL:2), in memory twice.
#   Ollama         GET /api/ps: models [{name, model, size, size_vram, expires_at, context_length (newer versions)}];
#                  POST /api/generate {model} without a prompt loads it (done_reason "load"), with keep_alive 0
#                  unloads it ("unload"); keep_alive in seconds, negative: until it is unloaded (its FAQ), options
#                  num_ctx the context it is loaded with. Each request sets the time again (its keep_alive, else
#                  OLLAMA_KEEP_ALIVE): the agents' OpenAI-compatible requests carry none.
#   llama.cpp      router mode (llama-server --models-dir or --models-preset): /v1/models with status.value per model
#                  (loaded, loading, unloaded, sleeping, ...), POST /models/load {model} and POST /models/unload {model}
#                  -> {success: true}; no context or TTL at the load (the router's arguments decide). One model else.
#   vLLM           serves the model it was started with: nothing to load or unload.
JIT_WARNING = "LM Studio may unload it when another sandbox's model is loaded: Auto-Evict; see Help > Keeping models " \
              "loaded"
# How a loaded instance stays loaded (llm loaded --json "kept"):
KEPT = {"explicit": "loaded explicitly: stays until it is unloaded",
        "jit": "JIT (idle TTL): LM Studio unloads it when idle, and Auto-Evict when it JIT-loads another model",
        "timed": "until idle for its keep_alive (expires_at), or memory runs short",
        "router": "the router unloads the least recently used beyond --models-max or its memory",
        "server": "served for as long as the server runs"}
FOREVER_YEARS = 50  # Ollama's keep_alive -1: an expires_at hundreds of years ahead
UNLOAD_TIMEOUT = 60


def load_net_module():
    spec = importlib.util.spec_from_file_location("sandbox_net_llm", HERE / "net.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def running_runs():
    """{run: record} of the sandboxes running now (sandbox-ai net's registry); {} where it cannot be read."""
    try:
        return load_net_module().running()
    except Exception:  # noqa: BLE001 - who uses a model is said where it is known
        return {}


def model_users(endpoint, runs=None, exclude=None):
    """{model: [the running sandboxes that use it]} on ENDPOINT: those started with --llm NAME[:MODEL,...] of it (the
    models named; none named: the endpoint's model). RUNS: {run: record} (net.running() when None); EXCLUDE: a run id
    left out (the start asking)."""
    found = {}
    for run, record in (running_runs() if runs is None else runs).items():
        if not isinstance(record, dict) or (exclude and exclude in (run, record.get("run"))):
            continue
        try:
            name, models = parse_models(record.get("llm")) if isinstance(record.get("llm"), str) else (None, [])
        except ValueError:
            continue
        if name != endpoint["name"]:
            continue
        label = printable(str(record.get("name") or record.get("session") or run))[:64]
        for model in models or ([endpoint["model"]] if endpoint.get("model") else []):
            found.setdefault(model, [])
            if label not in found[model]:
                found[model].append(label)
    return found


def instance_ttl(instance):
    """The seconds LM Studio says an instance stays loaded while idle (remaining_ttl_seconds), or None."""
    for where in (instance, instance.get("config") if isinstance(instance.get("config"), dict) else {}):
        value = where.get("remaining_ttl_seconds")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 1 << 40:
            return int(value)
    return None


def instance_context(instance):
    config = instance.get("config") if isinstance(instance.get("config"), dict) else {}
    return context_value(config.get("context_length"))


def instance_kept(kind, instance):
    """How a loaded instance stays loaded (KEPT): for LM Studio "jit" with an idle TTL, else "explicit"."""
    if kind == "lmstudio":
        return "jit" if instance_ttl(instance) is not None else "explicit"
    return None


def forever(expires):
    """Whether an Ollama expires_at is so far ahead that the model stays until it is unloaded (keep_alive -1)."""
    match = re.match(r"(\d{4})-", expires) if isinstance(expires, str) else None
    return bool(match) and int(match[1]) >= time.gmtime().tm_year + FOREVER_YEARS


def lmstudio_load_body(model, context=None, values=None):
    """POST /api/v1/models/load's body: only keys LM Studio knows (it refuses others): the context window, parallel
    1 and, of VALUES (the effective parameters), flash_attention and offload_kv_cache_to_gpu. parallel: LM Studio's
    default 4 keeps no prompt cache for several models (gpt-oss, Nemotron MLX: every agent step read the whole
    conversation again, 22-32 s at 12k tokens instead of 0.3-0.5 s, research 2026-11-15); an agent sends one request
    at a time, so 1 costs it nothing (a second agent on the same model waits its turn)."""
    return {"model": model, **({"context_length": context} if context else {}), "parallel": 1,
            **{key: values[key] for key in ("flash_attention", "offload_kv_cache_to_gpu")
               if isinstance((values or {}).get(key), bool)}}


PARALLEL_REFUSED = 'this LM Studio does not know "parallel" (its load endpoint refused it: an older version): ' \
    "loaded without it, so with LM Studio's default of 4 parallel requests, which keeps no prompt cache for some " \
    "models (every agent step reads the whole conversation again); update LM Studio to 0.4 or later"


def lmstudio_load(client, model, context=None, values=None):
    """POST /api/v1/models/load (lmstudio_load_body) with CLIENT.call: (status, bytes, note). An LM Studio that does
    not know "parallel" (a 4xx whose answer names the key; not 401/403, a refused API key) is sent the load once more
    without it; NOTE then says so (PARALLEL_REFUSED), else None. Any other refusal is the answer as it is."""
    body = lmstudio_load_body(model, context, values)
    status, data = client.call("/api/v1/models/load", body)
    if 400 <= status < 500 and status not in (401, 403) and isinstance(data, bytes) and \
            b"parallel" in data[:65536].lower():
        body.pop("parallel", None)
        status, data = client.call("/api/v1/models/load", body)
        return status, data, PARALLEL_REFUSED
    return status, data, None


def lmstudio_load_json(client, model, context=None, values=None):
    """lmstudio_load for `llm load`: (the JSON answer, note); ValueError as Client.json says it (never a secret)."""
    where = f"{client.endpoint['url']}/api/v1/models/load"
    try:
        status, data, refused = lmstudio_load(client, model, context, values)
    except OSError as exc:
        raise ValueError(f"{where}: {printable(getattr(exc, 'reason', exc))}") from None
    if status in (401, 403):
        raise ValueError(f"{where}: the server refused the key (HTTP {status})")
    if status != 200:
        raise ValueError(f"{where}: HTTP {status}")
    try:
        return json.loads(data), refused
    except (ValueError, RecursionError):
        raise ValueError(f"{where}: not JSON") from None


def loaded_raw(kind, fetch):
    """What a server of KIND has loaded now: {"instances": [{model, instance, context?, size?, memory_bytes?,
    vram_bytes?, ram_bytes?, kept?, ttl_seconds?, expires_at?, state?}], "reported_by": [...], "load": bool,
    "unload": bool} (load/unload: whether its API can). Untrusted: see clean_loaded. ValueError: no answer."""
    instances = []
    if kind == "lmstudio":
        listed = fetch("/api/v1/models")["models"]
        for m in listed if isinstance(listed, list) else []:
            if not isinstance(m, dict) or not isinstance(m.get("key"), str):
                continue
            for instance in m.get("loaded_instances") or []:
                if not isinstance(instance, dict):
                    continue
                instances.append({"model": m["key"], "instance": instance.get("id") or m["key"],
                                  "context": instance_context(instance), "size": count(m.get("size_bytes")),
                                  "kept": instance_kept(kind, instance), "ttl_seconds": instance_ttl(instance),
                                  "type": m.get("type")})
        return {"instances": instances, "reported_by": ["LM Studio /api/v1/models"], "load": True, "unload": True}
    if kind == "ollama":
        tags = optional(fetch, "/api/tags", {})
        sizes = {m.get("name"): m.get("size") for m in (tags.get("models") if isinstance(tags, dict) else None) or []
                 if isinstance(m, dict)}
        for m in fetch("/api/ps")["models"] or []:
            if not isinstance(m, dict):
                continue
            memory, vram = count(m.get("size")), count(m.get("size_vram"))
            expires = m.get("expires_at")
            instances.append({"model": m.get("name"), "instance": m.get("name"),
                              "size": count(sizes.get(m.get("name"))),
                              "memory_bytes": memory, "vram_bytes": vram,
                              "ram_bytes": memory - vram if memory is not None and vram is not None and memory >= vram
                              else None, "context": context_value(m.get("context_length")),
                              "kept": "explicit" if forever(expires) else "timed",
                              "expires_at": None if forever(expires) else expires})
        return {"instances": instances, "reported_by": ["Ollama /api/ps"], "load": True, "unload": True}
    if kind == "llamacpp":
        answer = fetch("/v1/models")
        data = [m for m in answer["data"] if isinstance(m, dict)]
        router = any(isinstance(m.get("status"), dict) for m in data)
        props = None if router else optional(fetch, "/props")
        for m in data:
            status = m.get("status") if isinstance(m.get("status"), dict) else {}
            state = status.get("value") if isinstance(status.get("value"), str) else None
            if router and state not in ("loaded", "loading", "sleeping"):
                continue
            memory = m.get("memory") if isinstance(m.get("memory"), dict) else {}
            effective = memory.get("effective") if isinstance(memory.get("effective"), dict) else {}
            meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
            entry = {"model": m.get("id"), "instance": m.get("id"), "size": count(meta.get("size")),
                     "context": context_value(effective.get("n_ctx")) or argument_context(status.get("args")),
                     "kept": "router" if router else "server", "state": state if router else None}
            vram = ram = 0
            for device in effective.get("devices") or []:
                if isinstance(device, dict):
                    used = sum(count(device.get(k)) or 0 for k in ("model", "context", "compute"))
                    if device.get("kind") == "host":
                        ram += used
                    else:
                        vram += used
            if effective.get("devices"):
                entry.update(vram_bytes=vram, ram_bytes=ram, memory_bytes=vram + ram)
            if not router and isinstance(props, dict) and not entry["context"]:
                settings = props.get("default_generation_settings")
                entry["context"] = context_value(settings.get("n_ctx")) if isinstance(settings, dict) else None
            instances.append(entry)
        return {"instances": instances, "reported_by": ["llama.cpp /v1/models"], "load": router, "unload": router}
    if kind == "vllm":
        for m in fetch("/v1/models")["data"]:
            if isinstance(m, dict):
                instances.append({"model": m.get("id"), "instance": m.get("id"),
                                  "context": context_value(m.get("max_model_len")), "kept": "server"})
        return {"instances": instances, "reported_by": ["vLLM /v1/models"], "load": False, "unload": False}
    return {"instances": None, "reported_by": [], "load": False, "unload": False}


def argument_context(args):
    """The context a llama.cpp router started a model with (its status args: -c N, --ctx-size N, -ctx N)."""
    if not isinstance(args, list):
        return None
    for index, arg in enumerate(args[:-1]):
        if arg in ("-c", "--ctx-size", "-ctx") and isinstance(args[index + 1], str) and args[index + 1].isdigit():
            return context_value(int(args[index + 1]))
    return None


def clean_loaded(answer):
    """A loaded_raw answer checked: names as models are named, numbers where numbers belong, texts terminal-safe."""
    instances = None
    if isinstance(answer.get("instances"), list):
        instances = []
        for raw in answer["instances"][:200]:
            model, instance = raw.get("model"), raw.get("instance")
            if not isinstance(model, str) or not MODEL_NAME.fullmatch(model):
                continue
            item = {"model": model, "instance": instance if isinstance(instance, str) and
                    MODEL_NAME.fullmatch(instance) else model}
            for key in ("size", "memory_bytes", "vram_bytes", "ram_bytes", "ttl_seconds"):
                if count(raw.get(key)) is not None:
                    item[key] = raw[key]
            if context_value(raw.get("context")):
                item["context"] = raw["context"]
            if raw.get("kept") in KEPT:
                item["kept"] = raw["kept"]
            if isinstance(raw.get("expires_at"), str) and raw["expires_at"].strip():
                item["expires_at"] = printable(raw["expires_at"].strip())[:40]
            for key in ("state", "type"):
                if isinstance(raw.get(key), str) and re.fullmatch(r"[a-z_-]{1,20}", raw[key]):
                    item[key] = raw[key]
            instances.append(item)
    return {"instances": instances, "reported_by": [v for v in answer.get("reported_by") or [] if isinstance(v, str)],
            "load": bool(answer.get("load")), "unload": bool(answer.get("unload"))}


def endpoint_loaded(endpoint, client, runs=None):
    """`llm loaded NAME --json`: {name, kind, url, read_at, instances (None: not reported), reported_by, load, unload,
    used_by: {model: [running sandboxes]}, kept: KEPT}; ValueError when the server does not answer."""
    try:
        found = clean_loaded(loaded_raw(endpoint["kind"], client.json))
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"unexpected answer from the {KINDS[endpoint['kind']]['label']}: {printable(exc)}") from None
    users = model_users(endpoint, runs)
    return {"name": endpoint["name"], "kind": endpoint["kind"], "url": endpoint["url"], "read_at": now_text(),
            **found, "used_by": {model: names for model, names in users.items()}, "kept": KEPT,
            "jit_warning": JIT_WARNING}


def loaded_lines(entry):
    """`llm loaded NAME` as text."""
    lines = [f"{entry['name']}: {KINDS[entry['kind']]['label']} at {entry['url']} (read {entry['read_at']})"]
    if entry["instances"] is None:
        return lines + ["  what is loaded is not reported by its API"]
    if not entry["instances"]:
        lines.append("  no model loaded")
    for item in entry["instances"]:
        parts = []
        if item["instance"] != item["model"]:
            parts.append(f"instance {item['instance']}")
        if item.get("context"):
            parts.append(f"context {item['context']}")
        if item.get("memory_bytes") is not None:
            split = [f"{format_size(item[k])} {label}" for k, label in (("vram_bytes", "VRAM"), ("ram_bytes", "RAM"))
                     if item.get(k) is not None]
            parts.append(f"{format_size(item['memory_bytes'])} in memory" + (f" ({', '.join(split)})" if split else ""))
        elif item.get("size") is not None:
            parts.append(f"size {format_size(item['size'])}")
        if item.get("state") and item["state"] != "loaded":
            parts.append(item["state"])
        kept = item.get("kept")
        if kept == "jit":
            parts.append(f"JIT, unloaded after {item['ttl_seconds']} s idle" if item.get("ttl_seconds") is not None
                         else "JIT")
        elif kept == "explicit":
            parts.append("explicit (stays until unloaded)")
        elif kept == "timed" and item.get("expires_at"):
            parts.append(f"unloaded at {item['expires_at']} unless used")
        users = entry["used_by"].get(item["model"])
        if users:
            parts.append(f"used by {', '.join(users)}")
        lines.append(f"  {item['model']}" + (f"  {', '.join(parts)}" if parts else ""))
        if kept == "jit":
            lines.append(f"    {JIT_WARNING}")
    if not entry["load"]:
        lines.append(f"  the {KINDS[entry['kind']]['label']}'s API loads and unloads no model"
                     + (" (llama.cpp: only in router mode)" if entry["kind"] == "llamacpp" else ""))
    return [printable(line) for line in lines]


def in_use_refusal(endpoint, model, users, what):
    return f"{model} on {endpoint['name']} is used by the running sandbox(es) {', '.join(users)}: {what} would " \
           f"take it from them (their next request loads it again, or fails); --force does it all the same"


def load_model(endpoint, model, client, context=None, ttl=None, force=False, runs=None, out=print):
    """`llm load NAME MODEL [--context N] [--ttl S] [--force]`: MODEL loaded explicitly (it stays until unloaded;
    Ollama with --ttl: S seconds idle). A JIT instance (LM Studio) is replaced: unloaded first, so the model is never in
    memory twice - refused while a running sandbox uses it, unless FORCE. Without --context the num_ctx of the model's
    parameters (llm set) applies, as at a start. Returns the exit status."""
    kind, label = endpoint["kind"], KINDS[endpoint["kind"]]["label"]
    if kind not in ("lmstudio", "ollama", "llamacpp"):
        why = "it serves the model it was started with" if kind == "vllm" else "its API has no way to"
        raise ValueError(f"the {label} loads no model on request ({why})")
    if ttl is not None and kind != "ollama":
        raise ValueError(f"--ttl: the {label}'s API takes no time to keep a model at loading"
                         + (" (its load endpoint refuses it: lms load --ttl S on the server's machine sets one)"
                            if kind == "lmstudio" else ""))
    if context is not None and kind == "llamacpp":
        raise ValueError("--context: llama.cpp's router loads a model with the arguments of its --models-preset "
                         "(ctx-size); its load endpoint takes none")
    values = effective_params(endpoint, model)[0]
    if context is None:
        context = values.get("num_ctx")
    if ttl is None and kind == "ollama" and isinstance(values.get("keep_alive"), int):
        ttl = values["keep_alive"]  # the server's keep_alive (llm set --param keep_alive=S), else until unloaded
    current = clean_loaded(loaded_raw(kind, client.json))
    mine = [item for item in current["instances"] or [] if item["model"] == model]
    users = model_users(endpoint, runs).get(model) or []
    if kind == "lmstudio":
        native = lmstudio_native(client.json)
        if model not in native:
            raise ValueError(f"LM Studio lists no model {model} (sandbox-ai llm models {endpoint['name']})")
        explicit = [item for item in mine if item.get("kept") == "explicit"]
        if explicit:
            have = explicit[0].get("context")
            if context and have and context != have:
                instance = explicit[0]["instance"]
                raise ValueError(f"{model} is loaded explicitly with context {have} (instance {instance}): unload it "
                                 f"first (sandbox-ai llm unload {endpoint['name']} {instance}) to load it with "
                                 f"{context}; a second load would hold it in memory twice")
            out(f"{model} is loaded explicitly already (instance {explicit[0]['instance']}"
                + (f", context {have}" if have else "") + "): it stays until it is unloaded")
            return 0
        if mine and users and not force:
            raise ValueError(in_use_refusal(endpoint, model, users, "reloading it"))
        context = context or next((item["context"] for item in mine if item.get("context")), None)
        for item in mine:  # JIT: unloaded first, never two copies in memory
            client.json("/api/v1/models/unload", body={"instance_id": item["instance"]})
            out(f"unloaded the JIT instance {item['instance']} (LM Studio may unload it: Auto-Evict)")
        answer, refused = lmstudio_load_json(client, model, context, values)
        instance = answer.get("instance_id") if isinstance(answer, dict) else None
        out(printable(f"loaded {model} explicitly" + (f" as {instance}" if isinstance(instance, str) else "")
                      + (f", context {context}" if context else "") + ": it stays until it is unloaded (Auto-Evict "
                      "leaves it)"))
        if refused:
            out(printable(f"note: {refused}"))
        return 0
    if kind == "ollama":
        tags = [m.get("name") for m in client.json("/api/tags").get("models") or [] if isinstance(m, dict)]
        if model not in tags:
            raise ValueError(f"Ollama has no model {model} (ollama pull {model} on the server first)")
        have = next((item.get("context") for item in mine if item.get("context")), None)
        if mine and users and not force and context and have and context != have:
            raise ValueError(in_use_refusal(endpoint, model, users, f"loading it again with context {context}"))
        body = {"model": model, "keep_alive": ttl if ttl is not None else -1, "stream": False}
        if context:
            body["options"] = {"num_ctx": context}
        answer = client.json("/api/generate", body=body)
        if not isinstance(answer, dict) or not answer.get("done"):
            raise ValueError(f"Ollama did not say it loaded {model}")
        out(printable(f"loaded {model}" + (f", context {context}" if context else "")
                      + (f": unloaded after {ttl} s idle" if ttl is not None else ": it stays until it is unloaded")))
        out("note: each request sets Ollama's time again, and the agents' requests carry no keep_alive: with "
            "OLLAMA_KEEP_ALIVE=-1 on the server it stays after their requests too")
        return 0
    if not current["load"]:
        raise ValueError("this llama.cpp server is not in router mode (--models-dir or --models-preset): it serves "
                         "the one model it was started with")
    if mine and mine[0].get("state") in ("loaded", "loading"):
        out(f"{model} is {mine[0]['state']} already")
        return 0
    answer = client.json("/models/load", body={"model": model})
    if not isinstance(answer, dict) or answer.get("success") is not True:
        raise ValueError(f"llama.cpp's router did not load {model}")
    out(f"llama.cpp's router loads {model} (the least recently used model goes beyond --models-max)")
    return 0


def unload_model(endpoint, target, client, force=False, runs=None, out=print):
    """`llm unload NAME MODEL|INSTANCE [--force]`: refused while a running sandbox uses the model, unless FORCE.
    Returns the exit status."""
    kind, label = endpoint["kind"], KINDS[endpoint["kind"]]["label"]
    if kind not in ("lmstudio", "ollama", "llamacpp"):
        raise ValueError(f"the {label} unloads no model on request (its API has no way to)")
    current = clean_loaded(loaded_raw(kind, client.json))
    if not current["unload"]:
        raise ValueError("this llama.cpp server is not in router mode: it serves the one model it was started with")
    # A model's name: every instance of it (LM Studio names the first instance as the model); else one instance.
    chosen = [item for item in current["instances"] or [] if item["model"] == target] or \
        [item for item in current["instances"] or [] if item["instance"] == target]
    if not chosen:
        out(f"{target} is not loaded on {endpoint['name']}")
        return 1
    model = chosen[0]["model"]
    users = model_users(endpoint, runs).get(model) or []
    if users and not force:
        raise ValueError(in_use_refusal(endpoint, model, users, "unloading it"))
    for item in chosen:
        if kind == "lmstudio":
            client.json("/api/v1/models/unload", body={"instance_id": item["instance"]})
        elif kind == "ollama":
            client.json("/api/generate", body={"model": item["model"], "keep_alive": 0, "stream": False})
        else:
            answer = client.json("/models/unload", body={"model": item["model"]})
            if not isinstance(answer, dict) or answer.get("success") is not True:
                raise ValueError(f"llama.cpp's router did not unload {item['model']}")
        out(printable(f"unloaded {item['instance']}" + (f" (used by {', '.join(users)})" if users else "")))
    return 0


def manage(args, out=print, get=None, runs=None):
    """sandbox-ai llm loaded|load|unload: from this host, or through the endpoint's VPN (llm add --vpn) as llm models
    reaches it; the same Client (the key only where llm add accepts it, no redirect followed)."""
    found = endpoint(args.name)
    model = getattr(args, "model", None)
    if model is not None and not MODEL_NAME.fullmatch(model):
        raise ValueError(f"not a model name: {printable(model)!r}")
    context, ttl = getattr(args, "context", None), getattr(args, "ttl", None)
    if context is not None and not context_value(context):
        raise ValueError(f"--context: {CONTEXT_RANGE[0]} to {CONTEXT_RANGE[1]} tokens")
    if ttl is not None and not 1 <= ttl <= 10 ** 8:
        raise ValueError("--ttl: 1 or more seconds (without it the model stays until it is unloaded)")
    timeout = LOAD_TIMEOUT if args.command == "load" else UNLOAD_TIMEOUT if args.command == "unload" else TIMEOUT
    runs = running_runs() if runs is None else runs
    with through_vpn(found) as tunnel:
        client = VPNClient(found, tunnel, get, timeout=timeout) if tunnel else Client(found, get, timeout=timeout)
        if args.command == "loaded":
            entry = endpoint_loaded(found, client, runs)
            for line in [json.dumps(entry, indent=2)] if args.json else loaded_lines(entry):
                out(line)
            return 0
        try:
            if args.command == "load":
                return load_model(found, model, client, context, ttl, args.force, runs, out)
            return unload_model(found, model, client, args.force, runs, out)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"unexpected answer from the {KINDS[found['kind']]['label']}: {printable(exc)}") from None


def where_sent(kind, key, apis):
    """Where KEY goes for a server of KIND, in words."""
    if apis:
        return ", ".join(apis)
    if key in MODELFILE_ONLY.get(kind, ()):
        return "Modelfile only"
    if key in LOAD_ONLY.get(kind, ()) and key != "num_ctx":
        return "at the model's load"
    return "the agents' configuration only" + (" and the model's load" if key in LOAD_ONLY.get(kind, ()) else "")


def server_only(kind):
    """[{"setting", "where"}] of what only the server's own start or settings decide for KIND (shown, never set)."""
    return [{"setting": setting, "where": where} for setting, where in SERVER_ONLY.get(kind, ())]


def params_catalogue(kind=None):
    """`llm params --json`: every parameter with its type, range, help and the kinds (and APIs) that take it."""
    found = []
    for key, (kind_of, low, high, text) in PARAMS.items():
        kinds = [k for k in KINDS if key in kind_params(k)]
        if kind and kind not in kinds:
            continue
        found.append({"name": key, "type": {float: "float", int: "int", bool: "bool"}[kind_of], "min": low,
                      "max": high, "help": text, "kinds": kinds,
                      "sent": {k: [api for api, keys in SENT[k].items() if key in keys] for k in kinds},
                      "ceiling": key == "max_tokens", "never_sent": key in AGENT_ONLY,
                      "at_load": [k for k in kinds if key in LOAD_ONLY.get(k, ())]})
    return found


# --- finding servers -------------------------------------------------------------------------------------------
#
# llm detect (and both wizards) look for servers at a few addresses only: this host's loopback and the addresses
# the human names (never a network range, never an address of its own choosing), on the usual ports of each kind.
# Each look is one read-only GET of the model listing, without a proxy, with a short timeout; nothing else is sent,
# and no network is scanned.

DETECT_PORTS = (("ollama", 11434), ("lmstudio", 1234), ("llamacpp", 8080), ("vllm", 8000))
DETECT_TIMEOUT = 1.5


def lan_address():
    """This host's outward-facing IPv4 address, or None; no packet is sent (a UDP socket only picks the route)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return None


def plain_fetch(url, timeout=DETECT_TIMEOUT):
    """The JSON of GET URL: no proxy (a local server is never reached through one), no redirects."""
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), handlers()[0]())
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "sandbox-ai"})
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read(RESPONSE_LIMIT + 1)[:RESPONSE_LIMIT])


def host_form(host):
    return f"[{host}]" if ":" in host else host


def identify(host, port, fetch=plain_fetch, kind=None, hint=None):
    """(kind, models) of the server at HOST:PORT: Ollama by /api/tags, any other by /v1/models (HINT, else the kind
    its port usually has, or what "owned_by" names); KIND: only that kind is tried. ValueError when nothing answers
    so."""
    base = f"http://{host_form(host)}:{port}"
    usual = hint or next((name for name, number in DETECT_PORTS if number == port), "openai-compatible")
    tried = [kind] if kind else [usual] + (["ollama"] if usual != "ollama" else ["openai-compatible"])
    problems = []
    for candidate in dict.fromkeys(tried):
        try:
            if candidate != "ollama" and not kind:
                answer = fetch(base + "/v1/models")
                owners = {str(m.get("owned_by", "")).lower() for m in answer.get("data", []) if isinstance(m, dict)}
                if "vllm" in owners:
                    candidate = "vllm"
                elif owners & {"llamacpp", "llama.cpp"}:
                    candidate = "llamacpp"
            return candidate, discover(candidate, lambda path: fetch(base + path))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f"{candidate}: {printable(getattr(exc, 'reason', exc))}")
    raise ValueError(f"no LLM server answers at {host}:{port} ({'; '.join(problems)})")


def detect_addresses(extra=()):
    """The addresses llm detect looks at: this host's loopback, then EXTRA, the ones the human names (each
    once). This host's LAN address only when named: a look there may reach the network's own policy."""
    found = ["127.0.0.1", *extra]
    return list(dict.fromkeys(address for address in found if address))


def detect(addresses=None, fetch=plain_fetch, ports=None):
    """[{kind, host, port, url, models}] of the servers that answer at ADDRESSES (default: detect_addresses()) on
    the usual PORTS ((kind, port) pairs), looked at in parallel. Addresses only: no name is resolved."""
    import concurrent.futures
    addresses = list(addresses or detect_addresses())
    for address in addresses:
        try:
            ipaddress.ip_address(address)
        except ValueError:
            raise ValueError(f"llm detect looks at IP addresses only: {printable(address)!r}") from None
    targets = [(host, kind, port) for host in addresses for kind, port in (ports or DETECT_PORTS)]

    def look(target):
        host, kind, port = target
        try:
            found, models = identify(host, port, fetch, hint=kind)
        except ValueError:
            return None
        return {"kind": found, "host": host, "port": port, "url": f"http://{host_form(host)}:{port}",
                "models": models}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(16, max(1, len(targets)))) as pool:
        return [entry for entry in pool.map(look, targets) if entry]


def suggested_name(kind, host, taken=()):
    """A name for a server found at HOST: its kind, with the host's last part when it is not this host."""
    base = kind if host in LOOPBACK_HOSTS else f"{kind}-{host.replace(':', '.').rsplit('.', 1)[-1] or 'lan'}"
    name, number = base[:32], 2
    while name in taken:
        name, number = f"{base[:29]}-{number}", number + 1
    return name


def register(name, kind, url, out=print, replace=False, models=None):
    """sandbox-ai llm add NAME --kind KIND --url URL (checked as the command checks it), then MODELS remembered;
    the checked endpoint."""
    args = argparse.Namespace(name=name, kind=kind, url=url, api_key_secret=None, header_secret=[], ca=None,
                              no_ca=False, vpn=None, model=None, anthropic=False, insecure_http=False, replace=replace)
    with settings_lock(store_path()):  # from reading the store to writing it back, as the command
        add(args, out)
    if models is not None:
        remember_models(name, [m["name"] for m in models],
                        {m["name"]: m["context"] for m in models if m.get("context")})
    return endpoint(name)


# --- which agent can use which server --------------------------------------------------------------------

# Servers with the Anthropic Messages API (POST /v1/messages), which Claude Code needs, and since when:
# Ollama 0.14.0 (2026-01-10), LM Studio 0.4.1 (2026-01-30), llama.cpp b7187 (2025-11-28, PR #17570),
# vLLM 0.11.1 (2025-11-18). Any other server only when added with --anthropic (e.g. a LiteLLM gateway).
ANTHROPIC_SINCE = {"ollama": "Ollama 0.14.0", "lmstudio": "LM Studio 0.4.1", "llamacpp": "llama.cpp b7187",
                   "vllm": "vLLM 0.11.1"}


def anthropic_api(endpoint):
    return endpoint["kind"] in ANTHROPIC_SINCE or endpoint.get("anthropic", False)


def article(label):
    """LABEL with "a" or "an" before it."""
    return ("an " if label[:1].lower() in "aeiou" else "a ") + label


def agent_problem(endpoint, agent):
    """Why AGENT cannot use ENDPOINT, or None."""
    if agent == "claude" and not anthropic_api(endpoint):
        return (f"Claude Code speaks only the Anthropic Messages API (/v1/messages); the LLM endpoint "
                f"{endpoint['name']} is {article(KINDS[endpoint['kind']]['label'])} without it. Use Codex or OpenCode with "
                f"it, run Ollama (0.14.0 or later), LM Studio (0.4.1), llama.cpp (b7187) or vLLM (0.11.1), or add "
                f"a server that offers /v1/messages (a LiteLLM gateway, for one) with --anthropic")
    return None


# --- a start with --llm NAME[:MODEL] ------------------------------------------------------------------------
#
# The launcher calls these few functions (see agent_sandbox.py); everything about endpoints stays here.

# What the agent sees instead of the key: the proxy swaps it for the real one on the wire (runtime.llm_head).
PLACEHOLDER = "sandbox-ai-llm-key-placeholder"
KEY_VARIABLE = "SANDBOX_AI_LLM_KEY"
OPENCODE_CONFIG = ".config/opencode/sandbox-ai-llm.json"
# The loop guard's instructions layer: a file of OpenCode's "instructions" (OpenCode concatenates the instructions of
# all its configuration files, so the user's own AGENTS.md and instructions stay as they are).
LOOP_INSTRUCTIONS = ".config/opencode/sandbox-ai-loop-guard.md"
LOOP_TEXT = """# Working without loops (sandbox-ai, local model)

## Before and after each step
- Before a non-trivial task, state how you will know it is done (a test passing, an output, a diff).
- Prefer one small verified change over broad repeated exploration.
- After each tool call, compare what it showed with that goal.

## Tool calls
- Do not repeat a tool call with the same arguments after the same result unless something changed in between.
- If the same command fails twice with the same error, stop retrying it: find the cause first.
- Every search or read answers a specific question that is not answered yet; do not re-scan the repository.
- Waiting for something (CI, a server) is fine: say so, wait with a sleep, and stop after a few tries.

## When you are stuck
- After 3 calls in a row that brought nothing new, stop using tools.
- Then summarise the confirmed facts, what you tried, the current blocker and one different next step.
- "All required tests pass" ends the task: do not go on searching.
- A note marked [sandbox-ai loop guard] in a tool result or an answer comes from the sandbox: follow it.

## Quality
- Check exit status, the relevant tests and the diff; claim nothing you have not seen work.
- Never repeat commands with side effects (git commit, push, install, delete, migrate, deploy).
- Do not widen the task silently.
"""
# The loop guard's progress_notes layer (LOOP_DEFAULTS): the local-model research's round 5 had Ornith-1.5-35B in
# Claude Code finish its forced-compaction task only with this line (runs/c11_compact-notes__claude__orn35__m1).
PROGRESS_TEXT = """
## Long tasks
- Keep PROGRESS.md in the working folder: after every finished step write what is done and what is next.
- After an interruption or a compaction of the conversation, read PROGRESS.md first and go on from there."""


def loop_text(loop):
    """The loop guard's instructions for LOOP (loop_config): LOOP_TEXT, with the progress_notes line when it is on."""
    return LOOP_TEXT + PROGRESS_TEXT if (loop or {}).get("progress_notes") else LOOP_TEXT


def parse_models(value):
    """(name, [models]) of --llm NAME[:MODEL[,MODEL...]]; a model may contain ':' itself (qwen2.5-coder:7b), never
    ','. The first model is the agent's; OpenCode offers exactly the ones named (its model allow-list)."""
    name, sep, models = (value or "").partition(":")
    listed = models.split(",") if sep else []
    if not NAME.fullmatch(name) or (sep and not all(MODEL_NAME.fullmatch(model) for model in listed)) \
            or len(listed) > 50:
        raise ValueError(f"--llm expects NAME[:MODEL[,MODEL...]], an endpoint of sandbox-ai llm list and model "
                         f"names: {printable(value)!r}")
    return name, list(dict.fromkeys(listed))


def parse_choice(value):
    """(name, model or None) of --llm NAME[:MODEL[,MODEL...]]: the first model, the one the agent starts with."""
    name, models = parse_models(value)
    return name, models[0] if models else None


def selected_models(a):
    """The models --llm names ([] when it names none: the endpoint's model then, and the ones it listed)."""
    return parse_models(a.llm)[1]


def provider_id(endpoint):
    """The provider's id in the agents' configurations: never one of their built-in providers (OpenCode 2 has
    ollama, lmstudio and vllm, Codex has ollama and lmstudio)."""
    return "llm-" + endpoint["name"]


def selection(a, path=None):
    """(endpoint, model) of --llm, read once per parsed argument set; ValueError says why there is none."""
    cached = getattr(a, "_llm", None)
    if cached is not None and cached[0] == a.llm:
        return cached[1]
    name, model = parse_choice(a.llm)
    found = endpoint(name, path)
    case = getattr(a, "use_case", None)
    report = None
    if case:  # --use-case: picks the model when --llm names none (yours wins), its settings go over the profile
        model, report = use_case_start(a, found, model)
    model = model or found["model"] or (found["models"][0] if len(found["models"]) == 1 else None)
    if report is not None and not report["picked"] and not report["named"] and model:
        # The server's default is not a choice for the use case: one it marks unsafe is never started on its own.
        marked = usecases_module().unsafe_for(usecases_module().use_case(usecase_table(), report["id"]), model)
        if marked is not None:
            raise ValueError(f"--llm {name}: the use case {report['id']} marks {model} unsafe ({marked['why']}; "
                             f"{marked['evidence']}): choose another model, or name it yourself to use it anyway: "
                             f"--llm {name}:{model}")
        report["note"] = "; ".join(filter(None, [report["note"], f"the server's default model {model} is used"]))
    if not model:
        listed = f"; listed: {', '.join(found['models'][:10])}" if found["models"] else \
            f"; see: sandbox-ai llm models {name}"
        raise ValueError(f"--llm {name}: name a model as --llm {name}:MODEL (or add the endpoint with --model)"
                         + listed)
    if getattr(a, "agent", None) and not getattr(a, "no_llm_profile", False):
        # The model profile of this agent and model applies to this start (model_profile): the mark, on a copy; with
        # --use-case the use case's settings for the pair too.
        found = {**found, "llm_profile": {"agent": a.agent, "model": model, **({"use_case": report["id"]}
                                                                               if report else {})}}
    if report is not None:
        report["model"] = model
        a._llm_use_case = report
    a._llm = (a.llm, (found, model))
    return found, model


# --- model and harness per use case (model_usecases.py, model_usecases.json) ---------------------------------------
#
# Per use case the local-model research's ranked choices: model pattern, harness, settings over the model's profile,
# why, known failures, evidence (model_usecases.json; round 5 fills it in). `llm recommend` picks from the models the
# servers have now - what is loaded, what fits their memory (llm set --memory; a server on this host's loopback: this
# host's) - the best tested choice per use case, else the nearest. A start with --use-case NAME lets it pick the model
# of --llm's server for the start's agent (a model --llm names wins) and puts the use case's settings for that model
# in that agent over the model's profile (pair_profile): the profile < the use case < the endpoint's values (llm set)
# < --llm-param < explicit options; --no-llm-profile drops both, the choice stays.
_usecases_module = None


def usecases_module():
    """model_usecases.py, loaded once."""
    global _usecases_module
    with LOADING:
        if _usecases_module is None:
            spec = importlib.util.spec_from_file_location("sandbox_model_usecases_llm", HERE / "model_usecases.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _usecases_module = module
    return _usecases_module


def usecase_table():
    """The use cases (model_usecases.json), checked with this module's rules; ValueError when they cannot be used."""
    try:
        return usecases_module().load(None, profile_param, loop_setting)
    except OSError as exc:
        raise ValueError(f"the model use cases (model_usecases.json beside sandbox-ai) cannot be read: "
                         f"{printable(exc)}") from None


def host_memory():
    """This host's memory in bytes (what a server on its loopback has), or None."""
    try:
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        if pages > 0 and size > 0:
            return pages * size
    except (ValueError, OSError, AttributeError):
        pass
    if sys.platform == "darwin":
        import subprocess
        try:
            return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True,
                                      timeout=5).stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return None


def memory_of(endpoint):
    """(bytes, where from) of ENDPOINT's memory: yours (llm set --memory GB: "llm set"), else this host's for a server
    on its loopback ("this host"), else (None, None): not known (the fit check says how to tell it)."""
    value = memory_value(endpoint.get("memory_gb")) if endpoint.get("memory_gb") is not None else None
    if value:
        return int(value * 1e9), "llm set"
    if (endpoint.get("parsed") or {}).get("loopback"):
        found = host_memory()
        if found:
            return found, "this host"
    return None, None


def usecase_server(endpoint, models, answered=True, error=None):
    """ENDPOINT with MODELS (`llm models`' entries, or names) as model_usecases.recommend takes a server."""
    return {"name": endpoint["name"], "memory_bytes": memory_of(endpoint)[0], "answered": answered, "error": error,
            "models": [m for m in models if not isinstance(m, dict) or chat_model(m)]}


def server_listing(endpoint, get=None, timeout=STATUS_TIMEOUT):
    """(models, answered, error) of ENDPOINT now - asked through its VPN as `llm models` asks, and remembered -, or
    its remembered names when it does not answer (answered False, why)."""
    try:
        with through_vpn(endpoint) as tunnel:
            client = VPNClient(endpoint, tunnel, get, timeout=timeout) if tunnel else \
                Client(endpoint, get, timeout=timeout)
            return endpoint_models(endpoint, get, client=client), True, None
    except Exception as exc:  # noqa: BLE001 - not answered, whatever the reason: the remembered names
        LOG.info("llm", f"the models of {endpoint.get('name')} were not listed: the remembered ones count", exc)
        return list(endpoint.get("models") or []), False, printable(str(exc) or type(exc).__name__)[:300]


def use_case_models(a, found):
    """The models of --llm's server for --use-case: what it lists now (sizes, what is loaded, what reads images; kept
    for the start as server_models keeps them), else the remembered names (a dry run asks nothing, nor a start whose
    server is behind a VPN or with SANDBOX_AI_LLM_NO_CONTEXT_FETCH=1)."""
    if found.get("vpn") or getattr(a, "vpn", None) or getattr(a, "dry_run", False) or \
            os.environ.get("SANDBOX_AI_LLM_NO_CONTEXT_FETCH") == "1":
        return list(found.get("models") or []), "remembered"
    try:
        models = endpoint_models(found, client=Client(found, timeout=CONTEXT_TIMEOUT))
    except Exception as exc:  # noqa: BLE001 - the remembered names then
        LOG.info("llm", f"the models of {found.get('name')} were not listed for --use-case: the remembered ones", exc)
        return list(found.get("models") or []), "remembered"
    contexts = {**{name: value for name, value in (found.get("contexts") or {}).items() if context_value(value)},
                **{m["name"]: m["context"] for m in models if context_value(m.get("context"))}}
    try:  # what server_models would ask again: the same listing
        a._llm_server_models = (found["name"], ([m["name"] for m in models if chat_model(m)][:OFFER_LIMIT], contexts,
                                                "server"))
    except AttributeError:
        pass
    return models, "server"


def use_case_start(a, found, named):
    """(model, report) of --use-case at a start with --llm's server FOUND: NAMED (the model --llm names) stays,
    with the use case's settings where it has them for that model in the start's agent; else the use case picks
    (model_usecases.recommend for the agent: its own harness first, what fits). REPORT {id, label, status, picked,
    named, choice, settings, note, listing, unknown}; ValueError for a use case the table does not have."""
    module = usecases_module()
    table = usecase_table()
    entry = module.use_case(table, a.use_case)
    agent = getattr(a, "agent", None)
    report = {"id": entry["id"], "label": entry["label"], "status": module.status(entry), "picked": False,
              "named": bool(named), "choice": None, "settings": {}, "note": "", "listing": None,
              "unknown": list(entry.get("unknown") or []), "unsafe": None, "unsafe_pairs": []}
    if named:
        marked = module.unsafe_for(entry, named)
        if marked is not None:  # yours: it starts (you decide), said with why
            report["unsafe"] = marked["why"]
        found_rec = module.settings_for(table, entry["id"], named, agent) if agent else None
        if found_rec is not None:
            report["settings"] = module._settings(found_rec)
            report["note"] = f"{named} as you named it; the use case's settings for it in " \
                             f"{module.harness_name(agent)} apply (rank {found_rec['rank']})"
        else:
            report["note"] = f"{named} as you named it; the use case's settings were not measured for this model in " \
                             f"{module.harness_name(agent)}: the model's profile applies"
        return named, report
    models, report["listing"] = use_case_models(a, found)
    result = module.recommend(table, [usecase_server(found, models)], entry["id"], agent, profile_table())
    choice = result["choice"]
    report["unsafe_pairs"] = [{"model": pair["model"], "why": pair["unsafe"]} for pair in result.get("pairs") or []
                              if pair.get("unsafe")]
    if choice is None:
        report["note"] = f"nothing of the use case's table is on {found['name']}, and no model there to go by"
        if report["unsafe_pairs"]:
            report["note"] += " but those it marks unsafe (" + ", ".join(
                pair["model"] for pair in report["unsafe_pairs"]) + ")"
        return None, report
    report.update(picked=True, choice=choice)
    if choice["source"] == "table" and choice["measured_in"] == agent:
        report["settings"] = dict(choice["settings"])
    report["note"] = choice["note"]
    if report["listing"] == "remembered":
        report["note"] = "; ".join(filter(None, [report["note"], "chosen from the remembered models (the server was "
                                                 "not asked: what is loaded and what fits not known)"]))
    return choice["model"], report


def pinned_argv(a, argv):
    """ARGV of a start whose --use-case picked the model of --llm NAME, with that model named (--llm NAME:MODEL): a
    session keeps the options it was started with, and its later runs (the supervisor's, a restart, a resume) then
    work with the same model, whatever the server has loaded or lists by then. ARGV as it is otherwise."""
    report = getattr(a, "_llm_use_case", None)
    if not report or not report.get("picked") or not report.get("model"):
        return list(argv)
    name, model = parse_choice(a.llm)
    if model:
        return list(argv)
    pinned, found, index = [], False, 0
    words = list(argv)
    while index < len(words):
        word = words[index]
        if word == "--":
            pinned += words[index:]
            break
        if word in ("--llm", "-lm") and index + 1 < len(words) and words[index + 1] == name:
            pinned += [word, f"{name}:{report['model']}"]
            index += 2
            found = True
            continue
        if word in (f"--llm={name}", f"-lm={name}"):
            pinned.append(f"{word}:{report['model']}")
            found = True
        else:
            pinned.append(word)
        index += 1
    return pinned if found else list(argv)


def use_case_lines(report, name):
    """The start's lines of --use-case's REPORT (use_case_start) for the endpoint NAME: what it chose and why, its
    note, and that the use case is not tested where it is not."""
    if not report:
        return []
    head = f"agent-sandbox: --llm {name}: use case {report['id']} ({report['label']}, {report['status']})"
    choice = report["choice"]
    lines = []
    if choice is not None:
        lines.append(f"{head}: {usecases_module().choice_line(choice)}: {choice['why']}")
    else:
        lines.append(f"{head}: {report.get('model') or 'no model'}")
    if report.get("unsafe"):
        lines.append(f"agent-sandbox: warning: --llm {name}: use case {report['id']}: {report.get('model')} is "
                     f"marked unsafe for it ({report['unsafe']}): started because you named it")
    if report["note"]:
        lines.append(f"agent-sandbox: note: --llm {name}: use case {report['id']}: {report['note']}")
    if report["status"] == "not tested":
        lines.append(f"agent-sandbox: note: --llm {name}: use case {report['id']} is not tested yet"
                     + (f" ({report['unknown'][0]})" if report["unknown"] else ""))
    return [printable(line) for line in lines]


def load_hosts_module():
    spec = importlib.util.spec_from_file_location("sandbox_hosts_llm", HERE / "hosts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def without_option(argv, option):
    """ARGV without OPTION and its value (OPTION VALUE or OPTION=VALUE)."""
    found, index = [], 0
    while index < len(argv):
        if argv[index] == option:
            index += 2
            continue
        if not argv[index].startswith(option + "="):
            found.append(argv[index])
        index += 1
    return found


def recommend(args, argv, out=print, get=None):
    """sandbox-ai llm recommend [--use-case NAME] [--server NAME] [--agent AGENT] [--host H] [--json]: from the models
    the servers have now (llm models: what is loaded, what fits their memory) the best tested choice per use case of
    the local-model research, else the nearest, with why; --host H: on that host of sandbox-ai hosts (over SSH)."""
    if args.host and args.host != "local":
        status, stdout, stderr = load_hosts_module().run(args.host, ["llm", "recommend",
                                                                     *without_option(argv[1:], "--host")],
                                                         timeout=120)
        if stdout:
            out(stdout.rstrip("\n"))
        if stderr:
            print(stderr.rstrip("\n"), file=sys.stderr)
        return status
    module = usecases_module()
    table = usecase_table()
    word = getattr(args, "use_case_word", None)
    if word:
        if args.use_case and module.use_case(table, args.use_case)["id"] != module.use_case(table, word)["id"]:
            raise ValueError(f"two use cases: {word} and --use-case {args.use_case} (name one)")
        args.use_case = args.use_case or word
    if args.use_case:
        module.use_case(table, args.use_case)  # an unknown one is refused before any server is asked
    store = load_store()
    names = [args.server] if args.server else sorted(store["endpoints"])
    found_ones = []
    for name in names:
        try:
            found_ones.append(endpoint(name))
        except ValueError as exc:
            if args.server:
                raise
            print(printable(f"sandbox-ai llm: {name}: unusable ({exc})"), file=sys.stderr)
    # Asked at once (a server that does not answer costs its time limit; one after the other they added up) - those
    # behind a VPN one after the other, each through its own tunnel as `llm models` asks.
    import concurrent.futures
    direct = [found for found in found_ones if not found.get("vpn")]
    listings = {}
    if direct:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(direct))) as pool:
            listings.update(zip((found["name"] for found in direct),
                                pool.map(lambda found: server_listing(found, get), direct)))
    servers = []
    for found in found_ones:
        models, answered, error = listings.get(found["name"]) or server_listing(found, get)
        servers.append(usecase_server(found, models, answered, error))
    profiles = profile_table()
    if args.use_case:
        found = module.recommend(table, servers, args.use_case, args.agent, profiles)
        for line in [json.dumps(found, indent=2)] if args.json else module.lines(found):
            out(printable(line) if not args.json else line)
    else:
        every = module.recommend_all(table, servers, args.agent, profiles)
        if args.json:
            out(json.dumps({"schema": table["schema"], "use_cases": every}, indent=2))
        else:
            for line in module.table_lines(every):
                out(printable(line))
            out("Why, the evidence and the settings: sandbox-ai llm recommend --use-case NAME")
    if not servers and not args.json:
        out("no LLM servers: the table's choices only (add one with: sandbox-ai llm add NAME --kind KIND --url URL)")
    for server in [] if args.json or args.use_case else servers:
        if not server["answered"]:
            out(printable(f"{server['name']} did not answer ({server['error']}): its remembered models were used"))
    return 0


# --- the server's other models in the agent (--llm-models) ----------------------------------------------------------
#
# The model of --llm NAME:MODEL is the agent's default; the server's other models can be chosen in the agent where it
# switches models (checked in each agent's pinned source, November 2026):
#   OpenCode    its provider's "models" (each with limit.context/output): /models
#   Qwen Code   0.26.4: modelProviders.openai, one entry per model (id + baseUrl): /model, /model ID
#   Hermes      0.22.5: /model lists a custom endpoint's models itself (its GET /v1/models through the proxy)
#   Goose       1.57.0: /model ID switches the model of the session at the same provider (any ID)
#   DSH         0.2.1-rc.5: a route's "models" list is what its web UI's model selection offers (Settings > Models)
#   Claude Code 2.1.322: /model ID takes any ID after a one-token request to the server; its picker shows the
#               ANTHROPIC_DEFAULT_*_MODEL slots only (all on the start's model: its background work stays there)
#   Codex       0.169: /model offers only the models of its catalog, and a catalog of other models must carry Codex's
#               whole system prompt per model (model_catalog_json: base_instructions); so a profile per model instead,
#               chosen when Codex starts (codex --profile sandbox-ai-llm-SLUG; codex resume ... continues a session)
# The relay takes requests for any model of that server (never another server), applying each model's parameters;
# --llm-models MODEL,... limits the models offered, and the relay refuses the others.
OFFER_LIMIT = 500          # models offered at most (as many as `llm models` remembers)
CODEX_PROFILES = 100       # per-model Codex profiles written at most


def parse_offer(value, where="--llm-models"):
    """None (all: every model the server lists) or [models] of --llm-models all|MODEL[,MODEL...]."""
    text = "all" if value is None else str(value).strip()
    if text == "all":
        return None
    listed = text.split(",")
    if not all(MODEL_NAME.fullmatch(model) for model in listed) or len(listed) > OFFER_LIMIT:
        raise ValueError(f"{where} expects all or MODEL[,MODEL...] (model names as sandbox-ai llm models NAME shows "
                         f"them): {printable(value)!r}")
    return list(dict.fromkeys(listed))


def chat_model(model):
    """Whether a listed model can chat (an embedding model cannot: never offered to an agent)."""
    name = model.get("name", "").lower()
    families = [str(family).lower() for family in model.get("families") or []]
    return not ("embed" in name or "embedding" in (model.get("capabilities") or [])
                or (families and all("bert" in family for family in families)))


def server_models(a, found):
    """([models], {model: context window}, source) of the server FOUND for this start, read once: what it lists now
    (asked at the start, CONTEXT_TIMEOUT; remembered for later starts as `llm models` does), else what was
    remembered ("remembered"). Not asked from this host for a server behind a VPN (or with
    SANDBOX_AI_LLM_NO_CONTEXT_FETCH=1), nor by a dry run (no DNS, no key read, no request: the app's previews run
    one for every change; what `llm add` and `llm models` remembered is offered then). Embedding models are left
    out. Never raises."""
    cached = getattr(a, "_llm_server_models", None)
    if cached is not None and cached[0] == found["name"]:
        return cached[1]
    names, source = list(found.get("models") or []), "remembered"
    contexts = {name: value for name, value in (found.get("contexts") or {}).items() if context_value(value)}
    if not (found.get("vpn") or getattr(a, "vpn", None) or getattr(a, "dry_run", False)
            or os.environ.get("SANDBOX_AI_LLM_NO_CONTEXT_FETCH") == "1"):
        try:
            models = endpoint_models(found, client=Client(found, timeout=CONTEXT_TIMEOUT))
            names = [m["name"] for m in models if chat_model(m)]
            contexts.update({m["name"]: m["context"] for m in models if context_value(m.get("context"))})
            source = "server"
            try:
                a._llm_server_vision = (found["name"], set(reads_images(models)))
            except AttributeError:
                pass
        except Exception as exc:  # noqa: BLE001 - the remembered list then
            LOG.info("llm", f"the models of {found.get('name')} were not listed at the start: the remembered ones "
                            "are offered", exc)
    result = (names[:OFFER_LIMIT], contexts, source)
    try:
        a._llm_server_models = (found["name"], result)
    except AttributeError:
        pass
    return result


def vision_models(a, found):
    """{model} of the server FOUND that read images: what its listing said at this start (server_models), else what
    was remembered with its models."""
    server_models(a, found)
    seen = getattr(a, "_llm_server_vision", None)
    if seen is not None and seen[0] == found["name"]:
        return set(seen[1])
    return set(found.get("vision") or [])


def offered_models(a):
    """([models], limited) the agent offers for --llm, the start's model first. --llm-models MODEL,...: those
    (limited: the relay refuses requests for any other); --llm NAME:MODEL,MODEL...: those, as before (limited too);
    else (--llm-models all, the default) every model the server lists (server_models)."""
    found, model = selection(a)
    offer = parse_offer(getattr(a, "llm_models", None))
    named = selected_models(a)
    if offer is not None:
        return list(dict.fromkeys([model, *offer]))[:OFFER_LIMIT], True
    if len(named) > 1:
        return named, True
    return list(dict.fromkeys([model, *server_models(a, found)[0]]))[:OFFER_LIMIT], False


# --- Claude Code's tools for a local model (--llm-tools) ---------------------------------------------------------
#
# Claude Code 2.1.325 sends 21 tools with every request, 58,660 characters of JSON (~10k tokens; the local-model
# research's round 2). In every Claude Code run of rounds 1-3 the local models called Bash (1,977 times), Read (558),
# Edit (195), Write (27), the MCP servers' tools and Claude Code's MCP resource tools - never Agent, Cron*,
# EnterWorktree/ExitWorktree, ListAgents, Monitor, NotebookEdit, ReportFindings, ScheduleWakeup, SendMessage, Skill,
# TaskStop, WebFetch, WebSearch or Workflow; and Workflow's schema (a property without a type) makes Seed-OSS-36B's
# chat template refuse every request (round 3). So the connection proxy leaves out of Claude Code's requests to the
# --llm server (runtime.llm_hide_tools) every tool but these and the MCP servers' (mcp__SERVER__TOOL): "core", the
# default. --llm-tools all keeps every tool; core,TOOL,... adds some; TOOL,TOOL,... names the built-in ones to keep
# exactly (the MCP tools stay always). A model profile may say the same ("tools"); the start's option wins. A call of
# a tool that was left out reaches Claude Code under a name it does not know, so that it answers with an error, which
# the proxy then gives sandbox-ai's own words (runtime.llm_hidden_results).
CLAUDE_CORE_TOOLS = ("Bash", "Read", "Edit", "Write", "Glob", "Grep", "TodoWrite", "ListMcpResourcesTool",
                     "ReadMcpResourceTool", "ReadMcpResourceDirTool")
TOOL_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}")
TOOLS_MAX = 64


def parse_tools(value, where="--llm-tools"):
    """None (all: every tool as Claude Code sends it), "core" (the default: VALUE None) or [names] ("core" among them
    adds the core tools) of --llm-tools all|core|TOOL[,TOOL...] or a profile's "tools" (that text, or a list)."""
    if value is None:
        return "core"
    listed = [str(item).strip() for item in value] if isinstance(value, list) else str(value).split(",")
    listed = [item.strip() for item in listed]
    if listed == ["all"]:
        return None
    if listed == ["core"]:
        return "core"
    if not listed or len(listed) > TOOLS_MAX or "all" in listed or \
            not all(item == "core" or TOOL_NAME.fullmatch(item) for item in listed):
        raise ValueError(f"{where} expects all, core or TOOL[,TOOL...] (Claude Code's tool names, such as "
                         f"core,WebFetch or Bash,Read,Edit; at most {TOOLS_MAX}): {printable(value)!r}")
    return list(dict.fromkeys(listed))


def kept_tools(tools):
    """The names of Claude Code's own tools the proxy keeps for TOOLS (parse_tools'), None for all of them."""
    if tools is None:
        return None
    if tools == "core":
        return list(CLAUDE_CORE_TOOLS)
    found = []
    for name in tools:
        found += list(CLAUDE_CORE_TOOLS) if name == "core" else [name]
    return list(dict.fromkeys(found))


def start_tools(a):
    """(parse_tools' value, where it comes from) for this start: --llm-tools ("--llm-tools"), else the model
    profile's "tools" of the model in Claude Code ("profile"; not with --no-llm-profile), else core ("default")."""
    given = getattr(a, "llm_tools", None)
    if given is not None:
        return parse_tools(given), "--llm-tools"
    found, model = selection(a)
    marked = found.get("llm_profile")
    profile = pair_profile(model, "claude", marked.get("use_case"), profile_table()) if marked else None
    if profile is not None and profile.get("tools") is not None:
        return parse_tools(profile["tools"], f"the model profile {profile['id']}: tools"), "profile"
    return "core", "default"


def runs_claude(a):
    """Whether Claude Code runs in this start: its agent or one --with."""
    return "claude" in [getattr(a, "agent", None), *(getattr(a, "companions", None) or [])]


def tools_text(tools):
    """parse_tools' value in words: all, core, core,WebFetch."""
    return "all" if tools is None else tools if isinstance(tools, str) else ",".join(tools)


def model_limits(a, found, names):
    """({model: context window}, {model: max_tokens}) of the offered NAMES that are known: each model's num_ctx (the
    start's --llm-param first), else what the server lists for it (or was remembered); max_tokens the ceiling of
    one answer. The start's model is told what agent_context says (the window it is loaded with first)."""
    override = start_params(a, found)[0]
    reported = server_models(a, found)[1]
    contexts, outputs = {}, {}
    for name in names:
        values = effective_params(found, name, override)[0]
        contexts[name] = context_value(told_context(found, name, override, context_value(reported.get(name)))[0])
        if values.get("max_tokens"):
            outputs[name] = values["max_tokens"]
    return served_limits(a, found, selection(a)[1], contexts), outputs


def profile_slug(model, taken=()):
    """A Codex profile name's part for MODEL (Codex takes letters, digits, - and _ only), not among TAKEN."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", model).strip("-")[:60] or "model"
    found, n = slug, 2
    while found in taken:
        found, n = f"{slug}-{n}", n + 1
    return found


SWITCHING = {
    "opencode": "in OpenCode with /models ({model} stays the default)",
    "opencode2": "in OpenCode 2 with /models ({model} stays the default)",
    "qwen": "in Qwen Code with /model",
    "hermes": "in Hermes with /model (it lists the server's models itself)",
    "goose": "in Goose with /model MODEL-ID (its context limit stays the one of {model})",
    "dsh": "in DSH's web UI (Settings > Models)",
    "claude": "in Claude Code with /model MODEL-ID (its picker shows {model} only; Claude Code checks a typed ID with "
              "a one-token request; its context window and background work stay with {model})",
    "codex": "in Codex only when it starts: Codex's /model offers its own catalog only (a catalog of other models "
             "must carry Codex's whole system prompt per model), so each model has a profile: in the sandbox "
             "codex --profile {profile} (codex resume --profile {profile} --last goes on with the conversation)",
}


# The agents that are given only the start's model (their configuration names that one: pi_llm, letta_model,
# nanobot_config, openhands_llm), whatever --llm-models says: SWITCHING has no way for them yet.
ONE_MODEL = tuple(agent for agent in AGENTS if agent not in SWITCHING)


def switch_note(a):
    """The start's note on the server's other models: how many the agent offers and how it switches to them, that a
    model the server has not loaded waits for its loading, and what keeps which model loaded. None with one model.
    An agent whose switching is not mapped yet (ONE_MODEL: the agents of 0.1.97 but OpenCode 2) is given the start's
    model only: said when --llm-models names others."""
    if a.agent not in SWITCHING:
        offer = parse_offer(getattr(a, "llm_models", None))
        found, model = selection(a)
        if offer is not None and any(name != model for name in offer):
            title = {"pi": "Pi", "letta": "Letta Code", "nanobot": "nanobot", "openhands": "OpenHands"}.get(
                a.agent, a.agent)
            return (f"--llm-models: {title} is given only the start's model {model} (sandbox-ai sets no other model "
                    f"of {found['name']} in it yet); another one: --llm {found['name']}:MODEL")
        return None
    found, model = selection(a)
    names, limited = offered_models(a)
    if len(names) < 2:
        return None
    other = next(name for name in names if name != model)
    profile = f"{CODEX_PROFILE}-{profile_slug(other)}"
    how = SWITCHING.get(a.agent, "").format(model=model, profile=profile)
    which = "named with --llm-models" if limited and parse_offer(getattr(a, "llm_models", None)) is not None else \
        "named with --llm" if limited else \
        ("the server lists" if server_models(a, found)[2] == "server" else "remembered for it (sandbox-ai llm models "
                                                                          f"{found['name']})")
    text = f"--llm {found['name']}: {model} and {len(names) - 1} other model{'s' if len(names) > 2 else ''} " \
           f"({which}{'' if limited else '; --llm-models MODEL,... limits them'}) can be chosen {how}"
    if limited:
        text += "; the proxy refuses requests for any other model of the server"
    if found["kind"] in ("lmstudio", "ollama", "llamacpp"):
        text += ". A model the server has not loaded is loaded by the first request to it, which waits until it is " \
                "(LM Studio: JIT loading; Ollama: on request; llama.cpp: in router mode)"
        if found["kind"] == "lmstudio" and found.get("evict") != "allow":
            text += f"; evict never keeps {model} loaded for this start, the others are loaded JIT (LM Studio's " \
                    "Auto-Evict may unload another JIT-loaded model for them)"
        elif found["kind"] == "ollama":
            text += "; Ollama keeps them by OLLAMA_MAX_LOADED_MODELS and OLLAMA_KEEP_ALIVE"
    return text


def agent_url(endpoint, api="openai"):
    """The endpoint as the agent in the sandbox reaches it: the host's loopback at 127.0.0.1 (the address the
    proxy's redirect and pasta's forward cover), with /v1 for OpenAI's API, the root for Anthropic's."""
    parsed = endpoint["parsed"]
    host = "127.0.0.1" if parsed["loopback"] and parsed["scheme"] == "http" else parsed["hostform"]
    default = 443 if parsed["scheme"] == "https" else 80
    root = f"{parsed['scheme']}://{host}" + ("" if parsed["port"] == default else f":{parsed['port']}") \
        + parsed["prefix"]
    return root + "/v1" if api == "openai" else root


def network_options(a, loopback_reachable):
    """What --llm opens, as the launcher's own options: an address as --allow-tcp ADDRESS:PORT, the host's
    loopback as --allow-host-port PORT, a name on 443 as --allow-host NAME. A name on another port is pinned
    and opened on that port alone at the start (resolved_network). Errors are left to validate()."""
    try:
        found, _ = selection(a)
    except ValueError:
        return
    parsed = found["parsed"]
    if parsed["loopback"]:
        if loopback_reachable and str(parsed["port"]) not in a.allow_host_port:
            a.allow_host_port.append(str(parsed["port"]))
    elif parsed["address"]:
        target = f"{parsed['hostform']}:{parsed['port']}"
        if target not in a.allow_tcp:
            a.allow_tcp.append(target)
    elif parsed["port"] == 443 and parsed["host"] not in a.allow_host:
        a.allow_host.append(parsed["host"])
    rule = prefix_rule(a, found)
    if rule and rule not in a.allow_http:
        a.allow_http.append(rule)


def prefix_rule(a, endpoint):
    """The path prefix as a rule of the proxy (an --allow-http entry): only requests below it reach the endpoint's
    host. Rules name hosts, so an address gets none; they need the proxy."""
    parsed = endpoint["parsed"]
    if parsed["prefix"] and not parsed["address"] and "." in parsed["host"] and not a.no_proxy and not a.offline:
        return f"{parsed['host']} * {parsed['prefix']}/**"
    return None


def validate(a, proxy_enabled, loopback_reachable, within_ceiling):
    """Refuse what cannot work, before anything starts."""
    found, model = selection(a)
    parsed = found["parsed"]
    problem = agent_problem(found, a.agent)
    if problem:
        raise ValueError(f"--llm {found['name']}: {problem}")
    if a.offline:
        raise ValueError(f"--llm {found['name']} needs the network to its server; drop --offline")
    if parsed["loopback"] and not loopback_reachable:
        raise ValueError(f"--llm {found['name']}: {found['url']} is on this host's loopback, which only the Podman "
                         "backend with its default network reaches (--allow-host-port)")
    own = found["api_key_secret"] or found["headers"] or found["ca"]
    if own and not proxy_enabled:
        raise ValueError(f"--llm {found['name']}: its key, headers or CA are used by the connection proxy, so the "
                         "agent never sees them; drop --no-proxy")
    if any(host_matches(parsed["host"], pattern) for pattern in a.no_inspect) and own:
        raise ValueError(f"--llm {found['name']}: --no-inspect {parsed['host']} would keep the proxy from adding "
                         "the key")
    if a.agent == "opencode" and a.local_config:
        raise ValueError("--llm configures OpenCode itself; drop --local-config")
    options = a.agent_args[:a.agent_args.index("--")] if "--" in a.agent_args else a.agent_args
    if a.agent == "codex" and any(value in ("-p", "--profile") or value.startswith("--profile=") for value in options):
        raise ValueError(f"--llm selects Codex's profile {CODEX_PROFILE}; drop -p/--profile from the agent's arguments")
    taken = set(agent_environment(a, windows=False)) & (set(a.env) |
                                                         {value.partition("=")[0] for value in a.secret_env})
    if taken:
        raise ValueError(f"--llm sets {', '.join(sorted(taken))} for the agent; drop it from --env/--secret-env")
    secret_values(found)  # missing or malformed secrets stop the start here, before anything runs
    if parse_offer(getattr(a, "llm_models", None)) is not None and len(selected_models(a)) > 1:
        raise ValueError(f"--llm {printable(a.llm)} names its models, and so does --llm-models: name them in one "
                         "place (--llm NAME:MODEL --llm-models MODEL,MODEL...)")
    override, start = start_params(a, found)  # a parameter the kind does not take stops the start here
    tools, source = start_tools(a)  # an --llm-tools (or a profile's tools) that is no list of tools stops it here
    if runs_claude(a) and tools is not None:
        print(printable(f"agent-sandbox: note: --llm {found['name']}: Claude Code's tools that local models do not "
                        f"use are left out of its requests (--llm-tools {tools_text(tools)}"
                        f"{' from the model profile' if source == 'profile' else ''}: it keeps "
                        f"{', '.join(kept_tools(tools))} and every MCP tool; --llm-tools all keeps every tool)"),
              file=sys.stderr)
    elif getattr(a, "llm_tools", None) is not None and not runs_claude(a):
        print("agent-sandbox: note: --llm-tools chooses Claude Code's tools; this start runs no Claude Code",
              file=sys.stderr)
    values = effective_params(found, model, override)[0]
    loop = loop_config(found, start)
    guarded = loop["warn"] or loop["block"] or loop["failures"] or loop["text"]
    if (any(key != "num_ctx" for key in values) or guarded) and \
            (not proxy_enabled or any(host_matches(parsed["host"], pattern) for pattern in a.no_inspect)):
        print(f"agent-sandbox: note: --llm {found['name']}: the sampling parameters and the loop guard are applied by "
              "the connection proxy, which does not see this server's requests here (--no-proxy or --no-inspect): "
              "only num_ctx and max_tokens reach the agent's configuration", file=sys.stderr)
    hint = modelfile_hint(found, model, effective_params(found, model, override, profile=False)[0])
    if hint:
        print(printable(f"agent-sandbox: note: --llm {found['name']}: {hint}"), file=sys.stderr)
    if not parsed["address"] and not parsed["loopback"] and not within_ceiling(a, host=parsed["host"]):
        raise ValueError(f"--llm {found['name']}: {parsed['host']} is not among the hosts the administrator's "
                         "policy allows (allow_hosts)")
    if found["insecure_http"]:
        print(f"agent-sandbox: WARNING: --llm {found['name']} uses plain http to a server outside your local "
              "networks (--insecure-http): prompts, code and the key cross the network unencrypted",
              file=sys.stderr)
    note = switch_note(a)
    if note:
        print(printable(f"agent-sandbox: note: {note}"), file=sys.stderr)
    for line in use_case_lines(getattr(a, "_llm_use_case", None), found["name"]):
        print(line, file=sys.stderr)
    report = profile_report(a, found, model)
    for line in profile_lines(report, found["name"]) if report else []:
        print(line, file=sys.stderr)
    return found, model


def host_matches(host, pattern):
    pattern = pattern.lower()
    return host == pattern or (pattern.startswith("*.") and host.endswith(pattern[1:]))


def resolved_network(a, hosts, resolve_names):
    """At the start, after the launcher resolved its hosts: the endpoint's name on a port other than 443 is
    resolved too, pinned ({name: addresses}, returned for /etc/hosts) and opened on exactly that port (added to
    --allow-tcp). Plain http to a name is refused unless all its addresses are local. Sets a.llm_addresses."""
    found, _ = selection(a)
    parsed = found["parsed"]
    pins = {}
    if parsed["loopback"]:
        addresses = ["127.0.0.1"]
    elif parsed["address"]:
        addresses = [parsed["address"]]
    else:
        addresses = hosts.get(parsed["host"])
        if addresses is None:
            addresses = resolve_names([parsed["host"]])[parsed["host"]]
            pins[parsed["host"]] = addresses
        if parsed["port"] != 443 or parsed["host"] in pins:
            for address in addresses:
                target = f"[{address}]:{parsed['port']}" if ":" in address else f"{address}:{parsed['port']}"
                if target not in a.allow_tcp:
                    a.allow_tcp.append(target)
    problem = plain_http_problem(parsed, addresses)
    if problem and not found["insecure_http"]:
        raise ValueError(f"--llm {found['name']}: {problem}")
    a.llm_addresses = addresses
    return pins


def title_of(name):
    """The session title the relay answers OpenCode's title request with: NAME (the start's, as sandbox-ai list shows
    it) printable on one line, at most TITLE_LIMIT characters (OpenCode cuts a title there), or None."""
    text = "".join(c for c in str(name or "") if c.isprintable()).strip()[:TITLE_LIMIT].strip()
    return text or None


def proxy_entry(a, get=None, title=None):
    """proxy.json's "llm": where the proxy swaps the placeholder for the key (and adds the header secrets), and the
    endpoint's own CA. The values live only there, in the proxy's directory the agent cannot read. TITLE: the name
    the start runs under, OpenCode's session title (runtime.llm_title_request; none: OpenCode asks the model)."""
    found, model = selection(a)
    parsed = found["parsed"]
    key, headers = secret_values(found, get)
    title = title_of(title)
    return [{"name": found["name"], "scheme": parsed["scheme"], "host": parsed["host"], "port": parsed["port"],
             **({"title": title} if title else {}),
             "addresses": list(getattr(a, "llm_addresses", []) or ([parsed["address"]] if parsed["address"] else [])),
             "prefix": parsed["prefix"], "placeholder": placeholder_for(a, found), "key": key,
             "headers": [[name, value] for name, value in headers], "ca": ca_text(found),
             **proxy_params(a, found, model)}]


# OpenCode asks the model for a session title beside the first real request (OpenCode 1.19.5 with max_tokens 32000;
# 2.2.5 with its own prompt), and at LM Studio's parallel 1 the real request waits for it: Seed-OSS-36B thought for
# 462-544 s over a title (the local-model research's round 3). Neither has a setting for a fixed title (1.19.5's
# agent "title": a prompt, a model, disable - disabled, the title stays "New session - DATE"; 2.2.5: only a plugin's
# hook), so the relay answers that request itself with the start's name (runtime.llm_title_request): the session is
# named as sandbox-ai list names the start, and the model is not asked. Your own agent.title (a prompt of yours, a
# model, disable) is left alone: its request is not OpenCode's own and goes to the model as it came.
TITLE_LIMIT = 100


# OpenCode's cut of a tool's output (the loop guard's compaction layer): what is longer reaches the model as a
# preview and the path of a file in OpenCode's home. 50,000 bytes: a whole part of the files MCP server's read_file
# fits (24,000 characters since sandbox-ai-mcp's servers/files answers in parts of that size; as JSON ~25 KB for
# ASCII text, ~31 KB with escapes and UTF-8), and one result cannot cost tens of thousands of tokens: with 128 KiB
# (0.1.96 and 0.1.97, when the server's parts were 60,000 characters and the model saw 860 bytes of one at 50,000)
# a single `read` took a request from 9k to 45k tokens, 47 s before the first token (the local-model research's
# evidence/tool_output_limits.txt and runs/t2_debug__opencode-hf__qcn). 1000 lines: a shell command's long output
# stays cut.
OPENCODE_TOOL_OUTPUT = {"max_lines": 1000, "max_bytes": 50000}


def opencode_config(endpoint, model, only=False, allowed=None, contexts=None, outputs=None, loop=None, wait=None, prune=True,
                    instructions="/root/" + LOOP_INSTRUCTIONS, vision=None):
    """OpenCode's configuration (the v1 shape, which OpenCode 2 reads too): an OpenAI-compatible provider
    (@ai-sdk/openai-compatible, Chat Completions) with the endpoint's models, or exactly ALLOWED (the models
    --llm NAME:MODEL,... names: no other one is offered). ONLY (--no-provider-defaults): no other provider is
    offered. CONTEXTS ({model: tokens}): limit.context and limit.output of the models whose window is known;
    OUTPUTS ({model: tokens}, their max_tokens): limit.output at most that. LOOP (loop_config): its OpenCode layers -
    steps (agent.<name>.steps), compaction (compaction, tool_output), instructions (the loop guard's file). WAIT
    (agent_wait, seconds): the provider's headerTimeout and chunkTimeout (OpenCode's own: 5 minutes each), and no
    timeout of the whole request. PRUNE: compaction.prune (OpenCode 1's; OpenCode 2 ignores it with a warning, so its
    file leaves it out). INSTRUCTIONS: where the loop guard's file lies in the sandbox (OpenCode 1's HOME; OpenCode
    2's read-only /etc/opencode2). VISION ({model}): the models that read images - OpenCode takes a custom provider's
    model for text-only and refuses to pass an image on ("this model does not support image input") unless its
    modalities say image (round 5)."""
    pid = provider_id(endpoint)
    names = list(dict.fromkeys([model, *(allowed if allowed else endpoint.get("models", []))]))

    def entry(name):
        context = context_value((contexts or {}).get(name))
        made = {"name": name}
        if context:
            output = output_limit(context)
            ceiling = (outputs or {}).get(name)
            made["limit"] = {"context": context, "output": min(output, ceiling) if ceiling else output}
        if name in (vision or ()):
            made.update({"attachment": True, "modalities": {"input": ["text", "image"], "output": ["text"]}})
        return made
    config = {"$schema": "https://opencode.ai/config.json",
              "provider": {pid: {"npm": "@ai-sdk/openai-compatible",
                                 "name": f"{endpoint['name']} ({KINDS[endpoint['kind']]['label']})",
                                 "options": {"baseURL": agent_url(endpoint), "apiKey": PLACEHOLDER},
                                 "models": {name: entry(name) for name in names}}},
              "model": f"{pid}/{model}", "small_model": f"{pid}/{model}", "share": "disabled"}
    if wait:
        # OpenCode 1.19.5 (OpenCode 2 alike) wraps the provider's fetch: headerTimeout (response headers) and
        # chunkTimeout (between two chunks), 300000 ms each by default, timeout (the whole request; false: none). A
        # silent prefill ended with "Provider response headers timed out after 300000ms" and the request sent again.
        config["provider"][pid]["options"].update({"timeout": False, "headerTimeout": wait * 1000,
                                                   "chunkTimeout": wait * 1000})
    if only:
        config["enabled_providers"] = [pid]
    loop = loop or {}
    if loop.get("steps"):
        steps = loop["steps"]
        config["agent"] = {"build": {"steps": steps}, "plan": {"steps": max(3, steps * 45 // 100)},
                           "explore": {"steps": max(3, steps * 45 // 100)},
                           "general": {"steps": max(4, steps * 2 // 3)}}
    if loop.get("compaction"):
        context = context_value((contexts or {}).get(model))
        config["compaction"] = {"auto": True, **({"prune": True} if prune else {}),
                                **({"reserved": max(2048, context // 5)} if context else {})}
        config["tool_output"] = dict(OPENCODE_TOOL_OUTPUT)
    if loop.get("instructions"):
        config["instructions"] = [instructions]
    return config


# Claude Code puts the MCP servers' tools behind its ToolSearch tool (deferred: the model sees their names only once
# it searched); the local-model research's scripted model was offered 12 tools, none of the MCP servers'
# (runs/mock_claude), and a local model rarely thinks of searching. ENABLE_TOOL_SEARCH=false (2.1.322: its "standard"
# mode) sends every tool with each request. Yours (--env) wins.
CLAUDE_TOOL_SEARCH = "ENABLE_TOOL_SEARCH"


def claude_environment(endpoint, model, given=()):
    """Claude Code against an Anthropic-compatible server: the placeholder as ANTHROPIC_AUTH_TOKEN (a Bearer token,
    which all four servers accept; vLLM accepts no other), every model slot on MODEL, no traffic to Anthropic
    beyond what the network options allow anyway, and the MCP servers' tools offered as they are (no tool search,
    unless GIVEN, the names of your --env and --secret-env, has its variable)."""
    return {"ANTHROPIC_BASE_URL": agent_url(endpoint, "anthropic"), "ANTHROPIC_AUTH_TOKEN": PLACEHOLDER,
            # Empty: a key here would make Claude ask whether to use it (and send x-api-key).
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # The attribution block changes per request and defeats the servers' prompt caching.
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            **({} if CLAUDE_TOOL_SEARCH in given else {CLAUDE_TOOL_SEARCH: "false"}),
            # The model profile's variables (Nemotron: CLAUDE_CODE_DISABLE_THINKING), under yours.
            **{name: value for name, value in ((model_profile(endpoint, model) or {}).get("env") or {}).items()
               if name not in given}}


# Claude Code knows the context window of Anthropic's models only. For any other model ID it warns ('Model "M" isn't
# recognized · context assumed to be 1m tokens', 2.1.325; 200k tokens in 2.1.322) and compacts the conversation at
# that window - a local model's 4k-256k overflow long before: the server refuses the request or cuts the
# conversation. Its documented override (code.claude.com/docs/en/model-config, "Correct the window for a gateway or
# custom model ID"): CLAUDE_CODE_MAX_CONTEXT_TOKENS, taken on its own for an ID that does not start with claude- (one
# Claude Code knows keeps its own window, the variable is ignored there). CLAUDE_CODE_MAX_OUTPUT_TOKENS: the most
# tokens of one answer, reserved out of the window before compaction (an unknown ID's default is 32000).
# CLAUDE_CODE_AUTO_COMPACT_WINDOW: the window its auto-compaction works against (2.1.322 reads it, "such as 200k or
# 200000"); told no window, the local-model research's Claude Code 2.1.322 compacted at ~164k tokens of a model
# loaded with 131,072 (runs/growc_default), with either variable at ~88k (runs/growc_maxctx, growc_window). Both
# are set: the one window, whichever of them a Claude Code version reads.
CLAUDE_WINDOW = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
CLAUDE_COMPACT = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
CLAUDE_OUTPUT = "CLAUDE_CODE_MAX_OUTPUT_TOKENS"
CLAUDE_OWN = 25000   # tokens of the window Claude Code's own instructions and tool definitions take, about


CLAUDE_SMALL_WINDOW, CLAUDE_SMALL_OUTPUT = 65536, 8192


def claude_window_environment(a, found, model):
    """Claude Code's window variables for MODEL at FOUND: the context window it is told (the start's num_ctx, else
    what the server has the model loaded with or reports: agent_context) and the most tokens of one answer (the
    max_tokens ceiling, else a quarter of the window). Your own --env/--secret-env of either stays yours. With no
    window known a note says what Claude Code assumes and how to set it; a window that leaves Claude Code little
    room is warned about."""
    if "claude" in model.lower() or "[1m]" in model.lower():
        return {}  # a model Claude Code knows: its own window
    given = set(getattr(a, "env", None) or []) | {value.partition("=")[0]
                                                   for value in getattr(a, "secret_env", None) or []}
    if CLAUDE_WINDOW in given:
        return {}
    context = start_context(a, found, model)
    if not context:
        print(printable(
            f"agent-sandbox: note: --llm {found['name']}: Claude Code does not know the model {model} and assumes a "
            "context window of its own for it (it says: isn't recognized · context assumed to be ...); the server "
            "did not say its window, so Claude Code may compact too late and the server refuse or cut the "
            f"conversation. Set the window: sandbox-ai llm set {found['name']} --model {model} --param "
            "num_ctx=TOKENS (or --llm-param num_ctx=TOKENS for this start)"), file=sys.stderr)
        return {}
    if context < 2 * CLAUDE_OWN:
        print(printable(
            f"agent-sandbox: WARNING: --llm {found['name']}: {model} has a context window of {context} tokens; "
            f"Claude Code's own instructions and tools take about {CLAUDE_OWN} of it, which leaves little or nothing "
            "for the work: load the model with 64k tokens or more (LM Studio: Context Length; Ollama: "
            "OLLAMA_CONTEXT_LENGTH; llama.cpp: --ctx-size; vLLM: --max-model-len)"), file=sys.stderr)
    values = effective_params(found, model, start_params(a, found)[0])[0]
    # At most a quarter of the window, whatever max_tokens allows: Claude Code reserves the answer (up to 20k) out of
    # the window and compacts ~13k before the rest; half of a 32k-48k window left it 3-20k (round 5: "Autocompact is
    # thrashing" at 32k, compactions at ~17-20k of 48k).
    output = max(256, min(values["max_tokens"], context // 4)) if values.get("max_tokens") else output_limit(context)
    # Up to a 64k window at most 8192: Claude Code's own prompt (24-28k) refilled a 16384-limit 64k window within 3
    # turns of every compaction until its breaker ended the run (round 6, D13: 8192 + the PROGRESS.md line passed).
    if context <= CLAUDE_SMALL_WINDOW:
        output = min(output, CLAUDE_SMALL_OUTPUT)
    env = {CLAUDE_WINDOW: str(context)}
    if CLAUDE_COMPACT not in given:
        env[CLAUDE_COMPACT] = str(context)
    if CLAUDE_OUTPUT not in given:
        env[CLAUDE_OUTPUT] = str(output)
    return env


def claude_settings(document):
    """~/.claude/settings.json: WebFetch's domain check goes to api.anthropic.com; skipped, since the server here is
    another. Returns whether DOCUMENT changed."""
    if document.get("skipWebFetchPreflight") is True:
        return False
    document["skipWebFetchPreflight"] = True
    return True



def claude_state(document):
    """~/.claude.json: first-run setup done. Without a claude.ai login (none is copied for --llm) Claude would
    otherwise start with its setup screens, the login step included. Returns whether DOCUMENT changed."""
    changed = False
    if document.get("hasCompletedOnboarding") is not True:
        document["hasCompletedOnboarding"] = changed = True
    if not isinstance(document.get("theme"), str) or not document["theme"]:
        document["theme"], changed = "dark", True
    return changed


# Codex (0.134 or later) layers $CODEX_HOME/NAME.config.toml over config.toml when started with --profile NAME:
# a file of our own, so the user's config.toml (and the workspaces' trust in it) stays as it is.
CODEX_PROFILE = "sandbox-ai-llm"


def toml_string(value):
    # JSON's escapes are TOML's for the ASCII text written here (names, URLs, model ids).
    return json.dumps(value)


def codex_profiles(models):
    """{model: profile name} of the per-model Codex profiles (sandbox-ai-llm-SLUG) of MODELS."""
    found = {}
    for model in models:
        found[model] = f"{CODEX_PROFILE}-{profile_slug(model, {p[len(CODEX_PROFILE) + 1:] for p in found.values()})}"
    return found


def codex_profile(endpoint, model, context=None, wait=None, profile=CODEX_PROFILE):
    """~/.codex/PROFILE.config.toml: a provider with the Responses API (Codex has no other since 0.95.0),
    the key from an environment variable that holds only the placeholder, and none of Codex's own calls home
    (update check, analytics, metrics, plugin sync, the hosted web search a local server does not have). CONTEXT:
    the model's context window as the server reports it (model_context_window), when known. WAIT
    (stream_idle_timeout, seconds): the provider's stream_idle_timeout_ms."""
    pid = provider_id(endpoint)
    context = context_value(context)
    return "\n".join([
        f"# Written by sandbox-ai for --llm {endpoint['name']} at every start (codex --profile {profile}).",
        f"model_provider = {toml_string(pid)}",
        f"model = {toml_string(model)}",
        *([f"model_context_window = {context}"] if context else []),
        "check_for_update_on_startup = false",
        'web_search = "disabled"',
        "",
        f"[model_providers.{pid}]",
        f"name = {toml_string(endpoint['name'] + ' (' + KINDS[endpoint['kind']]['label'] + ')')}",
        f"base_url = {toml_string(agent_url(endpoint))}",
        f"env_key = {toml_string(KEY_VARIABLE)}",
        'wire_api = "responses"',
        *([f"stream_idle_timeout_ms = {int(wait) * 1000}"] if wait else []),
        "",
        "[analytics]",
        "enabled = false",
        "",
        "[otel]",
        'metrics_exporter = "none"',
        "",
        "[features]",
        "plugins = false",
        ""])


def placeholder_for(a, endpoint):
    """The placeholder the agent sends instead of ENDPOINT's key (the proxy swaps exactly it): for Hermes at
    OpenRouter one with OpenRouter's key prefix, which Hermes requires (hermes.OPENROUTER_PLACEHOLDER)."""
    if a.agent == "hermes" and endpoint["parsed"]["host"] == HERMES_OPENROUTER_HOST:
        return HERMES_OPENROUTER_PLACEHOLDER
    return PLACEHOLDER


# hermes.py's values (kept here as well: this module loads no sibling); a test keeps both the same.
HERMES_OPENROUTER_HOST = "openrouter.ai"
HERMES_OPENROUTER_PLACEHOLDER = "sk-or-v1-sandbox-ai-placeholder"
HERMES_CONFIG = "/etc/hermes/config.yaml"
# Hermes refuses a smaller context window (agent_init._enforce_minimum_context: "below the minimum 64,000 required by
# Hermes Agent"), and its user sees only "Something went wrong and I couldn't finish this reply".
HERMES_MIN_CONTEXT = 64000


def hermes_config(a):
    """Hermes' model section for --llm (laid into its managed configuration by hermes.llm_config's rules): the
    provider, the URL with /v1, the model, its context window when known, the loop guard's layers."""
    found, model = selection(a)
    behind = bool(getattr(a, "vpn", None))
    note = keep_loaded(found, model, a, behind_vpn=behind)
    if note:
        print(printable(f"agent-sandbox: {'WARNING: ' if note.startswith('WARNING: ') else ''}--llm {found['name']}: "
                        f"{note[9:] if note.startswith('WARNING: ') else note}"), file=sys.stderr)
    # Ollama serves the agent's requests with the window it loads the model with, whatever Hermes is told (its
    # OpenAI-compatible API takes no num_ctx): Hermes is told that (start_context), and refused below its minimum.
    context = start_context(a, found, model)
    unknown_window_note(a, found, model, context_value(context), "hermes")
    served, how = served_context(a, found, model)
    loop = loop_guard_of(a, found)
    openrouter = found["parsed"]["host"] == HERMES_OPENROUTER_HOST
    section = {"provider": "openrouter" if openrouter else "custom", "base_url": agent_url(found), "default": model}
    if not openrouter:
        section["api_key"] = PLACEHOLDER
    if context_value(context):
        section["context_length"] = context_value(context)
        if section["context_length"] < HERMES_MIN_CONTEXT and served and section["context_length"] == served:
            why = OLLAMA_HOW.get(how, "as it loads it")
            problem = (f"--llm {found['name']}: Hermes needs a context window of at least 64,000 tokens and refuses "
                       f"to work with less (it says only 'Something went wrong'); Ollama serves {model} with "
                       f"{served} tokens ({why}), and its OpenAI-compatible API takes no num_ctx. Set "
                       "OLLAMA_CONTEXT_LENGTH=65536 (or more) "
                       "on the server and restart it, or give the model PARAMETER num_ctx 65536 in a Modelfile "
                       f"(ollama create); a model loaded already keeps its window until it is unloaded (sandbox-ai "
                       f"llm unload {found['name']} {model})")
            if a.agent == "hermes":
                raise ValueError(problem)
            print(printable(f"agent-sandbox: WARNING: {problem}"), file=sys.stderr)  # Hermes as a helper
        elif section["context_length"] < HERMES_MIN_CONTEXT:
            problem = (f"--llm {found['name']}: Hermes needs a context window of at least 64,000 tokens and refuses "
                       f"to work with less (it says only 'Something went wrong'); {model} would be told "
                       f"{section['context_length']}. Load the model with 64k tokens or more: --llm-param "
                       "num_ctx=65536 (LM Studio loads it so; or its Context Length), Ollama: OLLAMA_CONTEXT_LENGTH, "
                       "llama.cpp: --ctx-size, vLLM: --max-model-len")
            if a.agent == "hermes":
                raise ValueError(problem)
            print(printable(f"agent-sandbox: WARNING: {problem}"), file=sys.stderr)  # Hermes as a helper
    config = {"model": section, "fallback_providers": []}
    if loop["enabled"]:
        config["tool_loop_guardrails"] = {"warnings_enabled": True, "hard_stop_enabled": True,
                                          "non_interactive_hard_stop_enabled": True}
    if loop.get("steps"):
        config["agent"] = {"max_turns": loop["steps"]}
    if loop.get("compaction"):
        config["compression"] = {"enabled": True}
    if loop.get("instructions"):
        config["skills"] = {"auto_load": ["sandbox-ai-loop-guard"]}
    return config


QWEN_SETTINGS = "/etc/qwen-code/settings.json"  # qwen.py's SETTINGS (this module loads no sibling)
# Qwen Code's waits for --llm, all from agent_wait (LOCAL_WAIT for a server of your own): a large local model may be
# silent for minutes before its first token. Qwen Code 0.26.4 takes a modelProviders model's timeout (per request,
# 120 s by default) and streamIdleTimeoutMs (240 s) from that entry's generationConfig - not from model.generationConfig
# nor, for timeout, QWEN_CODE_API_TIMEOUT_MS; the variables QWEN_WAITS say the same for any other model. A stream's
# whole-life cap (15 minutes upstream) has only QWEN_STREAM_MAX_LIFETIME_MS: never under LOCAL_WAIT. 2 retries.
QWEN_RETRIES = 2
QWEN_WAITS = ("QWEN_STREAM_IDLE_TIMEOUT_MS", "QWEN_CODE_API_TIMEOUT_MS")
QWEN_LIFETIME = "QWEN_STREAM_MAX_LIFETIME_MS"
# Qwen Code 0.26.4's managed auto-memory sends one more request with the whole conversation after every turn (memory
# extraction), auto-dream one to consolidate the memories: on a local model each is a long prefill of its own (the
# local-model research's mock: 6 requests for a two-turn task, 4 with both off). Off for --llm; ~/.qwen/QWEN.md, the
# memory you keep, is not touched.
QWEN_MEMORY = {"enableManagedAutoMemory": False, "enableManagedAutoDream": False}
QWEN_EXCLUDED_TOOLS = ("read_mcp_resource",)  # tools.exclude for --llm (qwen_settings)
GOOSE_CONFIG = "/etc/goose/config.yaml"  # goose.py's CONFIG
GOOSE_LOOP_FILE = "/etc/goose/sandbox-ai-loop-guard.md"  # goose.py's LOOP_FILE
OPENHANDS_SETTINGS = "/etc/openhands/settings.json"  # openhands.py's SETTINGS
# letta.py's LLM_PROVIDER, LLM_URL_VARIABLE, LLM_KEY_VARIABLE: Letta Code's OpenAI-compatible provider that reads its
# server and key from variables.
LETTA_PROVIDER = "llama-cpp"
LETTA_URL_VARIABLE = "LLAMA_CPP_BASE_URL"
LETTA_KEY_VARIABLE = "LLAMA_CPP_API_KEY"
PI_PROVIDER = "sandbox-ai"  # pi.py's LLM_PROVIDER: the provider sandbox-ai's extension registers for --llm
DSH_PATCH = "/etc/dsh/sandbox-ai.patch.yml"  # dsh.py's PATCH
DSH_ROUTE = "sandbox-ai"  # dsh.py's LLM_ROUTE


def llm_context(a, found, model):
    """The model's context window as the agent is told it (the start's num_ctx, else what the server reports)."""
    behind = bool(getattr(a, "vpn", None))
    note = keep_loaded(found, model, a, behind_vpn=behind)
    if note:
        print(printable(f"agent-sandbox: {'WARNING: ' if note.startswith('WARNING: ') else ''}--llm {found['name']}: "
                        f"{note[9:] if note.startswith('WARNING: ') else note}"), file=sys.stderr)
    return start_context(a, found, model)


def qwen_generation(wait):
    """A Qwen Code provider entry's generationConfig waits for WAIT seconds (None: Qwen Code's own) and retries."""
    return {**({"timeout": wait * 1000, "streamIdleTimeoutMs": wait * 1000} if wait else {}),
            "maxRetries": QWEN_RETRIES}


def qwen_wait_environment(a, found, model):
    """Qwen Code's wait variables for agent_wait (your own --env of them wins)."""
    wait = agent_wait(a, found, model)
    env = {**({name: str(wait * 1000) for name in QWEN_WAITS} if wait else {}),
           QWEN_LIFETIME: str(max(wait or 0, LOCAL_WAIT) * 1000)}
    given = passed_names(a)
    return {name: value for name, value in env.items() if name not in given}


def qwen_settings(a):
    """Qwen Code's system settings for --llm (laid over its defaults by qwen.managed_settings): OpenAI-compatible
    auth, the model, the server as the model's provider (its /v1 URL, the context window when known, the waits of
    agent_wait, the key from OPENAI_API_KEY: the placeholder), no managed auto-memory (QWEN_MEMORY), and the loop
    guard's turn limit and Qwen Code's own loop detection."""
    found, model = selection(a)
    names = offered_models(a)[0]
    contexts = model_limits(a, found, names)[0]
    contexts[model] = llm_context(a, found, model)
    unknown_window_note(a, found, model, contexts[model], "qwen")

    def entry(name):
        # Each model of the server its own entry (Qwen Code tells them apart by id and baseUrl): /model offers them.
        made = {"id": name, "name": f"{name} ({found['name']}, sandbox-ai)", "baseUrl": agent_url(found),
                "envKey": "OPENAI_API_KEY", "generationConfig": qwen_generation(agent_wait(a, found, name))}
        if contexts.get(name):
            made["generationConfig"]["contextWindowSize"] = contexts[name]
        return made
    settings = {"security": {"auth": {"selectedType": "openai"}}, "model": {"name": model},
                "modelProviders": {"openai": [entry(name) for name in names]}, "memory": dict(QWEN_MEMORY)}
    loop = loop_guard_of(a, found)
    # Qwen Code's heuristic loop detection off (thoughts, repeated sentences, read loops): it compares the first 200
    # characters of the last three "thoughts", a local server streams one thought in many pieces, and Qwen Code
    # ended the run a minute into a task - "Loop detection halted the run (repetitive_thoughts ...)": Laguna-S twice
    # of two runs, Qwen3.6-35B (the local-model research's round 1, runs/t2_debug__qwen__laguna__r1, __q36__r1).
    # Its protection against a repeated tool call stays with the loop guard on (loopProtection), and sandbox-ai's
    # own guard holds on the wire (the same call, repeated text).
    settings["model"]["skipLoopDetection"] = True
    # Qwen Code's tool for MCP resources left out: it lists read_mcp_resource ("Reads a resource from a configured MCP
    # server ...") among its deferred tools whatever a server advertises, and a local model asked for an MCP server's
    # tool found it with tool_search and guessed URIs until Qwen Code stopped the run (Laguna-S, round 1:
    # runs/smoke__qwen__laguna__r2); the proxy's sentence (runtime.llm_resource_hint) never reaches it, as it is not in
    # the request's tools. sandbox-ai's catalog servers publish no resources.
    settings["tools"] = {"exclude": list(QWEN_EXCLUDED_TOOLS)}
    if loop["enabled"]:
        settings["model"]["loopProtection"] = "stop"
    if loop.get("steps") and "steps" in loop_given(a, found):
        # Only steps you set: Qwen Code's own limit is otherwise qwen.MAX_SESSION_TURNS (the guard's default is a
        # step limit of one turn, Qwen Code's counts a whole session).
        settings["model"]["maxSessionTurns"] = loop["steps"]
    return settings


def dsh_config(a):
    """DSH's model for --llm (patch rows by dsh.model_rows): an OpenAI chat-completions route at the server's /v1
    URL with the model and its context window, the key from SANDBOX_AI_LLM_KEY (the placeholder), and the loop
    guard's instructions for its system prompt."""
    found, model = selection(a)
    names = offered_models(a)[0]
    contexts = model_limits(a, found, names)[0]
    contexts[model] = llm_context(a, found, model)
    unknown_window_note(a, found, model, contexts[model], "dsh")
    # The route's models are what DSH's model selection offers (a model not listed fails with UNKNOWN_MODEL).
    route = {"displayName": f"{found['name']} (sandbox-ai)", "apiKeyEnv": KEY_VARIABLE, "api": "openai-completions",
             "baseURL": agent_url(found),
             "models": [{"id": name, **({"contextWindow": contexts[name]} if contexts.get(name) else {})}
                        for name in names]}
    loop = loop_guard_of(a, found)
    return {"route": route, "model": model, "instructions": loop_text(loop) if loop.get("instructions") else None}


# Goose 1.57.0's waits, in seconds: OPENAI_TIMEOUT (its openai provider's request, 600 by default: a silent prefill
# was given up after 600 s and sent again, three times) and GOOSE_STREAM_TIMEOUT (a stream's wait, which its error
# names beside OLLAMA_STREAM_TIMEOUT). --llm uses only the openai provider. With both at an hour it waited through
# 420 s of silence (the local-model research's runs/delayfix_silent_goose).
GOOSE_WAITS = ("OPENAI_TIMEOUT", "GOOSE_STREAM_TIMEOUT")


def goose_environment(a, found, model):
    """Goose's variables for --llm (they win over every configuration file of Goose's): its OpenAI provider at the
    server's /v1 URL with the model, the placeholder as key, its waits of agent_wait (your own --env of them wins), the
    context window when known, and the loop guard's turn limit (GOOSE_MAX_TURNS) and instructions (the Top Of Mind
    extension adds GOOSE_MOIM_MESSAGE_FILE's text to every turn; goose.files writes it)."""
    env = {"GOOSE_PROVIDER": "openai", "GOOSE_MODEL": model, "OPENAI_BASE_URL": agent_url(found),
           "OPENAI_API_KEY": placeholder_for(a, found)}
    wait, given = agent_wait(a, found, model), passed_names(a)
    if wait:
        env.update({name: str(wait) for name in GOOSE_WAITS if name not in given})
    context = llm_context(a, found, model)
    unknown_window_note(a, found, model, context, "goose")
    if context:
        env["GOOSE_CONTEXT_LIMIT"] = str(context)
    loop = loop_guard_of(a, found)
    if loop.get("steps"):
        env["GOOSE_MAX_TURNS"] = str(loop["steps"])
    if loop.get("instructions"):
        env["GOOSE_MOIM_MESSAGE_FILE"] = GOOSE_LOOP_FILE
    return env


NANOBOT_SETTINGS = "/etc/nanobot/sandbox-ai.json"  # nanobot.py's SETTINGS
PI_SETTINGS = "/etc/pi/sandbox-ai.json"  # pi.py's EXTENSION_CONFIG


def nanobot_config(a):
    """nanobot's settings for --llm (laid over its own file by nanobot.managed_config): its custom provider (any
    OpenAI-compatible server) at the server's /v1 URL with the placeholder as key, the model and its context window
    when known, and the loop guard's step limit (agents.defaults.maxToolIterations). nanobot has no place for the
    guard's instructions outside its own files; the proxy's sampling and the step limit hold."""
    found, model = selection(a)
    context = llm_context(a, found, model)
    unknown_window_note(a, found, model, context, "nanobot")
    defaults = {"provider": "custom", "model": model}
    if context:
        defaults["contextWindowTokens"] = context
    loop = loop_guard_of(a, found)
    if loop.get("steps"):
        defaults["maxToolIterations"] = loop["steps"]
    return {"providers": {"custom": {"apiBase": agent_url(found), "apiKey": placeholder_for(a, found)}},
            "agents": {"defaults": defaults}}


def goose_loop_text(a):
    """The loop guard's instructions for Goose's GOOSE_LOOP_FILE, or None when the guard gives none."""
    try:
        found, _ = selection(a)
    except ValueError:
        return None
    loop = loop_guard_of(a, found)
    return loop_text(loop) if loop.get("instructions") else None


def openhands_llm(a):
    """OpenHands' LLM for --llm (in the settings its start applies, openhands.managed_settings): LiteLLM's OpenAI
    provider at the server's /v1 URL with the model, the placeholder as key, the context window when known."""
    found, model = selection(a)
    settings = {"model": "openai/" + model, "base_url": agent_url(found), "api_key": placeholder_for(a, found)}
    context = llm_context(a, found, model)
    unknown_window_note(a, found, model, context, "openhands")
    if context:
        settings["max_input_tokens"] = context
    return settings


def openhands_loop(a):
    """The loop guard for OpenHands: its turn limit (the conversation's max_iterations) and instructions (the
    agent's system_message_suffix), each None when the guard gives none."""
    try:
        found, _ = selection(a)
    except ValueError:
        return None
    loop = loop_guard_of(a, found)
    return {"steps": loop.get("steps"), "instructions": loop_text(loop) if loop.get("instructions") else None}


def letta_model(a):
    """Letta Code's model handle for --llm: its OpenAI-compatible provider and the model (llama-cpp/MODEL)."""
    _, model = selection(a)
    return f"{LETTA_PROVIDER}/{model}"


def letta_environment(a, found):
    """Letta Code's variables for --llm: its OpenAI-compatible provider at the server's /v1 URL with the placeholder
    as key (Letta Code has no read-only place for the loop guard's instructions or a turn limit; the proxy's loop
    guard notes hold)."""
    return {LETTA_URL_VARIABLE: agent_url(found), LETTA_KEY_VARIABLE: placeholder_for(a, found)}


def pi_llm(a):
    """Pi's model for --llm (registered by sandbox-ai's extension, pi.extension_config): a provider PI_PROVIDER with
    the OpenAI Chat Completions API at the server's /v1 URL, the placeholder as key and the model with its context
    window when known (else Pi's default), and the loop guard's instructions (a section of Pi's system prompt) and
    turn limit (the extension stops a run there)."""
    found, model = selection(a)
    context = llm_context(a, found, model)
    unknown_window_note(a, found, model, context, "pi")
    entry = {"id": model, "name": f"{model} ({found['name']}, sandbox-ai)", "reasoning": False, "input": ["text"],
             "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}
    if context:
        entry["contextWindow"] = context
    provider = {"name": f"{found['name']} (sandbox-ai)", "baseUrl": agent_url(found),
                "apiKey": placeholder_for(a, found), "api": "openai-completions", "models": [entry]}
    loop = loop_guard_of(a, found)
    return {"provider": {"id": PI_PROVIDER, "config": provider}, "model": model,
            "instructions": loop_text(loop) if loop.get("instructions") else None, "steps": loop.get("steps")}


# The agents' wait for a model server of your own (local_model_server) where stream_idle_timeout is not set: a fresh
# 50-100k prompt on a large local model takes 2-6 minutes before its first token, and the agents' own waits (Qwen Code
# 120 s per request, OpenCode and Codex 300 s, Goose 600 s) gave up during that prefill and sent everything again, for
# ever (the local-model research's mock server: evidence/timeouts_mock.txt). An hour: the proxy's own idle limit
# (runtime.IDLE_TIMEOUT), so sandbox-ai's path never cuts a stream sooner either.
LOCAL_WAIT = 3600
# Two agents have no setting for that wait (Claude Code, DeepSeek Harness: 300 s without a byte, then the request
# again and the server's prefill from its beginning; the research's round 1, evidence/waits.txt). For Claude Code -
# and whatever else stands between an agent and a silent server - the connection proxy keeps a streamed answer of a
# model server of your own alive: a line every reader of server-sent events skips, after this many seconds without
# a byte from the server (proxy.json's "keepalive", runtime.llm_kept_alive). DeepSeek Harness gives up all the same
# (its limit is not reset by such a line, nor by an empty chunk): with it a prompt must be read within 300 s.
KEEPALIVE = 20


def agent_wait(a, found, model):
    """The seconds the agent waits for a streamed answer's first and next part: stream_idle_timeout of the start or
    the server, else LOCAL_WAIT for a model server of your own, else None (a public service: the agent's own)."""
    try:
        override = start_params(a, found)[0]
    except ValueError:
        override = {}  # validate() reports it
    wait = effective_params(found, model, override)[0].get("stream_idle_timeout")
    return wait or (LOCAL_WAIT if local_model_server(found) else None)


CLAUDE_WAITS = ("CLAUDE_STREAM_IDLE_TIMEOUT_MS", "CLAUDE_STREAM_FIRST_BYTE_TIMEOUT_MS")
# Pi's waits (1.4.0), 300000 ms each by default: httpIdleTimeoutMs - its HTTP client's wait for the response headers
# and between two parts of the body (core/http-dispatcher.js: undici's headersTimeout and bodyTimeout) - and
# retry.provider.timeoutMs, the provider request's (core/sdk.js; httpIdleTimeoutMs where it is not set). Through a
# silent prefill of 420 s Pi ended three requests after 300 s each ("terminated", "Request timed out.") and the run
# failed; with both raised it waited and answered (the local-model research's runs/delay_*_pi, delayfix3_pi). Pi reads
# them from its user's settings only - no managed layer, no variable, no option (pi.py) - so a --llm start sets them
# there, in the sandbox's HOME (prepare_home), from agent_wait like the other agents' waits.
PI_USER_SETTINGS = ".pi/agent/settings.json"


def pi_wait_settings(wait):
    """Pi's settings for a wait of WAIT seconds (agent_wait)."""
    return {"httpIdleTimeoutMs": wait * 1000, "retry": {"provider": {"timeoutMs": wait * 1000}}}


def pi_wait_apply(wait):
    """apply(document) for the launcher's update_json_defaults: pi_wait_settings(WAIT) set in Pi's settings
    DOCUMENT, every other setting of the user's kept (the rest of "retry" too); whether it changed anything. The
    start's wait goes over a value that is there: stream_idle_timeout is how it is chosen."""
    def apply(document):
        wanted = wait * 1000
        retry = document.get("retry") if isinstance(document.get("retry"), dict) else {}
        provider = retry.get("provider") if isinstance(retry.get("provider"), dict) else {}
        if document.get("httpIdleTimeoutMs") == wanted and provider.get("timeoutMs") == wanted and \
                type(document["httpIdleTimeoutMs"]) is int and type(provider["timeoutMs"]) is int:
            return False
        document["httpIdleTimeoutMs"] = wanted
        document["retry"] = {**retry, "provider": {**provider, "timeoutMs": wanted}}
        return True
    return apply


def passed_names(a):
    """The variables you pass to the start yourself (--env NAME, --secret-env NAME=SECRET)."""
    return set(getattr(a, "env", None) or []) | {value.partition("=")[0]
                                                 for value in getattr(a, "secret_env", None) or []}


def claude_wait_environment(a, found, model):
    """Claude Code's waits for agent_wait (your own --env of them wins)."""
    wait = agent_wait(a, found, model)
    given = passed_names(a)
    return {name: str(wait * 1000) for name in CLAUDE_WAITS if name not in given} if wait else {}


def agent_environment(a, windows=True):
    """The agent's variables for --llm: placeholders and addresses, nothing secret. WINDOWS=False: without Claude
    Code's window variables (which ask the server; yours of them win, so they are never refused)."""
    found, model = selection(a)
    if a.agent == "openhands":
        return {}  # its model is in the settings its start applies (openhands_llm)
    if a.agent == "letta":
        if windows:  # Letta Code takes no window: what it assumes is said (which asks the server)
            unknown_window_note(a, found, model, start_context(a, found, model), "letta")
        return letta_environment(a, found)
    if a.agent == "claude":
        return {**claude_environment(found, model, passed_names(a)), **claude_wait_environment(a, found, model),
                **(claude_window_environment(a, found, model) if windows else {})}
    if a.agent == "goose":
        return goose_environment(a, found, model)
    if a.agent == "qwen":
        # Its OpenAI-compatible auth reads these too (the system settings name the same server and model); its waits.
        return {"OPENAI_API_KEY": PLACEHOLDER, "OPENAI_BASE_URL": agent_url(found), "OPENAI_MODEL": model,
                **qwen_wait_environment(a, found, model)}
    if a.agent == "dsh":
        return {KEY_VARIABLE: PLACEHOLDER}
    if a.agent == "hermes":
        # Hermes reads OpenRouter's key from OPENROUTER_API_KEY; every other server's from its configuration.
        return {"OPENROUTER_API_KEY": placeholder_for(a, found)} if found["parsed"]["host"] == HERMES_OPENROUTER_HOST \
            else {}
    if a.agent == "codex":
        return {KEY_VARIABLE: PLACEHOLDER}
    if a.agent == "nanobot":
        return {}  # its provider, key placeholder included, is in sandbox-ai's settings (nanobot_config)
    if a.agent == "pi":
        return {}  # its provider, key placeholder included, is registered by sandbox-ai's extension (pi_llm)
    env = {"OPENCODE_CONFIG": "/root/" + OPENCODE_CONFIG, "OPENCODE_DISABLE_MODELS_FETCH": "1",
           "OPENCODE_DISABLE_AUTOUPDATE": "1"}
    if a.agent == "opencode2":
        # OpenCode 2 reads OpenCode 1's file (the v1 shape), read-only in its managed directory; -D is its
        # configuration layer's allow-all rule (it has no OPENCODE_PERMISSION and no doom_loop question: the loop
        # guard's steps and notes hold).
        return {**env, "OPENCODE_CONFIG": OPENCODE2_CONFIG}
    # --dangerously-skip-permissions gives OpenCode {"*": "allow"} (agent_sandbox.DANGER_ENV), which also allows its
    # doom_loop question (OpenCode 1.x: the same tool with the same input three times in one answer; its rules: the
    # last match wins). With the loop guard on, that question stays OpenCode's default, ask.
    if getattr(a, "dangerously_skip_permissions", False) and loop_guard_of(a, found)["enabled"] and \
            "OPENCODE_PERMISSION" not in (getattr(a, "env", None) or []):
        env["OPENCODE_PERMISSION"] = json.dumps({"*": "allow", "doom_loop": "ask"}, separators=(",", ":"))
    return env


def loop_guard_of(a, endpoint):
    """The start's loop guard: its layers (loop_config) with the start's --llm-param loop_guard... over them."""
    try:
        start = start_params(a, endpoint)[1]
    except ValueError:
        start = {}  # validate() reports it
    return loop_config(endpoint, start)


def opencode_layer(a, found, model, prune=True, instructions="/root/" + LOOP_INSTRUCTIONS, agent="opencode"):
    """(OpenCode's provider configuration for --llm, the loop guard's layers): the models the start offers
    (offered_models) with the context windows and output limits it gives them, and the waits (opencode_config)."""
    names = offered_models(a)[0]
    contexts, outputs = model_limits(a, found, names)
    contexts[model] = start_context(a, found, model)
    unknown_window_note(a, found, model, contexts[model], agent)
    loop = loop_guard_of(a, found)
    config = opencode_config(found, model, only=a.no_provider_defaults, allowed=names, contexts=contexts,
                             outputs=outputs, loop=loop, wait=agent_wait(a, found, model), prune=prune,
                             instructions=instructions, vision=vision_models(a, found))
    return config, loop


# opencode2.py's LLM_CONFIG and LOOP_FILE (this module loads no sibling): OpenCode 2's --llm file and the loop guard's
# instructions, read-only in its managed directory (OPENCODE_CONFIG names the first).
OPENCODE2_CONFIG = "/etc/opencode2/sandbox-ai-llm.json"
OPENCODE2_LOOP_FILE = "/etc/opencode2/sandbox-ai-loop-guard.md"


def opencode2_files(a):
    """OpenCode 2's managed files for --llm: {name in its managed directory: content} - OpenCode 1's provider file
    (the v1 shape OpenCode 2 reads, without compaction.prune) and the loop guard's instructions when they are on."""
    found, model = selection(a)
    note = keep_loaded(found, model, a, behind_vpn=bool(getattr(a, "vpn", None)))
    if note:
        print(printable(f"agent-sandbox: {'WARNING: ' if note.startswith('WARNING: ') else ''}--llm {found['name']}: "
                        f"{note[9:] if note.startswith('WARNING: ') else note}"), file=sys.stderr)
    config, loop = opencode_layer(a, found, model, prune=False, instructions=OPENCODE2_LOOP_FILE, agent="opencode2")
    files = {OPENCODE2_CONFIG.rsplit("/", 1)[1]: json.dumps(config, indent=2) + "\n"}
    if loop["instructions"]:
        files[OPENCODE2_LOOP_FILE.rsplit("/", 1)[1]] = loop_text(loop)
    return files


def opencode2_model(a):
    """OpenCode 2's model for --llm, as its configuration layer names it (provider/model): the layer above every
    file, so a project's opencode.json cannot point it elsewhere (OpenCode 2 falls back to its maker's free models
    when the model it is given does not exist)."""
    found, model = selection(a)
    return f"{provider_id(found)}/{model}"


def loop_given(a, endpoint):
    """The loop guard's layers set somewhere (your defaults.json, the endpoint's, the start's --llm-param), not
    sandbox-ai's defaults."""
    try:
        start = start_params(a, endpoint)[1]
    except ValueError:
        start = {}
    return {**user_loop(), **(endpoint.get("loop_guard") or {}), **start}


def agent_arguments(a):
    """Arguments put before the agent's own: Codex selects the profile written by prepare_home."""
    if a.agent == "letta" and not getattr(a, "helper_of", None):
        return ["--model", letta_model(a)]  # its provider's handle (a helper's Letta Code finds the model itself)
    if a.agent == "pi" and not getattr(a, "helper_of", None):
        return ["--model", f"{PI_PROVIDER}/{selection(a)[1]}"]  # the provider sandbox-ai's extension registers
    if a.agent == "qwen":
        # The loop guard's instructions: Qwen Code appends them to its system prompt for this run.
        try:
            found, _ = selection(a)
        except ValueError:
            return []
        loop = loop_guard_of(a, found)
        return ["--append-system-prompt", loop_text(loop)] if loop.get("instructions") else []
    if a.agent == "claude":
        # Claude Code gets no loop-guard text; the progress_notes line, where a profile or the start switches it on,
        # goes into its system prompt
        try:
            found, _ = selection(a)
        except ValueError:
            return []
        loop = loop_guard_of(a, found)
        return ["--append-system-prompt", PROGRESS_TEXT.strip()] if loop.get("progress_notes") else []
    return ["--profile", CODEX_PROFILE] if a.agent == "codex" else []


def prepare_home(a, home, replace_private, update_json_defaults, state_limit):
    """Write the agent's configuration for the endpoint into the sandbox HOME with the launcher's helpers, which never
    follow a symlink the agent planted. Returns the HOME-relative paths written."""
    found, model = selection(a)
    if a.agent == "pi":
        # Pi's provider is registered by sandbox-ai's extension at /etc/pi; only its waits are settings of the user's
        # (PI_USER_SETTINGS): set there for a server that is waited for (agent_wait), left alone otherwise.
        wait = agent_wait(a, found, model)
        if not wait:
            return []
        problem = update_json_defaults(home, PI_USER_SETTINGS, pi_wait_apply(wait))
        if problem:
            print(f"agent-sandbox: left sandbox ~/{PI_USER_SETTINGS} unchanged ({printable(problem)}): Pi gives up "
                  "after 5 minutes without a byte from the model unless it says otherwise (httpIdleTimeoutMs, "
                  "retry.provider.timeoutMs)", file=sys.stderr)
            return []
        return [PI_USER_SETTINGS]
    if a.agent in ("hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "opencode2"):
        # Their configuration is managed (/etc/hermes, /etc/dsh, /etc/qwen-code, /etc/openhands, OpenCode 2's
        # /etc/opencode2; Goose's and Letta Code's variables; nanobot's laid over its own file from /etc/nanobot at
        # its start), nothing in the HOME.
        return []
    note = keep_loaded(found, model, a, behind_vpn=bool(getattr(a, "vpn", None)))
    if note and note.startswith("WARNING: "):
        print(printable(f"agent-sandbox: WARNING: --llm {found['name']}: {note[9:]}"), file=sys.stderr)
    elif note:
        print(printable(f"agent-sandbox: --llm {found['name']}: {note}"), file=sys.stderr)
    if a.agent == "opencode":
        config, loop = opencode_layer(a, found, model)
        replace_private((json.dumps(config, indent=2) + "\n").encode(), home, OPENCODE_CONFIG)
        if loop["instructions"]:
            replace_private(loop_text(loop).encode(), home, LOOP_INSTRUCTIONS)
            return [OPENCODE_CONFIG, LOOP_INSTRUCTIONS]
        return [OPENCODE_CONFIG]
    if a.agent == "codex":

        relative = f".codex/{CODEX_PROFILE}.config.toml"
        context = start_context(a, found, model)
        unknown_window_note(a, found, model, context_value(context), "codex")
        wait = agent_wait(a, found, model)
        replace_private(codex_profile(found, model, context, wait).encode(), home, relative)
        written = [relative]
        # The server's other models: a profile each (Codex switches models of another catalog only when it starts).
        names = offered_models(a)[0]
        contexts = model_limits(a, found, names)[0] if names[1:] else {}
        for name, profile in codex_profiles(names[1:CODEX_PROFILES + 1]).items():
            relative = f".codex/{profile}.config.toml"
            replace_private(codex_profile(found, name, contexts.get(name), wait, profile).encode(), home, relative)
            written.append(relative)
        return written
    written = []
    for relative, apply, limit in ((".claude/settings.json", claude_settings, None), (".claude.json", claude_state,
                                                                                         state_limit)):
        problem = update_json_defaults(home, relative, apply, *([limit] if limit else []))
        if problem:
            print(f"agent-sandbox: left sandbox ~/{relative} unchanged ({problem})", file=sys.stderr)
        else:
            written.append(relative)
    return written


def plan(a):
    """The dry-run plan's "llm": what the start would open and write; secrets by name only."""
    found, model = selection(a)
    parsed = found["parsed"]
    if parsed["loopback"]:
        opens = f"127.0.0.1:{parsed['port']} (the host's loopback, --allow-host-port)"
    elif parsed["address"]:
        opens = f"{parsed['hostform']}:{parsed['port']}"
    else:
        opens = f"{parsed['host']}:{parsed['port']} (pinned at the start)"
    config = {"opencode": "/root/" + OPENCODE_CONFIG, "opencode2": OPENCODE2_CONFIG, "codex": f"/root/.codex/{CODEX_PROFILE}.config.toml",
              "claude": "environment (ANTHROPIC_BASE_URL ...)", "hermes": HERMES_CONFIG, "dsh": DSH_PATCH,
              "qwen": QWEN_SETTINGS, "goose": "environment (GOOSE_PROVIDER, OPENAI_BASE_URL ...)",
              "openhands": OPENHANDS_SETTINGS,
              "nanobot": NANOBOT_SETTINGS,
              "letta": f"environment ({LETTA_URL_VARIABLE}, {LETTA_KEY_VARIABLE}) and --model {LETTA_PROVIDER}/MODEL",
              "pi": f"{PI_SETTINGS} (sandbox-ai's extension), --model {PI_PROVIDER}/MODEL and its waits in "
                    f"/root/{PI_USER_SETTINGS}"
              }[a.agent]
    offered, limited = offered_models(a)
    return {"name": found["name"], "kind": found["kind"], "url": found["url"], "model": model,
            # The models the agent offers (the start's first; ONE_MODEL: that one alone); "models_limited": the
            # relay refuses any other.
            "models": [model] if a.agent in ONE_MODEL else offered, "models_limited": limited,
            "agent_url": agent_url(found, "anthropic" if a.agent == "claude" else "openai"),
            "api": {"claude": "anthropic-messages", "codex": "openai-responses", "opencode": "openai-chat",
                    "opencode2": "openai-chat",
                    "hermes": "openai-chat", "dsh": "openai-chat", "qwen": "openai-chat",
                    "goose": "openai-chat", "openhands": "openai-chat", "nanobot": "openai-chat", "letta": "openai-chat",
                    "pi": "openai-chat"}[a.agent],
            "opens": opens, "path_prefix": parsed["prefix"] or None, "http_rule": prefix_rule(a, found),
            "config": config,
            "key_secret": found["api_key_secret"], "header_secrets": found["headers"],
            "ca": str(ca_dir() / found["ca"]) if found["ca"] else None, "vpn": found["vpn"],
            "placeholder": placeholder_for(a, found), "insecure_http": found["insecure_http"],
            "params": plan_params(a, found, model),
            # Claude Code's own tools the proxy keeps in its requests (--llm-tools; None: no Claude Code in this start).
            "claude_tools": claude_tools_plan(a),
            # The model profile of this model in this agent (None: no family matches): profile_report.
            "profile": profile_report(a, found, model),
            # --use-case: what it chose and why, the settings it puts over the profile (None without it)
            "use_case": getattr(a, "_llm_use_case", None)}


def claude_tools_plan(a):
    """The dry run's "claude_tools": --llm-tools as it applies ("tools": all, core or the list; "from": --llm-tools,
    profile or default; "kept": the built-in tools kept, None for all), None when no Claude Code runs."""
    if not runs_claude(a):
        return None
    tools, source = start_tools(a)
    return {"tools": tools_text(tools), "from": source, "kept": kept_tools(tools)}


def plan_params(a, endpoint, model):
    """The dry run's "params": what the proxy sets for MODEL (forced: in place of the agent's own), what it never
    sends to this kind, and the loop guard."""
    override, start = start_params(a, endpoint)
    values, forced = effective_params(endpoint, model, override, profile=False)  # the profile's: plan()'s "profile"
    sent = set().union(*SENT[endpoint["kind"]].values())
    loop = loop_config(endpoint, start)
    load = LOAD_ONLY.get(endpoint["kind"], ())
    return {"values": values, "forced": sorted(forced & set(values)),
            "not_sent": sorted(key for key in values if key not in sent),
            # Where the values that are not sent go: the model's load (LM Studio) and the agents' configuration.
            "at_load": {key: values[key] for key in values if key in load},
            "agents_told": {key: values[key] for key in values if key in AGENT_ONLY},
            # The seconds the agent waits for the server's answer (agent_wait): None is the agent's own.
            "agent_wait": agent_wait(a, endpoint, model),
            "server_only": server_only(endpoint["kind"]),
            "family": (family_preset(model) or None) if loop["sampling"] else None, "loop_guard": loop}


def apply_vpn(a):
    """The endpoint's VPN profile (llm add --vpn) becomes the start's --vpn: the sandbox starts behind it, and the
    server's name is resolved through it (pinned_names). Another --vpn is refused: a sandbox has one VPN. Returns a
    note when it set --vpn, else None; a.vpn_origin names the endpoint for the VPN's own messages."""
    try:
        found, _ = selection(a)
    except ValueError:
        return None  # validate() reports it
    profile, chosen = found.get("vpn"), getattr(a, "vpn", None)
    if not profile or chosen == profile:
        return None
    if chosen:
        raise ValueError(f"--llm {found['name']} is reached through the VPN profile {profile}, but this start has "
                         f"--vpn {chosen}; a sandbox has one VPN: drop --vpn {chosen}, or register the server without "
                         f"one (sandbox-ai llm add {found['name']} ... --replace)")
    a.vpn, a.vpn_origin = profile, f"--llm {found['name']}"
    return f"--llm {found['name']} is reached through the VPN profile {profile}: the sandbox starts behind it " \
           f"(--vpn {profile})"


def pinned_names(a):
    """The server's host name, when the start resolves it (neither an address nor loopback). With --vpn the
    launcher resolves it through the tunnel, with the other names, so a name only the VPN's resolver knows works."""
    parsed = selection(a)[0]["parsed"]
    return [] if parsed["loopback"] or parsed["address"] else [parsed["host"]]


# --- llm check ----------------------------------------------------------------------------------------------

def probe(client, path):
    """Whether the server has the API at PATH: a POST of an empty object is refused as a bad request by a server
    that has it, and as unknown (404, 405, LM Studio's "Unexpected endpoint") by one that does not. Nothing is
    generated. Returns "yes", "no", "auth" (key refused) or an error text."""
    import ssl
    try:
        status, data = client.call(path, body={})
    except (OSError, ssl.SSLError) as exc:
        return printable(getattr(exc, "reason", exc))
    if status in (401, 403):
        return "auth"
    if status in (404, 405, 501) or b"Unexpected endpoint" in data[:4096]:
        return "no"
    return "yes"


def resolve(host, port):
    """The addresses of HOST as the host resolves it."""
    return sorted({str(ipaddress.ip_address(row[4][0])) for row in
                   socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def check(endpoint, agent=None, model=None, get=None, out=print, connect=socket.create_connection, client=None,
          tunnel=None):
    """Reachability, TLS, the key, the models and the APIs the agents need, line by line; True if nothing failed.
    TUNNEL (an endpoint added with --vpn: its profile's tunnel, vpn.channel): every lookup, connection and request
    goes through it, as the agent's will."""
    import ssl
    parsed, failed = endpoint["parsed"], []

    def line(ok, text):
        if ok is False:
            failed.append(text)
        out(f"  {'ok  ' if ok else 'FAIL' if ok is False else 'note'}  {printable(text)}")
    out(f"{endpoint['name']}: {KINDS[endpoint['kind']]['label']} at {endpoint['url']}")
    if tunnel is not None:
        line(True, f"through the VPN profile {endpoint['vpn']}: its tunnel is up and checked")
    elif endpoint.get("vpn"):
        line(None, f"reached through the VPN profile {endpoint['vpn']}; this check runs from the host's network")
    try:
        if parsed["address"] or parsed["loopback"]:
            addresses = [parsed["address"]] if parsed["address"] else ["127.0.0.1"]
        elif tunnel is not None:
            try:
                addresses = tunnel.request("resolve", names=[parsed["host"]])["hosts"][parsed["host"]]
            except (ValueError, KeyError) as exc:
                raise OSError(f"through the VPN: {exc}") from None
        else:
            addresses = resolve(parsed["host"], parsed["port"])
    except OSError as exc:
        line(False, f"{parsed['host']} does not resolve: {exc}")
        return False
    line(True, f"address {', '.join(addresses)}" + ("" if parsed["address"] else f" ({parsed['host']})"))
    if parsed["scheme"] == "http":
        problem = plain_http_problem(parsed, addresses)
        if problem and not endpoint.get("insecure_http"):
            line(False, problem + ("; the key is not sent" if endpoint.get("api_key_secret") or endpoint.get("headers")
                                   else ""))
            return False
        elif problem:
            line(None, "WARNING: plain http to a public address (--insecure-http): prompts, code and the key "
                       "cross the network unencrypted")
        else:
            line(True, "plain http to " + ("the host's loopback" if parsed["loopback"] else "a local address"))
    if tunnel is not None:
        try:
            reached = tunnel.request("connect", wait=TIMEOUT + 15, url=endpoint["url"], timeout=TIMEOUT,
                                     ca=ca_text(endpoint) or None)
        except ValueError as exc:
            line(False, f"{addresses[0]} port {parsed['port']} through the VPN: {exc}")
            return False
        line(True, f"TLS {reached['tls']}, certificate verified for {parsed['host']} through the VPN"
             + (" with the endpoint's own CA" if endpoint.get("ca") else "") if reached.get("tls") else
             f"port {parsed['port']} answers through the VPN")
        client = client or VPNClient(endpoint, tunnel, get)
    else:
        try:
            with connect((addresses[0], parsed["port"]), timeout=TIMEOUT) as sock:
                if parsed["scheme"] == "https":
                    with tls_context(endpoint).wrap_socket(sock, server_hostname=parsed["host"]) as tls:
                        line(True, f"TLS {tls.version()}, certificate verified for {parsed['host']}"
                             + (" with the endpoint's own CA" if endpoint.get("ca") else ""))
                else:
                    line(True, f"port {parsed['port']} answers")
        except (OSError, ssl.SSLError, ValueError) as exc:
            line(False, f"{addresses[0]} port {parsed['port']}: {getattr(exc, 'reason', None) or exc}")
            return False
    try:
        client = client or Client(endpoint, get)
    except ValueError as exc:
        line(False, str(exc))
        return False
    try:
        models = endpoint_models(endpoint, client=client)
    except ValueError as exc:
        line(False, f"models: {exc}")
        return False
    line(True, ("the key is accepted; " if endpoint.get("api_key_secret") else "")
         + f"{len(models)} model(s) listed")
    wanted = model or endpoint.get("model")
    if wanted:
        line(wanted in [m["name"] for m in models], f"model {wanted}"
             + (" is listed" if wanted in [m["name"] for m in models] else " is not listed"))
    elif not models:
        line(False, "the server lists no model; load or pull one first")
    apis = [("claude", "/v1/messages", "the Anthropic Messages API (Claude Code)"),
            ("codex", "/v1/responses", "the OpenAI Responses API (Codex)"),
            ("opencode", "/v1/chat/completions", "the OpenAI Chat Completions API (OpenCode)"),
            ("opencode2", "/v1/chat/completions", "the OpenAI Chat Completions API (OpenCode 2)"),
            ("hermes", "/v1/chat/completions", "the OpenAI Chat Completions API (Hermes)"),
            ("dsh", "/v1/chat/completions", "the OpenAI Chat Completions API (DeepSeek Harness)"),
            ("qwen", "/v1/chat/completions", "the OpenAI Chat Completions API (Qwen Code)"),
            ("goose", "/v1/chat/completions", "the OpenAI Chat Completions API (Goose)"),
            ("openhands", "/v1/chat/completions", "the OpenAI Chat Completions API (OpenHands)"),
            ("nanobot", "/v1/chat/completions", "the OpenAI Chat Completions API (nanobot)"),
            ("letta", "/v1/chat/completions", "the OpenAI Chat Completions API (Letta Code)"),
            ("pi", "/v1/chat/completions", "the OpenAI Chat Completions API (Pi)")]
    found_messages = False
    for name, path, label in apis:
        if agent and agent != name or not agent and name in ("opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"):  # OpenCode's API
            continue
        answer = probe(client, path)
        required = agent == name
        if answer == "yes":
            found_messages = found_messages or name == "claude"
            line(True, f"{label} at {path}")
        elif answer == "auth":
            line(False, f"{label}: the server refused the key at {path}")
        elif answer == "no":
            hint = {"claude": f" (needs {', '.join(ANTHROPIC_SINCE.values())} or later)",
                    "codex": " (Codex needs it; see sandbox-ai llm --help)"}.get(name, "")
            line(False if required else None, f"no {label} at {path}{hint}")
        else:
            line(False if required else None, f"{label}: {answer}")
    for text in evict_notes(endpoint):
        line(None, text)
    if agent:
        problem = agent_problem(endpoint, agent)
        if problem and agent == "claude" and found_messages:
            # The server has it; the endpoint is only not marked as having it.
            line(False, f"the server has the Anthropic Messages API, but the endpoint {endpoint['name']} is not marked "
                        f"with it: add it again with sandbox-ai llm add {endpoint['name']} --kind {endpoint['kind']} "
                        f"--url {endpoint['url']} --anthropic --replace")
        elif problem:
            line(False, problem)
    return not failed


# --- llm status ------------------------------------------------------------------------------------------------

WATCH_MIN = 2


def statuses(name=None, get=None, read=None):
    """[endpoint_status] of the endpoint NAME (ValueError when there is none) or of every endpoint, read at the same
    time (a server that does not answer holds up no other). An endpoint the store cannot use is listed with why."""
    read = read or (lambda found: vpn_status(found, get))
    if name is not None:
        found = [endpoint(name)]
        broken = []
    else:
        store = load_store()
        found, broken = [], []
        for key in sorted(store["endpoints"]):
            try:
                found.append(check_record(key, store["endpoints"][key]))
            except ValueError as exc:
                record = store["endpoints"][key] if isinstance(store["endpoints"][key], dict) else {}
                broken.append({"name": printable(key)[:32], "kind": record["kind"] if isinstance(record.get("kind"), str) and record["kind"] in KINDS
                               else None, "url": None, "reachable": False, "error": f"unusable: {printable(exc)}"[:600],
                               "read_at": now_text(), "models": None, "loaded": None, "usage": {}, "reported_by": [],
                               "not_reported": list(REPORTABLE)})
    if not found:
        return broken
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(found))) as pool:
        read_all = list(pool.map(read, found))
    return sorted(read_all + broken, key=lambda entry: entry["name"])


def show_status(args, out, get=None, read=None):
    """sandbox-ai llm status [NAME] [--json] [--watch SECONDS]."""
    if args.watch is not None and args.watch < WATCH_MIN:
        raise ValueError(f"--watch: at least {WATCH_MIN} seconds")
    while True:
        found = statuses(args.name, get, read)
        if args.json:
            out(json.dumps(found, separators=(",", ":")) if args.watch else json.dumps(found, indent=2))
        else:
            if args.watch and out is print and sys.stdout.isatty():
                sys.stdout.write("\033[H\033[J")  # each reading in place of the one before
            elif args.watch:
                out(f"--- {now_text()}")
            if not found:
                out("no LLM endpoints; add one with: sandbox-ai llm add NAME --kind KIND --url URL")
            for entry in found:
                lines = status_lines(entry) if entry.get("kind") in KINDS else \
                    [printable(f"{entry['name']}: {entry.get('error') or '?'}")]
                for line in lines:
                    out(line)
        if not args.watch:
            return 0 if all(entry.get("reachable") for entry in found) else 1
        try:
            watch_sleep(args.watch)
        except KeyboardInterrupt:
            return 0


def watch_sleep(seconds):
    """The pause between two readings of `llm status --watch`: a function of its own, so that a test replaces this
    pause alone - time.sleep is every thread's (on CI a thread another test had left running took a replaced
    time.sleep's KeyboardInterrupt, and the watch read on without a pause)."""
    time.sleep(seconds)


# --- the command -----------------------------------------------------------------------------------------------

def describe(endpoint):
    """Lines that show an endpoint; secrets by name only."""
    lines = [f"{endpoint['name']}: {KINDS[endpoint['kind']]['label']} at {endpoint['url']}"]
    if endpoint["parsed"]["loopback"]:
        lines.append("  on this host's loopback: Podman only, reached with --allow-host-port "
                     f"{endpoint['parsed']['port']}")
    lines.append(f"  key: secret {endpoint['api_key_secret']}" if endpoint["api_key_secret"] else "  key: none")
    lines += [f"  header {header}: secret {secret}" for header, secret in sorted(endpoint["headers"].items())]
    if endpoint["ca"]:
        lines.append(f"  CA: {ca_dir() / endpoint['ca']}")
    if endpoint["vpn"]:
        lines.append(f"  VPN profile: {endpoint['vpn']}")
    if endpoint["model"]:
        lines.append(f"  default model: {endpoint['model']}")
    if endpoint["models"]:
        lines.append(f"  models ({endpoint['checked'] or 'listed earlier'}): {', '.join(endpoint['models'][:20])}"
                     + (" ..." if len(endpoint["models"]) > 20 else ""))
    lines.append("  agents: " + ", ".join(a for a in AGENTS if not agent_problem(endpoint, a)))
    if endpoint.get("memory_gb"):
        lines.append(f"  memory: {endpoint['memory_gb']} GB (what fits: llm recommend, --use-case)")
    lines += params_lines(endpoint)
    if endpoint["insecure_http"]:
        lines.append("  WARNING: plain http to a public address (--insecure-http)")
    return [printable(line) for line in lines]


def public_view(endpoint):
    """An endpoint as --json shows it (no secret values: there are none in the store)."""
    view = {key: endpoint[key] for key in ("name", "kind", "url", "api_key_secret", "headers", "ca", "vpn", "model",
                                           "anthropic", "insecure_http", "models", "checked")}
    # The parameters as stored, the loop guard as it applies (0: off), the context windows the server reported.
    view.update(params=endpoint.get("params") or {}, forced=endpoint.get("forced") or {},
                loop_guard=loop_config(endpoint), loop_guard_own=endpoint.get("loop_guard") or {},
                contexts=endpoint.get("contexts") or {},
                evict=endpoint.get("evict") or "never", memory_gb=endpoint.get("memory_gb"))
    view["agents"] = [a for a in AGENTS if not agent_problem(endpoint, a)]
    return view


def add(args, out=print, get=None):
    """sandbox-ai llm add: check everything that can be checked offline, copy the CA, then store."""
    if not NAME.fullmatch(args.name):
        raise ValueError(name_refusal(args.name))
    parsed = parse_url(args.url)
    if parsed["note"]:
        out(f"note: {parsed['note']}")
    store = load_store()
    if args.name in store["endpoints"] and not args.replace:
        raise ValueError(f"there is an LLM endpoint {args.name} already; remove it first or add --replace")
    headers = {}
    for value in args.header_secret:
        header, sep, secret = value.partition("=")
        if not sep or not HEADER.fullmatch(header) or not SECRET.fullmatch(secret):
            raise ValueError(f"--header-secret expects HEADER=SECRET (a header name, a secret's name): "
                             f"{printable(value)!r}")
        if header.lower() in RESERVED_HEADERS:
            raise ValueError(f"--header-secret: the proxy cannot set {header}")
        headers[header] = secret
    record = {"kind": args.kind, "url": parsed["url"], "api_key_secret": args.api_key_secret, "headers": headers,
              "vpn": args.vpn, "model": args.model, "anthropic": args.anthropic, "insecure_http": args.insecure_http}
    record = {key: value for key, value in record.items() if value not in (None, False, {})}
    problem, unresolved = plain_http_resolved(parsed)
    if unresolved:
        out(f"note: {parsed['host']} does not resolve here now; plain http is checked again at every start "
            "(local addresses only)")
    if problem and not args.insecure_http:
        raise ValueError(problem)
    if args.insecure_http:
        if parsed["scheme"] != "http":
            raise ValueError("--insecure-http is for plain http URLs")
        out("WARNING: plain http to a server outside your local networks: prompts, your code and the key cross "
            "every network on the way unencrypted, and anyone on the path can read or change them.")
    elif parsed["scheme"] == "http" and (args.api_key_secret or headers) and not parsed["loopback"]:
        out("note: plain http: the key crosses your local network unencrypted")
    check_record(args.name, record)  # every field as a start will check it
    if args.anthropic and args.kind != "openai-compatible":
        out(f"note: --anthropic is implied for {KINDS[args.kind]['label']} (since {ANTHROPIC_SINCE[args.kind]})")
    if parsed["loopback"]:
        out(f"note: on this host's loopback: sandboxes reach it with Podman only (--allow-host-port "
            f"{parsed['port']}), at 127.0.0.1, so the server must listen there")
    usual = KINDS[args.kind]["port"]
    if usual and usual != parsed["port"] and not re.match(r"https?://(\[[^]]*\]|[^/:]*):[0-9]", args.url):
        out(f"note: no port given, so {parsed['port']} is used; {KINDS[args.kind]['label']} listens on {usual} "
            "by default")
    get = get or load_secret_store_module().get
    for name in [args.api_key_secret, *headers.values()]:
        if name and get(name) is None:
            out(f"note: there is no secret {name} yet; keep it with: sandbox-ai secrets set {name}")
    old = store["endpoints"].get(args.name) if isinstance(store["endpoints"].get(args.name), dict) else {}
    if old.get("models") and old.get("url") == record.get("url") and old.get("kind") == record.get("kind"):
        # The same server at the same address: its model list stays until it is read again.
        record["models"], record["checked"] = old["models"], old.get("checked")
    add_params(args, record, old, out)  # refused here: the stored endpoint and its CA stay as they are
    if args.ca:
        source = Path(args.ca).expanduser()
        try:
            text = source.read_bytes()[:CA_LIMIT + 1]
        except OSError as exc:
            raise ValueError(f"--ca {source}: {exc.strerror}") from None
        if len(text) > CA_LIMIT:
            raise ValueError(f"--ca {source} is larger than {CA_LIMIT >> 10} KiB")
        try:
            text = text.decode("ascii")
        except UnicodeDecodeError:
            raise ValueError(f"--ca {source}: not PEM text") from None
        check_ca(text, source)
        if parsed["scheme"] != "https":
            raise ValueError("--ca is for https URLs")
        # A copy of our own: the agent may be able to change the original later, and the store names only files here.
        write_private(ca_dir() / f"{args.name}.pem", text.encode())
        record["ca"] = f"{args.name}.pem"
    elif old.get("ca") and not args.no_ca:
        # --replace without --ca keeps the CA that is stored: a front end cannot send the file again (the store
        # holds only a copy of it). --no-ca drops it.
        record["ca"] = old["ca"]
    elif args.name in store["endpoints"]:
        (ca_dir() / f"{args.name}.pem").unlink(missing_ok=True)
    store["endpoints"][args.name] = record
    save_store(store)
    for line in describe(check_record(args.name, record)):
        out(line)
    out(f"Kept in {store_path()}. Check it with: sandbox-ai llm check {args.name}")
    return 0


def add_params(args, record, old, out):
    """llm add: --param/--force-param (every model's) and --loop-guard into RECORD; with --replace the parameters and
    the loop guard stored (OLD) are kept, those the (new) kind does not take dropped with a note."""
    kind, params, forced = record["kind"], {}, {}
    for scope, values in (old.get("params") or {}).items() if isinstance(old.get("params"), dict) else ():
        if not isinstance(values, dict):
            continue
        kept = {}
        for key, value in values.items():
            try:
                kept[key] = param_value(kind, key, value)
            except ValueError:
                out(f"note: {printable(key)} of {printable(scope)} dropped: {KINDS[kind]['label']} does not take it")
        if kept:
            params[scope] = kept
            strong = (old.get("forced") or {}).get(scope) if isinstance(old.get("forced"), dict) else None
            forced[scope] = [k for k in strong or [] if k in kept]
    every, strong = params.get("*", {}), set(forced.get("*", []))
    for text, force in [(t, False) for t in getattr(args, "param", None) or []] + \
            [(t, True) for t in getattr(args, "force_param", None) or []]:
        key, value = parse_param(kind, text, "--force-param" if force else "--param")
        every[key] = value
        (strong.add if force else strong.discard)(key)
    if every:
        params["*"], forced["*"] = every, [key for key in every if key in strong]
    params = {scope: values for scope, values in params.items() if values}
    forced = {scope: keys for scope, keys in forced.items() if keys and scope in params}
    if params:
        record["params"] = params
    if forced:
        record["forced"] = forced
    kept = old.get("loop_guard") if isinstance(old.get("loop_guard"), dict) else {}
    guard = parse_loop(getattr(args, "loop_guard", None) or [], into=kept)
    if guard:
        record["loop_guard"] = guard
    evict = getattr(args, "evict", None) or old.get("evict")
    if evict == "allow":
        record["evict"] = "allow"
    memory = getattr(args, "memory", None)
    if memory is not None and memory != 0 and memory_value(memory) is None:
        raise ValueError(f"--memory: the server's memory in GB, 1 to {MEMORY_MAX_GB}")
    memory = memory_value(memory) if memory else None if memory == 0 else memory_value(old.get("memory_gb"))
    if memory:
        record["memory_gb"] = memory


def param_options(parser, every=False):
    """--param, --force-param and --loop-guard of llm add and llm set."""
    scope = "every model's" if every else "of the model (--model; every model without)"
    parser.add_argument("-p", "--param", action="append", default=[], metavar="KEY=VALUE",
                        help=f"a sampling parameter {scope}, set where the agent's request has none (sandbox-ai llm "
                             "params lists them; max_tokens is a ceiling); repeatable")
    parser.add_argument("-F", "--force-param", action="append", default=[], metavar="KEY=VALUE",
                        help="the same, but in place of the agent's own value too; repeatable")
    parser.add_argument("-g", "--loop-guard", action="append", default=[], metavar="on|off|LAYER=VALUE",
                        help="the loop guard of this server: on or off as a whole, or one layer (steps, compaction, "
                             "instructions, sampling, context, warn, block, failures, text, watchdog_requests, "
                             "watchdog_minutes; LAYER=default: as your defaults say); repeatable")
    parser.add_argument("-e", "--evict", choices=EVICT,
                        help="never (the default): a start keeps the other models of the server loaded where the "
                             "server lets it (LM Studio: the model is loaded explicitly, which Auto-Evict leaves "
                             "alone); allow: the server's own loading and unloading")
    parser.add_argument("-M", "--memory", type=float, metavar="GB",
                        help="the server's memory in GB (its RAM, or the GPU's VRAM), for what fits it: llm recommend "
                             "and --use-case (a server on this host's loopback: this host's own); 0 forgets it")


def show_profiles(args, out=print):
    """sandbox-ai llm profiles [MODEL] [--agent AGENT] [--json]: every family's profiles (its settings for every
    harness as "*"), or MODEL's; the settings a start applies under yours, the status and the evidence; for MODEL
    also what we know of each pair: works (✓), does not work (✗), not known yet (?)."""
    if args.model is not None and not MODEL_NAME.fullmatch(args.model):
        raise ValueError(f"not a model name: {printable(args.model)!r}")
    table = profile_table()
    if table is None:
        raise ValueError("the model profiles (model_profiles.json beside sandbox-ai) cannot be read")
    module = profiles_module()
    rows = module.rows(table, args.model, args.agent)
    # the traffic light of each family MODEL names: the harnesses it is green, yellow, red in
    support = None if args.model is None else \
        [module.family_support(family, args.model, table) for family in module.families_of(args.model, table)]
    # the MCP tools MODEL copes with (its class's budget: a start with more says so), None: no class known
    budget = module.tool_budget(args.model, table) if args.model and hasattr(module, "tool_budget") else None
    if args.json:
        out(json.dumps({"schema": table["schema"], "profiles": rows, "support": support,
                        **({"tool_budget": budget} if args.model else {})}, indent=2))
        return 0
    for found in support or []:
        out(printable(f"{found['label']}: {found['symbol']} {found['text']}"))
    if budget is not None:
        how = f"{budget['params']}B, from its name" if budget["from"] == "name" else f"its family {budget['family']}"
        budget_line = printable(f"MCP tools: about {budget['tools']} at most (a {budget['class']} model: {how}; a "
                                f"start with more says so) - {budget['evidence']}")
    if not rows:
        out(printable(f"no profile for {args.model}: nothing is known about this model (no family of the research "
                      "matches it); the agents' and the server's own settings apply" if args.model else "no profiles"))
        if budget is not None:
            out(budget_line)
        return 0
    if args.model and len({row["family"] for row in rows}) > 1:
        names = ", ".join(dict.fromkeys(row["family"] for row in rows))
        out(printable(f"{args.model} names several families ({names}): give a family's id to see just that one"))
    family_wide = {}  # the family's own warnings (its "*" row): not repeated under each of its pairs
    for row in rows:
        every = row["agent"] == "*"
        status = "every agent" if every and row["status"] == module.UNTESTED else row["status"]
        symbol = " " if status == "every agent" else row.get("symbol", " ")  # the traffic light's shape
        out(printable(f"{row['name']:<34} {symbol} {status:<11} {module.values_text(module.settings(row)) or '-'}"))
        if every:
            family_wide[row["family"]] = set(row["warnings"])
        details = [row["reason"] if every else row["evidence"],
                   *(text for text in row["warnings"] if text not in family_wide.get(row["family"], ()) or every)]
        if args.model:
            details += [f"reasoning: {row['reasoning']}" if row["reasoning"] else None, *row["notes"],
                        *(f"sandbox-ai sets itself: {text}" for text in row["builtin"])]
            if not row["pair"]:
                details.append("this pair was not tested: the model's settings for every agent apply")
        for line in filter(None, details):
            out(printable(f"    {line}"))
        known = module.knowledge_lines(row) if args.model and hasattr(module, "knowledge_lines") else []
        if known:  # what we know of one model: works (✓), does not work (✗), not known yet (?)
            out("    what we know:")
            for line in known:
                out(printable(f"      {line}"))
    if budget is not None:
        out(budget_line)
    out("A start with --llm NAME:MODEL applies its profile under your own values (llm set, --llm-param, --env); "
        "--no-llm-profile switches it off.")
    return 0


def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def main(argv, out=print, get=None):
    parser = argparse.ArgumentParser(prog="sandbox-ai llm", description=__doc__.split("\n\n")[0],
                                     formatter_class=help_style,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:5]))
    commands = parser.add_subparsers(dest="command", required=True)
    adding = commands.add_parser("add", help="add (or with --replace change) an endpoint")
    adding.add_argument("name")
    adding.add_argument("-k", "--kind", choices=list(KINDS), required=True, help="the kind of server")
    adding.add_argument("-u", "--url", required=True, help="http(s)://HOST[:PORT][/PREFIX], the server's root")
    adding.add_argument("-s", "--api-key-secret", metavar="SECRET",
                        help="the secret (sandbox-ai secrets) holding the API key; the proxy sends it as a Bearer "
                             "token (or in x-api-key, where the agent puts its key)")
    adding.add_argument("-H", "--header-secret", action="append", default=[], metavar="HEADER=SECRET",
                        help="a header the proxy adds with the value of SECRET, e.g. for a gateway; repeatable")
    adding.add_argument("-c", "--ca", metavar="FILE", help="PEM CA that signed the server's certificate (copied)")
    adding.add_argument("-v", "--vpn", metavar="PROFILE", help="the VPN profile the server is reached through")
    adding.add_argument("-m", "--model", help="the model a start uses when --llm names none")
    adding.add_argument("-a", "--anthropic", action="store_true",
                        help="openai-compatible: the server also offers the Anthropic Messages API (Claude Code)")
    adding.add_argument("-i", "--insecure-http", action="store_true",
                        help="allow plain http to an address outside your local networks (unencrypted)")
    adding.add_argument("-r", "--replace", action="store_true", help="replace an endpoint of the same name")
    adding.add_argument("--no-ca", action="store_true", help="with --replace: drop the CA it has (without --ca the "
                                                             "stored one is kept)")
    param_options(adding, every=True)
    setting = commands.add_parser("set", help="sampling parameters of a model and the loop guard",
                                         description="Sampling parameters of a model (every model without --model) "
                                                     "and the loop guard.")
    setting.add_argument("name")
    setting.add_argument("-m", "--model", help="the model whose parameters change (default: every model's)")
    param_options(setting)
    setting.add_argument("-U", "--unset", action="append", default=[], metavar="KEY",
                         help="drop a parameter of the model (or of every model); repeatable")
    setting.add_argument("-R", "--reset", action="store_true",
                         help="drop all parameters of the model (or of every model) before the others apply")
    catalogue = commands.add_parser("params", help="the sampling parameters, their ranges and the servers taking them",
                                              description="The sampling parameters, their ranges and the servers "
                                                          "that take them.")
    catalogue.add_argument("name", nargs="?", help="a server's name: the parameters its kind takes")
    catalogue.add_argument("-k", "--kind", choices=list(KINDS), help="the kind of server")
    catalogue.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    profiling = commands.add_parser("profiles", help="the model x harness profiles a start applies under your values",
                                                description="The model x harness profiles a start applies under "
                                                            "your own values (the local-model research's settings "
                                                            "and status, with its traffic light: ✓ verified, ~ partly "
                                                            "working, ✗ does not work); for MODEL first the agents "
                                                            "it is verified, partly working and not working in.")
    profiling.add_argument("model", nargs="?", help="a model's name: its profile (in every harness, or --agent's) "
                                                    "and what we know of it: what works, what does not, what was "
                                                    "not tested")
    profiling.add_argument("-a", "--agent", choices=list(AGENTS), help="only for this agent")
    profiling.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    recommending = commands.add_parser(
        "recommend", help="the best tested model and harness per use case, from the models your servers have",
        description="The best tested model and harness per use case of the local-model research (model_usecases.json:"
                    " coding, sandbox behaviour, web research, code review ...), chosen from the models your LLM "
                    "servers have now - what is loaded, what fits their memory (llm set NAME --memory GB; a server on "
                    "this host's loopback: this host's) - else the nearest, with why, the evidence, the settings and "
                    "the command that starts it; a use case not tested yet says so. One use case (USE_CASE or "
                    "--use-case) also lists every model of your servers as a pair - model x its best harness -, "
                    "best first: the table's, then those not measured for it, then those it marks unsafe (with the "
                    "reason: never chosen on their own); with --agent, where it is not the pair's harness, what is "
                    "known of the model in it.")
    recommending.add_argument("use_case_word", nargs="?", metavar="USE_CASE",
                              help="only this use case, in detail, with the pairs (model x harness) of the models "
                                   "your servers have, best first (the same as --use-case)")
    recommending.add_argument("-u", "--use-case", metavar="NAME", help="only this use case, in detail (default: every "
                                                                       "use case, one line each)")
    recommending.add_argument("-s", "--server", "--llm", dest="server", metavar="NAME",
                              help="only this LLM server (default: every one); --llm NAME is the same")
    recommending.add_argument("-a", "--agent", choices=list(AGENTS),
                              help="for this agent: the choices measured in it first")
    recommending.add_argument("-H", "--host", metavar="HOST", help="on a host of sandbox-ai hosts (over SSH)")
    recommending.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    listing = commands.add_parser("list", help="the endpoints")
    listing.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    showing = commands.add_parser("show", help="one endpoint")
    showing.add_argument("name")
    showing.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    commands.add_parser("remove", help="forget an endpoint (its secrets stay in sandbox-ai secrets)").add_argument("name")
    models = commands.add_parser("models", help="the models the server offers now, each with its traffic light",
                                 description="The models the server offers now. SUPPORT is the traffic light of "
                                             "sandbox-ai's local-model research (model_profiles.json): green ✓ "
                                             "verified, yellow ~ partly working, red ✗ does not work, grey ? not "
                                             "tested, with the agents of the model's best colour; --json has the "
                                             "details per agent (\"support\": what worked and failed, the "
                                             "recommended settings, the speed, the round and its date).")
    models.add_argument("name")
    models.add_argument("-s", "--sort", choices=["name", "size", "loaded"], default="loaded", help="the order of the models: name, size or loaded (default: loaded)")
    models.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    watching = commands.add_parser("status", help="what the server has loaded and uses now",
                                             description="What the server has loaded and uses now (VRAM/RAM, KV "
                                                         "cache, requests), as its API reports it; every server "
                                                         "without NAME.")
    watching.add_argument("name", nargs="?")
    watching.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    watching.add_argument("-w", "--watch", type=float, metavar="SECONDS",
                          help=f"read again every SECONDS (at least {WATCH_MIN}) until Ctrl-C; with --json one line "
                               "per reading")
    detecting = commands.add_parser("detect", help="look for servers on this host and at the addresses given",
                                              description="Look for servers on this host and at the addresses "
                                                          "given (read-only model listings, short timeouts).")
    detecting.add_argument("-A", "--address", action="append", default=[], metavar="IP",
                           help="a further address to look at (this host's loopback always; its LAN address "
                                "and other machines only when named); repeatable")
    detecting.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    loaded = commands.add_parser("loaded", help="the models the server has loaded now",
                                           description="The models the server has loaded now: instances, context, "
                                                       "memory, JIT or explicit, the running sandboxes using them.")
    loaded.add_argument("name")
    loaded.add_argument("-j", "--json", action="store_true", help="print the result as JSON (for programs)")
    loading = commands.add_parser("load", help="load a model explicitly (it stays until unloaded)",
                                          description="Load a model explicitly (it stays until unloaded); a JIT "
                                                      "instance (LM Studio) is replaced, never held twice.")
    loading.add_argument("name")
    loading.add_argument("model")
    loading.add_argument("-c", "--context", type=int, metavar="TOKENS",
                         help="the context it is loaded with (default: num_ctx of its parameters, llm set; "
                              "LM Studio and Ollama)")
    loading.add_argument("-t", "--ttl", type=int, metavar="SECONDS",
                         help="Ollama: unloaded after SECONDS idle (default: stays until unloaded)")
    loading.add_argument("-f", "--force", action="store_true",
                         help="reload a model a running sandbox uses all the same")
    unloading = commands.add_parser("unload", help="unload a model or one instance of it",
                                              description="Unload a model (every instance of it) or one instance; "
                                                          "refused while a running sandbox uses it, unless "
                                                          "--force.")
    unloading.add_argument("name")
    unloading.add_argument("model", metavar="MODEL|INSTANCE")
    unloading.add_argument("-f", "--force", action="store_true",
                           help="unload it although a running sandbox uses it")
    checking = commands.add_parser("check", help="reachability, TLS, key, models and the agents' APIs")
    checking.add_argument("name")
    checking.add_argument("-a", "--agent", choices=list(AGENTS), help="only for this agent")
    checking.add_argument("-m", "--model", help="check this model (default: the server's first)")
    traffic = load_traffic_module()
    commands.add_parser("traffic", help="what an agent and its model exchanged, request by request",
                        parents=[traffic.parser(add_help=False)], formatter_class=argparse.RawDescriptionHelpFormatter,
                        description=traffic.__doc__.split("\n\n")[0])
    args = parser.parse_args(argv)
    if args.command == "traffic":
        return traffic.main(argv[1:])
    try:
        if args.command == "add":
            with settings_lock(store_path()):  # from reading the store to writing it back (and the CA)
                return add(args, out, get)
        if args.command == "set":
            return set_params(args, out)
        if args.command == "params":
            server = endpoint(args.name) if args.name else None  # `llm params lms`: the kind of server lms
            if server is not None and args.kind and args.kind != server["kind"]:
                raise ValueError(f"{args.name} is {server['kind']}, not {args.kind}")
            found = params_catalogue(server["kind"] if server is not None else args.kind)
            if args.json:
                out(json.dumps(found, indent=2))
                return 0
            for entry in found:
                bounds = "true|false" if entry["type"] == "bool" else f"{entry['min']}..{entry['max']}"
                out(f"{entry['name']:<20} {bounds:<20} {entry['help']}")
                out(" " * 42 + "; ".join(f"{KINDS[k]['label']}: {where_sent(k, entry['name'], apis)}"
                                         for k, apis in entry["sent"].items()))
            kind = server["kind"] if server is not None else args.kind
            for item in server_only(kind) if kind else []:
                out(f"set on the server only: {item['setting']}: {item['where']}")
            if server is not None:
                out(f"\n{args.name}'s own values: sandbox-ai llm set {args.name} --param KEY=VALUE "
                    f"[--model MODEL] (shown by sandbox-ai llm show {args.name})")
            return 0
        if args.command == "profiles":
            return show_profiles(args, out)
        if args.command == "recommend":
            return recommend(args, argv, out, get)
        if args.command == "list":
            store = load_store()
            endpoints = []
            for name in sorted(store["endpoints"]):
                try:
                    endpoints.append(check_record(name, store["endpoints"][name]))
                except ValueError as exc:  # with --json on stderr: stdout stays one JSON array
                    note = printable(f"{name}: unusable ({exc})")
                    if args.json:
                        print(note, file=sys.stderr)
                    else:
                        out(note)
            if args.json:
                out(json.dumps([public_view(e) for e in endpoints], indent=2))
            for e in [] if args.json else endpoints:
                agents = ",".join(a for a in AGENTS if not agent_problem(e, a))
                out(printable(f"{e['name']}  {e['kind']}  {e['url']}  key={e['api_key_secret'] or '-'}"
                              f"{'  vpn=' + e['vpn'] if e['vpn'] else ''}  agents={agents}"))
            if not endpoints and not args.json:
                out("no LLM endpoints; add one with: sandbox-ai llm add NAME --kind KIND --url URL")
            return 0
        if args.command == "show":
            found = endpoint(args.name)
            for line in [json.dumps(public_view(found), indent=2)] if args.json else describe(found):
                out(line)
            return 0
        if args.command == "remove":
            with settings_lock(store_path()):
                store = load_store()
                if store["endpoints"].pop(args.name, None) is None:
                    out(f"{printable(args.name)} was not there")
                    return 1
                save_store(store)
                (ca_dir() / f"{args.name}.pem").unlink(missing_ok=True)
            out(f"{args.name} removed")
            return 0
        if args.command == "detect":
            addresses = detect_addresses(args.address)
            found = detect(addresses)
            if args.json:
                out(json.dumps(found, indent=2))
                return 0
            for entry in found:
                loaded = sum(1 for m in entry["models"] if m["loaded"])
                out(printable(f"{entry['kind']}  {entry['url']}  {len(entry['models'])} models"
                              f"{f', {loaded} loaded' if loaded else ''}"))
            if not found:
                out("no LLM server found (looked at " + ", ".join(addresses) + " on ports "
                    + ", ".join(str(port) for _, port in DETECT_PORTS) + ")")
            else:
                out("add one with: sandbox-ai llm add NAME --kind KIND --url URL")
            return 0
        if args.command == "models":
            chosen = endpoint(args.name)
            with through_vpn(chosen) as tunnel:
                client = VPNClient(chosen, tunnel, get) if tunnel else Client(chosen, get)
                found = sort_models(endpoint_models(chosen, get, client=client), args.sort)
                served_windows(chosen, found, client)  # Ollama: the window it serves each one with
            model_support(found)  # the traffic light: how far the research supports each model, per harness
            for line in [json.dumps(found, indent=2)] if args.json else model_table(found):
                out(line)
            return 0
        if args.command == "status":
            try:
                return show_status(args, out, get)
            except KeyboardInterrupt:
                return 0
        if args.command in ("loaded", "load", "unload"):
            return manage(args, out, get)
        found = endpoint(args.name)
        if args.model and not MODEL_NAME.fullmatch(args.model):
            raise ValueError(f"--model: not a model name: {printable(args.model)!r}")
        if found.get("vpn"):
            out(f"{found['name']}: bringing up the VPN profile {found['vpn']} for the check ...")
        with through_vpn(found) as tunnel:
            return 0 if check(found, args.agent, args.model, get, out, tunnel=tunnel) else 1
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai llm: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
