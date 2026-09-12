"""Coverage against a separately captured public CDS variable snapshot."""
import json
from pathlib import Path
import unittest

from era5_download.catalogue import DATASET_VARIABLES, DOCUMENTED_GRIB_NAMES, VARIABLE_CATALOGUE
from era5_download.mappings import DATASET_MAPPING, resolve_variables
from era5_download.requests import build_template


class CatalogueTests(unittest.TestCase):
    def test_every_variable_in_each_cds_schema_is_mapped(self):
        snapshot = json.loads(Path(__file__).with_name('cds_variables.json').read_text())['collections']
        self.assertEqual(set(DATASET_VARIABLES), set(snapshot))
        for dataset, record in snapshot.items():
            with self.subTest(dataset=dataset):
                self.assertEqual(set(DATASET_VARIABLES[dataset].values()), set(record['variables']))
        self.assertEqual(len(set().union(*(set(r['variables']) for r in snapshot.values()))), 289)

    def test_every_mapped_variable_builds_a_request_for_its_dataset(self):
        for dataset, variables in DATASET_VARIABLES.items():
            levels = ['500'] if 'pressure-levels' in dataset else None
            for short, cds_name in variables.items():
                with self.subTest(dataset=dataset, short=short):
                    request = build_template(dataset, variables=[short], pressure_levels=levels)
                    self.assertEqual(request['variable'], [cds_name])

    def test_alias_and_raw_dataset_id_select_the_same_variable_map(self):
        for alias, dataset in DATASET_MAPPING.items():
            if dataset in DATASET_VARIABLES:
                for short in DATASET_VARIABLES[dataset]:
                    self.assertEqual(resolve_variables([short], alias), resolve_variables([short], dataset))

    def test_same_grib_name_can_have_different_cds_names(self):
        cases = [('e', 'evaporation', 'total_evaporation'),
                 ('sd', 'snow_depth', 'snow_depth_water_equivalent'),
                 ('dl', 'lake_depth', 'lake_total_depth')]
        for short, era5, land in cases:
            self.assertEqual(resolve_variables([short], 'era5_hourly_single'), ([era5], []))
            self.assertEqual(resolve_variables([short], 'era5land_hourly'), ([land], []))
            with self.assertRaisesRegex(ValueError, 'requires a dataset'):
                resolve_variables([short])
        self.assertEqual(resolve_variables(['sde'], 'era5land_hourly'), (['snow_depth'], []))

    def test_unavailable_variables_are_rejected_for_the_selected_dataset(self):
        for dataset, short in [('era5land_hourly', 'sst'), ('era5_hourly_pressure', '2t'),
                               ('era5land_daily', 'tp'), ('era5_monthly_single', '10fg')]:
            with self.subTest(dataset=dataset, short=short), self.assertRaisesRegex(ValueError, 'not available'):
                resolve_variables([short], dataset)

    def test_new_variable_families_and_known_documentation_corrections(self):
        for short, variable in [('cape','convective_available_potential_energy'),
                                ('mwd2','mean_wave_direction_of_second_swell_partition'),
                                ('p2ps','mean_wave_period_based_on_second_moment_for_swell')]:
            self.assertEqual(resolve_variables([short], 'era5_hourly_single'), ([variable], []))
        self.assertEqual(resolve_variables(['glm', '10u', 'evabs'], 'era5land_hourly'),
                         (['glacier_mask', '10m_u_component_of_wind', 'evaporation_from_bare_soil'], []))
        self.assertEqual(resolve_variables(['pv', 'o3'], 'era5_hourly_pressure'),
                         (['potential_vorticity', 'ozone_mass_mixing_ratio'], []))

    def test_documented_and_current_official_names_select_the_same_data(self):
        for documented, current in DOCUMENTED_GRIB_NAMES.items():
            for dataset, selections in DATASET_VARIABLES.items():
                if current in selections:
                    self.assertEqual(resolve_variables([documented], dataset), resolve_variables([current], dataset))

    def test_catalogue_has_no_conflicting_short_names_within_a_dataset(self):
        assignments = {}
        for short, variable, param_id, datasets in VARIABLE_CATALOGUE:
            self.assertIsInstance(param_id, int)
            for dataset in datasets:
                self.assertEqual(assignments.setdefault((dataset, short), variable), variable)
