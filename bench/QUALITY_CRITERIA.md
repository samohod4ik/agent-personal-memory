# Personal-memory quality criteria (Cluster B)

This document is the Cluster B gate for `agent_personal_memory`
retrieval quality. It is a scoring contract, not a Recall@k leaderboard.

The component harness in this directory (`README.md`, `corpus.json`,
`score.py`) already proves authorized include/exclude ids, state labels, and
byte-identical not-found denials. Cluster B adds quality rules that those
id-set checks do not cover. Mail, mailbox, and OAuth are out of scope.

Related:

- Component bench: `bench/README.md` (ported from upstream
  `evals/personal-memory-bench/README.md` @ `f0f6908`)
- Authorized query API: `src/agent_personal_memory/query.py`
- Ranking / FTS: `src/agent_personal_memory/ranking.py`,
  `src/agent_personal_memory/fts_index.py`
- Tenant-local rescoring: `src/agent_personal_memory/score_locality.py`

## How to read a verdict

A case **passes** only when every applicable criterion below is satisfied.
A case **fails** if any anti-pattern fires, even when Recall@k, nDCG, or
lexical overlap look strong.

Honest empty results are not failures. Vacuous green scores are.

---

## 1. Pointer is not copy

A retrieved memory is a **pointer**: an authorized `claim_id` /
`claim_version_id` (and optional source id) plus the minimized fields the
actor is allowed to see. It is not a copy of raw chat, files, email, or
another tenant's snapshot.

**Pass**

- Returned rows identify ledger records the actor may read.
- Payload fields are the authorized claim/source projection, not a second
  stored copy of the original blob.
- Provenance stays on ids, versions, and hashes. The model does not get a
  reconstructed source dump "because it ranked well."

**Fail**

- Retrieval materializes raw source snapshots, chat transcripts, or file
  bytes as if they were the memory.
- A high-ranking snippet is assembled from text that is not on the
  authorized pointer.
- Cross-tenant or erased content is inlined to "help" the answer.

**Anti-patterns**

- Treating embedding neighbors as owned memories.
- Caching decrypted FTS `body` as a durable second copy of the claim.
- Returning Mem0 / shadow text without a live authorized pointer.

---

## 2. Honest-zero is not green Recall@k

When the authorized set is empty, the correct score is an **honest zero**
(or abstention). Do not convert "nothing authorized matched" into
Recall@k = 1.0, nDCG = 1.0, or a green pass.

**Pass**

- Empty authorized result + empty gold set => recorded as authorized-zero /
  abstention, not as a perfect retrieval score.
- Empty authorized result + non-empty gold set => fail the case, score 0.
- Metrics that divide by `|relevant|` are undefined on empty gold; they
  must not default to 1.0.

**Fail**

- Vacuous Recall@k: `|relevant| = 0` reported as 1.0.
- "No leak" used as a substitute for "found the answer."
- A denied / not-found path marked passed because the model stayed silent
  on a question that had an authorized answer.

**Anti-patterns**

- Macro-averaging Recall@k across abstention cases until the suite looks
  green.
- Dropping honest-zero cases from the denominator.
- Scoring only queries that have at least one hit.

---

## 3. Access-contract

Auth-before-decrypt and tenant binding are quality criteria, not only
security tests. A ranker that finds the right fact for the wrong actor
fails Cluster B.

**Pass**

- Every candidate is resolved through the actor-bound query API
  (`query_claims`, `get_authorized_claim`, or an equivalent ACL filter).
- Cross-tenant, missing, restricted, expired, and erased targets produce
  the same not-found denial as an unknown id.
- Tools and hooks never accept a model-supplied tenant or user selector.

**Fail**

- A foreign `claim_id` appears in hits, scores, or snippets.
- Decrypt or FTS plaintext is read before authorization.
- Companion / shared-trip context opens owner-private memory.

**Anti-patterns**

- Global FTS `MATCH` then "filter later" without Candidate-ACL.
- Logging target ids or plaintext on denied lookups.
- Trusting hit `tenant_id` from the caller instead of the session actor.

The existing `acl` / `deleted` / `stale` corpus categories are the
id-level access-contract suite. Score-locality (section 6) is the ranking
layer of the same contract.

---

## 4. Rejected alternatives kept

A rejected, superseded, or corrected alternative stays on the ledger as a
non-retrieved alternative. Quality scoring must still be able to name it.

**Pass**

- Rejected / superseded ids remain addressable in fixtures and review
  traces.
- Default retrieval does not surface them as live memory.
- An eval can assert "this alternative was considered and rejected"
  without requiring the text to re-enter the prompt.

**Fail**

- Rejection deletes the only record that the alternative ever existed.
- The ranker re-promotes a rejected alternative because it still matches
  the query.
