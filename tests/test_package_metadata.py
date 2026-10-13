"""Package metadata smoke tests for agent-personal-memory (stdlib only)."""
from __future__ import annotations

import importlib
import importlib.metadata
import sys
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


def _import_package():
    """Import the package; fall back to the src/ tree when not installed."""
    try:
        return importlib.import_module(IMPORT_NAME)
    except ImportError:
        src = str(REPO_ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        return importlib.import_module(IMPORT_NAME)


class PackageMetadataTest(unittest.TestCase):
    @unittest.skipUnless(_dist_installed(), "distribution not installed (pip install .)")
    def test_distribution_metadata_name(self):
        version = importlib.metadata.version(DIST_NAME)
        self.assertTrue(version)

    def test_package_imports(self):
        module = _import_package()
        self.assertEqual(module.__name__, IMPORT_NAME)

    def test_version_attribute_exists(self):
        module = _import_package()
        self.assertTrue(getattr(module, "__version__", ""))

    def test_license_holder(self):
        text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("samohod4ik", text)
        self.assertIn("MIT", text)


if __name__ == "__main__":
    unittest.main()
