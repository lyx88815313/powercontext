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

"""Evidence lifecycle over Source availability and its derived artifacts.

The lifecycle keeps a per-Scope registration graph. A Source event changes one
Source's availability, the graph evaluates every derived node with its ``all``
or ``any`` policy, and Memory entries are deactivated/reactivated through their
native state so search, Context recall and exact reads stay consistent. Topic
Memory references are filtered by the same evaluation without rewriting their
immutable revisions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.artifacts.memory import MemoryEntryVersion, MemoryService
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_STATE_TABLE
from powercontext.builtin.runtime.models import GetMemoryEntryRequest
from powercontext.builtin.statistics import ModelUsagePurpose
from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

_DEFAULT_PURPOSE = "generation"
_PURPOSES = frozenset({"generation", "audit"})
_POLICIES = frozenset({"all", "any"})


class SourceLifecycleError(ValueError):
    """One lifecycle request cannot be applied to the registered graph."""

    def __init__(self, code: str) -> None:
        super().__init__(f"Source lifecycle error: {code}")
        self.code = code


class SourceLifecycleEvent(BaseModel):
    """One explicitly revised availability observation for a registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: StrictStr
    revision: StrictInt = Field(gt=0)
    sequence: StrictInt = Field(gt=0)
    available: StrictBool
    reason: StrictStr

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("node_id must be a non-empty trimmed string")  # noqa: TRY003
        return value

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must be a non-empty string")  # noqa: TRY003
        return value


class _SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: SourceRef
    revision: StrictInt = Field(ge=1)
    sequence: StrictInt = Field(ge=0)
    available: StrictBool
    applied: StrictBool
    reason: StrictStr | None = None


class _NodeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["memory", "topic"]
    position: StrictInt = Field(ge=0)
    parents: tuple[StrictStr, ...]
    policy: Literal["all", "any"]
    citation: MemoryCitation | None = None
    ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_reference(self):
        if self.kind == "memory":
            if self.citation is None or self.ref is not None:
                raise ValueError("Memory nodes carry exactly one citation")  # noqa: TRY003
        elif self.ref is None or self.citation is not None:
            raise ValueError("Topic nodes carry exactly one artifact reference")  # noqa: TRY003
        return self


class _LifecycleState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sources: dict[str, _SourceRecord] = Field(default_factory=dict)
    nodes: dict[str, _NodeRecord] = Field(default_factory=dict)


class SourceLifecycleRepository:
    """Load and store the single lifecycle graph row of one Scope."""

    def __init__(self, database: AsyncDatabase) -> None:
        self._database = database

    async def load(
        self,
        connection: AsyncConnection,
        scope_id: str,
        /,
        *,
        for_update: bool = False,
    ) -> tuple[_LifecycleState, int]:
        table = SOURCE_LIFECYCLE_STATE_TABLE
        statement = select(table.c.payload, table.c.generation).where(table.c.scope_id == scope_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await connection.execute(statement)).one_or_none()
        if row is None:
            return _LifecycleState(), 0
        try:
            state = _LifecycleState.model_validate_json(str(row[0]), strict=True)
        except ValidationError as error:
            raise SourceLifecycleError("stored-state") from error
        return state, int(row[1])

    async def save(
        self,
        connection: AsyncConnection,
        scope_id: str,
        state: _LifecycleState,
        generation: int,
        /,
    ) -> int:
        table = SOURCE_LIFECYCLE_STATE_TABLE
        payload = state.model_dump_json()
        if generation == 0:
            await connection.execute(
                insert(table).values(scope_id=scope_id, generation=1, payload=payload)
            )
            return 1
        result = await connection.execute(
            update(table)
            .where(table.c.scope_id == scope_id, table.c.generation == generation)
            .values(generation=generation + 1, payload=payload)
        )
        if result.rowcount != 1:
            raise SourceLifecycleError("generation-conflict")
        return generation + 1


