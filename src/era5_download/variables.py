"""Search and display the bundled variable catalogue without network access."""

from .catalogue import DATASET_VARIABLES, DOCUMENTED_GRIB_NAMES, VARIABLE_CATALOGUE
from .mappings import DATASET_MAPPING, resolve_dataset


def find_variables(*, datasets=(), search=(), family=None, frequency=None):
    """Return matching records; dataset selections are OR, search terms are AND."""
    selected = set(DATASET_VARIABLES)
    if datasets:
        requested = {resolve_dataset(dataset) for dataset in datasets}
        unknown = requested - selected
        if unknown:
            raise ValueError("No bundled variable catalogue for: " + ", ".join(sorted(unknown)))
        selected &= requested
    aliases = {dataset: alias for alias, dataset in DATASET_MAPPING.items()}
    if family:
        selected = {d for d in selected if ("era5-land" in d) == (family == "era5-land")}
    if frequency:
        selected = {d for d in selected if frequency in aliases[d]}
    terms = [word.casefold().replace("_", " ") for query in search for word in query.split()]
    results = []
    for short, variable, parameter_id, available in VARIABLE_CATALOGUE:
        matches = sorted(selected.intersection(available), key=lambda d: aliases[d])
        if not matches:
            continue
        old_names = sorted(name for name, current in DOCUMENTED_GRIB_NAMES.items() if current == short)
        haystack = " ".join([short, *old_names, variable, str(parameter_id)]).casefold().replace("_", " ")
        if not all(term in haystack for term in terms):
            continue
        results.append(dict(short_name=short, documented_names=old_names, cds_variable=variable,
                            parameter_id=parameter_id, datasets=[aliases[d] for d in matches]))
    return sorted(results, key=lambda row: (row["cds_variable"], row["short_name"]))


def print_variable_table(records):
    """Print complete names and availability flags, without terminal dependencies."""
    print("Bundled CDS catalogue: 2026-09-12")
    print("Availability in matching datasets: H=hourly, M=monthly, D=daily; -=absent")
    print("ERA5-S=single levels, ERA5-P=pressure levels, LAND=ERA5-Land")
    if not records:
        print("No variables match these filters.")
        return
    rows = [["GRIB SHORT NAME(S)", "CDS VARIABLE", "PARAM ID", "ERA5-S", "ERA5-P", "LAND"]]
    for record in records:
        flags = []
        for family in ("single", "pressure", "land"):
            flags.append("".join(
                letter if any(family in d and frequency in d for d in record["datasets"]) else "-"
                for letter, frequency in (("H", "hourly"), ("M", "monthly"), ("D", "daily"))))
        rows.append([", ".join([record["short_name"], *record["documented_names"]]),
                     record["cds_variable"], str(record["parameter_id"]), *flags])
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for index, row in enumerate(rows):
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
        if index == 0:
            print("  ".join("-" * width for width in widths))
    print(f"{len(records)} variable/parameter records")
