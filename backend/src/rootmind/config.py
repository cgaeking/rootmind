"""Configuration loading and persistence for the RootMind backend."""

import contextlib
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

LlmProvider = Literal["anthropic", "openai", "xai", "ollama"]

# Where a local Ollama server listens by default.
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434"

DEFAULT_MODEL = "claude-sonnet-5-5"

# Default file patterns for every indexed folder. Every included extension has
# a parser (a test checks this and that config.example.yaml uses the same list).
DEFAULT_INCLUDE = ("*.pdf", "*.docx", "*.xlsx", "*.pptx", "*.md", "*.txt", "*.csv")
DEFAULT_EXCLUDE = (".*", "~$*", "node_modules", "__pycache__")

# API keys are returned to clients only in masked form (see ``mask_secret``).
# The mask starts with a character no real provider key starts with, so a
# masked value sent back by a client can be recognized and never overwrites
# the stored key.
MASK_PREFIX = "…"  # horizontal ellipsis
MASK_VISIBLE_CHARS = 4


def mask_secret(value: str | None) -> str:
    """Return a display-safe form of a secret: ellipsis + last 4 chars, or ``""`` when unset.

    Secrets of 8 characters or fewer reveal nothing (just the ellipsis), so the
    visible suffix never gives away a meaningful share of a short key.
    """
    if not value:
        return ""
    if len(value) <= 2 * MASK_VISIBLE_CHARS:
        return MASK_PREFIX
    return MASK_PREFIX + value[-MASK_VISIBLE_CHARS:]


def is_masked(value: str | None) -> bool:
    """True if ``value`` has exactly the shape of a mask produced by :func:`mask_secret`."""
    return (
        isinstance(value, str)
        and value.startswith(MASK_PREFIX)
        and len(value) <= len(MASK_PREFIX) + MASK_VISIBLE_CHARS
    )


def check_base_url(value: str) -> None:
    """Reject a base URL that is not an http(s) address.

    Raises:
        ValueError: the value does not start with ``http://`` or ``https://``.
    """
    if not re.match(r"^https?://[^\s/]+", value.strip()):
        raise ValueError("The base URL must start with http:// or https://")


def check_api_key(value: str | None) -> None:
    """Reject a key that starts with the mask prefix but is longer than a mask.

    That happens when a new key is pasted into a field that still shows the
    mask: the result is neither the stored key nor the new one.

    Raises:
        ValueError: the value is such a mix.
    """
    if isinstance(value, str) and value.startswith(MASK_PREFIX) and not is_masked(value):
        raise ValueError(
            f"API key starts with the mask character '{MASK_PREFIX}' but is not a mask; "
            "clear the field and paste the whole key"
        )


def app_home() -> Path:
    """Base directory for ``config.yaml`` and the ``data/`` folder.

    When ``RLM_HOME`` is set (e.g. by the desktop app or the Windows service,
    which point it at an app-data folder), config and database live there so an
    installed app can write outside its read-only program folder. When unset,
    paths stay relative to the current working directory.
    """
    home = os.environ.get("RLM_HOME")
    if home:
        p = Path(home).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p
    return Path.cwd()


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    provider: LlmProvider = "anthropic"
    model: str = DEFAULT_MODEL
    # api_keys is the canonical per-provider store; api_key mirrors the active
    # provider's key (the engine reads api_key).
    api_key: str | None = None
    api_keys: dict[str, str] = Field(default_factory=dict)
    # Ollama only: the server's address (a LAN host works too). Only the user
    # can change it, through this file or the settings screen.
    base_url: str = OLLAMA_DEFAULT_BASE_URL
    # Generous but bounded timeout for outgoing LLM calls, so a stalled
    # provider response can't hang a request forever.
    timeout_seconds: float = 120.0


class DatabaseConfig(BaseModel):
    """Database configuration (SQLite only)."""

    path: str = "./data/index.db"


class ConnectorConfig(BaseModel):
    """Individual connector configuration."""

    name: str
    type: str
    path: str | None = None
    # None -> inherit the global indexer.include / indexer.exclude patterns.
    include: list[str] | None = None
    exclude: list[str] | None = None


class IndexerConfig(BaseModel):
    """Indexer configuration."""

    sync_schedule: str | None = "0 3 * * *"
    # Automatic re-index interval in hours (default: once a day). Takes precedence
    # over sync_schedule when > 0.
    sync_interval_hours: int = 24
    max_file_size_mb: int = 50
    # Global file patterns applied to every indexed folder.
    include: list[str] = Field(default_factory=lambda: list(DEFAULT_INCLUDE))
    exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDE))


