"""Small adapter around ECMWF's public job and results APIs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .config import Credentials
from .manifest import validate_filename
from .naming import filename_from_request
from .transfer import download_file


class CDSError(RuntimeError):
    """A CDS operation failed; messages never include keys or signed URLs."""


@dataclass(frozen=True)
class Job:
    job_id: str
    status: str
    dataset: str
    request: dict[str, Any]

    @property
    def filename(self) -> str:
        return filename_from_request(self.request, self.job_id)


class ERA5Client:
    def __init__(self, credentials: Credentials, *, timeout: float = 60,
                 retries: int = 5, backend: Any = None):
        self.account = credentials.name
        self.timeout = timeout
        self.retries = retries
        if backend is None:
            from ecmwf.datastores import Client
            backend = Client(url=credentials.url, key=credentials.key,
                             timeout=timeout, maximum_tries=retries + 1,
                             retry_after=1, progress=False, cleanup=False,
                             log_callback=lambda *args, **kwargs: None)
        self.backend = backend

    @staticmethod
    def _failure(operation: str, exc: Exception) -> CDSError:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f" (HTTP {status})" if isinstance(status, int) else ""
        return CDSError(f"CDS {operation} failed{detail}; check credentials, job availability, "
                        "and network connection")

    def submit_job(self, dataset: str, request: Mapping[str, Any]) -> Job:
        """Submit a new request; the returned job's status is not yet final."""
        if not isinstance(dataset, str) or not dataset.strip():
            raise ValueError("Dataset must be a non-empty string")
        if not isinstance(request, Mapping):
            raise TypeError("Request must be a mapping")
        try:
            remote = self.backend.submit(dataset, dict(request))
            return Job(remote.request_id, remote.status, dataset, dict(request))
        except Exception as exc:
            raise self._failure("job submission", exc) from None

    def get_job(self, job_id: str) -> Job:
        try:
            remote = self.backend.get_remote(job_id)
            return Job(remote.request_id, remote.status, remote.collection_id, remote.request)
        except Exception as exc:
            raise self._failure("job lookup", exc) from None

    def iter_jobs(self, *, status: str | None = None, limit: int = 1000) -> Iterator[Job]:
        """Visit paginated jobs, limiting the total number returned per account."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        try:
            page = self.backend.get_jobs(limit=min(limit, 100), sortby="-created", status=status)
            seen = set()
            while page is not None:
                for job_id in page.request_ids:
                    if job_id in seen:
                        continue
                    seen.add(job_id)
                    yield self.get_job(job_id)
                    if len(seen) >= limit:
                        return
                page = page.next
        except CDSError:
            raise
        except Exception as exc:
            raise self._failure("job listing", exc) from None

    def get_result_url(self, job: Job | str) -> str | None:
        """Return a successful job's temporary result URL, or None if not ready."""
        if isinstance(job, str):
            job = self.get_job(job)
        if job.status != "successful":
            return None
        try:
            return self.backend.get_results(job.job_id).location
        except Exception as exc:
            raise self._failure("results lookup", exc) from None

    def download_job(self, job: Job | str, output_dir: str | Path = ".", *,
                     filename: str | None = None, overwrite: bool = False,
                     progress: bool = True) -> Path | None:
        """Download a successful job; return None for a job that is not ready.

        A reservation and any partial bytes are shared by workers that use the
        same output directory and filename. No new CDS job is submitted.
        """
        if isinstance(job, str):
            job = self.get_job(job)
        if job.status != "successful":
            return None
        root = Path(output_dir).expanduser().resolve()
        target = root / validate_filename(filename or job.filename)
        # Check the auxiliary files as well as the final path for symlink escapes.
        for candidate in (target, Path(str(target) + ".part"), Path(str(target) + ".lock")):
            if not candidate.resolve().is_relative_to(root):
                raise ValueError("Download path must stay inside the output directory")
        try:
            results = self.backend.get_results(job.job_id)
            location = results.location
            size = results.content_length
        except Exception as exc:
            raise self._failure("results lookup", exc) from None
        return download_file(location, target, expected_size=size, overwrite=overwrite,
                             timeout=self.timeout, retries=self.retries, progress=progress)
