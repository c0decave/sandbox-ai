"""The development helpers in tests/devtools (what the sandbox-ai-dev sandbox runs for sandbox-ai's own tests): the
fake internet (HTTP and HTTPS echo, DNS with fixed names), the model server stub (OpenAI and Ollama APIs, streams,
connections closed while idle or after N answers), the window-manager check's parsing and verdict, and the dry runs
of the distribution matrix and the throwaway Gitea. The servers run here on loopback; nothing leaves the machine."""
import http.client
import importlib.util
import json
import os
import re
import shlex
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tests" / "devtools"


def load(name):
    spec = importlib.util.spec_from_file_location(f"devtools_{name}", TOOLS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fi, mm, wm = load("fake_internet"), load("model_mock"), load("wm_check")


# --- the fake internet ------------------------------------------------------------------------------------------

@pytest.fixture
def internet(tmp_path):
    log = tmp_path / "requests.jsonl"
    started = fi.start(http=0, https=0 if shutil.which("openssl") else None, dns=0,
                       names={"allowed.example": "192.0.2.10", "*.wild.example": "192.0.2.20"}, log=str(log),
                       workdir=str(tmp_path))
    yield started, log
    started["stop"]()


def test_the_http_echo_says_what_arrived(internet):
    started, log = internet
    connection = http.client.HTTPConnection("127.0.0.1", started["http"], timeout=10)
    connection.request("POST", "/some/path?x=1", body=b"abc", headers={"Host": "allowed.example", "X-Test": "1"})
    seen = json.loads(connection.getresponse().read())
    assert seen["method"] == "POST" and seen["path"] == "/some/path?x=1" and seen["host"] == "allowed.example"
    assert seen["body_bytes"] == 3 and seen["headers"]["X-Test"] == "1" and seen["tls"] is False
    connection.request("GET", "/status/418")
    response = connection.getresponse()
    assert response.status == 418 and json.loads(response.read())["path"] == "/status/418"
    connection.request("GET", "/bytes/5000")
    assert len(connection.getresponse().read()) == 5000
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["path"] for e in entries] == ["/some/path?x=1", "/status/418", "/bytes/5000"]


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl for the certificate")
def test_the_https_echo_with_its_own_certificate(internet):
    started, _ = internet
    context = ssl.create_default_context(cafile=started["certificate"])
    connection = http.client.HTTPSConnection("127.0.0.1", started["https"], timeout=10, context=context)
    connection.request("GET", "/", headers={"Host": "allowed.example"})
    seen = json.loads(connection.getresponse().read())
    assert seen["tls"] is True and seen["host"] == "allowed.example"


def ask(port, name, qtype=1):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(5)
    sock.sendto(fi.build_query(name, qtype), ("127.0.0.1", port))
    answer = sock.recv(4096)
    sock.close()
    return answer


def test_dns_answers_fixed_names_and_nothing_else(internet):
    started, log = internet
    answer = ask(started["dns"], "allowed.example")
    assert answer[:2] == b"\x12\x34" and answer[3] & 0x0F == 0 and answer[6:8] == b"\x00\x01"
    assert socket.inet_ntoa(answer[-4:]) == "192.0.2.10"
    assert socket.inet_ntoa(ask(started["dns"], "a.wild.example")[-4:]) == "192.0.2.20"
    assert ask(started["dns"], "elsewhere.example")[3] & 0x0F == 3  # NXDOMAIN
    empty = ask(started["dns"], "allowed.example", qtype=28)  # AAAA: no answer, no error
    assert empty[3] & 0x0F == 0 and empty[6:8] == b"\x00\x00"
    assert {"kind": "dns", "name": "elsewhere.example"}.items() <= [json.loads(line) for line in
                                                                     log.read_text().splitlines()][2].items()


def test_dns_default_and_broken_queries():
    answer = fi.dns_answer(fi.build_query("anything.example"), {}, default="192.0.2.99")
    assert socket.inet_ntoa(answer[-4:]) == "192.0.2.99"
    for broken in (b"", b"\x00" * 11, fi.build_query("x.example")[:-6]):
        with pytest.raises(ValueError):
            fi.dns_answer(broken, {})


def test_the_command_line_needs_a_server():
    with pytest.raises(SystemExit):
        fi.main([])
    with pytest.raises(SystemExit):
        fi.main(["--http", "0", "--name", "no-address"])


# --- the model server stub --------------------------------------------------------------------------------------

@pytest.fixture
def model(tmp_path):
    server, port = mm.start(models=("mock-model", "other"), log=str(tmp_path / "model.jsonl"))
    yield port, tmp_path / "model.jsonl"
    server.shutdown()


