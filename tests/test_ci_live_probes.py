"""What CI's live tests conclude and say, checked where no Podman and no network namespace can be had: the jobs
nested, killswitch and services run only on GitHub's runners, and a mistake in a probe's own script or in a test's
reading of it costs a whole run there.

  - tests/test_nested_containers_live.py's script, run for real by sh with stand-ins for podman, busybox and unshare
    (its own http.server and its lookups are real): the container reaches the agent's loopback; it is in another
    network namespace; it shares the namespace and is still refused; the server never answers the agent - each told
    apart by loopback_problem, with what the failing side said;
  - what a failure of it says first (account), and that everything goes to the report;
  - the reports of the VPN probes (test_mcp_vpn_live.report_text, test_mcp_lent_vpn_live.report_text): what a failure
    needs first comes first;
  - test_services_live.never_ran: a service's container that never appears is said with the start's own output."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest

import ci_annotations

TESTS = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_ci_probes", TESTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def nested():
    return load("test_nested_containers_live")


# Stand-ins, as shell functions before the script: podman runs a container's command right here ("shared": the
# agent's network namespace), or as a container in a namespace of its own would see it ("private"), or in the agent's
# namespace with its connection refused ("refused"); busybox's applets by the host's tools, its wget by Python.
STAND_INS = r"""
busybox() {
  applet=$1; shift
  case $applet in
    wget) for last in "$@"; do :; done
          python3 -I -c 'import sys, urllib.request
try:
    sys.stdout.write(urllib.request.urlopen(sys.argv[1], timeout=5).read().decode())
