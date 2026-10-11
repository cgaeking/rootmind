"""Standalone entry point for the packaged RootMind backend (REST API).

PyInstaller bundles this into a single executable (see ``rootmind-backend.spec``).
With no arguments it serves the REST API on ``127.0.0.1:8000``. ``RLM_HOME``
decides where ``config.yaml`` and the SQLite database live; ``RLM_HOST`` /
``RLM_PORT`` override the bind address.
"""

import multiprocessing
import os
import sys

from rootmind.main import main


def run() -> None:
    """Run the API server, defaulting to 127.0.0.1:8000 when no args are given."""
    if len(sys.argv) == 1:
        host = os.environ.get("RLM_HOST", "127.0.0.1")
        port = os.environ.get("RLM_PORT", "8000")
        sys.argv += ["api", "--host", host, "--port", port]
    main()


if __name__ == "__main__":
    # Required so a frozen exe does not re-spawn the whole app in child processes.
    multiprocessing.freeze_support()
    run()
