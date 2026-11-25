#!/usr/bin/env python3
"""sandbox-ai install: build or add what starts need, all of it, nothing, or a part.

  sandbox-ai install --list [--kind KIND] [--json]   what there is, what is installed, what each costs
  sandbox-ai install ITEM... [--dry-run]             these (a preset: what it needs)
  sandbox-ai install --all [--kind KIND] [--dry-run] everything signed that is not installed yet

ITEM is KIND:NAME - image:AGENT (claude, codex ...), image:team, image:cairn, image:artex, image:browser, image:vpn,
image:tor; mcp:NAME or mcp:CATALOG:NAME (mcp:duckduckgo, mcp:hub:binwalk); toolchain:NAME[@VERSION] (toolchain:go,
toolchain:jdk@17); preset:ID (its agent's image, its MCP servers and toolchains). KIND for --list and --all: images,
mcp, toolchains, presets.

Each item runs the command that does it, one after the other, in this terminal: sandbox-ai build TARGET, sandbox-ai
mcp add ENTRY, sandbox-ai toolchain add NAME[@VERSION]. Their checks stay as they are: only entries of the signed
catalogs (an MCP server's image pulled by its digest, a toolchain's archive checked against its SHA-256); an MCP
server the catalog has no signed image of yet (pending) is built from its reviewed source only when named, after
mcp add --build's own question; --all leaves it out, and so an MCP server that needs a secret of yours (named: set
the secret first). What is installed already is skipped. The end says what was installed, skipped and what failed.

On another host: sandbox-ai hosts run HOST -- install ... (the app's Install menu chooses the host).
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
KINDS = ("images", "mcp", "toolchains", "presets")
KIND_OF_PREFIX = {"image": "images", "mcp": "mcp", "toolchain": "toolchains", "preset": "presets"}
# The images of `sandbox-ai build` besides the agents', with what each is for; their names as the launcher, vpn.py,
# cairn.py, artex.py and browser.py name them (tests/test_install_cmd.py keeps both the same).
OTHER_IMAGES = {"team": ("localhost/team-sandbox:latest", "Claude Code, Codex and OpenCode in one image (--with)"),
                "cairn": ("localhost/sandbox-ai-cairn:latest", "Cairn's board (--cairn)"),
                "artex": ("localhost/sandbox-ai-artex:latest", "ARTEX (--artex)"),
                "browser": ("localhost/sandbox-ai-browser:latest", "the sandbox's own browser (--browser)"),
                "vpn": ("localhost/sandbox-ai-vpn:latest", "WireGuard, OpenVPN, Mullvad, Tailscale (--vpn)"),
                "tor": ("localhost/sandbox-ai-tor:latest", "Tor (--vpn with a Tor profile)")}
# What a preset's options need beside its agent: an image of its own.
OPTION_IMAGES = {"--cairn": "cairn", "--artex": "artex", "--browser": "browser"}
ITEM = re.compile(r"(image|mcp|toolchain|preset):[A-Za-z0-9][A-Za-z0-9_.@+:-]{0,127}")
READ_SECONDS = 180  # one listing (the catalogs are read over the network)

_launcher = None


def launcher():
    global _launcher
    if _launcher is None:
        spec = importlib.util.spec_from_file_location("agent_sandbox_for_install", HERE / "agent_sandbox.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        _launcher = loaded
    return _launcher


def printable(text):
    return launcher().printable(str(text))


def own_command(*args):
    """argv of this sandbox-ai's ARGS (its own main.py, the same interpreter)."""
    return [sys.executable, "-I", str(HERE / "main.py"), *args]


def read_json(args, run=subprocess.run):
    """The JSON `sandbox-ai ARGS` prints, or (None, why) when it cannot be read."""
    try:
        result = run(own_command(*args), capture_output=True, text=True, timeout=READ_SECONDS,
                     stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if result.returncode != 0:
        said = (result.stderr or result.stdout or "").strip().splitlines()
        return None, said[-1] if said else f"exit status {result.returncode}"
    try:
        return json.loads(result.stdout), None
    except (ValueError, RecursionError):
        return None, "not JSON"


def present_images(run=subprocess.run):
    """{image name: size in bytes} of the images Podman has, or None (no Podman, no answer)."""
    if not shutil.which("podman"):
        return None
    try:
        result = run(["podman", "images", "--format", "json"], capture_output=True, text=True, timeout=60,
                     stdin=subprocess.DEVNULL)
        listed = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, RecursionError):
        return None
    found = {}
    for entry in listed if isinstance(listed, list) else []:
        if isinstance(entry, dict):
            for name in entry.get("Names") or entry.get("names") or []:
                if isinstance(name, str):
                    found[name] = entry.get("Size") if isinstance(entry.get("Size"), int) else None
    return found


