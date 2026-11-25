#!/usr/bin/env python3
"""Burp Suite as an MCP server: the catalog entry burp of sandbox-ai-mcp (PortSwigger's MCP extension, reviewed and
built from source there), in one of two ways.

  host mode (default)   your own Burp - Community or Professional, licensed by you, its window on your desktop -
                        loads the extension jar of the signed image (`sandbox-ai mcp burp extension` writes it and
                        shows its path and SHA-256). sandbox-ai reaches the extension's endpoint
                        (http://127.0.0.1:9876/mcp, --burp-url for another port) from this host: for each connection
                        the relay - the MCP gateway with your MCP rules, the call records and the questions - runs
                        mcp_http.py here. No agent's sandbox ever gets that port. The image itself does not run.
  container mode        your Burp jar (--burp-jar PATH: Community or Professional, as PortSwigger gave it to you;
                        never redistributed by sandbox-ai) runs headless in the server's container with the extension
                        preloaded, for each connection: the jar is linked (or copied) into the server's own folder
                        /burp, read-only, its SHA-256 recorded and checked by the container before Burp starts; Burp's
                        user folder (licence, accepted terms, Professional's project) is the server's own folder
                        /data, kept. Professional needs your licence key as the secret BURP_LICENSE (sandbox-ai secrets
                        set BURP_LICENSE): passed into that container only, typed into Burp's first start there and
                        activated online at portswigger.net - an activation counts against your licence, and /data
                        keeps it, so it happens once. Never in an agent's sandbox, a log line or a report.

The definition (mcp.json) keeps what was chosen in "burp": {"mode", "edition" (community, professional or null: not
known yet), "jar" (container mode: {"name", "sha256", "size", "from"}), "checked" (host mode: when the endpoint last
answered)}; a host mode definition has "endpoint" and network none, no folders and no variables.

  sandbox-ai mcp add burp [--burp-mode host|container] [--burp-url URL] [--burp-jar PATH]
                          [--burp-edition community|professional]
  sandbox-ai mcp burp extension [--json]     the extension jar for your Burp: written, its path and SHA-256
  sandbox-ai mcp burp check [--json]         host mode: does the extension answer, Community or Professional

Standard library only.
"""
import contextlib
import hashlib
import importlib.util
import io
import ipaddress
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()
SOURCE = "mcp:burp"  # the catalog entry's id
DEFAULT_URL = "http://127.0.0.1:9876/mcp"
MODES = ("host", "container")
EDITIONS = ("community", "professional")
EDITION_TITLES = {"community": "Community", "professional": "Professional", None: "edition not known yet"}
LICENSE_SECRET = "BURP_LICENSE"
JAR_IN_IMAGE = "/opt/burp-mcp/burp-mcp-all.jar"
PROFESSIONAL_TOOLS = ("get_scanner_issues", "generate_collaborator_payload", "get_collaborator_interactions")
RECORD_KEYS = ("mode", "edition", "jar", "checked")
# Container mode's limits where neither the catalog's recommendation nor --memory/--pids/--tmp gave one: Burp's JVM
# with its project in memory does not start within the defaults (512m, 128 processes).
CONTAINER_LIMITS = {"memory": "4g", "pids": 512, "tmp": "256m"}
JAR_KEYS = ("name", "sha256", "size", "from")
JAR_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,120}\.jar")
LOOPBACK_V4 = "127."

_http = None


def http_module():
    """mcp_http.py (its URL check and its client), loaded once."""
    global _http
    if _http is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_mcp_http_for_burp", HERE / "mcp_http.py")
        with LOADING:
            if _http is None:
                loaded = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(loaded)
                _http = loaded
    return _http


def is_burp(definition):
    return isinstance(definition, dict) and isinstance(definition.get("burp"), dict)


