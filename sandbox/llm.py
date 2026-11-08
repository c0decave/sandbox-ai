#!/usr/bin/env python3
"""sandbox-ai llm: LLM servers for the agents, local or remote, kept by name.

  sandbox-ai llm add NAME --kind KIND --url URL [--api-key-secret SECRET] [--header-secret HEADER=SECRET]
                     [--ca FILE] [--vpn PROFILE] [--model MODEL] [--anthropic] [--insecure-http] [--replace]
  sandbox-ai llm list [--json]
  sandbox-ai llm show NAME [--json]
  sandbox-ai llm remove NAME
  sandbox-ai llm models NAME [--sort name|size|loaded] [--json]
  sandbox-ai llm check NAME [--agent claude|codex|opencode] [--model MODEL]

KIND is ollama, lmstudio, llamacpp, vllm or openai-compatible. URL is the server's root, with a path
prefix if a gateway puts one in front (https://gw.example.org/ollama); each agent gets the API path it
needs. https is checked against the system's CAs (or --ca FILE); plain http goes only to the host's
loopback (127.0.0.1 or localhost, reached with --allow-host-port), private, CGNAT/Tailscale
(100.64.0.0/10) and unique-local IPv6 addresses, unless --insecure-http.

A start with --llm NAME[:MODEL] opens exactly that server's address and port and configures the agent
for it. The key (a secret of sandbox-ai secrets, never stored here) is added by the connection proxy on
the wire, so the agent only ever sees a placeholder. Endpoints are kept in ~/.config/sandbox-ai/llm.json.

Standard library only.
"""
import argparse
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
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
             "models", "checked"}
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
    if parsed["scheme"] == "http" and not record.get("insecure_http"):
        problem = plain_http_problem(parsed)
        if problem:
            raise ValueError(f"{where}: {problem}")
    return {"name": name, "kind": record["kind"], "url": parsed["url"], "api_key_secret": secret,
            "headers": dict(headers), "ca": ca, "vpn": vpn, "model": model,
            "anthropic": record.get("anthropic", False), "insecure_http": record.get("insecure_http", False),
            "models": list(models), "checked": record.get("checked"), "parsed": parsed}


def endpoint(name, path=None):
    """The checked endpoint NAME; ValueError if there is none."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError(f"an LLM endpoint's name is 1-32 letters, digits, _ or - (no ':'): {printable(name)!r}")
    store = load_store(path)
    if name not in store["endpoints"]:
        known = ", ".join(sorted(store["endpoints"])) or "none yet"
        raise ValueError(f"no LLM endpoint {name} (known: {known}); add one with: sandbox-ai llm add {name} "
                         "--kind KIND --url URL")
    return check_record(name, store["endpoints"][name])


def record_of(endpoint):
    """The stored form of a checked endpoint."""
    record = {key: endpoint[key] for key in ("kind", "url", "api_key_secret", "headers", "ca", "vpn", "model",
                                              "anthropic", "insecure_http", "models", "checked")}
    return {key: value for key, value in record.items() if value not in (None, False, {}, [])}


def ca_text(endpoint):
    """The PEM text of the endpoint's own CA (copied into ca_dir() by `llm add`), or None."""
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

class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the key to wherever the server points; never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to {printable(newurl)[:200]} not followed",
                                     headers, fp)


def tls_context(endpoint):
    context = ssl.create_default_context()
    ca = ca_text(endpoint)
    if ca:
        context.load_verify_locations(cadata=ca)
    return context


