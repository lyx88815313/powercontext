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

"""Capture visible events from one selected Pi v3 session branch."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    AdapterSourceDefinition,
    ConnectorRunCompletion,
    ConnectorRunSession,
    ConnectorRunStatus,
    ConnectorSubmissionStatus,
    Source,
    SourceDefinition,
    SourceMaterialization,
    TextEvidence,
)

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"
PI_EVENT_DEFINITION_VERSION = "1"
_CHECKPOINT_VERSION = 1
_SHA256_HEX_LENGTH = 64


class PiSessionInput(BaseModel):
    """One complete offline Pi v3 JSONL session and its selected leaf."""

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible Pi session event with its complete original record."""

    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """One captured Pi event identified by its session and event ID."""

    materialization: SourceMaterialization = SourceMaterialization.CAPTURED
    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSourceAdapter:
    """Resolve one Pi event input into its durable identity and original record."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            source_id=source_id,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()


class PiEventTextEvidenceProjection:
    """Expose one captured Pi event through the standard text-evidence projection."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


PI_EVENT_SOURCE_DEFINITION: SourceDefinition[PiEventInput, PiEventSource, PiEventInput] = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version=PI_EVENT_DEFINITION_VERSION,
    projections=(PiEventTextEvidenceProjection(),),
)


class PiSessionConnector:
    """Import newly visible events from one selected Pi session branch."""

    name = PI_SESSION_CONNECTOR_NAME
    version = PI_EVENT_DEFINITION_VERSION
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self._path = Path(path)
        self._leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        parsed = _parse_pi_session(self._path.read_bytes(), self._leaf_id)
        checkpoint = _decode_checkpoint(session.checkpoint)
        imported = _verified_imports(parsed, checkpoint)
        complete = True
        for event in parsed.events:
            if event.event_id in imported:
                continue
            item_id = pi_event_source_id(event.session_id, event.event_id)
            outcome = await session.submit(item_id, PI_EVENT_SOURCE_NAME, event)
            if outcome.status is ConnectorSubmissionStatus.ACCEPTED:
                imported[event.event_id] = _record_digest(event.record)
            else:
                complete = False
        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE if complete else ConnectorRunStatus.INCOMPLETE,
            checkpoint={
                "version": _CHECKPOINT_VERSION,
                "session_id": parsed.session_id,
                "events": {event_id: imported[event_id] for event_id in sorted(imported)},
            },
        )


