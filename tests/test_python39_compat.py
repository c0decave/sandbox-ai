"""Python 3.9 compatibility of everything sandbox-ai runs with the host's Python (pyproject: requires-python >= 3.9;
macOS ships /usr/bin/python3 3.9 with its command line tools): the launcher and every module it loads, runtime.py
(whose clients run on the host too), the scripts and install.sh's embedded Python.

A syntax check alone misses what only fails when it runs on 3.9: an annotation `X | None` evaluated at definition
time, zip(strict=), dataclass(slots=), 3.10+ functions of the standard library. These are found in the syntax tree
here; with a Python 3.9 at hand (python3.9 on PATH, or SANDBOX_AI_PY39=/path/to/python3.9) each file is also compiled
by it."""
import ast
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The container images' own Pythons (3.11 and newer) run these alone, never the host's.
CONTAINER_ONLY = {"sandbox/hermes_guard.py", "sandbox/openhands_cli.py"}


def host_files():
    found = sorted(p for p in (ROOT / "sandbox").rglob("*.py") if "node_modules" not in p.parts)
    found += sorted((ROOT / "scripts").glob("*.py"))
    return [p for p in found if str(p.relative_to(ROOT)) not in CONTAINER_ONLY]


def install_blocks():
    """install.sh's embedded Python: each `python3 ... <<'PY'` block (name, source)."""
    text = (ROOT / "install.sh").read_text()
    return [(f"install.sh:{text[:m.start()].count(chr(10)) + 2}", m.group(1))
            for m in re.finditer(r"<<'PY'\n(.*?)\nPY\n", text, re.S)]


def sources():
    return [(str(p.relative_to(ROOT)), p.read_text(encoding="utf-8")) for p in host_files()] + install_blocks()


# module.attribute of 3.10 and later (the module as it is imported: import itertools, from datetime import ...)
NEWER = {("itertools", "pairwise"), ("itertools", "batched"), ("datetime", "UTC"), ("asyncio", "TaskGroup"),
         ("asyncio", "timeout"), ("contextlib", "chdir"), ("hashlib", "file_digest"), ("enum", "StrEnum"),
         ("typing", "Self"), ("typing", "TypeAlias"), ("typing", "ParamSpec"), ("typing", "TypeGuard"),
         ("typing", "Concatenate"), ("typing", "NotRequired"), ("typing", "LiteralString"), ("typing", "Never"),
         ("typing", "assert_never"), ("typing", "reveal_type"), ("os", "process_cpu_count"), ("sys", "exception"),
         ("inspect", "get_annotations")}
NEWER_KEYWORDS = {"zip": {"strict"}, "dataclass": {"slots", "kw_only", "match_args"}, "dataclasses.dataclass":
                  {"slots", "kw_only", "match_args"}, "bisect.bisect": {"key"}, "bisect.bisect_left": {"key"},
                  "bisect.bisect_right": {"key"}, "bisect.insort": {"key"}, "shutil.rmtree": {"onexc"},
                  "tempfile.NamedTemporaryFile": {"delete_on_close"}, "glob.glob": {"root_dir", "dir_fd"}}
NEWER_BUILTINS = {"ExceptionGroup", "BaseExceptionGroup", "aiter", "anext", "EncodingWarning"}


