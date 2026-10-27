from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install  # noqa: E402


class EnsureTests(unittest.TestCase):
    def test_installed_needs_every_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(install.installed(directory))
            for _, name in install.APPS:
                (Path(directory) / (name + '.app')).mkdir()
            self.assertTrue(install.installed(directory))

    def test_ensure_installs_fresh(self):
        with patch.object(install, 'installed', return_value=False), \
                patch.object(install, 'install') as run, patch.object(install, 'pause_watcher') as pause:
            install.main(['--ensure'])
        run.assert_called_once_with(update=False, rename_blocked=False)
        pause.assert_not_called()

    def test_ensure_updates_existing(self):
        with patch.object(install, 'installed', return_value=True), \
                patch.object(install, 'install') as run, patch.object(install, 'pause_watcher', return_value=None):
            install.main(['--ensure'])
        run.assert_called_once_with(update=True, rename_blocked=False)


if __name__ == '__main__':
    unittest.main()
