import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from era5_download.manifest import (
    ManifestEntry, append_manifest, read_manifest, validate_manifest_append, write_manifest,
)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "jobs.csv"

    def test_round_trip_handles_commas_and_relative_subdirectories(self):
        entries = [ManifestEntry("nested/u200_202001.nc", "test-job"),
                   ManifestEntry("sst,202001.nc", "second-job", "example-profile")]
        write_manifest(self.path, entries)
        self.assertEqual(read_manifest(self.path), entries)
        self.assertEqual(self.path.read_text().splitlines()[0], "filename,job_id,account")

    def test_old_csv_headers_work(self):
        self.path.write_text("fnm,rid,acc\nu200_202001.nc,example-id,example-profile\n")
        self.assertEqual(read_manifest(self.path),
                         [ManifestEntry("u200_202001.nc", "example-id", "example-profile")])

    def test_optional_account_defaults_and_bom_is_accepted(self):
        self.path.write_text("\ufefffilename,job_id\nsst.nc,example-id\n", encoding="utf-8")
        self.assertEqual(read_manifest(self.path), [ManifestEntry("sst.nc", "example-id")])

    def test_safe_paths_and_required_values_are_validated(self):
        for filename in ("/tmp/test.nc", "../test.nc", "nested/../../test.nc", "a/../b.nc",
                         "C:\\test.nc", "C:test.nc", "\\\\server\\file.nc", "a\\b.nc",
                         "./test.nc", "nested/", "a//b.nc", "", "a\x00.nc", "a\n.nc"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                ManifestEntry(filename, "test-job")
        with self.assertRaisesRegex(ValueError, "Job ID"):
            ManifestEntry("test.nc", " ")
        with self.assertRaisesRegex(ValueError, "Account"):
            ManifestEntry("test.nc", "test-job", "")

    def test_bad_headers_and_csv_values_report_useful_errors(self):
        cases = (
            ("", "empty"),
            ("filename,account\ntest.nc,default\n", "job_id"),
            ("job_id\nexample-id\n", "filename"),
            ("filename,filename,job_id\na,b,c\n", "duplicate"),
            ("filename,fnm,job_id\na,b,c\n", "only one"),
            ("filename,,job_id\na,b,c\n", "empty column"),
            ("filename,job_id\ntest.nc,\n", "line 2.*Job ID"),
            ("filename,job_id,account\ntest.nc,id,\n", "line 2.*Account"),
            ("filename,job_id\n../test.nc,id\n", "line 2.*relative path"),
            ("filename,job_id\ntest.nc,id,extra\n", "line 2.*too many"),
            ("filename,job_id\n\"unfinished", "invalid CSV"),
        )
        for content, message in cases:
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaisesRegex(ValueError, message):
                    read_manifest(self.path)

    def test_empty_manifest_round_trips(self):
        write_manifest(self.path, [])
        self.assertEqual(read_manifest(self.path), [])

    def test_append_manifest_creates_header_once_and_preserves_prior_rows(self):
        append_manifest(self.path, ManifestEntry("u200_202001.nc", "job-one", "acc-a"))
        append_manifest(self.path, ManifestEntry("v200_202001.nc", "job-two", "acc-b"))
        self.assertEqual(read_manifest(self.path), [
            ManifestEntry("u200_202001.nc", "job-one", "acc-a"),
            ManifestEntry("v200_202001.nc", "job-two", "acc-b"),
        ])
        self.assertEqual(self.path.read_text().splitlines()[0], "filename,job_id,account")

    def test_append_manifest_rejects_non_entry(self):
        with self.assertRaises(TypeError):
            append_manifest(self.path, "not an entry")
        self.assertFalse(self.path.exists())

    def test_append_preserves_column_layout_and_leaves_extra_columns_blank(self):
        old = ManifestEntry("old.nc", "old-job", "example-profile")
        new = ManifestEntry("nested/new,file.nc", "new-job", "example-profile")
        cases = (
            ("account,job_id,filename,notes\nexample-profile,old-job,old.nc,keep this\n",
             ["example-profile", "new-job", "nested/new,file.nc", ""]),
            ("rid,notes,acc,fnm\nold-job,keep this,example-profile,old.nc\n",
             ["new-job", "", "example-profile", "nested/new,file.nc"]),
            ("acc,filename,rid\nexample-profile,old.nc,old-job\n",
             ["example-profile", "nested/new,file.nc", "new-job"]),
        )
        for content, expected_row in cases:
            with self.subTest(content=content):
                original = content.encode("utf-8")
                self.path.write_bytes(original)
                append_manifest(self.path, new)
                self.assertTrue(self.path.read_bytes().startswith(original))
                self.assertEqual(read_manifest(self.path), [old, new])
                with self.path.open(newline="") as stream:
                    self.assertEqual(list(csv.reader(stream))[-1], expected_row)

    def test_append_handles_bom_and_missing_final_newline_without_rewriting(self):
        old = ManifestEntry("old.nc", "old-job")
        new = ManifestEntry("new.nc", "new-job")
        for bom in (b"", b"\xef\xbb\xbf"):
            for ending in (b"", b"\n", b"\r\n", b"\r"):
                for has_row in (False, True):
                    with self.subTest(bom=bom, ending=ending, has_row=has_row):
                        original = bom + b"filename,job_id,account"
                        if has_row:
                            original += b"\nold.nc,old-job,default"
                        original += ending
                        self.path.write_bytes(original)
                        append_manifest(self.path, new)
                        self.assertTrue(self.path.read_bytes().startswith(original))
                        self.assertEqual(read_manifest(self.path), ([old] if has_row else []) + [new])

    def test_append_without_account_column_accepts_only_default_account(self):
        for header in ("job_id,filename,notes", "rid,fnm,notes"):
            with self.subTest(header=header):
                original = (header + "\nold-job,old.nc,keep this\n").encode("utf-8")
                self.path.write_bytes(original)
                with self.assertRaisesRegex(ValueError, "non-default account"):
                    append_manifest(self.path, ManifestEntry("new.nc", "new-job", "example-profile"))
                self.assertEqual(self.path.read_bytes(), original)
                append_manifest(self.path, ManifestEntry("new.nc", "new-job"))
                self.assertEqual(read_manifest(self.path), [
                    ManifestEntry("old.nc", "old-job"), ManifestEntry("new.nc", "new-job"),
                ])
                self.assertEqual(self.path.read_bytes(), original + b"new-job,new.nc,\r\n")

    def test_append_rejects_invalid_existing_csv_without_changing_bytes(self):
        cases = (
            b"", b"\xef\xbb\xbf", b"filename,account\nold.nc,default\n",
            b"filename,fnm,job_id\na,b,c\n", b"filename,filename,job_id\na,b,c\n",
            b"filename,job_id\nold.nc,old-job,extra\n",
            b"filename,job_id\n../old.nc,old-job\n", b'filename,job_id\n"unfinished',
        )
        for original in cases:
            with self.subTest(original=original):
                self.path.write_bytes(original)
                with self.assertRaises(ValueError):
                    append_manifest(self.path, ManifestEntry("new.nc", "new-job"))
                self.assertEqual(self.path.read_bytes(), original)

    def test_append_preflight_validates_accounts_and_records_without_mutation(self):
        validate_manifest_append(self.path, ("example-profile",))
        self.assertFalse(self.path.exists())
        original = b"filename,job_id\nold.nc,old-job\n"
        self.path.write_bytes(original)
        validate_manifest_append(self.path, ("default",))
        with self.assertRaisesRegex(ValueError, "non-default account"):
            validate_manifest_append(self.path, ("default", "example-profile"))
        self.assertEqual(self.path.read_bytes(), original)
        original = b"filename,job_id,account\nold.nc,,default\n"
        self.path.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "line 2.*Job ID"):
            validate_manifest_append(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_bad_write_does_not_truncate_existing_file(self):
        self.path.write_text("keep this content\n")
        with self.assertRaises(TypeError):
            write_manifest(self.path, ["not an entry"])
        self.assertEqual(self.path.read_text(), "keep this content\n")


if __name__ == "__main__":
    unittest.main()
