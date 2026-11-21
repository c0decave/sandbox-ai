"""The package guard end to end: fake npm, PyPI, crates.io and Go registries behind the real connection proxy (real
TLS on both sides, the real HTTP path, the real log), a small fake copy of OSV's malicious packages, and the
supervisor's real approval broker for its questions.

Nothing here talks to a real registry: the proxy's original destination is the fake one, which answers under the
registries' own host names (one certificate each, chosen by SNI)."""
import gzip
import http.server
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import ssl
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_proxy as tp  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
rt = tp.rt


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


approvals = load("sandbox_approvals_packages", "approvals.py")
safeguards = load("sandbox_safeguards_packages", "safeguards.py")

needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
HOSTS = ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "index.crates.io", "static.crates.io",
         "proxy.golang.org", "other.example")
NOW = 1795000000  # the guard's clock in these tests (2026-11-18)
DAY = 86400


def stamp(days_ago):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(NOW - days_ago * DAY))


LISTS = {
    "npm": "evil-pkg\tMAL-2025-1\t*\nleft-pad\tMAL-2025-2\t1.3.1\n@bad/scoped\tMAL-2025-9\t*\n",
    "pypi": "reqeusts\tMAL-2025-3\t*\nrequests\tMAL-2025-4\t2.99.0\n",
    "crates": "rustdecimal\tMAL-2022-1\t*\n",
    "go": "github.com/evil/Mod\tMAL-2025-7\t1.0.1\n",
}