class ScopedLifecycleApplication:
    """Register evidence nodes and apply Source events in one Scope."""

    def __init__(
        self,
        runtime: BuiltinRuntime,
        scope_id: str,
        store: SourceLifecycleRepository | None,
    ) -> None:
        self._runtime = runtime
        self.scope_id = scope_id
        self._store = store

    async def register_source(
        self,
        node_id: str,
        source_ref: SourceRef,
        *,
        revision: int = 1,
    ) -> None:
        node_id = _require_node_id(node_id)
        revision = _require_positive("revision", revision)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("source-reference")
        store = self._require_store()
        async with self._runtime._scope_operation(self.scope_id):
            await self._require_source(source_ref)
            async with self._runtime._locked(self.scope_id), store._database.transaction() as connection:
                state, generation = await store.load(connection, self.scope_id, for_update=True)
                existing = state.sources.get(node_id)
                if existing is not None:
                    if existing.ref != source_ref or existing.revision != revision:
                        raise SourceLifecycleError("source-conflict")
                    return
                if node_id in state.nodes:
                    raise SourceLifecycleError("node-conflict")
                record = _SourceRecord(
                    ref=source_ref,
                    revision=revision,
                    sequence=0,
                    available=True,
                    applied=True,
                )
                updated = state.model_copy(update={"sources": {**state.sources, node_id: record}})
                await store.save(connection, self.scope_id, updated, generation)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        parents: tuple[str, ...],
        *,
        policy: str = "all",
    ) -> None:
        node_id = _require_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("memory-reference")
        parents = _require_parents(parents)
        policy = _require_policy(policy)
        store = self._require_store()
        async with self._runtime._scope_operation(self.scope_id):
            await self._require_memory(citation)
            state = await self._register_node(
                store,
                node_id,
                _NodeRecord(
                    kind="memory",
                    position=0,
                    parents=parents,
                    policy=policy,
                    citation=citation,
                ),
                validate_parents=True,
            )
            if state is not None:
                active = _availability(state)
                await self._sync_memory(state, active)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        parents: tuple[str, ...],
        *,
        policy: str = "all",
    ) -> None:
        node_id = _require_node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef):
            raise SourceLifecycleError("topic-reference")
        if artifact_ref.family != "topic-memory":
            raise SourceLifecycleError("topic-family")
        parents = _require_parents(parents)
        policy = _require_policy(policy)
        store = self._require_store()
        async with self._runtime._scope_operation(self.scope_id):
            await self._require_topic(artifact_ref)
            await self._register_node(
                store,
                node_id,
                _NodeRecord(
                    kind="topic",
                    position=0,
                    parents=parents,
                    policy=policy,
                    ref=artifact_ref,
                ),
                validate_parents=True,
            )

    async def apply(self, event: SourceLifecycleEvent) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("event")
        store = self._require_store()
        async with self._runtime._context(
            self.scope_id,
            embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING,
        ) as context, self._runtime._locked(self.scope_id):
            state, _generation, active = await self._record_event(store, event)
            if active is not None:
                return active
            await self._sync_memory(state, _availability(state), context=context)
            final = await self._complete_event(store, event)
            return _active_ids(final)

    async def resolve(self, node_id: str, purpose: str = _DEFAULT_PURPOSE) -> Any:
        node_id = _require_node_id(node_id)
        if purpose not in _PURPOSES:
            raise SourceLifecycleError("purpose")
        store = self._require_store()
        state = await self._load(store)
        if node_id in state.sources:
            reference: Any = state.sources[node_id].ref
        elif node_id in state.nodes:
            node = state.nodes[node_id]
            reference = node.citation if node.kind == "memory" else node.ref
        else:
            raise SourceLifecycleError("unknown-node")
        if purpose == "audit":
            return reference
        if not _availability(state).get(node_id, False):
            raise SourceLifecycleError("unavailable")
        return reference

    async def blocked_topic_artifact_ids(self) -> frozenset[str]:
        """Return registered Topic identities that generation must not see."""

        if self._store is None:
            return frozenset()
        state = await self._load(self._store)
        topics = {
            node_id: node
            for node_id, node in state.nodes.items()
            if node.kind == "topic" and node.ref is not None
        }
        if not topics:
            return frozenset()
        active = _availability(state)
        return frozenset(
            node.ref.artifact_id
            for node_id, node in topics.items()
            if not active.get(node_id, False) and node.ref is not None
        )

    async def _register_node(
        self,
        store: SourceLifecycleRepository,
        node_id: str,
        node: _NodeRecord,
        /,
        *,
        validate_parents: bool,
    ) -> _LifecycleState | None:
        async with self._runtime._locked(self.scope_id), store._database.transaction() as connection:
            state, generation = await store.load(connection, self.scope_id, for_update=True)
            if node_id in state.sources:
                raise SourceLifecycleError("node-conflict")
            existing = state.nodes.get(node_id)
            if existing is not None:
                if (
                    existing.kind == node.kind
                    and existing.parents == node.parents
                    and existing.policy == node.policy
                    and existing.citation == node.citation
                    and existing.ref == node.ref
                ):
                    return state
                raise SourceLifecycleError("node-conflict")
            if validate_parents:
                for parent in node.parents:
                    if parent == node_id:
                        raise SourceLifecycleError("cycle")
                    if parent not in state.sources and parent not in state.nodes:
                        raise SourceLifecycleError("unknown-parent")
            position = max((item.position for item in state.nodes.values()), default=0) + 1
            updated = node.model_copy(update={"position": position})
            merged = state.model_copy(update={"nodes": {**state.nodes, node_id: updated}})
            await store.save(connection, self.scope_id, merged, generation)
            return merged

    async def _record_event(
        self,
        store: SourceLifecycleRepository,
        event: SourceLifecycleEvent,
    ) -> tuple[_LifecycleState, int, list[str] | None]:
        async with store._database.transaction() as connection:
            state, generation = await store.load(connection, self.scope_id, for_update=True)
            source = state.sources.get(event.node_id)
            if source is None:
                raise SourceLifecycleError("unknown-source")
            if source.revision != event.revision:
                raise SourceLifecycleError("revision")
            if event.sequence < source.sequence:
                raise SourceLifecycleError("sequence")
            if event.sequence == source.sequence:
                if event.available != source.available or event.reason != source.reason:
                    raise SourceLifecycleError("sequence-conflict")
                if source.applied:
                    return state, generation, _active_ids(state)
                return state, generation, None
            updated_source = source.model_copy(
                update={
                    "sequence": event.sequence,
                    "available": event.available,
                    "applied": False,
                    "reason": event.reason,
                }
            )
            updated = state.model_copy(
                update={"sources": {**state.sources, event.node_id: updated_source}}
            )
            generation = await store.save(connection, self.scope_id, updated, generation)
            return updated, generation, None

    async def _complete_event(
        self,
        store: SourceLifecycleRepository,
        event: SourceLifecycleEvent,
    ) -> _LifecycleState:
        async with store._database.transaction() as connection:
            state, generation = await store.load(connection, self.scope_id, for_update=True)
            source = state.sources.get(event.node_id)
            if source is None or source.sequence != event.sequence:
                raise SourceLifecycleError("event-incomplete")
            if source.applied:
                return state
            updated_source = source.model_copy(update={"applied": True})
            updated = state.model_copy(
                update={"sources": {**state.sources, event.node_id: updated_source}}
            )
            await store.save(connection, self.scope_id, updated, generation)
            return updated

    async def _load(self, store: SourceLifecycleRepository) -> _LifecycleState:
        async with store._database.transaction() as connection:
            state, _ = await store.load(connection, self.scope_id)
            return state

    async def _require_source(self, source_ref: SourceRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(
                source_ref.source_type,
                source_ref.source_id,
            )
        except SourceLifecycleError:
            raise
        except Exception as error:
            raise SourceLifecycleError("source-reference") from error

    async def _require_memory(self, citation: MemoryCitation) -> None:
        try:
            await self._runtime.memory.for_scope(self.scope_id).get(
                GetMemoryEntryRequest(citation=citation)
            )
        except SourceLifecycleError:
            raise
        except Exception as error:
            raise SourceLifecycleError("memory-reference") from error

    async def _require_topic(self, artifact_ref: ArtifactRef) -> None:
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                artifact_ref.family,
                artifact_ref.artifact_id,
                artifact_ref.revision,
            )
        except SourceLifecycleError:
            raise
        except Exception as error:
            raise SourceLifecycleError("topic-reference") from error

    async def _sync_memory(
        self,
        state: _LifecycleState,
        active: dict[str, bool],
        /,
        *,
        context: Any = None,
    ) -> None:
        controlled, artifact_ids = _memory_targets(state, active)
        if not controlled:
            return
        if len(artifact_ids) != 1:
            raise SourceLifecycleError("memory-identity")
        if context is None:
            async with self._runtime._context(
                self.scope_id,
                embedding_purpose=ModelUsagePurpose.MEMORY_INDEXING,
            ) as opened:
                await self._sync_memory(state, active, context=opened)
            return
        artifact_id = next(iter(artifact_ids))
        if context.artifacts.memory_artifact_id != artifact_id:
            raise SourceLifecycleError("memory-identity")
        await _apply_memory_states(context.artifacts.memory, artifact_id, controlled)

    def _require_store(self) -> SourceLifecycleRepository:
        if self._store is None:
            raise SourceLifecycleError("unavailable")
        return self._store


