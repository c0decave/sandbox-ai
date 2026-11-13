"""Rulesets: the engine (rules, conditions, priorities, layering), HTTP rules in the connection proxy, the MCP
gateway in the relay and the questions both put to the human.

The bypass tests assume an agent the attacker controls in full (arXiv 2606.26479's adaptive evaluation): it
knows the rules and sends whatever bytes it likes, so no model is needed to play it. Each attempt must fail,
and the server behind the proxy or the relay must never see it. The proxy and relay are the real code on real
sockets; only the firewall's redirect is replaced by a fixed destination, as in test_proxy.py."""
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

import test_proxy as tp
from test_proxy import pki  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
rt = tp.rt


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_rules_test", "agent_sandbox.py")
m = load("sandbox_mcp_rules_test", "mcp.py")
approvals = load("approvals_rules_test", "approvals.py")
needs_openssl = pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")


def ruleset(*documents, pinned=None):
    return rt.Ruleset([{"name": f"r{index}", **document} for index, document in enumerate(documents, 1)], pinned)


def http(rules, method="GET", target="/", host="api.example", headers=(), body=rt.AMBIGUOUS, origin=None,
         address=None):
    facts = rt.Ruleset.request(origin or host, method, target, [["Host", host], *headers], address)
    facts["body"] = body
    return rules.http(facts)


def rule(ident, decision, **fields):
    return {"id": ident, "decision": decision, **fields}


# --- checking rulesets: malformed ones stop the start, with what is wrong and where -----------------------

def http_doc(**fields):
    return {"version": 1, "http": [{"id": "x", "host": "a.example", "decision": "deny", **fields}]}


MALFORMED = [
    ([], "a ruleset is a JSON object"),
    ({"http": []}, '"version": 1 is required'),
    ({"version": True}, '"version": 1 is required'),
    ({"version": 1, "rules": []}, "unknown key 'rules'"),
    ({"version": 1, "defaults": {"http": "block"}}, '"defaults" takes'),
    ({"version": 1, "defaults": {"device": "deny"}}, '"defaults" takes'),
    ({"version": 1, "http": {}}, '"http" is a list'),
    ({"version": 1, "http": [["a.example"]]}, "http rule 1: a rule is a JSON object"),
    ({"version": 1, "http": [{"host": "a.example", "decision": "deny"}]}, 'http rule 1: "id" is required'),
    ({"version": 1, "http": [{"id": "a b", "host": "a.example", "decision": "deny"}]}, '"id" is required'),
    (http_doc(decision="block"), 'http rule 1 (x): "decision" is required'),
    ({"version": 1, "http": [{"id": "x", "decision": "deny"}]}, '"host" is required'),
    (http_doc(host="exa mple"), "\"host\" 'exa mple': not a host name"),
    (http_doc(host="https://a.example"), "\"host\" 'https://a.example'"),
    (http_doc(host="*.*.example"), '"host"'),
    (http_doc(host=[]), '"host" is a host name, *.DOMAIN, an address, or * for every host, or a list of up to 100'),
    (http_doc(path="gists"), '"path"'),
    (http_doc(path="/a?x=1"), "a path has no query"),
    (http_doc(methods="GET POST"), '"methods"'),
    (http_doc(priority=-1), '"priority"'),
    (http_doc(priority="1"), '"priority"'),
    (http_doc(priority=True), '"priority"'),
    (http_doc(reason="two\nlines"), '"reason"'),
    (http_doc(hosts=["a.example"]), "unknown key 'hosts'"),
    (http_doc(body={"pattern": "("}), '"body.pattern"'),
    (http_doc(body={"regex": "x"}), "unknown keyword 'regex'"),
    (http_doc(body={"type": "str"}), '"body.type"'),
    (http_doc(body={"additionalProperties": True}), "only false"),
    (http_doc(body={"enum": []}), "1 to 1000"),
    (http_doc(body={"minLength": -1}), "whole number"),
    (http_doc(body={"maximum": "5"}), "a number"),
    (http_doc(body={"optional": "yes"}), "true or false"),
    (http_doc(body={"anyOf": []}), "1 to 100 conditions"),
    (http_doc(body=json.loads('{"not": ' * 20 + "{}" + "}" * 20)), "nest at most 16"),
    (http_doc(query={"q": "x"}), "a condition is a JSON object"),
    (http_doc(headers={"bad header": {}}), "header names"),
    ({"version": 1, "http": [rule("x", "deny", host="a.example"), rule("x", "allow", host="b.example")]},
     "http rule 2 (x): the id x is used twice"),
    ({"version": 1, "http": [rule("x", "deny", host="a.example")], "mcp": [rule("x", "deny", tool="t")]},
     "mcp rule 1 (x): the id x is used twice"),
    ({"version": 1, "mcp": [rule("x", "deny")]}, '"tool" is required'),
    ({"version": 1, "mcp": [rule("x", "deny", tool="t", server="bad server")]}, '"server"'),
    ({"version": 1, "mcp": [rule("x", "deny", tool="rm -rf")]}, '"tool"'),
    ({"version": 1, "mcp": [rule("x", "deny", tool="t", args={"path": {"path_glob": "work/**"}})]},
     "absolute paths"),
    ({"version": 1, "mcp": [rule("x", "deny", tool="t", args=[])]}, '"args" is an object'),
]


@pytest.mark.parametrize("document,message", MALFORMED, ids=[str(i) for i in range(len(MALFORMED))])
def test_malformed_rulesets_are_refused_with_what_and_where(document, message):
    with pytest.raises(rt.RuleError) as caught:
        rt.Ruleset.validated(document, "rules.json")
    assert str(caught.value).startswith("rules.json: ") and message in str(caught.value), str(caught.value)


def test_the_normal_form_is_canonical_and_checking_it_again_changes_nothing():
    document = {"version": 1, "description": "mine", "defaults": {"mcp": "ask"}, "http": [
        rule("gh", "deny", host=["API.GitHub.com.", "*.Example.org"], methods=["post", "Put"],
             path=["/gists/", "/a/./b//c", "/x/%2e%2e/y"], headers={"Content-Type": {"const": "a"}}, priority=7)],
        "mcp": [rule("w", "ask", tool="write_*", args={"path": {"path_glob": "/work/**"}})]}
    checked = rt.Ruleset.validated(document, "x")
    first = checked["http"][0]
    assert first["host"] == ["api.github.com", "*.example.org"] and first["methods"] == ["POST", "PUT"]
    assert first["path"] == ["/gists", "/a/b/c", "/y"] and list(first["headers"]) == ["content-type"]
    assert checked["defaults"] == {"http": "allow", "mcp": "ask", "devices": "allow", "tool": "allow"}
    assert checked["mcp"][0]["server"] == ["*"]
    assert rt.Ruleset.validated(checked, "x") == checked
    assert rt.Ruleset.validated(http_doc(methods=["*", "GET"]), "x")["http"][0]["methods"] == ["*"]
    assert rt.Ruleset.validated(http_doc(host="[2001:DB8::1]"), "x")["http"][0]["host"] == ["2001:db8::1"]


# --- matching: the defects found in Progent's checker are absent ------------------------------------------

def test_patterns_match_the_whole_value():
    """Progent used re.match: the pattern "pacman" allowed "pacman-evil"."""
    rules = ruleset({"version": 1, "mcp": [rule("pkg", "allow", tool="install", args={"name": {"pattern": "pacman"}})]})
    assert rules.mcp("s", "install", {"name": "pacman"})["decision"] == "allow"
    assert rules.mcp("s", "install", {"name": "pacman-evil"})["decision"] == "deny"
    assert rules.mcp("s", "install", {"name": "xpacman"})["decision"] == "deny"
    assert rt.Ruleset.holds({"pattern": "abc$"}, "abc\n") is False  # $ before a final line break: not the whole


def test_a_missing_argument_fails_its_condition_instead_of_skipping_it():
    """Progent checked a restriction only if the argument was there: leaving it out (to get a server-side
    default) passed every rule."""
    allow = ruleset({"version": 1, "mcp": [rule("w", "allow", tool="write_file", args={"path": {"path_glob": "/work/**"}})]})
    assert allow.mcp("s", "write_file", {"path": "/work/a"})["decision"] == "allow"
    assert allow.mcp("s", "write_file", {})["decision"] == "deny"
    assert allow.mcp("s", "write_file", {"content": "x"})["rule"] == "(unlisted)"
    assert allow.mcp("s", "read_file", {})["rule"] is None  # only the tool with allow rules is a list
    # A deny rule catches the omission when the condition says so; an allow rule may make an argument optional.
    deny = ruleset({"version": 1, "mcp": [rule("d", "deny", tool="fetch", args={"url": {"glob": "http://*", "optional": True}})]})
    assert deny.mcp("s", "fetch", {})["decision"] == "deny" and deny.mcp("s", "fetch", {"url": "https://a"})["rule"] is None
    optional = ruleset({"version": 1, "mcp": [rule("o", "allow", tool="ls", args={"depth": {"maximum": 2, "optional": True}})]})
    assert optional.mcp("s", "ls", {})["decision"] == "allow" and optional.mcp("s", "ls", {"depth": 9})["decision"] == "deny"


def test_nothing_matching_has_an_explicit_default():
    """Progent applied the last rule's fallback when nothing matched; here it is the ruleset's default, else allow."""
    assert ruleset().http(rt.Ruleset.request("a", "GET", "/", [])) == {"decision": "allow", "rule": None,
                                                                       "ruleset": None, "reason": None}
    for default in ("deny", "ask"):
        rules = ruleset({"version": 1, "defaults": {"http": default, "mcp": default},
                         "http": [rule("x", "allow", host="*", methods="GET")]})
        assert http(rules, "POST") == {"decision": default, "rule": "(default)", "ruleset": "r1",
                                       "reason": f"POST / on api.example: no rule of r1 allows it (its default is {default})"}
        assert http(rules, "GET")["decision"] == "allow"
        assert rules.mcp("s", "t", {})["rule"] == "(default)"