def write_lists(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for ecosystem, text in LISTS.items():
        (directory / f"{ecosystem}.list").write_bytes(b"".join(sorted(line.encode() + b"\n"
                                                                      for line in text.splitlines())))
    return directory


def registry_answers():
    """{(host, path): (status, content type, body)} of the fake registries."""
    packument = {"name": "left-pad", "time": {"created": stamp(900), "modified": stamp(2), "1.3.0": stamp(800),
                                              "1.3.1": stamp(400), "1.3.2": stamp(2)},
                 "versions": {"1.3.0": {}, "1.3.1": {}, "1.3.2": {}}}
    brand_new = {"name": "brand-new", "time": {"created": stamp(3), "modified": stamp(3), "1.0.0": stamp(3)},
                 "versions": {"1.0.0": {}}}
    scoped = {"name": "@scope/pkg", "time": {"created": stamp(500), "2.0.0": stamp(100)}, "versions": {"2.0.0": {}}}
    simple = {"meta": {"api-version": "1.1"}, "name": "requests", "versions": ["2.31.0", "2.32.0"], "files": [
        {"filename": "requests-2.31.0-py3-none-any.whl", "upload-time": stamp(300)},
        {"filename": "requests-2.31.0.tar.gz", "upload-time": stamp(300)},
        {"filename": "requests-2.32.0-py3-none-any.whl", "upload-time": stamp(5)}]}
    index = "\n".join(json.dumps(entry) for entry in (
        {"name": "serde", "vers": "1.0.0", "pubtime": stamp(700)},
        {"name": "serde", "vers": "1.0.1", "pubtime": stamp(1)}))
    return {
        ("registry.npmjs.org", "/left-pad"): (200, "application/json", json.dumps(packument).encode()),
        ("registry.npmjs.org", "/brand-new"): (200, "application/json", json.dumps(brand_new).encode()),
        ("registry.npmjs.org", "/@scope%2fpkg"): (200, "application/json", json.dumps(scoped).encode()),
        ("registry.npmjs.org", "/nope-not-there"): (404, "application/json", b'{"error":"Not found"}'),
        ("pypi.org", "/simple/requests/"): (200, "application/vnd.pypi.simple.v1+json", json.dumps(simple).encode()),
        ("pypi.org", "/simple/halucinated/"): (404, "text/plain", b"404 Not Found"),
        ("index.crates.io", "/se/rd/serde"): (200, "text/plain", index.encode()),
        ("proxy.golang.org", "/github.com/good/mod/@v/v1.2.0.info"):
            (200, "application/json", json.dumps({"Version": "v1.2.0", "Time": stamp(4)}).encode()),
        ("proxy.golang.org", "/github.com/good/mod/@v/v1.1.0.info"):
            (200, "application/json", json.dumps({"Version": "v1.1.0", "Time": stamp(90)}).encode()),
        ("proxy.golang.org", "/github.com/nobody/@v/list"): (404, "text/plain", b"not found"),
    }


def registries():
    """A handler class for the fake registries and the list of what reached it: (host, path, headers)."""
    seen, answers = [], registry_answers()

    class Registry(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            seen.append((host, self.path, dict(self.headers.items())))
            status, kind, body = answers.get((host, self.path), (200, "application/octet-stream",
                                                                 b"file:" + self.path.encode()))
            if "gzip" in (self.headers.get("Accept-Encoding") or "") and (host, self.path) in answers:
                body = gzip.compress(body)
                encoding = "gzip"
            else:
                encoding = None
            self.send_response(status)
            self.send_header("Content-Type", kind)
            if encoding:
                self.send_header("Content-Encoding", encoding)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Registry, seen


@pytest.fixture
def world(tmp_path):
    """The fake registries behind a real proxy with the package guard: send(host, path) -> the agent's answer."""
    session, upstream = tmp_path / "session-run", tmp_path / "upstream-run"
    rt.create_ca(session, tmp_path / "session-ca", bundles=())
    rt.create_ca(upstream, tmp_path / "upstream-ca", bundles=())
    contexts = {}
    for host in HOSTS:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(rt.server_certificate(upstream, host)), str(upstream / "leaf.key"))
        contexts[host] = context
    front = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    front.load_cert_chain(str(rt.server_certificate(upstream, HOSTS[0])), str(upstream / "leaf.key"))

    def choose(sock, name, _context):
        if name in contexts:
            sock.context = contexts[name]
    front.sni_callback = choose
    pki = {"run": session, "ca": tmp_path / "session-ca" / "ca.pem", "server": front,
           "upstream": ssl.create_default_context(cafile=str(upstream / "ca.pem"))}
    handler, seen = registries()
    server = tp.serve(handler, front)
    made = []

    class World:
        def start(self, packages=None, asker=None, **config):
            lists = write_lists(tmp_path / "lists")
            proxy = tp.Proxy(tmp_path, pki, server.server_address[1],
                             {"packages": {"lists": str(lists), **(packages or {})}, **config})
            proxy.server.packages.clock = lambda: NOW
            proxy.server.asker = asker
            made.append(proxy)
            self.proxy, self.seen, self.pki, self.port = proxy, seen, pki, server.server_address[1]
            return self

        def send(self, host, path, headers=(), name=None, method="GET"):
            lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close", *headers]
            return tp.https_request(self.proxy.port, pki["ca"], ("\r\n".join(lines) + "\r\n\r\n").encode(),
                                    name=name or host)

        def records(self, until):
            return [r for r in self.proxy.records(until=until) if until(r)]
    yield World()
    for proxy in made:
        proxy.close()
    server.shutdown()


class Asker:
    def __init__(self, decision="allow", scope="once"):
        self.decision, self.scope, self.questions = decision, scope, []

    def __call__(self, kind, details):
        self.questions.append((kind, details))
        return {"decision": self.decision, "scope": self.scope, "reason": "answered"}


# --- what a request asks a registry for ----------------------------------------------------------------------

@pytest.mark.parametrize("ecosystem, path, kind, name, version", [
    ("npm", "/left-pad", "metadata", "left-pad", None),
    ("npm", "/@scope/pkg", "metadata", "@scope/pkg", None),
    ("npm", "/left-pad/-/left-pad-1.3.0.tgz", "download", "left-pad", "1.3.0"),
    ("npm", "/@scope/pkg/-/pkg-2.0.0-beta.1.tgz", "download", "@scope/pkg", "2.0.0-beta.1"),
    ("npm", "/Left-Pad/-/Left-Pad-1.0.0.tgz", "download", "left-pad", "1.0.0"),
    ("npm", "/a/-/other-1.0.0.tgz", "download", "a", None),  # a file under another name: the version is not told
    ("pypi", "/simple/Requests", "metadata", "requests", None),
    ("pypi", "/simple/zope.interface", "metadata", "zope-interface", None),
    ("pypi", "/pypi/requests/json", "metadata", "requests", None),
    ("pypi", "/pypi/requests/2.31.0/json", "metadata", "requests", "2.31.0"),
    ("pypi", "/packages/aa/bb/cc/requests-2.31.0-py3-none-any.whl", "download", "requests", "2.31.0"),
    ("pypi", "/packages/aa/bb/cc/zope_interface-6.0-cp312-cp312-manylinux_2_5_x86_64.whl", "download",
     "zope-interface", "6.0"),
    ("pypi", "/packages/aa/bb/cc/my-long-name-1.0.tar.gz", "download", "my-long-name", "1.0"),
    ("crates", "/se/rd/serde", "metadata", "serde", None),
    ("crates", "/se/rd/serde_json", "metadata", "serde-json", None),
    ("crates", "/3/s/syn", "metadata", "syn", None),
    ("crates", "/crates/serde/serde-1.0.1.crate", "download", "serde", "1.0.1"),
    ("crates", "/api/v1/crates/serde/1.0.1/download", "download", "serde", "1.0.1"),
    ("crates", "/api/v1/crates/serde", "metadata", "serde", None),
    ("go", "/github.com/good/mod/@v/v1.2.0.info", "metadata", "github.com/good/mod", "1.2.0"),
    ("go", "/github.com/good/mod/@v/list", "metadata", "github.com/good/mod", None),
    ("go", "/github.com/good/mod/@latest", "metadata", "github.com/good/mod", None),
    ("go", "/github.com/evil/!mod/@v/v1.0.1.zip", "download", "github.com/evil/Mod", "1.0.1"),
    ("go", "/github.com/good/mod/@v/v2.0.0+incompatible.zip", "download", "github.com/good/mod", "2.0.0"),
])
def test_a_registry_request_names_its_package_and_version(ecosystem, path, kind, name, version):
    found = rt.package_request(ecosystem, path)
    assert (found["kind"], found["name"], found["version"]) == (kind, name, version) and found["ecosystem"] == ecosystem


@pytest.mark.parametrize("ecosystem, path", [
    ("npm", "/-/v1/search"), ("npm", "/-/npm/v1/security/advisories/bulk"), ("npm", "/left-pad/1.3.0"), ("npm", ""),
    ("pypi", "/simple"), ("pypi", "/packages/aa/bb/cc/requests-2.31.0-py3-none-any.whl.metadata"),
    ("pypi", "/pypi/requests"), ("crates", "/config.json"), ("crates", "/xx/yy/serde"),
    ("go", "/sumdb/sum.golang.org/lookup/github.com/x/y@v1.0.0"), ("go", "/github.com/good/mod/@v/v1.2.0.mod"),
    ("maven", "/x"),
])
def test_other_requests_to_a_registry_are_no_package(ecosystem, path):
    assert rt.package_request(ecosystem, path) is None


def test_registry_time_stamps():
    assert rt.package_time("1970-01-02T00:00:00Z") == 86400
    assert rt.package_time("2026-11-18T12:26:40.123Z") == 1795004800
    assert rt.package_time("2026-11-18T13:26:40+01:00") == 1795004800
    assert rt.package_time("2026-11-18 12:26:40") == 1795004800
    assert rt.package_time("2024-02-29T00:00:00Z") == 1709164800  # a leap day
    for value in ("", "yesterday", "2026-13-01T00:00:00Z", None, 5, "1700000000"):
        assert rt.package_time(value) is None


def test_metadata_answers_say_release_dates_and_whether_the_name_exists():
    answers = registry_answers()

    def told(host, path, ecosystem, kind=None):
        status, content_type, body = answers[(host, path)]
        return rt.package_times(rt.package_request(ecosystem, rt.normal_path(path)), status, body, content_type)
    npm = told("registry.npmjs.org", "/left-pad", "npm")
    assert npm["versions"]["1.3.2"] == NOW - 2 * DAY and npm["created"] == NOW - 900 * DAY and not npm["missing"]
    assert told("registry.npmjs.org", "/nope-not-there", "npm") == {"versions": {}, "created": None, "missing": True}
    pypi = told("pypi.org", "/simple/requests/", "pypi")
    assert pypi["versions"] == {"2.31.0": NOW - 300 * DAY, "2.32.0": NOW - 5 * DAY}
    assert pypi["created"] == NOW - 300 * DAY
    crates = told("index.crates.io", "/se/rd/serde", "crates")
    assert crates["versions"] == {"1.0.0": NOW - 700 * DAY, "1.0.1": NOW - DAY}
    go = told("proxy.golang.org", "/github.com/good/mod/@v/v1.2.0.info", "go")
    assert go["versions"] == {"1.2.0": NOW - 4 * DAY} and go["created"] is None
    # Go asks for every prefix of an import path: a 404 there is no made-up name
    assert told("proxy.golang.org", "/github.com/nobody/@v/list", "go") is None
    found = rt.package_request("npm", "/left-pad")
    # npm's abbreviated document has no dates: the versions exist, their age is not known
    short = rt.package_times(found, 200, json.dumps({"versions": {"1.0.0": {}}}).encode())
    assert short == {"versions": {"1.0.0": None}, "created": None, "missing": False}
    # PyPI's HTML index says no dates; an older crates.io index entry has none: the package is not young for it
    assert rt.package_times(rt.package_request("pypi", "/simple/requests"), 200, b"<html>", "text/html") is None
    old = "\n".join(json.dumps(e) for e in ({"name": "serde", "vers": "0.1.0"},
                                           {"name": "serde", "vers": "1.0.1", "pubtime": stamp(1)}))
    assert rt.package_times(rt.package_request("crates", "/se/rd/serde"), 200, old.encode())["created"] is None
    for junk in (b"", b"[1, 2]", b"\xff\xfe", b'{"time": 5}', b"{" * 100000):
        result = rt.package_times(found, 200, junk)
        assert result is None or result["versions"] == {}
    assert rt.package_times(found, 500, b"{}") is None and rt.package_times(found, 304, b"") is None


def test_the_malicious_lists_are_searched_in_place(tmp_path):
    lists = rt.open_package_lists(write_lists(tmp_path / "lists"))
    assert lists["npm"].find("evil-pkg") == [("MAL-2025-1", None)]
    assert lists["npm"].find("left-pad") == [("MAL-2025-2", {"1.3.1"})]
    assert lists["npm"].find("@bad/scoped") == [("MAL-2025-9", None)]
    for name in ("left", "left-pad2", "evil", "zzz", "", "a", "@bad", "left-pad\tMAL-2025-2"):
        assert lists["npm"].find(name) == []
    assert lists["go"].find("github.com/evil/Mod") == [("MAL-2025-7", {"1.0.1"})]
    assert rt.open_package_lists(tmp_path / "missing")["npm"].find("evil-pkg") == []
    # a larger list: every name is found, none that is not there
    names = sorted(f"pkg-{number:05d}" for number in range(0, 6000, 3))
    big = tmp_path / "big.list"
    big.write_bytes(b"".join(sorted(f"{name}\tMAL-1-{index}\t{'*' if index % 2 else '1.0.0,1.0.1'}\n".encode()
                                    for index, name in enumerate(names))))
    found = rt.PackageList.open(big)
    assert all(found.find(name) for name in names[::37])
    assert found.find("pkg-00003") == [("MAL-1-1", None)] and found.find("pkg-00006")[0][1] == {"1.0.0", "1.0.1"}
    assert not any(found.find(f"pkg-{number:05d}") for number in range(1, 6000, 3))
    # several advisories for one name: all of them
    double = rt.PackageList(b"a\tMAL-1\t1.0.0\na\tMAL-2\t2.0.0\nb\tMAL-3\t*\n")
    assert double.find("a") == [("MAL-1", {"1.0.0"}), ("MAL-2", {"2.0.0"})]


def test_allow_list_entries_and_settings():
    assert rt.package_spec("npm:left-pad") == ("npm", "left-pad", None)
    assert rt.package_spec("npm:@Scope/Name@1.2.3") == ("npm", "@scope/name", "1.2.3")
    assert rt.package_spec("pypi:Zope.Interface@6.0") == ("pypi", "zope-interface", "6.0")
    assert rt.package_spec("go:github.com/x/Y@v1.2.3") == ("go", "github.com/x/Y", "1.2.3")
    assert rt.package_spec("crates:serde_json") == ("crates", "serde-json", None)
    for bad in ("left-pad", "maven:x", "npm:", "npm:a b", "npm:x@", 5, "npm:x@1,2"):
        with pytest.raises(ValueError, match="ECOSYSTEM:NAME"):
            rt.package_spec(bad)
    assert rt.package_settings({"min_age_days": 30}) == {"min_age_days": 30}
    for bad in ({}, None, {"min_age": "allow"}, {"malicious": "report"}, {"min_age_days": -1}, {"min_age_days": True},
                {"other": 1}, {"new_names": "block"}):
        with pytest.raises(ValueError, match='"packages" takes'):
            rt.package_settings(bad)
    # the strictest decision and the longest age win: no ruleset widens another, nor the defaults
    merged = rt.package_decisions([("rules.json", {"min_age_days": 30, "new_names": "ask"}),
                                   ("project", {"min_age_days": 7, "min_age": "report"}),
                                   ("policy", {"malicious": "block"})])
    assert {k: merged[k] for k in rt.PACKAGE_DEFAULTS} == {"malicious": "block", "min_age": "ask", "min_age_days": 30,
                                                           "new_names": "ask"}
    assert merged["by"] == {"min_age_days": "rules.json", "new_names": "rules.json", "malicious": "policy"}
    document = {"version": 1, "packages": {"min_age_days": 21}}
    assert rt.Ruleset.validated(document, "mine")["packages"] == {"min_age_days": 21}
    with pytest.raises(rt.RuleError, match='mine: "packages" takes'):
        rt.Ruleset.validated({"version": 1, "packages": {"min_age": "never"}}, "mine")


# --- through the real proxy ----------------------------------------------------------------------------------

@needs_openssl
def test_a_known_malicious_version_is_refused_with_its_advisory(world):
    world.start()  # no approvals: nobody to ask
    answer = world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz")
    assert answer.startswith(b"HTTP/1.1 403")
    assert b"evil-pkg 1.0.0 is a known malicious package (MAL-2025-1, https://osv.dev/vulnerability/MAL-2025-1)" \
        in answer
    assert b"never allowed by a list or for a run" in answer
    assert world.seen == []  # nothing reached the registry
    record = world.records(lambda r: r.get("event") == "denied")[0]
    assert record["package"] == {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "age_days": None,
                                 "malicious": "MAL-2025-1"}
    assert record["rule"] == "malicious" and record["ruleset"] == "package-guard" and record["decision"] == "deny"
    # only the listed version of a package with clean ones
    assert world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.1.tgz").startswith(b"HTTP/1.1 403")
    assert world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.0.tgz").startswith(b"HTTP/1.1 200")
    assert world.send("registry.npmjs.org", "/@bad%2fscoped/-/scoped-0.0.1.tgz").startswith(b"HTTP/1.1 403")


@needs_openssl
def test_the_human_may_allow_a_malicious_version_for_that_one_download(world):
    asker = Asker("allow", "once")
    world.start(asker=asker)
    answer = world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz")
    assert answer.startswith(b"HTTP/1.1 200") and b"file:/evil-pkg/-/evil-pkg-1.0.0.tgz" in answer
    kind, details = asker.questions[0]
    assert kind == "http" and details["ruleset"] == "package-guard" and details["rule"] == "malicious"
    assert details["package"] == {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "age_days": None,
                                  "malicious": "MAL-2025-1"}  # no command that would allow it always
    assert "MAL-2025-1" in details["reason"] and details["call_digest"]
    record = world.records(lambda r: r.get("event") == "http")[0]
    assert record["approval"]["decision"] == "allow" and record["package"]["malicious"] == "MAL-2025-1"
    asker.decision = "deny"
    assert world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 403")
    assert len(asker.questions) == 2  # asked again: the first answer covered one download


@needs_openssl
def test_a_young_version_is_asked_about_with_its_age(world):
    asker = Asker("deny")
    world.start(asker=asker)
    assert world.send("registry.npmjs.org", "/left-pad").startswith(b"HTTP/1.1 200")  # the agent's own request
    old = world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.0.tgz")
    assert old.startswith(b"HTTP/1.1 200") and not asker.questions
    young = world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz")
    assert young.startswith(b"HTTP/1.1 403")
    assert b"left-pad 1.3.2 was released 2 days ago (younger than 14 days)" in young
    assert b"sandbox-ai packages allow npm:left-pad@1.3.2" in young
    kind, details = asker.questions[0]
    assert kind == "http" and details["rule"] == "min-age" and details["package"] == {
        "ecosystem": "npm", "name": "left-pad", "version": "1.3.2", "age_days": 2,
        "allow": "sandbox-ai packages allow npm:left-pad@1.3.2"}
    asker.decision = "allow"
    assert world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz").startswith(b"HTTP/1.1 200")
    records = world.records(lambda r: r.get("package", {}).get("version") in ("1.3.0", "1.3.2"))
    assert [(r["event"], r["package"]["age_days"], r.get("rule")) for r in records] == [
        ("http", 800, None), ("denied", 2, "min-age"), ("http", 2, "min-age")]
    assert records[1]["decision"] == "ask" and records[1]["approval"]["decision"] == "deny"
    # no request left that the agent did not make: the metadata once, the downloads that passed
    assert [(host, path) for host, path, _ in world.seen] == [
        ("registry.npmjs.org", "/left-pad"), ("registry.npmjs.org", "/left-pad/-/left-pad-1.3.0.tgz"),
        ("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz")]


@needs_openssl
def test_where_young_versions_are_only_recorded_they_pass_and_the_record_says_so(world):
    """The mode lax (it asks nothing), or a ruleset's "min_age": "report"."""
    world.start({"min_age": "report"})  # no approvals at all
    world.send("registry.npmjs.org", "/left-pad")
    assert world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz").startswith(b"HTTP/1.1 200")
    record = world.records(lambda r: r.get("package", {}).get("version") == "1.3.2")[0]
    assert record["event"] == "http" and record["package"]["reported"] == "min-age"
    assert record["package"]["age_days"] == 2
    found = safeguards.from_connection(record, "run1", safeguards.Redactor())
    assert found["kind"] == "package" and "released 2 days ago" in found["why"] and "recorded only" in found["why"]
    # a known malicious version is refused there all the same
    assert world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 403")
    # "deny": refused without a question, with the command that allows the package
    world.start({"min_age": "deny"}, asker=Asker("allow"))
    world.send("registry.npmjs.org", "/left-pad")
    refused = world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz")
    assert refused.startswith(b"HTTP/1.1 403") and b"sandbox-ai packages allow npm:left-pad@1.3.2" in refused
    assert not world.proxy.server.asker.questions
    # "block" for a malicious version: refused, never asked
    world.start({"malicious": "block"}, asker=Asker("allow"))
    assert world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 403")
    assert not world.proxy.server.asker.questions


@needs_openssl
def test_a_download_whose_metadata_was_not_seen_is_judged_by_the_lists_alone(world):
    world.start(asker=Asker("deny"))
    answer = world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz")
    assert answer.startswith(b"HTTP/1.1 200")
    record = world.records(lambda r: r.get("package"))[0]
    assert record["package"] == {"ecosystem": "npm", "name": "left-pad", "version": "1.3.2", "age_days": None}
    assert world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.1.tgz").startswith(b"HTTP/1.1 403")


@needs_openssl
def test_npm_is_asked_for_the_document_with_dates_and_a_compressed_answer_is_read(world):
    world.start(asker=Asker("deny"))
    answer = world.send("registry.npmjs.org", "/left-pad", headers=[
        "Accept: application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8, */*",
        "Accept-Encoding: gzip, deflate, br, zstd", 'If-None-Match: "abc"', "User-Agent: npm/11"])
    assert answer.startswith(b"HTTP/1.1 200") and b"Content-Encoding: gzip" in answer
    host, path, headers = world.seen[0]
    assert headers["Accept"] == "application/json" and headers["Accept-Encoding"] == "gzip"
    assert "If-None-Match" not in headers and headers["User-Agent"] == "npm/11"
    assert world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz").startswith(b"HTTP/1.1 403")
    # a client that takes no gzip is not sent any: the answer comes without an encoding
    world.send("registry.npmjs.org", "/@scope%2fpkg", headers=["Accept-Encoding: br, zstd"])
    assert world.seen[-1][2]["Accept-Encoding"] == "identity"
    assert world.proxy.server.packages.seen("npm", "@scope/pkg")
    # once its dates are known, a conditional request passes as the client sent it
    world.send("registry.npmjs.org", "/left-pad", headers=['If-None-Match: "abc"'])
    assert world.seen[-1][2].get("If-None-Match") == '"abc"'
    # a request for anything else on the registry is passed on as it came
    world.send("registry.npmjs.org", "/-/v1/search?text=x", headers=["Accept-Encoding: br"])
    assert world.seen[-1][2]["Accept-Encoding"] == "br"


@needs_openssl
def test_a_name_the_registry_does_not_know_and_a_brand_new_one_are_recorded(world):
    asker = Asker("deny")
    world.start(asker=asker)
    assert world.send("registry.npmjs.org", "/nope-not-there").startswith(b"HTTP/1.1 404")
    assert world.send("registry.npmjs.org", "/nope-not-there").startswith(b"HTTP/1.1 404")
    assert world.send("pypi.org", "/simple/halucinated/").startswith(b"HTTP/1.1 404")
    assert world.send("registry.npmjs.org", "/brand-new").startswith(b"HTTP/1.1 200")
    # the default: recorded, not asked - but the version itself is young, and that is asked
    assert world.send("registry.npmjs.org", "/brand-new/-/brand-new-1.0.0.tgz").startswith(b"HTTP/1.1 403")
    assert asker.questions[0][1]["rule"] == "min-age" and asker.questions[0][1]["package"]["new_name_days"] == 3
    records = world.records(lambda r: isinstance(r.get("package"), dict) and r.get("event") == "http")
    assert [r["package"] for r in records] == [  # each told once
        {"ecosystem": "npm", "name": "nope-not-there", "metadata": "packument", "missing": True},
        {"ecosystem": "pypi", "name": "halucinated", "metadata": "simple", "missing": True},
        {"ecosystem": "npm", "name": "brand-new", "metadata": "packument", "new_name_days": 3}]
    found = [safeguards.from_connection(r, "run1", safeguards.Redactor()) for r in records]
    assert [f["kind"] for f in found] == ["package"] * 3
    assert "npm nope-not-there" in found[0]["what"] and "does not know" in found[0]["why"]
    assert "hallucinated or slopsquatted" in found[0]["why"] and "brand-new" in found[2]["what"]


@needs_openssl
def test_new_names_ask_where_the_run_says_so_and_an_allow_list_entry_passes(world):
    asker = Asker("deny")
    world.start({"new_names": "ask", "min_age_days": 0, "allow": ["npm:left-pad@1.3.2", "pypi:requests"]},
                asker=asker)
    world.send("registry.npmjs.org", "/brand-new")
    answer = world.send("registry.npmjs.org", "/brand-new/-/brand-new-1.0.0.tgz")
    assert answer.startswith(b"HTTP/1.1 403") and b"possibly a hallucinated or slopsquatted name" in answer
    assert asker.questions[0][1]["rule"] == "new-name"
    world.start({"allow": ["npm:left-pad@1.3.2", "pypi:requests", "npm:evil-pkg"]}, asker=asker)
    del asker.questions[:]
    world.send("registry.npmjs.org", "/left-pad")
    world.send("pypi.org", "/simple/requests/", headers=["Accept: application/vnd.pypi.simple.v1+json"])
    assert world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz").startswith(b"HTTP/1.1 200")
    assert world.send("files.pythonhosted.org",
                      "/packages/aa/bb/cc/requests-2.32.0-py3-none-any.whl").startswith(b"HTTP/1.1 200")
    assert not asker.questions
    record = world.records(lambda r: r.get("package", {}).get("version") == "1.3.2")[0]
    assert record["package"]["allowed"] is True and record["package"]["age_days"] == 2
    # an allow-list never covers a known malicious version
    assert world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 403")
    assert asker.questions[0][1]["rule"] == "malicious"


@needs_openssl
def test_pypi_crates_and_go_downloads_are_judged_the_same_way(world):
    asker = Asker("deny")
    world.start(asker=asker)
    # PyPI: a typosquat on the list, a version of a real package on it, a young wheel
    assert b"MAL-2025-3" in world.send("files.pythonhosted.org", "/packages/aa/bb/cc/reqeusts-1.0.tar.gz")
    assert b"MAL-2025-4" in world.send("files.pythonhosted.org", "/packages/a/b/c/requests-2.99.0-py3-none-any.whl")
    world.send("pypi.org", "/simple/requests/", headers=["Accept: application/vnd.pypi.simple.v1+json"])
    assert world.send("files.pythonhosted.org",
                      "/packages/aa/bb/cc/requests-2.31.0-py3-none-any.whl").startswith(b"HTTP/1.1 200")
    young = world.send("files.pythonhosted.org", "/packages/aa/bb/cc/requests-2.32.0-py3-none-any.whl")
    assert young.startswith(b"HTTP/1.1 403") and b"pypi requests 2.32.0 was released 5 days ago" in young
    assert b"sandbox-ai packages allow pypi:requests@2.32.0" in young
    # a wheel's metadata file is no download of code
    assert world.send("files.pythonhosted.org",
                      "/packages/aa/bb/cc/requests-2.32.0-py3-none-any.whl.metadata").startswith(b"HTTP/1.1 200")
    # crates.io: the sparse index's dates, the static host's files; - and _ are one name
    assert b"MAL-2022-1" in world.send("static.crates.io", "/crates/rustdecimal/rustdecimal-1.0.0.crate")
    world.send("index.crates.io", "/se/rd/serde")
    assert world.send("static.crates.io", "/crates/serde/serde-1.0.0.crate").startswith(b"HTTP/1.1 200")
    assert b"crates serde 1.0.1 was released 1 day ago" in world.send("static.crates.io",
                                                                      "/crates/serde/serde-1.0.1.crate")
    # Go: the module proxy's .info says the time; the path's !m is M
    assert b"MAL-2025-7" in world.send("proxy.golang.org", "/github.com/evil/!mod/@v/v1.0.1.zip")
    assert world.send("proxy.golang.org", "/github.com/evil/!mod/@v/v1.0.2.zip").startswith(b"HTTP/1.1 200")
    world.send("proxy.golang.org", "/github.com/good/mod/@v/v1.2.0.info")
    world.send("proxy.golang.org", "/github.com/good/mod/@v/v1.1.0.info")
    assert world.send("proxy.golang.org", "/github.com/good/mod/@v/v1.1.0.zip").startswith(b"HTTP/1.1 200")
    go = world.send("proxy.golang.org", "/github.com/good/mod/@v/v1.2.0.zip")
    assert go.startswith(b"HTTP/1.1 403") and b"go github.com/good/mod 1.2.0 was released 4 days ago" in go
    assert world.send("proxy.golang.org", "/github.com/nobody/@v/list").startswith(b"HTTP/1.1 404")
    assert not world.records(lambda r: r.get("package", {}).get("missing"))  # (Go's prefix probing)
    assert [details["rule"] for _, details in asker.questions] == ["malicious"] * 2 + ["min-age", "malicious",
                                                                                      "min-age", "malicious",
                                                                                      "min-age"]


@needs_openssl
def test_a_registry_behind_another_name_or_an_unclear_path_is_no_way_around(world):
    world.start()
    # an allowed host on the same front end, the registry named in the Host header only
    answer = world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz", name="other.example")
    assert answer.startswith(b"HTTP/1.1 403") and b"MAL-2025-1" in answer
    # encodings and dot segments
    assert world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0%2Etgz").startswith(b"HTTP/1.1 403")
    assert world.send("registry.npmjs.org", "/EVIL-pkg/-/evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 403")
    assert world.send("registry.npmjs.org", "//evil-pkg//-//evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 403")
    unclear = world.send("registry.npmjs.org", "/left-pad/-/x/..%2f..%2f..%2fevil-pkg/-/evil-pkg-1.0.0.tgz")
    assert unclear.startswith(b"HTTP/1.1 403") and b"servers read differently" in unclear
    assert world.seen == []
    # another host is none of the guard's business
    assert world.send("other.example", "/evil-pkg/-/evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 200")
    # a HEAD or a POST is no download
    assert world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz", method="HEAD").startswith(b"HTTP/1.1 501")


@needs_openssl
def test_without_the_guard_nothing_is_judged_or_changed(world, tmp_path):
    """--no-package-guard: proxy.json has no "packages"."""
    world.start()
    (tmp_path / "plain").mkdir()
    plain = tp.Proxy(tmp_path / "plain", world.pki, world.port)
    try:
        assert plain.server.packages is None
        world.proxy = plain
        assert world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 200")
        world.send("registry.npmjs.org", "/left-pad", headers=["Accept: application/vnd.npm.install-v1+json",
                                                              "Accept-Encoding: br"])
        assert world.seen[-1][2]["Accept"] == "application/vnd.npm.install-v1+json"
        assert world.seen[-1][2]["Accept-Encoding"] == "br"
        assert not [r for r in plain.records(until=lambda r: r.get("status") == 200) if "package" in r]
    finally:
        plain.close()


@needs_openssl
def test_an_audit_rulesets_settings_are_recorded_not_enforced(world):
    world.start({"min_age_days": 0, "audit": [{"name": "trial.json", "min_age_days": 30, "min_age": "deny"}]},
                asker=Asker("deny"))
    world.send("registry.npmjs.org", "/left-pad")
    assert world.send("registry.npmjs.org", "/left-pad/-/left-pad-1.3.2.tgz").startswith(b"HTTP/1.1 200")
    record = world.records(lambda r: r.get("package", {}).get("version") == "1.3.2")[0]
    assert record["audit"] == [{"would": "deny", "rule": "(packages min-age)", "ruleset": "trial.json",
                                "reason": "package guard: npm left-pad 1.3.2 was released 2 days ago (younger than "
                                          "30 days)"}]


# --- the questions: the supervisor's broker and the host's lists ----------------------------------------------

def answer_next(directory, decision, scope, seen):
    def run():
        for _ in range(200):
            for path in sorted(directory.glob("*.json")):
                if path.name not in seen:
                    seen.add(path.name)
                    path.with_suffix(".answer").write_text(json.dumps({"decision": decision, "scope": scope}))
                    return
            time.sleep(0.02)
    threading.Thread(target=run, daemon=True).start()


def test_a_malicious_versions_allow_is_capped_to_the_one_download(tmp_path):
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda rules: None), timeout=5, poll=0.02)
    package = {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "age_days": None, "malicious": "MAL-2025-1"}
    details = {"rule": "malicious", "ruleset": "package-guard", "method": "GET", "host": "registry.npmjs.org",
               "path": "/evil-pkg/-/evil-pkg-1.0.0.tgz", "url": "https://registry.npmjs.org/evil-pkg/-/evil-pkg-1.0.0.tgz",
               "reason": "package guard: npm evil-pkg 1.0.0 is a known malicious package (MAL-2025-1, "
                         "https://osv.dev/vulnerability/MAL-2025-1)", "package": package, "call_digest": "d1"}
    seen = set()
    for scope in ("always", "run"):
        answer_next(tmp_path / "a", "allow", scope, seen)
        result = broker.ask("http", details)
        assert result["decision"] == "allow" and result["scope"] == "once"
    assert len(seen) == 2 and not broker.decided  # asked each time: nothing remembered for the run
    ident = sorted(seen)[0][:-5]
    with pytest.raises(ValueError, match="one download at most"):
        broker.control({"action": "allow", "question": ident})
    assert broker.control({"action": "deny", "question": ident})["decided"]["decision"] == "deny"
    assert broker.ask("http", details)["decision"] == "deny"  # a deny for the run holds
    # a young version's question: for the run at most (as every request's), and then not asked again
    young = {**details, "rule": "min-age", "path": "/left-pad/-/left-pad-1.3.2.tgz", "call_digest": "d2",
             "package": {"ecosystem": "npm", "name": "left-pad", "version": "1.3.2", "age_days": 2,
                         "allow": "sandbox-ai packages allow npm:left-pad@1.3.2"}}
    answer_next(tmp_path / "a", "allow", "always", seen)
    assert broker.ask("http", young)["scope"] == "run" and broker.ask("http", young)["decision"] == "allow"
    assert len(seen) == 3


