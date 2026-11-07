"""Use Scout's native LLM scanner and validation with a gate for each generation."""

import json
import math
import os
import secrets
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import anyio
from inspect_ai.model import (
    ChatMessageUser,
    GenerateConfig,
    Model,
    ModelOutput,
    ModelUsage,
    get_model,
)
from inspect_scout import Result, Scanner, Transcript, ValidationCase, ValidationSet, llm_scanner
from inspect_scout._validation.validate import validate

from evals_app.build import build_info
from evals_app.coordination import CallGate, atomic_json
from evals_app.models import SemanticAnalyzer, identifier


def actual_cost(output: ModelOutput, definition: SemanticAnalyzer) -> float | None:
    if output.model != definition.model_version or output.usage is None:
        return None
    usage = output.usage
    if any(
        value is not None and value < 0
        for value in (
            usage.input_tokens,
            usage.output_tokens,
            usage.input_tokens_cache_read,
            usage.input_tokens_cache_write,
        )
    ):
        return None
    if usage.total_cost is not None:
        return (
            usage.total_cost if math.isfinite(usage.total_cost) and usage.total_cost >= 0 else None
        )
    if definition.input_per_million is None or definition.output_per_million is None:
        return None
    components = [
        (usage.input_tokens, definition.input_per_million),
        (usage.output_tokens, definition.output_per_million),
        (usage.input_tokens_cache_read or 0, definition.cache_read_per_million),
        (usage.input_tokens_cache_write or 0, definition.cache_write_per_million),
    ]
    if any(tokens < 0 or (tokens and rate is None) for tokens, rate in components):
        return None
    return sum(tokens * (rate or 0) for tokens, rate in components) / 1_000_000


class GatedModel(Model):
    def __init__(self, original: Model, definition: SemanticAnalyzer, gate: CallGate) -> None:
        super().__init__(original.api, original.config, original.model_args)
        self.definition, self.gate = definition, gate
        self.sample_id = "unset"
        self.epoch = 1

    async def generate(self, *args: Any, **kwargs: Any) -> ModelOutput:
        reservation = await self.gate.reserve(
            identifier("judge_attempt"), self.sample_id, self.epoch
        )
        started = time.monotonic()
        cost, usage = None, None
        try:
            output = await super().generate(*args, **kwargs)
            cost = actual_cost(output, self.definition)
            usage = {
                "model": output.model,
                "tokens": output.usage.model_dump(mode="json") if output.usage else None,
                "cost_source": self.definition.cost_source,
                "pricing_version": self.definition.pricing_version,
            }
            if output.model != self.definition.model_version:
                raise ValueError("Judge served a model different from the frozen version")
            if cost is None:
                raise ValueError("Judge cost could not be verified")
            return output
        finally:
            await self.gate.settle(reservation, cost, time.monotonic() - started, usage=usage)


@contextmanager
def local_transport_environment() -> Iterator[None]:
    names = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    )
    previous = {name: os.environ[name] for name in names if name in os.environ}
    try:
        for name in names:
            os.environ.pop(name, None)
        os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"
        yield
    finally:
        for name in names:
            os.environ.pop(name, None)
        os.environ.update(previous)


def create_gated_model(definition: SemanticAnalyzer, coordination_dir: Path) -> GatedModel:
    model_args: dict[str, Any] = {}
    if definition.model.startswith(("openai/", "ollama/", "anthropic/")):
        # These SDKs retry independently of Inspect's GenerateConfig.
        model_args["max_retries"] = 0
    if definition.fixture_replies is not None:
        model_args["custom_outputs"] = [
            ModelOutput.from_content(
                definition.model_version,
                reply,
            ).model_copy(update={"usage": ModelUsage(total_cost=0)})
            for reply in definition.fixture_replies
        ]
    key = os.environ.get(definition.identity_ref) if definition.identity_ref else None
    if definition.identity_ref and not key:
        raise ValueError("Judge credential reference is unavailable")
    if (
        definition.deployment == "local"
        and not definition.model.startswith("mockllm/")
        and key is None
    ):
        key = "evals-local"
    # Construct the loopback client without proxies, then restore the application's environment.
    with local_transport_environment() if definition.deployment == "local" else nullcontext():
        original = get_model(
            definition.model,
            config=GenerateConfig(
                max_tokens=definition.max_tokens,
                max_retries=0,
                timeout=math.ceil(definition.timeout),
                temperature=0,
                cache=False,
            ),
            base_url=definition.base_url,
            api_key=key,
            memoize=False,
            **model_args,
        )
    if definition.deployment == "local" and not definition.model.startswith("mockllm/"):
        from inspect_ai.model._providers.openai import OpenAIAPI
        from inspect_ai.model._providers.openai_compatible import OpenAICompatibleAPI

        if not isinstance(original.api, (OpenAIAPI, OpenAICompatibleAPI)):
            raise ValueError("Local judge did not resolve to the frozen loopback provider")
        original.api.http_client.follow_redirects = False
    return GatedModel(original, definition, CallGate(coordination_dir))


