"""The real Windows environment of the ``service`` commands.

:class:`WindowsSystem` bundles everything :mod:`.install` needs from the
machine: the process token, known folders, the Service Control Manager, the
data and program folders, port tests and local HTTP checks. Tests pass a fake
with the same methods instead.
"""

from __future__ import annotations

import json
import ntpath
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__
from . import APP_UNINSTALL_KEY, EXE_NAME, PROGRAM_DIR_NAME, layout, listeners, ports, scm

HTTP_TIMEOUT_SECONDS = 5


def http_request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: Any = None,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> tuple[int, Any]:
    """Send one request to a local service; returns ``(status, JSON body or None)``.

    Status 0 means no answer (refused, reset or timed out). Proxy settings
    from the environment are ignored: the target is always 127.0.0.1.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 (loopback URL built by the caller)
    try:
        with opener.open(request, timeout=timeout) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as e:
        status, payload = e.code, e.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return 0, None
    try:
        return status, json.loads(payload) if payload else None
    except ValueError:
        return status, None


class WindowsSystem:
    """The machine, as seen by ``service install``, ``uninstall`` and ``status``."""

    def __init__(self) -> None:
        self.version = __version__
        self.executable = sys.executable
        self.frozen = bool(getattr(sys, "frozen", False))

    # --- process -------------------------------------------------------------------

    def is_windows(self) -> bool:
        return sys.platform == "win32"

    def is_release_build(self) -> bool:
        """The frozen onedir build: ``rootmind-backend.exe`` next to ``_internal\\``."""
        if not self.frozen:
            return False
        exe = Path(self.executable)
        return exe.name.lower() == EXE_NAME and (exe.parent / "_internal").is_dir()

    def is_elevated(self) -> bool:
        """Whether this process holds an enabled Administrators group (elevated)."""
        import win32security

        admins = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
        return bool(win32security.CheckTokenMembership(None, admins))

    def make_administrators_default_owner(self) -> None:
        """Make Administrators the owner of everything this process creates from now on.

        Allowed for an elevated token, where Administrators may be an owner.
        Without it, an administrator with UAC turned off would own the new
        files personally, and the folder checks would refuse them.

        Raises:
            OSError: the token cannot be changed.
        """
        import pywintypes
        import win32api
        import win32security

        try:
            token = win32security.OpenProcessToken(
                win32api.GetCurrentProcess(),
                win32security.TOKEN_ADJUST_DEFAULT | win32security.TOKEN_QUERY,
            )
            try:
                admins = win32security.CreateWellKnownSid(
                    win32security.WinBuiltinAdministratorsSid, None
                )
                win32security.SetTokenInformation(token, win32security.TokenOwner, admins)
            finally:
                token.Close()
        except pywintypes.error as e:
            raise OSError(None, e.strerror, None, e.winerror) from None

    def process_user_sid(self) -> str:
        from ..winaccess import current_user_sid

        return current_user_sid()

    def check_access(self, user_sid: str) -> None:
        """Build (and drop) the folder access check for ``user_sid``.

        Raises:
            AccessContextError: it cannot be built.
        """
        from ..winaccess import WindowsUserAccess

        WindowsUserAccess(user_sid).close()

    def uac_filters_this_account(self) -> bool:
        """Whether this account also runs unelevated (UAC gives it a split token).

        Without a split token (UAC off, or the built-in Administrator) every
        process of the account is an administrator anyway.
        """
        import win32api
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        try:
            elevation = win32security.GetTokenInformation(token, win32security.TokenElevationType)
        finally:
            token.Close()
        return elevation != 1  # TokenElevationTypeDefault

    def program_source_problems(self, folder: str, approver_sid: str) -> list[str]:
        """Why the program in ``folder`` must not be installed as the service (see layout).

        Files an administrator without a split token creates are owned by the
        account itself, which is then as good as Administrators. With UAC on,
        the account's unelevated programs could change such files, so only the
        administrator SIDs count.
        """
        allowed = layout.ADMIN_SIDS
        try:
            if not self.uac_filters_this_account():
                allowed = allowed | {approver_sid}
        except Exception:  # pywintypes.error: keep the strict rule
            allowed = layout.ADMIN_SIDS
        return layout.program_source_problems(Path(folder), owners=allowed, writers=allowed)

    def app_installed(self) -> bool:
        """Whether the RootMind app is installed: its uninstall entry in HKLM (either view).

        A RootMind 1.x installation for the current account (HKCU) counts too.
        """
        import winreg

        places = (
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
            (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
            (winreg.HKEY_CURRENT_USER, 0),
        )
        for hive, view in places:
            try:
                with winreg.OpenKey(hive, APP_UNINSTALL_KEY, 0, winreg.KEY_READ | view):
                    return True
            except OSError:
                continue
        return False

    # --- folders -------------------------------------------------------------------

    def program_files(self) -> str:
        return str(layout.program_files())

    def program_base(self) -> Path:
        return layout.program_files() / PROGRAM_DIR_NAME

    def data_root(self) -> Path:
        return layout.data_root()

    def prepare_data(self, root: Path, user_sid: str, mcp_enabled: bool) -> bool:
        mcp = layout.mcp_sid()
        return layout.prepare_data_tree(
            root, user_sid, mcp if mcp_enabled else None, mcp_account=mcp
        )

    def normalize_data(self, root: Path, user_sid: str, mcp_enabled: bool) -> None:
        layout.normalize_data_tree(root, user_sid, layout.mcp_sid() if mcp_enabled else None)

    def data_problems(self, root: Path) -> list[str]:
        return layout.validate_data_tree(root, mcp=layout.mcp_sid())

    def remove_data(self, root: Path) -> None:
        layout.remove_data_tree(root)

    def install_program(self, source_dir: str, version: str) -> str:
        return str(layout.install_program(Path(source_dir), self.program_base(), version))

    def remove_old_programs(self, keep: str) -> list[str]:
        return layout.remove_version_folders(self.program_base(), keep=Path(keep))

    def remove_all_programs(self) -> list[str]:
        return layout.remove_program_base(self.program_base())

    # --- services, ports, HTTP -----------------------------------------------------

    def scm(self, *, write: bool) -> scm.Scm:
        return scm.Scm(write=write)

    def wait_process_exit(self, pid: int, timeout: float) -> bool:
        return scm.wait_for_process_exit(pid, timeout)

    def can_bind(self, port: int) -> bool:
        return ports.can_bind(port)

    def port_owned_by(self, port: int, pid: int) -> bool:
        return listeners.port_owner_is(port, pid)

    def http(self, method: str, url: str, *, token: str | None = None, body: Any = None):
        return http_request(method, url, token=token, body=body)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

    @staticmethod
    def same_path(a: str, b: str) -> bool:
        return ntpath.normcase(ntpath.normpath(a)) == ntpath.normcase(ntpath.normpath(b))
