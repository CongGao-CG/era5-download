"""Credential-free coverage of the submit-cycle logic using fake clients."""

import json
import io
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from era5_download.cli import _submit_cycle, build_parser, main
from era5_download.manifest import ManifestEntry, append_manifest, read_manifest


class FakeClient:
    """Accepts every submission immediately; tracks what was submitted."""

    def __init__(self):
        self.submitted = []
        self._counter = 0

    def submit_job(self, dataset, request):
        self._counter += 1
        job_id = f"job-{self._counter}"
        self.submitted.append((dataset, dict(request), job_id))
        return SimpleNamespace(job_id=job_id)

    def get_job(self, job_id):
        return SimpleNamespace(status="successful")


class ThrottledClient(FakeClient):
    """Each job needs ``clears_after`` polls before it reports 'successful'."""

    def __init__(self, clears_after):
        super().__init__()
        self.clears_after = clears_after
        self._polls = {}

    def get_job(self, job_id):
        self._polls[job_id] = self._polls.get(job_id, 0) + 1
        status = "successful" if self._polls[job_id] >= self.clears_after else "running"
        return SimpleNamespace(status=status)


class SubmitCycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.template = self.root / "template.json"
        self.manifest = self.root / "manifest.csv"

    def write_template(self, content):
        self.template.write_text(json.dumps(content))

    def args(self, **overrides):
        base = dict(dataset="era5_monthly_pressure", template=self.template, begin=(2020, 1),
                    end=(2021, 1), manifest=self.manifest, max_in_flight=2, poll_interval=0, quiet=True)
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_submits_one_job_per_month_and_records_manifest(self):
        self.write_template({"variable": "vertical_velocity", "pressure_level": "500"})
        client = FakeClient()
        self.assertEqual(_submit_cycle(self.args(), {"acc": client}), 0)
        entries = read_manifest(self.manifest)
        self.assertEqual(len(entries), 12)
        self.assertEqual(entries[0].filename, "w500_202001.nc")
        self.assertEqual(entries[-1].filename, "w500_202012.nc")
        self.assertTrue(all(entry.account == "acc" for entry in entries))
        self.assertEqual(len(client.submitted), 12)
        self.assertEqual(client.submitted[0][0], "reanalysis-era5-pressure-levels-monthly-means")

    def test_skips_filenames_already_in_manifest(self):
        self.write_template({"variable": "vertical_velocity", "pressure_level": "500"})
        append_manifest(self.manifest, ManifestEntry("w500_202001.nc", "existing-job", "acc"))
        client = FakeClient()
        _submit_cycle(self.args(), {"acc": client})
        self.assertEqual(len(client.submitted), 11)
        self.assertEqual(len(read_manifest(self.manifest)), 12)

    def test_round_robins_across_accounts(self):
        self.write_template({"variable": "vertical_velocity", "pressure_level": "500"})
        clients = {"a": FakeClient(), "b": FakeClient()}
        _submit_cycle(self.args(), clients)
        entries = read_manifest(self.manifest)
        self.assertEqual([entry.account for entry in entries[:4]], ["a", "b", "a", "b"])

    def test_throttles_until_earlier_job_clears(self):
        self.write_template({"variable": "vertical_velocity", "pressure_level": "500"})
        client = ThrottledClient(clears_after=2)
        with patch("era5_download.cli.time.sleep") as sleep_mock:
            _submit_cycle(self.args(max_in_flight=1), {"acc": client})
        self.assertEqual(len(client.submitted), 12)
        self.assertEqual(sleep_mock.call_count, 11)

    def test_uses_raw_dataset_id_when_not_an_alias(self):
        self.write_template({"variable": "sst"})
        client = FakeClient()
        _submit_cycle(self.args(dataset="some-raw-collection-id"), {"acc": client})
        self.assertEqual(client.submitted[0][0], "some-raw-collection-id")

    def test_rejects_template_with_year_or_month(self):
        self.write_template({"variable": "sst", "year": ["2020"]})
        with self.assertRaisesRegex(ValueError, "must not include"):
            _submit_cycle(self.args(), {"acc": FakeClient()})

    def test_rejects_non_object_template(self):
        self.template.write_text(json.dumps(["not", "an", "object"]))
        with self.assertRaisesRegex(ValueError, "JSON object"):
            _submit_cycle(self.args(), {"acc": FakeClient()})

    def test_rejects_invalid_json(self):
        self.template.write_text("{not valid json")
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            _submit_cycle(self.args(), {"acc": FakeClient()})

    def test_requires_at_least_one_account(self):
        self.write_template({"variable": "sst"})
        with self.assertRaisesRegex(ValueError, "at least one account"):
            _submit_cycle(self.args(), {})

    def test_invalid_range_submits_nothing(self):
        self.write_template({"variable": "sea_surface_temperature"})
        for begin, end in (((2021, 1), (2020, 1)), ((0, 1), (2021, 1)), ((2020, 1), (10000, 1)),
                           ((10000, 1), (10000, 1)), ((2020, 13), (2021, 1))):
            with self.subTest(begin=begin, end=end):
                client = FakeClient()
                with self.assertRaises(ValueError):
                    _submit_cycle(self.args(begin=begin, end=end), {"acc": client})
                self.assertEqual(client.submitted, [])
                self.assertFalse(self.manifest.exists())

    def test_empty_range_submits_nothing(self):
        self.write_template({"variable": "sea_surface_temperature"})
        client = FakeClient()
        self.assertEqual(_submit_cycle(self.args(end=(2020, 1)), {"acc": client}), 0)
        self.assertEqual(client.submitted, [])
        self.assertFalse(self.manifest.exists())

    def test_single_month_submits_exactly_one_job(self):
        self.write_template({"variable": "vertical_velocity", "pressure_level": "500"})
        client = FakeClient()
        args = self.args(begin=(1996, 5), end=(1996, 6))
        self.assertEqual(_submit_cycle(args, {"acc": client}), 0)
        entries = read_manifest(self.manifest)
        self.assertEqual([e.filename for e in entries], ["w500_199605.nc"])
        self.assertEqual(len(client.submitted), 1)

    def test_manifest_without_account_rejects_named_account_before_submission(self):
        self.write_template({"variable": "vertical_velocity", "pressure_level": "500"})
        original = "filename,job_id\nw500_202001.nc,existing-job\n"
        self.manifest.write_text(original)
        client = FakeClient()
        with self.assertRaisesRegex(ValueError, "account"):
            _submit_cycle(self.args(), {"acc": client})
        self.assertEqual(client.submitted, [])
        self.assertEqual(self.manifest.read_text(), original)

    def test_resume_preserves_reordered_legacy_manifest(self):
        self.write_template({"variable": "vertical_velocity", "pressure_level": "500"})
        original = "acc,rid,fnm,note\nacc,existing-job,w500_202001.nc,keep"
        self.manifest.write_text(original)
        client = FakeClient()
        _submit_cycle(self.args(), {"acc": client})
        entries = read_manifest(self.manifest)
        self.assertEqual(len(entries), 12)
        self.assertEqual(entries[0], ManifestEntry("w500_202001.nc", "existing-job", "acc"))
        self.assertEqual(entries[1], ManifestEntry("w500_202002.nc", "job-1", "acc"))
        self.assertTrue(self.manifest.read_text().startswith(original))

    def test_submits_using_var_without_a_template(self):
        client = FakeClient()
        args = self.args(dataset="era5_monthly_single", template=None, variables=["sst"])
        self.assertEqual(_submit_cycle(args, {"acc": client}), 0)
        entries = read_manifest(self.manifest)
        self.assertEqual(len(entries), 12)
        for _, request, _ in client.submitted:
            self.assertEqual(request["variable"], ["sea_surface_temperature"])
        self.assertTrue(all(re.fullmatch(r"sst_2020\d{2}(-[a-f0-9]{12})?\.nc", e.filename)
                            for e in entries))

    def test_submits_using_var_and_pressure_level_without_a_template(self):
        client = FakeClient()
        args = self.args(dataset="era5_monthly_pressure", template=None,
                         variables=["u"], pressure_levels=["500"])
        self.assertEqual(_submit_cycle(args, {"acc": client}), 0)
        self.assertEqual(len(read_manifest(self.manifest)), 12)
        for _, request, _ in client.submitted:
            self.assertEqual(request["variable"], ["u_component_of_wind"])
            self.assertEqual(request["pressure_level"], ["500"])

    def test_var_missing_pressure_level_is_rejected_before_submission(self):
        client = FakeClient()
        args = self.args(dataset="era5_monthly_pressure", template=None, variables=["u"])
        with self.assertRaises(ValueError):
            _submit_cycle(args, {"acc": client})
        self.assertEqual(client.submitted, [])

    def test_var_overrides_only_the_templates_variable(self):
        self.write_template({"variable": ["2m_temperature"], "time": ["12:00"]})
        client = FakeClient()
        args = self.args(dataset="era5_hourly_single", variables=["sst"])
        _submit_cycle(args, {"acc": client})
        for _, request, _ in client.submitted:
            self.assertEqual(request["variable"], ["sea_surface_temperature"])
            self.assertEqual(request["time"], ["12:00"])

    def test_neither_var_nor_template_is_rejected(self):
        client = FakeClient()
        args = self.args(template=None)
        with self.assertRaisesRegex(ValueError, "Provide --var or --template"):
            _submit_cycle(args, {"acc": client})
        self.assertEqual(client.submitted, [])

    def test_nonofficial_variables_fail_before_submission(self):
        for name in ("sstk", "u10", "u200", "sea_surface_temperature"):
            client = FakeClient()
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "official ECMWF GRIB"):
                _submit_cycle(self.args(dataset="era5_monthly_single", template=None,
                                        variables=[name]), {"default": client})
            self.assertEqual(client.submitted, [])
            self.assertFalse(self.manifest.exists())


