"""Versioned search policies; business rubrics, budgets and permissions remain in Plan."""

from typing import Any

from pydantic import Field

from evals_app.models import Application, Contract, fingerprint, identifier
from evals_app.store import Conflict


class SearchPolicy(Contract):
    version: str = Field(min_length=1, pattern=r"\S")
    use_gepa: bool = Field(default=True, strict=True)
    configuration_candidates: bool = Field(default=False, strict=True)


BUILTIN_POLICY = SearchPolicy(version="builtin-search-v1")
SIMPLE_POLICY = SearchPolicy(
    version="simple-rule-v1", use_gepa=False, configuration_candidates=True
)


def default_policy(state: dict[str, Any], application_id: str | None = None) -> SearchPolicy:
    record = state.get("default_strategies", {}).get(application_id)
    return SearchPolicy.model_validate(record["policy"]) if record else BUILTIN_POLICY


def register_policy(state: dict[str, Any], policy: SearchPolicy) -> None:
    definitions = {
        item.version: item.model_dump(mode="json") for item in (BUILTIN_POLICY, SIMPLE_POLICY)
    }
    definitions.update(state.get("search_policies", {}))
    definition = policy.model_dump(mode="json")
    if policy.version in definitions and definitions[policy.version] != definition:
        raise Conflict("Search policy versions are immutable; choose a new version")
    state.setdefault("search_policies", {})[policy.version] = definition


def resolve_policy(
    state: dict[str, Any],
    policy: SearchPolicy | None,
    use_gepa: bool | None,
    application_id: str | None = None,
) -> SearchPolicy:
    if policy is not None:
        if use_gepa is not None and use_gepa != policy.use_gepa:
            raise Conflict("Explicit search flag conflicts with the versioned policy")
        return policy
    if use_gepa is None:
        return default_policy(state, application_id)
    return SearchPolicy(version=f"explicit-gepa-{int(use_gepa)}", use_gepa=use_gepa)


def configuration_candidates(application: Application, candidates: dict[str, Any]) -> list[str]:
    existing = {fingerprint(candidate["config"]) for candidate in candidates.values()}
    added = []
    for name, asset in application.assets.items():
        if asset.kind != "choice":
            continue
        for value in asset.values:
            config = {**application.config, name: value}
            if fingerprint(config) in existing:
                continue
            candidate_id = identifier("candidate")
            candidates[candidate_id] = {
                "id": candidate_id,
                "config": config,
                "changes": {name: value},
                "source": "config",
                "parent_id": "baseline",
                "expected_baseline": application.version,
                "hypothesis": f"Evaluate the declared alternative for {name}",
                "risk": "Changes to evidence coverage, quality or business constraints",
            }
            existing.add(fingerprint(config))
            added.append(candidate_id)
    return added
