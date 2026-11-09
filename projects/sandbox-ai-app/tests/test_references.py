"""The pages call only what backend.py defines (no Qt needed): every `backend.NAME`, `from .backend import
NAME`, `sai.NAME` and `self.sai.NAME` in the package must exist in backend.py, as a module-level name or an
attribute of SandboxAI. A merge once left pages calling backend.SECRET_NAME and sai.load_project, which no
longer existed; the Qt tests that would notice cannot run everywhere."""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "sandbox_ai_app"


def module_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for target in node.targets for t in ast.walk(target) if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names |= {(alias.asname or alias.name).split(".")[0] for alias in node.names}
    return names


def class_attributes(tree, name):
    """Methods, properties, class attributes and every self.X assigned in the methods of class NAME."""
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name)
    names = set()
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    for node in ast.walk(cls):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(
            node, (ast.AnnAssign, ast.AugAssign)) else []
        for target in targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "self":
                names.add(target.attr)
    return names


def is_sai(node):
    """sai or self.sai (how the pages hold their SandboxAI)."""
    return (isinstance(node, ast.Name) and node.id == "sai") or (
        isinstance(node, ast.Attribute) and node.attr == "sai" and isinstance(node.value, ast.Name)
        and node.value.id == "self")


def references():
    """(file, line, "backend" or "sai", name) of every use in the package outside backend.py."""
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "backend.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute):
                if isinstance(node.value, ast.Name) and node.value.id == "backend":
                    yield path.name, node.lineno, "backend", node.attr
                elif is_sai(node.value):
                    yield path.name, node.lineno, "sai", node.attr
            elif isinstance(node, ast.ImportFrom) and node.module == "backend" and node.level == 1:
                for alias in node.names:
                    yield path.name, node.lineno, "backend", alias.name


def test_pages_use_only_what_backend_defines():
    tree = ast.parse((PACKAGE / "backend.py").read_text())
    known = {"backend": module_names(tree), "sai": class_attributes(tree, "SandboxAI")}
    found = list(references())
    assert any(kind == "sai" for *_, kind, _ in found) and any(kind == "backend" for *_, kind, _ in found)
    missing = [f"{file}:{line} {kind}.{name}" for file, line, kind, name in found if name not in known[kind]]
    assert not missing, "not defined in backend.py:\n" + "\n".join(missing)


def test_the_check_catches_a_missing_name(tmp_path):
    tree = ast.parse("class SandboxAI:\n    def answer(self): pass\nAGENTS = ()\n")
    assert "answer" in class_attributes(tree, "SandboxAI") and "load_project" not in class_attributes(tree, "SandboxAI")
    assert "AGENTS" in module_names(tree) and "SECRET_NAME" not in module_names(tree)


# -- nothing slow on the GUI thread --

