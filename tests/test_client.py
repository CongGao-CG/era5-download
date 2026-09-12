"""Credential-free coverage of the CDS client adapter using a fake backend."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from era5_download.client import CDSError, ERA5Client, Job
from era5_download.config import Credentials


class FakeBackend:
    def __init__(self, remote):
        self.remote = remote
        self.calls = []

    def submit(self, dataset, request):
        self.calls.append((dataset, request))
        return self.remote


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.credentials = Credentials("test-account", "test-key", "https://example.test/api")

    def client(self, backend, **kwargs):
        return ERA5Client(self.credentials, backend=backend, **kwargs)

    def remote(self, job_id="job-123", status="successful"):
        return SimpleNamespace(
            request_id=job_id,
            status=status,
            collection_id="reanalysis-era5-single-levels",
            request={"variable": "sea_surface_temperature", "year": ["2020"], "month": ["01"]},
        )

    def output_dir(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name).resolve()

    def test_submit_job_returns_job_from_remote(self):
        remote = SimpleNamespace(request_id="job-123", status="accepted")
        backend = FakeBackend(remote)
        client = self.client(backend)
        request = {"variable": "sea_surface_temperature", "year": "2020", "month": "01"}
        job = client.submit_job("reanalysis-era5-single-levels", request)
        self.assertEqual(job.job_id, "job-123")
        self.assertEqual(job.status, "accepted")
        self.assertEqual(job.dataset, "reanalysis-era5-single-levels")
        self.assertEqual(job.request, request)
        self.assertEqual(backend.calls, [("reanalysis-era5-single-levels", request)])

    def test_submit_job_does_not_mutate_caller_request(self):
        remote = SimpleNamespace(request_id="job-123", status="accepted")
        backend = FakeBackend(remote)
        client = self.client(backend)
        request = {"variable": "sea_surface_temperature"}
        client.submit_job("dataset", request)
        backend.calls[0][1]["variable"] = "changed"
        self.assertEqual(request, {"variable": "sea_surface_temperature"})

    def test_submit_job_wraps_backend_errors(self):
        class FailingBackend:
            def submit(self, dataset, request):
                raise RuntimeError("boom")

        client = self.client(FailingBackend())
        with self.assertRaises(CDSError):
            client.submit_job("dataset", {})

    def test_submit_job_rejects_bad_arguments(self):
        client = self.client(FakeBackend(SimpleNamespace(request_id="x", status="accepted")))
        with self.assertRaises(ValueError):
            client.submit_job("", {})
        with self.assertRaises(TypeError):
            client.submit_job("dataset", ["not", "a", "mapping"])

    def test_get_job_preserves_remote_metadata(self):
        remote = self.remote(status="running")
        backend = SimpleNamespace(get_remote=Mock(return_value=remote))

        job = self.client(backend).get_job("job-123")

        self.assertEqual(job, Job("job-123", "running", remote.collection_id, remote.request))
        backend.get_remote.assert_called_once_with("job-123")

    def test_iter_jobs_paginates_deduplicates_and_stops_at_total_limit(self):
        # Repeated IDs can occur when the remote list changes between pages.
        last_page = SimpleNamespace(request_ids=["job-2", "job-3", "job-4"], next=None)
        first_page = SimpleNamespace(request_ids=["job-1", "job-2"], next=last_page)
        backend = SimpleNamespace(
            get_jobs=Mock(return_value=first_page),
            get_remote=Mock(side_effect=lambda job_id: self.remote(job_id)),
        )

        jobs = list(self.client(backend).iter_jobs(status="successful", limit=3))

        self.assertEqual([job.job_id for job in jobs], ["job-1", "job-2", "job-3"])
        backend.get_jobs.assert_called_once_with(limit=3, sortby="-created", status="successful")
        self.assertEqual(backend.get_remote.call_args_list,
                         [call("job-1"), call("job-2"), call("job-3")])

    def test_iter_jobs_caps_page_size_and_handles_empty_results(self):
        backend = SimpleNamespace(
            get_jobs=Mock(return_value=SimpleNamespace(request_ids=[], next=None)),
            get_remote=Mock(),
        )

        self.assertEqual(list(self.client(backend).iter_jobs(limit=150)), [])

        backend.get_jobs.assert_called_once_with(limit=100, sortby="-created", status=None)
        backend.get_remote.assert_not_called()

    def test_iter_jobs_rejects_nonpositive_limits_before_backend_calls(self):
        backend = SimpleNamespace(get_jobs=Mock())
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                list(self.client(backend).iter_jobs(limit=limit))
        backend.get_jobs.assert_not_called()

    def test_download_job_skips_non_successful_jobs_without_results_or_transfer(self):
        backend = SimpleNamespace(get_results=Mock())
        with patch("era5_download.client.download_file") as transfer:
            for status in ("accepted", "running", "failed", "rejected"):
                with self.subTest(status=status):
                    job = Job("job-123", status, "dataset", {})
                    self.assertIsNone(self.client(backend).download_job(job))
        backend.get_results.assert_not_called()
        transfer.assert_not_called()

    def test_download_job_id_uses_remote_filename_and_result_byte_count(self):
        remote = self.remote()
        location = "https://example.test/result?token=test-signed-token"
        backend = SimpleNamespace(
            get_remote=Mock(return_value=remote),
            get_results=Mock(return_value=SimpleNamespace(location=location, content_length=123)),
        )
        root = self.output_dir()
        target = root / "sst_202001-e3419f10c0ad67c5.nc"
        with patch("era5_download.client.download_file", return_value=target) as transfer:
            result = self.client(backend, timeout=12.5, retries=2).download_job("job-123", root)

        self.assertEqual(result, target)
        backend.get_remote.assert_called_once_with("job-123")
        backend.get_results.assert_called_once_with("job-123")
        transfer.assert_called_once_with(location, target, expected_size=123, overwrite=False,
                                         timeout=12.5, retries=2, progress=True)

    def test_download_job_forwards_explicit_filename_and_transfer_options(self):
        location = "https://example.test/result"
        backend = SimpleNamespace(
            get_results=Mock(return_value=SimpleNamespace(location=location, content_length=0)),
        )
        root = self.output_dir()
        target = root / "subdir" / "chosen.nc"
        job = Job("job-123", "successful", "dataset", {})
        with patch("era5_download.client.download_file", return_value=target) as transfer:
            result = self.client(backend).download_job(
                job, root, filename="subdir/chosen.nc", overwrite=True, progress=False,
            )

        self.assertEqual(result, target)
        backend.get_results.assert_called_once_with("job-123")
        transfer.assert_called_once_with(location, target, expected_size=0, overwrite=True,
                                         timeout=60, retries=5, progress=False)

    def test_download_job_rejects_unsafe_filenames_before_results_or_transfer(self):
        root = self.output_dir()
        backend = SimpleNamespace(get_results=Mock())
        job = Job("job-123", "successful", "dataset", {})
        with patch("era5_download.client.download_file") as transfer:
            for filename in ("../escape.nc", "subdir/../escape.nc", str(root / "absolute.nc")):
                with self.subTest(filename=filename), self.assertRaises(ValueError):
                    self.client(backend).download_job(job, root, filename=filename)
        backend.get_results.assert_not_called()
        transfer.assert_not_called()

    def test_download_job_rejects_final_and_auxiliary_symlink_escapes(self):
        root = self.output_dir()
        output = root / "downloads"
        output.mkdir()
        outside = root / "outside.nc"
        outside.write_bytes(b"untouched")
        backend = SimpleNamespace(get_results=Mock())
        job = Job("job-123", "successful", "dataset", {})
        with patch("era5_download.client.download_file") as transfer:
            for suffix in ("", ".part", ".lock"):
                with self.subTest(suffix=suffix):
                    candidate = output / ("data.nc" + suffix)
                    candidate.symlink_to(outside)
                    try:
                        with self.assertRaisesRegex(ValueError, "inside the output directory"):
                            self.client(backend).download_job(job, output, filename="data.nc")
                        self.assertTrue(candidate.is_symlink())
                        self.assertEqual(outside.read_bytes(), b"untouched")
                    finally:
                        candidate.unlink()
        backend.get_results.assert_not_called()
        transfer.assert_not_called()

    def test_backend_errors_hide_tokens_and_signed_urls_but_preserve_http_status(self):
        secret = "test-api-key https://example.test/result?token=test-signed-token"
        root = self.output_dir()
        for operation in ("submission", "lookup", "listing", "results"):
            with self.subTest(operation=operation):
                error = RuntimeError(secret)
                error.response = SimpleNamespace(status_code=403)
                backend = SimpleNamespace(
                    submit=Mock(side_effect=error),
                    get_remote=Mock(side_effect=error),
                    get_jobs=Mock(side_effect=error),
                    get_results=Mock(side_effect=error),
                )
                client = self.client(backend)
                with self.assertRaises(CDSError) as caught:
                    if operation == "submission":
                        client.submit_job("dataset", {})
                    elif operation == "lookup":
                        client.get_job("job-123")
                    elif operation == "listing":
                        list(client.iter_jobs())
                    else:
                        client.download_job(Job("job-123", "successful", "dataset", {}),
                                            root, filename="data.nc")
                self.assertIn("HTTP 403", str(caught.exception))
                self.assertNotIn("test-api-key", str(caught.exception))
                self.assertNotIn("https://", str(caught.exception))
                self.assertNotIn("test-signed-token", str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
