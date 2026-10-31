"""Bearer tokens shared by the REST API and the MCP HTTP server.

The REST API and MCP HTTP use separate tokens in separate files; this module
holds the code they share:

- :func:`resolve_token` reads a token from an environment variable or a file,
  and can generate the file on first use;
- :func:`check_bearer` checks an ``Authorization`` header in constant time;
- :func:`token_fingerprint` gives a short, non-reversible id for logs;
- :func:`token_proof` and :func:`proof_matches` let a server prove that it
  knows a token before a client sends it (``GET /proof``).

Tokens are never logged or printed. Logs show only where a token came from and
its fingerprint.
"""

import hashlib
import hmac
import logging
import os
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

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


# --- proof of the token -----------------------------------------------------------
#
# Before a client sends a token to 127.0.0.1:<port>, it asks the server there to
# prove that it knows the token: ``GET /proof?nonce=<hex>`` answers
# ``{"proof": HMAC-SHA256(token, PROOF_LABEL + nonce)}`` without a token. A
# program that took the port does not know the token, so it cannot answer, and
# the token never reaches it. This works where the TCP listener table does not
# show the service's socket (seen on Windows) and is checked as well where it does.

PROOF_PATH = "/proof"
PROOF_LABEL = b"rootmind-token-proof\x00"
_NONCE_HEX = frozenset("0123456789abcdef")


def valid_nonce(nonce: object) -> bool:
    """A nonce is 32 to 128 lowercase hex characters (16 to 64 random bytes)."""
    return (
        isinstance(nonce, str)
        and 32 <= len(nonce) <= 128
        and len(nonce) % 2 == 0
        and set(nonce) <= _NONCE_HEX
    )


def token_proof(token: bytes, nonce: str) -> str:
    """The proof that the holder of ``token`` answers for ``nonce`` (hex HMAC-SHA256)."""
    return hmac.new(token, PROOF_LABEL + nonce.encode("ascii"), hashlib.sha256).hexdigest()


def new_nonce() -> str:
    return secrets.token_hex(32)


def proof_matches(token: str, nonce: str, body: object) -> bool:
    """Whether a ``/proof`` answer ``body`` proves ``token`` for ``nonce`` (constant time)."""
    if not isinstance(body, dict) or not isinstance(body.get("proof"), str):
        return False
    expected = token_proof(token.strip().encode("utf-8"), nonce)
    return secrets.compare_digest(
        body["proof"].encode("ascii", "replace"), expected.encode("ascii")
    )


def proof_response(token: bytes, query_nonce: str | None) -> tuple[int, dict[str, str]]:
    """The ``/proof`` answer: ``(200, {"proof": ...})`` or ``(400, {"detail": ...})``."""
    if query_nonce is None or not valid_nonce(query_nonce):
        return 400, {"detail": "nonce must be 32 to 128 lowercase hex characters"}
    return 200, {"proof": token_proof(token, query_nonce)}


def proves_token(http: Callable[[str, str], tuple[int, Any]], url: str, token: str) -> bool | None:
    """Ask the server at ``url`` to prove that it knows ``token`` (``GET /proof``), without sending it.

    Returns True for a valid proof, False for a server that cannot give one
    (or nothing answering), and None when the answer cannot tell: 404 or 401,
    which a RootMind server from before ``/proof`` gives. The caller then
    falls back to the listener table.
    """
    nonce = new_nonce()
    status, body = http("GET", f"{url}{PROOF_PATH}?nonce={nonce}")
    if status == 200:
        return proof_matches(token, nonce, body)
    if status in (401, 404):
        return None
    return False
