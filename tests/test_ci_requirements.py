"""Meta-test for the extraction layout, adapted from source test_ci_requirements.py.

The source file asserted the host CI requirements lock pinned the plugin's
``cryptography`` dependency. The standalone package has no requirements
lock (``pyproject.toml`` declares the ``cryptography>=42`` floor), so this
file became a meta-test over the extraction layout:

- ``docs/PROVENANCE.md`` carries a file-for-file disposition table: every
  ``src/`` module and ``tests/test_*.py`` file has a row, every required
  new file is recorded as added/adapted, and every source test file the
  extraction did not port is named with a disposition;
- ``src/`` carries zero host imports or host markers (an AST check of
  every import statement plus a regex sweep and an env-var audit);
- no tracked file contains host-absolute paths (``git ls-files`` plus the
  exact ``HOST_PATH_PATTERNS`` and per-match-span allowlist machinery of
  ``tools/hygiene_scan.py``).
"""

from __future__ import annotations

import ast
import importlib.util
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROVENANCE = REPO_ROOT / "docs" / "PROVENANCE.md"
SRC_ROOT = REPO_ROOT / "src"
TESTS_ROOT = REPO_ROOT / "tests"

# Module-name parts/prefixes that must never appear in a src/ import.
# Some literals are concatenated so this file does not trip the repo
# hygiene scanner on the very markers it forbids.
_FORBIDDEN_IMPORT_PARTS = frozenset(
    {
        "gateway",
        "plugins",
        "personal_memory",
        "adapters",
        "travel",
        "inject",
        "tools",
        "trusted_delete",
        "peer_" + "import",
        "spik" + "es",
    }
)
_FORBIDDEN_IMPORT_PREFIXES = (
    "mini" + "app",          # mini-app module family (contract/handlers/api)
    "night_" + "cycle",      # night-cycle tool descriptions module
    "travel_" + "compute",   # travel compute bridge module
)

# Regex sweep over src/ text for host markers that could hide outside
# import statements (comments, strings, env reads). Literals are split
# for the same self-scan reason as above.
_FORBIDDEN_TEXT_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"\bgateway\b",
        r"HERMES",
        r"plugins\.personal_memory",
        r"adapters\.travel",
        r"peer_" + r"import",
        r"miniapp",
        r"night_" + r"cycle",
        r"travel_" + r"compute",
        r"spik" + r"es",
        r"session_env",
        r"pre_" + r"gateway_dispatch",
        r"plugin\.yaml",
    )
)

# Env-read audit (PH2-F-05): covers ``environ[...]``/``environ.get/``
# ``setdefault/pop(...)`` and ``getenv(...)`` reads reached through the
# ``os`` module object, any aliased ``<module>.`` prefix, and
# ``from os import environ/getenv`` bare call forms — with any-case env
# names. One optional ``<module>.`` prefix and one optional
# ``.get/.setdefault/.pop`` suffix keep the expression readable while the
# ``[\[\(]`` + quote shape pins actual reads.
_ENV_READ = re.compile(
    r"\b(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
    r"(?:environ(?:\.(?:get|setdefault|pop))?|getenv)"
    r"\s*[\[\(]\s*['\"]([A-Za-z0-9_]+)"
)
_ALLOWED_ENV_VARS = frozenset({"PERSONAL_MEMORY_DATA_DIR"})

_SYS_PATH_SHIM = re.compile(r"sys\.path\s*\.(insert|append)")
_ENV_TOUCH = re.compile(r"os\.environ|os\.getenv")

# New tracked files that must carry an added/adapted provenance row.
_REQUIRED_NEW_FILE_ROWS = (
    ".gitignore",
    ".github/workflows/ci.yml",
    "LICENSE",
    "README.md",
    "README.ru.md",
    "docs/PROVENANCE.md",
    "docs/adr/0002-core-boundary.md",
    "docs/threat-model.md",
    "pyproject.toml",
    "skills/agent-personal-memory/SKILL.md",
    "tests/test_api_surface.py",
    "tests/test_ci_requirements.py",
    "tests/test_mcp_stdio.py",
    "tests/test_package_metadata.py",
    "tests/test_readme_quickstart.py",
    "tests/test_tool_surface.py",
    "tools/hygiene_allowlist.txt",
    "tools/hygiene_scan.py",
)