# SandboxAI methods that run a command, ask the keyring or read big logs (the window must keep polling the
# approvals meanwhile, or questions are refused after a minute). Declared here; test_every_slow_backend_method_is_declared
# finds every method of backend.py that runs a process, sleeps or calls one of these, and wants it here too.
BLOCKING_SAI = {"call", "answer", "dry_run", "doctor", "mcp_catalog", "statistics", "set_secret", "delete_secret",
                "secrets", "login_status", "remembered", "forget_remembered", "question_context", "run_log",
                "devices_added", "device_check", "device_candidates", "add_device", "remove_device", "set_limits",
                "workspaces", "add_workspace", "remove_workspace",
                "host_version_known",  # one short SSH command (review 6 A-N3)
                "write_project",
                # over SSH to the hosts of `sandbox-ai hosts` (seconds; a host that does not answer: up to the timeout)
                "host_run", "host_json", "host_instances", "host_questions", "host_test", "host_update",
                "limits_shown", "review_home", "host_remembered", "host_net_options", "ended_starts", "start_history",
                "collect", "host_set", "host_add", "host_remove", "collect_as_shown", "host_devices_added",
                "host_workspaces", "host_secrets", "secret_exists", "host_approvals_history", "session_ids",
                "recent_folders", "host_install",
                # starts, MCP servers, net keep, the privacy check, version and demo device on a host (or here)
                "host_launch_options", "host_dry_run", "host_mcp_servers", "host_mcp_catalog", "host_mcp_targets",
                "host_mcp_attach", "net_keep", "privacy_check", "version_details", "host_doctor", "demo_device",
                "mcp_servers", "mcp_serving", "mcp_attach", "mcp_attach_targets",
                # the parity section: the Manage pages, Activity, Statistics and Teams on every host
                "host_listing", "host_vpn_profiles", "host_git_logins", "host_publish_logins", "host_identities",
                "host_save_identity", "host_login_status", "host_kits_inventory", "host_kit_command", "revoke_hooks",
                "plugins_status", "host_canary_log", "canary_runs", "canary_run", "canary_resume", "host_preset_dirs",
                "host_preset_dir_command", "trusted_projects", "forget_trusted", "host_activity", "host_statistics",
                "teams", "team_show", "team_post", "team_stop", "team_delete", "kits_inventory", "kit_command",
                "canary_log", "preset_dirs", "preset_dir_command", "identities", "save_identity", "git_logins",
                "publish_logins", "vpn_check", "vpn_remove", "vpn_add",
                # `sandbox-ai llm list` and `vpn list` (the wizard's Model page, the preset form, the VPN page)
                "llm_endpoints", "vpn_profiles", "listing",
                # MCP servers into a running start and out (--live), a host's model servers and connection logs
                "host_mcp_attach_live", "host_mcp_detach_live", "mcp_live", "host_llm_endpoints", "host_llm_models",
                "host_llm_servers", "host_connections", "host_log_summary",
                # every plugin marketplace of a host (the Kits page's browser) and its version check over SSH
                "marketplaces", "host_marketplaces", "require_marketplaces",
                # a host's presets, start choices, project files and trust, the web interface's logins and
                # certificate, an answer sent there, MCP servers stopped or detached, starts stopped, secrets' backend,
                # a login added, and the known-options check (`COMMAND --help` there) under each of them
                "host_preset", "host_presets", "host_start_choices", "host_project_show", "host_project_trust",
                "host_trust_defaults", "host_web_users", "host_web_user_remove", "host_web_cert", "host_answer",
                "host_mcp_detach", "stop_mcp", "stop_run", "secrets_backend", "add_login", "checked",
                "host_command_options", "require_options", "host_image_variants",
                # sandbox-ai's registry of what runs (net.running) asks Podman whether the pod of a run whose launcher
                # is gone still runs (up to its timeout) and removes the directories of runs that ended; the sessions'
                # rows ask Podman about stopped ones: so does everything that reads them
                "runs", "sessions", "instances", "run_for", "file_roots", "known_projects", "sandbox_folders",
                "rw_mounts", "autostart_risk"}
# (approvals_snapshot and questions read the registry with live_runs: files and /proc only, never Podman.)
# (host_statuses(cached=True) reads what `sandbox-ai hosts list` kept, without SSH: the Hosts page lists it at once.)
# Slow, but on the GUI thread on purpose (each call site says so with a comment "on the GUI thread on purpose"):
# stop_commands only when quitting, bounded by one grace period; end_groups is its part.
ON_PURPOSE = {"stop_commands", "end_groups"}
ON_PURPOSE_MARK = "on the GUI thread on purpose"
BLOCKING_MODULES = {"subprocess"}
BLOCKING_TIME = {"sleep"}  # time.sleep: never in a handler (a QTimer waits instead)
# QProcess's waits (and QThread.wait) block the event loop until the process (thread) is done: AsyncCommand's signals
# instead. Matched by the name alone.
BLOCKING_WAITS = {"waitForFinished", "waitForStarted", "waitForReadyRead", "waitForBytesWritten"}
BLOCKING_BACKEND = {"read_connections"}
# files.py: copies (up to 1 GiB), moves, deletes and owner changes of whole trees, podman unshare (the Files page
# runs them in Task.thread). Called as root.NAME(...) or files.NAME(...): matched by the name alone.
BLOCKING_FILES = {"copy_out", "copy_in", "copy_entry", "move_entry", "delete_entry", "chown_entry", "take_ownership"}
# wizard_llm's functions that run `sandbox-ai llm list|add|check|models` (called as WL.NAME or wizard_llm.NAME), and
# presets' host_documents (`sandbox-ai presets list --json` on a host: PR.NAME or presets.NAME).
BLOCKING_HELPERS = {"WL": {"list_endpoints", "add_and_check", "list_models"},
                    "wizard_llm": {"list_endpoints", "add_and_check", "list_models"},
                    "PR": {"host_documents"}, "presets": {"host_documents"},
                    # chromium.check: a child process tries Chromium's namespaces (up to its timeout)
                    "chromium": {"check", "probe"},
                    # name_fields.read_names: a host's secrets or VPN profiles (`secrets list`, `vpn list` there)
                    "name_fields": {"read_names"}, "NF": {"read_names"}}
