"""The MCP tools a start's model would see, against the budget of the model's class: a warning, never a refusal.

sandbox-ai's local-model research (round 5, "Memory A/B": research-llm/round5.md, LOCAL_LLM_REPORT.md section 6)
gave Gemma 4 12B and 26B the four memory and planning servers at once - 27 more tools - and both did worse (a finished
step redone after an interruption: 1.00 -> 0.67; part of a job dropped: 0.90 -> 0.74), while the mid-sized and large
models lost nothing to the count. The profiles (model_profiles.json "tool_budgets", model_profiles.tool_budget) give
each class of model the tools it copes with; this module counts what a start brings:

  a server's tools  its pin for the project (mcp_pins.py: the tools/list it was trusted with), else the catalog's
                    list of the server its definition came from ("source": mcp:NAME, hub:NAME; the snapshot
                    mcp_catalog_tools.json, scripts/catalog_tools.py), else sandbox-ai's own (the demo server, a
                    team's board), else unknown - the count is then "about" (the known ones only)
  the report        the count, the budget, the servers with the most tools first, those to leave out to get within
                    the budget (the largest first) and what is left (report, start_report)

The launcher says it as a WARNING line (warning_lines) and puts the report in the dry run ("mcp_tools"); the app's
Board makes a finding of it, its Expert view a note below the model. The human decides: nothing is refused.
Standard library only; Python 3.9.
"""
import importlib.util
import json
from pathlib import Path
import threading

HERE = Path(__file__).resolve().parent
CATALOG_TOOLS = HERE / "mcp_catalog_tools.json"
FILE_LIMIT = 4 << 20
# sandbox-ai's own servers: the demo (mcp_demo.TOOLS) and a team's board (team_board.tools: post, wait_for_task, done,
# wait_for_answer, board); tests/test_mcp_tool_budget.py keeps both the same as the servers.
OWN_TOOLS = {"demo": 2}
TEAM_TOOLS = 5
TEAM_PREFIX = "team-"
SHOWN = 3  # servers named with their tools in the warning
LOCK = threading.Lock()
_loaded = {}
_pins = None


def catalog_tools(path=None):
    """{catalog entry id: [tool names]} of the snapshot (mcp_catalog_tools.json); {} when it cannot be read."""
    path = Path(path or CATALOG_TOOLS)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        with LOCK:
            if key in _loaded:
                return _loaded[key]
        with open(path, "rb") as handle:
            data = handle.read(FILE_LIMIT + 1)
        document = json.loads(data.decode("utf-8")) if len(data) <= FILE_LIMIT else None
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return {}
    servers = document.get("servers") if isinstance(document, dict) else None
    found = {ident: tools for ident, tools in (servers.items() if isinstance(servers, dict) else [])
             if isinstance(ident, str) and isinstance(tools, list) and all(isinstance(t, str) for t in tools)}
    with LOCK:
        _loaded.clear()
        _loaded[key] = found
    return found


