"""Package metadata smoke tests for agent-personal-memory (stdlib only).

The suite requires an installed distribution (``pip install .``); there is
no ``src/`` sys.path fallback, so a bare checkout reports honest skips and a
broken install can never look green.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DIST_NAME = "agent-personal-memory"
IMPORT_NAME = "agent_personal_memory"


def _dist_installed() -> bool:
    try:
        importlib.metadata.version(DIST_NAME)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


class PackageMetadataTest(unittest.TestCase):
    @unittest.skipUnless(_dist_installed(), "distribution not installed (pip install .)")
    def test_distribution_metadata_name(self):
        version = importlib.metadata.version(DIST_NAME)
        self.assertTrue(version)

    @unittest.skipUnless(_dist_installed(), "distribution not installed (pip install .)")
    def test_package_imports(self):
        module = importlib.import_module(IMPORT_NAME)
        self.assertEqual(module.__name__, IMPORT_NAME)

    @unittest.skipUnless(_dist_installed(), "distribution not installed (pip install .)")
    def test_version_attribute_exists(self):
        module = importlib.import_module(IMPORT_NAME)
        self.assertTrue(getattr(module, "__version__", ""))

    def test_license_holder(self):
        text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("samohod4ik", text)
        self.assertIn("MIT", text)


if __name__ == "__main__":
    unittest.main()
