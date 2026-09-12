"""Produce safe filenames from ERA5 request metadata without contacting CDS."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from .mappings import VARIABLE_ABBREVIATIONS

_SIMPLE_KEYS = {
    "variable", "pressure_level", "level", "year", "month", "data_format",
    "format", "download_format", "product_type",
}


def _values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [item for item in value if item is not None and str(item).strip()]
    return [value] if str(value).strip() else []


def _slug(value: Any, fallback: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return cleaned[:48].rstrip("-") or fallback


def _request_hash(request: Mapping[str, Any]) -> str:
    try:
        serialized = json.dumps(dict(request), sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Request must contain JSON-compatible values") from exc
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]


def _date_part(values: list[Any], width: int, name: str) -> str:
    if len(values) > 1:
        return f"multi{name}"
    if not values:
        return ""
    text = str(values[0]).strip()
    if text.isdigit():
        number = int(text)
        if name == "year" and 1 <= number <= 9999:
            return f"{number:0{width}d}"
        if name == "month" and 1 <= number <= 12:
            return f"{number:0{width}d}"
    return _slug(text, name)


def _extension(request: Mapping[str, Any]) -> str:
    archive = " ".join(str(v).lower() for v in _values(request.get("download_format")))
    if archive in {"zip", "zipped", "archived"}:
        return ".zip"
    formats = _values(request.get("data_format")) or _values(request.get("format"))
    format_name = str(formats[0]).lower() if len(formats) == 1 else ""
    if format_name in {"grib", "grib1", "grib2"}:
        return ".grib"
    if format_name in {"zip", "netcdf.zip", "netcdf_zip", "netcdf-zip"}:
        return ".zip"
    return ".nc"


def filename_from_request(request: Mapping[str, Any], job_id: str | None = None) -> str:
    """Return a safe basename, e.g. ``u200_202001.nc`` for a simple request.

    Multiple variables, levels, years or months are represented explicitly and
    receive a deterministic request hash. Additional selections (days, times,
    area, grid, etc.) also receive a hash so subsets do not share a filename.
    A supplied job ID adds a 64-bit digest, distinguishing jobs without putting
    raw server identifiers into filesystem paths. Filenames are at most 180
    characters. Unknown output formats default to ``.nc``.
    """
    if not isinstance(request, Mapping):
        raise TypeError("Request must be a mapping")
    variables = _values(request.get("variable"))
    levels = _values(request.get("pressure_level")) or _values(request.get("level"))
    years = _values(request.get("year"))
    months = _values(request.get("month"))
    complex_request = any(len(v) > 1 for v in (variables, levels, years, months))
    complex_request |= any(k not in _SIMPLE_KEYS for k in request)
    complex_request |= any(len(_values(request.get(k))) > 1 for k in
                           ("product_type", "data_format", "format", "download_format"))

    if len(variables) > 1:
        field = "multivar"
    elif variables:
        variable = str(variables[0]).strip().lower()
        field = VARIABLE_ABBREVIATIONS.get(variable, _slug(variable, "var"))
        # Unrecognized names may need normalization/truncation, so include the
        # original metadata in a digest to avoid conflating distinct names.
        complex_request |= variable not in VARIABLE_ABBREVIATIONS
    else:
        field = "era5"
    if len(levels) > 1:
        field += "-multilevel"
    elif levels:
        level = re.sub(r"\s*hpa\s*$", "", str(levels[0]), flags=re.IGNORECASE).strip()
        if level:
            field += _slug(level, "level")

    year = _date_part(years, 4, "year")
    month = _date_part(months, 2, "month")
    if year and month:
        period = year + month if len(years) == len(months) == 1 else f"{year}-{month}"
    elif year:
        period = year
    elif month:
        period = f"month{month}"
    else:
        period = ""
    stem = f"{field}_{period}" if period else field
    suffix = f"-{_request_hash(request)}" if complex_request else ""
    if job_id is not None:
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("Job ID must be a non-empty string")
        suffix += "-" + hashlib.sha256(job_id.strip().encode("utf-8")).hexdigest()[:16]
    extension = _extension(request)
    stem = stem[:180 - len(suffix) - len(extension)].rstrip("-_.") or "era5"
    return stem + suffix + extension


def _year_month(value: Any, label: str) -> tuple[int, int]:
    if (not isinstance(value, tuple) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, int) for v in value)):
        raise ValueError(f"{label} must be a (year, month) tuple of integers")
    year, month = value
    if not 1 <= year <= 9999:
        raise ValueError(f"{label} year must be between 1 and 9999")
    if not 1 <= month <= 12:
        raise ValueError(f"{label} month must be between 1 and 12")
    return year, month


def month_range(begin: tuple[int, int], end: tuple[int, int]) -> Iterable[tuple[int, int]]:
    """Yield ``(year, month)`` pairs from ``begin`` (inclusive) to ``end`` (exclusive)."""
    begin_year, begin_month = _year_month(begin, "begin")
    end_year, end_month = _year_month(end, "end")
    start = begin_year * 12 + (begin_month - 1)
    stop = end_year * 12 + (end_month - 1)
    if start > stop:
        raise ValueError("begin must not be after end")
    for index in range(start, stop):
        yield index // 12, index % 12 + 1


def generate_flist(fields: Iterable[str], begin: tuple[int, int], end: tuple[int, int]) -> list[str]:
    """List monthly NC filenames from ``begin`` (year, month) to ``end`` (exclusive)."""
    months = list(month_range(begin, end))
    if isinstance(fields, (str, bytes)):
        raise ValueError("Fields must be an iterable of field names, not a string")
    try:
        field_names = list(fields)
    except TypeError as exc:
        raise ValueError("Fields must be an iterable of field names") from exc
    for field in field_names:
        if (not isinstance(field, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", field)):
            raise ValueError("Each field must be a safe name of 1–80 letters, digits, '_' or '-'")
    return [f"{field}_{year:04d}{month:02d}.nc"
            for field in field_names
            for year, month in months]
