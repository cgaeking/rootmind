"""Bearer tokens shared by the REST API and the MCP HTTP server.

The REST API and MCP HTTP use separate tokens in separate files; this module
holds the code they share:

- :func:`resolve_token` reads a token from an environment variable or a file,
  and can generate the file on first use;
- :func:`check_bearer` checks an ``Authorization`` header in constant time;
- :func:`token_fingerprint` gives a short, non-reversible id for logs.

Tokens are never logged or printed. Logs show only where a token came from and
its fingerprint.
"""

import hashlib
import logging
import os
import secrets
from pathlib import Path

from .config import app_home

logger = logging.getLogger(__name__)

REST_TOKEN_ENV = "RLM_API_TOKEN"  # noqa: S105 (an environment variable name)
REST_TOKEN_FILE_NAME = "rest_token.txt"  # noqa: S105 (a file name, not a secret)

MISSING_TOKEN = "Missing bearer token"  # noqa: S105 (a message, not a secret)
INVALID_TOKEN = "Invalid token"  # noqa: S105 (a message, not a secret)


class TokenError(RuntimeError):
    """The bearer token is missing, empty or unreadable; the server must not start."""


def rest_token_path() -> Path:
    """Default location of the REST API token: ``<RLM_HOME or cwd>/data/rest_token.txt``."""
    return app_home() / "data" / REST_TOKEN_FILE_NAME


def token_fingerprint(token: str) -> str:
    """Short, non-reversible identifier for logs (first 8 hex chars of SHA-256)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def write_private_file(path: Path, content: str) -> None:
    """Create ``path`` with owner-only permissions (0600 on POSIX).

    Fails if the file already exists (``O_EXCL``). On Windows the mode bits are
    ignored and the file inherits the ACL of its folder; the background service
    creates that folder with a protected ACL.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


def resolve_token(env_var: str, path: Path, *, generate: bool) -> tuple[str, str]:
    """Load a bearer token from ``env_var`` or ``path``, or create it.

    Priority: the environment variable, then the file, then (with
    ``generate=True``) a new random token written to the file.

    Returns:
        ``(token, source)``. ``source`` says where the token came from and is
        safe to log: it never contains the token.

    Raises:
        TokenError: the variable or the file is present but empty, the file
            cannot be read, or there is no token and ``generate`` is False.
    """
    env_token = os.environ.get(env_var)
    if env_token is not None:
        token = env_token.strip()
        if not token:
            raise TokenError(
                f"{env_var} is set but empty; refusing to start without authentication"
            )
        return token, f"environment variable {env_var}"

    if path.exists():
        try:
            token = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise TokenError(f"Cannot read token file {path}: {e}") from e
        if not token:
            raise TokenError(
                f"Token file {path} is empty; delete it to generate a new token, or set {env_var}"
            )
        return token, f"file {path}"

    if not generate:
        raise TokenError(f"No token: set {env_var} or create {path}")

    token = secrets.token_urlsafe(32)
    write_private_file(path, token)
    logger.info("Generated a new token and saved it to %s", path)
    return token, f"file {path} (newly generated)"


def check_bearer(header: str | None, expected: bytes) -> str | None:
    """Check an ``Authorization`` header value against the expected token.

    The scheme is case-insensitive. The comparison runs in constant time
    (``secrets.compare_digest``, called through the module so tests can spy on it).

    Returns:
        None when the header carries the token, otherwise the reason for a 401:
        ``"Missing bearer token"`` or ``"Invalid token"``.
    """
    if not header:
        return MISSING_TOKEN
    scheme, _, credentials = header.strip().partition(" ")
    credentials = credentials.strip()
    if scheme.lower() != "bearer" or not credentials:
        return MISSING_TOKEN
    if not secrets.compare_digest(credentials.encode("utf-8"), expected):
        return INVALID_TOKEN
    return None
