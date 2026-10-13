#!/usr/bin/env python3
"""Privacy hygiene scanner for the agent-personal-memory repository.

Scans tracked text files (enumerated via ``git ls-files``) for host-coupling
markers, host-absolute paths and secret-shaped literals. A hit is suppressed
when the source line containing it matches a regex from
``tools/hygiene_allowlist.txt`` (intentional retained markers, documented in
``docs/PROVENANCE.md``).

The scanner never scans ``.git/``, ``.delivery/``, the allowlist file or
itself. Exits 0 and prints ``hygiene scan: clean`` on pass; on hits it
prints ``<file>:<line>: <pattern>`` per hit and exits 1.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = Path(__file__).resolve().parent / "hygiene_allowlist.txt"

# Never scanned even if a path under them were tracked.
EXCLUDED_DIRS = (".git", ".delivery")
# The scanner must not flag its own pattern definitions or the allowlist.
EXCLUDED_FILES = frozenset({Path(__file__).resolve(), ALLOWLIST_PATH})

# Forbidden-content patterns (regular expressions searched per line).
SCAN_PATTERNS: tuple[str, ...] = (
    r"from\s+gateway",
    r"import\s+gateway",
    r"gateway\.session_context",
    r"HERMES_HOME",
    r"F:\\Hermes",
    r"G:\\",
    r"C:\\Users\\",
    r"plugins\.personal_memory\.",
    r"\.inject\b",
    r"peer_import",
    r"adapters\.travel",
    r"travel_compute",
    r"miniapp_",
    r"night_cycle1",
    r"spikes",
    r"pre_gateway_dispatch",
    r"plugin\.yaml",
    r"(?i)(api[_-]?key|secret|token|password)\s*=\s*['\"][^'\"]{8,}",
)


def load_allowlist(path: Path) -> list[re.Pattern[str]]:
    """Parse the allowlist file.

    Grammar: blank lines and lines starting with '#' are comments; every
    other line is '<regex>' or '<regex><TAB><reason>' (' # <reason>' also
    accepted as a separator).
    """
    patterns: list[re.Pattern[str]] = []
    if not path.is_file():
        return patterns
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            pattern = line.split("\t", 1)[0].strip()
        elif " #" in line:
            pattern = line.split(" #", 1)[0].strip()
        else:
            pattern = line
        try:
            patterns.append(re.compile(pattern))
        except re.error as exc:
            print(
                f"{path.name}:{lineno}: invalid allowlist regex {pattern!r}: {exc}",
                file=sys.stderr,
            )
            raise SystemExit(2) from exc
    return patterns


def tracked_files(root: Path) -> list[Path]:
    """Return tracked files under ``root`` via ``git ls-files``."""
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    files: list[Path] = []
    for name in proc.stdout.decode("utf-8", "replace").split("\0"):
        if not name:
            continue
        parts = Path(name).parts
        if any(part in EXCLUDED_DIRS for part in parts):
            continue
        resolved = (root / name).resolve()
        if resolved in EXCLUDED_FILES:
            continue
        files.append(resolved)
    return files


def is_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:8192]
    except OSError:
        return True
    return b"\x00" in chunk


def iter_text_lines(path: Path):
    """Yield (lineno, line) for UTF-8 text files; skip undecodable files."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    yield from enumerate(text.splitlines(), 1)


def main() -> int:
    compiled = [re.compile(p) for p in SCAN_PATTERNS]
    allowlist = load_allowlist(ALLOWLIST_PATH)
    hits = 0
    for path in tracked_files(REPO_ROOT):
        if is_binary(path):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in iter_text_lines(path):
            if any(al.search(line) for al in allowlist):
                continue
            for pattern in compiled:
                if pattern.search(line):
                    print(f"{rel}:{lineno}: {pattern.pattern}")
                    hits += 1
    if hits:
        print(f"hygiene scan: {hits} hit(s)", file=sys.stderr)
        return 1
    print("hygiene scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
