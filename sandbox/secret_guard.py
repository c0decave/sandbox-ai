#!/usr/bin/env python3
"""sandbox-ai secret-guard: the secret guard's host side - what a start registers with its proxy, and what you allow.

  sandbox-ai secret-guard [status] [--json]               what the guard looks for, and what is allowed here
  sandbox-ai secret-guard allow SPEC... [--project DIR]   let a kind of value go to a host, in every run of the project
  sandbox-ai secret-guard forget SPEC... [--project DIR]  take that back
  sandbox-ai secret-guard list [--project DIR | --all] [--json]     what is allowed

SPEC is WHAT@HOST. WHAT: the id a finding names - a token shape of the privacy check (github-token, google-api-key,
private-key ...; `sandbox-ai secret-guard status` lists them), a value the run was given (env:NAME for --secret-env
and --env, git-token:HOST for --git-token-env/-file, git-login:HOST for a login given into the sandbox,
mcp:SERVER:NAME for an MCP server's variable), or * for any. HOST: a name, *.DOMAIN for every name below it, or *
for any host: github-token@api.example.com, env:API_TOKEN@*.example.com, google-api-key@*.

The guard itself is the connection proxy's (runtime.SecretGuard). In every request the proxy opens it looks for

  - the secrets the run was handed in plain text: --secret-env, --env variables whose name says secret, a Git token
    without --token-proxy, the secret variables of the run's MCP servers, a Git login given into the sandbox - in the
    forms a request may carry them (plain, URL-, JSON-, hex- and base64-encoded, inside a gzip body, across two parts
    of a body), as canary tokens are found; and
  - the token shapes of the privacy check (GitHub, GitLab, AWS, Anthropic, OpenAI, Slack, Google, Stripe, npm, PyPI
    and Hugging Face tokens, private keys), which tell a secret the agent read from a file - also base64-encoded.

On its way to a host it was not given for, a finding is reported (the default: a safeguard record, `sandbox-ai
safeguards --kind secret-send`), asked about, or blocked: a ruleset's "secrets": {"on_send": "report|ask|block",
"on_shape": "report|ask|block"} (on_send: a known value; on_shape: a shape), the strictest wins; the security modes
high and paranoid block a known value, high asks about a shape and paranoid blocks it. A value going to the host it
was given for (a Git token to its server), and a token in the login of its own service (a GitHub token in a header
to github.com), is none.

A request to the run's own model provider is the agent's conversation: a secret in its body entered the model's
context. That is recorded (kind secret-context, once per secret) and never held; nothing the agent sees or sends is
ever changed or redacted.

No record, question or message holds a value: names, kinds and encodings only. Not seen: connections the proxy does
not open (--no-inspect, --allow-tcp, --no-proxy), and a secret a program encodes another way.

The allow-list (~/.config/sandbox-ai/secret-guard.json, per project; --allow-send for one start) is how you say
that a finding is none: a test fixture that looks like a key, a token that belongs to a host of yours.

Standard library only.
"""
import argparse
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()
_modules = {}

