#!/usr/bin/env python3
"""sandbox/mcp_catalog_tools.json: the tools each server of the MCP catalogs lists, from the catalogs' own records.

The signed catalogs (sandbox-ai-mcp, axross-mcp-hub) keep each server's tools/list in servers/NAME/tools.json - what
the signing run compared the server's answer with - but not in catalog.json, which is all a start downloads. The
launcher counts the MCP tools a start's model would see (mcp_budget.py: a warning for a small model with many tools)
from a server's pin first; for a catalog server that was never started in the project it takes this snapshot.

  python3 scripts/catalog_tools.py [--mcp DIR] [--hub DIR]           write sandbox/mcp_catalog_tools.json
  python3 scripts/catalog_tools.py --check [--mcp DIR] [--hub DIR]   exit 1 when the snapshot differs from them

DIR: a checkout of the catalog's repository (default: projects/sandbox-ai-mcp and projects/axross-mcp-hub beside
sandbox-ai); a catalog without a checkout keeps its entries of the snapshot as they are. Only servers that are entries
of the catalog's catalog.json are taken (a pending server's tools.json is not), by the entry's id (mcp:NAME, hub:NAME).
Standard library only.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "sandbox" / "mcp_catalog_tools.json"
CATALOGS = (("mcp", "sandbox-ai-mcp"), ("hub", "axross-mcp-hub"))


def commit(directory):
    try:
        done = subprocess.run(["git", "-C", str(directory), "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, timeout=30)
    except OSError:
        return None
    return done.stdout.strip() or None if done.returncode == 0 else None


def read_catalog(directory):
    """({entry id: [tool names]}, commit) of the catalog checkout DIRECTORY."""
    document = json.loads((directory / "catalog.json").read_text())
    found = {}
    for entry in document.get("entries", []):
        name, ident = entry.get("name"), entry.get("id")
        path = directory / "servers" / str(name) / "tools.json"
        if not isinstance(name, str) or not isinstance(ident, str) or not path.is_file():
            continue
        tools = json.loads(path.read_text()).get("tools")
        if isinstance(tools, list) and all(isinstance(tool, str) for tool in tools):
            found[ident] = sorted(tools)
    return found, commit(directory)


def snapshot(directories, old=None):
    """The snapshot of the catalogs in DIRECTORIES ({prefix: checkout or None}); OLD's entries for one without."""
    old = old or {"servers": {}, "sources": {}}
    servers, sources = {}, {}
    for prefix, repository in CATALOGS:
        directory = directories.get(prefix)
        if directory is not None and (directory / "catalog.json").is_file():
            found, head = read_catalog(directory)
            servers.update(found)
            sources[prefix] = f"{repository} {head}" if head else repository
        else:
            servers.update({k: v for k, v in old.get("servers", {}).items() if k.startswith(prefix + ":")})
            if prefix in old.get("sources", {}):
                sources[prefix] = old["sources"][prefix]
    return {"version": 1,
            "about": "The tools each server of the MCP catalogs lists (its servers/NAME/tools.json), by catalog entry "
                     "id; made by scripts/catalog_tools.py. The launcher counts a start's MCP tools from a server's "
                     "pin first, from this for a catalog server never started in the project.",
            "sources": sources, "servers": dict(sorted(servers.items()))}


def text(document):
    return json.dumps(document, indent=2) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--mcp", type=Path, default=ROOT / "projects" / "sandbox-ai-mcp")
    parser.add_argument("--hub", type=Path, default=ROOT / "projects" / "axross-mcp-hub")
    parser.add_argument("--check", action="store_true")
    a = parser.parse_args(argv)
    old = json.loads(TARGET.read_text()) if TARGET.is_file() else None
    new = snapshot({"mcp": a.mcp, "hub": a.hub}, old)
    if a.check:
        same = old is not None and old["servers"] == new["servers"]
        print("up to date" if same else f"{TARGET} differs from the catalogs: run scripts/catalog_tools.py")
        return 0 if same else 1
    TARGET.write_text(text(new))
    print(f"{TARGET}: {len(new['servers'])} servers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
