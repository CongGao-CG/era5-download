"""Build monthly CDS requests from variable selections or explicit templates."""

from __future__ import annotations

import calendar
from collections.abc import Mapping, Sequence
from copy import deepcopy
import math
import re
from typing import Any

from .mappings import ERA5_PRESSURE_LEVELS, resolve_dataset, resolve_variables


# These presets cover collections that accept a year and month selection.
# Keep specialized date-range and MARS request schemas out of this path.
_FAMILIES = {
    "reanalysis-era5-single-levels": "hourly",
    "reanalysis-era5-pressure-levels": "hourly",
    "reanalysis-era5-single-levels-monthly-means": "monthly",
    "reanalysis-era5-pressure-levels-monthly-means": "monthly",
    "derived-era5-single-levels-daily-statistics": "daily",
    "derived-era5-pressure-levels-daily-statistics": "daily",
    "reanalysis-era5-land": "hourly",
    "reanalysis-era5-land-monthly-means": "monthly",
    "derived-era5-land-daily-statistics": "daily",
}
_SPECIALIZED = {
    "reanalysis-era5-single-levels-timeseries",
    "reanalysis-era5-land-timeseries",
    "reanalysis-era5-complete",
}


def _strings(value: Any) -> list[str]:
    values = value if isinstance(value, (list, tuple)) else [value]
    if not values or any(v is None or not str(v).strip() for v in values):
        raise ValueError("Request selections must not be empty")
    return list(dict.fromkeys(str(v).strip() for v in values))


def _levels(value: Any) -> list[str]:
    levels = _strings(value)
    if any(level not in ERA5_PRESSURE_LEVELS for level in levels):
        raise ValueError("Use valid ERA5 pressure levels in hPa, e.g. 200, 500 or 850")
    return levels


def _normalize_options(options: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(options))
    for key in ("time", "day", "product_type"):
        if key in result:
            result[key] = _strings(result[key])
    if "day" in result:
        if any(not v.isdigit() or not 1 <= int(v) <= 31 for v in result["day"]):
            raise ValueError("--day values must be between 1 and 31")
        result["day"] = list(dict.fromkeys(f"{int(v):02d}" for v in result["day"]))
    if "time" in result:
        hours = []
        for value in result["time"]:
            match = re.fullmatch(r"(\d{1,2})(?::00)?", value)
            if not match or int(match[1]) > 23:
                raise ValueError("--time values must be whole hours from 00:00 to 23:00")
            hours.append(f"{int(match[1]):02d}:00")
        result["time"] = list(dict.fromkeys(hours))
    if "area" in result:
        area = result["area"]
        if (len(area) != 4 or not all(math.isfinite(v) for v in area)
                or not -90 <= area[2] <= area[0] <= 90
                or any(not -180 <= v <= 360 for v in (area[1], area[3]))):
            raise ValueError("--area requires valid NORTH WEST SOUTH EAST coordinates")
    if "grid" in result:
        if len(result["grid"]) != 2 or any(not math.isfinite(v) or v <= 0 for v in result["grid"]):
            raise ValueError("--grid requires two finite positive spacings")
    if "time_zone" in result:
        match = re.fullmatch(r"utc([+-])(\d{2}):00", result["time_zone"])
        if not match or int(match[2]) > (14 if match[1] == "+" else 12):
            raise ValueError("--time-zone requires a whole-hour offset from utc-12:00 to utc+14:00")
    return result


