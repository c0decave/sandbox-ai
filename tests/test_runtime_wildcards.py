"""runtime.py and host wildcards (--allow-host *.DOMAIN): canary names never asked about, the questions' own budget,
and the Public Suffix List that refuses wildcards anyone could rent a name below."""
import importlib.util
import json
from pathlib import Path
import socket
import struct
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_runtime_wildcards", ROOT / "sandbox/runtime.py")
rt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rt)


@pytest.fixture(autouse=True)
def no_waiting_brokers():
    """The brokers' answer threads (approval-wait) end with their test, never later beside other tests."""
    yield
    deadline = time.monotonic() + 10
    for thread in threading.enumerate():
        if thread.name in ("approval-wait", "dns-wildcard"):
            thread.join(max(0.0, deadline - time.monotonic()))


# --- a canary token in a name is never asked about (H1) ------------------------------------------------------------

TOKEN = "tok123canary"


class Broker:
    def __init__(self):
        self.asked = []

    def ask(self, kind, details, wait=True):
        self.asked.append((kind, dict(details), wait))
        return {"decision": "deny", "scope": "once", "reason": "test"}


class Canaries:
    """As runtime.Canaries for the DNS stub: names (a CanaryMatcher) and lookup, which reports a hit."""

    def __init__(self):
        self.names = rt.CanaryMatcher([[TOKEN, "c1", "plain"]], lower=True)
        self.reported = []

    def lookup(self, name, found=None):
        hit = self.names.name(name)
        if hit:
            self.reported.append(name)
        return hit


def query(port, host, ident=7):
    question = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\0\0\1\0\1"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(3)
        client.sendto(struct.pack("!HHHHHH", ident, 0x100, 1, 0, 0, 0) + question, ("127.0.0.1", port))
        return client.recv(512)