class LifecycleApplication:
    """Select the evidence lifecycle service for one Scope."""

    def __init__(self, runtime: BuiltinRuntime, store: SourceLifecycleRepository | None = None) -> None:
        self._runtime = runtime
        self._store = store

    def for_scope(self, scope_id: str, /) -> ScopedLifecycleApplication:
        from powercontext.builtin.sources import validate_scope_id

        return ScopedLifecycleApplication(self._runtime, validate_scope_id(scope_id), self._store)


def _memory_targets(
    state: _LifecycleState,
    active: dict[str, bool],
) -> tuple[dict[str, bool], set[str]]:
    controlled: dict[str, bool] = {}
    artifact_ids: set[str] = set()
    for node_id, node in state.nodes.items():
        if node.kind != "memory" or node.citation is None:
            continue
        artifact_ids.add(node.citation.memory_ref.artifact_id)
        entry_id = node.citation.entry_id
        controlled[entry_id] = controlled.get(entry_id, False) or active.get(node_id, False)
    return controlled, artifact_ids


async def _apply_memory_states(
    service: MemoryService,
    artifact_id: str,
    controlled: dict[str, bool],
) -> None:
    try:
        current, versions = await service.head_entries(artifact_id)
    except SourceLifecycleError:
        raise
    except Exception as error:
        raise SourceLifecycleError("memory-reference") from error
    manifest = {item.entry_id: item for item in current.content.manifest.entries}
    by_entry: dict[str, MemoryEntryVersion] = {entry.entry_id: entry for entry in versions}
    deactivate: list[MemoryEntryVersion] = []
    reactivate: list[MemoryEntryVersion] = []
    for entry_id, desired in controlled.items():
        item = manifest.get(entry_id)
        entry = by_entry.get(entry_id)
        if item is None or entry is None:
            raise SourceLifecycleError("memory-entry")
        if desired and item.state != "active":
            reactivate.append(entry)
        elif not desired and item.state == "active":
            deactivate.append(entry)
    if deactivate:
        current = await service.forget(current, entries=tuple(deactivate), reason="source lifecycle")
    if reactivate:
        await service.reactivate(current, entries=tuple(reactivate), reason="source lifecycle")


