#!/usr/bin/env python3
"""sandbox-ai llm: LLM servers for the agents, local or remote, kept by name.

  sandbox-ai llm add NAME --kind KIND --url URL [--api-key-secret SECRET] [--header-secret HEADER=SECRET]
                     [--ca FILE] [--vpn PROFILE] [--model MODEL] [--anthropic] [--insecure-http] [--replace]
  sandbox-ai llm list [--json]
  sandbox-ai llm show NAME [--json]
  sandbox-ai llm remove NAME
  sandbox-ai llm models NAME [--sort name|size|loaded] [--json]
  sandbox-ai llm check NAME [--agent claude|codex|opencode] [--model MODEL]
  sandbox-ai llm status [NAME] [--json] [--watch SECONDS]
  sandbox-ai llm detect [--address IP]... [--json]
  sandbox-ai llm set NAME [--model MODEL] [--param KEY=VALUE]... [--force-param KEY=VALUE]... [--unset KEY]...
                     [--reset] [--loop-guard on|off|LAYER=VALUE]... [--evict never|allow]
  sandbox-ai llm params [--kind KIND] [--json]

KIND is ollama, lmstudio, llamacpp, vllm or openai-compatible. URL is the server's root, with a path
prefix if a gateway puts one in front (https://gw.example.org/ollama); each agent gets the API path it
needs. https is checked against the system's CAs (or --ca FILE); plain http goes only to the host's
loopback (127.0.0.1 or localhost, reached with --allow-host-port), private, CGNAT/Tailscale
(100.64.0.0/10) and unique-local IPv6 addresses, unless --insecure-http.

A start with --llm NAME[:MODEL] opens exactly that server's address and port and configures the agent
for it; --llm NAME:MODEL,MODEL... names several (the first is the agent's; OpenCode offers exactly these).
The key (a secret of sandbox-ai secrets, never stored here) is added by the connection proxy on the wire,
so the agent only ever sees a placeholder. Endpoints are kept in ~/.config/sandbox-ai/llm.json. An endpoint
added with --vpn is checked, listed and watched through that VPN (llm check|models|status).

llm status shows what each server has loaded and uses now, as far as its API reports it: Ollama the loaded models
with their VRAM and RAM, vLLM the KV cache and the requests running and waiting, llama.cpp its slots (and requests
with --metrics; memory in router mode), LM Studio the loaded models. The host's total RAM and VRAM are not reported by
these APIs (llama.cpp's router mode excepted).

Standard library only.
"""
import argparse
import base64
import contextlib
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


_filelock = None