# widgets.background, ThreadCall, Task.thread and call_watched; the Kits page's start_change (its Task.call_watched)
WRAPPERS = {"background", "ThreadCall", "thread", "call_watched", "start_change"}
# The modules not checked call by call: backend.py (what is slow is declared above), widgets.py (background, ThreadCall
# and Task themselves; the waits of the app's end), and the helpers whose slow functions the pages may only hand to a
# wrapper (files.py, wizard_llm.py, presets.py, name_fields.py, chromium.py: BLOCKING_FILES, BLOCKING_HELPERS) or that
# start a terminal's command (pty_bridge.py: a fork and exec, read with a QSocketNotifier).
HELPER_MODULES = {"backend.py", "widgets.py", "files.py", "wizard_llm.py", "presets.py", "pty_bridge.py",
                  "name_fields.py", "chromium.py"}


def call_name(node):
    return node.func.id if isinstance(node.func, ast.Name) else \
        node.func.attr if isinstance(node.func, ast.Attribute) else None


def holds_sai(node):
    """sai, self.sai, or any X.sai (self.page.sai, self.wizard_.sai, self.parent().sai): a page's SandboxAI."""
    return (isinstance(node, ast.Name) and node.id == "sai") or (isinstance(node, ast.Attribute) and node.attr == "sai")


