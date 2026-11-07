#!/usr/bin/env python3
"""sandbox-ai plugins: Claude plugin marketplaces and plugins, kept for all sandboxes.

  sandbox-ai plugins [--status]                  what the kits declare and what is installed
  sandbox-ai plugins claude [launcher options]   install what the default kit and every kit declare
  sandbox-ai plugins claude --update             also update the marketplaces and installed plugins
  sandbox-ai plugins claude --kit egm            also install what the built-in kit egm declares
  sandbox-ai plugins claude -- ARGS              run `claude plugin ARGS` in the plugin store's sandbox
  sandbox-ai plugins claude trust-hooks PLUGIN [--yes|--revoke]
                                                 review a plugin's hooks and approve them: they run while the
                                                 agent's managed hooks are on (tool rules, kit hooks)

The kits declare marketplaces and plugins in AGENT/marketplaces.json. Installing runs
`claude plugin ...` inside a sandbox whose HOME is the plugin store ~/.local/share/sandbox-ai/plugins/claude,
with network access to the marketplaces' hosts only. Every Claude sandbox then gets the store
read-only and enables the plugins its kits list, so no sandbox can change another one's plugins.
"""
import argparse
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
_modules = {}


def module(name):
    if name not in _modules:
        spec = importlib.util.spec_from_file_location(f"sandbox_{name}_for_plugins", HERE / f"{name}.py")
        _modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_modules[name])
    return _modules[name]


def kits():
    return module("kits")


def network(sources):
    """Launcher options that reach the hosts of SOURCES (marketplace sources) and nothing else."""
    options = []
    if any(source["source"] == "github" for source in sources):
        options.append("--allow-github")
    for host in sorted({kits().source_host(source) for source in sources} - {None}):
        options += ["--allow-host", host]
    return options


def plugin_sources(plugin, marketplace):
    """Sources whose hosts installing PLUGIN ("NAME@MARKETPLACE") needs. A plugin of sandbox-ai's own
    marketplace (a directory, no network) may live on GitHub, pinned in its marketplace.json; it then
    needs the GitHub preset too. Other marketplaces: their own source only."""
    if marketplace["source"] != "directory":
        return [marketplace]
    manifest = kits().read_store_json(kits().MARKETPLACE / ".claude-plugin" / "marketplace.json") or {}
    entries = manifest.get("plugins") if isinstance(manifest.get("plugins"), list) else []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == plugin.split("@")[0]:
            remote = entry.get("source")
            if isinstance(remote, dict) and remote.get("source") == "github":
                return [marketplace, {"source": "github", "repo": remote.get("repo")}]
    return [marketplace]


def store_command(args, options=(), sources=()):
    """The launcher command that runs `claude plugin ARGS` with the plugin store as HOME."""
    store = kits().plugin_store("claude")
    return [sys.executable, "-I", str(HERE / "agent_sandbox.py"), "claude", *options, *network(sources),
            "--no-auth", "--no-config", "--no-tmux", "--no-default-kit", "--no-memory", "--no-plugins",
            "--binary", "claude", "--state-dir", str(store), "--", "plugin", *args]


def run_in_store(args, options=(), sources=(), runner=None):
    """Run `claude plugin ARGS` in a sandbox on the plugin store; its exit status."""
    runner = runner or subprocess.run
    store = kits().plugin_store("claude")
    if store.is_symlink():
        raise ValueError(f"{store} is a symlink; refusing to use it")
    store.mkdir(mode=0o700, parents=True, exist_ok=True)
    store.chmod(0o700)
    # A workspace of its own: the store sandbox never sees a project.
    work = Path(tempfile.mkdtemp(prefix=".work-", dir=store.parent))
    try:
        command = store_command(args, options, sources)
        position = command.index("--")
        command[position:position] = ["--workspace", str(work)]
        return runner(command).returncode
    finally:
        shutil.rmtree(work, ignore_errors=True)


