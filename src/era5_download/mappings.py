"""Public ERA5 dataset names and official GRIB short-name resolution; no account settings."""

from collections.abc import Iterable

from .catalogue import DATASET_VARIABLES, GRIB_VARIABLES

DATASET_MAPPING = {
    "era5_hourly_single": "reanalysis-era5-single-levels",
    "era5_hourly_pressure": "reanalysis-era5-pressure-levels",
    "era5_monthly_single": "reanalysis-era5-single-levels-monthly-means",
    "era5_monthly_pressure": "reanalysis-era5-pressure-levels-monthly-means",
    "era5_daily_single": "derived-era5-single-levels-daily-statistics",
    "era5_daily_pressure": "derived-era5-pressure-levels-daily-statistics",
    "era5_single_timeseries": "reanalysis-era5-single-levels-timeseries",
    "era5land_hourly": "reanalysis-era5-land",
    "era5land_monthly": "reanalysis-era5-land-monthly-means",
    "era5land_daily": "derived-era5-land-daily-statistics",
    "era5land_timeseries": "reanalysis-era5-land-timeseries",
    "era5_complete_mars": "reanalysis-era5-complete",
}

VARIABLE_ABBREVIATIONS = {
    "u_component_of_wind": "u",
    "v_component_of_wind": "v",
    "relative_humidity": "rh",
    "vorticity": "vo",
    "sea_surface_temperature": "sst",
    "land_sea_mask": "lsm",
    "temperature": "t",
    "geopotential": "z",
    "specific_humidity": "q",
    "vertical_velocity": "w",
    "divergence": "d",
    "2m_temperature": "t2m",
    "2m_dewpoint_temperature": "d2m",
    "10m_u_component_of_wind": "u10",
    "10m_v_component_of_wind": "v10",
    "surface_pressure": "sp",
    "mean_sea_level_pressure": "msl",
    "total_precipitation": "tp",
}

# ERA5 pressure levels in hPa, as listed in ECMWF's ERA5 data documentation.
ERA5_PRESSURE_LEVELS = (
    "1", "2", "3", "5", "7", "10", "20", "30", "50", "70", "100", "125",
    "150", "175", "200", "225", "250", "300", "350", "400", "450", "500",
    "550", "600", "650", "700", "750", "775", "800", "825", "850", "875",
    "900", "925", "950", "975", "1000",
)

# Lowercase alias eases migration from the original standalone scripts.
dataset_mapping = DATASET_MAPPING


def resolve_dataset(name: str) -> str:
    """Return a CDS collection id, resolving a short alias if one matches."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Dataset must be a non-empty string")
    return DATASET_MAPPING.get(name, name)


def resolve_variables(values: Iterable[str], dataset: str | None = None) -> tuple[list[str], list[str]]:
    """Translate supported official GRIB short names to CDS variable names.

    Accept repeated or comma-separated selections, using the exact lowercase
    short names. The dataset selects its CDS spelling and availability. Pressure levels
    must be supplied separately. The second list
    is retained for API compatibility and is always empty.
    """
    if isinstance(values, (str, bytes)):
        raise ValueError("Variables must be an iterable of non-empty strings, not a string")
    try:
        selections = iter(values)
    except TypeError:
        raise ValueError("Variables must be an iterable of non-empty strings") from None
    selected_dataset = resolve_dataset(dataset) if dataset is not None else None
    selections_for_dataset = DATASET_VARIABLES.get(selected_dataset, GRIB_VARIABLES)
    variables = []
    for value in selections:
        if not isinstance(value, str):
            raise ValueError("Each variable must be a non-empty string")
        for token in value.split(","):
            token = token.strip()
            if token not in GRIB_VARIABLES:
                raise ValueError(
                    f"Unsupported --var value {token!r}; use a supported official ECMWF GRIB "
                    "short name (e.g. sst, 2t, 10u, u), and --pressure-level for levels. "
                    "Use --template for CDS variable names without a supported short-name mapping"
                )
            if token not in selections_for_dataset:
                raise ValueError(f"GRIB short name {token!r} is not available in {selected_dataset}")
            if selected_dataset not in DATASET_VARIABLES:
                names = {m[token] for m in DATASET_VARIABLES.values() if token in m}
                if len(names) > 1:
                    raise ValueError(f"GRIB short name {token!r} requires a dataset to resolve its CDS variable name")
            variable = selections_for_dataset[token]
            if variable not in variables:
                variables.append(variable)
    if not variables:
        raise ValueError("Specify at least one variable")
    return variables, []
