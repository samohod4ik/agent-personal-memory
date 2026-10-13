#!/usr/bin/env python3
"""Privacy hygiene scanner for the agent-personal-memory repository.

Scans tracked text files (enumerated via ``git ls-files``) for host-coupling
markers, host-absolute paths and secret-shaped literals.

Allowlist suppression is **per match span** (T12, findings PH1-F-03 /
PH2-F-04): a hit is suppressed only when a regex from
``tools/hygiene_allowlist.txt`` matches text on the same line that *overlaps
the hit's own span*. Entries therefore name the marker text itself (plus
immediate context); a phrase merely appearing elsewhere on the line
suppresses nothing.

The scanner never scans ``.git/``, ``.delivery/``, the allowlist file or
itself. Exits 0 and prints ``hygiene scan: clean`` on pass; on hits it
prints ``<file>:<line>: <pattern>`` per hit and exits 1.

Known limits (independent review is the documented backstop for these):
identifiers assembled by string concatenation or escapes evade the
literal-shape patterns (e.g. an id written as ``"123" + "456"``), opaque
service handles such as ``t.me`` links are not modeled, and text seeded
with NUL bytes or not decodable as UTF-8 is skipped by the line iterator.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = Path(__file__).resolve().parent / "hygiene_allowlist.txt"

# Never scanned even if a path under them were tracked.
EXCLUDED_DIRS = (".git", ".delivery")
# The scanner must not flag its own pattern definitions or the allowlist.
EXCLUDED_FILES = frozenset({Path(__file__).resolve(), ALLOWLIST_PATH})

# Host-absolute paths: any drive letter with either separator, and POSIX
# user-home roots. The ``(?<![\w])`` lookbehind keeps URI schemes like
# ``https://`` or ``file://`` (letter preceded by a word char) unflagged.
HOST_PATH_PATTERNS: tuple[str, ...] = (
    r"(?<![\w])[A-Za-z]:[\\/]",
    r"(?<![\w])/(?:home|Users)/",
)

# Secret-name alternation shared by the assignment shapes below. A bare
# ``key`` term is deliberately absent (``primary_key``/``sort_key`` are not
# credentials); the name must end in a listed credential word.
_SECRET_NAME = (
    r"api[_-]?key|secret[_-]?key|access[_-]?key|private[_-]?key"
    r"|client[_-]?secret|secret|token|password|passwd"
)
# Credential-shaped value run used by the unquoted branch: requires at least
# one digit so ``token = generate()``-style code is not flagged.
_SECRET_VALUE_CHARS = r"A-Za-z0-9_.\-"

# Forbidden-content patterns (regular expressions searched per line).
SCAN_PATTERNS: tuple[str, ...] = (
    # Host-coupling markers.
    r"from\s+gateway",
    r"import\s+gateway",
    r"gateway\.session_context",
    r"HERMES_HOME",
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
    # Host-absolute paths (PH1-F-01/PH2-F-03: generic drive letters, both
    # separators, plus POSIX user-home roots).
    *HOST_PATH_PATTERNS,
    # Real telegram identifiers; the allowlisted ``telegram:{label}:{digest}``
    # external_ref shape is non-numeric and stays unflagged.
    r"\btelegram:\d",
    # Numeric owner/chat identifiers under their canonical key names only,
    # so ``version=1``, ``port=8080``, ``max_limit=...`` stay unflagged.
    r"\b(?:chat_id|user_id)\s*[:=]\s*[\"']?\d{6,}",
    # Secret-shaped assignments (PH1-F-02): ``name = "…"``, ``name: "…"``
    # (YAML/env style), ``name => "…"`` and bare ``NAME=value`` with a
    # digit-bearing value; the word boundary after the name keeps
    # ``tokenize``/``secretion`` out, and no leading ``\b`` keeps
    # ``session_token``/``mysecret`` in.
    r"(?i)(?:" + _SECRET_NAME + r")\b['\"]?\s*(?:=>|[:=])\s*['\"][^'\"]{8,}",
    r"(?i)(?:" + _SECRET_NAME + r")\b['\"]?\s*(?:=>|[:=])\s*"
    r"(?=[" + _SECRET_VALUE_CHARS + r"]{8,}\b)[" + _SECRET_VALUE_CHARS + r"]*\d[" + _SECRET_VALUE_CHARS + r"]*",
    # Short string multiplied into a secret: ``secret = "x" * 32``.
    r"(?i)(?:" + _SECRET_NAME + r")\b['\"]?\s*(?:=>|[:=])\s*['\"][^'\"]{0,16}['\"]\s*\*\s*\d+",
    # Well-known token literals (PH1-F-02).
    r"\bsk-[A-Za-z0-9_\-]{8,}",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    r"\b[Bb]earer[ \t]+[A-Za-z0-9][A-Za-z0-9_.\-~=]{9,}",
    r"\bgh[pousr]_[A-Za-z0-9]{16,}",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",
    r"\bxox[baprs]-[A-Za-z0-9\-]{8,}",
    r"\bAIza[0-9A-Za-z_\-]{30,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY[A-Z ]*-----",
    # JWT (header.payload.signature) literals.
    r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}",
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


def _spans_overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def unsuppressed_hits(
    line: str,
    compiled: Sequence[re.Pattern[str]],
    allowlist: Sequence[re.Pattern[str]],
) -> Iterator[re.Match[str]]:
    """Yield scan-pattern matches not covered by an allowlist span.

    Per-match-span suppression: an allowlist regex must match text that
    overlaps the hit's own span on the same line — merely appearing on the
    same line no longer masks unrelated hits.
    """
    allow_spans = [m.span() for entry in allowlist for m in entry.finditer(line)]
    for pattern in compiled:
        for match in pattern.finditer(line):
            if any(_spans_overlap(match.span(), span) for span in allow_spans):
                continue
            yield match


def main() -> int:
    compiled = [re.compile(p) for p in SCAN_PATTERNS]
    allowlist = load_allowlist(ALLOWLIST_PATH)
    hits = 0
    for path in tracked_files(REPO_ROOT):
        if is_binary(path):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in iter_text_lines(path):
            for match in unsuppressed_hits(line, compiled, allowlist):
                print(f"{rel}:{lineno}: {match.re.pattern}")
                hits += 1
    if hits:
        print(f"hygiene scan: {hits} hit(s)", file=sys.stderr)
        return 1
    print("hygiene scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
