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
# approvals meanwhile, or questions are refused after a minute).
BLOCKING_SAI = {"call", "answer", "dry_run", "doctor", "mcp_catalog", "statistics", "set_secret", "delete_secret",
                "secrets", "login_status", "remembered", "forget_remembered", "question_context", "run_log",
                "devices_added", "device_check", "device_candidates", "add_device", "remove_device", "set_limits",
                "workspaces", "add_workspace", "remove_workspace",
                "write_project"}
# (stop_commands runs on the GUI thread on purpose: only when quitting, bounded by one grace period.)
BLOCKING_MODULES = {"subprocess"}
BLOCKING_BACKEND = {"read_connections"}
WRAPPERS = {"background", "ThreadCall", "thread"}  # widgets.background, widgets.ThreadCall, Task.thread


def call_name(node):
    return node.func.id if isinstance(node.func, ast.Name) else \
        node.func.attr if isinstance(node.func, ast.Attribute) else None


def blocking(node):
    """A description if NODE (a Call) blocks, else None."""
    func = node.func
    if isinstance(func, ast.Attribute):
        if is_sai(func.value) and func.attr in BLOCKING_SAI:
            return f"sai.{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id in BLOCKING_MODULES:
            return f"{func.value.id}.{func.attr}()"
        if isinstance(func.value, ast.Name) and func.value.id == "backend" and func.attr in BLOCKING_BACKEND:
            return f"backend.{func.attr}()"
    if isinstance(func, ast.Name) and func.id in BLOCKING_BACKEND:
        return f"{func.id}()"
    return None


def deferred(node, parents):
    """True if NODE lies in a lambda handed to a wrapper, or in a nested function whose name is."""
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
            handed = any(isinstance(n, ast.Call) and call_name(n) in WRAPPERS and any(
                isinstance(a, ast.Name) and a.id == child.name for a in [*n.args, *(k.value for k in n.keywords)])
                for n in ast.walk(parent))
            if handed:
                return True
        child, parent = parent, parents.get(parent)
    return False


def blocking_calls(source):
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    return [(node.lineno, what) for node in ast.walk(tree) if isinstance(node, ast.Call)
            for what in [blocking(node)] if what and not deferred(node, parents)]


def test_pages_run_commands_and_slow_reads_only_in_the_background():
    found = [f"{name}:{line} {what}"
             for name in ("pages.py", "images_page.py", "ui.py", "board_view.py", "wizard.py", "settings.py")
             if (PACKAGE / name).exists()
             for line, what in blocking_calls((PACKAGE / name).read_text())]
    assert not found, "on the GUI thread (use widgets.background, ThreadCall or Task):\n" + "\n".join(found)


def test_the_blocking_check_tells_direct_calls_from_deferred_ones():
    source = '''
class Page:
    def bad(self):
        status, out, err = self.sai.call("net", "show")
        subprocess.run(["x"])
        records = backend.read_connections(path)

    def good(self):
        background(self.sai.call, "net", "show", done=self.done)
        background(lambda: self.sai.call("mcp", "add", timeout=900), done=self.done)
        def work(cancelled):
            return self.sai.statistics(directory)
        self.task.thread(work, self.done, "text")
        ThreadCall(self.sai.answer, "1-1-1", "allow", "once").start()

    def also_bad(self):
        def helper():
            return self.sai.doctor()
        return helper()
'''
    assert [what for _, what in blocking_calls(source)] == ["sai.call()", "subprocess.run()",
                                                             "backend.read_connections()", "sai.doctor()"]
