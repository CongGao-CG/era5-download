"""Load user-owned credentials from the environment or user configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_URL = "https://cds.climate.copernicus.eu/api"


@dataclass(frozen=True)
class Credentials:
    name: str
    key: str = field(repr=False)
    url: str = DEFAULT_URL
    key_source: str = ""


def _read_rc(path: Path) -> dict[str, str]:
    values = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ValueError(f"Cannot read credential file: {path}") from None
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"Invalid credential file: {path}; expected name: value")
        values[name.strip()] = value.strip().strip("\"'")
    return values


def load_accounts(config: str | Path | None = None, names: list[str] | None = None,
                  all_accounts: bool = False, rc_file: str | Path | None = None,
                  url: str | None = None) -> list[Credentials]:
    """Resolve selected accounts from environment variables or CDS rc files.

    A JSON config maps ``accounts`` to profiles containing ``key_env``,
    ``rc_file`` and/or ``url``. It contains references to secrets, not tokens.
    Explicitly configured profiles never fall back to another account's key.
    """
    if config and rc_file:
        raise ValueError("Use either --config or --rc-file")
    if names and all_accounts:
        raise ValueError("Use either --account or --all-accounts")
    base = Path.cwd()
    profiles = {"default": {}}
    if config:
        source = Path(config).expanduser()
        base = source.resolve().parent
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("Cannot read account config; expected a valid JSON file") from None
        profiles = document.get("accounts") if isinstance(document, dict) else None
        if not isinstance(profiles, dict) or not profiles:
            raise ValueError("Account config must contain a non-empty 'accounts' object")
        for name, profile in profiles.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
                raise ValueError("Account names may contain only letters, digits, '-' and '_'")
            if not isinstance(profile, dict) or set(profile) - {"key_env", "rc_file", "url"}:
                raise ValueError(f"Account '{name}' supports only key_env, rc_file, and url")
            if not all(isinstance(v, str) and v.strip() for v in profile.values()):
                raise ValueError(f"Account '{name}' settings must be non-empty strings")
            if not (profile.get("key_env") or profile.get("rc_file")):
                raise ValueError(f"Account '{name}' requires key_env or rc_file")
    selected = list(profiles) if all_accounts else list(dict.fromkeys(names or ["default"]))
    result = []
    for name in selected:
        if name not in profiles:
            raise ValueError(f"Unknown account '{name}'; select a profile with --account")
        profile = profiles[name]
        explicit_rc = rc_file or profile.get("rc_file")
        values = {}
        rc_path = None
        if explicit_rc:
            candidate = Path(explicit_rc).expanduser()
            rc_path = candidate if candidate.is_absolute() else base / candidate
            values = _read_rc(rc_path)
        elif not config and not os.environ.get("CDSAPI_KEY"):
            candidate = Path(os.environ.get("CDSAPI_RC", "~/.cdsapirc")).expanduser()
            if candidate.exists():
                rc_path = candidate
                values = _read_rc(candidate)
        if profile.get("key_env"):
            key = os.environ.get(profile["key_env"], "")
            key_source = f"environment variable {profile['key_env']}"
        elif explicit_rc or config:
            key = values.get("key", "")
            key_source = f"rc file {rc_path}" if rc_path is not None else "rc file (not found)"
        elif os.environ.get("CDSAPI_KEY"):
            key = os.environ["CDSAPI_KEY"]
            key_source = "environment variable CDSAPI_KEY"
        else:
            key = values.get("key", "")
            key_source = f"rc file {rc_path}" if rc_path is not None else "rc file (not found)"
        if not key.strip():
            raise ValueError(f"No credentials for '{name}'; set CDSAPI_KEY, use ~/.cdsapirc, "
                             "or configure key_env/rc_file")
        api_url = (url or profile.get("url") or
                   (os.environ.get("CDSAPI_URL") if not config and not explicit_rc else None)
                   or values.get("url") or DEFAULT_URL)
        if not api_url.startswith(("https://", "http://")):
            raise ValueError("The API URL must begin with https:// or http://")
        result.append(Credentials(name, key.strip(), api_url.rstrip("/"), key_source))
    return result

