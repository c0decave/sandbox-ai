#!/usr/bin/env python3
"""The runtime of sandbox-ai's browser container (Containerfile.browser; --browser, browser.py): mounted read-only at
/sandbox-browser at every start, like the supervisor's runtime.py beside it, so that a new sandbox-ai needs no
rebuild of the image.

It runs as the pod's root without capabilities. It trusts the run's CA (the connection proxy's, when there is one:
MOUNT/ca.pem) in Chromium's NSS database (Firefox takes it from the policies the image carries), starts the display
with runtime.start_gui - Xvfb without TCP and without the abstract socket, x11vnc on MOUNT/vnc/vnc.sock only - and
then the browser, again when it is closed (at most RESTARTS times in RESTART_WINDOW seconds). SIGTERM (the run ends)
stops them all. Before each start of the browser its settings are written into its profile (Chromium's Local State:
no DNS over HTTPS; Firefox's user.js: FIREFOX_PREFS), and behind Tor (config "tor") WebRTC gathers no address and
Firefox looks .onion names up.

Standard library only.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

MOUNT = Path("/sandbox-browser")
HOME = Path("/tmp/home")
RESTARTS = 20
RESTART_WINDOW = 600
# A profile kept from an earlier run (session, a folder) carries the lock of that run's browser, whose host name (the
# pod's) differs: the browser would refuse it as "in use on another computer".
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


def chromium_command(cfg, profile):
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
    return argv + ["about:blank"]


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


def firefox_command(cfg, profile):
    width, height = screen_size(cfg["screen"])
    return ["firefox-esr", "--profile", str(profile), "--no-remote", "--new-instance", "-width", str(width),
            "-height", str(height), "about:blank"]


def command(cfg, profile):
    return chromium_command(cfg, profile) if cfg["kind"] == "chromium" else firefox_command(cfg, profile)


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
    display = runtime.start_gui({"display": cfg["display"], "screen": cfg["screen"], "vnc": True, "private": True,
                                 "selection": True})
    restarts, browser = Restarts(), None
    try:
        while not stopping:
            if browser is None or browser.poll() is not None:
                if browser is not None and not restarts.allowed():
                    print("sandbox-ai browser: it ended too often; not started again", file=sys.stderr)
                    return 1
                clear_locks(cfg["kind"], profile)
                settings(cfg, profile)  # at every start of it: what it changed meanwhile does not stay
                browser = popen(command(cfg, profile), stdin=subprocess.DEVNULL)
            sleep(0.5)
        return 0
    finally:
        for process in ([browser] if browser is not None else []) + list(reversed(display)):
            runtime.stop_process(process)


if __name__ == "__main__":
    sys.exit(main())