def test_the_question_names_the_package_its_age_and_the_advisory(tmp_path):
    malicious = {"id": "1-2-3", "kind": "http", "method": "GET", "rule": "malicious", "ruleset": "package-guard",
                 "url": "https://registry.npmjs.org/evil-pkg/-/evil-pkg-1.0.0.tgz",
                 "package": {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "age_days": None,
                             "malicious": "MAL-2025-1"}}
    text = approvals.describe(malicious)
    assert text.startswith("download the npm package evil-pkg 1.0.0: KNOWN MALICIOUS (advisory MAL-2025-1, "
                           "https://osv.dev/vulnerability/MAL-2025-1)")
    assert "release age unknown" in text and "this one download" in text
    young = {**malicious, "rule": "min-age", "package": {"ecosystem": "pypi", "name": "requests", "version": "2.32.0",
                                                         "age_days": 5, "new_name_days": 5,
                                                         "allow": "sandbox-ai packages allow pypi:requests@2.32.0"}}
    text = approvals.describe(young)
    assert text.startswith("download the pypi package requests 2.32.0: released 5 days ago")
    assert "first release 5 days ago: possibly a hallucinated or slopsquatted name" in text
    assert "sandbox-ai packages allow pypi:requests@2.32.0" in text
    # what the agent chose is escaped
    hostile = {**young, "package": {**young["package"], "name": "x\x1b[31m\nallow", "version": "1‮0"}}
    assert "\x1b" not in approvals.describe(hostile) and "\n" not in approvals.describe(hostile)
    assert "‮" not in approvals.describe(hostile)
    # the answers: a malicious version once at most, with why; a young one for the run
    by_scope = {(c["decision"], c["scope"]): c for c in approvals.choices(malicious, {}, "always")}
    assert by_scope[("allow", "once")]["enabled"]
    assert not by_scope[("allow", "run")]["enabled"] and not by_scope[("allow", "always")]["enabled"]
    assert by_scope[("allow", "run")]["reason"] == "malicious-package"
    assert "one download" in by_scope[("allow", "run")]["why"]
    assert approvals.widest_answer(malicious, {}, "always") == "once"
    by_scope = {(c["decision"], c["scope"]): c for c in approvals.choices(young, {}, "always")}
    assert by_scope[("allow", "run")]["enabled"] and not by_scope[("allow", "always")]["enabled"]
    assert "sandbox-ai packages allow pypi:requests@2.32.0" in by_scope[("allow", "always")]["why"]