def checked_record(record, definition, where):
    """DEFINITION's "burp" RECORD checked (mcp.validated_image); ValueError says what is wrong."""
    if not isinstance(record, dict) or set(record) - set(RECORD_KEYS):
        raise ValueError(f"{where}: burp must be {{{', '.join(RECORD_KEYS)}}}")
    mode = record.get("mode")
    if mode not in MODES:
        raise ValueError(f"{where}: burp mode must be host or container")
    if record.get("edition") not in (None, *EDITIONS):
        raise ValueError(f"{where}: burp edition must be community, professional or null")
    if "checked" in record and not (isinstance(record["checked"], str) and len(record["checked"]) <= 40):
        raise ValueError(f"{where}: burp checked must be a time")
    jar = record.get("jar")
    if mode == "host":
        if jar is not None:
            raise ValueError(f"{where}: host mode runs your own Burp: no jar")
        if "endpoint" not in definition:
            raise ValueError(f"{where}: host mode needs its endpoint")
        return record
    if "endpoint" in definition:
        raise ValueError(f"{where}: container mode has no endpoint (Burp runs in the server's container)")
    if not isinstance(jar, dict) or set(jar) - set(JAR_KEYS) or not JAR_NAME.fullmatch(str(jar.get("name"))) or \
            not re.fullmatch(r"[0-9a-f]{64}", str(jar.get("sha256"))) or \
            not isinstance(jar.get("size"), int) or isinstance(jar.get("size"), bool) or jar["size"] < 0:
        raise ValueError(f"{where}: container mode needs its jar {{name, sha256, size}}")
    if "from" in jar and not isinstance(jar["from"], str):
        raise ValueError(f"{where}: burp jar from must be a path")
    return record


def edition_title(edition):
    return EDITION_TITLES.get(edition, EDITION_TITLES[None])


def label(definition):
    """How people read the Burp server DEFINITION: "Burp Suite Professional, host mode (127.0.0.1:9876)"."""
    record = definition.get("burp") or {}
    edition = record.get("edition")
    title = f"Burp Suite {edition_title(edition)}" if edition in EDITIONS else f"Burp Suite ({edition_title(None)})"
    if record.get("mode") == "container":
        jar = (record.get("jar") or {}).get("name") or "?"
        return f"{title}, container mode ({jar})"
    endpoint = str(definition.get("endpoint") or DEFAULT_URL)
    where = endpoint.split("//", 1)[-1].split("/", 1)[0]
    return f"{title}, host mode ({where})"


# --- host mode -------------------------------------------------------------------------------------------------------

def listeners(port, proc=Path("/proc")):
    """The local addresses something listens on at TCP PORT of this host (/proc/net/tcp and tcp6; [] elsewhere)."""
    found = []
    for name, size in (("tcp", 8), ("tcp6", 32)):
        try:
            lines = (Path(proc) / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A" or ":" not in fields[1]:
                continue
            address, _, hex_port = fields[1].partition(":")
            try:
                if int(hex_port, 16) != port or len(address) != size:
                    continue
                raw = bytes.fromhex(address)
            except ValueError:
                continue
            if size == 8:
                found.append(".".join(str(b) for b in raw[::-1]))
            else:
                words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
                packed = b"".join(words)
                found.append(str(ipaddress.IPv6Address(packed)))
    return sorted(set(found))


def loopback(address):
    return address.startswith(LOOPBACK_V4) or address in ("::1", "::ffff:127.0.0.1")


def bind_warning(port, proc=Path("/proc")):
    """A warning when the extension's PORT listens on anything but loopback (Burp's MCP tab: Server host), else None."""
    wide = [address for address in listeners(port, proc) if not loopback(address)]
    if not wide:
        return None
    return (f"Burp's MCP endpoint listens on {', '.join(wide)}:{port}, not only on 127.0.0.1: it has no "
            "authentication, so every machine that reaches this host on that port can drive your Burp (send requests, "
            "read its history). Set Server host to 127.0.0.1 in Burp's MCP tab (Advanced Options)")


def probe(url, timeout=10.0):
    """{"ok", "tools" (names), "edition" (from the tools: Professional has the scanner and Collaborator ones),
    "server" (its name and version), "error"} of the MCP endpoint URL: initialize, tools/list, the session closed."""
    http = http_module()
    try:
        client = http.Bridge(url, out=io.StringIO(), timeout=timeout)
        messages, session = client.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "sandbox-ai-burp-check", "version": "1"}}})
        result = next((m.get("result") for m in messages if isinstance(m, dict) and m.get("id") == 1), None)
        if not isinstance(result, dict):
            raise ValueError("no answer to initialize")
        client.session = session
        if isinstance(result.get("protocolVersion"), str):
            client.protocol = result["protocolVersion"][:40]
        client.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        messages, _ = client.post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = next((m.get("result") for m in messages if isinstance(m, dict) and m.get("id") == 2), None)
        tools = [str(t.get("name")) for t in (listed or {}).get("tools") or [] if isinstance(t, dict)]
        if not tools:
            raise ValueError("it lists no tools")
        try:
            client.post(None, method="DELETE")
        except Exception:  # noqa: BLE001 - the extension ends an idle session itself
            pass
        info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
        return {"ok": True, "tools": sorted(tools), "edition": edition_of(tools),
                "server": f"{str(info.get('name') or '?')[:60]} {str(info.get('version') or '')[:20]}".strip(),
                "error": None}
    except Exception as exc:  # noqa: BLE001 - said, never raised: a check
        return {"ok": False, "tools": [], "edition": None, "server": None, "error": str(exc)[:600]}