- The suite cannot distinguish "never learned" from "learned and rejected."

**Anti-patterns**

- Using physical erase as the only reject path when the quality question
  is "did we keep the alternative?"
- Collapsing reject and forget into one score bucket.
- Re-indexing rejected plaintext into FTS.

Note: `reject_candidate_memory` no longer erases from a model `confirm`
path. Preview plus the operator-confirmed consume
(`consume_delete_request`, surfaced as the `personal_memory_confirm`
tool) is the only public erase. Cluster B still requires fixtures and
reviews to keep the rejected alternative as an evaluable id, not as live
retrieval.

---

## 5. Abstention / authorized zero

Abstention is a first-class authorized outcome: the actor is allowed to
see nothing, and the system says so.

**Pass**

- Authorized-empty queries return `[]` (or an equivalent abstention
  payload), not a guessed neighbor.
- Denied gets stay byte-identical `NotFoundError` strings.
- Bench scoring records abstention separately from "retrieved and wrong."

**Fail**

- Filling the slot with a global / other-tenant / stale hit so the model
  has "something to say."
- Turning abstention into a generated fact with no pointer.
- Treating authorized zero as an infrastructure error.

**Anti-patterns**

- Fallback to unauthenticated search when the tenant index is empty.
- Injecting travel or shared context as a stand-in for personal memory.
- Counting abstention as Recall@k success (see honest-zero).

---

## 6. Score-locality is three barriers

"Score-locality" is not a single BM25 flag. It is three barriers that
must all hold. Shared FTS document frequency is not tenant-local BM25.

### 6.1 Candidate-ACL

Only hits that belong to the actor's tenant may enter the scored set.

**Pass:** foreign `tenant_id` / unauthorized `claim_id` is dropped before
score compare. **Fail:** a cross-tenant row influences rank, cutoff, or
"no result" decisions. **Anti-pattern:** shared posting list, ACL applied
only after top-k.

### 6.2 Score-locality (tenant-local DF / IDF)

Scores are computed from the **authorized tenant corpus only**.

**Pass:** IDF / BM25 / rarity uses tenant-local document frequency.
Adding another tenant's documents must not change this tenant's scores.
**Fail:** `bm25(shared_fts_table)` (or any global DF) moves a tenant's
hits because other tenants used the same terms. **Anti-pattern:** one
FTS5 table for every tenant with `tenant_id UNINDEXED` and
`ORDER BY bm25(table)` — SQLite DF is table-global even when `WHERE
tenant_id = ?` filters rows.

Verified on the ported package: `src/agent_personal_memory/fts_index.py`
builds a single `memory_claim_fts_index` and ranks with
`bm25(memory_claim_fts_index)`. Native `rank_claims` does not use IDF
(it is lexical overlap over `query_claims`). Do not add a second index
to "fix" DF; recompute or replace the score with a tenant-local function
(`score_locality`). When a store is passed, that function binds tenant
identity with `store.ensure_principal(actor)` and ignores a
caller-supplied `tenant_id`.

### 6.3 Snippet-ACL

Text shown to the model or user must come from an authorized pointer.

**Pass:** snippet / highlight / `body` is clipped to authorized claim
content. **Fail:** a leaked foreign sentence remains in the snippet after
the id was filtered. **Anti-pattern:** FTS highlight across the shared
`body` column; concatenating unauthorized hits "for context."

### Combined pass/fail

| Barrier        | Pass                         | Fail                                      |
|----------------|------------------------------|-------------------------------------------|
| Candidate-ACL  | Cross-tenant hit dropped     | Foreign id in the candidate set           |
| Score-locality | Score invariant to shared DF | Shared DF changes tenant rank             |
| Snippet-ACL    | Unauthorized tokens stripped | Foreign text survives in snippet / body   |

All three must pass. Winning Candidate-ACL and still ranking with shared
DF is a Cluster B fail.

---

## Suite mapping

| Criterion              | Where it is enforced today                          | Cluster B addition                         |
|------------------------|-----------------------------------------------------|--------------------------------------------|
| Pointer != copy        | Authorized query + assembler projection             | Ban copy-as-memory in evals                |
| Honest-zero            | Empty `[]` from query; bench does not award Recall@k | Never treat empty gold as 1.0              |
| Access-contract        | `acl` / `deleted` / `stale` corpus cases            | Apply the same contract to ranker hits     |
| Rejected alternatives  | State labels + review/erase fixtures                | Keep rejected ids evaluable                |
| Abstention             | Byte-identical not-found; empty lists               | Named authorized-zero outcome              |
| Score-locality (3)     | Query ACL; FTS still shared-DF                      | `score_locality` Candidate / DF / snippet  |

Component mode remains the merge gate. Live LLM scoring is additive and
must not override a Cluster B fail.