def item(kind, name, title, installed, size=None, note="", pending=False, every=True, command=None, needs=None):
    """One thing that can be installed: KIND (image, mcp, toolchain, preset) and NAME make its id; INSTALLED True,
    False or None (not known); SIZE in bytes where known; EVERY: part of --all; COMMAND: the argv after sandbox-ai."""
    return {"id": f"{kind}:{name}", "kind": KIND_OF_PREFIX[kind], "name": name, "title": title, "installed": installed,
            "size": size, "note": note, "pending": pending, "all": every and not pending, "command": command or [],
            **({"needs": list(needs)} if needs is not None else {})}


def image_items(present):
    l = launcher()
    rows = []
    note = "" if present is not None else "Podman is not installed or does not answer: not known whether it is built"
    for agent, title in l.AGENT_TITLES.items():
        name = f"localhost/{agent}-sandbox:latest"
        have = None if present is None else name in present
        rows.append(item("image", agent, f"{title}'s image", have, present.get(name) if have else None,
                         note or "a build of several minutes", command=["build", agent]))
    for target, (name, what) in OTHER_IMAGES.items():
        have = None if present is None else name in present
        rows.append(item("image", target, what, have, present.get(name) if have else None,
                         note or "a build of several minutes", command=["build", target]))
    return rows


def mcp_items(catalog, defined):
    """The servers of the signed catalogs (CATALOG: `mcp catalog --json`), each name once (sandbox-ai's own catalog
    first); DEFINED: the names of the servers there are (`mcp list --json`)."""
    entries = [e for e in (catalog or {}).get("entries") or [] if isinstance(e, dict)]
    entries.sort(key=lambda e: (e.get("prefix") != "mcp", str(e.get("name"))))
    rows, seen = [], set()
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or name in seen or entry.get("signature") != "good":
            continue  # signed things only: an unsigned or bad catalog's entries are not offered
        seen.add(name)
        prefix = entry.get("prefix") or "mcp"
        ref = name if prefix == "mcp" else f"{prefix}:{name}"
        pending = not entry.get("published")
        secrets = [str(v.get("name")) for v in entry.get("env") or [] if isinstance(v, dict) and v.get("required")]
        notes = []
        if pending:
            notes.append("pending: no signed image yet; built here from its reviewed source after a question "
                         "(sandbox-ai mcp add NAME --build), unsigned")
        if secrets:
            notes.append(f"needs {', '.join(secrets)}: set it first (sandbox-ai secrets set NAME)")
        rows.append(item("mcp", ref, str(entry.get("title") or name), name in defined, None,
                         "; ".join(notes) or "an image pulled by its digest", pending=pending,
                         every=not secrets, command=["mcp", "add", ref, *(["--build"] if pending else [])]))
    return rows


def toolchain_items(catalog):
    rows = []
    for entry in (catalog or {}).get("toolchains") or []:
        if not isinstance(entry, dict) or not entry.get("signed") or not isinstance(entry.get("toolchain"), str):
            continue
        spec = entry["toolchain"]
        requires = [str(r) for r in entry.get("requires") or []]
        rows.append(item("toolchain", spec, f"{entry.get('title') or entry.get('name')} {entry.get('version') or ''}"
                         .strip(), bool(entry.get("installed")),
                         entry.get("size") if isinstance(entry.get("size"), int) else None,
                         "a download" + (f"; needs {', '.join(requires)} (added with it)" if requires else ""),
                         command=["toolchain", "add", spec]))
    return rows


def option_values(options, name):
    """The values of the option NAME in a preset's OPTIONS ("--x v" or "--x=v"), placeholders ({...}) left out."""
    found = []
    for index, token in enumerate(options):
        if not isinstance(token, str):
            continue
        if token == name and index + 1 < len(options) and isinstance(options[index + 1], str):
            found.append(options[index + 1])
        elif token.startswith(name + "="):
            found.append(token.split("=", 1)[1])
    return [v for v in found if v and "{" not in v and not v.startswith("-")]


