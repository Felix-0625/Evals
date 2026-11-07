"""Frozen application contracts and evaluation plans."""

import hashlib
import ipaddress
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def fingerprint(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class Dimension(StrEnum):
    RESULT = "result"
    RETRIEVAL = "retrieval"
    EVIDENCE = "evidence"
    PROCESS = "process"
    STABILITY = "stability"
    RECOVERY = "recovery"
    COST = "cost"
    SECURITY = "security"


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNVERIFIABLE = "unverifiable"
    GRADING_ERROR = "grading_error"
    NOT_RUN = "not_run"
    NOT_APPLICABLE = "not_applicable"


class Asset(Contract):
    kind: Literal["text", "choice"]
    values: list[str | int | float | bool] = Field(min_length=1)
    allow_generated: bool = False
    max_length: int = Field(default=20000, ge=1, le=100000)

    @model_validator(mode="after")
    def valid_asset(self) -> Self:
        if self.kind == "text" and any(not isinstance(v, str) for v in self.values):
            raise ValueError("Text assets require string seeds")
        if self.kind != "text" and self.allow_generated:
            raise ValueError("Only text assets may accept generated values")
        return self


class Application(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    version: str = Field(min_length=1)
    kind: Literal["harness", "rag", "agent"]
    adapter: Literal["python", "http", "cli"]
    entrypoint: str = Field(min_length=1)
    identity_ref: str = Field(min_length=1)
    executor: str = "local"
    provenance: Literal["fixture", "real"]
    data_source: str = Field(min_length=1)
    observable: list[Dimension]
    assets: dict[str, Asset]
    config: dict[str, str | int | float | bool]
    timeout: float = Field(default=60, gt=0, le=3600)
    read_only: bool = True
    reset_entrypoint: str | None = None
    remote_cancel: bool = False
    evidence_scope: Literal["local", "shareable"] = "local"
    estimate_per_attempt: float = Field(ge=0)

    @model_validator(mode="after")
    def valid_config(self) -> Self:
        if not self.read_only and not self.reset_entrypoint:
            raise ValueError("Write tasks require an authorized reset entrypoint")
        if set(self.config) != set(self.assets):
            raise ValueError("Every configuration asset must be declared")
        self.validate_changes(self.config)
        return self

    def validate_changes(self, changes: dict[str, Any]) -> None:
        for name, value in changes.items():
            asset = self.assets.get(name)
            generated = (
                asset is not None
                and asset.kind == "text"
                and asset.allow_generated
                and isinstance(value, str)
                and 0 < len(value) <= asset.max_length
            )
            declared = asset is not None and any(
                type(value) is type(v) and value == v for v in asset.values
            )
            if not generated and not declared:
                raise ValueError(f"Undeclared asset or value: {name}")


class Rule(Contract):
    dimension: Dimension
    path: str = Field(min_length=1)
    operator: Literal["eq", "contains", "subset", "lte", "gte", "semantic"] = "eq"
    expected: Any
    critical: bool = False
    source: str = Field(min_length=1)

    @model_validator(mode="after")
    def semantic_label(self) -> Self:
        if self.operator == "semantic" and (
            self.expected not in ("yes", "no")
            or self.critical
            or self.dimension == Dimension.SECURITY
        ):
            raise ValueError(
                "Semantic rules need a yes/no label and cannot establish hard constraints"
            )
        return self


class Case(Contract):
    id: str = Field(min_length=1)
    group: str = Field(min_length=1)
    split: Literal["train", "selection", "holdout", "counterexample"]
    input: str
    rules: list[Rule]
    metadata: dict[str, Any] = Field(default_factory=dict)


class Calibration(Contract):
    scorer_version: str
    dataset_version: str
    expected: int = Field(gt=0)
    correct: int = Field(ge=0)
    minimum_accuracy: float = Field(default=1, gt=0, le=1)
    artifact: str = Field(min_length=1)

    @model_validator(mode="after")
    def calibrated(self) -> Self:
        if self.correct > self.expected or self.correct / self.expected < self.minimum_accuracy:
            raise ValueError("Scorer calibration did not meet the declared threshold")
        return self


class Budget(Contract):
    application: float = Field(ge=0)
    grading: float = Field(ge=0)
    analysis: float = Field(ge=0)
    reflection: float = Field(ge=0)


class SemanticAnalyzer(Contract):
    id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    question: str = Field(min_length=1, max_length=12000)
    evidence_paths: list[str] = Field(min_length=1)
    model: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    deployment: Literal["local", "external"] = "local"
    base_url: str | None = None
    identity_ref: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    estimate_per_call: float = Field(ge=0)
    pricing_version: str = Field(min_length=1)
    cost_source: str = Field(min_length=1)
    input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)
    cache_read_per_million: float | None = Field(default=None, ge=0)
    cache_write_per_million: float | None = Field(default=None, ge=0)
    timeout: float = Field(default=60, gt=0, le=600)
    max_tokens: int = Field(default=512, ge=32, le=8000)
    max_input_chars: int = Field(default=12000, ge=100, le=100000)
    calibration_id: str = Field(min_length=1)
    calibration_dataset_version: str = Field(min_length=1)
    minimum_accuracy: float = Field(default=1, gt=0, le=1)
    minimum_coverage: float = Field(default=1, gt=0, le=1)
    fixture_replies: list[str] | None = None

    @model_validator(mode="after")
    def access_boundary(self) -> Self:
        if len(set(self.evidence_paths)) != len(self.evidence_paths):
            raise ValueError("Semantic evidence paths must be unique")
        parsed = urlsplit(self.base_url or "")
        if self.base_url and (
            parsed.username or parsed.password or parsed.query or parsed.fragment
        ):
            raise ValueError("Judge endpoints must not contain credentials, query or fragment")
        if self.model.startswith("mockllm/"):
            if self.deployment != "local" or self.base_url:
                raise ValueError("Mock judge is a local fixture only")
        elif self.deployment == "local":
            try:
                local = (
                    parsed.hostname == "localhost"
                    or ipaddress.ip_address(parsed.hostname or "").is_loopback
                )
            except ValueError:
                local = False
            if (
                not local
                or parsed.scheme not in {"http", "https"}
                or not self.model.startswith(("ollama/", "openai/"))
            ):
                raise ValueError(
                    "Local judge requires an explicit loopback Ollama/OpenAI-compatible endpoint"
                )
        elif (
            not self.identity_ref
            or self.estimate_per_call <= 0
            or (self.base_url and parsed.scheme != "https")
        ):
            raise ValueError(
                "External judge requires a credential reference, positive estimate and HTTPS"
            )
        if self.fixture_replies is not None and not self.model.startswith("mockllm/"):
            raise ValueError("Scripted judge replies are only available for mock fixtures")
        return self


class Plan(Contract):
    application: Application
    dataset_version: str = Field(min_length=1)
    scorer_version: Literal["rules-v1"] = "rules-v1"
    calibration: Calibration
    cases: list[Case] = Field(min_length=4)
    epochs: int = Field(default=2, ge=1, le=20)
    objective: Literal["quality", "cost_per_success", "latency_p95"]
    minimum_gain: float = Field(ge=0)
    quality_tolerance: float = Field(ge=0, le=1)
    minimum_groups: int = Field(default=8, ge=2)
    minimum_coverage: float = Field(default=1, gt=0, le=1)
    bootstrap_samples: int = Field(default=2000, ge=500)
    confidence: float = Field(default=0.95, gt=0.5, lt=1)
    multiple_comparison: Literal["selection_then_single_holdout"] = "selection_then_single_holdout"
    hard_dimensions: list[Dimension] = Field(default_factory=lambda: [Dimension.SECURITY])
    budget: Budget
    platform_retries: int = Field(default=0, ge=0, le=3)
    retryable: list[str] = Field(default_factory=lambda: ["TimeoutError", "ConnectError"])
    environment: dict[str, str] = Field(min_length=1)
    adapter_version: Literal["adapter-v1"] = "adapter-v1"
    analyzer_version: Literal["scout-rules-v1"] = "scout-rules-v1"
    seed: int = 42
    text_proposer: str | None = None
    reflection_estimate: float = Field(default=0, ge=0)
    search_metric_calls: int = Field(default=32, ge=1)
    semantic_analyzer: SemanticAnalyzer | None = None
    semantic_grader: SemanticAnalyzer | None = None
    log_retention: Literal["retain_all"] = "retain_all"

    @model_validator(mode="after")
    def isolated(self) -> Self:
        if self.calibration.scorer_version != self.scorer_version:
            raise ValueError("Calibration scorer version does not match")
        definitions = [item for item in (self.semantic_analyzer, self.semantic_grader) if item]
        if (
            any(item.deployment == "external" for item in definitions)
            and self.application.evidence_scope != "shareable"
        ):
            raise ValueError(
                "Application must authorize external semantic evidence before calibration"
            )
        if len({item.calibration_id for item in definitions}) != len(definitions):
            raise ValueError("Semantic grading and analysis require distinct calibration IDs")
        ids: set[str] = set()
        groups: dict[str, str] = {}
        contents: dict[str, str] = {}
        splits: set[str] = set()
        for case in self.cases:
            if case.id in ids:
                raise ValueError("Duplicate sample id")
            ids.add(case.id)
            splits.add(case.split)
            for key, mapping in ((case.group, groups), (fingerprint(case.input), contents)):
                if key in mapping and mapping[key] != case.split:
                    raise ValueError("Related or identical samples cross data partitions")
                mapping[key] = case.split
            if len({r.dimension for r in case.rules}) != len(case.rules):
                raise ValueError("Use one composite business assertion per dimension")
            semantic_rules = [rule for rule in case.rules if rule.operator == "semantic"]
            if len(semantic_rules) > 1:
                raise ValueError("A case can use one frozen semantic question")
            for rule in semantic_rules:
                if (
                    self.semantic_grader is None
                    or rule.path not in self.semantic_grader.evidence_paths
                    or rule.dimension in self.hard_dimensions
                ):
                    raise ValueError(
                        "Semantic rules require a frozen grader and a non-hard evidence path"
                    )
        if splits != {"train", "selection", "holdout", "counterexample"}:
            raise ValueError("Train, selection, holdout and counterexample partitions are required")
        return self


class Evidence(Contract):
    source: str = Field(min_length=1, pattern=r"\S")
    locator: str = Field(min_length=1, pattern=r"\S")
    version: str = Field(min_length=1, pattern=r"\S")
    scope: Literal["local", "shareable"]
    collected_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def dated_reference(self) -> Self:
        collected = datetime.fromisoformat(self.collected_at)
        if collected.tzinfo is None:
            raise ValueError("Evidence collection time requires a timezone")
        return self


class Observation(Contract):
    output: str
    application_version: str
    facts: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    cost: float | None = Field(default=None, ge=0)
    cost_source: str | None = None
    pricing_version: str | None = None
    latency: float | None = Field(default=None, ge=0)
    error: str | None = None

    @model_validator(mode="after")
    def usage_provenance(self) -> Self:
        if self.cost is not None and (not self.cost_source or not self.pricing_version):
            raise ValueError("Known cost requires a source and pricing version")
        return self


class ExecutionPlan(Contract):
    application: Application
    epochs: int = Field(ge=1)
    platform_retries: int = Field(ge=0)
    retryable: list[str]
    semantic_grader: SemanticAnalyzer | None = None


class Judgment(Contract):
    dimension: Dimension
    status: Status
    reason: str
    scorer_version: str = "rules-v1"
    evidence: list[Evidence] = Field(default_factory=list)
    critical: bool = False
    value: float | None = None


class Trial(Contract):
    sample_id: str
    group: str
    epoch: int
    attempt_id: str
    candidate_id: str
    run_id: str
    config_fingerprint: str
    application_version: str
    observation: Observation | None
    judgments: list[Judgment]
    attempts: list[dict[str, Any]]
    log: str


class Proposal(Contract):
    changes: dict[str, str | int | float | bool]
    source: Literal["manual", "application_rsi", "config", "gepa"]
    hypothesis: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    expected_baseline: str
    parent_id: str = "baseline"
