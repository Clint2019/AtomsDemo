from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from atoms_demo.core.types import (
    BlobRef, EntityId, Environment, Mutation, OperationReceipt,
    Page, PageQuery, RequestContext, Scope, SecretRef,
)

from atoms_demo.core.events import TaskEvent

class TaskState(StrEnum):
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    QUEUED = "queued"
    GENERATING = "generating"
    BUILDING = "building"
    COMMITTING = "committing"
    REPAIRING = "repairing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"

@dataclass(frozen=True, kw_only=True)
class Task:
    id: EntityId
    workspace_id: EntityId
    actor_id: EntityId
    project_id: EntityId
    request: SubmitTask
    state: TaskState
    base_version_id: EntityId | None
    result_version_id: EntityId | None
    attempts: int
    error_code: str | None
    revision: int

@dataclass(frozen=True, kw_only=True)
class SubmitTask:
    project_id: EntityId
    message_id: EntityId
    base_version_id: EntityId | None
    expected_head_revision: int
    model: str
    agent_mode: str
    mutation: Mutation
    approval_items: tuple[str, ...] = ()
    confirmed_build_intent: bool = False

@dataclass(frozen=True, kw_only=True)
class TaskApproval:
    id: EntityId
    task_id: EntityId
    items: tuple[str, ...]
    selected_indices: tuple[int, ...] | None
    additional_instructions: str
    decision_key: str | None
    revision: int

@dataclass(frozen=True, kw_only=True)
class TaskTransition:
    task_id: EntityId
    expected_revision: int
    target: TaskState
    result_version_id: EntityId | None = None
    error_code: str | None = None

class TaskAPI(Protocol):
    async def record_usage(self, ctx: RequestContext, task_id: EntityId, call_key: str,
                           input_tokens: int, output_tokens: int) -> None: ...
    async def begin_model_call(self, ctx: RequestContext, task_id: EntityId,
                               call_key: str, lease_owner: str | None) -> None: ...
    async def checkpoint_answer(self, ctx: RequestContext, task_id: EntityId,
                                content: str, input_tokens: int,
                                output_tokens: int,
                                lease_owner: str | None) -> None: ...
    async def answer_checkpoint(self, ctx: RequestContext,
                                task_id: EntityId) -> str | None: ...
    async def complete_answer(self, ctx: RequestContext, task_id: EntityId,
                              expected_revision: int, lease_owner: str | None) -> Task: ...
    async def recover_answer(self, ctx: RequestContext, task_id: EntityId) -> Task: ...
    async def has_uncheckpointed_model_call(self, task_id: EntityId) -> bool: ...
    async def usage_totals(self, ctx: RequestContext, task_id: EntityId) -> tuple[int, int]: ...
    async def usage_for_execution(self, task_id: EntityId) -> tuple[int, int]: ...
    async def execution_context(self, task_id: EntityId) -> RequestContext: ...
    async def interrupt_execution(self, task_id: EntityId, reason: str) -> Task: ...
    async def reconcile_committed(self, task_id: EntityId, version_id: EntityId) -> Task: ...
    async def find_submission(self, ctx: RequestContext, project_id: EntityId,
                              idempotency_key: str) -> Task | None: ...
    async def submit(self, ctx: RequestContext, command: SubmitTask) -> Task: ...
    async def approval(self, ctx: RequestContext, task_id: EntityId) -> TaskApproval | None: ...
    async def approve(self, ctx: RequestContext, task_id: EntityId,
                      selected_indices: tuple[int, ...], additional_instructions: str,
                      mutation: Mutation) -> Task: ...
    async def get(self, ctx: RequestContext, task_id: EntityId) -> Task: ...
    async def active_for_project(self, ctx: RequestContext, project_id: EntityId) -> Task | None: ...
    async def latest_for_project(self, ctx: RequestContext, project_id: EntityId) -> Task | None: ...
    async def transition(self, ctx: RequestContext, command: TaskTransition) -> Task: ...
    async def events(self, ctx: RequestContext, task_id: EntityId, after_sequence: int) -> tuple[TaskEvent, ...]: ...
    async def append_event(self, ctx: RequestContext, event: TaskEvent) -> None: ...
    async def cancel(self, ctx: RequestContext, task_id: EntityId, mutation: Mutation) -> Task: ...
    async def cancel_with_previous_state(self, ctx: RequestContext, task_id: EntityId,
                                         mutation: Mutation) -> tuple[Task, TaskState]: ...
    async def recover_interrupted(self) -> tuple[EntityId, ...]: ...