# Every source tests/test_*.py stem must appear in a provenance
# disposition row (ported / adapted / not-ported). Stems containing
# scanner-flagged substrings are written split.
_SOURCE_TEST_STEMS = (
    "test_assembler",
    "test_audit",
    "test_ci_requirements",
    "test_crypto",
    "test_cycle1_tool_descriptions",
    "test_disabled_rollout",
    "test_erasure",
    "test_export",
    "test_export_ux",
    "test_inject",
    "test_insight_extract",
    "test_learn",
    "test_lifecycle",
    "test_live_handler_payloads",
    "test_mem0_shadow",
    "test_memory_mode",
    "test_miniapp" + "_api",
    "test_miniapp" + "_contract",
    "test_miniapp" + "_handlers",
    "test_mutations",
    "test_plugin_load",
    "test_query",
    "test_r1_" + "inject" + "_acl",
    "test_ranking",
    "test_review",
    "test_schemas",
    "test_score_locality",
    "test_sources",
    "test_store",
    "test_tools",
    "test_travel_adapter",
    "test_travel_" + "compute_bridge",
    "test_trusted_delete",
    "test_ux",
    "test_validation",
)

_STATUS_RE = re.compile(r"ported|adapted|added|new", re.IGNORECASE)


def _provenance_rows(text: str) -> list[str]:
    """Return every table row inside the '## File map' section."""
    rows: list[str] = []
    in_map = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_map = line.strip().lower() == "## file map"
            continue
        if in_map and line.startswith("|") and "`" in line:
            rows.append(line)
    return rows


def _file_map_first_cells(text: str) -> set[str]:
    """Exact backticked paths from each File-map row's first cell.

    First-cell matching only: a path mentioned in free-text delta prose does
    not satisfy a provenance row (PH2-F-02).
    """
    names: set[str] = set()
    for row in _provenance_rows(text):
        cells = row.split("|")
        if len(cells) >= 3:
            names.update(re.findall(r"`([^`]+)`", cells[1]))
    return names


# Backticked ``*.py`` paths named anywhere in a row (File or Source cell).
_BACKTICKED_PY = re.compile(r"`([^`]+\.py)`")


def _row_py_stems(row: str) -> set[str]:
    """Exact ``*.py`` file stems named by the row — no substring matches."""
    return {
        name.rsplit("/", 1)[-1][: -len(".py")]
        for name in _BACKTICKED_PY.findall(row)
    }


