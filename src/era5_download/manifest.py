"""Read and write portable CSV download manifests."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Iterable


def validate_filename(filename: str) -> str:
    """Validate a relative POSIX path without interpreting it on the host OS."""
    if not isinstance(filename, str) or not filename.strip():
        raise ValueError("Filename must be a non-empty string")
    filename = filename.strip()
    if (PurePosixPath(filename).is_absolute()
            or PureWindowsPath(filename).drive
            or "\\" in filename or ":" in filename
            or any(part in {"", ".", ".."} for part in filename.split("/"))
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)):
        raise ValueError(f"Filename must be a safe relative path without '..': {filename!r}")
    return filename


def _required_string(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    value = value.strip()
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


@dataclass(frozen=True)
class ManifestEntry:
    """One remote job and its destination; account is a local profile label."""

    filename: str
    job_id: str
    account: str = "default"

    def __post_init__(self) -> None:
        object.__setattr__(self, "filename", validate_filename(self.filename))
        object.__setattr__(self, "job_id", _required_string(self.job_id, "Job ID"))
        object.__setattr__(self, "account", _required_string(self.account, "Account"))


def _read_manifest(
    source: Path,
) -> tuple[list[ManifestEntry], list[str], dict[str, str | None]]:
    with source.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        try:
            headers = reader.fieldnames
            if not headers:
                raise ValueError(f"{source}: manifest is empty; expected CSV headers")
            if any(not header or not header.strip() for header in headers):
                raise ValueError(f"{source}: manifest contains an empty column header")
            if len(headers) != len(set(headers)):
                raise ValueError(f"{source}: manifest contains duplicate column headers")
            selected: dict[str, str | None] = {}
            for name, aliases in (("filename", ("filename", "fnm")),
                                  ("job_id", ("job_id", "rid")),
                                  ("account", ("account", "acc"))):
                found = [alias for alias in aliases if alias in headers]
                if len(found) > 1:
                    raise ValueError(f"{source}: use only one of {', '.join(aliases)}")
                if not found and name != "account":
                    raise ValueError(f"{source}: missing required '{name}' column (or '{aliases[1]}')")
                selected[name] = found[0] if found else None
            entries = []
            for row in reader:
                if None in row:
                    raise ValueError(f"{source}: line {reader.line_num}: too many CSV values")
                try:
                    entries.append(ManifestEntry(
                        filename=row[selected["filename"]],
                        job_id=row[selected["job_id"]],
                        account=row[selected["account"]] if selected["account"] else "default",
                    ))
                except ValueError as exc:
                    raise ValueError(f"{source}: line {reader.line_num}: {exc}") from exc
            return entries, headers, selected
        except csv.Error as exc:
            raise ValueError(f"{source}: line {reader.line_num}: invalid CSV: {exc}") from exc


def read_manifest(path: str | Path) -> list[ManifestEntry]:
    """Read ``filename,job_id,account`` or legacy ``fnm,rid,acc`` CSV columns.

    The account column is optional and defaults to ``default``. Extra named
    columns are ignored. Invalid records report their source path and line.
    """
    return _read_manifest(Path(path))[0]


def _append_columns(
    target: Path, accounts: Iterable[str],
) -> tuple[list[str], dict[str, str | None]]:
    accounts = [_required_string(account, "Account") for account in accounts]
    if not target.exists():
        headers = ["filename", "job_id", "account"]
        return headers, {name: name for name in headers}
    _, headers, selected = _read_manifest(target)
    if selected["account"] is None and any(account != "default" for account in accounts):
        raise ValueError(
            f"{target}: cannot append a non-default account without an 'account' (or 'acc') column"
        )
    return headers, selected


def validate_manifest_append(path: str | Path, accounts: Iterable[str] = ()) -> None:
    """Check existing CSV records and whether its columns can store the accounts.

    A missing file can be created with canonical columns. Existing empty or
    invalid files are rejected. This check does not create or modify files.
    """
    _append_columns(Path(path), accounts)


def append_manifest(path: str | Path, entry: ManifestEntry) -> None:
    """Append one row to a manifest CSV, creating it with a header if needed.

    Safe to call once per submitted job: rows already on disk are never
    rewritten, so an interrupted batch loses at most the row in progress.
    Existing column order and aliases are retained, with extra columns left
    blank. A file without an account column accepts only the default account.
    """
    if not isinstance(entry, ManifestEntry):
        raise TypeError("Manifest rows must be ManifestEntry instances")
    target = Path(path)
    new_file = not target.exists()
    headers, selected = _append_columns(target, (entry.account,))
    row = {selected[name]: getattr(entry, name) for name in selected if selected[name]}
    needs_newline = False
    if not new_file:
        with target.open("rb") as stream:
            stream.seek(-1, 2)
            needs_newline = stream.read(1) not in (b"\n", b"\r")
    with target.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        if new_file:
            writer.writeheader()
        elif needs_newline:
            stream.write("\r\n")
        writer.writerow(row)


def write_manifest(path: str | Path, entries: Iterable[ManifestEntry]) -> None:
    """Write canonical CSV columns, replacing an existing file if present."""
    rows = list(entries)
    if any(not isinstance(entry, ManifestEntry) for entry in rows):
        raise TypeError("Manifest rows must be ManifestEntry instances")
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("filename", "job_id", "account"))
        writer.writerows((entry.filename, entry.job_id, entry.account) for entry in rows)