def sync(options=(), update=False, out=None, runner=None, builtin=()):
    """Install every marketplace and plugin the kits declare (and the built-in kits named in BUILTIN);
    with UPDATE also refresh them. 0 if all is there."""
    out = out or (lambda line: print(line, file=sys.stderr))
    k = kits()
    if k.ensure_default_home("claude"):
        out(f"sandbox-ai plugins: created the default kit {k.default_home('claude')} with examples")
    marketplaces, plugins, notes = k.declarations("claude", k.all_plugin_files("claude", builtin))
    for note in notes:
        out(f"sandbox-ai plugins: {note}")
    known, _ = k.installed()
    for name, source in marketplaces.items():
        if name in known:
            continue
        out(f"sandbox-ai plugins: adding marketplace {name} ({k.source_argument(source)})")
        runner_status = run_in_store(["marketplace", "add", k.source_argument(source)], options, [source], runner)
        known, _ = k.installed()
        if runner_status == 0 and name not in known:
            out(f"sandbox-ai plugins: the marketplace from {k.source_argument(source)} is not called {name}; "
                "use the name its marketplace.json gives in marketplaces.json")
    if update and known:
        out("sandbox-ai plugins: updating the marketplaces")
        run_in_store(["marketplace", "update"], options,
                     [marketplaces[name] for name in known if name in marketplaces], runner)
    known, installed = k.installed()
    for plugin in plugins:
        source = plugin_sources(plugin, marketplaces[plugin.split("@")[1]])
        if plugin in installed:
            if update:
                out(f"sandbox-ai plugins: updating {plugin}")
                run_in_store(["update", plugin], options, source, runner)
            continue
        if plugin.split("@")[1] not in known:
            continue  # its marketplace failed above
        out(f"sandbox-ai plugins: installing {plugin}")
        run_in_store(["install", plugin], options, source, runner)
    known, installed = k.installed()
    missing = [p for p in plugins if p not in installed] + [f"marketplace {m}" for m in marketplaces if m not in known]
    if missing:
        out(f"sandbox-ai plugins: not installed: {', '.join(missing)}")
        return 1
    out(f"sandbox-ai plugins: {len(installed)} plugin(s) in {k.plugin_store('claude')}; every Claude sandbox "
        "enables those its kits list")
    return 0


def status(out=print):
    k = kits()
    known, installed = k.installed()
    out(f"claude    plugin store {k.plugin_store('claude')}")
    out(f"          marketplaces: {', '.join(sorted(known)) or 'none'}")
    out(f"          plugins: {', '.join(sorted(installed)) or 'none'}")
    try:
        marketplaces, plugins, _ = k.declarations("claude", k.all_plugin_files("claude"))
    except ValueError as exc:
        out(f"          kits: {exc}")
        return 1
    for label, path in k.all_plugin_files("claude"):
        own = [p for p in k.read_declaration(label, path).get("plugins", []) if p in plugins]
        state = ", ".join(f"{p}{'' if p in installed else ' (not installed)'}" for p in own) or "no plugins"
        out(f"          {label}: {state}")
    missing = [p for p in plugins if p not in installed]
    if missing:
        out("          -> sandbox-ai plugins claude")
    for name in k.builtin_kits("claude"):
        path = k.BUILTIN_KITS / name / "claude" / k.PLUGINS_FILE
        if path.is_file():
            own = k.read_declaration(f"built-in kit {name}", path).get("plugins", [])
            state = ", ".join(f"{p}{'' if p in installed else ' (not installed)'}" for p in own) or "no plugins"
            hint = "" if all(p in installed for p in own) else f" -> sandbox-ai plugins claude --kit {name}"
            out(f"          built-in kit {name} (--kit {name}): {state}{hint}")
    for agent in ("opencode", "codex"):
        try:
            _, listed, notes = k.declarations(agent, k.all_plugin_files(agent))
        except ValueError as exc:
            out(f"{agent:9} {exc}")
            continue
        if agent == "opencode":
            out(f"opencode  npm plugins from the kits: {', '.join(listed) or 'none'} (loaded by OpenCode itself; "
                "needs --allow-host registry.npmjs.org)")
        else:
            out("codex     plugins: not supported yet" + (f" ({len(notes)} kit(s) declare some)" if notes else ""))
    return 0


REVIEW_LIMIT = 64 << 10  # a file trust-hooks shows in full


def printable(text):
    """TEXT safe for a terminal: the launcher's printable (one implementation for every command)."""
    return module("agent_sandbox").printable(str(text))


def plugin_hooks(plugin):
    """read_plugin_hooks of the installed PLUGIN (NAME@MARKETPLACE)."""
    k = kits()
    _, installed = k.installed()
    if plugin not in installed:
        raise ValueError(f"{plugin} is not in the plugin store; see: sandbox-ai plugins --status")
    where = k.plugin_install(plugin, installed[plugin])
    if where is None:
        raise ValueError(f"{plugin}: the plugin store does not say where it is installed")
    info = k.read_plugin_hooks(plugin, *where)
    if not info["declared"]:
        raise ValueError(f"{plugin} has no hooks")
    return info