def edition_of(tools):
    return "professional" if any(name in tools for name in PROFESSIONAL_TOOLS) else "community"


def probe_text(url, found):
    if found["ok"]:
        return (f"Burp's MCP extension answers on {url}: Burp Suite {edition_title(found['edition'])}, "
                f"{len(found['tools'])} tools" + (" (with the scanner and Collaborator tools)"
                                                  if found["edition"] == "professional" else
                                                  " (the scanner and Collaborator tools come with Professional)"))
    return (f"Burp's MCP extension does not answer on {url} ({found['error']}). In Burp: Extensions, Add, Java, the "
            "jar sandbox-ai mcp burp extension shows; then the MCP tab, Enabled. sandbox-ai mcp burp check checks "
            "again")


# --- container mode --------------------------------------------------------------------------------------------------

def guess_edition(name):
    lowered = name.lower()
    if "community" in lowered:
        return "community"
    if "_pro" in lowered or "professional" in lowered or lowered.startswith("burpsuite_pro"):
        return "professional"
    return None


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def checked_jar(path):
    """{name, sha256, size, from} of your Burp jar PATH: a regular file (its link resolved and said), a zip; ValueError
    otherwise."""
    given = Path(os.path.expanduser(str(path)))
    try:
        real = given.resolve(strict=True)
        info = os.stat(real)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"--burp-jar {path}: not there ({exc.__class__.__name__})") from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"--burp-jar {path}: not a file")
    with open(real, "rb") as handle:
        if handle.read(2) != b"PK":
            raise ValueError(f"--burp-jar {path}: not a jar (a zip archive): Burp's jar as PortSwigger's download page "
                             "gives it (Burp Suite, JAR file)")
    name = real.name if JAR_NAME.fullmatch(real.name) else "burpsuite.jar"
    return {"name": name, "sha256": sha256_of(real), "size": info.st_size, "from": str(real)}


def stage_jar(record, folder, out=print):
    """Your jar (RECORD: checked_jar) in FOLDER (the server's own /burp) under its name: a hard link where it can be
    (no copy of hundreds of megabytes), else a copy; checked against its SHA-256 there; any other jar of the folder
    removed (the container takes the one jar it finds)."""
    folder = Path(folder)
    source = Path(record["from"])
    target = folder / record["name"]
    temporary = folder / f".{record['name']}.{os.getpid()}.part"
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    try:
        os.link(source, temporary)
        how = "linked"
    except OSError:
        shutil.copyfile(source, temporary)
        how = "copied"
    try:
        if sha256_of(temporary) != record["sha256"]:
            raise ValueError(f"{source} changed while it was added: add it again")
        if how == "copied":
            os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    for other in folder.iterdir():
        if other.name != target.name and other.name.endswith(".jar"):
            other.unlink()
    out(f"sandbox-ai mcp: your Burp jar {source.name} {how} into {folder} (SHA-256 {record['sha256']}; checked again "
        "before every start of Burp: a jar that changed does not run)")
    return target


# --- mcp add ---------------------------------------------------------------------------------------------------------

