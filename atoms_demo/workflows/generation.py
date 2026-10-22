"""Generation orchestration through business protocols and explicit pricing."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Awaitable, Callable

from atoms_demo.contracts.billing import UsageCharge
from atoms_demo.contracts.builds import BuildInput
from atoms_demo.contracts.conversations import AppendMessage, MessageRole
from atoms_demo.contracts.generation import GenerationInput, RepairInput, SourceFile, FileChangeSet
from atoms_demo.contracts.storage import Upload
from atoms_demo.contracts.tasks import Task, TaskState, SubmitTask, TaskTransition
from atoms_demo.contracts.versions import CreateVersion, EditVersion, FileEntry, PromoteVersion, Version
from atoms_demo.core.events import TaskEvent
from atoms_demo.core.errors import DomainError, ErrorCode, MeteredProviderError, UnsentModelError
from atoms_demo.core.types import EntityId, Mutation, PageQuery, RequestContext, Scope
from atoms_demo.ports.providers import UsagePricing
from atoms_demo.ports.runtime import Clock
from atoms_demo.workflows.registry import GenerationServices


@dataclass(frozen=True, kw_only=True)
class GenerateApplication:
    project_id: EntityId
    prompt: str
    attachment_ids: tuple[EntityId, ...]
    model: str
    agent_mode: str
    expected_head_revision: int
    mutation: Mutation
    confirmed_build_intent: bool = False


def _key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _approval_items(prompt: str) -> tuple[str, ...]:
    """Split the user's request into selectable clauses without inventing tasks."""
    clauses = [part.strip() for part in re.split(r"[\n；;。]+", prompt) if part.strip()]
    if len(clauses) > 8:
        clauses = clauses[:7] + ["；".join(clauses[7:])]
    return tuple(clauses) or (prompt.strip(),)


def _asks_how_to(prompt: str) -> bool:
    """Route an unambiguous how-to question away from the build pipeline."""
    text = prompt.strip()
    if not text or len(text) > 2_000:
        return False
    question = re.match(
        r"^(?:(?:请问|我想知道|想知道|告诉我|请教我)[，,:：\s]*)?"
        r"(?:如何|怎么|怎样|该如何|怎样才能|how\s+(?:to|do\s+i|can\s+i)\b)",
        text, flags=re.IGNORECASE)
    explicit_build = re.search(
        r"(?:^|[，。；;？！?\n])\s*(?:请(?!问|教)|帮我|给我|直接|现在|立刻|马上)"
        r"(?:.{0,4})?(?:创建|生成|实现|开发|搭建|制作|构建)", text)
    return bool(question) and not bool(explicit_build)


