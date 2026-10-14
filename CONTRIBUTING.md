# Contributing

## Development setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # POSIX
pip install -e ".[mcp]"       # library + optional MCP extra
```

Python 3.10+. The sole runtime dependency is `cryptography>=42`; tests and
the bench need no network access.

## Tests and bench

```bash
python -m unittest discover -s tests
python -m unittest discover -s bench
python bench/run_bench.py component --repeats 1   # optional bench run
```

`tests/` is the regression surface; `bench/` is the deterministic
recall/ACL suite. New behavior should come with tests — negative
(cross-user, denied) cases matter as much as positive ones here.

## Hygiene scan

```bash
python tools/hygiene_scan.py    # or: py -3.12 tools\hygiene_scan.py
```

The scan is CI-enforced: it sweeps every tracked file for host-absolute
paths, secret-shaped literals, and source-host coupling markers, and must
print `hygiene scan: clean`. Intentional retained markers go through
`tools/hygiene_allowlist.txt` (per-match-span suppression — an entry must
match the marker text itself).

## Provenance rule

This package is an extraction from a host project. Every ported file, its
source path, and every deviation from the source snapshot is recorded in
the file map in [docs/PROVENANCE.md](docs/PROVENANCE.md); a meta-test
(`tests/test_ci_requirements.py`) enforces completeness. When you add a
tracked file or change ported behavior, update the table in the same
commit.

## Pull requests

Keep diffs small and coherent. Do not commit secrets, operator paths,
real user data, or generated runtime state (`*.db`, `.venv`, outbox
files). Owner-facing API changes are breaking changes — flag them in the
PR description and record them in `docs/PROVENANCE.md` and
`CHANGELOG.md`.