def shaped(local, definition, options, entry, out=print, secret_exists=None, is_own=None):
    """The definition `mcp add burp` writes for OPTIONS ({"mode", "url", "jar", "edition"}), from DEFINITION (what
    mcp.definition_from_entry made of the catalog ENTRY). SECRET_EXISTS(name): whether sandbox-ai secrets has it;
    IS_OWN(name, target, host): whether HOST is the server's own folder (mcp.is_own_folder). ValueError for what
    cannot work."""
    if str(entry.get("id") or "") != SOURCE:
        raise ValueError(f"--burp-mode, --burp-url, --burp-jar and --burp-edition are for the catalog entry burp "
                         f"(sandbox-ai-mcp), not {entry.get('name')}")
    mode = options.get("mode") or ("container" if options.get("jar") else "host")
    if mode not in MODES:
        raise ValueError("--burp-mode must be host or container")
    edition = options.get("edition")
    if edition not in (None, *EDITIONS):
        raise ValueError("--burp-edition must be community or professional")
    base = {key: value for key, value in definition.items()
            if key not in ("mounts", "env", "env_optional", "env_values", "env_secrets", "network", "hosts",
                           "allow_private", "allow_tcp", "vpn", "memory", "cpus", "pids", "tmp")}
    if mode == "host":
        if options.get("jar"):
            raise ValueError("--burp-jar is for container mode: in host mode your own Burp runs (--burp-mode "
                             "container --burp-jar PATH runs your jar in the server's container)")
        for key in ("vpn", "allow_private", "allow_tcp"):
            if definition.get(key):
                raise ValueError("host mode reaches only your Burp on this host: no --vpn, --allow-private or "
                                 "--allow-tcp (Burp itself uses your network as you set it up)")
        url = options.get("url") or DEFAULT_URL
        http_module().checked_url(url)
        return {**base, "network": "none", "endpoint": url,
                "burp": {"mode": "host", "edition": edition}}
    if options.get("url"):
        raise ValueError("--burp-url is for host mode (your Burp's endpoint); in container mode Burp's endpoint stays "
                         "inside its container")
    if not options.get("jar"):
        raise ValueError("container mode needs your Burp jar: --burp-jar PATH (Burp Suite Community or Professional, "
                         "the JAR file of PortSwigger's download page; sandbox-ai never downloads or passes it on)")
    jar = checked_jar(options["jar"])
    edition = edition or guess_edition(jar["name"])
    if edition is None:
        raise ValueError(f"which edition is {jar['name']}? --burp-edition community or --burp-edition professional")
    own = [m for m in definition.get("mounts", []) if m.get("target") in ("/burp", "/data")]
    folders = {m["target"]: m for m in own}
    if folders.get("/burp") is None or folders.get("/data") is None:
        raise ValueError("the catalog entry burp has no /burp and /data folders: update sandbox-ai's catalogs")
    if is_own is not None and not is_own(local, "/burp", folders["/burp"]["host"]):
        raise ValueError("container mode puts your jar into the server's own folder /burp: give --burp-jar PATH "
                         "instead of --mount /burp=...")
    variables = {key: definition[key] for key in ("env", "env_optional", "env_values", "env_secrets")
                 if key in definition}
    values = {**(variables.get("env_values") or {}), "BURP_EDITION": edition, "BURP_JAR_SHA256": jar["sha256"]}
    secrets = dict(variables.get("env_secrets") or {})
    optional = [name for name in variables.get("env_optional") or [] if name not in values and name not in secrets]
    if edition == "professional":
        secrets.setdefault(LICENSE_SECRET, LICENSE_SECRET)
        optional = [name for name in optional if name != LICENSE_SECRET]
        if secret_exists is not None and not secret_exists(secrets[LICENSE_SECRET]):
            raise ValueError(f"Burp Suite Professional needs your licence key as the secret {secrets[LICENSE_SECRET]}: "
                             f"sandbox-ai secrets set {secrets[LICENSE_SECRET]} (it reaches only the server's "
                             "container, typed into Burp's first start there), then add it again")
    shaped_definition = {**definition, "mounts": [{**folders["/burp"], "mode": "ro"}, {**folders["/data"], "mode": "rw"}],
                         "env": [name for name in variables.get("env") or [] if name not in values and
                                 name not in secrets],
                         "env_values": values,
                         "burp": {"mode": "container", "edition": edition, "jar": jar}}
    for key, value in CONTAINER_LIMITS.items():  # Burp's JVM: the catalog recommends these; never below them unasked
        shaped_definition.setdefault(key, value)
    shaped_definition.pop("env_optional", None)
    shaped_definition.pop("env_secrets", None)
    if optional:
        shaped_definition["env_optional"] = optional
    if secrets:
        shaped_definition["env_secrets"] = secrets
    return shaped_definition