class CheckCommandTests(unittest.TestCase):
    def test_prints_account_credential_source_and_url(self):
        credential = SimpleNamespace(name="default", key_source="environment variable CDSAPI_KEY",
                                     url="https://cds.climate.copernicus.eu/api")
        output = io.StringIO()
        with patch("era5_download.cli.load_accounts", return_value=[credential]), redirect_stdout(output):
            result = main(["check"])
        self.assertEqual(result, 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(lines[0], "ACCOUNT\tCREDENTIALS\tURL")
        self.assertEqual(lines[1], "default\tenvironment variable CDSAPI_KEY\t"
                                   "https://cds.climate.copernicus.eu/api")

    def test_check_never_constructs_a_cds_client(self):
        credential = SimpleNamespace(name="default", key_source="environment variable CDSAPI_KEY",
                                     url="https://cds.climate.copernicus.eu/api")
        with (patch("era5_download.cli.load_accounts", return_value=[credential]),
              patch("era5_download.cli.ERA5Client") as client_cls, redirect_stdout(io.StringIO())):
            main(["check"])
        client_cls.assert_not_called()

    def test_check_forwards_account_selection_and_config(self):
        with (patch("era5_download.cli.load_accounts", return_value=[]) as accounts,
              redirect_stdout(io.StringIO())):
            main(["--config", "accounts.json", "--account", "secondary", "check"])
        accounts.assert_called_once_with(Path("accounts.json"), ["secondary"], False, None, None)



class DocumentedCLITests(unittest.TestCase):
    def test_multi_account_example_places_global_options_before_command(self):
        args = build_parser().parse_args([
            "--config", "accounts.json", "--all-accounts", "submit", "era5_monthly_pressure",
            "--template", "request.json", "--begin", "1980", "--end", "2026",
            "--manifest", "jobs.csv",
        ])
        self.assertTrue(args.all_accounts)
        self.assertEqual(args.command, "submit")
        self.assertEqual(args.begin, (1980, 1))
        self.assertEqual(args.end, (2026, 1))

    def test_var_example_needs_no_template(self):
        args = build_parser().parse_args([
            "submit", "era5_monthly_single", "--var", "sst", "--begin", "1980",
            "--end", "2026", "--manifest", "jobs.csv",
        ])
        self.assertEqual(args.command, "submit")
        self.assertEqual(args.variables, ["sst"])
        self.assertIsNone(args.template)
        self.assertIsNone(args.pressure_levels)

    def test_single_month_example_accepts_yyyymm(self):
        args = build_parser().parse_args([
            "submit", "era5_hourly_pressure", "--var", "u", "--pressure-level", "250",
            "--begin", "199605", "--end", "199606", "--manifest", "jobs.csv",
        ])
        self.assertEqual(args.begin, (1996, 5))
        self.assertEqual(args.end, (1996, 6))

    def test_yyyymmdd_is_accepted_and_the_day_is_validated_but_dropped(self):
        args = build_parser().parse_args([
            "filenames", "sst", "--begin", "19960115", "--end", "19960601",
        ])
        self.assertEqual(args.begin, (1996, 1))
        self.assertEqual(args.end, (1996, 6))
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["filenames", "sst", "--begin", "19960230", "--end", "19960601"])
        with self.assertRaises(SystemExit):
            build_parser().parse_args(["filenames", "sst", "--begin", "19961301", "--end", "19970101"])

    def test_filenames_example_is_offline_and_end_is_exclusive(self):
        output = io.StringIO()
        with patch("era5_download.cli.load_accounts") as accounts, redirect_stdout(output):
            result = main(["filenames", "u200", "sst", "--begin", "2020",
                           "--end", "2021"])
        self.assertEqual(result, 0)
        accounts.assert_not_called()
        names = output.getvalue().splitlines()
        self.assertEqual(len(names), 24)
        self.assertEqual(names[0], "u200_202001.nc")
        self.assertEqual(names[-1], "sst_202012.nc")

    def test_invalid_submit_range_reports_error_without_submitting(self):
        with TemporaryDirectory() as directory:
            template = Path(directory) / "request.json"
            template.write_text('{"variable": "sea_surface_temperature"}')
            client = FakeClient()
            error = io.StringIO()
            with (patch("era5_download.cli.load_accounts", return_value=[SimpleNamespace(name="default")]),
                  patch("era5_download.cli.ERA5Client", return_value=client), redirect_stderr(error)):
                result = main(["submit", "era5_monthly_single", "--template", str(template),
                               "--begin", "2021", "--end", "2020",
                               "--manifest", str(Path(directory) / "jobs.csv")])
            self.assertEqual(result, 1)
            self.assertIn("begin must not be after end", error.getvalue())
            self.assertEqual(client.submitted, [])