def settings_lock(file):
    """The lock of a read-modify-write of the settings file FILE (filelock.py): commands that run at the same time
    never lose each other's change."""
    global _filelock
    if _filelock is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_filelock", HERE / "filelock.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _filelock = loaded
    return _filelock.locked(file)
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
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
             "models", "checked", "contexts", "params", "forced", "loop_guard", "evict"}
    if set(record) - known:
        raise ValueError(f"{where}: unknown fields {sorted(set(record) - known)}")
    if record.get("kind") not in KINDS:
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
    check_params(where, record["kind"], record.get("params", {}), record.get("forced", {}), record.get("loop_guard"))
    if record.get("evict", "never") not in EVICT:
        raise ValueError(f"{where}: evict is never or allow")
    if parsed["scheme"] == "http" and not record.get("insecure_http"):
        problem = plain_http_problem(parsed)
        if problem:
            raise ValueError(f"{where}: {problem}")
    return {"name": name, "kind": record["kind"], "url": parsed["url"], "api_key_secret": secret,
            "headers": dict(headers), "ca": ca, "vpn": vpn, "model": model,
            "anthropic": record.get("anthropic", False), "insecure_http": record.get("insecure_http", False),
            "models": list(models), "checked": record.get("checked"), "contexts": dict(contexts),
            "params": {scope: dict(values) for scope, values in record.get("params", {}).items()},
            "forced": {scope: list(keys) for scope, keys in record.get("forced", {}).items()},
            "loop_guard": dict(record.get("loop_guard") or {}), "evict": record.get("evict", "never"), "parsed": parsed}


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
                                              "params", "forced", "loop_guard", "evict")
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
        try:
            status, data = self.call(path, body)
        except (OSError, ssl.SSLError) as exc:
            raise ValueError(f"{self.endpoint['url']}{path}: {printable(getattr(exc, 'reason', exc))}") from None
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
                 "quantization": m.get("details", {}).get("quantization_level")}
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
                     "quantization": m.get("quantization"), "context": m.get("max_context_length")}
                    for m in data if m.get("type") in ("llm", "vlm")]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        if native:
            return [{"name": key, "size": m.get("size_bytes"), "loaded": bool(m.get("loaded_instances")),
                     "details": "", "type": m.get("type"), "arch": m.get("architecture"),
                     "parameters": m.get("params_string"), "quantization": lmstudio_quantization(m),
                     "context": m.get("max_context_length")}
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
                                                else "", m.get("created"))})
        return result
    if kind == "openai-compatible":
        return [{"name": m["id"], "size": None, "loaded": None, "details": with_date("", m.get("created"))}
                for m in fetch("/v1/models")["data"]]
    raise ValueError(f"Unknown server type: {kind}")


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
                                       "context": config.get("context_length")})
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
               ("CONTEXT", lambda m: f"{m['context'] // 1024}k" if m.get("context") else ""),
               ("SIZE", lambda m: format_size(m["size"]) if m["size"] is not None else ""),
               ("DETAILS", lambda m: m["details"] if m["details"] != (m.get("quantization") or "") else "")]
    shown = [(title, cell) for index, (title, cell) in enumerate(columns)
             if index == 0 or any(cell(m) for m in models)]
    rows = [["#", *[title for title, _ in shown]]]
    rows += [[str(i), *[cell(m) for _, cell in shown]] for i, m in enumerate(models, 1)]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = ["  ".join(v.ljust(width) for v, width in zip(row, widths)).rstrip() for row in rows]
    return lines if models else [lines[0], "(no models found)"]


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
                    {m["name"]: m["context"] for m in models if m.get("context")})
    return models


