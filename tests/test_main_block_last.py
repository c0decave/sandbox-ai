"""A script's `if __name__ == "__main__":` block is its last statement: code after it is not defined yet when main()
runs (2026-11-23: a merge put live_gaps.py's main block before model_found, and the user's live check ended in
"NameError: name 'model_found' is not defined" - the tests import the module, so they never saw it)."""
import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted(p for pattern in ("scripts/*.py", "sandbox/*.py", "projects/*/scripts/*.py", "projects/*/*/*.py")
                 for p in ROOT.glob(pattern) if "/tests/" not in p.as_posix())


def main_block(node):
    test = node.test if isinstance(node, ast.If) else None
    return (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and any(isinstance(c, ast.Constant) and c.value == "__main__" for c in test.comparators))


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_the_main_block_is_the_last_statement(path):
    body = ast.parse(path.read_text(encoding="utf-8")).body
    blocks = [i for i, node in enumerate(body) if main_block(node)]
    if not blocks:
        return
    after = [getattr(node, "name", type(node).__name__) for node in body[blocks[-1] + 1:]]
    assert after == [], f"{path.name}: defined after its __main__ block, so not there when main() runs: {after}"
