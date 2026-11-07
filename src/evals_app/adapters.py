"""Trusted local Python, HTTP and executable application adapters."""

import importlib
import inspect
import json
import os
import shlex
import time
from typing import Any

import anyio
import httpx

from evals_app.coordination import CallDenied, CallGate
from evals_app.models import Application, Case, Observation, identifier, now


def load_function(entrypoint: str) -> Any:
    module, name = entrypoint.rsplit(":", 1)
    function = getattr(importlib.import_module(module), name)
    if not callable(function):
        raise ValueError("Entrypoint is not callable")
    return function


async def python_call(entrypoint: str, payload: dict[str, Any]) -> Any:
    function = load_function(entrypoint)
    if inspect.iscoroutinefunction(function):
        return await function(payload)
    return await anyio.to_thread.run_sync(function, payload, abandon_on_cancel=True)


def command_arguments(entrypoint: str) -> list[str]:
    if entrypoint.lstrip().startswith("["):
        args = json.loads(entrypoint)
        if not isinstance(args, list) or not args or any(not isinstance(a, str) for a in args):
            raise ValueError("CLI entrypoint must be a nonempty JSON string array")
        return args
    args = shlex.split(entrypoint, posix=os.name != "nt")
    if os.name == "nt":
        args = [a[1:-1] if len(a) >= 2 and a[0] == a[-1] and a[0] in "\"'" else a for a in args]
    if not args:
        raise ValueError("CLI entrypoint is empty")
    return args


async def invoke(
    application: Application, case: Case, config: dict[str, Any], context: dict[str, Any]
) -> Observation:
    payload = {
        "input": case.input,
        "metadata": case.metadata,
        "config": config,
        "context": context,
        "identity_ref": application.identity_ref,
        "application_version": application.version,
    }
    start = time.monotonic()
    with anyio.fail_after(application.timeout):
        if application.reset_entrypoint:
            await python_call(application.reset_entrypoint, payload)
        if application.adapter == "python":
            result = await python_call(application.entrypoint, payload)
        elif application.adapter == "http":
            token = os.environ.get(application.identity_ref)
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            # Application endpoints must use the frozen address without ambient proxy routing.
            async with httpx.AsyncClient(timeout=application.timeout, trust_env=False) as client:
                response = await client.post(application.entrypoint, json=payload, headers=headers)
                response.raise_for_status()
                result = response.json()
        else:
            args = command_arguments(application.entrypoint)
            process = await anyio.run_process(args, input=json.dumps(payload).encode(), check=True)
            result = json.loads(process.stdout)
    observation = Observation.model_validate(result)
    if observation.application_version != application.version:
        raise ValueError("Application version differs from the frozen execution contract")
    return observation.model_copy(update={"latency": time.monotonic() - start})


async def execute(
    application: Application,
    case: Case,
    config: dict[str, Any],
    context: dict[str, Any],
    retries: int,
    retryable: list[str],
    gate: CallGate | None = None,
) -> dict[str, Any]:
    attempts = []
    observation = None
    blocked = None
    for index in range(retries + 1):
        attempt_id = identifier("attempt")
        reservation_id = None
        if gate:
            try:
                reservation_id = await gate.reserve(attempt_id, case.id, context["epoch"])
            except CallDenied as error:
                blocked = str(error)
                break
        start = time.monotonic()
        started_at = now()
        try:
            observation = await invoke(
                application, case, config, {**context, "attempt_id": attempt_id, "attempt": index}
            )
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "started_at": started_at,
                    "finished_at": now(),
                    "cost": observation.cost,
                    "cost_source": observation.cost_source,
                    "pricing_version": observation.pricing_version,
                    "latency": time.monotonic() - start,
                    "error": observation.error,
                }
            )
        except Exception as error:
            attempts.append(
                {
                    "attempt_id": attempt_id,
                    "started_at": started_at,
                    "finished_at": now(),
                    "cost": None,
                    "latency": time.monotonic() - start,
                    "error": type(error).__name__,
                    "detail": str(error),
                }
            )
        if gate and reservation_id:
            await gate.settle(
                reservation_id, observation.cost if observation else None, time.monotonic() - start
            )
        # A missing HTTP response cannot distinguish a failed write from a committed write.
        if (
            observation is not None
            or attempts[-1]["error"] not in retryable
            or (application.adapter == "http" and not application.read_only)
        ):
            break
    return {
        "observation": observation.model_dump(mode="json") if observation else None,
        "attempts": attempts,
        "blocked_reason": blocked,
    }