def remember_models(name, names, contexts=None):
    """The models NAME lists now, in its record (with CONTEXTS, {model: tokens}, the context windows known): read and
    written back under the store's lock, and only while NAME is still there (an `llm remove` meanwhile is never
    undone)."""
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
    except Exception:  # noqa: BLE001 - a server that cannot say leaves the agents' defaults
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
    "max_tokens": (int, 16, 1 << 20, "the most tokens of one answer: a ceiling, the agent's own lower value stays"),
    "seed": (int, 0, (1 << 31) - 1, "a fixed seed (reproducible answers)"),
    "num_ctx": (int, CONTEXT_RANGE[0], CONTEXT_RANGE[1], "the context window (tokens) the agents are told; the "
                                                         "server must load the model with at least this much"),
    "cache_prompt": (bool, None, None, "llama.cpp: reuse the KV cache of the previous request (default on)"),
}
# Sent on each API (chat: /v1/chat/completions, completions: /v1/completions, responses: /v1/responses, messages:
# Anthropic's /v1/messages) to a server of each kind; the rest of a kind's own keys only where named.
_RESPONSES = ("temperature", "top_p", "max_tokens")   # max_tokens as max_output_tokens
_MESSAGES = ("temperature", "top_p", "top_k", "max_tokens")
_LLAMACPP = STANDARD_FIELDS + ("top_k", "min_p", "repeat_penalty", "repeat_last_n", "dry_multiplier", "cache_prompt")
SENT = {
    "ollama": {"chat": STANDARD_FIELDS, "completions": STANDARD_FIELDS, "responses": _RESPONSES,
               "messages": _MESSAGES},
    "lmstudio": {"chat": STANDARD_FIELDS + ("top_k", "repeat_penalty"),
                 "completions": STANDARD_FIELDS + ("top_k", "repeat_penalty"), "responses": _RESPONSES,
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
#                two thirds); at the last one OpenCode asks for a text answer (agent.<name>.steps; 0: OpenCode's own)
#   compaction   OpenCode: automatic compaction with pruning of old tool output and room for it, tool output cut at
#                1000 lines / 50 KB (compaction, tool_output)
#   instructions OpenCode: rules against loops as an instruction file of its own (instructions; your AGENTS.md stays)
#   sampling     the model family's recommended sampling as the lowest default (Qwen3, QwQ, Qwen3-Coder,
#                DeepSeek-R1, gpt-oss), never greedy for those that warn against it
#   context      warn when the agents are told a larger context window than the server loaded
#   warn, block  the proxy, on each request: the same tool call with the same result WARN times since the human's last
#                message: a note in that result tells the model so; BLOCK times: tool use is paused for that answer (the
#                model summarises; the human goes on). Polling (sleep, --watch, wait) counts against three times both.
#   text         the proxy, on a streamed answer: one block repeated TEXT times in a row (4 KiB at least): the answer
#                ends cleanly with a note, and the next request gets stronger anti-repetition sampling
#   watchdog_requests, watchdog_minutes   for unattended runs: tool use paused after this many model requests or
#                minutes in one turn (0: off, the default)
LOOP_DEFAULTS = {"enabled": True, "steps": 40, "compaction": True, "instructions": True, "sampling": True,
                 "context": True, "warn": 3, "block": 6, "text": 10, "watchdog_requests": 0, "watchdog_minutes": 0}
LOOP_RANGES = {"steps": (5, 1000), "warn": (2, 100), "block": (3, 200), "text": (3, 100),
               "watchdog_requests": (5, 100000), "watchdog_minutes": (1, 10080)}  # 0 is off besides these
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
    """The parameter names a server of KIND takes (num_ctx always: it goes to the agents' configurations)."""
    names = {"num_ctx", *MODELFILE_ONLY.get(kind, ())}
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
    if number != number or number in (float("inf"), float("-inf")) or not low <= number <= high:
        raise ValueError(f"{where} {key}: from {low} to {high}, not {printable(value)}")
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
    except (OSError, ValueError, AttributeError):
        return {}


def loop_config(endpoint, start=None, user=None):
    """The loop guard as it applies: sandbox-ai's defaults, yours (USER, else defaults.json), the endpoint's, the
    start's (START) over each other; every layer off when "enabled" is off."""
    found = {**LOOP_DEFAULTS, **(user_loop() if user is None else user), **(endpoint.get("loop_guard") or {}),
             **(start or {})}
    if not found["enabled"]:
        found.update({key: False if isinstance(value, bool) else 0 for key, value in LOOP_DEFAULTS.items()})
    return found


def family_preset(model):
    """The sampling MODEL's family recommends ({} for others)."""
    for pattern, values in FAMILY_PRESETS:
        if pattern.search(model or ""):
            return dict(values)
    return {}


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


def effective_params(endpoint, model, override=None):
    """({key: value}, {forced keys}) for MODEL: the endpoint's every-model values, its model's over them, the start's
    OVERRIDE (--llm-param: forced) over both."""
    params, forced = endpoint.get("params") or {}, endpoint.get("forced") or {}
    values, strong = {}, set()
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
    lines.append("  other models: " + ("the server may unload them (evict allow)" if endpoint.get("evict") == "allow"
                                       else "kept loaded where the server lets it (evict never)"))
    return lines


def proxy_params(a, endpoint, model):
    """proxy.json's parameters of the --llm endpoint: what runtime.llm_request applies (the kind, every model's and
    each model's values, the forced ones, the start's override) and the loop guard."""
    override, start = start_params(a, endpoint)
    loop = loop_config(endpoint, start)
    names = list(dict.fromkeys([model, *(selected_models(a) or endpoint.get("models", []))]))[:500]
    presets = {name: family_preset(name) for name in names} if loop["sampling"] else {}
    return {"kind": endpoint["kind"], "params": endpoint.get("params") or {}, "forced": endpoint.get("forced") or {},
            "override": override, "sent": {api: list(keys) for api, keys in SENT[endpoint["kind"]].items()},
            "nested": {api: list(keys) for api, keys in NESTED.get(endpoint["kind"], {}).items()},
            "presets": {name: values for name, values in presets.items() if values}, "loop": loop}


def agent_context(endpoint, model, a=None, reported=None, loaded=None):
    """The context window the agents are told for MODEL: num_ctx of its parameters (the start's first), else the
    window the server has the model LOADED with, else what it reports (REPORTED). With the loop guard's context layer:
    a warning when the agent would be told more than the server keeps (it cuts the conversation then)."""
    start = start_params(a, endpoint) if a is not None else ({}, {})
    values, _ = effective_params(endpoint, model, start[0])
    chosen = values.get("num_ctx")
    told = chosen or loaded or reported
    keeps = loaded or (reported if chosen else None)
    if told and keeps and told > keeps and loop_config(endpoint, start[1])["context"]:
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


def set_params(args, out=print):
    """sandbox-ai llm set NAME [--model MODEL] [--param K=V]... [--force-param K=V]... [--unset K]... [--reset]
    [--loop-guard on|off|N]: the parameters of one model (every model without --model), under the store's lock."""
    if not (args.param or args.force_param or args.unset or args.reset or args.loop_guard or args.evict):
        raise ValueError("llm set: nothing to change (--param, --force-param, --unset, --reset, --loop-guard or "
                         "--evict)")
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
        found = check_record(args.name, record)
        save_store(store)
    for line in describe(found):
        out(line)
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


def keep_loaded(endpoint, model, a=None, get=None, client=None, behind_vpn=False):
    """evict never (the default): LM Studio loads MODEL explicitly, unless an instance of it is loaded, so that its
    Auto-Evict never unloads it for another sandbox's model. Returns a note, or None; never raises (a model that does
    not load is loaded by the agent's first request, as before). Not from this host for a server behind a VPN."""
    if endpoint["kind"] != "lmstudio" or endpoint.get("evict") == "allow" or endpoint.get("vpn") or behind_vpn or \
            os.environ.get("SANDBOX_AI_LLM_NO_CONTEXT_FETCH") == "1":
        return None
    try:
        client = client or Client(endpoint, get, timeout=LOAD_TIMEOUT)
        native = lmstudio_native(client.json)
        if not native:
            return f"LM Studio's /api/v1/models (0.4 or later) does not answer: {model} is left to JIT loading"
        found = native.get(model)
        if found is None:
            return None  # an instance id or a model LM Studio does not list: its own JIT loading
        if found.get("loaded_instances"):
            return None
        body = {"model": model}
        values = effective_params(endpoint, model, start_params(a, endpoint)[0] if a is not None else None)[0]
        if values.get("num_ctx"):
            body["context_length"] = values["num_ctx"]
        status, data = client.call("/api/v1/models/load", body)
        if status >= 300:
            return f"LM Studio did not load {model} (HTTP {status}): left to JIT loading"
        return f"LM Studio loaded {model} for this start (not JIT: Auto-Evict leaves it loaded; unload it in LM " \
               "Studio or with lms unload)"
    except Exception as exc:  # noqa: BLE001 - never fatal
        return f"LM Studio: {model} not loaded ahead ({printable(exc)[:200]}): left to JIT loading"


def where_sent(kind, key, apis):
    """Where KEY goes for a server of KIND, in words."""
    if apis:
        return ", ".join(apis)
    return "Modelfile only" if key in MODELFILE_ONLY.get(kind, ()) else "the agents' configuration only"


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
                      "ceiling": key == "max_tokens", "never_sent": key == "num_ctx"})
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
    model = model or found["model"] or (found["models"][0] if len(found["models"]) == 1 else None)
    if not model:
        listed = f"; listed: {', '.join(found['models'][:10])}" if found["models"] else \
            f"; see: sandbox-ai llm models {name}"
        raise ValueError(f"--llm {name}: name a model as --llm {name}:MODEL (or add the endpoint with --model)"
                         + listed)
    a._llm = (a.llm, (found, model))
    return found, model


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
    taken = set(agent_environment(a)) & (set(a.env) | {value.partition("=")[0] for value in a.secret_env})
    if taken:
        raise ValueError(f"--llm sets {', '.join(sorted(taken))} for the agent; drop it from --env/--secret-env")
    secret_values(found)  # missing or malformed secrets stop the start here, before anything runs
    override, start = start_params(a, found)  # a parameter the kind does not take stops the start here
    values = effective_params(found, model, override)[0]
    loop = loop_config(found, start)
    if (any(key != "num_ctx" for key in values) or loop["warn"] or loop["block"] or loop["text"]) and \
            (not proxy_enabled or any(host_matches(parsed["host"], pattern) for pattern in a.no_inspect)):
        print(f"agent-sandbox: note: --llm {found['name']}: the sampling parameters and the loop guard are applied by "
              "the connection proxy, which does not see this server's requests here (--no-proxy or --no-inspect): "
              "only num_ctx and max_tokens reach the agent's configuration", file=sys.stderr)
    hint = modelfile_hint(found, model, values)
    if hint:
        print(printable(f"agent-sandbox: note: --llm {found['name']}: {hint}"), file=sys.stderr)
    if a.agent == "claude" and "/" in model:
        print(f"agent-sandbox: note: Claude Code may not accept the model name {model} (it contains '/'); "
              "name it without one on the server (vLLM: --served-model-name)", file=sys.stderr)
    if not parsed["address"] and not parsed["loopback"] and not within_ceiling(a, host=parsed["host"]):
        raise ValueError(f"--llm {found['name']}: {parsed['host']} is not among the hosts the administrator's "
                         "policy allows (allow_hosts)")
    if found["insecure_http"]:
        print(f"agent-sandbox: WARNING: --llm {found['name']} uses plain http to a server outside your local "
              "networks (--insecure-http): prompts, code and the key cross the network unencrypted",
              file=sys.stderr)
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


