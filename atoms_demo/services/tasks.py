"""Durable project task state, one-active-task guard and restart recovery."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from atoms_demo.contracts.tasks import TaskState, Task, SubmitTask, TaskTransition
from atoms_demo.core.events import TaskEvent
from atoms_demo.core.errors import DomainError, ErrorCode
from atoms_demo.core.types import EntityId, Mutation, PageQuery, RequestContext, Scope
from atoms_demo.ports.dependencies import TaskDependencies

GLOBAL = Scope(workspace_id=None)
TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.INTERRUPTED, TaskState.CANCELLED}
TRANSITIONS = {
    TaskState.QUEUED: {TaskState.GENERATING, TaskState.BUILDING, TaskState.FAILED,
                       TaskState.CANCELLED, TaskState.INTERRUPTED},
    TaskState.GENERATING: {TaskState.BUILDING, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED},
    TaskState.BUILDING: {TaskState.REPAIRING, TaskState.COMMITTING, TaskState.FAILED,
                         TaskState.CANCELLED, TaskState.INTERRUPTED},
    TaskState.COMMITTING: {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.INTERRUPTED},
    TaskState.REPAIRING: {TaskState.BUILDING, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED},
}


@dataclass(frozen=True)
class TaskLocator:
    id: EntityId
    target_workspace_id: EntityId
    target_project_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class TaskMutation:
    id: EntityId
    task_id: EntityId
    input_digest: EntityId
    revision: int = 0


@dataclass(frozen=True)
class ActiveTask:
    id: EntityId
    project_id: EntityId
    task_id: EntityId
    revision: int = 0


@dataclass(frozen=True)
class ModelUsageRecord:
    id: EntityId
    task_id: EntityId
    call_key: str
    input_tokens: int | None
    output_tokens: int | None
    revision: int = 0


def decode_task(data: dict) -> Task:
    request = data["request"]
    command = SubmitTask(**{**request, "mutation": Mutation(**request["mutation"])})
    return Task(**{**data, "request": command, "state": TaskState(data["state"])})


def _hash(value: str) -> EntityId:
    return EntityId(hashlib.sha256(value.encode()).hexdigest())


class TaskService:
    def __init__(self, dependencies: TaskDependencies) -> None:
        self.dependencies = dependencies

    async def _located(self, uow, task_id: EntityId) -> tuple[Task, Scope] | None:
        locator = await uow.repository(TaskLocator).get(GLOBAL, task_id)
        if locator is None:
            return None
        scope = Scope(workspace_id=locator.target_workspace_id, project_id=locator.target_project_id)
        task = await uow.repository(Task).get(scope, task_id)
        return (task, scope) if task else None

    async def execution_context(self, task_id: EntityId) -> RequestContext:
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, task_id)
        if located is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Task not found")
        task = located[0]
        return RequestContext(actor_id=task.actor_id, session_id=None,
                              workspace_id=task.workspace_id, request_id=f"worker:{task.id}")

    async def interrupt_execution(self, task_id: EntityId, reason: str) -> Task:
        if reason not in {"authorization_revoked", "worker_unavailable", "process_restart"}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid interruption reason")
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, task_id)
            if located is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Task not found")
            task, scope = located
            if task.state in TERMINAL:
                return task
            updated = await uow.repository(Task).save(scope, Task(
                id=task.id, workspace_id=task.workspace_id, actor_id=task.actor_id,
                project_id=task.project_id, request=task.request, state=TaskState.INTERRUPTED,
                base_version_id=task.base_version_id, result_version_id=task.result_version_id,
                attempts=task.attempts, error_code=reason, revision=task.revision), task.revision)
            active = await uow.repository(ActiveTask).get(scope, task.project_id)
            if active and active.task_id == task.id:
                await uow.repository(ActiveTask).delete(scope, active.id, active.revision)
            await uow.commit()
        return updated

    async def reconcile_committed(self, task_id: EntityId, version_id: EntityId) -> Task:
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, task_id)
            if located is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Task not found")
            task, scope = located
            if task.state != TaskState.INTERRUPTED or task.result_version_id != version_id:
                raise DomainError(ErrorCode.CONFLICT, "Task commit checkpoint differs")
            updated = await uow.repository(Task).save(scope, Task(
                id=task.id, workspace_id=task.workspace_id, actor_id=task.actor_id,
                project_id=task.project_id, request=task.request, state=TaskState.SUCCEEDED,
                base_version_id=task.base_version_id, result_version_id=version_id,
                attempts=task.attempts, error_code=None, revision=task.revision), task.revision)
            await uow.commit()
        return updated

    async def find_submission(self, ctx: RequestContext, project_id: EntityId,
                              idempotency_key: str) -> Task | None:
        await self.dependencies.authorization.require(ctx, "project:write", project_id)
        if ctx.actor_id is None or not idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid submission lookup")
        mutation_id = _hash(f"task:submit:{ctx.actor_id}:{project_id}:{idempotency_key}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(TaskMutation).get(GLOBAL, mutation_id)
            located = await self._located(uow, prior.task_id) if prior else None
        if prior and located is None:
            raise DomainError(ErrorCode.CONFLICT, "Task record is inconsistent")
        return located[0] if located else None

    async def record_usage(self, ctx: RequestContext, task_id: EntityId, call_key: str,
                           input_tokens: int, output_tokens: int) -> None:
        # A provider may finish after membership is revoked. Persist its usage
        # before any further authorized business action is attempted.
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, task_id)
        if located is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Task not found")
        task = located[0]
        if ctx.actor_id != task.actor_id or ctx.workspace_id != task.workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Task execution identity changed")
        if (not re.fullmatch(r"generate|repair:[12]", call_key) or
                type(input_tokens) is not int or type(output_tokens) is not int or
                input_tokens < 0 or output_tokens < 0):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid model usage checkpoint")
        scope = Scope(workspace_id=task.workspace_id, project_id=task.project_id)
        record_id = _hash(f"model-usage:{task.id}:{call_key}")
        async with self.dependencies.uow() as uow:
            repo = uow.repository(ModelUsageRecord)
            existing = await repo.get(scope, record_id)
            if existing:
                if existing.task_id != task_id:
                    raise DomainError(ErrorCode.CONFLICT, "Model usage checkpoint changed")
                if existing.input_tokens is not None or existing.output_tokens is not None:
                    if (existing.input_tokens != input_tokens or
                            existing.output_tokens != output_tokens):
                        raise DomainError(ErrorCode.CONFLICT, "Model usage checkpoint changed")
                    return
            await repo.save(scope, ModelUsageRecord(id=record_id, task_id=task_id,
                call_key=call_key, input_tokens=input_tokens, output_tokens=output_tokens,
                revision=existing.revision if existing else 0),
                existing.revision if existing else None)
            await uow.commit()

    async def begin_model_call(self, ctx: RequestContext, task_id: EntityId,
                               call_key: str, lease_owner: str | None) -> None:
        task = await self.get(ctx, task_id)
        if ctx.actor_id != task.actor_id or ctx.workspace_id != task.workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Task execution identity changed")
        if not re.fullmatch(r"generate|repair:[12]", call_key):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid model call key")
        scope = Scope(workspace_id=task.workspace_id, project_id=task.project_id)
        record_id = _hash(f"model-usage:{task.id}:{call_key}")
        async with self.dependencies.uow() as uow:
            current = await uow.repository(Task).get(scope, task_id)
            expected_state = TaskState.GENERATING if call_key == "generate" else TaskState.REPAIRING
            if current is None or current.state != expected_state:
                raise DomainError(ErrorCode.CONFLICT, "Task state changed before model call")
            await self.dependencies.commit_fence.assert_owned_lease(
                uow, task_id, lease_owner or "")
            repo = uow.repository(ModelUsageRecord)
            if await repo.get(scope, record_id):
                raise DomainError(ErrorCode.CONFLICT, "Model call already started")
            await repo.save(scope, ModelUsageRecord(id=record_id, task_id=task_id,
                call_key=call_key, input_tokens=None, output_tokens=None), None)
            await uow.commit()

    async def has_uncheckpointed_model_call(self, task_id: EntityId) -> bool:
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, task_id)
        if located is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Task not found")
        task, scope = located
        cursor = None
        while True:
            async with self.dependencies.uow() as uow:
                page = await uow.repository(ModelUsageRecord).list(scope,
                    PageQuery(cursor=cursor, limit=100))
            if any(item.task_id == task_id and
                   (item.input_tokens is None or item.output_tokens is None)
                   for item in page.items):
                return True
            if page.next_cursor is None:
                return False
            cursor = page.next_cursor

    async def usage_totals(self, ctx: RequestContext, task_id: EntityId) -> tuple[int, int]:
        await self.get(ctx, task_id)
        return await self.usage_for_execution(task_id)

    async def usage_for_execution(self, task_id: EntityId) -> tuple[int, int]:
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, task_id)
        if located is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Task not found")
        task = located[0]
        scope = Scope(workspace_id=task.workspace_id, project_id=task.project_id)
        cursor = None
        total_input = total_output = 0
        while True:
            async with self.dependencies.uow() as uow:
                page = await uow.repository(ModelUsageRecord).list(scope,
                    PageQuery(cursor=cursor, limit=100))
            for item in page.items:
                if item.task_id == task_id:
                    total_input += item.input_tokens or 0
                    total_output += item.output_tokens or 0
            if page.next_cursor is None:
                return total_input, total_output
            cursor = page.next_cursor

    async def submit(self, ctx: RequestContext, command: SubmitTask) -> Task:
        await self.dependencies.authorization.require(ctx, "project:write", command.project_id)
        if ctx.actor_id is None or ctx.workspace_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        if (not command.mutation.idempotency_key or not command.model or not command.agent_mode or
                command.expected_head_revision < 0):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid task input")
        scope = Scope(workspace_id=ctx.workspace_id, project_id=command.project_id)
        mutation_id = _hash(f"task:submit:{ctx.actor_id}:{command.project_id}:{command.mutation.idempotency_key}")
        digest = _hash(f"{command.project_id}:{command.message_id}:{command.base_version_id}:"
                       f"{command.expected_head_revision}:{command.model}:{command.agent_mode}")
        async with self.dependencies.uow() as uow:
            prior = await uow.repository(TaskMutation).get(GLOBAL, mutation_id)
            if prior:
                if prior.input_digest != digest:
                    raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different task")
                located = await self._located(uow, prior.task_id)
                if located is None:
                    raise DomainError(ErrorCode.CONFLICT, "Task record is inconsistent")
                task = located[0]
            else:
                active = await uow.repository(ActiveTask).get(scope, command.project_id)
                if active:
                    raise DomainError(ErrorCode.CONFLICT, "A project task is already active")
                task = Task(id=self.dependencies.ids.new(), workspace_id=ctx.workspace_id,
                    actor_id=ctx.actor_id, project_id=command.project_id, request=command,
                    state=TaskState.QUEUED, base_version_id=command.base_version_id,
                    result_version_id=None, attempts=0, error_code=None, revision=0)
                task = await uow.repository(Task).save(scope, task, None)
                await uow.repository(TaskLocator).save(GLOBAL, TaskLocator(id=task.id,
                    target_workspace_id=ctx.workspace_id, target_project_id=command.project_id), None)
                await uow.repository(TaskMutation).save(GLOBAL, TaskMutation(id=mutation_id,
                    task_id=task.id, input_digest=digest), None)
                await uow.repository(ActiveTask).save(scope, ActiveTask(id=command.project_id,
                    project_id=command.project_id, task_id=task.id), None)
                await uow.commit()
        # Recovery scans queued records if a crash occurs between commit and enqueue.
        await self.dependencies.queue.enqueue(task.id, f"task:{task.id}")
        return task

    async def get(self, ctx: RequestContext, task_id: EntityId) -> Task:
        async with self.dependencies.uow() as uow:
            located = await self._located(uow, task_id)
        if located is None:
            raise DomainError(ErrorCode.NOT_FOUND, "Task not found")
        task = located[0]
        await self.dependencies.authorization.require(ctx, "project:history", task.project_id)
        return task

    async def active_for_project(self, ctx: RequestContext, project_id: EntityId) -> Task | None:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        scope = Scope(workspace_id=ctx.workspace_id, project_id=project_id)
        async with self.dependencies.uow() as uow:
            active = await uow.repository(ActiveTask).get(scope, project_id)
            task = await uow.repository(Task).get(scope, active.task_id) if active else None
        if active and task is None:
            raise DomainError(ErrorCode.CONFLICT, "Active task record is inconsistent")
        return task

    async def latest_for_project(self, ctx: RequestContext, project_id: EntityId) -> Task | None:
        await self.dependencies.authorization.require(ctx, "project:history", project_id)
        if ctx.workspace_id is None:
            raise DomainError(ErrorCode.UNAUTHENTICATED, "Sign in required")
        scope = Scope(workspace_id=ctx.workspace_id, project_id=project_id)
        async with self.dependencies.uow() as uow:
            task = await uow.repository(Task).latest(scope)
        if task is None:
            return None
        if task.project_id != project_id or task.workspace_id != ctx.workspace_id:
            raise DomainError(ErrorCode.CONFLICT, "Latest task record is inconsistent")
        return task

    async def transition(self, ctx: RequestContext, command: TaskTransition) -> Task:
        task = await self.get(ctx, command.task_id)
        await self.dependencies.authorization.require(ctx, "project:write", task.project_id)
        if command.target not in TRANSITIONS.get(task.state, set()):
            raise DomainError(ErrorCode.CONFLICT, "Invalid task state transition")
        if command.target == TaskState.SUCCEEDED and command.result_version_id is None:
            raise DomainError(ErrorCode.INVALID_INPUT, "Successful task needs a result version")
        if command.target == TaskState.REPAIRING and task.attempts >= 2:
            raise DomainError(ErrorCode.CONFLICT, "Automatic repair limit reached")
        scope = Scope(workspace_id=task.workspace_id, project_id=task.project_id)
        async with self.dependencies.uow() as uow:
            repo = uow.repository(Task)
            current = await repo.get(scope, task.id)
            if current is None or current.revision != command.expected_revision:
                raise DomainError(ErrorCode.CONFLICT, "Task revision changed")
            updated = await repo.save(scope, Task(
                id=task.id, workspace_id=task.workspace_id, actor_id=task.actor_id,
                project_id=task.project_id, request=task.request, state=command.target,
                base_version_id=task.base_version_id,
                result_version_id=command.result_version_id or task.result_version_id,
                attempts=task.attempts + (1 if command.target == TaskState.REPAIRING else 0),
                error_code=command.error_code, revision=task.revision), task.revision)
            if command.target in TERMINAL:
                active = await uow.repository(ActiveTask).get(scope, task.project_id)
                if active and active.task_id == task.id:
                    await uow.repository(ActiveTask).delete(scope, active.id, active.revision)
            await uow.commit()
        return updated

    async def events(self, ctx: RequestContext, task_id: EntityId,
                     after_sequence: int) -> tuple[TaskEvent, ...]:
        await self.get(ctx, task_id)
        return await self.dependencies.events.read(task_id, after_sequence)

    async def append_event(self, ctx: RequestContext, event: TaskEvent) -> None:
        task = await self.get(ctx, event.task_id)
        await self.dependencies.authorization.require(ctx, "project:write", task.project_id)
        if event.project_id != task.project_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Event project differs from task")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", event.kind) or len(event.message) > 4000:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid task event")
        await self.dependencies.events.append(event)

    async def cancel(self, ctx: RequestContext, task_id: EntityId, mutation: Mutation) -> Task:
        task = await self.get(ctx, task_id)
        if task.state == TaskState.CANCELLED:
            return task
        if task.state in TERMINAL:
            raise DomainError(ErrorCode.CONFLICT, "Task is already finished")
        return await self.transition(ctx, TaskTransition(task_id=task_id,
            expected_revision=task.revision, target=TaskState.CANCELLED))

    async def recover_interrupted(self) -> tuple[EntityId, ...]:
        recovered = []
        cursor = None
        while True:
            async with self.dependencies.uow() as uow:
                page = await uow.repository(TaskLocator).list(GLOBAL, PageQuery(cursor=cursor, limit=100))
                tasks = [await self._located(uow, locator.id) for locator in page.items]
            for located in tasks:
                if located is None:
                    continue
                task, scope = located
                if task.state == TaskState.QUEUED:
                    await self.dependencies.queue.enqueue(task.id, f"task:{task.id}")
                    recovered.append(task.id)
                elif task.state not in TERMINAL:
                    async with self.dependencies.uow() as uow:
                        repo = uow.repository(Task)
                        current = await repo.get(scope, task.id)
                        if (current and current.state not in TERMINAL and
                                not await self.dependencies.commit_fence.has_live_lease(uow, current.id)):
                            await repo.save(scope, Task(id=current.id, workspace_id=current.workspace_id,
                                actor_id=current.actor_id, project_id=current.project_id,
                                request=current.request, state=TaskState.INTERRUPTED,
                                base_version_id=current.base_version_id,
                                result_version_id=current.result_version_id, attempts=current.attempts,
                                error_code="process_restart", revision=current.revision), current.revision)
                            active = await uow.repository(ActiveTask).get(scope, current.project_id)
                            if active and active.task_id == current.id:
                                await uow.repository(ActiveTask).delete(scope, active.id, active.revision)
                            await uow.commit()
                            recovered.append(task.id)
                elif task.state in {TaskState.INTERRUPTED, TaskState.FAILED,
                                    TaskState.CANCELLED}:
                    async with self.dependencies.uow() as uow:
                        if not await self.dependencies.commit_fence.has_live_lease(uow, task.id):
                            recovered.append(task.id)
            if page.next_cursor is None:
                return tuple(recovered)
            cursor = page.next_cursor
