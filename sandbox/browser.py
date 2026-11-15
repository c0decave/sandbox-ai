#!/usr/bin/env python3
"""The sandbox's own browser (--browser [chromium|firefox]): a container of sandbox-ai's browser image in the
sandbox's Podman pod, beside the agent, with its own display that only you see - or that the agent drives.

  --browser [KIND]                   chromium (the default when no KIND is given) or firefox
  --browser-control human|agent|both who drives it (default human): human - you, in the desktop app's viewer; agent -
                                     the agent, through the Chrome DevTools Protocol on 127.0.0.1:9222 in the pod;
                                     both - the two. Agent control is Chromium's: Firefox speaks only WebDriver BiDi
  --browser-profile fresh|session|DIR  its profile: fresh (default) - a new, empty one at every start, gone with the
                                     run; session - kept with the session like its HOME (but never in it: the agent's
                                     HOME does not hold your browser's logins); DIR - a folder of yours (absolute)

Where it runs: its own container (POD-service-browser) of localhost/sandbox-ai-browser (sandbox-ai build browser:
Debian's chromium and firefox-esr, Xvfb, x11vnc, NSS's certutil; its Debian base pinned by digest when it is built),
started like a service (services.py: created, initialised, proven to be in the pod's user namespace and - with
--vpn - behind the VPN's kill switch, only then started, after the supervisor's firewall and proxy are up). It shares
the pod's network namespace, so it has the agent's exit and nothing more: the firewall and the allow-list, the kill
switch (sandbox-ai net cut), the VPN or Tor, the connection proxy with its TLS inspection (the run's CA goes into the
browser's trust store: Chromium's NSS database, Firefox's policies), the approvals for unknown hosts and the logs.
What you type in it - passwords too - passes that proxy like the agent's own traffic (logged with credentials
redacted, checked by the privacy rules).

Nothing of its own on the network: at every start its runtime (browser_runtime.py) sets Chromium's DNS over HTTPS
off (Local State) beside its flags (no QUIC, background networking, component updates, pings, crash reports) and
writes Firefox's user.js (no DNS over HTTPS, HTTP/3, prefetching, Safe Browsing lists, telemetry, push service,
region or plugin downloads). Behind Tor (config "tor") WebRTC gathers no address (the pod's - with pasta this
host's own - would reach a site's JavaScript) and Firefox looks .onion names up, which it refuses otherwise.

Its display: Xvfb without TCP and without the abstract socket (the agent shares the pod's network namespace and
would reach an abstract X socket), and x11vnc on a Unix socket only (runtime.start_gui, as --gui-vnc DIR), in a
folder beside the run's directory that only you can open (RUN.browser-vnc/vnc.sock; no TCP port anywhere). The
desktop app's Browser button shows it. With control human the agent reaches nothing of it: no DevTools port, no X
socket, no VNC socket. With agent or both, Chromium's DevTools listen on 127.0.0.1:9222 of the pod: whatever runs in
the pod reaches them (the agent, your services) - and in both whatever you sign in to there, the agent can use.

It runs as the pod's root (your user on the host, as the agent's processes; no capabilities, no new privileges, a
read-only root, /tmp a tmpfs), so that a folder of yours (its profile, the VNC socket's) stays yours. Chromium
refuses its own sandbox as root: the container is the boundary. SysV shared memory - the pod's IPC namespace is
shared with the agent - is not used: Xvfb has no MIT-SHM, x11vnc runs with -noshm, Chromium keeps its shared memory
in /tmp (--disable-dev-shm-usage).

Linux: the app reaches the socket directly. macOS (podman machine): the socket lives in the machine's VM, which
macOS cannot connect to through a shared folder, so the live view is Linux-only for now; --browser-control agent
works there. Bubblewrap has no pod and no container engine: --browser needs the Podman backend.

Standard library only.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
KINDS = ("chromium", "firefox")
CONTROLS = ("human", "agent", "both")
DEFAULT_KIND = "chromium"
IMAGE = "localhost/sandbox-ai-browser:latest"
ROLE = "browser"  # the image's label sandbox-ai.role (build_image.sh)
BUILD = "sandbox-ai build browser"
NAME = "browser"  # the container: POD-service-browser (services.container_name)
USER = "0:0"
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
SCREEN = "1280x800x24"
DISPLAY = ":42"  # not the agent's (:99): the pod shares its network namespace, so the displays must differ
MOUNT = "/sandbox-browser"  # in its container: the runtime, its configuration, the CA, the VNC folder, the profile
CONFIG_FILE = "browser.json"  # in the run's session folder, mounted at MOUNT/config.json
CA_FILE = "browser-ca.pem"  # written by the supervisor into the run's directory (runtime.share_browser_ca)
CA_LIMIT = 64 << 10
VNC_SOCKET = "vnc.sock"
# A URL the launcher hands to the browser (request_open), in the VNC folder: only you and the browser's container
# reach it, never the agent. browser_runtime.py opens it (OPEN_REQUEST there).
OPEN_REQUEST = "open.url"
OPEN_URL = re.compile(r"https?://[!-~]{1,4000}")
LIMITS = {"memory": "2g", "cpus": "2", "pids": 1024}
READY_TIMEOUT = 90  # agent and both: the agent starts once the DevTools port answers
TOR_NOTE = ("behind Tor this is {name}, not Tor Browser: sites can fingerprint it and link its visits to each other "
            "(and to you, if you sign in); use Tor Browser for anonymity")
NAMES = {"chromium": "Chromium", "firefox": "Firefox ESR"}


def control_of(a):
    return getattr(a, "browser_control", None) or "human"


def parse_profile(value):
    """(kind, path): ("fresh", None), ("session", None) or ("dir", absolute Path) of --browser-profile VALUE (None:
    fresh). ValueError for anything else."""
    if value in (None, "", "fresh"):
        return "fresh", None
    if value == "session":
        return "session", None
    text = str(value)
    if any(c in text for c in (",", "\n", "\r", "\0")) or not (text.startswith("/") or text.startswith("~")):
        raise ValueError(f"--browser-profile is fresh, session or an absolute folder (no commas): {text!r}")
    return "dir", Path(os.path.normpath(Path(text).expanduser().absolute()))


def agent_drives(a):
    return control_of(a) in ("agent", "both")


def human_sees(a):
    return control_of(a) in ("human", "both")


def session_key(a):
    """The kept start whose folder keeps a session profile: a foreground kept start's key, a session's id (its
    supervisor runs with --start-id and its HOME as --state-dir); None for a start that keeps nothing."""
    key = getattr(a, "start_key", None)
    if key:
        return key
    if getattr(a, "tmux_socket_dir", None) and getattr(a, "state_dir", None) and getattr(a, "start_id", None):
        return a.start_id
    return None


def keeps_profile(a):
    """Whether this start can keep a profile with its session: it keeps a HOME of sandbox-ai's (a session, a kept
    start), not one of yours (--state-dir) nor none."""
    if getattr(a, "tmux_socket_dir", None):
        return bool(getattr(a, "state_dir", None))
    return bool(getattr(a, "keep_home", False)) or bool(getattr(a, "start_key", None))


def session_profile(a, sessions=None):
    """The folder of a session profile (the session's folder/browser/KIND, beside its HOME, never in it), or None
    while it is not known yet (a dry run, a session not begun)."""
    key = session_key(a)
    if not key:
        return None
    if sessions is None:
        sessions = launcher().load_sessions_module()
    return sessions.session_dir(key) / "browser" / a.browser


_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_browser", HERE / "agent_sandbox.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _launcher = loaded
    return _launcher


def check(a, l=None):
    """--browser and its options, checked; a.browser_container gets the browser's container (a service of
    services.members), None without --browser. ValueError for whatever the launcher refuses."""
    a.browser_container = None
    if not getattr(a, "browser", None):
        for option, value in (("--browser-control", "browser_control"), ("--browser-profile", "browser_profile")):
            if getattr(a, value, None):
                raise ValueError(f"{option} sets an option of the sandbox's browser; add --browser [chromium|firefox]")
        return None
    l = l or launcher()
    if a.browser not in KINDS:
        raise ValueError(f"--browser is chromium or firefox: {a.browser!r}")
    control = control_of(a)
    if control not in CONTROLS:
        raise ValueError(f"--browser-control is human, agent or both: {control!r}")
    if a.backend != "podman":
        raise ValueError("--browser needs the Podman backend: the browser is a container of its own in the sandbox's "
                         "Podman pod, on the agent's network; Bubblewrap has no pod and no container engine (start "
                         "with --backend podman, or use --gui with a browser in the agent's image)")
    if a.podman_url:
        raise ValueError("--browser with --podman-url: its display's socket would be on that machine, out of this "
                         "app's reach; start it there (sandbox-ai hosts run HOST -- ...)")
    if getattr(a, "mcp_socket_dir", None) or getattr(a, "helper_of", None):
        raise ValueError("--browser is for an agent's start; an MCP server's or helper's sandbox has none")
    if a.browser == "firefox" and control != "human":
        raise ValueError(f"--browser-control {control} needs Chromium: the agent drives the browser through the Chrome "
                         "DevTools Protocol, which Firefox no longer speaks (only WebDriver BiDi, which Playwright "
                         "cannot attach to); use --browser chromium, or --browser-control human with Firefox")
    if human_sees(a) and l.on_machine(a):
        raise ValueError(f"--browser-control {control} on macOS: the browser's display socket lives in podman "
                         "machine's VM, which macOS cannot connect to, so the live view is Linux-only for now; "
                         "--browser-control agent works (the agent drives it)")
    kind, path = parse_profile(getattr(a, "browser_profile", None))
    if kind == "session" and not keeps_profile(a):
        raise ValueError("--browser-profile session keeps the profile with the session, but this start keeps none (its "
                         "HOME is discarded or your own --state-dir): use fresh, or a folder of yours")
    if kind == "dir":
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise ValueError(f"--browser-profile {path}: not a folder (or a symbolic link)")
        if path.exists() and path.stat().st_uid != os.getuid():
            raise ValueError(f"--browser-profile {path}: not your folder (its owner is another user)")
        l.check_private_dir(a, path, "--browser-profile")
        l.check_output_dir(path, "--browser-profile")
    if agent_drives(a):
        if CDP_PORT in l.host_ports(a):
            raise ValueError(f"--browser-control {control}: the DevTools port {CDP_PORT} is a port of the host here "
                             f"(--allow-host-port {CDP_PORT})")
    for service in getattr(a, "services", None) or []:
        if service["name"] == NAME:
            raise ValueError(f"--service {NAME}: that name is the sandbox's browser's (--browser); name it otherwise")
        if agent_drives(a) and CDP_PORT in service.get("ports", []):
            raise ValueError(f"--service {service['name']}: port {CDP_PORT} is the browser's DevTools port")
    a.browser_container = {
        "name": NAME, "browser": True, "kind": a.browser, "control": control, "profile": kind,
        "profile_dir": str(path) if path else None, "image": ROLE, "user": USER,
        "ports": [CDP_PORT] if agent_drives(a) else [], "ready": CDP_PORT if agent_drives(a) else "none",
        "ready_timeout": READY_TIMEOUT, "env": [], "secrets": [], "data": [], "keep": False, "read_only": True,
        **LIMITS, "record": {"image": IMAGE, "id": None, "kind": "sandbox-ai"}}
    return a.browser_container


def notes(a):
    """What a start with the browser says about it before it runs."""
    if not getattr(a, "browser_container", None):
        return []
    name, control = NAMES[a.browser], control_of(a)
    found = [{"human": f"browser: {name} in its own container beside the agent, for you only (the app's Browser "
                       "button; no DevTools port, the agent cannot drive it)",
              "agent": f"browser: {name} in its own container beside the agent, driven by the agent through the "
                       f"DevTools Protocol at {CDP_URL}; only the agent's: the app shows no display for you of it "
                       "(--browser-control both to watch it too)",
              "both": f"browser: {name} in its own container beside the agent, for you and the agent (DevTools at "
                      f"{CDP_URL}): whatever you sign in to there, the agent can use"}[control]]
    if getattr(a, "offline", False):
        found.append("browser: the run is offline (--offline): the browser reaches no network either")
    elif getattr(a, "no_proxy", False):
        found.append("browser: its connections take the agent's exit - firewall, allow-list, kill switch, VPN or Tor; "
                     "no connection proxy (--no-proxy): what it sends is neither inspected nor logged")
    else:
        found.append("browser: its connections take the agent's exit - firewall, allow-list, kill switch, VPN or Tor, "
                     "the connection proxy: what you type in it (passwords too) passes the proxy, logged with "
                     "credentials redacted")
    if launcher_tor(a):
        found.append("browser: " + TOR_NOTE.format(name=name))
    kind, path = parse_profile(getattr(a, "browser_profile", None))
    if kind == "dir":
        found.append(f"browser: its profile (logins, cookies, history) is kept in {path}")
    elif kind == "session":
        found.append("browser: its profile (logins, cookies, history) is kept with the session, outside its HOME")
    if kind != "fresh" and launcher_tor(a):
        found.append("browser: behind Tor a kept profile brings the cookies, logins, history and cache of its "
                     "earlier runs (with Tor or without, from your own address) into this one: a site links this "
                     "run's visits to them at once; use --browser-profile fresh behind Tor")
    return found


def launcher_tor(a):
    return (getattr(a, "vpn_profile", None) or {}).get("type") == "tor"


def vnc_dir(a, l=None):
    """The folder of the browser's VNC socket on the host: beside the run's directory (only you can open it)."""
    if getattr(a, "control_dir", None):
        return Path(a.control_dir).with_name(Path(a.control_dir).name + ".browser-vnc")
    l = l or launcher()
    return l.runs_dir() / f"{a.run_id}.browser-vnc"


def profile_dir(a, l=None):
    """The host folder of the profile, or None (fresh: a tmpfs in its container)."""
    kind, path = parse_profile(getattr(a, "browser_profile", None))
    if kind == "dir":
        return path
    if kind == "session":
        return session_profile(a, (l or launcher()).load_sessions_module())
    return None


def config(a):
    """MOUNT/config.json of the browser's runtime (browser_runtime.py)."""
    kind, _ = parse_profile(getattr(a, "browser_profile", None))
    return {"kind": a.browser, "control": control_of(a), "screen": SCREEN, "display": DISPLAY,
            "cdp_port": CDP_PORT if agent_drives(a) else None, "profile": kind, "tor": launcher_tor(a)}


