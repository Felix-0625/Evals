"""Deterministic Scout analysis of permitted development observations."""

import json
from collections.abc import Mapping
from typing import Any

from inspect_scout import Result, Scanner, Transcript, scanner


def call_identity(call: Any) -> str | None:
    """Compare actual tool inputs; event IDs and results do not identify inputs."""
    if not isinstance(call, dict) or not call.get("name") or "arguments" not in call:
        return None
    return json.dumps([call["name"], call["arguments"]], sort_keys=True)


def _document_ids(value: Any) -> list[str | int] | None:
    if isinstance(value, list) and all(type(item) in (str, int) for item in value):
        return list(dict.fromkeys(value))
    return None


def _retrieval_findings(retrieval: dict[str, Any]) -> list[dict[str, Any]]:
    recalled = _document_ids(retrieval.get("recalled"))
    context = _document_ids(retrieval.get("context"))
    required = _document_ids(retrieval.get("required"))
    if not recalled or context is None or (required is None and context):
        return []
    missing = [
        item for item in recalled if item not in context and (required is None or item in required)
    ]
    if not missing:
        return []
    related = ["retrieval.recalled"]
    if required is not None:
        related.append("retrieval.required")
    reranked = _document_ids(retrieval.get("reranked"))
    if reranked is None:
        return [
            {
                "fact": "Retrieved documents did not enter final context",
                "hypothesis": "Reranking or context budget may have removed evidence",
                "test": "Expose the intermediate retrieval stages before changing their limits",
                "path": "retrieval.context",
                "related_paths": related,
                "stage": "unobserved",
                "document_ids": missing,
            }
        ]
    result = []
    rerank_losses = [item for item in missing if item not in reranked]
    if rerank_losses:
        result.append(
            {
                "fact": "Retrieved documents are absent after reranking and in final context",
                "hypothesis": "Reranking scores or limits may have excluded the documents",
                "test": "Compare reranking scores and limits on labeled development cases",
                "path": "retrieval.reranked",
                "related_paths": [*related, "retrieval.context"],
                "stage": "rerank",
                "document_ids": rerank_losses,
            }
        )
    context_losses = [item for item in missing if item in reranked]
    if context_losses:
        result.append(
            {
                "fact": "Reranked documents are absent in final context",
                "hypothesis": "Context deduplication or limits may have removed the documents",
                "test": "Compare context assembly inputs and limits on labeled development cases",
                "path": "retrieval.context",
                "related_paths": [*related, "retrieval.reranked"],
                "stage": "context",
                "document_ids": context_losses,
            }
        )
    return result


def findings(observation: dict[str, Any] | None) -> list[dict[str, Any]]:
    if observation is None:
        return [
            {
                "fact": "Application produced no verifiable observation",
                "hypothesis": "Execution or environment failed",
                "test": "Inspect the original attempt error before changing application assets",
                "path": "attempts",
            }
        ]
    facts = observation.get("facts", {})
    retrieval = facts.get("retrieval")
    result = _retrieval_findings(retrieval) if isinstance(retrieval, dict) else []
    process = facts.get("process", {})
    calls = process.get("calls", [])
    seen: dict[str, int] = {}
    for index, call in enumerate(calls):
        identity = call_identity(call)
        if identity is None:
            continue
        previous = seen.get(identity)
        if previous is not None:
            result.append(
                {
                    "fact": "Tool name and arguments match an earlier call",
                    "hypothesis": "The repeat may be avoidable if state and data have not changed",
                    "test": "Check intervening state changes before testing result reuse",
                    "path": f"process.calls.{index}",
                    "related_paths": [f"process.calls.{previous}"],
                }
            )
            if calls[previous].get("error"):
                result.append(
                    {
                        "fact": "A failed tool call was retried with unchanged arguments",
                        "hypothesis": "Recovery may ignore a non-transient failure",
                        "test": (
                            "Inject transient and persistent errors; check retry policy and state"
                        ),
                        "path": f"process.calls.{index}",
                        "related_paths": [f"process.calls.{previous}.error"],
                    }
                )
        seen[identity] = index
    if process.get("terminated") is True and process.get("required_steps_complete") is False:
        result.append(
            {
                "fact": "Execution terminated with required steps still incomplete",
                "hypothesis": "The stopping rule may accept an incomplete business state",
                "test": "Check the stopping condition against the required business steps",
                "path": "process.required_steps_complete",
            }
        )
    if facts.get("evidence", {}).get("required") is True and not facts["evidence"].get("citations"):
        result.append(
            {
                "fact": "Required citations are absent from the exposed answer evidence",
                "hypothesis": "The answer path may omit its grounding references",
                "test": "Compare answer citations with the final authorized source context",
                "path": "evidence.citations",
            }
        )
    if not facts.get("evidence", {}).get("supported", True):
        result.append(
            {
                "fact": "Business verifier reports unsupported citation",
                "hypothesis": "Answer generation ignores source grounding",
                "test": "Test prompt grounding on supporting and conflicting sources",
                "path": "evidence.supported",
            }
        )
    return result


@scanner(messages="all", metadata=True, name="evals_business_findings")
def business_scanner() -> Scanner[Result]:
    async def scan(transcript: Transcript) -> Result:
        metadata = transcript.metadata or {}
        # Live sample transcripts flatten this field; saved eval logs wrap sample metadata.
        sample_metadata = metadata.get("sample_metadata", metadata)
        detected = (
            findings(sample_metadata["evals_observation"])
            if isinstance(sample_metadata, Mapping) and "evals_observation" in sample_metadata
            else []
        )
        return Result(value=bool(detected), explanation=json.dumps(detected, ensure_ascii=False))

    return scan
