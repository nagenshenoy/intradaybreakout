import os
import tempfile

# `app.py` builds a default engine at import time; keep it away from the real
# data/session.json so running the tests never reads or overwrites a saved session.
os.environ.setdefault("SCANNER_SESSION_FILE", os.path.join(tempfile.mkdtemp(prefix="scanner-tests-"), "session.json"))