def post(port, path, body, connection=None):
    connection = connection or http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("POST", path, body=json.dumps(body), headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    return response.status, response.read(), connection


def test_openai_models_chat_and_its_stream(model):
    port, log = model
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/v1/models")
    assert [m["id"] for m in json.loads(connection.getresponse().read())["data"]] == ["mock-model", "other"]
    status, data, _ = post(port, "/v1/chat/completions", {"model": "mock-model", "messages": [
        {"role": "system", "content": "be brief"}, {"role": "user", "content": "hello there"}]})
    answer = json.loads(data)
    assert status == 200 and answer["choices"][0]["message"]["content"] == "mock reply to: hello there"
    assert answer["usage"]["prompt_tokens"] == 3
    status, data, _ = post(port, "/v1/chat/completions", {"model": "mock-model", "stream": True,
                                                          "messages": [{"role": "user", "content": "streamed"}]})
    events = [line[len(b"data: "):] for line in data.split(b"\n") if line.startswith(b"data: ")]
    assert events[-1] == b"[DONE]"
    text = "".join(json.loads(e)["choices"][0]["delta"].get("content", "") for e in events[:-1])
    assert text == "mock reply to: streamed" and len(events) >= 3
    assert json.loads(events[-2])["choices"][0]["finish_reason"] == "stop"
    status, data, _ = post(port, "/v1/chat/completions", {"model": "missing", "messages": []})
    assert status == 404 and "not found" in json.loads(data)["error"]["message"]
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["stream"] for e in entries if e["path"] == "/v1/chat/completions"] == [None, True, None]


def test_ollama_tags_chat_and_generate(model):
    port, _ = model
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/api/tags")
    assert [m["name"] for m in json.loads(connection.getresponse().read())["models"]] == ["mock-model", "other"]
    status, data, _ = post(port, "/api/chat", {"model": "other", "messages": [{"role": "user", "content": "hi"}]})
    lines = [json.loads(line) for line in data.splitlines() if line.strip()]
    assert lines[-1]["done"] is True and "".join(l["message"]["content"] for l in lines) == "mock reply to: hi"
    status, data, _ = post(port, "/api/generate", {"model": "mock-model", "prompt": "p", "stream": False})
    assert json.loads(data)["response"] == "mock reply to: p"


def test_a_connection_closed_while_idle_is_what_the_client_sees_next(tmp_path):
    """--idle-close: the kept-alive connection is closed without a word once idle; the next request on it fails
    (OpenCode's "socket connection was closed unexpectedly"), a new connection works."""
    server, port = mm.start(idle_close=0.5, reply="fixed")
    try:
        status, data, connection = post(port, "/v1/chat/completions", {"messages": []})
        assert status == 200 and json.loads(data)["choices"][0]["message"]["content"] == "fixed"
        time.sleep(1.5)
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError, http.client.BadStatusLine)):
            post(port, "/v1/chat/completions", {"messages": []}, connection)
        assert post(port, "/v1/chat/completions", {"messages": []})[0] == 200
    finally:
        server.shutdown()


def test_close_after_n_answers():
    server, port = mm.start(close_after=1)
    try:
        status, _, connection = post(port, "/api/generate", {"prompt": "a", "stream": False})
        assert status == 200
        time.sleep(0.2)
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError, http.client.BadStatusLine)):
            post(port, "/api/generate", {"prompt": "b", "stream": False}, connection)
    finally:
        server.shutdown()


def test_pieces_cover_the_text():
    assert "".join(mm.pieces("abcdefghij", 3)) == "abcdefghij" and len(mm.pieces("abcdefghij", 3)) == 3
    assert mm.pieces("", 4) == [""] and mm.pieces("ab", 10) == ["a", "b"]


# --- the window-manager check -----------------------------------------------------------------------------------

CHILDREN = """
xwininfo: Window id: 0x3d8 (the root window) (has no name)

  Root window id: 0x3d8 (the root window) (has no name)
  Parent window id: 0x0 (none)
     3 children:
     0x200001 "sandbox-ai": ("sandbox-ai-app" "sandbox-ai")  1280x800+0+0  +0+0
     0x200010 "Board: Properties": ("sandbox-ai-app" "sandbox-ai")  300x400+10+10  +10+10
     0x400001 (has no name): ()  1x1+-100+-100  +-100+-100
"""
MANAGED = """
  Map State: IsViewable
  Override Redirect State: no
"""
BYPASS = """
  Map State: IsViewable
  Override Redirect State: yes
"""
PROPS = """_NET_WM_PID(CARDINAL) = 4242
WM_STATE(WM_STATE):
\t\twindow state: Normal
\t\ticon window: 0x0
WM_NAME(UTF8_STRING) = "Board: Properties"
"""


