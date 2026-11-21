#!/usr/bin/env python3
"""sandbox-ai rules check|show|reload|builtin|audit: the rulesets of the starts (README "Rulesets").

  sandbox-ai rules check [FILE ...] [--project DIR] [--json]
        check rulesets: the FILEs (as --rules reads them), else yours (~/.config/sandbox-ai/rules.json), the
        administrator's policy and the project file of DIR (default: here)
  sandbox-ai rules show [--agent AGENT] [--project DIR] [--json] [-- OPTIONS]
        the rulesets a start of AGENT in DIR with OPTIONS would have, in order, each with where it comes from
  sandbox-ai rules show NAME [--json]
        the HTTP rules the running start NAME holds now (its connection proxy's)
  sandbox-ai rules reload NAME [--json]
        the running start NAME reads its HTTP rules anew: your rules.json, its --rules files and its trusted project
        file's "rules" as they are now; the administrator's policy, the security mode, --publish, --telemetry and
        --allow-http stay as the start had them
  sandbox-ai rules builtin [NAME [--audit]] [--json]
        sandbox-ai's own rulesets (--rules builtin:NAME): github-persistence, destructive, mcp-no-credentials,
        secret-files; NAME prints one (--audit: with "mode": "audit", to save and try first)
  sandbox-ai rules audit [--project DIR | --log-dir DIR] [--run RUN] [--json]
        what the rulesets with "mode": "audit" would have decided in the project's runs (nothing of it was enforced)

Each ruleset decides on its own and the strictest decision wins: a reload can loosen your own rules, never what
the policy or the security mode decide. MCP, tool and device rules are read at a start only (they are named when a
reload sees them).
"""
import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading

HERE = Path(__file__).resolve().parent
LOADING = threading.RLock()
SECTIONS = ("http", "mcp", "devices", "tool")
RELOAD_BYTES = 3 << 20  # what one reload may send (the control socket takes up to runtime.CONTROL_LIMIT)
_modules = {}


def module(name):
    with LOADING:
        if name not in _modules:
            spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_rules", HERE / f"{name}.py")
            loaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(loaded)
            if name != "agent_sandbox" and hasattr(loaded, "_launcher"):
                loaded._launcher = module("agent_sandbox")
            _modules[name] = loaded
        return _modules[name]


def launcher():
    return module("agent_sandbox")


def printable(value):
    return launcher().printable(str(value))


def counts(source):
    """{section: number of rules, "untrusted": number of untrusted sources} of a checked ruleset."""
    return {**{section: len(source.get(section, [])) for section in SECTIONS},
            "untrusted": len(source.get("untrusted", []))}


def summary(source):
    """One line: how many rules of each kind, and the defaults other than allow."""
    found = counts(source)
    parts = [f"{found['http']} HTTP", f"{found['mcp']} MCP", f"{found['tool']} tool", f"{found['devices']} device"]
    text = ", ".join(parts) + " rules"
    if found["untrusted"]:
        text += f", {found['untrusted']} untrusted source{'s' if found['untrusted'] != 1 else ''}"
    strict = [f"{key} {value}" for key, value in source.get("defaults", {}).items() if value != "allow"]
    text += f"; defaults: {', '.join(strict)}" if strict else ""
    # its "packages" section: what it has the package guard decide (the strictest of all rulesets holds)
    packages = source.get("packages") if isinstance(source.get("packages"), dict) else {}
    text += "; package guard: " + ", ".join(f"{key} {packages[key]}" for key in sorted(packages)) if packages else ""
    # its "secrets" section: what it has the secret guard do about a secret on its way to another host
    secrets = source.get("secrets") if isinstance(source.get("secrets"), dict) else {}
    return text + ("; secret guard: " + ", ".join(f"{key} {secrets[key]}" for key in sorted(secrets))
                   if secrets else "")


# --- check ---------------------------------------------------------------------------------------------------------

def project_file(directory=None):
    l = launcher()
    return l.config_key(Path(directory or os.getcwd()) / l.CONFIG_NAME)


