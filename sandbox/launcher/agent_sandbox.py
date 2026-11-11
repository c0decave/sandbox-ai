"""The agents' launcher as a process of its own: `python3 -I .../sandbox/launcher/agent_sandbox.py AGENT ...` runs
sandbox/agent_sandbox.py as __main__ in this process, as `python3 -I sandbox/agent_sandbox.py AGENT ...` does - with
its arguments, exit status and signals - but from its cached bytecode (__pycache__): a script Python runs itself is
compiled anew every time, about 55 ms of every start. Its command line names agent_sandbox.py, as before: net, the app
and the sessions recognise a running start by that name in /proc."""
import importlib.util
import os
import sys

LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "agent_sandbox.py")
spec = importlib.util.spec_from_file_location("__main__", LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
sys.argv[0] = LAUNCHER  # what the launcher sees is what it saw as a script of its own
sys.modules["__main__"] = launcher
spec.loader.exec_module(launcher)
