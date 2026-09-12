"""Offline coverage of official GRIB short-name selection."""
import unittest
from era5_download.mappings import resolve_variables


class ResolveVariablesTests(unittest.TestCase):
    def test_supported_official_names(self):
        expected = {
            "10u": "10m_u_component_of_wind", "10v": "10m_v_component_of_wind",
            "2t": "2m_temperature", "2d": "2m_dewpoint_temperature",
            "sst": "sea_surface_temperature", "lsm": "land_sea_mask",
            "u": "u_component_of_wind", "v": "v_component_of_wind",
            "t": "temperature", "z": "geopotential", "q": "specific_humidity",
            "w": "vertical_velocity", "d": "divergence", "r": "relative_humidity",
            "vo": "vorticity", "sp": "surface_pressure",
            "msl": "mean_sea_level_pressure", "tp": "total_precipitation",
        }
        for name, variable in expected.items():
            with self.subTest(name=name):
                self.assertEqual(resolve_variables([name]), ([variable], []))

    def test_nonofficial_aliases_full_names_and_unknown_names_rejected(self):
        for name in ("u10", "v10", "t2m", "d2m", "sstk", "rh", "u200",
                     "v850", "t500", "r600", "vo850", "u010", "SST", "10U",
                     "sea_surface_temperature", "10m_u_component_of_wind", "unknown"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "official ECMWF GRIB"):
                resolve_variables([name])

    def test_repeated_and_comma_selections_preserve_order_without_duplicates(self):
        self.assertEqual(resolve_variables([" sst,2t ", "sst", "10u,2t"]),
                         (["sea_surface_temperature", "2m_temperature", "10m_u_component_of_wind"], []))

    def test_invalid_variable_values(self):
        for values in ("sst", b"sst", None, 5, [], [""], [" "], [None], [42],
                       ["sst,"], [",sst"], ["sst,,2t"]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                resolve_variables(values)


if __name__ == "__main__":
    unittest.main()