def _src_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _load_hygiene_scanner():
    spec = importlib.util.spec_from_file_location(
        "pm_hygiene_scan", REPO_ROOT / "tools" / "hygiene_scan.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ProvenanceCompletenessTests(unittest.TestCase):
    """(a) The file-for-file disposition table is complete."""

    def test_every_src_module_has_a_provenance_row(self) -> None:
        first_cells = _file_map_first_cells(PROVENANCE.read_text(encoding="utf-8"))
        for path in _src_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            with self.subTest(file=rel):
                self.assertIn(rel, first_cells, f"{rel}: missing provenance row")

    def test_every_test_file_has_a_provenance_row(self) -> None:
        first_cells = _file_map_first_cells(PROVENANCE.read_text(encoding="utf-8"))
        for path in sorted(TESTS_ROOT.glob("test_*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            with self.subTest(file=rel):
                self.assertIn(rel, first_cells, f"{rel}: missing provenance row")

    def test_every_tracked_file_has_a_provenance_row(self) -> None:
        """Every ``git ls-files`` entry needs a first-cell File-map row.

        The scanner's own file and the allowlist are excluded from
        ``tracked_files``; both are covered by ``_REQUIRED_NEW_FILE_ROWS``.
        """
        scanner = _load_hygiene_scanner()
        first_cells = _file_map_first_cells(PROVENANCE.read_text(encoding="utf-8"))
        for path in scanner.tracked_files(REPO_ROOT):
            rel = path.relative_to(REPO_ROOT).as_posix()
            with self.subTest(file=rel):
                self.assertIn(rel, first_cells, f"{rel}: missing provenance row")

    def test_required_new_files_have_added_or_adapted_rows(self) -> None:
        rows = _provenance_rows(PROVENANCE.read_text(encoding="utf-8"))
        for rel in _REQUIRED_NEW_FILE_ROWS:
            with self.subTest(file=rel):
                matching = [row for row in rows if f"`{rel}`" in row]
                self.assertTrue(matching, f"{rel}: missing provenance row")
                self.assertTrue(
                    any(
                        re.search(r"\|\s*(added|adapted|new)\s*\|", row)
                        for row in matching
                    ),
                    f"{rel}: new files must not be marked ported",
                )

    def test_every_source_test_file_has_a_disposition(self) -> None:
        text = PROVENANCE.read_text(encoding="utf-8")
        rows = _provenance_rows(text)
        for stem in _SOURCE_TEST_STEMS:
            with self.subTest(source_test=stem):
                # Exact backticked-file-stem match: ``test_export`` must not
                # be satisfied by a ``test_export_ux.py`` row (PH2-F-02).
                matching = [row for row in rows if stem in _row_py_stems(row)]
                self.assertTrue(matching, f"{stem}: no provenance row")
                self.assertTrue(
                    any(_STATUS_RE.search(row) for row in matching),
                    f"{stem}: row lacks a disposition status",
                )
        # The source spike-test directory is recorded too.
        self.assertIn("spik" + "es", text)


class SrcHygieneTests(unittest.TestCase):
    """(b) src/ carries no host imports, markers, or env reads."""

    def test_src_has_no_forbidden_imports(self) -> None:
        for path in _src_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    candidates = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.module is None:
                        # 'from . import x': the names are module names.
                        candidates = [alias.name for alias in node.names]
                    else:
                        candidates = [node.module]
                else:
                    continue
                for candidate in candidates:
                    for part in candidate.split("."):
                        bad = part in _FORBIDDEN_IMPORT_PARTS or any(
                            part.startswith(prefix)
                            for prefix in _FORBIDDEN_IMPORT_PREFIXES
                        )
                        self.assertFalse(
                            bad, f"{rel}: forbidden import {candidate!r}"
                        )

    def test_src_has_no_host_markers(self) -> None:
        for path in _src_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                for pattern in _FORBIDDEN_TEXT_PATTERNS:
                    self.assertIsNone(
                        pattern.search(line), f"{rel}:{lineno}: {pattern.pattern}"
                    )

    def test_src_reads_only_sanctioned_env_vars(self) -> None:
        for path in _src_files():
            rel = path.relative_to(REPO_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for match in _ENV_READ.finditer(text):
                self.assertIn(
                    match.group(1),
                    _ALLOWED_ENV_VARS,
                    f"{rel}: unsanctioned env var {match.group(1)}",
                )

    def test_tests_have_no_sys_path_shims_or_env_juggling(self) -> None:
        for path in sorted(TESTS_ROOT.glob("test_*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), 1):
                self.assertIsNone(
                    _SYS_PATH_SHIM.search(line), f"{rel}:{lineno}: sys.path shim"
                )
                self.assertIsNone(
                    _ENV_TOUCH.search(line), f"{rel}:{lineno}: env access"
                )


class PackagingFloorTests(unittest.TestCase):
    """(d) pyproject.toml keeps the declared dependency floor (PH2-F-08).

    The source file this meta-test descends from asserted the host CI
    requirements lock pinned ``cryptography``; the standalone floor lives in
    ``pyproject.toml`` instead, so it is asserted here directly.
    """

    def test_pyproject_pins_cryptography_floor(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text)
        self.assertIsNotNone(
            match, "pyproject.toml: [project] dependencies block missing"
        )
        self.assertIn('"cryptography>=42"', match.group(1))


class TrackedFileHygieneTests(unittest.TestCase):
    """(c) No host-absolute paths in tracked files.

    Uses the scanner's own ``HOST_PATH_PATTERNS`` and per-match-span
    allowlist suppression, so this meta-test and ``tools/hygiene_scan.py``
    can never disagree about what a host path is or what an allowlist
    entry covers.
    """

    def test_no_host_absolute_paths_in_tracked_files(self) -> None:
        scanner = _load_hygiene_scanner()
        allowlist = scanner.load_allowlist(scanner.ALLOWLIST_PATH)
        compiled = [re.compile(p) for p in scanner.HOST_PATH_PATTERNS]
        hits: list[str] = []
        for path in scanner.tracked_files(REPO_ROOT):
            if scanner.is_binary(path):
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            for lineno, line in scanner.iter_text_lines(path):
                for match in scanner.unsuppressed_hits(line, compiled, allowlist):
                    hits.append(f"{rel}:{lineno}: {match.re.pattern}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