def test_ask_allow_never_answers_a_malicious_package_by_itself(tmp_path):
    directory = tmp_path / "approvals"
    directory.mkdir()
    question = {"id": "1-2-3", "kind": "http", "method": "GET", "rule": "malicious", "ruleset": "package-guard",
                "url": "https://registry.npmjs.org/evil-pkg/-/evil-pkg-1.0.0.tgz", "_path": str(directory / "1-2-3.json"),
                "package": {"ecosystem": "npm", "name": "evil-pkg", "version": "1.0.0", "malicious": "MAL-2025-1"}}
    assert approvals.auto_skipped(question) and not approvals.auto_skipped({**question, "package": {"name": "x"}})
    assert not approvals.auto_skipped({"kind": "connect"}) and approvals.auto_skipped({"kind": "canary"})


# --- performance -----------------------------------------------------------------------------------------------

def test_the_guard_costs_other_requests_next_to_nothing(tmp_path):
    guard = rt.PackageGuard({}, rt.open_package_lists(write_lists(tmp_path / "lists")), clock=lambda: NOW)
    server = type("Server", (), {"packages": guard})()
    headers = [("Host", "api.example.com"), ("Accept", "*/*")]
    started = time.perf_counter()
    for _ in range(20000):
        assert rt.ProxyServer.package_found(server, "api.example.com", headers, "GET", "/v1/items?x=1") is None
    other = (time.perf_counter() - started) / 20000
    assert other < 20e-6, f"{other * 1e6:.1f} us per request that is none of a registry's"
    # a download: told, looked up in the mapped list and judged; a version that passed is not judged again
    guard.learn(rt.package_request("npm", "/left-pad"), 200, registry_answers()[("registry.npmjs.org", "/left-pad")][2])
    found = rt.package_request("npm", "/left-pad/-/left-pad-1.3.0.tgz")
    lookups = []
    find = rt.PackageList.find
    guard.lists["npm"].find = lambda name: lookups.append(name) or find(guard.lists["npm"], name)
    started = time.perf_counter()
    for _ in range(5000):
        assert guard.verdict(found)["decision"] == "allow"
    judged = (time.perf_counter() - started) / 5000
    assert lookups == ["left-pad"] and judged < 20e-6, f"{judged * 1e6:.1f} us per download judged again"
    started = time.perf_counter()
    for number in range(5000):
        rt.package_request("npm", f"/pkg-{number}/-/pkg-{number}-1.0.0.tgz")
    assert (time.perf_counter() - started) / 5000 < 50e-6


