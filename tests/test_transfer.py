"""Credential-free, offline coverage of resumable HTTP transfers."""

import tempfile
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from era5_download.transfer import DownloadBusy, DownloadError, download_file


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=()):
        self.status_code = status
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})
        self.chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.target = Path(self.directory.name) / "data.nc"
        self.partial = self.target.with_name("data.nc.part")
        self.lock = self.target.with_name("data.nc.lock")
        self.url = "https://example.invalid/object?token=private-token"

    def run_download(self, responses, **kwargs):
        session = FakeSession(responses)
        with patch("era5_download.transfer.requests.Session", return_value=session):
            result = download_file(self.url, self.target, progress=False, backoff=0, **kwargs)
        return result, session

    def test_fresh_download_is_promoted(self):
        result, session = self.run_download([FakeResponse(headers={"Content-Length": "6"}, chunks=[b"abc", b"", b"def"])])
        self.assertEqual(result, self.target)
        self.assertEqual(self.target.read_bytes(), b"abcdef")
        self.assertFalse(self.partial.exists())
        self.assertFalse(self.lock.exists())
        self.assertEqual(session.calls[0][1]["headers"], {"Accept-Encoding": "identity"})

    def test_resume_validates_range(self):
        self.partial.write_bytes(b"abc")
        _, session = self.run_download([FakeResponse(206, {"Content-Range": "bytes 3-5/6", "Content-Length": "3"}, [b"def"])])
        self.assertEqual(self.target.read_bytes(), b"abcdef")
        self.assertEqual(session.calls[0][1]["headers"]["Range"], "bytes=3-")

    def test_ignored_range_restarts_partial(self):
        self.partial.write_bytes(b"old")
        self.run_download([FakeResponse(headers={"Content-Length": "6"}, chunks=[b"abcdef"])])
        self.assertEqual(self.target.read_bytes(), b"abcdef")

    def test_interrupted_stream_retries_from_partial_length(self):
        _, session = self.run_download([
            FakeResponse(headers={"Content-Length": "6"}, chunks=[b"abc", requests.ConnectionError("secret")]),
            FakeResponse(206, {"Content-Range": "bytes 3-5/6"}, [b"def"]),
        ], retries=1)
        self.assertEqual(self.target.read_bytes(), b"abcdef")
        self.assertEqual(session.calls[1][1]["headers"]["Range"], "bytes=3-")

    def test_short_response_retries(self):
        self.run_download([
            FakeResponse(headers={"Content-Length": "6"}, chunks=[b"abc"]),
            FakeResponse(206, {"Content-Range": "bytes 3-5/6"}, [b"def"]),
        ], retries=1)
        self.assertEqual(self.target.read_bytes(), b"abcdef")

    def test_retryable_http_status(self):
        self.run_download([
            FakeResponse(503),
            FakeResponse(headers={"Content-Length": "3"}, chunks=[b"abc"]),
        ], retries=1)
        self.assertEqual(self.target.read_bytes(), b"abc")

    def test_existing_verified_file_skips_request(self):
        self.target.write_bytes(b"abc")
        _, session = self.run_download([], expected_size=3)
        self.assertEqual(session.calls, [])

    def test_existing_unverified_file_requires_overwrite(self):
        self.target.write_bytes(b"abc")
        for size in (None, 4):
            with self.subTest(size=size), self.assertRaises(FileExistsError):
                self.run_download([], expected_size=size)

    def test_failed_overwrite_preserves_old_final_and_partial(self):
        self.target.write_bytes(b"old")
        with self.assertRaises(DownloadError):
            self.run_download([FakeResponse(headers={"Content-Length": "6"}, chunks=[b"abc"])], overwrite=True, retries=0)
        self.assertEqual(self.target.read_bytes(), b"old")
        self.assertEqual(self.partial.read_bytes(), b"abc")
        self.assertFalse(self.lock.exists())

    def test_existing_lock_is_untouched_and_blocks_even_skip(self):
        self.lock.write_text("another worker")
        self.target.write_bytes(b"abc")
        self.partial.write_bytes(b"other work")
        with self.assertRaises(DownloadBusy):
            self.run_download([], expected_size=3)
        self.assertEqual(self.lock.read_text(), "another worker")
        self.assertEqual(self.target.read_bytes(), b"abc")
        self.assertEqual(self.partial.read_bytes(), b"other work")

    def test_only_one_concurrent_downloader_claims_target(self):
        started = threading.Event()
        release = threading.Event()
        errors = []
        metadata = []

        class BlockingResponse(FakeResponse):
            def iter_content(response, chunk_size):
                metadata.append(json.loads(self.lock.read_text()))
                started.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("Timed out waiting for the concurrent test.")
                yield b"abc"

        session = FakeSession([BlockingResponse(headers={"Content-Length": "3"})])

        def worker():
            try:
                download_file(self.url, self.target, progress=False)
            except BaseException as error:
                errors.append(error)

        with patch("era5_download.transfer.requests.Session", return_value=session):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(started.wait(timeout=5))
                with self.assertRaises(DownloadBusy):
                    download_file(self.url, self.target, progress=False)
                self.assertFalse(self.target.exists())
            finally:
                release.set()
                thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(self.target.read_bytes(), b"abc")
        self.assertFalse(self.lock.exists())
        self.assertEqual(set(metadata[0]), {"hostname", "pid", "created_at"})

    def test_lock_released_on_keyboard_interrupt(self):
        class InterruptedResponse(FakeResponse):
            def iter_content(self, chunk_size):
                yield b"ab"
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            self.run_download([InterruptedResponse(headers={"Content-Length": "3"})])
        self.assertFalse(self.lock.exists())
        self.assertEqual(self.partial.read_bytes(), b"ab")

    def test_replaced_lock_is_not_deleted(self):
        class ReplacedLockResponse(FakeResponse):
            def iter_content(response, chunk_size):
                self.lock.unlink()
                self.lock.write_text("replacement claim")
                yield b"abc"

        self.run_download([ReplacedLockResponse(headers={"Content-Length": "3"})])
        self.assertEqual(self.lock.read_text(), "replacement claim")

    def test_overwrite_replaces_final_after_success(self):
        self.target.write_bytes(b"old")
        self.run_download([FakeResponse(headers={"Content-Length": "3"}, chunks=[b"new"])], overwrite=True)
        self.assertEqual(self.target.read_bytes(), b"new")

    def test_invalid_ranges_preserve_partial(self):
        for value in ("", "bytes 0-2/6", "bytes 3-6/6", "bytes 3-2/6", "bytes 3-5/*"):
            with self.subTest(value=value):
                self.partial.write_bytes(b"abc")
                with self.assertRaises(DownloadError):
                    self.run_download([FakeResponse(206, {"Content-Range": value}, [b"def"])])
                self.assertEqual(self.partial.read_bytes(), b"abc")
                self.assertFalse(self.target.exists())

    def test_range_and_content_length_must_agree(self):
        self.partial.write_bytes(b"abc")
        with self.assertRaises(DownloadError):
            self.run_download([FakeResponse(206, {"Content-Range": "bytes 3-5/6", "Content-Length": "4"}, [b"def"])])

    def test_verified_416_promotes_complete_partial(self):
        self.partial.write_bytes(b"abcdef")
        self.run_download([FakeResponse(416, {"Content-Range": "bytes */6"})], expected_size=6)
        self.assertEqual(self.target.read_bytes(), b"abcdef")

    def test_unverified_416_never_promotes(self):
        for value, expected in (("bytes */7", None), ("", None), ("bytes */6", 7)):
            with self.subTest(value=value, expected=expected):
                self.partial.write_bytes(b"abcdef")
                with self.assertRaises(DownloadError):
                    self.run_download([FakeResponse(416, {"Content-Range": value})], expected_size=expected)
                self.assertFalse(self.target.exists())

    def test_size_mismatch_does_not_truncate_partial(self):
        self.partial.write_bytes(b"abc")
        with self.assertRaises(DownloadError):
            self.run_download([FakeResponse(headers={"Content-Length": "6"}, chunks=[b"abcdef"])], expected_size=7)
        self.assertEqual(self.partial.read_bytes(), b"abc")

    def test_oversized_partial_restarts_without_range(self):
        self.partial.write_bytes(b"too large")
        _, session = self.run_download([FakeResponse(headers={"Content-Length": "3"}, chunks=[b"abc"])], expected_size=3)
        self.assertNotIn("Range", session.calls[0][1]["headers"])
        self.assertEqual(self.target.read_bytes(), b"abc")

    def test_overlong_response_is_not_promoted(self):
        with self.assertRaises(DownloadError):
            self.run_download([FakeResponse(headers={"Content-Length": "3"}, chunks=[b"ab", b"cd"])])
        self.assertFalse(self.target.exists())
        self.assertEqual(self.partial.read_bytes(), b"ab")

    def test_empty_file(self):
        self.run_download([FakeResponse(headers={"Content-Length": "0"})], expected_size=0)
        self.assertEqual(self.target.read_bytes(), b"")

    def test_chunked_unknown_size_clean_eof(self):
        self.run_download([FakeResponse(headers={"Transfer-Encoding": "chunked"}, chunks=[b"abc"])])
        self.assertEqual(self.target.read_bytes(), b"abc")

    def test_unknown_length_without_chunking_requires_size(self):
        with self.assertRaises(DownloadError):
            self.run_download([FakeResponse(chunks=[b"abc"])])
        self.assertFalse(self.target.exists())
        self.run_download([FakeResponse(chunks=[b"abc"])], expected_size=3)
        self.assertEqual(self.target.read_bytes(), b"abc")

    def test_interrupted_chunked_stream_retains_partial(self):
        with self.assertRaises(DownloadError):
            self.run_download([FakeResponse(headers={"Transfer-Encoding": "chunked"}, chunks=[b"abc", requests.exceptions.ChunkedEncodingError("secret")])], retries=0)
        self.assertEqual(self.partial.read_bytes(), b"abc")
        self.assertFalse(self.target.exists())

    def test_request_error_is_sanitized(self):
        secret = "https://user:password@example.invalid/file?token=private-token"
        with self.assertRaises(DownloadError) as caught:
            self.run_download([requests.ConnectionError(secret)], retries=0)
        self.assertNotIn("password", str(caught.exception))
        self.assertNotIn("private-token", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_invalid_metadata_is_not_promoted(self):
        for headers in ({"Content-Length": "-1"}, {"Content-Length": "bogus"}, {"Content-Length": "3", "Content-Encoding": "gzip"}):
            with self.subTest(headers=headers), self.assertRaises(DownloadError):
                self.run_download([FakeResponse(headers=headers, chunks=[b"abc"])])
        self.assertFalse(self.target.exists())

    def test_invalid_options(self):
        for kwargs in ({"expected_size": -1}, {"expected_size": True}, {"retries": -1}, {"retries": 1.5}, {"timeout": 0}, {"timeout": float("nan")}, {"backoff": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                download_file(self.url, self.target, **kwargs)
        with self.assertRaises(ValueError):
            download_file("file:///private/example", self.target)


if __name__ == "__main__":
    unittest.main()