def test_deny_wins_over_allow_at_equal_priority_and_lower_numbers_come_first():
    for order in ([rule("a", "allow", host="*"), rule("d", "deny", host="*")],
                  [rule("d", "deny", host="*"), rule("a", "allow", host="*")]):
        assert http(ruleset({"version": 1, "http": order}))["rule"] == "d"
    rules = ruleset({"version": 1, "http": [rule("ask", "ask", host="*"), rule("allow", "allow", host="*")]})
    assert http(rules)["rule"] == "ask"  # ask before allow too
    rules = ruleset({"version": 1, "http": [rule("d", "deny", host="*", priority=100),
                                            rule("exception", "allow", host="*", path="/repos/**", priority=10)]})
    assert http(rules, target="/repos/x")["rule"] == "exception" and http(rules, target="/gists")["rule"] == "d"
    # Equal priority and decision: file order.
    rules = ruleset({"version": 1, "http": [rule("first", "deny", host="*"), rule("second", "deny", host="*")]})
    assert http(rules)["rule"] == "first"


# --- matching: hosts, methods, paths, query, headers, body -------------------------------------------------

def test_hosts_paths_and_methods():
    rules = ruleset({"version": 1, "http": [
        rule("gists", "deny", host="api.example", path="/gists/**"),
        rule("sub", "deny", host="*.corp.example", methods=["DELETE"]),
        rule("ip", "deny", host="192.0.2.7", path="/admin/**"),
        rule("one", "deny", host="*", path="/repos/*/secret")]})
    assert http(rules, "POST", "/gists")["rule"] == "gists"  # /** covers the directory itself
    assert http(rules, "GET", "/gists/1/comments")["rule"] == "gists"
    assert http(rules, "GET", "/gistsx")["rule"] is None
    assert http(rules, "DELETE", "/", host="a.corp.example")["rule"] == "sub"
    assert http(rules, "DELETE", "/", host="corp.example")["rule"] is None  # *.DOMAIN is below DOMAIN only
    assert http(rules, "GET", "/admin", origin="192.0.2.7:80", host="anything")["rule"] == "ip"
    assert http(rules, "GET", "/repos/a/secret", host="x.example")["rule"] == "one"
    assert http(rules, "GET", "/repos/a/b/secret", host="x.example")["rule"] is None  # * stays in one segment
    assert rt.path_matches("/a/**/z", "/a/z") and rt.path_matches("/a/**/z", "/a/b/c/z")
    assert not rt.path_matches("/a/**/z", "/a/bz") and rt.path_matches("/**", "")


def verdict_before(rules, facts):
    """Ruleset.http as it was before 0.1.94's second performance round: every rule of every source held against the
    request (http_match), the hosts compared by Ruleset.host for each rule."""
    def applies(rule):
        return rt.Ruleset.host(rule["host"], facts["names"]) or facts["address"] in rule["addresses"]
    methods = list(facts["methods"])
    method, path = facts["methods"][0], facts["paths"][0] or "/"
    shown_host = facts["claimed"] or facts["server"]
    applying = [source["name"] for source in rules.sources if any("*" not in rule["host"] and applies(rule)
                                                                   for rule in source["http"])]
    if applying and facts["claimed"] and not facts["literal"] and facts["claimed"] != facts["server"]:
        return {"decision": "deny", "rule": "(host)", "ruleset": applying[0]}
    best, model = None, bool(facts.get("model_api")) or rules.provider(facts)
    for source in rules.sources:
        verdict, exempt = None, model and source["name"] != "policy"
        for rule in source["http"]:
            if exempt and "*" in rule["host"]:
                continue
            outcome = rules.http_match(rule, facts, methods)
            if outcome or (outcome is None and rule["decision"] != "allow"):
                verdict = {"decision": rule["decision"], "rule": rule["id"], "ruleset": source["name"]}
                break
        else:
            if any(rule["decision"] == "allow" and "*" not in rule["host"] and applies(rule) for rule in source["http"]):
                verdict = {"decision": "deny", "rule": "(unlisted)", "ruleset": source["name"]}
            elif source["defaults"]["http"] != "allow" and not exempt:
                verdict = {"decision": source["defaults"]["http"], "rule": "(default)", "ruleset": source["name"]}
        if verdict and (best is None or rt.RULE_DECISIONS.index(verdict["decision"]) >
                        rt.RULE_DECISIONS.index(best["decision"])):
            best = verdict
    return best or {"decision": "allow", "rule": None, "ruleset": None}


def test_each_request_meets_only_the_rules_that_apply_with_the_same_verdict():
    """Performance round 2: a request is held against the rules whose hosts or addresses apply (compared through
    Ruleset.matchers), each rule looked at once; the verdict is the one every rule held against it gave."""
    import random
    chosen = random.Random(7)
    patterns = ["*", "*.corp.example", "*.example", "api.example", "a.corp.example", "192.0.2.7", "2001:db8::7",
                "x.example", "*.x.example"]
    names = ["api.example", "a.corp.example", "corp.example", "x.example", "b.x.example", "other.test"]
    for _ in range(400):
        documents = []
        for index in range(chosen.randint(1, 3)):
            rules_ = [rule(f"r{index}-{n}", chosen.choice(rt.RULE_DECISIONS),
                           host=chosen.sample(patterns, chosen.randint(1, 3)),
                           methods=chosen.choice(["*", "GET", ["GET", "POST"]]),
                           path=chosen.choice(["/**", "/a/**", "/b"]))
                      for n in range(chosen.randint(0, 6))]
            documents.append({"version": 1, "http": rules_,
                              **({"defaults": {"http": chosen.choice(rt.RULE_DECISIONS)}} if chosen.random() < 0.3
                                 else {})})
        sources = [{"name": "policy" if index == 0 and chosen.random() < 0.3 else f"r{index}", **document}
                   for index, document in enumerate(documents)]
        rules = rt.Ruleset(sources, {"api.example": ["192.0.2.7"], "x.example": ["2001:db8::7"]},
                           chosen.choice([[], ["api.example"]]))
        for _ in range(5):
            host = chosen.choice(names)
            origin = chosen.choice([host, host, chosen.choice(names), "192.0.2.7:443", "[2001:db8::7]:443"])
            facts = rt.Ruleset.request(origin, chosen.choice(["GET", "POST", "DELETE"]), chosen.choice(["/a/1", "/b", "/"]),
                                       [["Host", host]], chosen.choice([None, "192.0.2.7", "2001:db8::7", "198.51.100.1"]))
            facts.update(body=rt.AMBIGUOUS, taint=None, model_api=chosen.random() < 0.2)
            found = rules.http(facts)
            assert {key: found[key] for key in ("decision", "rule", "ruleset")} == verdict_before(rules, facts), \
                (sources, facts)
            for source in rules.sources:  # the compiled comparison is Ruleset.host's
                for each in source["http"]:
                    assert rules.applies(each, facts) == bool(
                        rt.Ruleset.host(each["host"], facts["names"]) or facts["address"] in each["addresses"])
                    assert rules.applies(each, {**facts, "names": set()}) == (facts["address"] in each["addresses"])


def test_a_host_name_is_told_from_an_address_as_ipaddress_tells_it():
    """Performance round 2: maybe_address() spares host names ipaddress's exception; what is an address stays so."""
    import ipaddress

    def address(value):
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False
    values = ["api.example", "1.2.3.4", "1.2.3.4.", "01.2.3.4", "1.2.3", "::1", "[::1]", "fe80::1%eth0", "", ".",
              "256.1.1.1", "1.2.3.4a", "١.2.3.4", "1.2.3.²", "１.2.3.4", " 1.2.3.4", "2001:db8::7", "a:b"]
    values += [f"1.2.3.{chr(code)}" for code in range(0x3000)] + [chr(code) for code in range(0x3000)]
    for value in values:
        assert rt.Ruleset.is_address(value) == address(value), repr(value)
        assert rt.is_address_origin(f"{value}:443") == address(value.strip("[]")), repr(value)
        if rt.HOST_HEADER.fullmatch(value.strip()):
            found = rt.host_authority(value)
            if found is not None and not value.strip().startswith("["):
                plain = value.strip().lower().rstrip(".")
                assert found[0] == (str(ipaddress.ip_address(plain)) if address(plain) else plain), repr(value)


def test_pinned_addresses_and_the_host_header_carry_the_rules():
    rules = ruleset({"version": 1, "http": [rule("read", "allow", host="api.example", methods="GET")]},
                    pinned={"api.example": ["192.0.2.21"], "other.example": ["192.0.2.99"]})
    assert rules.pins("192.0.2.21") and not rules.pins("192.0.2.99")
    # Another name, but the API's address: its rules hold.
    verdict = http(rules, "POST", "/x", host="www.example", origin="192.0.2.21:443")
    assert verdict["rule"] == "(unlisted)" and "not among the requests allowed for www.example" in verdict["reason"]
    # A verified name with the API's Host header: refused, whatever the rules say.
    verdict = http(rules, "GET", "/x", host="api.example", origin="www.example")
    assert verdict["rule"] == "(host)" and "differs from the server name" in verdict["reason"]
    assert http(rules, "GET", "/x", host="other.example", origin="other.example")["rule"] is None


def test_query_header_and_body_conditions():
    rules = ruleset({"version": 1, "http": [
        rule("search", "allow", host="api.example", path="/search", query={"visibility": {"enum": ["private"]}}),
        rule("json", "allow", host="api.example", methods="POST", path="/pulls",
             headers={"content-type": {"pattern": "application/json(;.*)?"}},
             body={"type": "object", "properties": {"base": {"enum": ["main"]}}, "required": ["head"]})]})
    json_type = [["Content-Type", "application/json"]]
    assert http(rules, target="/search?visibility=private")["decision"] == "allow"
    assert http(rules, target="/search?visibility=public")["decision"] == "deny"
    assert http(rules, target="/search")["decision"] == "deny"  # missing fails
    assert http(rules, "POST", "/pulls", headers=json_type, body={"base": "main", "head": "x"})["decision"] == "allow"
    assert http(rules, "POST", "/pulls", headers=json_type, body={"base": "dev", "head": "x"})["decision"] == "deny"
    assert http(rules, "POST", "/pulls", headers=json_type, body={"base": "main"})["decision"] == "deny"
    assert http(rules, "POST", "/pulls", body={"base": "main", "head": "x"})["decision"] == "deny"  # no header
    assert http(rules, "POST", "/pulls", headers=json_type, body=rt.MISSING)["decision"] == "deny"