def test_the_guard_takes_only_settings_that_are_settings():
    guard = rt.PackageGuard({"malicious": "allow", "min_age": 5, "min_age_days": "0", "new_names": "ask",
                             "new_name_days": -3, "allow": ["npm:ok", "junk", 7], "registries": {"Mirror.Example": "npm",
                                                                                                "x.example": "gem"},
                             "audit": [{"name": "trial", "min_age": "deny"}, "junk"]}, {})
    assert guard.settings == {"malicious": "deny", "min_age": "ask", "min_age_days": 14, "new_names": "ask"}
    assert guard.new_name_days == 30 and guard.allowed == {("npm", "ok", None)}
    assert guard.ecosystem(["mirror.example"]) == "npm" and guard.ecosystem(["x.example"]) is None
    assert guard.ecosystem(["api.example", "pypi.org"]) == "pypi" and guard.ecosystem([]) is None
    assert [source["name"] for source in guard.audits] == ["trial"]
    assert rt.PackageGuard(None, {}).settings == rt.PACKAGE_DEFAULTS
    assert rt.PackageGuard({"min_age_days": 0, "min_age": "report"}, {}).settings["min_age_days"] == 0


def test_the_dates_kept_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PACKAGE_NAMES", 50)
    monkeypatch.setattr(rt, "PACKAGE_VERSIONS", 400)
    guard = rt.PackageGuard({}, {}, clock=lambda: NOW)
    for number in range(300):
        document = {"time": {"created": stamp(100), **{f"1.0.{v}": stamp(50) for v in range(10)}}}
        guard.learn(rt.package_request("npm", f"/pkg-{number}"), 200, json.dumps(document).encode())
    assert len(guard.known) <= 50 and guard.versions <= 400 and guard.versions == sum(
        len(entry["versions"]) for entry in guard.known.values())
    assert guard.seen("npm", "pkg-299") and not guard.seen("npm", "pkg-0")
    guard.forget()
    assert not guard.known and guard.versions == 0