def after_add(local, definition, out=print, run=subprocess.run, proc=Path("/proc"), probe_url=None):
    """What `mcp add burp` does once the definition is written: container mode - your jar into the server's own
    /burp; host mode - the extension jar written for your Burp (extension), the endpoint checked (probe) and its
    edition kept. Returns the definition as it is now."""
    record = definition["burp"]
    if record["mode"] == "container":
        folder = next(m["host"] for m in definition["mounts"] if m["target"] == "/burp")
        stage_jar(record["jar"], folder, out)
        out(f"sandbox-ai mcp: {local} runs Burp Suite {edition_title(record['edition'])} headless in its container "
            "for each connection; its first start takes a minute or two"
            + (" and activates your licence online at portswigger.net once (an activation counts against your "
               "licence; the server's own folder /data keeps it)" if record["edition"] == "professional" else ""))
        out(f"sandbox-ai mcp: Burp's approval dialogs cannot show in a container: {local}'s tool calls are what your "
            "MCP rules allow, ask or deny (the preset pentest-burp asks before every request Burp sends)")
        return definition
    try:
        written = extension(definition, run=run)
        out(f"sandbox-ai mcp: the extension for your Burp: {written['path']} (SHA-256 {written['sha256']}): in Burp, "
            "Extensions, Add, Extension type Java, this file; then its MCP tab, Enabled")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        out(f"sandbox-ai mcp: the extension jar was not written ({exc}); sandbox-ai mcp burp extension writes it")
    return check(local, definition, out, proc=proc, url=probe_url)


def check(local, definition, out=print, proc=Path("/proc"), url=None, write=None):
    """Host mode: whether the extension answers (probe), said; its edition (and when) kept in the definition (WRITE:
    mcp.write_definition). Returns the definition."""
    endpoint = url or definition.get("endpoint") or DEFAULT_URL
    found = probe(endpoint)
    out(f"sandbox-ai mcp: {probe_text(endpoint, found)}")
    port = http_module().checked_url(endpoint)[1]
    warning = bind_warning(port, proc)
    if warning:
        out(f"sandbox-ai mcp: warning: {warning}")
    if not found["ok"]:
        return definition
    record = {**definition["burp"], "edition": found["edition"],
              "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    changed = {**definition, "burp": record}
    if write is not None:
        write(local, changed)
    return changed


# --- the extension jar for your Burp -----------------------------------------------------------------------------------

def data_folder():
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "sandbox-ai" / "burp"


def extension(definition, destination=None, run=subprocess.run):
    """{"path", "sha256", "image"}: the extension jar of the server's signed image (or its rootfs), written to
    DESTINATION (default: sandbox-ai's data folder, burp/burp-mcp-all.jar), checked against the SHA-256 the image
    carries beside it. ValueError when the image has none or it does not match."""
    destination = Path(destination) if destination else data_folder() / "burp-mcp-all.jar"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".burp-extension-", dir=str(destination.parent)) as scratch:
        jar, checksum = Path(scratch) / "burp-mcp-all.jar", Path(scratch) / "burp-mcp-all.jar.sha256"
        rootfs = (definition.get("rootfs") or {}).get("path") if isinstance(definition.get("rootfs"), dict) else None
        source = definition.get("image") or (f"rootfs {definition['rootfs'].get('sha256', '')[:12]}"
                                             if rootfs else None)
        if definition.get("image"):
            created = run(["podman", "create", "--pull=never", definition["image"]], capture_output=True, text=True,
                          timeout=120, check=False)
            container = (created.stdout or "").strip().splitlines()[-1:] if created.returncode == 0 else []
            if not container:
                raise ValueError(f"the image {definition['image']} is not there (sandbox-ai mcp add burp pulls it): "
                                 f"{(created.stderr or '').strip()[-300:]}")
            try:
                for path, target in ((JAR_IN_IMAGE, jar), (JAR_IN_IMAGE + ".sha256", checksum)):
                    copied = run(["podman", "cp", f"{container[0]}:{path}", str(target)], capture_output=True,
                                 text=True, timeout=120, check=False)
                    if copied.returncode != 0:
                        raise ValueError(f"the image has no {path}: {(copied.stderr or '').strip()[-300:]}")
            finally:
                run(["podman", "rm", "-f", container[0]], capture_output=True, text=True, timeout=60, check=False)
        elif rootfs:
            for path, target in ((JAR_IN_IMAGE, jar), (JAR_IN_IMAGE + ".sha256", checksum)):
                found = Path(rootfs) / path.lstrip("/")
                if found.is_symlink() or not found.is_file():
                    raise ValueError(f"the rootfs has no {path}")
                shutil.copyfile(found, target)
        else:
            raise ValueError("the server has neither its image nor its rootfs")
        words = checksum.read_text(errors="replace").split()
        wanted = words[0].lower() if words else ""
        found_sha = sha256_of(jar)
        if not re.fullmatch(r"[0-9a-f]{64}", wanted) or found_sha != wanted:
            raise ValueError(f"the extension jar of {source} does not match its SHA-256 ({found_sha} against "
                             f"{wanted or 'none'})")
        os.chmod(jar, 0o644)
        os.replace(jar, destination)
    return {"path": str(destination), "sha256": found_sha, "image": source}
