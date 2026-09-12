import copy
import unittest

from era5_download.mappings import DATASET_MAPPING
from era5_download.requests import build_template, monthly_request


HOURS = [f"{hour:02d}:00" for hour in range(24)]
PRESETS = (
    ("era5_hourly_single", "sst", "sea_surface_temperature", None),
    ("era5_hourly_pressure", "u", "u_component_of_wind", ["200"]),
    ("era5_monthly_single", "sst", "sea_surface_temperature", None),
    ("era5_monthly_pressure", "u", "u_component_of_wind", ["200"]),
    ("era5_daily_single", "2t", "2m_temperature", None),
    ("era5_daily_pressure", "u", "u_component_of_wind", ["200"]),
    ("era5land_hourly", "2t", "2m_temperature", None),
    ("era5land_monthly", "2t", "2m_temperature", None),
    ("era5land_daily", "2t", "2m_temperature", None),
)


class RequestTemplateTests(unittest.TestCase):
    def test_all_nine_presets_use_their_cds_request_fields(self):
        for dataset, variable, full_name, levels in PRESETS:
            with self.subTest(dataset=dataset):
                request = build_template(dataset, variables=[variable], pressure_levels=levels)
                self.assertEqual(request["variable"], [full_name])
                self.assertNotIn("year", request)
                self.assertNotIn("month", request)
                if levels:
                    self.assertEqual(request["pressure_level"], levels)
                else:
                    self.assertNotIn("pressure_level", request)
                if "daily" in dataset:
                    self.assertEqual(request["daily_statistic"], "daily_mean")
                    self.assertEqual(request["time_zone"], "utc+00:00")
                    self.assertEqual(request["frequency"], "1_hourly")
                    for key in ("time", "data_format", "download_format"):
                        self.assertNotIn(key, request)
                    if dataset == "era5land_daily":
                        self.assertNotIn("product_type", request)
                    else:
                        self.assertEqual(request["product_type"], "reanalysis")
                else:
                    self.assertEqual(request["data_format"], "netcdf")
                    self.assertEqual(request["download_format"], "unarchived")
                    self.assertEqual(request["time"], ["00:00"] if "monthly" in dataset else HOURS)
                    if "monthly" in dataset:
                        self.assertEqual(request["product_type"], ["monthly_averaged_reanalysis"])
                    elif dataset == "era5land_hourly":
                        self.assertNotIn("product_type", request)
                    else:
                        self.assertEqual(request["product_type"], ["reanalysis"])

    def test_raw_collection_ids_get_the_same_presets_as_aliases(self):
        for dataset, variable, _, levels in PRESETS:
            with self.subTest(dataset=dataset):
                self.assertEqual(
                    build_template(DATASET_MAPPING[dataset], variables=[variable], pressure_levels=levels),
                    build_template(dataset, variables=[variable], pressure_levels=levels),
                )

    def test_full_cds_variable_names_remain_available_in_templates(self):
        request = build_template("era5_hourly_single", {"variable": ["boundary_layer_height"]})
        self.assertEqual(request["variable"], ["boundary_layer_height"])

    def test_explicit_template_is_preserved_without_preset_defaults(self):
        template = {"variable": "sea_surface_temperature", "format": "grib",
                    "area": [60, -20, 10, 40], "custom_option": {"nested": [1, 2]}}
        original = copy.deepcopy(template)
        request = build_template("era5_monthly_single", template)
        self.assertEqual(request, original)
        self.assertEqual(template, original)

    def test_cli_variables_replace_template_variables(self):
        template = {"variable": ["2m_temperature"], "time": ["12:00"]}
        request = build_template("era5_hourly_single", template, variables=["sst"])
        self.assertEqual(request, {"variable": ["sea_surface_temperature"], "time": ["12:00"]})
        self.assertEqual(template["variable"], ["2m_temperature"])

    def test_explicit_options_replace_template_options(self):
        template = {"variable": ["sea_surface_temperature"], "area": [90, -180, -90, 180],
                    "data_format": "netcdf", "download_format": "unarchived"}
        options = {"area": [50, -10, 40, 10], "data_format": "grib", "download_format": "zip"}
        request = build_template("era5_monthly_single", template, options=options)
        self.assertEqual(request, {**template, **options})
        self.assertEqual(template["data_format"], "netcdf")

    def test_explicit_options_replace_generated_defaults(self):
        options = {"area": [50, -10, 40, 10], "time": ["06:00", "18:00"],
                   "data_format": "grib", "download_format": "zip"}
        request = build_template("era5_hourly_single", variables=["sst"], options=options)
        for key, value in options.items():
            self.assertEqual(request[key], value)

    def test_daily_statistic_options_override_the_default_mean(self):
        options = {"daily_statistic": "daily_maximum", "time_zone": "utc+03:00", "frequency": "3_hourly"}
        request = build_template("era5land_daily", variables=["2t"], options=options)
        for key, value in options.items():
            self.assertEqual(request[key], value)

    def test_pressure_variables_can_share_an_explicit_level(self):
        request = build_template("era5_monthly_pressure", variables=["u", "v"], pressure_levels=["200"])
        self.assertEqual(request["variable"], ["u_component_of_wind", "v_component_of_wind"])
        self.assertEqual(request["pressure_level"], ["200"])

    def test_explicit_levels_apply_to_every_requested_pressure_variable(self):
        request = build_template("era5_hourly_pressure", variables=["u", "v"],
                                 pressure_levels=["200", "850"])
        self.assertEqual(request["variable"], ["u_component_of_wind", "v_component_of_wind"])
        self.assertEqual(request["pressure_level"], ["200", "850"])

    def test_template_can_supply_pressure_levels_for_cli_variables(self):
        template = {"variable": ["temperature"], "pressure_level": ["200"], "time": ["00:00"]}
        request = build_template("era5_monthly_pressure", template, variables=["u"])
        self.assertEqual(request["variable"], ["u_component_of_wind"])
        self.assertEqual(request["pressure_level"], ["200"])
        self.assertNotIn("product_type", request)

    def test_non_grib_names_are_rejected_even_with_pressure_levels(self):
        for name in ("u200", "v850", "temperature", "sstk", "rh"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "official ECMWF GRIB"):
                build_template("era5_monthly_pressure", variables=[name], pressure_levels=["850"])

    def test_single_level_and_land_presets_reject_pressure_selections(self):
        for dataset in ("era5_hourly_single", "era5_monthly_single", "era5_daily_single",
                        "era5land_hourly", "era5land_monthly", "era5land_daily"):
            with self.subTest(dataset=dataset):
                with self.assertRaises(ValueError):
                    build_template(dataset, variables=["u"], pressure_levels=["200"])
                with self.assertRaises(ValueError):
                    build_template(dataset, variables=["2t"], pressure_levels=["200"])

    def test_presets_require_variables_and_pressure_presets_require_levels(self):
        for dataset, _, _, _ in PRESETS:
            with self.subTest(dataset=dataset), self.assertRaises(ValueError):
                build_template(dataset)
        for dataset in ("era5_hourly_pressure", "era5_monthly_pressure", "era5_daily_pressure"):
            with self.subTest(dataset=dataset), self.assertRaises(ValueError):
                build_template(dataset, variables=["t"])

    def test_templates_must_leave_year_and_month_to_the_batcher(self):
        for key, value in (("year", ["2020"]), ("month", ["01"])):
            with self.subTest(key=key), self.assertRaises(ValueError):
                build_template("era5_monthly_single", {"variable": "sea_surface_temperature", key: value})

    def test_unknown_collection_requires_an_explicit_template(self):
        with self.assertRaises(ValueError):
            build_template("custom-cds-collection", variables=["t"])
        template = {"variable": ["temperature"], "custom_setting": "value"}
        self.assertEqual(build_template("custom-cds-collection", template), template)

    def test_known_specialized_collections_reject_year_month_submission(self):
        for dataset in ("era5_single_timeseries", "era5land_timeseries", "era5_complete_mars"):
            for name in (dataset, DATASET_MAPPING[dataset]):
                with self.subTest(dataset=name), self.assertRaises(ValueError):
                    build_template(name, {"variable": ["temperature"]})


