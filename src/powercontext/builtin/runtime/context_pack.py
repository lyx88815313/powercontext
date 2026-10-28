# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Context Pack: a chunked, verifiable wire form for one prepared context.

A Context Pack carries one prepared context as an ordered list of byte chunks.
The first exchange sends the whole pack; later exchanges send a delta that keeps
a prefix of the previous pack and carries only the new suffix, so a receiver
that already holds the previous pack rebuilds the current context from the
serialized bytes alone.

The wire form is canonical JSON — sorted keys, no insignificant whitespace — so
both peers derive the same bytes and the same SHA-256 digest from the same
chunk list, and a receiver can reject a tampered or mismatched payload before
it trusts the content.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "DEFAULT_CHUNK_BYTES",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
]

CONTEXT_PACK_SCHEMA: Final[str] = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA: Final[str] = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES: Final[int] = 512

_DIGEST_PREFIX: Final[str] = "sha256:"
_HEX_DIGITS: Final[str] = "0123456789abcdef"
_MAX_BYTE_COUNT_DIGITS: Final[int] = 20


class ContextPackError(Exception):
    """A context pack or delta violated the wire contract."""


def _canonical(payload: dict[str, object]) -> bytes:
    """Serialize one wire payload the way every peer must."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encode_items(items: Sequence[bytes]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(values: object) -> tuple[bytes, ...]:
    if not isinstance(values, list):
        raise ContextPackError("pack-items-not-a-list")
    decoded: list[bytes] = []
    for value in values:
        if not isinstance(value, str):
            raise ContextPackError("pack-item-not-a-string")
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ContextPackError("pack-item-not-base64") from error
        if base64.b64encode(raw).decode("ascii") != value:
            raise ContextPackError("pack-item-not-canonical-base64")
        decoded.append(raw)
    return tuple(decoded)


def _as_bytes(item: object) -> bytes:
    if isinstance(item, bytes):
        return item
    if isinstance(item, (bytearray, memoryview)):
        return bytes(item)
    raise ContextPackError("pack-item-not-bytes")


def _digest_of(items: Sequence[bytes]) -> str:
    """Digest the signed content of a pack: its schema and its chunk list."""
    unsigned = {"items": _encode_items(items), "schema": CONTEXT_PACK_SCHEMA}
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(unsigned)).hexdigest()


def _check_digest(value: object) -> str:
    if not isinstance(value, str) or not value.startswith(_DIGEST_PREFIX):
        raise ContextPackError("pack-digest-malformed")
    hexpart = value[len(_DIGEST_PREFIX) :]
    if len(hexpart) != 64 or any(char not in _HEX_DIGITS for char in hexpart):
        raise ContextPackError("pack-digest-malformed")
    return value


def _resolve_byte_count(items: Sequence[bytes], digest: str) -> int:
    """Find the byte count that the serialized payload reports about itself."""
    payload = {
        "byte_count": 0,
        "digest": digest,
        "items": _encode_items(items),
        "schema": CONTEXT_PACK_SCHEMA,
    }
    fixed = len(_canonical(payload)) - len("0")
    for digits in range(1, _MAX_BYTE_COUNT_DIGITS + 1):
        candidate = fixed + digits
        if len(str(candidate)) == digits:
            return candidate
    raise ContextPackError("pack-byte-count-unresolvable")


def _load_object(data: object) -> dict[str, object]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ContextPackError("pack-payload-not-bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ContextPackError("pack-payload-not-json") from error
    if not isinstance(payload, dict):
        raise ContextPackError("pack-payload-not-an-object")
    return payload


def _require_fields(payload: dict[str, object], expected: set[str]) -> None:
    if expected - payload.keys():
        raise ContextPackError("pack-field-missing")
    if payload.keys() - expected:
        raise ContextPackError("pack-field-unknown")


def _validate_limit(chunk_bytes: object) -> int:
    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes <= 0:
        raise ContextPackError("pack-chunk-bytes-not-positive")
    return chunk_bytes


def _split_by_character(text: str, limit: int) -> list[str]:
    """Split text into pieces of at most ``limit`` bytes without splitting a character."""
    pieces: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for char in text:
        size = len(char.encode("utf-8"))
        if size > limit:
            raise ContextPackError("pack-character-exceeds-limit")
        if current and current_bytes + size > limit:
            pieces.append("".join(current))
            current = []
            current_bytes = 0
        current.append(char)
        current_bytes += size
    if current:
        pieces.append("".join(current))
    return pieces


def _chunk_text(text: str, limit: int) -> tuple[str, ...]:
    """Split text into deterministic chunks of at most ``limit`` UTF-8 bytes.

    Line boundaries are preferred, and a line longer than the limit is split on
    character boundaries. The result always reproduces the original text.
    """
    chunks: list[str] = []
    pending: list[str] = []
    pending_bytes = 0
    for line in text.splitlines(keepends=True):
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > limit:
            if pending:
                chunks.append("".join(pending))
                pending = []
                pending_bytes = 0
            chunks.extend(_split_by_character(line, limit))
            continue
        if pending and pending_bytes + line_bytes > limit:
            chunks.append("".join(pending))
            pending = []
            pending_bytes = 0
        pending.append(line)
        pending_bytes += line_bytes
    if pending:
        chunks.append("".join(pending))
    return tuple(chunks)


@dataclass(frozen=True)
class ContextPack:
    """One prepared context as an ordered list of byte chunks."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int

    @classmethod
    def build(cls, items: Iterable[bytes]) -> ContextPack:
        """Build a pack from byte chunks."""
        normalized = tuple(_as_bytes(item) for item in items)
        digest = _digest_of(normalized)
        return cls(items=normalized, digest=digest, byte_count=_resolve_byte_count(normalized, digest))

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        """Chunk prepared context text into a pack.

        Every chunk holds valid UTF-8 and stays within ``chunk_bytes``; joining the
        chunks reproduces the original text byte for byte.
        """
        limit = _validate_limit(chunk_bytes)
        if content is None:
            return cls.build(())
        if not isinstance(content, str):
            raise ContextPackError("pack-content-not-text")
        return cls.build(chunk.encode("utf-8") for chunk in _chunk_text(content, limit))

    def to_bytes(self) -> bytes:
        """Serialize the pack, including its own byte count."""
        payload = {
            "byte_count": self.byte_count,
            "digest": self.digest,
            "items": _encode_items(self.items),
            "schema": CONTEXT_PACK_SCHEMA,
        }
        data = _canonical(payload)
        if len(data) != self.byte_count:
            raise ContextPackError("pack-byte-count-mismatch")
        return data

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        """Decode a serialized pack and check its digest and byte count."""
        payload = _load_object(data)
        _require_fields(payload, {"byte_count", "digest", "items", "schema"})
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("pack-schema-unsupported")
        items = _decode_items(payload["items"])
        digest = _check_digest(payload["digest"])
        if digest != _digest_of(items):
            raise ContextPackError("pack-digest-mismatch")
        byte_count = payload["byte_count"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int):
            raise ContextPackError("pack-byte-count-not-an-integer")
        if byte_count != len(bytes(data)):
            raise ContextPackError("pack-byte-count-mismatch")
        return cls(items=items, digest=digest, byte_count=byte_count)


