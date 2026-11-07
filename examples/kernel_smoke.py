"""Deterministic task for checking the preserved Inspect CLI after installation."""

from inspect_ai import Task, task
from inspect_ai.dataset import Sample
from inspect_ai.scorer import match
from inspect_ai.solver import generate


@task
def kernel_smoke():
    return Task(
        dataset=[
            Sample(input="Return the mock response", target="Default output from mockllm/model")
        ],
        solver=generate(),
        scorer=match(),
    )