def guarded(tree):
    """Names checked with hasattr/getattr somewhere in TREE (a guarded use of a newer API is fine)."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("hasattr", "getattr") \
                and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            names.add(node.args[1].value)
    return names


def problems_of(name, source):
    tree = ast.parse(source, name, feature_version=(3, 9))
    future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and
                 any(a.name == "annotations" for a in n.names) for n in tree.body)
    safe = guarded(tree)
    found = []

    def union(annotation):
        return annotation is not None and any(isinstance(s, ast.BinOp) and isinstance(s.op, ast.BitOr)
                                              for s in ast.walk(annotation))
    in_try = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError", "Exception")
                for h in node.handlers):
            in_try.update(id(n) for n in ast.walk(node))
    for node in ast.walk(tree):
        where = f"{name}:{getattr(node, 'lineno', '?')}"
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not future:
            args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs + \
                [a for a in (node.args.vararg, node.args.kwarg) if a]
            if any(union(a.annotation) for a in args) or union(node.returns):
                found.append(f"{where}: an annotation X | Y of {node.name} is evaluated on 3.9")
        elif isinstance(node, ast.AnnAssign) and not future and union(node.annotation):
            found.append(f"{where}: an annotation X | Y is evaluated on 3.9")
        elif isinstance(node, ast.Call):
            func = ast.unparse(node.func)
            for keyword in node.keywords:
                if keyword.arg in NEWER_KEYWORDS.get(func, ()):
                    found.append(f"{where}: {func}({keyword.arg}=) is Python 3.10+")
                if keyword.arg == "encoding" and isinstance(keyword.value, ast.Constant) and \
                        keyword.value.value == "locale":
                    found.append(f"{where}: encoding=\"locale\" is Python 3.10+")
            if func in ("isinstance", "issubclass") and len(node.args) == 2 and isinstance(node.args[1], ast.BinOp):
                found.append(f"{where}: {func}(x, A | B) is Python 3.10+")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and \
                (node.value.id, node.attr) in NEWER and node.attr not in safe:
            found.append(f"{where}: {node.value.id}.{node.attr} is Python 3.10+")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys" and \
                node.attr == "orig_argv" and "orig_argv" not in safe:
            found.append(f"{where}: sys.orig_argv is Python 3.10+ (check hasattr first)")
        elif isinstance(node, ast.ImportFrom) and node.module and any(
                (node.module, a.name) in NEWER for a in node.names) and id(node) not in in_try:
            found.append(f"{where}: from {node.module} import ... of Python 3.10+")
        elif isinstance(node, (ast.Import, ast.ImportFrom)) and id(node) not in in_try and (
                any(a.name == "tomllib" for a in node.names) if isinstance(node, ast.Import)
                else node.module == "tomllib"):
            found.append(f"{where}: tomllib is Python 3.11+ (import it in a try, with a fallback)")
        elif isinstance(node, ast.Name) and node.id in NEWER_BUILTINS and node.id not in safe:
            found.append(f"{where}: {node.id} is Python 3.10+")
    return found


def test_the_files_are_found():
    names = [name for name, _ in sources()]
    assert "sandbox/agent_sandbox.py" in names and "sandbox/runtime.py" in names and "scripts/make_pyz.py" in names
    assert sum(name.startswith("install.sh:") for name in names) >= 2, "install.sh's embedded Python"


def test_nothing_of_python_3_10_or_later_on_the_host():
    found = [problem for name, source in sources() for problem in problems_of(name, source)]
    assert not found, "\n".join(found)


def test_the_tests_collect_on_python_3_9_too():
    """CI runs the tests on 3.9 and 3.10 as well: a test file using 3.11's tomllib without its tomli fallback was
    an ERROR collecting there (0.1.186: test_plugins_codex_opencode2.py)."""
    tests = sorted(Path(__file__).resolve().parent.glob("*.py"))
    found = [problem for path in tests for problem in problems_of(path.name, path.read_text(encoding="utf-8"))]
    assert not found, "\n".join(found)


@pytest.mark.parametrize("source, problem", [
    ("def f(x: int | None): pass", "evaluated on 3.9"),
    ("x: int | None = 1", "evaluated on 3.9"),
    ("list(zip(a, b, strict=True))", "zip(strict=)"),
    ("import itertools\nitertools.pairwise(x)", "itertools.pairwise"),
    ("import tomllib", "tomllib"),
    ("isinstance(x, int | str)", "isinstance(x, A | B)"),
    ("import sys\nsys.orig_argv", "sys.orig_argv"),
    ("from dataclasses import dataclass\n@dataclass(slots=True)\nclass A: pass", "dataclass(slots=)"),
])
def test_the_check_finds_each_kind(source, problem):
    assert any(problem in line for line in problems_of("x.py", source)), problems_of("x.py", source)


@pytest.mark.parametrize("source", [
    "from __future__ import annotations\ndef f(x: int | None): pass",
    "try:\n    import tomllib\nexcept ImportError:\n    tomllib = None",
    "import sys\nargv = sys.orig_argv if hasattr(sys, 'orig_argv') else sys.argv",
    "zip(a, b)",
])
def test_what_runs_on_3_9_passes(source):
    assert problems_of("x.py", source) == []


def python39():
    for candidate in (os.environ.get("SANDBOX_AI_PY39"), shutil.which("python3.9")):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


@pytest.mark.skipif(python39() is None, reason="no Python 3.9 here (python3.9 on PATH or SANDBOX_AI_PY39)")
def test_python_3_9_compiles_every_file(tmp_path):
    listing = tmp_path / "files.json"
    import json
    listing.write_text(json.dumps(sources()))
    check = ("import json, sys\nbad = []\nfor name, source in json.load(open(sys.argv[1])):\n    try:\n"
             "        compile(source, name, 'exec')\n    except SyntaxError as exc:\n"
             "        bad.append(f'{name}: {exc}')\nprint('\\n'.join(bad))\nsys.exit(1 if bad else 0)\n")
    done = subprocess.run([python39(), "-I", "-c", check, str(listing)], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout + done.stderr
