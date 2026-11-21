#!/usr/bin/env python3
"""The runtime of sandbox-ai's browser container (Containerfile.browser; --browser, browser.py): mounted read-only at
/sandbox-browser at every start, like the supervisor's runtime.py beside it, so that a new sandbox-ai needs no
rebuild of the image.

It runs as the pod's root without capabilities. It trusts the run's CA (the connection proxy's, when there is one:
MOUNT/ca.pem) in Chromium's NSS database (Firefox takes it from the policies the image carries), starts the display
with runtime.start_gui - Xvfb without TCP and without the abstract socket, x11vnc on MOUNT/vnc/vnc.sock only - and
then the browser, again when it is closed - and the display, with the browser, when its Xvfb or x11vnc ended (at
most RESTARTS times in RESTART_WINDOW seconds together). SIGTERM (the run ends) stops them all. A URL the launcher
hands over (MOUNT/vnc/OPEN_REQUEST, browser.request_open: DeepSeek Harness' web UI) is opened: Chromium's second start
hands it to the running one (a new tab), Firefox (--no-remote) is started again on it. Before each start of the
browser its settings are written into its profile (Chromium's Local State:
no DNS over HTTPS; Firefox's user.js: FIREFOX_PREFS), and behind Tor (config "tor") WebRTC gathers no address and
Firefox looks .onion names up.

Standard library only.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time

MOUNT = Path("/sandbox-browser")
HOME = Path("/tmp/home")
RESTARTS = 20
RESTART_WINDOW = 600
OPEN_REQUEST = "open.url"  # browser.OPEN_REQUEST: in MOUNT/vnc, written by the launcher only
OPEN_URL = re.compile(r"https?://[!-~]{1,4000}")
# A profile kept from an earlier run (session, a folder) carries the lock of that run's browser, whose host name (the
# pod's) differs: the browser would refuse it as "in use on another computer".
# A page the start opens on (browser.config "start_url": Cairn's board, --cairn): a page of the pod's loopback only,
# waited for up to START_WAIT seconds at the first start (the part that serves it may start a moment later).
START_URL = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})/")
START_WAIT = 60
LOCKS = {"chromium": ("SingletonLock", "SingletonSocket", "SingletonCookie"),
         "firefox": ("lock", ".parentlock", "parent.lock")}


def load_runtime(path=None):
    """The supervisor's runtime.py (its start_gui), loaded from beside this file."""
    path = Path(path or MOUNT / "runtime.py")
    spec = importlib.util.spec_from_file_location("sandbox_browser_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def screen_size(screen):
    width, height, _ = str(screen).split("x")
    return int(width), int(height)


def start_page(cfg):
    """The page the browser starts on: the configuration's start_url when it is one of START_URL, else about:blank."""
    url = cfg.get("start_url")
    return url if isinstance(url, str) and START_URL.fullmatch(url) else "about:blank"


def wait_for_start(cfg, sleep=time.sleep, clock=time.monotonic, wait=START_WAIT):
    """Until the start page's port accepts (at most WAIT seconds); True when it does."""
    found = START_URL.fullmatch(start_page(cfg))
    if not found:
        return False
    deadline = clock() + wait
    while True:
        try:
            with socket.create_connection(("127.0.0.1", int(found[1])), timeout=1):
                return True
        except OSError:
            if clock() > deadline:
                return False
            sleep(0.5)


# Chromium's own services that call Google (or a server of their own) beyond what --disable-background-networking
# stops: one --disable-features (a second one would replace the first).
CHROMIUM_FEATURES_OFF = ("OptimizationHints", "MediaRouter", "Translate", "AutofillServerCommunication",
                         "CertificateTransparencyComponentUpdater", "InterestFeedContentSuggestions")
# Behind Tor WebRTC sends UDP only through a proxy - and the browser has none - so it gathers no candidate at all:
# no address of the pod's (with pasta: this host's own) reaches a site through JavaScript, no STUN over UDP is tried.
CHROMIUM_TOR = ("--force-webrtc-ip-handling-policy=disable_non_proxied_udp",)
# Local State (the browser's, not a profile's): DNS over HTTPS off - every lookup through the pod's resolv.conf and
# hosts (behind Tor: tor's DNSPort, through the redirect), never a DoH server of Chromium's choosing.
CHROMIUM_LOCAL_STATE = {"dns_over_https": {"mode": "off", "templates": ""}}
# Firefox's preferences, written as user.js into its profile at every start (a profile kept with the session or a
# folder of yours too: user.js wins over what was set in it). Nothing of its own on the network: no DNS over HTTPS,
# no HTTP/3 (UDP), no prefetching or speculative connections, no Safe Browsing lists, telemetry, crash reports,
# studies, push service, region or location lookups, plugin (OpenH264) downloads or search updates - each of these
# would be an approval question, and behind Tor a call that makes the browser recognisable.
FIREFOX_PREFS = {
    "network.trr.mode": 5, "network.http.http3.enable": False, "network.http.http3.enabled": False,
    "network.prefetch-next": False, "network.dns.disablePrefetch": True, "network.dns.disablePrefetchFromHTTPS": True,
    "network.predictor.enabled": False, "network.predictor.enable-prefetch": False,
    "network.http.speculative-parallel-limit": 0, "browser.urlbar.speculativeConnect.enabled": False,
    "browser.places.speculativeConnect.enabled": False, "network.proxy.socks_remote_dns": True,
    "browser.safebrowsing.malware.enabled": False, "browser.safebrowsing.phishing.enabled": False,
    "browser.safebrowsing.downloads.enabled": False, "browser.safebrowsing.downloads.remote.enabled": False,
    "browser.safebrowsing.blockedURIs.enabled": False, "browser.safebrowsing.provider.google4.updateURL": "",
    "browser.safebrowsing.provider.google.updateURL": "", "browser.safebrowsing.provider.mozilla.updateURL": "",
    "toolkit.telemetry.enabled": False, "toolkit.telemetry.unified": False, "toolkit.telemetry.archive.enabled": False,
    "datareporting.healthreport.uploadEnabled": False, "datareporting.policy.dataSubmissionEnabled": False,
    "breakpad.reportURL": "", "browser.tabs.crashReporting.sendReport": False,
    "browser.crashReports.unsubmittedCheck.autoSubmit2": False, "app.normandy.enabled": False,
    "app.normandy.api_url": "", "app.shield.optoutstudies.enabled": False, "app.update.auto": False,
    "dom.push.connection.enabled": False, "dom.push.serverURL": "", "browser.region.network.url": "",
    "browser.region.update.enabled": False, "geo.provider.network.url": "", "media.gmp-manager.updateEnabled": False,
    "media.gmp-manager.url": "", "browser.search.update": False, "extensions.getAddons.cache.enabled": False,
    "extensions.systemAddon.update.enabled": False, "network.captive-portal-service.enabled": False,
    "network.connectivity-service.enabled": False, "browser.newtabpage.enabled": False,
    "browser.newtabpage.activity-stream.feeds.section.topstories": False,
    "browser.newtabpage.activity-stream.feeds.topsites": False, "browser.newtabpage.activity-stream.telemetry": False,
    "browser.startup.homepage": "about:blank", "browser.startup.page": 0, "browser.aboutwelcome.enabled": False,
    # Remote Settings (firefox.settings.services.mozilla.com and its attachments' CDN: blocklists, certificate
    # intermediates and CRLite, query stripping lists, translation and other models) pointed nowhere, as Mozilla's
    # own test profiles do; the features that live on it are off. The MITM check after a certificate error
    # (mitm-detection.services.mozilla.com), the private attribution reports (Firefox 128), the captive portal URL
    # and the sponsored top sites, which call out by themselves, too.
    "services.settings.server": "data:,#remote-settings-dummy/v1", "browser.translations.enable": False,
    "browser.ml.enable": False, "security.certerrors.mitm.priming.enabled": False,
    "dom.private-attribution.submission.enabled": False, "captivedetect.canonicalURL": "",
    "browser.topsites.contile.enabled": False, "browser.discovery.enabled": False,
}
# Behind Tor (and the opposite, Firefox's own defaults, without: a profile kept from a Tor run gets them back).
# .onion names: Firefox refuses to look them up at all (network.dns.blockDotOnion) - behind Tor the pod's resolver
# maps them into tor's virtual network, through which they load. WebRTC: off, as in Tor Browser - its candidates would
# show the pod's address (with pasta: this host's own) to a site's JavaScript. No location lookups.
FIREFOX_TOR = {True: {"network.dns.blockDotOnion": False, "media.peerconnection.enabled": False,
                      "media.peerconnection.ice.default_address_only": True, "media.peerconnection.ice.no_host": True,
                      "media.peerconnection.ice.proxy_only_if_behind_proxy": True, "geo.enabled": False},
               False: {"network.dns.blockDotOnion": True, "media.peerconnection.enabled": True,
                       "media.peerconnection.ice.default_address_only": False,
                       "media.peerconnection.ice.no_host": False,
                       "media.peerconnection.ice.proxy_only_if_behind_proxy": False, "geo.enabled": True}}


def take_open_request(folder):
    """The URL the launcher left in FOLDER (OPEN_REQUEST), taken (removed), or None: none there, or not an http(s)
    URL of printable ASCII."""
    path = Path(folder) / OPEN_REQUEST
    try:
        if path.is_symlink():
            path.unlink()
            return None
        with open(path, "rb") as stream:
            data = stream.read(8192)
        path.unlink()
    except OSError:
        return None
    url = data.decode("ascii", "replace").strip()
    return url if OPEN_URL.fullmatch(url) else None


def chromium_command(cfg, profile, url=None):
    width, height = screen_size(cfg["screen"])
    argv = ["chromium", f"--user-data-dir={profile}",
            # As root Chromium refuses its own sandbox; the container (no capabilities, no new privileges, a
            # read-only root, the pod's firewall) is the boundary.
            "--no-sandbox", "--no-first-run", "--no-default-browser-check", "--password-store=basic",
            "--disable-dev-shm-usage",  # its shared memory in /tmp: nothing in the pod's shared /dev/shm
            "--disable-gpu", "--disable-quic",  # QUIC (UDP) would not pass the proxy anyway
            # Nothing of its own on the network (approvals and Tor runs would see it): no updates, pings, metrics.
            "--disable-background-networking", "--disable-component-update", "--disable-sync", "--no-pings",
            "--disable-domain-reliability", "--disable-breakpad", "--metrics-recording-only",
            "--disable-client-side-phishing-detection", "--disable-features=" + ",".join(CHROMIUM_FEATURES_OFF),
            "--window-position=0,0", f"--window-size={width},{height}"]
    if cfg.get("tor"):
        argv += list(CHROMIUM_TOR)
    if cfg.get("cdp_port"):
        argv += [f"--remote-debugging-port={int(cfg['cdp_port'])}", "--remote-debugging-address=127.0.0.1"]
    return argv + [url or start_page(cfg)]


def write_profile_file(path, text):
    """TEXT into PATH of the profile, never through a link there (the profile is the browser's to write)."""
    path = Path(path)
    if path.is_symlink():
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(text)


def merged(base, extra):
    for key, value in extra.items():
        base[key] = merged(base.get(key) if isinstance(base.get(key), dict) else {}, value) \
            if isinstance(value, dict) else value
    return base


def chromium_settings(profile):
    """Chromium's Local State in PROFILE (its user data folder) with CHROMIUM_LOCAL_STATE merged in."""
    path = Path(profile) / "Local State"
    try:
        state = json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else {}
    except (OSError, ValueError, RecursionError):
        state = {}
    write_profile_file(path, json.dumps(merged(state if isinstance(state, dict) else {}, CHROMIUM_LOCAL_STATE)))


def firefox_prefs(cfg):
    return {**FIREFOX_PREFS, **FIREFOX_TOR[bool(cfg.get("tor"))]}


def firefox_settings(cfg, profile):
    """Firefox's user.js in PROFILE (firefox_prefs)."""
    lines = ["// sandbox-ai: written at every start of the sandbox's browser; changes here are replaced"]
    lines += [f"user_pref({json.dumps(name)}, {json.dumps(value)});" for name, value in firefox_prefs(cfg).items()]
    write_profile_file(Path(profile) / "user.js", "\n".join(lines) + "\n")


def settings(cfg, profile):
    (chromium_settings(profile) if cfg["kind"] == "chromium" else firefox_settings(cfg, profile))


def firefox_command(cfg, profile, url=None):
    width, height = screen_size(cfg["screen"])
    return ["firefox-esr", "--profile", str(profile), "--no-remote", "--new-instance", "-width", str(width),
            "-height", str(height), url or start_page(cfg)]


def command(cfg, profile, url=None):
    return chromium_command(cfg, profile, url) if cfg["kind"] == "chromium" else firefox_command(cfg, profile, url)


def trust_ca(cfg, home, ca, run=subprocess.run):
    """Chromium: the run's CA into the NSS database of HOME (~/.pki/nssdb, which Chromium reads). Firefox: its
    policies (in the image) install MOUNT/ca.pem. Returns whether a CA was trusted."""
    if not Path(ca).is_file():
        return False
    if cfg["kind"] != "chromium":
        return True
    database = Path(home) / ".pki" / "nssdb"
    database.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not (database / "cert9.db").exists():
        run(["certutil", "-N", "-d", f"sql:{database}", "--empty-password"], check=True, stdin=subprocess.DEVNULL)
    run(["certutil", "-A", "-d", f"sql:{database}", "-t", "C,,", "-n", "sandbox-ai run CA", "-i", str(ca)],
        check=True, stdin=subprocess.DEVNULL)
    return True


def clear_locks(kind, profile):
    for name in LOCKS[kind]:
        path = Path(profile) / name
        if path.is_symlink() or path.is_file():
            path.unlink()


class Restarts:
    """The browser is started again when it ends (a closed window), RESTARTS times in RESTART_WINDOW at most."""

    def __init__(self, limit=RESTARTS, window=RESTART_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock, self.times = limit, window, clock, []

    def allowed(self):
        now = self.clock()
        self.times = [t for t in self.times if now - t < self.window] + [now]
        return len(self.times) <= self.limit


def main(mount=MOUNT, home=HOME, runtime=None, popen=subprocess.Popen, sleep=time.sleep):
    cfg = json.loads((Path(mount) / "config.json").read_text())
    if cfg.get("kind") not in LOCKS:
        print("sandbox-ai browser: no such browser in its configuration", file=sys.stderr)
        return 2
    home = Path(home)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.environ.update({"HOME": str(home), "DISPLAY": cfg["display"], "XDG_RUNTIME_DIR": str(home),
                       "NO_AT_BRIDGE": "1"})
    profile = Path(mount) / "profile" if cfg.get("profile") in ("session", "dir") else home / "profile"
    profile.mkdir(mode=0o700, parents=True, exist_ok=True)
    clear_locks(cfg["kind"], profile)
    if trust_ca(cfg, home, Path(mount) / "ca.pem"):
        print("sandbox-ai browser: the run's CA is trusted (the connection proxy's)", file=sys.stderr)
    runtime = runtime or load_runtime(Path(mount) / "runtime.py")
    runtime.VNC_DIR = Path(mount) / "vnc"
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(True))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(True))
    gui = {"display": cfg["display"], "screen": cfg["screen"], "vnc": True, "private": True, "selection": True}
    display = runtime.start_gui(gui)
    restarts, browser, start_url, handing, stopped = Restarts(), None, None, [], False
    if start_page(cfg) != "about:blank" and not wait_for_start(cfg, sleep):
        print("sandbox-ai browser: its start page did not answer yet; reload it", file=sys.stderr)
    try:
        while not stopping:
            gone = [name for name, process in zip(("Xvfb", "x11vnc"), display) if process.poll() is not None]
            if gone:
                # Its display ended (a crash, the out-of-memory killer): started again, the browser with it -
                # else the container ran on without one, for the rest of the run.
                if not restarts.allowed():
                    print("sandbox-ai browser: its display ended too often; not started again", file=sys.stderr)
                    return 1
                print(f"sandbox-ai browser: its display ended ({', '.join(gone)}); started again", file=sys.stderr)
                for process in ([browser] if browser is not None else []) + list(reversed(display)):
                    runtime.stop_process(process)
                browser, display, stopped = None, [], True
                for stale in (runtime.x_socket(cfg["display"]), Path(f"/tmp/.X{cfg['display'][1:]}-lock"),
                              runtime.VNC_DIR / "vnc.sock"):
                    if stale.is_symlink() or stale.exists():
                        stale.unlink()
                display = runtime.start_gui(gui)
            url = take_open_request(Path(mount) / "vnc")
            if url and cfg["kind"] == "chromium" and browser is not None and browser.poll() is None:
                # Chromium's own singleton hands the URL to the running browser (a new tab) and ends.
                handing.append(popen(command(cfg, profile, url), stdin=subprocess.DEVNULL))
            elif url:
                start_url = url  # its next start opens it: Firefox (--no-remote) is started again for it
                if browser is not None and browser.poll() is None:
                    runtime.stop_process(browser)
                    stopped = True
            handing = [process for process in handing if process.poll() is None]
            if browser is None or browser.poll() is not None:
                if browser is not None and not stopped and not restarts.allowed():
                    print("sandbox-ai browser: it ended too often; not started again", file=sys.stderr)
                    return 1
                clear_locks(cfg["kind"], profile)
                settings(cfg, profile)  # at every start of it: what it changed meanwhile does not stay
                browser = popen(command(cfg, profile, start_url), stdin=subprocess.DEVNULL)
                start_url, stopped = None, False
            sleep(0.5)
        return 0
    finally:
        for process in ([browser] if browser is not None else []) + handing + list(reversed(display)):
            runtime.stop_process(process)


if __name__ == "__main__":
    sys.exit(main())