def _active_ids(state: _LifecycleState) -> list[str]:
    return sorted(node_id for node_id, active in _availability(state).items() if active)


def _availability(state: _LifecycleState) -> dict[str, bool]:
    active = {node_id: source.available for node_id, source in state.sources.items()}
    for node_id, node in sorted(state.nodes.items(), key=lambda item: item[1].position):
        support = [active[parent] for parent in node.parents]
        active[node_id] = all(support) if node.policy == "all" else any(support)
    return active


def _require_node_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise SourceLifecycleError("node-id")
    if len(value) > MAX_SOURCE_ID_LENGTH:
        raise SourceLifecycleError("node-id")
    return value


def _require_positive(field: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceLifecycleError(field)
    return value


def _require_policy(value: object) -> Literal["all", "any"]:
    if value not in _POLICIES:
        raise SourceLifecycleError("policy")
    return value  # type: ignore[return-value]


def _require_parents(value: object) -> tuple[str, ...]:
    if isinstance(value, str | bytes) or not isinstance(value, tuple | list):
        raise SourceLifecycleError("parents")
    parents = tuple(value)
    if not parents:
        raise SourceLifecycleError("parents")
    if any(not isinstance(item, str) or not item.strip() for item in parents):
        raise SourceLifecycleError("parents")
    if len(set(parents)) != len(parents):
        raise SourceLifecycleError("parents")
    return parents