class GenerationWorkflow:
    def __init__(self, services: GenerationServices, pricing: UsagePricing,
                 max_repairs: int, clock: Clock) -> None:
        self.services = services
        self.pricing = pricing
        self.max_repairs = max_repairs
        self.clock = clock

    async def _message(self, ctx: RequestContext, task: Task):
        cursor = None
        while True:
            page = await self.services.conversations.history(
                ctx, task.project_id, PageQuery(cursor=cursor, limit=100))
            for item in page.items:
                if item.id == task.request.message_id:
                    return item
            if page.next_cursor is None:
                raise DomainError(ErrorCode.NOT_FOUND, "Task message not found")
            cursor = page.next_cursor

    async def cancel(self, ctx: RequestContext, task_id: EntityId, mutation: Mutation) -> Task:
        task, previous_state = await self.services.tasks.cancel_with_previous_state(
            ctx, task_id, mutation)
        if (task.request.agent_mode == "approval" and
                previous_state in {TaskState.WAITING_FOR_APPROVAL, TaskState.QUEUED}):
            await self.services.billing.release_task(ctx, task_id)
        return task

    async def _event(self, ctx: RequestContext, task: Task, kind: str, message: str,
                     version_id: EntityId | None = None) -> None:
        prior = await self.services.tasks.events(ctx, task.id, 0)
        sequence = prior[-1].sequence + 1 if prior else 1
        await self.services.tasks.append_event(ctx, TaskEvent(
            event_id=EntityId(_key(f"{task.id}:{sequence}:{kind}")), task_id=task.id,
            project_id=task.project_id, sequence=sequence, kind=kind, message=message,
            created_at=self.clock.now(), version_id=version_id))

    async def _assistant_reply(self, ctx: RequestContext, task: Task, summary: str) -> None:
        """Record one durable agent turn after a successful generated version."""
        if task.request.agent_mode == "manual_edit":
            return
        detail = summary.strip()[:4000]
        content = ("应用已生成并通过构建。" + (f"{detail}\n\n" if detail else "") +
                   "可以查看预览，或继续告诉我需要修改什么。")
        await self.services.conversations.append(ctx, AppendMessage(
            project_id=task.project_id, role=MessageRole.ASSISTANT, content=content,
            attachment_ids=(), task_id=task.id,
            mutation=Mutation(idempotency_key=f"{task.id}:assistant-reply")))

    async def ensure_assistant_reply(self, ctx: RequestContext, task: Task) -> None:
        version = await self.services.versions.get(ctx, task.result_version_id)
        await self._assistant_reply(ctx, task, version.summary)

    async def recover_answer(self, ctx: RequestContext, task: Task) -> Task:
        if (task.request.agent_mode != "answer" or task.state != TaskState.INTERRUPTED or
                task.error_code not in {"process_restart", "worker_unavailable"} or
                await self.services.tasks.answer_checkpoint(ctx, task.id) is None):
            return task
        status = await self.services.billing.task_reservation_status(
            task.workspace_id, task.id)
        if status != "settled":
            return task
        completed = await self.services.tasks.recover_answer(ctx, task.id)
        await self._event(ctx, completed, "succeeded", "Question answered after recovery")
        return completed

    async def submit(self, ctx: RequestContext, command: GenerateApplication) -> Task:
        if not command.prompt.strip() or not command.model or not command.mutation.idempotency_key:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid generation request")
        if (type(command.confirmed_build_intent) is not bool or
                (command.confirmed_build_intent and command.agent_mode == "answer")):
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid confirmed build intent")
        if not command.confirmed_build_intent and _asks_how_to(command.prompt):
            command = replace(command, agent_mode="answer")
        prior = await self.services.tasks.find_submission(ctx, command.project_id,
            command.mutation.idempotency_key)
        if prior is not None:
            previous = await self._message(ctx, prior)
            if (previous.content != command.prompt or previous.attachment_ids != command.attachment_ids or
                    prior.request.expected_head_revision != command.expected_head_revision or
                    prior.request.model != command.model or prior.request.agent_mode != command.agent_mode or
                    prior.request.confirmed_build_intent != command.confirmed_build_intent):
                raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different generation input")
            return prior
        if command.agent_mode == "answer":
            for object_id in command.attachment_ids:
                attachment = await self.services.storage.get(ctx, object_id)
                if (attachment.blob.media_type not in {"text/plain", "text/markdown", "application/json"} or
                        attachment.blob.size_bytes > 100_000):
                    raise DomainError(ErrorCode.INVALID_INPUT,
                                      "Questions accept UTF-8 text attachments up to 100 KB only")
                try:
                    (await self.services.storage.download(ctx, object_id)).decode("utf-8")
                except UnicodeDecodeError:
                    raise DomainError(ErrorCode.INVALID_INPUT,
                                      "Question attachment must be UTF-8 text") from None
        await self.services.projects.get(ctx, command.project_id)
        options = await self.services.generation.models(ctx)
        if not any(option.key == command.model and option.available for option in options):
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Selected model is unavailable")
        head = await self.services.versions.head(ctx, command.project_id)
        if head.revision != command.expected_head_revision:
            raise DomainError(ErrorCode.CONFLICT, "Project head revision changed")
        maximum = await self.pricing.reserve(command.model, command.prompt)
        if type(maximum) is not int or maximum <= 0:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Invalid generation price quote")
        message = await self.services.conversations.append(ctx, AppendMessage(
            project_id=command.project_id, role=MessageRole.USER, content=command.prompt,
            attachment_ids=command.attachment_ids, task_id=None,
            mutation=Mutation(idempotency_key=f"{command.mutation.idempotency_key}:message")))
        task = await self.services.tasks.submit(ctx, SubmitTask(
            project_id=command.project_id, message_id=message.id, base_version_id=head.version_id,
            expected_head_revision=head.revision, model=command.model, agent_mode=command.agent_mode,
            mutation=command.mutation,
            confirmed_build_intent=command.confirmed_build_intent,
            approval_items=(_approval_items(command.prompt)
                            if command.agent_mode == "approval" else ())))
        try:
            await self.services.billing.reserve(ctx, task.id, maximum,
                Mutation(idempotency_key=f"{task.id}:reserve"), task.project_id)
        except Exception:
            await self.services.tasks.cancel(ctx, task.id,
                Mutation(idempotency_key=f"{task.id}:no-reservation"))
            raise
        if task.state == TaskState.WAITING_FOR_APPROVAL:
            await self._event(ctx, task, "waiting_for_approval", "Waiting for selected tasks")
        elif task.request.agent_mode == "answer":
            await self._event(ctx, task, "queued", "Question queued")
        else:
            await self._event(ctx, task, "queued", "Generation queued")
        return task

    async def _files(self, ctx: RequestContext, version: Version | None) -> tuple[dict[str, FileEntry], tuple[SourceFile, ...]]:
        if version is None:
            return {}, ()
        entries = {item.path: item for item in version.files}
        source = []
        for item in version.files:
            source.append(SourceFile(path=item.path,
                content=await self.services.versions.read_file(ctx, version.id, item.path)))
        return entries, tuple(source)

    async def _candidate(self, ctx: RequestContext, task: Task, base: Version | None,
                         changes: FileChangeSet, index: int) -> Version:
        entries, _ = await self._files(ctx, base)
        for path in changes.deleted_paths:
            if path not in entries:
                raise DomainError(ErrorCode.INVALID_INPUT, "Model deleted a missing file")
            del entries[path]
        for source in changes.upserts:
            upload = await self.services.storage.upload(ctx, Upload(
                scope=Scope(workspace_id=task.workspace_id, project_id=task.project_id),
                purpose="source", display_name=source.path, media_type="text/plain",
                content=source.content.encode("utf-8"),
                mutation=Mutation(idempotency_key=f"{task.id}:file:{index}:{_key(source.path)}")))
            entries[source.path] = FileEntry(path=source.path, blob=upload.blob)
        return await self.services.versions.create_candidate(ctx, CreateVersion(
            project_id=task.project_id, base_version_id=base.id if base else None,
            files=tuple(entries.values()), summary=changes.summary,
            mutation=Mutation(idempotency_key=f"{task.id}:candidate:{index}")))

    async def _transition(self, ctx: RequestContext, task: Task, state: TaskState,
                          result: EntityId | None = None, error: str | None = None) -> Task:
        return await self.services.tasks.transition(ctx, TaskTransition(
            task_id=task.id, expected_revision=task.revision, target=state,
            result_version_id=result, error_code=error))

    async def run(self, ctx: RequestContext, task_id: EntityId,
                  lease_guard: Callable[[], Awaitable[None]] | None = None,
                  lease_owner: str | None = None) -> Task:
        task = await self.services.tasks.get(ctx, task_id)
        if ctx.actor_id != task.actor_id or ctx.workspace_id != task.workspace_id:
            raise DomainError(ErrorCode.FORBIDDEN, "Task actor or workspace changed")
        if task.state != TaskState.QUEUED:
            raise DomainError(ErrorCode.CONFLICT, "Task is not queued")
        manual = task.request.agent_mode == "manual_edit"
        answering = task.request.agent_mode == "answer"
        message = None if manual else await self._message(ctx, task)
        approved_prompt = message.content if message else ""
        if task.request.agent_mode == "approval":
            approval = await self.services.tasks.approval(ctx, task.id)
            if approval is None or approval.selected_indices is None:
                raise DomainError(ErrorCode.CONFLICT, "Task approval is missing")
            selected = [approval.items[index] for index in approval.selected_indices]
            approved_prompt = ("只实现本次已批准的任务，未选中的任务暂不实现：\n" +
                               "\n".join(f"{index + 1}. {item}" for index, item in enumerate(selected)))
            if approval.additional_instructions.strip():
                approved_prompt += "\n补充要求：" + approval.additional_instructions.strip()
        maximum = None
        reservation = None
        total_input = total_output = 0
        active_call_key: str | None = None
        current = task
        try:
            maximum = await self.pricing.reserve(task.request.model,
                message.content if message else "manual edit")
            if type(maximum) is not int or maximum < 0 or (maximum == 0 and not manual):
                raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Invalid generation price quote")
            reservation = await self.services.billing.reserve(ctx, task.id, maximum,
                Mutation(idempotency_key=f"{task.id}:reserve"), task.project_id)
            head = await self.services.versions.head(ctx, task.project_id)
            if head.revision != task.request.expected_head_revision or head.version_id != task.base_version_id:
                raise DomainError(ErrorCode.CONFLICT, "Project head changed before execution")
            base = await self.services.versions.get(ctx, task.base_version_id) if task.base_version_id else None
            if manual:
                candidate = await self.services.versions.get(ctx, task.request.message_id)
                current = await self._transition(ctx, current, TaskState.BUILDING)
            else:
                current = await self._transition(ctx, current, TaskState.GENERATING)
                await self._event(ctx, current, "generating",
                                  "Answering question" if answering else "Model generation started")
                _, source = await self._files(ctx, base)
                attachments = []
                for object_id in message.attachment_ids:
                    attachments.append((await self.services.storage.get(ctx, object_id)).blob)
                generation_input = GenerationInput(task_id=task.id, prompt=approved_prompt,
                    context_summary=await self.services.conversations.summarize(ctx, task.project_id),
                    files=source, attachments=tuple(attachments), model=task.request.model,
                    agent_mode=task.request.agent_mode)
                active_call_key = "generate"
                if lease_guard:
                    await lease_guard()
                await self.services.tasks.begin_model_call(ctx, task.id, active_call_key, lease_owner)
                if answering:
                    answer = await self.services.generation.answer(ctx, generation_input)
                    await self.services.tasks.checkpoint_answer(ctx, task.id, answer.content,
                        answer.input_tokens, answer.output_tokens, lease_owner)
                    active_call_key = None
                    units = await self.pricing.charge(task.request.model,
                        answer.input_tokens, answer.output_tokens)
                    if type(units) is not int or not 0 <= units <= maximum:
                        raise DomainError(ErrorCode.CONFLICT, "Usage charge exceeds reservation")
                    if lease_guard:
                        await lease_guard()
                    await self.services.billing.settle(ctx, UsageCharge(
                        reservation_id=reservation.id, consumed_units=units,
                        source_event_id=f"task:{task.id}:usage"))
                    current = await self.services.tasks.complete_answer(
                        ctx, task.id, current.revision, lease_owner)
                    await self._event(ctx, current, "succeeded", "Question answered")
                    return current
                changes = await self.services.generation.generate(ctx, generation_input)
                await self.services.tasks.record_usage(ctx, task.id, "generate",
                    changes.input_tokens, changes.output_tokens)
                active_call_key = None
                total_input += changes.input_tokens
                total_output += changes.output_tokens
                candidate = await self._candidate(ctx, task, base, changes, 0)
                current = await self._transition(ctx, current, TaskState.BUILDING)
            diagnostics = ()
            for repair_index in range(self.max_repairs + 1):
                await self._event(ctx, current, "building", "Fixed recipe build started", candidate.id)
                result = await self.services.builds.build(ctx, BuildInput(task_id=task.id,
                    version_id=candidate.id, source_archive=candidate.source_archive,
                    template_id="react-vite-v1"))
                if result.success and result.artifact:
                    scan = await self.services.builds.scan(ctx, candidate.id, result.artifact)
                    if scan.passed:
                        await self.services.versions.record_build(ctx, candidate.id, result.artifact, True)
                        if lease_guard:
                            await lease_guard()
                        current = await self._transition(ctx, current, TaskState.COMMITTING,
                                                         candidate.id)
                        total_input, total_output = await self.services.tasks.usage_totals(ctx, task.id)
                        units = await self.pricing.charge(task.request.model, total_input, total_output)
                        if type(units) is not int or not 0 <= units <= maximum:
                            raise DomainError(ErrorCode.CONFLICT, "Usage charge exceeds reservation")
                        await self.services.billing.settle(ctx, UsageCharge(
                            reservation_id=reservation.id, consumed_units=units,
                            source_event_id=f"task:{task.id}:usage"))
                        if lease_guard:
                            await lease_guard()
                        await self.services.versions.promote(ctx, PromoteVersion(
                            project_id=task.project_id, version_id=candidate.id,
                            expected_head_revision=task.request.expected_head_revision,
                            task_id=task.id, mutation=Mutation(idempotency_key=f"{task.id}:promote"),
                            lease_owner=lease_owner))
                        await self._assistant_reply(ctx, current, candidate.summary)
                        current = await self._transition(ctx, current, TaskState.SUCCEEDED, candidate.id)
                        await self._event(ctx, current, "succeeded", "Build and scan succeeded", candidate.id)
                        return current
                    diagnostics = scan.findings
                else:
                    diagnostics = result.diagnostics
                await self.services.versions.record_build(ctx, candidate.id, result.artifact, False)
                if manual or repair_index >= self.max_repairs:
                    raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Build or scan failed")
                current = await self._transition(ctx, current, TaskState.REPAIRING)
                await self._event(ctx, current, "repairing", "Automatic repair started", candidate.id)
                _, candidate_source = await self._files(ctx, candidate)
                active_call_key = f"repair:{repair_index + 1}"
                if lease_guard:
                    await lease_guard()
                await self.services.tasks.begin_model_call(ctx, task.id, active_call_key, lease_owner)
                changes = await self.services.generation.repair(ctx, RepairInput(
                    generation=replace(generation_input, files=candidate_source), diagnostics=diagnostics,
                    repair_number=repair_index + 1))
                await self.services.tasks.record_usage(ctx, task.id, active_call_key,
                    changes.input_tokens, changes.output_tokens)
                active_call_key = None
                total_input += changes.input_tokens
                total_output += changes.output_tokens
                candidate = await self._candidate(ctx, task, candidate, changes, repair_index + 1)
                current = await self._transition(ctx, current, TaskState.BUILDING)
        except Exception as exc:
            try:
                if isinstance(exc, MeteredProviderError) and active_call_key is not None:
                    await self.services.tasks.record_usage(ctx, task.id, active_call_key,
                        exc.input_tokens, exc.output_tokens)
                elif isinstance(exc, UnsentModelError) and active_call_key is not None:
                    await self.services.tasks.record_usage(ctx, task.id, active_call_key, 0, 0)
                total_input, total_output = await self.services.tasks.usage_totals(ctx, task.id)
                if await self.services.tasks.has_uncheckpointed_model_call(task.id):
                    pass  # Keep the reservation for provider usage reconciliation.
                elif reservation is None:
                    await self.services.billing.release_task(ctx, task.id)
                elif total_input or total_output:
                    units = await self.pricing.charge(task.request.model, total_input, total_output)
                    if type(units) is int and 0 <= units <= maximum:
                        await self.services.billing.settle(ctx, UsageCharge(
                            reservation_id=reservation.id, consumed_units=units,
                            source_event_id=f"task:{task.id}:usage"))
                    else:
                        await self.services.billing.release(ctx, reservation.id)
                else:
                    await self.services.billing.release(ctx, reservation.id)
            finally:
                code = exc.code.value if isinstance(exc, DomainError) else "execution_failed"
                if await self.services.tasks.has_uncheckpointed_model_call(task.id):
                    code = "usage_reconciliation_required"
                latest = await self.services.tasks.get(ctx, task.id)
                if latest.state == TaskState.COMMITTING and latest.result_version_id:
                    head = await self.services.versions.head(ctx, task.project_id)
                    if head.version_id == latest.result_version_id:
                        await self.ensure_assistant_reply(ctx, latest)
                        current = await self._transition(ctx, latest, TaskState.SUCCEEDED,
                                                         latest.result_version_id)
                        await self._event(ctx, current, "succeeded", "Build and scan succeeded",
                                          current.result_version_id)
                        return current
                if latest.state not in {TaskState.SUCCEEDED, TaskState.CANCELLED,
                                        TaskState.FAILED, TaskState.INTERRUPTED}:
                    if answering and await self.services.tasks.answer_checkpoint(ctx, task.id):
                        current = await self._transition(ctx, latest, TaskState.INTERRUPTED,
                                                         error="worker_unavailable")
                        await self._event(ctx, current, "interrupted",
                                          "Answer saved; finishing delivery")
                    else:
                        current = await self._transition(ctx, latest, TaskState.FAILED, error=code)
                        await self._event(ctx, current, "failed", "Generation failed")
                else:
                    current = latest
            return current
        raise AssertionError("Unreachable generation state")

    async def edit(self, ctx: RequestContext, command: EditVersion) -> Task:
        prior = await self.services.tasks.find_submission(ctx, command.project_id,
            command.mutation.idempotency_key)
        if prior is not None:
            if prior.request.agent_mode != "manual_edit" or prior.base_version_id != command.base_version_id:
                raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different edit")
            candidate = await self.services.versions.edit_candidate(ctx, command)
            if candidate.id != prior.request.message_id:
                raise DomainError(ErrorCode.CONFLICT, "Idempotency key was used with different edit")
            return prior
        head = await self.services.versions.head(ctx, command.project_id)
        if head.version_id != command.base_version_id:
            raise DomainError(ErrorCode.CONFLICT, "Edit base is not current head")
        maximum = await self.pricing.reserve("manual", "manual edit")
        if type(maximum) is not int or maximum < 0:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Invalid edit price quote")
        candidate = await self.services.versions.edit_candidate(ctx, command)
        task = await self.services.tasks.submit(ctx, SubmitTask(
            project_id=command.project_id, message_id=candidate.id,
            base_version_id=command.base_version_id, expected_head_revision=head.revision,
            model="manual", agent_mode="manual_edit", mutation=command.mutation))
        try:
            await self.services.billing.reserve(ctx, task.id, maximum,
                Mutation(idempotency_key=f"{task.id}:reserve"), task.project_id)
        except Exception:
            await self.services.tasks.cancel(ctx, task.id,
                Mutation(idempotency_key=f"{task.id}:no-reservation"))
            raise
        await self._event(ctx, task, "queued", "Manual edit queued", candidate.id)
        return task
