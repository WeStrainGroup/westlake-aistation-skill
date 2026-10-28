"""Offline portable-profile and credential-policy tests; no OS store is opened."""
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from config import ConfigurationError, default_profile_path, load_profile, state_directory, validate_profile
from credentials import CredentialError, _service, _system_keyring, get_password, password_available, store_password


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "ca.pem").write_text("synthetic public test data")
        self.source = {"portal": "https://cluster.example.org:443/", "account": "example-user",
                       "ca_file": "ca.pem", "certificate_sha256": "ab" * 32,
                       "ssh_host_fingerprint": "SHA256:" + "A" * 43,
                       "development_group_id": "example-resource-group", "image": "registry.example.org/image:tag",
                       "persistent_root": "/example-user/compute"}
    def test_profile_relative_ca_and_safe_defaults(self):
        profile = validate_profile(self.source, self.root)
        self.assertEqual(profile["ca_file"], str(self.root / "ca.pem"))
        self.assertEqual(profile["portal"], "https://cluster.example.org:443")
        self.assertEqual(profile["ssh_host"], "cluster.example.org")
        self.assertEqual(profile["max_environment_seconds"], 14400)
        self.assertNotIn("password", profile)
    def test_load_explicit_profile_and_env_override(self):
        target = self.root / "profile.json"
        target.write_text(json.dumps(self.source))
        with patch.dict(os.environ, {"WLAI_PROFILE": str(target)}):
            self.assertEqual(default_profile_path(), target)
            self.assertEqual(load_profile()["account"], "example-user")
    def test_credential_fields_rejected_without_value_leak(self):
        source = dict(self.source, password="synthetic-hidden-value")
        with self.assertRaises(ConfigurationError) as failure:
            validate_profile(source, self.root)
        self.assertNotIn("synthetic-hidden-value", str(failure.exception))
    def test_url_credentials_rejected(self):
        source = dict(self.source, portal="https://someone:synthetic-hidden-value@cluster.example.org")
        with self.assertRaises(ConfigurationError) as failure:
            validate_profile(source, self.root)
        self.assertNotIn("synthetic-hidden-value", str(failure.exception))
    def test_plain_http_and_paths_rejected(self):
        for origin in ("http://cluster.example.org", "https://cluster.example.org/api", "https://cluster.example.org?x=1"):
            with self.subTest(origin=origin), self.assertRaises(ConfigurationError):
                validate_profile(dict(self.source, portal=origin), self.root)
    def test_long_environment_limit_and_path_traversal_rejected(self):
        for changes in ({"max_environment_seconds": 14401}, {"max_environment_seconds": True},
                        {"persistent_root": "/personal/../other"}, {"persistent_root": "/"},
                        {"remote_python": "python"}):
            with self.subTest(changes=changes), self.assertRaises(ConfigurationError):
                validate_profile(dict(self.source, **changes), self.root)
    def test_state_isolation_by_account_and_portal(self):
        profile = validate_profile(self.source, self.root)
        first = state_directory(profile)
        self.assertNotEqual(first, state_directory(dict(profile, account="another-user")))
        self.assertNotEqual(first, state_directory(dict(profile, portal="https://another.example.org")))
        self.assertNotIn(profile["account"], str(first))
    def test_existing_ca_required(self):
        with self.assertRaises(ConfigurationError):
            validate_profile(dict(self.source, ca_file="missing.pem"), self.root)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"portal": "https://cluster.example.org", "account": "example-user"}
    def test_saved_password_used_without_prompt(self):
        with patch("credentials._read_stored", return_value="synthetic-hidden-value"), patch("credentials.getpass.getpass") as prompt:
            self.assertEqual(get_password(self.profile, interactive=False), "synthetic-hidden-value")
            prompt.assert_not_called()
    def test_background_missing_credential_fails_without_prompt(self):
        with patch("credentials._read_stored", return_value=None), patch("credentials.getpass.getpass") as prompt:
            with self.assertRaises(CredentialError):
                get_password(self.profile, interactive=False)
            prompt.assert_not_called()
    def test_password_is_hidden_and_never_read_from_environment(self):
        with patch("credentials._read_stored", return_value=None), patch("credentials.sys.stdin.isatty", return_value=True), \
                patch("credentials.getpass.getpass", return_value="synthetic-interactive-value") as prompt, \
                patch.dict(os.environ, {"AISTATION_PASSWORD": "synthetic-ignored-env-value"}):
            self.assertEqual(get_password(self.profile), "synthetic-interactive-value")
            prompt.assert_called_once()
    def test_pipe_input_is_not_used_as_password(self):
        with patch("credentials._read_stored", return_value=None), patch("credentials.sys.stdin.isatty", return_value=False):
            with self.assertRaises(CredentialError):
                get_password(self.profile)
    def test_password_availability_does_not_prompt(self):
        with patch("credentials._read_stored", return_value=None), patch("credentials.getpass.getpass") as prompt:
            self.assertFalse(password_available(self.profile))
            prompt.assert_not_called()
    def test_credential_store_namespace_is_origin_specific(self):
        self.assertNotEqual(_service(self.profile), _service(dict(self.profile, portal="https://other.example.org")))
        self.assertNotIn("cluster.example.org", _service(self.profile))
    def test_plaintext_keyring_backend_is_rejected(self):
        unsafe_type = type("PlaintextKeyring", (), {"__module__": "keyrings.alt.file"})
        fake_keyring = types.SimpleNamespace(get_keyring=lambda: unsafe_type())
        with patch.dict(sys.modules, {"keyring": fake_keyring}):
            self.assertIsNone(_system_keyring())
    def test_no_plaintext_fallback_when_store_unavailable(self):
        with patch("credentials.sys.platform", "linux"), patch("credentials._system_keyring", return_value=None):
            with self.assertRaises(CredentialError):
                store_password(self.profile, "synthetic-hidden-value")
    def test_backend_failure_suppresses_exception_body(self):
        backend = Mock()
        backend.set_password.side_effect = RuntimeError("synthetic-hidden-value")
        with patch("credentials.sys.platform", "linux"), patch("credentials._system_keyring", return_value=backend):
            with self.assertRaises(CredentialError) as failure:
                store_password(self.profile, "synthetic-hidden-value")
            self.assertNotIn("synthetic-hidden-value", str(failure.exception))


if __name__ == "__main__":
    unittest.main()
