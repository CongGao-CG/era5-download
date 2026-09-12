import re
import unittest

from era5_download.naming import filename_from_request, generate_flist


class FilenameTests(unittest.TestCase):
    def test_reference_filename_and_scalar_values(self):
        request = {"variable": ["u_component_of_wind"], "pressure_level": ["200"],
                   "year": ["2020"], "month": ["01"]}
        self.assertEqual(filename_from_request(request), "u200_202001.nc")
        self.assertEqual(filename_from_request({key: value[0] for key, value in request.items()}),
                         "u200_202001.nc")

    def test_reference_field_aliases_and_surface_levels(self):
        for variable, level, expected in (
            ("relative_humidity", 600, "rh600_202001.nc"),
            ("vorticity", 850, "vo850_202001.nc"),
            ("v_component_of_wind", "250 hPa", "v250_202001.nc"),
            ("sea_surface_temperature", None, "sst_202001.nc"),
            ("land_sea_mask", None, "lsm_202001.nc"),
        ):
            with self.subTest(variable=variable):
                self.assertEqual(filename_from_request({"variable": variable, "level": level,
                                                        "year": 2020, "month": 1}), expected)

    def test_all_multiple_main_selections_are_visible_and_hashed(self):
        basic = {"variable": "u_component_of_wind", "pressure_level": "200",
                 "year": "2020", "month": "01"}
        for key, values, marker in (
            ("variable", ["u_component_of_wind", "v_component_of_wind"], "multivar"),
            ("pressure_level", ["200", "850"], "multilevel"),
            ("year", ["2020", "2021"], "multiyear"),
            ("month", ["01", "02"], "multimonth"),
        ):
            with self.subTest(key=key):
                name = filename_from_request({**basic, key: values})
                self.assertIn(marker, name)
                self.assertRegex(name, r"-[a-f0-9]{12}\.nc$")
                self.assertNotEqual(name, filename_from_request(basic))

    def test_request_hash_is_deterministic_and_distinguishes_subsets(self):
        request = {"variable": "u_component_of_wind", "year": 2020, "month": 1,
                   "day": ["01", "02"], "time": ["00:00"]}
        self.assertEqual(filename_from_request(request),
                         filename_from_request(dict(reversed(list(request.items())))))
        self.assertNotEqual(filename_from_request(request),
                            filename_from_request({**request, "day": ["03", "04"]}))
        self.assertNotEqual(filename_from_request({**request, "area": [90, 0, 0, 180]}),
                            filename_from_request({**request, "area": [45, 0, 0, 180]}))

    def test_job_ids_differentiate_repeated_requests(self):
        request = {"variable": "sea_surface_temperature", "year": 2020, "month": 1}
        one = filename_from_request(request, "job-one")
        two = filename_from_request(request, "job-two")
        self.assertNotEqual(one, two)
        self.assertRegex(one, r"^sst_202001-[a-f0-9]{16}\.nc$")
        self.assertEqual(one, filename_from_request(request, "job-one"))

    def test_extensions_reflect_format_and_archive(self):
        for options, extension in (
            ({"data_format": "grib"}, ".grib"),
            ({"format": ["grib2"]}, ".grib"),
            ({"data_format": "netcdf"}, ".nc"),
            ({"data_format": "netcdf", "download_format": "zip"}, ".zip"),
            ({"format": "netcdf.zip"}, ".zip"),
            ({"data_format": "unknown"}, ".nc"),
            ({"download_format": "unarchived"}, ".nc"),
        ):
            with self.subTest(options=options):
                self.assertTrue(filename_from_request(options).endswith(extension))

    def test_untrusted_request_values_produce_bounded_safe_basename(self):
        name = filename_from_request({"variable": "../../\\evil:" + "a" * 1000,
                                      "level": "../../\\etc/passwd",
                                      "year": "../2020", "month": "\\01"},
                                     job_id="../../\\job-id")
        self.assertLessEqual(len(name), 180)
        self.assertTrue(re.fullmatch(r"[a-z0-9_-]+\.(nc|grib|zip)", name))

    def test_missing_and_empty_values_never_add_none(self):
        self.assertEqual(filename_from_request({}), "era5.nc")
        self.assertEqual(filename_from_request({"variable": ["sea_surface_temperature"],
                                               "pressure_level": [None]}), "sst.nc")
        with self.assertRaises(ValueError):
            filename_from_request({}, job_id="")
        with self.assertRaises(TypeError):
            filename_from_request(None)


class FileListTests(unittest.TestCase):
    def test_end_year_is_exclusive_and_field_order_is_preserved(self):
        names = generate_flist(["u200", "sst"], (2020, 1), (2022, 1))
        self.assertEqual(len(names), 48)
        self.assertEqual(names[0], "u200_202001.nc")
        self.assertEqual(names[23], "u200_202112.nc")
        self.assertEqual(names[24], "sst_202001.nc")
        self.assertEqual(names[-1], "sst_202112.nc")

    def test_single_month_range(self):
        self.assertEqual(generate_flist(["sst"], (1996, 5), (1996, 6)), ["sst_199605.nc"])

    def test_empty_range_and_empty_fields(self):
        self.assertEqual(generate_flist(["sst"], (2020, 1), (2020, 1)), [])
        self.assertEqual(generate_flist([], (2020, 1), (2021, 1)), [])

    def test_invalid_fields_and_years_fail_early(self):
        for fields in ("sst", None, ["../sst"], ["a/b"], [""], [None]):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                generate_flist(fields, (2020, 1), (2021, 1))
        for begin, end in (((2021, 1), (2020, 1)), ((0, 1), (2021, 1)), ((2020, 1), (2020, 13)),
                           ((True, 1), (2021, 1)), ((2020.0, 1), (2021, 1)), ((2020, "1"), (2021, 1)),
                           (2020, (2021, 1)), ((2020, 1, 1), (2021, 1))):
            with self.subTest(begin=begin, end=end), self.assertRaises(ValueError):
                generate_flist(["sst"], begin, end)


if __name__ == "__main__":
    unittest.main()
