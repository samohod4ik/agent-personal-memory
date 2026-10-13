# Personal-memory P1.1 must-run checklist

This is the P1.1 quality gate for the `bench/` personal-memory suite.
It is a **columnar** scorecard, not a Recall@k leaderboard and not a
single blended "memory score."

**Metrics MUST stay separate columns.**

- Recall@k is not honest-zero.
- Honest-zero is not abstention.
- Abstention is not evidence-grounded answer rate.
- Score-locality (ids / scores / order) are three comparisons, not one flag.

Never collapse these into one blended memory score. A green Recall@k
column cannot hide a failed honest-zero, abstention, or ACL column.
Cluster B pass/fail in `QUALITY_CRITERIA.md` still applies even when a
retrieval metric looks strong.

Related:

- Quality criteria: `bench/QUALITY_CRITERIA.md`
- Frozen component corpus: `bench/corpus.json`
  (`REQUIRED_CATEGORIES` = `{acl, deleted, stale, contradictory, bilingual}`)
- Checklist fixture stubs (optional pack, not loaded by `load_corpus`):
  `bench/cases/`
- Tenant-local rescoring: `src/agent_personal_memory/score_locality.py`

## Criterion ids

| Id | Cluster B meaning |
|----|-------------------|
| `C-POINTER` | Pointer is not copy; source-hash / derived-row integrity |
| `C-HONEST-ZERO` | Authorized-empty is an honest zero, not green Recall@k |
| `C-ACCESS` | Access-contract: auth-before-decrypt, valid-window, receipts |
| `C-REJECTED` | Rejected / superseded alternatives stay evaluable |
| `C-ABSTAIN` | False-premise / unknown → named abstention |
| `C-SCORE-LOC` | Tenant-local DF/IDF; freeze-A / mutate-B falsifier |
| `C-ACL-3WAY` | Tenant A never sees B plaintext, ids, or existence-leaking scores |

## Must-run items

### 1. Answerable authorized

Authorized questions that have a live pointer must retrieve it and
ground the answer on that evidence.

- **Metrics (separate columns):** Recall@k; evidence-grounded answer rate.
- **Criterion:** `C-ACCESS`
- **Case / test ids:** `bilingual_same_fixture_en`,
  `bilingual_same_fixture_ru`, `test_all_corpus_cases_pass`
- **Pass:** gold ids are in the authorized hit set; the answer cites those
  pointers. **Fail:** high lexical overlap with no live `claim_id`.

### 2. Honest zero

Absent, expired, deleted, or unauthorized targets must score as
`no_memory`. Inventing a fact is a hard fail.

- **Metrics (separate columns):** no_memory accuracy; false-memory rate
  (must be 0). Do not report Recall@k = 1.0 on empty gold.
- **Criterion:** `C-HONEST-ZERO`
- **Case / test ids:** `honest_zero_absent`
  (`cases/honest_zero_absent.json`); also `deleted_erased_rows_en`,
  `stale_expired_default_en`, `acl_cross_tenant_en`
- **Pass:** authorized-empty → honest zero. **Fail:** fabricated memory
  or vacuous green Recall@k.

### 3. Abstention