def preset_items(presets, by_id):
    """What each preset needs (BY_ID: the items above by id): its agent's image (one of its agents' is enough), the
    images of --cairn, --artex, --browser and of the agents --with or --helper, its MCP servers and its toolchains."""
    rows = []
    for preset in presets or []:
        if not isinstance(preset, dict) or not isinstance(preset.get("id"), str):
            continue
        options = [o for o in preset.get("options") or [] if isinstance(o, str)]
        agents = [a for a in preset.get("agents") or [] if isinstance(a, str)]
        needs = []
        images = [f"image:{a}" for a in agents if f"image:{a}" in by_id]
        if images:
            built = [i for i in images if by_id[i]["installed"]]
            needs.append(built[0] if built else images[0])
        for value in option_values(options, "--with") + option_values(options, "--helper"):
            needs += [f"image:{a}" for a in value.split(",") if f"image:{a}" in by_id]
        needs += [f"image:{OPTION_IMAGES[o]}" for o in OPTION_IMAGES if any(t == o or t.startswith(o + "=")
                                                                           for t in options)]
        for ref in preset.get("mcp") or []:
            if isinstance(ref, str):
                prefix, _, name = ref.rpartition(":")
                for candidate in (f"mcp:{name}" if prefix in ("", "mcp") else f"mcp:{ref}", f"mcp:{name}"):
                    if candidate in by_id:
                        needs.append(candidate)
                        break
        needs += [f"toolchain:{v}" for v in option_values(options, "--toolchain") if f"toolchain:{v}" in by_id]
        needs = list(dict.fromkeys(needs))
        if not needs:
            continue
        missing = [by_id[n] for n in needs if not by_id[n]["installed"]]
        sizes = [m["size"] for m in missing if isinstance(m.get("size"), int)]
        rows.append(item("preset", preset["id"], str(preset.get("title") or preset["id"]),
                         None if any(by_id[n]["installed"] is None for n in needs) else not missing,
                         sum(sizes) if sizes else None, "", needs=needs,
                         every=not any(by_id[n]["pending"] or not by_id[n]["all"] for n in needs)))
    return rows


def listing(kinds=KINDS, run=subprocess.run):
    """{"items": [...], "problems": [...]}: what can be installed here, each with its state (item())."""
    problems, rows = [], []
    if "images" in kinds or "presets" in kinds:
        rows += image_items(present_images(run))
    if "mcp" in kinds or "presets" in kinds:
        catalog, why = read_json(["mcp", "catalog", "--json"], run)
        if why:
            problems.append(f"MCP catalogs: {why}")
        defined, why = read_json(["mcp", "list", "--json"], run)
        names = {d.get("name") for d in defined or [] if isinstance(d, dict)}
        rows += mcp_items(catalog, names)
        problems += [f"MCP catalogs: {p}" for p in (catalog or {}).get("problems") or [] if isinstance(p, str)]
    if "toolchains" in kinds or "presets" in kinds:
        catalog, why = read_json(["toolchain", "catalog", "--json"], run)
        if why:
            problems.append(f"toolchain catalog: {why}")
        rows += toolchain_items(catalog)
    if "presets" in kinds:
        presets, why = read_json(["presets", "list", "--json"], run)
        if why:
            problems.append(f"presets: {why}")
        rows += preset_items(presets, {r["id"]: r for r in rows})
    return {"items": [r for r in rows if r["kind"] in kinds], "problems": [str(p) for p in problems]}


def size_text(size):
    if not isinstance(size, int):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return "-"


def state_text(row):
    return "pending" if row["pending"] and not row["installed"] else \
        {True: "installed", False: "-", None: "?"}[row["installed"]]


def show_listing(found, out=print):
    for kind in KINDS:
        rows = [r for r in found["items"] if r["kind"] == kind]
        if not rows:
            continue
        out(f"{kind}:")
        width = max(len(r["id"]) for r in rows) + 2
        for r in rows:
            note = r["note"] if r["kind"] != "presets" else "needs " + ", ".join(r.get("needs") or [])
            out(f"  {printable(r['id']).ljust(width)}{state_text(r).ljust(11)}{size_text(r['size']).rjust(9)}  "
                f"{printable(r['title'])}" + (f" - {printable(note)}" if note else ""))
        out("")
    for problem in found["problems"]:
        out(f"note: {printable(problem)}")


def plan(found, wanted, every=False, kinds=KINDS):
    """(steps, skipped, unknown): STEPS [(id, argv after sandbox-ai)] for WANTED (ids; a preset: what it needs) or,
    EVERY, each item of KINDS that --all takes, in the order images, toolchains, MCP servers; SKIPPED [(id, why)]."""
    by_id = {r["id"]: r for r in found["items"]}
    unknown = [w for w in wanted if w not in by_id]
    chosen = [r["id"] for r in found["items"] if r["all"] and r["kind"] in kinds and r["kind"] != "presets"] \
        if every else [w for w in wanted if w in by_id]
    expanded = []
    for name in chosen:
        expanded += by_id[name].get("needs") or [] if by_id[name]["kind"] == "presets" else [name]
    order = {"images": 0, "toolchains": 1, "mcp": 2}
    steps, skipped = [], []
    for name in sorted(dict.fromkeys(expanded), key=lambda n: order.get(by_id[n]["kind"], 3)):
        row = by_id[name]
        if row["installed"]:
            skipped.append((name, "installed already"))
        else:
            steps.append((name, row["command"]))
    if every:
        skipped += [(r["id"], r["note"]) for r in found["items"] if r["kind"] in kinds and r["kind"] != "presets"
                    and not r["all"] and not r["installed"]]
    return steps, skipped, unknown


