# Layered memory API (0.3)

Inspired by [Iva](https://github.com/smixs/iva-agent),
[agent-second-brain](https://github.com/smixs/agent-second-brain) and
[autograph](https://github.com/smixs/autograph) by **Serge Shima (smixs)**.
The reference is Iva commit `00e357eddae7aacdad3d8fe5509531e2528d3b1d`.
This is an original implementation of those organizational ideas, not copied
Iva source, prompts, Telegram runtime or embedded eve code.

## Host boundary and opt-in

The existing `Actor` and an explicit `PersonalMemoryStore` are required. These
new functions are host APIs; actor, origin and event identity are not model tool
arguments. The host authenticates identity and supplies the user's capture policy.
`CapturePolicy()` disables raw capture. `CapturePolicy(True, 30)` retains events
for 30 days from occurrence; supported range is 1–3650 days. Secret-like text and
explicit English/Russian “do not remember” instructions are excluded. This filter
is defense in depth, not a universal sensitive-data detector.

```python
from agent_personal_memory import (
    Actor, PersonalMemoryStore, CapturePolicy, record_turn,
    remember_from_evidence, recall, evidence_for_claim,
)
from datetime import datetime, timezone

store = PersonalMemoryStore("./example-memory")
actor = Actor("example", "owner")  # authenticated by your host
text = "I prefer tea."
event = record_turn(
    actor, event_id="chat-1/message-7", text=text,
    occurred_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    timezone="Europe/Moscow", policy=CapturePolicy(True, 30), store=store,
)
if event["status"] in {"recorded", "replayed"}:
    claim = remember_from_evidence(
        actor, source_id=event["source_id"], source_version=event["version"],
        start=0, end=len(text), store=store,
    )
    context = recall(actor, query="tea", store=store)
    original = evidence_for_claim(actor, claim["claim_id"], store=store)
```

Record capture alone does not promote every message into a fact. A source span
must be a whole statement from a direct owner message, not an assistant,
forwarded or OCR/STT-derived utterance. Existing `confirm_required` keeps it a
candidate; `auto_insight` uses existing conflict reconciliation before promotion.
Whole-statement validation prevents dropping surrounding negation/conditions;
it is conservative syntax validation, not a proof that the owner is truthful.

| API | Parameters and result |
| --- | --- |
| `record_turn` | Keyword-only `event_id`, `text`, UTC-aware `occurred_at`, `policy`, `store`; optional `timezone='UTC'`, `role='user'`, `origin='direct'`, `session_id=''`, `attachments=()`. Roles: user/assistant/tool. Origins: direct/forwarded/derived/imported. Derived events may bind `parent_source_id` + `parent_source_version`: current same-actor nonredacted journal source, immutable per event. Returns status recorded/replayed/skipped/forgotten; accepted events include source ID/version. Same ID+envelope replays; metadata or text edits append a version and retract prior derived facts. |
| `get_journal_event` | `event_id`, `store`. Resolves authenticated host event identity to source ID/current version/version ID/date/captured generation without plaintext, capture or learning; returns `None` for absent, deleted, redacted or expired originals. |
| `query_journal` | `store`, `limit=50` (1–500), opaque `cursor=None`, optional exact substring `query`, `include_text=False`. Returns items + next_cursor. Pagination/search covers all retained events, not just the newest 500. |
| `remember_from_evidence` | `source_id`, `source_version`, Unicode character `start:end`, `claim_kind='confirmed_fact'`, `store`. Requires current accessible version; refuses overlapping retained fact spans. Returns existing claim result on exact replay. |
| `correct_from_evidence` | Existing claim ID and a new current source version/whole statement span; corrects and binds the new evidence atomically, preserving old lineage for forget. An explicit correction prefix may be omitted. |
| `learn_from_turn` | Existing API; optional `source_id` + `source_version` route exact original turns through journal evidence. Without them it retains legacy insight-source behavior and does not silently enable raw capture. |
| `evidence_for_claim` | Explicit source opening for a current confirmed claim, `max_chars=2000` (1–8000). Returns bounded exact quote, source version/span, role/origin/date. Corrections without fresh evidence return no quote. |
| `build_core` | `char_budget=1200` (0–12000). Current confirmed, nonrestricted, unexpired and valid claims; text + provenance items. |
| `recall` | Required `query`, optional `token_budget=4000` (1–32000), `limit=20` (1–500), `include_journal=False`. Returns core, matched claims/current summaries/one-hop grounded neighbors, budgets. Explicit journal opening returns at most 800 characters per original excerpt. |
| `assemble_context` | Existing API remains compatible; `include_core=True` includes core inside its existing total text budget. |
| `link_memories` | Two current same-actor claim IDs, type related_to/depends_on/same_entity, and direct-owner source version/span containing both exact facts. A name match never automatically merges entities. |
| `export_markdown` | Explicit owner projection as a string with source links. No automatic file creation, Git or network upload. |
| `prune_journal` | Delete expired events/derived facts; optional UTC `now` for a host clock/test. Returns expired count. Host schedules this. |

Token counts are conservative text estimates for context selection, not the
provider's full serialized prompt count. Provenance objects are references for
host rendering and should not all be serialized into a second model prompt.
The core's text consumes the same recall/assembly text budget. Exact-source
opening is separate consent/host policy, not automatic inclusion of full chats.

Generated assistant responses are derived data, not owner facts or delivery
receipts. A reply can reuse an older memory unrelated to its immediate prompt.
Therefore source edit, forget and retention pruning conservatively erase **all
assistant-origin journal records for the same actor**, plus declared derived
children. This intentionally loses unrelated generated responses rather than
retain a paraphrase of forgotten data. Unrelated direct user originals remain;
semantic copies independently re-entered by users and external backups are not
claimed to be detected or erased. Parent bindings prevent stale/foreign replay
and support capture exclusions; they do not prove complete semantic ancestry.
Each captured event also stores the actor's current invalidation generation.
Assistant capture atomically rechecks that host-owned generation: forget,
prune, source edit, correction or retraction invalidates in-flight answers even
when their immediate prompt remains unchanged. Replaying a prompt cannot refresh
its generation. Such a late answer is skipped; a fresh captured turn can proceed.

## Consolidation and hierarchy

`consolidate_period(actor, period=..., start='YYYY-MM-DD', timezone='UTC',
store=..., adapter=None, budget=None)` supports day, Monday-starting week,
first-of-month month and January-1 year. Periods are half-open in the chosen
IANA timezone; Windows installs `tzdata`. Parent projections reference valid
children (day → week → month → year), but leaf IDs are deduplicated within exact
parent boundaries, so an overlapping week does not double-count a month edge.
Coverage reports captured days/events and missing days. A missing day never
means “nothing happened”; an explicitly processed empty period records zero
captured events. `mark_capture_complete(actor, day='YYYY-MM-DD', timezone='UTC',
store=...)` lets the authenticated host attest a closed day's complete capture
window. Only then can a zero-event day be listed as `known_empty_days`; the
worker must not invent this attestation. Late arrivals invalidate it. Today's original events are searchable before consolidation.

`pending_periods(..., timezone='UTC', limit=8)` returns changed/missing child
periods first. A finite host tick processes these and stops; completed unchanged
periods disappear from the list. Late arrivals, edits, corrections, erasure and
retention invalidate affected version fingerprints. No scheduler is installed.

The default adapter-free projection selects exact current confirmed statements,
with no network or model cost. `ConsolidationBudget(max_calls=1,
max_input_tokens=8000, max_output_tokens=2000, max_seconds=10)` bounds each run.
A host can set `max_calls=0` to forbid external calls. Output truncation is explicit
in coverage. Deterministic output uses a conservative UTF-8-byte allowance.

An optional callable `adapter(request) -> response` receives existing facts,
period/bounds, opt-in direct-owner original events and limits; it must return
`selected_claim_ids` (may be empty) and integer
`usage.input_tokens` / `usage.output_tokens`. It selects facts, not free-form
rewrites. Optional `proposals` (at most 20) contain source_id/source_version,
whole-statement start/end and optional claim_kind. After freshness validation they
are persisted atomically as candidates, never automatically promoted. Existing
candidate review supplies the confirmation boundary. Unknown IDs, missing usage,
oversized or late results are rejected. Admission uses request UTF-8 bytes as a
conservative token upper estimate; accounting never trusts a smaller reported
input number. An oversized period is refused before the call; the host must choose smaller
periods/input policy rather than assume every period fits. The host must enforce provider timeout/cancellation and output
limits; the library itself cannot stop an arbitrary hanging Python callable.

Calls run outside the SQLite transaction. A persisted lease blocks a concurrent
same-period run. A crash, timeout or invalid response after paid-call admission
is an **uncertain outcome**: unchanged input is not automatically charged again.
Changed inputs can proceed. The original error/prompt is not persisted. This
avoids an infinite paid retry loop; a host needing an explicit retry should make
a deliberate new admission after resolving the provider outcome.

Statuses: completed (new projection), cached (same inputs, zero calls), pending
(in-progress/error/uncertain outcome), stale (inputs changed before commit),
budget_exhausted. Job state and usage persist; no failed result becomes a fact.

## Privacy, correction and forgetting

Canonical originals and projections use existing KEK-wrapped per-record DEKs.
Optional legacy FTS still contains plaintext claim text; enabling it does not
make the entire SQLite file encrypted. Event identities use actor-bound HMACs.
Internal IDs, timing and job counters remain structural metadata.

Core is rebuilt from current facts. A correction invalidates old summaries and
links by version; old exact evidence is not presented as proof of the corrected
value. Trusted claim deletion redacts the bound character spans in retained
original versions, rotates their DEKs, erases the claim, removes links and clears
rebuildable actor projections/jobs. Equal unrelated text at a different span is
preserved within its original version. Across edited versions, inserted and
replaced regions may contain relocated or rewritten forgotten text; all such
changed regions are conservatively redacted, including punctuation edits and
statement moves. Affected derived
claims lose their stored content rather than retain false evidence. Provably
unrelated equal spans remain. Semantic paraphrases in new independent events
are not automatically identified as the same fact. An opaque event tombstone blocks
redelivery of a forgotten/redacted event. New event IDs are new provenance, not
assumed duplicates. Normal source deletion cascades to dependent claims.

The existing trusted confirmation API remains required for model-initiated
forget. Hosts may bind raw-only originals without synthesizing a claim:
`store.erase_bound_sources(actor, targets)` requires source_id, version,
source_version_id and content_hash for every reviewed current source.
`store.erase_bound_memory(actor, claim_targets=(), source_targets=(),
require_state=None)` validates a mixed exact set before changing any record.
These methods grant no confirmation authority; the host's trusted nonce/preview
must already cover the exact targets. Existing `erase_bound_claims` delegates to
the same atomic boundary. Exported files and old external backups cannot be recalled or erased by
this library; hosts must apply their retention policies. The module does not
publish raw memory into an Obsidian vault or Git without an explicit export.

## Migration and checks

Opening a v1–v5 store upgrades it transactionally to v6. Existing sources, keys
and claims remain. Before upgrading a real store, take a consistent SQLite
backup and retain its existing key. Reverting to old code requires restoring
that matching old database, not reopening v6 with v0.2. No production data is
migrated by the library's unit tests.

```sh
python -m unittest discover -s tests
python -m unittest discover -s bench
python tools/hygiene_scan.py
python bench/layered_bench.py --baseline-src ../memory-baseline/src
```

The last command compares a separately checked-out baseline with current code:
recall, exact original, foreign actor, unknown query and forgotten source span.
It reports context estimates, zero model calls and measured latency. These are
mechanism tests, not a claim about real-model extraction quality or Windows
service integration. No live-model acceptance was performed for this change.