def check(files=(), project=None):
    """[{"name", "path", "ok", "error"?, "counts"?, "defaults"?, "trusted"?}] of the rulesets checked."""
    l = launcher()
    RUNTIME = l.RUNTIME
    results = []

    def checked(name, path, read):
        entry = {"name": name, "path": str(path) if path else None}
        try:
            source = read()
        except OSError as exc:  # its reason, not the descriptor's number of an os.open
            return results.append({**entry, "ok": False, "error": exc.strerror or str(exc)})
        except ValueError as exc:
            return results.append({**entry, "ok": False, "error": str(exc)})
        results.append({**entry, "ok": True, "counts": counts(source), "defaults": source["defaults"],
                        "summary": summary(source)})
    if files:
        for given in files:
            try:
                path = l.rules_file(given)
            except Exception as exc:  # noqa: BLE001 - builtin:NAME that does not exist (argparse's error type)
                results.append({"name": str(given), "path": None, "ok": False, "error": str(exc)})
                continue
            checked(l.ruleset_label(path), Path(path).expanduser().absolute(),
                    lambda path=path: l.read_ruleset(Path(path).expanduser(), l.ruleset_label(path)))
        return results
    user = l.user_rules_file()
    if user.exists() or user.is_symlink():
        checked(l.RULES_NAME, user, lambda: l.read_ruleset(user, l.RULES_NAME))
    try:
        policy = l.load_policy()
    except ValueError as exc:
        results.append({"name": "policy", "path": str(l.POLICY_FILE), "ok": False, "error": str(exc)})
        policy = {}
    if "rules" in policy:
        checked("policy", l.POLICY_FILE, lambda: RUNTIME.Ruleset.validated(policy["rules"], "policy rules"))
    path = project_file(project)
    if path.exists() or path.is_symlink():
        def read_project():
            try:
                data = l.read_config_bytes(path)
                document = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                raise ValueError(f"{path}: not valid JSON ({exc})") from None
            results_trust["trusted"] = l.trusted_configs(note=lambda text: None).get(str(path)) == \
                hashlib.sha256(data).hexdigest()
            if not isinstance(document, dict) or "rules" not in document:
                raise LookupError
            return RUNTIME.Ruleset.validated(document["rules"], "project rules")
        results_trust = {}
        try:
            checked("project", path, read_project)
        except LookupError:
            pass  # a project file without rules
        else:
            results[-1]["trusted"] = results_trust.get("trusted", False)
    return results


def show_check(results, out, as_json):
    if as_json:
        out(json.dumps({"rulesets": results, "ok": all(entry["ok"] for entry in results)}, indent=2))
    elif not results:
        out("no rulesets: none of yours (~/.config/sandbox-ai/rules.json), none in the administrator's policy, none in "
            "the project file here")
    for entry in [] if as_json else results:
        where = f" ({printable(entry['path'])})" if entry.get("path") else ""
        if entry["ok"]:
            trust = "" if entry.get("trusted", True) else " - the project file is not trusted (sandbox-ai wizard trust)"
            out(f"ok     {printable(entry['name'])}{where}: {entry['summary']}{trust}")
        else:
            out(f"error  {printable(entry['name'])}{where}: {printable(entry['error'])}")
    return 0 if all(entry["ok"] for entry in results) else 1


# --- show: the rulesets of a start ---------------------------------------------------------------------------------

def start_rulesets(agent, options=(), directory=None):
    """(a, [ruleset with "from"]) of a start of AGENT with OPTIONS in DIRECTORY, read as the launcher reads them at
    a start: the presets, the trusted project file, your defaults, the security mode and the administrator's policy
    first (load_rulesets then has what it needs)."""
    l = launcher()
    previous = os.getcwd()
    if directory:
        os.chdir(directory)
    try:
        argv = l.canonical_options([agent, *options])
        argv, _ = l.load_presets_module().apply(argv, canonical=l.canonical_options, with_mode=True, notes=[],
                                                  credential=l.copies_credential, private=l.private_source)
        argv = l.canonical_options(argv)
        a = l.parse_args(argv)
        config_path, document = l.load_project_config(a)
        merged = l.merge_config(argv, a, document) if document else argv
        if document:
            a = l.parse_args(merged)
        defaults, policy = l.load_user_defaults(a), l.load_policy()
        a = l.MODES.apply(l.launcher_namespace(), merged, a, document, config_path, defaults, policy)
        a.project_config, a.project_document = (str(config_path) if config_path else None), document
        a.canary_default, a.telemetry_default = defaults.get("canaries"), defaults.get("telemetry")
        l.check_policy(a, policy)
        sources = l.load_rulesets(a)
    except SystemExit:
        raise ValueError("these options do not parse; see sandbox-ai claude --help") from None
    finally:
        os.chdir(previous)
    files = ([str(l.user_rules_file())] if l.user_rules_file().exists() else []) + \
        [str(Path(path).expanduser().absolute()) for path in a.rules]
    shown = []
    for source in sources:
        name = source["name"]
        if name == "policy":
            where = str(l.POLICY_FILE)
        elif name.startswith("mode "):
            where = f"the security mode {name[5:]}"
        elif name.startswith((l.RULES_NAME, "--rules", "builtin:")) and files:
            where = files.pop(0)
        elif name == "project":
            where = a.project_config
        else:
            where = {"--allow-http": "the --allow-http options", "publish": "--publish",
                     "telemetry": "--telemetry (the agents' telemetry hosts)"}.get(name, name)
        shown.append({"name": name, "from": where, **source})
    return a, shown


