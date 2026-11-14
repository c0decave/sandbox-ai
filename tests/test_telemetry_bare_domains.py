"""The telemetry of the agents whose telemetry is never switchable (Hermes, Qwen Code, DSH, Goose) is refused by the
connection proxy at every name their checks call a telemetry host: a *.DOMAIN entry covers DOMAIN itself too (a legacy
Sentry DSN posts to sentry.io, a PostHog client may post to posthog.com), as check_telemetry refuses to open it."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rt = load("runtime_telemetry_bare_tests", "sandbox/runtime.py")
MODULES = {agent: load(f"{agent}_telemetry_bare_tests", f"sandbox/{agent}.py")
           for agent in ("hermes", "qwen", "dsh", "goose")}


def rules_of(module):
    return module.telemetry_rules(None) if module.__name__.startswith("goose") else module.telemetry_rules()


def decision(rules, host):
    ruleset = rt.Ruleset([{"name": "telemetry", "version": 1, "http": rules}])
    return ruleset.http(rt.Ruleset.request(host, "POST", "/api/1/store/", [["Host", host]]))["decision"]


@pytest.mark.parametrize("agent", sorted(MODULES))
def test_every_name_check_telemetry_refuses_is_refused_by_the_proxy(agent):
    module = MODULES[agent]
    rules = rules_of(module)
    for pattern in module.TELEMETRY_HOSTS:
        bare = pattern[2:] if pattern.startswith("*.") else pattern
        names = [bare] + ([f"o1.ingest.{bare}"] if pattern.startswith("*.") else [])
        for name in names:
            assert module.telemetry_hosts_in([name]), (agent, name)  # the start refuses to open it ...
            assert decision(rules, name) == "deny", (agent, name)  # ... and the proxy refuses to pass it
    assert decision(rules, "github.com") == "allow"
