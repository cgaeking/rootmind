"""Background service for the RootMind backend (Windows services, systemd user units).

``rootmind-backend.exe service install | uninstall | status`` manages two
Windows services, hosted by the same executable (``service run <role>``):

- ``RootMind-Backend`` runs the REST API and the indexer as LocalSystem. Its
  reads are confined to what the installing account can read without
  elevation (:class:`rootmind.winaccess.WindowsUserAccess`).
- ``RootMind-MCP-HTTP`` serves MCP over HTTP as the virtual account
  ``NT SERVICE\\RootMind-MCP-HTTP``. It only reads the index. It is on for a new
  install; ``--no-mcp-http`` turns it off.

Since 2.0 the service is RootMind's only backend: the per-machine installer
runs ``service install`` for the account that installs the app, and the
uninstaller runs ``service uninstall``. The program is copied to
``%ProgramFiles%\\RootMind Service\\<version>-<id>\\`` (why: :mod:`.install`)
and the data lives in ``%ProgramData%\\RootMind``. Modules:

- :mod:`.cli`: the ``service`` command line;
- :mod:`.install`: install, update, uninstall and status;
- :mod:`.host`: the service host (``service run``);
- :mod:`.imagepath`, :mod:`.ports`: the ImagePath contract and port choice;
- :mod:`.scm`, :mod:`.layout`, :mod:`.system`: the Windows adapters (Service
  Control Manager, folders and ACLs, process token);
- :mod:`.common`: helpers both platforms share.

On Linux the same commands manage two systemd **user** units of the account
that runs them (``rootmind-backend.service`` and ``rootmind-mcp-http.service``
under ``~/.config/systemd/user/``), without root: :mod:`.linux` (install,
uninstall, status), :mod:`.systemd` (unit files and ``systemctl --user``),
:mod:`.linux_layout` (the data folder ``~/.local/share/rootmind`` with its
modes, and the program copy in its ``runtime/``) and :mod:`.procnet` (who
listens on a port). The Windows modules are never imported there.
"""

import re
from enum import IntEnum

BACKEND_SERVICE = "RootMind-Backend"
MCP_SERVICE = "RootMind-MCP-HTTP"

ROLE_BACKEND = "backend"
ROLE_MCP = "mcp-http"
SERVICE_BY_ROLE = {ROLE_BACKEND: BACKEND_SERVICE, ROLE_MCP: MCP_SERVICE}

DISPLAY_NAMES = {
    BACKEND_SERVICE: "RootMind Backend",
    MCP_SERVICE: "RootMind MCP over HTTP",
}
DESCRIPTIONS = {
    BACKEND_SERVICE: (
        "RootMind REST API and document indexing for the RootMind app. Reads only folders "
        "the installing account can open."
    ),
    MCP_SERVICE: "RootMind MCP server over HTTP (bearer token) for remote agents such as n8n.",
}

# %ProgramFiles%\RootMind Service\<version>-<8 hex>\rootmind-backend.exe
PROGRAM_DIR_NAME = "RootMind Service"
EXE_NAME = "rootmind-backend.exe"
# %ProgramData%\RootMind
DATA_DIR_NAME = "RootMind"
# database.path of the service config (inside data\db\, where the MCP account may write).
SERVICE_DATABASE_PATH = "./data/db/index.db"

DEFAULT_API_PORT = 8000
DEFAULT_MCP_PORT = 3000
# Fallbacks for "--port auto", outside the dynamic port range.
API_FALLBACK_PORTS = range(18300, 18400)
MCP_FALLBACK_PORTS = range(18400, 18500)

# Linux: systemd user units, the data folder and the program copy.
UNIT_BY_ROLE = {ROLE_BACKEND: "rootmind-backend.service", ROLE_MCP: "rootmind-mcp-http.service"}
LINUX_EXE_NAME = "rootmind-backend"
# $XDG_DATA_HOME/rootmind (default ~/.local/share/rootmind)
LINUX_DATA_DIR_NAME = "rootmind"
# <data>/runtime/<version>-<8 hex>/rootmind-backend
RUNTIME_DIR_NAME = "runtime"

RELEASES_URL = "https://github.com/cgaeking/rootmind/releases"
RELEASE_ZIP_URL = RELEASES_URL + "/download/v{tag}/rootmind-backend-windows-x64.zip"
RELEASE_LINUX_URL = RELEASES_URL + "/download/v{tag}/rootmind-backend-linux-x64.tar.gz"
_RELEASE_VERSION = re.compile(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?")
_PRERELEASE_LABELS = {"a": "alpha", "b": "beta", "rc": "rc"}


def release_zip_url(version: str, template: str = RELEASE_ZIP_URL) -> str:
    """Download address of the release build for ``version`` (PEP 440 form).

    Release tags spell prereleases the npm way (``1.2.0rc1`` is tag
    ``v1.2.0-rc.1``). A version that is not a release, such as a development
    build, gets the releases page instead.
    """
    match = _RELEASE_VERSION.fullmatch(version)
    if match is None:
        return RELEASES_URL
    tag = match[1]
    if match[2]:
        tag += f"-{_PRERELEASE_LABELS[match[2]]}.{match[3]}"
    return template.format(tag=tag)


def release_linux_url(version: str) -> str:
    """Download address of the Linux release build for ``version`` (see :func:`release_zip_url`)."""
    return release_zip_url(version, RELEASE_LINUX_URL)


# The app's uninstall entry (NSIS). Since 2.0 the app is installed per machine
# (HKLM); RootMind 1.x installed it per user (HKCU).
APP_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\RootMind"

# Service-specific exit code of a clean stop on a config, token or access
# error. The failure actions do not restart the service after it.
SERVICE_SPECIFIC_CONFIG_ERROR = 2


class ExitCode(IntEnum):
    """Exit codes of ``service install``, ``uninstall`` and ``status``."""

    OK = 0
    UNEXPECTED = 1
    USAGE = 2  # wrong usage, not Windows or Linux, or not the release build
    FOREIGN_SERVICE = 3  # a service with our name exists that we did not install
    PORT_BUSY = 4
    NOT_ELEVATED = 5
    DATA_FOLDER_REFUSED = 6
    HEALTH_CHECK_FAILED = 7
    ACCOUNT_MISMATCH = 8  # the approving account is not the app's, or the service is another's
    ACCESS_CONTEXT = 9  # the access check for the account cannot be built
    # The program folder this command would copy from (the folder of the
    # running rootmind-backend.exe, _internal included) can be changed by
    # accounts other than administrators.
    UNSAFE_PROGRAM_FOLDER = 10
    # Linux: ``systemctl --user`` cannot reach the account's service manager
    # (no systemd, or no user session, as under ``su`` or ``sudo``).
    NO_USER_MANAGER = 11


class ServiceCommandError(Exception):
    """A ``service`` command stops with ``code`` and a user-facing ``message``."""

    def __init__(self, code: ExitCode, message: str) -> None:
        super().__init__(message)
        self.code = ExitCode(code)
        self.message = message