class APIConfig(BaseModel):
    """REST API server configuration, used by ``rootmind api``.

    The bind address resolves as: command-line flag, then ``RLM_HOST`` /
    ``RLM_PORT``, then these values. The API binds to the loopback interface
    by default and refuses connections from other machines. A bearer token
    (``RLM_API_TOKEN`` or ``--require-token``) is optional for the CLI; the
    Windows service, which the desktop app uses, always uses one.
    Do not expose the API on a network interface.
    """

    host: str = "127.0.0.1"
    port: int = 8000


class RemoteAccessConfig(BaseModel):
    """Built-in remote access: an ngrok tunnel to MCP over HTTP (off by default).

    The RootMind service's MCP server runs the tunnel while ``enabled`` is
    true (see :mod:`rootmind.remote_access`). Only these non-secret settings
    live here; the ngrok authtoken has its own file in ``data``.
    """

    enabled: bool = False
    # A reserved ngrok domain (host name only); None: ngrok assigns one.
    domain: str | None = None


class TracingConfig(BaseModel):
    """Tracing of the question loop (both off by default; see :mod:`rootmind.tracing`).

    Only ``config.yaml`` turns these on; the settings screen does not change
    them. Traces contain questions and the document text the model read.
    """

    # JSONL and a readable summary under <home>/logs/traces/; nothing leaves the computer.
    local: bool = False
    # Export to Langfuse; needs the `tracing` extra and LANGFUSE_* environment variables.
    langfuse: bool = False


class AppConfig(BaseModel):
    """Main application configuration."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    connectors: list[ConnectorConfig] = Field(default_factory=list)
    indexer: IndexerConfig = Field(default_factory=IndexerConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    remote_access: RemoteAccessConfig = Field(default_factory=RemoteAccessConfig)
    tracing: TracingConfig = Field(default_factory=TracingConfig)


class Settings(BaseSettings):
    """Environment-based settings (also read from a ``.env`` file in the working directory)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    xai_api_key: str | None = None
    config_path: str = "config.yaml"


def _resolve_config_path(config_path: str | Path | None, settings: Settings) -> Path:
    """Resolve a relative config path under the app home (RLM_HOME or cwd).

    Loading and saving always hit the same file, even when a default name like
    "config.yaml" is passed in explicitly.
    """
    path = Path(config_path or settings.config_path)
    if not path.is_absolute():
        path = app_home() / path
    return path


