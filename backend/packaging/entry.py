"""Entry point of the packaged RootMind backend (``rootmind-backend.exe``).

PyInstaller builds this into a onedir folder (see ``rootmind-backend.spec``).
The executable runs the same command line as ``rootmind`` (``api``, ``index``,
``mcp``, ``mcp-http``, ``service``, ...). Without arguments it runs ``api``,
which binds as configured: ``--host``/``--port``, then ``RLM_HOST``/``RLM_PORT``,
then ``api.host``/``api.port`` in ``config.yaml``, then 127.0.0.1:8000.
``RLM_HOME`` decides where ``config.yaml`` and the data live.
"""

import multiprocessing
import sys

from rootmind.main import main


def run() -> None:
    """Run the command line; no arguments means ``api``."""
    if len(sys.argv) == 1:
        sys.argv.append("api")
    main()


if __name__ == "__main__":
    # Required so a frozen exe does not re-spawn the whole app in child processes.
    multiprocessing.freeze_support()
    run()
