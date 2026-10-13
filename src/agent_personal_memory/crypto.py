"""AES-GCM helpers with a KEK-wrapped per-record DEK."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_SIZE = 12
KEY_SIZE = 32
FIELD_SNAPSHOT = "snapshot_enc"
FIELD_LOCATOR = "locator_enc"
FIELD_METADATA = "metadata_enc"
FIELD_CONTENT = "content_enc"
FIELD_EXPLANATION = "explanation_enc"
FIELD_LABELS = frozenset(
    {FIELD_SNAPSHOT, FIELD_LOCATOR, FIELD_METADATA, FIELD_CONTENT, FIELD_EXPLANATION}
)
LOOKUP_INFO = b"hermes.personal_memory.lookup.v1"


class CryptoError(Exception):
    """Raised when key load or AES-GCM authenticated encryption fails."""


def ensure_master_key(key_path: Path) -> bytes:
    """Load or create a 256-bit KEK. The KEK never encrypts record content."""
    key_path = Path(key_path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if key_path.exists():
        key = key_path.read_bytes()
        if len(key) != KEY_SIZE:
            raise CryptoError(f"Invalid master key length at {key_path}: {len(key)}")
        return key
    key = secrets.token_bytes(KEY_SIZE)
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
    except Exception:
        try:
            key_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return key


def record_aad(tenant_id: str, entity_kind: str, entity_id: str, version: int) -> bytes:
    return f"{tenant_id}\0{entity_kind}\0{entity_id}\0{version}".encode("utf-8")


def _encrypt_bytes(key: bytes, plaintext: bytes, associated_data: bytes) -> bytes:
    if len(key) != KEY_SIZE:
        raise CryptoError("AES-GCM key must be 32 bytes")
    nonce = secrets.token_bytes(NONCE_SIZE)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, associated_data)


def _decrypt_bytes(key: bytes, blob: bytes, associated_data: bytes) -> bytes:
    if len(key) != KEY_SIZE:
        raise CryptoError("AES-GCM key must be 32 bytes")
    if len(blob) < NONCE_SIZE + 16:
        raise CryptoError("Ciphertext too short")
    nonce, ciphertext = blob[:NONCE_SIZE], blob[NONCE_SIZE:]
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, associated_data)
    except Exception as exc:
        raise CryptoError("Decryption failed") from exc


def wrap_dek(kek: bytes, dek: bytes, associated_data: bytes) -> bytes:
    if len(dek) != KEY_SIZE:
        raise CryptoError("DEK must be 32 bytes")
    return _encrypt_bytes(kek, dek, associated_data)


def unwrap_dek(kek: bytes, wrapped: bytes, associated_data: bytes) -> bytes:
    dek = _decrypt_bytes(kek, wrapped, associated_data)
    if len(dek) != KEY_SIZE:
        raise CryptoError("Unwrapped DEK must be 32 bytes")
    return dek


def encrypt_record(
    kek: bytes,
    plaintext: bytes,
    *,
    tenant_id: str,
    entity_kind: str,
    entity_id: str,
    version: int,
) -> tuple[bytes, bytes]:
    dek = secrets.token_bytes(KEY_SIZE)
    aad = record_aad(tenant_id, entity_kind, entity_id, version)
    wrapped = wrap_dek(kek, dek, aad)
    ciphertext = _encrypt_bytes(dek, plaintext, aad)
    return wrapped, ciphertext


def decrypt_record(
    kek: bytes,
    wrapped: bytes,
    blob: bytes,
    *,
    tenant_id: str,
    entity_kind: str,
    entity_id: str,
    version: int,
) -> bytes:
    aad = record_aad(tenant_id, entity_kind, entity_id, version)
    dek = unwrap_dek(kek, wrapped, aad)
    return _decrypt_bytes(dek, blob, aad)


def field_aad(
    tenant_id: str, entity_kind: str, entity_id: str, version: int, field: str
) -> bytes:
    if field not in FIELD_LABELS:
        raise CryptoError("Unknown encrypted field label")
    return record_aad(tenant_id, entity_kind, entity_id, version) + b"\0" + field.encode(
        "utf-8"
    )


def new_dek() -> bytes:
    return secrets.token_bytes(KEY_SIZE)


def encrypt_field(dek: bytes, plaintext: bytes, associated_data: bytes) -> bytes:
    return _encrypt_bytes(dek, plaintext, associated_data)


def decrypt_field(dek: bytes, blob: bytes, associated_data: bytes) -> bytes:
    return _decrypt_bytes(dek, blob, associated_data)


def derive_lookup_key(kek: bytes) -> bytes:
    if len(kek) != KEY_SIZE:
        raise CryptoError("AES-GCM key must be 32 bytes")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=None,
        info=LOOKUP_INFO,
    ).derive(kek)


def keyed_lookup_hash(
    lookup_key: bytes, tenant_id: str, source_kind: str, external_ref: str
) -> str:
    payload = f"{tenant_id}\0{source_kind}\0{external_ref}".encode("utf-8")
    return hmac.new(lookup_key, payload, hashlib.sha256).hexdigest()