def proxy_entry(a, get=None):
    """proxy.json's "llm": where the proxy swaps the placeholder for the key (and adds the header secrets), and the
    endpoint's own CA. The values live only there, in the proxy's directory the agent cannot read."""
    found, model = selection(a)
    parsed = found["parsed"]
    key, headers = secret_values(found, get)
    return [{"name": found["name"], "scheme": parsed["scheme"], "host": parsed["host"], "port": parsed["port"],
             "addresses": list(getattr(a, "llm_addresses", []) or ([parsed["address"]] if parsed["address"] else [])),
             "prefix": parsed["prefix"], "placeholder": PLACEHOLDER, "key": key,
             "headers": [[name, value] for name, value in headers], "ca": ca_text(found),
             **proxy_params(a, found, model)}]


def opencode_config(endpoint, model, only=False, allowed=None, contexts=None, outputs=None, loop=None):
    """OpenCode's configuration (the v1 shape, which OpenCode 2 reads too): an OpenAI-compatible provider
    (@ai-sdk/openai-compatible, Chat Completions) with the endpoint's models, or exactly ALLOWED (the models
    --llm NAME:MODEL,... names: no other one is offered). ONLY (--no-provider-defaults): no other provider is
    offered. CONTEXTS ({model: tokens}): limit.context and limit.output of the models whose window is known;
    OUTPUTS ({model: tokens}, their max_tokens): limit.output at most that. LOOP (loop_config): its OpenCode layers -
    steps (agent.<name>.steps), compaction (compaction, tool_output), instructions (the loop guard's file)."""
    pid = provider_id(endpoint)
    names = list(dict.fromkeys([model, *(allowed if allowed else endpoint.get("models", []))]))

    def entry(name):
        context = context_value((contexts or {}).get(name))
        if not context:
            return {"name": name}
        output = output_limit(context)
        ceiling = (outputs or {}).get(name)
        return {"name": name, "limit": {"context": context, "output": min(output, ceiling) if ceiling else output}}
    config = {"$schema": "https://opencode.ai/config.json",
              "provider": {pid: {"npm": "@ai-sdk/openai-compatible",
                                 "name": f"{endpoint['name']} ({KINDS[endpoint['kind']]['label']})",
                                 "options": {"baseURL": agent_url(endpoint), "apiKey": PLACEHOLDER},
                                 "models": {name: entry(name) for name in names}}},
              "model": f"{pid}/{model}", "small_model": f"{pid}/{model}", "share": "disabled"}
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
        config["compaction"] = {"auto": True, "prune": True,
                                **({"reserved": max(2048, context // 5)} if context else {})}
        config["tool_output"] = {"max_lines": 1000, "max_bytes": 50000}
    if loop.get("instructions"):
        config["instructions"] = ["/root/" + LOOP_INSTRUCTIONS]
    return config


def claude_environment(endpoint, model):
    """Claude Code against an Anthropic-compatible server: the placeholder as ANTHROPIC_AUTH_TOKEN (a Bearer token,
    which all four servers accept; vLLM accepts no other), every model slot on MODEL, and no traffic to Anthropic
    beyond what the network options allow anyway."""
    return {"ANTHROPIC_BASE_URL": agent_url(endpoint, "anthropic"), "ANTHROPIC_AUTH_TOKEN": PLACEHOLDER,
            # Empty: a key here would make Claude ask whether to use it (and send x-api-key).
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": model, "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model, "CLAUDE_CODE_SUBAGENT_MODEL": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            # The attribution block changes per request and defeats the servers' prompt caching.
            "CLAUDE_CODE_ATTRIBUTION_HEADER": "0"}


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


def codex_profile(endpoint, model, context=None):
    """~/.codex/sandbox-ai-llm.config.toml: a provider with the Responses API (Codex has no other since 0.95.0),
    the key from an environment variable that holds only the placeholder, and none of Codex's own calls home
    (update check, analytics, metrics, plugin sync, the hosted web search a local server does not have). CONTEXT:
    the model's context window as the server reports it (model_context_window), when known."""
    pid = provider_id(endpoint)
    context = context_value(context)
    return "\n".join([
        f"# Written by sandbox-ai for --llm {endpoint['name']} at every start (codex --profile {CODEX_PROFILE}).",
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


def agent_environment(a):
    """The agent's variables for --llm: placeholders and addresses, nothing secret."""
    found, model = selection(a)
    if a.agent == "claude":
        return claude_environment(found, model)
    if a.agent == "codex":
        return {KEY_VARIABLE: PLACEHOLDER}
    env = {"OPENCODE_CONFIG": "/root/" + OPENCODE_CONFIG, "OPENCODE_DISABLE_MODELS_FETCH": "1",
           "OPENCODE_DISABLE_AUTOUPDATE": "1"}
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


def agent_arguments(a):
    """Arguments put before the agent's own: Codex selects the profile written by prepare_home."""
    return ["--profile", CODEX_PROFILE] if a.agent == "codex" else []


def prepare_home(a, home, replace_private, update_json_defaults, state_limit):
    """Write the agent's configuration for the endpoint into the sandbox HOME with the launcher's helpers, which never
    follow a symlink the agent planted. Returns the HOME-relative paths written."""
    found, model = selection(a)
    note = keep_loaded(found, model, a, behind_vpn=bool(getattr(a, "vpn", None)))
    if note:
        print(printable(f"agent-sandbox: --llm {found['name']}: {note}"), file=sys.stderr)
    if a.agent == "opencode":
        contexts, outputs = {}, {}
        names = list(dict.fromkeys([model, *(selected_models(a) or found.get("models", []))]))
        override = start_params(a, found)[0]
        for name in names:
            values = effective_params(found, name, override)[0]
            reported = context_value((found.get("contexts") or {}).get(name))
            contexts[name] = values.get("num_ctx") or reported
            if values.get("max_tokens"):
                outputs[name] = values["max_tokens"]
        behind = bool(getattr(a, "vpn", None))
        contexts[model] = agent_context(found, model, a, context_window(found, model, behind_vpn=behind),
                                        loaded_context(found, model, behind_vpn=behind))
        loop = loop_guard_of(a, found)
        config = opencode_config(found, model, only=a.no_provider_defaults, allowed=selected_models(a) or None,
                                 contexts=contexts, outputs=outputs, loop=loop)
        replace_private((json.dumps(config, indent=2) + "\n").encode(), home, OPENCODE_CONFIG)
        if loop["instructions"]:
            replace_private(LOOP_TEXT.encode(), home, LOOP_INSTRUCTIONS)
            return [OPENCODE_CONFIG, LOOP_INSTRUCTIONS]
        return [OPENCODE_CONFIG]
    if a.agent == "codex":
        relative = f".codex/{CODEX_PROFILE}.config.toml"
        behind = bool(getattr(a, "vpn", None))
        context = agent_context(found, model, a, context_window(found, model, behind_vpn=behind),
                                loaded_context(found, model, behind_vpn=behind))
        replace_private(codex_profile(found, model, context).encode(), home, relative)
        return [relative]
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
    config = {"opencode": "/root/" + OPENCODE_CONFIG, "codex": f"/root/.codex/{CODEX_PROFILE}.config.toml",
              "claude": "environment (ANTHROPIC_BASE_URL ...)"}[a.agent]
    return {"name": found["name"], "kind": found["kind"], "url": found["url"], "model": model,
            # OpenCode offers exactly these (the models --llm names); none named: the endpoint's model and list.
            "models": selected_models(a) or None,
            "agent_url": agent_url(found, "anthropic" if a.agent == "claude" else "openai"),
            "api": {"claude": "anthropic-messages", "codex": "openai-responses", "opencode": "openai-chat"}[a.agent],
            "opens": opens, "path_prefix": parsed["prefix"] or None, "http_rule": prefix_rule(a, found),
            "config": config,
            "key_secret": found["api_key_secret"], "header_secrets": found["headers"],
            "ca": str(ca_dir() / found["ca"]) if found["ca"] else None, "vpn": found["vpn"],
            "placeholder": PLACEHOLDER, "insecure_http": found["insecure_http"],
            "params": plan_params(a, found, model)}


def plan_params(a, endpoint, model):
    """The dry run's "params": what the proxy sets for MODEL (forced: in place of the agent's own), what it never
    sends to this kind, and the loop guard."""
    override, start = start_params(a, endpoint)
    values, forced = effective_params(endpoint, model, override)
    sent = set().union(*SENT[endpoint["kind"]].values())
    loop = loop_config(endpoint, start)
    return {"values": values, "forced": sorted(forced & set(values)),
            "not_sent": sorted(key for key in values if key not in sent),
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
            ("opencode", "/v1/chat/completions", "the OpenAI Chat Completions API (OpenCode)")]
    found_messages = False
    for name, path, label in apis:
        if agent and agent != name:
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
                broken.append({"name": printable(key)[:32], "kind": record.get("kind") if record.get("kind") in KINDS
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
            time.sleep(args.watch)
        except KeyboardInterrupt:
            return 0


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
    lines.append("  agents: " + ", ".join(a for a in ("claude", "codex", "opencode") if not agent_problem(endpoint, a)))
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
                evict=endpoint.get("evict") or "never")
    view["agents"] = [a for a in ("claude", "codex", "opencode") if not agent_problem(endpoint, a)]
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
    add_params(args, record, old, out)
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
                             "instructions, sampling, context, warn, block, text, watchdog_requests, "
                             "watchdog_minutes; LAYER=default: as your defaults say); repeatable")
    parser.add_argument("-e", "--evict", choices=EVICT,
                        help="never (the default): a start keeps the other models of the server loaded where the "
                             "server lets it (LM Studio: the model is loaded explicitly, which Auto-Evict leaves "
                             "alone); allow: the server's own loading and unloading")


def main(argv, out=print, get=None):
    parser = argparse.ArgumentParser(prog="sandbox-ai llm", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:5]))
    commands = parser.add_subparsers(dest="command", required=True)
    adding = commands.add_parser("add", help="add (or with --replace change) an endpoint")
    adding.add_argument("name")
    adding.add_argument("-k", "--kind", choices=list(KINDS), required=True)
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
    setting = commands.add_parser("set", help="sampling parameters of a model (every model without --model) and "
                                              "the loop guard")
    setting.add_argument("name")
    setting.add_argument("-m", "--model", help="the model whose parameters change (default: every model's)")
    param_options(setting)
    setting.add_argument("-U", "--unset", action="append", default=[], metavar="KEY",
                         help="drop a parameter of the model (or of every model); repeatable")
    setting.add_argument("-R", "--reset", action="store_true",
                         help="drop all parameters of the model (or of every model) before the others apply")
    catalogue = commands.add_parser("params", help="the sampling parameters, their ranges and the servers that take "
                                                   "them")
    catalogue.add_argument("-k", "--kind", choices=list(KINDS))
    catalogue.add_argument("-j", "--json", action="store_true")
    listing = commands.add_parser("list", help="the endpoints")
    listing.add_argument("-j", "--json", action="store_true")
    showing = commands.add_parser("show", help="one endpoint")
    showing.add_argument("name")
    showing.add_argument("-j", "--json", action="store_true")
    commands.add_parser("remove", help="forget an endpoint (its secrets stay in sandbox-ai secrets)").add_argument("name")
    models = commands.add_parser("models", help="the models the server offers now")
    models.add_argument("name")
    models.add_argument("-s", "--sort", choices=["name", "size", "loaded"], default="loaded")
    models.add_argument("-j", "--json", action="store_true")
    watching = commands.add_parser("status", help="what the server has loaded and uses now (VRAM/RAM, KV cache, "
                                                  "requests), as its API reports it; every server without NAME")
    watching.add_argument("name", nargs="?")
    watching.add_argument("-j", "--json", action="store_true")
    watching.add_argument("-w", "--watch", type=float, metavar="SECONDS",
                          help=f"read again every SECONDS (at least {WATCH_MIN}) until Ctrl-C; with --json one line "
                               "per reading")
    detecting = commands.add_parser("detect", help="look for servers on this host and at the addresses given "
                                    "(read-only model listings, short timeouts)")
    detecting.add_argument("-A", "--address", action="append", default=[], metavar="IP",
                           help="a further address to look at (this host's loopback always; its LAN address "
                                "and other machines only when named); repeatable")
    detecting.add_argument("-j", "--json", action="store_true")
    checking = commands.add_parser("check", help="reachability, TLS, key, models and the agents' APIs")
    checking.add_argument("name")
    checking.add_argument("-a", "--agent", choices=["claude", "codex", "opencode"])
    checking.add_argument("-m", "--model")
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            with settings_lock(store_path()):  # from reading the store to writing it back (and the CA)
                return add(args, out, get)
        if args.command == "set":
            return set_params(args, out)
        if args.command == "params":
            found = params_catalogue(args.kind)
            if args.json:
                out(json.dumps(found, indent=2))
                return 0
            for entry in found:
                bounds = "true|false" if entry["type"] == "bool" else f"{entry['min']}..{entry['max']}"
                out(f"{entry['name']:<20} {bounds:<20} {entry['help']}")
                out(" " * 42 + "; ".join(f"{KINDS[k]['label']}: {where_sent(k, entry['name'], apis)}"
                                         for k, apis in entry["sent"].items()))
            return 0
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
                agents = ",".join(a for a in ("claude", "codex", "opencode") if not agent_problem(e, a))
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
                found = sort_models(endpoint_models(chosen, get, client=VPNClient(chosen, tunnel, get) if tunnel
                                                    else None), args.sort)
            for line in [json.dumps(found, indent=2)] if args.json else model_table(found):
                out(line)
            return 0
        if args.command == "status":
            try:
                return show_status(args, out, get)
            except KeyboardInterrupt:
                return 0
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
