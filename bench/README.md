# Personal-memory query benchmark

Deterministic component suite for the `agent_personal_memory`
authorized query API (`query_claims`, `query_sources`,
`get_authorized_claim`, `get_authorized_source`). Cases seed an isolated
temp `PersonalMemoryStore` and score include/exclude ids, state labels,
and byte-identical not-found denials. This suite does not run live LLM
evals.

## Component run

From the repository root (with the package installed, e.g.
`pip install .`):

```bash
python bench/run_bench.py component --repeats 1
```

Unittest harness (all corpus cases plus scorer/isolation checks):

```bash
python -m unittest discover -s bench -v
```

No API keys are required. Each case uses a fresh temp directory under
the system temp folder; no host or production data directory is ever
used.

Output directories must be empty. `--force-clean-out` deletes existing
contents. Default artifacts land in `bench/results/component/`
(gitignored: they embed absolute temp paths).

## Corpus versioning

`corpus.json` is a JSON object with:

- `corpus_version` — integer, starts at `1`
- `cases` — array of deterministic fixtures

Each case has a stable `id`, `language` (`ru` or `en`), `category`,
`fixture_id`, prompt metadata (`prompt.language` / `prompt.text`), actor
map, seed (sources, claims, optional mutations), and query expectations.

Categories in version 1:

- `acl` — cross-tenant isolation
- `deleted` — erased claims/sources are invisible
- `stale` — expired and restricted rows are excluded by the default query
- `contradictory` — two active candidates coexist and both return when filtered
- `bilingual` — the same semantic fixture queried with RU vs EN prompt metadata

Versioning rules:

- Bump `corpus_version` when an existing case id changes meaning, seed
  semantics, or expected authorized outcomes.
- New additive cases may land in the current version when they do not
  reinterpret old ids.
- Do not reuse a retired case id inside the same version.
- Prompt text is provenance only. The component runner never sends it to
  a model; authorized results must not depend on prompt language.

This suite was ported from the upstream personal-memory benchmark; see
`docs/PROVENANCE.md` for the source revision and the per-file port
disposition.

Cluster B quality rules (pointer vs copy, honest-zero, access-contract,
rejected alternatives, abstention / authorized zero, and the three
score-locality barriers) live in `QUALITY_CRITERIA.md`. Those rules are
stricter than id-set Recall@k and apply even when this component harness
already passes.

The P1.1 must-run checklist (12 items, separate metric columns, never a
single blended memory score) lives in `CHECKLIST.md`. Checklist fixture
stubs under `cases/` are an optional pack: they are not loaded by
`load_corpus` / `run_bench.py` and must not be added to the frozen
`REQUIRED_CATEGORIES` in `corpus.json`.