def test_metadata_answers_are_read_a_few_at_a_time_and_never_past_their_limits(tmp_path, monkeypatch):
    guard = rt.PackageGuard({}, {}, clock=lambda: NOW)
    found = rt.package_request("npm", "/left-pad")
    document = json.dumps({"time": {"created": stamp(100), "1.0.0": stamp(3)}}).encode()
    assert guard.read(found, 200, [document[:10], document[10:]], len(document), []) == {}
    assert guard.verdict(rt.package_request("npm", "/left-pad/-/left-pad-1.0.0.tgz"))["package"]["age_days"] == 3
    # larger than the guard reads, an encoding it does not read, a bomb: said, nothing kept, nothing raised
    assert guard.read(found, 200, [], rt.PACKAGE_METADATA_LIMIT + 1, []) == {"unread": "larger than the guard reads"}
    assert guard.read(found, 200, [b"\x1b"], 1, [("Content-Encoding", "br")]) == {
        "unread": "an encoding the guard does not read"}
    monkeypatch.setattr(rt, "PACKAGE_UNPACKED_LIMIT", 4096)
    bomb = gzip.compress(b'{"time": {"1.0.0": "' + b"x" * 100000 + b'"}}')
    assert guard.read(found, 200, [bomb], len(bomb), [("Content-Encoding", "gzip")]) == {
        "unread": "an encoding the guard does not read"}
    small = gzip.compress(document)
    assert guard.read(found, 200, [small], len(small), [("Content-Encoding", "gzip")]) == {}
    # as many answers as PACKAGE_READING are kept at once: one more passes unread, a place given back is free again
    taken = [guard.reading.acquire(blocking=False) for _ in range(rt.PACKAGE_READING + 1)]
    assert taken == [True] * rt.PACKAGE_READING + [False]
    guard.reading.release()
    assert guard.reading.acquire(blocking=False)