VALUE_MIN, VALUE_MAX = 8, 4096   # characters of a value that is registered (shorter: ordinary traffic carries it)
VALUES_LIMIT = 128               # values one start registers
# A variable whose name says secret (the activity log's rule, activity.SECRET_NAME): --env NAME with such a name is
# registered as --secret-env is.
SECRET_NAME = re.compile(r"token|secret|passw|api_?key|apikey|auth|credential|private|cookie|session_?key", re.I)
# The literals a shape's matches begin with: the proxy looks for the shape only where one of them is (a tenth of the
# time). A shape of the privacy check that is missing here is searched whole (a test keeps the two lists the same).
SHAPE_ANCHORS = {
    "private-key": ["-----BEGIN "],
    "aws-access-key-id": ["AKIA", "ASIA"],
    "github-token": ["ghp_", "gho_", "ghu_", "ghs_", "ghr_"],
    "github-fine-grained-token": ["github_pat_"],
    "gitlab-token": ["glpat-"],
    "anthropic-api-key": ["sk-ant-"],
    "openai-api-key": ["sk-"],
    "slack-token": ["xoxa-", "xoxb-", "xoxp-", "xoxo-", "xoxs-", "xoxr-"],
    "google-api-key": ["AIza"],
    "stripe-secret-key": ["sk_live_", "rk_live_"],
    "npm-token": ["npm_"],
    "pypi-token": ["pypi-AgE"],
    "huggingface-token": ["hf_"],
}
# What follows the anchor in every match of a shape (an expression the proxy looks ahead with, so that a look-alike in
# ordinary text - sk-learn, hf_hub, npm_config - costs it nothing): sixteen token characters unless said otherwise;
# none for a private key's header. A test holds every shape's own expression to it.
SHAPE_TAIL = "[A-Za-z0-9_-]{16}"
SHAPE_TAILS = {"private-key": "", "slack-token": "[A-Za-z0-9-]{10}", "aws-access-key-id": "[0-9A-Z]{16}"}
# Where a token of a shape is the login of its own service: in a header or the URL of a request to these hosts it is
# no finding (in a body it is content - but for the run's own model provider, whose login carries its tokens there).
SHAPE_HOSTS = {
    "aws-access-key-id": ["*.amazonaws.com"],
    "github-token": ["github.com", "*.github.com", "ghcr.io", "*.githubusercontent.com"],
    "github-fine-grained-token": ["github.com", "*.github.com", "ghcr.io", "*.githubusercontent.com"],
    "gitlab-token": ["gitlab.com", "*.gitlab.com"],
    "anthropic-api-key": ["anthropic.com", "*.anthropic.com", "claude.ai", "*.claude.ai", "claude.com", "*.claude.com"],
    "openai-api-key": ["openai.com", "*.openai.com", "chatgpt.com", "*.chatgpt.com"],
    "slack-token": ["slack.com", "*.slack.com"],
    "google-api-key": ["*.googleapis.com"],
    "stripe-secret-key": ["api.stripe.com"],
    "npm-token": ["registry.npmjs.org"],
    "pypi-token": ["upload.pypi.org", "test.pypi.org"],
    "huggingface-token": ["huggingface.co", "*.huggingface.co", "*.hf.co"],
}
# Variables everybody names the same: their value was given for these hosts (there it is no finding anywhere).
ENV_HOSTS = {
    "ANTHROPIC_API_KEY": ["api.anthropic.com"], "OPENAI_API_KEY": ["api.openai.com"],
    "GH_TOKEN": ["github.com", "*.github.com"], "GITHUB_TOKEN": ["github.com", "*.github.com"],
    "GITLAB_TOKEN": ["gitlab.com", "*.gitlab.com"], "HF_TOKEN": ["huggingface.co", "*.huggingface.co", "*.hf.co"],
    "HUGGING_FACE_HUB_TOKEN": ["huggingface.co", "*.huggingface.co", "*.hf.co"],
    "GEMINI_API_KEY": ["*.googleapis.com"], "GOOGLE_API_KEY": ["*.googleapis.com"],
    "NPM_TOKEN": ["registry.npmjs.org"], "OPENROUTER_API_KEY": ["openrouter.ai"],
    "MISTRAL_API_KEY": ["api.mistral.ai"], "DEEPSEEK_API_KEY": ["api.deepseek.com"], "XAI_API_KEY": ["api.x.ai"],
    "GROQ_API_KEY": ["api.groq.com"],
}
VALUE_ID = re.compile(r"(?:env:[A-Za-z_][A-Za-z0-9_]{0,63}|git-(?:token|login):[a-z0-9\[\]][a-z0-9.:\[\]-]{0,99}"
                      r"|mcp:[A-Za-z0-9_-]{1,32}:[A-Za-z_][A-Za-z0-9_]{0,63})")
HOST = re.compile(r"\*|(?:\*\.)?(?:[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?\.)*[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?")


def module(name):
    """A sibling module, loaded once by its path (the launcher runs with python -I)."""
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_secret_guard", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            _modules[name] = loaded
        return _modules[name]


def runtime():
    return module("runtime")


def printable(text, limit=300):
    text = "".join(char if char.isprintable() else "?" for char in str(text))
    return text if len(text) <= limit else text[:limit - 1] + "…"


