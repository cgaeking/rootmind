"""A small JSON client for the RootMind REST API.

- No proxy for loopback addresses: the request must reach the local service.
- No redirects: a redirect would carry the token to an address nobody checked.
- Errors become :class:`ClientError` with the service's ``detail`` and an
  exit code; the token never appears in a message.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from enum import IntEnum
from typing import Any

DEFAULT_TIMEOUT_SECONDS = 30.0
# The question loop can take minutes (several LLM calls and tool calls).
ASK_TIMEOUT_SECONDS = 600.0
MAX_RESPONSE_BYTES = 64 * 1024 * 1024

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class ClientExit(IntEnum):
    """Exit codes of the client commands."""

    OK = 0
    FAILED = 1  # the service refused or failed the request, or an unexpected error
    USAGE = 2  # wrong usage
    NO_SERVICE = 3  # no service found, stopped, not answering, or another program on its port
    NOT_ALLOWED = 4  # the token cannot be read, or the service refused it (401/403)


class ClientError(Exception):
    """A client command stops with ``code`` and a user-facing ``message``."""

    def __init__(
        self,
        message: str,
        code: ClientExit = ClientExit.FAILED,
        status: int = 0,
        *,
        service: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.code = ClientExit(code)
        self.status = status  # the HTTP status, 0 without an answer
        self.service = service  # the service status, when discovery got that far


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def check_url(url: str) -> str:
    """Validate a REST API base address and return it without a trailing slash.

    ``http://`` is accepted only for loopback hosts: anything else would send
    the token in clear text over a network. ``https://`` is accepted for any
    host (for example an SSH or TLS tunnel to the service).

    Raises:
        ClientError: exit 2 for an address that is refused.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
        host = parts.hostname
        _ = parts.port  # raises ValueError for a bad port
    except ValueError:
        raise ClientError(f"not a valid address: {url!r}", ClientExit.USAGE) from None
    if parts.scheme not in ("http", "https") or not host:
        raise ClientError(
            f"the address must start with http:// or https://, not {url!r}", ClientExit.USAGE
        )
    if parts.scheme == "http" and host not in LOOPBACK_HOSTS:
        raise ClientError(
            f"refusing plain http:// to {host}: the token would cross the network unencrypted. "
            "Use https:// or an SSH tunnel to 127.0.0.1.",
            ClientExit.USAGE,
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ClientError(
            "the address must not contain credentials, a query or a fragment", ClientExit.USAGE
        )
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"


def _detail(payload: Any) -> str | None:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list):
            # FastAPI validation errors: report the fields, not the input values.
            fields = []
            for item in detail:
                if isinstance(item, dict):
                    loc = ".".join(str(p) for p in item.get("loc", []) if p != "body")
                    fields.append(f"{loc}: {item.get('msg', 'invalid')}" if loc else "invalid")
            return "; ".join(fields) or None
    return None


class RestClient:
    """JSON requests to one REST API base address with an optional bearer token."""

    def __init__(self, url: str, token: str | None, *, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.url = check_url(url)
        self._token = token
        self.timeout = timeout
        host = urllib.parse.urlsplit(self.url).hostname
        handlers: list[Any] = [_NoRedirect()]
        if host in LOOPBACK_HOSTS:
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def __repr__(self) -> str:  # never shows the token
        return f"RestClient({self.url!r}, token={'set' if self._token else 'none'})"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """Send one request and return the decoded JSON body (None for an empty one).

        Raises:
            ClientError: no answer (exit 3), 401/403 (exit 4), any other error
                status (exit 1, with the service's ``detail``).
        """
        url = self.url + path
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310 (checked by check_url)
        try:
            with self._opener.open(request, timeout=timeout or self.timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read(MAX_RESPONSE_BYTES + 1)
            except OSError:
                raw = b""
        except TimeoutError:
            raise ClientError(
                f"RootMind at {self.url} did not answer within {timeout or self.timeout:.0f} s",
                ClientExit.NO_SERVICE,
            ) from None
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", None) or e
            raise ClientError(
                f"cannot reach RootMind at {self.url}: {reason}", ClientExit.NO_SERVICE
            ) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ClientError("the answer of the service is too large", ClientExit.FAILED, status)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        if 200 <= status < 300:
            return payload
        detail = _detail(payload)
        if 300 <= status < 400:
            message = f"the service answered with a redirect ({status}); not followed"
            raise ClientError(message, ClientExit.FAILED, status)
        if status in (401, 403):
            message = (
                "the service refused the token"
                if status == 401
                else f"the service refused the request ({detail or 'forbidden'})"
            )
            raise ClientError(message, ClientExit.NOT_ALLOWED, status)
        message = f"{method} {path} failed with HTTP {status}" + (f": {detail}" if detail else "")
        raise ClientError(message, ClientExit.FAILED, status)

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.request("POST", path, body=body if body is not None else {}, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)