def test_conditions_are_three_valued():
    holds = rt.Ruleset.holds
    assert holds({"type": "integer"}, 3) and holds({"type": "number"}, 3) and not holds({"type": "integer"}, 3.5)
    assert holds({"type": ["string", "null"]}, None) and not holds({"type": "integer"}, True)
    assert holds({"const": 1}, 1.0) and not holds({"const": 1}, True) and not holds({"const": True}, 1)
    assert holds({"enum": [{"a": [1]}]}, {"a": [1]}) and not holds({"enum": [{"a": [1]}]}, {"a": [1], "b": 2})
    assert holds({"minLength": 2, "maxLength": 3}, "abc") and not holds({"maxLength": 2}, "abc")
    assert holds({"minimum": 1, "maximum": 2}, 2) and not holds({"minimum": 3}, 2)
    assert holds({"items": {"type": "string"}}, ["a", "b"]) and not holds({"items": {"type": "string"}}, ["a", 1])
    assert holds({"not": {"const": "x"}}, "y") and not holds({"anyOf": [{"const": 1}, {"const": 2}]}, 3)
    assert holds({"allOf": [{"type": "string"}, {"glob": "a*"}]}, "abc")
    assert holds({"properties": {"a": {}}, "additionalProperties": False}, {"a": 1})
    assert not holds({"properties": {"a": {}}, "additionalProperties": False}, {"a": 1, "b": 2})
    assert holds({"glob": ["*.py", "*.txt"]}, "src/x.py") and not holds({"glob": "*.py"}, "x.pyc")
    assert holds({"path_glob": "/work/**"}, "/work//a/./b") and not holds({"path_glob": "/work/**"}, "/work/../etc")
    # A keyword for another type is unknown, never passed: a list where a deny rule expects a string.
    assert holds({"pattern": "rm .*"}, ["rm", "-rf"]) is None and holds({"maximum": 3}, "4") is None
    assert holds({"properties": {"a": {}}}, "a") is None and holds({"items": {}}, {}) is None
    # Unknown values: a duplicate key, a key also given in another case, a relative path.
    assert holds({"const": "x"}, rt.AMBIGUOUS) is None and holds({}, rt.AMBIGUOUS) is True
    assert holds({"properties": {"query": {"const": "q"}}}, {"query": "q", "Query": "m"}) is None
    assert holds({"path_glob": "/work/**"}, "work/x") is None and holds({"not": {"const": 1}}, rt.AMBIGUOUS) is None
    # "." and line breaks: both readings must agree.
    assert holds({"pattern": "git status.*"}, "git status\nrm -rf /") is None
    assert holds({"pattern": "(?s)git status.*"}, "git status\nx") is True
    assert holds({"pattern": "a+"}, "a" * (rt.PATTERN_INPUT_LIMIT + 1)) is None


# --- layering: each ruleset decides on its own, the strictest wins, nobody widens ---------------------------

def test_a_project_ruleset_cannot_widen():
    user = {"version": 1, "http": [rule("no-gists", "deny", host="api.example", path="/gists/**"),
                                   rule("reads", "allow", host="api.example", methods=["GET", "HEAD"])]}
    project = {"version": 1, "http": [rule("everything", "allow", host="*", priority=0),
                                      rule("gists", "allow", host="api.example", methods="POST", path="/gists", priority=0)]}
    rules = rt.Ruleset([{"name": "rules.json", **user}, {"name": "project", **project}])
    assert http(rules, "POST", "/gists") == {"decision": "deny", "rule": "no-gists", "ruleset": "rules.json",
                                             "reason": "POST /gists on api.example is denied by rule no-gists (rules.json)"}
    # The user's allow rules list what the host may get: the project's allow adds nothing.
    assert http(rules, "POST", "/repos/x/issues")["rule"] == "(unlisted)"
    assert http(rules, "GET", "/repos/x")["decision"] == "allow"
    # It may narrow.
    narrower = rt.Ruleset([{"name": "rules.json", **user}, {"name": "project", "version": 1, "http": [
        rule("no-orgs", "deny", host="api.example", path="/orgs/**")]}])
    assert http(narrower, "GET", "/orgs/x")["ruleset"] == "project" and http(narrower, "GET", "/repos")["decision"] == "allow"


def test_the_administrators_ask_cannot_be_answered_by_a_users_allow():
    rules = rt.Ruleset([
        {"name": "policy", "version": 1, "http": [rule("writes", "ask", host="*", methods=["POST", "PUT", "PATCH", "DELETE"])],
         "mcp": [rule("tools", "ask", tool="*")]},
        {"name": "--rules mine.json", "version": 1, "defaults": {"mcp": "allow"}, "http": [
            rule("prs", "allow", host="api.example", methods="POST", path="/repos/*/*/pulls", priority=0)],
         "mcp": [rule("all", "allow", tool="*", priority=0)]}])
    assert http(rules, "POST", "/repos/o/r/pulls")["ruleset"] == "policy"
    assert rules.mcp("files", "read_file", {})["decision"] == "ask"
    # Equally strict: the first ruleset is the one named.
    both = rt.Ruleset([{"name": "policy", "version": 1, "http": [rule("p", "deny", host="*")]},
                       {"name": "mine", "version": 1, "http": [rule("m", "deny", host="*")]}])
    assert http(both)["ruleset"] == "policy"


def test_mcp_rules_and_what_tools_list_hides():
    rules = ruleset({"version": 1, "mcp": [
        rule("reads", "allow", server="files", tool=["read_file", "list_*"]),
        rule("writes", "allow", server="files", tool="write_file", args={"path": {"path_glob": "/work/src/**"}}),
        rule("ask-move", "ask", server="files", tool="move_file"),
        rule("etc", "deny", server="files", tool="read_file", args={"path": {"path_glob": "/etc/**"}}, priority=1),
        rule("anywhere", "deny", tool="shell"),
        rule("rest", "deny", server="files", tool="*", priority=1000)]})  # what is not allowed: the server's list
    assert rules.mcp("files", "read_file", {"path": "/work/a"})["rule"] == "reads"
    assert rules.mcp("files", "read_file", {"path": "/etc/shadow"})["rule"] == "etc"
    assert rules.mcp("files", "write_file", {"path": "/work/src/x.py"})["rule"] == "writes"
    assert rules.mcp("files", "write_file", {"path": "/work/.git/config"})["rule"] == "rest"
    assert rules.mcp("files", "move_file", {})["decision"] == "ask"
    assert rules.mcp("files", "delete_file", {})["rule"] == "rest"
    assert rules.mcp("other", "delete_file", {})["rule"] is None and rules.mcp("other", "shell", {})["rule"] == "anywhere"
    # A tool with allow rules is a list of its own; its name in another case is no way around them.
    assert rules.mcp("other", "write_file", {"path": "/etc/x"})["rule"] is None  # the rules name files only
    assert rules.mcp("files", "Write_File", {"path": "/work/src/x"})["decision"] == "deny"
    hidden = {tool for tool in ("read_file", "list_directory", "write_file", "move_file", "delete_file", "shell")
              if rules.hidden("files", tool)}
    assert hidden == {"delete_file", "shell"}  # write_file and read_file are allowed for some arguments
    assert not rules.hidden("other", "delete_file") and rules.hidden("other", "shell")
    assert rules.mcp_asks() and not ruleset({"version": 1, "mcp": [rule("d", "deny", tool="x")]}).mcp_asks()


# --- HTTP rules in the connection proxy ---------------------------------------------------------------------

def recording():
    """An upstream handler class that records every request it receives (method, path, body), and that list."""
    seen = []

    class Recording(tp.Handler):
        def do_GET(self):
            seen.append((self.command, self.path, b""))
            tp.Handler.do_GET(self)

        def do_POST(self):
            if "chunked" in self.headers.get("Transfer-Encoding", ""):
                data = b""
                while size := int(self.rfile.readline().split(b";")[0], 16):
                    data += self.rfile.read(size)
                    self.rfile.readline()
                self.rfile.readline()
            else:
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append((self.command, self.path, data))
            self.send_response(201)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        do_PUT = do_PATCH = do_DELETE = do_POST
    return Recording, seen


def raw_exchange(port, data, wait=10):
    """Send DATA as the agent would (plain HTTP, any bytes) and read until the proxy closes."""
    received = b""
    with socket.create_connection(("127.0.0.1", port)) as client:
        client.settimeout(wait)
        try:
            client.sendall(data)
            while chunk := client.recv(65536):
                received += chunk
        except (socket.timeout, ConnectionResetError, BrokenPipeError):
            pass
    return received


def checked(document, name):
    return {"name": name, **rt.Ruleset.validated(document, name)}


GITHUB = {"version": 1, "http": [
    rule("no-gists", "deny", host="example.test", path="/gists/**", reason="gists can publish workspace data"),
    rule("no-issue-writes", "deny", host="example.test", methods=["POST", "PATCH", "PUT", "DELETE"],
         path="/repos/*/*/issues/**")]}