except OSError as exc:
    sys.exit(f"wget: cannot connect to remote host: {exc}")' "$last" ;;
    echo) echo "$@" ;;
    true) return 0 ;;
    readlink) readlink "$@" ;;
    sh) sh "$@" ;;
    netstat) echo "tcp 0 0 127.0.0.1:PORT 0.0.0.0:* LISTEN" ;;
    ip) echo "1: lo    inet 127.0.0.1/8 scope host lo" ;;
    *) echo "$applet: applet not found" >&2; return 127 ;;
  esac
}
podman() {
  case $1 in
    version) echo 4.3.1; return 0 ;;
    info|--log-level=debug) echo "false overlay /tmp/containers/storage cni"; return 0 ;;
    import) return 0 ;;
    run) ;;
    *) return 125 ;;
  esac
  shift
  while [ $# -gt 0 ]; do
    case $1 in
      --rm) shift ;;
      --user) shift 2 ;;
      --network) [ "$2" = pasta ] && { echo "Error: unable to find network with name or ID pasta" >&2; return 125; }
                 shift 2 ;;
      localhost/*) shift; break ;;
      *) echo "podman stand-in: $1?" >&2; return 125 ;;
    esac
  done
  if [ "$FAKE" != shared ] && [ "$2" = wget ]; then
    echo "wget: can't connect to remote host (127.0.0.1): Connection refused" >&2; return 1
  fi
  if [ "$FAKE" = private ]; then
    case $2 in
      readlink) echo "net:[4026539999]"; return 0 ;;
      netstat) echo "Active Internet connections (only servers)"; return 0 ;;
    esac
  fi
  if [ "$FAKE" = nodevnull ] && [ "$2" = sh ]; then
    echo "sh: can't create /dev/null: Permission denied" >&2; return 1
  fi
  "$@"
}
unshare() { return 0; }
"""


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_script(nested, folder, fake, port, held=None):
    """The live test's script as the sandbox's sh would run it, with the stand-ins: its results."""
    script = nested.SCRIPT.replace("HOST", "127.0.0.1")
    for name in nested.ATTEMPTS:
        script = script.replace(f"PORT_{name}", str(free_port()))  # nothing listens there: refused
    script = script.replace("/tmp/", f"{folder}/").replace("/bin/busybox", "busybox").replace("8765", str(port))
    script = script.replace(" 20 ]", " 3 ]")  # (the wait for the agent's own server: 3 s here)
    assert "8765" not in script and " 20 ]" not in script
    done = subprocess.run(["sh", "-c", STAND_INS.replace("PORT", str(port)) + script], capture_output=True, text=True,
                          timeout=300, cwd=folder, env={**os.environ, "FAKE": fake})
    assert done.returncode == 0, done.stderr
    return nested.results(done.stdout), done


needs_sh = pytest.mark.skipif(not shutil.which("sh") or not shutil.which("python3"), reason="needs sh and python3")


@needs_sh
def test_the_script_finds_the_agents_loopback_from_a_container(nested, tmp_path):
    found, done = run_script(nested, tmp_path, "shared", free_port())
    assert found["loopback_agent"] == "ok" and found["loopback"] == "ok", done.stdout + done.stderr
    own, _, inside = found["netns"].partition(" ")
    assert own.startswith("net:[") and own == inside
    assert found["devnull"] == "ok" and found["run"] == "hello"
    served = found["served"].split(" ", 1)
    assert int(served[0]) >= 2 and '"GET /bin/ HTTP/1.1" 200' in served[1], "the agent's request and the container's"
    assert nested.loopback_problem(found) is None
    # The attempts at the outside are as before: refused, pasta not there in Podman 4.3.
    assert [found[name] for name in ("agent", "shared", "user1", "slirp4netns")] == ["refused"] * 4
    # ... and each says how it was refused (the proxy's answer, or no way at all: the next failure's first question).
    assert all(found[f"{name}_said"].startswith("wget: cannot connect to remote host")
               for name in ("agent", "shared", "user1", "slirp4netns")), found
    assert found["pasta"].startswith("unavailable:Error: unable to find network with name or ID pasta")
    assert not nested.pasta_supported(found["podman_version"])


@needs_sh
def test_the_script_says_when_a_container_is_in_another_network_namespace(nested, tmp_path):
    """What CI's first run this far could not tell: "loopback failed" - of a container that did not share the
    agent's network namespace, or of one that did?"""
    found, done = run_script(nested, tmp_path, "private", free_port())
    assert found["loopback_agent"] == "ok", done.stdout + done.stderr
    assert found["loopback"] == "failed:wget: can't connect to remote host (127.0.0.1): Connection refused "
    problem = nested.loopback_problem(found)
    assert problem.startswith("the containers do not share the agent's network namespace (the agent net:[")
    assert 'a container net:[4026539999]) although sandbox-ai\'s containers.conf says netns = "host": Podman 4.3.1 ' \
        "inside did not take it" in problem
    assert "Connection refused" in problem


@needs_sh
def test_the_script_says_when_a_container_that_shares_the_namespace_is_refused(nested, tmp_path):
    found, _ = run_script(nested, tmp_path, "refused", free_port())
    assert found["loopback_agent"] == "ok" and found["loopback"].startswith("failed:wget: can't connect")
    problem = nested.loopback_problem(found)
    assert problem.startswith("a container in the agent's network namespace did not reach the agent's server on "
                              "127.0.0.1: \"failed:wget: can't connect to remote host (127.0.0.1): Connection refused")
    assert "the server saw: '1 " in problem, "the agent's own request only"
    assert "listening for the container: 'tcp 0 0 127.0.0.1:" in problem


@needs_sh
def test_the_script_says_when_its_own_server_never_answered(nested, tmp_path):
    """The port is taken (bound, not listening): the server cannot start, the agent's own request is refused - and
    nothing is concluded about the containers."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        found, _ = run_script(nested, tmp_path, "shared", held.getsockname()[1])
    assert found["loopback_agent"].startswith("failed:wget: cannot connect to remote host")
    assert "Address already in use" in found["loopback_agent"], "what the server itself said"
    problem = nested.loopback_problem(found)
    assert problem.startswith("the test's own server on the agent's loopback did not answer the agent itself")
    assert "nothing is known of the containers" in problem


@needs_sh
def test_the_script_says_when_a_container_cannot_write_dev_null(nested, tmp_path):
    found, _ = run_script(nested, tmp_path, "nodevnull", free_port())
    assert found["devnull"] == "failed:sh: can't create /dev/null: Permission denied "


@pytest.mark.parametrize("found, expected", [
    ({"loopback_agent": "ok", "loopback": "ok", "netns": "net:[1] net:[1]"}, None),
    ({"loopback_agent": "ok", "loopback": "ok", "netns": "net:[1] crun: not found"}, None),  # (reached: that counts)
    ({"loopback_agent": "ok", "loopback": "ok", "netns": "net:[1] net:[2]"}, "do not share"),  # reached by chance
    ({"loopback_agent": "ok", "loopback": "failed:timeout", "netns": "net:[1] net:[2]"}, "do not share"),
    ({"loopback_agent": "ok", "loopback": "failed:timeout", "netns": "net:[1] net:[1]"},
     "a container in the agent's network namespace did not reach"),
    ({"loopback_agent": "ok", "loopback": "failed:timeout", "netns": "net:[1] Error: no such file"},
     "a container (its network namespace: Error: no such file; the agent's: net:[1]) did not reach"),
    ({"loopback_agent": "ok", "loopback": "failed:timeout"},
     "(its network namespace: not read; the agent's: not read)"),
    ({"loopback_agent": "ok"}, "did not reach the agent's server on 127.0.0.1: None"),
    ({"loopback_agent": "failed:refused server: ", "loopback": "ok", "netns": "net:[1] net:[1]"},
     "did not answer the agent itself"),
    ({}, "did not answer the agent itself (None)")])
def test_why_the_containers_have_not_the_agents_loopback(nested, found, expected):
    problem = nested.loopback_problem(found)
    assert (problem is None) if expected is None else (expected in problem), problem


def test_a_failure_says_what_matters_first(nested):
    """The results (without Podman's long debug account) and the end of the start's standard error come before
    everything else: they are in the first annotation, whatever is cut after it."""
    found = {"run": "hello", "loopback": "failed:wget: timeout",
             "podman_debug": "|".join(f"debug {n}" for n in range(30))}
    stderr = "\n".join(f"line {n}" for n in range(200))
    text = nested.account(0, found, stderr)
    assert text.startswith('exit 0\nRESULTS:\n{\n "run": "hello",\n "loopback": "failed:wget: timeout"\n}\nSTDERR')
    assert "line 199\nPODMAN'S DEBUG ACCOUNT:\ndebug 0\ndebug 1\n" in text and "line 149" not in text
    assert "line 150\n" in text and text.endswith("debug 29")
    assert "podman_debug" not in text.split("STDERR")[0]
    assert len(ci_annotations.parts(text.split("PODMAN'S DEBUG")[0])) == 1, "the first annotation holds it"


def test_the_vpn_probes_report_starts_with_the_vpn_up():
    live = load("test_mcp_vpn_live")
    found = {"before": {"results": {"tcp internet": "connected"}, "arrived": [["tcp", "192.168.50.2", "203.0.113.10"]]},
             "events": [{"event": "ready"}], "killswitch": '{"ok": true}', "rules": "table inet sandbox_ai_vpn {\n}\n",
             "up": {"results": {"dns lan resolver": "failed: no answer from 192.168.50.1"}, "arrived": []},
             "facts": {"vpn": {"ip -4 route": "default dev wg0", "grep -w udp /proc/net/nf_conntrack": "udp 17 ..."},
                       "outside": {"ip -s link show wgs": "RX: bytes packets errors\n 148 1 1"}},
             "peer down": {"results": {}, "arrived": []}, "runtime exit": 0}
    text = live.report_text(found, "a warning of the probe\n")
    assert [line for line in text.splitlines() if line.startswith("== ")] == [
        "== up: results ==", "== up: arrived ==", "== vpn$ ip -4 route ==",
        "== vpn$ grep -w udp /proc/net/nf_conntrack ==", "== outside$ ip -s link show wgs ==", "== rules ==",
        "== events ==", "== before: results ==", "== before: arrived ==", "== killswitch ==", "== peer down ==",
        "== runtime exit ==", "== the probe's standard error =="]
    assert '== up: results ==\n{"dns lan resolver": "failed: no answer from 192.168.50.1"}\n' in text
    assert "== outside$ ip -s link show wgs ==\nRX: bytes packets errors\n 148 1 1\n" in text  # (as it was printed)
    assert text.endswith("== the probe's standard error ==\na warning of the probe\n")
    # A probe that printed no report: what it printed, and its standard error.
    assert live.report_text({"stdout": "Traceback"}, "boom") == \
        "== rules ==\n\n== events ==\nnull\n== stdout ==\nTraceback\n== the probe's standard error ==\nboom\n"
    assert live.said({"a": [1, 2], "b": "c" * 300}) == json.dumps({"a": [1, 2], "b": "c" * 300})


def test_the_lent_tunnels_report_starts_with_the_phases_that_failed():
    live = load("test_mcp_lent_vpn_live")
    found = {"before": {"results": {}}, "start": {"lends": True},
             "image": {"error": "Failed: the VPN container did not come up", "where": ["File x, line 3"]},
             "network": {"start": {"ip -br addr": "wg0 UNKNOWN 10.66.0.2/32", "nft list ruleset": "table inet x {\n}"},
                         "outside": {"wg show wgs": "interface: wgs"}},
             "cut all": {"error": "KeyError: 'image'"}, "left behind": False}
    text = live.report_text(found, "")
    assert [line for line in text.splitlines() if line.startswith("== ")] == [
        "== image ==", "== cut all ==", "== before ==", "== start ==", "== left behind ==",
        "== start$ ip -br addr ==", "== start$ nft list ruleset ==", "== outside$ wg show wgs =="]
    assert "== start$ nft list ruleset ==\ntable inet x {\n}\n" in text
    assert "standard error" not in text
    # The phase "network" itself failed in the probe: said too.
    assert live.report_text({"network": {"error": "OSError"}}) == "== network ==\n{\"error\": \"OSError\"}\n"


def test_the_probes_facts_never_fail_and_say_what_did_not_run():
    probe = load("mcp_vpn_probe")
    found = probe.facts((), (("sh", "-c", "echo out; echo err >&2"), ("sh", "-c", "echo gone >&2; exit 3"),
                             ("no-such-tool-of-the-probe", "-x"), ("sh", "-c", "printf '%s' " + "y" * 400)), limit=100)
    assert found["sh -c echo out; echo err >&2"] == "out"
    assert found["sh -c echo gone >&2; exit 3"] == "failed (3): gone"
    assert found["no-such-tool-of-the-probe -x"].startswith("did not run: ")
    assert found["sh -c printf '%s' " + "y" * 400] == "y" * 100
    # With a prefix (nsenter into a namespace): the prefix runs the command, the name stays the command's.
    assert probe.facts(["env", "PROBE=1"], (("sh", "-c", "echo $PROBE"),)) == {"sh -c echo $PROBE": "1"}
    assert ("conntrack", "-L", "-p", "udp", "--orig-port-dst", "53") in probe.NAMESPACE_FACTS


def test_a_service_that_never_ran_is_said_with_the_starts_own_words(tmp_path):
    """CI's services job said "the service's container never ran" and no more: the start's output was never read.
    never_ran ends the start (its process group) and returns what it printed and what Podman has."""
    live = load("test_services_live")
    script = tmp_path / "start.py"
    script.write_text("import pathlib, sys, time\nprint('RESULT up no', flush=True)\n"
                      "print('agent-sandbox: error: the service cache did not start: image not known', "
                      "file=sys.stderr, flush=True)\npathlib.Path(sys.argv[1]).write_text('said')\ntime.sleep(600)\n")
    process = subprocess.Popen([sys.executable, "-I", str(script), str(tmp_path / "said")], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    deadline = time.monotonic() + 60
    while not (tmp_path / "said").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    listed = []

    def podman(arguments):
        listed.append(arguments)
        return "0123456789ab  localhost/claude-sandbox  Exited (125)  agent-claude\n"
    said = live.never_ran(process, "sandbox-ai.service=cache", wait=5, podman=podman)
    assert process.poll() is not None, "the start was ended"
    assert said.startswith("no container with the label sandbox-ai.service=cache appeared; the start ")
    assert "STDERR:\nagent-sandbox: error: the service cache did not start: image not known" in said
    assert "STDOUT:\nRESULT up no" in said
    assert "PODMAN PS --ALL:\n0123456789ab  localhost/claude-sandbox  Exited (125)  agent-claude" in said
    assert listed == [["ps", "--all", "--format", "{{.ID}}  {{.Image}}  {{.Status}}  {{.Names}}  {{.Labels}}"]]
    # A start that had already ended says with which status.
    ended = subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.exit('refused: no such image')"],
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    ended.wait(timeout=60)
    said = live.never_ran(ended, "sandbox-ai.service=web", wait=5, podman=lambda arguments: "")
    assert "the start had ended with status 1" in said and "refused: no such image" in said