def blocking(node):
    """A description if NODE (a Call) blocks, else None."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if holds_sai(func.value) and func.attr in BLOCKING_SAI:
            return f"sai.{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id in BLOCKING_MODULES:
            return f"{func.value.id}.{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id == "time" and func.attr in BLOCKING_TIME:
            return f"time.{func.attr}()"
        if func.attr in BLOCKING_WAITS:
            return f"{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id == "backend" and func.attr in BLOCKING_BACKEND:
            return f"backend.{func.attr}()"
        if func.attr in BLOCKING_FILES:
            return f"{func.attr}()"
        if isinstance(func.value, ast.Name) and func.attr in BLOCKING_HELPERS.get(func.value.id, ()):
            return f"{func.value.id}.{func.attr}()"
    if isinstance(func, ast.Name) and func.id in BLOCKING_BACKEND:
        return f"{func.id}()"
    return None


def handed_methods(tree):
    """The names of methods handed to a wrapper as self.NAME (background(self.scan, ...)) and never called directly
    (self.NAME(...)) in the module: their body runs in the wrapper's thread."""
    handed, called = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and call_name(node) in WRAPPERS:
            for value in [*node.args, *(k.value for k in node.keywords)]:
                if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self":
                    handed.add(value.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                isinstance(node.func.value, ast.Name) and node.func.value.id == "self":
            called.add(node.func.attr)
    return handed - called


def deferred(node, parents, handed=frozenset()):
    """True if NODE lies in a lambda handed to a wrapper, in a nested function whose name is, or in a method that is
    (HANDED: handed_methods)."""
    child, parent = node, parents.get(node)
    while parent is not None:
        if isinstance(child, ast.Lambda) and isinstance(parent, ast.Call) and call_name(parent) in WRAPPERS \
                and child is not parent.func:
            return True
        if isinstance(child, ast.keyword) and isinstance(parent, ast.Call) and call_name(parent) in WRAPPERS \
                and isinstance(child.value, ast.Lambda):
            return True
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(parent, (ast.FunctionDef,
                                                                                            ast.AsyncFunctionDef)):
            handed_here = any(isinstance(n, ast.Call) and call_name(n) in WRAPPERS and any(
                isinstance(a, ast.Name) and a.id == child.name for a in [*n.args, *(k.value for k in n.keywords)])
                for n in ast.walk(parent))
            if handed_here:
                return True
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and isinstance(parent, ast.ClassDef) and \
                child.name in handed:
            return True
        child, parent = parent, parents.get(parent)
    return False


def blocking_calls(source):
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    handed, lines = handed_methods(tree), source.splitlines()
    return [(node.lineno, what) for node in ast.walk(tree) if isinstance(node, ast.Call)
            for what in [blocking(node)] if what and not deferred(node, parents, handed)
            and ON_PURPOSE_MARK not in lines[node.lineno - 1]]


def test_pages_run_commands_and_slow_reads_only_in_the_background():
    """Every module of the app but the helpers (HELPER_MODULES): pages, dialogs, the window, tours, menus."""
    names = sorted(path.name for path in PACKAGE.glob("*.py") if path.name not in HELPER_MODULES)
    assert {"ui.py", "kits_page.py", "hosts_page.py", "tours.py", "intro.py", "main.py"} <= set(names)
    found = [f"{name}:{line} {what}" for name in names
             for line, what in blocking_calls((PACKAGE / name).read_text())]
    assert not found, "on the GUI thread (use widgets.background, ThreadCall or Task):\n" + "\n".join(found)


# What makes a method of backend.py slow by itself: a process (subprocess, the runner of call()), a pipe served until
# its end (Pipes), a sleep.
SLOW_PRIMITIVES = {("subprocess", None), ("time", "sleep"), ("self", "run"), (None, "Pipes")}


def backend_calls(function):
    """{("self", NAME)} of the methods FUNCTION calls on self, {(None, NAME)} of the module's functions it calls, and
    ("subprocess", ATTR) / ("time", ATTR) of what it calls of those modules."""
    found = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            found.add((func.value.id, func.attr))
        elif isinstance(func, ast.Name):
            found.add((None, func.id))
    return found


def slow_methods(tree):
    """The SandboxAI methods that run a process or sleep, or call (through self, or a function of the module) one
    that does or one BLOCKING_SAI declares slow."""
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SandboxAI")
    graph = {("self", n.name): backend_calls(n) for n in cls.body if isinstance(n, ast.FunctionDef)}
    graph.update({(None, n.name): backend_calls(n) for n in tree.body if isinstance(n, ast.FunctionDef)})

    def primitive(call):
        return call in SLOW_PRIMITIVES or (call[0] == "subprocess")
    slow = {key for key, calls in graph.items() if any(primitive(c) for c in calls)}
    slow |= {("self", name) for name in BLOCKING_SAI if ("self", name) in graph}
    while True:
        more = {key for key, calls in graph.items() if key not in slow and calls & slow}
        if not more:
            break
        slow |= more
    return {name for kind, name in slow if kind == "self"}


def test_every_slow_backend_method_is_declared():
    """A method that runs `sandbox-ai ...` (here or over SSH), a process, or reads what asks Podman is declared in
    BLOCKING_SAI (or ON_PURPOSE), so the check above holds the pages to the background for it."""
    tree = ast.parse((PACKAGE / "backend.py").read_text())
    known = class_attributes(tree, "SandboxAI")
    assert not (BLOCKING_SAI | ON_PURPOSE) - known, f"not in backend.py: {sorted((BLOCKING_SAI | ON_PURPOSE) - known)}"
    undeclared = sorted(slow_methods(tree) - BLOCKING_SAI - ON_PURPOSE)
    assert not undeclared, "slow (a process, SSH, Podman), not declared in BLOCKING_SAI:\n" + "\n".join(undeclared)


def test_the_slow_method_check_follows_calls():
    tree = ast.parse('''
class SandboxAI:
    def call(self, *args):
        return subprocess.run(args)
    def wrapper(self):
        return self.call("x")
    def deeper(self):
        return helper(self)
    def fast(self):
        return 1
    def runs(self):
        return self.module("net").running()
    def users(self):
        return self.runs()
def helper(sai):
    return sai.wrapper()
''')
    assert slow_methods(tree) >= {"call", "wrapper", "users", "runs"} and "fast" not in slow_methods(tree)


def test_the_blocking_check_knows_the_file_operations():
    source = '''
class Page:
    def bad(self):
        self.root.delete_entry("x")
        files.copy_entry(a, "x", b, "")

    def good(self):
        def work(cancelled):
            return files.move_entry(a, "x", b, "", cancelled=cancelled)
        self.task.thread(work, self.done, "text", cancellable=True)
'''
    assert [what for _, what in blocking_calls(source)] == ["delete_entry()", "copy_entry()"]


def test_the_blocking_check_tells_direct_calls_from_deferred_ones():
    source = '''
class Page:
    def bad(self):
        status, out, err = self.sai.call("net", "show")
        subprocess.run(["x"])
        records = backend.read_connections(path)
        self.page.sai.file_roots()
        self.process.waitForFinished(5000)
        time.sleep(1)

    def good(self):
        background(self.sai.call, "net", "show", done=self.done)
        background(lambda: self.sai.call("mcp", "add", timeout=900), done=self.done)
        def work(cancelled):
            return self.sai.statistics(directory)
        self.task.thread(work, self.done, "text")
        ThreadCall(self.sai.answer, "1-1-1", "allow", "once").start()
        background(lambda: WL.add_and_check(sai, fields, "opencode"), done=self.done)
        background(self.scan, done=self.done)
        self.task.call_watched(self.sai, lambda: self.sai.host_install("h"), self.done, "text")
        self.sai.stop_commands()  # on the GUI thread on purpose: quitting
        background(PR.host_documents, self.sai, "h", done=self.done)

    def scan(self):
        return self.sai.instances()

    def also_bad(self):
        def helper():
            return self.sai.doctor()
        return helper()

    def models(self):
        return WL.list_models(self.sai, "lmstudio") + PR.host_documents(self.sai, "h")

    def read(self):
        return self.sai.runs()

    def again(self):
        background(self.read, done=self.done)
        return self.read()
'''
    assert sorted(what for _, what in blocking_calls(source)) == sorted([
        "sai.call()", "subprocess.run()", "backend.read_connections()", "sai.file_roots()", "waitForFinished()",
        "time.sleep()", "sai.doctor()", "WL.list_models()", "PR.host_documents()", "sai.runs()"])


# -- one confirmation policy: every call path that ends, removes or weakens something asks first --

# How the pages do each action of backend.NEEDS_CONFIRM: a SandboxAI method, or the words of a sandbox-ai command
# (consecutive string literals of one call or tuple). The functions that issue one must ask first with
# widgets.ask_first(..., ACTION, ...): in themselves or in a function they are nested in.
MUTATIONS = {
    "start-stop": ["stop_run", ("session", "stop")],
    "team-stop": ["team_stop"],
    "mcp-stop": ["stop_mcp"],
    "project-trust": ["trust_project", "host_project_trust"],
    "login-remove": [("identities", "remove"), "save_identity"],
    "login-logout": [("--logout",)],
    "kit-hooks-trust": ["trust_hooks_argv"],
    "gc": ["collect_as_shown"],
    "approval-forget": ["forget_remembered"],
    "defaults-lower": [("defaults", "set"), ("defaults", "unset")],
    "net-allow": [("net", "allow")],
    "net-untaint": [("net", "untaint")],
    "workspace-writable": ["add_workspace"],
    "device-add": ["add_device"],
    "limits-raise": ["set_limits"],
    "host-remove": ["host_remove"],
    "host-edit": ["host_set"],
    "host-install": ["host_install"],
    "vpn-remove": ["vpn_remove"],
    "llm-remove": [("llm", "remove")],
    "secret-delete": ["delete_secret"],
    "secret-replace": ["set_secret", "vpn_add"],
    "canary-tokens": ["canary_run"],
    "canary-resume": ["canary_resume"],
    "project-forget": ["forget_trusted", ("wizard", "forget")],
    "team-delete": ["team_delete"],
    "image-remove": [("images", "remove")],
    "mcp-attach": ["mcp_attach", "host_mcp_attach_live"],
    "mcp-detach": ["host_mcp_detach_live", "host_mcp_detach"],
    "net-keep": ["net_keep"],
    "web-user-remove": ["host_web_user_remove"],
    "web-user-reset": [("reset",)],
    "web-cert-renew": ["host_web_cert"],
    "web-listen": [("serve",)],
}

# The actions of the web interface's table (backend.NEEDS_CONFIRM is the same set) that no page of the app does yet:
# the app gets them with the command-line parity work, and asks first there then. Exact: an action a page asks for is
# taken off this list (the test says so), and nothing else may be missing.
WEB_ONLY = set()  # every action of the table is asked for by a page now

def string_runs(node):
    """Every run of consecutive string literals among the arguments of a call or the items of a tuple or list."""
    items = list(node.args) if isinstance(node, ast.Call) else list(getattr(node, "elts", []))
    words = [item.value if isinstance(item, ast.Constant) and isinstance(item.value, str) else None for item in items]
    return [tuple(words[i:j]) for i in range(len(words)) for j in range(i + 1, len(words) + 1)
            if all(w is not None for w in words[i:j])]


def issues(node, marker):
    """True if NODE (a Call, Tuple, List or Attribute) is how MARKER is done."""
    if isinstance(marker, str):
        return isinstance(node, ast.Attribute) and node.attr == marker and is_sai(node.value)
    return isinstance(node, (ast.Call, ast.Tuple, ast.List)) and marker in string_runs(node)


def asked_actions(function):
    """The actions FUNCTION (with what is nested in it) asks for: the string literals of ask_first's second argument."""
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "ask_first" \
                and len(node.args) > 1:
            found |= {n.value for n in ast.walk(node.args[1]) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return found


def unasked(source):
    """[(line, action)] of the mutating calls in SOURCE whose function (or one it is nested in) does not ask first."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        for action, markers in MUTATIONS.items():
            if not any(issues(node, marker) for marker in markers):
                continue
            chain, parent = [], parents.get(node)
            while parent is not None:
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    chain.append(parent)
                parent = parents.get(parent)
            if not chain:
                continue  # module level: a table, not a call path
            if not any(action in asked_actions(function) for function in chain):
                found.append((node.lineno, action))
    return found


def test_every_call_that_ends_removes_or_weakens_something_asks_first():
    """The walk over the mutating calls of the pages (review round 3, A5): net allow, raised limits, a replaced or
    deleted secret, kits, MCP attach and restart, defaults lowered, gc, forget, untaint, devices, workspaces, hosts,
    stops - each asks through widgets.ask_first with its action of backend.NEEDS_CONFIRM."""
    import sys
    sys.path.insert(0, str(PACKAGE.parent))
    from sandbox_ai_app import backend
    actions = set(backend.NEEDS_CONFIRM) | set(backend.APP_NEEDS_CONFIRM)
    assert set(MUTATIONS) <= actions
    found, asked = [], set()
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in ("backend.py", "widgets.py"):
            continue
        source = path.read_text()
        found += [f"{path.name}:{line} {action}" for line, action in unasked(source)]
        asked |= asked_actions(ast.parse(source))
    assert not found, "not asked first (widgets.ask_first):\n" + "\n".join(found)
    assert not asked - actions, f"asked for actions the table does not have: {sorted(asked - actions)}"
    assert WEB_ONLY <= actions, f"WEB_ONLY names actions the table does not have: {sorted(WEB_ONLY - actions)}"
    assert not WEB_ONLY & asked, f"asked for now: take them off WEB_ONLY: {sorted(WEB_ONLY & asked)}"
    assert actions - WEB_ONLY <= asked, f"actions of the table no page asks for: {sorted(actions - WEB_ONLY - asked)}"


def test_the_walk_finds_a_call_that_does_not_ask():
    source = '''
class Page:
    def bad(self):
        background(self.sai.host_run, host, "net", "allow", run, done=self.done)
        background(self.sai.set_limits, run, "16g", done=self.done)

    def good(self):
        if not ask_first(self, "net-allow", "t", "x", "Allow"):
            return
        def work():
            return self.sai.host_run(host, "net", "allow", run)
        background(work, done=self.done)

    def unrelated(self):
        if ask_first(self, "gc", "t", "x", "Remove"):
            self.sai.set_limits(run, "16g")
'''
    assert unasked(source) == [(4, "net-allow"), (5, "limits-raise"), (16, "limits-raise")]