class ManifestJobStatusTests(unittest.TestCase):
    def test_download_urls_uses_only_filename_and_url(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "url.csv"
            source.write_text("filename,url,status,account\nsub/a.nc,https://example.test/a,failed,unknown\n"
                              "b.nc,,successful,unknown\n")
            with patch("era5_download.cli.load_accounts") as load, \
                 patch("era5_download.cli.download_file") as download, redirect_stdout(io.StringIO()):
                result = main(["download", "--urls-csv", str(source), "-o", directory, "--quiet"])
            self.assertEqual(result, 0)
            load.assert_not_called()
            download.assert_called_once_with("https://example.test/a", Path(directory).resolve() / "sub/a.nc",
                                             overwrite=False, timeout=60, retries=5, progress=False)

    def test_download_urls_validates_before_transfers(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "url.csv"
            for bad in ("../bad.nc,https://example.test/b", "a.nc,https://example.test/b",
                        "bad.nc,file:///tmp/data"):
                source.write_text("filename,url\na.nc,https://example.test/a\n" + bad + "\n")
                with patch("era5_download.cli.download_file") as download, redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["download", "--urls-csv", str(source)]), 1)
                download.assert_not_called()

    def test_download_urls_continues_after_failure(self):
        from era5_download.transfer import DownloadError
        with TemporaryDirectory() as directory:
            source = Path(directory) / "url.csv"
            source.write_text("filename,url\na.nc,https://example.test/a\nb.nc,https://example.test/b\n")
            with patch("era5_download.cli.download_file", side_effect=[DownloadError("private-url"), Path("b.nc")]) as download, \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["download", "--urls-csv", str(source)]), 1)
            self.assertEqual(download.call_count, 2)
            self.assertNotIn("private-url", stderr.getvalue())

    def test_urls_csv_preserves_manifest_and_records_status(self):
        import csv
        from unittest.mock import Mock
        with TemporaryDirectory() as directory:
            source = Path(directory) / "jobs.csv"
            output = Path(directory) / "url.csv"
            source.write_text("filename,job_id,account\na.nc,id1,qq\nb.nc,id2,qq\n")
            original = source.read_bytes()
            client = Mock()
            client.get_job.side_effect = [
                SimpleNamespace(job_id="id1", status="successful", dataset="era5"),
                SimpleNamespace(job_id="id2", status="running", dataset="era5")]
            client.get_result_url.side_effect = ["https://example.test/data?a=1,b=2", None]
            with patch("era5_download.cli.load_accounts", return_value=[SimpleNamespace(name="qq")]), \
                 patch("era5_download.cli.ERA5Client", return_value=client), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(main(["jobs", "--from-manifest", str(source),
                                       "--urls-csv", str(output)]), 0)
            with output.open(newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0], dict(filename="a.nc", job_id="id1", account="qq",
                                           status="successful", url="https://example.test/data?a=1,b=2"))
            self.assertEqual(rows[1]["url"], "")
            self.assertEqual(rows[1]["status"], "running")
            self.assertEqual(source.read_bytes(), original)
            with redirect_stderr(io.StringIO()), patch("era5_download.cli.load_accounts") as load:
                self.assertEqual(main(["jobs", "--urls-csv", str(output)]), 1)
            load.assert_not_called()

    def test_manifest_accounts_filenames_filter_and_export(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "jobs.csv"
            output = Path(directory) / "selected.csv"
            source.write_text("filename,job_id,account\ncustom.nc,id1,qq\nother.nc,id2,gmail\n")
            original = source.read_bytes()
            clients = {}

            def make_client(credential, **kwargs):
                from unittest.mock import Mock
                client = Mock()
                client.get_job.return_value = SimpleNamespace(
                    job_id="id1" if credential.name == "qq" else "id2",
                    status="running" if credential.name == "qq" else "successful",
                    dataset="era5", filename="generated.nc")
                clients[credential.name] = client
                return client

            for selection in ([], ["--account", "qq"]):
                credentials = [SimpleNamespace(name=name) for name in
                               (["qq"] if selection else ["qq", "gmail"])]
                with patch("era5_download.cli.load_accounts", return_value=credentials) as load, \
                     patch("era5_download.cli.ERA5Client", side_effect=make_client), \
                     redirect_stdout(io.StringIO()) as stdout:
                    result = main(["--config", "accounts.json", *selection, "jobs",
                                   "--from-manifest", str(source), "--status", "running",
                                   "--limit", "1", "--json"])
                self.assertEqual(result, 0)
                self.assertEqual(load.call_args.args[1], ["qq"] if selection else ["qq", "gmail"])
                rows = json.loads(stdout.getvalue())
                self.assertEqual([(r["filename"], r["status"]) for r in rows], [("custom.nc", "running")])
                clients["qq"].get_job.assert_called_once_with("id1")
                clients["qq"].iter_jobs.assert_not_called()
            with patch("era5_download.cli.load_accounts", return_value=credentials), \
                 patch("era5_download.cli.ERA5Client", side_effect=make_client), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(main(["jobs", "--from-manifest", str(source),
                                       "--manifest", str(output)]), 0)
            self.assertEqual(read_manifest(output), [ManifestEntry("custom.nc", "id1", "qq")])
            self.assertEqual(source.read_bytes(), original)

    def test_empty_manifest_needs_no_credentials(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / "empty.csv"
            source.write_text("filename,job_id,account\n")
            with patch("era5_download.cli.load_accounts") as load, redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(main(["jobs", "--from-manifest", str(source), "--json"]), 0)
            load.assert_not_called()
            self.assertEqual(json.loads(stdout.getvalue()), [])


if __name__ == "__main__":
    unittest.main()