@needs_openssl
def test_allow_github_narrowed_by_a_rules_file(tmp_path, pki, monkeypatch):  # noqa: F811
    """The concrete win: --allow-github opens api.github.com for everything; a rules file passed with --rules
    keeps gists and issue writes out, the rest of the API working. (example.test stands in for api.github.com:
    the test's upstream has a certificate for that name.) The proxy's configuration is the launcher's own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    (tmp_path / "github.json").write_text(json.dumps(GITHUB))
    handler, seen = recording()
    upstream = tp.serve(handler, pki["server"])
    a = s.parse_args(["claude", "--allow-github", "--rules", str(tmp_path / "github.json")])
    config = s.proxy_config(a, {"example.test": ["127.0.0.1"]}, "connections.jsonl")
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], {"rules": config["rules"], "pinned": config["pinned"]})

    def send(method, target, body=b"{}"):
        return tp.https_request(proxy.port, pki["ca"], f"{method} {target} HTTP/1.1\r\nHost: example.test\r\n"
                                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
    try:
        assert send("GET", "/repos/me/app").startswith(b"HTTP/1.1 200")
        assert send("POST", "/repos/me/app/pulls").startswith(b"HTTP/1.1 201")
        assert send("GET", "/repos/me/app/issues").startswith(b"HTTP/1.1 200")
        for method, target in (("POST", "/gists"), ("PATCH", "/gists/abc"), ("POST", "/repos/me/app/issues"),
                               ("POST", "/repos/me/app/issues/5/comments"), ("PATCH", "/repos/me/app/issues/5")):
            answer = send(method, target)
            assert answer.startswith(b"HTTP/1.1 403"), (method, target, answer)
        assert b"gists can publish workspace data" in send("POST", "/gists")
        assert [(method, path) for method, path, _ in seen] == [
            ("GET", "/repos/me/app"), ("POST", "/repos/me/app/pulls"), ("GET", "/repos/me/app/issues")]
        records = proxy.records(until=lambda r: r.get("rule") == "no-issue-writes")
        denied = [r for r in records if r.get("event") == "denied"]
        assert {r["rule"] for r in denied} == {"no-gists", "no-issue-writes"}
        assert all(r["decision"] == "deny" and r["ruleset"] == "--rules github.json" for r in denied)
        allowed = [r for r in records if r.get("event") == "http"]
        # Every record names the decision and the rule (none matched: rule null, decision allow).
        assert allowed and all(r["decision"] == "allow" and r["rule"] is None for r in allowed)
    finally:
        proxy.close()
        upstream.shutdown()


class Asker:
    def __init__(self, decision):
        self.decision, self.questions = decision, []

    def __call__(self, kind, details):
        self.questions.append((kind, details))
        return {"decision": self.decision, "scope": "once", "reason": "answered" if self.decision else "no answer"}


@needs_openssl
@pytest.mark.parametrize("decision", ["allow", "deny", None], ids=["approved", "refused", "no-approvals"])
def test_an_ask_rule_puts_the_request_to_the_human(tmp_path, pki, decision):  # noqa: F811
    handler, seen = recording()
    upstream = tp.serve(handler)
    rules = checked({"version": 1, "http": [rule("releases", "ask", host="api.example", methods="POST",
                                                 path="/repos/*/*/releases", body={"type": "object"})]}, "mine")
    proxy = tp.Proxy(tmp_path, pki, upstream.server_address[1], {"rules": [rules]})
    proxy.server.asker = Asker(decision) if decision else None
    try:
        body = b'{"tag_name": "v1", "token": "ghp_' + b"A" * 36 + b'"}'
        answer = raw_exchange(proxy.port, b"POST /repos/me/app/releases HTTP/1.1\r\nHost: api.example\r\n"
                              b"Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        if decision == "allow":
            assert answer.startswith(b"HTTP/1.1 201") and seen[0][2] == body  # passed on as it came
            kind, details = proxy.server.asker.questions[0]
            assert kind == "http" and details["rule"] == "releases" and details["ruleset"] == "mine"
            assert details["method"] == "POST" and details["path"] == "/repos/me/app/releases"
            assert "v1" in details["body"] and "ghp_A" not in details["body"]  # redacted in the question
        else:
            assert answer.startswith(b"HTTP/1.1 403") and not seen
            assert (b"asking is off" if decision is None else b"not approved") in answer
        record = proxy.records(until=lambda r: r.get("rule") == "releases")[-1]
        assert record["decision"] == "ask" and record["rule"] == "releases"
        if decision:
            assert record["approval"]["decision"] == decision
    finally:
        proxy.close()
        upstream.shutdown()


def test_http_questions_go_through_the_broker(tmp_path):
    """Kind http: the answer covers this endpoint under this rule; "always" is capped to the run."""
    broker = rt.ApprovalBroker(tmp_path / "a", rt.RuntimeNetwork({}, None, lambda rules: None), timeout=5, poll=0.05)
    details = {"rule": "releases", "ruleset": "mine", "method": "POST", "host": "api.example",
               "path": "/repos/me/app/releases", "url": "http://api.example/repos/me/app/releases", "reason": "x"}

    def answer():
        for _ in range(100):
            questions = list((tmp_path / "a").glob("*.json"))
            if questions:
                questions[0].with_suffix(".answer").write_text(json.dumps({"decision": "allow", "scope": "always"}))
                return
            time.sleep(0.05)
    threading.Thread(target=answer, daemon=True).start()
    result = broker.ask("http", details)
    assert result["decision"] == "allow" and result["scope"] == "run"
    assert broker.ask("http", details)["decision"] == "allow"  # remembered for the run: not asked again
    assert len(list((tmp_path / "a").glob("*.json"))) == 1
    other = rt.ApprovalBroker.key("http", {**details, "path": "/repos/me/other/releases"})
    assert other != rt.ApprovalBroker.key("http", details)
    question = json.loads(next((tmp_path / "a").glob("*.json")).read_text())
    assert approvals.describe(question) == ("POST http://api.example/repos/me/app/releases (rule releases of mine)")
    # The supervisor's end of the proxy's channel takes the new kind.
    left, right = socket.socketpair()
    threading.Thread(target=rt.serve_asks, args=(right, broker), daemon=True).start()
    left.sendall((json.dumps({"id": 1, "kind": "http", **details}) + "\n").encode())
    assert json.loads(left.makefile("r").readline())["decision"] == "allow"


# --- bypass attempts against HTTP rules: an agent that knows the rules and sends any bytes ------------------

BYPASS = {"version": 1, "http": [
    rule("reads", "allow", host="api.example", methods=["GET", "HEAD"]),
    rule("pulls", "allow", host="api.example", methods="POST", path="/repos/*/*/pulls"),
    rule("graphql-read", "allow", host="api.example", methods="POST", path="/graphql",
         body={"type": "object", "required": ["query"], "additionalProperties": False, "properties": {
             "query": {"type": "string", "pattern": r"(?s)\s*(query\b|\{).*"},
             "variables": {"type": "object", "optional": True}}}),
    rule("no-gists", "deny", host="api.example", path="/gists/**", reason="gists can publish workspace data")]}


@pytest.fixture(scope="module")
def bypass(tmp_path_factory):
    """One upstream that records what reaches it, behind a proxy with the BYPASS rules (plain HTTP: the
    checks are the same as inside TLS, and every byte is the agent's choice)."""
    handler, seen = recording()
    upstream = tp.serve(handler)
    directory = tmp_path_factory.mktemp("bypass")
    log = directory / "connections.jsonl"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    import ssl
    config = {"body_limit": 65536, "allow": {"loopback": [upstream.server_address[1]]},
              "rules": [checked(BYPASS, "rules.json")], "pinned": {"api.example": ["127.0.0.1"]}}
    server = rt.ProxyServer(config, fd, None, run_dir=directory, upstream=ssl.create_default_context(),
                            destination=lambda sock: ("127.0.0.1", upstream.server_address[1]))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    server.serve([listener])
    yield {"port": listener.getsockname()[1], "seen": seen, "log": log}
    listener.close()
    os.close(fd)
    upstream.shutdown()


def request(method, target, headers=(), body=b"", host="api.example", length=True):
    head = f"{method} {target} HTTP/1.1\r\nHost: {host}\r\n" + "".join(f"{n}: {v}\r\n" for n, v in headers)
    if length:
        head += f"Content-Length: {len(body)}\r\n"
    return head.encode() + b"Connection: close\r\n\r\n" + body


def chunked(body, size=7):
    return b"".join(b"%x\r\n" % len(body[i:i + size]) + body[i:i + size] + b"\r\n"
                    for i in range(0, len(body), size)) + b"0\r\n\r\n"


MUTATION = b'{"query": "mutation { createGist(public: true) { id } }"}'
QUERY = b'{"query": "query {\\n  viewer { login }\\n}"}'
GRAPHQL = "/graphql"
JSON = [("Content-Type", "application/json")]
HTTP_PASSES = {
    "get": request("GET", "/repos/me/app"),
    "pull": request("POST", "/repos/me/app/pulls", body=b"{}"),
    "graphql-query": request("POST", GRAPHQL, JSON, QUERY),
    "graphql-query-gzip": request("POST", GRAPHQL, JSON + [("Content-Encoding", "gzip")], gzip.compress(QUERY)),
    "graphql-query-chunked": request("POST", GRAPHQL, JSON + [("Transfer-Encoding", "chunked")], chunked(QUERY),
                                     length=False),
    "graphql-query-expect": request("POST", GRAPHQL, JSON + [("Expect", "100-continue")], QUERY),
}
HTTP_ATTACKS = {
    # Paths servers resolve to /gists: encodings, dot segments, ;parameters, backslashes, case, lenient readings.
    **{f"path {target}": request("POST", target, body=b"{}") for target in (
        "/gists", "/gists/", "//gists", "/%67ists", "/%2567ists", "/x/../gists", "/x/%2e%2e/gists", "/x/..;/gists",
        "/x/..%2fgists", "/x/..%5cgists", "/x\\..\\gists", "/GISTS", "/Gists/1", "/gists;x=1", "/gists.",
        "/gists%20", "/gists%00.json", "/gists%2e", "/repos/me/app/pulls/../../../../gists", "/./gists",
        "/gists?x=1", "/repos/me/app/pulls/%2e%2e/%2e%2e/%2e%2e/%2e%2e/gists")},
    "get gists encoded": request("GET", "/%67ists/1"),
    "get gists case": request("GET", "/GISTS"),
    # Methods: case, overrides a framework would run instead of the request line's.
    "lowercase method": request("post", "/gists", body=b"{}"),
    "mixed-case method": request("PoSt", "/gists", body=b"{}"),
    "method override header": request("POST", "/repos/me/app/pulls", [("X-HTTP-Method-Override", "DELETE")], b"{}"),
    "method override x-http-method": request("POST", "/repos/me/app/pulls", [("X-HTTP-Method", "PUT")], b"{}"),
    "method override query": request("GET", "/repos/me/app?_method=DELETE"),
    "method override form field": request("POST", GRAPHQL, [("Content-Type", "application/x-www-form-urlencoded")],
                                          b"query=query+%7B+x+%7D&_method=DELETE"),
    "unlisted method": request("DELETE", "/repos/me/app"),
    # Paths a framework would take from a header instead of the request line.
    "x-original-url": request("GET", "/repos/me/app", [("X-Original-URL", "/gists")]),
    "x-rewrite-url": request("GET", "/repos", [("X-Rewrite-URL", "/gists/1")]),
    # Hosts: case, trailing dot, port, another name for the pinned address.
    "host case and dot": request("POST", "/gists", body=b"{}", host="API.Example."),
    "host with port": request("POST", "/gists", body=b"{}", host="api.example:80"),
    "other name, same address": request("POST", "/gists", body=b"{}", host="cdn.example"),
    # Smuggling: framing two servers read differently never passes.
    "content-length and chunked": b"POST /repos/me/app/pulls HTTP/1.1\r\nHost: api.example\r\nContent-Length: 4\r\n"
                                  b"Transfer-Encoding: chunked\r\n\r\n0\r\n\r\nPOST /gists HTTP/1.1\r\nHost: api.example\r\n"
                                  b"Content-Length: 2\r\n\r\n{}",
    "chunked, identity": request("POST", "/repos/me/app/pulls", [("Transfer-Encoding", "chunked, identity")],
                                 chunked(b"{}"), length=False),
    "two content-lengths": request("POST", "/repos/me/app/pulls", [("Content-Length", "40")], b"{}"),
    "folded header": b"POST /gists HTTP/1.1\r\nHost: api.example\r\nX-A: 1\r\n  folded\r\nContent-Length: 2\r\n\r\n{}",
    "bare LF": b"POST /gists HTTP/1.1\nHost: api.example\nContent-Length: 2\n\n{}",
    "two hosts": b"POST /gists HTTP/1.1\r\nHost: other.example\r\nHost: api.example\r\nContent-Length: 2\r\n\r\n{}",
    "absolute form": request("POST", "http://api.example/gists", body=b"{}"),
    "fragment": request("POST", "/repos/me/app/pulls#/../../../../gists", body=b"{}"),
    "pipelined": request("GET", "/repos/me/app").replace(b"Connection: close\r\n", b"")
                 + request("POST", "/gists", body=b"{}"),
    # JSON bodies the rule's parser and the server's could read differently, or not at all.
    "mutation": request("POST", GRAPHQL, JSON, MUTATION),
    "duplicate key": request("POST", GRAPHQL, JSON, b'{"query": "query { a }", "query": "mutation { b }"}'),
    "key in another case": request("POST", GRAPHQL, JSON, b'{"query": "query { a }", "Query": "mutation { b }"}'),
    "unicode-escaped key": request("POST", GRAPHQL, JSON, b'{"\\u0071uery": "mutation { b }"}'),
    "unicode-escaped value": request("POST", GRAPHQL, JSON, b'{"query": "\\u006dutation { b }"}'),
    "gzip": request("POST", GRAPHQL, JSON + [("Content-Encoding", "gzip")], gzip.compress(MUTATION)),
    "brotli claimed": request("POST", GRAPHQL, JSON + [("Content-Encoding", "br")], QUERY),
    "two encodings": request("POST", GRAPHQL, JSON + [("Content-Encoding", "gzip"), ("Content-Encoding", "gzip")],
                             gzip.compress(gzip.compress(QUERY))),
    "json sent as text": request("POST", GRAPHQL, [("Content-Type", "text/plain")], MUTATION),
    "form": request("POST", GRAPHQL, [("Content-Type", "application/x-www-form-urlencoded")],
                    b"query=mutation+%7B+b+%7D"),
    "NaN": request("POST", GRAPHQL, JSON, b'{"query": "query { a }", "variables": {"n": NaN}}'),
    "invalid UTF-8": request("POST", GRAPHQL, JSON, b'{"query": "query { \xff }"}'),
    "byte order mark": request("POST", GRAPHQL, JSON, b"\xef\xbb\xbf" + MUTATION),
    "query as a list": request("POST", GRAPHQL, JSON, b'{"query": ["query { a }", "mutation { b }"]}'),
    "operation name": request("POST", GRAPHQL, JSON, b'{"query": "query a { x } mutation b { y }", '
                                                     b'"operationName": "b"}'),
    "chunked": request("POST", GRAPHQL, JSON + [("Transfer-Encoding", "chunked")], chunked(MUTATION), length=False),
    "expect continue": request("POST", GRAPHQL, JSON + [("Expect", "100-continue")], MUTATION),
    "larger than the body limit": request("POST", GRAPHQL, JSON, b'{"query": "query { a }", "variables": {"x": "'
                                          + b"a" * rt.RULE_BODY_LIMIT + b'"}}'),
}


@pytest.mark.parametrize("name", list(HTTP_PASSES))
def test_requests_the_rules_allow_pass(bypass, name):
    bypass["seen"].clear()
    answer = raw_exchange(bypass["port"], HTTP_PASSES[name])
    assert answer.startswith(b"HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 2") or answer.startswith(b"HTTP/1.1 2"), answer
    assert bypass["seen"], "the server got nothing"


@pytest.mark.parametrize("name", list(HTTP_ATTACKS))
def test_http_rules_cannot_be_bypassed(bypass, name):
    bypass["seen"].clear()
    answer = raw_exchange(bypass["port"], HTTP_ATTACKS[name])
    allowed = [(method, path) for method, path, _ in bypass["seen"] if method == "GET" and "gists" not in path.lower()
               and "_method" not in path]
    # Refused (403, 400, or the connection closed); at most the allowed GET of a pipeline reached the server.
    assert bypass["seen"] == [] or (name == "pipelined" and allowed == [("GET", "/repos/me/app")]
                                    and len(bypass["seen"]) == 1), (bypass["seen"], answer[:300])
    status = answer.split(b"\r\n", 1)[0]
    assert b" 2" not in status or name == "pipelined", answer[:300]
    if b"403" in status and b"/gists" in HTTP_ATTACKS[name].split(b"\r\n", 1)[0]:
        assert b"gists can publish workspace data" in answer or b"not among the requests allowed" in answer


def test_every_refusal_is_logged_with_its_rule(bypass):
    bypass["seen"].clear()
    raw_exchange(bypass["port"], HTTP_ATTACKS["duplicate key"])
    raw_exchange(bypass["port"], HTTP_ATTACKS["path /x/..;/gists"])
    records = [json.loads(line) for line in bypass["log"].read_text().splitlines()]
    denied = [r for r in records if r.get("event") == "denied" and r.get("rule")]
    assert {"(unlisted)", "no-gists"} <= {r["rule"] for r in denied}
    assert all(r["ruleset"] == "rules.json" and r["decision"] == "deny" for r in denied)


def test_names_and_paths_are_read_as_servers_read_them():
    """Trailing dots and case in names (SNI or Host), NUL and trailing dots in paths: the stricter reading counts."""
    rules = ruleset({"version": 1, "http": [rule("no-gists", "deny", host="api.example", path="/gists/**"),
                                            rule("reads", "allow", host="api.example", methods="GET")]})
    for origin in ("api.example.", "API.EXAMPLE", "api.example.."):
        assert http(rules, "POST", "/gists", host=origin, origin=origin)["rule"] == "no-gists"
    for target in ("/gists%00", "/gists.", "/gists. . ", "/gists%0a"):
        assert http(rules, "GET", target)["rule"] == "no-gists", target
    assert rt.Ruleset.query("/x?a=1&a=2")["a"] is rt.AMBIGUOUS and rt.Ruleset.query("/x?a=1;b=2") is rt.AMBIGUOUS
    assert rt.Ruleset.query("/x?a=%ff") is rt.AMBIGUOUS and rt.Ruleset.query("/x?a=b+c") == {"a": "b c"}
    assert rt.Ruleset.body(b"", []) is rt.MISSING and rt.Ruleset.body(b'{"a": 1}', []) == {"a": 1}
    assert rt.Ruleset.body(b"a=1&a=2", [["Content-Type", "application/x-www-form-urlencoded"]])["a"] is rt.AMBIGUOUS
    assert rt.Ruleset.body(b"a=1", [["Content-Type", "text/plain"]]) is rt.AMBIGUOUS
    assert rt.Ruleset.body(gzip.compress(b"x" * (rt.RULE_BODY_LIMIT + 1)), [["Content-Encoding", "gzip"]]) is rt.AMBIGUOUS


# --- the MCP gateway in the relay ---------------------------------------------------------------------------

# A stdio MCP server that lists four tools, runs every call it gets (it records them in the file argv[1]) and
# answers everything else with an empty result.
FAKE_SERVER = r"""
import json, sys
TOOLS = ["read_file", "write_file", "delete_file", "run_command"]
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message or "method" not in message:
        if message.get("method") == "tools/call":
            open(sys.argv[1], "a").write(json.dumps(message["params"]) + "\n")
        continue
    if message["method"] == "tools/list":
        result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in TOOLS]}
    elif message["method"] == "tools/call":
        open(sys.argv[1], "a").write(json.dumps(message["params"]) + "\n")
        result = {"content": [{"type": "text", "text": "ran " + message["params"]["name"]}], "isError": False}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""

FILES = {"version": 1, "mcp": [
    rule("reads", "allow", server="files", tool="read_file", args={"path": {"path_glob": "/work/**"}}),
    rule("writes", "allow", server="files", tool="write_file",
         args={"path": {"type": "string", "path_glob": "/work/src/**"}, "content": {"type": "string", "maxLength": 100000}}),
    rule("deletes", "ask", server="files", tool="delete_file"),
    rule("commands", "deny", server="files", tool="run_command", reason="no shell through MCP")]}


class Relay:
    """mcp_serve with the gateway on a real Unix socket, in front of FAKE_SERVER."""

    def __init__(self, gateway=None):
        self.directory = Path(tempfile.mkdtemp(prefix="gw-", dir="/tmp"))  # a short socket path
        self.sock, self.ran_file, self.out = self.directory / "s.sock", self.directory / "ran.jsonl", io.StringIO()
        command = [sys.executable, "-c", FAKE_SERVER, str(self.ran_file)]
        threading.Thread(target=rt.mcp_serve, args=(str(self.sock), command), daemon=True,
                         kwargs={"cwd": "/", "out": self.out, "gateway": gateway}).start()
        for _ in range(200):
            if self.sock.exists():
                break
            time.sleep(0.02)

    def session(self, *messages):
        """Send MESSAGES (dicts, or bytes sent as they are) on one connection; the replies, in order."""
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(30)
            client.connect(str(self.sock))
            for message in messages:
                client.sendall(message if isinstance(message, bytes) else (json.dumps(message) + "\n").encode())
            client.shutdown(socket.SHUT_WR)
            data = b""
            while chunk := client.recv(65536):
                data += chunk
        return [json.loads(line) for line in data.splitlines()]

    def records(self, event=None):
        found = [json.loads(line.split(" ", 1)[1]) for line in self.out.getvalue().splitlines()
                 if line.startswith("sandbox-ai-mcp-call ")]
        return [r for r in found if event is None or r.get("event") == event]

    def ran(self):
        return [json.loads(line)["name"] for line in self.ran_file.read_text().splitlines()] \
            if self.ran_file.exists() else []

    def close(self):
        shutil.rmtree(self.directory, ignore_errors=True)


def call(name, arguments=None, ident=1, **params):
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": name, **({"arguments": arguments} if arguments is not None else {}), **params}}


@pytest.fixture
def files_relay():
    relay = Relay({"server": "files", "rules": [checked(FILES, "rules.json")]})
    yield relay
    relay.close()


def test_the_gateway_allows_denies_and_logs_every_call(files_relay):
    replies = files_relay.session(
        call("read_file", {"path": "/work/README.md"}, 1),
        call("write_file", {"path": "/work/src/a.py", "content": "x"}, 2),
        call("write_file", {"path": "/work/.github/workflows/x.yml", "content": "x"}, 3),
        call("run_command", {"command": "id"}, 4),
        call("delete_file", {"path": "/work/a"}, 5))
    by_id = {reply["id"]: reply for reply in replies}
    assert by_id[1]["result"]["content"][0]["text"] == "ran read_file" and by_id[2]["result"]["isError"] is False
    assert by_id[3]["result"]["isError"] is True
    assert "not among the calls of it allowed for files" in by_id[3]["result"]["content"][0]["text"]
    assert by_id[4]["result"] == {"content": [{"type": "text", "text": "sandbox-ai: no shell through MCP"}], "isError": True}
    # An ask rule on a server that cannot ask (no approvals directory) is refused.
    assert "cannot ask" in by_id[5]["result"]["content"][0]["text"]
    assert files_relay.ran() == ["read_file", "write_file"]
    calls = {r["id"]: r for r in files_relay.records("call")}
    assert {i: (calls[i]["decision"], calls[i]["rule"]) for i in calls} == {
        1: ("allow", "reads"), 2: ("allow", "writes"), 3: ("deny", "(unlisted)"), 4: ("deny", "commands"),
        5: ("ask", "deletes")}
    assert all(r["server"] == "files" and r["ruleset"] == "rules.json" for r in calls.values())
    assert calls[5]["approval"]["decision"] == "deny" and calls[4]["reason"] == "no shell through MCP"
    assert '"path": "/work/src/a.py"' in calls[2]["arguments"]
    results = {r["id"]: r for r in files_relay.records("result")}
    assert set(results) == {1, 2} and results[1]["tool"] == "read_file" and results[1]["is_error"] is False


def test_tools_list_leaves_out_what_is_always_denied(files_relay):
    replies = files_relay.session({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert [tool["name"] for tool in replies[0]["result"]["tools"]] == ["read_file", "write_file", "delete_file"]
    assert files_relay.records("list") and files_relay.records("list")[0]["hidden"] == ["run_command"]
    # Hidden is not the boundary: calling it anyway is refused.
    assert files_relay.session(call("run_command", {"command": "id"}))[0]["result"]["isError"] is True
    assert files_relay.ran() == []


def test_a_relay_without_rules_passes_calls_and_still_logs_them():
    relay = Relay()
    try:
        replies = relay.session(call("run_command", {"command": "id"}), {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert replies[0]["result"]["isError"] is False and len(replies[1]["result"]["tools"]) == 4
        record = relay.records("call")[0]
        assert record["tool"] == "run_command" and "decision" not in record and "server" not in record
    finally:
        relay.close()


def test_an_ask_rule_waits_for_the_answer_in_the_approvals_directory(tmp_path):
    answers = tmp_path / "approvals"
    answers.mkdir()
    relay = Relay({"server": "files", "rules": [checked(FILES, "rules.json")], "approvals": str(answers), "timeout": 20})

    def answer(decision, scope, count):
        """The human, through the log reader: the question arrives on the relay's output; the answer is a file."""
        for _ in range(500):
            asks = [json.loads(line.split(" ", 1)[1]) for line in relay.out.getvalue().splitlines()
                    if line.startswith("sandbox-ai-mcp-ask ")]
            if len(asks) >= count:
                (answers / f"{asks[count - 1]['id']}.answer").write_text(json.dumps({"decision": decision, "scope": scope}))
                return asks[count - 1]
            time.sleep(0.02)
    try:
        asked = []
        threading.Thread(target=lambda: asked.append(answer("allow", "run", 1)), daemon=True).start()
        replies = relay.session(call("delete_file", {"path": "/work/a"}, 1), call("delete_file", {"path": "/work/b"}, 2))
        assert [reply["result"]["isError"] for reply in replies] == [False, False]
        question = asked[0]
        assert question["kind"] == "mcp" and question["tool"] == "delete_file" and question["rule"] == "deletes"
        assert question["server"] == "files" and '"/work/a"' in question["arguments"]
        assert approvals.describe(question).startswith('call the MCP tool delete_file of files with {"path": "/work/a"}')
        calls = relay.records("call")
        assert calls[0]["question"] == question["id"] and calls[0]["approval"] == {
            "decision": "allow", "scope": "run", "reason": "answered", "question": question["id"]}
        assert "question" not in calls[1] and calls[1]["approval"]["scope"] == "run"  # asked once for the run
        assert relay.ran() == ["delete_file", "delete_file"]
        # Another rule's question is asked anew; a deny for once refuses this call only.
        relay2 = Relay({"server": "files", "rules": [checked(FILES, "rules.json")], "approvals": str(answers),
                        "timeout": 20})
        relay, first = relay2, relay
        threading.Thread(target=answer, args=("deny", "once", 1), daemon=True).start()
        reply = relay.session(call("delete_file", {"path": "/work/c"}))[0]
        assert reply["result"]["isError"] and "not approved (answered)" in reply["result"]["content"][0]["text"]
        assert relay.ran() == []
        first.close()
    finally:
        relay.close()


def test_the_log_reader_splits_records_and_questions(tmp_path):
    directory = tmp_path / "mcp" / "files"
    (directory / "approvals").mkdir(parents=True)
    ask = {"time": "t", "id": "1-2-3", "kind": "mcp", "timeout": 60, "tool": "delete_file", "arguments": "{}",
           "rule": "deletes", "ruleset": "rules.json", "reason": "r"}
    record = {"time": "t", "event": "call", "id": 1, "tool": "delete_file", "decision": "ask", "question": "1-2-3",
              "approval": {"decision": "deny", "scope": "once", "reason": "no answer within 60 s"}}
    stream = io.BytesIO(b"agent-sandbox: MCP server ready\n"
                        b"sandbox-ai-mcp-ask " + json.dumps(ask).encode() + b"\n"
                        b"sandbox-ai-mcp-call " + json.dumps(record).encode() + b"\n"
                        b"mcp-server: sandbox-ai-mcp-call {\"event\": \"call\", \"tool\": \"forged\"}\n"
                        b"sandbox-ai-mcp-ask {\"id\": \"../../x\"}\n"
                        b"sandbox-ai-mcp-call not json\n")
    m.split_log(directory, stream)
    log = (directory / "supervisor.log").read_text()
    assert "MCP server ready" in log and "mcp-server: sandbox-ai-mcp-call" in log and "not json" in log
    calls = [json.loads(line) for line in (directory / "mcp-calls.jsonl").read_text().splitlines()]
    assert calls == [{**record, "server": "files"}]
    question = json.loads((directory / "approvals/1-2-3.json").read_text())
    assert question["kind"] == "mcp" and question["server"] == "files"
    assert json.loads((directory / "approvals/1-2-3.done").read_text())["reason"] == "no answer within 60 s"
    assert sorted(p.name for p in (directory / "approvals").iterdir()) == ["1-2-3.done", "1-2-3.json"]
    assert oct((directory / "mcp-calls.jsonl").stat().st_mode & 0o777) == "0o600"


def test_mcp_questions_are_listed_and_answered_like_a_sandboxs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    directory = tmp_path / "state/sandbox-ai/mcp/files"
    (directory / "approvals").mkdir(parents=True)
    (tmp_path / "state/sandbox-ai/mcp/idle").mkdir()  # a server that never asked: not listed
    question = {"id": "1-2-3", "kind": "mcp", "time": "t", "server": "files", "tool": "delete_file",
                "arguments": '{"path": "/work/a"}', "rule": "deletes", "ruleset": "rules.json"}
    (directory / "approvals/1-2-3.json").write_text(json.dumps(question))
    runs = approvals.mcp_runs()
    assert list(runs) == ["mcp-files"] and runs["mcp-files"]["directory"] == str(directory)
    assert [(name, q["id"]) for name, _, q in approvals.questions(runs)] == [("mcp-files", "1-2-3")]
    lines = []
    reply = approvals.answer("1-2-3", "allow", "always", runs=runs, out=lines.append)
    assert reply == {"decision": "allow", "scope": "run"} and "at most for the run" in lines[0]
    assert json.loads((directory / "approvals/1-2-3.answer").read_text()) == reply
    assert approvals.questions(runs) == []  # answered: no longer open


# --- bypass attempts against the MCP gateway ----------------------------------------------------------------

MCP_ATTACKS = {
    "batch with a call": b'[{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_command", '
                         b'"arguments": {"command": "id"}}}]\n',
    "batch of one allowed call": b'[{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": '
                                 b'"read_file", "arguments": {"path": "/work/a"}}}]\n',
    "duplicate method": b'{"jsonrpc": "2.0", "id": 1, "method": "ping", "method": "tools/call", "params": '
                        b'{"name": "run_command", "arguments": {"command": "id"}}}\n',
    "duplicate name": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file", '
                      b'"name": "run_command", "arguments": {"path": "/work/a", "command": "id"}}}\n',
    "duplicate argument": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "write_file", '
                          b'"arguments": {"path": "/work/src/a", "path": "/work/.git/hooks/pre-commit", "content": "x"}}}\n',
    "message across lines": b'{"jsonrpc": "2.0", "id": 1,\n"method": "tools/call", "params": {"name": "run_command",'
                            b' "arguments": {"command": "id"}}}\n',
    "two messages on a line": b'{"jsonrpc": "2.0", "id": 1, "method": "ping"} {"jsonrpc": "2.0", "id": 2, "method": '
                              b'"tools/call", "params": {"name": "run_command", "arguments": {"command": "id"}}}\n',
    "NaN": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file", "arguments": '
           b'{"path": "/work/a", "n": NaN}}}\n',
    "invalid UTF-8": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "run_command\xff", '
                     b'"arguments": {}}}\n',
    "method in another case": {**call("run_command", {"command": "id"}), "method": "Tools/Call"},
    "method with a space": {**call("run_command", {"command": "id"}), "method": "tools/call "},
    "tool name in another case": call("RUN_COMMAND", {"command": "id"}),
    "tool name with a space": call("run_command ", {"command": "id"}),
    "tool name with a zero-width space": call("run​command", {"command": "id"}),
    "tool name with a full-width letter": call("ｒun_command", {"command": "id"}),
    "tool name unicode-escaped": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": '
                                 b'"\\u0072un_command", "arguments": {"command": "id"}}}\n',
    "argument left out": call("write_file", {"content": "x"}),
    "arguments left out": call("write_file"),
    "argument in another case": call("write_file", {"path": "/work/src/a", "Path": "/work/.git/config", "content": "x"}),
    "argument of another type": call("write_file", {"path": ["/work/src/a", "/etc/passwd"], "content": "x"}),
    "arguments not an object": call("read_file", ["/etc/shadow"]),
    "path traversal": call("read_file", {"path": "/work/../etc/shadow"}),
    "relative path": call("read_file", {"path": "../etc/shadow"}),
    "path with a NUL": call("write_file", {"path": "/work/src/\u0000/../../.git/config", "content": "x"}),
    "oversized argument": call("write_file", {"path": "/work/src/a", "content": "x" * 200001}),
    "hidden tool called": call("run_command", {"command": "id"}),
    "call as a notification": {key: value for key, value in call("run_command", {"command": "id"}).items() if key != "id"},
    "id as an object": {**call("run_command", {"command": "id"}), "id": {"a": 1}},
}


@pytest.mark.parametrize("name", list(MCP_ATTACKS))
def test_the_mcp_gateway_cannot_be_bypassed(files_relay, name):
    replies = files_relay.session(MCP_ATTACKS[name])
    assert files_relay.ran() == [], f"the server ran {files_relay.ran()}"
    for reply in replies:  # every refusal is an answer the agent can read, never a silent drop of the connection
        assert "error" in reply or reply["result"].get("isError") is True, reply
    assert files_relay.records("call") or files_relay.records("refused") or name == "call as a notification"


def test_an_open_requests_id_cannot_be_reused(files_relay):
    """A second request with the id of one still open is refused: answers would be attributed to the wrong call."""
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(30)
        client.connect(str(files_relay.sock))
        client.sendall((json.dumps({"jsonrpc": "2.0", "id": 9, "method": "slow/never"}) + "\n").encode())
        client.sendall((json.dumps(call("read_file", {"path": "/work/a"}, 9)) + "\n").encode())
        reply = json.loads(client.makefile("rb").readline())
    assert reply["id"] == 9 and "still open" in reply["error"]["message"] and files_relay.ran() == []


def test_a_message_over_the_limit_closes_the_connection(files_relay, monkeypatch):
    monkeypatch.setattr(rt, "MCP_LINE_LIMIT", 4096)
    line = (json.dumps(call("write_file", {"path": "/work/src/a", "content": "x" * 5000})) + "\n").encode()
    assert files_relay.session(line) == [] and files_relay.ran() == []


# --- the launcher: where rulesets come from, and how they reach the proxy and the gateway --------------------

def write(path, document):
    path.write_text(json.dumps(document))
    return path


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A host HOME, configuration and state outside the workspace, and a workspace."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "work").mkdir()
    (tmp_path / "cfg/sandbox-ai").mkdir(parents=True)
    return tmp_path


def test_the_dry_run_shows_every_ruleset(host, capsys):
    (host / "github.json").write_text(json.dumps(GITHUB))
    s.user_rules_file().write_text(json.dumps({"version": 1, "defaults": {"mcp": "ask"}}))
    s.main(["claude", "--workspace", str(host / "work"), "--rules", str(host / "github.json"),
            "--allow-http", "example.test GET /**", "--dry-run"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["rulesets"] == [
        {"name": "rules.json", "defaults": {"http": "allow", "mcp": "ask", "devices": "allow", "tool": "allow"},
         "http": [], "mcp": [], "devices": [], "tool": []},
        {"name": "--rules github.json", "defaults": {"http": "allow", "mcp": "allow", "devices": "allow",
                                                     "tool": "allow"},
         "http": ["no-gists", "no-issue-writes"], "mcp": [], "devices": [], "tool": []},
        {"name": "--allow-http", "defaults": {"http": "allow", "mcp": "allow", "devices": "allow", "tool": "allow"},
         "http": ["allow-http-1"], "mcp": [], "devices": [], "tool": []},
        # --telemetry off (the default): Claude Code's event log is refused by the proxy too (test_telemetry.py).
        {"name": "telemetry", "defaults": {"http": "allow", "mcp": "allow", "devices": "allow", "tool": "allow"},
         "http": ["telemetry-claude-1"], "mcp": [], "devices": [], "tool": []}]


def test_a_broken_or_reachable_rules_file_stops_the_start(host):
    (host / "bad.json").write_text(json.dumps({"version": 1, "http": [{"id": "x", "host": "a.example"}]}))
    with pytest.raises(ValueError, match=r'bad.json: http rule 1 \(x\): "decision" is required'):
        s.main(["claude", "--workspace", str(host / "work"), "--rules", str(host / "bad.json"), "--dry-run"])
    # Inside the workspace the agent could rewrite it.
    (host / "work/rules.json").write_text(json.dumps(GITHUB))
    with pytest.raises(ValueError, match="--rules .* lies inside"):
        s.main(["claude", "--workspace", str(host / "work"), "--rules", str(host / "work/rules.json"), "--dry-run"])
    # HTTP rules without the proxy could not hold.
    with pytest.raises(ValueError, match="need the connection proxy"):
        s.main(["claude", "--workspace", str(host / "work"), "--no-proxy", "--rules", str(write(host / "gh.json", GITHUB)),
                "--dry-run"])


def test_the_project_file_has_a_rules_key_and_no_rules_option(host):
    config = host / "work" / s.CONFIG_NAME
    write(config, {"version": 1, "agent": "claude", "rules": {"version": 1, "http": [
        rule("no-orgs", "deny", host="api.github.com", path="/orgs/**")]}})
    s.trust_config(config)
    a = s.parse_args(["claude", "--workspace", str(host / "work")])
    path, document = s.load_project_config(a)
    a.project_document = document
    assert [source["name"] for source in s.load_rulesets(a)] == ["project", "telemetry"]  # --telemetry off
    write(config, {"version": 1, "agent": "claude", "rules": {"version": 1, "http": [{"id": "x"}]}})
    s.trust_config(config)
    with pytest.raises(ValueError, match="rules: http rule 1 .x.: \"decision\" is required"):
        s.load_project_config(s.parse_args(["claude", "--workspace", str(host / "work")]))
    # A project file names no rules files (its own rules are hashed with it, a file elsewhere would not be).
    write(config, {"version": 1, "agent": "claude", "options": ["--rules", str(host / "x.json")]})
    s.trust_config(config)
    with pytest.raises(ValueError, match="option --rules is not allowed"):
        s.load_project_config(s.parse_args(["claude", "--workspace", str(host / "work")]))


def test_the_policy_has_a_rules_key(host, monkeypatch):
    path = host / "policy.json"
    write(path, {"version": 1, "rules": {"version": 1, "http": [rule("writes", "ask", host="*", methods="POST")]}})
    real_lstat, real_stat = os.lstat, os.stat

    def as_root(function):
        def call(target, *args, **kwargs):
            info = function(target, *args, **kwargs)
            if Path(target) in (path, path.parent):
                values = list(info)
                values[4], values[0] = 0, (info.st_mode & ~0o7777) | 0o644  # uid, mode
                return os.stat_result(values)
            return info
        return call
    monkeypatch.setattr(s.os, "lstat", as_root(real_lstat))
    monkeypatch.setattr(s.os, "stat", as_root(real_stat))
    policy = s.load_policy(path)
    a = s.parse_args(["claude"])
    s.check_policy(a, policy)
    assert [source["name"] for source in s.load_rulesets(a)] == ["policy", "telemetry"]  # --telemetry off
    write(path, {"version": 1, "rules": {"version": 1, "http": [rule("writes", "maybe", host="*")]}})
    with pytest.raises(ValueError, match="policy.json: rules: http rule 1 .writes.: \"decision\""):
        s.load_policy(path)


def test_an_mcp_server_starts_with_its_gateway(host, monkeypatch):
    write(s.user_rules_file(), {"version": 1, "mcp": [rule("deletes", "ask", server="demo", tool="delete_*")]})
    started = []

    def popen(cmd, **kwargs):
        started.append(cmd)
        if "--internal-log" not in cmd:
            (s.mcp_socket_dir("demo") / "server.sock").touch()
        return mock_process()
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m.launcher(), "remove_stale_pod", lambda *args: False)
    monkeypatch.setattr(m, "PROC", host / "proc")
    a = s.parse_args(["codex"])
    assert m.ensure_running("demo", a) == "started"
    cmd = started[-1]
    serve = cmd[cmd.index("--internal-mcp-serve"):]
    gateway = json.loads(__import__("base64").urlsafe_b64decode(serve[serve.index("--gateway") + 1]))
    assert gateway == {"server": "demo", "approvals": "/sandbox/mcp-approvals", "rules": [
        {"name": "rules.json", "version": 1, "defaults": {"mcp": "allow"},
         "mcp": [{"id": "deletes", "decision": "ask", "priority": 100, "server": ["demo"], "tool": ["delete_*"]}]}]}
    assert cmd[cmd.index("--mcp-approvals-dir") + 1] == str(m.state_dir("demo") / "approvals")
    # Running with these rules: an agent with other rules must not share it.
    m.save_state("demo", {**m.load_state("demo"), "pid": os.getpid()})
    monkeypatch.setattr(m, "status", lambda name: "running")
    assert m.ensure_running("demo", a) == "running"
    write(s.user_rules_file(), {"version": 1})
    with pytest.raises(ValueError, match="different definition or other rules"):
        m.ensure_running("demo", s.parse_args(["codex"]))


def test_mcp_servers_behind_a_vpn_keep_their_gateway(host, monkeypatch):
    """--vpn (feat/vpn) and the gateway (the rules engine) together: the VPN wrapper runs the relay with its rules;
    a command server's sandbox gets the answers directory and the VPN."""
    write(s.user_rules_file(), {"version": 1, "mcp": [rule("deletes", "ask", server="*", tool="delete_*")]})
    write(m.config_file(), {"img": {"image": "ghcr.io/a/b@sha256:" + "a" * 64, "network": "full", "vpn": "corp"}})
    started = []

    def popen(cmd, **kwargs):
        started.append(cmd)
        return mock_process()
    monkeypatch.setattr(m.subprocess, "Popen", popen)
    monkeypatch.setattr(m, "wait_for_socket", lambda *args: None)
    monkeypatch.setattr(m.launcher(), "remove_stale_pod", lambda *args: False)
    monkeypatch.setattr(m, "PROC", host / "proc")
    # The VPN proofs after the start (mcp.verify_network) as passed: no Podman here.
    vpn = m.launcher().load_vpn_module()
    vpn.check_vpn_container = lambda *args, **kwargs: {"id": "c" * 64, "started": "t"}
    vpn.check_pod_vpn = lambda *args, **kwargs: {}
    monkeypatch.setattr(m.launcher(), "load_vpn_module", lambda: vpn)
    assert m.ensure_running("img", s.parse_args(["codex"])) == "started"
    cmd = started[-1]
    assert cmd[2:4] == [str(ROOT / "sandbox/vpn.py"), "--internal-mcp-vpn"] and cmd[4] == "corp"
    serve = cmd[cmd.index("--internal-mcp-serve"):]
    gateway = json.loads(__import__("base64").urlsafe_b64decode(serve[serve.index("--gateway") + 1]))
    assert gateway["server"] == "img" and gateway["approvals"] == str(m.state_dir("img") / "approvals")
    assert "--network=container:sandbox-ai-vpn-mcp-img" in serve and m.load_state("img")["vpn"] == "corp"
    assert m.ensure_running("demo", s.parse_args(["codex"]), vpn="corp") == "started"
    cmd = started[-1]
    options = cmd[:cmd.index("--")]
    assert options[options.index("--vpn") + 1] == "corp"
    assert options[options.index("--mcp-approvals-dir") + 1] == str(m.state_dir("demo") / "approvals")
    assert "--gateway" in cmd[cmd.index("--internal-mcp-serve"):] and m.load_state("demo")["vpn"] == "corp"


def mock_process():
    class Process:
        pid = 4321

        def poll(self):
            return None
    return Process()


def test_the_server_sandbox_mounts_the_answers_read_only(host):
    a = s.parse_args(["codex", "--workspace", str(host / "work"), "--mcp-socket-dir", str(host / "sock"),
                      "--mcp-approvals-dir", str(host / "approvals")])
    s.validate(a)
    _, run = s.pod_commands(a, "pod", host, host / "home")
    assert f"type=bind,src={host / 'approvals'},dst=/sandbox/mcp-approvals,ro,nodev" in run
    a.backend = "bwrap"
    cmd = s.bwrap_command(a, host, host / "home")
    index = cmd.index("/sandbox/mcp-approvals")
    assert cmd[index - 2:index] == ["--ro-bind", str(host / "approvals")]


def test_the_relay_entry_point_takes_the_gateway(tmp_path):
    """python3 -I runtime.py --internal-mcp-serve SOCKET --gateway CONFIG -- COMMAND, as the server sandbox runs it."""
    import base64
    directory = Path(tempfile.mkdtemp(prefix="gw-", dir="/tmp"))
    gateway = {"server": "files", "rules": [checked(FILES, "rules.json")]}
    encoded = base64.urlsafe_b64encode(json.dumps(gateway).encode()).decode()
    relay = subprocess.Popen([sys.executable, "-I", str(ROOT / "sandbox/runtime.py"), "--internal-mcp-serve",
                              str(directory / "s.sock"), "--gateway", encoded, "--", sys.executable, "-c", FAKE_SERVER,
                              str(directory / "ran.jsonl")], stderr=subprocess.PIPE)
    try:
        for _ in range(250):
            if (directory / "s.sock").exists():
                break
            time.sleep(0.02)
        helper = Relay.__new__(Relay)
        helper.sock, helper.ran_file = directory / "s.sock", directory / "ran.jsonl"
        replies = helper.session(call("run_command", {"command": "id"}), call("read_file", {"path": "/work/a"}, 2))
        assert replies[0]["result"]["isError"] and not replies[1]["result"]["isError"]
        assert helper.ran() == ["read_file"]
    finally:
        relay.kill()
        records = relay.communicate(timeout=10)[1].decode()
        shutil.rmtree(directory, ignore_errors=True)
    assert '"rule": "commands"' in records and '"server": "files"' in records
    bad = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox/runtime.py"), "--internal-mcp-serve", "/x.sock",
                          "--gateway", "not-base64!", "--", "true"], capture_output=True, text=True, timeout=30)
    assert bad.returncode == 1


