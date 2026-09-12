"""HTTP downloads with resumable partial files and verified completion."""

from __future__ import annotations

import math
import json
import os
import re
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from tqdm import tqdm


class DownloadError(RuntimeError):
    """A download failed; messages never include the remote URL or headers."""


class DownloadBusy(DownloadError):
    """Another worker has claimed this target, or a stale claim remains."""


class _RetryableDownloadError(DownloadError):
    pass


_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)", re.IGNORECASE)
_UNSATISFIED_RANGE = re.compile(r"bytes \*/(\d+)", re.IGNORECASE)
_RETRY_STATUS = {408, 429, 500, 502, 503, 504}
_CHUNK_SIZE = 1024 * 1024


@contextmanager
def _claim_target(target: Path):
    """Hold an exclusive reservation, removing only the lock this call created."""
    lock = target.with_name(target.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise DownloadBusy("The target is reserved by an existing .lock file.") from None
    try:
        metadata = {
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        with os.fdopen(descriptor, "w", closefd=False) as output:
            json.dump(metadata, output)
            output.write("\n")
        yield
    finally:
        try:
            current = lock.lstat()
            owned = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                lock.unlink()
        except FileNotFoundError:
            pass
        finally:
            os.close(descriptor)


def _content_length(headers: requests.structures.CaseInsensitiveDict) -> int | None:
    value = headers.get("Content-Length")
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]+", value.strip()):
        raise DownloadError("The server returned an invalid Content-Length.")
    return int(value)


def _validate_options(
    url: str, expected_size: int | None, timeout: float, retries: int, backoff: float
) -> None:
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except (TypeError, ValueError):
        valid_url = False
    if not valid_url:
        raise ValueError("A valid HTTP or HTTPS URL is required.")
    if expected_size is not None and (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
    ):
        raise ValueError("expected_size must be a nonnegative integer or None.")
    if isinstance(retries, bool) or not isinstance(retries, int) or retries < 0:
        raise ValueError("retries must be a nonnegative integer.")
    for name, value, allow_zero in (("timeout", timeout, False), ("backoff", backoff, True)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or (not allow_zero and value == 0)
        ):
            raise ValueError(f"{name} must be finite and {'nonnegative' if allow_zero else 'positive'}.")


def _transfer_once(
    session: requests.Session,
    url: str,
    partial: Path,
    *,
    expected_size: int | None,
    timeout: float,
    progress: bool,
    description: str,
) -> None:
    offset = partial.stat().st_size if partial.exists() else 0
    if expected_size is not None and offset > expected_size:
        offset = 0
    headers = {"Accept-Encoding": "identity"}
    if offset:
        headers["Range"] = f"bytes={offset}-"

    with session.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as response:
        status = response.status_code
        if status == 416:
            match = _UNSATISFIED_RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
            total = int(match[1]) if match else None
            if (
                total is not None
                and partial.exists()
                and partial.stat().st_size == total
                and offset == total
                and (expected_size is None or total == expected_size)
            ):
                return
            raise DownloadError("The server rejected the resume range; the partial file could not be verified.")
        if status in _RETRY_STATUS:
            raise _RetryableDownloadError(f"The server returned HTTP {status}.")
        if status not in {200, 206}:
            raise DownloadError(f"The server returned HTTP {status}.")
        if response.headers.get("Content-Encoding", "identity").strip().lower() not in {"", "identity"}:
            raise DownloadError("The server returned an encoded response despite requesting identity encoding.")

        content_length = _content_length(response.headers)
        if status == 206:
            match = _RANGE.fullmatch(response.headers.get("Content-Range", "").strip())
            if not match:
                raise DownloadError("The server returned an invalid Content-Range.")
            start, end, total = map(int, match.groups())
            if start != offset or end < start or end >= total:
                raise DownloadError("The server returned a Content-Range that does not match the partial file.")
            response_size = end - start + 1
            if content_length is not None and content_length != response_size:
                raise DownloadError("Content-Length does not match Content-Range.")
            mode = "ab" if offset else "wb"
        else:
            # A server may ignore Range; replace the partial instead of appending.
            offset = 0
            total = content_length
            response_size = content_length
            mode = "wb"

        if expected_size is not None:
            if total is not None and expected_size != total:
                raise DownloadError("The server's file size does not match the expected size.")
            total = expected_size
        chunked = response.headers.get("Transfer-Encoding", "").strip().lower() == "chunked"
        if total is None and not chunked:
            raise DownloadError("The server supplied no verifiable length; provide expected_size.")

        received = 0
        with partial.open(mode) as output, tqdm(
            total=total,
            initial=offset,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=description,
            disable=not progress,
        ) as bar:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if not chunk:
                    continue
                if (
                    (response_size is not None and received + len(chunk) > response_size)
                    or (total is not None and offset + received + len(chunk) > total)
                ):
                    raise DownloadError("The response exceeded its declared or expected size.")
                output.write(chunk)
                received += len(chunk)
                bar.update(len(chunk))

        if response_size is not None and received != response_size:
            raise _RetryableDownloadError("The response ended before its declared length.")
        if total is not None and partial.stat().st_size != total:
            raise _RetryableDownloadError("The partial file has not yet reached the expected length.")
        # Without a size, a clean chunked EOF is the only completion signal.
        # requests raises ChunkedEncodingError for interrupted chunk framing.


def download_file(
    url: str,
    target: str | Path,
    *,
    expected_size: int | None = None,
    overwrite: bool = False,
    timeout: float = 60,
    retries: int = 5,
    backoff: float = 1.0,
    progress: bool = True,
) -> Path:
    """Download to ``target``, retaining ``target.part`` until completion.

    Retry failed requests and interrupted streams up to ``retries`` additional
    times, resuming the partial file when supported. A server that ignores Range
    restarts the partial file. Sizes come from ``expected_size`` or HTTP response
    headers. A chunked response without a known size is accepted after a clean
    end of stream, but cannot be checked against an independent file length.

    An existing final file is skipped only when its size equals a supplied
    ``expected_size``. Otherwise ``overwrite=True`` is required; the old final
    file is kept until its replacement has downloaded successfully. Retries
    never include the URL, authentication details, or underlying request error
    in their exception messages.

    An exclusive ``target.lock`` claim coordinates workers before either final
    or partial files are inspected. ``DownloadBusy`` means another claim exists.
    The claim records hostname, PID and creation time, and is removed on normal
    exit or a handled interruption. After a crash, remove it manually only once
    the owning worker is known to have stopped; claims are never stolen by age.
    """
    _validate_options(url, expected_size, timeout, retries, backoff)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with _claim_target(target):
        return _download_locked(
            url, target, expected_size=expected_size, overwrite=overwrite,
            timeout=timeout, retries=retries, backoff=backoff, progress=progress,
        )


def _download_locked(
    url: str,
    target: Path,
    *,
    expected_size: int | None,
    overwrite: bool,
    timeout: float,
    retries: int,
    backoff: float,
    progress: bool,
) -> Path:
    if target.exists():
        if not target.is_file():
            raise IsADirectoryError("The download target is not a regular file.")
        if not overwrite:
            if expected_size is not None and target.stat().st_size == expected_size:
                return target
            raise FileExistsError("The target already exists; use overwrite to replace it.")

    partial = target.with_name(target.name + ".part")
    if partial.exists() and not partial.is_file():
        raise DownloadError("The partial download path is not a regular file.")
    failure = "Download failed."
    with requests.Session() as session:
        for attempt in range(retries + 1):
            try:
                _transfer_once(
                    session,
                    url,
                    partial,
                    expected_size=expected_size,
                    timeout=timeout,
                    progress=progress,
                    description=target.name,
                )
            except requests.RequestException:
                failure = "A network error interrupted the download."
            except _RetryableDownloadError as error:
                failure = str(error)
            else:
                if target.exists() and not overwrite:
                    raise FileExistsError("The target appeared during the download; use overwrite to replace it.")
                partial.replace(target)
                return target
            if attempt < retries:
                time.sleep(min(backoff * (2 ** min(attempt, 30)), 60))

    raise DownloadError(f"{failure} Exhausted {retries + 1} attempts; the partial file is retained.") from None