def pins_module():
    """mcp_pins.py, loaded once."""
    global _pins
    with LOCK:
        if _pins is None:
            spec = importlib.util.spec_from_file_location("sandbox_mcp_pins_budget", HERE / "mcp_pins.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _pins = module
    return _pins


def server_tools(name, definition, project=None, catalog=None):
    """{"server", "tools": how many (None: not known), "from": "pin", "catalog", "sandbox-ai" or None} of MCP
    server NAME (its DEFINITION) for a start in PROJECT (its main workspace; None: no pins read)."""
    if project is not None:
        try:
            pinned = pins_module().load(Path(project).expanduser().resolve(), name)
        except (OSError, ValueError):
            pinned = None
        if pinned is not None and pinned["tools"]:
            return {"server": name, "tools": len(pinned["tools"]), "from": "pin"}
    definition = definition if isinstance(definition, dict) else {}
    source = definition.get("source")
    if isinstance(source, str) and ("image" in definition or "rootfs" in definition):
        listed = (catalog_tools() if catalog is None else catalog).get(source)
        if listed is not None:
            return {"server": name, "tools": len(listed), "from": "catalog"}
    command = " ".join(str(part) for part in definition.get("command") or [])
    if name in OWN_TOOLS and "mcp_demo.py" in command:
        return {"server": name, "tools": OWN_TOOLS[name], "from": "sandbox-ai"}
    if name.startswith(TEAM_PREFIX) and "image" not in definition and "rootfs" not in definition:
        return {"server": name, "tools": TEAM_TOOLS, "from": "sandbox-ai"}
    return {"server": name, "tools": None, "from": None}


def report(model, budget, servers):
    """What a start of MODEL (its BUDGET: model_profiles.tool_budget, None: none known) with SERVERS (server_tools'
    entries) brings: {"model", "class", "budget", "budget_from", "family", "params", "evidence", "short", "tools"
    (the known ones), "about" (some not known), "unknown", "over" (more known tools than the budget), "servers"
    (the most tools first, the unknown last), "drop" (the fewest servers to leave out, the largest first, to get
    within the budget), "left" (the tools then)}."""
    servers = [dict(entry) for entry in servers]
    known = sorted((s for s in servers if s["tools"] is not None), key=lambda s: (-s["tools"], s["server"]))
    unknown = [s for s in servers if s["tools"] is None]
    total = sum(s["tools"] for s in known)
    limit = budget["tools"] if budget else None
    drop, left = [], total
    if limit is not None:
        for entry in known:
            if left <= limit:
                break
            drop.append(entry["server"])
            left -= entry["tools"]
    budget = budget or {}
    return {"model": model, "class": budget.get("class"), "budget": limit, "budget_from": budget.get("from"),
            "family": budget.get("family"), "params": budget.get("params"), "evidence": budget.get("evidence"),
            "short": budget.get("short"), "tools": total, "about": bool(unknown),
            "unknown": [s["server"] for s in unknown], "over": limit is not None and total > limit,
            "servers": known + unknown, "drop": drop, "left": left}


def start_report(model, servers, project, definitions, table, catalog=None):
    """report() of a start of MODEL with the MCP servers SERVERS (names) in PROJECT (its main workspace),
    DEFINITIONS {name: definition} (mcp.definitions and the start's own), TABLE the profiles (None: no budgets
    known: None)."""
    if table is None or not model:
        return None
    budget = profiles_module().tool_budget(model, table)
    names = list(dict.fromkeys(servers or []))
    return report(model, budget, [server_tools(name, (definitions or {}).get(name), project, catalog)
                                  for name in names])


_profiles = None


def profiles_module():
    """model_profiles.py, loaded once."""
    global _profiles
    with LOCK:
        if _profiles is None:
            spec = importlib.util.spec_from_file_location("sandbox_model_profiles_budget", HERE / "model_profiles.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _profiles = module
    return _profiles


def words(names):
    """"a", "a and b", "a, b and c"."""
    names = [str(name) for name in names]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def counted_text(found):
    """"27 MCP tools", "about 27 MCP tools (the tools of mine are not known yet)"."""
    if not found["about"]:
        return f"{found['tools']} MCP tools"
    return f"about {found['tools']} MCP tools (the tools of {words(found['unknown'])} are not known yet)"


def budget_text(found):
    """"about 10; 12B, from its name" or "about 35; its family ornith-1.5-35b"."""
    if found["budget_from"] == "name":
        return f"about {found['budget']}; {found['params']}B, from its name"
    return f"about {found['budget']}; its family {found['family']}"


def most_text(found):
    """"tasks brings 10, memory 9, codesearch 7": the servers with the most tools."""
    shown = [s for s in found["servers"] if s["tools"]][:SHOWN]
    if not shown:
        return ""
    return ", ".join([f"{shown[0]['server']} brings {shown[0]['tools']}"] +
                     [f"{s['server']} {s['tools']}" for s in shown[1:]])


def warning_lines(found, name):
    """The launcher's lines of FOUND (report(); None: nothing) for the endpoint NAME: one WARNING when the model would
    see more MCP tools than its class copes with, else none."""
    if not found or not found["over"]:
        return []
    research = f"sandbox-ai's research, {found['short']}" if found.get("short") else "sandbox-ai's research"
    most = most_text(found)
    line = (f"agent-sandbox: WARNING: --llm {name}: {found['model']} would see {counted_text(found)}, more than a "
            f"{found['class']} model copes with ({budget_text(found)})" + (f": {most}" if most else "") +
            f". Leave out {words(found['drop'])} (--mcp) to get to {found['left']}" +
            (f" and those of {words(found['unknown'])}" if found["about"] else "") + " - a warning: the start goes on "
            f"({research})")
    return ["".join(c if c.isprintable() else "?" for c in line)]