class MonthlyRequestTests(unittest.TestCase):
    def test_each_family_uses_its_official_year_month_shapes(self):
        for dataset, variable, _, levels in PRESETS:
            with self.subTest(dataset=dataset):
                template = build_template(dataset, variables=[variable], pressure_levels=levels)
                request = monthly_request(dataset, template, 2020, 2)
                year = "2020" if "daily" in dataset or dataset == "era5land_hourly" else ["2020"]
                month = "02" if dataset in ("era5land_hourly", "era5land_daily") else ["02"]
                self.assertEqual(request["year"], year)
                self.assertEqual(request["month"], month)
                self.assertNotIn("year", template)
                self.assertNotIn("month", template)

    def test_generated_hourly_and_daily_requests_use_calendar_month_days(self):
        for dataset in ("era5_hourly_single", "era5_daily_single", "era5land_hourly", "era5land_daily"):
            template = build_template(dataset, variables=["2t"])
            for year, month, days in ((2020, 2, 29), (2021, 2, 28), (2020, 4, 30), (2020, 1, 31)):
                with self.subTest(dataset=dataset, year=year, month=month):
                    request = monthly_request(dataset, template, year, month, fill_days=True)
                    self.assertEqual(request["day"], [f"{day:02d}" for day in range(1, days + 1)])
                    self.assertNotIn("day", template)

    def test_explicit_day_selection_survives_calendar_filling(self):
        template = build_template("era5_hourly_single", variables=["2t"], options={"day": ["01", "15"]})
        request = monthly_request("era5_hourly_single", template, 2020, 2, fill_days=True)
        self.assertEqual(request["day"], ["01", "15"])

    def test_no_implicit_day_selection_for_templates_or_monthly_products(self):
        template = {"variable": ["2m_temperature"], "time": ["00:00"]}
        request = monthly_request("era5_hourly_single", template, 2020, 2)
        self.assertNotIn("day", request)
        for dataset in ("era5_monthly_single", "era5_monthly_pressure", "era5land_monthly"):
            with self.subTest(dataset=dataset):
                self.assertNotIn("day", monthly_request(dataset, template, 2020, 2, fill_days=True))

    def test_unknown_collection_preserves_original_list_based_batching(self):
        template = {"variable": ["custom_field"], "custom_setting": "value"}
        self.assertEqual(monthly_request("custom-cds-collection", template, 2020, 2),
                         {**template, "year": ["2020"], "month": ["02"]})


if __name__ == "__main__":
    unittest.main()