@dataclass(frozen=True, slots=True)
class _ParsedSession:
    session_id: str
    records: dict[str, dict[str, JsonValue]]
    events: tuple[PiEventInput, ...]


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    session_id: str
    events: dict[str, str]


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse one Pi session and return the visible events on the selected branch."""

    return _parse_pi_session(value.content, value.leaf_id).events


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Return the bounded, delimiter-safe identity of one Pi event Source."""

    _validate_identity_part("session_id", session_id)
    _validate_identity_part("event_id", event_id)
    encoded = f"pi:{_quote_identity(session_id)}:{_quote_identity(event_id)}"
    if len(encoded) <= MAX_SOURCE_ID_LENGTH:
        return encoded
    canonical = json.dumps([session_id, event_id], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"pi-h:{digest}"


def _parse_pi_session(content: bytes, leaf_id: str | None) -> _ParsedSession:
    lines = _read_json_lines(content)
    if not lines:
        raise ValueError("Pi session must contain a session header")  # noqa: TRY003
    session_id = _validate_header(lines[0])
    records: dict[str, dict[str, JsonValue]] = {}
    for record in lines[1:]:
        event_id = _validate_event_record(record, records)
        records[event_id] = record
    if not records:
        raise ValueError("Pi session must contain at least one event")  # noqa: TRY003
    if leaf_id is None:
        selected = next(reversed(records))
    else:
        _validate_identity_part("leaf_id", leaf_id)
        if leaf_id not in records:
            raise ValueError(f"Pi session leaf {leaf_id!r} does not exist")  # noqa: TRY003
        selected = leaf_id
    chain = _branch_records(records, selected)
    events = tuple(event for record in chain if (event := _event_for_record(session_id, record)) is not None)
    return _ParsedSession(session_id=session_id, records=records, events=events)


def _read_json_lines(content: bytes) -> list[dict[str, JsonValue]]:
    if not isinstance(content, bytes):
        raise ValueError("Pi session content must be bytes")  # noqa: TRY003, TRY004
    try:
        text = content.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error  # noqa: TRY003
    records: list[dict[str, JsonValue]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError("Pi session contains invalid JSON") from error  # noqa: TRY003
        if not isinstance(parsed, dict):
            raise ValueError("Pi session records must be JSON objects")  # noqa: TRY003, TRY004
        records.append(cast(dict[str, JsonValue], parsed))
    return records


def _validate_header(header: dict[str, JsonValue]) -> str:
    if header.get("type") != "session":
        raise ValueError("first Pi session record must have type session")  # noqa: TRY003
    if header.get("version") != 3:
        raise ValueError("Pi session version must be 3")  # noqa: TRY003
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("Pi session id must be a non-empty string")  # noqa: TRY003
    return session_id


def _validate_event_record(record: dict[str, JsonValue], known: dict[str, dict[str, JsonValue]]) -> str:
    if record.get("type") == "session":
        raise ValueError("Pi session may contain only one session header")  # noqa: TRY003
    event_id = record.get("id")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("Pi session event id must be a non-empty string")  # noqa: TRY003
    if event_id in known:
        raise ValueError(f"Pi session event id {event_id!r} is duplicated")  # noqa: TRY003
    parent_id = record.get("parentId")
    if parent_id is not None:
        if not isinstance(parent_id, str) or not parent_id.strip():
            raise ValueError("Pi session parentId must be null or a non-empty string")  # noqa: TRY003
        if parent_id not in known:
            raise ValueError(f"Pi session parent {parent_id!r} does not exist before its child")  # noqa: TRY003
    return event_id


def _branch_records(
    records: dict[str, dict[str, JsonValue]],
    leaf_id: str,
) -> list[dict[str, JsonValue]]:
    chain: list[dict[str, JsonValue]] = []
    current: str | None = leaf_id
    while current is not None:
        record = records[current]
        chain.append(record)
        parent = record.get("parentId")
        current = parent if isinstance(parent, str) else None
    chain.reverse()
    return chain


def _event_for_record(session_id: str, record: dict[str, JsonValue]) -> PiEventInput | None:
    event_type = record.get("type")
    if not isinstance(event_type, str):
        return None
    rendered = _render_event(record, event_type)
    if rendered is None:
        return None
    role, body = rendered
    if not body.strip():
        return None
    parent_id = record.get("parentId")
    return PiEventInput(
        session_id=session_id,
        event_id=cast(str, record["id"]),
        parent_id=cast(str | None, parent_id),
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=record,
    )


def _render_event(record: dict[str, JsonValue], event_type: str) -> tuple[str, str] | None:
    if event_type == "message":
        return _render_message(record)
    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        return (event_type, summary) if isinstance(summary, str) else None
    if event_type == "custom_message":
        return "custom", _render_content(record.get("content"), include_tool_calls=False)
    return None


def _render_message(record: dict[str, JsonValue]) -> tuple[str, str] | None:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if not isinstance(role, str) or not role:
        return None
    if role == "toolResult":
        return role, _render_tool_result(message)
    if role == "bashExecution":
        return role, _render_bash_execution(message)
    return role, _render_content(message.get("content"), include_tool_calls=True)


def _render_tool_result(message: dict[str, JsonValue]) -> str:
    text = _render_content(message.get("content"), include_tool_calls=False)
    name = _string_field(message.get("toolName"))
    call_id = _string_field(message.get("toolCallId"))
    return f"[tool_result {name} {call_id}] {text}"


def _render_bash_execution(message: dict[str, JsonValue]) -> str:
    command = _string_field(message.get("command"))
    output = _string_field(message.get("output"))
    return f"$ {command}\n{output}"


def _render_content(content: JsonValue | None, *, include_tool_calls: bool) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif block_type == "toolCall" and include_tool_calls:
            parts.append(_render_tool_call(block))
    return "\n".join(parts)


def _render_tool_call(block: dict[str, JsonValue]) -> str:
    name = _string_field(block.get("name"))
    call_id = _string_field(block.get("id"))
    arguments = block.get("arguments")
    if arguments is None:
        arguments = {}
    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("Pi tool call arguments must be JSON values") from error  # noqa: TRY003
    return f"[tool_call {name} {call_id}] {encoded}"


def _string_field(value: JsonValue | None) -> str:
    return value if isinstance(value, str) else ""


def _decode_checkpoint(value: JsonValue | None) -> _Checkpoint | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Pi checkpoint must be a JSON object")  # noqa: TRY003, TRY004
    session_id = value.get("session_id")
    events = value.get("events")
    if value.get("version") != _CHECKPOINT_VERSION:
        raise ValueError("Pi checkpoint version is not supported")  # noqa: TRY003
    if not isinstance(session_id, str) or not session_id or not isinstance(events, dict):
        raise ValueError("Pi checkpoint is malformed")  # noqa: TRY003
    imported: dict[str, str] = {}
    for event_id, digest in events.items():
        if not isinstance(event_id, str) or not event_id or not _is_sha256(digest):
            raise ValueError("Pi checkpoint event entry is malformed")  # noqa: TRY003
        imported[event_id] = digest
    return _Checkpoint(session_id=session_id, events=imported)


def _verified_imports(parsed: _ParsedSession, checkpoint: _Checkpoint | None) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if checkpoint.session_id != parsed.session_id:
        raise ValueError("Pi session id no longer matches the imported checkpoint")  # noqa: TRY003
    for event_id, digest in checkpoint.events.items():
        record = parsed.records.get(event_id)
        if record is None:
            raise ValueError(f"imported Pi event {event_id!r} is missing from the session")  # noqa: TRY003
        if _record_digest(record) != digest:
            raise ValueError(f"imported Pi event {event_id!r} was changed")  # noqa: TRY003
    return dict(checkpoint.events)


def _record_digest(record: dict[str, JsonValue]) -> str:
    canonical = json.dumps(
        record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_HEX_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_identity_part(field: str, value: str) -> None:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError(f"{field} must be a non-empty, non-blank string")  # noqa: TRY003


def _quote_identity(value: str) -> str:
    try:
        return quote(value, safe="", encoding="utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("Pi session identity must be valid Unicode") from error  # noqa: TRY003


def _reject_json_constant(value: str) -> JsonValue:
    raise ValueError(f"Pi session JSON must not contain {value}")  # noqa: TRY003