def rule_text(section, rule):
    rest = {key: value for key, value in rule.items() if key not in ("id", "decision", "priority", "addresses")}
    text = json.dumps(rest, ensure_ascii=True, sort_keys=True, default=sorted)
    return f"  {section:<8}{rule['id']}: {rule['decision']} {text[:160]}{'...' if len(text) > 160 else ''}"


def show_start(sources, out, as_json, agent=None, project_config=None):
    if as_json:
        out(json.dumps({"agent": agent, "project_config": project_config, "rulesets": sources}, indent=2,
                       default=sorted))
        return 0
    if not sources:
        out("no rulesets: every request and tool call this start makes is decided by its options alone")
        return 0
    out(f"rulesets of a start of {printable(agent)} here, in order (each decides on its own, the strictest wins):")
    for source in sources:
        out(f"{printable(source['name'])} (from {printable(source['from'] or '?')}): {summary(source)}")
        for section in SECTIONS:
            for rule in source.get(section, []):
                out(printable(rule_text(section, rule)))
        for entry in source.get("untrusted", []):
            out(printable(f"  untrusted {entry.get('id')}"))
    return 0


# --- a running start: show and reload --------------------------------------------------------------------------------

def running(name, command):
    net = module("net")
    record = net.choose(net.running(), name, command=f"rules {command}")
    if record.get("kind") == "mcp":
        raise ValueError("an MCP server sandbox has the rules of its gateway: stop it and start it again "
                         f"(sandbox-ai stop {printable(record.get('name') or record.get('run'))})")
    return record


def show_running(name, out, as_json):
    record = running(name, "show")
    answer = module("net").ask(record["directory"], {"op": "rules", "action": "show"})
    return show_held(record, answer, out, as_json)


def show_held(record, answer, out, as_json, said=()):
    shown = printable(record.get("name") or record.get("run"))
    if as_json:
        out(json.dumps({"name": record.get("name"), "run": record.get("run"), "rules": answer.get("rules", []),
                        "reloaded": answer.get("reloaded"), "not_reloaded": answer.get("not_reloaded", [])},
                       indent=2))
        return 0
    for line in said:
        out(line)
    if not answer.get("rules"):
        out(f"{shown}: its connection proxy holds no HTTP rules")
    else:
        out(f"the HTTP rules {shown} holds" + (f" (reloaded {answer['reloaded']})" if answer.get("reloaded") else "")
            + ":")
    for source in answer.get("rules", []):
        strict = [f"{key} {value}" for key, value in source.get("defaults", {}).items() if value != "allow"]
        out(f"  {printable(source['name'])}{' (kept from its start)' if source.get('kept') else ''}: "
            f"{len(source.get('http', []))} HTTP rules" + (f"; defaults: {', '.join(strict)}" if strict else "")
            + (f": {printable(', '.join(source['http'][:20]))}" if source.get("http") else ""))
    if answer.get("not_reloaded"):
        out(f"not reloaded (read at a start only): the MCP, tool and device rules of "
            f"{printable(', '.join(answer['not_reloaded']))}; they apply from its next start")
    return 0


def trusted_document(path):
    """The trusted project file at PATH as a start reads it (the same bytes hashed and parsed); ValueError when it
    is missing, changed since it was trusted, or not trusted."""
    l = launcher()
    data = l.read_config_bytes(path)
    if l.trusted_configs(note=lambda text: None).get(str(l.config_key(path))) != hashlib.sha256(data).hexdigest():
        raise ValueError(f"the project file {printable(path)} changed since it was trusted (or is not): review it, "
                         f"trust it (sandbox-ai wizard trust {printable(path)}), then reload")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError(f"{printable(path)}: invalid JSON") from None
    if not isinstance(document, dict):
        raise ValueError(f"{printable(path)}: not a project file")
    return document