def config_file_path() -> Path:
    """The config file that :func:`get_config` loads and :func:`save_config` writes.

    That is the file passed to :func:`init_config`, or ``CONFIG_PATH`` /
    ``config.yaml``, resolved under the app home.
    """
    return _resolve_config_path(active_config_path(), Settings())


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Load configuration from a YAML file.

    Args:
        config_path: Path to config file. If None, uses the default or the
            ``CONFIG_PATH`` environment variable.

    Returns:
        Loaded AppConfig instance.
    """
    settings = Settings()
    path = _resolve_config_path(config_path, settings)

    data = {}
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    config = AppConfig.model_validate(data)

    # Resolve a relative SQLite path under the app home so the database lives
    # next to the config (matters when RLM_HOME points outside the program dir).
    if config.database.path and not Path(config.database.path).is_absolute():
        config.database.path = str(app_home() / config.database.path)

    # Normalize per-provider keys. Seed from a single api_key in the file and
    # from environment variables, then expose the active provider's key as api_key.
    if config.llm.api_key and not config.llm.api_keys.get(config.llm.provider):
        config.llm.api_keys[config.llm.provider] = config.llm.api_key
    if settings.anthropic_api_key and not config.llm.api_keys.get("anthropic"):
        config.llm.api_keys["anthropic"] = settings.anthropic_api_key
    if settings.openai_api_key and not config.llm.api_keys.get("openai"):
        config.llm.api_keys["openai"] = settings.openai_api_key
    if settings.xai_api_key and not config.llm.api_keys.get("xai"):
        config.llm.api_keys["xai"] = settings.xai_api_key
    config.llm.api_key = config.llm.api_keys.get(config.llm.provider)

    return config


def _merge_api_keys(incoming: dict[str, str | None], stored: dict[str, str]) -> dict[str, str]:
    """Merge client-sent API keys with the keys stored in the config file.

    - a masked value (as returned by ``GET /config``) keeps the stored key; if
      no key is stored for that provider (e.g. it came from an environment
      variable), nothing is persisted for it
    - an empty value removes the key
    - a value that starts with the mask but is longer is rejected
      (see :func:`check_api_key`)
    - any other value replaces the key
    """
    merged: dict[str, str] = {}
    for provider, value in incoming.items():
        if not value:
            continue
        check_api_key(value)
        if is_masked(value):
            if stored.get(provider):
                merged[provider] = stored[provider]
            continue
        merged[provider] = value
    return merged


def save_config(data: dict, config_path: str | Path | None = None) -> AppConfig:
    """Persist editable config fields to the YAML file.

    Only known, user-editable sections are merged in (``llm``, ``connectors``,
    parts of ``indexer`` and ``remote_access``); all other sections in the
    file are kept as-is.
    The merged result is validated against ``AppConfig`` before anything is
    written, and a ``.bak`` backup of the previous file is created.

    Masked API keys (see :func:`mask_secret`) never overwrite stored keys.

    Args:
        data: Editable config subset, e.g.
            ``{"llm": {...}, "connectors": [...], "indexer": {"sync_schedule": ...}}``.
        config_path: Target file. Defaults to the same path as ``load_config``.

    Returns:
        The validated :class:`AppConfig`.
    """
    settings = Settings()
    path = _resolve_config_path(config_path, settings)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Start from the existing file so untouched sections survive.
    if path.exists():
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    else:
        raw = AppConfig().model_dump(mode="json")

    if "llm" in data and data["llm"] is not None:
        llm_in = data["llm"]
        raw_llm = raw.get("llm") or {}
        raw["llm"] = raw_llm
        stored_provider = raw_llm.get("provider", "anthropic")
        stored_keys = dict(raw_llm.get("api_keys") or {})
        if raw_llm.get("api_key") and not stored_keys.get(stored_provider):
            stored_keys[stored_provider] = raw_llm["api_key"]

        if llm_in.get("base_url") is not None:
            check_base_url(llm_in["base_url"])
        for key in ("provider", "model", "base_url"):
            if key in llm_in:
                raw_llm[key] = llm_in[key]
        check_api_key(llm_in.get("api_key"))
        if llm_in.get("api_keys") is not None:
            raw_llm["api_keys"] = _merge_api_keys(llm_in["api_keys"], stored_keys)
        elif llm_in.get("api_key") and not is_masked(llm_in["api_key"]):
            prov0 = llm_in.get("provider") or raw_llm.get("provider", "anthropic")
            raw_llm.setdefault("api_keys", {})[prov0] = llm_in["api_key"]
        # Keep the single api_key in sync with the active provider (engine compat).
        prov = llm_in.get("provider") or raw_llm.get("provider", "anthropic")
        raw_llm["api_key"] = (raw_llm.get("api_keys") or {}).get(prov)

    if "connectors" in data and data["connectors"] is not None:
        new_conns = []
        for c in data["connectors"]:
            entry = {"name": c["name"], "type": c.get("type", "local"), "path": c.get("path")}
            if c.get("include") is not None:
                entry["include"] = c["include"]
            if c.get("exclude") is not None:
                entry["exclude"] = c["exclude"]
            new_conns.append(entry)
        raw["connectors"] = new_conns

    if "indexer" in data and data["indexer"] is not None:
        raw.setdefault("indexer", {})
        if "sync_schedule" in data["indexer"]:
            raw["indexer"]["sync_schedule"] = data["indexer"]["sync_schedule"]
        if data["indexer"].get("sync_interval_hours") is not None:
            raw["indexer"]["sync_interval_hours"] = data["indexer"]["sync_interval_hours"]
        if data["indexer"].get("include") is not None:
            raw["indexer"]["include"] = data["indexer"]["include"]
        if data["indexer"].get("exclude") is not None:
            raw["indexer"]["exclude"] = data["indexer"]["exclude"]

    if "remote_access" in data and data["remote_access"] is not None:
        section = raw.get("remote_access")
        raw["remote_access"] = section = dict(section) if isinstance(section, dict) else {}
        for key in ("enabled", "domain"):
            if key in data["remote_access"]:
                section[key] = data["remote_access"][key]

    # Validate the full merged config before touching disk.
    config = AppConfig.model_validate(raw)
    text = yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False)

    if path.exists():
        backup = path.with_suffix(path.suffix + ".bak")
        _write_private_atomic(backup, path.read_text(encoding="utf-8"))
    _write_private_atomic(path, text)

    return config


def _write_private_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` atomically; owner-only (0600) on POSIX.

    The text goes to a temporary file in the same folder, is flushed to disk
    and then moved over the target, so a crash or a full disk never leaves a
    truncated config. The file holds API keys, hence the mode. On Windows the
    mode is ignored and the file inherits the folder's permissions.
    """
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # On Windows, a reader that holds the target open makes the replace
        # fail for a moment; retry a few times before giving up.
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


