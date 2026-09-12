"""User-facing offline catalogue search and filtering."""

import io
import json
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from era5_download.cli import main
from era5_download.variables import find_variables


class VariablesTests(unittest.TestCase):
    def run_cli(self, *arguments):
        output, error = io.StringIO(), io.StringIO()
        with (patch("era5_download.cli.load_accounts") as accounts,
              patch("era5_download.cli.ERA5Client") as client,
              redirect_stdout(output), redirect_stderr(error)):
            status = main(["variables", *arguments])
        accounts.assert_not_called()
        client.assert_not_called()
        return status, output.getvalue(), error.getvalue()

    def test_complete_table_and_legend_without_credentials(self):
        status, output, error = self.run_cli()
        self.assertEqual(status, 0)
        self.assertEqual(error, "")
        self.assertIn("H=hourly", output)
        self.assertIn("GRIB SHORT NAME(S)", output)
        self.assertIn("glacier_mask", output)
        self.assertIn("290 variable/parameter records", output)

    def test_dataset_alias_and_raw_id_filter_identically(self):
        rows = find_variables(datasets=["era5_hourly_pressure"])
        self.assertEqual(len(rows), 16)
        self.assertEqual(rows, find_variables(datasets=["reanalysis-era5-pressure-levels"]))
        self.assertTrue(all(row['datasets'] == ['era5_hourly_pressure'] for row in rows))

    def test_text_search_is_case_insensitive_and_all_terms_must_match(self):
        rows = find_variables(search=["SEA SURFACE", "temperature"])
        self.assertEqual([row['short_name'] for row in rows], ['sst'])
        self.assertEqual(rows, find_variables(search=['sea_surface_temperature']))

    def test_search_matches_current_documented_names_and_parameter_ids(self):
        for query in ('avg_tprate', 'mtpr', '235055'):
            rows = find_variables(search=[query])
            self.assertEqual([row['cds_variable'] for row in rows], ['mean_total_precipitation_rate'])

    def test_combined_family_frequency_and_search(self):
        status, output, _ = self.run_cli('--family', 'era5-land', '--frequency', 'daily',
                                         '--search', 'snow', '--json')
        self.assertEqual(status, 0)
        rows = json.loads(output)
        self.assertTrue(rows)
        self.assertTrue(all(row['datasets'] == ['era5land_daily'] for row in rows))
        self.assertTrue(all('snow' in row['cds_variable'] for row in rows))

    def test_repeated_datasets_are_unioned_and_preserve_dataset_specific_names(self):
        status, output, _ = self.run_cli('evaporation', '--dataset', 'era5_hourly_single',
                                        '--dataset', 'era5land_hourly', '--json')
        self.assertEqual(status, 0)
        rows = [row for row in json.loads(output) if row['short_name'] == 'e']
        self.assertEqual({row['cds_variable'] for row in rows}, {'evaporation', 'total_evaporation'})
        self.assertTrue(all(len(row['datasets']) == 1 for row in rows))

    def test_no_matches_is_a_successful_empty_result(self):
        status, output, _ = self.run_cli('--search', 'no_such_variable')
        self.assertEqual(status, 0)
        self.assertIn('No variables match', output)
        status, output, _ = self.run_cli('no_such_variable', '--json')
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output), [])

    def test_unavailable_dataset_has_actionable_error(self):
        status, _, error = self.run_cli('--dataset', 'era5_complete_mars')
        self.assertEqual(status, 1)
        self.assertIn('No bundled variable catalogue', error)

    def test_table_flags_reflect_the_filtered_datasets(self):
        _, output, _ = self.run_cli('10u', '--dataset', 'era5land_hourly')
        row = next(line for line in output.splitlines() if '10m_u_component_of_wind' in line)
        self.assertEqual(row.split()[-3:], ['---', '---', 'H--'])