def install(steps, out=print, call=subprocess.call):
    """Run each step's command in this terminal (its questions asked here), one after the other; one that fails stops
    none of the others. (installed, failed) ids."""
    done, failed = [], []
    for number, (name, argv) in enumerate(steps, 1):
        out(f"sandbox-ai install: [{number}/{len(steps)}] {name}: sandbox-ai {' '.join(argv)}")
        sys.stdout.flush()
        try:
            status = call(own_command(*argv))
        except OSError as exc:
            status = 1
            out(f"sandbox-ai install: {name}: {printable(exc)}")
        (done if status == 0 else failed).append(name)
        out(f"sandbox-ai install: [{number}/{len(steps)}] {name}: " + ("done" if status == 0 else
                                                                      f"failed (exit status {status})"))
    return done, failed


def help_style(prog):
    loaded = sys.modules.get("sandbox_ai_helpstyle")
    if loaded is None:
        spec = importlib.util.spec_from_file_location("sandbox_ai_helpstyle", HERE / "helpstyle.py")
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        sys.modules["sandbox_ai_helpstyle"] = loaded
    return loaded.CommandsFormatter(prog)


def parser():
    p = argparse.ArgumentParser(prog="sandbox-ai install", formatter_class=help_style, allow_abbrev=False,
                                description="Build or add what starts need - agent images, MCP servers of the "
                                            "signed catalogs, toolchains, what a preset needs: all of it, nothing, "
                                            "or a part, through sandbox-ai build, mcp add and toolchain add.",
                                epilog="\n\n".join(__doc__.split("\n\n")[1:5]))
    p.add_argument("items", nargs="*", metavar="ITEM",
                   help="KIND:NAME - image:claude, mcp:duckduckgo, mcp:hub:binwalk, toolchain:go, toolchain:jdk@17, "
                        "preset:python")
    p.add_argument("-l", "--list", action="store_true", help="what there is, whether it is installed, its size")
    p.add_argument("-a", "--all", action="store_true",
                   help="everything signed not installed yet (not: pending MCP servers, those that need a secret)")
    p.add_argument("-k", "--kind", action="append", choices=KINDS, default=[],
                   help="only this kind with --list or --all; repeatable")
    p.add_argument("-n", "--dry-run", action="store_true", help="say the commands, run none")
    p.add_argument("-j", "--json", action="store_true", help="with --list or --dry-run: as JSON, for programs")
    return p


def main(argv, out=print, run=subprocess.run, call=subprocess.call):
    args = parser().parse_args(argv)
    kinds = tuple(args.kind) or KINDS
    if sum((bool(args.list), bool(args.all), bool(args.items))) != 1:
        print("sandbox-ai install: give one of --list, --all or the items (KIND:NAME)", file=sys.stderr)
        return 2
    bad = [i for i in args.items if not ITEM.fullmatch(i)]
    if bad:
        print(f"sandbox-ai install: {printable(bad[0])} is no KIND:NAME (image:claude, mcp:duckduckgo, "
              "toolchain:go, preset:python)", file=sys.stderr)
        return 2
    wanted_kinds = kinds if not args.items else tuple(dict.fromkeys(
        KIND_OF_PREFIX[i.split(":", 1)[0]] for i in args.items)) + (("images", "mcp", "toolchains")
                                                                     if any(i.startswith("preset:") for i in args.items)
                                                                     else ())
    found = listing(wanted_kinds if not args.list else kinds, run)
    if args.list:
        if args.json:
            out(json.dumps(found, indent=1))
        else:
            show_listing(found, out)
        return 0
    steps, skipped, unknown = plan(found, args.items, every=args.all, kinds=kinds)
    if unknown:
        print(f"sandbox-ai install: not known here: {printable(', '.join(unknown))} (sandbox-ai install --list)",
              file=sys.stderr)
        return 2
    if args.dry_run:
        if args.json:
            out(json.dumps({"steps": [{"id": n, "command": ["sandbox-ai", *a]} for n, a in steps],
                            "skipped": [{"id": n, "why": w} for n, w in skipped], "problems": found["problems"]},
                           indent=1))
            return 0
        for name, argv_ in steps:
            out(f"{name}: sandbox-ai {' '.join(argv_)}")
        for name, why in skipped:
            out(f"{name}: skipped ({printable(why)})")
        return 0
    for problem in found["problems"]:
        out(f"sandbox-ai install: note: {printable(problem)}")
    done, failed = install(steps, out, call)
    out(f"sandbox-ai install: {len(done)} installed, {len(skipped)} skipped, {len(failed)} failed"
        + (f": {', '.join(failed)}" if failed else ""))
    for name, why in skipped:
        out(f"  skipped {name}: {printable(why)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