def test_the_agents_model_api_is_not_held_by_rules_for_every_host():
    """0.1.94: a ruleset's rules for every host ("*") and its default are not meant for the agent's own model API -
    a Tor start's "writes" rule asked about every chat completion to the --llm server, and an unanswered one
    stopped the agent for the run. A rule naming the host still holds; so does the administrator's policy."""
    writes = {"id": "writes", "host": "*", "methods": ["POST"], "decision": "ask", "reason": "sends data"}
    preset = {"name": "tor-researcher", "version": 1, "http": [writes]}
    rules = rt.Ruleset([preset], {}, ["api.anthropic.com"])
    llm = rt.Ruleset.request("192.0.2.20:1234", "POST", "/v1/chat/completions", [], "192.0.2.20")
    assert rules.http(llm)["decision"] == "ask"  # not known as the model API: asked
    assert rules.http({**llm, "model_api": True})["decision"] == "allow"
    provider = rt.Ruleset.request("api.anthropic.com", "POST", "/v1/messages", [["Host", "api.anthropic.com"]])
    assert rules.http(provider)["decision"] == "allow"
    other = rt.Ruleset.request("example.org", "POST", "/form", [])
    assert rules.http(other)["decision"] == "ask"
    named = {"name": "mine", "version": 1, "http": [{"id": "no-llm", "host": "192.0.2.20", "methods": ["POST"],
                                                      "decision": "deny"}]}
    assert rt.Ruleset([preset, named]).http({**llm, "model_api": True})["decision"] == "deny"
    default_ask = {"name": "strict", "version": 1, "defaults": {"http": "ask"}, "http": []}
    assert rt.Ruleset([default_ask]).http({**llm, "model_api": True})["decision"] == "allow"
    policy = {"name": "policy", "version": 1, "http": [writes]}
    assert rt.Ruleset([policy]).http({**llm, "model_api": True})["decision"] == "ask"


