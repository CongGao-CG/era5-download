import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from era5_download.config import DEFAULT_URL, load_accounts


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"CDSAPI_RC": str(self.root / "absent")}, clear=True)
        env.start()
        self.addCleanup(env.stop)

    def config(self, accounts):
        source = self.root / "accounts.json"
        source.write_text(json.dumps({"accounts": accounts}))
        return source

    def test_environment_key_and_no_repr_secret(self):
        os.environ["CDSAPI_KEY"] = "test-secret"
        account, = load_accounts()
        self.assertEqual(account.key, "test-secret")
        self.assertEqual(account.url, DEFAULT_URL)
        self.assertNotIn("test-secret", repr(account))

    def test_missing_credentials(self):
        with self.assertRaisesRegex(ValueError, "No credentials"):
            load_accounts()

    def test_explicit_rc_overrides_environment(self):
        source = self.root / "settings"
        source.write_text("# Example\nurl: https://example.test/api\nkey: 'rc-test-secret'\n")
        os.environ["CDSAPI_KEY"] = "environment-test-secret"
        account, = load_accounts(rc_file=source)
        self.assertEqual(account.key, "rc-test-secret")
        self.assertEqual(account.url, "https://example.test/api")

    def test_named_profiles_use_only_their_credentials(self):
        source = self.config({"research": {"key_env": "RESEARCH_CDS_KEY"}})
        os.environ["CDSAPI_KEY"] = "wrong-profile-secret"
        with self.assertRaisesRegex(ValueError, "No credentials"):
            load_accounts(source, ["research"])
        os.environ["RESEARCH_CDS_KEY"] = "right-profile-secret"
        self.assertEqual(load_accounts(source, ["research"])[0].key, "right-profile-secret")

    def test_profile_rc_path_relative_to_config(self):
        (self.root / "settings").write_text("key: local-secret\n")
        source = self.config({"research": {"rc_file": "settings"}})
        self.assertEqual(load_accounts(source, all_accounts=True)[0].key, "local-secret")

    def test_unknown_account_and_unrecognized_secret_setting(self):
        with self.assertRaisesRegex(ValueError, "Unknown account"):
            load_accounts(names=["missing"])
        source = self.config({"research": {"key": "do-not-echo-this"}})
        with self.assertRaises(ValueError) as caught:
            load_accounts(source)
        self.assertNotIn("do-not-echo-this", str(caught.exception))

    def test_invalid_json_not_echoed(self):
        source = self.root / "broken.json"
        source.write_text("some-secret: [invalid")
        with self.assertRaises(ValueError) as caught:
            load_accounts(source)
        self.assertNotIn("some-secret", str(caught.exception))

    def test_default_rc_and_url_override(self):
        source = self.root / "settings"
        source.write_text("key: test-key\nurl: https://example.test/api\n")
        os.environ["CDSAPI_RC"] = str(source)
        account, = load_accounts(url="https://override.test/api/")
        self.assertEqual(account.key, "test-key")
        self.assertEqual(account.url, "https://override.test/api")

    def test_all_accounts_and_duplicate_selection(self):
        source = self.config({"one": {"key_env": "ONE"}, "two": {"key_env": "TWO"}})
        os.environ.update(ONE="one-secret", TWO="two-secret")
        self.assertEqual([a.name for a in load_accounts(source, all_accounts=True)], ["one", "two"])
        self.assertEqual(len(load_accounts(source, ["one", "one"])), 1)

    def test_conflicting_sources(self):
        with self.assertRaisesRegex(ValueError, "either --config"):
            load_accounts(config="a", rc_file="b")
        with self.assertRaisesRegex(ValueError, "either --account"):
            load_accounts(names=["one"], all_accounts=True)

    def test_key_source_reports_the_environment_variable(self):
        os.environ["CDSAPI_KEY"] = "test-secret"
        account, = load_accounts()
        self.assertEqual(account.key_source, "environment variable CDSAPI_KEY")

    def test_key_source_reports_the_default_rc_file(self):
        source = self.root / "settings"
        source.write_text("key: test-key\n")
        os.environ["CDSAPI_RC"] = str(source)
        account, = load_accounts()
        self.assertEqual(account.key_source, f"rc file {source}")

    def test_key_source_reports_an_explicit_rc_file(self):
        source = self.root / "settings"
        source.write_text("key: rc-test-secret\n")
        os.environ["CDSAPI_KEY"] = "environment-test-secret"
        account, = load_accounts(rc_file=source)
        self.assertEqual(account.key_source, f"rc file {source}")

    def test_key_source_reports_a_profiles_key_env(self):
        source = self.config({"research": {"key_env": "RESEARCH_CDS_KEY"}})
        os.environ["RESEARCH_CDS_KEY"] = "right-profile-secret"
        account, = load_accounts(source, ["research"])
        self.assertEqual(account.key_source, "environment variable RESEARCH_CDS_KEY")

    def test_key_source_reports_a_profiles_rc_file(self):
        (self.root / "settings").write_text("key: local-secret\n")
        source = self.config({"research": {"rc_file": "settings"}})
        account, = load_accounts(source, all_accounts=True)
        self.assertTrue(account.key_source.startswith("rc file "))
        self.assertEqual(Path(account.key_source.removeprefix("rc file ")).resolve(),
                         (self.root / "settings").resolve())

    def test_key_source_never_echoes_the_key(self):
        os.environ["CDSAPI_KEY"] = "test-secret"
        account, = load_accounts()
        self.assertNotIn("test-secret", account.key_source)


if __name__ == "__main__":
    unittest.main()