def reloaded_sources(record):
    """The human's rulesets of the running start RECORD, read now as its start read them: your rules.json, its
    --rules files, its trusted project file's "rules", and the administrator's policy rules if they changed since
    (one more ruleset: it can only narrow)."""
    l = launcher()
    RUNTIME = l.RUNTIME
    recorded = record.get("rules_sources")
    if not isinstance(recorded, dict):
        raise ValueError("this sandbox was started by an older sandbox-ai, which did not record its rulesets: stop it "
                         "and start it again")
    files = [(l.user_rules_file(), l.RULES_NAME)] if l.user_rules_file().exists() else []
    files += [(Path(path), l.ruleset_label(path)) for path in recorded.get("files") or [] if isinstance(path, str)]
    sources, taken = [], {"policy", "--allow-http", "project", "publish", "telemetry"}
    for path, name in files:
        unique, number = name, 2
        while unique in taken:
            unique, number = f"{name} ({number})", number + 1
        taken.add(unique)
        sources.append({"name": unique, **l.read_ruleset(path, unique)})
    project = recorded.get("project_config")
    if isinstance(project, str) and project:
        document = trusted_document(Path(project))
        if "rules" in document:
            try:
                sources.append({"name": "project", **RUNTIME.Ruleset.validated(document["rules"], "project rules")})
            except RUNTIME.RuleError as exc:
                raise ValueError(str(exc)) from None
    policy = l.load_policy().get("rules")
    if policy is not None:
        sources.append({"name": "policy (now)", **RUNTIME.Ruleset.validated(policy, "policy rules")})
    return sources


def reload(name, out, as_json):
    record = running(name, "reload")
    if not record.get("proxy"):
        raise ValueError(f"{printable(record.get('name') or record.get('run'))} runs without the connection proxy "
                         "(--no-proxy): it holds no HTTP rules to reload")
    sources = reloaded_sources(record)
    # The names allowed during the run with their addresses: the reloaded rules hold for a bare address too.
    pinned = {host: entry.get("addresses", []) for host, entry in module("net").remembered(record).items()
              if isinstance(entry, dict)}
    request = {"op": "rules", "action": "reload", "rules": sources, "pinned": pinned}
    if len(json.dumps(request)) > RELOAD_BYTES:
        raise ValueError(f"these rulesets are larger than {RELOAD_BYTES >> 20} MiB together: too large to reload")
    answer = module("net").ask(record["directory"], request)
    said = [f"{printable(record.get('name') or record.get('run'))}: reloaded "
            + (", ".join(printable(source["name"]) for source in sources) or "no rulesets of yours (none now)")]
    return show_held(record, answer, out, as_json, said)


# --- builtin: sandbox-ai's own rulesets --------------------------------------------------------------------------

def builtin(name=None, audit=False, out=print, as_json=False):
    """Without NAME: sandbox-ai's own rulesets with what each does. NAME: the ruleset itself (AUDIT: with
    "mode": "audit", to be saved as a file of yours and tried before it holds)."""
    l = launcher()
    names = l.builtin_ruleset_names()
    if name is None:
        rows = []
        for item in names:
            source = l.read_ruleset(l.BUILTIN_RULESETS / f"{item}.json", f"builtin:{item}")
            document = json.loads((l.BUILTIN_RULESETS / f"{item}.json").read_text())
            rows.append({"name": item, "option": f"--rules builtin:{item}", "summary": summary(source),
                         "description": document.get("description", "")})
        if as_json:
            out(json.dumps(rows, indent=2))
            return 0
        for row in rows:
            out(f"builtin:{row['name']}  ({row['summary']})")
            out(f"  {row['description']}")
        out("Use one with --rules builtin:NAME (opt-in); try it first with: sandbox-ai rules builtin NAME --audit > "
            "FILE, then --rules FILE (records what it would decide: sandbox-ai rules audit)")
        return 0
    if name not in names:
        raise ValueError(f"no built-in ruleset {name!r}; there are: {', '.join(names)}")
    document = json.loads((l.BUILTIN_RULESETS / f"{name}.json").read_text())
    if audit:
        document = {"version": document["version"], "mode": "audit", **{k: v for k, v in document.items()
                                                                          if k != "version"}}
    out(json.dumps(document, indent=2, ensure_ascii=False))
    return 0


# --- audit: what audit rulesets would have decided ---------------------------------------------------------------

