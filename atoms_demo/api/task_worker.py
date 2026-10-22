"""Local queue consumer with lease checks and restart compensation."""
from __future__ import annotations

import asyncio
import logging
from threading import Event
from time import monotonic
from uuid import uuid4

from atoms_demo.api.application import Application
from atoms_demo.contracts.tasks import TaskState
from atoms_demo.core.errors import DomainError, ErrorCode


async def compensate(application: Application, task) -> None:
    status = await application.services.billing.task_reservation_status(
        task.workspace_id, task.id)
    if status in {"settled", "released"}:
        return
    if await application.services.tasks.has_uncheckpointed_model_call(task.id):
        logging.warning("Model usage needs manual reconciliation before settlement: %s", task.id)
        return
    input_tokens, output_tokens = await application.services.tasks.usage_for_execution(task.id)
    if input_tokens or output_tokens:
        units = await application.generation.pricing.charge(task.request.model,
                                                            input_tokens, output_tokens)
        if type(units) is not int or units < 0:
            raise DomainError(ErrorCode.PROVIDER_UNAVAILABLE, "Invalid checkpoint usage price")
        await application.services.billing.settle_interrupted(task.workspace_id, task.id, units)
    else:
        await application.services.billing.release_interrupted(task.workspace_id, task.id)


async def recover(application: Application) -> None:
    for task_id in await application.services.tasks.recover_interrupted():
        ctx = await application.services.tasks.execution_context(task_id)
        try:
            task = await application.services.tasks.get(ctx, task_id)
        except DomainError as exc:
            if exc.code not in {ErrorCode.FORBIDDEN, ErrorCode.UNAUTHENTICATED}:
                raise
            task = await application.services.tasks.interrupt_execution(task_id,
                "authorization_revoked")
        if task.state == TaskState.INTERRUPTED and task.result_version_id:
            try:
                head = await application.services.versions.head(ctx, task.project_id)
            except DomainError:
                head = None
            if head and head.version_id == task.result_version_id:
                await application.generation.ensure_assistant_reply(ctx, task)
                task = await application.services.tasks.reconcile_committed(
                    task.id, task.result_version_id)
        if task.state in {TaskState.INTERRUPTED, TaskState.FAILED, TaskState.CANCELLED}:
            try:
                await compensate(application, task)
                if task.state == TaskState.INTERRUPTED:
                    await application.generation.recover_answer(ctx, task)
            except DomainError:
                logging.exception("Task compensation remains pending: %s", task.id)


async def run_once(application: Application, worker_id: str, lease_seconds: int) -> bool:
    task_id = await application.queue.lease(worker_id)
    if task_id is None:
        return False
    pulse_stop = asyncio.Event()
    lease_lost = asyncio.Event()
    acknowledge = False

    async def guard() -> None:
        if lease_lost.is_set():
            raise DomainError(ErrorCode.CONFLICT, "Worker lease was lost")
        try:
            await application.queue.heartbeat(task_id, worker_id)
        except ValueError:
            lease_lost.set()
            raise DomainError(ErrorCode.CONFLICT, "Worker lease was lost") from None

    async def pulse() -> None:
        while not pulse_stop.is_set():
            try:
                await asyncio.wait_for(pulse_stop.wait(), timeout=max(1, lease_seconds // 3))
            except asyncio.TimeoutError:
                try:
                    await guard()
                except DomainError:
                    return

    heartbeat = asyncio.create_task(pulse())
    recover_after_ack = None
    try:
        ctx = await application.services.tasks.execution_context(task_id)
        try:
            task = await application.services.tasks.get(ctx, task_id)
        except DomainError as exc:
            if exc.code not in {ErrorCode.FORBIDDEN, ErrorCode.UNAUTHENTICATED}:
                raise
            task = await application.services.tasks.interrupt_execution(task_id,
                "authorization_revoked")
        if task.state == TaskState.QUEUED:
            try:
                task = await application.generation.run(ctx, task_id, guard, worker_id)
            except DomainError as exc:
                if exc.code in {ErrorCode.FORBIDDEN, ErrorCode.UNAUTHENTICATED}:
                    task = await application.services.tasks.interrupt_execution(task_id,
                        "authorization_revoked")
                else:
                    raise
        elif task.state == TaskState.COMMITTING and task.result_version_id:
            head = await application.services.versions.head(ctx, task.project_id)
            if head.version_id == task.result_version_id:
                await application.generation.ensure_assistant_reply(ctx, task)
                task = await application.services.tasks.reconcile_committed(
                    task.id, task.result_version_id)
            else:
                task = await application.services.tasks.interrupt_execution(task_id,
                    "worker_unavailable")
        elif task.state not in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED,
                                TaskState.INTERRUPTED}:
            task = await application.services.tasks.interrupt_execution(task_id,
                "worker_unavailable")
        if task.state in {TaskState.CANCELLED, TaskState.INTERRUPTED,
                          TaskState.FAILED}:
            await compensate(application, task)
        acknowledge = task.state in {TaskState.SUCCEEDED, TaskState.FAILED,
                                     TaskState.CANCELLED, TaskState.INTERRUPTED}
        if task.state == TaskState.INTERRUPTED and task.request.agent_mode == "answer":
            recover_after_ack = (ctx, task)
    finally:
        pulse_stop.set()
        await heartbeat
        if acknowledge:
            try:
                await application.queue.acknowledge(task_id, worker_id)
                if recover_after_ack is not None:
                    ctx, interrupted = recover_after_ack
                    await application.generation.recover_answer(ctx, interrupted)
            except ValueError:
                logging.warning("Task lease was lost before acknowledgement: %s", task_id)
    return True


async def run_forever(application: Application, lease_seconds: int, stop: Event) -> None:
    while not stop.is_set():
        try:
            await recover(application)
            break
        except Exception:
            logging.exception("Local task recovery failed; retrying")
            await asyncio.sleep(1)
    worker_id = uuid4().hex
    next_recovery = monotonic() + 30
    while not stop.is_set():
        try:
            if monotonic() >= next_recovery:
                await recover(application)
                next_recovery = monotonic() + 30
            if not await run_once(application, worker_id, lease_seconds):
                await asyncio.sleep(0.5)
        except Exception:
            logging.exception("Local task worker iteration failed")
            await asyncio.sleep(1)