def build_template(dataset: str, template: Mapping[str, Any] | None = None, *,
                   variables: Sequence[str] | None = None,
                   pressure_levels: Sequence[str] | None = None,
                   options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return fixed request fields, applying CLI selections over a template.

    With no template, verified collection presets supply request settings.
    Existing templates receive no implicit defaults. GRIB short names are
    translated only for ``variables``; template values remain CDS request values.
    """
    dataset = resolve_dataset(dataset)
    if dataset in _SPECIALIZED:
        raise ValueError(f"Monthly submit does not support {dataset}: it uses date-range or MARS "
                         "requests. Use ERA5Client.submit_job with an explicit request instead")
    if template is not None and not isinstance(template, Mapping):
        raise ValueError("Request template must be a JSON object")
    if template is not None and ("year" in template or "month" in template):
        raise ValueError("Request template must not include 'year' or 'month'; "
                         "these come from --begin/--end")
    family = _FAMILIES.get(dataset)
    if template is None and variables is None:
        raise ValueError("Provide --var or --template for submit")
    if template is None and family is None:
        raise ValueError(f"No request defaults for {dataset}; provide --template with its CDS request fields")
    request = deepcopy(dict(template)) if template is not None else {}
    land = "era5-land" in dataset
    if template is None:
        if family == "daily":
            request.update(daily_statistic="daily_mean", time_zone="utc+00:00", frequency="1_hourly")
            if not land:
                request["product_type"] = "reanalysis"
        else:
            request.update(data_format="netcdf", download_format="unarchived")
            if family == "monthly":
                request["product_type"] = ["monthly_averaged_reanalysis"]
            elif not land:
                request["product_type"] = ["reanalysis"]
    explicit = _normalize_options(options or {})
    request.update(explicit)
    if variables is not None:
        request["variable"], _ = resolve_variables(variables, dataset)
    if pressure_levels is not None:
        request["pressure_level"] = _levels(pressure_levels)
    # Preserve the original template-only submission path. Validate shortcuts
    # locally, but leave dataset-specific variable availability to CDS.
    shortcut = template is None or variables is not None or pressure_levels is not None or bool(explicit)
    if family is not None and shortcut:
        pressure = "pressure-levels" in dataset
        if pressure and not request.get("pressure_level"):
            raise ValueError("This pressure-level dataset needs --pressure-level or pressure_level in the template")
        if not pressure and ("pressure_level" in request or "level" in request):
            raise ValueError("Pressure levels cannot be used with this single-level or ERA5-Land dataset")
        if pressure:
            request["pressure_level"] = _levels(request["pressure_level"])
        if family == "monthly" and "day" in request:
            raise ValueError("Monthly averaged datasets do not accept --day; select an hourly or daily dataset")
        if family == "daily":
            if any(key in request for key in ("time", "data_format", "download_format")):
                raise ValueError("Daily statistics datasets do not accept time, data_format or download_format; "
                                 "use --frequency and --time-zone")
            if "product_type" in request:
                products = _strings(request["product_type"])
                if len(products) != 1:
                    raise ValueError("Daily statistics requests require a single product type")
                request["product_type"] = products[0]
            if request.get("daily_statistic") == "daily_sum" and (land or pressure):
                raise ValueError("daily_sum is available only for compatible ERA5 daily single-level variables")
        elif any(key in request for key in ("daily_statistic", "time_zone", "frequency")):
            raise ValueError("Daily statistic, time zone and frequency settings require a daily dataset")
        if land and family != "monthly" and "product_type" in request:
            raise ValueError("This ERA5-Land dataset does not accept product_type")
    if template is None and family != "daily" and "time" not in request:
        products = _strings(request.get("product_type", "reanalysis"))
        if family == "monthly" and not any(p.endswith("by_hour_of_day") for p in products):
            request["time"] = ["00:00"]
        else:
            interval = 3 if any("ensemble" in p for p in products) else 1
            request["time"] = [f"{hour:02d}:00" for hour in range(0, 24, interval)]
    return request


def monthly_request(dataset: str, template: Mapping[str, Any], year: int, month: int, *,
                    fill_days: bool = False) -> dict[str, Any]:
    """Add each collection's date fields and, for presets, actual calendar days."""
    dataset = resolve_dataset(dataset)
    request = deepcopy(dict(template))
    family = _FAMILIES.get(dataset)
    land = "era5-land" in dataset
    request["year"] = f"{year:04d}" if family == "daily" or (land and family == "hourly") else [f"{year:04d}"]
    request["month"] = f"{month:02d}" if land and family in {"hourly", "daily"} else [f"{month:02d}"]
    if fill_days and family in {"hourly", "daily"} and "day" not in request:
        request["day"] = [f"{day:02d}" for day in range(1, calendar.monthrange(year, month)[1] + 1)]
    return request