AUDIT_READ_LIMIT = 64 << 20  # bytes read of one log file
AUDIT_EXAMPLES = 3


def audit_logs(log_dir):
    """[(path, layer)]: the logs of LOG_DIR whose records carry "audit": the hooks' tools.jsonl, the relays'
    mcp-calls.jsonl, the proxy's connection logs."""
    log_dir = Path(log_dir)
    found = [(log_dir / "tools.jsonl", "tool"), (log_dir / "mcp-calls.jsonl", "mcp")]
    with contextlib.suppress(OSError):
        found += [(path, "http") for path in sorted(log_dir.glob("connections-*.jsonl"))]
    return [(path, layer) for path, layer in found if path.is_file() and not path.is_symlink()]


def audited_what(record, layer):
    if layer == "http":
        return f"{record.get('method', '?')} {record.get('url') or record.get('host', '?')}"
    if layer == "mcp":
        return f"{record.get('server', '?')}: {record.get('tool', '?')} {str(record.get('arguments', ''))[:120]}"
    return f"{record.get('agent', '?')}: {record.get('tool', '?')} {str(record.get('input', ''))[:120]}"


def audit_report(log_dir, run=None):
    """{"log_dir", "hits": [{"ruleset", "rule", "would", "layer", "count", "first", "last", "reason", "examples"}],
    "records"}: what the audit rulesets would have decided in LOG_DIR's runs (RUN: only that run)."""
    hits, records = {}, 0
    for path, layer in audit_logs(log_dir):
        try:
            with open(path, "rb") as stream:
                data = stream.read(AUDIT_READ_LIMIT)
        except OSError:
            continue
        for line in data.decode("utf-8", "replace").splitlines():
            if '"audit"' not in line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(record, dict) or not isinstance(record.get("audit"), list):
                continue
            if run and record.get("run") not in (None, run):
                continue
            records += 1
            for entry in record["audit"]:
                if not isinstance(entry, dict):
                    continue
                key = (str(entry.get("ruleset")), str(entry.get("rule")), str(entry.get("would")), layer)
                hit = hits.setdefault(key, {"ruleset": key[0], "rule": key[1], "would": key[2], "layer": layer,
                                            "count": 0, "unread_body": 0, "first": record.get("time"), "last": None,
                                            "reason": str(entry.get("reason") or "")[:300], "examples": []})
                hit["count"] += 1
                # matched without the request body (no enforcing rule needed it read: the proxy reads none for audit)
                hit["unread_body"] += entry.get("body") == "not read"
                hit["last"] = record.get("time")
                if len(hit["examples"]) < AUDIT_EXAMPLES:
                    hit["examples"].append({"time": record.get("time"), "what": audited_what(record, layer),
                                            "decided": record.get("decision")})
    ordered = sorted(hits.values(), key=lambda hit: (-hit["count"], hit["ruleset"], hit["rule"]))
    return {"log_dir": str(log_dir), "records": records, "hits": ordered}


def show_audit(report, out, as_json):
    if as_json:
        out(json.dumps(report, indent=2))
        return 0
    if not report["hits"]:
        out(f"No audit ruleset would have decided anything in {printable(report['log_dir'])} (a ruleset with "
            "\"mode\": \"audit\" records what it would decide; sandbox-ai rules builtin NAME --audit writes one)")
        return 0
    out(f"What the audit rulesets would have decided ({printable(report['log_dir'])}; nothing of it was enforced):")
    for hit in report["hits"]:
        out(f"  {printable(hit['ruleset'])} / {printable(hit['rule'])}: would {hit['would']} {hit['count']} "
            f"{hit['layer']} action(s), {printable(hit['first'])} .. {printable(hit['last'])}")
        if hit.get("unread_body"):
            out(f"    {hit['unread_body']} of them matched without the request body (no enforcing rule needed it "
                "read; the proxy reads none for an audit rule): its body condition was not checked")
        if hit["reason"]:
            out(f"    {printable(hit['reason'])}")
        for example in hit["examples"]:
            out(f"    {printable(example['time'])}  {printable(example['what'])[:200]}")
    out("Let a ruleset enforce: remove its \"mode\": \"audit\" (or use --rules builtin:NAME itself).")
    return 0


def audit_log_dir(project=None, log_dir=None):
    if log_dir:
        return Path(log_dir).expanduser()
    return launcher().default_log_dir(Path(project or os.getcwd()).expanduser().resolve())


