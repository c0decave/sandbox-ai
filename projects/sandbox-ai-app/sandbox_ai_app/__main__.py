import os
import sys

# Never import from the current directory: the app is often started from a workspace, which the agent
# writes (a planted PySide6.py or sandbox_ai_app/ there would run with your rights).
sys.path[:] = [entry for entry in sys.path if entry not in ("", ".") and os.path.abspath(entry) != os.getcwd()]

from .main import main  # noqa: E402

sys.exit(main())