# A config file is a few kilobytes; import_config() refuses anything larger.
MAX_IMPORT_BYTES = 1024 * 1024


def _is_reparse_point(info: os.stat_result) -> bool:
    """True for symlinks and, on Windows, any reparse point (junctions included)."""
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _read_import_source(source: Path) -> str:
    """Read a config file for :func:`import_config`, refusing links and odd files.

    The file is checked before it is opened (no link or reparse point, a
    regular file with one name, at most 1 MB), and the opened handle must be
    the file that was checked, so a link swapped in between is not followed.
    """
    info = os.lstat(source)
    if _is_reparse_point(info):
        raise ValueError(f"{source} is a link or reparse point; import the file itself")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{source} is not a regular file")
    if info.st_nlink > 1:
        raise ValueError(f"{source} has more than one hard link")
    if info.st_size > MAX_IMPORT_BYTES:
        raise ValueError(f"{source} is larger than 1 MB")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(source, flags), "rb") as f:
        opened = os.fstat(f.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError(f"{source} was replaced while it was being read")
        data = f.read(MAX_IMPORT_BYTES + 1)
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError(f"{source} is larger than 1 MB")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ValueError(f"{source} is not UTF-8 text") from e


def _validation_summary(error: ValidationError) -> str:
    """The field paths and messages of a validation error, without the input values.

    Input values can be API keys, which must not reach a log.
    """
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc']) or 'config'}: {item['msg']}"
        for item in error.errors(include_url=False, include_input=False)
    )


def import_config(source: str | Path, home: str | Path, *, database_path: str) -> AppConfig:
    """Replace ``<home>/config.yaml`` with the config in ``source``.

    ``service install`` uses it to move the background service's index into
    ``data/db`` when its ``config.yaml`` (edited by hand) names a database
    elsewhere: ``source`` is then that same file.

    - The file is read directly (YAML, validated against :class:`AppConfig`).
      API keys from environment variables or a ``.env`` file are not merged
      in, unlike :func:`load_config`.
    - Links, reparse points, hard-linked files, non-regular files and files
      over 1 MB are refused.
    - ``database.path`` is always set to ``database_path``, because the source
      belongs to another installation with its own index.
    - The previous ``config.yaml`` is kept as ``config.yaml.bak``, and the new
      file is written atomically (owner-only on POSIX).

    Returns:
        The imported configuration, as written (``database.path`` unresolved).

    Raises:
        FileNotFoundError: ``source`` does not exist.
        ValueError: ``source`` is refused, is not valid YAML or is not a valid
            config. The message never contains values from the file.
        OSError: reading or writing failed.
    """
    source = Path(source)
    text = _read_import_source(source)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ValueError(f"{source} is not valid YAML{where}") from None
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{source} does not contain a RootMind config")
    database = data.get("database") or {}
    if not isinstance(database, dict):
        raise ValueError(f"{source} has an invalid 'database' section")
    data["database"] = {**database, "path": database_path}
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as e:
        raise ValueError(
            f"{source} is not a valid RootMind config: {_validation_summary(e)}"
        ) from None

    # Only the fields the source sets (plus database.path), validated and
    # normalized; unknown keys are dropped.
    out = yaml.safe_dump(
        config.model_dump(mode="json", exclude_unset=True),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    target_dir = Path(home)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "config.yaml"
    if target.exists():
        _write_private_atomic(
            target.with_name(target.name + ".bak"), target.read_text(encoding="utf-8")
        )
    _write_private_atomic(target, out)
    return config


# Global config instance (lazy loaded) and the file it was loaded from, so a
# later save or reload targets the same file (e.g. after ``--config other.yaml``).
_config: AppConfig | None = None
_config_path: str | Path | None = None


def get_config() -> AppConfig:
    """Get the global config instance."""
    global _config
    if _config is None:
        _config = load_config(_config_path)
    return _config


def active_config_path() -> str | Path | None:
    """The config file passed to :func:`init_config`, or None for the default."""
    return _config_path


def init_config(config: AppConfig | str | Path | None = None) -> AppConfig:
    """Initialize the global config from an ``AppConfig`` object or a file path.

    With a path (or None), the file is loaded and remembered for later saves
    and reloads. With an ``AppConfig`` object, that object is used as-is.
    """
    global _config, _config_path
    if isinstance(config, AppConfig):
        _config = config
    else:
        if config is not None:
            _config_path = config
        _config = load_config(_config_path)
    return _config


def reset_config() -> None:
    """Forget the cached config and config path, so the next ``get_config()`` reloads the default."""
    global _config, _config_path
    _config = None
    _config_path = None
