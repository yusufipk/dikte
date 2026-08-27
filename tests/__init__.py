"""Test-wide safety net, applied before anything under test is imported.

Every module resolves its paths at import time from the XDG variables, so those
are redirected here: a test that forgets to ask for a throwaway directory writes
into a temporary one instead of into the real ~/.config/dikte. The Qt platform
is pinned for the same reason, so that a machine with no display and a CI runner
behave the way a desktop does.
"""

import atexit
import os
import shutil
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_SANDBOX = tempfile.mkdtemp(prefix="dikte-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_SANDBOX, "data")
os.environ["XDG_CACHE_HOME"] = os.path.join(_SANDBOX, "cache")
# Home goes with them: the shortcut file, the applications directory and every
# macOS path start from it rather than from an XDG variable, and a test run is
# not allowed to touch the real one.
os.environ["HOME"] = os.path.join(_SANDBOX, "home")
os.makedirs(os.environ["HOME"], exist_ok=True)
atexit.register(shutil.rmtree, _SANDBOX, True)

# A key sitting in the environment would otherwise reach the code that falls
# back to it, and the tests for "there is no key" would pass only on a machine
# without one.
for _var in ("OPENAI_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY"):
    os.environ.pop(_var, None)

# The interface language leaks through module-level state, so the tests fix it
# rather than inherit whatever the developer's locale says.
for _var in ("LC_ALL", "LC_MESSAGES", "LANG"):
    os.environ.pop(_var, None)