# --- the command -------------------------------------------------------------------------------------------------

def help_style(prog):
    """The layout of this command's help, the one of every command with subcommands (helpstyle.py)."""
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai rules", description=__doc__.split("\n\n")[0],
                                formatter_class=help_style, allow_abbrev=False,
                                epilog="\n\n".join(__doc__.split("\n\n")[1:]))
    commands = p.add_subparsers(dest="command", required=True)
    checking = commands.add_parser("check", help="check rulesets: files, or yours, the policy's and the project's",
                                   allow_abbrev=False)
    checking.add_argument("files", nargs="*", metavar="FILE", help="ruleset files as --rules reads them")
    checking.add_argument("-p", "--project", metavar="DIR", help="the project file of DIR (default: here)")
    checking.add_argument("-j", "--json", action="store_true", help="as JSON")
    showing = commands.add_parser("show", help="the rulesets of a start here, or what a running start holds",
                                  allow_abbrev=False)
    showing.add_argument("name", nargs="?", metavar="NAME", help="a running start (sandbox-ai list)")
    showing.add_argument("-a", "--agent", choices=("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi"),
                         help="the agent of the start (default: the project file's, else claude)")
    showing.add_argument("-p", "--project", metavar="DIR", help="the project folder (default: here)")
    showing.add_argument("-j", "--json", action="store_true", help="as JSON")
    showing.epilog = "Launcher options of the start follow -- (--rules FILE, --allow-http ..., --mode ...)."
    reloading = commands.add_parser("reload", help="a running start reads its HTTP rules anew", allow_abbrev=False)
    reloading.add_argument("name", nargs="?", metavar="NAME", help="a running start (default: the only one, or the "
                                                                  "one of this folder)")
    reloading.add_argument("-j", "--json", action="store_true", help="as JSON")
    building = commands.add_parser("builtin", help="sandbox-ai's own rulesets (--rules builtin:NAME)",
                                   allow_abbrev=False)
    building.add_argument("name", nargs="?", metavar="NAME", help="print this one (to save a copy of it)")
    building.add_argument("--audit", action="store_true", help='with "mode": "audit": it records what it would '
                                                                "decide, and decides nothing")
    building.add_argument("-j", "--json", action="store_true", help="the list as JSON")
    auditing = commands.add_parser("audit", help="what the audit rulesets would have decided in a project's runs",
                                   allow_abbrev=False)
    auditing.add_argument("-p", "--project", metavar="DIR", help="the project (its log directory; default: here)")
    auditing.add_argument("-l", "--log-dir", metavar="DIR", help="this log directory instead")
    auditing.add_argument("--run", metavar="RUN", help="only this run (its id)")
    auditing.add_argument("-j", "--json", action="store_true", help="as JSON")
    return p


def project_agent(directory):
    """The agent the project file of DIRECTORY names, or None."""
    try:
        document = json.loads(launcher().read_config_bytes(project_file(directory)).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
    agent = document.get("agent") if isinstance(document, dict) else None
    return agent if agent in ("claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose", "openhands", "nanobot", "letta", "pi") else None


def main(argv, out=print):
    argv = list(argv)
    options = argv[argv.index("--") + 1:] if "--" in argv else []
    args = parser().parse_args(argv[:argv.index("--")] if "--" in argv else argv)
    if options and args.command != "show":
        parser().error("only rules show takes launcher options (after --)")
    try:
        if args.command == "check":
            return show_check(check(args.files, args.project), out, args.json)
        if args.command == "reload":
            return reload(args.name, out, args.json)
        if args.command == "builtin":
            return builtin(args.name, args.audit, out, args.json)
        if args.command == "audit":
            return show_audit(audit_report(audit_log_dir(args.project, args.log_dir), args.run), out, args.json)
        if args.name is not None:
            if options or args.agent or args.project:
                raise ValueError("NAME shows a running start's rules: it goes without --agent, --project and options")
            return show_running(args.name, out, args.json)
        directory = Path(args.project).expanduser().absolute() if args.project else None
        if directory is not None and not directory.is_dir():
            raise ValueError(f"--project {printable(args.project)}: no such folder")
        agent = args.agent or project_agent(directory) or "claude"
        a, sources = start_rulesets(agent, options, directory)
        return show_start(sources, out, args.json, agent, a.project_config)
    except (ValueError, OSError) as exc:
        print(f"sandbox-ai rules {args.command}: {printable(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
