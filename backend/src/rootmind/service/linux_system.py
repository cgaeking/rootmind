"""The real Linux environment of the ``service`` commands.

:class:`LinuxSystem` bundles everything :mod:`.linux` needs from the machine:
the account, the XDG folders, ``systemctl --user``, the data and program
folders, port tests and local HTTP checks. Tests pass a fake with the same
methods instead. Nothing here imports a Windows module.
"""

from __future__ import annotations

import os
import posixpath
import sys
import time
from pathlib import Path
from typing import Any

from .. import __version__
from . import LINUX_EXE_NAME, linux_layout, ports, procnet, systemd
from .common import http_request


class LinuxSystem:
    """The machine, as seen by ``service install``, ``uninstall`` and ``status`` on Linux."""

    def __init__(self) -> None:
        self.version = __version__
        self.executable = sys.executable
        self.frozen = bool(getattr(sys, "frozen", False))

    # --- process -------------------------------------------------------------------

    def is_linux(self) -> bool:
        return sys.platform.startswith("linux")

    def is_windows(self) -> bool:
        return False

    def is_release_build(self) -> bool:
        """The frozen onedir build: ``rootmind-backend`` next to ``_internal/``."""
        if not self.frozen:
            return False
        exe = Path(self.executable)
        return exe.name == LINUX_EXE_NAME and (exe.parent / "_internal").is_dir()

    def uid(self) -> int:
        return os.geteuid()  # type: ignore[attr-defined]

    def linger_enabled(self) -> bool | None:
        """Whether ``loginctl enable-linger`` is on for this account; None if unknown."""
        try:
            import pwd

            name = pwd.getpwuid(self.uid()).pw_name  # type: ignore[attr-defined]
        except (ImportError, KeyError):
            return None
        return os.path.exists(f"/var/lib/systemd/linger/{name}")

    # --- folders -------------------------------------------------------------------

    def data_root(self) -> Path:
        return linux_layout.data_root()

    def unit_dir(self) -> Path:
        return linux_layout.unit_dir()

    def prepare_data(self, root: Path) -> bool:
        return linux_layout.prepare_data_tree(root, self.uid())

    def normalize_data(self, root: Path) -> None:
        linux_layout.normalize_data_tree(root)

    def data_problems(self, root: Path) -> list[str]:
        return linux_layout.validate_data_tree(root, self.uid())

    def remove_data(self, root: Path) -> None:
        linux_layout.remove_tree(root)

    def program_source_problems(self, folder: str) -> list[str]:
        return linux_layout.program_source_problems(Path(folder), self.uid())

    def install_program(self, source_dir: str, version: str) -> str:
        base = linux_layout.runtime_base(self.data_root())
        return str(linux_layout.install_program(Path(source_dir), base, version))

    def remove_old_programs(self, keep: str) -> list[str]:
        base = linux_layout.runtime_base(self.data_root())
        return linux_layout.remove_version_folders(base, keep=Path(keep))

    def remove_all_programs(self, root: Path) -> list[str]:
        return linux_layout.remove_program_base(linux_layout.runtime_base(root))

    # --- units, ports, HTTP --------------------------------------------------------

    def systemctl(self) -> systemd.Systemctl:
        return systemd.Systemctl()

    def wait_process_exit(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while os.path.exists(f"/proc/{pid}"):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)
        return True

    def can_bind(self, port: int) -> bool:
        return ports.can_bind(port)

    def port_owned_by(self, port: int, pid: int) -> bool:
        return procnet.port_owner_is(port, pid)

    def http(self, method: str, url: str, *, token: str | None = None, body: Any = None):
        return http_request(method, url, token=token, body=body)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def monotonic(self) -> float:
        return time.monotonic()

    @staticmethod
    def same_path(a: str, b: str) -> bool:
        return posixpath.normpath(a) == posixpath.normpath(b)