@needs_openssl
def test_a_metadata_answer_that_breaks_off_gives_its_place_back(world):
    world.start()
    guard = world.proxy.server.packages
    for _ in range(rt.PACKAGE_READING + 3):
        assert world.send("registry.npmjs.org", "/left-pad").startswith(b"HTTP/1.1 200")
        assert world.send("registry.npmjs.org", "/nope-not-there").startswith(b"HTTP/1.1 404")
    deadline = time.time() + 5
    while time.time() < deadline and guard.reading._value != rt.PACKAGE_READING:
        time.sleep(0.05)
    assert guard.reading._value == rt.PACKAGE_READING  # every place free again
    assert guard.seen("npm", "left-pad")


@needs_openssl
def test_the_whole_chain_from_the_download_to_the_humans_answer(world, tmp_path):
    """The proxy asks the supervisor over its channel, the supervisor's broker files the question where the host reads
    it, the host's answer comes back: a malicious version passes once for an "always", and is asked about again."""
    directory = tmp_path / "approvals"
    broker = rt.ApprovalBroker(directory, rt.RuntimeNetwork({}, None, lambda rules: None), timeout=5, poll=0.02)
    left, right = socket.socketpair()
    threading.Thread(target=rt.serve_asks, args=(right, broker), daemon=True).start()
    world.start(asker=rt.AskChannel(left, timeout=10))
    seen = set()
    answer_next(directory, "allow", "always", seen)
    assert world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz").startswith(b"HTTP/1.1 200")
    (name,) = seen
    question = json.loads((directory / name).read_text())
    assert question["kind"] == "http" and question["package"]["malicious"] == "MAL-2025-1"
    assert question["url"] == "https://registry.npmjs.org/evil-pkg/-/evil-pkg-1.0.0.tgz"
    assert approvals.describe(question).startswith(
        "download the npm package evil-pkg 1.0.0: KNOWN MALICIOUS (advisory MAL-2025-1")
    assert approvals.widest_answer(question, {}, "always") == "once"
    done = json.loads((directory / name).with_suffix(".done").read_text())
    assert done["decision"] == "allow" and done["scope"] == "once"  # the "always" counted for this one download
    record = world.records(lambda r: r.get("event") == "http" and r.get("package"))[0]
    assert record["approval"] == {"decision": "allow", "scope": "once", "reason": "answered"}
    # the next download of it is asked about again; a "deny" for the run then holds without another question
    answer_next(directory, "deny", "run", seen)
    refused = world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz")
    assert refused.startswith(b"HTTP/1.1 403") and b"MAL-2025-1" in refused and len(seen) == 2
    again = world.send("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz")
    assert again.startswith(b"HTTP/1.1 403") and b"you denied it for this run" in again and len(seen) == 2
    assert [(host, path) for host, path, _ in world.seen] == [
        ("registry.npmjs.org", "/evil-pkg/-/evil-pkg-1.0.0.tgz")]  # one download reached the registry
    # nobody answers: refused with the advisory, and the refusal says how to answer
    broker.timeout = 0.3
    slow = world.send("registry.npmjs.org", "/@bad%2fscoped/-/scoped-1.0.0.tgz")
    assert slow.startswith(b"HTTP/1.1 403") and b"not answered yet" in slow and b"MAL-2025-9" in slow
    assert b"sandbox-ai approvals allow " in slow and b"only your answer to its question allows" in slow