def stamp(seconds=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def allow_file():
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "sandbox-ai" / "secret-guard.json"


# --- what is looked for -----------------------------------------------------------------------------------------------

def forms(value):
    """{text: encoding}: VALUE as a request may carry it (canary.forms: plain, URL-, JSON-, hex- and base64-encoded)."""
    return module("canary").forms(value)


def builtin_shapes():
    """[(id, regex, label)] of the privacy check's blocking token shapes (privacy.BUILTIN)."""
    privacy = module("privacy")
    return [(ident, regex, reason) for ident, regex, severity, reason in privacy.BUILTIN if severity == privacy.BLOCK]


def shapes():
    """proxy.json's "secrets" "shapes": the privacy check's blocking token shapes, each with the literals its matches
    begin with, what follows them in every match, and the hosts of its own service."""
    return [{"id": ident, "regex": regex, "label": label, "anchors": list(SHAPE_ANCHORS.get(ident, [])),
             "hosts": list(SHAPE_HOSTS.get(ident, [])),
             "tail": SHAPE_TAILS.get(ident, SHAPE_TAIL) if ident in SHAPE_ANCHORS else ""}
            for ident, regex, label in builtin_shapes()]


def shape_of(value):
    """The id of the token shape VALUE has, or None."""
    for ident, regex, _ in builtin_shapes():
        if re.match(regex, value):
            return ident
    return None


def secret_name(name):
    """NAME (a variable's) says that its value is a secret."""
    return bool(SECRET_NAME.search(name))


def value_entry(ident, label, value, hosts=()):
    """One value a start registers: {"id", "label", "value", "hosts"}; None for a value the guard cannot tell from
    ordinary traffic (shorter than VALUE_MIN characters) or that is no single secret (longer than VALUE_MAX)."""
    if not isinstance(value, str) or not VALUE_MIN <= len(value.strip()) <= VALUE_MAX:
        return None
    return {"id": ident, "label": label, "value": value.strip(), "hosts": list(dict.fromkeys(hosts))}


def env_hosts(name):
    """The hosts the value of the variable NAME was given for, where its name says so (ENV_HOSTS)."""
    return list(ENV_HOSTS.get(name, []))


def server_hosts(host):
    """The hosts a login for the server HOST is at home on: itself and the names below it (api.HOST, uploads.HOST)."""
    host = str(host).lower().strip("[]")
    return [host] if ":" in host or not host.strip("0123456789.") else [host, f"*.{host}"]


# --- what a start gives its proxy -------------------------------------------------------------------------------------

def proxy_settings(values, rulesets, allow=(), providers=(), mode=None, mode_name=None, mode_base=None):
    """proxy.json's "secrets" for a run: the decisions - the defaults (MODE_BASE in place of them where the security
    mode has its own), the mode's settings (MODE: modes.py's "secrets") and every ruleset's "secrets" section weighed
    in (runtime.secret_decisions: the strictest wins) -, the VALUES the run was given ([value_entry]) in every form a
    request may carry them, the token shapes, ALLOW (the run's and the project's allow-list entries) and the run's
    model PROVIDERS, whose requests are the model's context."""
    rt = runtime()
    base = dict(rt.SECRET_DEFAULTS)
    if isinstance(mode_base, dict):
        with contextlib.suppress(ValueError):
            base.update(rt.secret_settings(mode_base))
    sections = [(f"mode {mode_name}", mode)] if mode else []
    sections += [(source.get("name", "rules"), source["secrets"]) for source in rulesets
                 if isinstance(source, dict) and isinstance(source.get("secrets"), dict)]
    listed, found, seen = [], [], set()
    for entry in values[:VALUES_LIMIT]:
        if entry["value"] in seen:
            continue  # one value under two names: told by the first
        seen.add(entry["value"])
        shape = shape_of(entry["value"])
        listed.append({"id": entry["id"], "label": entry["label"], "hosts": list(entry.get("hosts") or []),
                       **({"shape": shape} if shape else {})})
        found += [[text, entry["id"], encoding] for text, encoding in forms(entry["value"]).items()]
    return {**rt.secret_decisions(sections, base), "values": listed, "forms": found, "shapes": shapes(),
            "allow": list(dict.fromkeys(allow)), "providers": list(dict.fromkeys(providers))}


def added_entries(values):
    """VALUES ([value_entry]) as the control op "secret-values" takes them (runtime.give_secret_values): registered
    with a running sandbox's proxy."""
    return [{"id": entry["id"], "label": entry["label"], "hosts": list(entry.get("hosts") or []),
             "forms": [[text, encoding] for text, encoding in forms(entry["value"]).items()]} for entry in values]


# --- the allow-list -------------------------------------------------------------------------------------------------

def spec_text(spec):
    """SPEC (WHAT@HOST) in its normal form; ValueError says what it takes."""
    what, at, host = spec.rpartition("@") if isinstance(spec, str) else ("", "", "")
    host = host.strip().lower().rstrip(".")
    what = what.strip()
    known = {ident for ident, _, _ in builtin_shapes()}
    what = what.lower() if what.lower() in known else what
    if not at or not (what == "*" or what in known or VALUE_ID.fullmatch(what)) or not HOST.fullmatch(host) or \
            len(host) > 253 or host == "*.*":
        raise ValueError(f"{printable(spec, 120)!r}: WHAT@HOST - WHAT is a token shape ({', '.join(sorted(known))}), "
                         "a value the run was given (env:NAME, git-token:HOST, git-login:HOST, mcp:SERVER:NAME) or *; "
                         "HOST is a name, *.DOMAIN or *")
    return f"{what}@{host}"


def project_key(project):
    return str(Path(project).expanduser().resolve())


def read_json(path, limit=4 << 20):
    try:
        data = json.loads(Path(path).read_bytes()[:limit])
    except (OSError, ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def read_allowed(path=None):
    """{project: [spec ...]} of the allow-list file (valid entries only); {} when there is none or it is damaged."""
    data = read_json(path or allow_file()) or {}
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    result = {}
    for project, entry in projects.items():
        specs = []
        for spec in entry.get("allow", []) if isinstance(entry, dict) and isinstance(entry.get("allow"), list) else []:
            with contextlib.suppress(ValueError):
                specs.append(spec_text(spec))
        if specs:
            result[str(project)] = list(dict.fromkeys(specs))
    return result


def allowed_for(project, path=None):
    """What may go where in every run of PROJECT (its folder)."""
    return read_allowed(path).get(project_key(project), [])


@contextlib.contextmanager
def allow_store(path=None):
    """The allow-list file as a dict, read and written back under a lock. A damaged file is kept aside, never
    written over."""
    path = Path(path or allow_file())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(path.with_name(".secret-guard.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = None
        if path.exists():
            data = read_json(path)
            if data is None and path.read_bytes().strip():
                aside = path.with_name(f"{path.name}.damaged-{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}")
                os.rename(path, aside)
                print(f"sandbox-ai: warning: {path} could not be read (not a JSON object): kept as {aside.name}; a "
                      "new one is begun", file=sys.stderr)
        data = data if isinstance(data, dict) else {}
        data.setdefault("version", 1)
        if not isinstance(data.get("projects"), dict):
            data["projects"] = {}
        yield data
        temporary = path.with_name(f".secret-guard.{os.getpid()}.json")
        temporary.write_text(json.dumps(data, indent=2) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)


def allow(project, specs, path=None):
    """Allow SPECS in every run of PROJECT; returns those that were not allowed yet."""
    wanted = [spec_text(spec) for spec in specs]
    added = []
    with allow_store(path) as data:
        entry = data["projects"].setdefault(project_key(project), {})
        listed = entry.get("allow") if isinstance(entry.get("allow"), list) else []
        details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
        for spec in wanted:
            if spec not in listed:
                listed.append(spec)
                details[spec] = {"added": stamp()}
                added.append(spec)
        entry["allow"], entry["details"] = listed, details
    return added


def forget(project, specs, path=None):
    """Take SPECS off PROJECT's allow-list; returns those that were on it."""
    wanted = [spec_text(spec) for spec in specs]
    gone = []
    with allow_store(path) as data:
        key = project_key(project)
        entry = data["projects"].get(key)
        if isinstance(entry, dict) and isinstance(entry.get("allow"), list):
            gone = [spec for spec in wanted if spec in entry["allow"]]
            entry["allow"] = [spec for spec in entry["allow"] if spec not in wanted]
            if isinstance(entry.get("details"), dict):
                for spec in gone:
                    entry["details"].pop(spec, None)
            if not entry["allow"]:
                data["projects"].pop(key, None)
    return gone


# --- the command ------------------------------------------------------------------------------------------------------

def status(project="."):
    """What `secret-guard status` says: the shapes, the defaults, the project's allow-list."""
    rt = runtime()
    return {"shapes": [{"id": shape["id"], "label": shape["label"], "hosts": shape["hosts"]} for shape in shapes()],
            "defaults": dict(rt.SECRET_DEFAULTS), "decisions": list(rt.SECRET_DECISIONS),
            "values": ["env:NAME (--secret-env, --env with a secret's name)", "git-token:HOST (--git-token-env, "
                       "--git-token-file without --token-proxy)", "git-login:HOST (a login given into the sandbox)",
                       "mcp:SERVER:NAME (an MCP server's secret variable)"],
            "project": project_key(project), "allow": allowed_for(project), "allow_file": str(allow_file())}


def status_lines(info):
    lines = ["The secret guard looks, in every request the connection proxy opens, for",
             "  the values a start was given in plain text:"]
    lines += [f"    {text}" for text in info["values"]]
    lines.append("  and what looks like a credential (the privacy check's token shapes):")
    lines += [f"    {shape['id']}: {shape['label']}"
              + (f" (its own service: {', '.join(shape['hosts'])})" if shape["hosts"] else "")
              for shape in info["shapes"]]
    lines.append(f"On its way to another host a finding is: a known value {info['defaults']['on_send']}ed, a shape "
                 f"{info['defaults']['on_shape']}ed (the defaults; rulesets' \"secrets\" and the modes high and paranoid "
                 "ask or block). To the model provider: recorded as having entered the model's context, never held.")
    allowed = ", ".join(info["allow"]) or "nothing"
    lines.append(f"Allowed in every run of {printable(info['project'])}: {allowed}")
    return lines


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai secret-guard", description=__doc__.split("\n\n")[0],
                                epilog="SPEC: WHAT@HOST - a token shape, a value's id (env:NAME, git-token:HOST, "
                                       "git-login:HOST, mcp:SERVER:NAME) or *, and a host, *.DOMAIN or *. No value "
                                       "is ever shown or kept.")
    sub = p.add_subparsers(dest="command")
    shown = sub.add_parser("status", help="What the guard looks for, and what is allowed for the project")
    shown.add_argument("--project", metavar="DIR", default=".",
                       help="The project's folder: the first workspace of its starts (default: this one)")
    shown.add_argument("--json", action="store_true", help="As JSON")
    for name, text in (("allow", "Let a kind of value go to a host in every run of the project"),
                       ("forget", "Take entries off the project's allow-list")):
        command = sub.add_parser(name, help=text)
        command.add_argument("spec", nargs="+", metavar="SPEC")
        command.add_argument("--project", metavar="DIR", default=".",
                             help="The project's folder: the first workspace of its starts (default: this one)")
    listed = sub.add_parser("list", help="What is allowed for the project")
    listed.add_argument("--project", metavar="DIR", default=".",
                        help="The project's folder: the first workspace of its starts (default: this one)")
    listed.add_argument("-a", "--all", action="store_true", help="Every project's")
    listed.add_argument("--json", action="store_true", help="As JSON")
    return p


def main(argv=None, out=print):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--json"]:
        argv = ["status", "--json"]
    a = parser().parse_args(argv or ["status"])
    try:
        if a.command == "status":
            info = status(a.project)
            for line in [json.dumps(info, indent=2)] if a.json else status_lines(info):
                out(line)
            return 0
        if a.command == "allow":
            added = allow(a.project, a.spec)
            for spec in added:
                what, _, host = spec.rpartition("@")
                out(f"allowed {spec} in every run of {project_key(a.project)}: "
                    + ("anything the guard finds" if what == "*" else what) + " may go to "
                    + ("any host" if host == "*" else host) + " (from the next start on)")
            for spec in sorted(set(spec_text(s) for s in a.spec) - set(added)):
                out(f"{spec} was allowed already")
            return 0
        if a.command == "forget":
            gone = forget(a.project, a.spec)
            for spec in gone:
                out(f"forgot {spec}")
            missing = sorted(set(spec_text(s) for s in a.spec) - set(gone))
            for spec in missing:
                print(f"sandbox-ai secret-guard: {spec} is not on the allow-list of {project_key(a.project)}",
                      file=sys.stderr)
            return 1 if missing else 0
        if a.command == "list":
            known = read_allowed()
            if not a.all:
                key = project_key(a.project)
                known = {key: known[key]} if key in known else {}
            if a.json:
                out(json.dumps(known, indent=2))
            elif not known:
                out("(none)" + ("" if a.all else f" for {project_key(a.project)}; --all: every project's"))
            else:
                module("textout").table(["PROJECT", "ALLOWED"], [[printable(project), spec] for project, specs in
                                                                 sorted(known.items()) for spec in specs], out, indent=0)
            return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(f"sandbox-ai secret-guard: {printable(exc, 1000)}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