@pytest.fixture
def stub(tmp_path, monkeypatch):
    """A real DNS stub (dns_logger) with the wildcard *.example.com, a recording broker and one canary token."""
    monkeypatch.setattr(rt, "LOG_DIR", tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    broker, canaries, stop = Broker(), Canaries(), threading.Event()
    thread = threading.Thread(target=rt.dns_logger, args=(set(), port, stop, broker, (), None, canaries, None, 53,
                                                         lambda: {"example.com"}), daemon=True)
    thread.start()
    time.sleep(0.2)
    yield {"port": port, "broker": broker, "canaries": canaries}
    stop.set()
    thread.join(3)


def test_a_canary_name_below_a_wildcard_is_reported_and_never_asked_about(stub):
    """The host answers lookups below a wildcard by itself and resolves the name there: a question for a name with
    a canary token would send the token out through the host's DNS. It is reported, refused, never asked."""
    for name in (f"{TOKEN}.example.com", f"x.{TOKEN[:6]}.{TOKEN[6:]}.example.com",  # across labels too
                 f"{TOKEN}.elsewhere.org"):  # not below the wildcard: the ordinary question is not filed either
        assert query(stub["port"], name)[3] & 0xF == 3  # NXDOMAIN at once
    time.sleep(0.3)
    assert stub["broker"].asked == []
    assert len(stub["canaries"].reported) == 3


def test_other_names_are_still_asked_about(stub):
    query(stub["port"], "api.example.com")
    query(stub["port"], "pypi.org")
    deadline = time.monotonic() + 3
    while len(stub["broker"].asked) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    asked = {details["name"]: (details.get("wildcard"), wait) for _, details, wait in stub["broker"].asked}
    assert asked == {"api.example.com": ("example.com", True), "pypi.org": (None, False)}


# --- wildcard questions have a budget of their own (LOW) ----------------------------------------------------------

def broker(tmp_path, name="approvals"):
    network = rt.RuntimeNetwork({"loopback": []}, tmp_path / "allow.json", lambda *a: None)
    return rt.ApprovalBroker(tmp_path / name, network, timeout=0.3, poll=0.02)


def test_lookups_below_a_wildcard_do_not_use_up_the_runs_questions(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "RUN_QUESTIONS", 3)
    monkeypatch.setattr(rt, "WILDCARD_QUESTIONS", 5)
    asking = broker(tmp_path)
    for n in range(5):
        assert asking.ask("dns", {"name": f"n{n}.example.com", "wildcard": "example.com"}, wait=False) is None
    refused = asking.ask("dns", {"name": "more.example.com", "wildcard": "example.com"}, wait=False)
    assert refused["reason"] == "too many lookups below the wildcard"
    # Each wildcard its own budget; the human's questions untouched by all of them.
    assert asking.ask("dns", {"name": "a.other.org", "wildcard": "other.org"}, wait=False) is None
    for n in range(3):
        assert asking.ask("dns", {"name": f"real{n}.example"}, wait=False) is None
    assert asking.ask("dns", {"name": "real9.example"}, wait=False)["reason"] == "too many questions"
    ids = sorted(json.loads(p.read_text())["id"] for p in (tmp_path / "approvals").glob("*.json"))
    assert len(ids) == len(set(ids)) == 9  # one numbering for both: IDs stay unique


def test_open_wildcard_lookups_do_not_crowd_out_the_humans_questions(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "OPEN_QUESTIONS", 2)
    asking = broker(tmp_path)
    for n in range(4):
        asking.ask("dns", {"name": f"n{n}.example.com", "wildcard": "example.com"}, wait=False)
    assert asking.ask("connect", {"destination": "198.51.100.1:443", "address": "198.51.100.1", "port": 443},
                      wait=False) is None


# --- the Public Suffix List (MEDIUM) ------------------------------------------------------------------------------

def psl(tmp_path, private="", icann="", pad=1000):
    """A small list in the real list's shape (PAD filler rules: a list with fewer than 1000 counts as cut short)."""
    filler = "\n".join(f"filler{n}.test" for n in range(pad))
    path = tmp_path / "list.dat"
    path.write_text(f"// header\n// ===BEGIN ICANN DOMAINS===\ncom\nuk\nco.uk\n*.ck\n!www.ck\n{icann}\n{filler}\n"
                    f"// ===END ICANN DOMAINS===\n\n// ===BEGIN PRIVATE DOMAINS===\n{private}\n"
                    "// ===END PRIVATE DOMAINS===\n", encoding="utf-8")
    return path


def test_the_shipped_list_is_whole_and_refuses_private_section_suffixes():
    listed = rt.PublicSuffixList.load()
    # The file ships beside runtime.py, whole.
    assert listed is not None and len(listed.rules) > 5000 and listed.wildcards and listed.exceptions
    header = rt.PUBLIC_SUFFIX_FILE.read_text().splitlines()[:4]
    assert "publicsuffix/list" in header[1] and "2026-11-08" in header[2]
    for value in ("*.github.io", "*.devtunnels.ms", "*.ts.net", "*.hf.space", "*.azurefd.net", "*.b-cdn.net",
                  "*.supabase.co", "*.csb.app", "*.eu.org", "*.webhook.site", "*.pipedream.net", "*.interact.sh",
                  "*.oast.fun", "*.lhr.life", "*.co.uk", "*.kyoto.jp", "*.vercel.app", "*.amazonaws.com",
                  "*.us-west-2.rds.amazonaws.com"):
        with pytest.raises(ValueError, match="anyone can have"):
            rt.wildcard_suffix(value)
    for value in ("*.example.com", "*.shop.co.uk", "*.openai.com", "*.pythonhosted.org", "*.www.ck"):
        assert rt.wildcard_suffix(value) == value[2:]


def test_the_list_rules_wildcards_and_exceptions(tmp_path):
    path = psl(tmp_path, private="tunnel.example\n*.rent.example\nshared.cloud.example  trailing words are ignored")
    for value in ("*.tunnel.example",  # a private-section suffix
                  "*.x.rent.example",  # covered by *.rent.example
                  "*.rent.example",  # names below it are public suffixes
                  "*.cloud.example",  # a public suffix lies below it
                  "*.foo.ck"):
        with pytest.raises(ValueError, match="anyone can have"):
            rt.wildcard_suffix(value, path)
    assert rt.wildcard_suffix("*.www.ck", path) == "www.ck"  # !www.ck: an exception
    assert rt.wildcard_suffix("*.mine.tunnel.example", path) == "mine.tunnel.example"  # someone's own name
    assert rt.wildcard_suffix("*.own.example", path) == "own.example"


def test_names_outside_ascii_are_compared_in_their_xn_form(tmp_path):
    path = psl(tmp_path, icann="公司.cn")
    with pytest.raises(ValueError, match="anyone can have"):
        rt.wildcard_suffix("*.xn--55qx5d.cn", path)


@pytest.mark.parametrize("bad", ["*.*.example", "a.*.example", "!*.example", "a..example", "!a!b.example",
                                 "exa%mple", "a_b-.example"])
def test_a_malformed_list_refuses_every_wildcard(tmp_path, bad):
    path = psl(tmp_path, private=bad)
    with pytest.raises(ValueError, match="every wildcard is refused"):
        rt.wildcard_suffix("*.example.com", path)


def test_a_list_cut_short_or_without_its_sections_refuses_every_wildcard(tmp_path):
    short = psl(tmp_path, pad=10)
    with pytest.raises(ValueError, match="cut short"):
        rt.wildcard_suffix("*.example.com", short)
    sections = tmp_path / "plain.dat"
    sections.write_text("com\nco.uk\n")
    with pytest.raises(ValueError, match="sections are missing"):
        rt.wildcard_suffix("*.example.com", sections)


def test_without_the_list_file_the_built_in_suffixes_stand_in(tmp_path):
    missing = tmp_path / "none.dat"
    for value in ("*.ts.net", "*.hf.space", "*.supabase.co", "*.csb.app", "*.eu.org", "*.devtunnels.ms",
                  "*.lhr.life", "*.co.uk"):
        with pytest.raises(ValueError, match="anyone can have"):
            rt.wildcard_suffix(value, missing)
    assert rt.wildcard_suffix("*.example.com", missing) == "example.com"
