"""Version adoption and rollback receipts, separate from experiment execution."""

from typing import TYPE_CHECKING, Any

from evals_app.models import fingerprint, identifier, now
from evals_app.store import Conflict

if TYPE_CHECKING:
    from evals_app.service import Service


def operation(
    service: "Service",
    experiment_id: str,
    candidate_id: str,
    expected_baseline: str,
    requested_version: str,
    *,
    key: str,
    rollback: str | None = None,
) -> Any:
    """Prepare a version change; execution remains the target application responsibility."""

    def update(state: dict[str, Any]) -> Any:
        experiment = state["experiments"][experiment_id]
        app = state["applications"][experiment["application_id"]]
        if app.get("status", "active") != "active":
            raise Conflict("Application effective state requires reconciliation")
        if app["version"] != expected_baseline:
            raise Conflict("Target application baseline changed")
        if requested_version == expected_baseline or not requested_version:
            raise ValueError("Requested version must identify a new effective version")
        if any(
            o["application_id"] == experiment["application_id"] and o["status"] == "pending"
            for o in state["operations"].values()
        ):
            raise Conflict("Application already has a pending version operation")
        if rollback:
            previous = state["operations"][rollback]
            if (
                previous["application_id"] != experiment["application_id"]
                or previous["status"] != "applied"
                or previous["requested_version"] != expected_baseline
                or previous["expected_baseline"] != requested_version
            ):
                raise Conflict("Rollback target does not match the active operation")
            config = previous["previous_config"]
        else:
            report = experiment["reports"].get(candidate_id)
            if (
                not report
                or report["decision"] != "recommend"
                or experiment["status"] != "completed"
            ):
                raise ValueError("Only an independently recommended candidate may be adopted")
            if (
                report["baseline"] != expected_baseline
                or experiment["plan"]["application"]["config"] != app["config"]
            ):
                raise Conflict("Candidate must be revalidated against the current baseline")
            config = experiment["candidates"][candidate_id]["config"]
        operation_id = identifier("operation")
        record = {
            "id": operation_id,
            "experiment_id": experiment_id,
            "candidate_id": candidate_id,
            "application_id": experiment["application_id"],
            "expected_baseline": expected_baseline,
            "requested_version": requested_version,
            "config": config,
            "config_fingerprint": fingerprint(config),
            "previous_config": app["config"],
            "executor": experiment["plan"]["application"]["executor"],
            "status": "pending",
            "rollback_of": rollback,
            "created_at": now(),
            "validation": experiment["reports"].get(candidate_id),
        }
        state["operations"][operation_id] = record
        return record

    return service.store.update(
        update,
        key=f"operation:{key}",
        payload={
            "experiment": experiment_id,
            "candidate": candidate_id,
            "expected": expected_baseline,
            "requested": requested_version,
            "rollback": rollback,
        },
    )


def receipt(
    service: "Service",
    operation_id: str,
    *,
    executor: str,
    actual_version: str,
    config_fingerprint: str,
    status: str,
    evidence: dict[str, Any],
    key: str,
) -> Any:
    """Accept one terminal receipt and reconcile effective version and configuration."""

    def update(state: dict[str, Any]) -> Any:
        operation = state["operations"][operation_id]
        app = state["applications"][operation["application_id"]]
        if executor != operation["executor"]:
            raise PermissionError("Receipt executor is not authorized for this application")
        if operation["status"] != "pending":
            raise Conflict("Operation has already received its terminal receipt")
        if app["version"] != operation["expected_baseline"]:
            raise Conflict("Application version changed before receipt")
        if status not in {
            "applied",
            "partial_failure",
            "failed",
            "cancel_pending_confirmation",
        }:
            raise ValueError("Unsupported receipt state")
        if not all(evidence.get(k) for k in ("source", "locator", "collected_at")):
            raise ValueError("Receipt needs effective-state verification evidence")
        if status == "applied":
            if (
                actual_version != operation["requested_version"]
                or config_fingerprint != operation["config_fingerprint"]
            ):
                raise Conflict(
                    "Effective version or configuration does not match the requested change"
                )
            app.update(
                version=actual_version, config=operation["config"], revision=app["revision"] + 1
            )
            app["declaration"].update(version=actual_version, config=operation["config"])
            app["status"] = "active"
        elif status in {"partial_failure", "cancel_pending_confirmation"}:
            app["status"] = "reconciliation_required"
        elif actual_version != operation["expected_baseline"] or config_fingerprint != fingerprint(
            operation["previous_config"]
        ):
            app["status"] = "reconciliation_required"
        operation.update(
            status=status,
            actual_version=actual_version,
            evidence=evidence,
            actual_config_fingerprint=config_fingerprint,
            applied_at=now(),
        )
        return operation

    return service.store.update(
        update,
        key=f"receipt:{operation_id}:{key}",
        payload={
            "executor": executor,
            "actual_version": actual_version,
            "config": config_fingerprint,
            "status": status,
            "evidence": evidence,
        },
    )
