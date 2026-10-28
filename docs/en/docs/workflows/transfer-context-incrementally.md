---
title: Transfer context incrementally
description: Send a prepared context as a Context Pack, then reuse the previous pack to send only the changed suffix.
---

# Transfer context incrementally

A plain `prepare` returns the whole prepared context every time. When a host already holds the previous
result, most of those bytes repeat. The Context Pack protocol sends the context once as a complete pack and
then sends a delta per round: the delta names a prefix of the previous pack the receiver keeps and carries the
new suffix. The receiver rebuilds the current context from the serialized bytes alone, without access to the
sender's process.

## Prepare a pack and then a delta

`ScopedContextApplication.prepare_incremental` takes the same request as `prepare` and returns the same
content. Without a base pack it returns the complete pack; with one it returns the delta that turns that base
pack into this round's pack.

```python
from powercontext.builtin.runtime import PrepareContextRequest
from powercontext.builtin.runtime.context_pack import (
    ContextPack,
    ContextPackDelta,
    apply_context_pack_delta,
)

request = PrepareContextRequest(query="remote control", max_bytes=8000)

first = await context.prepare_incremental(request)
assert isinstance(first, ContextPack)

second = await context.prepare_incremental(request, base_pack=first)
assert isinstance(second, ContextPackDelta)
```

The receiver keeps the pack it already has and applies each delta:

```python
held: ContextPack = first
for delta in deltas:
    held = apply_context_pack_delta(held, delta)
    text = b"".join(held.items).decode("utf-8")
```

`apply_context_pack_delta` verifies the base digest, the prefix bounds, and the target digest before it
returns the rebuilt pack, so a truncated or mismatched delta fails with `ContextPackError` instead of silently
producing a different context.

## Read the wire format

Both objects are canonical JSON: UTF-8, sorted keys, `,` and `:` separators, no extra whitespace.

| Object | Fields |
| --- | --- |
| Pack | `schema`, `items`, `digest`, `byte_count`; `schema` is `powercontext.context-pack.v1` |
| Delta | `schema`, `base_digest`, `target_digest`, `prefix_items`, `appended`; `schema` is `powercontext.context-pack-delta.v1` |

`items` and `appended` are arrays of standard Base64 strings on the wire and tuples of `bytes` in memory. The
pack digest is the SHA-256 of the canonical JSON of `schema` and `items`, written as `sha256:<hex>`. The pack
`byte_count` equals the length of its own serialized form, including that field. A delta reports its size
through the `byte_count` property; the property is not a wire field.

`ContextPack.from_content` splits text into deterministic chunks. Each chunk holds valid UTF-8 and stays
within the caller's byte limit, which defaults to 512 bytes; joining the chunks reproduces the original text
byte for byte. A character that cannot fit the limit raises `ContextPackError`. An empty prepare result is an
empty pack, and its delta rebuilds to zero bytes.

## Measure the saving

`scripts/measure_context_pack.py` drives the real Runtime over a repeated prepare sequence and reports the
wire cost against the plain context and against two observations:

```bash
python scripts/measure_context_pack.py --meeting data/ALL/test/ES2004a.json
python scripts/measure_context_pack.py --meeting data/ALL/test/ES2004a.json --append-every 4
```

The report lists `raw_full_bytes` (the UTF-8 bytes of every plain prepare result), `incremental_bytes` (the
first full pack plus every serialized delta), the cumulative bytes of complete v1 packs, and the cumulative
bytes of `gzip.compress(compresslevel=9, mtime=0)` over the same bodies. A negative saving means the
incremental protocol sent more bytes than that baseline. The gzip baseline compresses each body on its own and
reuses nothing across rounds, so it is an observation rather than a target.

The size of the win follows how much of the context repeats: the first pack always carries the whole context,
and each later delta carries only the suffix after the common prefix. Rounds whose context repeats in full
cost one digest pair, while a round whose content changes near the beginning costs close to a complete pack
again.

## Measured on a meeting transcript

The QMSum meeting `ES2004a` with the retrieval term `remote` gives three reference points for 40 rounds. The
turns are the meeting turns whose body is 120 to 900 bytes and that contain the term, in their original order.

| Sequence | Distinct contexts | Raw bytes | Incremental bytes | Full v1 pack bytes | gzip bytes | Saving against raw |
| --- | --- | --- | --- | --- | --- | --- |
| One query for every round | 1 | 105,760 | 13,689 | 148,200 | 46,760 | 87.06% |
| One extra turn every four rounds | 10 | 142,636 | 54,553 | 197,692 | 47,884 | 61.75% |
| A different query every round | 4 | 61,660 | 85,654 | 88,980 | 29,080 | -38.91% |

The first row is the best case and the last row is the worst one: when the selected evidence changes from the
first byte, the delta has to carry the whole body again, and the Base64 encoding of that body alone costs about
a third more than the plain context. Against the two observations the same three sequences save 90.76%, 72.41%,
and -194.55% respectively, the last of them being the honest cost of a prefix reuse protocol on a sequence that
shares almost nothing between rounds.
