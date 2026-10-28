"""State-location precedence without reading configuration, credentials or state."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from config import ConfigurationError, profile_identity, state_directory


class StateDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.profile = {'portal': 'https://cluster.example.invalid', 'account': 'example-user'}

    def test_explicit_application_root_precedes_xdg_and_keeps_identity_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'new state with spaces'
            xdg = Path(directory).resolve() / 'xdg state'
            with patch.dict(os.environ, {'WLAI_STATE_ROOT': str(root), 'XDG_STATE_HOME': str(xdg)}, clear=True):
                selected = state_directory(self.profile)
            self.assertEqual(selected, root / profile_identity(self.profile))
            self.assertFalse(root.exists())
            self.assertFalse(xdg.exists())

    def test_xdg_and_home_fallback_are_unchanged_when_override_is_missing_or_empty(self):
        with patch.dict(os.environ, {'XDG_STATE_HOME': '/example-state'}, clear=True):
            expected = Path('/example-state/westlake-aistation') / profile_identity(self.profile)
            self.assertEqual(state_directory(self.profile), expected)
        with patch.dict(os.environ, {'WLAI_STATE_ROOT': '', 'XDG_STATE_HOME': '/example-state'}, clear=True):
            self.assertEqual(state_directory(self.profile), expected)
        with patch.dict(os.environ, {}, clear=True), patch('config.Path.home', return_value=Path('/example-home')):
            self.assertEqual(state_directory(self.profile), Path('/example-home/.local/state/westlake-aistation') / profile_identity(self.profile))

    def test_portal_and_account_stay_isolated_with_a_shared_explicit_root(self):
        with patch.dict(os.environ, {'WLAI_STATE_ROOT': '/example-state'}, clear=True):
            selected = state_directory(self.profile)
            account = state_directory(dict(self.profile, account='another-user'))
            portal = state_directory(dict(self.profile, portal='https://another.example.invalid'))
        self.assertNotEqual(selected, account)
        self.assertNotEqual(selected, portal)
        self.assertNotIn(self.profile['account'], str(selected))

    def test_relative_root_cannot_silently_split_agent_registries_by_working_directory(self):
        for root in ('relative/state', '/', '/example\nstate'):
            with self.subTest(root=root), patch.dict(os.environ, {'WLAI_STATE_ROOT': root}, clear=True):
                with self.assertRaises(ConfigurationError):
                    state_directory(self.profile)


if __name__ == '__main__':
    unittest.main()