class Client:
    """Requests to one endpoint from the host, with its key and headers, its CA, no proxy, no redirects."""

    def __init__(self, endpoint, get=None, timeout=TIMEOUT, opener=None):
        self.endpoint, self.timeout = endpoint, timeout
        key, headers = secret_values(endpoint, get)
        self.headers = {"Accept": "application/json", "User-Agent": "sandbox-ai"}
        if key:
            self.headers["Authorization"] = f"Bearer {key}"
        self.headers.update(dict(headers))
        context = tls_context(endpoint) if endpoint["parsed"]["scheme"] == "https" else None
        # A remote server must not be reached through the host's HTTP proxy variables.
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                            urllib.request.HTTPSHandler(context=context))

    def call(self, path, body=None):
        """(status, bytes) of GET PATH (POST with BODY, a dict) below the endpoint's root; OSError when there is no
        answer at all."""
        data = None if body is None else json.dumps(body).encode()
        headers = dict(self.headers, **({"Content-Type": "application/json"} if data is not None else {}))
        request = urllib.request.Request(self.endpoint["url"] + path, data=data, headers=headers,
                                         method="GET" if data is None else "POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read(RESPONSE_LIMIT + 1)[:RESPONSE_LIMIT]
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read(65536) if exc.fp else b""

    def json(self, path):
        """The JSON answer of GET PATH; ValueError says what went wrong (never with a secret)."""
        try:
            status, data = self.call(path)
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
            return json.loads(data)
        except (ValueError, RecursionError):
            raise ValueError(f"{self.endpoint['url']}{path}: the answer is not JSON") from None


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
                                      m.get("modified_at"))}
                for m in tags]
    if kind == "lmstudio":
        try:
            data = fetch("/api/v0/models")["data"]
            return [{"name": m["id"], "size": m.get("size_bytes"), "loaded": m.get("state") == "loaded",
                     "details": m.get("quantization", "")} for m in data if m.get("type") in ("llm", "vlm")]
        except (OSError, ValueError, KeyError, TypeError):
            # Older LM Studio, or one behind a gateway: the OpenAI-compatible listing only.
            return [{"name": m["id"], "size": None, "loaded": None, "details": with_date("", m.get("created"))}
                    for m in fetch("/v1/models")["data"]]
    if kind == "vllm":
        # vLLM serves exactly the models it has loaded.
        return [{"name": m["id"], "size": None, "loaded": True,
                 "details": with_date(f"max_model_len {m['max_model_len']}" if m.get("max_model_len") else "",
                                      m.get("created"))}
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
        result.append({"name": name,
                       "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None,
                       "loaded": loaded if isinstance(loaded, bool) else None,
                       "details": printable(details)[:80] if isinstance(details, str) else ""})
    return result


def discover(kind, fetch):
    """Models of a server, checked (see clean_models); ValueError when the listing fails."""
    try:
        return clean_models(discover_raw(kind, fetch))
    except (OSError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"unexpected answer from the {KINDS[kind]['label']}: {printable(exc)}") from None


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


def model_table(models):
    rows = [["#", "NAME", "SIZE", "LOADED", "DETAILS"]]
    rows += [[str(i), m["name"], format_size(m["size"]), {True: "yes", False: "no", None: "?"}[m["loaded"]],
              m["details"]] for i, m in enumerate(models, 1)]
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = ["  ".join(v.ljust(width) for v, width in zip(row, widths)).rstrip() for row in rows]
    return lines if models else [lines[0], "(no models found)"]


def endpoint_models(endpoint, get=None, client=None):
    """The models the endpoint lists now, checked; remembered in the store for later starts."""
    client = client or Client(endpoint, get)
    models = discover(endpoint["kind"], client.json)
    remember_models(endpoint["name"], [m["name"] for m in models])
    return models


def remember_models(name, names):
    store = load_store()
    record = store["endpoints"].get(name)
    if isinstance(record, dict):
        record["models"] = names[:500]
        record["checked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_store(store)


# --- which agent can use which server --------------------------------------------------------------------

# Servers with the Anthropic Messages API (POST /v1/messages), which Claude Code needs, and since when:
# Ollama 0.14.0 (2026-01-10), LM Studio 0.4.1 (2026-01-30), llama.cpp b7187 (2025-11-28, PR #17570),
# vLLM 0.11.1 (2025-11-18). Any other server only when added with --anthropic (e.g. a LiteLLM gateway).
ANTHROPIC_SINCE = {"ollama": "Ollama 0.14.0", "lmstudio": "LM Studio 0.4.1", "llamacpp": "llama.cpp b7187",
                   "vllm": "vLLM 0.11.1"}


def anthropic_api(endpoint):
    return endpoint["kind"] in ANTHROPIC_SINCE or endpoint.get("anthropic", False)


def agent_problem(endpoint, agent):
    """Why AGENT cannot use ENDPOINT, or None."""
    if agent == "claude" and not anthropic_api(endpoint):
        return (f"Claude Code speaks only the Anthropic Messages API (/v1/messages); the LLM endpoint "
                f"{endpoint['name']} is a {KINDS[endpoint['kind']]['label']} without it. Use Codex or OpenCode with "
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


def parse_choice(value):
    """(name, model or None) of --llm NAME[:MODEL]; a model may contain ':' itself (qwen2.5-coder:7b)."""
    name, sep, model = (value or "").partition(":")
    if not NAME.fullmatch(name) or (sep and not MODEL_NAME.fullmatch(model)):
        raise ValueError(f"--llm expects NAME[:MODEL], an endpoint of sandbox-ai llm list and a model name: "
                         f"{printable(value)!r}")
    return name, model or None


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
    found, _ = selection(a)
    parsed = found["parsed"]
    key, headers = secret_values(found, get)
    return [{"name": found["name"], "scheme": parsed["scheme"], "host": parsed["host"], "port": parsed["port"],
             "addresses": list(getattr(a, "llm_addresses", []) or ([parsed["address"]] if parsed["address"] else [])),
             "prefix": parsed["prefix"], "placeholder": PLACEHOLDER, "key": key,
             "headers": [[name, value] for name, value in headers], "ca": ca_text(found)}]


def opencode_config(endpoint, model, only=False):
    """OpenCode's configuration (the v1 shape, which OpenCode 2 reads too): an OpenAI-compatible provider
    (@ai-sdk/openai-compatible, Chat Completions) with the endpoint's models. ONLY (--no-provider-defaults):
    no other provider is offered."""
    pid = provider_id(endpoint)
    names = list(dict.fromkeys([model, *endpoint.get("models", [])]))
    config = {"$schema": "https://opencode.ai/config.json",
              "provider": {pid: {"npm": "@ai-sdk/openai-compatible",
                                 "name": f"{endpoint['name']} ({KINDS[endpoint['kind']]['label']})",
                                 "options": {"baseURL": agent_url(endpoint), "apiKey": PLACEHOLDER},
                                 "models": {name: {"name": name} for name in names}}},
              "model": f"{pid}/{model}", "small_model": f"{pid}/{model}", "share": "disabled"}
    if only:
        config["enabled_providers"] = [pid]
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


def codex_profile(endpoint, model):
    """~/.codex/sandbox-ai-llm.config.toml: a provider with the Responses API (Codex has no other since 0.95.0),
    the key from an environment variable that holds only the placeholder, and none of Codex's own calls home
    (update check, analytics, metrics, plugin sync, the hosted web search a local server does not have)."""
    pid = provider_id(endpoint)
    return "\n".join([
        f"# Written by sandbox-ai for --llm {endpoint['name']} at every start (codex --profile {CODEX_PROFILE}).",
        f"model_provider = {toml_string(pid)}",
        f"model = {toml_string(model)}",
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
    return {"OPENCODE_CONFIG": "/root/" + OPENCODE_CONFIG, "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1"}


def agent_arguments(a):
    """Arguments put before the agent's own: Codex selects the profile written by prepare_home."""
    return ["--profile", CODEX_PROFILE] if a.agent == "codex" else []


def prepare_home(a, home, replace_private, update_json_defaults, state_limit):
    """Write the agent's configuration for the endpoint into the sandbox HOME with the launcher's helpers, which never
    follow a symlink the agent planted. Returns the HOME-relative paths written."""
    found, model = selection(a)
    if a.agent == "opencode":
        config = opencode_config(found, model, only=a.no_provider_defaults)
        replace_private((json.dumps(config, indent=2) + "\n").encode(), home, OPENCODE_CONFIG)
        return [OPENCODE_CONFIG]
    if a.agent == "codex":
        relative = f".codex/{CODEX_PROFILE}.config.toml"
        replace_private(codex_profile(found, model).encode(), home, relative)
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
            "agent_url": agent_url(found, "anthropic" if a.agent == "claude" else "openai"),
            "api": {"claude": "anthropic-messages", "codex": "openai-responses", "opencode": "openai-chat"}[a.agent],
            "opens": opens, "path_prefix": parsed["prefix"] or None, "http_rule": prefix_rule(a, found),
            "config": config,
            "key_secret": found["api_key_secret"], "header_secrets": found["headers"],
            "ca": str(ca_dir() / found["ca"]) if found["ca"] else None, "vpn": found["vpn"],
            "placeholder": PLACEHOLDER, "insecure_http": found["insecure_http"]}


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


def check(endpoint, agent=None, model=None, get=None, out=print, connect=socket.create_connection, client=None):
    """Reachability, TLS, the key, the models and the APIs the agents need, line by line; True if nothing failed.
    From the host: a server only reachable through the endpoint's VPN needs that VPN up here too."""
    parsed, failed = endpoint["parsed"], []

    def line(ok, text):
        if ok is False:
            failed.append(text)
        out(f"  {'ok  ' if ok else 'FAIL' if ok is False else 'note'}  {printable(text)}")
    out(f"{endpoint['name']}: {KINDS[endpoint['kind']]['label']} at {endpoint['url']}")
    if endpoint.get("vpn"):
        line(None, f"reached through the VPN profile {endpoint['vpn']}; this check runs from the host's network")
    try:
        addresses = [parsed["address"]] if parsed["address"] else \
            (["127.0.0.1"] if parsed["loopback"] else resolve(parsed["host"], parsed["port"]))
    except OSError as exc:
        line(False, f"{parsed['host']} does not resolve: {exc}")
        return False
    line(True, f"address {', '.join(addresses)}" + ("" if parsed["address"] else f" ({parsed['host']})"))
    if parsed["scheme"] == "http":
        problem = plain_http_problem(parsed, addresses)
        if problem and not endpoint.get("insecure_http"):
            line(False, problem)
        elif problem:
            line(None, "WARNING: plain http to a public address (--insecure-http): prompts, code and the key "
                       "cross the network unencrypted")
        else:
            line(True, "plain http to " + ("the host's loopback" if parsed["loopback"] else "a local address"))
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
    for name, path, label in apis:
        if agent and agent != name:
            continue
        answer = probe(client, path)
        required = agent == name
        if answer == "yes":
            line(True, f"{label} at {path}")
        elif answer == "auth":
            line(False, f"{label}: the server refused the key at {path}")
        elif answer == "no":
            hint = {"claude": f" (needs {', '.join(ANTHROPIC_SINCE.values())} or later)",
                    "codex": " (Codex needs it; see sandbox-ai llm --help)"}.get(name, "")
            line(False if required else None, f"no {label} at {path}{hint}")
        else:
            line(False if required else None, f"{label}: {answer}")
    if agent:
        problem = agent_problem(endpoint, agent)
        if problem:
            line(False, problem)
    return not failed


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
    if endpoint["insecure_http"]:
        lines.append("  WARNING: plain http to a public address (--insecure-http)")
    return [printable(line) for line in lines]


def public_view(endpoint):
    """An endpoint as --json shows it (no secret values: there are none in the store)."""
    view = {key: endpoint[key] for key in ("name", "kind", "url", "api_key_secret", "headers", "ca", "vpn", "model",
                                           "anthropic", "insecure_http", "models", "checked")}
    view["agents"] = [a for a in ("claude", "codex", "opencode") if not agent_problem(endpoint, a)]
    return view


def add(args, out=print, get=None):
    """sandbox-ai llm add: check everything that can be checked offline, copy the CA, then store."""
    if not NAME.fullmatch(args.name):
        raise ValueError(f"an endpoint's name is 1-32 letters, digits, _ or - (no ':'): {printable(args.name)!r}")
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
    if parsed["scheme"] == "http" and not parsed["loopback"] and not parsed["address"]:
        try:
            addresses = resolve(parsed["host"], parsed["port"])
        except OSError:
            addresses = []
            out(f"note: {parsed['host']} does not resolve here now; plain http is checked again at every start "
                "(local addresses only)")
        problem = plain_http_problem(parsed, addresses)
        if problem and not args.insecure_http:
            raise ValueError(problem)
    else:
        problem = plain_http_problem(parsed)
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
    elif args.name in store["endpoints"]:
        (ca_dir() / f"{args.name}.pem").unlink(missing_ok=True)
    store["endpoints"][args.name] = record
    save_store(store)
    for line in describe(check_record(args.name, record)):
        out(line)
    out(f"Kept in {store_path()}. Check it with: sandbox-ai llm check {args.name}")
    return 0


def main(argv, out=print, get=None):
    parser = argparse.ArgumentParser(prog="sandbox-ai llm", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="\n\n".join(__doc__.split("\n\n")[1:4]))
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
    checking = commands.add_parser("check", help="reachability, TLS, key, models and the agents' APIs")
    checking.add_argument("name")
    checking.add_argument("-a", "--agent", choices=["claude", "codex", "opencode"])
    checking.add_argument("-m", "--model")
    args = parser.parse_args(argv)
    try:
        if args.command == "add":
            return add(args, out, get)
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
            store = load_store()
            if store["endpoints"].pop(args.name, None) is None:
                out(f"{printable(args.name)} was not there")
                return 1
            save_store(store)
            (ca_dir() / f"{args.name}.pem").unlink(missing_ok=True)
            out(f"{args.name} removed")
            return 0
        if args.command == "models":
            found = sort_models(endpoint_models(endpoint(args.name), get), args.sort)
            for line in [json.dumps(found, indent=2)] if args.json else model_table(found):
                out(line)
            return 0
        found = endpoint(args.name)
        if args.model and not MODEL_NAME.fullmatch(args.model):
            raise ValueError(f"--model: not a model name: {printable(args.model)!r}")
        return 0 if check(found, args.agent, args.model, get, out) else 1
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai llm: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
