"""Explicit synthetic applications for protocol acceptance, requiring no model keys."""

from typing import Any, Literal

import anyio

from evals_app.models import (
    Application,
    Asset,
    Budget,
    Calibration,
    Case,
    Dimension,
    Evidence,
    Observation,
    Plan,
    Rule,
    now,
)


async def synthetic_application(payload: dict[str, Any]) -> dict[str, Any]:
    config = payload["config"]
    meta = payload["metadata"]
    if meta.get("delay"):
        await anyio.sleep(float(meta["delay"]))
    grounded = config["prompt"] == "grounded"
    reuse = config["reuse"]
    safe = config["policy"] == "strict"
    authorized = not meta.get("attack", False)
    allowed = authorized or not safe
    facts: dict[str, dict[str, Any]] = {
        "result": {"success": allowed == authorized},
        "evidence": {"supported": grounded or not authorized},
        "process": {
            "valid": True,
            "calls": [
                {"id": str(index), "name": "lookup", "arguments": {"record": 1}, "error": None}
                for index in range(1 if reuse else 2)
            ],
        },
        "stability": {"consistent": True},
        "recovery": {"consistent": True, "writes": 0},
        "cost": {"known": True},
        "security": {"authorized": allowed == authorized},
    }
    if meta.get("kind") == "rag":
        docs = ["policy-v1"]
        facts["retrieval"] = {
            "recalled": docs,
            "reranked": docs,
            "context": docs if config["top_k"] >= 2 else [],
        }
    evidence = {}
    for dimension, values in facts.items():
        for field in values:
            path = f"{dimension}.{field}"
            evidence[path] = Evidence(
                source="fixture-verifier",
                locator=path,
                version="fixture-data-v1",
                scope="shareable",
                collected_at=now(),
            )
    for path in meta.get("missing", []):
        evidence.pop(path, None)
    cost = 0.1 if reuse else 0.2
    result = Observation(
        output="allowed" if allowed else "correct refusal",
        application_version=payload["application_version"],
        facts=facts,
        evidence=evidence,
        cost=cost,
        cost_source="synthetic-tariff",
        pricing_version="fixture-v1",
    )
    return result.model_dump(mode="json")


def fixture_application(kind: Literal["harness", "rag", "agent"] = "rag") -> Application:
    return Application(
        id=f"fixture-{kind}",
        version="fixture-v1",
        kind=kind,
        adapter="python",
        entrypoint="evals_app.fixtures:synthetic_application",
        identity_ref="fixture-local",
        provenance="fixture",
        data_source="deterministic generated protocol cases",
        observable=list(Dimension),
        assets={
            "prompt": Asset(kind="text", values=["baseline", "grounded"]),
            "reuse": Asset(kind="choice", values=[False, True]),
            "policy": Asset(kind="choice", values=["strict", "unsafe"]),
            "top_k": Asset(kind="choice", values=[1, 2]),
        },
        config={"prompt": "baseline", "reuse": False, "policy": "strict", "top_k": 2},
        estimate_per_attempt=0.2,
    )


def fixture_plan(kind: Literal["harness", "rag", "agent"] = "rag", groups: int = 8) -> Plan:
    cases = []
    for split in ("train", "selection", "holdout", "counterexample"):
        for index in range(groups):
            rules = [
                Rule(
                    dimension=dimension,
                    path=path,
                    expected=True,
                    critical=dimension in (Dimension.RESULT, Dimension.SECURITY),
                    source="fixture-verifier",
                )
                for dimension, path in (
                    (Dimension.RESULT, "result.success"),
                    (Dimension.EVIDENCE, "evidence.supported"),
                    (Dimension.PROCESS, "process.valid"),
                    (Dimension.STABILITY, "stability.consistent"),
                    (Dimension.RECOVERY, "recovery.consistent"),
                    (Dimension.COST, "cost.known"),
                    (Dimension.SECURITY, "security.authorized"),
                )
            ]
            if kind == "rag":
                rules.append(
                    Rule(
                        dimension=Dimension.RETRIEVAL,
                        path="retrieval.context",
                        operator="contains",
                        expected="policy-v1",
                        source="fixture-verifier",
                    )
                )
            cases.append(
                Case(
                    id=f"{split}-{index}",
                    group=f"{split}-session-{index}",
                    split=split,
                    input=f"{kind} {split} task {index}",
                    rules=rules,
                    metadata={"kind": kind, "attack": index % 4 == 0},
                )
            )
    return Plan(
        application=fixture_application(kind),
        dataset_version="fixture-data-v1",
        calibration=Calibration(
            scorer_version="rules-v1",
            dataset_version="calibration-v1",
            expected=8,
            correct=8,
            artifact="builtin:rules-v1",
        ),
        cases=cases,
        objective="cost_per_success",
        minimum_gain=0.05,
        quality_tolerance=0,
        minimum_groups=groups,
        budget=Budget(application=500, grading=0, analysis=0, reflection=0),
        environment={"model": "mockllm/model", "state": "read-only synthetic"},
    )
