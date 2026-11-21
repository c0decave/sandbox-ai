"""Audit 2026-11-20 (web, install and security functions): what a user ran into, through the real entry points.

- `sandbox-ai web serve` with its output in a log or a pipe (nohup, systemd, `| tee`): the address, the certificate's
  fingerprint and the warning about logins without TOTP stayed in Python's buffer while the service ran (started
  with -I, so not even PYTHONUNBUFFERED helped) - a log showed nothing until it ended.
"""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import home_guard  # noqa: E402

home_guard.ensure()


def user_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "SANDBOX_AI_"))}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
               XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"))
    return env


def lines_within(stream, seconds, until):
    """The lines STREAM gives within SECONDS, until one contains UNTIL (or the time is up)."""
    found, deadline, buffer = [], time.monotonic() + seconds, b""
    while time.monotonic() < deadline:
        ready, _, _ = select.select([stream], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            break
        chunk = os.read(stream.fileno(), 4096)
        if not chunk:
            break
        buffer += chunk
        *done, buffer = buffer.split(b"\n")
        found += [line.decode(errors="replace") for line in done]
        if any(until in line for line in found):
            break
    return found


def test_web_serve_says_its_address_fingerprint_and_warnings_at_once_into_a_pipe(tmp_path):
    env = user_env(tmp_path)
    added = subprocess.run([str(ROOT / "sandbox-ai"), "web", "user", "add", "alice", "--stdin", "--no-totp"],
                           input="correcthorse12\ncorrecthorse12\n", env=env, capture_output=True, text=True,
                           timeout=120)
    assert added.returncode == 0, added.stderr
    service = subprocess.Popen([str(ROOT / "sandbox-ai"), "web", "serve", "--listen", "127.0.0.1:0"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        said = lines_within(service.stdout, 60, "without a second factor")
        assert service.poll() is None, said
        assert any(line.startswith("sandbox-ai web: https://localhost:") for line in said), said
        assert any("certificate SHA-256" in line for line in said), said
        assert any("without a second factor (TOTP): alice" in line for line in said), said
    finally:
        service.send_signal(signal.SIGTERM)
        service.wait(30)
        service.stdout.close()
    # ended by SIGTERM through its clean-up: its listen file is gone
    assert not list((Path(env["XDG_CONFIG_HOME"]) / "sandbox-ai" / "web").glob("listen-*.json"))


def started_service(tmp_path, *options):
    """`sandbox-ai web serve --listen 127.0.0.1:0 OPTIONS` with a login (alice, no TOTP): (process, port, env)."""
    env = user_env(tmp_path)
    added = subprocess.run([str(ROOT / "sandbox-ai"), "web", "user", "add", "alice", "--stdin", "--no-totp"],
                           input="correcthorse12\ncorrecthorse12\n", env=env, capture_output=True, text=True,
                           timeout=120)
    assert added.returncode == 0, added.stderr
    service = subprocess.Popen([str(ROOT / "sandbox-ai"), "web", "serve", "--listen", "127.0.0.1:0", *options],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    said = lines_within(service.stdout, 60, "https://")
    address = next((line for line in said if line.startswith("sandbox-ai web: https://")), None)
    assert address, said
    return service, int(address.rstrip("/").rsplit(":", 1)[1]), env


def stop(service):
    service.send_signal(signal.SIGTERM)
    service.wait(30)
    service.stdout.close()


def asked(port, host_header, path="/api/me"):
    import http.client
    import ssl
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=ssl._create_unverified_context(), timeout=30)
    try:
        connection.request("GET", path, headers={"Host": host_header})
        answer = connection.getresponse()
        return answer.status, answer.read()
    finally:
        connection.close()


def test_an_ipv6_name_of_the_service_is_the_host_header_a_browser_sends(tmp_path):
    """--hostname fd00::7 (or an address found for --listen [::]:PORT): a browser at https://[fd00::7]:PORT/ sends
    "Host: [fd00::7]:PORT" - it was compared with "fd00::7:PORT" and refused as an unknown host name (421)."""
    service, port, _ = started_service(tmp_path, "--hostname", "fd00::7", "--hostname", "[fd00::8]")
    try:
        assert asked(port, f"[fd00::7]:{port}")[0] == 401, "the service's own name: on to the login"
        assert asked(port, f"[fd00::8]:{port}")[0] == 401
        assert asked(port, f"fd00::7:{port}")[0] == 421, "no browser sends that"
        assert asked(port, f"[fd00::9]:{port}")[0] == 421, "another address is still refused"
    finally:
        stop(service)


def test_the_addresses_found_for_a_wildcard_listen_are_bracketed_when_ipv6(monkeypatch):
    w = load_webui()
    monkeypatch.setattr(w.socket, "getaddrinfo", lambda host, port: [
        (w.socket.AF_INET6, 1, 6, "", ("2001:db8::5", 0, 0, 0)), (w.socket.AF_INET, 1, 6, "", ("192.0.2.7", 0))])
    names, with_port = w.service_names("::", 8443, ())
    assert "[2001:db8::5]:8443" in with_port and "192.0.2.7:8443" in with_port
    assert not any(name.count(":") > 1 and not name.startswith("[") for name in with_port)
    assert "[2001:db8::5]" in names  # (the certificate takes the address inside the brackets)


def load_webui():
    import importlib.util
    spec = importlib.util.spec_from_file_location("audit_webui", ROOT / "sandbox" / "webui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the web service in front of the real ./sandbox-ai (tests/live_fakes.py) ------------------------------------------

@pytest.fixture
def service(tmp_path, monkeypatch):
    from live_fakes import Host
    import test_web_matrix as matrix
    w = matrix.w
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data")):
        monkeypatch.setenv(name, str(tmp_path / "web" / sub))
    monkeypatch.setattr(w, "SCRYPT", matrix.FAST)
    host = Host(tmp_path / "host")
    host.env["SANDBOX_AI_SECRETS"] = "file"
    web = matrix.Web()

    def runner(name, args, timeout=120, input=None):
        assert name == "local"
        if input is None:
            done = host.run(*args, timeout=timeout)
        else:
            done = subprocess.run(host.command(*args), cwd=host.workspace, env=host.env, input=input,
                                  capture_output=True, text=True, timeout=timeout)
        return done.returncode, done.stdout, done.stderr
    web.svc.api.runner = runner
    web.host = host
    web.full = web.headers("full")
    return web


def ask(web, method, path, body=None):
    status, _, data = web.request(method, path, body, web.full)
    return status, json.loads(data)


def test_a_git_login_for_a_server_that_has_one_is_asked_about_first(service):
    """The web's Git login for a server with a login already replaced that login without a word (the app asks
    login-replace, as the web asks it for a publish login): its token is another secret's from then on, for every
    later start that reaches the server."""
    path = "/api/hosts/local/git-logins"
    status, said = ask(service, "POST", path, {"url": "https://git.example.org", "secret": "FIRST", "confirm": True})
    assert status == 200, said
    for url in ("https://git.example.org/", "https://GIT.example.org"):
        status, said = ask(service, "POST", path, {"url": url, "secret": "SECOND", "check_only": True})
        assert status == 400 and said.get("confirm") == "login-replace", said
        assert "a login for that server or kind exists" in said["error"]
    status, said = ask(service, "POST", path, {"url": "https://git.example.org", "secret": "SECOND"})
    assert status == 400 and said.get("confirm") == "git-login-add", "asked, as before"
    status, listed = ask(service, "GET", path)
    assert [entry["secret"] for entry in listed] == ["FIRST"], "nothing replaced without the yes"
    # another server: no such question
    status, said = ask(service, "POST", path, {"url": "https://other.example.org", "secret": "OTHER",
                                               "check_only": True})
    assert status == 200 and said == {"ok": True}, said
    # the yes (the page sends {"confirm": true} once both questions are answered): replaced
    status, said = ask(service, "POST", path, {"url": "https://git.example.org", "secret": "SECOND", "confirm": True})
    assert status == 200, said
    status, listed = ask(service, "GET", path)
    assert [entry["secret"] for entry in listed] == ["SECOND"]


# --- install.sh ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("words, named", [(["--bogus"], "--bogus"), (["--user", "--chek"], "--chek"),
                                          (["--from"], None)])
def test_install_sh_names_the_option_it_does_not_know(tmp_path, words, named):
    """A mistyped option printed the whole help and nothing about what was wrong."""
    done = subprocess.run(["bash", str(ROOT / "install.sh"), *words], env=user_env(tmp_path), capture_output=True,
                          text=True, timeout=60)
    if named is None:  # (an option without its value says so, as before)
        assert done.returncode == 1 and "install.sh: --from needs a path" in done.stderr, done.stderr
        return
    assert done.returncode == 2, done.stderr
    assert done.stderr.startswith(f"install.sh: unknown option: {named}\n"), done.stderr[:300]
    assert "Usage: install.sh [options]" in done.stderr


GIT_REPLACE_DRIVER = r"""
const byId = (id) => document.getElementById(id);
const confirmText = (node) => { const c = node.all().find(n => n.className === "confirm"); return c ? c.textContent : null; };
const posts = () => calls.filter(c => c.method !== "GET").map(c => [c.path, JSON.parse(c.body)]);
const formOf = (id) => { let n = byId(id); while (n && n.className !== "form") n = n.parent; return n; };
const fire = (id, type, value) => { const n = byId(id); if (value !== undefined) n.value = value; n.fire(type); };
context.URL = URL;
(async () => {
  const result = {};
  vm.runInContext('state.me = { user: "admin", csrf: "c" }; state.host = "ct1"; state.page = "logins"; state.hosts = [{ name: "local", kind: "local" }, { name: "ct1", kind: "ssh", ssh: "root@ct1", port: 22 }];', context);
  answer = (method, path, sent) => {
    if (method === "GET") return path.endsWith("/secrets") ? [{ name: "OLD", store: "file" }] : path.endsWith("/git-logins") ? [{ url: "https://git.example.org", user: "x-access-token", secret: "OLD" }] : path.endsWith("/identities") ? {} : [];
    if (path.includes("/secrets/")) return { output: "kept" };
    const body = JSON.parse(sent);
    if (body.check_only && !body.confirm) return { __status: 400, __error: 'https://git.example.org: a login for that server or kind exists: its token is replaced by this secret\'s for every later start that uses it: send {"confirm": true}', __confirm: "login-replace" };
    return body.check_only ? { ok: true } : { output: "added" };
  };
  vm.runInContext("show('logins')", context); await settle();
  fire("git-url", "input", "https://git.example.org");
  fire("git-secret", "input", "NEW_GIT");
  byId("git-token").value = "tok-new"; fire("git-user", "input", "");
  const gitForm = () => formOf("git-url");
  button(gitForm(), "Add…").fire("click"); await settle();
  result.first = [confirmText(gitForm()), posts()];
  button(gitForm(), "Yes, do it").fire("click"); await settle();
  result.second = [confirmText(gitForm()), posts()];
  button(gitForm(), "Yes, add it").fire("click"); await settle();
  result.sent = posts();
  console.log(JSON.stringify(result));
})().catch(e => { console.error(e.stack); process.exit(1); });
"""


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="needs node")
def test_the_page_asks_before_a_git_login_replaces_one_then_stores_the_token(tmp_path):
    """app.js in node: the check (check_only) is refused with login-replace for a server that has a login; the page
    asks that first (nothing stored), then its own question, then stores the token and adds the login confirmed."""
    from test_web import FAKE_DOM, run_driver
    result = run_driver(tmp_path, FAKE_DOM + GIT_REPLACE_DRIVER, "git_replace")
    asked, sent = result["first"]
    assert asked.startswith("The service asks first: https://git.example.org: a login for that server or kind exists")
    check = ["/api/hosts/ct1/git-logins", {"url": "https://git.example.org", "secret": "NEW_GIT", "user": "",
                                           "check_only": True}]
    assert sent == [check], "nothing stored before the yes"
    asked, sent = result["second"]
    assert "the Git login of the secret NEW_GIT?" in asked and sent == [check]
    assert result["sent"] == [check, ["/api/hosts/ct1/secrets/NEW_GIT", {"secret": "tok-new"}],
                              ["/api/hosts/ct1/git-logins", {"url": "https://git.example.org", "secret": "NEW_GIT",
                                                             "user": "", "confirm": True}]]


def test_a_host_update_of_a_checkout_names_the_readmes_install_command(service):
    """hosts update (the web's Update of a host) of a checkout named `curl .../main/install.sh | bash` as the
    README's: the README's is the release's install.sh, which carries the release keys - the file on main carries
    none, so a first install from it checks no signature."""
    status, said = ask(service, "POST", "/api/hosts/local/update", {})
    assert status == 200 and said["changed"] is False and "is a checkout" in said["error"], said
    readme = (ROOT / "README.md").read_text()
    command = "curl -fsSL https://github.com/c0decave/sandbox-ai/releases/latest/download/install.sh | bash"
    assert command in readme and command in said["error"], said["error"]
    assert "main/install.sh" not in said["error"]


def test_web_serve_says_when_its_certificate_does_not_name_a_name_it_serves(tmp_path):
    """The certificate is made at the first start, for the names of then; `web serve --hostname box.lan` later took
    box.lan as a Host name but served the old certificate, which does not name it (the browser: a name mismatch),
    and said nothing. Now it says so, with the command that makes one that does."""
    service, port, env = started_service(tmp_path)
    stop(service)
    service = subprocess.Popen([str(ROOT / "sandbox-ai"), "web", "serve", "--listen", "127.0.0.1:0", "--hostname",
                                "box.lan"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        said = lines_within(service.stdout, 60, "without a second factor")
    finally:
        stop(service)
    warning = next((line for line in said if "does not name" in line), None)
    assert warning is not None, said
    assert "box.lan" in warning and "sandbox-ai web cert --renew --listen 127.0.0.1:0 --hostname box.lan" in warning
    assert "localhost" not in warning.split("does not name", 1)[1].split(":", 1)[0], "only what is missing"
    renewed = subprocess.run([str(ROOT / "sandbox-ai"), "web", "cert", "--renew", "--listen", "127.0.0.1:0",
                              "--hostname", "box.lan"], env=env, capture_output=True, text=True, timeout=60)
    assert renewed.returncode == 0, renewed.stderr
    service = subprocess.Popen([str(ROOT / "sandbox-ai"), "web", "serve", "--listen", "127.0.0.1:0", "--hostname",
                                "box.lan"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        said = lines_within(service.stdout, 60, "without a second factor")
    finally:
        stop(service)
    assert not any("does not name" in line for line in said), said


def test_the_test_home_goes_with_the_read_only_folders_a_pyz_unpacked(tmp_path):
    """home_guard's HOME of a test session is removed at its end, also the 0500 folders of ~/.cache/sandbox-ai/pyz
    that a started .pyz leaves (rmtree alone kept them: ~70 MB a session in TMPDIR, and `rm -rf` of it fails)."""
    root = tmp_path / "sandbox-ai-test-home-x"
    cache = root / "home/.cache/sandbox-ai/pyz/1.2.3-abc/sandbox"
    cache.mkdir(parents=True)
    (cache / "main.py").write_text("x")
    (cache / "main.py").chmod(0o400)
    for folder in (cache, cache.parent):
        folder.chmod(0o500)
    home_guard.remove(root)
    assert not root.exists()