def prepare_folders(a, l=None):
    """At a real start: the VNC folder (0700, removed with the run by the caller) and the profile's folder, both
    yours alone (a folder of yours made with a plain mkdir is 0755: Firefox's databases in it, 0644, would be every
    user's to read). Returns the VNC folder."""
    folder = vnc_dir(a, l)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    folder.chmod(0o700)
    profile = profile_dir(a, l)
    if profile is not None:
        profile.mkdir(mode=0o700, parents=True, exist_ok=True)
        profile.chmod(0o700)
    a.browser_vnc_dir = folder
    return folder


def image_id(a, l=None, run=None):
    """The ID of sandbox-ai's browser image in this machine's Podman, or None when it is not there (or is not
    sandbox-ai's: its label sandbox-ai.role must be browser)."""
    l = l or launcher()
    run = run or subprocess.run
    try:
        done = run(l.podman_command(a, "image", "inspect", "--format",
                                    '{{.Id}} {{index .Labels "sandbox-ai.role"}}', IMAGE),
                   stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise ValueError("Required tool missing: podman") from None
    words = (done.stdout or "").split()
    if done.returncode != 0 or len(words) != 2 or words[1] != ROLE or \
            not re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", words[0]):
        return None
    return words[0].removeprefix("sha256:")


def prepare(a, l=None, run=None):
    """At a real start (not a dry run): the browser's image is there; its ID is what the container is made of (a
    later change of the tag does not change this run's browser). ValueError naming the build otherwise."""
    container = getattr(a, "browser_container", None)
    if not container:
        return None
    found = image_id(a, l, run)
    if found is None:
        l = l or launcher()
        try:  # (there, but without sandbox-ai's label: another image holds the tag - said, not "not there")
            there = (run or subprocess.run)(l.podman_command(a, "image", "exists", IMAGE), stdin=subprocess.DEVNULL,
                                            capture_output=True, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            there = False
        if there:
            raise ValueError(f"--browser: the image {IMAGE} is not sandbox-ai's own browser image (its label "
                             f"sandbox-ai.role is not {ROLE}: another build holds the tag); build sandbox-ai's, which "
                             f"replaces it: {BUILD}")
        raise ValueError(f"--browser: sandbox-ai's browser image {IMAGE} is not there (nothing is pulled at a start); "
                         f"build it once: {BUILD}")
    container["record"]["id"] = found
    return found


def read_ca(control_dir):
    """The run's CA certificate the supervisor put into its directory (runtime.share_browser_ca), checked: one PEM
    certificate of at most CA_LIMIT bytes, else None (no proxy, or nothing usable)."""
    path = Path(control_dir) / CA_FILE
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > CA_LIMIT:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    text = data.decode("ascii", "replace")
    if text.count("-----BEGIN CERTIFICATE-----") != 1 or "-----END CERTIFICATE-----" not in text or \
            "PRIVATE KEY" in text:
        return None
    return data


def mounts(a, session, l=None, ca=None):
    """[(host path, container path, mode)] of the browser's container."""
    session = Path(session)
    found = [(session / "resolv.conf", "/etc/resolv.conf", "ro"), (session / "hosts", "/etc/hosts", "ro"),
             (HERE / "runtime.py", f"{MOUNT}/runtime.py", "ro"),
             (HERE / "browser_runtime.py", f"{MOUNT}/browser_runtime.py", "ro"),
             (session / CONFIG_FILE, f"{MOUNT}/config.json", "ro"), (vnc_dir(a, l), f"{MOUNT}/vnc", "rw")]
    if ca is not None:
        found.append((ca, f"{MOUNT}/ca.pem", "ro"))
    profile = profile_dir(a, l)
    if profile is not None:
        found.append((profile, f"{MOUNT}/profile", "rw"))
    return found


def create_command(a, pod, service, session, l=None, limits=None):
    """(podman create command, environment) of the browser's container in POD: created only (services.launch
    proves its namespaces before it starts it). The run's CA, when the proxy made one, is copied from the run's
    directory into SESSION and mounted read-only."""
    l = l or launcher()
    services = l.load_services_module()
    limits = services.enforceable(a, l) if limits is None else limits
    ca = None
    data = read_ca(a.control_dir) if getattr(a, "control_dir", None) else None
    if data is not None:
        ca = Path(session) / CA_FILE
        ca.write_bytes(data)
        ca.chmod(0o644)
    image = (service.get("record") or {}).get("id")
    if not image:
        raise ValueError(f"the browser's image was not checked ({BUILD})")
    argv = l.podman_command(a, "create", "--pull=never", "--pod", pod, "--name", services.container_name(pod, service),
                            "--label", f"{l.RUN_LABEL}={a.run_id}", "--label", f"{services.SERVICE_LABEL}={NAME}",
                            "--user", USER, "--cap-drop=ALL", "--security-opt=no-new-privileges", "--init",
                            "--read-only", "--log-driver=k8s-file", "--log-opt", "max-size=10mb",
                            "--image-volume=tmpfs", "--tmpfs", "/tmp:rw,nosuid,nodev,size=1g,mode=1777",
                            "--env", "HOME=/tmp/home")
    if "memory" in limits:
        argv += ["--memory", service["memory"]]
    if "cpus" in limits:
        argv += ["--cpus", str(service["cpus"])]
    if "pids" in limits:
        argv += ["--pids-limit", str(service["pids"])]
    for source, target, mode in mounts(a, session, l, ca):
        argv += l.mount_option(source, target, mode)
    return argv + ["--entrypoint", "/usr/bin/python3", image, "-I", f"{MOUNT}/browser_runtime.py"], {}


def write_config(a, session):
    """The runtime's configuration in the run's session folder."""
    path = Path(session) / CONFIG_FILE
    path.write_text(json.dumps(config(a)))
    path.chmod(0o644)
    return path


def plan(a, l=None):
    """The dry run's account of the browser, or None without one."""
    if not getattr(a, "browser_container", None):
        return None
    l = l or launcher()
    kind, path = parse_profile(getattr(a, "browser_profile", None))
    profile = {"fresh": {"kind": "fresh", "where": "a tmpfs in its container: new at every start, gone with the run"},
               "session": {"kind": "session", "where": str(session_profile(a, l.load_sessions_module()))
                           if session_key(a) else "the session's folder (browser/" + a.browser + "), beside its HOME"},
               "dir": {"kind": "dir", "where": str(path)}}[kind]
    pod = l.pod_name(a, getattr(a, "run_id", None))
    vnc = vnc_dir(a, l)
    shown = [{"host": str(source), "container": target, "mode": mode}
             for source, target, mode in mounts(a, Path("SESSION"), l, Path("SESSION") / CA_FILE
                                                if l.proxy_enabled(a) else None)]
    for entry in shown:
        if entry["host"].startswith("SESSION/"):
            entry["host"] = "the run's session folder: " + entry["host"][len("SESSION/"):]
    return {"kind": a.browser, "name": NAMES[a.browser], "control": control_of(a),
            "container": f"{pod}-service-{NAME}", "image": IMAGE, "build": BUILD, "user": USER,
            "network": "the pod's own, which is the agent's: its firewall, allow-list, connection proxy, approvals, "
                       "kill switch and VPN or Tor",
            "capabilities": "none (--cap-drop=ALL, no-new-privileges)", "read_only": True,
            "limits": dict(LIMITS), "mounts": shown,
            "ca": ("the run's CA (the connection proxy's), " + ("into Chromium's NSS database" if a.browser ==
                                                                 "chromium" else "through Firefox's policies"))
            if l.proxy_enabled(a) else None,
            "display": {"xvfb": f"{DISPLAY} {SCREEN}, without TCP and without an abstract socket",
                        "vnc_socket": str(vnc / VNC_SOCKET), "tcp": False},
            "tcp_ports": [CDP_PORT] if agent_drives(a) else [],
            "cdp": CDP_URL if agent_drives(a) else None, "profile": profile,
            "config": config(a), "settings": settings_text(a),
            "tor": TOR_NOTE.format(name=NAMES[a.browser]) if launcher_tor(a) else None}


def settings_text(a):
    """What the runtime sets in the browser at every start (browser_runtime.py), said for the plan."""
    found = ["DNS: only the pod's resolv.conf and hosts - " + (
        "DNS over HTTPS off (Local State dns_over_https.mode off), QUIC off" if a.browser == "chromium" else
        "DNS over HTTPS off (network.trr.mode 5), HTTP/3 off"),
             "no prefetching, Safe Browsing lists, telemetry, crash reports, updates or other calls of its own"]
    if launcher_tor(a):
        found.append("behind Tor: WebRTC gathers no address (" + (
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" if a.browser == "chromium" else
            "media.peerconnection.enabled false") + "); .onion names resolve into Tor's virtual network and load "
            "through Tor" + (" (network.dns.blockDotOnion false)" if a.browser == "firefox" else ""))
    return found


def record(a, pod):
    """The browser in run.json: the app's Browser button finds its VNC socket there."""
    if not getattr(a, "browser_container", None):
        return None
    kind, _ = parse_profile(getattr(a, "browser_profile", None))
    return {"kind": a.browser, "control": control_of(a), "profile": kind,
            "vnc": str(vnc_dir(a) / VNC_SOCKET) if human_sees(a) else None,
            "cdp": CDP_URL if agent_drives(a) else None, "container": f"{pod}-service-{NAME}",
            "tor": launcher_tor(a)}  # the app's Browser tab says that it is not Tor Browser


def agent_environment(a):
    """What the agent is told of the browser (the sandbox-ai skill explains it)."""
    if not getattr(a, "browser_container", None):
        return {}
    env = {"SANDBOX_AI_BROWSER": a.browser, "SANDBOX_AI_BROWSER_CONTROL": control_of(a)}
    if agent_drives(a):
        env["SANDBOX_AI_BROWSER_CDP"] = CDP_URL
    return env


def request_open(folder, url):
    """URL to the browser of the run whose VNC folder is FOLDER: written into it (OPEN_REQUEST, 0600, replaced at
    once), where its runtime takes it within a second - Chromium opens it in a new tab, Firefox is started again on
    it. ValueError for anything but an http(s) URL of printable ASCII."""
    if not isinstance(url, str) or not OPEN_URL.fullmatch(url):
        raise ValueError("only an http(s) URL is opened in the session browser")
    folder = Path(folder)
    temporary = folder / f".{OPEN_REQUEST}.{os.getpid()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(url + "\n")
    os.replace(temporary, folder / OPEN_REQUEST)


def remove_vnc_dir(folder):
    shutil.rmtree(folder, ignore_errors=True)