False-premise and unknown questions must abstain. This is not the same
column as honest-zero (empty authorized set vs "the question is not
answerable as asked").

- **Metrics (separate columns):** abstain rate on false-premise / unknown;
  never fold into Recall@k.
- **Criterion:** `C-ABSTAIN`
- **Case / test ids:** `abstention_unknown`
  (`cases/abstention_unknown.json`)
- **Pass:** named abstention payload. **Fail:** guessed neighbor or
  generated fact with no pointer.

### 4. Conflict / supersession

When facts conflict, the latest valid fact wins. Stale answers are a
separate error column.

- **Metrics (separate columns):** latest-valid-fact accuracy;
  stale-answer rate.
- **Criterion:** `C-REJECTED`
- **Case / test ids:** `contradictory_two_candidates_en` (today: two
  active candidates may coexist);
  `rejected_alternatives` (`cases/rejected_alternatives.json`) for
  superseded options that must remain addressable
- **Pass:** latest valid pointer is preferred; rejected ids stay
  evaluable. **Fail:** stale fact answered as current.

### 5. Temporal

Time-qualified questions must use validity / expiry windows, not "any
matching string."

- **Metrics (separate columns):** time-qualified accuracy.
- **Criterion:** `C-ACCESS`
- **Case / test ids:** `stale_expired_default_en`,
  `stale_expired_default_ru`, `test_stale_and_restricted_are_excluded_by_default`
- **Pass:** expired / out-of-window rows stay out of the default set.
  **Fail:** last year's preference answered as current.

### 6. ACL isolation

Tenant A never sees tenant B plaintext, ids, or scores that leak
existence.

- **Metrics (separate columns):** 3-way isolation — plaintext, ids,
  scores. A Candidate-ACL pass with a leaked score is still a fail.
- **Criterion:** `C-ACL-3WAY`
- **Case / test ids:** `acl_cross_tenant_en`, `acl_cross_tenant_ru`,
  `test_denied_gets_are_byte_identical_to_missing`,
  `test_cross_tenant_candidate_is_rejected`,
  `test_store_principal_wins_over_caller_tenant_id`
- **Pass:** denied gets are byte-identical to missing; foreign ids and
  scores are absent. **Fail:** existence leak via score, log, or snippet.

### 7. Pointer integrity

A memory row is a pointer. Mutating the source hash must mark the
derived row stale. No silent trust of a rewritten blob.

- **Metrics (separate columns):** derived-stale detection rate.
- **Criterion:** `C-POINTER`
- **Case / test ids:** `pointer_stale` (`cases/pointer_stale.json`)
- **Pass:** source-hash mismatch → derived row stale; retrieval does not
  treat the new bytes as the same memory. **Fail:** silent trust.

### 8. Rejected alternatives

Explain / export must still list rejected options after compaction.
Rejection is not the same as forget.

- **Metrics (separate columns):** rejected-options retained on
  explain/export; default retrieval must not re-promote them.
- **Criterion:** `C-REJECTED`
- **Case / test ids:** `rejected_alternatives`
  (`cases/rejected_alternatives.json`)
- **Pass:** rejected ids remain addressable in review traces.
  **Fail:** physical erase as the only reject path, or re-index of
  rejected plaintext.

### 9. Erasure

`forget` / `do_not_remember` must purge FTS and survive a residual
probe. Residual hits are false memory.

- **Metrics (separate columns):** FTS purge success; residual-probe
  miss rate (must be 0 hits). Not the same column as honest-zero on
  never-written facts.
- **Criterion:** `C-HONEST-ZERO`
- **Case / test ids:** `deleted_erased_rows_en`,
  `deleted_erased_rows_ru`, `test_deleted_rows_are_invisible`
- **Pass:** erased claim/source ids are invisible to query and get.
  **Fail:** FTS residual or reconstructed snippet.

### 10. Score-locality falsifier

Freeze tenant A rows and query. Mutate only tenant B's corpus.
Compare **ids**, **scores**, and **order** as three separate checks.

- **Metrics (separate columns):** id invariance; score invariance;
  order invariance. Do not OR them into one "locality" bit.
- **Criterion:** `C-SCORE-LOC`
- **Case / test ids:** `score_locality_falsifier`
  (`cases/score_locality_falsifier.json`);
  `test_score_does_not_use_shared_df_pollution`
- **Pass:** A's ids, tenant-local scores, and order are unchanged.
  **Fail:** shared DF moves A's rank or score.

### 11. Receipt fields

A finished run is not a verified run. Receipts must carry `stop_reason`
and coverage counts so Done is not confused with Verified.

- **Metrics (separate columns):** receipt completeness (`stop_reason`,
  coverage counts); Done ≠ Verified.
- **Criterion:** `C-ACCESS`
- **Case / test ids:** `receipt_stop_reason` (checklist item; no
  production corpus case yet). Suite mapping lives with the verdict
  rules in `QUALITY_CRITERIA.md` ("How to read a verdict").
- **Pass:** every scored case records why it stopped and what it
  covered. **Fail:** a green pass_rate with no stop/coverage receipt.

### 12. Mem0 shadow non-SoT

Native write path is canonical. Mem0 is shadow only and never the
source of truth.

- **Metrics (separate columns):** native-write assertion; Mem0-canonical
  rate (must be 0).
- **Criterion:** `C-POINTER`
- **Case / test ids:** `mem0_shadow_non_sot` (checklist item; see
  `src/agent_personal_memory/engines/mem0_shadow.py`). Quality anti-pattern:
  "Returning Mem0 / shadow text without a live authorized pointer."
- **Pass:** persist goes through native `create_candidate` /
  authorized pointer. **Fail:** Mem0 text treated as canonical memory.

## Fixture pack vs frozen corpus

`cases/*.json` stubs are checklist fixtures. They are **not** registered
in `corpus.json` and must not be added to `REQUIRED_CATEGORIES`. Adding
a new category there breaks `test_corpus_version_and_required_strata`.

`load_corpus()` continues to read only `corpus.json`. The stubs omit
`corpus_version` / `cases` so they cannot be mistaken for a production
pack.

When a stub later becomes a runnable corpus case, bump or extend
`corpus.json` in a dedicated change; do not silently widen version 1
strata.