def review(info, out):
    """Each hook of the plugin, the full text of every file its commands name, and what else its folder holds."""
    k = kits()
    out(f"{info['plugin']}: hooks in {printable(info['path'])}")
    named = set()
    for hook in info["hooks"]:
        out(f"  {hook['event']}, matcher {printable(hook['matcher'] or 'every tool')}: {printable(hook['command'])}")
        for item in info["files"]:
            if f"{k.PLUGIN_ROOT_VARIABLE}/{item['path']}" in hook["command"] or \
                    f"$CLAUDE_PLUGIN_ROOT/{item['path']}" in hook["command"]:
                named.add(item["path"])
    for note in info["notes"]:
        out(f"  note: {printable(note)}")
    for error in info["errors"]:
        out(f"  error: {printable(error)}")
    for item in info["files"]:
        if item["path"] not in named:
            continue
        out("")
        out(f"  --- {printable(item['path'])} ({item['size']} bytes{', executable' if item['executable'] else ''}, "
            f"sha256 {item['sha256'][:16]})")
        data, _ = k.read_at_path(info["path"], item["path"]) if item["size"] <= REVIEW_LIMIT else (None, None)
        if data is None:
            out("  (larger than 64 KiB: read it yourself before approving)")
            continue
        for line in data.decode("utf-8", "replace").splitlines()[:400]:
            out(f"  | {printable(line)}")
    others = [f for f in info["files"] if f["path"] not in named]
    size = sum(f["size"] for f in others)
    out("")
    out(f"  and {len(others)} other file(s), {size >> 10} KiB, which the hooks may run too; the approval covers "
        f"them all (read them in {printable(info['path'])})")


def trust_hooks(argv, out=print):
    parser = argparse.ArgumentParser(prog="sandbox-ai plugins claude trust-hooks",
                                     description="Review a Claude plugin's hooks and approve them")
    parser.add_argument("plugin", metavar="NAME@MARKETPLACE")
    choice = parser.add_mutually_exclusive_group()
    choice.add_argument("--yes", action="store_true", help="approve what is shown without asking")
    choice.add_argument("--revoke", action="store_true", help="take the approval back")
    a = parser.parse_args(argv)
    k = kits()
    info = plugin_hooks(a.plugin)
    key = k.plugin_trust_key(a.plugin)
    if a.revoke:
        k.change_trust("claude", info, key, approve=False)
        out(f"{a.plugin}: the approval of its hooks is taken back")
        return 0
    review(info, out)
    if info["errors"]:
        raise ValueError(f"these hooks cannot be approved: {printable(info['errors'][0])}")
    if not a.yes:
        if not sys.stdin.isatty():
            raise ValueError("review the hooks above, then approve them with --yes")
        answer = input("Approve exactly these hooks and this plugin's files? Any change takes the approval away. "
                       "[y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            out("not approved")
            return 1
    k.change_trust("claude", info, key)
    out(f"{a.plugin}: {len(info['hooks'])} hook(s) approved (sha256 {info['sha256'][:16]}); they run while the "
        "managed hooks are on, until the plugin changes")
    return 0


def cli(argv):
    if argv[:2] == ["claude", "trust-hooks"]:
        return trust_hooks(argv[2:])
    passthrough = None
    if "--" in argv:
        position = argv.index("--")
        argv, passthrough = argv[:position], argv[position + 1:]
    parser = argparse.ArgumentParser(prog="sandbox-ai plugins", description=__doc__.split("\n\n")[0],
                                     epilog="Launcher options (e.g. --backend bwrap) are passed on to the "
                                            "plugin store's sandbox.")
    parser.add_argument("agent", nargs="?", choices=["claude", "codex", "opencode"])
    parser.add_argument("-s", "--status", action="store_true", help="Show declared and installed plugins")
    parser.add_argument("-u", "--update", action="store_true", help="Also update marketplaces and installed plugins")
    parser.add_argument("--kit", action="append", default=[], metavar="NAME",
                        help="Also install the plugins of sandbox-ai's built-in kit NAME (e.g. egm); repeatable")
    a, options = parser.parse_known_args(argv)
    if a.status or not a.agent:
        return status()
    if a.agent != "claude":
        print(f"sandbox-ai plugins: {a.agent} has no plugin store; "
              + ("OpenCode loads the npm plugins its kits list by itself" if a.agent == "opencode"
                 else "plugins for Codex are not supported yet"), file=sys.stderr)
        return 1
    if passthrough is not None:
        marketplaces, plugins, _ = kits().declarations("claude", kits().all_plugin_files("claude", a.kit))
        sources = list(marketplaces.values())
        sources += [s for p in plugins for s in plugin_sources(p, marketplaces[p.split("@")[1]])[1:]]
        return run_in_store(passthrough, options, sources)
    return sync(options, a.update, builtin=a.kit)


if __name__ == "__main__":
    try:
        sys.exit(cli(sys.argv[1:]))
    except (OSError, ValueError) as exc:
        print(f"sandbox-ai plugins: {exc}", file=sys.stderr)
        sys.exit(1)