def semantic_scanner(definition: SemanticAnalyzer, model: GatedModel) -> Scanner[Transcript]:
    return llm_scanner(
        question=definition.question
        + "\nReturn exactly yes, no, or unverifiable. Missing evidence is unverifiable.",
        answer="string",
        model=model,
        retry_refusals=False,
        nonce=secrets.token_hex(8),
        name="evals_semantic",
    )


async def scan_row(
    definition: SemanticAnalyzer,
    model: GatedModel,
    scanner: Scanner[Transcript],
    row: dict[str, Any],
    *,
    grading: bool = False,
) -> Result:
    text = row.get("text")
    if text is None:
        return Result(value="unverifiable", explanation="Authorized evidence is missing")
    if len(text) > definition.max_input_chars:
        raise ValueError("Semantic transcript exceeds its frozen input limit")
    if definition.deployment == "external" and row["scope"] != "shareable":
        raise ValueError("Local evidence cannot enter an external judge")
    model.sample_id, model.epoch = row.get("sample_id", row["id"]), row.get("epoch", 1)
    scanned = await scanner(
        Transcript(transcript_id=row["id"], messages=[ChatMessageUser(content=text)])
    )
    if not isinstance(scanned, Result):
        raise ValueError("Semantic scanner returned an unexpected result set")
    if scanned.value not in ("yes", "no", "unverifiable"):
        raise ValueError("Semantic answer is not one of the calibrated labels")
    if (scanned.value == "yes" or grading and scanned.value == "no") and not scanned.references:
        raise ValueError("A conclusive semantic grade or positive finding needs cited evidence")
    return scanned


async def run(job: dict[str, Any]) -> dict[str, Any]:
    if job["build"] != build_info():
        raise ValueError("Semantic worker differs from the frozen execution build")
    definition = SemanticAnalyzer.model_validate(job["definition"])
    model = create_gated_model(definition, Path(job["coordination_dir"]))
    scanner = semantic_scanner(definition, model)
    targets = [
        ValidationCase(id=row["id"], target=row["expected"])
        for row in job["rows"]
        if "expected" in row
    ]
    validation = ValidationSet(cases=targets)
    rows = []
    async with model:
        for row in job["rows"]:
            result = await scan_row(
                definition, model, scanner, row, grading=job.get("role") == "grading"
            )
            recorded = {
                "id": row["id"],
                "epoch": row.get("epoch", 1),
                "result": result.model_dump(mode="json"),
            }
            if "expected" in row:
                recorded["expected"] = row["expected"]
                recorded["correct"] = await validate(validation, result, target=row["expected"])
            rows.append(recorded)
            with Path(job["partial_path"]).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(recorded, ensure_ascii=False) + "\n")
    return {
        "rows": rows,
        "provenance": "fixture" if definition.model.startswith("mockllm/") else "model",
        "definition": job["definition"],
        "build": job["build"],
    }


def main() -> None:
    path = Path(sys.argv[1])
    try:
        result = anyio.run(run, json.loads(path.read_text("utf-8")))
    except Exception as error:
        result = {"error": type(error).__name__, "detail": str(error)}
    atomic_json(path.with_suffix(".result.json"), result)
    if "error" in result:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