def test_the_window_managers_view_is_read_from_x11_utils():
    assert wm.parse_children(CHILDREN) == ["0x200001", "0x200010", "0x400001"]
    assert wm.parse_xwininfo(MANAGED) == {"map_state": "IsViewable", "override_redirect": False}
    assert wm.parse_xwininfo(BYPASS)["override_redirect"] is True
    assert wm.parse_xprop(PROPS) == {"pid": 4242, "wm_state": "Normal", "name": "Board: Properties"}
    assert wm.parse_xprop("_NET_WM_PID:  not found.\nWM_STATE:  not found.\n") == \
        {"pid": None, "wm_state": None, "name": None}


def test_an_override_redirect_or_unmanaged_window_of_the_program_is_named():
    windows = [{"id": "0x1", "pid": 10, "map_state": "IsViewable", "override_redirect": False, "wm_state": "Normal"},
               {"id": "0x2", "pid": 10, "map_state": "IsViewable", "override_redirect": True, "wm_state": None},
               {"id": "0x3", "pid": 11, "map_state": "IsViewable", "override_redirect": False, "wm_state": None},
               {"id": "0x4", "pid": 10, "map_state": "IsUnMapped", "override_redirect": True, "wm_state": None},
               {"id": "0x5", "pid": 99, "map_state": "IsViewable", "override_redirect": True, "wm_state": None}]
    assert [w["id"] for w in wm.verdict(windows, {10, 11})] == ["0x2", "0x3"]  # not unmapped ones, not others'
    assert wm.verdict(windows[:1], {10}) == []


def test_the_programs_processes_are_found_below_it(tmp_path):
    for pid, parent in ((100, 1), (101, 100), (102, 101), (200, 1)):
        (tmp_path / str(pid)).mkdir()
        (tmp_path / str(pid) / "stat").write_text(f"{pid} (name with) spaces) S {parent} 0 0\n")
    assert wm.descendants(100, tmp_path) == {100, 101, 102}


def test_the_check_says_what_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(wm.shutil, "which", lambda tool: None)
    assert wm.main(["--", "true"]) == 2
    assert "missing" in json.loads(capsys.readouterr().out)["error"]


# --- the distribution matrix and the throwaway Gitea (dry runs) -------------------------------------------------