@needs_openssl
@pytest.mark.parametrize("guarded", [True, False], ids=["guard", "no-package-guard"])
def test_the_proxy_process_opens_the_lists_before_it_gives_up_its_rights(tmp_path, monkeypatch, guarded):
    """proxy_main (the proxy's own process in a sandbox): the launcher's mount is read while the process may still
    read it; what serves afterwards, as the proxy's user, holds the lists mapped."""
    proxy_dir, run = tmp_path / "proxy", tmp_path / "run"
    proxy_dir.mkdir()
    (proxy_dir / "proxy.json").write_text(json.dumps({
        "port": 0, "allow": {"all": True}, "push_check": False,
        **({"packages": {**rt.PACKAGE_DEFAULTS, "by": {}, "allow": ["npm:left-pad@1.3.2"]}} if guarded else {})}))
    monkeypatch.setattr(rt, "PROXY_DIR", proxy_dir)
    monkeypatch.setattr(rt, "PROXY_RUN", run)
    monkeypatch.setattr(rt, "CA_DIR", tmp_path / "ca")
    monkeypatch.setattr(rt, "PACKAGES_DIR", write_lists(tmp_path / "lists"))
    monkeypatch.setattr(rt.os, "chown", lambda path, uid, gid: None)
    order, servers, opened = [], [], rt.open_package_lists
    monkeypatch.setattr(rt, "open_package_lists", lambda directory=None: order.append("lists") or opened(directory))
    monkeypatch.setattr(rt, "drop_to_proxy_user", lambda settings=None: order.append("drop"))
    monkeypatch.setattr(rt.ProxyServer, "wait", lambda self: 0)
    monkeypatch.setattr(rt.ProxyServer, "serve", lambda self, listeners: servers.append((self, listeners)))
    ready_r, ready_w = os.pipe()
    log = os.open(tmp_path / "log", os.O_WRONLY | os.O_CREAT)
    try:
        assert rt.proxy_main(log, ready_w) == 0
        server = servers[0][0]
        if guarded:
            assert order == ["lists", "drop"]
            assert server.packages.malicious("npm", "evil-pkg", "9.9.9") == "MAL-2025-1"
            assert server.packages.malicious("npm", "left-pad", "1.3.0") is None
            assert ("npm", "left-pad", "1.3.2") in server.packages.allowed
        else:
            assert order == ["drop"] and server.packages is None
    finally:
        os.close(ready_r)
        os.close(log)
        for _, listeners in servers:
            for sock in listeners:
                sock.close()
