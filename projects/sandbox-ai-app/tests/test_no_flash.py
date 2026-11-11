"""No widget is shown before it has a parent: a widget without a parent made visible is a window of its own for a
moment (a flash; on a tiling window manager every other window is laid out anew around it - the app flickered at its
start and with every notification). A static check over the app's code: in one function, a widget made there and
shown (setVisible with anything but False, show(), setRowVisible) before the line that puts it into a layout."""
import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "sandbox_ai_app"
ADD = {"addWidget", "addRow", "insertWidget", "setWidget", "addTab", "insertRow", "setCellWidget", "setCornerWidget",
       "addPermanentWidget"}
SHOW = {"setVisible", "show", "setRowVisible"}


def name_of(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return "self." + node.attr
    return None


def hidden(call):
    return call.func.attr == "setVisible" and call.args and isinstance(call.args[0], ast.Constant) \
        and call.args[0].value is False


def shown_early(source):
    found = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef):
            continue
        made, shown, added = {}, {}, {}
        for node in ast.walk(function):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call = node.value
                parentless = not any(k.arg == "parent" for k in call.keywords) and (
                    not call.args or (len(call.args) == 1 and isinstance(call.args[0], (ast.Constant, ast.JoinedStr,
                                                                                         ast.Call))))
                for target in node.targets:
                    name = name_of(target)
                    if name and parentless:
                        made.setdefault(name, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in SHOW and not hidden(node):
                    name = name_of(node.args[0]) if node.func.attr == "setRowVisible" and node.args \
                        else name_of(node.func.value)
                    if name:
                        shown.setdefault(name, node.lineno)
                if node.func.attr in ADD:
                    for argument in node.args:
                        name = name_of(argument)
                        if name:
                            added[name] = max(added.get(name, 0), node.lineno)
        found += [f"{function.name}: {name} shown at line {line}, put in its place at {added[name]}"
                  for name, line in shown.items() if name in made and name in added and made[name] < line < added[name]]
    return found


def test_the_check_finds_a_widget_shown_before_it_has_a_parent():
    assert shown_early("def f(self):\n    x = QLabel('a')\n    x.setVisible(True)\n    layout.addWidget(x)\n")
    assert not shown_early("def f(self):\n    x = QLabel('a')\n    x.setVisible(False)\n    layout.addWidget(x)\n")
    assert not shown_early("def f(self):\n    x = QLabel('a')\n    layout.addWidget(x)\n    x.setVisible(True)\n")


def test_no_widget_of_the_app_is_shown_before_it_has_a_parent():
    problems = [f"{path.name}: {problem}" for path in sorted(APP.glob("*.py"))
                for problem in shown_early(path.read_text())]
    assert not problems, "\n".join(problems)