def test_the_proxy_knows_the_llm_endpoint_of_a_request():
    entry = {"name": "macbook", "scheme": "http", "host": "192.0.2.20", "port": 1234, "addresses": ["192.0.2.20"],
             "prefix": "/v1"}
    assert rt.llm_endpoint([entry], "http", "192.0.2.20:1234", 1234, "/v1/chat/completions") is entry
    assert rt.llm_endpoint([entry], "http", "192.0.2.20:1234", 1234, "/admin") is None
    assert rt.llm_endpoint([entry], "http", "192.0.2.21:1234", 1234, "/v1/chat/completions") is None
    assert rt.llm_endpoint([entry], "http", "192.0.2.20:1235", 1235, "/v1/chat/completions") is None
    assert rt.llm_endpoint([entry], "https", "192.0.2.20:1234", 1234, "/v1/chat/completions") is None


def test_a_request_at_the_llm_endpoint_naming_another_host_is_no_model_api_request():
    """The exemption of the agent's model API from rules for every host holds only for requests that name the
    endpoint in their Host header: a front end routes by it, so "Host: other.example" at the endpoint's name or
    address reaches another server there, and the ruleset's "*" rules (writes asked, a tainted run's) still hold -
    as for the provider, whose claimed name must be the verified one."""
    https = {"name": "gw", "scheme": "https", "host": "llm.example.org", "port": 8443, "addresses": ["192.0.2.30"],
             "prefix": "/v1"}
    http = {"name": "macbook", "scheme": "http", "host": "macbook.lan", "port": 1234, "addresses": ["192.0.2.20"],
            "prefix": "/v1"}
    local = {"name": "ollama", "scheme": "http", "host": "localhost", "port": 11434, "addresses": ["127.0.0.1"]}
    path = "/v1/chat/completions"
    assert rt.model_api_endpoint([https], "https", "llm.example.org", 8443, path,
                                 [("Host", "llm.example.org:8443")]) is https
    assert rt.model_api_endpoint([https], "https", "llm.example.org", 8443, path,
                                 [("Host", "other.example:8443")]) is None
    assert rt.model_api_endpoint([https], "https", "llm.example.org", 8443, path, []) is None
    for host in ("macbook.lan:1234", "192.0.2.20:1234"):
        assert rt.model_api_endpoint([http], "http", "192.0.2.20:1234", 1234, path, [("Host", host)]) is http
    assert rt.model_api_endpoint([http], "http", "192.0.2.20:1234", 1234, path, [("Host", "evil.example")]) is None
    for host in ("localhost:11434", "127.0.0.1:11434"):
        assert rt.model_api_endpoint([local], "http", "127.0.0.1:11434", 11434, "/api/chat", [("Host", host)]) is local