def test_the_distribution_matrix_runs_the_installer_as_a_user_in_each_image():
    done = subprocess.run(["bash", str(TOOLS / "distro_matrix.sh"), "--dry-run"], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert len(lines) == 6 and all(line.startswith("podman run --rm -v ") for line in lines)
    for image in ("debian:13", "ubuntu:22.04", "ubuntu:24.04", "fedora:43", "archlinux:base"):
        assert any(image in line for line in lines), image
    assert all(":/src:ro" in line and "useradd" in line and "install.sh" in line for line in lines)
    one = subprocess.run(["bash", str(TOOLS / "distro_matrix.sh"), "--dry-run", "--engine", "docker",
                          "--appimage", str(TOOLS / "gitea.sh"), "debian:12"], capture_output=True, text=True,
                         timeout=60).stdout
    assert one.startswith("docker run") and "e2e_package.sh" in one and "--extract-and-run" in one and ":/pkg:ro" in one
    wrong = subprocess.run(["bash", str(TOOLS / "distro_matrix.sh"), "--bogus"], capture_output=True, text=True)
    assert wrong.returncode == 2


def test_the_gitea_is_on_loopback_with_an_admin_of_its_own():
    done = subprocess.run(["bash", str(TOOLS / "gitea.sh"), "start", "--dry-run", "--port", "3090"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    account = json.loads(done.stdout)
    assert account["url"] == "http://localhost:3090" and account["user"] == "tester" and len(account["password"]) >= 16
    assert "-p 127.0.0.1:3090:3000" in done.stderr and "docker.io/gitea/gitea:1." in done.stderr
    assert account["password"] in done.stderr  # made for this container, shown where it is made
    assert subprocess.run(["bash", str(TOOLS / "gitea.sh"), "start", "--port", "x"], capture_output=True).returncode == 2
    assert os.access(TOOLS / "gitea.sh", os.X_OK) and os.access(TOOLS / "distro_matrix.sh", os.X_OK)


# --- review fixes: a stalled TLS client, broken DNS queries, quoting, no window, IPv6 ---------------------------

@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl for the certificate")
def test_a_client_that_stalls_in_the_tls_handshake_holds_nobody_else(tmp_path):
    started = fi.start(https=0, workdir=str(tmp_path))
    try:
        stalled = socket.create_connection(("127.0.0.1", started["https"]), timeout=5)  # sends nothing at all
        context = ssl.create_default_context(cafile=started["certificate"])
        connection = http.client.HTTPSConnection("127.0.0.1", started["https"], timeout=5, context=context)
        begun = time.monotonic()
        connection.request("GET", "/after-the-stalled-one")
        assert json.loads(connection.getresponse().read())["path"] == "/after-the-stalled-one"
        assert time.monotonic() - begun < 4
        stalled.close()
    finally:
        started["stop"]()


def test_dns_survives_short_and_broken_queries():
    started = fi.start(dns=0, names={"allowed.example": "192.0.2.10"})
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        for broken in (b"\x00", b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03abc\x00\x00",
                       fi.build_query("allowed.example")[:-3], b"\xff" * 40):
            sock.sendto(broken, ("127.0.0.1", started["dns"]))
        assert socket.inet_ntoa(ask(started["dns"], "allowed.example")[-4:]) == "192.0.2.10"  # still answering
        sock.close()
    finally:
        started["stop"]()
    with pytest.raises(ValueError, match="truncated"):
        fi.parse_query(b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x03abc\x00\x00")


def test_a_bad_address_is_refused_at_the_start_not_by_a_dying_thread():
    for names, default in (({}, "not-an-address"), ({"x.example": "300.1.1.1"}, None), ({}, "10.1")):
        with pytest.raises(ValueError, match="IPv4 address"):
            fi.start(dns=0, names=names, default=default)
    with pytest.raises(SystemExit):
        fi.main(["--dns", "0", "--default", "nonsense"])


def test_the_appimage_name_reaches_su_as_one_word(tmp_path):
    """distro_matrix.sh: a file name with spaces and a ; arrives in the container's su -c as one argument."""
    engine = tmp_path / "engine"
    engine.write_text('#!/bin/sh\nfor last; do :; done\nprintf "%s" "$last" > "$(dirname "$0")/script"\n')
    engine.chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("apt-get", "useradd"):
        (stubs / name).write_text("#!/bin/sh\nexit 0\n")
        (stubs / name).chmod(0o755)
    (stubs / "su").write_text('#!/bin/sh\nprintf "%s" "$3" > "$SU_OUT"\n')
    (stubs / "su").chmod(0o755)
    package = tmp_path / "a b;touch PWNED.AppImage"
    package.write_text("")
    done = subprocess.run(["bash", str(TOOLS / "distro_matrix.sh"), "--engine", str(engine), "--appimage",
                           str(package), "debian:12"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    script = (tmp_path / "script").read_text()
    subprocess.run(["bash", "-c", script], env={"PATH": f"{stubs}:/usr/bin:/bin", "SU_OUT": str(tmp_path / "su")},
                   check=True, cwd=tmp_path)
    assert shlex.split((tmp_path / "su").read_text()) == [
        "/src/scripts/e2e_package.sh", "appimage", "/pkg/a b;touch PWNED.AppImage", "--extract-and-run", "--cli-only"]


def test_every_default_image_is_pinned_by_digest():
    text = (TOOLS / "distro_matrix.sh").read_text()
    start = text.index("DISTROS=(")
    images = text[start:text.index(")", start)].split()[1:]
    assert len(images) == 6 and all(re.fullmatch(r"docker\.io/library/[a-z]+:[a-z0-9.]+@sha256:[0-9a-f]{64}", i)
                                    for i in images), images


def test_a_program_without_a_window_fails_the_check():
    viewable = {"id": "0x1", "pid": 1, "map_state": "IsViewable", "override_redirect": False, "wm_state": "Normal"}
    assert wm.outcome({"windows": [viewable], "unmanaged": []}) == 0
    assert wm.outcome({"windows": [], "unmanaged": []}) == 3
    assert wm.outcome({"windows": [{**viewable, "map_state": "IsUnMapped"}], "unmanaged": []}) == 3
    assert wm.outcome({"windows": [viewable], "unmanaged": [viewable]}) == 1


def ipv6_loopback():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.mark.skipif(not ipv6_loopback(), reason="no IPv6 loopback here")
def test_the_model_server_binds_to_an_ipv6_address():
    server, port = mm.start(bind="::1", reply="six")
    try:
        connection = http.client.HTTPConnection("::1", port, timeout=10)
        connection.request("POST", "/api/generate", body=json.dumps({"prompt": "x", "stream": False}))
        assert json.loads(connection.getresponse().read())["response"] == "six"
    finally:
        server.shutdown()
