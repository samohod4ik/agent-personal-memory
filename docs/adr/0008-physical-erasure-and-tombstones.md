# ADR 0008: Physical erasure and tombstones

Status: Accepted
Date: 2026-09-01

This ADR records the Task 11 physical-erasure contracts that follow ADR 0002
and the per-record wrapped DEK model in ADR 0005. Schema version becomes 3.

## Context

Task 10 persisted versioned claims with per-version DEKs. Task 11 must make
owner-initiated deletion immediately exclude reads, destroy active content and
key material, cascade from a deleted source to dependent claims, and keep only
an anonymized tombstone. The plugin stays disabled. `register(ctx)` remains a
no-op. Telegram and production config are unchanged.

## Decision

### APIs

`erase_claim` and `erase_source` run in one `BEGIN IMMEDIATE` transaction and
return a frozen `ErasureReceipt` with `operation` `claim.erase` or
`source.erase`, `target_kind` `claim` or `source`, a random `receipt_id` that
is not the erased id, `outcome` `erased`, RFC3339 Z `erased_at`, and a backup
retention note. Missing and foreign ids raise the byte-identical
`NotFoundError("Claim not found")` or `NotFoundError("Source not found")`.

### In-transaction destruction

Inside the deleting transaction the store marks the target deleted, nulls
content blobs for every version, destroys every matching `memory_keys` row
after overwriting the wrapped DEK, and purges `memory_support` edges. Source
erasure also clears `keyed_lookup_hash` so the partial unique index can accept
a later re-registration. Cryptographic erasure is DEK destruction, not a
filesystem overwrite claim.

### Deterministic cascade

`erase_source` erases every same-tenant claim linked through `memory_support`.
Those claims become non-retrievable and lose content plus keys. Other sources
that also supported a cascaded claim are left in place. `erase_claim` does not
delete sources.

### Tombstones and schema v3

Audit and `memory_deletion_manifest` store only anonymized receipt metadata:
operation, target kind, receipt id, outcome, and timestamp. They do not store
plaintext, ciphertext, wrapped DEKs, or original claim/source ids. Version 3
adds the additive `memory_deletion_manifest` table. Restore in a later task
must consult this manifest before deleted data can become available again.

### Connection maintenance

Every store connection enables `PRAGMA secure_delete=ON`. After a successful
erasure commit the store serializes compaction under the same lock as `_tx`
and runs `PRAGMA wal_checkpoint(TRUNCATE)`, `VACUUM`, then a trailing
`PRAGMA wal_checkpoint(TRUNCATE)` so purged pages are not left in the main
DB, WAL, SHM, or freelist. Compaction `OperationalError` after commit is
logged and does not revoke the `ErasureReceipt`. Tests scan those files for
fixture plaintext, captured ciphertext, and wrapped DEK bytes.

### Backup honesty

The receipt states that encrypted backups may retain ciphertext until the
recorded retention policy expires. Documentation does not claim pre-expiry
physical purge of backups. A concrete numeric retention period remains an M1
human gate before production enablement.

## Consequences

Reads exclude erased rows immediately. Source deletion removes derived claims
from the active store. Active-store scans after compaction must not find
erased content or keys. Older encrypted backups can still decrypt those rows
until retention expires. Export and Telegram enablement remain later tasks.