@dataclass(frozen=True)
class ContextPackDelta:
    """The change from one pack to the next: a kept prefix plus a new suffix."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        """Describe the change from ``base`` to ``target`` by their common chunk prefix."""
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta-packs-not-context-packs")
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(
            base_digest=base.digest,
            target_digest=target.digest,
            prefix_items=prefix,
            appended=target.items[prefix:],
        )

    def to_bytes(self) -> bytes:
        """Serialize the delta; the byte count is a property, not a wire field."""
        payload = {
            "appended": _encode_items(self.appended),
            "base_digest": self.base_digest,
            "prefix_items": self.prefix_items,
            "schema": CONTEXT_PACK_DELTA_SCHEMA,
            "target_digest": self.target_digest,
        }
        return _canonical(payload)

    @property
    def byte_count(self) -> int:
        """Serialized size in bytes, including every protocol field."""
        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        """Decode a serialized delta and check its fields."""
        payload = _load_object(data)
        _require_fields(payload, {"appended", "base_digest", "prefix_items", "schema", "target_digest"})
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("delta-schema-unsupported")
        appended = _decode_items(payload["appended"])
        base_digest = _check_digest(payload["base_digest"])
        target_digest = _check_digest(payload["target_digest"])
        prefix_items = payload["prefix_items"]
        if isinstance(prefix_items, bool) or not isinstance(prefix_items, int) or prefix_items < 0:
            raise ContextPackError("delta-prefix-not-a-non-negative-integer")
        return cls(
            base_digest=base_digest,
            target_digest=target_digest,
            prefix_items=prefix_items,
            appended=appended,
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild the target pack from a base pack and one delta."""
    if not isinstance(base, ContextPack):
        raise ContextPackError("delta-base-not-a-pack")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta-not-a-delta")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta-base-digest-mismatch")
    if delta.prefix_items < 0 or delta.prefix_items > len(base.items):
        raise ContextPackError("delta-prefix-out-of-range")
    rebuilt = ContextPack.build((*base.items[: delta.prefix_items], *delta.appended))
    if rebuilt.digest != delta.target_digest:
        raise ContextPackError("delta-target-digest-mismatch")
    return rebuilt
